"""T-B2: Verify SchemaResolver invalidates sources cache on all mutations.

This test confirms that every mutation method clears _sources_cache,
forcing re-parse on the next as_sources_dict() call.
"""

import io

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver


def test_schema_resolver_invalidates_cache_on_add_create_table():
    """Verify add_create_table invalidates _sources_cache."""
    resolver = SchemaResolver(dialect="snowflake")

    # Initial setup
    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    # Get first result (populates cache)
    result1 = resolver.as_sources_dict()
    assert "table1" in result1

    # Mutate via add_create_table
    ast = sqlglot.parse_one("CREATE TABLE schema1.table2 (col1 TEXT, col2 INT)")
    resolver.add_create_table(ast)

    # Get second result (should reflect the new table)
    result2 = resolver.as_sources_dict()

    # Assert the returned dict is NOT the same object (identity changed)
    assert result1 is not result2, "as_sources_dict() should return new dict after mutation"

    # Assert the new table is reflected
    assert "table2" in result2, "New table should appear in updated cache"


def test_schema_resolver_invalidates_cache_on_add_view_sources():
    """Verify add_view_sources invalidates _sources_cache."""
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    result1 = resolver.as_sources_dict()

    # Mutate via add_view_sources
    from pathlib import Path

    from sqlcg.parsers.base import ParsedFile

    parsed = ParsedFile(path=Path("test.sql"))
    resolver.add_view_sources({"view1": parsed})

    result2 = resolver.as_sources_dict()

    assert result1 is not result2, "Cache should be invalidated after add_view_sources"


def test_schema_resolver_invalidates_cache_on_register_cross_file_sources():
    """Verify register_cross_file_sources invalidates _sources_cache."""
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    result1 = resolver.as_sources_dict()

    # Mutate via register_cross_file_sources
    ctas_body = sqlglot.parse_one("SELECT 1 AS x")
    resolver.register_cross_file_sources({"ctas_table": ctas_body})

    result2 = resolver.as_sources_dict()

    assert result1 is not result2, "Cache should be invalidated after register_cross_file_sources"


def test_schema_resolver_invalidates_cache_on_add_dbt_manifest():
    """Verify add_dbt_manifest invalidates _sources_cache."""
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    result1 = resolver.as_sources_dict()

    # Create a temporary dbt manifest file
    import json
    import tempfile

    manifest = {
        "nodes": {
            "model.project.model1": {
                "database": "DWH",
                "schema": "schema1",
                "name": "model1",
                "columns": {"col1": {}, "col2": {}},
            }
        }
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
        manifest_path = f.name

    try:
        # Mutate via add_dbt_manifest
        resolver.add_dbt_manifest(manifest_path)
        result2 = resolver.as_sources_dict()
        assert result1 is not result2, "Cache should be invalidated after add_dbt_manifest"
    finally:
        import os

        os.unlink(manifest_path)


def test_schema_resolver_invalidates_cache_on_add_information_schema():
    """Verify add_information_schema invalidates _sources_cache."""
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    result1 = resolver.as_sources_dict()

    # Mutate via add_information_schema (add more tables)
    csv_content2 = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema2,table3,col3,1,INTEGER\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content2))

    result2 = resolver.as_sources_dict()

    assert result1 is not result2, "Cache should be invalidated after add_information_schema"
    assert "table3" in result2, "New table should appear in updated cache"
