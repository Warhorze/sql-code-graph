# Sprint Plan: Lineage Coverage — Fix E5, E2, DDL-Skip, E4, Scope Reuse

Plan date: 2026-05-11
Author: sprint-planner
Source authority: `ARCHITECTURE_REVIEW.md` § 12 (Parsing Errors Experiment findings)
Policy: no backward compatibility; re-index is the migration path; no TODO in happy path

---

## Summary

Five tickets address the dominant lineage-coverage gaps found in the full-corpus
experiment (1,445 Snowflake DWH files). In priority order:

1. **FIX-E5** — Pass `SchemaResolver`-loaded information schema as `sources=` to
   `sg_lineage()` to unblock 1,280 E5 errors (`Cannot find column` on CTE-derived
   columns in `CREATE DYNAMIC TABLE` files whose sources are cross-file semantic views).
2. **FIX-E2** — Best-effort expression name extraction for unaliased non-`exp.Column`
   expressions (60.4% of all SELECT columns are currently skipped silently).
3. **FIX-DDL-SKIP** — Detect pure-DDL files early (all statements are `Command` or DDL
   with no SELECT/INSERT/MERGE/CTAS body) and bypass `sg_lineage()` entirely, eliminating
   the p99 slow-file outliers (18–21 s per `WTDH_ARTIKEL*` file) and 2,507 E3 noise events.
4. **FIX-E4** — Evaluate and apply local pre-processing workarounds for three distinct
   sqlglot Snowflake dialect gaps: `IF NOT EXISTS` misparse, unexpected-token in complex
   DML, UNPIVOT clause. Six DML files currently lose all column lineage.
5. **OPT-SCOPE** — Build the sqlglot `Scope` once per statement and reuse it across
   per-column `sg_lineage()` calls. Estimated 40–60% speedup for multi-column SELECTs.

---

## Scope

### In Scope

- `src/sqlcg/parsers/base.py` — `_extract_column_lineage`, `_build_sources_map`
- `src/sqlcg/parsers/ansi_parser.py` — `parse_file`, `_parse_statement`
- `src/sqlcg/parsers/snowflake_parser.py` — `parse_file` (DDL-skip and pre-processing)
- `src/sqlcg/lineage/schema_resolver.py` — `as_sources_dict` (new helper, FIX-E5)
- Unit and integration tests for each ticket
- Regression guard integration test

### Non-Goals

- E8 (no edges from subquery sources, 87% of root-returning `sg_lineage()` calls):
  deferred until E5 is resolved and edge volume increases
- Cross-file expansion beyond what `sources=` in the information schema provides
- KuzuDB schema changes
- BigQuery / ANSI / TSQL coverage — this sprint is Snowflake-dialect only
- Upstream sqlglot bug reports (FIX-E4 uses local workarounds only)

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| E5: `sg_lineage()` receives `sources=sources or {}` but sources is only the `sources_map` (temp table bodies), never the information schema | Confirmed — [`base.py` line 575](src/sqlcg/parsers/base.py): `sg_lineage(col_name, body, schema=schema, sources=sources or {}, dialect=self.DIALECT)`. The `sources` parameter comes from `sources_map` (CTAS bodies), never from `SchemaResolver`. `SchemaResolver.as_dict()` is passed as `schema=`, not `sources=`. |
| E2: unaliased non-`exp.Column` expressions execute `continue` silently | Confirmed — [`base.py` lines 539–542](src/sqlcg/parsers/base.py): `else: # Expression with no resolvable name ... continue`. No fallback, no error recorded, no edge emitted. |
| FIX-DDL-SKIP: no early DDL-file detection exists | Confirmed — [`ansi_parser.py` lines 56–60](src/sqlcg/parsers/ansi_parser.py) only sets `ParseQuality.SCRIPTING_FALLBACK` when a `Command` is detected, but still processes all statements through `_parse_statement` and `_extract_column_lineage`. No pre-check skips pure-DDL files before scope-build and lineage calls. |
| FIX-DDL-SKIP: `no_ddl` CLI flag exists but is a TODO | Confirmed — [`index.py` line 45–47](src/sqlcg/cli/commands/index.py): `# TODO: wire no_ddl through to the indexer once it supports the parameter` — confirmed TODO in the happy path. |
| FIX-E4: `IF NOT EXISTS` misparsed as `If()` function call | Confirmed by Q-12-1 findings in ARCHITECTURE_REVIEW.md § 12.6: `Required keyword: 'true' missing for <class 'sqlglot.expressions.functions.If'>` on 2 files (`htwfm_contract_us28254.sql`, `htwfm_employee_us28254.sql`). |
| OPT-SCOPE: `build_scope` called once per statement in `AnsiParser.parse_file`, but `sg_lineage()` re-qualifies per column | Confirmed — [`ansi_parser.py` lines 62–70](src/sqlcg/parsers/ansi_parser.py) builds `file_scopes` per statement and passes `scope=scope` to `_parse_statement`. However, [`base.py` line 574](src/sqlcg/parsers/base.py) calls `sg_lineage(col_name, body, ...)` per column without passing the pre-built scope — `sg_lineage` will call `qualify.qualify()` internally on every call. |
| `SchemaResolver.add_information_schema` populates `_tables` keyed by `(None, schema, table)` | Confirmed — [`schema_resolver.py` line 167](src/sqlcg/lineage/schema_resolver.py): `key = (None, row["TABLE_SCHEMA"] or None, row["TABLE_NAME"])`. The `as_dict()` method returns `{db: {table: [cols]}}` (no catalog level when catalog=None). |

---

## Ticket Table

| ID | Title | Primary Files | Effort | Priority | Depends on | Blocks |
|----|-------|--------------|--------|----------|-----------|--------|
| T-01 | `SchemaResolver.as_sources_dict()` + pass to `sg_lineage` | `schema_resolver.py`, `base.py` | M | 1 — highest | INDEPENDENT | T-05 (OPT-SCOPE can reuse the same param plumbing) |
| T-02 | Best-effort expression name extraction (FIX-E2) | `base.py` | S | 2 | INDEPENDENT | NONE |
| T-03 | Pure-DDL file early skip + wire `--no-ddl` (FIX-DDL-SKIP) | `ansi_parser.py`, `snowflake_parser.py`, `index.py` | S | 3 | INDEPENDENT | T-05 (DDL-skip reduces the file set that OPT-SCOPE needs to handle) |
| T-04 | Snowflake dialect gap workarounds (FIX-E4) | `snowflake_parser.py`, `base.py` | M | 4 | INDEPENDENT | NONE |
| T-05 | `scope=` reuse per statement (OPT-SCOPE) | `base.py`, `ansi_parser.py` | M | 5 — lowest | T-01, T-03 | NONE |

---

