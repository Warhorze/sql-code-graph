# Fix: Schema Case Mismatch + Synthetic SELECT Quoting

**Plan file**: `plan/fix_schema_case_mismatch.md`
**Status**: READY FOR IMPLEMENTATION
**Branch**: `feat/fix-schema-case-mismatch` (new branch off current)
**Date**: 2026-05-12

---

## Problem Statement

Two independent bugs prevent schema-aware column lineage from working correctly when an
INFORMATION_SCHEMA CSV is loaded:

1. **Case mismatch at lookup time**: Snowflake INFORMATION_SCHEMA exports identifiers in
   UPPERCASE (`COLUMN_NAME = "ORDER_ID"`). `sqlglot` normalises unquoted SQL identifiers
   to lowercase via `.name` (`col_name = "order_id"`). The schema validation check at
   `base.py` line 749 (`col_name not in table_cols`) therefore always misses, causing
   every column to be treated as "not in schema" and emitting a false low-confidence edge.

2. **Unquoted identifiers in synthetic SELECT**: `as_sources_dict()` builds
   `SELECT col1, col2 FROM db.table`. If any name contains a space (e.g. `"table name"`)
   the generated SQL is invalid. The bare `except Exception` at line 276 silently swallows
   the parse failure, so that table is absent from the sources dict.

---

## Non-Goals

- No changes to `mapping_schema()` key case (sqlglot's `qualify()` accepts any case for
  the schema dict keys — it normalises internally).
- No changes to `add_create_table()` — DDL paths use sqlglot AST `.name` which already
  returns lowercase for unquoted identifiers.
- No changes to `add_dbt_manifest()` — dbt manifests store lowercase names by convention.
- No change to `as_dict()` shape or callers.

---

## Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-1 | `add_information_schema` with UPPERCASE CSV → `as_dict()` returns lowercase keys for TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME |
| AC-2 | `add_information_schema` with UPPERCASE CSV → `mapping_schema()` returns lowercase db/table/col keys |
| AC-3 | `as_sources_dict()` on a table with a space in its name → key present in result (no silent skip) |
| AC-4 | `as_sources_dict()` on a table with a space → parsed `exp.Select` node has correct column list |
| AC-5 | Column lookup in `base.py` schema validation block succeeds for UPPERCASE CSV + lowercase `col_name` (no false low-confidence edge) |
| AC-6 | All existing `test_schema_resolver.py` tests still pass after Fix 1 (existing fixtures use lowercase CSV — no regression) |

---

## Fix 1 — Normalize identifiers to lowercase at load time

