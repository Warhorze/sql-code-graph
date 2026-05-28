"""T-B1: Verify SchemaResolver caches as_sources_dict() result.

This test confirms that as_sources_dict() returns the same cached dict
reference on the second call without re-parsing.
"""

import io
from unittest.mock import patch

from sqlcg.lineage.schema_resolver import SchemaResolver


def test_schema_resolver_caches_sources():
    """Verify as_sources_dict() returns a cached dict by reference on second call.

    Build a resolver, load a small CSV via add_information_schema.
    Call as_sources_dict() twice; assert object identity (is) of the returned dicts.
    Patch sqlglot.parse_one and assert it is called the first time but not the second.
    """
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
        "DWH,schema1,table1,col2,2,INTEGER\n"
        "DWH,schema1,table2,col3,1,FLOAT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))
    # add_information_schema pre-warms the cache; reset it so we can observe a
    # cold-build inside the patch context and verify parse_one is called exactly once.
    resolver._sources_cache = None

    # Call as_sources_dict() first time — should parse (cold build)
    with patch("sqlglot.parse_one", wraps=__import__("sqlglot").parse_one) as mock_parse:
        result1 = resolver.as_sources_dict()
        first_call_count = mock_parse.call_count

    # Call as_sources_dict() second time — should use cache (same object)
    with patch("sqlglot.parse_one", wraps=__import__("sqlglot").parse_one) as mock_parse:
        result2 = resolver.as_sources_dict()
        second_call_count = mock_parse.call_count

    # Assert object identity (same reference)
    assert result1 is result2, "as_sources_dict() should return the same cached dict reference"

    # Assert parse_one was called the first time but not the second
    assert first_call_count > 0, "parse_one should be called on first as_sources_dict() call"
    assert second_call_count == 0, (
        "parse_one should NOT be called on second as_sources_dict() call (cached)"
    )


def test_schema_resolver_cache_contains_expected_entries():
    """Verify the cached dict contains expected entries with correct structure.

    Load a small CSV and verify as_sources_dict() returns a dict with
    lowercased table names as keys and exp.Select nodes as values.
    """
    resolver = SchemaResolver(dialect="snowflake")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH,schema1,table1,col1,1,TEXT\n"
        "DWH,schema1,table1,col2,2,INTEGER\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))

    result = resolver.as_sources_dict()

    # Assert dict contains expected keys (lowercased)
    assert isinstance(result, dict), "as_sources_dict() must return a dict"
    assert "table1" in result, "Bare table name (lowercased) should be in result"

    # Assert schema.table key also present
    assert "schema1.table1" in result, "schema.table key (lowercased) should be in result"

    # Assert values are AST nodes (have 'sql' attribute or are exp.Select)
    import sqlglot.expressions as exp

    for key, value in result.items():
        assert isinstance(value, exp.Select), f"Value for key {key} should be exp.Select node"
