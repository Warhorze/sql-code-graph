# E8 dual-emission feasibility — can we have our cake and eat it too?

**Author role:** researcher (opus)
**Date:** 2026-06-14
**Branch:** `research/e8-dual-emission`
**Question:** The E8 temp-chain recall fix and the shipped temp-table-namespacing
feature demand **opposite lineage shapes for the identical SQL pattern** (inline-through
vs keep-temp-visible). [`plan/sprints/e8_revival_column_lineage.md`](../sprints/e8_revival_column_lineage.md)
§0.2 proved a single collapse-or-not toggle cannot satisfy both. This doc tests the
**dual-emission** hypothesis: keep the structural temp hops AND additively emit a
provenance-tagged transitive `source→sink` edge, so consumers pick the view.

---

## VERDICT: **FEASIBLE** (empirically proven, both test sets green simultaneously)

A spike that **composes the already-extracted structural edges into an additive,
`transform='TEMP_INLINE'`-tagged transitive edge** — without inlining the temp body,
without re-running `exp.expand()`/`sg_lineage`/`qualify` — makes **both** the E8 suite
**and** the temp-table-namespacing suites pass at once, with **zero collateral failures
across the entire test suite (1008 unit + 503 integration)**, and costs **<1% of
parse time** on a pathologically temp-heavy file. The "fork" in the revival plan §0.2 is
a **false dichotomy**: the conflict only exists because E8's mechanism (registering the
qualified `sources_map` key → `exp.expand()` *replaces* the temp body) is **destructive**.
A **non-destructive, additive** mechanism dissolves the conflict.

There is one real cost (metric inflation / double-counting) and one bounded risk
(fan-in × fan-out edge multiplication on a narrow SQL shape). Both are characterized
below with recommendations.

---

## 1. Why the original conflict exists — and why dual-emission sidesteps it

### 1.1 The collapse is a side-effect of `exp.expand()`, NOT something E8 chooses

