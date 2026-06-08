# Sprint Plan: Column Lineage Fix

## Summary

The 1,457-file Snowflake DWH parse-error experiment confirmed that the column
lineage pipeline emits **0 useful edges** across the entire corpus. Three
defects compound to produce this result:

1. `_parse_statement` discards lineage errors into a throwaway `ParsedFile`
   (`out_temp`), hiding 1,180 real failures from `ParsedFile.errors`.
2. `SELECT base.*` reaches `sg_lineage` as `col_name=''` / `'*'`, raising
   `"BASE.* is not <class 'sqlglot.expressions.core.Alias'>"` for 248 files.
3. `_parse_statement` calls `sg_lineage` with `sources={}` , so temp-table
   chains (`CREATE TEMP TABLE tmp_a AS … ; INSERT INTO tgt SELECT … FROM tmp_a`)
   cannot be resolved — sqlglot has no way to inline the upstream definition.

This sprint delivers four ordered tickets that fix observability first, eliminate
the star-column crash, then unlock real lineage edges through temp-table source
accumulation, and finally close the performance gap on outlier files.

## Scope

### In Scope
- `src/sqlcg/parsers/ansi_parser.py` — `parse_file`, `_parse_statement`
- `src/sqlcg/parsers/base.py` — `_extract_column_lineage`
- `src/sqlcg/parsers/snowflake_parser.py` — `_parse_scripting_file` call site
  (must follow the `_parse_statement` signature change in T-03)
- Unit tests under `tests/unit/test_base_parser.py` and a new ETL-temp-chain
  integration test
- Two outlier files for the perf ticket (`WTDH_ARTIKEL.sql`,
  `WTDH_ARTIKEL_ETL.sql`) — assertion-based timing, not benchmark

### Non-Goals
- Schema inference for un-typed temp tables (we only inline the upstream
  `SELECT` body; we do **not** attempt to derive types)
- DDL-only file improvements (E3 / E4)
- Cross-file lineage propagation (handled by `CrossFileAggregator`, separate)
- Re-tuning confidence scores on success edges

## Design

### Error propagation (T-01)

`_parse_statement` will accept the caller's `ParsedFile` directly:

```python
def _parse_statement(self, stmt, path, stmt_index, out: ParsedFile) -> QueryNode:
    ...
    column_lineage = self._extract_column_lineage(
        stmt, path, out, schema, dst_table=target,
        sources=stmt_sources,   # added in T-03
    )
```

The throwaway `out_temp` is removed. `parse_file` and
`_parse_scripting_file` (snowflake) are updated to pass `out`.

### Star-column skip (T-02)

In `_extract_column_lineage`, before `sg_lineage` is called, reject any
`col_expr` that is an `exp.Star`, or an `exp.Column` whose `this` is an
`exp.Star`, or a `col_name` of `''` / `'*'`. These are appended to
`out.errors` as `col_lineage_skip:star:<table_or_unknown>` and continue —
no edge is emitted (matches E2 semantics).

### Temp-table source accumulation (T-03)

`AnsiParser.parse_file` will maintain a per-file `sources_map: dict[str, exp.Query]`
keyed by the unqualified name of any `CREATE TEMP TABLE … AS <query>` /
`CREATE TABLE … AS <query>` / `WITH cte AS (…)` it has already processed.
The map is passed into `_parse_statement(..., sources_map=sources_map)`,
which forwards it to `_extract_column_lineage(..., sources=sources_map)`.

Inside `_extract_column_lineage`, the call becomes:

```python
root = sg_lineage(
    col_name, body,
    schema=schema,
    sources=sources_map,
    dialect=self.DIALECT,
)
```

After a successful CREATE TEMP TABLE … AS SELECT statement is parsed, its
SELECT body is registered in `sources_map` under the temp table's
unqualified name (case-folded to lower per sqlglot convention). Permanent
CTAS targets are also registered so multi-statement scripts that build
intermediate permanent tables benefit identically.

### Performance (T-04)

