# Feature Plan: Data-Loss Impact Analysis (two sequenced PRs)

> Status: REVIEWED (shepherd-verified 2026-06-12 — M1/M6 blockers confirmed
> resolved by reading the revised §"two views" and §"PR 2 Detection mechanism"
> against the tree; gate passed). Ready for implementation, PR 1 first. Preserves the
> plan-reviewer-revised W1/W2/N1/N2/N3 PASS content (commit dcda023), scoped into
> **PR 1**. View 1 is reframed (M1) as a documented SUPPLEMENT to View 2 (the
> primary, reliable answer) after the corpus measurement below; O2 is resolved
> (M6) — `get_pr_impact` requires a base-indexed graph and orchestrates the resync
> internally. Owner (compliance): architect-planner.
> Version targets: **PR 1 → 1.22.0**, **PR 2 → 1.23.0** (both additive → minor).
>
> **M1 corpus measurement (the reframe basis).** On the DWH corpus
> `SELECTS_FROM = 4,944` edges vs `COLUMN_LINEAGE = 53,285` — ~90% of cross-table
> reads are CTE/column-lineage routed and emit **no top-level `SELECTS_FROM` edge**
> (the parser's `_real_tables`, [`base.py` L604](../../src/sqlcg/parsers/base.py),
> excludes CTE-sourced names; a CTE-wrapped `INSERT ... WITH cte AS (SELECT FROM X)
> SELECT FROM cte` yields `stmt.sources=[]`). So a View 1 built on
> `SELECTS_FROM ∪ STAR_SOURCE` alone is near-empty on this corpus. **We are NOT
> fixing this in the parser** — `_real_tables` is on the performance-invariant hot
> path (CLAUDE.md) and out of scope. The reframe below makes View 2 the headline
> and rebuilds View 1 as a supplement whose genuine value-add is the direct
> gating-join reader tables, with a documented residual gap.

## Feature Summary (both PRs)

Answer the operational question "a job failed and left table(s) empty — what
downstream is at risk?" in two stages:

- **PR 1 — Blast-radius core (the engine).** Given user-NAMED empty table(s),
  report the downstream blast radius. **View 2 (column-lineage closure) is the
  PRIMARY, reliable answer** — which downstream COLUMNS lose their source VALUES,
  rolled up to their tables; it already captures CTE-wrapped *derived* reads and is
  the headline. **View 1 (`row_empty_tables`) is a documented SUPPLEMENT** — the
  UNION of (a) View 2's affected tables and (b) tables with a DIRECT
  `SELECTS_FROM`/`STAR_SOURCE` read of a source (the gating-join case); its genuine
  value-add over View 2 is the direct gating-join reader tables. Ships first,
  independently. Entry points: MCP `get_empty_propagation(tables)` + CLI
  `sqlcg analyze empty-impact <table>...`.
- **PR 2 — PR-impact detector (detection front-end).** Detect, from a code change
  between two index runs / refs, when a table LOSES its producer (its feeding
  edges disappear) — then auto-feed those tables into PR 1's engine, returning the
  same two-view blast radius without hand-naming tables. Ships after PR 1, depends
  on its engine. Entry point: MCP `get_pr_impact(base_ref)` + CLI
  `sqlcg analyze pr-impact --base <ref>`.

## Shared design (built by PR 1, reused by PR 2 — no duplication)

PR 2 MUST NOT reimplement any blast-radius logic. PR 1 builds, and PR 2 reuses:

| Shared artifact | Built in | Reused by PR 2 |
|---|---|---|
| `EmptyPropagationResult` (two-view model) | PR 1 Step 1.1 | returned verbatim, wrapped with attribution |
| `_compute_empty_propagation(db, tables, max_depth)` engine | PR 1 Step 1.3 | called directly with the detected lost-producer table set |
| `_table_row_reachability(db, tables, max_depth)` (view 1 traversal) | PR 1 Step 1.2 | invoked transitively via the engine |
| `_affected_columns_closure` / `_rollup_to_tables` / `_exclude_synthetic_tables` / `_kahn_topological_sort` (existing helpers) | already in tree | unchanged |
| `get_backend(read_only=True)` CLI seam (W2/O1) | PR 1 Step 3.1 | same seam reused by PR 2's CLI |

> **Dependency gate:** PR 2 cannot start until PR 1's engine
> (`_compute_empty_propagation`, both views, `EmptyPropagationResult`) is merged.

---

# PR 1 — Blast-radius core (ships first, independently)

## PR 1 Scope

### In Scope
- MCP tool `get_empty_propagation(tables: list[str])` returning a two-view
  `EmptyPropagationResult`.
- CLI `sqlcg analyze empty-impact <table> [<table> ...] [--raw] [--max-depth N]`.
- **View 2 (PRIMARY — existing plan content) — column-lineage refinement**: the
  `_affected_columns_closure` over `COLUMN_LINEAGE`, rolled up to tables, with the
  full/partial split. This is the reliable headline answer (it captures CTE-wrapped
  *derived* reads, which dominate the corpus — see M1 measurement in the header).
- **View 1 (NEW — documented SUPPLEMENT) — `row_empty_tables`**: the UNION of
  (a) View 2's affected tables [tables with ≥1 column derived from a source] and
  (b) tables with a DIRECT `SELECTS_FROM`/`STAR_SOURCE` read of a source (the
  gating-join case — a table that row-empties via inner join on a source but
  derives no column from it). Built via the new `GET_TABLE_READS_ADJACENCY` query +
  cycle-safe BFS for (b), rolled up from the View 2 closure for (a). Carries a
  **documented residual gap**: CTE-wrapped PURE-gating reads are invisible to both
  views (see §View 1 honest semantics).
- Multiple named tables (union of the unfilled set).
- Reuse of the existing closure / rollup / synthetic-exclusion / topological-order
  helpers. No new **column-lineage** traversal code; view 1 adds one new
  reachability query + BFS for branch (b).

### Non-Goals
- **No auto-detection in PR 1.** The trigger is a table-name list. (Auto-detection
  is PR 2.)
- **No parse-time or schema-CSV path.** Pure read-side graph query; does not touch
  the parser, qualify loop, `exp.expand()`, or `qualify()` (CLAUDE.md performance
  invariants + the 2026-05 prohibition untouched).
- **No partial-fill / NULL-rate modelling.** Binary: derived-from-unfilled → empty
  (view 2); reads-from-unfilled → may-be-empty (view 1).
- **No new ordering algorithm.** Depth ordering reuses `_kahn_topological_sort`.
- **No join-type modelling.** Join type is NOT recorded on `SELECTS_FROM` (verified,
  §View 1 honest semantics) — view 1 is therefore an upper bound and labelled as such.

## The two views — View 2 is PRIMARY, View 1 is a documented SUPPLEMENT

### View 2 — Column-lineage refinement (PRIMARY; the reliable headline answer)

Defined fully in its own subsection below (kept from the prior plan). **It is the
headline** because, per the M1 corpus measurement, ~90% of cross-table reads are
CTE/column-lineage routed and carry no top-level `SELECTS_FROM` edge — so the
column-lineage closure is the only view that reliably captures them. View 2 already
captures CTE-wrapped *derived* reads (a column whose value flows out of a source
through a CTE lands as a concrete `COLUMN_LINEAGE` edge at index time).

