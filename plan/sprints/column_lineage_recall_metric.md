# Feature Plan: Column-Lineage Recall Metric (`resolvable_write_col_edges`)

## Summary

Add one falsifiable `sqlcg gain` / `db info` metric — **column-lineage edge volume on
resolvable-source write queries** — that is the *only* graph-derived ruler proven (by
direct probe) to move monotonically with the recall improvement the E8 temp-chain fix
delivers. This is the **measure-first gate** for deciding whether to revive E8
(closed PR [#111](https://github.com/Warhorze/sql-code-graph/pull/111); tracked under
issue #38). This plan is metric **design + spec only** — it does not touch the parser
and does not implement E8.

## Context — why a new metric is needed (measured, not assumed)

E8 (the `col_lineage_skip:dynamic_source` temp-chain `sources_map` multi-key fix) was
measured before/after on the live DWH and adds **+1,728 COLUMN_LINEAGE edges**
(+1,080 strict-good, +490 scoped-good) — real column→column lineage previously dropped
at the temp-chain lookup. Full table:
[`plan/research/e8_column_vs_island_remeasure.md`](../research/e8_column_vs_island_remeasure.md)
(commit `d28661b`, branch `worktree-agent-a37071c3b4d7ab596`).

Every existing `gain` metric is blind to this win (all measured below on the two E8
DBs `/tmp/e8_without.duckdb` and `/tmp/e8_with.duckdb`, schema v8, 1,335-file DWH):

| Existing metric | WITHOUT E8 | WITH E8 | Why it's blind |
|---|---|---|---|
| strict % | 84% | 84% | new edges land in same good/bad ratio |
| scoped % | 98% | 97% | **dips** 1pp (new edges land on CTE/derived/temp dst) |
| `col_lineage_skip:dynamic_source` raw | 1,033 | 1,100 | **rises** (+67) — wrong direction; exposes more downstream skips |
| `zero_edge_write_queries` | 889 | 919 | **rises** (+30) — E8 exposes new downstream writes |
| `cte_source_gap_writes` | 168 | 167 | flat (-1) — wins are inside already-partial queries |

## Scope

### In Scope
- One new `_Q_*` SQL constant in [`coverage.py`](../../src/sqlcg/cli/coverage.py).
- One new `CoverageStats` field, one `collect_coverage()` wiring line, one
  `render_coverage_lines()` line, one `coverage_to_json()` key.
- Probe-validated SQL + a measured master-1.26.0 baseline and a predicted post-E8 value.
- Unit/integration tests pinning the metric's value on a synthetic graph and the
  monotonicity property (more edges → metric rises).

### Non-Goals
- **Not** implementing or reviving E8 (no `ansi_parser.py` / `sources_map` change).
- **Not** touching the parser, the qualify loop, or any indexing-path code.
- **Not** a deficit/drop-count or recall-ratio metric (proven unsuitable below — §Design).
- **Not** any networkx / parse-time / per-statement re-computation. Graph-read only,
  via `run_read_routed`, exactly like the existing §G counters.

## Design

### The metric: `resolvable_write_col_edges` (a productivity counter, not a deficit counter)

> **Count of COLUMN_LINEAGE edges whose `query_id` is a write-kind query
> (`kind IN _WRITE_KINDS AND target_table <> ''`) that has ≥1 resolvable source
> (≥1 `SELECTS_FROM` row).** Higher is better; a recall improvement can only raise it.

```sql
SELECT COUNT(*) AS resolvable_write_col_edges
FROM "COLUMN_LINEAGE" cl
JOIN "SqlQuery" q ON q.id = cl.query_id
WHERE q.kind IN ('INSERT', 'MERGE', 'CREATE_TABLE', 'UPDATE')
  AND q.target_table <> ''
  AND EXISTS (SELECT 1 FROM "SELECTS_FROM" sf WHERE sf.src_key = q.id)
```

`_WRITE_KINDS` and the `target_table <> ''` discriminator are reused verbatim from the
existing `_Q_CTE_SOURCE_GAP_WRITES` constant, so the write-query population is identical
across the two §G metrics (no drift). The `EXISTS … SELECTS_FROM` clause is the
"has a resolvable source table" gate — it scopes the count to exactly the queries E8 can
help and excludes literal-only / source-less writes whose zero edges are correct-by-design.

### Why a raw edge-VOLUME counter, and NOT a drop-count or a recall ratio

The starting candidate (a column analogue of `cte_source_gap_writes`: count (query,
output-column) pairs with a resolvable source but zero COLUMN_LINEAGE edge) and **every
deficit/ratio variant of it was probed and falsified** — each moves the WRONG way under E8:

| Candidate (probed on the two E8 DBs) | WITHOUT | WITH | Verdict |
|---|---|---|---|
| query-level drop: write+SELECTS_FROM, zero COLUMN_LINEAGE | 138 | **167** | UP — wrong way |
| (query,col) drop vs HAS_COLUMN catalog denominator | 10,886 | **11,635** | UP — wrong way |
| catalog-denominator recall ratio | 63.2% | **60.7%** | DOWN — wrong way |
| (query, source-table) drop, catalogued source | 490 | **976** | UP — wrong way |
| (query, source-table) recall ratio | 80.0% | **60.2%** | DOWN — wrong way |
| distinct (query,col) resolved pairs | 20,491 | 19,715 | DOWN — wrong way |
| queries-with-source that resolve ≥1 col | 1,090 | 1,060 | DOWN — wrong way |
| **edges on resolvable-source writes (chosen)** | **25,246** | **27,036** | **UP +1,790 — correct** |
| edges on all writes (target_table<>'') | 34,117 | 35,815 | UP +1,698 — also correct |

**Root cause that kills every deficit/ratio design:** E8's mechanism enlarges the
*unresolved universe* faster than it resolves individual units. By resolving the temp body,
E8 lets `exp.expand()` proceed further into *downstream* statements that then newly acquire
a `SELECTS_FROM` source but still hit `dynamic_source` on their own columns — so the count
of "(query / column / source-table) with a source but no edge" **rises** with E8. The same
mechanism adds *more source edges to columns that already resolved* (depth, not breadth),
so "distinct resolved columns/queries" **falls**. Both numerator and denominator of any
recall ratio churn in opposite directions and the ratio is not monotone. A drop-count is
fooled by exactly the same downstream-exposure pollution that fools the raw
`col_lineage_skip:dynamic_source` count the task warned us about (they share a cause).

**Why edge-volume is robust where the ratio is not:**
- It counts edges that *actually exist*, never edges that *should* exist. E8 can only add
  COLUMN_LINEAGE rows (it removes a lookup-miss → it never deletes an edge), so the count
  is **monotonic non-decreasing in the improvement direction by construction.**
- It is immune to the rising `dynamic_source` count: that count tallies parse-time *skip
  events*; this metric tallies *emitted edges*. A skip event produces no edge, so newly
  exposed downstream skips cannot inflate it.
- The `EXISTS … SELECTS_FROM` gate scopes it to the population E8 targets, so the signal
  is not diluted by source-less writes.

**Corpus-sensitivity trade-off (argued, per the task):** a ratio is normally preferred for
cross-corpus comparability, but here the denominator (the "expected" or "attempted" column
universe) is **not observable in the graph** — `SELECTS_FROM` is query→table with no column
granularity, and the HAS_COLUMN catalog over-counts (a write rarely populates every column
of its target). Any denominator we can build is itself E8-sensitive and churns, which is
exactly why all five ratio/drop probes failed monotonicity. An absolute edge count is
admittedly corpus-sized — but this metric's job is **falsifiable within-corpus before/after
comparison on the same DWH** (the E8 revival gate), not cross-corpus benchmarking. For that
job a monotone absolute counter is strictly more robust than a non-monotone ratio. The metric
is rendered as a labelled count alongside the existing absolute §G counters
(`zero_edge_write_queries`, `cte_source_gap_writes`), all of which are corpus-sized, so it
fits the established surface.

### API Changes
None. Same CLI surface (`sqlcg gain`, `sqlcg gain --json`, `sqlcg db info`); one extra
render line and one extra JSON key — additive, no flag, no breaking change.

### Data Models
[`coverage.py`](../../src/sqlcg/cli/coverage.py) `CoverageStats` gains one field:

```python
# --- column-lineage recall (column_lineage_recall_metric.md) ---
# COLUMN_LINEAGE edges on write-kind queries that have a resolvable SELECTS_FROM
# source. Monotone-up productivity counter — the E8 revival gate (issue #38,
# closed PR #111). Baseline (master 1.26.0, /tmp/e8_without.duckdb proxy): 25,246.
resolvable_write_col_edges: int = 0
```

No new property (it is an absolute count, like `phantom_edges` / `blindspot_tables`).

### Dependencies
None new. `run_read_routed`, `_WRITE_KINDS`, `_WRITE_KINDS_SQL` already exist in
`coverage.py`.

## Implementation Steps

### Phase 1: Metric wiring (single PR)

**Step 1.1**: Add the SQL constant.
- File: [`coverage.py`](../../src/sqlcg/cli/coverage.py) — new `_Q_RESOLVABLE_WRITE_COL_EDGES`
  constant after `_Q_CTE_SOURCE_GAP_WRITES`, using `_WRITE_KINDS_SQL` (do not re-list kinds).
- Acceptance: the constant's SQL is byte-for-byte the probe-validated query in §Design
  (modulo the `f"… {_WRITE_KINDS_SQL} …"` interpolation).

**Step 1.2**: Add the `CoverageStats` field (shown in §Data Models).
- File: [`coverage.py`](../../src/sqlcg/cli/coverage.py) `CoverageStats`.
- Acceptance: field defaults to `0`; docstring cites this plan + the baseline 25,246.

**Step 1.3**: Wire `collect_coverage()`.
- File: [`coverage.py`](../../src/sqlcg/cli/coverage.py) — add
  `q_rwce = run_read_routed(_Q_RESOLVABLE_WRITE_COL_EDGES, {})`, read
  `row_rwce.get("resolvable_write_col_edges")`, pass into the `CoverageStats(...)` ctor.
- Acceptance: `collect_coverage()` on a populated graph returns a `CoverageStats` whose
  `resolvable_write_col_edges` equals the direct SQL count.

**Step 1.4**: Add the render line.
- File: [`coverage.py`](../../src/sqlcg/cli/coverage.py) `render_coverage_lines()`, in the
  recall block next to the `cte_source_gap_writes` line:
  ```python
  lines.append(
      f"{indent}Column-lineage edges on resolvable-source writes: "
      f"{coverage.resolvable_write_col_edges}"
  )
  ```
- Acceptance: line appears in both `sqlcg gain` §G and `sqlcg db info` (shared renderer).

**Step 1.5**: Add the JSON key.
- File: [`coverage.py`](../../src/sqlcg/cli/coverage.py) `coverage_to_json()`:
  `"resolvable_write_col_edges": coverage.resolvable_write_col_edges`.
- Acceptance: `sqlcg gain --json` includes the key with the correct value.

**Step 1.6**: Version bump (new metric surface ⇒ **minor**).
- [`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py)
  `1.26.0` → `1.27.0`; `uv lock`.

## Test Strategy

- **Unit — value correctness**: build a small in-memory DuckDB graph with (a) a write
  query that has a `SELECTS_FROM` row and 3 `COLUMN_LINEAGE` edges, (b) a write query with
  a `SELECTS_FROM` row but 0 edges, (c) a write query with 2 edges but **no** `SELECTS_FROM`
  row, (d) a non-write (SELECT) query with edges. Assert `resolvable_write_col_edges == 3`
  (only the edges from (a) are counted). Verifies the `SELECTS_FROM` gate, the kind filter,
  and the `target_table <> ''` discriminator each exclude the right rows.
- **Unit — monotonicity property**: add one more `COLUMN_LINEAGE` edge to query (a) and
  re-collect; assert the metric strictly increased and never decreased. Pins the
  "edges can only be added" invariant that makes this a valid E8 ruler.
- **Unit — render + json presence**: assert the new render line text and the new JSON key
  appear with the computed value (observable output, not just no-exception).
- **Integration**: run `collect_coverage()` against a real indexed in-memory graph and
  assert the field equals an independent direct `COUNT(*)` of the same SQL — guards against
  the field silently reading the wrong row key.

> Tests are named `test_<unit>_<scenario>_<expected>` and link this plan in the docstring.

## Acceptance Criteria

- [ ] `sqlcg gain` and `sqlcg db info` both print a
      `Column-lineage edges on resolvable-source writes: <N>` line (shared renderer).
- [ ] `sqlcg gain --json` includes `"resolvable_write_col_edges": <N>` equal to the
      direct SQL `COUNT(*)`.
- [ ] On the synthetic 4-query fixture, the metric equals **3** (only edges from the
      write query that has both a `SELECTS_FROM` source and emitted edges).
- [ ] Adding one COLUMN_LINEAGE edge to a counted query strictly increases the metric;
      no graph mutation that only adds edges ever decreases it (monotonicity test green).
- [ ] On the master-1.26.0 baseline graph the metric reads **25,246**
      (proxy `/tmp/e8_without.duckdb`); the predicted post-E8 value is **≈27,036**
      (+1,790, consistent with the +1,728 global COLUMN_LINEAGE delta — the small surplus
      is the share of E8 edges landing on resolvable-source writes vs. all writes).
- [ ] No change to any file under `src/sqlcg/parsers/` or `src/sqlcg/indexer/`.

## Baseline and predicted post-E8 value (the falsifiable gate)

| Quantity | Value | Source |
|---|---|---|
| Graph probed (baseline = master proxy) | `/tmp/e8_without.duckdb`, schema v8, 1,335 files, pre-E8 `ansi_parser.py` | confirmed master has no E8 multi-key block (`git show master:…ansi_parser.py`) |
| **Baseline `resolvable_write_col_edges`** | **25,246** | direct probe |
| **Predicted post-E8** | **≈27,036** (+1,790) | probe of `/tmp/e8_with.duckdb` |
| Cross-check (all-write edges) | 34,117 → 35,815 (+1,698) | probe; brackets the +1,728 global delta |

**Gate rule for the E8 revival decision (issue #38, PR #111):** revive E8 only if, after a
from-scratch DWH reindex with the fix, `resolvable_write_col_edges` rises by **≥ +1,000**
over the pre-E8 baseline measured by the *same* metric on the *same* corpus. The probe
predicts +1,790; a result materially below +1,000 would falsify the expected win and the
revival should be reconsidered. (Threshold chosen below the predicted +1,790 to absorb
corpus drift, well above noise — the metric moved by ≤1 across the unrelated existing §G
counters.)

## Coverage.py additive-pattern fit (confirmed)

Mirrors `cte_source_gap_writes` exactly — same five touch-points, same shared renderer so
both `gain` and `db info` surface it with no duplicate code:

| Touch-point | Existing analogue | This metric |
|---|---|---|
| `_Q_*` SQL constant | `_Q_CTE_SOURCE_GAP_WRITES` | `_Q_RESOLVABLE_WRITE_COL_EDGES` |
| `CoverageStats` field | `cte_source_gap_writes: int = 0` | `resolvable_write_col_edges: int = 0` |
| `collect_coverage()` line | `run_read_routed(_Q_CTE_SOURCE_GAP_WRITES, {})` | `run_read_routed(_Q_RESOLVABLE_WRITE_COL_EDGES, {})` |
| render line | "CTE-wrapped writes missing SELECTS_FROM source:" | "Column-lineage edges on resolvable-source writes:" |
| `coverage_to_json()` key | `"cte_source_gap_writes"` | `"resolvable_write_col_edges"` |

Read path is `run_read_routed` (server-aware); no networkx, no parse-time work, no
indexing-path change — consistent with the file's stated contract.

## Version Impact
New observable metric surface (render line + JSON key) ⇒ **minor** bump `1.26.0 → 1.27.0`.
No backward-compat shim needed (additive JSON key, additive line).

## Risks and Mitigations

- **Risk: an absolute count is corpus-sized and not portable across repos.** Mitigation:
  this is by design — the metric exists for within-corpus before/after E8 comparison; it is
  labelled and grouped with the other absolute §G counters; the §Design argument documents
  why a ratio is *less* robust here (non-observable, E8-churning denominator).
- **Risk: someone later "improves" it into a recall ratio.** Mitigation: the §Design table
  records the five falsified ratio/drop variants with measured numbers so the regression is
  not silently re-introduced; the monotonicity unit test would go red on a ratio.
- **Risk: the metric rises for a reason other than E8** (e.g. more write queries indexed).
  Mitigation: the gate rule requires the *same corpus* (same `indexed_sha`, already surfaced
  in §G fingerprint) for the before/after comparison.

---

## Measure-first note (the E8 gate)

This metric is the **prerequisite ruler** for the E8 revival decision under issue #38.
E8 (closed PR #111) is a measured +1,728-edge column-lineage win that no current `gain`
metric can see. Per the project's measure-before-improve rule, this metric ships **first**
and establishes the falsifiable baseline (25,246) and predicted post-E8 value (≈27,036);
only then does reviving E8 become a provable win or a falsified one. Implementing E8 itself
is explicitly out of scope for this plan.