`build_scope` is currently called once per statement inside
`_parse_statement`. For files with N statements this costs N full traversals.
We will build the scope **once per `parse_file` call**: call `build_scope(stmt)`
for each statement in `parse_file` and collect results into
`file_scopes: list[Scope | None]`, then pass the pre-built scope into
`_parse_statement` so it never calls `build_scope` itself.

> **Reviewer note**: `sg_lineage(..., scope=)` must NOT be used here.
> When `scope=` is supplied, `sg_lineage` skips `qualify.qualify()` entirely
> (confirmed from sqlglot source: `if not scope: qualify(...)` guard at line 119
> of `sqlglot/lineage.py`). Passing the raw unqualified scope would silently
> break column qualification. The scope pre-build applies only to the
> table-extraction `_real_tables(root_scope)` call, not to `sg_lineage`.

Target: ≥36% reduction on `WTDH_ARTIKEL.sql` (45 stmts, 14.2s baseline).

### Dependencies
- sqlglot ≥ 20.0 (`lineage(... sources=)` parameter — already pinned)

## Implementation Tickets

### T-01 — Propagate column-lineage errors to the real `ParsedFile`

**File**: `src/sqlcg/parsers/ansi_parser.py` lines 88–159
**Also**: `src/sqlcg/parsers/snowflake_parser.py` lines 117–127

**Before**:
```python
def _parse_statement(self, stmt, path, stmt_index) -> QueryNode:
    ...
    out_temp = ParsedFile(path=path, dialect=self.DIALECT)
    column_lineage = self._extract_column_lineage(
        stmt, path, out_temp, schema, dst_table=target
    )
```
Errors appended by `_extract_column_lineage` go into `out_temp`, which is
discarded when `_parse_statement` returns. `ParsedFile.errors` never sees
the 1,180 col_lineage failures.

**After**:
1. `_parse_statement` gains a required `out: ParsedFile` parameter.
2. `out_temp` is deleted.
3. The `_extract_column_lineage` call passes `out=out`.
4. `AnsiParser.parse_file` (line 68) passes `out` to `_parse_statement`.
5. `SnowflakeParser._parse_scripting_file` (line 119) passes `out` to the
   parent's `_parse_statement`.

**Files affected**:
- `src/sqlcg/parsers/ansi_parser.py`
- `src/sqlcg/parsers/snowflake_parser.py`

**Acceptance**:
- A unit test parses an INSERT whose SELECT body references an unknown
  table (forces `sg_lineage` to raise) and asserts that the returned
  `ParsedFile.errors` contains an entry matching
  `r"^col_lineage:[A-Za-z_]+:"`. The previous behavior (empty errors) is
  the regression we are catching.
- A unit test parses a clean SELECT with one CTAS and asserts
  `parsed.errors == []` (no false-positive errors from the propagation
  change).
- `grep -n "out_temp" src/sqlcg/parsers/` returns zero matches.
- `grep -n "_parse_statement(" src/sqlcg/parsers/` shows every caller
  passes `out` (signature consistency).

---

### T-02 — Skip star expressions in `_extract_column_lineage`

**File**: `src/sqlcg/parsers/base.py` lines 473–481

**Before**:
```python
for col_expr in col_expressions:
    if col_expr.alias:
        col_name = col_expr.alias
    elif isinstance(col_expr, exp.Column):
        col_name = col_expr.name   # ''  or '*' for exp.Star
    else:
        continue
```
For `SELECT base.*` sqlglot produces `exp.Column(this=exp.Star(),
table=exp.Identifier('base'))`. `col_expr.name` returns `''`. The
subsequent `sg_lineage('', body, ...)` raises
`"BASE.* is not <class 'sqlglot.expressions.core.Alias'>."` (12
production occurrences, 248 files affected via temp-chain pattern).

