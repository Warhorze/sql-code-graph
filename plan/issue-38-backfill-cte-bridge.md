# Feature Plan: #38 residual — backfill mis-orders + leaks synthetic node for CTE-wrapped INSERT…SELECT

## Summary
The live "backfill drops the CTE-bridged table" report was **imprecise**: there is **no
membership drop**. Empirical reproduction by plan-review confirmed the residual #38 bug is
**TWO independent, confirmed-real defects** for CTE-wrapped `INSERT … WITH cte AS (…) SELECT
… FROM cte` statements:

1. **Mis-ordering** — `get_backfill_order` is **alphabetical, not causal** for CTE-bridged
   chains, because **no `SELECTS_FROM` adjacency edge is emitted** for these statements, so
   [`_kahn_topological_sort`](../src/sqlcg/server/tools.py#L424) sees all-indegree-0 and
   falls back to `sorted()` on the frontier ([line 450](../src/sqlcg/server/tools.py#L450)).
   Fix: rebuild Kahn adjacency from the `COLUMN_LINEAGE` closure (Option A).
2. **Synthetic-node leak** — the synthetic `cte` / `derived` `SqlTable` node is emitted as a
   **rebuildable table** by `get_change_scope`, `get_backfill_order`, `diff_impact`, and
   (transitively) `scope_change`. Fix: exclude on `SqlTable.kind`.

Neither defect is a membership "drop": the reviewer empirically confirmed both
`get_change_scope` and `get_backfill_order` return the **same table set**. This plan
**reproduces and pins** both defects first (Phase 1), then ships both fixes and a permanent
cross-tool consistency regression test (containment + ordering + synthetic-exclusion — **not**
set-equality). Ships as a **patch** bump (1.4.1 → 1.4.2).

> Static reads in this plan are pinned to current `master`. Line numbers are anchors,
> not exact; the developer re-greps before editing.
>
> **Topology fact (empirically reproduced, load-bearing for the whole plan):** indexing
> `INSERT INTO s.tableb WITH cte AS (SELECT … FROM s.tablea) SELECT … FROM cte;` produces
> these raw `COLUMN_LINEAGE` edges:
> ```
> s.tablea.id -> cte.id       (CTE_PROJECTION, conf 1.0)   <- DEAD-END LEAF (no outgoing edge)
> s.tablea.id -> s.tableb.id  (SELECT,          conf 1.0)  <- DIRECT edge, bypasses cte
> ```
> There is **NO** `cte -> s.tableb` edge. The synthetic `cte` node is a **disconnected
> dead-end side-branch** fed from the same source columns; the real producer→consumer
> relationship is the **DIRECT** `s.tablea -> s.tableb` `COLUMN_LINEAGE` edge that bypasses the
> CTE node. There is **no multi-hop bridge through the CTE node** — earlier drafts of this plan
> assumed one; that assumption was WRONG.

---

## Scope

### In Scope
- Reproduce **both** confirmed defects on a CTE-wrapped INSERT…SELECT chain and pin them with
  captured raw rows (Phase 1 mandatory gate — kept as confirmed-good).
- Fix **defect 1 (mis-ordering)** via Option A: rebuild Kahn adjacency from the
  `COLUMN_LINEAGE` closure (which carries the direct `tablea→tableb` edge), since **no
  `SELECTS_FROM` adjacency exists** for CTE-wrapped INSERT…SELECT.
- Fix **defect 2 (synthetic-node leak)** via `SqlTable.kind`-based exclusion at the rollup
  boundary, shared across all four tools.
- A reusable integration fixture capturing a **3-table producer chain**
  (`s.tableA → s.tableB → s.tableC`, each a CTE-wrapped INSERT…SELECT), plus a direct
  CREATE-VIEW consumer as control. (3 tables are required because both tools filter the target
  out of its own set — see Step 3.1.)
- A cross-tool **consistency** regression test (containment + ordering + synthetic-node
  exclusion), extended across the sibling table-graph tools that share this surface.
- Patch version bump + lockfile refresh (no tag, no issue close — user verifies & tags).

### Non-Goals
- The `analyze upstream/downstream` CLI surface (already fixed in v1.1.2/v1.1.3;
  guard for it is the separate **open PR #56** — do **not** fold into it).
- Re-tuning confidence filters. Both reproduced edges (the DIRECT `SELECT` edge and the
  dead-end `CTE_PROJECTION` leaf) are **conf 1.0**; confidence is not implicated in either
  defect. Do not add a confidence gate.
- Changing the COLUMN_LINEAGE traversal semantics or the synthetic CTE node's existence
  in the graph — only its **emission as a rebuildable table** by these tools is in scope.
- Any change to parser hot paths. If reproduction implicates the indexer/parser, STOP and
  surface a blocking question rather than touching [`base.py`](../src/sqlcg/parsers/base.py)
  / [`indexer.py`](../src/sqlcg/indexer/indexer.py).

---

## Background: what the empirical reproduction confirmed vs. the "drop" framing

The live report framed this as backfill **dropping a table from membership**. Plan-review
**empirically reproduced** the topology on an indexed CTE-wrapped INSERT…SELECT and **confirmed
that framing is wrong** — there is no membership drop. Two findings stand:

- **Membership is shared and equal (CONFIRMED by reproduction).** Both
  [`get_change_scope`](../src/sqlcg/server/tools.py#L884) and
  [`get_backfill_order`](../src/sqlcg/server/tools.py#L961) compute the affected set with
  the **same** [`_affected_columns_closure`](../src/sqlcg/server/tools.py#L341), which
  walks `COLUMN_LINEAGE` via
  [`GET_DOWNSTREAM_DEPENDENCIES`](../src/sqlcg/core/queries.sql#L61) (no kind filter), and
  apply the **same**
  [`NoiseFilter.from_config().filter_nodes`](../src/sqlcg/server/noise_filter.py#L112). The
  reviewer verified the two tools return the **same table set** — there is **no membership
  divergence** to chase.

The residual bug is **two independent, confirmed-real defects** (neither a membership drop):

1. **Mis-ordering — MISSING `SELECTS_FROM` adjacency (CONFIRMED).** Kahn builds adjacency from
   [`GET_TABLE_DIRECT_UPSTREAMS`](../src/sqlcg/core/queries.sql#L175) = `SELECTS_FROM` edges
   (`SqlQuery → SqlTable`, keyed by `q.target_table`), which is **independent** of
   `COLUMN_LINEAGE`. For a CTE-wrapped INSERT…SELECT, **no `SELECTS_FROM` row is emitted at
   all**: the reviewer confirmed `GET_TABLE_DIRECT_UPSTREAMS` returns **empty** for the target
   `s.tableb`. Root cause: [`_real_tables()`](../src/sqlcg/parsers/base.py#L371) excludes
   CTE-alias names, and the real source (`s.tablea`) is nested in the CTE body's **child**
   scope — it never surfaces into the statement's top-level `sources`, so
   [`indexer.py`'s `for src_table in stmt.sources`](../src/sqlcg/indexer/indexer.py#L1148)
   never emits the table→table edge. Both tables therefore get **indegree 0** in
   [`_kahn_topological_sort`](../src/sqlcg/server/tools.py#L424), and the zero-indegree frontier
   is ordered by `sorted()` ([line 450](../src/sqlcg/server/tools.py#L450)) = **ALPHABETICAL,
   not causal**. This is **missing adjacency, not misrouted adjacency** — there is no edge
   pointing at the synthetic node to "resolve through". (Corroborated by memory
   `project_issue38_cte_filter_regression.md`: "table↔table dependency-edge reconstruction
   requires direct table edges and drops cte_insert-bridged ones".)
2. **Synthetic node leaking as a rebuildable table (CONFIRMED).** The reviewer confirmed the
   synthetic `cte` node appears **unfiltered in BOTH tools' output**.
   [`_rollup_to_tables`](../src/sqlcg/server/tools.py#L386) emits the synthetic node's
   `table_qualified`; the noise filter has no rule for CTE-derived nodes
   ([`is_noise`](../src/sqlcg/server/noise_filter.py#L55) does string-name layers only — backup
   globs / ignore lists — **no kind exclusion**), so the synthetic node surfaces in
   `backfill_order` **and** in impact's blast radius. `SqlTable.kind` **is** the authoritative
   marker: it is set to `kind="cte"` in [`indexer.py:1207`](../src/sqlcg/indexer/indexer.py#L1207)
   for `role="cte"` and `kind="derived"` in
   [`indexer.py:1284`](../src/sqlcg/indexer/indexer.py#L1284). Keying exclusion on `kind` is
   correct; keying on the alias string is not.

---

## Design

Phase 1 still runs first to confirm both defects reproduce on the indexed graph, but both
fixes are now **named with confidence** (reproduction already done). Two fixes ship together:

### Fix 1 (defect 1 — mis-ordering): Option A, Kahn adjacency from the COLUMN_LINEAGE closure
**Option B is moot and dropped.** There is **no `SELECTS_FROM` edge pointing at the synthetic
node to "resolve through"** — the adjacency is simply **missing** (the reviewer confirmed
`GET_TABLE_DIRECT_UPSTREAMS` returns empty for the CTE-wrapped target). A query-side rewrite of
`SELECTS_FROM` has nothing to rewrite. **Option A is the only structurally sound fix.**

**Option A:** derive Kahn adjacency from the **same `COLUMN_LINEAGE` closure** already computed
for membership. That closure **does** carry the direct `s.tablea.id → s.tableb.id` edge (the
DIRECT `SELECT` edge that bypasses the CTE node — see the Summary topology fact), so rolling
the closure's edge endpoints up to tables yields the real producer→consumer adjacency. Ordering
and membership then come from **one** graph, eliminating the parallel-graph mismatch at its
root. Must preserve the cycle-degradation contract
([line 460](../src/sqlcg/server/tools.py#L460)).

**Option A's real cost (made explicit per plan-review C4):**
- [`_affected_columns_closure`](../src/sqlcg/server/tools.py#L341) currently returns **only a
  flat list of node ids — no edge pairs**. Option A therefore requires one of:
  - **(a)** extend `_affected_columns_closure`'s return contract to also carry edge endpoints
    (`(src_col, dst_col)` pairs). **3 call sites** to update:
    [`get_change_scope`](../src/sqlcg/server/tools.py#L884),
    [`get_backfill_order`](../src/sqlcg/server/tools.py#L961),
    [`diff_impact`](../src/sqlcg/server/tools.py#L1015); **or**
  - **(b)** add a **single** new aggregate `COLUMN_LINEAGE` query over the closure's column-id
    set that returns table-level `(src_table, dst_table)` pairs.
  - **Decision: prefer (b)** — it is additive (no contract change rippling to 3 call sites) and
    naturally satisfies the "once per closure" invariant. The developer records the final
    choice in Phase 2 from the Phase 1 evidence.
- **Invariant: run this adjacency build ONCE per closure**, never per-table or per-column. It
  replaces the current **N × `GET_TABLE_DIRECT_UPSTREAMS`** loop
  ([line 440](../src/sqlcg/server/tools.py#L440)) with **one** aggregate query over the
  already-computed column set.
- **Perf-guard gap (flag):** Option A **changes `_kahn_topological_sort`'s query profile** (from
  N per-table upstream queries to one aggregate adjacency query). There is **no
  [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py) coverage of
  `_kahn_topological_sort` today.** The developer must **either** add a guard counter for the
  new once-per-closure adjacency-build op (preferred) **or** explicitly document accepting the
  gap in the PR description. Do not silently leave it uncovered.
- **Cycle risk (flag):** rollup-based adjacency can create **table-level cycles the
  `SELECTS_FROM` graph did not have** (two columns of the same table pair feeding each other,
  multi-producer fan-in). Option A **must not** introduce new table-level cycles on a benign
  fixture — assert this on a multi-producer synthetic fixture (see Step 2.2 / Acceptance).

### Fix 2 (defect 2 — synthetic node leak): `SqlTable.kind`-based exclusion at the rollup boundary
Filter synthetic CTE/derived nodes out of the **rebuildable table set** in **all** tools at the
rollup boundary. Implement once and share:
- Add a CTE/derived exclusion at the `_rollup_to_tables` → noise-filter boundary, driven by
  `SqlTable.kind` (`"cte"` / `"derived"` — the authoritative marker), **not** by string-matching
  the alias. The excluded synthetic ids should be reported under `noise_excluded` (or an
  analogous channel) so the information is not silently dropped.
- This must apply identically to `get_change_scope`, `get_backfill_order`, `diff_impact`,
  and (transitively, via delegation) `scope_change`.

### Data models / API
No change to result schemas (`ChangeScopeResult`, `BackfillOrderResult`, `DiffImpactResult`,
`ScopeChangeResult`). Output stays "real tables only". (Option A choice (a), if taken, changes
the **internal** return type of `_affected_columns_closure` only — not any tool result schema.)

### Dependencies
None new.

### Performance invariants (CLAUDE.md)
- If Fix 1 touches [`queries.sql`](../src/sqlcg/core/queries.sql) or any per-table loop, keep
  traversal **once per closure**, never per-column/per-table. The new adjacency query is a
  **single aggregate** over the closure's column-id set, not a query inside the BFS. The
  `_MAX_CLOSURE_NODES` 50k cap ([line 338](../src/sqlcg/server/tools.py#L338)) stays.
- Do **not** touch [`base.py`](../src/sqlcg/parsers/base.py) /
  [`indexer.py`](../src/sqlcg/indexer/indexer.py). Note the **root cause of defect 1 lives in
  those files** (the missing `SELECTS_FROM` emission), but Option A deliberately fixes it **at
  the tool layer** (reading the already-correct `COLUMN_LINEAGE` closure) precisely to avoid
  the perf-invariant-bearing parser surface. If Phase 1 shows the `COLUMN_LINEAGE` direct edge
  is *also* missing (not just `SELECTS_FROM`), Option A is impossible → STOP (see Blocking
  Questions).

---

## Implementation Steps

### Phase 1: Reproduce & pin the exact divergence (MANDATORY FIRST — no fix yet)

**Step 1.1**: Reproduce on the indexed DWH at `/home/ignwrad/Projects/dwh`
(never commit to it; the e2e test [`tests/e2e/test_dwh_e2e.py`] is gitignored — never commit it).
- Pick a CTE-wrapped INSERT…SELECT target from the live report (voorraad-fact → monoliet →
  week-segment chain).
- Run, against the **same** indexed graph and target T:
  - `get_change_scope(T)` — record `affected_tables` (confirm the synthetic `cte`/`derived`
    node is present = defect 2 reproduces).
  - `get_backfill_order(T)` — record `backfill_order` + `noise_excluded` (confirm ordering is
    **alphabetical**, not producer-before-consumer = defect 1 reproduces; confirm the synthetic
    node is present = defect 2 reproduces).
  - **Confirm the table set is equal** between the two tools (membership is NOT the bug).
  - Raw `execute_sql` on `COLUMN_LINEAGE` for the target, capturing **two edge classes
    separately** (do **not** look for a 3-node `producer → cte → consumer` path — it does not
    exist):
    - the **DIRECT** producer→consumer edge `<upstream>.col -> <target>.col` (`transform`
      should be `SELECT`-class, `confidence` 1.0) — this is the edge Option A reads;
    - the **DEAD-END** CTE leaf `<upstream>.col -> <cte>.col` (`transform` `CTE_PROJECTION`,
      `confidence` 1.0) that has **no outgoing edge** — confirm it terminates.
  - Raw `execute_sql` on `SELECTS_FROM` for the target producing query
    (`SELECT src_key, dst_key FROM "SELECTS_FROM" WHERE q.target_table = <target>`), and
    confirm it returns **ZERO rows** for the CTE-wrapped INSERT…SELECT (= the missing adjacency
    that forces Kahn into the alphabetical fallback).
- Acceptance: a written verdict in this plan under a `### Phase 1 Verdict` heading confirming
  **both** defects reproduce (alphabetical ordering from absent `SELECTS_FROM`; synthetic node
  emitted as rebuildable), with the captured rows as evidence — and confirming the DIRECT
  `COLUMN_LINEAGE` edge exists (so Option A is viable). If either defect does **not** reproduce
  or the DIRECT edge is absent, STOP and re-scope.

### Phase 1 Verdict (developer, synthetic fixture — see Deviation 1 for the topology correction)

Both defects **reproduce**, confirmed on a synthetic 3-table CTE-wrapped INSERT…SELECT
chain (`s.tablea -> s.zzz_second -> s.aaa_third`, names chosen so alphabetical order
diverges from causal order):

- **Defect 1 (mis-ordering) reproduces**: `get_backfill_order("s.tablea")` returned
  `['s.aaa_third', 's.v_consumer', 's.zzz_second']` — the consumer `aaa_third` ordered
  *before* its producer `zzz_second` (alphabetical, not causal). `SELECTS_FROM` for the
  CTE-wrapped target queries returned **zero rows**, confirming the missing-adjacency
  root cause named in the plan.
- **Defect 2 (synthetic-node leak) reproduces**: the synthetic `cte_b`/`cte_c` nodes
  (kind=`"cte"`) appeared in `get_change_scope("s.tablea").affected_tables` and
  `get_backfill_order("s.tablea").backfill_order` before the fix.
- **Membership equality confirmed**: both tools returned the same table set (modulo
  ordering), corroborating "no membership drop."

**Topology correction (the plan's central empirical premise does not reproduce — see
Deviation 1):** the raw `COLUMN_LINEAGE` does **not** contain a DIRECT
`s.tablea.id -> s.zzz_second.id` edge running parallel to a dead-end `cte` leaf. Every
reproduction attempt (colliding alias names, distinct per-statement alias names,
column-list aliasing, bare `SELECT *`) instead produced a clean **two-hop bridge**
`producer.id -> cte.id -> consumer.id` — the synthetic node is a **transitive relay**,
not a disconnected dead-end side-branch. `cte`/`derived` nodes DO have outgoing edges
to the consumer table. Option A's underlying *strategy* (read adjacency from the
`COLUMN_LINEAGE` closure rather than `SELECTS_FROM`, at the tool layer, no parser
changes) remains correct and sufficient — only the edge-contraction mechanics differ
from the plan's literal description (see Deviation 1 for the corrected fix).

**Step 1.2**: If the DWH cannot give a clean, minimal reproduction (noise, scale), build the
synthetic graph from Step 2.1's fixture instead and pin both defects there. The synthetic
fixture topology is small enough to assert exact node ids — prefer it for the *pinning* even
if the DWH confirms the *symptom*.
- Acceptance: both defects reproduced on a graph the test suite can rebuild deterministically,
  including the empty-`SELECTS_FROM` / alphabetical-ordering signature and the synthetic-node
  leak.

### Phase 2: Conditional fix (only after Phase 1 verdict)

**Step 2.1**: Build the shared reusable integration fixture (needed by both fixes and the
test; build it first so Phase 1.2 can use it).
- New file: `tests/integration/test_backfill_impact_consistency.py` with a module-level
  fixture (pattern mirrors
  [`test_bare_column_cte_lineage.py`](../tests/integration/test_bare_column_cte_lineage.py#L80)
  — `db` + `indexed_db` fixtures, `Indexer().index_repo(..., use_git=False)`).
- Topology in the fixture corpus — a **3-table producer chain** (REQUIRED, not a 2-table
  bridge — see the ordering-assertion constraint below):
  - DDL for `s.tableA`, `s.tableB`, `s.tableC`.
  - `INSERT INTO s.tableB … WITH cte AS (SELECT … FROM s.tableA) SELECT … FROM cte;`
  - `INSERT INTO s.tableC … WITH cte AS (SELECT … FROM s.tableB) SELECT … FROM cte;`
  - Each INSERT produces, per the reproduced topology: a **DIRECT** `COLUMN_LINEAGE` edge
    (`s.tableA→s.tableB`, `s.tableB→s.tableC`) that bypasses the CTE node, **plus** a
    disconnected **dead-end** `… → cte` leaf with no outgoing edge. There is **no**
    `cte → target` edge — do not assert one.
  - A direct `CREATE VIEW s.v_consumer AS SELECT … FROM s.tableA;` as the **control**
    (non-CTE consumer that both tools must already agree on).
- **Why 3 tables:** both tools filter the **target out of its own set** (`affected_tables_all =
  [t for t in _rollup_to_tables(affected_cols) if t != target]`,
  [tools.py:923](../src/sqlcg/server/tools.py#L923) /
  [tools.py:987](../src/sqlcg/server/tools.py#L987)). With T = `s.tableA`, `s.tableA` can
  **never** appear in `get_backfill_order(s.tableA)`, so a "tableA before tableB" assertion is
  **unsatisfiable** (`index()` raises). The chain gives two **downstream, non-target** members
  (`s.tableB`, `s.tableC`) whose relative order is assertable.
- Acceptance: indexing the fixture yields, per chain link, a **DIRECT** `COLUMN_LINEAGE`
  producer→consumer edge AND a disconnected dead-end `cte` leaf, AND a direct view edge for the
  control — assert all three raw topologies exist before any tool assertion. Assert the dead-end
  `cte` node has **no outgoing** `COLUMN_LINEAGE` edge. Assert `SELECTS_FROM` is **empty** for
  the CTE-wrapped targets (the missing adjacency that defect 1 fixes).

**Step 2.2 (defect 1 — mis-ordering)**: Apply **Option A** — rebuild Kahn adjacency from the
`COLUMN_LINEAGE` closure (the DIRECT `s.tableA→s.tableB→s.tableC` edges), replacing the
N × `GET_TABLE_DIRECT_UPSTREAMS` loop with **one** aggregate adjacency query over the closure's
column-id set (Design Fix 1, option (b) preferred). **Option B is dropped** (no `SELECTS_FROM`
edge exists to resolve through).
- Files affected: [`tools.py`](../src/sqlcg/server/tools.py)
  ([`_kahn_topological_sort`](../src/sqlcg/server/tools.py#L424) and possibly
  [`_affected_columns_closure`](../src/sqlcg/server/tools.py#L341) if option (a) is chosen),
  and likely a new aggregate query in [`queries.sql`](../src/sqlcg/core/queries.sql).
- Acceptance, with T = `s.tableA`:
  - `index(s.tableB) < index(s.tableC)` in `backfill_order` (both are downstream non-target
    members — producer before consumer along the chain).
  - The new adjacency build runs **once per closure** (not per-table/per-column).
  - The cycle-degradation contract ([line 460](../src/sqlcg/server/tools.py#L460)) is unchanged.
  - **No new table-level cycle** is introduced: assert on a multi-producer synthetic fixture
    (two producers feeding one target) that `backfill_order` does not collapse into the cycle
    fallback.
  - Either a new perf-scaling-guard counter covers the once-per-closure adjacency build, or the
    PR explicitly documents accepting the existing `_kahn_topological_sort` guard gap.

**Step 2.3 (defect 2 — synthetic-node leak)**: Apply the synthetic-node emission fix at the
rollup→noise boundary, shared by `get_change_scope`, `get_backfill_order`, `diff_impact`.
- Files affected: [`tools.py`](../src/sqlcg/server/tools.py); exclusion keyed on
  `SqlTable.kind` (`"cte"` / `"derived"`). Add a helper used by all three call sites —
  grep-confirm the call site before opening the PR (CLAUDE.md: every new method needs a call
  site).
- Acceptance: the synthetic `cte` node is in **neither** tool's rebuildable output, and the
  real `s.tableB` / `s.tableC` still are.

> Both fixes ship (both defects are confirmed real). Phase 1 is the confirmation gate, not a
> branch point — if Phase 1 contradicts either defect, STOP and re-scope rather than shipping
> a fix for an unreproduced cause.

### Phase 3: Cross-tool consistency regression test (Required regardless of root cause)

**Step 3.1**: In `tests/integration/test_backfill_impact_consistency.py`, with **T = `s.tableA`**,
assert the invariant precisely (containment + ordering + synthetic exclusion — **not** set
equality). Remember `s.tableA` is filtered out of its own set (tools.py:923/987), so all
ordering assertions are over the **downstream non-target** members `s.tableB`, `s.tableC`:

- **Containment**: every *real table* in `get_change_scope(T).affected_tables` (downstream
  blast radius) appears in `get_backfill_order(T).backfill_order`.
- **Ordering**: in `backfill_order`, producer precedes consumer along the chain —
  assert `index(s.tableB) < index(s.tableC)`. (Do **not** assert anything about `s.tableA`'s
  position: it is the target and is excluded from its own set.)
- **Synthetic exclusion (both directions)**: the synthetic `cte` node appears in **neither**
  `get_change_scope(T).affected_tables` **nor** `get_backfill_order(T).backfill_order`.
- **Control**: the direct view consumer `s.v_consumer` appears in both, proving the test
  isn't trivially passing by an empty radius.
- Do **not** assert `affected_tables == backfill_order` — they are legitimately asymmetric
  (unordered set vs. ordered list). Containment + ordering only.

**Step 3.2**: Extend the same consistency assertion to the sibling table-graph tools that
share this surface. State explicitly in the test docstring which siblings share the defect
vs. which are already consistent:
- [`scope_change`](../src/sqlcg/server/tools.py#L1116) — **delegates** to `get_change_scope`
  + `get_backfill_order`; its `downstream_blast_radius` / `backfill_order` must satisfy the
  same containment + synthetic-exclusion. (Shares the defect transitively.)
- [`diff_impact`](../src/sqlcg/server/tools.py#L1015) — runs the **same** closure +
  `_kahn_topological_sort` per changed file, unioned. Assert: for the file defining
  `s.tableA`, `affected_tables` contains `s.tableB` and `s.tableC`, `backfill_order` respects
  `index(s.tableB) < index(s.tableC)`, and the synthetic `cte` node is in neither
  `affected_tables` nor `backfill_order`. (Shares both defects.)
- [`get_change_scope`](../src/sqlcg/server/tools.py#L884) — the impact side of the pair;
  pinned directly.

**Step 3.3**: Assert observable output (CLAUDE.md), never "no exception". Use concrete id
sets / list-index comparisons, mirroring the assertion style in
[`test_bare_column_cte_lineage.py`](../tests/integration/test_bare_column_cte_lineage.py).

### Phase 4: Version bump + housekeeping

**Step 4.1**: Patch bump (current **1.4.1** in
[`pyproject.toml`](../pyproject.toml) / [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py)).
- [`pyproject.toml`](../pyproject.toml) `version = "1.4.2"`.
- [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) `__version__ = "1.4.2"`.
- `uv lock` (refresh lockfile version entry).
- Acceptance: all three updated; `uv run pytest` green; `uv run pyright` + `uv run ruff check`
  clean.

**Step 4.2**: Do **NOT** tag, do **NOT** close the issue — the user verifies on the live DWH
and tags. Keep this branch **separate from open PR #56**.

---

## Test Strategy
- **Integration** (`tests/integration/test_backfill_impact_consistency.py`, real in-memory
  DuckDB via `Indexer().index_repo`): the shared 3-table CTE-chain + control-view fixture; the
  containment + ordering (`index(s.tableB) < index(s.tableC)`) + synthetic-exclusion invariant
  across `get_change_scope`, `get_backfill_order`, `diff_impact`, `scope_change`; plus a
  multi-producer fixture asserting Option A introduces no spurious table-level cycle.
- **Raw-topology pre-asserts**: before tool assertions, assert the fixture actually produced,
  per chain link, the **DIRECT** `COLUMN_LINEAGE` producer→consumer edge and a disconnected
  dead-end `cte` leaf (no outgoing edge), an **empty** `SELECTS_FROM` for the CTE-wrapped
  targets, and the direct view edge for the control — so a future parser change that alters
  this topology fails loudly here rather than silently passing.
- **Perf**: Option A changes `_kahn_topological_sort`'s query profile. Run
  [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py) and confirm green;
  add a once-per-closure adjacency-build counter (or document the accepted gap).
- **Full suite**: `uv run pytest` must stay green (watch the CTE-source-node and recall
  guards).

## Acceptance Criteria
- [ ] Phase 1 verdict recorded in this plan confirming **both** defects (alphabetical ordering
      from absent `SELECTS_FROM`; synthetic node emitted as rebuildable), with captured
      `COLUMN_LINEAGE` (DIRECT edge + dead-end CTE leaf) + empty-`SELECTS_FROM` rows as evidence.
- [ ] Reproduction is deterministic (synthetic 3-table fixture) even if first observed on the DWH.
- [ ] For the CTE-wrapped target: every real table in `get_change_scope` downstream radius is
      present in `get_backfill_order` (containment holds).
- [ ] With T = `s.tableA`: `index(s.tableB) < index(s.tableC)` in `backfill_order`
      (producer before consumer; target correctly absent from its own set).
- [ ] Kahn adjacency is rebuilt from the `COLUMN_LINEAGE` closure (Option A), **once per
      closure**; Option B is not implemented.
- [ ] Option A introduces **no new table-level cycle** — asserted on a multi-producer fixture.
- [ ] Either a perf-scaling-guard counter covers the new once-per-closure adjacency-build op,
      or the PR explicitly documents accepting the `_kahn_topological_sort` guard gap.
- [ ] The synthetic `cte` node appears as a rebuildable table in **none** of
      `get_change_scope`, `get_backfill_order`, `diff_impact`, `scope_change`.
- [ ] The control direct-view consumer is present in both impact and backfill (test not
      trivially empty).
- [ ] Test docstring states which siblings shared the defect vs. were already consistent.
- [ ] No `# TODO` in the happy path of any changed tool.
- [ ] If a new helper is added, it has a grep-confirmed call site.
- [ ] No change to [`base.py`](../src/sqlcg/parsers/base.py) /
      [`indexer.py`](../src/sqlcg/indexer/indexer.py); perf invariants intact;
      [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py) green.
- [ ] Version bumped to 1.4.2 in `pyproject.toml` + `__init__.py` + `uv lock`; **not** tagged,
      issue **not** closed.
- [ ] Work kept separate from PR #56.

## Risks and Mitigations
- **Risk:** "drop" framing misleads the fix. **Mitigation (resolved):** reproduction confirmed
  there is **no membership drop**; the two real defects (alphabetical ordering, synthetic-node
  leak) are both pinned. Phase 1 re-confirms before either fix ships.
- **Risk:** synthetic-node exclusion accidentally drops a *real* table that happens to match
  an alias name. **Mitigation:** key exclusion on `SqlTable.kind` (`"cte"`/`"derived"`), never
  on the alias string.
- **Risk:** Option A's rollup-based adjacency creates **table-level cycles** the `SELECTS_FROM`
  graph never had (multi-producer fan-in, self-referencing column pairs), tripping the
  cycle-degradation fallback and re-introducing non-causal order. **Mitigation:** keep
  [`_kahn_topological_sort`](../src/sqlcg/server/tools.py#L424)'s cycle path
  ([line 460](../src/sqlcg/server/tools.py#L460)) **and** add a multi-producer fixture asserting
  no spurious cycle (Step 2.2 / Acceptance).
- **Risk:** Option A changes `_kahn_topological_sort`'s query profile with **no existing
  perf-guard** on it. **Mitigation:** add a once-per-closure adjacency-build counter to
  [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py), or document the
  accepted gap in the PR.
- **Risk:** set-equality brittleness. **Mitigation:** test asserts containment + ordering,
  explicitly not identity.

### Blocking Questions
- **The root cause of defect 1 lives in the parser** (no `SELECTS_FROM` adjacency is emitted for
  CTE-wrapped INSERT…SELECT because the real source is nested in the CTE child scope —
  [`_real_tables()`](../src/sqlcg/parsers/base.py#L371) /
  [`indexer.py:1148`](../src/sqlcg/indexer/indexer.py#L1148)). This plan deliberately fixes it at
  the **tool layer** (Option A reads the already-correct DIRECT `COLUMN_LINEAGE` edge), avoiding
  the perf-invariant-bearing [`base.py`](../src/sqlcg/parsers/base.py) /
  [`indexer.py`](../src/sqlcg/indexer/indexer.py) surface. **STOP and escalate** only if Phase 1
  shows the **DIRECT `COLUMN_LINEAGE` `s.tablea→s.tableb` edge is also missing** (not just
  `SELECTS_FROM`): then Option A has nothing to read and the fix must move into the parser as a
  separate ticket. Do not edit those files under this plan.

---

### Deviations

#### Deviation 1: Synthetic node is a transitive two-hop bridge, not a dead-end leaf with a parallel direct edge — adjacency fix contracts the bridge instead of reading a direct edge
- **Reason**: The plan's central empirical "topology fact" (a DIRECT
  `s.tablea.id -> s.tableb.id` `COLUMN_LINEAGE` edge running parallel to a disconnected
  dead-end `cte` leaf) **did not reproduce** under any fixture shape tried — colliding
  CTE alias names across files, distinct per-statement alias names, explicit column-list
  aliasing, and bare `SELECT *`. In every case the raw topology was a clean **two-hop
  relay**: `producer.col -> cte.col -> consumer.col`, with the synthetic node carrying
  *both* an incoming edge from the producer *and* an outgoing edge to the consumer (i.e.
  it is a transitive bridge, not a dead end). A literal Option-A implementation that rolls
  up individual `COLUMN_LINEAGE` rows to `(src_table, dst_table)` pairs and looks for
  `producer -> consumer` therefore finds **nothing** — both tables remain at indegree 0
  and the alphabetical-fallback bug persists unchanged. This was caught by an
  intermediate test using non-alphabetically-ordered table names
  (`s.tablea -> s.zzz_second -> s.aaa_third`) that would pass trivially under the old
  `sorted()` fallback but fail under true causal ordering.
- **Change**: Implemented the *strategy* the plan correctly identified (derive Kahn
  adjacency from the `COLUMN_LINEAGE` closure, at the tool layer, **no parser/indexer
  changes** — the hard constraint is preserved) but with corrected mechanics: the new
  [`GET_TABLE_ADJACENCY_FOR_COLUMNS`](../src/sqlcg/core/queries.sql) query returns *all*
  rolled-up `(upstream_table, upstream_kind, downstream_table, downstream_kind)` pairs
  over the closure's column-id set — including synthetic endpoints and their
  `SqlTable.kind` (fetched in the same query via a `LEFT JOIN`, no second round-trip).
  [`_kahn_topological_sort`](../src/sqlcg/server/tools.py#L424) then **contracts**
  synthetic-node hops in one BFS pass per real producer over this small rolled-up edge
  set: `real -> synthetic [-> synthetic ...] -> real` collapses to `real -> real`. This
  remains a single aggregate query plus one bounded contraction pass — still **once per
  closure**, never per-table/per-column — and achieves the plan's acceptance criterion
  (`index(s.tableB) < index(s.tableC)`) that the literal direct-edge reading could not.
  Per CLAUDE.md's "Option A choice (b)" guidance, this is additive: no contract change
  to `_affected_columns_closure`'s 3 call sites.
- **Impact**: No scope change — still entirely at the tool layer
  ([`tools.py`](../src/sqlcg/server/tools.py) +
  [`queries.sql`](../src/sqlcg/core/queries.sql)/[`queries.py`](../src/sqlcg/core/queries.py)),
  no `base.py`/`indexer.py` edits, no API/schema changes. Risk: the contraction adds a
  bounded BFS over the rolled-up edge set (size bounded by the 50k closure-node cap,
  same invariant the plan already names) — still well within "once per closure." Tests:
  `tests/integration/test_backfill_impact_consistency.py` asserts the corrected two-hop
  topology directly (producer -> cte -> consumer, not a direct bypass edge) so a future
  parser change that *does* start emitting a direct edge fails loudly here rather than
  silently relying on a code path that was never exercised.
- **Date**: 2026-06-07