## Recommended Implementation Order

### Why This Order

**T-01 ships first** because it is the highest-impact correctness fix (1,280 errors) and
is causally independent of all other tickets. No ticket depends on it completing before it
starts, but T-05 will need the new `as_sources_dict()` helper so T-01 must merge before T-05.

**T-02 and T-03 can proceed in parallel after T-01 or independently** — they both touch
`base.py` but in different code paths (T-02 is in the `else:` branch of the column-name
extraction loop; T-03 introduces a pre-check before the loop even starts). They have no
causal link. On a two-developer team, T-02 and T-03 start after T-01 merges but can run
simultaneously. On a single developer, sequence T-02 before T-03 to keep the diff surface
small (T-02 is a 4-line change; T-03 is a new method + CLI wiring).

**T-03 ships before T-05** because DDL-file skip reduces the set of files `sg_lineage()`
is called on. OPT-SCOPE benefits are measured correctly only after the DDL-file noise is
eliminated. Shipping T-05 before T-03 would optimise the wrong workload.

**T-04 is independent and can be sequenced anywhere after T-01** — it only touches
`snowflake_parser.py` pre-processing and does not interact with E5/E2/scope logic.
Best slotted as Track B work for a two-developer team, or after T-02 for a single developer
(lower priority than the high-reward correctness fixes).

**T-05 ships last** because it depends on T-01's parameter plumbing (it will pass the same
`sources_dict` to `sg_lineage` via the scope-reuse path) and on T-03's DDL-skip (which
shrinks the optimisation target).

### Single-Developer Sequence

1. T-01 — `as_sources_dict()` + `sources=` wiring (unblocks E5, establishes parameter shape)
2. T-02 — best-effort expression name (smallest change, high reward, safe)
3. T-03 — DDL-file early skip + `--no-ddl` wiring (eliminates p99 outliers)
4. T-04 — Snowflake dialect gap pre-processing (isolated, lower reward than above)
5. T-05 — `scope=` reuse (optimisation, no correctness risk if done last)

### Two-Developer Sequence

Track A (correctness):
- A1: T-01 — `as_sources_dict()` + `sources=` wiring
- A2: T-02 — best-effort expression name
- A3: T-03 — DDL-file early skip

Track B (dialect + perf):
- B1: T-04 — Snowflake dialect gap workarounds (starts after T-01 merges; no dep on T-02/T-03)
- B2: T-05 — `scope=` reuse (starts after T-01 and T-03 merge)

Both tracks start after T-01 merges. T-05 must wait for T-03 to merge before opening
its PR, even if implementation starts earlier.

---

## Ticket Specifications

---

### T-01 — `SchemaResolver.as_sources_dict()` and pass to `sg_lineage()` as `sources=`

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-E5, § 12.6 Q-12-2 (High severity)
**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: T-05

**Root cause**: `sg_lineage()` in [`base.py` line 574–576](src/sqlcg/parsers/base.py)
receives `sources=sources or {}`, where `sources` is `sources_map` — a dict of CTAS
bodies accumulated within the current file. When a `CREATE OR REPLACE DYNAMIC TABLE`
statement references a cross-file semantic view (`IA_SEMANTIC."Schapbeschikbaarheid"`) via
a multi-level CTE, `qualify()` inside `sg_lineage()` cannot resolve the CTE because the
source view's column list is absent from both `schema=` and `sources=`. The result is
`Cannot find column 'MOVERTYPE' in query.` — 1,280 times in the full corpus.

`SchemaResolver` already holds the information schema in `_tables` (loaded via
`add_information_schema`), but only `as_dict()` is exposed. `as_dict()` returns
`{db: {table: [cols]}}`, which is `schema=` format. The `sources=` parameter to
`sg_lineage()` expects `Mapping[str, str | exp.Query]` — a mapping of scope name to SQL
or parsed query. These are different shapes.

The fix is: add `SchemaResolver.as_sources_dict()` returning `{table_name: SELECT_body}`
where the SELECT body is a minimal synthetic `SELECT col1, col2 FROM <table>` string for
each table in the schema. This lets `qualify()` expand CTE references to cross-file views
by providing a stub SELECT body that contains the column list. Then pass the result to
`_extract_column_lineage` as a new `schema_sources` parameter, merged with `sources_map`
before passing to `sg_lineage()`.

**What to do**:

1. Add `as_sources_dict()` to `SchemaResolver` in [`schema_resolver.py`](src/sqlcg/lineage/schema_resolver.py):

```python
def as_sources_dict(self) -> dict[str, str]:
    """Return a sources= dict for sg_lineage(): {table_name: synthetic_SELECT}.

    For each table known to the resolver, emits a minimal:
        SELECT col1, col2, ... FROM table_name
    This lets qualify() inside sg_lineage() expand CTE references to cross-file
    views whose columns are known from INFORMATION_SCHEMA but whose SQL body is
    not available in the current file.

    Returns:
        Dict mapping table_name (last component) to a synthetic SELECT SQL string.
        Keys are lowercased to match sqlglot's qualify() normalisation.
    """
    with self._lock:
        result: dict[str, str] = {}
        for (_, db, name), cols in self._tables.items():
            if not cols:
                continue
            col_list = ", ".join(cols)
            qualified = f"{db}.{name}" if db else name
            # Key is the bare table name (lowercased) — sqlglot expand() uses
            # the scope name which comes from the CTE or alias, not the full path.
            result[name.lower()] = f"SELECT {col_list} FROM {qualified}"
            # Also register under schema.table key for fully-qualified CTE sources
            if db:
                result[f"{db}.{name}".lower()] = f"SELECT {col_list} FROM {qualified}"
        return result
```

2. In [`base.py`](src/sqlcg/parsers/base.py), update `_extract_column_lineage` signature:

Add parameter after `sources`: `schema_sources: dict[str, str] | None = None`.

In the body, merge the two dicts before the `sg_lineage` call:

```python
combined_sources = {**(sources or {}), **(schema_sources or {})}
root = sg_lineage(
    col_name, body, schema=schema, sources=combined_sources, dialect=self.DIALECT
)
```

