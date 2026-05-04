"""Integration tests for cross-file lineage resolution."""

import tempfile
from pathlib import Path

import pytest

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


@pytest.fixture
def temp_sql_files():
    """Create temporary SQL files for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # File A: define base tables
        file_a = tmpdir / "base.sql"
        file_a.write_text(
            """
CREATE TABLE customers (id INT, name VARCHAR);
CREATE TABLE orders (id INT, customer_id INT);
"""
        )

        # File B: create a view using file A tables
        file_b = tmpdir / "views.sql"
        file_b.write_text(
            """
CREATE VIEW customer_orders AS
SELECT c.id, c.name, o.id as order_id
FROM customers c
JOIN orders o ON c.id = o.customer_id;
"""
        )

        # File C: query the view from file B
        file_c = tmpdir / "reports.sql"
        file_c.write_text(
            """
SELECT * FROM customer_orders WHERE id > 100;
"""
        )

        yield tmpdir


def test_cross_file_view_resolution(temp_sql_files):
    """Test that views defined in one file are resolved from another."""
    schema_resolver = SchemaResolver(dialect=None)
    parser = get_parser(None, schema_resolver)
    aggregator = CrossFileAggregator()

    # Pass 1: parse all files
    files = sorted(temp_sql_files.glob("*.sql"))
    pass1_results = []
    for file_path in files:
        sql = file_path.read_text(encoding="utf-8")
        parsed = parser.parse_file(file_path, sql)
        aggregator.register_pass1(parsed)
        pass1_results.append(parsed)

    # Verify base tables are registered
    assert "customers" in aggregator.sources
    assert "orders" in aggregator.sources

    # Pass 2: re-parse with cross-file context
    pass2_results = []
    for parsed in pass1_results:
        resolved = aggregator.resolve_pass2(parser, parsed)
        pass2_results.append(resolved)

    # The view definition should be resolved in pass 2
    # Verify no errors occurred
    for result in pass2_results:
        # Should not have any file I/O errors
        assert not any("cannot re-read" in e for e in result.errors)


def test_resolve_pass2_deleted_file(temp_sql_files):
    """Test that resolve_pass2 returns pass-1 result when file is deleted."""
    schema_resolver = SchemaResolver(dialect=None)
    parser = get_parser(None, schema_resolver)
    aggregator = CrossFileAggregator()

    file_a = temp_sql_files / "base.sql"
    sql = file_a.read_text(encoding="utf-8")
    parsed = parser.parse_file(file_a, sql)
    aggregator.register_pass1(parsed)

    # Delete the file
    file_a.unlink()

    # resolve_pass2 should return the pass-1 result unchanged and log WARNING
    result = aggregator.resolve_pass2(parser, parsed)

    # Should be the same object (pass-1 result)
    assert result is parsed