### View 1 — `row_empty_tables` (NEW; documented SUPPLEMENT to View 2)

**What it answers:** the set of downstream tables that may go fully empty when a
source is empty. It is the **UNION of two branches**:
- **(a) value-derived tables** — View 2's affected-table rollup [every table with
  ≥1 column transitively derived from a source]. These are already in View 2; View 1
  re-exposes them as part of the "may be fully empty" table set.
- **(b) direct gating-join readers** — tables whose writing query has a DIRECT
  (non-CTE) `SELECTS_FROM` or `STAR_SOURCE` read of a source but derives NO column
  from it. This is the only branch View 2 cannot produce.

Consequently `row_empty_tables ⊇ value_affected_tables`. **The genuine value-add of
View 1 over View 2 is exactly branch (b): the direct gating-join reader tables.**

**The motivating case branch (b) catches:**
```sql
INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k
```
`d.x` is lineage-derived from `other.x`, NOT from `x` — so COLUMN_LINEAGE (view 2)
will NOT flag `d.x` when `x` is empty. But if `x` is empty the inner join yields 0
rows, so `d` is **fully empty including `d.x`**. Branch (b) catches this because
`x` is a direct non-CTE source of the query that writes `d`, so a `SELECTS_FROM`
edge exists — independent of column derivation. (If the same read were CTE-wrapped
with no column derived from `x`, **neither** branch would catch it — that is the
documented residual gap below.)

**Edge composition (VERIFIED against the tree):**
- The graph records query→source via `SELECTS_FROM` (`SqlQuery.id` → `SqlTable.qualified`,
  [`queries.sql` L53-59 FIND_TABLE_USAGES](../../src/sqlcg/core/queries.sql)) and
  `STAR_SOURCE` (`SqlQuery` → `SqlTable`, [`indexer.py` L289](../../src/sqlcg/indexer/indexer.py),
  emitted at L1454 with `kind="table"`).
- **The query's OUTPUT table is NOT an edge.** Verified: the indexer emits only
  `QUERY_DEFINED_IN`, `SELECTS_FROM`, `STAR_SOURCE`, `HAS_COLUMN`, `DEFINED_IN`,
  `COLUMN_LINEAGE` from/around a `SqlQuery` ([`indexer.py` L280-289](../../src/sqlcg/indexer/indexer.py)).
  The `INSERTS_INTO` and `DECLARES` RelType enum values exist in
  [`schema.py` L25,L29](../../src/sqlcg/core/schema.py) **but are never written by
  the indexer** (grep-confirmed: zero `INSERTS_INTO`/`DECLARES` writes in
  `src/sqlcg/indexer/`). The output table is the **`SqlQuery.target_table`
  property** (VARCHAR column, [`duckdb_backend.py` L87,L250](../../src/sqlcg/core/duckdb_backend.py);
  set at [`indexer.py` L1346](../../src/sqlcg/indexer/indexer.py) from
  `stmt.target.full_id`). The empty-string sentinel `""` is used when a statement
  has no target (e.g. a bare SELECT).
- **Therefore the table adjacency S→D must be DERIVED at query time** — there is no
  existing table-level reachability to reuse. It is:
  ```sql
  -- GET_TABLE_READS_ADJACENCY (NEW query, view 1)
  -- A query reads FROM S (SELECTS_FROM or STAR_SOURCE) and writes D (q.target_table).
  SELECT DISTINCT sf.dst_key AS source_table, q.target_table AS dest_table
  FROM "SqlQuery" q
  JOIN "SELECTS_FROM" sf ON sf.src_key = q.id
  WHERE q.target_table <> '' AND q.target_table IS NOT NULL
  UNION
  SELECT DISTINCT ss.dst_key AS source_table, q.target_table AS dest_table
  FROM "SqlQuery" q
  JOIN "STAR_SOURCE" ss ON ss.src_key = q.id
  WHERE q.target_table <> '' AND q.target_table IS NOT NULL
  ```
  (Developer MUST verify the exact `STAR_SOURCE` table name and key columns
  (`src_key`/`dst_key`) against the backend's relation tables — confirmed pattern
  matches `SELECTS_FROM` usage in FIND_TABLE_USAGES and ANALYZE_DOWNSTREAM. The
  query name and final SQL are pinned by the view-1 reachability test.)

**Branch (b) traversal: NEW cycle-safe Python BFS mirroring `_affected_columns_closure`.**
- A recursive-CTE form is rejected for the first cut: the existing closure code is
  Python BFS over a per-node query, and view 1 must apply the same node cap and
  cycle guard. Build `_table_row_reachability(db, start_tables, max_depth)`:
  - Load the full adjacency map once (the `GET_TABLE_READS_ADJACENCY` query returns
    the whole `source→dest` edge set; it is table-sized, not closure-sized, so one
    query is acceptable — same shape as how `_kahn_topological_sort` loads adjacency
    once). Build `successors: dict[str, set[str]]`.
  - **M4 — contract synthetic nodes OUT OF THE ADJACENCY ITSELF, before/within the
    BFS, not only from the output list.** A CTE/derived/temp node can legitimately
    appear as a `SELECTS_FROM.dst_key` (i.e. as a `source_table` in the adjacency),
    so a raw BFS would either surface it or dead-end at it. When building
    `successors`, drop any edge whose endpoint `kind in {cte,derived,temp}` and
    **bridge through it** (mirror the contraction intent of `_kahn`'s synthetic
    handling: a real→synthetic→real path must become a real→real successor edge),
    so synthetic nodes never gate or pollute the traversal. The final
    `_exclude_synthetic_tables` on the result is then a belt-and-braces second pass,
    not the only filter.
  - BFS from `start_tables`, `visited` set for cycle-safety, node cap reusing the
    same `_MAX_CLOSURE_NODES` (50k) constant ([`tools.py` L330](../../src/sqlcg/server/tools.py)),
    `truncated=True` when hit.
  - Exclude the named sources themselves from the result (E7 generalised:
    `t not in source_set`).
  - Apply `_exclude_synthetic_tables` on the result (belt-and-braces) so cte/derived/temp
    bridges never surface as row-empty tables (consistent with view 2).
  - Returns `(reachable_tables: list[str], truncated: bool)`.

**View 1 assembly:** `row_empty_tables = _dedup_preserve_order(value_affected_tables
∪ branch_b_reachable)` — branch (a) is rolled up from the View 2 closure, branch (b)
is the BFS result. Both branches exclude the named sources and synthetic nodes.

**HONEST SEMANTICS (must appear in the plan, the model docstrings, AND the output
labels):**

1. **Documented residual gap (M1) — CTE-wrapped pure-gating reads are NOT detected.**
   A query that reads a source *inside a CTE* and derives NO column from it (e.g.
   `INSERT INTO d WITH cte AS (SELECT 1 AS k FROM x) SELECT other.y FROM other JOIN
   cte ON ...`) emits **neither** a top-level `SELECTS_FROM` edge to `x` (the parser's
   `_real_tables` excludes CTE-sourced names — [`base.py` L604](../../src/sqlcg/parsers/base.py),
   out of scope, M1) **nor** a `COLUMN_LINEAGE` edge to `x` (no value derives from it).
   It is therefore invisible to BOTH branches and BOTH views. The `row_empty_tables`
   field docstring **and** the CLI label MUST read honestly:
   **"tables that may go fully empty — direct gating-join reads + value-derived
   tables; CTE-wrapped pure-gating reads are NOT detected (known gap)."** This is
   pinned by Test T0b.