3. In [`ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py), update the call site in
   `_parse_statement` (line 182):

```python
schema_sources = self._schema.as_sources_dict() if self._schema else {}
extraction = self._extract_column_lineage(
    stmt, path, out, schema,
    dst_table=target,
    sources=sources_map,
    query_sources=sources,
    schema_sources=schema_sources,
)
```

Note: `schema_sources` must be computed once per file (not once per statement) in
`parse_file` and passed into `_parse_statement` to avoid calling `as_sources_dict()`
thousands of times per file. Add `schema_sources: dict[str, str] | None = None` to
`_parse_statement`'s signature and compute it once in `parse_file`:

```python
# Compute schema sources once per file
schema_sources = self._schema.as_sources_dict() if self._schema else {}
# Pass into the loop:
query_node = self._parse_statement(
    stmt, path, stmt_index, out, sources_map, scope=scope,
    schema_sources=schema_sources,
)
```

4. In [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py), the scripting
   fallback path calls `AnsiParser._parse_statement` directly (line 123). Update that
   call to pass `schema_sources` the same way. Compute `schema_sources` once at the top
   of `_parse_scripting_file`.

**Wiring verification**:

Before opening the PR, run:
- `grep -n "as_sources_dict" src/sqlcg/lineage/schema_resolver.py` must show the method defined.
- `grep -n "as_sources_dict" src/sqlcg/parsers/ansi_parser.py` must show at least one call site.
- `grep -n "schema_sources" src/sqlcg/parsers/base.py` must show the parameter in the signature and the merge line.
- `grep -n "schema_sources" src/sqlcg/parsers/snowflake_parser.py` must show the scripting fallback path passing it.
- `grep -n "TODO" src/sqlcg/parsers/base.py src/sqlcg/parsers/ansi_parser.py` must return zero results in the happy path.
- Confirm `_extract_column_lineage` parameter list matches callers in both `ansi_parser.py` and `snowflake_parser.py`.

**Files affected**:
- [`src/sqlcg/lineage/schema_resolver.py`](src/sqlcg/lineage/schema_resolver.py) — add `as_sources_dict()`
- [`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py) — add `schema_sources` parameter and merge logic
- [`src/sqlcg/parsers/ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py) — compute `schema_sources` once per file, pass to `_parse_statement` and `_extract_column_lineage`
- [`src/sqlcg/parsers/snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py) — pass `schema_sources` in scripting fallback path

**Tests to add**:

- **Scenario A — single-file CTE with cross-file source resolves via schema_sources**:
  Setup: `SchemaResolver` with `add_information_schema` loaded (or manually set `_tables`
  for `(None, "IA_SEMANTIC", "Schapbeschikbaarheid")` → `["Rotatie", "Movertype"]`).
  SQL: `CREATE OR REPLACE DYNAMIC TABLE agg AS WITH cte AS (SELECT "Rotatie" FROM IA_SEMANTIC."Schapbeschikbaarheid") SELECT "Rotatie" FROM cte;`
  Action: `parser.parse_file(path, sql)`.
  Assertion: `parsed.statements[0].column_lineage` must be non-empty AND no edge has
  `confidence=0.0` (which would indicate an E5 placeholder).

- **Scenario B — `as_sources_dict()` returns non-empty dict when schema loaded**:
  Setup: `SchemaResolver` with `_tables = {(None, "BA", "orders"): ["id", "amount"]}`.
  Action: `resolver.as_sources_dict()`.
  Assertion: `result["orders"] == "SELECT id, amount FROM BA.orders"` and
  `result["ba.orders"] == "SELECT id, amount FROM BA.orders"`.

- **Scenario C — empty resolver returns empty dict**:
  Setup: `SchemaResolver()` with no tables.
  Action: `resolver.as_sources_dict()`.
  Assertion: `result == {}`.

- **Scenario D — `schema_sources` computed once per file, not once per statement**:
  Setup: spy on `as_sources_dict` call count via `unittest.mock.patch`.
  Action: `parser.parse_file(path, sql)` on a file with 5 statements.
  Assertion: `as_sources_dict` called exactly once, not 5 times.

**Acceptance criteria**:
- `[ ]` `SchemaResolver.as_sources_dict()` is defined in `schema_resolver.py` (grep confirmed)
- `[ ]` `_extract_column_lineage` signature includes `schema_sources` parameter (grep confirmed)
- `[ ]` `schema_sources` is computed once per file in `parse_file`, not per statement
- `[ ]` `schema_sources` reaches the `sg_lineage()` call site via merge with `sources_map`
- `[ ]` Scripting fallback path in `snowflake_parser.py` also passes `schema_sources`
- `[ ]` No `TODO` in any happy path of the changed files
- `[ ]` Scenario A test passes: CTE-derived column lineage is non-empty with schema loaded

---

### T-02 — Best-effort expression name extraction (FIX-E2)

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-E2, § 12.3 C-3 (High severity — 60.4% of columns skipped)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: In [`base.py` lines 532–542](src/sqlcg/parsers/base.py), the column-name
extraction logic is:

```python
if col_expr.alias:
    col_name = col_expr.alias
elif isinstance(col_expr, exp.Column):
    col_name = col_expr.name
    ...
else:
    # Expression with no resolvable name (e.g. ROUND(...), CAST(...))
    # — sg_lineage requires a plain column name, skip these
    continue
```

The `else: continue` silently discards 441 column expressions (60.4% of all SELECT
columns in the 1,445-file corpus). Many of these have a usable name: sqlglot's
`exp.Expr` has `.alias` and `.name` attributes on all node types. For example,
`ROUND(amount, 2)` as an `exp.Anonymous` has `.name == "ROUND"` — not ideal, but
`str(col_expr)[:40]` gives `"ROUND(amount, 2)"` which is a valid `col_name` for
`sg_lineage()` to attempt resolution. If `sg_lineage()` fails on this name, the
existing exception handler (which appends to `out.errors` and emits a `confidence=0.0`
placeholder edge) catches it. We lose nothing over the current `continue`.

**What to do**:

Replace the `else: continue` in `_extract_column_lineage` with a best-effort fallback:

```python
else:
    # Best-effort: try .alias, then .name, then str representation.
    # sg_lineage may still fail; the existing exception handler records it.
    col_name = (
        col_expr.alias
        or (col_expr.name if hasattr(col_expr, "name") else None)
        or str(col_expr)[:40]
    )
    if not col_name or col_name == "*":
        out.errors.append("col_lineage_skip:expr_no_name:<empty>")
        continue
```

After this change, `col_lineage_skip:expr_no_name` errors should record the expression
type for observability. Add one `out.errors.append` with the type:

```python
    out.errors.append(f"col_lineage_attempt:expr_fallback:{type(col_expr).__name__}")
```

This distinguishes fallback attempts (informational) from genuine skips (the `continue`
on `col_name == "*"`).

**Wiring verification**:

- `grep -n "col_lineage_skip:expr_no_name" src/sqlcg/parsers/base.py` must show the
  `col_name == "*"` guard line only — the blanket `continue` must be gone.
- `grep -n "col_lineage_attempt:expr_fallback" src/sqlcg/parsers/base.py` must show
  the new observability append line.
- `grep -n "else:" src/sqlcg/parsers/base.py` — the only `else:` inside
  `_extract_column_lineage` after the `isinstance(col_expr, exp.Column)` check must
  be the fallback block, not a bare `continue`.
