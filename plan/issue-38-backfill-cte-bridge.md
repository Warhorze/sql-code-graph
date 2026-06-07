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

---

## Phase 5: Column case-normalization (the REAL DWH blocker)

> **Status: PLANNED — not yet implemented.** Phases 1–4 (PR #57: COLUMN_LINEAGE-derived
> adjacency, synthetic-hop contraction, synthetic-node exclusion) are implemented and
> **verified-correct in isolation**. This phase folds in on top of them, **by explicit user
> decision to make #57 the complete #38 fix** — see the *Deliberate deviation from "separate
> PRs"* note below.

### Why Phases 1–4 were necessary but not sufficient

PR #57's three mechanisms each work and are verified. But on the **real DWH**,
[`get_change_scope`](../src/sqlcg/server/tools.py#L884) /
[`get_backfill_order`](../src/sqlcg/server/tools.py#L961) **still miss** the CTE-bridged
week-segment ETL — because the closure **seed is starved by a column case-split**, upstream of
everything Phases 1–4 touch. The adjacency/contraction logic is never reached for the
downstream tables because the BFS dies at depth 1.

**The split (live-DWH verification finding):**

| Source | Column-id case | Carries `COLUMN_LINEAGE`? | Has `HAS_COLUMN` edge? |
|--------|----------------|---------------------------|------------------------|
| DDL `CREATE TABLE` columns | **UPPERCASE** (`ba.<table>.MA_VOORRAAD_WAARDE_TOTAAL_BIP`) — 93% of `HAS_COLUMN` dst ids | no | **yes** |
| ETL `INSERT…SELECT` lineage columns | **lowercase** (`…ma_voorraad_waarde_totaal_bip`) | **yes** | no |

The table-level tools **seed** the closure from
[`GET_COLUMNS_FOR_TABLE`](../src/sqlcg/core/queries.sql#L186) (= `HAS_COLUMN` dst ids =
**UPPERCASE**), at
[`tools.py:1020`](../src/sqlcg/server/tools.py#L1020) /
[`tools.py:1090`](../src/sqlcg/server/tools.py#L1090) /
[`tools.py:1177`](../src/sqlcg/server/tools.py#L1177):
```python
col_rows  = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": target})
start_cols = [r["col_id"] for r in col_rows]              # UPPERCASE ids
affected_cols, ... = _affected_columns_closure(db, start_cols, max_depth=None)
```
[`_affected_columns_closure`](../src/sqlcg/server/tools.py#L341) then walks
[`GET_DOWNSTREAM_DEPENDENCIES`](../src/sqlcg/core/queries.sql#L61) =
`WHERE cl.src_key = <UPPERCASE id>`. The `COLUMN_LINEAGE` endpoints are **lowercase**
(stamped by [`ColumnRef.__post_init__`](../src/sqlcg/parsers/base.py#L102)), so the seed
ids are **disjoint** from the lineage ids → **zero rows → BFS dies at depth 1**, never
reaching `cte_insert` / the week-segment ETL.

**Proof the rest of the pipeline is complete** (live-DWH): re-seeding the *same* closure
from **ALL** of the table's `SqlColumn` nodes (including the lowercase lineage columns)
reaches the week-segment table at **depth 6** with correct synthetic-hop contraction and a
correct causal backfill order. The lineage **data is complete**; only the **seed** is wrong.

This is the **same bug class as [#50](../src/sqlcg/server/tools.py)** (analyze case-fold,
fixed by lowercasing the analyze anchor — commit `8f227dd`). The lineage convention is
**lowercase**; the UPPERCASE DDL columns are the **anomaly**.

### Chosen approach (user-decided): lowercase DDL column-name components at INDEX time

Normalize DDL `CREATE TABLE` / `CREATE VIEW` column-name components to lowercase when the
`SqlColumn.id` and `SqlColumn.col_name` are built, so DDL columns share **node identity**
with the ETL lineage columns. Then `HAS_COLUMN` edges land on the **same nodes** the lineage
uses → `GET_COLUMNS_FOR_TABLE` seed ids match the closure's lineage ids → the closure reaches
downstream. **Re-index is the migration path** (allowed per CLAUDE.md — "No backward
compatibility. Re-index is the migration path.").

> **Why index-time, not query-time `lower()` on the seed:** lowercasing only the seed at the
> tool layer would make the seed ids match `COLUMN_LINEAGE` **but leave them disjoint from the
> `HAS_COLUMN` / DDL `SqlColumn` nodes** — creating a *new* split between the two halves of the
> graph and breaking [`GET_COLUMNS_FOR_TABLE`](../src/sqlcg/core/queries.sql#L186) itself
> (which joins `HAS_COLUMN`→`SqlColumn` and would return UPPERCASE rows that then no longer
> matched anything). Index-time normalization makes **one** identity for each column across
> DDL and lineage — the only fix that closes the split rather than relocating it. This mirrors
> how `table_qualified` is **already** normalized (the table component is lowercased by
> [`TableRef.__post_init__`](../src/sqlcg/parsers/base.py#L60); the column component is the
> sole remaining un-normalized identity field).

### Design

The DDL column-name normalization is missing at **exactly one origin**:
[`AnsiParser._extract_defined_columns`](../src/sqlcg/parsers/ansi_parser.py#L400) returns
raw column names (`col_def.this.name`, [line 412–414](../src/sqlcg/parsers/ansi_parser.py#L412))
with **no `.lower()`**. Everything downstream of `defined_columns` inherits that raw case.
`SnowflakeParser` inherits `_extract_defined_columns` from `AnsiParser` (no override —
confirmed by grep), so fixing the base method fixes both dialects.

**Fix site (single, preferred): normalize in `_extract_defined_columns`** so
`StatementNode.defined_columns` is **already lowercase** at the source — every consumer
(indexer HAS_COLUMN emission, any future reader of `defined_columns`) then inherits the
normalized value with no second site to keep in sync. This is the mirror of how `TableRef` /
`ColumnRef` normalize in `__post_init__` (normalize **at construction**, never at each use
site). The string component normalized is the **column name only** — quote-stripping +
`.lower()`, identical to [`ColumnRef.__post_init__`](../src/sqlcg/parsers/base.py#L102) so the
two halves produce byte-identical ids.

> **Quote-strip parity (do not skip):** `ColumnRef.__post_init__` strips surrounding
> double-quotes **before** lowercasing ([base.py:110–114](../src/sqlcg/parsers/base.py#L110)).
> `_extract_defined_columns` must apply the **same** strip-then-lower so a DDL
> `"MA_Voorraad"` and a lineage `MA_Voorraad` collapse to the same `ma_voorraad` node.
> Reuse the exact transform — extract a tiny shared helper (e.g. `_normalize_col_name`) if
> duplicating the 3 lines is undesirable; either way the two transforms must be **identical**,
> and the test in Step 5.4 must include a quoted-identifier DDL column to pin parity.

### Step 5.0 — Per-site column-id construction audit (MANDATORY before editing)

The danger is **partial** normalization creating a *new* split. Below is the full audit of
every column-id / `col_name` / `HAS_COLUMN` / `COLUMN_LINEAGE`-endpoint construction site.
The developer **re-greps and re-confirms** each before editing
(`grep -rn 'col_name\|\.id =\|HAS_COLUMN\|SqlColumn\|\.full_id' src/sqlcg/parsers src/sqlcg/indexer src/sqlcg/core`).

| # | Site | What it builds | Already lowercase? | Action |
|---|------|----------------|--------------------|--------|
| A | [`ColumnRef.__post_init__`](../src/sqlcg/parsers/base.py#L102) | every lineage `ColumnRef.name` (→ `full_id`) | **YES** (strip-quote + `.lower()`) | none — the reference impl to mirror |
| B | [`TableRef.__post_init__`](../src/sqlcg/parsers/base.py#L60) | `table_qualified` component of every col id | **YES** (`.lower()` on catalog/db/name) | none — already normalized |
| C | [`AnsiParser._extract_defined_columns`](../src/sqlcg/parsers/ansi_parser.py#L400) | `StatementNode.defined_columns` (raw DDL names) | **NO** | **FIX HERE** — strip-quote + `.lower()`, mirroring site A |
| D | [`indexer.py` DDL block](../src/sqlcg/indexer/indexer.py#L1096) | `col_id = f"{table.full_id}.{col_name}"`, `col_name`, `HAS_COLUMN` src/dst | inherits **C** | no edit if C is fixed — `col_name` flows from `defined_columns`. (Defensive alternative: lower here too; see "consistency assertion" below) |
| E | [`indexer.py` lineage block — src](../src/sqlcg/indexer/indexer.py#L1161) | `src_id = edge.src.full_id`, `col_name = edge.src.name`, `HAS_COLUMN`-free | **YES** (via `ColumnRef`, site A) | none |
| F | [`indexer.py` lineage block — dst](../src/sqlcg/indexer/indexer.py#L1189) | `dst_id = edge.dst.full_id`, `col_name = edge.dst.name` | **YES** (via `ColumnRef`, site A) | none |
| G | [`EXPAND_STAR_SOURCES`](../src/sqlcg/core/queries.sql#L93) | new dst col id `q.target_table || '.' || c.col_name` + `HAS_COLUMN` | inherits **D** (`c.col_name` from the source table's `HAS_COLUMN`) | no edit — once C is fixed, the source `c.col_name` is lowercase, so star-expanded ids are lowercase too. **Verify** in Step 5.5 |
| H | [`SchemaResolver`](../src/sqlcg/lineage/schema_resolver.py#L71) | in-memory `_tables[key] = col_names` (DDL `node.name`) used for parse-time qualify, **not** graph ids | raw case, but qualify is case-insensitive at the sqlglot layer | none — does not build graph ids; out of scope (flag only) |

**Consistency assertion (the partial-normalization guard):** after the fix, every
`SqlColumn.id` and the `dst_key` of every `HAS_COLUMN` edge whose `source='ddl'` MUST be
fully lowercase. Step 5.4's test asserts this directly on the indexed graph
(`SELECT id FROM SqlColumn WHERE id <> lower(id)` returns **zero rows**), so any *new* site
that re-introduces an uppercase id (now or in future) fails loudly. The fix is **complete only
if** site C is normalized AND this whole-graph assertion passes — if it does not, site D (or
G) is independently emitting an unnormalized id and must also be fixed. This is why the fix is
expressed as "normalize at the single origin C **and** assert the invariant graph-wide",
not "edit C and hope."

### Step 5.1 — Apply the index-time normalization

- File: [`ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) — normalize the column name in
  [`_extract_defined_columns`](../src/sqlcg/parsers/ansi_parser.py#L400) (strip surrounding
  double-quotes, then `.lower()`), identical to
  [`ColumnRef.__post_init__`](../src/sqlcg/parsers/base.py#L102).
- If a shared `_normalize_col_name` helper is introduced, it needs a **grep-confirmed call
  site** (CLAUDE.md) — there are two: `_extract_defined_columns` and `ColumnRef.__post_init__`.
- **Acceptance:** indexing a fixture with `CREATE TABLE s.t (MA_VOORRAAD INT, "Mixed_Case" INT)`
  yields `SqlColumn` ids `s.t.ma_voorraad` and `s.t.mixed_case`, and `HAS_COLUMN.dst_key`
  matches; no uppercase column id exists anywhere in the graph.

### Step 5.2 — PERF INVARIANTS (critical — touches the protected surface)

This phase touches [`ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py). It does **not**
touch the hot per-column / per-statement loops in
[`base.py`](../src/sqlcg/parsers/base.py) `_extract_column_lineage` or the upsert paths in
[`indexer.py`](../src/sqlcg/indexer/indexer.py). The change is a **cheap string op
(strip+lower) at DDL node-creation time**, run **once per defined column at parse time** —
the same complexity class as the column name was already being copied at. It explicitly:

- adds **no** per-column or per-statement **re-work** (no extra `qualify` / `build_scope` /
  `expand` / scope rebuild — it touches only the DDL `ColumnDef` walk, which already iterates
  every defined column once);
- keeps `qualify` / `build_scope` **once per statement** (untouched);
- keeps the bulk-upsert paths (`upsert_nodes_bulk` / `upsert_edges_bulk`,
  `_flush_row_batch` per-batch) **untouched**;
- does **not** alter `exp.expand()`'s `sources` / `dependency_filter`, `body_scope` reuse,
  the pure-literal skip, the `copy=False` / `trim_selects=False` kwargs, the INSERT
  body-copy-once path, or any other entry in CLAUDE.md's **Performance invariants** table.

**Per-invariant impact (every potentially-affected row in CLAUDE.md's table):**

| Invariant | Affected? | Why not |
|-----------|-----------|---------|
| `qualify`/`build_scope` module-level import | no | no import change |
| `exp.expand()` with file-level `sources` only | no | DDL path does not call expand |
| `body_scope` built once per statement | no | DDL path has no body_scope |
| pure-literal skip | no | unchanged |
| `sources=` not passed when scope present | no | unchanged |
| `copy=False` + `trim_selects=False` with scope | no | unchanged |
| `body_scope` built once before column loop | no | unchanged |
| INSERT body copied once | no | unchanged |
| `_upsert_parsed_file` uses bulk upsert | no | unchanged |
| `_flush_row_batch` per-batch flush | no | unchanged |

**Guard tests that MUST stay green** (no slack-raising; a red means the invariant broke):
[`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py),
[`test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py).
No new perf counter is required (no new hot-path op is introduced); confirm the existing
scaling guard stays flat at N and 2N.

### Step 5.3 — Consumer audit (lookups that assume a case)

Confirm no consumer that looks up a column by id breaks when DDL cols become lowercase. Most
already expect lowercase from lineage; the fix **removes** a mismatch rather than introducing
one.

- [`GET_COLUMNS_FOR_TABLE`](../src/sqlcg/core/queries.sql#L186) — joins
  `HAS_COLUMN`→`SqlColumn`; returns whatever case is stored. After the fix it returns
  **lowercase**, which is exactly what makes the seed match the lineage. **This is the fix's
  payoff site.**
- [`GET_DOWNSTREAM_DEPENDENCIES`](../src/sqlcg/core/queries.sql#L61) /
  [`GET_UPSTREAM_DEPENDENCIES`](../src/sqlcg/core/queries.sql#L68) — match on `cl.src_key` /
  `cl.dst_key`, already lowercase; the lowercase seed now matches. No change.
- [`GET_TABLE_ADJACENCY_FOR_COLUMNS`](../src/sqlcg/core/queries.sql#L201) (PR #57's new query)
  — filters `cl.src_key = ANY(col_ids)` / `cl.dst_key = ANY(col_ids)`. The closure col_ids are
  now lowercase end-to-end, so the `ANY(...)` membership matches. No change.
- [`EXPAND_STAR_SOURCES`](../src/sqlcg/core/queries.sql#L93) — propagates `c.col_name`
  (lowercase after the fix) into new dst ids; output stays consistent. No change; **verify** in
  Step 5.5.
- [`SchemaResolver`](../src/sqlcg/lineage/schema_resolver.py#L71) — parse-time qualify helper,
  builds **no** graph ids; sqlglot qualify is case-insensitive. No change.
- [`TRACE_COLUMN_LINEAGE`](../src/sqlcg/core/queries.sql#L34),
  `analyze upstream/downstream` (already case-folds its anchor — #50) — unaffected; both
  already operate in lowercase space.

**Acceptance:** no consumer query references a column id with an assumed-uppercase literal
(grep for any hardcoded uppercase column name in [`queries.sql`](../src/sqlcg/core/queries.sql)
and [`tools.py`](../src/sqlcg/server/tools.py) — expect none).

### Step 5.4 — Seed-level regression test (the exact live-DWH failure mode)

New integration test (in
[`test_backfill_impact_consistency.py`](../tests/integration/test_backfill_impact_consistency.py)
or a new `test_ddl_column_case_seed.py` — developer's choice; co-locating is fine since it
shares the fixture style). It is **distinct** from the existing
[`test_backfill_impact_consistency.py`](../tests/integration/test_backfill_impact_consistency.py)
fixture, whose DDL columns are **same-case** as its INSERT columns (`id`, `val` — see
[lines 65–82](../tests/integration/test_backfill_impact_consistency.py#L65)), which is why that
test passes **despite** the bug. The new fixture must reproduce the **split**:

- **DDL with UPPERCASE columns**:
  `CREATE TABLE s.tablea (ID INT, MA_AMOUNT INT, "Quoted_Col" INT);` and likewise for
  `s.tableb`, `s.tablec`.
- **ETL `INSERT…SELECT` through a CTE in lowercase**:
  `INSERT INTO s.tableb (id, ma_amount) WITH cte AS (SELECT id, ma_amount FROM s.tablea) SELECT id, ma_amount FROM cte;`
  and `s.tableb → s.tablec` likewise — the chain through the synthetic CTE node, mirroring the
  DWH week-segment shape.
- **Assertions:**
  1. **Whole-graph case invariant:** `SELECT id FROM "SqlColumn" WHERE id <> lower(id)`
     returns **zero rows** (no uppercase column id survives; quoted-col parity holds —
     `"Quoted_Col"` → `s.tablea.quoted_col`).
  2. **Seed reaches downstream:** `get_backfill_order("s.tablea")` and
     `get_change_scope("s.tablea")` **contain `s.tableb` and `s.tablec`** (the CTE-bridged
     downstream tables) — the exact membership the live DWH lost.
  3. **Causal order preserved:** `index(s.tableb) < index(s.tablec)` in `backfill_order`
     (Phase 1–4 contraction still holds on the lowercase ids).
  4. **No synthetic leak:** the synthetic `cte` node is in **neither** result (Phase 1–4
     exclusion still holds).
- **The test MUST FAIL before Step 5.1 and PASS after.** The developer demonstrates the
  red→green transition explicitly (run the new test against the pre-fix parser to capture the
  failure: assertion 2 fails — `s.tableb`/`s.tablec` absent — because the UPPERCASE seed is
  disjoint from the lowercase lineage). Record the captured pre-fix failure in the PR
  description.

> **Why this test is not redundant with Phase 3's consistency test:** Phase 3's fixture has
> matching case on both sides, so its seed already matches lineage — it exercises
> contraction/exclusion but **cannot** catch the seed-starvation split. This test pins the
> split specifically.

### Step 5.5 — Live-DWH re-verification (read-only)

After implementation, re-run the live check the user performed — **read-only, never write to
`/home/ignwrad/Projects/dwh`** (the DWH repo: no commits, ever; the e2e harness
`tests/e2e/test_dwh_e2e.py` is gitignored — never commit it):

1. Index the real DWH into a throwaway DB.
2. Run `get_change_scope` and `get_backfill_order` on the voorraad-fact / monoliet target.
3. **Assert:** the week-segment ETL table now **appears** in both results; the backfill order
   is **causal** (producer before consumer); **no synthetic** `cte` / `derived` node leaks.
4. Confirm the whole-graph case invariant holds on the real index
   (`SELECT count(*) FROM "SqlColumn" WHERE id <> lower(id)` = 0).
5. Spot-check indexing time is within the perf budget (CLAUDE.md: 1,600 files < 5 min; the
   string op is negligible — confirm no regression vs the pre-fix baseline of ~210–256s).

Record the verdict in the PR description (not committed to the DWH repo).

### Step 5.6 — Version

No further bump. The branch is already at **1.4.2**
([`pyproject.toml`](../pyproject.toml), [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) —
confirmed). This phase **folds into the same patch ship** as #57 (single PR, one version). Run
`uv lock` only if the lockfile drifts. **Do not tag, do not close the issue** — the user
verifies on the live DWH and tags (per project convention).

### Secondary finding (scope decision): `diff_impact` empty `changed_tables` on monoliet ETL

Live verification also found: `diff_impact` on monoliet's **ETL file** returns an empty
`changed_tables`, because monoliet's
[`DEFINED_IN`](../src/sqlcg/core/queries.sql) provenance points only at the **DDL changelog
file**, not the **ETL producer file**. `diff_impact` maps a changed file → its defined tables
via `DEFINED_IN`, so an ETL file that produces a table via `INSERT…SELECT` (but does not
`CREATE` it) has no `DEFINED_IN` edge to that table and yields no changed tables.

**Recommendation: track as a SEPARATE follow-up issue, NOT part of #38/Phase 5.** Rationale:
- It is a **different mechanism** — `DEFINED_IN` provenance (which file is recorded as
  "defining" a table), not the column case-split. The case fix does not touch it and it does
  not block the week-segment membership fix.
- Fixing it means deciding whether an `INSERT…SELECT` producer file should get a `DEFINED_IN`
  (or a new `PRODUCES`) edge to its target table — a **data-model question** with its own blast
  radius across the indexer and the four impact tools, deserving its own plan + plan-review.
- Folding it here would re-expand a PR that the user explicitly wants scoped to "the complete
  #38 fix = #57 + case-normalization."

Action for the developer: open a new issue titled e.g. *"diff_impact misses INSERT…SELECT
producer files (DEFINED_IN points only at DDL)"* referencing this finding, and link it from
the PR description. Do not implement it under this plan.

### Deliberate deviation from the "separate PRs" rule

The user's standing convention is **never fold new work into an already-reviewed PR; ship a
separate patch** (memory `feedback_separate_prs.md`). This phase **intentionally deviates** —
by **explicit user instruction** — folding the column case-normalization into PR #57 so that
**#57 becomes the complete, verified #38 fix** rather than shipping a #57 that demonstrably
still fails on the real DWH. This deviation is recorded here so it is not mistaken for an
oversight; it applies to **this phase only** and does not change the standing rule.

### Phase 5 Acceptance Criteria

- [ ] `_extract_defined_columns` strips surrounding quotes then lowercases each DDL column
      name, byte-identical to [`ColumnRef.__post_init__`](../src/sqlcg/parsers/base.py#L102)
      (quoted-identifier parity pinned by test).
- [ ] Whole-graph invariant: `SELECT id FROM "SqlColumn" WHERE id <> lower(id)` returns zero
      rows after indexing (no partial-normalization split re-introduced).
- [ ] New seed-level test reproduces the UPPERCASE-DDL / lowercase-ETL split, **FAILS** before
      Step 5.1 and **PASSES** after; pre-fix failure captured in the PR.
- [ ] `get_backfill_order` / `get_change_scope` seeded from the table **reach** the CTE-bridged
      downstream tables (`s.tableb`, `s.tablec`) — the exact live-DWH failure mode.
- [ ] Causal order (`index(s.tableb) < index(s.tablec)`) and synthetic-node exclusion (Phases
      1–4) still hold on the lowercase ids.
- [ ] No consumer query references an assumed-uppercase column literal (grep clean).
- [ ] Perf invariants intact; no per-column/per-statement re-work added; `qualify`/`build_scope`
      stay once-per-statement; bulk + per-batch upsert untouched;
      [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py),
      [`test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py),
      [`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
      [`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py) green.
- [ ] Live-DWH re-verification recorded (week-segment appears, causal, no synthetic leak,
      case invariant holds, no perf regression) — read-only, no DWH commit.
- [ ] Version stays **1.4.2** (folded into #57); not tagged; issue not closed.
- [ ] Secondary `diff_impact`/`DEFINED_IN` finding filed as a **separate** issue, linked from
      the PR, **not** implemented here.
- [ ] No `# TODO` in the happy path; any new helper has a grep-confirmed call site.

### Phase 5 Risks and Mitigations

- **Risk: PARTIAL normalization creates a NEW split** (fix site C but a stale site D/G keeps
  emitting uppercase). **Mitigation:** normalize at the single origin (C) **and** assert the
  whole-graph `id = lower(id)` invariant in Step 5.4 — any leaking site fails the test loudly.
- **Risk: quoted-identifier mismatch** (`"MA_Voorraad"` DDL vs `MA_Voorraad` lineage collapse
  differently). **Mitigation:** reuse `ColumnRef`'s exact strip-then-lower transform; pin with
  a quoted DDL column in the test.
- **Risk: perf regression on the protected surface.** **Mitigation:** the change is a
  once-per-defined-column string op in the DDL walk — no scope/qualify/expand re-work; the
  full CLAUDE.md invariant table is addressed row-by-row (Step 5.2) and the four guard tests
  must stay green.
- **Risk: a consumer silently assumed uppercase.** **Mitigation:** Step 5.3 audits every
  column-lookup query; grep for hardcoded uppercase literals expects none.
- **Risk: scope creep via the `diff_impact`/`DEFINED_IN` finding.** **Mitigation:** explicitly
  carved out to a separate issue (different mechanism, own blast radius).

### Phase 5 Blocking Questions

- None outstanding. The root cause, fix site, and approach are all confirmed by the live-DWH
  verification and the static audit above. **STOP and escalate only if** Step 5.4's pre-fix run
  does **not** show the seed-starvation failure (assertion 2 still passes before the fix) — that
  would mean the split is not the live blocker on the test fixture and the fixture does not
  faithfully reproduce the DWH topology; re-scope rather than ship a fix for an unreproduced
  cause.