2. **Join-type UPPER BOUND.** A LEFT/OUTER join on the empty table does NOT fully
   empty the downstream table — rows survive with NULLs in the columns drawn from the
   empty side. But **join type is NOT recorded on `SELECTS_FROM`** (verified: the edge
   is a bare `src_key`/`dst_key` pair, no join-type column), so view 1 cannot
   distinguish inner from outer. The label MUST also carry **"may be fully empty
   (depends on join type)"**, never "is empty".

Both caveats appear together in the field docstring and CLI label.

### View 2 — Column-lineage refinement (existing plan content — kept)

Which downstream COLUMNS lose their source VALUES because transitively derived from
the named table's columns. This is the existing `_affected_columns_closure` over
`COLUMN_LINEAGE`, rolled up to tables, with the full/partial split. **Precise for
value derivation; does NOT capture row-level emptiness** (the inner-join case above).

The forward column traversal is **done end-to-end** (verified against the tree):
- [`_affected_columns_closure`](../../src/sqlcg/server/tools.py) (L333) — forward BFS
  over `COLUMN_LINEAGE`; returns `(affected_col_ids, depth_reached, truncated)`; 50k
  cap (`_MAX_CLOSURE_NODES`, L330); cycle-safe; optional `max_depth`.
- [`get_change_scope`](../../src/sqlcg/server/tools.py) (L1024) chains closure →
  `_rollup_to_tables` (L378) → noise-filter → `_exclude_synthetic_tables` (L402,
  drops `cte`/`derived`/`temp`) — exactly the column-empty set for a single source.
- [`_kahn_topological_sort`](../../src/sqlcg/server/tools.py) (L465) +
  `GET_TABLE_ADJACENCY_FOR_COLUMNS` ([`queries.sql` L226](../../src/sqlcg/core/queries.sql))
  give producer-before-consumer ordering. No new column-lineage SQL required.

### The relationship: `row_empty_tables ⊇ value_affected_tables`

View 1 is a superset of View 2's affected-table set by construction (branch (a) IS
that set). State this explicitly:
- **What View 1 adds over View 2:** branch (b) — direct gating-join reader tables
  (e.g. `d` row-emptied by an inner join on `x` even though no `d` column is
  COLUMN_LINEAGE-derived from `x`). This is the genuine value-add.
- **What View 2 carries that View 1's table list flattens:** the per-column
  full/partial distinction. View 2 catches a column `d.x` whose VALUE derives from a
  still-row-populated path; that table may not be fully row-empty, so the table-level
  "may be fully empty" framing of View 1 is coarser. The per-column refinement lives
  only in View 2.
- **What NEITHER catches:** CTE-wrapped pure-gating reads (the documented residual
  gap above).
- **Present them side by side. View 2 is the primary answer; View 1 is the
  supplement.**

## PR 1 Decision — New tool, not an extension or alias (RECOMMENDED, unchanged)

| Option | Verdict |
|--------|---------|
| Extend `get_change_scope` with a flag | **Rejected.** Conflates "if I change this" vs "this is empty"; changes a shipped contract; single-table only; cannot carry view 1. |
| Pure alias delegating to `get_change_scope` | **Rejected as the public shape**; internals delegate to shared helpers. Cannot take a list, cannot carry view 1. |
| **New tool `get_empty_propagation(tables)`** | **CHOSEN.** Distinct intent, multi-source union, two views; reuses existing helpers + the new view-1 traversal. |

## PR 1 Decision — Output shape (`EmptyPropagationResult`, two-view redesign)

New Pydantic model in [`models.py`](../../src/sqlcg/server/models.py), next to
`ChangeScopeResult`. The two views are **first-class and clearly named**; the prior
draft's `fully_empty_tables` / `partially_empty_tables` (which conflated the two
concepts) are **replaced** by the reconciliation below.

```python
class EmptyPropagationResult(BaseModel):
    sources: list[str]               # named unfilled tables (echoed, lowercased, dedup)

    # --- View 1: table-level ROW reachability (NEW, SUPPLEMENT) ---
    row_empty_tables: list[str]      # tables that may go fully empty — UNION of
                                     # (a) value-derived tables [= value_affected_tables]
                                     # and (b) direct gating-join readers (SELECTS_FROM/
                                     # STAR_SOURCE). row_empty_tables ⊇ value_affected_tables.
                                     # KNOWN GAP: CTE-wrapped pure-gating reads are NOT
                                     # detected. UPPER BOUND: depends on join type
                                     # (not recorded). Docstring MUST carry both caveats.

    # --- View 2: COLUMN-lineage refinement (value derivation) ---
    value_empty_columns: list[str]   # downstream column ids transitively derived from a
                                     # source's columns; these lose their source VALUES
    value_affected_tables: list[str] # table rollup of value_empty_columns
                                     # (noise-filtered, synthetic-excluded)
    value_fully_empty_tables: list[str]    # value_affected_tables ALL of whose graph-known
                                           # columns are in value_empty_columns
    value_partially_empty_tables: list[str]  # value_affected_tables where only SOME columns
                                             # are value-empty

    # --- Shared ordering + diagnostics ---
    propagation_order: list[str]     # UNION(row_empty_tables, value_affected_tables) ordered
                                     # closest-to-source first (reuses _kahn_topological_sort).
                                     # NOTE: tables that appear ONLY in row_empty_tables (a
                                     # branch-(b) gating-join reader with no column-lineage
                                     # footprint) have no closure_col_ids adjacency, so their
                                     # ordering falls back to alphabetical.
    downstream_count: int            # len(union of the two views' table sets)
    noise_excluded: list[str]        # tables dropped as backup/noise + synthetic
    truncated: bool                  # 50k cap hit in EITHER traversal
    hint: str | None                 # diagnostics (not found / no downstream / cycle / cap)
```

**Reconciliation of the old fields (state the reasoning, required):** the old draft
exposed `empty_columns`/`affected_tables`/`fully_empty_tables`/`partially_empty_tables`
as the *only* answer — implicitly equating "column-derived-empty" with "the table is
empty", which is exactly the inner-join blind spot. The redesign:
- keeps the column-lineage answer but **renames it `value_*`** to signal it is about
  VALUE derivation, not row presence;
- adds `row_empty_tables` as the **supplement** row-presence answer (view 1) — the
  union of view 2's affected tables and the direct gating-join readers, so
  `row_empty_tables ⊇ value_affected_tables`;
- `fully_empty` / `partially_empty` survive **only inside view 2** (`value_fully_*` /
  `value_partially_*`) where the full-vs-empty-column comparison is meaningful — a
  table is "value-fully-empty" iff all its known columns are value-derived from a
  source. A table can be in `row_empty_tables` (row-gated to zero) without being in
  `value_fully_empty_tables` (because its columns derive from elsewhere) — that
  divergence is the whole point of two views.
- `propagation_order` orders the **union** of both views' table sets; W1 (below)
  still governs the closure id set passed to the sort.

## PR 1 Decision — Entry-point signatures