- Confirm no `TODO` in the changed block.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py) — replace `else: continue` with fallback

**Tests to add**:

- **Scenario A — unaliased function expression attempts lineage instead of silently skipping**:
  SQL: `CREATE VIEW v AS SELECT ROUND(amount, 2) FROM orders;`
  Action: `parser.parse_file(path, sql)` with ANSI dialect (no schema).
  Assertion: `parsed.errors` must NOT contain `"col_lineage_skip:expr_no_name"` AND
  must contain `"col_lineage_attempt:expr_fallback:Anonymous"` (or whichever type).
  (The edge may be a `confidence=0.0` placeholder if `sg_lineage` fails — that is
  acceptable. What matters is that the attempt is made.)

- **Scenario B — star expressions are still skipped, not attempted**:
  SQL: `CREATE VIEW v AS SELECT * FROM orders;`
  Action: `parser.parse_file(path, sql)`.
  Assertion: `parsed.errors` must contain a `col_lineage_skip:star:` entry and must
  NOT contain `col_lineage_attempt:expr_fallback`.

- **Scenario C — expression with empty name is skipped with record**:
  This tests the `col_name == "*"` guard: create an expression whose fallback resolves
  to `"*"` or empty. Assert that `col_lineage_skip:expr_no_name:<empty>` is in errors.

**Acceptance criteria**:
- `[ ]` The `else: continue` at line 542 of `base.py` is replaced (grep: no bare `continue` inside the `else` branch of the column-name block)
- `[ ]` `col_lineage_attempt:expr_fallback` is appended for every expression that enters the fallback path
- `[ ]` Star expressions (`*`) remain unaffected — `col_lineage_skip:star:` is still emitted
- `[ ]` No `TODO` in the changed block

---

### T-03 — Pure-DDL file early skip + wire `--no-ddl` (FIX-DDL-SKIP)

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-DDL-SKIP, § 12.3 C-4 and C-6 (Medium severity — p99 outliers, E3 noise)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: T-05

**Root cause**: Files like `WTDH_ARTIKEL*.sql` and `COMBI_*.sql` contain hundreds of
`ALTER DYNAMIC TABLE`, `ALTER WAREHOUSE IDENTIFIER(...)`, and similar DDL-only statements.
sqlglot parses these as `exp.Command` nodes. Currently, [`ansi_parser.py` lines 56–113](src/sqlcg/parsers/ansi_parser.py)
sets `ParseQuality.SCRIPTING_FALLBACK` when a `Command` is detected but still builds
scopes and calls `_parse_statement` for all 200–400 statements in the file, driving
p99 latency to 18–21 s per file. 2,507 E3 fallback noise events originate from 5 such files.

Additionally, `index_cmd` in [`index.py` lines 45–47](src/sqlcg/cli/commands/index.py)
has `# TODO: wire no_ddl through to the indexer` — an explicit TODO in the happy path
(`--no-ddl` prints a yellow warning but does nothing). This ticket removes that TODO.

**What to do**:

1. Add `_is_pure_ddl_file(statements: list[Any]) -> bool` as a static method on
   `SqlParser` in [`base.py`](src/sqlcg/parsers/base.py):

```python
@staticmethod
def _is_pure_ddl_file(statements: list[Any]) -> bool:
    """Return True if every non-None statement is a DDL Command or pure DDL node
    with no embedded SELECT/INSERT/MERGE/CTAS body.

    A file is pure-DDL when it will never produce column lineage regardless of
    how many times sg_lineage() is called on it. Such files should be skipped
    early to avoid O(N_statements) scope-build and lineage calls.

    Files that mix DDL and DML (e.g., a changelog with CREATE TABLE followed by
    INSERT) are NOT pure-DDL and must not be skipped.
    """
    import sqlglot.expressions as exp

    has_any_stmt = False
    for stmt in statements:
        if stmt is None:
            continue
        has_any_stmt = True
        # Command = sqlglot fallback for unsupported DDL (e.g. ALTER DYNAMIC TABLE)
        if isinstance(stmt, exp.Command):
            continue
        # Pure DDL creates (no CTAS body)
        if isinstance(stmt, exp.Create) and not isinstance(
            stmt.expression, (exp.Select, exp.Subquery)
        ):
            continue
        # Anything else (SELECT, INSERT, MERGE, CTAS) — file is NOT pure-DDL
        return False
    return has_any_stmt  # must have at least one statement
```

2. In `AnsiParser.parse_file` in [`ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py),
   add an early-exit check immediately after `statements = sqlglot.parse(...)` succeeds:

```python
if self._is_pure_ddl_file(statements):
    out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
    out.errors.append("parse_mode:pure_ddl_skip")
    return out
```

3. In [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py), the scripting
   fallback in `_parse_scripting_file` already returns early for scripting blocks.
   No change needed there — the DDL-skip fires in `AnsiParser.parse_file` which
   `SnowflakeParser.parse_file` delegates to via `AnsiParser.parse_file(self, path, sql)`.

4. Wire `--no-ddl` in [`index.py`](src/sqlcg/cli/commands/index.py): remove the TODO
   comment and the yellow warning. Pass `no_ddl` to `indexer.index_repo()` as a new
   parameter. In `Indexer.index_repo` in [`indexer.py`](src/sqlcg/indexer/indexer.py),
   add `no_ddl: bool = False`. When `no_ddl=True`, skip any `ParsedFile` from
   `_index_single_file` where `parse_quality == ParseQuality.SCRIPTING_FALLBACK` or
   where all statements have `kind in ("CREATE_TABLE",)` and none have `column_lineage`.

   Note: the `_is_pure_ddl_file` check (step 2) fires inside the parser regardless of
   `--no-ddl`. The `no_ddl` flag gives users explicit control over whether DDL-only
   files are indexed into the graph (table nodes without columns). These are separate
   concerns: the DDL-skip prevents `sg_lineage()` calls; `no_ddl` prevents even the
   table-node upserts.

**Wiring verification**:

- `grep -n "_is_pure_ddl_file" src/sqlcg/parsers/base.py` must show the method definition.
- `grep -n "_is_pure_ddl_file" src/sqlcg/parsers/ansi_parser.py` must show the call site.
- `grep -n "pure_ddl_skip" src/sqlcg/parsers/ansi_parser.py` must show the error append.
- `grep -n "TODO" src/sqlcg/cli/commands/index.py` must return zero results (the TODO is removed).
- `grep -n "no_ddl" src/sqlcg/indexer/indexer.py` must show the new parameter.
- `grep -n "no_ddl" src/sqlcg/cli/commands/index.py` must show the wired call to `indexer.index_repo(no_ddl=no_ddl)`.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py) — add `_is_pure_ddl_file` static method
- [`src/sqlcg/parsers/ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py) — add early-exit check after parse
- [`src/sqlcg/cli/commands/index.py`](src/sqlcg/cli/commands/index.py) — remove TODO, wire `no_ddl` to indexer
- [`src/sqlcg/indexer/indexer.py`](src/sqlcg/indexer/indexer.py) — add `no_ddl` parameter

