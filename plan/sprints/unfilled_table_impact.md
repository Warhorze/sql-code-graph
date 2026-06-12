# Feature Plan: Unfilled-Table Impact (empty-propagation report)

> Status: DRAFT — pending plan-reviewer gate.
> Owner (compliance): architect-planner.
> Version target: **1.22.0** (additive feature → minor; see §Version & Release).

## Summary

Given one or more tables the user *knows* are empty (e.g. an ETL run left them
unpopulated), report which downstream **columns** will receive no/empty data and
which downstream **tables** therefore cannot be fully populated — ordered by
dependency depth so the user sees the propagation front. The unfilled table(s)
are named explicitly by the user; there is no auto-detection.

## Scope

### In Scope
- A single new MCP tool `get_empty_propagation(tables: list[str])` that, given one
  or more explicitly-named source tables, returns the column-precise set of
  downstream columns that go empty and the tables that become unpopulatable,
  ordered by dependency depth.
- A thin CLI surface `sqlcg analyze empty-impact <table> [<table> ...]` that
  presents the same result for human use.
- **Multiple tables IS in scope** (union of the unfilled set), because a stalled
  ETL run typically leaves *several* targets empty at once. A single table is the
  degenerate case of the same path.
- Reuse of the existing traversal engine (`_affected_columns_closure`) and table
  rollup / synthetic-exclusion / topological-order helpers. No new traversal code.

### Non-Goals
- **No auto-detection.** No row-count export, no `last_loaded`/`filled`/`row_count`
  signal, no graph-schema extension. The trigger is a table name list only.
  (Explicit user decision — do not relitigate.)
- **No parse-time or schema-CSV path.** This is a pure read-side graph query; it
  does not touch the parser, the qualify loop, `exp.expand()`, or `qualify()`
  (CLAUDE.md performance invariants and the 2026-05 prohibition are untouched).
- **No partial-fill / NULL-rate modelling.** We report "this column is transitively
  derived from an unfilled source, therefore receives no data." We do NOT model
  COALESCE/IFNULL fallbacks, default values, or "this column is 30% null." Binary:
  derived-from-unfilled → empty.
- **No new ordering algorithm.** Depth ordering reuses the already-shipped
  `_kahn_topological_sort` (producer-before-consumer = closer-to-source-first).

## Why this is mostly already built (verified)

The forward traversal is **done end-to-end**; the genuine gap is framing + entry
point + multi-source union. Verified against the current tree:

- [`_affected_columns_closure`](../../src/sqlcg/server/tools.py) (tools.py ~L333)
  — forward BFS over `COLUMN_LINEAGE` from a set of starting column ids; returns
  `(affected_col_ids, depth_reached, truncated)`; 50k-node cap (`_MAX_CLOSURE_NODES`,
  L330); cycle-safe (`visited` set); optional `max_depth`. **The core engine.**
- [`get_change_scope`](../../src/sqlcg/server/tools.py) (tools.py ~L1024) — already
  does: get columns of target (`GET_COLUMNS_FOR_TABLE_QUERY`, L1065) → closure →
  `_rollup_to_tables` (L378) → noise-filter → `_exclude_synthetic_tables` (L402,
  drops `cte`/`derived`/`temp` per `_SYNTHETIC_TABLE_KINDS` L399) → filter
  `affected_columns` to kept tables (L1078). This is **exactly the empty-propagation
  set** for a single source.
- [`get_backfill_order`](../../src/sqlcg/server/tools.py) (tools.py ~L1111) +
  [`_kahn_topological_sort`](../../src/sqlcg/server/tools.py) (L465) — produce
  producer-before-consumer ordering over the same closure. This IS the
  "what fills first / propagation order" the user wants.
- `COLUMN_LINEAGE` edges are written end-to-end ([`indexer.py`](../../src/sqlcg/indexer/indexer.py) ~L1442);
  column ids are `{table_qualified}.{col_name}` (confirmed by `_rollup_to_tables`
  splitting on the last `.` and by `_split_column_ref` tools.py ~L323).
- [`ChangeScopeResult`](../../src/sqlcg/server/models.py) (models.py ~L264) and
  [`BackfillOrderResult`](../../src/sqlcg/server/models.py) (~L297) already carry
  `affected_columns`, `affected_tables`/`backfill_order`, `noise_excluded`,
  `truncated`.