### MCP tool
```python
@mcp.tool()
@_timed_tool("get_empty_propagation")
def get_empty_propagation(tables: list[str]) -> EmptyPropagationResult: ...
```
- `tables`: one or more qualified names, lowercased (graph keys lowercased at index).
- Raises `NotIndexedError` if no repos indexed (mirror `get_change_scope`).

### CLI surface
```
sqlcg analyze empty-impact <table> [<table> ...] [--raw] [--max-depth N]
```
- Added to the existing `analyze` Typer app
  ([`analyze.py`](../../src/sqlcg/cli/commands/analyze.py)).
- **Backend acquisition (W2 / O1 — RESOLVED, NEW pattern for `analyze.py`).** Verified:
  **no existing `analyze` command holds a `GraphBackend` handle** — all five
  (`upstream`/`downstream`/`impact`/`unused`/`failures`) read via
  `run_read_routed(sql, params)` with raw SQL strings ([`analyze.py` L240-300](../../src/sqlcg/cli/commands/analyze.py)).
  `_compute_empty_propagation` needs a real `db` handle, so there is no sibling
  pattern to copy. The command opens a backend **directly**, mirroring
  `run_read_routed`'s own read-only fallback ([`read_client.py` ~L191](../../src/sqlcg/server/read_client.py)):
  ```python
  from sqlcg.core.config import get_backend
  with get_backend(read_only=True) as db:
      result = _compute_empty_propagation(db, tables, max_depth)
  ```
  `read_only=True` is **mandatory** (lock semantics, read_client.py L187-188). Do
  NOT route this through `run_read_routed` (it returns rows, not a backend) and do
  NOT invent a third seam.

## PR 1 Edge cases (must be handled and tested)

- **E1 — table not found / not indexed.** Echo in `sources`, contribute nothing,
  set `hint`. Do not raise. If no source resolves and no downstream found, `hint`
  says so.
- **E2 — table with no downstream.** Both views empty, `propagation_order` empty,
  `hint` = "No downstream consumers — nothing propagates."
- **E3 — column-level partial impact (view 2 precision).** A downstream table fed
  by an unfilled source AND a still-filled source: ONLY columns derived from the
  unfilled source are in `value_empty_columns`; the table is in
  `value_partially_empty_tables`. **Headline view-2 correctness claim** (Test T3).
- **E3b — direct gating-join (branch (b); the View 1 value-add).** The direct
  inner-join case: `d` ∈ `row_empty_tables` (branch (b) — `x` is a direct non-CTE
  source) but the non-derived column ∉ `value_empty_columns`. **Required new test
  (Test T0a).**
- **E3c — CTE-wrapped pure-gating read (the documented residual gap).** A query reads
  `x` inside a CTE and derives no column from it. `d` ∉ `row_empty_tables` for source
  `x` and `d.*` ∉ `value_empty_columns` — invisible to both views. The test asserts
  the absence and documents the gap. **Required new test (Test T0b).**
- **E4 — CTE/derived/temp synthetic nodes.** Excluded by `_exclude_synthetic_tables`
  (`_SYNTHETIC_TABLE_KINDS = {cte, derived, temp}`) in BOTH views and contracted in
  `_kahn_topological_sort`. Synthetic bridges never appear in any output table list.
- **E5 — 50k cap.** `truncated=True` if EITHER traversal hits the cap; surface in
  `hint`. Multi-source applies the cap across the combined start set.
- **E6 — star-source columns.** Star-expanded lineage lands as concrete
  `COLUMN_LINEAGE` at index time (view 2); the `STAR_SOURCE` edge also feeds view 1's
  adjacency. Test asserts a star-fed downstream is reported in both views.
- **E7 — self-reference / cycle.** A source in its own downstream closure must not be
  reported as its own affected table — `t not in source_set` filter in both views.

## PR 1 Multi-source union semantics

- View 2: resolve each source's columns (`GET_COLUMNS_FOR_TABLE_QUERY`), concat into
  one `start_cols`, run a **single** `_affected_columns_closure` over the union.
- View 1: seed `_table_row_reachability` BFS from all named source tables at once.
- `sources` echoes all named tables (lowercased, dedup, order-preserving).
- Exclude every named source from both views' table sets (E7 generalised).

## PR 1 Implementation Steps (ranked — build first → last)

### Phase 1: Model + view-1 traversal + engine (no MCP/CLI yet)

**Step 1.1 — Add two-view `EmptyPropagationResult` model.**
- File: [`models.py`](../../src/sqlcg/server/models.py), next to `ChangeScopeResult`.
- Acceptance: model importable; all fields typed as §Output shape; defaults match
  siblings (`default_factory=list`, `truncated=False`, `hint=None`); docstrings carry
  the view-1 upper-bound caveat and the view-1-vs-view-2 distinction.

**Step 1.2 — Add view-1 reachability: `GET_TABLE_READS_ADJACENCY` query +
`_table_row_reachability(db, start_tables, max_depth)`.**
- File: query in [`queries.sql`](../../src/sqlcg/core/queries.sql) (named block) +
  constant in [`queries.py`](../../src/sqlcg/core/queries.py); helper in `tools.py`.
- Query: `SELECTS_FROM ∪ STAR_SOURCE` (query→source) joined with `SqlQuery.target_table`
  (query→dest), `target_table <> ''`. Developer verifies exact relation/key names.
- Helper (branch (b)): load adjacency once, **contract synthetic nodes out of the
  adjacency before/within the BFS (M4)** — drop/bridge edges whose endpoint
  `kind in {cte,derived,temp}` so a real→synthetic→real path becomes a real→real
  successor edge — then cycle-safe BFS from `start_tables`, `_MAX_CLOSURE_NODES`
  cap → `truncated`, exclude named sources, final `_exclude_synthetic_tables`
  belt-and-braces pass. Returns `(reachable_tables, truncated)`.
- Acceptance: given the direct gating-join fixture (Test T0a), returns `d` for
  source `x`; the CTE-wrapped pure-gating fixture (Test T0b) does NOT return `d`
  (documented gap); cycle fixture does not loop; a synthetic node that appears as a
  `SELECTS_FROM.dst_key` is bridged through and never in output.

**Step 1.3 — Factor the engine `_compute_empty_propagation(db, tables, max_depth)`.**
- Build view 2 first (existing closure → rollup → noise → exclude → full/partial
  split) — it is the primary answer and supplies `value_affected_tables`. Then build
  view 1 as `row_empty_tables = _dedup_preserve_order(value_affected_tables ∪
  _table_row_reachability(...))` — i.e. branch (a) = the view-2 affected-table rollup,
  branch (b) = the BFS result. No new column-lineage traversal/SQL.
- **W1 (MANDATORY — silent wrong output if missed).** When calling
  `_kahn_topological_sort` for `propagation_order`, pass the **union of start columns
  and affected columns**, deduped order-preserving, exactly as `get_backfill_order`
  ([`tools.py` L1154](../../src/sqlcg/server/tools.py)):
  ```python
  closure_col_ids = _dedup_preserve_order([*start_cols, *affected_cols])
  order, had_cycle = _kahn_topological_sort(order_tables, db, closure_col_ids)
  ```
  Rationale: `GET_TABLE_ADJACENCY_FOR_COLUMNS_QUERY` needs both endpoints of the
  direct producer edge — including the source's own columns — present in the id set,
  otherwise multi-hop chains routed through CTE/derived bridges degrade toward
  alphabetical (the synthetic-node contraction relies on those endpoints). Do NOT
  pass `affected_cols` alone. `order_tables` is the union of `row_empty_tables` and
  `value_affected_tables` (dedup order-preserving).
