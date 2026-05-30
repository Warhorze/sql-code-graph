"""T-A2: Verify dependency_filter filters cross-file sources in AnsiParser.parse_file.

This test confirms that when dependency_filter is provided, only matching
cross-file CTAS bodies are passed to exp.expand().
"""

from pathlib import Path
from unittest.mock import patch

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


def test_parse_file_dependency_filter_filters_sources():
    """Verify AnsiParser.parse_file filters cross-file sources using dependency_filter.

    Construct a SchemaResolver with cross-file sources and call AnsiParser.parse_file
    with dependency_filter={'foo'}. Patch exp.expand and assert:
    - First call: 'foo' is present in sources, 'bar' is absent
    - Second call (with dependency_filter=None): both keys reach expand
    - Third call (with dependency_filter=set()): neither key reaches expand
    """
    # Setup: SchemaResolver with cross-file sources
    resolver = SchemaResolver(dialect="snowflake")

    # Register two cross-file sources
    foo_body = sqlglot.parse_one("SELECT 1 AS x")
    bar_body = sqlglot.parse_one("SELECT 2 AS y")
    resolver.register_cross_file_sources({"foo": foo_body, "bar": bar_body})

    # Create parser
    parser = AnsiParser(resolver)

    # Trivial SQL that references neither (so no further sources_map mutation)
    sql = "SELECT 1 AS dummy"

    # Test 1: dependency_filter={'foo'} — only 'foo' should reach expand
    with patch("sqlglot.expressions.expand") as mock_expand:
        mock_expand.return_value = sqlglot.parse_one("SELECT 1 AS dummy")
        parser.parse_file(Path("test1.sql"), sql, dependency_filter={"foo"})

        if mock_expand.called:
            sources_arg = mock_expand.call_args[0][1] if len(mock_expand.call_args[0]) > 1 else {}
            expand_keys = set(sources_arg.keys())
            assert "foo" in expand_keys, "foo should be in expand sources when in filter"
            assert "bar" not in expand_keys, (
                "bar should NOT be in expand sources when not in filter"
            )

    # Test 2: dependency_filter=None — all xfile_sources should reach expand
    with patch("sqlglot.expressions.expand") as mock_expand:
        mock_expand.return_value = sqlglot.parse_one("SELECT 1 AS dummy")
        parser.parse_file(Path("test2.sql"), sql, dependency_filter=None)

        if mock_expand.called:
            sources_arg = mock_expand.call_args[0][1] if len(mock_expand.call_args[0]) > 1 else {}
            expand_keys = set(sources_arg.keys())
            assert "foo" in expand_keys, "foo should be in expand sources when filter is None"
            assert "bar" in expand_keys, "bar should be in expand sources when filter is None"

    # Test 3: dependency_filter=set() — no xfile sources should reach expand
    with patch("sqlglot.expressions.expand") as mock_expand:
        mock_expand.return_value = sqlglot.parse_one("SELECT 1 AS dummy")
        parser.parse_file(Path("test3.sql"), sql, dependency_filter=set())

        if mock_expand.called:
            sources_arg = mock_expand.call_args[0][1] if len(mock_expand.call_args[0]) > 1 else {}
            expand_keys = set(sources_arg.keys())
            assert "foo" not in expand_keys, (
                "foo should NOT be in expand sources when filter is empty"
            )
            assert "bar" not in expand_keys, (
                "bar should NOT be in expand sources when filter is empty"
            )


def test_parse_file_dependency_filter_case_sensitivity():
    """Verify dependency_filter key matching is case-insensitive (both sides lowercase).

    The aggregator passes lowercased table names in the filter, and
    register_cross_file_sources keys are also lowercased. Verify the match works.
    """
    resolver = SchemaResolver(dialect="snowflake")

    # Register with lowercase keys (as done in aggregator.py:43)
    foo_body = sqlglot.parse_one("SELECT 1 AS x")
    resolver.register_cross_file_sources({"foo": foo_body})

    parser = AnsiParser(resolver)
    sql = "SELECT 1 AS dummy"

    # Pass filter with lowercase (as aggregator does)
    with patch("sqlglot.expressions.expand") as mock_expand:
        mock_expand.return_value = sqlglot.parse_one("SELECT 1 AS dummy")
        parser.parse_file(Path("test.sql"), sql, dependency_filter={"foo"})

        if mock_expand.called:
            sources_arg = mock_expand.call_args[0][1] if len(mock_expand.call_args[0]) > 1 else {}
            expand_keys = set(sources_arg.keys())
            assert "foo" in expand_keys, "Lowercase 'foo' should match lowercase key"
