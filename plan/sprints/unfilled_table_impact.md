# Feature Plan: Data-Loss Impact Analysis (three sequenced PRs) + one standalone correctness bugfix

> **Shepherd-directed addition (2026-06-12): PR 4 added** — a STANDALONE `SELECTS_FROM`-
> completeness CORRECTNESS bugfix for the existing `analyze unused` (HIGH severity,
> actively misleading) and `find_table_usages` (MEDIUM, under-counts) commands. Surfaced
> by the SAME M1 insight that drove the reframe (`SELECTS_FROM` = 4,944 vs
> `COLUMN_LINEAGE` = 53,285 — ~90% of reads are CTE/column-lineage routed). PR 4 is
> INDEPENDENT of PR 1/2/3's engine (existing relations only) but edits `analyze.py` +
> `queries.sql` which PR 1 also touches, so its DEVELOPMENT is sequenced AFTER PR 1
> merges (rebase on master) — a file-overlap sequencing, NOT a code dependency. Version:
> PATCH (target `1.24.1`; `1.21.3` if it merges first). PR 1/2/3 sections are UNTOUCHED.
> NOT re-looped through the plan-reviewer per shepherd instruction; shepherd verifies.
>
> Status: REVIEWED (shepherd-verified 2026-06-12 — M1/M6 blockers confirmed
> resolved by reading the revised §"two views" and §"PR 2 Detection mechanism"
> against the tree; gate passed). **PR 1 is in development.** Ready for implementation,
> PR 1 first. Preserves the
> plan-reviewer-revised W1/W2/N1/N2/N3 PASS content (commit dcda023), scoped into
> **PR 1**. View 1 is reframed (M1) as a documented SUPPLEMENT to View 2 (the
> primary, reliable answer) after the corpus measurement below; O2 is resolved
> (M6) — `get_pr_impact` requires a base-indexed graph and orchestrates the resync
> internally. Owner (compliance): architect-planner.
> Version targets: **PR 1 → 1.22.0**, **PR 2 → 1.23.0**, **PR 3 → 1.24.0** (all
> additive → minor).
>
> **Shepherd-directed amendment (2026-06-12):** (A) **PR 3 added** — retrofit PR 1's
> gating-join supplement (branch (b)) into the existing impact tools
> (`get_change_scope`/`diff_impact`) via a SEPARATE `gating_join_tables` field, reusing
> PR 1's `_table_row_reachability` (no reimplementation); depends on PR 1. (B) **PR 2
> rename-handling folded in** as a CORRECTNESS requirement — `producers(base) −
> producers(head)` mis-reports renames as total data loss; PR 2 now classifies renames
> (file + in-file structural) into a distinct `renamed_tables` category and excludes
> them from the blast radius. PR 1 sections are UNTOUCHED.
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

## Feature Summary (three feature PRs + one standalone bugfix = four PRs)

Answer the operational question "a job failed and left table(s) empty — what
downstream is at risk?" in three stages (PR 1 the engine; PR 2 the change-detection
front-end; PR 3 retrofits PR 1's gating-join supplement into the existing impact tools
so they answer consistently). **PR 4 is a separate, standalone correctness bugfix** (not
part of the feature) that fixes two existing `SELECTS_FROM`-only commands surfaced by the
same M1 insight:

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
  `sqlcg analyze pr-impact --base <ref>`. **Classifies renames into a distinct
  `renamed_tables` category and excludes them from the blast radius** (correctness —
  raw `producers(base) − producers(head)` would cry wolf on every rename).
- **PR 3 — gating-join supplement retrofit (consistency).** Extend the existing impact
  tools `get_change_scope` and `diff_impact` with a SEPARATE `gating_join_tables` field,
  reusing PR 1's `_table_row_reachability` so the existing tools and the new one give
  consistent answers about direct gating-join readers. Ships after PR 1, independent of
  PR 2. No new MCP tool, no new traversal code.
- **PR 4 — `SELECTS_FROM`-completeness bugfix (standalone correctness; NOT the feature).**
  Redefine "is this table read/used" in `analyze unused` + `find_table_usages` to count the
  column-lineage path (D3) and `STAR_SOURCE` (D2), not just direct `SELECTS_FROM` (D1) —
  so a CTE-only-consumed table stops being falsely reported "unused". INDEPENDENT of the
  engine; develops AFTER PR 1 for file-overlap reasons only; PATCH version. Entry points:
  existing `sqlcg analyze unused` CLI + existing MCP `find_table_usages` tool (augmented).

## Shared design (built by PR 1, reused by PR 2 + PR 3 — no duplication)

> **PR 4 shares NO new symbols with PR 1/2/3.** It does not use
> `_compute_empty_propagation`, `_table_row_reachability`, `EmptyPropagationResult`, or
> `GET_TABLE_READS_ADJACENCY`. It reads only the existing `SELECTS_FROM` / `STAR_SOURCE` /
> `COLUMN_LINEAGE` / `SqlColumn` relations. Its ONLY coupling to PR 1 is the shared FILES
> `analyze.py` + `queries.sql` — hence the develop-after-PR-1 sequencing (rebase), not a
> dependency-gate.

PR 2 and PR 3 MUST NOT reimplement any blast-radius / reachability logic. PR 1 builds,
and PR 2 / PR 3 reuse:

| Shared artifact | Built in | Reused by PR 2 / PR 3 |
|---|---|---|
| `EmptyPropagationResult` (two-view model) | PR 1 Step 1.1 | PR 2: returned verbatim, wrapped with attribution |
| `_compute_empty_propagation(db, tables, max_depth)` engine | PR 1 Step 1.3 | PR 2: called directly with the detected lost-producer table set |
| `_table_row_reachability(db, tables, max_depth)` (view 1 traversal) | PR 1 Step 1.2 | PR 2: invoked transitively via the engine. **PR 3: called DIRECTLY in `get_change_scope`/`diff_impact` to populate `gating_join_tables`** |
| `GET_TABLE_READS_ADJACENCY` query | PR 1 Step 1.2 | PR 3: reused transitively via `_table_row_reachability` (no new query) |
| `_affected_columns_closure` / `_rollup_to_tables` / `_exclude_synthetic_tables` / `_kahn_topological_sort` (existing helpers) | already in tree | unchanged |
| `get_backend(read_only=True)` CLI seam (W2/O1) | PR 1 Step 3.1 | same seam reused by PR 2's CLI |

> **Dependency gate:** PR 2 cannot start until PR 1's engine
> (`_compute_empty_propagation`, both views, `EmptyPropagationResult`) is merged.
> PR 3 cannot start until PR 1's `_table_row_reachability` + `GET_TABLE_READS_ADJACENCY`
> are merged. PR 2 and PR 3 are mutually independent.

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

> **Rename handling is a CORRECTNESS requirement, not optional (shepherd-directed,
> 2026-06-12).** Raw `producers(base) − producers(head)` reports a RENAME as a lost
> producer: renaming `da.foo` → `da.bar` drops `da.foo` from the producer set, and the
> detector cries "total data loss + full downstream blast radius" when nothing was lost.
> Unhandled, the detector cries wolf on every rename and is untrustworthy. PR 2 MUST
> classify renames into a distinct `renamed_tables` category and EXCLUDE them from the
> data-loss blast radius. See §PR 2 Rename handling below.

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
- **Rename classification is a HEURISTIC, not an exact rename oracle.** It pairs a lost
  producer with a new producer by BOTH column-set and downstream-consumer Jaccard
  (≥ 0.6 each, §PR 2 Rename handling). A rename that simultaneously rewrites the schema
  AND re-points the consumers below threshold will be (correctly, conservatively)
  treated as deletion+addition rather than a silent rename — the safe failure direction
  (report a blast radius rather than hide a real loss). A genuine deletion that happens
  to coincide with an unrelated new table sharing ≥ 0.6 on BOTH axes could be
  mis-classified as a rename; the AND-of-both-axes threshold makes this rare. The
  `renamed_tables` docstring/CLI MUST label it a heuristic so callers verify.

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
  **Now also returns the rename classification (see §PR 2 Rename handling) — its
  signature and return shape are extended there.**

