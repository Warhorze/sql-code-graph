# Fix: Snowflake Dynamic Table Parsing Failures

**Plan file**: `plan/fix_dynamic_table_parsing.md`
**Branch**: `feat/fix-schema-case-mismatch` (extends current branch; new branch acceptable if Fix B is split off)
**Date**: 2026-05-12
**Status**: PLAN CREATED — awaiting plan-reviewer

This plan addresses two distinct dynamic-table failure modes observed on the DWH
e2e corpus after the schema case-mismatch fixes (`plan/fix_schema_case_mismatch.md`)
landed.

| Fix | Priority | Implementation order |
|-----|----------|----------------------|
| Fix A — Strip `WITH TAG (...)` in `_preprocess_snowflake_sql` | HIGH | Implement now |
| Fix B — Table-level lineage fallback when `qualify()` fails or `cached_scope is None` | LOWER | Design only; implement after Fix A e2e validation |

---

## Problem Statement — Fix A (WITH TAG in column definitions)

Snowflake `CREATE OR REPLACE DYNAMIC TABLE` files in `ddl/changelogs/IA-DATAPRODUCTS/`
attach object tags to columns:

```sql
CREATE OR REPLACE DYNAMIC TABLE foo (
    "Week koppelcode" WITH TAG (IA_DATAPRODUCTS."Semantic role"='dimension') COMMENT 'Week koppelcode',
    "col2" COMMENT 'bar',
    ...
) TARGET_LAG = '1 hour' AS SELECT ...
```

The Snowflake dialect of sqlglot (vendored version in this repo) does not understand
`WITH TAG (...)` inside a column definition. On a 368-line file
(`COMBI_WEEK_ARTIKELGROEP_BOUWMARKT.sql`) the parser stalls/hits the 30 s timeout
guard, so the entire IA-DATAPRODUCTS batch is dropped before the `AS SELECT` body
is even examined. The body contains column lineage we want.

