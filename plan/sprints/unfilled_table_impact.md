# Feature Plan: Data-Loss Impact Analysis (two sequenced PRs)

> Status: EXPANDED to a SEQUENCED TWO-PR spec 2026-06-12. Preserves the
> plan-reviewer-revised W1/W2/N1/N2/N3 content (commit dcda023), now scoped into
> **PR 1**. Adds the table-level row-reachability view (PR 1, NEW) and the
> PR-impact detector (PR 2, NEW). **This expanded spec MUST go back through the
> plan-reviewer gate before any implementation** — PR 2's edge-disappearance
> detection is genuinely new logic, and PR 1's row-reachability view adds a new
> query + traversal that the prior review did not cover.
> Owner (compliance): architect-planner.
> Version targets: **PR 1 → 1.22.0**, **PR 2 → 1.23.0** (both additive → minor).

## Feature Summary (both PRs)

Answer the operational question "a job failed and left table(s) empty — what
downstream is at risk?" in two stages:

- **PR 1 — Blast-radius core (the engine).** Given user-NAMED empty table(s),
  report the downstream blast radius in **two clearly-separated first-class views**:
  (1) **table-level row reachability** (which downstream tables transitively READ
  FROM the empty table and therefore MAY go fully empty) and (2) **column-lineage
  refinement** (which downstream COLUMNS lose their source VALUES). Ships first,
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
- **View 1 (NEW) — table-level row reachability**: which downstream tables
  transitively READ FROM a named empty table (S→D where a query reads FROM S and
  writes D), via a new adjacency query + cycle-safe BFS.
- **View 2 (existing plan content) — column-lineage refinement**: the
  `_affected_columns_closure` over `COLUMN_LINEAGE`, rolled up to tables, with the
  full/partial split.
- Multiple named tables (union of the unfilled set).
- Reuse of the existing closure / rollup / synthetic-exclusion / topological-order
  helpers. No new **column-lineage** traversal code; view 1 adds one new
  reachability query + BFS.

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

## The two views — both first-class, NOT merged

### View 1 — Table-level row reachability (NEW; the headline answer for a failed job)

**What it answers:** which downstream tables transitively READ FROM the empty
table and therefore MAY go fully empty (rows gated to zero by an inner join /
WHERE on the empty input).

**Why column lineage misses it (the motivating case):**
```sql
INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k
```
`d.x` is lineage-derived from `other.x`, NOT from `x` — so COLUMN_LINEAGE (view 2)
will NOT flag `d.x` when `x` is empty. But if `x` is empty the inner join yields 0
rows, so `d` is **fully empty including `d.x`**. View 1 catches this because it
asks "does the query that writes `d` READ FROM `x`?" (yes — `x` is in the query's
`SELECTS_FROM`), independent of column derivation.

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

**Traversal: NEW cycle-safe Python BFS mirroring `_affected_columns_closure`.**
- A recursive-CTE form is rejected for the first cut: the existing closure code is
  Python BFS over a per-node query, and view 1 must apply the same node cap and
  cycle guard. Build `_table_row_reachability(db, start_tables, max_depth)`:
  - Load the full adjacency map once (the `GET_TABLE_READS_ADJACENCY` query returns
    the whole `source→dest` edge set; it is table-sized, not closure-sized, so one
    query is acceptable — same shape as how `_kahn_topological_sort` loads adjacency
    once). Build `successors: dict[str, set[str]]`.
  - BFS from `start_tables`, `visited` set for cycle-safety, node cap reusing the
    same `_MAX_CLOSURE_NODES` (50k) constant ([`tools.py` L330](../../src/sqlcg/server/tools.py)),
    `truncated=True` when hit.
  - Exclude the named sources themselves from the result (E7 generalised:
    `t not in source_set`).
  - Apply `_exclude_synthetic_tables` so cte/derived/temp bridges never surface as
    row-empty tables (consistent with view 2).
  - Returns `(reachable_tables: list[str], truncated: bool)`.

**HONEST SEMANTICS (must appear in the plan, the model docstrings, AND the output
labels):** view 1 is an **UPPER BOUND**. A LEFT/OUTER join on the empty table does
NOT fully empty the downstream table — rows survive with NULLs in the columns drawn
from the empty side. But **join type is NOT recorded on `SELECTS_FROM`** (verified:
the edge is a bare `src_key`/`dst_key` pair, no join-type column), so view 1 cannot
distinguish inner from outer. The field and CLI label MUST read **"may be fully
empty (depends on join type)"**, never "is empty". This caveat is the difference
between an honest tool and one that over-claims.

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

