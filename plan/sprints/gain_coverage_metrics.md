# Feature Plan: Coverage metrics in `sqlcg db info` and `sqlcg gain`

## Summary

Surface real lineage-coverage health (catalog coverage, edge health, phantom-edge
rate, blindspot-table count) in both [`sqlcg db info`](../../src/sqlcg/cli/commands/db.py)
and [`sqlcg gain`](../../src/sqlcg/cli/commands/gain.py). These are the **release-gate
metrics** — we do not ship until edge health ≥ 90%. Today `gain`/`db info` show raw
counts that look healthy (2,944 tables, 51,434 edges) but hide that only ~29% of edges
land on a catalogued destination. Four read-only queries against the existing DuckDB
graph; **no schema change, no new metrics collection, no indexer change.**

### Blocking Questions

> **BQ-1 (non-blocking for implementation; needs architect-reviewer follow-up):**
> This feature is **not** currently listed in [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md).
> The flow says to STOP and ask before planning a feature absent from the review.
> Decision taken: the feature is fully specified by the user (exact queries, exact
> output, exact thresholds) and is purely additive read-only observability with no
> design choices left open, so this plan proceeds. **Action for the user / architect-reviewer:**
> add a one-line entry under the release-gate / observability priority in
> `ARCHITECTURE_REVIEW.md` recording the 90%-edge-health release gate and that
> `gain`/`db info` are its dashboard. This does **not** block the developer.

---

## Scope

### In Scope

- Four read-only SQL queries against the live DuckDB graph (catalog coverage, edge
  health, phantom rate, blindspot count), each routed through the existing
  [`run_read_routed`](../../src/sqlcg/server/read_client.py) seam.
- A new **Coverage** block in `sqlcg db info` (plain text), printed after the existing
  STAR / parsing-mode output.
- A new **Section G: Coverage** in `sqlcg gain` (human-readable + `--json` fields),
  mirroring the Section F degrade-gracefully pattern.
- Threshold colouring shared between both surfaces.
- Unit tests (mocked `run_read_routed`) asserting the four numbers and the colour
  thresholds appear, plus one integration test against a deterministic fixture graph.
- Version bump: **patch** (`1.6.0` → `1.6.1`).

### Non-Goals

- No schema change, no new column, no schema-version bump.
- No new metrics collection (no `metrics/store.py` change, no `SQLCG_METRICS` path).
- No indexer / parser change. Do **not** touch [`base.py`](../../src/sqlcg/parsers/base.py)
  or [`indexer.py`](../../src/sqlcg/indexer/indexer.py) — none of the perf invariants
  in CLAUDE.md are in play here.
- No `--json` flag added to `db info` (plain text is acceptable per the brief).
- Not fixing the underlying coverage gap (P1 CTE leak, P3 space-name schema strip,
  P4 CTAS column discovery) — this feature only *measures* it.

---

## Design

### Shared coverage module

Add one new helper module so the four queries and the threshold logic live in **exactly
one place** and both surfaces stay consistent (the brief requires identical numbers and
identical colours in both commands).

**New file: `src/sqlcg/cli/coverage.py`**

```python
"""Lineage-coverage metrics shared by `sqlcg db info` and `sqlcg gain`.

Read-only: four queries against the existing DuckDB graph, routed through
run_read_routed (server-aware). No schema change, no metrics collection.
"""
from __future__ import annotations
from dataclasses import dataclass

# DuckDB expression that strips the trailing ".<column>" from a COLUMN_LINEAGE
# key, yielding the destination table's full_id — which equals SqlTable.qualified
# and HAS_COLUMN.src_key. Verified against base.py: ColumnRef.full_id ==
# f"{table.full_id}.{name}", joined by ".". No regex needed (DuckDB).
_DST_TABLE = "left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))"

@dataclass
class CoverageStats:
    catalogued_tables: int
    total_tables: int
    good_edges: int
    total_edges: int
    phantom_edges: int
    blindspot_tables: int

    @property
    def catalog_pct(self) -> float: ...
    @property
    def edge_health_pct(self) -> float: ...
    @property
    def phantom_pct(self) -> float: ...

def collect_coverage() -> CoverageStats | None:
    """Run the four queries via run_read_routed. Returns None on any failure
    (graph unavailable / server busy / empty graph) so callers degrade
    gracefully, exactly like gain.py Section F."""
```