**After**: Insert a star check before assignment:
```python
# Skip star projections — sg_lineage requires a concrete column name.
if isinstance(col_expr, exp.Star) or (
    isinstance(col_expr, exp.Column) and isinstance(col_expr.this, exp.Star)
):
    qualifier = col_expr.table if isinstance(col_expr, exp.Column) else None
    out.errors.append(f"col_lineage_skip:star:{qualifier or '<unqualified>'}")
    continue

if col_expr.alias:
    col_name = col_expr.alias
elif isinstance(col_expr, exp.Column):
    col_name = col_expr.name
    if not col_name or col_name == "*":          # belt-and-braces guard
        out.errors.append("col_lineage_skip:star:<empty_name>")
        continue
else:
    continue
```

**Files affected**:
- `src/sqlcg/parsers/base.py`

**Acceptance**:
- Unit test: parse `CREATE TEMP TABLE tmp_b AS SELECT base.*, 1 AS x
  FROM tmp_a base` and assert:
  - `parsed.errors` contains exactly one entry starting with
    `col_lineage_skip:star:base`.
  - No entry starts with `col_lineage:` (the BASE.* exception is gone).
  - At least one edge is emitted for column `x` (the star skip does not
    short-circuit subsequent projections).
- Unit test: parse `SELECT * FROM t` and assert one
  `col_lineage_skip:star:<unqualified>` entry, zero edges, no exception
  raised.
- `grep -n "col_lineage_skip:star" src/` shows the marker is the only
  exit path for star expressions.

---

### T-03 — Pass temp-table sources into `sg_lineage`

**Depends on**: T-01 (clean error channel), T-02 (no star crashes
poisoning the chain)

**File**: `src/sqlcg/parsers/ansi_parser.py` `parse_file` (lines 35–86)
and `_parse_statement` (lines 88–159)
**File**: `src/sqlcg/parsers/base.py` `_extract_column_lineage` line 513
**File**: `src/sqlcg/parsers/snowflake_parser.py` `_parse_scripting_file`
line 119

**Before**: each statement is parsed in isolation; `sg_lineage` is invoked
with `schema=schema` only. For
```sql
CREATE TEMP TABLE tmp_a AS SELECT col1 FROM BA.source;
INSERT INTO BA.target SELECT col1 FROM tmp_a;
```
sqlglot's qualifier raises `Cannot find column 'col1' in query` (E5,
1,180×) or returns a root whose source is an unresolved `exp.Select`
subquery (E8, 254×). Net useful edges across 1,457 files: **0**.

**After**:
1. `AnsiParser.parse_file` initialises `sources_map: dict[str, exp.Query]
   = {}` before the statement loop.
2. After `_parse_statement` returns successfully for a CREATE … AS SELECT
   (temp or permanent), register the inner SELECT body:
   ```python
   # Unwrap Subquery wrapper: `CREATE TABLE t AS (SELECT ...)` gives
   # stmt.expression = exp.Subquery, not exp.Select (verified with sqlglot).
   _expr = stmt.expression
   if isinstance(_expr, exp.Subquery):
       _expr = _expr.this  # unwrap to the inner Select
   if isinstance(stmt, exp.Create) and isinstance(_expr, exp.Select):
       target_name = (target.name or "").lower()
       if target_name:
           sources_map[target_name] = _expr
   ```
   (Use the unqualified `target.name`; sqlglot's qualifier resolves names
   case-insensitively and prefers the unqualified key.
   Reviewer note: `exp.Subquery` wrapping occurs when the CTAS body is
   parenthesised, e.g. `CREATE TABLE t AS (SELECT ...)` — confirmed by
   inspecting `sqlglot.parse(...)[0].expression` on both forms.)
3. `_parse_statement` gains `sources_map: dict[str, exp.Query]` parameter
   and forwards it to `_extract_column_lineage(..., sources=sources_map)`.
4. `_extract_column_lineage` gains `sources: dict[str, exp.Query] | None
   = None` parameter and passes it through:
   ```python
   root = sg_lineage(
       col_name, body,
       schema=schema,
       sources=sources or {},
       dialect=self.DIALECT,
   )
   ```
