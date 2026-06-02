"""Unit tests for db info health check warnings (T-02)."""

from unittest.mock import patch

from typer.testing import CliRunner

from sqlcg.cli.commands.db import app


def _make_run_read_routed_side_effect(repo_count=0, query_count=0, col_count=0, edge_count=0):
    """Return a side_effect function for run_read_routed mocks in db info tests."""

    def side_effect(query, _params):
        # Schema version
        if "SchemaVersion" in query and "indexed_sha" not in query:
            return [{"version": "6"}]
        # Indexed SHA (freshness)
        if "indexed_sha" in query:
            return [{"sha": None}]
        # Freshness repo path
        if "Repo" in query and "path" in query and "name" not in query and "COUNT" not in query:
            return []
        # STAR_SOURCE / STAR_EXPANSION
        if "STAR_SOURCE" in query:
            return [{"n": 0}]
        if "STAR_EXPANSION" in query:
            return [{"n": 0}]
        # Node counts
        if "Repo" in query and "COUNT" in query:
            return [{"count": repo_count}]
        if "SqlQuery" in query and "COUNT" in query:
            return [{"count": query_count}]
        if "SqlColumn" in query and "COUNT" in query:
            return [{"count": col_count}]
        if "COLUMN_LINEAGE" in query and "COUNT" in query:
            return [{"count": edge_count}]
        if "parsing_mode" in query:
            return []
        # All other node labels
        return [{"count": 0}]

    return side_effect


class TestDbInfoHealthChecks:
    """Test health check warnings in db info command."""

    def test_empty_database_shows_error(self):
        """Test that empty database shows red error message."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.run_read_routed") as mock_rrr:
            mock_rrr.side_effect = _make_run_read_routed_side_effect(repo_count=0)

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Database is empty" in result.output
            assert "sqlcg db init" in result.output
            assert "sqlcg index <path>" in result.output

    def test_no_column_lineage_shows_warning(self):
        """Test that database with no column lineage shows yellow warning."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.run_read_routed") as mock_rrr:
            mock_rrr.side_effect = _make_run_read_routed_side_effect(
                repo_count=1, query_count=10, col_count=0
            )

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Column lineage not available" in result.output
            assert "trace_column_lineage" in result.output

    def test_healthy_database_no_warnings(self):
        """Test that healthy database shows no warning lines."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.run_read_routed") as mock_rrr:
            mock_rrr.side_effect = _make_run_read_routed_side_effect(
                repo_count=1, query_count=10, col_count=50, edge_count=25
            )

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Database is empty" not in result.output
            assert "No queries indexed" not in result.output
            assert "Column lineage not available" not in result.output

    def test_edges_count_always_printed(self):
        """Test that COLUMN_LINEAGE edges count is always printed."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.run_read_routed") as mock_rrr:
            mock_rrr.side_effect = _make_run_read_routed_side_effect(
                repo_count=1, query_count=5, col_count=0, edge_count=0
            )

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "COLUMN_LINEAGE edges:" in result.output
            assert "0" in result.output

    def test_star_metrics_in_info_output(self):
        """Test that db info output contains STAR_SOURCE edges and STAR_EXPANSION metrics."""
        runner = CliRunner()

        def star_side_effect(query, _params):
            if "SchemaVersion" in query and "indexed_sha" not in query:
                return [{"version": "6"}]
            if "indexed_sha" in query:
                return [{"sha": None}]
            if "Repo" in query and "path" in query and "name" not in query and "COUNT" not in query:
                return []
            if "STAR_SOURCE" in query:
                return [{"n": 5}]
            if "STAR_EXPANSION" in query:
                return [{"n": 10}]
            if "Repo" in query and "COUNT" in query:
                return [{"count": 1}]
            if "SqlQuery" in query and "COUNT" in query:
                return [{"count": 10}]
            if "SqlColumn" in query and "COUNT" in query:
                return [{"count": 50}]
            if "COLUMN_LINEAGE" in query and "COUNT" in query:
                return [{"count": 25}]
            if "parsing_mode" in query:
                return []
            return [{"count": 0}]

        with patch("sqlcg.cli.commands.db.run_read_routed") as mock_rrr:
            mock_rrr.side_effect = star_side_effect

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "STAR_SOURCE edges:" in result.output
            assert "STAR_EXPANSION lineage edges:" in result.output