**Tests to add**:

- **Scenario A — pure-DDL file returns early with `parse_mode:pure_ddl_skip`**:
  SQL: `ALTER DYNAMIC TABLE t1 RESUME; ALTER WAREHOUSE IDENTIFIER('WH1') RESUME;`
  (Note: sqlglot parses both as `exp.Command` in Snowflake dialect.)
  Action: `SnowflakeParser(resolver).parse_file(path, sql)`.
  Assertion: `parsed.errors` contains `"parse_mode:pure_ddl_skip"` AND
  `parsed.statements == []` AND `parsed.defined_tables == []`.

- **Scenario B — hybrid DDL+DML file is NOT skipped**:
  SQL: `CREATE TABLE t1 (id INT); INSERT INTO t2 SELECT id FROM t1;`
  Action: `parser.parse_file(path, sql)`.
  Assertion: `parsed.errors` does NOT contain `"parse_mode:pure_ddl_skip"` AND
  `len(parsed.statements) >= 1`.

- **Scenario C — CTAS is NOT treated as pure-DDL**:
  SQL: `CREATE OR REPLACE VIEW v AS SELECT id FROM t1;`
  Action: `parser.parse_file(path, sql)`.
  Assertion: `parsed.errors` does NOT contain `"parse_mode:pure_ddl_skip"`.

- **Scenario D — `--no-ddl` removes table-node upserts for DDL-only files**:
  Integration test: index a single CREATE TABLE file with `no_ddl=True`.
  Assertion: `backend.run_read("MATCH (t:SqlTable) RETURN count(t) AS n", {})[0]["n"] == 0`.

**Acceptance criteria**:
- `[ ]` `_is_pure_ddl_file` defined in `base.py` and called in `ansi_parser.py` (grep confirmed)
- `[ ]` `parse_mode:pure_ddl_skip` is in errors for a pure-Command file (Scenario A passes)
- `[ ]` `--no-ddl` in `index.py` no longer prints a yellow warning and has no TODO (grep: zero TODO in index.py)
- `[ ]` `no_ddl` parameter wired through from `index_cmd` to `indexer.index_repo` (grep confirmed)
- `[ ]` No `TODO` in the happy path of `index.py` or `indexer.py`

---

### T-04 — Snowflake dialect gap workarounds (FIX-E4)

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-E4, § 12.6 Q-12-1 (Medium severity — 6 DML files lose all lineage)
**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: Six DML files fail at `sqlglot.parse()` time (E4 = full parse failure)
due to three distinct sqlglot Snowflake dialect gaps:

1. **IF NOT EXISTS misparse** (2 files: `htwfm_contract_us28254.sql`,
   `htwfm_employee_us28254.sql`): sqlglot misparses Snowflake `CREATE TABLE IF NOT EXISTS`
   as `If()` function call. Error: `Required keyword: 'true' missing for If()`.
   Root: these files use `CREATE TEMP TABLE t IF NOT EXISTS (...)` — a Snowflake-specific
   form that sqlglot's dialect tokenizer does not handle.

2. **Unexpected token in complex DML** (2 files: `us15770-check-controles-herstelactie.sql`,
   and likely `outbound_ga4_artikelen.sql`): `Invalid expression / Unexpected token.`
   at statement boundaries in files with multiple consecutive INSERT+SELECT blocks.

3. **UNPIVOT not fully supported** (1 file: `test.sql`): `Expecting ).` at line 329 in
   a large SELECT with `UNPIVOT` clause.

The approach for each gap:

- **Gap 1 (IF NOT EXISTS)**: Pre-process the SQL string before `sqlglot.parse()` to
  normalise `IF NOT EXISTS` inside `CREATE TEMP TABLE` to a form sqlglot can parse:
  `CREATE TEMPORARY TABLE t (...)` (drop the `IF NOT EXISTS` clause for temp tables).
  Regex: `r'CREATE\s+(TEMP(?:ORARY)?)\s+TABLE\s+(\w+)\s+IF\s+NOT\s+EXISTS'` →
  `r'CREATE \1 TABLE \2'`.

- **Gap 2 (unexpected token)**: These files are likely to have a SQL syntax variant
  that cannot be fixed without structural changes. Strategy: wrap each statement parse
  in try/except independently (sqlglot's `parse()` already does this for lists of
  statements, but some files may have a single-string multi-statement form). Ensure
  `parse_one()` is not used for multi-statement files. This is already handled by
  the current `sqlglot.parse()` call — the gap may be that a specific statement
  boundary fails and sqlglot aborts the whole file. Add a per-statement try/except
  recovery wrapper in `SnowflakeParser._parse_with_recovery()`.

- **Gap 3 (UNPIVOT)**: Pre-process the SQL to strip `UNPIVOT` clauses before parsing.
  Regex: `r'\s+UNPIVOT\s*\([^)]+\)\s+(?:AS\s+\w+)?'` → `''`. This is destructive but
  acceptable: the file is currently a complete lineage failure; stripping UNPIVOT allows
  partial lineage extraction from the rest of the query.

**What to do**:

1. Add `_preprocess_snowflake_sql(sql: str) -> str` as a static method on `SnowflakeParser`
   in [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py). Apply in order:
   - Normalise `CREATE TEMP TABLE t IF NOT EXISTS` (Gap 1 regex)
   - Strip UNPIVOT clauses (Gap 3 regex — only if `UNPIVOT` is present to avoid false matches)

2. Call `_preprocess_snowflake_sql(sql)` at the start of `SnowflakeParser.parse_file`
   before the scripting-block check and before the `AnsiParser.parse_file` delegation:

```python
def parse_file(self, path: Path, sql: str) -> ParsedFile:
    sql = self._preprocess_snowflake_sql(sql)   # Gap 1 + Gap 3
    if self._has_scripting_block(sql):
        ...
```

3. Add `_parse_with_recovery(sql: str, dialect: str) -> list[Any]` as a static method
   that calls `sqlglot.parse()` and, on failure, attempts to split the SQL on `;` and
   re-parse each chunk individually, collecting successful statements and recording errors
   for failed chunks (Gap 2 recovery):