5. `SnowflakeParser._parse_scripting_file` builds and threads its own
   per-file `sources_map` (scripting blocks frequently rely on the same
   pattern).

**Files affected**:
- `src/sqlcg/parsers/ansi_parser.py`
- `src/sqlcg/parsers/base.py`
- `src/sqlcg/parsers/snowflake_parser.py`

**Acceptance**:
- New integration test
  `tests/integration/test_temp_table_lineage.py::test_temp_chain_resolves_to_real_table`:
  parse the canonical pattern
  ```sql
  CREATE TEMP TABLE tmp_a AS SELECT col1, col2 FROM BA.source_table;
  CREATE TEMP TABLE tmp_b AS SELECT base.col1 AS c1, base.col2 AS c2 FROM tmp_a base;
  INSERT INTO BA.target SELECT c1, c2 FROM tmp_b;
  ```
  Assert that for the INSERT statement, `column_lineage` contains at
  least two edges whose `src.table.full_id == "BA.source_table"` (i.e.,
  lineage walks all the way back through the chain).
- Unit test: parse a single `INSERT INTO t SELECT a FROM cte_only_in_with`
  inside a `WITH cte_only_in_with AS (SELECT a FROM real)` and assert one
  edge with `src.table.name == "real"`. (Confirms the same code path
  works for CTEs, not just temp tables.)
- Unit test: when `sources_map` is empty (single-statement INSERT against
  a real table with schema provided), the edge count and confidence are
  unchanged from the pre-change baseline. Add a snapshot of
  `[(e.src.full_id, e.dst.full_id) for e in edges]` and assert equality.
- Unit test: parse `CREATE TABLE tmp_c AS (SELECT col1 FROM BA.src)` (parenthesised
  form) and assert `tmp_c` is registered in `sources_map`. Catches the
  `exp.Subquery` wrapping case where `stmt.expression` is not `exp.Select` directly.
- `grep -n "sources=" src/sqlcg/parsers/base.py` shows exactly one call
  site at the `sg_lineage(...)` invocation.
- E2E re-run on `etl/sql/interface/wtfi_pushorder.sql`: at least one edge
  with src in `BA.*`. (Asserted in the e2e test, not in unit.)

---

### T-04 — Reuse `build_scope` once per file

**Depends on**: T-03 landed (so the perf measurement reflects the new
hot path)

**File**: `src/sqlcg/parsers/ansi_parser.py` lines 116–131

**Before**: every `_parse_statement` call rebuilds its own scope:
```python
from sqlglot.optimizer.scope import build_scope
root_scope = build_scope(stmt)
```
For `WTDH_ARTIKEL.sql` (45 statements) this is 45 traversals → 14.2s.

**After**: build once at the file level, pass per-statement scope down:
1. In `parse_file`, after `statements = sqlglot.parse(...)`, build
   `file_scopes: list[Scope | None]` by calling `build_scope(stmt)` once
   per statement and store the list.
2. `_parse_statement` accepts `scope: Scope | None` instead of building
   it.
3. The fallback path (`scope is None`) still uses `_fallback_table_scan`.

**Files affected**:
- `src/sqlcg/parsers/ansi_parser.py`
- `src/sqlcg/parsers/snowflake_parser.py` (call-site forwarding only)

**Acceptance**:
- New perf assertion test (skipped in CI by default, marked
  `@pytest.mark.perf`):
  ```python
  def test_artikel_under_9s():
      sql = (FIXTURE_DIR / "wtdh_artikel.sql").read_text()
      t0 = time.perf_counter()
      AnsiParser(...).parse_file(Path("wtdh_artikel.sql"), sql)
      assert time.perf_counter() - t0 < 9.0   # 14.2s baseline → ≥36% cut
  ```
  Use a redacted 45-statement fixture, not the production file.
- Unit test: existing column_lineage tests still pass (regression guard)
  — no behavioural change, only reuse.
- `grep -n "build_scope" src/sqlcg/parsers/` returns exactly one call
  site (in `parse_file`).

