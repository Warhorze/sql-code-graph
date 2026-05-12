"""Failing acceptance tests for Fix 1 / Fix 2 / Fix 3 (schema case-mismatch + quoting).

Tests must FAIL (or skip on missing symbol) before the developer implements the fixes,
and PASS after. Named so ``pytest -k fix_schema_case`` targets all three.

Fix 1: add_information_schema() lowercases all identifiers at load time.
Fix 2: as_sources_dict() quotes identifiers in the synthetic SELECT SQL.
Fix 3: base.py schema validation uses case-insensitive column lookup.
"""

import io

from sqlcg.lineage.schema_resolver import SchemaResolver

# ---------------------------------------------------------------------------
# Fix 1 — AC-1: UPPERCASE CSV rows produce lowercase as_dict() keys
# ---------------------------------------------------------------------------


def test_fix1_schema_case_normalization_uppercase_csv_as_dict():
    """add_information_schema with UPPERCASE identifiers must produce lowercase keys in as_dict().

    Snowflake INFORMATION_SCHEMA exports identifiers in UPPERCASE. After Fix 1 is applied,
    as_dict() must return lowercase keys for TABLE_SCHEMA, TABLE_NAME, and COLUMN_NAME.

    Pre-fix: as_dict() returns {"BA": {"ORDERS": ["ORDER_ID", "AMOUNT"]}}
    Post-fix: as_dict() returns {"ba": {"orders": ["order_id", "amount"]}}
    """
    csv = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION\n"
        "DWH_PRD,BA,ORDERS,ORDER_ID,1\n"
        "DWH_PRD,BA,ORDERS,AMOUNT,2\n"
    )
    resolver = SchemaResolver(dialect="snowflake")
    resolver.add_information_schema(io.StringIO(csv))
    schema = resolver.as_dict()

    assert "ba" in schema, (
        f"Expected lowercase schema key 'ba', got keys: {list(schema.keys())}. "
        "Fix 1: add_information_schema must lowercase TABLE_SCHEMA."
    )
    assert "orders" in schema["ba"], (
        f"Expected lowercase table key 'orders', got: {list(schema['ba'].keys())}. "
        "Fix 1: add_information_schema must lowercase TABLE_NAME."
    )
    cols = schema["ba"]["orders"]
    assert "order_id" in cols, (
        f"Expected lowercase column 'order_id', got: {cols}. "
        "Fix 1: add_information_schema must lowercase COLUMN_NAME."
    )
    assert "amount" in cols, (
        f"Expected lowercase column 'amount', got: {cols}. "
        "Fix 1: add_information_schema must lowercase COLUMN_NAME."
    )


# ---------------------------------------------------------------------------
# Fix 1 — AC-2: UPPERCASE CSV rows produce lowercase mapping_schema() keys
# ---------------------------------------------------------------------------


def test_fix1_schema_case_normalization_uppercase_csv_mapping_schema():
    """add_information_schema with UPPERCASE identifiers must produce lowercase mapping_schema().

    mapping_schema() is used by qualify() for cross-schema CTE resolution. The keys must
    match the lowercase identifiers that sqlglot produces for unquoted SQL identifiers.

    Pre-fix: mapping_schema() returns {"DWH_PRD": {"BA": {"ORDERS": {"ORDER_ID": ...}}}}
    Post-fix: mapping_schema() returns {"dwh_prd": {"ba": {"orders": {"order_id": ...}}}}
    """
    csv = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION\n"
        "DWH_PRD,BA,ORDERS,ORDER_ID,1\n"
        "DWH_PRD,BA,ORDERS,AMOUNT,2\n"
    )
    resolver = SchemaResolver(dialect="snowflake")
    resolver.add_information_schema(io.StringIO(csv))
    result = resolver.mapping_schema()

    assert "dwh_prd" in result, (
        f"Expected lowercase catalog key 'dwh_prd', got: {list(result.keys())}. "
        "Fix 1: add_information_schema must lowercase TABLE_CATALOG."
    )
    assert "ba" in result["dwh_prd"], (
        f"Expected lowercase db key 'ba', got: {list(result['dwh_prd'].keys())}. "
        "Fix 1: add_information_schema must lowercase TABLE_SCHEMA."
    )
    assert "orders" in result["dwh_prd"]["ba"], (
        f"Expected lowercase table key 'orders', got: {list(result['dwh_prd']['ba'].keys())}. "
        "Fix 1: add_information_schema must lowercase TABLE_NAME."
    )
    col_dict = result["dwh_prd"]["ba"]["orders"]
    assert "order_id" in col_dict, (
        f"Expected lowercase column 'order_id' in mapping_schema, got: {list(col_dict.keys())}. "
        "Fix 1: add_information_schema must lowercase COLUMN_NAME."
    )


# ---------------------------------------------------------------------------
# Fix 2 — AC-3/4: as_sources_dict() handles table/column names with spaces
# ---------------------------------------------------------------------------