```python
@staticmethod
def _parse_with_recovery(sql: str, dialect: str | None) -> tuple[list[Any], list[str]]:
    """Parse SQL, falling back to per-chunk recovery on tokenisation failure.

    Returns (statements, recovery_errors). recovery_errors is non-empty when
    at least one chunk failed to parse.
    """
    import sqlglot
    try:
        return sqlglot.parse(sql, dialect=dialect), []
    except Exception as primary_exc:
        # Recovery: split on semicolons and parse each chunk independently
        errors = [f"parse_recovery:primary:{primary_exc}"]
        stmts: list[Any] = []
        for chunk in sql.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = sqlglot.parse(chunk, dialect=dialect)
                stmts.extend(s for s in parsed if s is not None)
            except Exception as chunk_exc:
                errors.append(f"parse_recovery:chunk:{chunk_exc}")
        return stmts, errors
```

4. In `AnsiParser.parse_file` (which `SnowflakeParser` delegates to), replace the
   `statements = sqlglot.parse(sql, dialect=self.DIALECT)` call with:

```python
statements, recovery_errors = self._parse_with_recovery(sql, self.DIALECT)
out.errors.extend(recovery_errors)
if recovery_errors:
    out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
```

   Wait — this is `AnsiParser.parse_file`, not `SnowflakeParser`. The recovery should
   only be applied for Snowflake dialect (Gap 2 is Snowflake-specific). Override
   `_parse_with_recovery` in `SnowflakeParser` or call it only from `SnowflakeParser`.

   Cleanest approach: override only in `SnowflakeParser`. In `AnsiParser.parse_file`,
   expose a hook `_do_parse(sql)` that `SnowflakeParser` overrides:

```python
# In AnsiParser:
def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
    import sqlglot
    try:
        return sqlglot.parse(sql, dialect=self.DIALECT), []
    except Exception as exc:
        return [], [f"parse_error:{exc}"]

# In SnowflakeParser:
def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
    return self._parse_with_recovery(sql, self.DIALECT)
```

   Update `AnsiParser.parse_file` to call `self._do_parse(sql)` and handle the
   `(statements, parse_errors)` tuple (the current bare `sqlglot.parse()` call
   is at line 49 — replace it with `_do_parse`).

**Wiring verification**:

- `grep -n "_preprocess_snowflake_sql" src/sqlcg/parsers/snowflake_parser.py` must show
  both definition and call site.
- `grep -n "_preprocess_snowflake_sql" src/sqlcg/parsers/snowflake_parser.py` call site
  must be the FIRST operation in `parse_file`, before `_has_scripting_block`.
- `grep -n "_do_parse" src/sqlcg/parsers/ansi_parser.py` must show definition and call.
- `grep -n "_do_parse" src/sqlcg/parsers/snowflake_parser.py` must show override.
- `grep -n "_parse_with_recovery" src/sqlcg/parsers/snowflake_parser.py` must show definition.
- `grep -n "sqlglot.parse" src/sqlcg/parsers/ansi_parser.py` must return zero results
  after the change (all parses go through `_do_parse`).

**Files affected**:
- [`src/sqlcg/parsers/ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py) — extract `_do_parse` hook, replace raw `sqlglot.parse` call
- [`src/sqlcg/parsers/snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py) — add `_preprocess_snowflake_sql`, `_parse_with_recovery`, override `_do_parse`

**Tests to add**:

- **Scenario A — IF NOT EXISTS normalisation allows parse to succeed**:
  SQL: `CREATE TEMP TABLE t IF NOT EXISTS AS SELECT id FROM src;`
  (This will fail with raw sqlglot Snowflake dialect; must succeed after pre-processing.)
  Action: `SnowflakeParser(resolver).parse_file(path, sql)`.
  Assertion: `parsed.parse_quality != ParseQuality.FAILED` AND
  `parsed.statements` is non-empty.

- **Scenario B — UNPIVOT stripping allows partial lineage extraction**:
  SQL: `SELECT a, b FROM t UNPIVOT (val FOR col IN (x, y)) AS u;`
  Action: `SnowflakeParser(resolver).parse_file(path, sql)`.
  Assertion: `parsed.statements` is non-empty (i.e., file was not a complete failure).

- **Scenario C — recovery parser collects partial statements on chunk failure**:
  Setup: SQL with two valid INSERT statements separated by one invalid chunk.
  Construct: `valid_sql + ";\n INVALID SYNTAX HERE ;\n" + valid_sql`.
  Action: `SnowflakeParser._parse_with_recovery(multi_sql, "snowflake")`.
  Assertion: `len(stmts) >= 2` AND `len(errors) >= 1`.

- **Scenario D — ANSI dialect is unaffected by Snowflake pre-processing**:
  Action: `AnsiParser(resolver).parse_file(path, valid_ansi_sql)`.
  Assertion: `_do_parse` is called and succeeds without recovery errors. `parsed.errors`
  contains no `parse_recovery:` entries.

**Acceptance criteria**:
- `[ ]` `_preprocess_snowflake_sql` defined in `snowflake_parser.py` and called first in `parse_file` (grep confirmed)
- `[ ]` `_do_parse` defined in `ansi_parser.py` with no raw `sqlglot.parse` remaining in `AnsiParser.parse_file` (grep confirmed: zero results for `sqlglot.parse` in `ansi_parser.py`)
- `[ ]` `_do_parse` overridden in `snowflake_parser.py` (grep confirmed)
- `[ ]` Scenario A passes: `IF NOT EXISTS` pre-processing allows parse to succeed
- `[ ]` Scenario C passes: recovery collects at least the valid statements on mixed input
- `[ ]` No `TODO` in any changed file's happy path

---

### T-05 — `scope=` reuse per statement (OPT-SCOPE)

**Source**: ARCHITECTURE_REVIEW.md § 12.4 OPT-SCOPE, § 12.5, Finding R-06 in `plan/parsing_errors_experiment.md` (Low-medium severity — performance)
**Effort**: M
**Depends on**: T-01 (parameter plumbing), T-03 (DDL-skip eliminates the worst outliers first)
**Blocks**: NONE

**Root cause**: `AnsiParser.parse_file` builds `file_scopes` once per statement (line 62–70
of `ansi_parser.py`) and passes the scope to `_parse_statement`. However, `_parse_statement`
passes `scope` only to `_real_tables()` — it does not pass it to `_extract_column_lineage`.
Inside `_extract_column_lineage`, `sg_lineage(col_name, body, ...)` is called once per
column. `sg_lineage()` internally calls `qualify.qualify()` on the SQL body for each
column — this re-parses and re-qualifies the same AST multiple times per statement.

The `scope=` parameter to `sg_lineage()` (confirmed by source inspection in
`plan/parsing_errors_experiment.md` R-06) accepts a pre-built `Scope` from
`sqlglot.optimizer.build_scope()`. Passing `scope=` skips the internal `qualify.qualify()`
call. For a 10-column SELECT, this eliminates 9 redundant qualifications per statement.
Estimated speedup: 40–60% for normal multi-column SELECT files (p50 files).

**What to do**:

1. In `_extract_column_lineage` in [`base.py`](src/sqlcg/parsers/base.py), add a `scope`
   parameter: `scope: Any | None = None`.

2. Pass `scope=scope` to the `sg_lineage()` call:

```python
root = sg_lineage(
    col_name, body,
    schema=schema,
    sources=combined_sources,
    dialect=self.DIALECT,
    scope=scope,          # NEW — reuse pre-built scope across columns
)
```

3. In `_parse_statement` in [`ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py), thread
   the pre-built `scope` through to `_extract_column_lineage`:

```python
extraction = self._extract_column_lineage(
    stmt, path, out, schema,
    dst_table=target,
    sources=sources_map,
    query_sources=sources,
    schema_sources=schema_sources,
    scope=root_scope,          # NEW — pass pre-built scope
)
```

4. In [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py), the scripting
   fallback path calls `AnsiParser._parse_statement` without a pre-built scope. This is
   acceptable — `scope=None` means `sg_lineage` falls back to its internal qualification,
   which is the current behaviour. No change required in the scripting path.

5. Add a note in the docstring of `_extract_column_lineage` explaining that `scope=`
   skips `qualify.qualify()` inside `sg_lineage()` — passing an incorrect scope will
   produce wrong lineage silently, so the scope must be built from the same `body` as
   passed to `sg_lineage`.

**Wiring verification**:

- `grep -n "scope=" src/sqlcg/parsers/base.py` must show: the parameter in the
  `_extract_column_lineage` signature AND the `sg_lineage(... scope=scope)` call.
- `grep -n "scope=root_scope" src/sqlcg/parsers/ansi_parser.py` must show the call
  in `_parse_statement`.
- `grep -n "scope=" src/sqlcg/parsers/snowflake_parser.py` — scripting fallback must
  NOT pass scope (confirm by absence of `scope=` in `_parse_scripting_file`).
- `grep -n "build_scope" src/sqlcg/parsers/ansi_parser.py` must show it is called once
  per statement in `parse_file`, not inside `_extract_column_lineage`.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py) — add `scope` param, pass to `sg_lineage`
- [`src/sqlcg/parsers/ansi_parser.py`](src/sqlcg/parsers/ansi_parser.py) — pass `scope=root_scope` to `_extract_column_lineage`

**Tests to add**:

- **Scenario A — scope reuse produces identical edges to no-scope call**:
  SQL: `CREATE VIEW v AS SELECT a, b, c FROM orders;`
  Action A: parse with default path (scope=None or scope not passed).
  Action B: parse after monkey-patching `_extract_column_lineage` to assert `scope` is
  non-None.
  Assertion: both produce the same `column_lineage` edges (list equality on
  `[(src.name, dst.name, transform)]` tuples).

- **Scenario B — benchmark guard: multi-column SELECT is faster with scope reuse**:
  SQL: 20-column SELECT with 3 CTEs.
  Action: time `parser.parse_file(path, sql)` 10 iterations before and after the change.
  Note: this scenario is informational; do not assert a time threshold (CI machines vary).
  Assert only that `column_lineage` is non-empty and identical edge count in both cases.

**Acceptance criteria**:
- `[ ]` `scope` parameter in `_extract_column_lineage` signature (grep confirmed)
- `[ ]` `scope=scope` passed to `sg_lineage()` call (grep confirmed)
- `[ ]` `scope=root_scope` in `_parse_statement` call to `_extract_column_lineage` (grep confirmed)
- `[ ]` `build_scope` is NOT called inside `_extract_column_lineage` — it remains in `parse_file` only (grep: zero results for `build_scope` in `base.py`)
- `[ ]` Scenario A passes: edges are identical with and without scope reuse
- `[ ]` No `TODO` in the changed methods

---

## Test Strategy

### The Single Most Important Regression Guard

This test must be added to `tests/integration/` and must never be marked `xfail`.
It asserts that the E5 fix (T-01) actually produces column lineage edges for a
`CREATE OR REPLACE DYNAMIC TABLE` file with cross-file CTE sources and an
information schema loaded — the exact pattern responsible for 1,280 errors.

```python
# tests/integration/test_e5_regression_guard.py

"""Regression guard for FIX-E5: sg_lineage() must resolve CTE-derived columns
when the information schema is loaded as sources=.

Guards against a reversion of T-01 (sprint_06_lineage_coverage.md), where
sg_lineage() was called without schema_sources= and produced 1,280 E5 errors
('Cannot find column' on CTE-derived columns in CREATE DYNAMIC TABLE files).
"""

import io
import csv
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def _write_schema_csv(tmp_path, rows: list[dict]) -> str:
    """Write a minimal INFORMATION_SCHEMA.COLUMNS CSV and return its path."""
    path = tmp_path / "schema.csv"
    fieldnames = ["TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_cte_cross_file_column_lineage_resolves_with_schema(tmp_path):
    """COLUMN_LINEAGE edges must be non-zero after indexing a CREATE DYNAMIC TABLE
    whose CTE sources are resolved via the information schema.

    Without T-01, sg_lineage() raises 'Cannot find column' for CTE-derived columns
    because qualify() cannot expand cross-file CTE sources without sources=.
    This test guards against reversion of that fix.

    Failure message: 'E5 regression: CTE-derived column lineage must be non-zero
    when information schema is loaded — guards against sprint_06 T-01 regression.'
    """
    # Minimal information schema: IA_SEMANTIC.Schapbeschikbaarheid has two columns
    schema_csv = _write_schema_csv(tmp_path, [
        {
            "TABLE_CATALOG": "DWH", "TABLE_SCHEMA": "IA_SEMANTIC",
            "TABLE_NAME": "Schapbeschikbaarheid", "COLUMN_NAME": "Rotatie",
            "ORDINAL_POSITION": "1",
        },
        {
            "TABLE_CATALOG": "DWH", "TABLE_SCHEMA": "IA_SEMANTIC",
            "TABLE_NAME": "Schapbeschikbaarheid", "COLUMN_NAME": "Movertype",
            "ORDINAL_POSITION": "2",
        },
    ])

    # SQL that mirrors the E5-triggering pattern from the corpus:
    # CREATE DYNAMIC TABLE with CTE sourcing from a cross-file semantic view.
    sql = """
    CREATE OR REPLACE DYNAMIC TABLE agg_besteladviezen AS
    WITH movertype_per_dag AS (
        SELECT
            CASE WHEN AVG("Rotatie") >= 0.5 THEN 'Fast-mover' ELSE 'Slow-mover' END AS "Movertype"
        FROM IA_SEMANTIC."Schapbeschikbaarheid"
    )
    SELECT mvt."Movertype"
    FROM movertype_per_dag AS mvt;
    """
    (tmp_path / "agg.sql").write_text(sql)

    backend = KuzuBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect="snowflake", db=backend,
        schema_csv=tmp_path / "schema.csv",
    )

    rows = backend.run_read(
        "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN COUNT(e) AS cnt",
        {},
    )
    cnt = rows[0]["cnt"] if rows else 0
    assert cnt > 0, (
        f"E5 regression: CTE-derived column lineage must be non-zero when information "
        f"schema is loaded — guards against sprint_06 T-01 regression. "
        f"Index summary: {summary}"
    )
```

**Which ticket makes it pass**: T-01.

---

## Wiring Checklist

| Question | T-01 | T-02 | T-03 | T-04 | T-05 |
|----------|------|------|------|------|------|
| What calls this? | `_extract_column_lineage` calls `sg_lineage` with merged `combined_sources`; `ansi_parser._parse_statement` calls `_extract_column_lineage` with `schema_sources`; `schema_sources` comes from `self._schema.as_sources_dict()` | `_extract_column_lineage` fallback branch calls `sg_lineage` with the best-effort `col_name` | `AnsiParser.parse_file` calls `_is_pure_ddl_file` immediately after `_do_parse`; `index_cmd` calls `indexer.index_repo(no_ddl=no_ddl)` | `SnowflakeParser.parse_file` calls `_preprocess_snowflake_sql` first, then delegates to `AnsiParser.parse_file`; `AnsiParser.parse_file` calls `self._do_parse` which is overridden in `SnowflakeParser` | `_parse_statement` in `ansi_parser.py` passes `scope=root_scope` to `_extract_column_lineage`; `_extract_column_lineage` passes `scope=scope` to `sg_lineage` |
| Where is the callback/parameter passed? | `as_sources_dict()` → `schema_sources` local in `parse_file` → passed to `_parse_statement(schema_sources=...)` → passed to `_extract_column_lineage(schema_sources=...)` → merged into `combined_sources` → `sg_lineage(sources=combined_sources)` | No new callback; the fallback block is in the existing `for col_expr in col_expressions` loop in `_extract_column_lineage` | `no_ddl` is a Typer option in `index_cmd` → `indexer.index_repo(no_ddl=no_ddl)` → controls whether DDL-only parsed files are upserted | `sql` string pre-processed in `SnowflakeParser.parse_file` before any other operation; `_do_parse` hook called in `AnsiParser.parse_file` instead of raw `sqlglot.parse` | `root_scope` is already built in `parse_file`'s scope loop → threaded into `_parse_statement` signature → threaded into `_extract_column_lineage` signature → `sg_lineage(scope=scope)` |
| What constant/path does this align with? | `SchemaResolver.as_sources_dict()` uses the same `_tables` dict populated by `add_information_schema` (same CSV loaded by `_load_schema_into_graph` in `load_schema.py` and auto-discovered from `<path>/.sqlcg/schema.csv`) | N/A — no constants | `ParseQuality.SCRIPTING_FALLBACK` is the quality set on early-exit; `no_ddl` aligns with the existing CLI flag name in `index_cmd` | `SnowflakeParser.DIALECT = "snowflake"` must match the dialect string passed to `_parse_with_recovery` | `build_scope` call location must remain in `parse_file`, not migrate to `_extract_column_lineage` |
| Does any TODO remain in the happy path? | No — T-01 introduces no TODO | No | No — the `# TODO: wire no_ddl` in `index.py` line 45 must be removed as part of this ticket | No | No |

---

## Acceptance Criteria (sprint-level)

- `[ ]` After `sqlcg index` on a Snowflake corpus with `columns.csv` loaded, the count of
  `COLUMN_LINEAGE` edges is strictly greater than the pre-sprint baseline of 24 edges on
  the 1,445-file DWH corpus (measurable via `sqlcg db info` or Cypher query).
- `[ ]` A single `CREATE OR REPLACE DYNAMIC TABLE` file with multi-level CTEs sourcing from
  `IA_SEMANTIC.*` views produces at least 1 `COLUMN_LINEAGE` edge when `columns.csv` is
  provided (regression guard test passes — `test_e5_regression_guard.py`).
- `[ ]` Indexing a file containing only `ALTER DYNAMIC TABLE` and `ALTER WAREHOUSE` statements
  completes in under 500 ms (measured by the unit test `test_pure_ddl_returns_early`).
- `[ ]` `sqlcg index <dir> --dialect snowflake` on the DWH corpus no longer prints the
  `--no-ddl is not yet fully implemented` warning.
- `[ ]` `sqlcg index <dir> --no-ddl --dialect snowflake` suppresses table-node upserts for
  files containing only DDL statements (verifiable by querying `MATCH (t:SqlTable) RETURN count(t)` before and after indexing a DDL-only directory).
- `[ ]` No `TODO` comment remains in the happy path of `index.py`, `indexer.py`,
  `ansi_parser.py`, `snowflake_parser.py`, or `base.py`.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `sg_lineage(scope=)` with pre-built scope produces wrong edges when scope and body diverge | LOW — scope is always built from the same `stmt` as `body` in the current flow | Add Scenario A test (T-05) verifying edge identity with and without scope; catch at review |
| `as_sources_dict()` emits too many keys and pollutes `sources=`, causing `qualify()` to resolve columns to wrong tables | MEDIUM — the `sources=` dict is matched by scope name; if a CTE alias happens to collide with a table name in the schema dict, wrong resolution could occur | Limit `as_sources_dict()` to tables with at least 1 column; document that keys are table names (last component), not fully qualified names. For the E5 pattern (CTE name is the table alias, not the schema.table), the schema.table key form in the dict is what `qualify()` needs |
| `_preprocess_snowflake_sql` regex strips valid SQL that happens to match `IF NOT EXISTS` pattern | LOW — the regex is anchored to `CREATE TEMP TABLE ... IF NOT EXISTS` | Unit test with valid non-TEMP `CREATE TABLE IF NOT EXISTS` to confirm it is unaffected |
| `_parse_with_recovery` splits on `;` inside string literals, producing invalid chunks | MEDIUM — string-safe splitting is complex | Recovery errors are non-fatal: failed chunks are logged to `out.errors` and skipped. The primary parse (no split) is tried first; recovery only fires on primary failure |
| DDL-skip fires incorrectly on a file that has a CTAS after some DDL | LOW — `_is_pure_ddl_file` returns False as soon as one non-DDL statement is found | Scenario B and C unit tests confirm this |
| `schema_sources` dict size (one entry per column × tables) causes memory pressure on 144k-row INFORMATION_SCHEMA | MEDIUM — 144k columns across ~5k tables ≈ 5k `SELECT ...` strings in memory | Profile peak size. If > 50 MB, make `as_sources_dict()` lazy (build on demand per CTE name). Document decision |
