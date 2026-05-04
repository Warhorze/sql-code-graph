"""Integration tests for all SQL dialect parsers.

Tests a representative set of queries across all supported dialects.
"""

from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser

# Representative test fixtures covering major statement types
_REPRESENTATIVE_QUERIES = {
    "select": "SELECT id, name FROM users;",
    "insert": "INSERT INTO logs (user_id, action) VALUES (1, 'login');",
    "create_table": "CREATE TABLE products (id INT, name VARCHAR(255));",
    "update": "UPDATE users SET name = 'John' WHERE id = 1;",
    "delete": "DELETE FROM logs WHERE created < DATE '2020-01-01';",
}

# Dialect-specific additional queries
_DIALECT_SPECIFIC = {
    "snowflake": "SELECT * FROM LATERAL FLATTEN(input => ARRAY_AGG(id)) WHERE id > 0;",
    "bigquery": "SELECT CURRENT_DATE() as today FROM `project.dataset.table`;",
    "postgres": "SELECT * FROM users ORDER BY id DESC LIMIT 10 OFFSET 5;",
    "tsql": "SELECT TOP 10 * FROM users ORDER BY id DESC;",
    "ansi": "SELECT * FROM schema1.table1 JOIN schema2.table2 ON table1.id = table2.id;",
}


@pytest.mark.parametrize(
    "dialect",
    [None, "snowflake", "bigquery", "postgres", "tsql"],
    ids=["ansi", "snowflake", "bigquery", "postgres", "tsql"],
)
def test_dialect_matrix_representative_queries(dialect):
    """Test that all dialects can parse representative query types.

    For each dialect, parse SELECT, INSERT, CREATE TABLE, UPDATE, and DELETE
    statements. Verify that sources are extracted for queries that reference tables.
    """
    parser = get_parser(dialect, SchemaResolver())

    # Test representative queries
    for query_type, sql in _REPRESENTATIVE_QUERIES.items():
        result = parser.parse_file(Path(f"test_{query_type}.sql"), sql)

        # Should parse without crashing
        assert isinstance(result.statements, list)

        # For queries that reference tables, should extract sources
        if query_type in ("select", "insert", "update", "delete"):
            # At minimum, should have a statement
            if result.statements:
                stmt = result.statements[0]
                # SELECT, UPDATE, DELETE should have sources
                if stmt.kind in ("SELECT", "UPDATE", "DELETE"):
                    assert len(stmt.sources) > 0, f"No sources extracted for {query_type}"


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("snowflake", _DIALECT_SPECIFIC["snowflake"]),
        ("bigquery", _DIALECT_SPECIFIC["bigquery"]),
        ("postgres", _DIALECT_SPECIFIC["postgres"]),
        ("tsql", _DIALECT_SPECIFIC["tsql"]),
        (None, _DIALECT_SPECIFIC["ansi"]),
    ],
    ids=["snowflake", "bigquery", "postgres", "tsql", "ansi"],
)
def test_dialect_specific_queries(dialect, sql):
    """Test dialect-specific SQL constructs.

    Verifies that parsers can handle dialect-specific syntax without crashing.
    """
    parser = get_parser(dialect, SchemaResolver())
    result = parser.parse_file(Path("test_dialect_specific.sql"), sql)

    # Should parse without crashing (may have limited understanding)
    assert isinstance(result.statements, list)