### The two views overlap but neither subsumes the other

- View 1 catches `d` (row-emptied by inner join on `x`) even though no `d` column is
  COLUMN_LINEAGE-derived from `x` → view 2 misses it.
- View 2 catches a column `d.x` whose VALUE derives from a still-row-populated path
  (e.g. a partially-empty table where only some columns lose their source) → that
  table may not be fully row-empty, so view 1's "fully empty" framing is too coarse.
- **Present them side by side. Do not merge.**

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

    # --- View 1: table-level ROW reachability (NEW) ---
    row_empty_tables: list[str]      # downstream tables that READ FROM a source and
                                     # MAY GO FULLY EMPTY (depends on join type — UPPER BOUND)

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
                                     # closest-to-source first (reuses _kahn_topological_sort)
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
- adds `row_empty_tables` as the distinct **row-presence** answer (view 1);
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
- **E3b — row-empty vs value-empty divergence (view 1 vs view 2; the NEW headline).**
  The inner-join case: `d` ∈ `row_empty_tables` but the non-derived column ∉
  `value_empty_columns`. **Required new test (Test T0).**
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
- Helper: load adjacency once, cycle-safe BFS from `start_tables`, `_MAX_CLOSURE_NODES`
  cap → `truncated`, exclude named sources, `_exclude_synthetic_tables`. Returns
  `(reachable_tables, truncated)`.
- Acceptance: given the inner-join fixture (Test T0), returns `d` for source `x`;
  cycle fixture does not loop; synthetic bridge not in output.

**Step 1.3 — Factor the engine `_compute_empty_propagation(db, tables, max_depth)`.**
- Combines view 1 (`_table_row_reachability`) + view 2 (existing closure → rollup →
  noise → exclude → full/partial split) into one `EmptyPropagationResult`. No new
  column-lineage traversal/SQL.
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
  [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md) (verify current count; the
  prior plan referenced "16 → 17" at L87 — re-verify the live number before editing).

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
- Renders a Rich layout with the **two views clearly labelled and separate**: a
  "may be fully empty (row reachability — depends on join type)" table
  (`row_empty_tables`, in `propagation_order`) and a "value-affected columns" table
  (`value_*`, with per-table `value-empty / total columns` and a `full|partial`
  marker). Honours `--raw` and `--max-depth`.
- Acceptance: `uv run sqlcg analyze empty-impact <fixture-table>` prints BOTH views
  with the row-reachability caveat label and the value full/partial marker; `--raw`
  shows noise rows; omitting `--max-depth` runs unbounded (exit 0); `--max-depth 0`
  and `--max-depth 101` each exit non-zero with the documented message.

### Phase 4: Docs + version
**Step 4.1 — Version bump + ARCHITECTURE_REVIEW tool-count + tool docstring.**
- `pyproject.toml` + `src/sqlcg/__init__.py` → `1.22.0`; `uv lock`.
- Update MCP-tool count in ARCHITECTURE_REVIEW.md (verify live number).

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
  - **T0 (NEW headline — row vs value divergence).** Fixture:
    `INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k`, where `d.x`
    is COLUMN_LINEAGE-derived from `other.x` (NOT from `x`). For source `x`, assert:
    `d` ∈ `row_empty_tables` (it reads FROM `x`); `d.x` ∉ `value_empty_columns` (not
    derived from `x`). This is the inner-join blind-spot claim — view 1 catches what
    view 2 misses.
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
- [ ] **Row reachability (view 1):** for the inner-join fixture, `d` ∈
      `row_empty_tables` for source `x`, AND the non-derived column ∉
      `value_empty_columns` (column equality, Test T0).
- [ ] `row_empty_tables` and the CLI label carry the **"may be fully empty (depends on
      join type)"** upper-bound caveat (string assertion).
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
  and the empty-string sentinel must be excluded. Pinned by Test T0 and a query test.
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
attribution, gated by the freshness check.** Concretely:

1. Resolve `base_ref` → `base_sha`; read the graph's `indexed_sha`. The detection is
   defined as **`producers(indexed_sha) − producers(HEAD-after-current-change)`**.