The existing `_preprocess_snowflake_sql` (file
[`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py)
lines 73–110) already removes `UNPIVOT (...) AS alias` clauses with a regex that
handles one level of paren nesting. A similar but slightly stronger regex can strip
`WITH TAG (...)` before sqlglot sees it. Doing so loses the tag metadata (we don't
model it anyway) but recovers full column-level lineage on the body.

**Key constraint**: `WITH TAG (...)` contains nested parens
(`WITH TAG (schema."tag"='value')` — one inner pair from the schema-qualified
identifier in some variants and another from any function-call value). The
regex must tolerate **at least two** levels of nesting in the argument list.
Multiple tags chained inside one `WITH TAG (a=..., b=...)` are still a single
parenthesised group.

A single column definition may also carry **multiple** `WITH TAG` clauses or
multiple separate clauses (e.g. `WITH TAG (...) WITH MASKING POLICY ...`). The
fix only strips `WITH TAG (...)` and leaves any other column clauses
(`COMMENT '…'`, `WITH MASKING POLICY …`) untouched — those parse fine.

---

## Problem Statement — Fix B (table-level fallback when column lineage is impossible)

`_extract_column_lineage` in
[`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) builds a qualified
body + cached scope in a single `try` (lines 637–666). When `qualify()` raises
(E5 pattern: CTE referencing a cross-file source like
`IA_SEMANTIC."Schapbeschikbaarheid"`) the code records
`col_lineage_skip:qualify_failed:<ExcName>` and sets `cached_scope = None`.
`sg_lineage` is still tried per-column but typically also fails or returns no
edges, and the file ends with **zero** `COLUMN_LINEAGE` edges even though we
can see the `FROM`/`JOIN` table names plainly in the AST.

The fix is to walk the body's `FROM`/`JOIN` nodes when column lineage is
unrecoverable and emit table-level lineage edges at lower confidence (0.4) so
downstream lineage tools have at least the table graph. This is a strictly
additive recovery path — it does not change behaviour when `qualify()` succeeds.

---

## Non-Goals

- Fix A does **not** preserve `WITH TAG` metadata anywhere. Tag values are
  stripped and discarded. (No graph model exists for column tags.)
- Fix A does **not** strip `WITH MASKING POLICY ...` or `COMMENT '...'`. Those
  parse correctly in sqlglot today; touching them risks regression.
- Fix B does **not** attempt column-level lineage when `qualify()` fails. We do
  not try to walk the un-qualified AST per column.
- Fix B does **not** change the existing `SELECTS_FROM` table-level edges
  (those are query → table; this fix produces column → column edges marked as
  table-resolution).
- No backward compatibility shims. Re-index is the migration path (per
  CLAUDE.md project rule).

---

## Verification of Code References

| Reference | File | Line(s) | Verified |
|-----------|------|---------|----------|
| `_preprocess_snowflake_sql` | `src/sqlcg/parsers/snowflake_parser.py` | 73–110 | yes |
| UNPIVOT stripper regex | `src/sqlcg/parsers/snowflake_parser.py` | 96–108 | yes |
| `_extract_column_lineage` | `src/sqlcg/parsers/base.py` | 512–onwards | yes |
| `qualify()` + `cached_scope` block | `src/sqlcg/parsers/base.py` | 637–666 | yes |
| `col_lineage_skip:qualify_failed:` marker | `src/sqlcg/parsers/base.py` | 664 | yes |
| `LineageEdge` dataclass | `src/sqlcg/parsers/base.py` | 98–130 | yes |
| `COLUMN_LINEAGE` REL schema | `src/sqlcg/core/schema.cypher` | 90–95 | yes (columns: `transform STRING, confidence FLOAT, query_id STRING`) |
| Existing preprocess unit tests | `tests/unit/test_sprint_06_t04_t05.py` | `TestT04SnowflakePreprocessingUnit` (line 110+) | yes |

The `COLUMN_LINEAGE` rel currently has **no** `resolution` field. Fix B
**deliberately reuses the existing `transform` column** (a `STRING`) to carry a
new marker value `"TABLE_LEVEL_FALLBACK"`, paired with `confidence=0.4`. This
avoids a schema migration and keeps query-side compatibility.

---

# Fix A — Strip `WITH TAG (...)` in `_preprocess_snowflake_sql`

## File and Location

**File**: [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py)
**Method**: `SnowflakeParser._preprocess_snowflake_sql` (static, lines 73–110)
**Insertion point**: After the existing UNPIVOT strip block (after line 108),
before the final `return sql` at line 110.

## Code Change

Update the method docstring to mention `WITH TAG` stripping and add the new
block:

```python
        # Gap 4: Strip "WITH TAG (...)" clauses from column definitions in
        # CREATE [OR REPLACE] [DYNAMIC] TABLE statements. sqlglot's Snowflake
        # dialect does not parse this clause and stalls past the 30s timeout
        # on real DWH dynamic-table DDL. Stripping the clause loses only the
        # tag metadata (we do not model it) and unblocks parsing of the AS
        # SELECT body for column lineage.
        #
        # WITH TAG may contain up to two levels of nested parens, e.g.
        #   WITH TAG (SCHEMA."tag_name"='value', other='v2')
        # A column may carry multiple WITH TAG clauses (chained). Strip each
        # occurrence independently; do not touch COMMENT or WITH MASKING
        # POLICY clauses, which sqlglot already handles.
        if "WITH TAG" in sql.upper():
            sql = re.sub(
                r"\s+WITH\s+TAG\s*\((?:[^()]*|\([^()]*\))*\)",
                "",
                sql,
                flags=re.IGNORECASE,
            )
```

### Regex behaviour and limits

`\(   (?: [^()]* | \([^()]*\) )*  \)` matches a top-level paren group whose
body contains arbitrary non-paren content interleaved with single-nested
paren groups. This handles:

- `WITH TAG (a='b')` — no nesting (0 levels inside)
- `WITH TAG (SCHEMA."tag"='value')` — quoted identifiers, no real nesting
- `WITH TAG (FUNC(x)='y')` — one level of nested parens
- `WITH TAG (a=FUNC(x), b=FUNC(y))` — multiple sibling nested groups

It does **not** handle a hypothetical three-level case
(`WITH TAG (FUNC(NESTED(x)))`). If a future corpus shows that pattern, replace
the regex with a paren-counting helper (compare the same trade-off the existing
UNPIVOT stripper makes). Record the assumption in code comment.

### Chained occurrences

`re.sub(..., sql)` replaces **all** non-overlapping matches by default, so
`"x" WITH TAG (...) WITH TAG (...)` collapses to `"x"` in one pass.

### Why a leading `\s+` is required

Matches the same convention as the UNPIVOT stripper at line 104. Prevents the
regex from matching inside a larger identifier like `BLAHWITH TAG`. The DDL we
care about always has whitespace before `WITH TAG`.

---

## Acceptance Criteria — Fix A

| ID | Criterion |
|----|-----------|
| A-AC-1 | `_preprocess_snowflake_sql` removes `WITH TAG (IA_DATAPRODUCTS."Semantic role"='dimension')` from a simple column definition, leaving the rest of the SQL byte-equal modulo whitespace |
| A-AC-2 | A column carrying both `WITH TAG (…)` and `COMMENT 'foo'` retains the `COMMENT 'foo'` clause |
| A-AC-3 | Two chained `WITH TAG (...)` clauses on the same column are both removed in one pass |
| A-AC-4 | A `WITH TAG (FUNC(x)='y')` value with one level of nested parens is fully removed |
| A-AC-5 | Input SQL containing no `WITH TAG` substring is returned unchanged (string-equal) |
| A-AC-6 | A `CREATE OR REPLACE DYNAMIC TABLE` DDL with several quoted column names + `WITH TAG (...) COMMENT '...'` per column parses successfully through `SnowflakeParser.parse_file` and produces at least one `QueryNode` with `kind == "CREATE_TABLE"` and a non-empty `column_lineage` list extracted from the trailing `AS SELECT` body |
| A-AC-7 | `WITH MASKING POLICY foo` is **not** stripped (regression guard for adjacent column clauses) |

---

## Tests Required — Fix A

Add a new unit test class to
[`tests/unit/test_sprint_06_t04_t05.py`](../tests/unit/test_sprint_06_t04_t05.py)
alongside the existing `TestT04SnowflakePreprocessingUnit` class. (Same file
is the convention for preprocessing tests; do not create a new file.) Each
test corresponds to a single A-AC ID.

```python
class TestWithTagStripping:
    """Unit tests for Fix A — WITH TAG (...) preprocessing."""

    def test_preprocess_with_tag_simple_removal(self):
        # A-AC-1
        from sqlcg.parsers.snowflake_parser import SnowflakeParser
        sql = (
            'CREATE OR REPLACE DYNAMIC TABLE foo (\n'
            '    "Week koppelcode" WITH TAG (IA_DATAPRODUCTS."Semantic role"=\'dimension\')\n'
            ') TARGET_LAG = \'1 hour\' AS SELECT 1;'
        )
        result = SnowflakeParser._preprocess_snowflake_sql(sql)
        assert "WITH TAG" not in result.upper()
        assert '"Week koppelcode"' in result
        assert "TARGET_LAG" in result
        assert "AS SELECT 1" in result

    def test_preprocess_with_tag_preserves_comment(self):
        # A-AC-2
        ...
        # Assert that COMMENT 'foo' remains after WITH TAG is stripped.

    def test_preprocess_with_tag_chained(self):
        # A-AC-3
        sql = '... "col" WITH TAG (a=\'b\') WITH TAG (c=\'d\') COMMENT \'k\' ...'
        result = SnowflakeParser._preprocess_snowflake_sql(sql)
        assert "WITH TAG" not in result.upper()
        assert "COMMENT 'k'" in result

    def test_preprocess_with_tag_nested_function_value(self):
        # A-AC-4
        sql = '... "col" WITH TAG (NS."t"=FUNC(\'x\')) ...'
        result = SnowflakeParser._preprocess_snowflake_sql(sql)
        assert "WITH TAG" not in result.upper()
        # The function call value is gone with the clause.
        assert "FUNC" not in result.upper()

    def test_preprocess_no_with_tag_unchanged(self):
        # A-AC-5
        sql = "SELECT a, b FROM t WHERE c > 0;"
        result = SnowflakeParser._preprocess_snowflake_sql(sql)
        assert result == sql

    def test_preprocess_with_masking_policy_preserved(self):
        # A-AC-7 (regression guard)
        sql = '... "col" WITH MASKING POLICY mp WITH TAG (a=\'b\') ...'
        result = SnowflakeParser._preprocess_snowflake_sql(sql)
        assert "WITH TAG" not in result.upper()
        assert "WITH MASKING POLICY" in result.upper()
```

Add an integration-style assertion in
[`tests/unit/test_sprint_06_t04_t05.py`](../tests/unit/test_sprint_06_t04_t05.py)
(can live in the same `TestWithTagStripping` class — uses the full
`SnowflakeParser.parse_file` path):

```python
    def test_dynamic_table_with_tag_yields_column_lineage(self):
        # A-AC-6 — observable output check (not just "no exception")
        from pathlib import Path
        from sqlcg.lineage.schema_resolver import SchemaResolver
        from sqlcg.parsers.snowflake_parser import SnowflakeParser

        sql = (
            'CREATE OR REPLACE DYNAMIC TABLE OUT_TBL (\n'
            '    "Week koppelcode" WITH TAG (NS."r"=\'dim\') COMMENT \'Week\',\n'
            '    "Amount" COMMENT \'amount\'\n'
            ') TARGET_LAG = \'1 hour\' AS\n'
            'SELECT a AS "Week koppelcode", b AS "Amount" FROM src;'
        )
        parser = SnowflakeParser(SchemaResolver())
        result = parser.parse_file(Path("dyn.sql"), sql)
        assert len(result.statements) >= 1
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        # The body produces at least one column lineage edge.
        assert len(stmt.column_lineage) >= 1
```

**Why this assertion matters** (per project rule "Tests must assert observable
output"): it would *not* pass under the current code — the pre-process hangs
or the parse fails — so reverting Fix A flips this test red. The unit tests
above check the regex in isolation; this one verifies the wiring through
`parse_file`.

---

## Wiring Checklist — Fix A

| Item | Evidence required |
|------|-------------------|
| New regex block lives **after** the UNPIVOT block and **before** `return sql` | grep on file: order of `WITH TAG` and `UNPIVOT` substrings |
| Regex uses `re.IGNORECASE` (matches lowercase `with tag` too) | inspection |
| `"WITH TAG" in sql.upper()` guard present (match the UNPIVOT guard style) | inspection |
| Method docstring lists "Gap 4: Strip WITH TAG (...)" | grep for `Gap 4` |
| All seven Fix A unit tests are added and pass under `uv run pytest tests/unit/test_sprint_06_t04_t05.py -x` | test runner |
| No existing test in `tests/unit/test_sprint_06_t04_t05.py` regresses | full file run green |
| `uv run pyright` passes with zero new errors | type checker |
| `uv run ruff check src tests` passes | linter |

---

# Fix B — Table-Level Lineage Fallback on Qualify Failure (Design Only)

> **Implement after Fix A e2e validation.** Fix A may by itself raise the
> column lineage hit-rate enough that Fix B's threshold of "value vs.
> complexity" changes. Before opening a Fix B PR, re-run the DWH e2e corpus
> and re-count how many files still emit zero `COLUMN_LINEAGE` edges. If Fix
> A alone reduces that count by more than 50 %, downgrade Fix B priority
> further.

## File and Location

**File**: [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)
**Method**: `_extract_column_lineage`, the `try/except` block at lines 637–666

## Design

### 1. New helper: `_emit_table_level_fallback_edges`

A new private method on `SqlParser` that walks the **raw** (pre-`qualify`)
body, collects `exp.Table` references from `FROM` and `JOIN` clauses, and
emits one synthetic `LineageEdge` per source table.

Signature:

```python
def _emit_table_level_fallback_edges(
    self,
    body: Any,
    dst_table: "TableRef | None",
    path: Path,
    out: ParsedFile,
) -> list[LineageEdge]:
    """Walk FROM/JOIN of the raw body and emit table-level fallback edges.

    Used when column lineage is unrecoverable (qualify() failed, or
    cached_scope is None). Each source table contributes a single
    LineageEdge with src.name == "<table>", dst.name == "<table>",
    confidence = 0.4, transform = "TABLE_LEVEL_FALLBACK".

    The src and dst column names are both set to the sentinel "*" to
    indicate "all columns of this table feed all columns of the destination
    table at table granularity". This sentinel is consumed by the lineage
    aggregator and presented as a table-resolution edge.

    Returns an empty list when no real tables are found.
    """
```

Walking strategy (use existing helper to stay consistent):

```python
# Reuse _real_tables() if a scope is available; otherwise walk exp.Table
# nodes directly under exp.From / exp.Join. _fallback_table_scan() at
# ansi_parser.py:205 already does exactly this — call into it (or its
# equivalent in base.py) rather than reimplementing.
```

**Action item for the developer**: grep for `_fallback_table_scan` and
confirm whether it lives on `AnsiParser` or `SqlParser`. The new helper
should reuse it. If it is on `AnsiParser` only, lift it to `SqlParser` (or
call `self._fallback_table_scan(body)` since `SnowflakeParser(AnsiParser)`
inherits it). Document the call site.

### 2. Edge shape

| Field | Value |
|-------|-------|
| `src` | `ColumnRef(src_table_ref, "*")` where `src_table_ref` is the FROM/JOIN table |
| `dst` | `ColumnRef(dst_table or TableRef(name="<output>"), "*")` |
| `transform` | `"TABLE_LEVEL_FALLBACK"` (new constant; document below) |
| `confidence` | `0.4` (new tier; document below) |
| `query_id` | `None` (set by upstream code if needed) |

The `"*"` sentinel column name is intentional. It is the same sentinel the
star expansion already uses; downstream consumers either materialise it via
the schema or treat it as table-resolution.

### 3. Confidence tier table (record in code as a module-level docstring)

| Tier | Value | Meaning |
|------|-------|---------|
| Schema-backed | 1.0 | Source table in `mapping_schema` |
| Inferred | 0.7 | Source table not in `mapping_schema` (existing behaviour) |
| Schema-validation failed | 0.5 | Column not found in schema (existing behaviour at base.py line 764) |
| **Table-level fallback** | **0.4** | **NEW — qualify() failed, only table granularity recovered** |
| Lineage exception | 0.0 | sg_lineage raised (existing behaviour at base.py line 808) |

### 4. Wiring inside `_extract_column_lineage`

Replace the existing `except` block (currently lines 660–666):

```python
            except Exception as exc:
                out.errors.append(f"col_lineage_skip:qualify_failed:{type(exc).__name__}")
                qualified_body = body
                cached_scope = None
```

with:

```python
            except Exception as exc:
                out.errors.append(f"col_lineage_skip:qualify_failed:{type(exc).__name__}")
                qualified_body = body
                cached_scope = None
                # Fix B: emit table-level fallback edges so the file is not a
                # zero-edge outcome. These are additive — the per-column
                # sg_lineage attempts below may still succeed for some columns,
                # in which case both sets of edges coexist (with different
                # confidence tiers).
                fallback_edges = self._emit_table_level_fallback_edges(
                    body=body,
                    dst_table=dst_table,
                    path=path,
                    out=out,
                )
                edges.extend(fallback_edges)
                if fallback_edges:
                    out.errors.append(
                        f"col_lineage_fallback:table_level:{len(fallback_edges)}"
                    )
```

**Do not** also emit fallback edges when `cached_scope is None` but `qualify()`
did **not** raise — that path can still succeed via `sg_lineage`'s internal
qualifier. Only the explicit `except` is the fallback trigger. (This contracts
the original brief slightly; rationale documented above.)

### 5. Schema compatibility

The `COLUMN_LINEAGE` rel table
[`src/sqlcg/core/schema.cypher`](../src/sqlcg/core/schema.cypher) lines 90–95
has columns `transform STRING, confidence FLOAT, query_id STRING`. No
migration is needed. The new `transform="TABLE_LEVEL_FALLBACK"` value is just
a new string constant. Downstream queries that filter by transform (e.g.
`STAR_EXPANSION` count at line 72 of `queries.cypher`) are unaffected.

### 6. Constant placement

Add a module-level constant in
[`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) near the top
(after imports, before `TableRef`):

```python
# Lineage transform constants.
TRANSFORM_SELECT = "SELECT"
TRANSFORM_TABLE_LEVEL_FALLBACK = "TABLE_LEVEL_FALLBACK"
TRANSFORM_UNKNOWN = "UNKNOWN"

# Confidence tiers (see plan/fix_dynamic_table_parsing.md Fix B §3).
CONFIDENCE_SCHEMA_BACKED = 1.0
CONFIDENCE_INFERRED = 0.7
CONFIDENCE_SCHEMA_VALIDATION_FAILED = 0.5
CONFIDENCE_TABLE_LEVEL_FALLBACK = 0.4
CONFIDENCE_LINEAGE_EXCEPTION = 0.0
```

The developer **does not need** to refactor all existing literals to use
these constants in the same PR — that is a churn-only change. Use the
constants only in the new fallback path. Existing literals can be migrated
in a follow-up sweep (or never).

---

## Acceptance Criteria — Fix B

| ID | Criterion |
|----|-----------|
| B-AC-1 | A `CREATE TABLE … AS SELECT` whose body references a cross-file CTE source that triggers `qualify_failed` produces at least one `LineageEdge` per source table with `transform == "TABLE_LEVEL_FALLBACK"` and `confidence == 0.4` |
| B-AC-2 | The same statement still records `col_lineage_skip:qualify_failed:<ExcName>` in `ParsedFile.errors` (existing behaviour preserved) |
| B-AC-3 | The same statement additionally records `col_lineage_fallback:table_level:<N>` where N is the number of fallback edges emitted |
| B-AC-4 | When `qualify()` succeeds, no `TABLE_LEVEL_FALLBACK` edge is emitted (no regression for the happy path) |
| B-AC-5 | When `qualify()` fails and the body has no real tables (only literals or CTEs), no fallback edge is emitted and no `col_lineage_fallback` error is recorded — the existing zero-edge behaviour is preserved |
| B-AC-6 | A fallback edge's `dst` column is `ColumnRef(dst_table or TableRef(name="<output>"), "*")` and `src` column is `ColumnRef(<src_table>, "*")` |
| B-AC-7 | After indexing, `COLUMN_LINEAGE` edges with `transform = 'TABLE_LEVEL_FALLBACK'` are queryable via the existing Kuzu schema (no schema migration required) |

---

## Tests Required — Fix B

Create a new test file
`tests/unit/test_table_level_fallback.py` (follow the pattern of
`tests/unit/test_T09_01_qualify_once.py` — direct method tests on
`SqlParser`/`AnsiParser` with a tiny SchemaResolver fixture).

Test ordering and IDs:

| Test name | AC mapped |
|-----------|-----------|
| `test_qualify_failure_emits_table_level_fallback_edges` | B-AC-1, B-AC-3 |
| `test_qualify_failure_preserves_existing_error_marker` | B-AC-2 |
| `test_qualify_success_does_not_emit_fallback_edges` | B-AC-4 |
| `test_qualify_failure_with_no_real_tables_emits_no_fallback` | B-AC-5 |
| `test_table_level_fallback_edge_shape` | B-AC-6 |

Use `monkeypatch` to force `qualify` to raise for B-AC-1/2/3/5/6:

```python
def test_qualify_failure_emits_table_level_fallback_edges(monkeypatch):
    from sqlcg.parsers import base as base_mod

    def raise_qualify(*a, **kw):
        raise ValueError("forced for test")

    monkeypatch.setattr(base_mod, "qualify", raise_qualify)
    # ... build a parser, parse `CREATE TABLE x AS SELECT a FROM src`,
    # assert at least one edge with transform == "TABLE_LEVEL_FALLBACK"
    # and confidence == 0.4 referencing src.
```

This is the pattern `test_T09_01_qualify_once.py` already uses to test
qualify behaviour (see existing fixtures there); reuse the same approach
verbatim.

**Observable output assertions** (per project rule): each test asserts a
specific edge attribute (`transform`, `confidence`, `src.table.name`), not
"no exception raised".

---

## Wiring Checklist — Fix B

| Item | Evidence required |
|------|-------------------|
| `_emit_table_level_fallback_edges` defined on `SqlParser` | grep `def _emit_table_level_fallback_edges` |
| `_emit_table_level_fallback_edges` **called** from the `except` block in `_extract_column_lineage` | grep finds the call site, not just the definition (project rule: "Called, not just defined") |
| New constants `TRANSFORM_TABLE_LEVEL_FALLBACK` and `CONFIDENCE_TABLE_LEVEL_FALLBACK` defined in `base.py` | grep |
| Both constants **used** in `_emit_table_level_fallback_edges` (not hard-coded literals) | grep finds them inside the method body |
| New error marker `col_lineage_fallback:table_level:<N>` appended to `out.errors` only when at least one fallback edge is emitted | inspection + B-AC-3/5 tests |
| No `# TODO` left in the happy path or the new fallback path (project rule) | grep `# TODO` on the diff |
| `_fallback_table_scan` (or equivalent table walker) call site documented and reused, not reimplemented | grep |
| All five Fix B unit tests pass; `uv run pytest tests/unit/test_table_level_fallback.py -x` green | test runner |
| No regression in `tests/unit/test_T09_01_qualify_once.py` or `tests/unit/test_sprint_06_t04_t05.py` | full unit suite green |
| `uv run pyright` passes with zero new errors | type checker |
| `uv run ruff check src tests` passes | linter |

---

# Implementation Ordering

1. **Fix A regex + unit tests** in `_preprocess_snowflake_sql`. PR-1.
2. Re-run the DWH e2e corpus locally. Record the IA-DATAPRODUCTS file count
   that now produces non-empty `column_lineage`. If A-AC-6 holds on real
   files, Fix A is done.
3. **Pause and re-assess Fix B**: count files still emitting zero
   `COLUMN_LINEAGE` edges after Fix A. If the residual zero-edge cohort is
   small (< 5 % of the corpus), defer Fix B to a later sprint and instead
   open a finding ticket against `ARCHITECTURE_REVIEW.md`. If it is still
   meaningful, proceed.
4. **Fix B helper + wiring + unit tests** in `_extract_column_lineage`.
   Separate PR (PR-2). Do not bundle with Fix A — they are independent and
   the rollback boundary must be clean.
5. Re-run the DWH e2e corpus after Fix B and update the lineage-coverage
   metrics tracked in `MEMORY.md` (`project_lineage_coverage_escalation.md`).

---

# Files Changed (Both Fixes)

| Fix | File | Change |
|-----|------|--------|
| A | [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py) | Add `WITH TAG (...)` strip block in `_preprocess_snowflake_sql` |
| A | [`tests/unit/test_sprint_06_t04_t05.py`](../tests/unit/test_sprint_06_t04_t05.py) | Add `TestWithTagStripping` class (six unit tests + one parse-through test) |
| B | [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) | Add transform/confidence constants; add `_emit_table_level_fallback_edges`; wire into `_extract_column_lineage` qualify-failure `except` block |
| B | `tests/unit/test_table_level_fallback.py` (new) | Five unit tests against monkeypatched `qualify` |

No changes to:

- `src/sqlcg/core/schema.cypher` (no migration)
- `src/sqlcg/core/queries.cypher` (table-level fallback edges are queryable
  via existing `COLUMN_LINEAGE` pattern; downstream filters by `transform`
  are unaffected)
- `src/sqlcg/server/tools.py` (the `trace_column_lineage` tool already walks
  `COLUMN_LINEAGE` edges regardless of `transform`; fallback edges appear in
  results as `table.* → table.*`)

---

# Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Fix A regex strips a `WITH TAG`-like substring inside a string literal (e.g. inside a `COMMENT 'WITH TAG (x)'`) | The `\s+WITH\s+TAG\s*\(` anchor requires the tokens to be at SQL grammar position. String literals are quoted with single quotes; the regex does not enter quoted contexts. A pathological literal `'... WITH TAG (...) ...'` would still be matched — accept this risk; the corpus has no such case. Add a regression-guard test if a future failure surfaces. |
| Fix A regex hits three-level paren nesting that the regex cannot handle | Documented in the code comment. If a future corpus shows it, swap the regex for a paren-counting helper (same path the UNPIVOT stripper would take). |
| Fix B table-level fallback edges inflate edge counts in downstream tools that don't filter by `transform` | All consumers documented above (`db.py:114`, `analyze.py`, `tools.py`) already use `transform`-agnostic counts where appropriate; the increment is a feature, not a bug. The CLI summary at `db.py:114` reports `COLUMN_LINEAGE edges` — users will see a higher number, which is correct. |
| Fix B fallback edges with `"*"` column names collide with star expansion sentinels | Star expansion uses `STAR_SOURCE` (a separate REL table, schema.cypher:103) and `transform="STAR_EXPANSION"`. The new `transform="TABLE_LEVEL_FALLBACK"` is distinct. No collision. |
| Re-emitting fallback edges on subsequent passes (pass-2 patching of `QueryNode`) double-counts edges | Fix B only adds to `edges: list[LineageEdge]` within a single `_extract_column_lineage` call. The list is rebuilt per statement. No double-emission risk. |

---

# Rollback Plan

- Fix A rollback: revert the regex block. The IA-DATAPRODUCTS batch returns
  to its pre-fix state (zero column lineage). No data migration required —
  the index can simply be rebuilt without the fix.
- Fix B rollback: revert the new helper + wiring change. Existing
  `COLUMN_LINEAGE` edges with `transform="TABLE_LEVEL_FALLBACK"` are still
  query-compatible (they are just rows in the same rel table). They can be
  deleted with `MATCH ()-[r:COLUMN_LINEAGE {transform: 'TABLE_LEVEL_FALLBACK'}]->() DELETE r`
  in Kuzu, or wiped by a re-index.

---

# Blocking Questions

None at planning time. Open question for after Fix A e2e validation:

- Does Fix A alone push the zero-edge file count below the acceptable
  threshold? If yes, escalate Fix B priority discussion to the
  architect-reviewer before implementing.
