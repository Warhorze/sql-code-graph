# Remaining table-level islands after E8 — does dual-emission move them, and what's the next lever?

**Author role:** researcher (opus)
**Date:** 2026-06-14
**Branch:** `research/remaining-islands-post-e8`
**Mode:** READ-ONLY. No source changed, DWH repo untouched, **no `sqlcg index` run** (a
concurrent DWH index OOMs the 3.8 GB box — single-slot owned by another agent). No usable
pre-built corpus DB was available: the only `/tmp/*.duckdb` present is
`/tmp/e8_baseline.duckdb` (12 KB, a unit fixture, and locked by PID 341000). All numbers
below are therefore **[cited]** from prior committed research with a code-evidence cross-check,
or **[code]** (read directly from source). A `[needs-reconfirm]` flag marks every cited number
that must be re-measured on the post-E8 graph once the index slot frees (§4).

**Edge-type discipline (the trap that has burned this project):** **SELECTS_FROM = table-level**,
**COLUMN_LINEAGE = column-level**, **HAS_COLUMN = catalog**. The island metric is a
SELECTS_FROM-only construct (§1.2). A COLUMN_LINEAGE count is never cross-applied to an island
baseline.

---

## 0. Answer first

1. **Does E8 (dual-emission temp-chain column-lineage) reduce table-level islands? NO —
   definitively, by construction.** E8 emits **only COLUMN_LINEAGE** edges; the island count is
   computed **only over SELECTS_FROM** edges. The two never intersect. Code evidence in §1;
   the prior live A/B measured it directly: **islands 147→147, unchanged** (§1.3).

2. **The remaining islands are ~87–90% irreducible.** Of 97 post-PR-2 small islands (250
   members), **~10 members across ~10 islands are recoverable**; the other **~87 are genuinely
   dead** (reference-only out-of-corpus producers, noise-filtered sources, or literal/`LIKE`/star
   producers with no real in-corpus source). Taxonomy with counts in §2 [cited, needs-reconfirm].

3. **The next island lever is NOT E8.** It is **PR-3 subscope-descent generalisation to nested
   CTE / derived / UNION scopes** (a SELECTS_FROM extraction lever), worth ≈**−5–6 islands** as a
   side effect, plus a heterogeneous **−4–5** from per-case view/self-join source-emission
   edge-cases. Total table-level headroom is **≈−12 islands / +26 SELECTS_FROM edges** — versus
   PR-2's banked −50/+473. The table-level well is nearly dry. Ranked list in §3.

4. E8's real value is orthogonal: **+1,728 COLUMN_LINEAGE edges** (column recall), gated by
   `resolvable_write_col_edges` ≥+1,000. It is the top *column* lever, not an island lever.

---

## 1. Does E8 fix the islands? NO — code-level proof

### 1.1 What dual-emission emits: COLUMN_LINEAGE only

The dual-emission design ([`e8_dual_emission_feasibility.md`](e8_dual_emission_feasibility.md),
branch `research/e8-dual-emission`) composes the two already-extracted structural temp hops
(`src→temp`, `temp→sink`) into one additive transitive edge:

> "build `LineageEdge(src=F.src, dst=E.dst, transform="TEMP_INLINE", …)`"
> — feasibility doc §1.2, referencing [`base.py:144`](../../src/sqlcg/parsers/base.py#L144)

A `LineageEdge` is persisted **exclusively** as a `COLUMN_LINEAGE` row. In the indexer the
two edge classes are populated from two disjoint sources:

- **SELECTS_FROM** rows come from `stmt.sources` (the table-path), emitted in the source-table
  loop at [`indexer.py:1499`](../../src/sqlcg/indexer/indexer.py#L1499):
  `rows.selects_from_edges.append({"src_key": query_id, "dst_key": src_table.full_id})`.
- **COLUMN_LINEAGE** rows come from `stmt.column_lineage` (the `LineageEdge` list) at
  [`indexer.py:1570`](../../src/sqlcg/indexer/indexer.py#L1570).

`TEMP_INLINE` edges are appended to `column_lineage` → they become COLUMN_LINEAGE rows only.
**Nothing on the column path ever writes a SELECTS_FROM row.** The only COLUMN_LINEAGE→other
derivation is [`_harvest_usage_catalog`](../../src/sqlcg/indexer/indexer.py#L93), which derives
`HAS_COLUMN(source='usage')` catalog rows from COLUMN_LINEAGE *src* endpoints — **catalog, not
SELECTS_FROM** [code].

### 1.2 What the island metric counts: SELECTS_FROM only

The island count is **not** in the shipped coverage CLI — [`coverage.py`](../../src/sqlcg/cli/coverage.py)
mentions "island" only in comments; its closest SELECTS_FROM metric is `cte_source_gap_writes`
([L327](../../src/sqlcg/cli/coverage.py#L327)). The island count itself is an ad-hoc NetworkX
WCC over the table-level graph, defined in
[`dwh_graph_analysis.md`](../reports/dwh_graph_analysis.md):

> "Table-level edges are reconstructed from `SELECTS_FROM` + `SqlQuery.target_table` … weakly-
> connected components: 148 … 147 small islands"
> — dwh_graph_analysis.md L242, L150, L155 [cited]

i.e. `nx.DiGraph` of `SELECTS_FROM.dst_key (src table) → SqlQuery.target_table`, self-edges
dropped, `_bck`/`ma.rtetl_delta` noise filter, count weakly-connected components below the
giant. **COLUMN_LINEAGE is never an input to this graph.**

### 1.3 The two are disjoint ⇒ E8 is structurally island-neutral — and it was measured so

Because (a) E8/dual-emission emits 0 SELECTS_FROM edges and (b) the island metric reads 0
COLUMN_LINEAGE edges, **E8 cannot move the island count under any input.** This is not a
hypothesis — it was measured live in the prior A/B
([`e8_column_vs_island_remeasure.md`](e8_column_vs_island_remeasure.md), branch
`origin/research/e8-column-vs-island-remeasure`):

| Metric | WITHOUT E8 | WITH E8 | Δ | Edge type |
|---|---|---|---|---|
| COLUMN_LINEAGE total | 53,162 | 54,890 | **+1,728** | COLUMN_LINEAGE |
| strict-good edges | 44,771 | 45,851 | +1,080 | COLUMN_LINEAGE |
| SELECTS_FROM total | 4,869 | 4,867 | **−2** (noise) | SELECTS_FROM |
| WCC components | 148 | 148 | **0** | SELECTS_FROM-WCC |
| **Small islands (not giant)** | **147** | **147** | **0** | SELECTS_FROM-WCC |

The remeasure doc states the mechanism explicitly:

> "`sources_map` feeds `exp.expand()` on the column-lineage path only. The SELECTS_FROM
> emission path (`stmt.sources`) is populated **before** `sources_map` is consulted; E8 does
> not touch it. Islands are determined by SELECTS_FROM-WCC, so E8 is structurally neutral."
> — e8_column_vs_island_remeasure.md L41-44 [cited]

**Verdict: E8 does NOT and cannot reduce table-level islands.** It is a column-recall lever
mis-judged on a table-level ruler when it was first parked (PR #111).

### 1.4 The subtle temp-chain case — is a temp-mediated REAL→REAL dependency missing at SELECTS_FROM?

Checked, and it is **not** a recoverable island lever for the temp pattern itself. In the
temp chain, the **producer** statement `CREATE temp AS SELECT … FROM da.leaf_src` and the
**sink** statement `INSERT INTO ba.final FROM ba.tmp` are *two separate `SqlQuery` rows*, and
each already emits its own SELECTS_FROM from its own `stmt.sources`:
`leaf_src→(temp producer query)` and `tmp→(final sink query)`. At the table level the temp node
**is** in the SELECTS_FROM graph and **does** connect `leaf_src`'s component to `final`'s
component through the temp table node. So the temp pattern does not orphan its real endpoints —
the connection exists, just routed through the temp node (which is correct table-level lineage).

The genuine island-makers are a *different* failure: statements whose **table path emits zero
SELECTS_FROM rows at all** even though the column path resolved a real source — i.e.
`stmt.sources` is empty while `stmt.column_lineage` has a real-table src. That is the
`cte_source_gap_writes` population, and its fix is a **SELECTS_FROM extraction** change
(§3 lever #1), categorically distinct from E8's column work.

---

## 2. The remaining islands, classified by root cause [cited — needs-reconfirm on post-E8 graph]

Source: [`sql_only_coverage_lever_post_pr2.md`](sql_only_coverage_lever_post_pr2.md) §2,
measured on `/tmp/ab_pr2.duckdb` (sha `fdf1b551`, 1,335 files, schema v8), cross-checked
against [`issue38_pr2_live_acceptance.md`](../reports/issue38_pr2_live_acceptance.md). These
are the **post-PR-2** numbers; the headline "147→43" the maintainer cited blends PR-2 (−50, to
97) with the PR-3 in-flight delta. **The −71% to 43 is a PR-2+PR-3 figure that was never
re-confirmed on a single clean post-E8 index** — see §4. The fully-classified census we *can*
cite is the 97-island post-PR-2 graph:

WCC histogram [cited]: `{2:66, 3:17, 4:9, 5:2, 6:1, 7:1, 8:1, 1826:1}` → **97 small islands,
250 members, 1 giant (1,826)**. A member only disconnects if it is a **root** (zero incoming
SELECTS_FROM). 250 members → **101 roots**.

| Root-cause class | Count (roots) | Recoverable? | Lever |
|---|---|---|---|
| **(e) External / egress-boundary** — read but never produced in-corpus (producer lives in Liquibase XML, out of scope) | **54** | No | issue #35 (external injection) — out of current SQL scope |
| **(b) Noise-only source** — all producer sources `_bck`-filtered (correctly excluded) | **4** | No (correct-by-design) | none |
| **(a)+(d) Dead producer** — has producer, but its only column sources are `LIKE`/literal/star-of-out-of-corpus (no real in-corpus source) | **33** | No | none (sourceless/literal — correct zero-source) |
| **(c) Genuinely recoverable** — producer's COLUMN_LINEAGE resolves a real (non-cte/derived/temp, non-noise) source table, but the table path emitted no SELECTS_FROM | **~10** | **Yes** | §3 levers #1, #5 |

So the requested 5-way split maps as: **(a) sourceless literal inserts** + **(d) dead
code/star** = the 33 dead-producer roots; **(b) plain self-reload / self-loop** = dropped by
the self-edge filter before counting (not an island, correct); **(c) recoverable** = ~10;
**(e) external/egress (issue #35)** = 54 reference-only roots — the single largest bucket.

### The ~10 recoverable members, by mechanism [cited]

| Mechanism | Count | Lever owner |
|---|---|---|
| Derived subquery `FROM (SELECT …)` (`da.tmp_dubbel`) | 1 | PR-3 subscope descent (in flight) |
| UNION alias (`ba.wtfv_vrd_laatstgeteld`) | 1 | PR-3-adjacent (derived) |
| Self-join, both aliased (`ba.vorig_jaar`) | 1 | new (small) |
| Top-level CTE, descent still missed (4 `etl_wtfe_*` / `wvfv_rfm_classificatie`) | 4 | PR-2 follow-up / nested-CTE descent |
| Plain CREATE_VIEW source not emitted (3 views) | 3 | per-case view edge-case |

Projected reconnection if **all 10** were fixed [cited measured-synthesis]:
**islands 97→85 (−12), SELECTS_FROM +26, 9/10 members fold into the giant.** That is the
entire remaining table-level headroom.

**Honesty:** CREATE_VIEW is not systemically broken — **722/726** view queries already emit
SELECTS_FROM [cited]; the 3 misses are edge-cases (long `COMMENT=` clauses, lateral/cast join
predicates), not a clean lever.

---

## 3. Ranked next-lever list (island-reduction potential, SELECTS_FROM unless noted)

| # | Lever | Class it kills | Island Δ [cited] | Edge type | Falsifiable gate | Effort |
|---|---|---|---|---|---|---|
| **1** | **PR-3 subscope-descent generalisation to nested CTE / derived / UNION scopes** | (c) recoverable: derived-subquery + UNION-alias + nested-CTE roots | **−5–6 islands** (subset of −12) | SELECTS_FROM | `cte_source_gap_writes` ↓ / island recount | **Already in flight** — flag to PR-3 dev, no new PR |
| 2 | Self-join + plain-view source-emission edge-cases | (c) remaining ~4–5 recoverable | −4–5 islands | SELECTS_FROM | island recount | Med, heterogeneous, per-case — low ROI |
| 3 | **External-injection (issue #35)** for the 54 reference-only roots | (e) egress-boundary | up to −54 islands **IF** producers brought in-corpus | SELECTS_FROM | island recount | **Large / out of SQL scope** — needs Liquibase-XML DDL or catalog catalog of external producers |
| — | catalog gap / `rescuable_unqualified` (86) | column-level only | **0 islands** | COLUMN_LINEAGE | n/a | does not touch islands |
| — | E8 dual-emission | column-level only | **0 islands** (§1) | COLUMN_LINEAGE | `resolvable_write_col_edges` ≥+1,000 | the column lever, not this list |

**Ranking logic.** Lever #1 is free (rides PR-3, issue #33-adjacent CTE/derived parity) and
hits the largest *recoverable* mechanism cluster. Lever #2 is the per-case tail — only worth it
opportunistically. Lever #3 (issue #35) has by far the biggest *raw* potential (54 roots) but
those producers are **out of the SQL corpus entirely** (Liquibase XML); reducing them is not a
parser lever — it requires either indexing the XML DDL or a catalog of external producers. It
is the correct long-term answer to "where do the islands actually come from" (answer: **mostly
external/egress boundaries, issue #35**) but it is not a SQL-parsing PR.

**Bottom line:** the next *SQL-only* island lever is #1 (≈−5–6, already in flight). Beyond that,
table-level islands are dominated by the issue-#35 external boundary (54 roots) — which is the
real lever if the maintainer wants the count to drop substantially, and it is not an extraction
fix.

---

## 4. Empirical re-measurement plan (run once the index slot frees)

Everything in §2 is **post-PR-2** (`ab_pr2`, sha `fdf1b551`) and predates E8 and any merged
PR-3. The maintainer's "147→43" is a blended PR-2+PR-3 figure not confirmed on a single clean
post-E8 index. To confirm E8's island-neutrality and size the next lever, run **one clean index
of the current HEAD** (post-E8, post-PR-3), then:

**A. Confirm E8 island-neutrality (A/B on the same corpus):**
1. Index WITHOUT E8 (control) and WITH E8 (treatment) — same sha, same `.sqlcg.toml`.
2. For each DB capture, via `sqlcg gain --json`: `resolvable_write_col_edges` (gate: WITH−WITHOUT
   ≥ +1,000), `good_edges_strict`, `total_edges` (COLUMN_LINEAGE).
3. Island count both DBs (script below). **Expect Δ = 0.** If it moves, the SELECTS_FROM path
   was touched — investigate, do not ship.

**B. Size the next (table-level) lever on the post-E8 graph:**
4. Re-run the §2 census script and re-confirm: total small islands, #roots, the (e)=54 / dead=37
   / recoverable=~10 split. Flag any drift from PR-3 (which may already have closed some of the
   6 derived/nested recoverable members — that only *strengthens* "islands aren't the lever").

**"Small island" definition (pin this):** weakly-connected component of the noise-filtered
table-level DiGraph whose size < the giant component. Build exactly as
[`dwh_graph_analysis.md`](../reports/dwh_graph_analysis.md):

```python
import duckdb, networkx as nx
con = duckdb.connect("<post_e8>.duckdb", read_only=True)   # READ-ONLY, never index here
rows = con.execute('''
  SELECT t.qualified AS src, q.target_table AS dst
  FROM "SELECTS_FROM" sf
  JOIN "SqlQuery" q  ON q.id = sf.src_key
  JOIN "SqlTable" t  ON t.qualified = sf.dst_key
  WHERE q.target_table <> '' AND t.qualified <> q.target_table   -- drop self-loops (class b)
''').fetchall()
NOISE = ('_bck', 'ma.rtetl_delta')   # match .sqlcg.toml
def keep(n): return not (n.endswith('_bck') or n.startswith('ma.rtetl_delta'))
g = nx.DiGraph((s, d) for s, d in rows if keep(s) and keep(d))
comps = sorted((len(c) for c in nx.weakly_connected_components(g)), reverse=True)
giant = comps[0]
print("small islands:", sum(1 for c in comps[1:]), "members:", sum(comps[1:]), "giant:", giant)
```

Capture **before/after** (control vs E8) of: `small islands`, `members`, `giant`, and from
`gain --json`: `resolvable_write_col_edges`, `cte_source_gap_writes`, `good_edges_strict`,
`total_edges`. Confirm islands flat across the E8 A/B; report the absolute post-E8 island count
to settle the "147 vs 43" discrepancy.

---

## 5. Verdict

- **E8 fix the islands? NO** — E8/dual-emission emits only COLUMN_LINEAGE
  ([`indexer.py:1570`](../../src/sqlcg/indexer/indexer.py#L1570) via `LineageEdge`); the island
  metric reads only SELECTS_FROM ([`dwh_graph_analysis.md`](../reports/dwh_graph_analysis.md)
  L242). Disjoint by construction; measured 147→147 [cited].
- **Where the remaining islands come from:** ~54/97 are **external/egress-boundary** producers
  out of the SQL corpus (issue #35), ~37 are dead (literal/star/noise, correctly zero-source),
  and only ~10 are SQL-recoverable.
- **Next lever:** PR-3 nested-CTE/derived/UNION subscope descent (≈−5–6 islands, in flight) for
  SQL-only headroom; issue #35 external injection (≈54 roots) is the only large-volume lever and
  it is not an extraction fix. Total SQL-only island headroom is ≈−12 / +26 SELECTS_FROM —
  small. The table-level well is nearly dry; E8's value is the +1,728 COLUMN_LINEAGE column-recall
  win, on a different ruler.
- **All §2/§3 counts are [cited] from post-PR-2 `ab_pr2` and need re-confirmation on a clean
  post-E8 index (§4).**