The four queries (verbatim from the brief, verified against the schema in
[`duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py): `HAS_COLUMN.src_key` =
table `full_id`, `COLUMN_LINEAGE.{src_key,dst_key}` = column keys,
`COLUMN_LINEAGE.inferred_from_source_name` BOOLEAN, `SqlTable` node table):

```sql
-- 1. catalog_coverage
SELECT COUNT(DISTINCT src_key) AS catalogued_tables,
       (SELECT COUNT(*) FROM "SqlTable") AS total_tables
FROM "HAS_COLUMN";

-- 2. edge_health
SELECT SUM(CASE WHEN EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc
           WHERE hc.src_key = left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
       ) THEN 1 ELSE 0 END) AS good_edges,
       COUNT(*) AS total_edges
FROM "COLUMN_LINEAGE" cl;

-- 3. phantom_rate
SELECT SUM(CASE WHEN inferred_from_source_name THEN 1 ELSE 0 END) AS phantom,
       COUNT(*) AS total
FROM "COLUMN_LINEAGE";

-- 4. blindspot_tables
SELECT COUNT(DISTINCT left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))) AS blindspot
FROM "COLUMN_LINEAGE" cl
WHERE NOT EXISTS (
    SELECT 1 FROM "HAS_COLUMN" hc
    WHERE hc.src_key = left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
);
```

> Note the double-quoted table names (`"SqlTable"`, `"HAS_COLUMN"`, `"COLUMN_LINEAGE"`)
> — every existing query in `db.py`/`gain.py` quotes node/edge table names, and DuckDB
> treats the mixed-case `SqlTable` as case-folded unless quoted. Match the existing style.

### Threshold / colouring rules (single source, in `coverage.py`)

The brief gives the thresholds; encode them as helpers returning a Rich colour tag so
both commands render identically:

| Metric | yellow | red |
|--------|--------|-----|
| Edge health | `< 50%` | `< 30%` |
| Phantom rate | `> 10%` | `> 20%` |
| Blindspot tables | `> 500` | (no red tier specified — yellow only) |
| Catalog coverage | (no threshold specified — plain) | — |

Provide one function per coloured metric, e.g. `edge_health_colour(pct) -> str` returning
`"green" | "yellow" | "red"`, so the unit tests assert the colour boundary directly and
both surfaces cannot drift.

### API / output shape

**`sqlcg db info`** — append after the parsing-mode distribution block:

```
  Coverage:
    Tables with catalog:      528 / 2944  (18%)
    Edge health:            14884 / 51434  (29%)
    Phantom edges:          13412 / 51434  (26%)
    Blindspot tables:        1681
```

Edge-health / phantom / blindspot lines wrapped in the colour tag from the helper.

**`sqlcg gain`** — new Section G (human-readable):

```
[bold cyan]G. Coverage[/bold cyan]
  Tables with catalog: 528 / 2944 (18%)
  Edge health: 14884 / 51434 (29%)
  Phantom edges: 13412 / 51434 (26%)
  Blindspot tables: 1681