2. The honest, cheap contract for a CI gate: the graph is expected to be indexed AT
   `base_ref` when the gate runs (the gate's job is "I'm about to merge a change — what
   breaks?"). So:
   - Capture `producers_before = SELECT DISTINCT target_table FROM "SqlQuery" WHERE target_table <> ''`
     from the current graph (asserted to be at `base_ref` via `get_indexed_sha()` ==
     `base_sha`; if not, the tool returns a `hint` telling the user to index at
     `base_ref` first — it does NOT silently compare mismatched states).
   - Run the existing `resync_changed(base_sha → HEAD)` (or require the caller to have
     done so) to advance the graph to the post-change state.
   - Capture `producers_after` the same way.
   - `lost_producer_tables = producers_before − producers_after`, minus synthetic
     (`_exclude_synthetic_tables`) and the named-source self-exclusion.
3. **Option C attribution (refinement):** for each lost-producer table, use
   `git_name_status_delta(base_sha, head)` `deleted ∪ modified` to attribute which
   file(s) dropped the producer (parse the OLD file version via `git show` only for the
   small changed set — bounded by the diff, not the corpus; this is the only parse and
   it is scoped, not a hot-path violation).
4. Feed `lost_producer_tables` into PR 1's `_compute_empty_propagation(db, lost_producer_tables, max_depth)`
   and return its two-view result, wrapped with the attribution.

> **Why not just diff the file delta directly?** A `modified` file may still write the
> same table (it changed a WHERE clause); a `deleted` file's table may be written by
> another surviving file. Only the producer-SET diff (B) answers "is the table now
> orphaned?" correctly. Option C alone would over-report (every changed producer file)
> — it is attribution, not detection.

> **Open question O2 (NEW — for plan-reviewer):** does the gate REQUIRE the graph to be
> pre-indexed at `base_ref`, or should `get_pr_impact` orchestrate the index-at-base
> itself? Recommendation: **require pre-indexed at base** (the user's normal `reindex`
> flow leaves the graph at HEAD; a CI gate indexes the base commit, runs the gate, which
> internally resyncs to the PR head). Orchestrating a checkout from inside the tool is a
> larger surface and risks mutating the user's working tree — flag for reviewer decision.

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

**Step 2.2 — `_detect_lost_producers(db, base_sha, head_sha) -> tuple[list[str], dict[str, list[str]]]`.**
- Implements Option B (producer-set diff) + Option C attribution. Verifies
  `get_indexed_sha()` against `base_sha`; sets the mismatch hint path. Excludes
  synthetic via `_exclude_synthetic_tables`.
- Producer query: `SELECT DISTINCT target_table FROM "SqlQuery" WHERE target_table <> ''`
  (NEW named query `GET_PRODUCER_TABLES`).
- Acceptance: on a fixture where a producer file is deleted, the orphaned table is in
  the lost set and a still-produced table is NOT; attribution maps the table to the
  deleted file.

### Phase 2: Orchestration + MCP tool
**Step 2.3 — `get_pr_impact(base_ref, max_depth)` MCP tool.**
- Resolve `base_ref`→`base_sha`; capture `producers_before`; advance/verify state;
  capture `producers_after`; `_detect_lost_producers`; feed into
  `_compute_empty_propagation`; build `PrImpactResult`.
- Acceptance: registered (`@mcp.tool()`); fixture with a dropped producer returns the
  lost table + its PR-1 two-view blast radius; `NotIndexedError` when empty; mismatch
  state returns the documented hint, no crash.
- **Call site:** `@mcp.tool()` registration. Bump the MCP-tool count in
  `ARCHITECTURE_REVIEW.md` again (1.22.0 count + 1).

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
- `pyproject.toml` + `__init__.py` → `1.23.0`; `uv lock`. Document the
  code-regression-only / runtime-blind contract in the tool docstring and ARCHITECTURE_REVIEW.

## PR 2 Test Strategy
- **Unit:** producer-set diff returns `before − after`; synthetic excluded; mismatch
  state (graph not at base) returns hint, no detection; attribution maps lost table → file.
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
- [ ] Every new method has a grep-confirmed call site (`_detect_lost_producers` ← MCP +
      CLI; `get_pr_impact` ← `@mcp.tool()`; CLI command ← Typer app).
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
- **O2 — PR 2 base-ref orchestration — OPEN (for plan-reviewer).** Should `get_pr_impact`
  require the graph pre-indexed at `base_ref` (recommended), or orchestrate the
  index-at-base itself (larger surface, risks mutating the working tree)? Recommendation:
  require pre-indexed at base; flag for reviewer decision before PR 2 implementation.

> **This expanded spec MUST go back through the plan-reviewer gate before
> implementation.** PR 2's edge-disappearance detection and PR 1's new view-1
> reachability query/traversal are new logic the prior (dcda023) review did not cover.