- queries: `GET_DOWNSTREAM_DEPENDENCIES` (queries.sql L61), `GET_COLUMNS_FOR_TABLE`
  (queries.sql L200) — both used by the engine above. **No new SQL required.**

**Conclusion:** the traversal, rollup, synthetic-exclusion, noise-filtering, depth
cap, and topological ordering all exist. The feature is a *reframing + a clean
multi-source entry point*, not new graph logic.

## Decision 1 — New tool, not an extension or pure alias (RECOMMENDED)

Three options were considered:

| Option | Verdict |
|--------|---------|
| Extend `get_change_scope` with a flag | **Rejected.** Conflates two questions ("if I change this" vs "this is empty"); changes a shipped tool's contract; single-table only. |
| Pure alias that calls `get_change_scope` and relabels | **Rejected as the public shape**, but the *internals* do delegate. An alias cannot take a *list* of sources (change-scope is single-table) and cannot union closures. |
| **New tool `get_empty_propagation(tables)`** delegating to the shared engine | **CHOSEN.** Distinct intent + multi-source union; reuses `_affected_columns_closure`, `_rollup_to_tables`, `_exclude_synthetic_tables`, `_kahn_topological_sort`. No traversal reimplemented. |

**Rationale.** Semantically, empty-propagation is a *subset/relabel* of change-scope
(every empty column is one whose data depends on the unfilled source). But the
*output framing* differs (which columns go empty; which tables can't populate;
ordered closest-to-source-first) and the *input is a set*, which the single-table
change-scope tools cannot express. A new tool keeps each shipped contract stable
and is the smallest correct surface for the multi-source case.

The new tool MUST NOT duplicate the closure/rollup/exclude/order logic. It composes
the existing private helpers (all already in `tools.py`, same module — no new
imports across module boundaries).

## Decision 2 — Entry-point signatures

### MCP tool
```python
@mcp.tool()
@_timed_tool("get_empty_propagation")
def get_empty_propagation(tables: list[str]) -> EmptyPropagationResult: ...
```
- `tables`: one or more qualified table names (e.g. `["ba.wtfe_verkoopinfo"]`).
  Lowercased per existing convention (graph keys are lowercased at index time).
- Returns `EmptyPropagationResult` (new model, §Decision 3).
- Raises `NotIndexedError` if no repos indexed (mirror `get_change_scope`).

### CLI surface
```
sqlcg analyze empty-impact <table> [<table> ...] [--raw] [--max-depth N]
```
- Added to the existing `analyze` Typer app
  ([`analyze.py`](../../src/sqlcg/cli/commands/analyze.py)).
- `--raw` disables noise filtering (consistent with sibling `analyze` commands).
- `--max-depth` optional (default: full depth = `None`), matching the engine's
  `max_depth` parameter.

> **Architectural constraint (verified):** CLI `analyze` commands read via
> [`run_read_routed`](../../src/sqlcg/server/read_client.py) (server-routed, with
> read-only fallback + lock semantics), **not** by calling MCP tool functions
> directly (those open their own backend via `_open_backend`). The CLI command
> therefore CANNOT just `from sqlcg.server.tools import get_empty_propagation` and
> call it — that would bypass server routing and reopen the DB.
> **Resolution:** the shared work (closure → rollup → exclude → order → filter) is
> factored into a private helper `_compute_empty_propagation(db, tables, max_depth)`
> in `tools.py` that takes an open backend. The MCP tool calls it under
> `_open_backend()`. The CLI command obtains a backend the same way sibling
> commands do — see §Open question O1; the developer MUST confirm the exact CLI
> backend-acquisition pattern before implementing, not guess.

## Decision 3 — Output shape (`EmptyPropagationResult`)

New Pydantic model in [`models.py`](../../src/sqlcg/server/models.py), placed next
to `ChangeScopeResult`. Field semantics are reframed for the empty case:

```python
class EmptyPropagationResult(BaseModel):
    sources: list[str]              # the unfilled tables that were named (echoed, lowercased)
    empty_columns: list[str]        # downstream column ids transitively derived from any source; these receive no data
    affected_tables: list[str]      # table rollup of empty_columns (noise-filtered, synthetic-excluded)
    propagation_order: list[str]    # affected_tables ordered closest-to-source first (reuses _kahn_topological_sort)
    fully_empty_tables: list[str]   # affected tables ALL of whose graph-known columns are in empty_columns
    partially_empty_tables: list[str]  # affected tables where SOME columns are empty but others are still fed by filled sources
    downstream_count: int           # len(affected_tables)
    noise_excluded: list[str]       # tables dropped as backup/noise + synthetic
    truncated: bool                 # 50k-node cap hit
    hint: str | None                # diagnostics (not found / no downstream / cycle / truncation)
```

Notes:
- `empty_columns`, `affected_tables`, `noise_excluded`, `truncated` map 1:1 onto
  the already-computed values inside `get_change_scope` — same derivation, renamed
  for the empty framing (`affected_columns` → `empty_columns`).
- `propagation_order` reuses `get_backfill_order`'s topological output (producers
  first = closest-to-the-unfilled-source first). **This is the requested
  depth/order field. Do not invent a second ordering.**
- `fully_empty_tables` vs `partially_empty_tables` is the precise expression of
  column-level partial impact (§Edge case E3): compare each affected table's
  `empty_columns` against its full known column set
  (`GET_COLUMNS_FOR_TABLE_QUERY`). This split is the one genuinely new computation;
  it is read-only and table-set-sized (not closure-sized).

## Edge cases (must be handled and tested)

- **E1 — table not found / not indexed.** `tables` contains a name with no columns
  in the graph. Echo it in `sources`, contribute nothing to the closure, set
  `hint` (reuse `get_change_scope`'s "confirm qualified name / file was indexed"
  wording). Do not raise (a partial set may still be valid). If *no* source resolves
  to any column and no downstream is found, `hint` says so explicitly.
- **E2 — table with no downstream.** Closure is empty → `empty_columns` and
  `affected_tables` empty, `propagation_order` empty, `hint` = "No downstream
  consumers — nothing propagates." (mirror `get_backfill_order` empty-order hint).
- **E3 — column-level partial impact (the precision requirement).** A downstream
  table fed by an unfilled source AND by still-filled sources: ONLY the columns
  transitively derived from the unfilled source go empty. This is inherent to the
  closure (it walks `COLUMN_LINEAGE` per column, not per table). The table appears
  in `partially_empty_tables`, and only its derived columns appear in
  `empty_columns`. **This is the headline correctness claim and must have a
  dedicated fixture** (§Tests T3).
- **E4 — CTE/derived/temp synthetic nodes.** Already contracted/excluded by
  `_exclude_synthetic_tables` (`_SYNTHETIC_TABLE_KINDS = {cte, derived, temp}`,
  tools.py L399) and by the synthetic-node contraction in `_kahn_topological_sort`.
  Reused as-is. Synthetic bridges must NOT appear as affected/empty tables.
- **E5 — 50k cap / truncation.** `_affected_columns_closure` sets `truncated=True`
  at `_MAX_CLOSURE_NODES`. Surface it on the result and in `hint`. Multi-source
  union must apply the cap across the *combined* start set (see §Multi-source).
- **E6 — star-source columns.** Star-expanded lineage already lands as concrete
  `COLUMN_LINEAGE` edges at index time; no special handling here. If a star source
  produced a synthetic/derived column id, E4's exclusion covers it. Test asserts a
  star-fed downstream column is reported as empty (no separate code path).
- **E7 — self-reference.** A source table appearing in its own downstream closure
  (cycle) must not be reported as its own affected table — mirror
  `get_change_scope`'s `t != target` filter, generalised to `t not in source_set`.

## Multi-source union semantics

- Resolve each named source's columns (`GET_COLUMNS_FOR_TABLE_QUERY`), concatenate
  into one `start_cols` list, run a **single** `_affected_columns_closure` over the
  union (the BFS seeds its queue from all start columns at once — cheaper and avoids
  double-counting vs per-source closures). The 50k cap then applies to the union,
  which is correct.
- `sources` echoes all named tables (lowercased, dedup, order-preserving).
- Exclude every named source from `affected_tables` (E7 generalised).

## Implementation Steps (ranked — build first → last)

### Phase 1: Engine extraction + model (no new behaviour)
**Step 1.1 — Add `EmptyPropagationResult` model.**
- File: [`models.py`](../../src/sqlcg/server/models.py), next to `ChangeScopeResult`.
- Acceptance: model importable; all fields typed as in §Decision 3; defaults match
  sibling models (`default_factory=list`, `truncated=False`, `hint=None`).