## PR 2 Rename handling (CORRECTNESS — shepherd-directed, 2026-06-12)

**Problem.** Detection is `lost = producers(base) − producers(head)`. A RENAME is a
**false positive**: if `da.foo` is renamed to `da.bar`, `da.foo` drops out of
`producers(head)` and lands in `lost`, so the detector reports `da.foo` as a "lost
producer" = total data loss + full downstream blast radius — when in fact the table was
renamed and nothing was lost. The detector MUST distinguish a rename from a genuine
deletion, classify renames into a distinct visible category, and keep them OUT of the
blast radius. Genuine deletions (no matching new producer) still flow to the engine.

**The mirror set — "new producers".** Compute, symmetrically to the lost set:
```
new_producers = producers(head) − producers(base)     # tables that GAINED a producer
lost_producers = producers(base) − producers(head)     # the raw candidate lost set
```
A rename of `da.foo` → `da.bar` shows up as `da.foo ∈ lost_producers` AND
`da.bar ∈ new_producers` simultaneously. Rename detection pairs members of `lost` with
members of `new` that are "the same table under a new name".

**Two rename shapes — both MUST be handled:**

1. **FILE rename.** `git_name_status_delta` already splits a git rename into a
   `deleted` of the old `.sql` path + an `added` of the new `.sql` path
   ([`git_delta.py`](../../src/sqlcg/indexer/git_delta.py), "renames split into
   delete+add"). The rename is reconstructable from the delta: a `deleted` old-path
   whose producer table is in `lost`, paired with an `added` new-path whose producer
   table is in `new`. (A file rename may or may not also rename the table it writes —
   the structural overlap check below is the actual classifier; the file-delta pairing
   is supporting attribution evidence.)
2. **IN-FILE table rename.** `CREATE TABLE da.bar` replacing `CREATE TABLE da.foo` in
   the **same modified file** — the file path is unchanged, so git reports it as a plain
   `modified` with **NO rename signal**. This MUST be detected **STRUCTURALLY** from the
   graph, not from git. `da.foo ∈ lost`, `da.bar ∈ new`, and the overlap check (below)
   classifies the pair as a rename.

**Structural rename heuristic (the classifier).** A lost producer `L` that coincides,
in the same change set, with a new producer `N ∈ new_producers` whose **column set AND
downstream-consumer set substantially overlap** `L`'s is almost certainly a rename.

- **Overlap signal — use BOTH, AND-combined, and justify the threshold.** A pure
  column-name overlap is too weak (two unrelated tables can share `id`/`created_at`); a
  pure downstream-consumer overlap is too weak (a brand-new table can be wired into the
  same consumers a deleted one fed). Requiring BOTH cuts the false-rename rate sharply:
  - **column overlap** = Jaccard of `L`'s graph-known column-name set vs `N`'s
    (`GET_COLUMNS_FOR_TABLE_QUERY` for each, captured at the *base* graph for `L` and the
    *head* graph for `N`) `≥ 0.6`.
  - **downstream-consumer overlap** = Jaccard of the table sets that read `L` (at base)
    vs read `N` (at head), via `GET_TABLE_READS_ADJACENCY` reverse lookup, `≥ 0.6`.
  - **Classify the `(L, N)` pair as a rename iff BOTH Jaccard scores ≥ 0.6.** Rationale
    for 0.6: a true rename keeps the same schema and the same consumers, so both scores
    are typically ~1.0; an unrelated coincidence rarely clears 0.6 on BOTH axes at once.
    The threshold is a named constant `_RENAME_OVERLAP_THRESHOLD = 0.6` (single source of
    truth, tunable), pinned by the in-file-rename test. The FILE-rename git pairing, when
    present, is recorded as corroborating attribution but the BOTH-Jaccard structural
    check is the authoritative classifier (so a file rename that also genuinely changes
    the schema/consumers is correctly treated as deletion+addition, not a silent rename).

**Classification outcome:**
- `(L, N)` pair clears BOTH thresholds → `L` is classified **"possible rename"**, placed
  in a distinct `renamed_tables` entry (`old → new`), and **EXCLUDED from
  `lost_producer_tables` and from the blast-radius input set**. (Recommended: distinct
  category so the rename is VISIBLE — "verify consumers updated" — not silently dropped.)
- `L` with **no** matching `N` clearing the thresholds → **genuine lost producer**;
  stays in `lost_producer_tables` and **flows to `_compute_empty_propagation`**.

**Helper extension.** `_diff_lost_producers` is extended to take both snapshots and the
delta, and to return the rename classification:
```python
def _diff_lost_producers(
    before: set[str], after: set[str], db, base_sha, head_sha
) -> tuple[
    list[str],            # genuine lost_producer_tables (renames removed)
    dict[str, list[str]], # attribution: lost_table -> [files that dropped its producer]
    dict[str, str],       # renamed_tables: old_name -> new_name (excluded from blast radius)
]:
    ...
```
- Computes `lost = before − after`, `new = after − before`, both synthetic-excluded.
- For each `L ∈ lost`, search `new` for an `N` clearing BOTH Jaccard thresholds
  (`_RENAME_OVERLAP_THRESHOLD`); first match wins, classify as rename. File-delta pairing
  from `git_name_status_delta(base_sha, head_sha)` corroborates attribution.
- `lost_producer_tables` = `lost` minus all classified-rename `L`s.
- Only `lost_producer_tables` (genuine losses) is fed to the blast-radius engine in
  `get_pr_impact`; `renamed_tables` is reported but produces NO blast radius.

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
    lost_producer_tables: list[str]     # GENUINE lost producers (renames removed) — detection output
    renamed_tables: dict[str, str]      # old_name -> new_name; classified renames (file + in-file
                                        # structural), EXCLUDED from lost_producer_tables and from
                                        # blast_radius. "verify consumers updated" category — a rename
                                        # is NOT data loss. Docstring MUST state this is a heuristic
                                        # (both column-set AND downstream-consumer Jaccard ≥ 0.6).
    attribution: dict[str, list[str]]   # lost_table -> [files that dropped its producer] (Option C)
    blast_radius: EmptyPropagationResult  # PR 1 engine output for lost_producer_tables ONLY
                                          # (renamed_tables contribute NO blast radius)
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
  dict[str, list[str]], dict[str, str]]` — computes `before − after` AND `after − before`
  (the new-producer mirror set), excludes synthetic via `_exclude_synthetic_tables`,
  **classifies renames** (§PR 2 Rename handling: pair each lost `L` with a new `N`
  clearing BOTH column-set and downstream-consumer Jaccard ≥ `_RENAME_OVERLAP_THRESHOLD`),
  removes classified renames from the lost set, and builds Option C attribution from
  `git_name_status_delta(base_sha, head_sha)` `deleted ∪ modified`. Returns
  `(genuine_lost_producer_tables, attribution, renamed_tables)`.
- These two helpers do NOT capture SHAs (the SHA *args* to `_diff_lost_producers` are
  only for the git-delta attribution lookup, not for snapshotting), do NOT assert base
  state, and do NOT resync — that orchestration lives in `get_pr_impact` (Step 2.3).
- Add a named constant `_RENAME_OVERLAP_THRESHOLD = 0.6` (single source of truth).
- Acceptance: `_capture_producer_set` returns the producer set on a hand-built
  fixture; `_diff_lost_producers` on a `before`/`after` pair where a producer was
  dropped returns the orphaned table in the lost set, excludes a still-produced
  table, and maps the orphaned table to the deleted file; on a `before`/`after` pair
  where a producer was RENAMED (old in `before` only, new in `after` only, both column
  and consumer Jaccard ≥ 0.6) returns the old name in `renamed_tables[old]==new` and
  NOT in the genuine lost set; a genuine deletion with NO matching new producer stays in
  the lost set.

### Phase 2: Orchestration + MCP tool
**Step 2.3 — `get_pr_impact(base_ref, max_depth)` MCP tool (owns orchestration).**
- Sequence (O2 RESOLVED — M6): (1) resolve `base_ref`→`base_sha`; (2) **assert**
  `get_indexed_sha() == base_sha` else return mismatch hint (no crash, no compare);
  (3) `producers_before = _capture_producer_set(db)`; (4) **orchestrate**
  `resync_changed(root, base_sha, head_sha, backend, dialect, ...)` to advance the
  graph DB to HEAD (graph-only, NOT a working-tree mutation — same as `sqlcg
  reindex`); (5) `producers_after = _capture_producer_set(db)`; (6)
  `lost, attribution, renamed = _diff_lost_producers(producers_before, producers_after,
  db, base_sha, head_sha)`; (7) feed **`lost` only** (renames excluded) into
  `_compute_empty_propagation`; (8) build `PrImpactResult` with `renamed_tables=renamed`.
- Acceptance: registered (`@mcp.tool()`); fixture with a dropped producer returns the
  lost table + its PR-1 two-view blast radius; a fixture with a RENAMED producer puts
  the table in `renamed_tables` (not `lost_producer_tables`) and produces NO blast
  radius for it; `NotIndexedError` when empty; graph not at `base_ref` returns the
  documented hint with NO resync run, no crash; unresolvable `base_ref` returns a hint.
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
- **Unit — rename classification (the correctness requirement):**
  - **In-file table rename.** Hand-built `before`/`after`: `da.foo ∈ before` only,
    `da.bar ∈ after` only, both sharing the SAME column-name set AND the SAME downstream
    consumers (Jaccard 1.0 ≥ 0.6 on both axes). Assert `_diff_lost_producers` returns
    `renamed_tables["da.foo"] == "da.bar"` and `da.foo ∉` the genuine lost set.
  - **Below-threshold rename → treated as deletion+addition (conservative).** A lost
    `L` whose only candidate `N` clears column-Jaccard but NOT consumer-Jaccard (or vice
    versa) is NOT classified as a rename: `L` stays in the genuine lost set,
    `renamed_tables` does not contain it (pins the AND-of-both-axes rule and the 0.6
    threshold via `_RENAME_OVERLAP_THRESHOLD`).
- **Integration (real DuckDB + a tmp git repo):**
  - **Genuine deletion → blast radius.** Index a base state with a producer file writing
    `d`; delete/break that file; reindex to head; assert `d ∈ lost_producer_tables`, a
    still-produced `e ∉`, `renamed_tables` empty, attribution maps `d`→the deleted file,
    and `blast_radius.row_empty_tables` contains `d`'s downstream. A
    `modified`-but-still-produces fixture asserts the table is NOT reported (no over-report).
  - **FILE rename (no false data loss).** Rename the `.sql` file that writes `da.foo` to
    a new path that writes `da.bar` (same columns + consumers); reindex. `git_name_status_delta`
    splits this into delete(old)+add(new). Assert `da.foo ∈ renamed_tables` with value
    `da.bar`, `da.foo ∉ lost_producer_tables`, and the rename produces NO data-loss blast
    radius (`blast_radius` does NOT include `da.foo`'s old downstream as lost).
  - **IN-FILE table rename (no git rename signal, no false data loss).** In a single
    file, change `CREATE TABLE da.foo ...` to `CREATE TABLE da.bar ...` (same columns +
    consumers); reindex (git reports a plain `modified`, NO rename). Assert it is
    detected STRUCTURALLY: `da.foo ∈ renamed_tables` (= `da.bar`), `da.foo ∉
    lost_producer_tables`, NO blast radius for `da.foo`.
- **E2E:** `sqlcg analyze pr-impact --base <ref>` end-to-end on a fixture repo prints
  the lost producer + blast radius + the runtime-blind caveat; a rename fixture prints
  the renamed table under the "verify consumers updated" category with no blast radius.
- Tests assert **observable output**, not "no exception". Aliased/schema-qualified
  fixtures matching the live corpus.

## PR 2 Acceptance Criteria
- [ ] A deleted/broken producer file (NOT a rename) makes its orphaned table appear in
      `lost_producer_tables`; a table still produced by a surviving query does NOT.
- [ ] **FILE rename** of a producer (old `.sql` → new `.sql`, same columns + consumers)
      puts the table in `renamed_tables` (`old → new`), NOT in `lost_producer_tables`,
      and produces NO data-loss blast radius for it.
- [ ] **IN-FILE table rename** (`CREATE TABLE da.bar` replacing `CREATE TABLE da.foo` in
      the same modified file — no git rename signal) is detected STRUCTURALLY: the table
      is in `renamed_tables`, NOT in `lost_producer_tables`, with NO blast radius.
- [ ] A genuine deletion with NO matching new producer clearing BOTH Jaccard thresholds
      DOES flow to the blast-radius engine (stays in `lost_producer_tables`).
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
      confirm the orphaned table + blast radius are sane; AND rename a real producer
      table, confirm it lands in `renamed_tables` with NO false data-loss blast radius).

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
- **R10 — rename cries wolf (the correctness bug).** Raw `producers(base) −
  producers(head)` reports every rename as total data loss. Mitigated by the structural
  rename classifier (§PR 2 Rename handling): BOTH column-set AND downstream-consumer
  Jaccard ≥ `_RENAME_OVERLAP_THRESHOLD` (0.6) → `renamed_tables`, excluded from blast
  radius. Pinned by the file-rename, in-file-rename, below-threshold, and genuine-deletion
  tests. Failure direction is conservative: an ambiguous case is treated as deletion
  (reports a blast radius) rather than silently hiding a real loss.

## PR 2 Version & Release
- Additive → **minor**: `1.22.0` → **`1.23.0`**. Agent does NOT close issues or tag;
  user verifies live + tags `v1.23.0`.

---

# PR 3 — Retrofit the gating-join supplement into the existing impact tools (ships after PR 1; depends on its engine)

## PR 3 Goal

The three existing impact tools — [`get_change_scope`](../../src/sqlcg/server/tools.py)
(L1028), [`diff_impact`](../../src/sqlcg/server/tools.py) (L1180), and
[`get_backfill_order`](../../src/sqlcg/server/tools.py) (L1115) — all build their
affected-table set by the SAME path: `_affected_columns_closure` over `COLUMN_LINEAGE`
→ `_rollup_to_tables` → noise-filter → `_exclude_synthetic_tables`
([`tools.py` L1067-1078, L1138-1149, L1226-1248](../../src/sqlcg/server/tools.py)).
This is the CTE-robust #38 traversal — it correctly captures CTE-wrapped *derived*
reads. **But it shares ONE blind spot with PR 1's View 2: a downstream table that
READS a source via a gating inner join yet derives NO column from it** is invisible to
the column-lineage closure (the exact case PR 1's View-1 branch (b) catches —
`INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k`, where `d` row-empties
when `x` empties but no `d` column is COLUMN_LINEAGE-derived from `x`).

PR 3 closes that gap consistently across the existing tools by **reusing PR 1's
`_table_row_reachability` + `GET_TABLE_READS_ADJACENCY`** (no reimplementation), so the
existing impact tools and the new `get_empty_propagation` give consistent answers about
gating-join readers.

> **Dependency gate:** PR 3 cannot start until PR 1's `_table_row_reachability`
> helper + `GET_TABLE_READS_ADJACENCY` query merge. PR 3 is independent of PR 2.

## PR 3 Decision — SEPARATE field, NOT broadened `affected_tables` (RECOMMENDED — chosen)

| Option | Verdict |
|--------|---------|
| **(A) ADD branch (b) into the existing `affected_tables`** | **Rejected.** `get_change_scope` and `diff_impact` are SHIPPED public MCP tools. `affected_tables` is documented as "column-precise, rolled up to tables" ([`tools.py` L1033, L1192](../../src/sqlcg/server/tools.py)) and `downstream_count` is a fact pinned to `len(affected_tables)` ([`models.py` L284](../../src/sqlcg/server/models.py)) and feeds the `risk` heuristic thresholds ([`tools.py` L1083-1090](../../src/sqlcg/server/tools.py)). Silently folding gating-join readers in would (i) change the meaning of an existing field for every caller, (ii) inflate `downstream_count` and therefore the risk label, and (iii) mix two semantically different signals (value-derivation vs row-gating, the very distinction PR 1 went to two views to keep separate). Worse, branch (b) is an UPPER BOUND (join-type not recorded) carrying a known gap — folding an upper-bound set into a field documented as precise is dishonest. |
| **(B) Surface branch (b) as a SEPARATE `gating_join_tables` field** | **CHOSEN.** Adds a new, clearly-labelled field alongside the existing `affected_tables`; existing callers see no change to `affected_tables`/`downstream_count`/`risk`; the new signal is opt-in and carries its own honest caveats. Mirrors PR 1's deliberate two-view separation rather than collapsing it. Additive → no contract break. |

**Justification (required):** the separate-field approach is mandated because
`affected_tables` is an existing public contract whose consumers (CI gates, risk
scoring) rely on its current "value-derived, column-precise" meaning. Branch (b) is a
different KIND of answer (row-gating, upper-bound, with a documented CTE-pure-gating
gap). Keeping it in its own field preserves backward behaviour AND keeps the two
signals legible — exactly the reasoning that produced PR 1's `row_empty_tables` vs
`value_affected_tables` split. There is no strong reason to override the recommendation.

## PR 3 Scope

### In Scope
- Add a `gating_join_tables: list[str]` field to `ChangeScopeResult` and (if it shares
  the affected-table rollup — it does) `DiffImpactResult`, populated by reusing PR 1's
  `_table_row_reachability` seeded from the tool's source/changed table set.
- Carry the SAME honest caveats as PR 1 View-1 branch (b) into the new field's
  docstring and CLI labels: the CTE-wrapped pure-gating residual gap AND the join-type
  upper bound.
- `gating_join_tables` excludes the tool's own target/changed tables and any table
  already covered conceptually — but per the decision it is reported as its OWN set
  (NOT subtracted from / merged into `affected_tables`); the relationship
  `gating_join_tables` may overlap `affected_tables` (a table can be both value-derived
  AND a gating-join reader) is documented, not deduplicated away.

### Non-Goals
- **`get_backfill_order` is NOT amended.** Its single job is a topological *rebuild
  order* over the value-derived blast radius; adding row-gating readers to a rebuild
  order conflates two concerns and there is no rebuild-order meaning for a gating-join
  reader that derives no column. State this explicitly so the developer does not
  "consistency-add" the field everywhere. (If a future need arises, file it separately.)
- **No change to `affected_tables`, `downstream_count`, `risk`, `presentation_facing`,
  or `order`** in either tool — those keep their exact current semantics (decision B).
- **No new traversal code.** PR 3 calls PR 1's `_table_row_reachability` verbatim; it
  MUST NOT reimplement the adjacency/BFS. If PR 1 has not merged, PR 3 does not start.
- **No parse-time/schema-CSV path, no join-type modelling** (inherited from PR 1).

## PR 3 Decision — which tools get the field

| Tool | Shares the closure→rollup affected-table path? | Gets `gating_join_tables`? |
|---|---|---|
| `get_change_scope` (single table) | YES ([`tools.py` L1069-1077](../../src/sqlcg/server/tools.py)) | **YES** — seed `_table_row_reachability` from `[target]`. |
| `diff_impact` (changed-file set) | YES ([`tools.py` L1232-1247](../../src/sqlcg/server/tools.py)) | **YES** — seed from the resolved `changed_tables` set (the same seed its column closure uses). |
| `get_backfill_order` | YES, but produces a rebuild *order*, not an impact set | **NO** (Non-Goal above). |

## PR 3 Decision — Output shape (additive fields)

In [`models.py`](../../src/sqlcg/server/models.py), add to BOTH `ChangeScopeResult`
and `DiffImpactResult`:
```python
    gating_join_tables: list[str] = Field(
        default_factory=list,
        description=(
            "Downstream tables that READ a changed/source table via a DIRECT "
            "(non-CTE) gating join (SELECTS_FROM/STAR_SOURCE) but derive NO column "
            "from it — they may go fully empty if the source empties, even though "
            "they are NOT in affected_tables (no value-derivation). Reused from the "
            "PR 1 row-reachability supplement. KNOWN GAP: CTE-wrapped pure-gating "
            "reads are NOT detected. UPPER BOUND: depends on join type (not recorded "
            "on SELECTS_FROM) — may be fully empty only for inner joins."
        ),
    )