**File**: [`schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
**Location**: `add_information_schema()`, rows loop (~line 200-211)

Replace:
```python
db_name    = row["TABLE_SCHEMA"] or None
table_name = row["TABLE_NAME"]
key_2part  = (db_name, table_name)
key_3part  = (None, db_name, table_name)
tables.setdefault(key_3part, []).append(
    (int(row["ORDINAL_POSITION"]), row["COLUMN_NAME"])
)
catalogs[key_2part] = row["TABLE_CATALOG"] or None
```

With:
```python
catalog_val = (row["TABLE_CATALOG"] or "").lower() or None
db_name     = (row["TABLE_SCHEMA"]  or "").lower() or None
table_name  =  row["TABLE_NAME"].lower()
col_name_lc = row["COLUMN_NAME"].lower()
key_2part   = (db_name, table_name)
key_3part   = (None, db_name, table_name)
tables.setdefault(key_3part, []).append(
    (int(row["ORDINAL_POSITION"]), col_name_lc)
)
catalogs[key_2part] = catalog_val
```

**Why lowercasing catalog is safe**: `mapping_schema()` passes the resulting dict to
`qualify()`. sqlglot's qualifier normalises schema key comparison internally (case-folding)
so the lowercase catalog/db keys are fine. The `mapping_schema_tables` set in `base.py`
is compared against `src_table_ref.catalog/db/name` from sqlglot `LineageNode` — which
also returns lowercase `.name` for unquoted identifiers. Lowercasing at load time makes
these match.

**Existing test impact**: `test_add_information_schema_populates_tables` uses a CSV with
`TABLE_SCHEMA=BA` (uppercase) and asserts `"BA" in schema`. This test **will break** after
Fix 1 because `as_dict()` will return `"ba"` instead of `"BA"`. The test must be updated:
- `assert "BA" in schema` → `assert "ba" in schema`
- `assert "orders" in schema["BA"]` → `assert "orders" in schema["ba"]`
- `assert "customers" in schema["BA"]` → `assert "customers" in schema["ba"]`

Similarly, `test_T09_01_mapping_schema_returns_depth3_dict` asserts `"DWH_PRD" in result`
and `"BA" in result["DWH_PRD"]` and `"X" in result["DWH_PRD"]["BA"]`. After Fix 1 these
become `"dwh_prd"`, `"ba"`, `"x"`.

**Developer must update these test assertions** — they are not regressions, they are
correct adaptations to the new normalised contract.

---

## Fix 2 — Quote identifiers in synthetic SELECT

**File**: [`schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
**Location**: `as_sources_dict()` (~lines 264-266)

Replace:
```python
col_list  = ", ".join(cols)
qualified = f"{db}.{name}" if db else name
sql       = f"SELECT {col_list} FROM {qualified}"
```

With:
```python
quoted_cols  = ", ".join(f'"{c}"' for c in cols)
quoted_table = f'"{db}"."{name}"' if db else f'"{name}"'
sql          = f"SELECT {quoted_cols} FROM {quoted_table}"
```

**sqlglot dialect interaction**: Double-quote quoting is the ANSI standard and is valid in
all supported dialects (Snowflake, BigQuery, ANSI). `sqlglot.parse_one(sql, dialect=self.dialect)`
parses double-quoted identifiers correctly in all these dialects. The resulting `exp.Select`
node's column references will have `.quoted = True`, which has no effect on how `qualify()`
or `sg_lineage()` uses the sources dict — it resolves by name, not by quoting.

**For normal (unspaced) identifiers**: quoting is semantically identical — `"id"` and `id`
produce the same resolved column reference. No regression for existing callers.

**Logging improvement**: upgrade the `except Exception` warning to also log the SQL string
(currently logged — no change needed).

---

## Fix 3 — Case-insensitive column lookup in schema validation block

**File**: [`base.py`](../src/sqlcg/parsers/base.py)
**Location**: ~line 749

Replace:
```python
if table_cols is not None and col_name not in table_cols:
```

With:
```python
if table_cols is not None and col_name.lower() not in {c.lower() for c in table_cols}:
```

**Ordering dependency**: Fix 1 (lowercasing at load time) is sufficient to eliminate the
case mismatch for CSV-loaded schemas. Fix 3 is a **defensive guard** for two remaining
cases that Fix 1 does not cover:
1. Schemas loaded via `add_create_table()` — sqlglot `.name` returns lowercase for
   unquoted identifiers, but quoted identifiers (e.g. `"OrderID"`) preserve case.
2. Schemas loaded via `add_dbt_manifest()` — dbt manifests store column names as authored
   (mixed case in some projects).

Fix 3 must be applied **after** Fix 1 — it is safe to apply both, and Fix 3 is a no-op
when Fix 1 has already normalised the CSV columns.

**Performance note**: The set comprehension `{c.lower() for c in table_cols}` is rebuilt
for every column in every statement. For tables with hundreds of columns this is O(N) per
lookup. This is acceptable for the schema validation block (it runs only when `schema`
is non-empty and `not col_expr.alias`). If profiling shows it is hot, cache the lowercased
set per table name in a local dict before the column loop — but do not pre-optimise.

---

## Fix 3 Scope Clarification — `mapping_schema()` gap

The question "Is there a `mapping_schema()` case-sensitivity gap not covered by these
three fixes?" has two parts:

1. **`mapping_schema()` dict keys passed to `qualify()`**: sqlglot's qualify() uses
   schema dict keys for column resolution. After Fix 1, all keys are lowercase, matching
   the lowercase identifiers sqlglot produces for unquoted column refs. No gap.

2. **`mapping_schema_tables` set used for confidence scoring** (base.py line 463-467):
   `schema_key = (src_table_ref.catalog, src_table_ref.db, src_table_ref.name)`.
   `src_table_ref` comes from a `LineageNode` returned by `sg_lineage()`. sqlglot's
   `LineageNode.name` for an unquoted table produces lowercase. After Fix 1, the set
   contains lowercase tuples. These will match. However, quoted identifiers in the SQL
   (e.g. `"MY_SCHEMA"."MY_TABLE"`) produce uppercase `.name` from sqlglot. The set will
   not match → confidence falls back to 0.7, not 1.0. This is a **known limitation** of
   the current confidence model and is **out of scope** for this fix. No action needed now.

---

## Implementation Ordering

Apply in this order:

1. Fix 1 (`add_information_schema` — lowercase at load)
2. Update affected tests (`test_add_information_schema_populates_tables` and
   `test_T09_01_mapping_schema_returns_depth3_dict`) to expect lowercase keys
3. Fix 2 (`as_sources_dict` — quoted identifiers)
4. Fix 3 (`base.py` — case-insensitive lookup)

Fixes 2 and 3 are independent of each other after Fix 1 is applied, but both depend on
Fix 1 being present first to avoid introducing new inconsistencies.

---

## Files Changed

| File | Change |
|------|--------|
| [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py) | Fix 1: lowercase at load; Fix 2: quote identifiers |
| [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) | Fix 3: case-insensitive lookup |
| [`tests/unit/test_schema_resolver.py`](../tests/unit/test_schema_resolver.py) | Update case assertions broken by Fix 1 |
| [`tests/unit/test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py) | Update case assertions broken by Fix 1 |

---

## Wiring Checklist

- [ ] `add_information_schema` loop lowercases all four fields before constructing keys
- [ ] `as_sources_dict` uses double-quoted identifiers in generated SQL
- [ ] `base.py` schema validation uses `.lower()` set comprehension
- [ ] `test_add_information_schema_populates_tables` expects `"ba"` not `"BA"`
- [ ] `test_T09_01_mapping_schema_returns_depth3_dict` expects `"dwh_prd"`, `"ba"`, `"x"`
- [ ] New test `test_schema_case_normalization_uppercase_csv` passes
- [ ] New test `test_as_sources_dict_quoted_identifier_with_space` passes
- [ ] New test `test_schema_validation_case_insensitive_lookup` passes
- [ ] `uv run pytest tests/unit/test_schema_resolver.py -x` passes (no regressions)
- [ ] `uv run pytest tests/unit/test_T09_01_qualify_once.py -x` passes (no regressions)
- [ ] `uv run pyright` passes with zero errors
- [ ] `uv run ruff check src tests` passes