- `value_fully_empty_tables` / `value_partially_empty_tables`: compare each
  `value_affected_tables` table's value-empty column subset against its full known
  column set (`GET_COLUMNS_FOR_TABLE_QUERY`). Read-only, table-set-sized.
- Returns a plain dataclass/tuple the tool and CLI both consume.
- Acceptance: pure function of `(db, tables, max_depth)`; on the fixtures returns the
  documented sets; the `_kahn_topological_sort` call receives
  `_dedup_preserve_order([*start_cols, *affected_cols])` — pinned behaviourally by the
  N3 bridge test.

### Phase 2: MCP tool
**Step 2.1 — `get_empty_propagation(tables)` MCP tool.**
- File: tools.py, near the other change tools.
- Body: `_assert_indexed`, `_open_backend`, call `_compute_empty_propagation`, build
  `EmptyPropagationResult`.
- Acceptance: registered (`@mcp.tool()`); single + multi inputs return correct
  two-view sets on the integration fixtures; `NotIndexedError` when empty.
- **Call site:** the `@mcp.tool()` registration IS the call site (FastMCP dispatch);
  confirm it appears in the server tool list. Bump the MCP-tool count in
  [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md) **L87 (live value: 16) → 17
  after PR 1** (PR 2 takes it to 18). Verified 2026-06-12.

### Phase 3: CLI surface
**Step 3.1 — `sqlcg analyze empty-impact` command.**
- File: [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py).
- Backend acquisition: W2/O1 pattern (`with get_backend(read_only=True) as db:`).
- **N1 — noise filter repo root.** Non-`--raw` mode: `NoiseFilter.from_config(repo_root=resolved_repo_root())`,
  importing `resolved_repo_root` from `read_client.py` exactly as the siblings do
  (analyze.py L249, L298). Do NOT hardcode the root.
- **N2 — `--max-depth` validation.** Default `None` (= unbounded, valid). When
  supplied, must satisfy `1 <= max_depth <= 100`; `0`, negative, or `> 100` prints
  `[red]Error: --max-depth must be between 1 and 100[/red]` and `raise typer.Exit(1)`
  (match siblings analyze.py L138-140, L179-181).
- Renders a Rich layout. **View 2 (value-affected columns) is presented first as the
  primary answer** — a table (`value_*`, with per-table `value-empty / total columns`
  and a `full|partial` marker). **View 1 (`row_empty_tables`) follows as a labelled
  supplement** whose header/caption MUST carry BOTH caveats:
  **"tables that may go fully empty — direct gating-join reads + value-derived
  tables; CTE-wrapped pure-gating reads are NOT detected (known gap); depends on join
  type."** Honours `--raw` and `--max-depth`.
- Acceptance: `uv run sqlcg analyze empty-impact <fixture-table>` prints BOTH views;
  View 2 first; the View 1 label string contains both the "known gap" and the
  "depends on join type" caveats; the value full/partial marker is shown; `--raw`
  shows noise rows; omitting `--max-depth` runs unbounded (exit 0); `--max-depth 0`
  and `--max-depth 101` each exit non-zero with the documented message.

### Phase 4: Docs + version
**Step 4.1 — Version bump + ARCHITECTURE_REVIEW tool-count + tool docstring.**
- `pyproject.toml` + `src/sqlcg/__init__.py` → `1.22.0`; `uv lock`.
- Update MCP-tool count in ARCHITECTURE_REVIEW.md **L87: 16 → 17**.

## PR 1 Test Strategy

Tests assert **observable output** (specific column/table ids), never just "no
exception". Name `test_<unit>_<scenario>_<expected>`, link this plan in the docstring.

- **Unit (`tests/unit/`):**
  - View-2 helper, hand-built graph: exact `value_empty_columns` / `value_affected_tables`
    for a known single source.
  - Multi-source union: deduped union of two sources' closures; both sources excluded.
  - Table-not-found: contributes nothing, sets `hint`, no raise.
  - No-downstream: empty sets + "nothing propagates" hint.
  - Truncation: stub backend reporting the cap sets `truncated=True`; hint mentions cap.

- **Integration (real in-memory DuckDB, `tests/integration/`):**
  - **T0a (branch (b) — direct gating join, CAUGHT).** Fixture:
    `INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k`, where `d.x`
    is COLUMN_LINEAGE-derived from `other.x` (NOT from `x`). `x` is a direct non-CTE
    source → a `SELECTS_FROM` edge exists. For source `x`, assert:
    `d` ∈ `row_empty_tables` (branch (b)); `d.x` ∉ `value_empty_columns` (not derived
    from `x`). This is View 1's value-add — it catches the direct gating join View 2
    misses.
  - **T0b (CTE-wrapped pure-gating — DOCUMENTED GAP).** Fixture:
    `INSERT INTO d WITH cte AS (SELECT 1 AS k FROM x) SELECT other.y FROM other JOIN
    cte ON other.k = cte.k` — no column derived from `x`, and `x` is read only inside
    the CTE. For source `x`, assert `d` is **NOT** in `row_empty_tables` and `d.*` ∉
    `value_empty_columns`. This pins the documented residual gap: neither view detects
    a CTE-wrapped pure-gating read. The test docstring states this is an accepted
    known gap (parser `_real_tables` is out of scope, M1).
  - **T3 (view-2 partial impact).** `d.x` derives from unfilled `s.a`, `d.y` from a
    still-filled `f.b`. Assert `d.x` ∈ `value_empty_columns`; `d.y` ∉; `d` ∈
    `value_partially_empty_tables`, ∉ `value_fully_empty_tables`.
  - **Value-fully-empty fixture:** a downstream table all of whose columns derive from
    the unfilled source → in `value_fully_empty_tables`.
  - **Propagation order (N3 — exercises the W1 path).** Multi-hop `s → m → d` returns
    `[m, d]` (closest-to-source first). The hop `s → m` MUST route **through a
    CTE/derived BRIDGE node** (synthetic `kind in {cte,derived,temp}`), NOT a direct
    real-table edge — only the contraction path inside `_kahn_topological_sort`
    depends on `start_cols` being in the adjacency id set (W1). A direct-edge fixture
    would pass even with `affected_cols` alone, masking the regression. Build the
    bridge so that passing `affected_cols` alone makes this test observably fail.
  - **Synthetic exclusion:** a CTE/derived bridge between `s` and `d` appears in NO
    output table list (either view); `d` still appears.
  - **Star-source:** `SELECT *` from `s` feeding `d` reports `d` in both views.
  - Fixtures use **aliased / schema-qualified** statement shapes matching the live
    corpus (minimal idealized fixtures hide real failure modes — project history).

- **E2E (`tests/e2e/`):**
  - `sqlcg analyze empty-impact <table>` against a small indexed fixture repo prints
    both views with the row caveat label and the value full/partial marker.

## PR 1 Acceptance Criteria
- [ ] **Row reachability branch (b) (view 1, CAUGHT):** for the direct gating-join
      fixture, `d` ∈ `row_empty_tables` for source `x`, AND the non-derived column ∉
      `value_empty_columns` (column equality, Test T0a).
