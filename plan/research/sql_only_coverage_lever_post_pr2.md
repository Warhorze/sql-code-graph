# SQL-only coverage lever — post-PR-2, with PR-3 in flight

**Date:** 2026-06-13
**Mode:** READ-ONLY analysis. No source changed, DWH repo untouched, indexer NOT run.
All measurements reuse the existing A/B databases (a PR-3 dev is mid live-index; concurrent
indexing OOMs the 3.8 GB box).
**Measurement DBs (read-only):**
- `/tmp/ab_pr2.duckdb` — current master lineage (post-PR-2 CTE-body source descent), indexed
  sha `fdf1b551`, 1,335 files, schema v8.
- `/tmp/ab_master.duckdb` — pre-PR-2 baseline (for the before/after frame).

**Edge-type discipline (the trap that has burned this project):** every count below names
its edge type. **SELECTS_FROM = table-level**, **COLUMN_LINEAGE = column-level**, **HAS_COLUMN =
catalog**. A COLUMN_LINEAGE count is NEVER cross-applied to a SELECTS_FROM baseline. Where
COLUMN_LINEAGE is used to reason about a table-level gap, it is used only as a *probe* ("the
engine already resolved this source at the column level, therefore a table-level fix can
surface it") and the projected edge is converted to a SELECTS_FROM edge before any island
recount. Every number is tagged **[measured]** (a query ran) or **[inferred]** (reasoned).

---

## 0. Recommendation (read this first)

**The single highest-value next SQL-only lever is E8 revival (the temp-chain `dynamic_source`
multi-key fix), a COLUMN_LINEAGE-recall lever.** It is the only candidate with a four-figure
measured edge gain (**+1,728 COLUMN_LINEAGE edges [measured]** on the same corpus, from the
prior A/B), it now has a falsifiable gate that did not exist when it was parked
(`resolvable_write_col_edges`, shipped v1.27.0), and it is off the perf hot-path. Everything
else is a long tail.

**The island gap is essentially irreducible by SQL means.** This is the adversarial result the
charter asked for, and the data backs it: of the 97 residual table-level islands, **only ~10
members across ~10 islands are recoverable [measured]**, and they are a heterogeneous tail (a
self-join, a derived subquery already owned by PR-3, two nested/COMMENT-clause views, a UNION
alias) — not a single coherent lever. **87 of 97 islands are genuinely dead** (reference-only
out-of-corpus tables, `LIKE`/literal-only producers, or `_bck` noise). Chasing them is low
reward and high per-case effort.

**The 84% strict-health "16% gap" is mostly a mirage.** Of the 8,720 strict-bad COLUMN_LINEAGE
edges, **8,125 (93%) have a CTE/derived/temp destination [measured]** — correct-by-design
intermediate nodes that the *scoped* metric (98%) already excludes. Only **595 strict-bad edges
land on a real table** [measured]. The headline 84% is not a coverage failure; it is the
structural cost of tracking CTE/derived columns, which is desirable. Do not spend a PR trying
to move it.

Ranked table in §6. Gate for the #1 lever in §3.

---

## 1. Baseline reconciliation (all [measured] on `/tmp/ab_pr2.duckdb`)

| Metric | Value | Edge type |
|---|---|---|
| COLUMN_LINEAGE total | 54,338 | COLUMN_LINEAGE |
| SELECTS_FROM total | 5,344 | SELECTS_FROM |
| HAS_COLUMN total | 142,183 | catalog |
| SqlTable | 6,574 | node |
| SqlQuery | 5,186 | node |
| File | 1,335 | node |
| strict health | 45,618 / 54,338 = **84.0%** | COLUMN_LINEAGE |
| `cte_source_gap_writes` | **110** | COLUMN_LINEAGE ≥1 AND SELECTS_FROM = 0 |
| `resolvable_write_col_edges` | **33,475** | COLUMN_LINEAGE (write-kind, source-resolved) |
| `rescuable_unqualified_edges` | **86** | COLUMN_LINEAGE strict-bad |
| degraded-parse queries | **20 / 5,186** | SqlQuery |
| zero-edge write queries | 1,090 / 2,349 | SqlQuery |

All match the shepherd's stated figures (84% strict, gap=110). **The `resolvable_write_col_edges`
baseline on THIS db is 33,475, not the 25,246 quoted in the E8 gate comment** — that 25,246 is
the `/tmp/e8_without.duckdb` proxy from a *different* config/catalog run. See §3 for why the
gate threshold (+≥1,000) is corpus-relative and still valid.

### Before/after PR-2 (table-level, [measured])

| | pre-PR-2 (`ab_master`) | post-PR-2 (`ab_pr2`) | Δ |
|---|---|---|---|
| SELECTS_FROM-WCC small islands | 147 | **97** | **−50** |
| giant component | 1,625 | 1,826 | +201 |
| SELECTS_FROM edges | 4,871 | 5,344 | +473 |
| COLUMN_LINEAGE edges | 53,571 | 54,338 | +767 |

PR-2 (the CTE-body source descent in [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604))
landed almost exactly the −48/+299 projection the prior
[`table_level_island_lever.md`](table_level_island_lever.md) made. **That lever is shipped and
spent.** This doc asks: what is left.

---

## 2. The 97 islands — every one classified [measured]

**Method.** Build the table-level DiGraph (`SELECTS_FROM.dst_key → SqlQuery.target_table`,
self-edges dropped, noise filter `_bck` regex + `ma.rtetl_delta` applied to both endpoints —
identical to [`dwh_graph_analysis.md`](../reports/dwh_graph_analysis.md)). Result [measured]:
`nodes=2076 edges=3368`, WCC histogram `{2:66, 3:17, 4:9, 5:2, 6:1, 7:1, 8:1, 1826:1}` →
**97 small islands, 250 members, 1 giant (1,826)**.

A member can only disconnect its island if it is a **root** (zero incoming SELECTS_FROM edge);
the other members trail behind a root. Classify the roots:

| Class | Count | Meaning |
|---|---|---|
| Island members total | 250 | — |
| …roots (zero incoming SELECTS_FROM) | **101** | the disconnection points |
| ……root IS a resolved `target_table` (a producer exists) | **47** | a write produces it in-corpus |
| ……root NEVER a target (reference-only) | **54** | read but never produced in-corpus → **dead** |

Of the 47 has-producer roots, why is the SELECTS_FROM **source** missing?

| Bucket | Count | Mechanism [measured] |
|---|---|---|
| A — producer has ZERO SELECTS_FROM source rows | **43** | source side never resolved to any table node |
| B — all producer sources noise-filtered (`_bck`) | **4** | correctly excluded → **dead** |
| C — live non-noise source exists but dropped | **0** | no graph-build bug |

Drill into bucket A: does the producer's COLUMN_LINEAGE already resolve a **real** (non
cte/derived/temp, non-noise) source table? If yes, a table-level fix could surface it →
**recoverable**. If no, the producer genuinely reads nothing in-corpus → **dead**.

