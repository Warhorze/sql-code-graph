"""T-A3: Verify CrossFileAggregator.resolve_pass2 computes and passes dependency_filter.

This test confirms that resolve_pass2 extracts lowercased table names from
pass-1 referenced_tables and passes them as dependency_filter to parse_file.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.parsers.base import ParsedFile, TableRef


def test_resolve_pass2_passes_dependency_filter():
    """Verify resolve_pass2 computes filter from referenced_tables and passes it to parse_file.

    Construct a ParsedFile with referenced_tables=[Table(name="Foo"), Table(name="BAR")].
    Register these in aggregator.cross_file_sources so _needs_pass2 is satisfied.
    Patch parser.parse_file and call aggregator.resolve_pass2(parser, parsed).
    Assert parse_file was called with dependency_filter={'foo', 'bar'} (lowercased).
    """
    # Setup: create a parsed file with referenced tables and statements that reference them
    parsed = ParsedFile(path=Path("test.sql"), dialect="snowflake")
    parsed.referenced_tables = [
        TableRef(name="Foo", db=None, catalog=None),
        TableRef(name="BAR", db=None, catalog=None),
    ]

    # Add a statement that references foo and bar so _needs_pass2 detects cross-file deps
    from sqlcg.parsers.base import QueryNode

    stmt = QueryNode(
        file=Path("test.sql"),
        statement_index=0,
        sql="SELECT * FROM foo JOIN bar",
        kind="SELECT",
        target=None,
        sources=[
            TableRef(name="foo", db=None, catalog=None),
            TableRef(name="bar", db=None, catalog=None),
        ],
    )
    parsed.statements = [stmt]

    # Create aggregator and register cross-file sources so _needs_pass2 is satisfied
    aggregator = CrossFileAggregator()
    # Register dummy cross-file sources (these are referenced in statements)
    aggregator.cross_file_sources = {"foo": None, "bar": None}

    parser_mock = MagicMock()

    # Mock parser._schema.add_view_sources and parser.parse_file
    parser_mock._schema = MagicMock()
    parser_mock._schema.add_view_sources = MagicMock()
    parser_mock.parse_file = MagicMock(return_value=ParsedFile(path=Path("test.sql")))

    # Mock the file read to return the SQL
    test_sql = "SELECT 1"
    with patch.object(Path, "read_text", return_value=test_sql):
        # Call resolve_pass2
        aggregator.resolve_pass2(parser_mock, parsed)

    # Assert parser.parse_file was called with dependency_filter={'foo', 'bar'}
    parser_mock.parse_file.assert_called_once()
    call_args = parser_mock.parse_file.call_args

    # Extract the dependency_filter kwarg
    dependency_filter = call_args.kwargs.get("dependency_filter")
    assert dependency_filter is not None, "dependency_filter should be passed as kwarg"
    assert isinstance(dependency_filter, set), "dependency_filter should be a set"
    assert dependency_filter == {"foo", "bar"}, (
        f"dependency_filter should be lowercase {{'foo', 'bar'}}, got {dependency_filter}"
    )


def test_resolve_pass2_skips_when_no_cross_file_deps():
    """Verify resolve_pass2 returns pass-1 result unchanged (identity) when no re-parse needed.

    The identity semantics are used by callers to detect skipped files.
    """
    # Setup: create a simple parsed file with no cross-file dependencies
    parsed = ParsedFile(path=Path("test.sql"), dialect="snowflake")
    parsed.referenced_tables = []  # No references
    parsed.defined_tables = [TableRef(name="test_table", db=None, catalog=None)]

    aggregator = CrossFileAggregator()
    parser_mock = MagicMock()
    parser_mock._schema = MagicMock()

    # Call resolve_pass2
    result = aggregator.resolve_pass2(parser_mock, parsed)

    # Assert the returned object is the exact same (identity check)
    assert result is parsed, "Should return the same ParsedFile object when no re-parse needed"

    # Assert parse_file was NOT called
    parser_mock.parse_file.assert_not_called()


def test_resolve_pass2_handles_missing_names():
    """Verify resolve_pass2 handles tables with None or empty names gracefully.

    Construct a ParsedFile with referenced_tables including None and empty name entries.
    Assert dependency_filter only includes valid non-empty lowercased names.
    """
    # Setup: create a parsed file with mixed valid/invalid table names
    parsed = ParsedFile(path=Path("test.sql"), dialect="snowflake")
    parsed.referenced_tables = [
        TableRef(name="Foo", db=None, catalog=None),
        TableRef(name=None, db=None, catalog=None),  # Invalid
        TableRef(name="", db=None, catalog=None),  # Invalid (empty)
        TableRef(name="BAR", db=None, catalog=None),
    ]

    # Add statement that references foo and bar so _needs_pass2 is satisfied
    from sqlcg.parsers.base import QueryNode

    stmt = QueryNode(
        file=Path("test.sql"),
        statement_index=0,
        sql="SELECT * FROM foo JOIN bar",
        kind="SELECT",
        target=None,
        sources=[
            TableRef(name="foo", db=None, catalog=None),
            TableRef(name="bar", db=None, catalog=None),
        ],
    )
    parsed.statements = [stmt]

    aggregator = CrossFileAggregator()
    # Register cross-file sources so _needs_pass2 is satisfied
    aggregator.cross_file_sources = {"foo": None, "bar": None}

    parser_mock = MagicMock()
    parser_mock._schema = MagicMock()
    parser_mock._schema.add_view_sources = MagicMock()
    parser_mock.parse_file = MagicMock(return_value=ParsedFile(path=Path("test.sql")))

    test_sql = "SELECT 1"
    with patch.object(Path, "read_text", return_value=test_sql):
        aggregator.resolve_pass2(parser_mock, parsed)

    # Assert only valid names are in dependency_filter
    call_args = parser_mock.parse_file.call_args
    dependency_filter = call_args.kwargs.get("dependency_filter")
    assert dependency_filter == {"foo", "bar"}, (
        f"Should exclude None and empty names; got {dependency_filter}"
    )