- [ ] **Documented residual gap (view 1, NOT caught):** for the CTE-wrapped
      pure-gating fixture, `d` ∉ `row_empty_tables` for source `x` (Test T0b).
- [ ] `row_empty_tables ⊇ value_affected_tables` holds on a fixture exercising both
      branches (set-containment assertion).
- [ ] `row_empty_tables` docstring and the CLI label carry BOTH caveats: the
      **"CTE-wrapped pure-gating reads are NOT detected (known gap)"** residual-gap
      string AND the **"depends on join type"** upper-bound string (string assertions).
- [ ] **Value derivation (view 2):** `value_empty_columns` = exactly the downstream
      columns transitively derived from the source (column-id equality on a fixture).
- [ ] Partial impact: a table fed by both unfilled + still-filled sources has ONLY
      derived columns in `value_empty_columns` and is in `value_partially_empty_tables`.
- [ ] `propagation_order` lists the union of both views' tables closest-to-source first
      (3-hop bridge chain asserted).
- [ ] cte/derived/temp synthetic nodes never appear in any output table list.
- [ ] Multi-source returns the union and excludes all named sources.
- [ ] Not-found source echoed, no raise, `hint` set. No-downstream → empty + hint.
      `truncated=True` + hint when the 50k cap is hit (either traversal).
- [ ] CLI prints BOTH views; `--raw` and `--max-depth` work; bad `--max-depth` exits
      non-zero with the documented message.
- [ ] Version `1.22.0` in `pyproject.toml`, `__init__.py`, `uv.lock`.
- [ ] Every new method has a grep-confirmed call site
      (`_table_row_reachability` ← engine; `_compute_empty_propagation` ← MCP + CLI;
      `get_empty_propagation` ← `@mcp.tool()`; CLI command ← Typer app).
- [ ] **Live-DWH re-acceptance** before tag (lineage-class — CI green is necessary but
      not sufficient; see R4).

## PR 1 Risks and Mitigations
- **R1 — CLI cannot call the MCP tool function directly.** Mitigated by the
  `_compute_empty_propagation` extraction + the W2/O1 `get_backend(read_only=True)` seam.
- **R2 — view-1 adjacency derived wrong.** `target_table` is a property, not an edge,
  and the empty-string sentinel must be excluded. Pinned by Test T0a and a query test.
- **R3 — `value_fully/partially` split mis-derives the full column set.** The "known
  columns" must come from the same `GET_COLUMNS_FOR_TABLE_QUERY` the engine seeds from.
- **R4 — perf.** Pure read path. View 1 adds one whole-graph adjacency query (table-sized)
  + a BFS; view 2 reuses the single-closure + single aggregate-adjacency query. Does NOT
  touch parser/indexer hot paths or CLAUDE.md performance invariants.
- **R5 — live correctness (lineage-class).** Requires **live-DWH re-acceptance** before
  tag: run `get_empty_propagation` / `analyze empty-impact` on a real DWH graph for a
  known-empty table; eyeball that the row-reachability set ⊇ the value-affected set in
  the expected places and that the inner-join case is caught. Record in postmortem.

## PR 1 Version & Release
- Additive → **minor**: `1.21.2` → **`1.22.0`**. Bump `pyproject.toml`,
  `__init__.py`, `uv lock` in the feature branch.
- Agent does NOT close issues, does NOT tag; user verifies live + tags `v1.22.0`.

---

# PR 2 — PR-impact detector (ships after PR 1; depends on its engine)

## PR 2 Goal

Detect, from a code change between two index runs / git refs, when a table **LOSES
its producer** (the feeding edges that wrote it disappear) — then auto-feed those
tables into PR 1's blast-radius engine and return the same two-view result,
attributed to the detected change. This turns the blast radius into a CI gate
without hand-naming tables.

## PR 2 Scope

### In Scope
- Detect "tables that lost their producer" between a base ref and the current
  indexed/HEAD state.
- Feed that table set into PR 1's `_compute_empty_propagation` engine; return the
  same two-view `EmptyPropagationResult`, wrapped with attribution (which removed/
  changed files dropped each producer).
- MCP tool `get_pr_impact(base_ref: str)` + CLI `sqlcg analyze pr-impact --base <ref>`.

### Non-Goals (state prominently — do not mis-sell)
- **BLIND to runtime emptiness.** An edge "disappears" only when the SQL that
  produced it is removed/renamed/broken. This is a **CODE-REGRESSION** signal. If a
  job runs and produces 0 rows while the SQL is unchanged, NO edge disappears and PR 2
  detects nothing. Runtime row-count monitoring needs an external row-count feed and
  is **explicitly OUT OF SCOPE**. The tool MUST NOT be described as "data monitoring".
- No row-count export, no `last_loaded`/`filled`/`row_count` signal (consistent with
  PR 1 and the standing non-detection decision for runtime signals).
- No new blast-radius logic — PR 2 reuses PR 1's engine verbatim.

## PR 2 Detection mechanism — investigation + RECOMMENDATION

**What already exists (verified):**
- [`git_name_status_delta(root, old_sha, new_sha)`](../../src/sqlcg/indexer/git_delta.py)
  — runs `git diff --name-status`, returns a `Delta(added, modified, deleted)` of
  absolute `.sql` paths (renames split into delete+add). Returns `None` on git failure
  / shallow / unknown SHA → callers fall back to full index. **This is the file-level
  change set PR 2 needs.**
- [`resync_changed(path, from_sha, to_sha, backend, dialect, ...)`](../../src/sqlcg/indexer/indexer.py)
  (L770) — already consumes a `Delta`, splits into `added | modified` (re-resolve) vs
  `deleted` (drop), and updates the graph. Used by `sqlcg reindex`
  ([`reindex.py` L172-210](../../src/sqlcg/cli/commands/reindex.py)) with stored
  `indexed_sha → HEAD`.
- [`compute_freshness(root, indexed_sha)`](../../src/sqlcg/core/freshness.py) +
  `DuckDBBackend.get_indexed_sha()` ([`duckdb_backend.py` L807](../../src/sqlcg/core/duckdb_backend.py))
  — give `indexed_sha` (the base of the currently-indexed graph) and the commit delta
  to HEAD. **The indexed graph's base ref is recoverable.**
- Tests: `test_resync.py`, `test_mvcc_rebuild.py`, `test_reindex_via_server.py` cover
  the resync path; no existing `detect_changes`/snapshot-diff API exists (grep-confirmed:
  no `detect_changes` symbol anywhere in `src/sqlcg`).

**The main open design question — how to capture the prior producer-edge state.**
A table "lost its producer" means: at `base_ref` some query wrote it (it had an
incoming producer edge), and at the new state no query writes it. The producer edge is
NOT a stored relation — it is the `SqlQuery.target_table` property (§PR 1 View 1). So
"who produces table T" = `SELECT DISTINCT target_table FROM SqlQuery` intersected with
T. We need this set at TWO points in time.