| Bucket A split | Count |
|---|---|
| **recoverable** (COLUMN_LINEAGE has a real source table) | **10** |
| **dead** (no real column source either — `LIKE`/literal/star-of-out-of-corpus) | **33** |

### Island census, final [measured]

| Verdict | Islands/members | Reward |
|---|---|---|
| **Recoverable** | ~10 members across ~10 islands | the only lever-able set |
| Dead — reference-only (never a target) | 54 roots | none (out-of-corpus producer, e.g. Liquibase XML — OUT OF SCOPE) |
| Dead — noise-only source | 4 roots | none (correctly excluded) |
| Dead — no real column source (`LIKE`/literal/star) | 33 roots | none |

**Verdict: the prior "all islands are dead" conclusion is FALSIFIED, but only barely.** There
*is* a recoverable subset — 10, not 0 — so the blanket claim was wrong. But 87 of 97 islands
are genuinely irreducible by SQL parsing, and the recoverable 10 do not share a mechanism.

### Projected reconnection if all 10 recoverable were fixed [measured synthesis]

Synthesise, per recoverable member, the SELECTS_FROM edges its own COLUMN_LINEAGE already
resolved (noise-filtered), add to the graph, recount:

| | before | after synth | Δ |
|---|---|---|---|
| small islands | 97 | **85** | **−12** |
| giant component | 1,826 | 1,864 | +38 |
| SELECTS_FROM edges | 3,368 | 3,394 | +26 |
| recoverable members folding into giant | — | **9 / 10** | — |

So the **entire** remaining table-level island lever is worth **−12 islands / +26 SELECTS_FROM
edges**, spread across ≥4 distinct parser mechanisms. Compare to PR-2's −50/+473. The
table-level well is nearly dry.

### The 10 recoverable members, by mechanism [measured]

