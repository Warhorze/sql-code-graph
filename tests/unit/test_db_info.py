"""Unit tests for db info health check warnings (T-02)."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg.cli.commands.db import app


class TestDbInfoHealthChecks:
    """Test health check warnings in db info command."""

    def test_empty_database_shows_error(self):
        """Test that empty database shows red error message."""
        runner = CliRunner()

        # Mock backend to return zero Repo nodes
        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "1.0"

            # Return 0 for all node counts
            def run_read_side_effect(query, _params):
                if "HAS_COLUMN" in query:  # gold_tables query
                    return []
                if "STAR_SOURCE" in query or "STAR_EXPANSION" in query:
                    return [{"n": 0}]
                if "Repo" in query:
                    return [{"count": 0}]
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Database is empty" in result.output
            assert "sqlcg db init" in result.output
            assert "sqlcg index <path>" in result.output

    def test_no_column_lineage_shows_warning(self):
        """Test that database with no column lineage shows yellow warning."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "1.0"

            # Return Repo and SqlQuery, but 0 SqlColumn
            def run_read_side_effect(query, _params):
                if "HAS_COLUMN" in query:  # gold_tables query
                    return []
                if "STAR_SOURCE" in query or "STAR_EXPANSION" in query:
                    return [{"n": 0}]
                if "Repo" in query:
                    return [{"count": 1}]
                elif "SqlQuery" in query:
                    return [{"count": 10}]
                elif "SqlColumn" in query:
                    return [{"count": 0}]
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Column lineage not available" in result.output
            assert "trace_column_lineage" in result.output

    def test_healthy_database_no_warnings(self):
        """Test that healthy database shows no warning lines."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "1.0"

            # Return non-zero counts for all
            def run_read_side_effect(query, _params):
                if "HAS_COLUMN" in query:  # gold_tables query
                    return []
                if "STAR_SOURCE" in query or "STAR_EXPANSION" in query:
                    return [{"n": 0}]
                if "Repo" in query:
                    return [{"count": 1}]
                elif "SqlQuery" in query:
                    return [{"count": 10}]
                elif "SqlColumn" in query:
                    return [{"count": 50}]
                elif "COLUMN_LINEAGE" in query:
                    return [{"count": 25}]
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "Database is empty" not in result.output
            assert "No queries indexed" not in result.output
            assert "Column lineage not available" not in result.output

    def test_edges_count_always_printed(self):
        """Test that COLUMN_LINEAGE edges count is always printed."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "1.0"

            # Return various counts with COLUMN_LINEAGE edges
            def run_read_side_effect(query, _params):
                if "HAS_COLUMN" in query:  # gold_tables query
                    return []
                if "STAR_SOURCE" in query or "STAR_EXPANSION" in query:
                    return [{"n": 0}]
                if "Repo" in query:
                    return [{"count": 1}]
                elif "SqlQuery" in query:
                    return [{"count": 5}]
                elif "SqlColumn" in query:
                    return [{"count": 0}]
                elif "COLUMN_LINEAGE" in query:
                    return [{"count": 0}]
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "COLUMN_LINEAGE edges:" in result.output
            assert "0" in result.output

    def test_star_metrics_in_info_output(self):
        """Test that db info output contains STAR_SOURCE edges and STAR_EXPANSION metrics."""
        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "1.0"

            # Return counts including star metrics
            def run_read_side_effect(query, _params):
                if "HAS_COLUMN" in query:  # gold_tables query
                    return []
                if "STAR_SOURCE" in query:
                    return [{"n": 5}]
                if "STAR_EXPANSION" in query:
                    return [{"n": 10}]
                if "Repo" in query:
                    return [{"count": 1}]
                elif "SqlQuery" in query:
                    return [{"count": 10}]
                elif "SqlColumn" in query:
                    return [{"count": 50}]
                elif "COLUMN_LINEAGE" in query:
                    return [{"count": 25}]
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

            assert result.exit_code == 0
            assert "STAR_SOURCE edges:" in result.output
            assert "STAR_EXPANSION lineage edges:" in result.output