**Step 1.2 — Factor `_compute_empty_propagation(db, tables, max_depth)` in tools.py.**
- Reuses `GET_COLUMNS_FOR_TABLE_QUERY`, `_affected_columns_closure`,
  `_rollup_to_tables`, noise filter, `_exclude_synthetic_tables`,
  `_kahn_topological_sort`. No new traversal/SQL.
- Computes `fully_empty_tables` / `partially_empty_tables` by comparing each
  affected table's empty-column subset against its full known column set.
- Returns a plain dataclass/tuple the tool and CLI both consume.
- Acceptance: pure function of `(db, tables, max_depth)`; given a fixture graph,
  returns the documented sets; `partially_empty_tables` is non-empty for the E3
  fixture.

### Phase 2: MCP tool
**Step 2.1 — `get_empty_propagation(tables)` MCP tool.**
- File: tools.py, after `scope_change` / near the other change tools.
- Body: `_assert_indexed`, `_open_backend`, call `_compute_empty_propagation`,
  build `EmptyPropagationResult`.
- Acceptance: registered (`@mcp.tool()`); single-table and multi-table inputs both
  return correct sets on the integration fixture; `NotIndexedError` when empty.
- **Grep-confirmed call site:** the `@mcp.tool()` decorator registration IS the
  call site (FastMCP dispatch). Note for developer: confirm it appears in the
  server's tool list and bump the "16 MCP tools" count in
  [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md) L87 to 17.

### Phase 3: CLI surface
**Step 3.1 — `sqlcg analyze empty-impact` command.**
- File: [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py).
- Reads a backend via the **confirmed** CLI pattern (O1), calls
  `_compute_empty_propagation`, renders a Rich table: propagation_order with a
  per-table `empty / total columns` count and a `full|partial` marker; honours
  `--raw` and `--max-depth`.
- Acceptance: `uv run sqlcg analyze empty-impact <fixture-table>` prints the
  affected tables in propagation order with the partial/full marker; `--raw` shows
  noise rows; exit code 0 on success, non-zero on bad `--max-depth`.

### Phase 4: Docs + version
**Step 4.1 — Version bump + ARCHITECTURE_REVIEW tool-count + tool docstring.**
- `pyproject.toml` + `src/sqlcg/__init__.py` → `1.22.0`; `uv lock`.
- Update tool count in ARCHITECTURE_REVIEW.md.

## Test Strategy

Tests assert **observable output** (specific column/table ids), never just
"no exception". Name tests `test_<unit>_<scenario>_<expected>` and link this plan
in the docstring (per developer.md test-naming).

- **Unit (models / helper, `tests/unit/`):**
  - The helper, given a hand-built fixture graph, returns the exact `empty_columns`
    and `affected_tables` for a known single source.
  - Multi-source union returns the deduplicated union of two sources' closures and
    excludes both named sources from `affected_tables`.
  - Table-not-found source contributes nothing and sets `hint`; no raise.
  - No-downstream source yields empty sets and the "nothing propagates" hint.
  - Truncation: a stub backend that reports the cap sets `truncated=True` and the
    hint mentions the cap.

- **Integration (real in-memory DuckDB, `tests/integration/`):**
  - **T3 (headline) — partial impact fixture.** Build a downstream table `d` whose
    column `d.x` derives from unfilled `s.a` and whose column `d.y` derives from a
    *separate, still-filled* table `f.b`. Assert: `s.a`→`d.x` makes `d.x` ∈
    `empty_columns`; `d.y` ∉ `empty_columns`; `d` ∈ `partially_empty_tables`,
    `d` ∉ `fully_empty_tables`. This is the precision claim — a table fed partly by
    the unfilled source loses only the derived columns.
  - Fully-empty fixture: a downstream table all of whose columns derive from the
    unfilled source ⇒ it lands in `fully_empty_tables`.
  - Propagation order: a 3-hop chain `s → m → d` returns `[m, d]` in
    `propagation_order` (closest-to-source first).
  - Synthetic exclusion: a CTE/derived bridge between `s` and `d` does NOT appear in
    any output table list; `d` still appears.
  - Star-source: a `SELECT *` from `s` feeding `d` reports `d`'s columns as empty.
  - Fixtures use **aliased / schema-qualified** statement shapes that match the live
    corpus (per project history: minimal idealized fixtures hide real failure modes).