| Mechanism | Count | Members | Lever owner |
|---|---|---|---|
| **Derived subquery** (`FROM (SELECT …)`) | 1 | `da.tmp_dubbel` | **PR-3 (subscope descent) — already in flight** |
| **UNION alias** (`FROM subunion`, a UNION-of-selects derived alias) | 1 | `ba.wtfv_vrd_laatstgeteld` | PR-3-adjacent (derived) |
| **Self-join, both aliased** (`FROM t AS a JOIN t AS b`) | 1 | `ba.vorig_jaar` | new (small) |
| **Top-level CTE, descent still missed** | 4 | `etl_wtfe_klantfeedback_{review,na_aankoop,website_verlater}`, `wvfv_rfm_classificatie` | PR-2 follow-up |
| **Plain CREATE_VIEW** (non-CTE, source not emitted) | 3 | `wtfv_transacties_uurlijks`, `wvfa_rfm_classificatie`, `wtda_ail_logging` | view edge-case |

Two caveats on this tail:
- **CREATE_VIEW is not systemically broken**: 722 / 726 CREATE_VIEW queries DO emit SELECTS_FROM
  [measured]. The 3 view misses are edge-cases (long `COMMENT='…'` clauses, a 1-1-relation
  comment, lateral/cast join predicates) — per-case, not a clean lever.
- **The 4 top-WITH-CTE misses are the most interesting**: PR-2 added CTE-child-scope descent,
  yet these 4 still emit no SELECTS_FROM. [inferred] cause: nested `WITH` (a CTE body that
  itself opens a `WITH`, or a CTE that selects from a derived subquery), which the single-level
  `cte_sources` walk in [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) does not
  recurse into — the same recursion PR-3 is adding for subscopes. **If PR-3 generalises the
  descent to nested CTE/derived scopes, it likely sweeps up 5–6 of these 10 for free.** Worth
  flagging to the PR-3 dev, but not its own PR.

**Conclusion on islands:** not a lever. The ≤10 recoverable members are best handled as a
*side effect* of PR-3's subscope-descent generalisation, not as a dedicated PR. Reward
(−12 islands / +26 SELECTS_FROM) does not justify a standalone ticket.

---

## 3. E8 revival — the #1 lever, now gate-able

