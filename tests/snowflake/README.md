# Snowflake Parser — Lineage Failure Codes (E-codes)

This directory contains synthetic fixtures and tests that pin the Snowflake
parser's behaviour for every known **E-code** — a taxonomy of reasons why
column-lineage edges are missing or incomplete.

## What is an E-code?

When `sqlcg` indexes a Snowflake SQL file it may fail to produce column-lineage
edges for some or all columns. Each failure mode has a code (`E1`–`E36+`) that
identifies **why** lineage is absent. The codes appear in:

- `ParsedFile.errors` — structured strings like `col_lineage_skip:dynamic_source:my_col`
- `ParsedFile.parse_quality` — file-level summary: `FULL`, `TABLE_ONLY`, `SCRIPTING_FALLBACK`, or `FAILED`
- `sqlcg report --stdout` — aggregated per-corpus summary (see [Diagnosing your corpus](#diagnosing-your-corpus) below)

---

## Code reference

### E1 — Alias column reference ✅ Fixed

**What it was**: A column referenced via a SELECT-level alias inside the same
query (e.g., `SELECT a + 1 AS b, b * 2 AS c …`) caused a crash during lineage
extraction.

**Status**: Fixed in sprint v0.3.1-postmortem. E1 count is 0 on the full
corpus. No test needed; the fix is verified.

---

### E2 — Unaliased / non-Column expression

**Error strings**: `col_lineage_skip:expr_no_name:<empty>`,
`col_lineage_skip:star:<qualifier>`

**What it means**: A SELECT column is an arithmetic expression, function call,
or star-expansion that has no alias. sqlcg cannot determine a destination column
name so the column is skipped.

```sql
-- E2 example: result of x + y has no alias → skipped
SELECT x + y FROM src;

-- E2 example: star expansion of an unresolvable qualifier → skipped
SELECT t.* FROM t;
```

**Impact**: 60.4% of all column expressions in one corpus. A best-effort
fallback (`alias or name or str(expr)[:40]`) is planned (FIX-E2).

**Workaround**: Add explicit column aliases in your SQL.

**Tests**: `tests/snowflake/E2/`

---

### E3 — DDL-only / scripting fallback

**parse_quality**: `TABLE_ONLY` or `SCRIPTING_FALLBACK`

**What it means**: The file contains only DDL statements (`ALTER TABLE`,
`ALTER DYNAMIC TABLE`, `CREATE SEQUENCE`, `ALTER WAREHOUSE`, etc.) that produce
no column lineage. The parser recognises the file as DDL and returns zero edges
— this is **correct behaviour**, not a bug.

```sql
-- E3 example: no lineage is possible or expected
ALTER DYNAMIC TABLE my_table RESUME;
```

**Impact**: High statement count (e.g., changelog files with hundreds of ALTER
statements) causes these files to be the p99 slow-parse outliers (18–21 s
each). A DDL-file early-skip is planned (FIX-DDL-SKIP).

**Tests**: `tests/snowflake/E3/`

---

### E4 — sqlglot parse failure (dialect gap)

**parse_quality**: `FAILED` (or graceful `TABLE_ONLY` after preprocessing)

**What it means**: The file contains a Snowflake construct that sqlglot's
Snowflake dialect parser does not yet support. The file fails to parse entirely.

Known gaps:

| Pattern | Preprocessing handles it? |
|---------|--------------------------|
| `CREATE TABLE t IF NOT EXISTS (…)` | Yes — stripped before parse |
| `SELECT … UNPIVOT(…)` | Yes — clause stripped |
| `EXECUTE IMMEDIATE '…'` | No — FAILED |
| Scripting blocks with `LET x := …` | No — FAILED |

```sql
-- E4 example: sqlglot cannot parse the DECLARE/BEGIN/LET block
DECLARE x INT DEFAULT 0;
BEGIN
    LET x := 1;
    RETURN x;
END;
```

**Impact**: 6 files in one corpus lost all lineage.

**Tests**: `tests/snowflake/E4/`

---

### E5 — Cross-file CTE source not resolvable

**Error strings**: `col_lineage:<col>:<exception>`,
`col_lineage:statement:<exception>`

**What it means**: `sqlglot.lineage.lineage()` (called `sg_lineage`) throws an
exception during column extraction. Root cause: `qualify()` cannot expand a CTE
source that comes from another file when no information-schema is loaded. The
error fires once per column, so a single file can contribute many E5 counts.

```sql
-- E5 example: table_not_in_schema is defined in another file
WITH x AS (SELECT a FROM table_not_in_schema)
SELECT a FROM x;
```

**Impact**: 1,280 errors — the dominant coverage blocker. Fix (FIX-E5) passes
the loaded information schema as `sources=` to `sg_lineage()`.

**Tests**: `tests/snowflake/E5/`

---

### E6 — Schema format mismatch ✅ Closed

**Status**: 0 occurrences. The `SchemaResolver.as_dict()` format is compatible
with sqlglot's `Schema` object. No action needed.

---

### E8 — Dynamic / runtime source

**Error strings**: `col_lineage_skip:dynamic_source:<col>`

**What it means**: A column's value comes from a runtime-only expression that
has no static lineage: `IDENTIFIER($var)`, `SEQ.NEXTVAL`, `UUID_STRING()`,
`CURRENT_TIMESTAMP()`, etc. sqlcg records the skip explicitly rather than
silently dropping the column.

```sql
-- E8 examples: no static lineage possible
SELECT UUID_STRING() AS uid FROM dual;
SELECT SEQ.NEXTVAL AS id FROM dual;
SELECT IDENTIFIER($table_name) AS col FROM dual;
```

**Impact**: ~87% of `sg_lineage()` calls that returned a root in one corpus.
Cross-file temp-table chains are the dominant form. Cross-file source expansion
(using `sources=`) is the planned fix but depends on FIX-E5 first.

**Tests**: `tests/snowflake/E8/`

---

### E12 — LATERAL FLATTEN / semi-structured path

**What it means**: Snowflake-specific array and semi-structured access patterns
that sqlglot's lineage walker cannot trace through:

- `LATERAL FLATTEN(input => arr) f` — the virtual column `f.value` has no
  static table-level source.
- `src:address:city::STRING` — colon-path JSON access; sqlglot may resolve
  `src` as the column root but lose the path.

```sql
-- E12 examples
SELECT f.value::STRING AS col FROM tbl, LATERAL FLATTEN(input => arr) f;
SELECT src:address:city::STRING AS city FROM customer_raw;
```

**Status**: Unimplemented. Tests are `# INVERSION TARGET` (xfail).

**Tests**: `tests/snowflake/E12/`

---

### E16 — MERGE multi-branch

**What it means**: `MERGE INTO … USING … WHEN MATCHED THEN UPDATE … WHEN NOT
MATCHED THEN INSERT …` statements are parsed but column lineage is **not**
extracted from MERGE branches. The statement is seen as table-level lineage
only (`parse_quality = TABLE_ONLY`).

```sql
MERGE INTO dst USING src ON dst.id = src.id
WHEN MATCHED THEN UPDATE SET col = src.col_a
WHEN NOT MATCHED THEN INSERT (col) VALUES (src.col_b);
```

**Status**: Unimplemented. Tests are `# INVERSION TARGET` (xfail).

**Tests**: `tests/snowflake/E16/`

---

### E18 — IFF / DECODE conditional expressions

**What it means**: `IFF(cond, a, b)`, `DECODE(expr, …)`, and `NVL2(…)` are
Snowflake conditional functions. sqlglot currently extracts edges for all
branches (including the condition column for IFF). This is **working** as of the
current sqlglot version — E18 tests are positive assertions that guard against
regression.

```sql
SELECT IFF(x > 0, a, b) AS col FROM src;
-- edges: src.a → col, src.b → col (both branches)
```

**Status**: Working. Tests are positive (no xfail).

**Tests**: `tests/snowflake/E18/`

---

### E21 — Alias forward reference

**What it means**: Snowflake allows using a SELECT-level alias in a later
expression within the same SELECT list (e.g., `SELECT a AS b, b * 2 AS c`).
sqlglot resolves this chain back to the original source column. This is
**working** — E21 tests guard against regression.

```sql
SELECT a + 1 AS b, b * 2 AS c FROM src;
-- edges: src.a → b, src.a → c (chain resolved)
```

**Status**: Working. Tests are positive (no xfail).

**Tests**: `tests/snowflake/E21/`

---

### E23 — Stored procedure body

**What it means**: `CREATE OR REPLACE PROCEDURE … LANGUAGE SQL AS $$ … $$`
blocks contain embedded DML. sqlcg detects the `BEGIN` keyword, routes to the
scripting-fallback parser, and extracts `INSERT INTO … SELECT …` statements via
regex. Lineage **is** extracted with `parse_quality = SCRIPTING_FALLBACK`.

```sql
CREATE OR REPLACE PROCEDURE p() RETURNS STRING LANGUAGE SQL AS
$$
BEGIN
    INSERT INTO dst (a) SELECT a FROM src;
    RETURN 'ok';
END;
$$;
```

**Status**: Working (scripting fallback). Tests are positive (no xfail).

**Tests**: `tests/snowflake/E23/`

---

### E25 — Cross-database / cross-schema reference

**What it means**: Three-part (`catalog.schema.table`) or two-part
(`schema.table`) identifiers are parsed but the qualifier is **not** preserved
in the lineage edge's source table name. Only the bare table name appears.

```sql
SELECT a FROM other_db.public.tbl;
-- edge: TBL.A → <output>.a   (other_db.public lost)
```

**Status**: Partially working (bare name traced), qualifier loss is the bug.
Tests are `# INVERSION TARGET` for the qualifier assertion.

**Tests**: `tests/snowflake/E25/`

---

### E27 — UDF as column source

**What it means**: A user-defined function used as a column expression
(`SELECT my_udf(src_col) AS dst_col`) may not be traced through, because the
UDF body is not available to the lineage walker. The argument column is
currently not forwarded as a lineage source.

```sql
-- my_udf body not known to sqlcg
SELECT my_udf(src_col) AS dst_col FROM src;
```

**Status**: Unimplemented. Tests are `# INVERSION TARGET` (xfail).

**Tests**: `tests/snowflake/E27/`

---

### E36 — Cross-statement / cross-file temp table

**What it means**: A `CREATE TEMP TABLE t AS SELECT …` in one statement
(or file) is used as a source in a later `INSERT INTO dst SELECT … FROM t` in a
**different** file. Within a single file, sqlcg resolves the chain via
`sources_map`. Across files, `t` is invisible to the second file's parser.

```sql
-- file_a.sql
CREATE TEMP TABLE t AS SELECT a FROM src;

-- file_b.sql  ← t is not visible here
INSERT INTO dst SELECT a FROM t;
```

**Impact**: 38 of 184 files in one corpus produced zero edges due to E36.
This is the primary zero-edge root cause for multi-file ETL pipelines.

**Status**: Within-file resolution works. Cross-file propagation is
unimplemented. Tests are `# INVERSION TARGET` for the cross-file case.

**Tests**: `tests/snowflake/E36/`

---

## Diagnosing your corpus

Run the built-in report to see which E-codes affect your indexed SQL files:

```bash
# After indexing:
sqlcg index /path/to/sql --dialect snowflake

# Print a summary of parse error patterns to stdout:
sqlcg report --stdout
```

The report groups error strings by frequency and shows which files have the
highest error counts. Error strings map to E-codes as follows:

| Error string prefix | E-code |
|---------------------|--------|
| `col_lineage_skip:expr_no_name` | E2 |
| `col_lineage_skip:star` | E2 |
| `parse_mode:pure_ddl_skip` | E3 |
| `parse_quality=FAILED` in output | E4 |
| `col_lineage:<col>:<exc>` | E5 |
| `col_lineage:statement:<exc>` | E5 |
| `col_lineage_skip:dynamic_source` | E8 |

`parse_quality` values per file are also surfaced in the report. A file with
`parse_quality=TABLE_ONLY` that you expect to have column lineage is a candidate
for E3, E4, E5, or E36 investigation.

---

## Running the tests

```bash
# All Snowflake lineage tests:
uv run pytest tests/snowflake/ -v

# A specific E-code:
uv run pytest tests/snowflake/ -k e36 -v

# Only anchor (sprint gate) tests:
uv run pytest tests/snowflake/anchors/ -v

# Only tests expected to fail (xfail = unimplemented features):
uv run pytest tests/snowflake/ -v --runxfail
```

Tests marked `# INVERSION TARGET` use `@pytest.mark.xfail(strict=True)` and
document the **intended** behaviour once the fix lands. When a fix is
implemented, remove the `xfail` marker and flip the assertion to positive.