- **E2E (`tests/e2e/`):**
  - `sqlcg analyze empty-impact <table>` against a small indexed fixture repo prints
    the expected affected tables with the partial/full marker.

## Acceptance Criteria
- [ ] `get_empty_propagation(["s.tbl"])` returns `empty_columns` = exactly the
      downstream columns transitively derived from `s.tbl` (verified against a known
      fixture, column-id equality).
- [ ] For a downstream table fed by both the unfilled source and a still-filled
      source, ONLY the derived columns are in `empty_columns`, and the table is in
      `partially_empty_tables` (not `fully_empty_tables`).
- [ ] `propagation_order` lists affected tables closest-to-source first (3-hop chain
      asserted).
- [ ] CTE/derived/temp synthetic nodes never appear in `affected_tables`,
      `propagation_order`, `fully_empty_tables`, or `partially_empty_tables`.
- [ ] Multi-source input returns the union closure and excludes all named sources.
- [ ] Not-found source: echoed in `sources`, no raise, `hint` set.
- [ ] No-downstream source: empty sets, "nothing propagates" hint.
- [ ] `truncated=True` and hint surface when the 50k cap is hit.
- [ ] `sqlcg analyze empty-impact <table>` prints affected tables in propagation
      order with a full/partial marker; `--raw` and `--max-depth` work; bad
      `--max-depth` exits non-zero.
- [ ] Version is `1.22.0` in `pyproject.toml`, `__init__.py`, and `uv.lock`.
- [ ] Every new method has a grep-confirmed call site before the PR opens
      (`_compute_empty_propagation` ← MCP tool + CLI; `get_empty_propagation` ←
      `@mcp.tool()` registration; CLI command ← Typer app).

## Risks and Mitigations
- **R1 — CLI cannot call the MCP tool function directly** (different backend/routing
  seam). Mitigated by extracting `_compute_empty_propagation` and confirming the CLI
  backend pattern (O1) before coding.
- **R2 — `fully/partially` split mis-derives "full" column set.** A table's "known
  columns" must come from the same `GET_COLUMNS_FOR_TABLE_QUERY` the engine seeds
  from, so the comparison is apples-to-apples. Test pins this.
- **R3 — perf.** Pure read path; reuses single-closure + single aggregate-adjacency
  query (no per-table/per-column loops). Does NOT touch parser/indexer hot paths or
  CLAUDE.md performance invariants. The only added work is the table-set-sized
  full-vs-empty column comparison.
- **R4 — live correctness.** This is lineage-class. Per project history (CI has
  shipped green while live-broken), the feature requires **live-DWH re-acceptance**
  before tag: run `get_empty_propagation` / `analyze empty-impact` on a real
  DWH-indexed graph for a known-empty table and eyeball that the affected/partial
  split is sane. Record the run in the postmortem. CI green is necessary but not
  sufficient.

## Version & Release
- Additive feature, nothing breaks → **minor**: `1.21.2` → **`1.22.0`**.
- Bump `pyproject.toml`, `src/sqlcg/__init__.py`, `uv lock` in the feature branch.
- Per project rule: agent does NOT close issues and does NOT tag; user verifies
  live + tags `v1.22.0` after merge.

## Open Questions (resolve before / during implementation; do not block planning)
- **O1 — CLI backend acquisition.** Sibling `analyze` commands read via
  `run_read_routed` (raw SQL strings), not via a `GraphBackend` handle, whereas
  `_compute_empty_propagation` wants a backend (for `db.run_read(...)` and the
  helper reuse). The developer MUST confirm the canonical pattern: either (a) run
  the helper inside the MCP/server seam and have the CLI route to it, or (b) obtain
  a read-only backend in the CLI the way the server fallback does
  (`get_backend(read_only=True)`), respecting the lock semantics documented in
  `read_client.py`. **Pick the pattern an existing command already uses; do not
  invent a third seam.** This is a minor-uncertainty resolution by existing
  pattern, not a new architecture decision.

> No blocking questions. Feature is well-defined and aligned with
> ARCHITECTURE_REVIEW.md (read-side lineage tooling; no parser/schema-CSV path).