```
- `default_factory=list` so the field is backward-additive (older serialised payloads
  and any caller not setting it get `[]`).
- `downstream_count` is NOT changed and remains `len(affected_tables)` — the docstring
  for `gating_join_tables` notes it is reported separately and not counted there.

## PR 3 Implementation Steps (ranked — build first → last)

### Phase 1: Model fields
**Step 3.1 — Add `gating_join_tables` to `ChangeScopeResult` and `DiffImpactResult`.**
- File: [`models.py`](../../src/sqlcg/server/models.py).
- Acceptance: both models importable; field typed `list[str]`, `default_factory=list`;
  docstring carries BOTH caveats (known gap + join-type upper bound) as string content.

### Phase 2: Tool wiring
**Step 3.2 — Populate `gating_join_tables` in `get_change_scope`.**
- File: [`tools.py`](../../src/sqlcg/server/tools.py), inside `get_change_scope` after
  the existing `affected_tables` is built, before constructing `ChangeScopeResult`.
- Call `gating_join_tables, gj_truncated = _table_row_reachability(db, [target], max_depth=None)`
  (PR 1's helper — it already excludes the seed sources and synthetic nodes). OR the
  reachable set minus `affected_tables` is NOT computed — report the full reachable set
  per the decision (overlap with `affected_tables` is allowed and documented).
- Fold `gj_truncated` into the existing `truncated` flag (`truncated = truncated or gj_truncated`).
- Pass `gating_join_tables=gating_join_tables` to `ChangeScopeResult`.
- Acceptance: on the PR-1 gating-join fixture, `get_change_scope("x")`-style call
  returns `d` in `gating_join_tables`, `affected_tables` is UNCHANGED from pre-PR-3
  behaviour (no `d` added there because `d` derives no column from the source), and
  `downstream_count == len(affected_tables)` (unchanged).

**Step 3.3 — Populate `gating_join_tables` in `diff_impact`.**
- File: [`tools.py`](../../src/sqlcg/server/tools.py), inside `diff_impact` after
  `affected_tables`/`order` are built.
- Seed `_table_row_reachability(db, changed_tables, max_depth=None)` from the resolved
  `changed_tables` set (same seed the column closure uses); fold `truncated`.
- Pass `gating_join_tables=` to `DiffImpactResult`. `order`/`presentation_facing`/
  `external_consumers`/`downstream_count` are UNCHANGED.
- Acceptance: on a multi-file fixture where one changed file's table is read by a
  downstream table via a gating join with no column derived, that downstream appears in
  `gating_join_tables` but not in `affected_tables`.

### Phase 3: CLI labels (if these tools have a CLI surface)
**Step 3.4 — Surface `gating_join_tables` honestly wherever these results render.**
- Verify the CLI/render path for `get_change_scope`/`diff_impact` (the `analyze` Typer
  app and/or MCP-only). For each render site, add a labelled section for
  `gating_join_tables` carrying BOTH caveats: **"direct gating-join reads only;
  CTE-wrapped pure-gating reads NOT detected (known gap); may be fully empty depends on
  join type."** If a tool is MCP-only (no CLI), the caveats live in the field docstring
  (Step 3.1) and that is sufficient — record which tools have a CLI surface.
- Acceptance: any CLI that prints these results shows the new section with both caveat
  strings; the existing affected-tables section is unchanged.

### Phase 4: Docs + version
**Step 3.5 — Version `1.24.0` + ARCHITECTURE_REVIEW + docstrings.**
- `pyproject.toml` + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) → `1.24.0`;
  `uv lock`. (Additive fields on existing tools.)
- **Tool-count:** PR 3 adds NO new MCP tool (it extends two existing tools), so the
  `ARCHITECTURE_REVIEW.md` **L87** MCP-tool count is NOT incremented by PR 3 (PR 1 → 17,
  PR 2 → 18; PR 3 stays at 18). If a field-count or capability table is tracked
  elsewhere, note the two new fields there instead — developer verifies whether such a
  count exists; if it does, update it, otherwise no count change.
- Update the `get_change_scope` and `diff_impact` tool docstrings to mention
  `gating_join_tables` and its caveats.

## PR 3 Test Strategy
Tests assert **observable output** (specific table ids + caveat strings), never just
"no exception". Name `test_<unit>_<scenario>_<expected>`, link this plan in the docstring.

- **Integration (real in-memory DuckDB, aliased/schema-qualified fixtures):**
  - **Gating-join reader appears in the new field, NOT in the column closure.** Reuse
    PR 1's branch-(b) fixture shape
    (`INSERT INTO d SELECT other.x FROM other JOIN x v ON other.k = v.k`). For
    `get_change_scope("x")`: assert `d ∈ gating_join_tables`, `d ∉ affected_tables`,
    and `affected_tables`/`downstream_count` equal the pre-PR-3 values for the same
    fixture (regression-pin that `affected_tables` did NOT broaden). This is the
    headline criterion the shepherd named.
  - **`diff_impact` equivalent.** A changed file writes the joined-into source; a
    downstream gating-join reader is in `gating_join_tables`, not `affected_tables`.
  - **Documented gap carried over.** The CTE-wrapped pure-gating fixture (PR 1 Test
    T0b shape) yields the reader in NEITHER `gating_join_tables` NOR `affected_tables`;
    test docstring states the accepted gap.
  - **No regression on a value-derived case.** A purely value-derived downstream (no
    gating join) appears in `affected_tables` exactly as before; `gating_join_tables`
    may be empty.
- **Unit:** the `gating_join_tables` field docstring string-asserts contain both the
  "CTE-wrapped pure-gating reads are NOT detected" and "depends on join type" caveats.
- **E2E (only if these tools have a CLI surface):** the render shows the new labelled
  section with both caveats.

## PR 3 Acceptance Criteria
- [ ] `get_change_scope` returns the direct gating-join reader in `gating_join_tables`
      for a fixture where that reader derives NO column from the target (table-id
      equality), AND `affected_tables` + `downstream_count` are UNCHANGED from pre-PR-3
      behaviour on that fixture (regression-pin).
- [ ] `diff_impact` returns the gating-join reader in `gating_join_tables`, not in
      `affected_tables`, for a changed-file fixture.
- [ ] The CTE-wrapped pure-gating reader appears in NEITHER field (documented gap pinned).
- [ ] `gating_join_tables` docstring carries BOTH caveats (known gap + join-type upper
      bound) — string assertions.
- [ ] `get_backfill_order` is UNCHANGED (no `gating_join_tables` field, Non-Goal).
- [ ] PR 3 reuses PR 1's `_table_row_reachability` (grep-confirms the call sites in
      `get_change_scope` and `diff_impact`; NO new adjacency/BFS function added).
- [ ] Version `1.24.0` in `pyproject.toml`, `__init__.py`, `uv.lock`; MCP-tool count in
      `ARCHITECTURE_REVIEW.md` UNCHANGED at 18 (no new tool).
- [ ] **Live-DWH re-acceptance** before tag: run `get_change_scope` on a real DWH table
      with a known gating-join downstream; confirm it surfaces in `gating_join_tables`
      and that `affected_tables` matches the prior shipped value for that table.

## PR 3 Risks and Mitigations
- **R10 — silently broadening a shipped contract.** Mitigated by decision B (separate
  field) + the regression-pin asserting `affected_tables`/`downstream_count` unchanged.
- **R11 — reimplementing PR 1's traversal.** Mitigated by the grep-confirmed reuse
  criterion + the dependency gate (PR 3 cannot start before PR 1 merges).
- **R12 — over-promising the new field.** The upper-bound + CTE-pure-gating-gap caveats
  ride in the docstring and any CLI label, identical to PR 1 View 1.
- **R13 — perf.** PR 3 adds one `_table_row_reachability` call (one table-sized
  adjacency query + BFS) per tool invocation — the same cost class PR 1 already
  measured as a pure read path; no parser/indexer/CLAUDE.md invariant impact.

## PR 3 Version & Release
- Additive → **minor**: `1.23.0` → **`1.24.0`**. Agent does NOT close issues or tag;
  user verifies live + tags `v1.24.0`.

---

# PR 4 — SELECTS_FROM-completeness fix for the existing dependency-insight commands (CORRECTNESS bugfix; standalone; develop after PR 1 lands)

## PR 4 Goal

Two SHIPPED dependency-insight commands answer "is this table read/used?" using
`SELECTS_FROM` **alone**, and are therefore WRONG on this corpus. Per the M1 corpus
measurement (plan header): on the DWH `SELECTS_FROM = 4,944` edges vs
`COLUMN_LINEAGE = 53,285` — **~90% of cross-table reads are CTE/column-lineage routed
and emit NO top-level `SELECTS_FROM` edge** (the parser's `_real_tables` excludes
CTE-sourced names — [`base.py` L604](../../src/sqlcg/parsers/base.py), out of scope,
M1). A table read ONLY inside CTEs has no `SELECTS_FROM` edge, so:

1. **`analyze unused` (HIGH severity — actively misleading / DANGEROUS).** A
   CTE-only-consumed table is falsely reported "unused" even when heavily read. A user
   pruning "dead" tables on this signal could DROP a live, CTE-fed table.
2. **`find_table_usages` (MEDIUM — under-counts).** Misses CTE-wrapped usages and its
   empty-result hint MISATTRIBUTES the cause ("not referenced … or consumed externally")
   when the real reason is CTE/column-lineage routing.

> **This is a correctness bugfix, NOT part of the empty-impact feature.** It is
> INDEPENDENT of PR 1/2/3's engine (it needs only the existing `COLUMN_LINEAGE`,
> `SELECTS_FROM`, `STAR_SOURCE` relations — no `_compute_empty_propagation`, no
> `_table_row_reachability`, no `EmptyPropagationResult`). **But it edits
> [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py) and
> [`queries.sql`](../../src/sqlcg/core/queries.sql), which PR 1 also edits**, so to avoid
> merge conflicts its DEVELOPMENT must be sequenced **AFTER PR 1 merges** — branch off
> master and rebase on PR 1's landed `analyze.py`/`queries.sql`. It is NOT a dependency
> of PR 1 and shares NONE of PR 1's new symbols.

## PR 4 The bug (verified against the tree)

- **`analyze unused` CLI** ([`analyze.py` L283-300](../../src/sqlcg/cli/commands/analyze.py)):
  `SELECT DISTINCT qualified FROM "SqlTable" WHERE qualified NOT IN (SELECT DISTINCT
  dst_key FROM "SELECTS_FROM") LIMIT 100`. Verified `SELECTS_FROM`-only.
- **`ANALYZE_UNUSED_TABLES` query** ([`queries.sql` L262-267](../../src/sqlcg/core/queries.sql)):
  `SELECT t.qualified FROM "SqlTable" t WHERE t.qualified NOT IN (SELECT DISTINCT dst_key
  FROM "SELECTS_FROM") ORDER BY t.qualified`. Same shape; same blind spot. (Note: the CLI
  command inlines its own SQL and does NOT currently call `ANALYZE_UNUSED_TABLES`; both
  the inlined CLI SQL AND the named query are fixed so neither carries the bug.)
- **`find_table_usages` tool** ([`tools.py` L915-959](../../src/sqlcg/server/tools.py))
  runs `FIND_TABLE_USAGES` ([`queries.sql` L51-59](../../src/sqlcg/core/queries.sql)):
  `JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified` — `SELECTS_FROM`-only. Misleading
  empty hint at [`tools.py` L953-956](../../src/sqlcg/server/tools.py).

## PR 4 The fix to spec — redefine "is this table read/used"

A table is **USED** if ANY of the following holds:
- (D1) it has an incoming `SELECTS_FROM` edge (`dst_key = t.qualified`) — direct read;
- (D2) it has an incoming `STAR_SOURCE` edge (`dst_key = t.qualified`) — `SELECT *` read;
- (D3) any of its columns is the SOURCE of a `COLUMN_LINEAGE` edge — i.e. a VALUE flows
  out of the table (captures CTE-wrapped *derived* reads, the dominant corpus case).

**The column-lineage rollup (D3) — exact relations/keys VERIFIED against the tree.**
A `COLUMN_LINEAGE` edge's `src_key` is a `SqlColumn.id`
([`queries.sql` L46](../../src/sqlcg/core/queries.sql) `JOIN "SqlColumn" src ON
src.id = cl.src_key`). `SqlColumn` carries a denormalized `table_qualified` column
([`duckdb_backend.py` L77](../../src/sqlcg/core/duckdb_backend.py); selected in the
COLUMN node projection [L243](../../src/sqlcg/core/duckdb_backend.py)). So the
column→table rollup is `COLUMN_LINEAGE.src_key → SqlColumn.id → SqlColumn.table_qualified`
— this is the SAME rollup mechanism the existing `_rollup_to_tables` / lineage queries
use (denormalized `table_qualified`, no `HAS_COLUMN` join needed). The set of
"tables a value flows OUT of" is therefore:
```sql
-- the column-lineage source-table rollup (the D3 set)
SELECT DISTINCT src.table_qualified AS table_qualified
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
WHERE src.table_qualified IS NOT NULL AND src.table_qualified <> ''
```
> The original task framing mentioned a `HAS_COLUMN` join; the tree's actual,
> already-used mechanism is the denormalized `SqlColumn.table_qualified` column — the
> developer MUST use `table_qualified` (matching `FIND_COLUMN_LINEAGE` / the COLUMN node
> projection), NOT introduce a `HAS_COLUMN` join, and MUST verify the exact relation/key
> names (`COLUMN_LINEAGE.src_key`, `SqlColumn.id`, `SqlColumn.table_qualified`,
> `STAR_SOURCE.dst_key`) against the backend before finalising the SQL.

The **read-set** (every table read by any signal) is the UNION:
```sql
-- READ_TABLES (the "used" set) — UNION of the three signals
SELECT DISTINCT dst_key AS table_qualified FROM "SELECTS_FROM"
UNION
SELECT DISTINCT dst_key AS table_qualified FROM "STAR_SOURCE"
UNION
SELECT DISTINCT src.table_qualified AS table_qualified
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
WHERE src.table_qualified IS NOT NULL AND src.table_qualified <> ''
```

### Apply to BOTH commands

- **`unused` (CLI inlined SQL) + `ANALYZE_UNUSED_TABLES` (named query):** redefine
  "unused" as `SqlTable.qualified NOT IN (READ_TABLES)`, i.e. `NOT IN` the UNION above
  rather than `NOT IN (SELECT dst_key FROM "SELECTS_FROM")`. Rewrite BOTH the inlined CLI
  SQL and `ANALYZE_UNUSED_TABLES` (keep them in sync; both are fixed even though the CLI
  does not currently call the named one — leaving the named query buggy would re-introduce
  the trap for any future caller).
- **`find_table_usages` (augment):** in addition to the `SELECTS_FROM` rows from
  `FIND_TABLE_USAGES`, ADD usages discovered via the column-lineage path (a query whose
  output column is `COLUMN_LINEAGE`-derived from a column of the target table), and LABEL
  their kind so the caller distinguishes **read-via-lineage** from **direct read**. Do NOT
  silently merge them into the existing rows — the caller must be able to see which usages
  are direct `SELECTS_FROM`/`STAR_SOURCE` and which are CTE/column-lineage routed.
  - Mechanism: a new `FIND_TABLE_USAGES_VIA_LINEAGE` query joining
    `COLUMN_LINEAGE.src_key → SqlColumn (src.table_qualified = target)` →
    `SqlQuery q ON q.id = cl.query_id` → `QUERY_DEFINED_IN` → `File`, returning the same
    `file`/`sql`/`kind` shape so the existing `TableUsage` row builder is reused. Developer
    verifies `COLUMN_LINEAGE.query_id` is populated (it is the LEFT-joined `q.id` in
    `FIND_COLUMN_LINEAGE`, [`queries.sql` L47](../../src/sqlcg/core/queries.sql)) — if a
    lineage row has a null `query_id`, fall back to surfacing the source-column attribution
    so the usage is still reported, not dropped.
  - **Label the distinction.** Add a `usage_kind: Literal["direct", "via_lineage"]` field
    to [`TableUsage`](../../src/sqlcg/server/models.py) (L100), defaulting to `"direct"`
    (additive, backward-safe) so direct `SELECTS_FROM` usages keep their meaning and the
    new lineage-routed rows are explicitly `"via_lineage"`. Do NOT overload the existing
    `kind` field (that is the SQL statement kind, SELECT/INSERT/…).

### Document the SAME residual gap as PR 1

A **CTE-wrapped PURE-gating read** (reads X inside a CTE, derives NO column from it —
`INSERT INTO d WITH cte AS (SELECT 1 AS k FROM x) SELECT other.y FROM other JOIN cte ON
…`) emits NEITHER a top-level `SELECTS_FROM` edge to `x` (parser `_real_tables`, M1) NOR a
`COLUMN_LINEAGE` edge sourced at `x` (no value derives from it). It is invisible to ALL
THREE signals. Therefore:
- The `unused` command help/output MUST read honestly: **"unused = no read of any kind
  detected (direct SELECTS_FROM/STAR_SOURCE or column-lineage value flow); a table read
  ONLY inside a CTE without deriving any column — a pure-gating read — is NOT detected
  (known gap)."**
- `find_table_usages`' empty hint MUST be FIXED to stop misattributing: replace the
  current "not referenced … or consumed externally" line with one that states the real
  causes in order — direct reads, column-lineage-routed reads, the CTE-pure-gating known
  gap, then external consumption — so a user does not conclude "unused" when the true cause
  is CTE routing.

## PR 4 Scope

### In Scope
- Rewrite the `unused` CLI inlined SQL ([`analyze.py` L289-293](../../src/sqlcg/cli/commands/analyze.py))
  and `ANALYZE_UNUSED_TABLES` ([`queries.sql` L262-267](../../src/sqlcg/core/queries.sql))
  to the `NOT IN (READ_TABLES UNION)` definition.
- Add `FIND_TABLE_USAGES_VIA_LINEAGE` query + augment `find_table_usages`
  ([`tools.py` L915-959](../../src/sqlcg/server/tools.py)) to include via-lineage usages,
  labelled.
- Add `usage_kind` to `TableUsage` ([`models.py` L100](../../src/sqlcg/server/models.py)).
- Fix the `find_table_usages` empty hint and the `unused` command help/output caveat.

### Non-Goals
- **No parser change.** `_real_tables` / the CTE-pure-gating gap is OUT OF SCOPE
  (CLAUDE.md performance invariant + M1). The gap is documented, not closed.
- **No new MCP tool** (augments the existing `find_table_usages`; no tool-count change).
- **No reuse of / dependency on PR 1/2/3's engine.** Pure read-side SQL over existing
  relations.
- **No join-type / NULL-rate modelling.** Binary read / not-read.

## PR 4 Implementation Steps (ranked — build first → last)

### Phase 1: Query layer
**Step 4.1 — Rewrite the "unused" definition (both copies).**
- Files: `ANALYZE_UNUSED_TABLES` in [`queries.sql`](../../src/sqlcg/core/queries.sql);
  inlined SQL in `unused` in [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py).
- Replace `NOT IN (SELECT DISTINCT dst_key FROM "SELECTS_FROM")` with `NOT IN
  (<READ_TABLES UNION>)` (the three-signal union above). Keep both copies identical in
  intent; developer verifies the exact `STAR_SOURCE.dst_key`, `COLUMN_LINEAGE.src_key`,
  `SqlColumn.id`/`table_qualified` names against the backend.
- Acceptance: on a hand-built/integration graph, a table read ONLY via a CTE-wrapped
  *derived* SELECT (D3) is NO LONGER returned by `unused`; a genuinely unreferenced table
  still is; a direct-`SELECTS_FROM` table still is not.

**Step 4.2 — Add `FIND_TABLE_USAGES_VIA_LINEAGE` query.**
- File: [`queries.sql`](../../src/sqlcg/core/queries.sql) (named block) + constant in
  [`queries.py`](../../src/sqlcg/core/queries.py).
- Returns `file`/`sql`/`kind` for queries whose output is column-lineage-derived from the
  target table's columns (join chain in §The fix). Verifies `COLUMN_LINEAGE.query_id`
  availability with the documented fallback.
- Acceptance: on the T0c fixture (below) returns the CTE-routed consuming query for the
  target; on a direct-only fixture returns nothing extra (the direct usage already comes
  from `FIND_TABLE_USAGES`).

### Phase 2: Model + tool
**Step 4.3 — Add `usage_kind` to `TableUsage`; augment `find_table_usages`.**
- File: [`models.py`](../../src/sqlcg/server/models.py) — `usage_kind:
  Literal["direct", "via_lineage"] = "direct"` (additive default).
- File: [`tools.py`](../../src/sqlcg/server/tools.py) `find_table_usages` — after the
  existing `FIND_TABLE_USAGES` rows (labelled `usage_kind="direct"`), run
  `FIND_TABLE_USAGES_VIA_LINEAGE` and append its rows labelled `usage_kind="via_lineage"`;
  dedup on `(query_file, sql)` so a query that is BOTH a direct and a lineage source is
  reported once (prefer `"direct"`). Fix the empty hint (§residual gap).
- Acceptance: registered tool returns both direct and via_lineage usages with correct
  labels; CTE-only-consumed target now returns ≥1 `via_lineage` usage (was empty before);
  the empty hint, when truly empty, lists the CTE-pure-gating known gap.

### Phase 3: CLI caveat
**Step 4.4 — `unused` help/output honest caveat.**
- File: [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py) — update the command
  docstring/help and printed output to carry the "no read of any kind detected … CTE
  pure-gating NOT detected (known gap)" caveat.
- Acceptance: `uv run sqlcg analyze unused` output/help contains the caveat string.

### Phase 4: Docs + version
**Step 4.5 — Version bump (PATCH) + changelog note.**
- **SemVer: bug fix → PATCH** (CLAUDE.md). Pick a NON-COLLIDING version by the rule:
  **a patch on top of the latest FEATURE version that has merged at PR 4's merge time.**
  Assuming PR 4 merges after PR 1–3 (1.22.0 / 1.23.0 / 1.24.0), the target is
  **`1.24.1`**. State the RULE rather than hard-pinning, because merge order may shift —
  if PR 4 somehow merges BEFORE the feature PRs, it would be **`1.21.3`** (patch on the
  current master `1.21.2`); if it merges after only PR 1, it would be `1.22.1`; etc.
  Bump `pyproject.toml` + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py); `uv lock`.
- **No MCP tool-count change** (augments an existing tool). `ARCHITECTURE_REVIEW.md` L87
  is NOT touched by PR 4.

## PR 4 Test Strategy

Tests assert **observable output** (specific table ids / usage rows / labels), never just
"no exception". Name `test_<unit>_<scenario>_<expected>`, link this plan in the docstring.
Fixtures use aliased / schema-qualified statement shapes matching the live corpus.

- **Integration (real in-memory DuckDB, `tests/integration/`):**
  - **T0c — CTE-wrapped derived read is COUNTED (the headline fix).** Fixture: a table
    `x` read ONLY via a CTE-wrapped derived SELECT that lands a `COLUMN_LINEAGE` edge
    sourced at `x` (e.g. `INSERT INTO d WITH cte AS (SELECT x.a FROM x) SELECT cte.a FROM
    cte` — `d.a` derives from `x.a`, `x` has NO top-level `SELECTS_FROM` edge). Assert: `x`
    is NOT returned by the `unused` query; `find_table_usages("x")` returns ≥1 usage with
    `usage_kind == "via_lineage"`. This proves the column-lineage path is counted.
  - **Direct-read table still works.** A table read via a plain top-level `SELECTS_FROM`
    is NOT in `unused`, and `find_table_usages` returns it with `usage_kind == "direct"`.
  - **Genuinely-unreferenced table still appears in `unused`.** A table with NO
    `SELECTS_FROM`, NO `STAR_SOURCE`, NO outgoing-value `COLUMN_LINEAGE` is still returned
    by `unused` (the bugfix does not hide real dead tables).
  - **Star-source read counted.** A `SELECT *` from `x` (STAR_SOURCE) keeps `x` out of
    `unused` (D2).
  - **T0d — CTE-wrapped PURE-gating read is the documented gap.** Fixture mirrors PR 1
    Test T0b (`INSERT INTO d WITH cte AS (SELECT 1 AS k FROM x) SELECT other.y FROM other
    JOIN cte ON …`): `x` is read only inside a CTE and derives no column. Assert `x` IS
    STILL returned by `unused` (invisible to all three signals) AND `find_table_usages("x")`
    is empty — and assert the empty hint contains the CTE-pure-gating known-gap string. The
    test docstring states this is an accepted known gap (parser out of scope, M1).
- **Unit:**
  - The `unused` command help/output string contains the known-gap caveat.
  - `TableUsage(usage_kind=...)` defaults to `"direct"` and round-trips `"via_lineage"`.
- **E2E (`tests/e2e/`):** `uv run sqlcg analyze unused` on a small indexed fixture repo
  where a table is CTE-only-consumed does NOT list that table, and prints the caveat.

## PR 4 Acceptance Criteria
- [ ] A table read ONLY via a CTE-wrapped *derived* SELECT (column-lineage path, D3) does
      NOT appear in `analyze unused` / `ANALYZE_UNUSED_TABLES` (table-id assertion, T0c).
- [ ] The SAME table appears in `find_table_usages` with `usage_kind == "via_lineage"`
      (proving the column-lineage path is now counted; was empty pre-fix).
- [ ] A direct-`SELECTS_FROM` table still does NOT appear in `unused` and is returned by
      `find_table_usages` with `usage_kind == "direct"`.
- [ ] A `STAR_SOURCE` (`SELECT *`) read keeps its source out of `unused` (D2).
- [ ] A genuinely-unreferenced table (no read of any kind) STILL appears in `unused`.
- [ ] A CTE-wrapped PURE-gating read (T0d) STILL appears in `unused` and yields no
      `find_table_usages` rows — and the empty hint + `unused` caveat document this residual
      gap (string assertions).
- [ ] `unused` help/output and the `find_table_usages` empty hint carry the honest
      "CTE pure-gating NOT detected (known gap)" wording; the hint no longer misattributes
      CTE routing as "not referenced / external".
- [ ] Both the inlined `unused` CLI SQL AND `ANALYZE_UNUSED_TABLES` are rewritten (kept in
      sync); neither carries the `SELECTS_FROM`-only definition.
- [ ] `usage_kind` added to `TableUsage`, defaulting to `"direct"` (additive, no break).
- [ ] Version bumped per the PATCH rule (target `1.24.1`; `1.21.3` if it merges first) in
      `pyproject.toml`, `__init__.py`, `uv.lock`; ARCHITECTURE_REVIEW.md tool-count UNCHANGED.
- [ ] Every new query/field has a grep-confirmed call site
      (`FIND_TABLE_USAGES_VIA_LINEAGE` ← `find_table_usages`; `usage_kind` ← the
      `TableUsage` row builder; rewritten `unused` SQL ← the CLI command).
- [ ] **Live-DWH re-acceptance (the HEADLINE acceptance — directly verifies the bug is
      fixed on the real corpus):** run `sqlcg analyze unused` on the DWH and confirm
      known-hot DA tables (heavily CTE-fed) NO LONGER falsely appear as unused; spot-check a
      previously-"unused" table with `find_table_usages` and confirm `via_lineage` usages
      now surface. Record before/after table lists in the postmortem.

## PR 4 Risks and Mitigations
- **R14 — wrong column→table rollup relation.** The rollup MUST use the denormalized
  `SqlColumn.table_qualified` (as `FIND_COLUMN_LINEAGE` does), NOT a `HAS_COLUMN` join.
  Pinned by T0c (the CTE-derived table must drop out of `unused`).
- **R15 — `COLUMN_LINEAGE.query_id` null on some rows** would lose a via-lineage usage.
  Mitigated by the documented source-column-attribution fallback so the usage is still
  surfaced, and by T0c asserting a non-empty `via_lineage` result.
- **R16 — over-correcting (a real dead table hidden).** Mitigated by the
  genuinely-unreferenced-table test (still appears) and the T0d gap test (pure-gating does
  NOT silently make a table "used").
- **R17 — merge conflict with PR 1** on `analyze.py`/`queries.sql`. Mitigated by sequencing
  development AFTER PR 1 merges + rebasing on master (not a code dependency, a file-overlap
  one).
- **R18 — live correctness (lineage-class).** CI green is necessary but not sufficient;
  the DWH re-acceptance (known-hot DA tables no longer "unused") is the authoritative gate.

## PR 4 Version & Release
- Bug fix → **PATCH**, by the rule above (target `1.24.1`; `1.21.3` if it merges before the
  feature PRs). Agent does NOT close issues or tag; user verifies live + tags the chosen
  patch version.

---

## Cross-cutting build order + gating

- **Within each PR:** model → helper/engine → MCP tool → CLI → docs/version.
- **Across PRs:** PR 2 cannot start until PR 1's engine (`_compute_empty_propagation`,
  both views, `EmptyPropagationResult`) merges. **PR 3 cannot start until PR 1's
  `_table_row_reachability` + `GET_TABLE_READS_ADJACENCY` merge** (PR 3 reuses them
  verbatim). PR 2 and PR 3 are independent of each other and may proceed in either
  order once PR 1 lands.
- **PR 4** is a STANDALONE bugfix, NOT engine-dependent. It must DEVELOP after PR 1 merges
  to avoid `analyze.py`/`queries.sql` conflicts (rebase on master), but it is independent
  of PR 2 and PR 3 and may land in any order relative to them once PR 1 is in. No
  tool-count change; PATCH version.
- All PRs: grep-confirmed call sites; observable-output tests (not "no exception");
  ARCHITECTURE_REVIEW.md tool-count updates (PR 1/2 only; PR 3/4 none); live-DWH
  re-acceptance before tag.

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

> **The PR 1 + PR 2 core spec has been through the plan-reviewer gate; the two BLOCKERS
> (M1 view-1 reframe, M6 / O2 orchestration) and the M4/Kahn/tool-count warnings are
> resolved per the shepherd's decisions (2026-06-12).** The PR 3 section and the PR 2
> rename-handling requirement were FOLDED IN by shepherd direction (2026-06-12) AFTER
> the reviewer gate and are NOT re-looped through the reviewer per the shepherd's
> instruction; the shepherd verifies them directly. Ready for shepherd verification —
> do NOT re-loop the reviewer.
