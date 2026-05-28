"""Unit tests for hint fields in result models (T-07)."""

# Integration test (real KuzuDB -> hint field) deferred - see plan T-07

from unittest.mock import MagicMock, patch

from sqlcg.server.tools import (
    find_table_usages,
    get_downstream_dependencies,
    get_upstream_dependencies,
    search_sql_pattern,
    trace_column_lineage,
)


class TestTraceColumnLineageHint:
    """Test hint field in trace_column_lineage empty results."""

    def test_empty_result_has_hint(self):
        """Test that empty lineage result has a hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock empty lineage results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed with 'n' key
                [],  # trace query returns empty
            ]

            result = trace_column_lineage("orders.amount")

            assert result.hint is not None
            assert "SqlColumn" in result.hint
            assert result.lineage == []

    def test_non_empty_result_no_hint(self):
        """Test that non-empty lineage result has no hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock non-empty results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [{"id": "raw_orders.amount", "col_name": "amount"}],  # trace query
                [],  # visited, no more rows
            ]

            result = trace_column_lineage("orders.amount")

            assert result.hint is None
            assert len(result.lineage) >= 1


class TestFindTableUsagesHint:
    """Test hint field in find_table_usages empty results."""

    def test_empty_result_has_hint(self):
        """Test that empty usages result has a hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock empty usages results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [],  # find usages query returns empty
            ]

            result = find_table_usages("orders")

            assert result.hint is not None
            assert "BI tools" in result.hint or "analyze impact" in result.hint
            assert result.usages == []

    def test_non_empty_result_no_hint(self):
        """Test that non-empty usages result has no hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock non-empty usages
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [{"file": "etl.sql", "sql": "SELECT * FROM orders", "kind": "SELECT"}],
            ]

            result = find_table_usages("orders")

            assert result.hint is None
            assert len(result.usages) >= 1


class TestGetDownstreamDependenciesHint:
    """Test hint field in get_downstream_dependencies empty results."""

    def test_empty_result_has_hint(self):
        """Test that empty downstream dependencies result has a hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock empty downstream results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [],  # downstream query returns empty
            ]

            result = get_downstream_dependencies("orders.amount")

            assert result.hint is not None
            assert "SqlColumn" in result.hint
            assert result.nodes == []


class TestGetUpstreamDependenciesHint:
    """Test hint field in get_upstream_dependencies empty results."""

    def test_empty_result_has_hint(self):
        """Test that empty upstream dependencies result has a hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock empty upstream results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [],  # upstream query returns empty
            ]

            result = get_upstream_dependencies("orders.amount")

            assert result.hint is not None
            assert "SqlColumn" in result.hint
            assert result.nodes == []


class TestSearchSqlPatternHint:
    """Test hint field in search_sql_pattern empty results."""

    def test_empty_result_has_hint(self):
        """Test that empty pattern search result has a hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock empty pattern search results
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [],  # pattern query returns empty
            ]

            result = search_sql_pattern("INSERT INTO")

            assert result.hint is not None
            assert "case-sensitive" in result.hint
            assert result.matches == []

    def test_non_empty_result_no_hint(self):
        """Test that non-empty pattern search result has no hint."""
        with patch("sqlcg.server.tools._open_backend") as mock_get_backend:
            mock_db = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_db

            # Mock non-empty pattern search
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                [{"file": "etl.sql", "sql": "INSERT INTO orders", "kind": "INSERT"}],
            ]

            result = search_sql_pattern("INSERT INTO")

            assert result.hint is None
            assert len(result.matches) >= 1