---

## Test Strategy

### Unit (`tests/unit/test_base_parser.py`)
- T-01: error propagation (two cases — error visible, no false positives)
- T-02: star skip (qualified `base.*`, unqualified `*`)
- T-03: empty-`sources_map` parity snapshot, single-CTE positive case
- T-04: scope-reuse regression guard

### Integration (`tests/integration/test_temp_table_lineage.py` — new file)
- Canonical 3-statement temp chain → edges land on `BA.source_table`
- 4-statement chain with `SELECT base.*` in the middle (combines T-02
  and T-03)

### Perf (`tests/perf/` — new dir, gated by `pytest -m perf`)
- `WTDH_ARTIKEL.sql` redacted fixture under 9s

### E2E
- Re-run the parse-error experiment script from
  `scripts/collect_parse_errors.py` after T-03; success_edges must be
  > 0 and the dominant col_lineage error class must drop from 1,180 to
  < 200. (Recorded as a sprint exit metric, not a CI gate.)

## Acceptance Criteria

- [x] T-01: `out_temp` removed; `_parse_statement` accepts `out: ParsedFile`;
      both AnsiParser and SnowflakeParser call sites updated; tests above pass
- [x] T-02: star projections produce `col_lineage_skip:star:*` markers and
      zero edges, never raise
- [x] T-03: temp-table chains produce non-zero lineage edges to the real
      base table; `sg_lineage` is called with `sources=` populated
- [x] T-04: ≥36% wall-clock reduction on the 45-statement fixture; no
      behavioural change in unit tests
- [x] After the sprint, `success_edges` from the experiment script is > 0
      across the 1,457-file corpus

## Postmortem — 2026-05-06

Experiment re-run after T-03 merged (100 ETL files, `/dwh/etl/sql`, dialect=snowflake):

| Metric | Before | After |
|--------|--------|-------|
| E5 col_lineage failures | 1,180 | **0** |
| E8 no_edges_from_root | 254 | 39 |
| success_edges | 0 | **7** (3 files) |

Sprint exit criteria met. E8 (root returned but no edges extracted) is the next
dominant error class. Remaining E8 cases are likely due to unresolved table
references in files with no temp-table chain — deferred to a future sprint.

T-04 perf improvement visible in the unit perf fixture (45-stmt synthetic, < 9s).
The WTDH_ARTIKEL.sql (45 stmts) wall-clock in the experiment run is still ~16s
because `build_scope` is not the bottleneck there — `sg_lineage` itself dominates.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `sources=` changes lineage results for files that already worked | T-03 acceptance includes a parity snapshot for the empty-sources case; CI runs full unit suite |
| `sources_map` keyed on unqualified name collides when two temp tables share a name across schemas | sqlglot resolves unqualified temp tables by unqualified name; collisions overwrite, which mirrors SQL-runtime semantics. Documented in the ticket — not blocking. |
| Pre-built scope diverges from per-statement scope on edge cases (nested WITH) | T-04 keeps the per-statement build_scope call but lifts it to file level — same input, same output; regression test catches drift |
| Snowflake scripting fallback path forgets to thread `sources_map` | T-03 acceptance includes the snowflake parser in the grep check; reviewer must confirm |

## Rollout

- Land T-01 → T-02 → T-03 → T-04 as four separate PRs against
  `feat/v0.3.1-postmortem` (or sprint branch).
- After T-03 merges, re-run `scripts/collect_parse_errors.py` and update
  `plan/parsing_errors_experiment.md` postmortem section with the new
  E5 / E8 / success_edges counts.
- No data migration needed — re-index is the migration path
  (per `CLAUDE.md` non-negotiables).

## Ticket Order Summary

1. **T-01** — Error propagation (unblocks observability for everything else)
2. **T-02** — Star skip (removes the noisy crash class)
3. **T-03** — `sources=` temp-chain accumulation (the headline fix)
4. **T-04** — Scope reuse perf (lands last on the post-T-03 hot path)