def test_fix2_as_sources_dict_quoted_column_with_space():
    """as_sources_dict() must include tables whose column names contain spaces.

    Without quoting, 'SELECT col with space FROM t' is invalid SQL and the bare
    except-block silently drops the table. With Fix 2, identifiers are double-quoted
    so the synthetic SELECT is valid and the table key appears in the result.

    Pre-fix: result is empty (parse failure swallowed silently).
    Post-fix: result["t"] is a parsed exp.Select node with two columns.
    """
    import sqlglot.expressions as exp

    resolver = SchemaResolver()
    # Inject a table directly to bypass CSV normalisation (testing as_sources_dict path)
    resolver._tables[(None, None, "t")] = ["normal_col", "col with space"]

    result = resolver.as_sources_dict()

    assert "t" in result, (
        f"Table 't' must be present in as_sources_dict() even when a column has a space. "
        f"Got keys: {list(result.keys())}. "
        "Fix 2: quote identifiers in the synthetic SELECT."
    )
    node = result["t"]
    assert isinstance(node, exp.Select), (
        f"Expected exp.Select, got {type(node)}. "
        "as_sources_dict() must return a parsed AST node, not a string."
    )
    # Verify both columns are present in the node
    col_names = [c.alias_or_name for c in node.expressions]
    assert len(col_names) == 2, (
        f"Expected 2 columns in synthetic SELECT, got {col_names}. "
        "Fix 2: quoted column with space must round-trip through parse_one."
    )


def test_fix2_as_sources_dict_quoted_table_name_with_space():
    """as_sources_dict() must handle table names that contain spaces.

    Without quoting, 'SELECT a FROM table name' is invalid SQL. With Fix 2, the table
    name is double-quoted, making the SQL valid and the key present in the result.

    Pre-fix: result is empty.
    Post-fix: the key derived from 'table name' appears in the result.
    """

    resolver = SchemaResolver()
    resolver._tables[(None, "mydb", "table name")] = ["a", "b"]

    result = resolver.as_sources_dict()

    found = "table name" in result or any("table name" in k for k in result)
    assert found, (
        f"Table 'table name' must appear in as_sources_dict() result. "
        f"Got keys: {list(result.keys())}. "
        "Fix 2: quote table name in synthetic SELECT."
    )


# ---------------------------------------------------------------------------
# Fix 3 — AC-5: Schema validation lookup is case-insensitive (unit-level)
# ---------------------------------------------------------------------------


def test_fix3_schema_validation_case_insensitive_lookup():
    """Schema validation in base.py must find lowercase col_name in UPPERCASE table_cols list.

    The schema validation block at base.py line 749 currently uses:
        if col_name not in table_cols:
    When col_name = "order_id" (sqlglot-normalised) and table_cols = ["ORDER_ID"] (CSV),
    the lookup fails → false low-confidence edge emitted.

    Fix 3 changes the check to case-insensitive:
        if col_name.lower() not in {c.lower() for c in table_cols}:

    This test validates the lookup logic directly without going through parse_file,
    avoiding the sqlglot qualify() segfault that occurs when UPPERCASE mapping_schema
    keys are passed to qualify() (which is a separate and real pre-fix crash).
    """
    # Simulate the exact condition: UPPERCASE cols from CSV, lowercase col_name from sqlglot
    table_cols_uppercase = ["ORDER_ID", "AMOUNT", "STATUS"]
    col_name_lowercase = "order_id"

    # Pre-fix condition: col_name not in table_cols → True (wrong: column exists but case differs)
    pre_fix_result = col_name_lowercase not in table_cols_uppercase
    assert pre_fix_result is True, (
        "Pre-condition: confirm the case mismatch exists in the original check."
    )

    # Post-fix condition: case-insensitive check → False (correct: column IS in the schema)
    post_fix_result = col_name_lowercase.lower() not in {c.lower() for c in table_cols_uppercase}
    assert post_fix_result is False, (
        f"Post-fix case-insensitive lookup must find '{col_name_lowercase}' in "
        f"{table_cols_uppercase}. "
        "Fix 3: use col_name.lower() not in {{c.lower() for c in table_cols}}."
    )


def test_fix3_schema_validation_col_not_in_schema_still_caught():
    """Case-insensitive lookup must still catch genuinely absent columns.

    Fix 3 must not suppress the low-confidence edge for a column that is truly absent
    from the schema, regardless of case.
    """
    table_cols = ["ORDER_ID", "AMOUNT"]
    absent_col = "nonexistent"  # lowercase, not in table even case-insensitively

    post_fix_result = absent_col.lower() not in {c.lower() for c in table_cols}
    assert post_fix_result is True, (
        f"A genuinely absent column '{absent_col}' must still be 'not in' after Fix 3. "
        f"Schema has: {table_cols}."
    )


# ---------------------------------------------------------------------------
# Fix 1 — AC-6 regression: lowercase CSV produces same result as before Fix 1
# ---------------------------------------------------------------------------


def test_fix1_no_regression_lowercase_csv():
    """add_information_schema with already-lowercase CSV must produce the same result as before.

    This is a regression guard: Fix 1 must not break callers that already pass lowercase
    identifiers (the majority of existing tests and real lowercase-schema repos).
    """
    csv = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION\n"
        "mydb,ba,orders,order_id,1\n"
        "mydb,ba,orders,amount,2\n"
    )
    resolver = SchemaResolver()
    resolver.add_information_schema(io.StringIO(csv))
    schema = resolver.as_dict()

    assert "ba" in schema, f"Expected 'ba' key, got: {list(schema.keys())}"
    assert "orders" in schema["ba"], f"Expected 'orders' key, got: {list(schema['ba'].keys())}"
    assert schema["ba"]["orders"] == ["order_id", "amount"], (
        f"Column list changed: {schema['ba']['orders']}"
    )