**What E8 is.** A COLUMN_LINEAGE fix in
[`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) CTAS `sources_map` registration:
register the temp target under all three key forms (bare, post-alias-qualified,
pre-alias-qualified) so `exp.expand()` resolves Snowflake `USE SCHEMA`-prefixed temp references
through the chain. Without it, the lookup misses → `col_lineage_skip:dynamic_source` (the E{n}
"E8" class) → no column lineage past that temp link.

**Measured gain (prior A/B, `e8_column_vs_island_remeasure.md`, same 1,335-file corpus):**

| Metric | WITHOUT E8 | WITH E8 | Δ | Edge type |
|---|---|---|---|---|
| COLUMN_LINEAGE total | 53,162 | 54,890 | **+1,728** | COLUMN_LINEAGE |
| strict-good edges | 44,771 | 45,851 | +1,080 | COLUMN_LINEAGE |
| scoped-good edges | 36,057 | 36,547 | +490 | COLUMN_LINEAGE |
| SELECTS_FROM-WCC islands | 147 | 147 | 0 | SELECTS_FROM (E8 is column-only — structurally cannot move this) |

**Why it was parked, and why that no longer holds.** E8 (PR #111) was closed because it was
judged on the **island count** (147→147) — the wrong ruler, since E8 is COLUMN_LINEAGE-only.
The remeasure proved a real +1,728 COLUMN_LINEAGE win existed but was *unfalsifiable* at the
time: there was no monotone column-recall metric to gate it. **That metric now exists:**
`resolvable_write_col_edges` (shipped v1.27.0, [`coverage.py`](../../src/sqlcg/cli/coverage.py#L338)),
which counts COLUMN_LINEAGE edges on source-resolved write queries — a counter that can only
rise when a recall fix lands more edges.

**The gate (falsifiable, names the metric):**

> Re-index the DWH with E8 restored. **PASS iff `resolvable_write_col_edges` rises by ≥+1,000
> on the same corpus.** The prior A/B predicts ≈+1,790 of the +1,728 new edges land on
> source-resolved write queries; the +1,000 floor absorbs corpus drift and sits far above
> noise. FAIL ⇒ do not revive.

Note the absolute baseline is **corpus-relative**: on `/tmp/ab_pr2.duckdb` it is **33,475
[measured]**; on the `/tmp/e8_without.duckdb` proxy it was 25,246. The threshold is the
**delta on one corpus**, not an absolute value, so the discrepancy does not weaken the gate —
the dev must measure WITHOUT-vs-WITH on whatever corpus they re-index.

**Reward vs everything else.** +1,728 COLUMN_LINEAGE edges dwarfs the next candidate (the
~595 strict-bad-on-real-table scoped gap, the −12-island synth, the 86 rescuable-unqualified).
It is the only four-figure lever on the board.

**Perf hot-path?** [inferred, low risk] No. E8 changes `sources_map` *registration* (adds two
dict keys per CTAS target), not the per-column `qualify`/`body_scope`/`expand` loop the
CLAUDE.md invariants protect. It does not touch `_real_tables`, `_extract_column_lineage`'s
column loop, or the bulk-upsert batch path. The prior A/B index wall-times (5m57 with, 4m18
without) were dominated by WSL2 load + the 122k-column catalog re-apply, not E8 — but the dev
**must record an op-count check against `test_perf_scaling_guard.py`** as part of the revival
PR (the guard pins per-statement scope ops; E8 should leave them flat).

**Risk.** Medium-low. E8 had two prior bug-fix commits (`a841c4b`, `3a31953`) for sources_map
key-mismatch; its unit tests (`tests/unit/test_e8_temp_chain_key_mismatch.py`) exist and pin
behaviour. Reviving = un-revert + re-run the gate. The −1pp scoped drop (98%→97%) the remeasure
saw is acceptable: it reflects new edges landing on legitimate temp/CTE intermediate nodes, not
wrong edges.

---

## 4. Column-level coverage — the 16% gap is 93% mirage [measured]

`/tmp/ab_pr2.duckdb`: 8,720 strict-bad COLUMN_LINEAGE edges (the 16%).

| Class | Count | Recoverable? |
|---|---|---|
| dst is **CTE/derived/temp** (correct-by-design intermediate) | **8,125 (93%)** | No — this is what the *scoped* 98% metric correctly excludes |
| dst is a **real table** (the genuine scoped gap) | **595** | Partially — catalog or resolution gap |
| of which `rescuable_unqualified` (bare name, exists schema-qualified) | 86 | Yes, small — F2 USE-SCHEMA keying |
| phantom **contradicted** (`inferred_from_source_name`, provably-wrong dst column) | 412 | These are *bad* edges to remove/fix, not recall to gain |

**Reading:** the 84% strict / 98% scoped split is not a coverage failure — it is the structural
cost of (desirably) tracking columns through CTE/derived/temp nodes. Moving strict-84% would
mean cataloguing intermediate nodes that have no stable schema, which is neither possible nor
useful. The only real column-recall lever in this population is **E8** (§3), which *adds*
correct edges; the 595-real-table gap and 86-rescuable-unqualified are small mop-up.

**Degraded parse is no longer a lever.** Only **20 / 5,186 queries** are `parse_failed`
[measured] — down from the ~178–404 cited in older docs (clean reindex + PR-2/PR-3-era parsing).
There is no degraded-parse population worth a PR.

---

## 5. Other SQL levers (the long tail) [measured]

- **`rescuable_unqualified_edges` = 86** [measured]. Bare-name dst edges whose name exists
  schema-qualified in HAS_COLUMN — recoverable by tighter USE-SCHEMA keying (F2). 86 edges; a
  cleanup PR, not a coverage lever. Gate: the existing `rescuable_unqualified_edges` metric → 0.
- **Phantom contradicted = 412** [measured]. `inferred_from_source_name` edges whose dst column
  is provably absent from the catalogued dst table — these are *wrong* edges. A precision (not
  recall) PR: suppress/flag them. Gate: `phantom_contradicted` (Query 3-split) → falls. Worth
  a small ticket for trust, but does not improve coverage.
- **595 strict-bad-on-real-table** [measured]. Mixed: some are out-of-corpus tables (no DDL,
  catalog miss — XML-DDL territory, OUT OF SCOPE), some overlap the 86 rescuable. After removing
  the XML-DDL and rescuable slices, the residual SQL-recoverable count is in the low hundreds at
  most. No single mechanism dominates → no clean lever.
- **Self-join / UNION-alias / nested-CTE descent** (the 6 non-PR-3 recoverable island members):
  fold into PR-3's descent generalisation; not standalone.

---

## 6. Ranked levers — reward vs risk/effort, SQL-only

| # | Lever | Reward [measured] | Edge type | Falsifiable gate (existing metric) | Hot-path? | Risk/Effort |
|---|---|---|---|---|---|---|
| **1** | **E8 revival** (temp-chain `dynamic_source` multi-key) | **+1,728 COLUMN_LINEAGE** (+1,080 strict-good) | COLUMN_LINEAGE | `resolvable_write_col_edges` rises ≥+1,000 (v1.27.0) | No [inferred] | Low-med — un-revert + gate; tests exist |
| 2 | PR-3 descent generalisation sweeps nested-CTE/derived/UNION island members | −5–6 islands (subset of −12) | SELECTS_FROM | island count / `cte_source_gap_writes` ↓ | No | **Already in flight** — flag to PR-3 dev, no new PR |
| 3 | Phantom-contradicted suppression | −412 wrong edges (precision, not recall) | COLUMN_LINEAGE | `phantom_contradicted` ↓ | No | Low — but trust, not coverage |
| 4 | `rescuable_unqualified` USE-SCHEMA keying | +86 edges promoted | COLUMN_LINEAGE | `rescuable_unqualified_edges` → 0 | No | Low — small |
| 5 | Self-join / plain-view source emission edge-cases | −4–5 islands | SELECTS_FROM | island count ↓ | No | Med per-case, heterogeneous; **not worth it** |
| — | Remaining 87 islands | 0 | SELECTS_FROM | — | — | **Irreducible by SQL (out-of-corpus / noise / literal)** |

---

## 7. The single recommendation

**Revive E8** (lever #1). It is the only SQL-only lever with a four-figure measured edge gain,
it is now gate-able by a metric that did not exist when it was parked, it is off the perf
hot-path, and its tests already exist. Take it through architect-planner → plan-review →
developer as a single PR whose acceptance is: **re-index DWH, `resolvable_write_col_edges`
rises ≥+1,000 vs the WITHOUT-E8 control on the same corpus, and `test_perf_scaling_guard.py`
op-counts stay flat.**

**Secondary, free:** tell the PR-3 developer that generalising subscope descent to **nested
CTE / derived / UNION** scopes will sweep up ~5–6 of the 10 recoverable island members at no
extra cost (members `tmp_dubbel`, `wtfv_vrd_laatstgeteld`, `etl_wtfe_klantfeedback_*`,
`wvfv_rfm_classificatie`). No separate ticket.

**Do not** open a PR to chase the islands (−12 max, heterogeneous), the 84% strict number
(93% is correct-by-design CTE/derived nodes), or degraded parse (only 20 queries). The
remaining 87 islands are genuinely irreducible by SQL parsing — their producers live in
Liquibase XML (out of scope) or they are reference-only / literal / noise.

---

## 8. Method limitations / honesty

- All counts are on the **existing** `/tmp/ab_pr2.duckdb` (sha `fdf1b551`); no fresh index was
  run (concurrent-index OOM constraint). The PR-3 in-flight A/B was not consulted — its result
  may shift the −12 island synth slightly (PR-3 may already have closed some of the 6
  derived/nested members), which would only *strengthen* the "islands are not the lever"
  conclusion.
- The −12 islands / +26 SELECTS_FROM figure is a **measured synthesis** (column-resolved
  sources promoted to table edges), not a post-fix re-index. It is a tight upper bound because
  it assumes a fix surfaces exactly the column-resolved source set — the same descent the
  column path already performs.
- The E8 +1,728 figure is **measured** from the prior A/B (`e8_column_vs_island_remeasure.md`),
  not re-measured here (re-running E8 is out of scope and OOM-risky). The gate re-measures it
  at implementation time.
- The `sql` column in `SqlQuery` is stored truncated to ~500 chars; producer-shape
  classification used the CL-resolved source set + the visible prefix, not full-text FROM
  parsing. Shape labels (self-join / derived / nested-CTE) are [inferred] from those signals.
- Perf-hot-path verdicts for E8 are [inferred] from reading the registration site, not from a
  fresh op-count — the revival PR must confirm against `test_perf_scaling_guard.py`.
- Edge-type discipline held throughout: island counts are SELECTS_FROM-WCC; recall figures are
  COLUMN_LINEAGE; the only cross-use is COLUMN_LINEAGE-as-probe for recoverability, explicitly
  converted to SELECTS_FROM before any island recount.

---

## Verdict

The remaining coverage gap is **mostly irreducible by SQL means** — 87 of 97 islands are dead,
and 93% of the strict-health gap is correct-by-design CTE/derived nodes. The one real,
four-figure, gate-able SQL-only lever left is **E8 revival (+1,728 COLUMN_LINEAGE edges, gated
by `resolvable_write_col_edges` ≥+1,000)**. Everything else is a sub-hundred-edge mop-up or a
side effect of the in-flight PR-3.