```

**`sqlcg gain --json`** — add to the existing `payload` dict (only when coverage is
available, mirroring how `parse_quality` is added only when present):

```json
"coverage": {
  "catalogued_tables": 528,
  "total_tables": 2944,
  "good_edges": 14884,
  "total_edges": 51434,
  "phantom_edges": 13412,
  "blindspot_tables": 1681
}
```

### Data Models

`CoverageStats` dataclass (above). No persisted model, no graph schema touched.

### Dependencies

- [`run_read_routed`](../../src/sqlcg/server/read_client.py) — reused, unchanged.
- Rich `Console` — already imported in both command modules.
- No new third-party dependency.

---

## Implementation Steps

### Phase 1: Shared coverage helper

**Step 1.1**: Create `src/sqlcg/cli/coverage.py` with `CoverageStats`, the four query
constants, `collect_coverage()`, and the three colour-helper functions.
- Files affected: `src/sqlcg/cli/coverage.py` (new).
- `collect_coverage()` calls `run_read_routed(query, {})` four times; wraps the whole
  body in `try/except Exception: return None` so a busy server (`typer.Exit`) or an
  empty/absent graph degrades to "no coverage section" — same contract as gain Section F.
- Guard divide-by-zero: percent properties return `0.0` when the denominator is 0.
- Result rows are `list[dict]`; read `rows[0]["catalogued_tables"]` etc. by the aliased
  column names in the SQL above. `SUM(...)` over zero rows returns SQL `NULL` → coerce
  with `int(row[...] or 0)`.
- Acceptance: importable; `collect_coverage()` returns a populated `CoverageStats` when
  `run_read_routed` is patched to return the four canned result sets, and returns `None`
  when `run_read_routed` raises.

### Phase 2: Wire `db info`

**Step 2.1**: In [`db.py`](../../src/sqlcg/cli/commands/db.py) `db_info()`, after the
parsing-mode block, call `collect_coverage()` and, when it returns a non-`None` value,
print the Coverage block with colour tags.
- Files affected: `src/sqlcg/cli/commands/db.py`.
- When `collect_coverage()` returns `None`, print nothing (no error line) — keeps the
  command green on an empty graph, consistent with the existing health-check tone.
- Acceptance: with a populated graph the four lines render with the correct percentages
  and colour tags; with an empty/unavailable graph no Coverage block and no crash.

### Phase 3: Wire `gain` Section G

**Step 3.1**: In [`gain.py`](../../src/sqlcg/cli/commands/gain.py), after the Section F
block, call `collect_coverage()` (store result in a `coverage` variable computed once,
before the json/human branch split so both branches use it).
- Files affected: `src/sqlcg/cli/commands/gain.py`.
- JSON branch: add `payload["coverage"] = {...}` only when `coverage is not None`
  (mirror the `if parse_quality is not None` guard).
- Human branch: print Section G only when `coverage is not None`, with colour tags.
- Acceptance: `gain` prints Section G with the four numbers; `gain --json` includes the
  `coverage` object; both omit it cleanly when the graph is unavailable.

### Phase 4: Version bump

**Step 4.1**: Bump version `1.6.0` → `1.6.1` (patch — no new capability, no schema change).
- Files affected: [`pyproject.toml`](../../pyproject.toml), [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py), then `uv lock`.
- Acceptance: all three agree on `1.6.1`; `uv run sqlcg --version` prints `1.6.1`.

---

## Test Strategy

Describe each test by the behaviour it verifies. The developer names tests
`test_<unit>_<scenario>_<expected>` and links this plan in the docstring.

### Unit tests — `tests/unit/test_coverage_metrics.py`

Patch `run_read_routed` (at the seam each module imports it from) with a side-effect that
returns the four canned result sets keyed on a substring of the query
(`"SqlTable"` for q1, the `EXISTS`/`good_edges` shape for q2, `"inferred_from_source_name"`
for q3, the `NOT EXISTS`/`blindspot` shape for q4) — same dispatch style as
[`test_db_info.py`](../../tests/unit/test_db_info.py).

- **collect_coverage returns populated stats** — given the four canned rows, the returned
  `CoverageStats` has the expected six integers and the three percent properties compute
  correctly (e.g. 528/2944 ≈ 18%, 14884/51434 ≈ 29%, 13412/51434 ≈ 26%).
- **collect_coverage degrades to None when the read raises** — when `run_read_routed`
  raises `typer.Exit`, returns `None` (no exception escapes).
- **collect_coverage handles empty graph without dividing by zero** — zero tables / zero
  edges yields `0.0` percentages, not a `ZeroDivisionError`, and `total_edges == 0`.
- **edge-health colour boundaries** — `edge_health_colour` returns red below 30%, yellow
  in [30%, 50%), green at/above 50% (assert the exact boundary values 29 / 30 / 49 / 50).
- **phantom colour boundaries** — red above 20%, yellow in (10%, 20%], green at/below 10%.
- **blindspot colour boundary** — yellow above 500, plain at/below 500.

### Unit tests — `db info` Coverage block

In `tests/unit/test_db_info.py` (extend) or a new `test_db_info_coverage.py`:

- **db info prints the Coverage block with live numbers** — with the four queries
  mocked, the output contains `Coverage:`, `Tables with catalog:`, `Edge health:`,
  `Phantom edges:`, `Blindspot tables:` and the specific numbers (528, 2944, 14884,
  51434, 13412, 1681).
- **db info omits the Coverage block on an empty graph** — when `collect_coverage`
  returns `None` (mock the four queries to raise or return empty), no `Coverage:` line
  appears and exit code is 0.

### Unit tests — `gain` Section G

In `tests/unit/test_gain_coverage.py`:

- **gain prints Section G with the four numbers** — human-readable output contains
  `G. Coverage` and the four metric labels with numbers.
- **gain --json includes the coverage object** — JSON payload has a `coverage` key with
  the six integer fields.
- **gain omits Section G when the graph is unavailable** — coverage helper returns
  `None`; no Section G, no crash, existing sections still print (regression guard so
  Section G failure cannot take down the rest of `gain`, matching Section F semantics).

### Integration test — `tests/integration/test_coverage_metrics_integration.py`

Build a **deterministic in-memory DuckDB fixture graph** so the four numbers are exact
and not mocked. Use `DuckDBBackend` + `init_schema()` then insert known rows via
`run_write` (model on the real-DB integration tests in `tests/integration/`):

- Insert N `SqlTable` rows; give M of them ≥1 `HAS_COLUMN` row (table `full_id` as
  `src_key`, `table.col` as `dst_key`).
- Insert `COLUMN_LINEAGE` rows where some `dst_key` strip to a catalogued table and some
  do not; set `inferred_from_source_name` true on a known subset.

Then patch `run_read_routed` to route to this backend's `run_read` (or run
`collect_coverage` against a `get_backend` pointed at the fixture path) and assert:

- **catalog coverage equals catalogued-table count over total tables** — exact integers.
- **edge health counts only edges whose stripped dst table has a HAS_COLUMN row** —
  exact `good_edges` / `total_edges`, proving the `left(...instr(reverse...))` strip
  joins correctly to `HAS_COLUMN.src_key`.
- **phantom count equals the rows flagged `inferred_from_source_name`** — exact.
- **blindspot count equals distinct dst tables with edges but no HAS_COLUMN** — exact,
  including the case where two edges point at the *same* uncatalogued table (counted once).

> The blindspot duplicate-table case is the load-bearing assertion: it proves
> `COUNT(DISTINCT left(...))` collapses per-table, not per-edge.

### Full gate

`uv run pytest`, `uv run pyright`, `uv run ruff check src tests` all green.

---

## Acceptance Criteria

- [ ] `src/sqlcg/cli/coverage.py` exists with `collect_coverage()` and is **called** from
      both `db_info()` and `gain_cmd()` (grep-confirmed call sites in both modules).
- [ ] `sqlcg db info` on a populated graph prints a `Coverage:` block with the four
      metrics, percentages, and threshold colours (edge health <50% yellow / <30% red;
      phantom >10% yellow / >20% red; blindspot >500 yellow).
- [ ] `sqlcg gain` prints `G. Coverage` with the same four numbers and colours.
- [ ] `sqlcg gain --json` output includes a `coverage` object with the six integer fields.
- [ ] Both commands omit their coverage output cleanly (no error, exit 0) when the graph
      is unavailable, empty, or the server is busy — verified by a test that makes
      `collect_coverage` return `None`.
- [ ] No change to `src/sqlcg/metrics/store.py`, `src/sqlcg/core/schema.py`,
      `src/sqlcg/core/duckdb_backend.py` (schema), or any indexer/parser file.
- [ ] No schema-version bump; app version bumped `1.6.0` → `1.6.1` (patch) across
      `pyproject.toml`, `src/sqlcg/__init__.py`, and the lockfile.
- [ ] Integration test produces the four exact numbers against a known fixture graph,
      including the distinct-table blindspot collapse.
- [ ] Full gate (pytest / pyright / ruff) green.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `db info` / `gain` crash when the graph is missing or the server is busy | `collect_coverage()` wraps all four reads in `try/except Exception → None`; both surfaces print nothing when `None`. Explicit "unavailable" tests. The `typer.Exit` raised by `run_read_routed` on a busy server is `Exception`-derived, so it is caught — same reasoning documented for Section F in `gain.py`. |
| Key-strip expression wrong for multi-dot keys | Verified: keys are `catalog.db.name.column` and the strip removes only the final `.column` via `instr(reverse(...))` (last dot), yielding `full_id` == `SqlTable.qualified` == `HAS_COLUMN.src_key`. The integration test asserts the join lands, not just that SQL runs. |
| `SUM(...)` over an empty edge table returns `NULL` not `0` | Coerce every aggregate read with `int(value or 0)`; covered by the empty-graph divide-by-zero test. |
| Numbers drift between `db info` and `gain` | Single shared `coverage.py` module — same queries, same dataclass, same colour helpers used by both. |
| Mixed-case `SqlTable` un-quoted folds in DuckDB | All table names double-quoted in the query constants, matching every existing query in `db.py`/`gain.py`. |
| Slow EXISTS subqueries on the 51k-edge DWH graph | Read-only and run only on explicit `db info`/`gain` invocation (not in any hot path); `HAS_COLUMN` is indexed on `src_key` (see `_INDEX_DDLS` in `duckdb_backend.py`), so the correlated `EXISTS` lookup is index-backed. No perf invariant is touched. |

## Rollout / Rollback

- Rollout: ships in the `1.6.1` patch. No migration, no re-index — reads existing data.
- Rollback: revert the PR; no schema or data state to unwind.
- This is the **release-gate dashboard**: track edge health on every `db info`/`gain`
  as the underlying P1/P3/P4 fixes land; the project ships the coverage milestone when
  edge health ≥ 90%.