| Option | How | Pros | Cons | Verdict |
|---|---|---|---|---|
| **(A) Index-two-refs-and-diff** | Index `base_ref` into a throwaway/secondary graph, index current state into another, diff the producer-table sets | Exact; reuses `index_repo` wholesale | Two full indexes (minutes each on DWH); needs a checkout of `base_ref`; heavy for a CI gate | **Rejected as default** — too slow; violates the 5-min budget twice over |
| **(B) Producer-set snapshot + diff** | Before the change, snapshot the producer-table set (`SELECT DISTINCT target_table FROM SqlQuery WHERE target_table<>''`) from the **currently-indexed** graph (whose base is `indexed_sha`); after `reindex`/index to the new state, recompute the producer set; the lost producers = `before − after` | One cheap query before + after; no second full index; reuses the existing `reindex` resync the user already runs | The "before" must be captured against the graph state at `base_ref`; if the graph was already advanced, the snapshot is stale | **RECOMMENDED** with the base-ref guard below |
| **(C) Parse-only of the changed files** | For each `deleted`/`modified` file in `git_name_status_delta(base, head)`, parse it to recover the `target_table`(s) it USED to write, then check whether any surviving query still writes those tables in the current graph | No second full index; scoped to the diff (fast); directly answers "which producers did this change remove?" | Needs to parse the OLD version of deleted/modified files (`git show base:path`); parsing is the same hot path PR 1 avoids | **Fallback / refinement** — precise attribution, used to attribute lost producers to specific files |

**RECOMMENDATION — Option B as the primary mechanism, with Option C for
attribution, gated by the freshness check.**

**O2 RESOLVED (M6 — shepherd decision):** `get_pr_impact` **REQUIRES the graph
pre-indexed at `base_ref`** and **orchestrates the resync to HEAD internally**.
Orchestrating `resync_changed` from inside the tool is **NOT** "mutating the working
tree" — `resync_changed` updates only the graph DB, never the filesystem; it is
exactly what `sqlcg reindex` already does. The tool's precise sequence:

1. Resolve `base_ref` → `base_sha`. The detection is defined as
   **`producers(base_sha) − producers(HEAD-after-resync)`**.
2. **Assert** `get_indexed_sha() == base_sha`. If not, return a `hint` telling the
   user to index at `base_ref` first — do NOT silently compare mismatched states, do
   NOT crash.
3. **Capture `producers_before`** = `SELECT DISTINCT target_table FROM "SqlQuery"
   WHERE target_table <> ''` (the `GET_PRODUCER_TABLES` query) from the
   base-indexed graph.
4. **Orchestrate `resync_changed(root, base_sha, head_sha, backend, dialect, ...)`**
   internally to advance the graph DB from `base_sha` to HEAD (graph-only update, not
   a working-tree mutation — same op as `sqlcg reindex`).
5. **Capture `producers_after`** the same way from the now-advanced graph.
6. **`lost_producer_tables = producers_before − producers_after`**, minus synthetic
   (`_exclude_synthetic_tables`) and named-source self-exclusion.
7. **Option C attribution (refinement):** for each lost-producer table, use
   `git_name_status_delta(base_sha, head_sha)` `deleted ∪ modified` to attribute which
   file(s) dropped the producer (parse the OLD file version via `git show` only for the
   small changed set — bounded by the diff, not the corpus; this is the only parse and
   it is scoped, not a hot-path violation).
8. Feed `lost_producer_tables` into PR 1's `_compute_empty_propagation(db,
   lost_producer_tables, max_depth)` and return its two-view result, wrapped with the
   attribution.

> **Why not just diff the file delta directly?** A `modified` file may still write the
> same table (it changed a WHERE clause); a `deleted` file's table may be written by
> another surviving file. Only the producer-SET diff (B) answers "is the table now
> orphaned?" correctly. Option C alone would over-report (every changed producer file)
> — it is attribution, not detection.

**Helper split (M6 — the reviewer noted the prior `_detect_lost_producers` conflated
snapshot-capture with diff-computation).** Split into three pieces:
- `_capture_producer_set(db) -> set[str]` — runs `GET_PRODUCER_TABLES`, returns the
  producer-table set (used for both the before and after snapshots; pure read).
- `get_pr_impact` (the MCP tool) **owns the orchestration** — assert base, capture
  before, run `resync_changed`, capture after (steps 2–5 above).
- `_diff_lost_producers(before, after, db) -> tuple[list[str], dict[str, list[str]]]`
  — computes `before − after`, excludes synthetic, builds Option C attribution.

## PR 2 Decision — Entry-point signatures

### MCP tool
```python
@mcp.tool()
@_timed_tool("get_pr_impact")
def get_pr_impact(base_ref: str, max_depth: int | None = None) -> PrImpactResult: ...
```
- Returns `PrImpactResult` (new model, §below) wrapping the PR 1 two-view result.
- Raises `NotIndexedError` if no repos indexed.

### CLI surface
```
sqlcg analyze pr-impact --base <ref> [--raw] [--max-depth N]
```
- Same `analyze` Typer app; **same backend seam as PR 1** (W2/O1:
  `with get_backend(read_only=True) as db:`). Reuses PR 1's resolved CLI pattern —
  no new seam.
- `--base` required (the ref to compare against). `--max-depth` / `--raw` mirror PR 1.

## PR 2 Decision — Output shape (`PrImpactResult`)

New model in [`models.py`](../../src/sqlcg/server/models.py), wrapping PR 1's result:
```python
class PrImpactResult(BaseModel):
    base_ref: str                       # echoed
    base_sha: str | None                # resolved base SHA (None if unresolvable → hint)
    head_sha: str | None
    lost_producer_tables: list[str]     # tables whose producer disappeared (detection output)
    attribution: dict[str, list[str]]   # lost_table -> [files that dropped its producer] (Option C)
    blast_radius: EmptyPropagationResult  # PR 1 engine output for lost_producer_tables
    detection_only_code_regression: bool = True  # constant True — documents the runtime-blind contract
    hint: str | None                    # base not resolved / graph not at base_ref / no producers lost