E8's fix is two extra `sources_map` dict writes at
[`ansi_parser.py:229-232`](../../src/sqlcg/parsers/ansi_parser.py#L229) (register the
schema-qualified key alongside the bare key). But the **collapse** happens downstream, in
`_extract_column_lineage` at [`base.py:1147-1170`](../../src/sqlcg/parsers/base.py#L1147):

```python
expand_sources = {k: v for k, v in (sources or {}).items() if isinstance(v, exp.Query)}
if expand_sources:
    expanded_body = exp.expand(body, expand_sources, dialect=self.DIALECT, copy=True)
...
body_scope = build_scope(qualify(expanded_body, ...))
```

When the INSERT body `... FROM ba.tmp_base` finds `ba.tmp_base` in `sources_map`,
`exp.expand()` **textually substitutes** the temp's SELECT body into the INSERT body.
`sg_lineage` then traces the substituted tree and produces `leaf_src → final_tgt`
**directly** — the temp node never appears as a lineage endpoint on the INSERT side. That
is the namespacing regression: the temp is left with **zero outgoing** COLUMN_LINEAGE
edges. Confirmed by direct edge probe (baseline, no E8):

```
stmt0 CREATE: da.leaf_src.col -> etl/x.sql::ba.tmp_inkoop.col   [PASS_THROUGH]
stmt1 INSERT: etl/x.sql::ba.tmp_inkoop.col -> ba.final_tgt.col  [PASS_THROUGH]
```
…and WITH E8's qualified key, `stmt1` becomes `da.leaf_src.col -> ba.final_tgt.col`
(the temp endpoint is gone — exactly §0.2's empirical finding).

**Key realization:** the structural hops (`src→temp`, `temp→sink`) and the transitive
edge (`src→sink`) are *the same information at different granularities*. The transitive
edge is **derivable by composing the two structural hops** — it does NOT require inlining.

### 1.2 The dual-emission mechanism (the spike)

Leave `sources_map` registration **unchanged** (bare key only — temp body is NOT inlined,
so the structural hops survive and namespacing stays green). After the per-statement loop
in `parse_file`, run one O(edges) composition pass over the file's already-extracted edges:

> For every edge `E` whose `E.src.table.role == 'temp'`, find the temp's incoming edges
> `F` (where `F.dst.table.full_id == E.src.table.full_id` and `F.dst.name == E.src.name`),
> and emit an additive edge `F.src → E.dst` with `transform='TEMP_INLINE'`. Drop it if it
> is a self-loop (`F.src == E.dst`).

This is **pure in-memory edge composition** — no `exp.expand()`, no `sg_lineage`, no
`qualify`, no `build_scope`. It touches only the post-loop `ParsedFile.statements[*].column_lineage`
lists. Exact spike wiring (reverted; reproduce verbatim):

- New helper `AnsiParser._emit_transitive_temp_edges(out)` (~45 lines), called once from
  `parse_file` immediately **before** the `self._current_file_namespace = None` reset at
  [`ansi_parser.py:249-250`](../../src/sqlcg/parsers/ansi_parser.py#L249).
- It reads `e.src.table.role == 'temp'` / `e.dst.table.role == 'temp'` (the role stamped at
  [`ansi_parser.py:343-352`](../../src/sqlcg/parsers/ansi_parser.py#L343)) and builds
  `LineageEdge(src=F.src, dst=E.dst, transform="TEMP_INLINE", confidence=min(...), query_id=E.query_id)`
  ([`base.py:144`](../../src/sqlcg/parsers/base.py#L144)).

---

## 2. The edge-key normalization / dedup chokepoint — does it survive distinctly?

**YES — the transitive edge is structurally distinct and is NOT deduped away.**

The single batch-level dedup chokepoint is at
[`indexer.py:240-242`](../../src/sqlcg/indexer/indexer.py#L240), keyed on **`(src_key, dst_key)` only**:

```python
column_lineage_edges = list(
    {(r["src_key"], r["dst_key"]): r for r in buf.column_lineage_edges}.values()
)
```

The transitive edge `leaf_src.col → final_tgt.col` has a **different `(src_key, dst_key)`**
than either structural hop (`leaf_src.col → tmp.col`, `tmp.col → final_tgt.col`), so it is
preserved as a distinct row. (Note the dedup does **not** key on `transform`, so a transitive
edge that happened to share `(src,dst)` with a structural one would be collapsed — but by
construction it cannot, because the temp node sits in exactly one of the two endpoints of
each structural hop and in **neither** endpoint of the transitive edge.)

**Provenance carrier:** `COLUMN_LINEAGE` already has a `transform STRING` column
([`schema.cypher:93-99`](../../src/sqlcg/core/schema.cypher#L93)) that is persisted from
`LineageEdge.transform` ([`indexer.py:1574`](../../src/sqlcg/indexer/indexer.py#L1574)) and
read back by the trace query ([`queries.sql` `TRACE_COLUMN_LINEAGE` → `cl.transform`]).
So **no schema migration is needed** — `transform='TEMP_INLINE'` is a fully distinguishable,
already-plumbed provenance tag. (A dedicated boolean `derived`/`via` column would be cleaner
for downstream filtering and is a recommended follow-up, but is not required for feasibility.)

---

## 3. Empirical test results — BOTH SUITES PASS SIMULTANEOUSLY

All runs `uv run pytest` on this worktree (base `292da6a`). E8 test file ported verbatim
from `fix/e8-temp-chain:tests/unit/test_e8_temp_chain_key_mismatch.py`.

| Suite | Baseline (no change) | E8 collapse (3-key) | **Dual-emission** |
|---|---|---|---|
| `tests/unit/test_e8_temp_chain_key_mismatch.py` (8) | **6 pass / 2 fail** | **8 pass** | **8 pass** |
| `tests/integration/test_temp_table_namespacing.py` (21) | 21 pass | **2 FAIL** | **21 pass** |
| `tests/unit/test_temp_table_namespacing.py` (32) | 32 pass | **1 FAIL** | **32 pass** |

- **Baseline** reproduces §0.2: 2 E8 inline-through tests fail
  (`test_temp_chain_qualified_sources_map_key_registered`,
  `test_temp_column_resolves_while_star_still_skips`); namespacing all green.
- **E8 collapse** reproduces §0.2 exactly: 8/8 E8 pass, **3 namespacing fail**
  (`TestLineageChainThroughTemp::test_column_lineage_edges_span_the_temp`,
  `TestSchemaAliasDualWriteRegression::test_lineage_chain_through_temp_with_alias`,
  `TestLineageLeafStamping::test_column_lineage_src_from_temp_is_namespaced`).
- **Dual-emission: 8/8 E8 AND 53/53 namespacing PASS together.** The E8 tests assert the
  leaf→sink edge EXISTS (the `TEMP_INLINE` edge satisfies); the namespacing tests assert the
  temp stays visible with edges on both sides (the structural hops are untouched → satisfies).

Direct edge probe on E8's own alias fixture under dual-emission (`USE SCHEMA BA_TMP;
CREATE OR REPLACE TEMPORARY TABLE tmp_inkoop AS SELECT col FROM da.leaf_src;
INSERT INTO ba_tmp.final_tgt SELECT col FROM tmp_inkoop;`, alias `ba_tmp→ba`):

```
stmt0 da.leaf_src.col      -> etl/x.sql::ba.tmp_inkoop.col [PASS_THROUGH]   ← into temp
stmt1 etl/x.sql::ba.tmp_inkoop.col -> ba.final_tgt.col     [PASS_THROUGH]   ← out of temp (VISIBLE)
stmt1 da.leaf_src.col      -> ba.final_tgt.col             [TEMP_INLINE]    ← additive transitive
```
All three coexist. Cake had; cake eaten.

### 3.1 Full-suite collateral check (dual-emission applied)

- **Full unit suite: 1008 passed, 1 skipped, 1 xfailed — 0 failures.**
- **Full integration suite: 503 passed, 3 skipped — 0 failures.**
- **All four perf-invariant suites pass UNMODIFIED:**
  `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`,
  `test_upsert_batch_invariant.py`, `test_perf_scaling_guard.py` (13 passed). The scaling
  guard does not trip — the composition is not in the per-column qualify/scope path.
- `ruff check` clean on the spiked file.

This is the strongest possible result: the additive edge changes nothing that any existing
test pins, *including* the golden/anchor lineage tests (which use `>= 1` lower-bound
assertions on raw COLUMN_LINEAGE and so are insensitive to additive edges — see §6).

---

## 4. The `da_tmp→da` alias self-loop trap — HANDLED

The project drops `src==tgt` self-loops after the `ba_tmp→ba` alias remap. The transitive
composition can produce a self-loop in the **plain self-reload** case
(`CREATE TEMP tmp AS SELECT col FROM da.t; INSERT INTO da.t SELECT col FROM tmp;` — the
temp reloads its own source table). Probe under dual-emission:

```
===== SELF-RELOAD (src==sink) =====
  stmt0 da.t.col -> etl/x.sql::da.tmp_x.col [PASS_THROUGH]
  stmt1 etl/x.sql::da.tmp_x.col -> da.t.col [PASS_THROUGH]
  TEMP_INLINE edges: 0      ← self-loop da.t.col -> da.t.col correctly dropped
```

The spike's explicit guard (`if F.src.full_id == E.dst.table.full_id and F.src.name ==
E.dst.name: skip`) drops the spurious `da.t.col → da.t.col` edge **before** emission. No
spurious self-loop is created; the structural hops (which legitimately show the reload
through the temp) remain. **Recommendation:** the production version should compute the
self-loop check on the **post-alias** identities (the structural edges are already
alias-normalized by the time this pass runs, per
[`base.py:993-1020`](../../src/sqlcg/parsers/base.py#L993)), so the guard sees `da.t == da.t`
even when the SQL wrote `ba_tmp` — verified correct in the probe above (alias applied).

---

## 5. THE FAN-OUT CASE — quantified

One temp read by N sinks. Synthetic measurements (`transform != TEMP_INLINE` = structural):

| ncols | nsinks | structural | TEMP_INLINE | total | inline/struct |
|---|---|---|---|---|---|
| 1 | 1 | 2 | 1 | 3 | 0.50 |
| 5 | 5 | 30 | 25 | 55 | 0.83 |
| 10 | 10 | 110 | 100 | 210 | 0.91 |
| 20 | 20 | 420 | 400 | 820 | 0.95 |
| 10 | 50 | 510 | 500 | 1010 | 0.98 |

**The common case is BOUNDED, not multiplicative.** With the realistic assumption of
**one source per temp column** (a column is produced from one place), the transitive edge
count = `ncols × nsinks` = **exactly the count of structural `temp→sink` edges**. So
`TEMP_INLINE edges ≈ 0.5 × total edges` and the ratio asymptotes to ~1.0× the structural
temp→sink half. This is the **same order** as the structural edges E8/namespacing already
produce — adding the file's edge count by roughly the temp→sink fraction. No explosion.

**The genuine multiplication risk is fan-IN × fan-OUT at the same column.** When a single
temp column is fed by **M** sources (e.g. `COALESCE(a, b, c)`) **and** the temp is read by
**N** sinks, the composition emits **M × N** transitive edges for that column:

| fan-in M | fan-out N | structural | TEMP_INLINE | (M×N) |
|---|---|---|---|---|
| 3 | 3 | 6 | 9 | 9 |
| 5 | 10 | 15 | 50 | 50 |
| 10 | 10 | 20 | 100 | 100 |

This is the only place super-linear growth lives. It requires a multi-source column AND a
multi-sink temp simultaneously — a narrow pattern. On the real DWH corpus this could not be
measured (no corpus present on this box), so **the production runbook MUST measure**
`max(fan_in × fan_out)` per temp-column and the total `TEMP_INLINE` edge delta on the
1,335-file corpus before merge. **Predicted floor:** since the prior collapse A/B measured
**+1,728 leaf→sink edges**, dual-emission emits **at least** those same +1,728 as
`TEMP_INLINE` edges (collapse produced exactly the source→sink edges that dual-emission now
adds non-destructively) — comfortably above the gate's **+1,000** `resolvable_write_col_edges`
floor. The fan-in×fan-out cases would push it *higher*, not lower.

---

## 6. Consumer impact

### 6.1 Recall / coverage metrics — INFLATION is real; recommend filtering

The metric queries in [`coverage.py`](../../src/sqlcg/cli/coverage.py) count COLUMN_LINEAGE
rows with **no `transform` filter**:

- **`resolvable_write_col_edges`** ([`coverage.py:358-365`](../../src/sqlcg/cli/coverage.py#L358)):
  `COUNT(*) FROM COLUMN_LINEAGE cl JOIN SqlQuery q … WHERE q.kind IN (write) AND EXISTS
  (SELECTS_FROM)`. The `TEMP_INLINE` edge carries the **INSERT's** `query_id` (a write query
  with a SELECTS_FROM), so it **IS counted**. Under dual-emission this metric rises by the
  full `TEMP_INLINE` count **on top of** the structural `temp→sink` edges that already
  counted → the same source→sink provenance is **double-counted via two paths** (the hop
  path and the shortcut). The +1,000 gate would pass trivially but for a partly inflated
  reason.
- **`good_edges_strict` / `good_edges_scoped`** ([`coverage.py:48-90`](../../src/sqlcg/cli/coverage.py#L48)):
  `TEMP_INLINE` edges land `real_src → real_sink` (dst is a real table, in HAS_COLUMN, and
  NOT `cte/derived/temp`), so they count as **both** strict-good and scoped-good. This
  **inflates the numerator** and would *raise* the health % — masking the −1pp denominator
  dip the revival plan expected, but dishonestly (it is the same edge counted twice).

**Recommendation (metric-counting policy):** the recall/health metrics should count
**structural edges only** and **exclude `transform='TEMP_INLINE'`** (add `AND cl.transform
<> 'TEMP_INLINE'` to the three queries). Rationale: the transitive edge is a *derived
convenience view*, not new resolution — the resolution was already captured by the two
structural hops. Counting it double-counts. The honest recall signal is "did the temp→sink
hop resolve?", which the structural edge already answers. (If a product wants a "transitive
reach" KPI, add a **separate** counter `transitive_col_edges = COUNT(*) WHERE transform =
'TEMP_INLINE'` — do not fold it into recall/health.) **This is the single most important
follow-up: without the filter, the gate metric becomes uninterpretable.**

### 6.2 Golden / anchor lineage tests (issue #40) — NOT broken

`test_live_anchors.py` / `test_anchor_tools.py` traverse raw COLUMN_LINEAGE bypassing the
CLI/MCP filter layer, but assert **lower bounds** (`assert len(rows) >= 1`,
`count(*) >= 1`). Additive edges only ever *increase* these counts → no golden assertion
breaks (confirmed: full integration green). The exact-`==` assertions in `test_anchor_tools.py`
are on `result.definitions` / `downstream_count == len(affected_tables)` (table-level,
SELECTS_FROM-driven), not on COLUMN_LINEAGE edge *counts*, so they are unaffected.
**No filter-layer change is required for tests to pass** — but see §6.3 for the
presentation concern.

### 6.3 `analyze impact` / trace / table_graph.html — needs a view selector

- **Column trace BFS** ([`tools.py:759-873`](../../src/sqlcg/server/tools.py#L759)) traverses
  the COLUMN_LINEAGE closure via `TRACE_COLUMN_LINEAGE` ([`queries.sql`]), which selects
  `cl.transform` but **does not filter it**. Under dual-emission, tracing upstream from
  `final_tgt.col` returns **both** `tmp.col [PASS_THROUGH]` **and** `leaf_src.col
  [TEMP_INLINE]` as direct parents — i.e. a shortcut edge appears *alongside* the two-hop
  path. The BFS visited-set dedups *nodes*, so the **blast-radius node set is unchanged**
  (no double-counting of affected tables), but the rendered **edge list / Mermaid diagram**
  ([`tools.py:73-96`](../../src/sqlcg/server/tools.py#L73)) would show a redundant shortcut
  edge next to the real hops — visually confusing.
- **Fix:** the consumer layer must pick a view. Default `analyze impact` (structural blast
  radius) should **filter OUT `transform='TEMP_INLINE'`** (the BFS already reaches the leaf
  via the hops, so the shortcut adds nothing to reachability and only clutters the diagram).
  Offer the `TEMP_INLINE`-only view as an opt-in "source→sink (temps elided)" mode for users
  who want the one-hop answer "what real table feeds this column?". This is a small filter
  addition in `tools.py`, not a schema or parser change.
- **`table_graph.html` / table-level blast radius** read **SELECTS_FROM** (table-level),
  not COLUMN_LINEAGE — so they are **completely unaffected** by dual-emission. No
  double-traversal of pr-impact at the table level.

---

## 7. HARD PERF GATE — the 3-minute full-corpus index budget

**Assessment: dual-emission stays within budget on a real box. High confidence.**

This box is a potato (3.8 GB RAM, swap-thrashes; full-corpus wall-times here run 4–6 min vs
~210–256 s canonical — directional only, per `project_indexer_perf_baseline`). So the
verdict rests on **edge-count delta + algorithmic complexity**, not raw seconds:

(a) **Complexity.** `_emit_transitive_temp_edges` is `O(sum over temps of fan_in × fan_out)`
— a pure Python pass over edges already in memory. It builds one `dict[temp_full_id →
incoming_edges]` (O(edges)) then one pass over out-of-temp edges. **No `exp.expand()`,
`sg_lineage`, `qualify`, or `build_scope` call** — the forbidden hot-loop cost class
([`CLAUDE.md`](../../CLAUDE.md) "body_scope built once per statement", "exp.expand only
file-level sources") is untouched. It is **not** a quadratic graph traversal (single-hop
composition, bounded per temp).

(b) **Touches registration/post-loop only.** It runs once per file *after* the statement
loop, mutating `ParsedFile` lists. It does not enter the per-column loop, the bulk-upsert
path, or any cross-file traversal. The four perf-invariant suites pass UNMODIFIED (§3.1) —
including `test_perf_scaling_guard.py`, which would flag any new op that started scaling
with column/edge count.

(c) **Measured cost (isolated).** On a pathological 200-statement file (50 temps × 10 cols ×
3 sinks = 3,500 edges, 1,500 of them `TEMP_INLINE`), the composition step alone takes
**3.5 ms** vs **441 ms** total parse time for that file — **<1% of parse time**, and that is
the worst-case temp-density. A typical ETL file (1–3 temps) costs microseconds.

(d) **Edge-count delta.** Adds ≈ the structural `temp→sink` edge count per file (the +1,728
floor from the collapse A/B, non-destructively). This grows the COLUMN_LINEAGE *upsert*
volume by that fraction — a bulk-write cost, not a parse cost, and bulk upsert is already
batched ([`indexer.py` `_flush_row_batch`]). Even a generous 2× COLUMN_LINEAGE row growth
rides the existing batched path.

**Caveat (loud):** if the real corpus has temps with high **fan-in × fan-out** at the same
column (§5), the `TEMP_INLINE` delta could be a multiple of +1,728. The runbook MUST measure
`max(fan_in × fan_out)` and the total delta on the real corpus. If that delta is, say, >10×
the structural edge count, the bulk-upsert volume (not parse time) could pressure the budget
— at which point a **cap** (skip transitive emission for a temp whose `fan_in × fan_out`
exceeds a threshold, e.g. emit only the structural hops + a single `TEMP_INLINE` per
(distinct_src, distinct_sink) pair) keeps it bounded. I do **not** expect this to fire on a
normal ETL corpus (COALESCE-fed multi-source temp columns that are also multi-sink are rare),
but it is the one thing that could move the budget and must be measured, not assumed.

---

## 8. Recommended runbook (FEASIBLE path)

1. **Parser:** add `AnsiParser._emit_transitive_temp_edges(out)`, call it from `parse_file`
   before the namespace reset at [`ansi_parser.py:249`](../../src/sqlcg/parsers/ansi_parser.py#L249).
   Compose `role='temp'` src edges with the temp's incoming edges, match on column name,
   tag `transform='TEMP_INLINE'`, drop self-loops on post-alias identity. Leave the
   `sources_map` registration **bare-key-only** (do NOT apply E8's qualified-key writes —
   the inlining is what regresses namespacing). **~45 LOC, one new method, off the hot path.**
   - *(Optional, recommended)* add a `via STRING` or `derived BOOLEAN` column to
     `COLUMN_LINEAGE` in [`schema.cypher`](../../src/sqlcg/core/schema.cypher) for cleaner
     downstream filtering than overloading `transform`. Not required for correctness.
2. **Metrics (REQUIRED):** add `AND cl.transform <> 'TEMP_INLINE'` to
   `_Q_RESOLVABLE_WRITE_COL_EDGES`, `_Q_EDGE_HEALTH_STRICT`, `_Q_EDGE_HEALTH_SCOPED`
   ([`coverage.py:48-90,358`](../../src/sqlcg/cli/coverage.py#L48)) so recall/health count
   structural edges only (no double-count). Optionally add a separate `transitive_col_edges`
   counter.
3. **Consumer (`analyze impact`):** filter `transform='TEMP_INLINE'` OUT of the default
   structural trace in [`tools.py`](../../src/sqlcg/server/tools.py); add an opt-in
   "source→sink (temps elided)" view. Table-level (`table_graph`/SELECTS_FROM) needs nothing.
4. **Tests:** port `tests/unit/test_e8_temp_chain_key_mismatch.py` verbatim; ADD a
   `test_dual_emission_*` that asserts (a) all three edges coexist on the alias fixture,
   (b) self-reload produces no `TEMP_INLINE` self-loop, (c) the metric query with the
   `TEMP_INLINE` filter counts structural only. Keep all namespacing tests green.
5. **Live-DWH A/B (REQUIRED gate):** measure on the real corpus — (i) `resolvable_write_col_edges`
   structural delta ≥ +1,000 (predicted ≥ +1,728), (ii) total `TEMP_INLINE` delta and
   `max(fan_in × fan_out)` per temp-column (the §5/§7 multiplication check), (iii) parse+CLONE+ingest
   phase under ~2m30 via `profile=True`. If the `TEMP_INLINE` delta is pathologically large,
   apply the §7(d) cap.
6. **Version:** minor bump (new resolution surface, additive, no break) per
   [`CLAUDE.md`](../../CLAUDE.md) "Releasing".

---

## 9. Predicted gate outcomes (for the runbook author)

| Gate | Prediction | Basis |
|---|---|---|
| Both test sets green | **YES (proven)** | §3 — 8/8 E8 + 53/53 namespacing, this experiment |
| Full suite green | **YES (proven)** | §3.1 — 1008 unit + 503 integration, 0 failures |
| Perf invariants UNMODIFIED | **YES (proven)** | §3.1 — 4 suites, 13 passed; §7 — off hot path |
| `resolvable_write_col_edges` ≥ +1,000 | **YES** (structural-counted) | §5/§7 — ≥ +1,728 floor from collapse A/B |
| Strict/scoped good-count no drop | **YES** | additive only; structural edges untouched |
| 3-min index budget | **YES** (measure fan-in×out) | §7 — composition <1% parse time, O(edges) |
| Self-loop trap | **HANDLED (proven)** | §4 — 0 spurious self-loops on self-reload |

---

## 10. Bottom line

The revival plan §0.2 framed this as an irreducible product fork ("pick which lineage shape
the product wants"). **It is not.** The fork was an artifact of E8's *destructive* mechanism
(inline-via-`exp.expand`). A *non-destructive* additive composition gives the product **both**
shapes at once — the temp stays a visible, queryable intermediate node (namespacing intent
preserved) **and** the end-to-end source→sink edge is recoverable (E8 recall intent
satisfied), distinguished by `transform='TEMP_INLINE'`. Proven: both suites green, whole
suite green, off the hot path. The remaining work is honest accounting (metric filter to
avoid double-count) and a consumer view selector — both small, both characterized above.

**Cake: had. Cake: eaten.**

---

### Reproduction notes
- Base commit: `292da6a` (worktree `agent-ab0d99b216b5e12f3`).
- E8 tests sourced from `fix/e8-temp-chain:tests/unit/test_e8_temp_chain_key_mismatch.py`.
- All spike code was reverted; this doc is the only committed artifact.
- Spike helper, probes, fan-out/timing scripts are described inline (not committed) — they
  re-derive trivially from §1.2 and §5/§7.