```
- `detection_only_code_regression` is a literal `True` field whose docstring states the
  runtime-emptiness blind spot — the contract is visible in the output, not just the docs.

## PR 2 Implementation Steps (ranked — build first → last)

### Phase 1: Detection helper + model
**Step 2.1 — Add `PrImpactResult` model** (§Output shape). Acceptance: importable,
typed, docstrings carry the code-regression-only caveat.

**Step 2.2 — Snapshot + diff helpers (M6 split — snapshot ≠ diff).**
- `_capture_producer_set(db) -> set[str]` — runs the NEW named query
  `GET_PRODUCER_TABLES` (`SELECT DISTINCT target_table FROM "SqlQuery" WHERE
  target_table <> ''`), returns the producer-table set. Pure read; no SHA logic.
- `_diff_lost_producers(before, after, db, base_sha, head_sha) -> tuple[list[str],
  dict[str, list[str]]]` — computes `before − after`, excludes synthetic via
  `_exclude_synthetic_tables`, and builds Option C attribution from
  `git_name_status_delta(base_sha, head_sha)` `deleted ∪ modified`.
- These two helpers do NOT capture SHAs, do NOT assert base state, and do NOT
  resync — that orchestration lives in `get_pr_impact` (Step 2.3).
- Acceptance: `_capture_producer_set` returns the producer set on a hand-built
  fixture; `_diff_lost_producers` on a `before`/`after` pair where a producer was
  dropped returns the orphaned table in the lost set, excludes a still-produced
  table, and maps the orphaned table to the deleted file.

### Phase 2: Orchestration + MCP tool
**Step 2.3 — `get_pr_impact(base_ref, max_depth)` MCP tool (owns orchestration).**
- Sequence (O2 RESOLVED — M6): (1) resolve `base_ref`→`base_sha`; (2) **assert**
  `get_indexed_sha() == base_sha` else return mismatch hint (no crash, no compare);
  (3) `producers_before = _capture_producer_set(db)`; (4) **orchestrate**
  `resync_changed(root, base_sha, head_sha, backend, dialect, ...)` to advance the
  graph DB to HEAD (graph-only, NOT a working-tree mutation — same as `sqlcg
  reindex`); (5) `producers_after = _capture_producer_set(db)`; (6)
  `lost, attribution = _diff_lost_producers(producers_before, producers_after, db,
  base_sha, head_sha)`; (7) feed `lost` into `_compute_empty_propagation`; (8) build
  `PrImpactResult`.
- Acceptance: registered (`@mcp.tool()`); fixture with a dropped producer returns the
  lost table + its PR-1 two-view blast radius; `NotIndexedError` when empty; graph
  not at `base_ref` returns the documented hint with NO resync run, no crash;
  unresolvable `base_ref` returns a hint.
- **Call site:** `@mcp.tool()` registration. Bump the MCP-tool count in
  `ARCHITECTURE_REVIEW.md` **L87: 17 → 18 after PR 2**.

### Phase 3: CLI surface
**Step 2.4 — `sqlcg analyze pr-impact --base <ref>` command.**
- File: `analyze.py`; reuse PR 1's `get_backend(read_only=True)` seam, N1 noise-filter
  pattern, N2 `--max-depth` validation.
- Renders: lost-producer tables + attribution, then PR 1's two-view blast radius;
  prints the code-regression-only caveat line prominently.
- Acceptance: `uv run sqlcg analyze pr-impact --base <ref>` on a fixture repo with a
  dropped producer prints the lost table, its attribution, and both blast-radius views.

### Phase 4: Docs + version
**Step 2.5 — Version `1.23.0` + ARCHITECTURE_REVIEW tool-count + docstrings.**
- `pyproject.toml` + `__init__.py` → `1.23.0`; `uv lock`. Bump ARCHITECTURE_REVIEW.md
  **L87: 17 → 18**. Document the code-regression-only / runtime-blind contract in the
  tool docstring and ARCHITECTURE_REVIEW.

## PR 2 Test Strategy
- **Unit:** `_capture_producer_set` returns the producer set; `_diff_lost_producers`
  returns `before − after` with synthetic excluded and attribution mapping lost
  table → file; `get_pr_impact` mismatch path (graph not at base) returns hint with NO
  resync run and no detection (assert `resync_changed` not invoked).
- **Integration (real DuckDB + a tmp git repo):** index a base state with a producer
  file writing `d`; delete/break that file; reindex to head; assert `d` ∈
  `lost_producer_tables`, a still-produced `e` ∉, attribution maps `d`→the deleted file,
  and `blast_radius.row_empty_tables` contains `d`'s downstream. A `modified`-but-still-
  produces fixture asserts the table is NOT reported (no over-report).
- **E2E:** `sqlcg analyze pr-impact --base <ref>` end-to-end on a fixture repo prints
  the lost producer + blast radius + the runtime-blind caveat.
- Tests assert **observable output**, not "no exception". Aliased/schema-qualified
  fixtures matching the live corpus.

## PR 2 Acceptance Criteria
- [ ] A deleted/renamed/broken producer file makes its orphaned table appear in
      `lost_producer_tables`; a table still produced by a surviving query does NOT.
- [ ] `attribution[lost_table]` lists the file(s) that dropped the producer.
- [ ] `blast_radius` is PR 1's two-view `EmptyPropagationResult` for the lost set
      (row + value views both populated as in PR 1).
- [ ] `detection_only_code_regression is True` and the docstring + CLI output state the
      runtime-emptiness blind spot.
- [ ] Graph not indexed at `base_ref` → documented hint, no silent mismatched compare.
- [ ] Unresolvable `base_ref` → hint, no crash.
- [ ] CLI `analyze pr-impact --base <ref>` prints lost producers, attribution, both
      blast-radius views, and the runtime-blind caveat; `--raw`/`--max-depth` work.
- [ ] Version `1.23.0` in `pyproject.toml`, `__init__.py`, `uv.lock`.
- [ ] Every new method has a grep-confirmed call site (`_capture_producer_set` ←
      `get_pr_impact`; `_diff_lost_producers` ← `get_pr_impact`; `get_pr_impact` ←
      `@mcp.tool()`; CLI command ← Typer app).
- [ ] **Live-DWH re-acceptance** before tag (delete a real producer file, run the gate,
      confirm the orphaned table + blast radius are sane).

## PR 2 Risks and Mitigations
- **R6 — mis-sold as runtime data monitoring.** Mitigated by the prominent non-goal,
  the `detection_only_code_regression` field, and the CLI caveat line.
- **R7 — stale base snapshot.** The `get_indexed_sha() == base_sha` guard prevents a
  silent mismatched compare; mismatch returns a hint instead.
- **R8 — over-report from modified-but-still-produces files.** Mitigated by the
  producer-SET diff (Option B), not a raw file diff; pinned by the modified-fixture test.
- **R9 — perf.** Producer-set query is `DISTINCT target_table` over `SqlQuery`
  (table-sized); attribution parses only the small changed-file set (`git_name_status_delta`),
  never the corpus. No hot-path / CLAUDE.md invariant impact.

## PR 2 Version & Release
- Additive → **minor**: `1.22.0` → **`1.23.0`**. Agent does NOT close issues or tag;
  user verifies live + tags `v1.23.0`.

---

## Cross-cutting build order + gating

- **Within each PR:** model → helper/engine → MCP tool → CLI → docs/version.
- **Across PRs:** PR 2 cannot start until PR 1's engine (`_compute_empty_propagation`,
  both views, `EmptyPropagationResult`) merges.
- Both PRs: grep-confirmed call sites; observable-output tests (not "no exception");
  ARCHITECTURE_REVIEW.md tool-count updates; live-DWH re-acceptance before tag.

## Open Questions
- **O1 — CLI backend acquisition — RESOLVED** (W2). Both PRs open a backend directly via
  `with get_backend(read_only=True) as db:` (mirrors `read_client.py` read-only fallback,
  L187-191); no existing `analyze` command holds a `GraphBackend` handle. Not a third seam.
- **O2 — PR 2 base-ref orchestration — RESOLVED (M6, shepherd decision).**
  `get_pr_impact` **requires the graph pre-indexed at `base_ref`** (asserts
  `get_indexed_sha() == base_sha`) and **orchestrates `resync_changed` to HEAD
  internally**. The resync updates only the graph DB, not the filesystem — it is the
  same op as `sqlcg reindex`, so this is NOT a working-tree mutation. The helpers are
  split so snapshot-capture (`_capture_producer_set`) and diff-computation
  (`_diff_lost_producers`) are separate from the orchestration in the tool. See §PR 2
  Detection mechanism steps 1–8 and Step 2.3.

> **This spec has been through the plan-reviewer gate; the two BLOCKERS (M1 view-1
> reframe, M6 / O2 orchestration) and the M4/Kahn/tool-count warnings are resolved
> per the shepherd's decisions (2026-06-12).** Ready for shepherd verification — do
> NOT re-loop the reviewer.
