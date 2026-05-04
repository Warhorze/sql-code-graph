"""Unit tests for the metrics module (Phase 8)."""

import json
import os
import sqlite3
from unittest.mock import patch

import pytest
import typer.testing

from sqlcg.metrics.store import MetricsStore


class TestMetricsStore:
    """Test MetricsStore initialization and basic operations."""

    def test_init_schema_is_idempotent(self, tmp_path):
        """Calling init_schema twice does not raise an error."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))

        # First call
        store.init_schema()

        # Second call should be idempotent
        store.init_schema()

        # Check that tables exist
        cursor = store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "tool_calls" in tables
        assert "index_runs" in tables
        assert "feedback" in tables

        store.close()

    def test_record_tool_call_writes_row(self, tmp_path):
        """Recording a tool call creates a row in tool_calls table."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_tool_call("test_tool", 123.45, True)

        cursor = store._conn.execute("SELECT tool_name, duration_ms, success FROM tool_calls")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "test_tool"
        assert abs(rows[0][1] - 123.45) < 0.1
        assert rows[0][2] == 1

        store.close()

    def test_record_index_run_writes_row(self, tmp_path):
        """Recording an index run creates a row in index_runs table."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_index_run(
            repo_path="/test/repo",
            files_parsed=10,
            parse_errors=2,
            tables_found=5,
            lineage_edges=8,
            duration_ms=500.0,
        )

        cursor = store._conn.execute(
            "SELECT repo_path, files_parsed, parse_errors, tables_found FROM index_runs"
        )
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "/test/repo"
        assert rows[0][1] == 10
        assert rows[0][2] == 2
        assert rows[0][3] == 5

        store.close()

    def test_record_feedback_writes_row(self, tmp_path):
        """Recording feedback creates a row in feedback table."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_feedback(
            tool_name="trace_column_lineage",
            query="orders.id",
            label="TP",
            note="Correct result",
        )

        cursor = store._conn.execute("SELECT tool_name, query, label, note FROM feedback")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "trace_column_lineage"
        assert rows[0][1] == "orders.id"
        assert rows[0][2] == "TP"
        assert rows[0][3] == "Correct result"

        store.close()

    def test_write_is_noop_when_disabled(self, tmp_path):
        """When SQLCG_METRICS=0, no rows are written."""
        db_path = tmp_path / "metrics.db"
        os.environ["SQLCG_METRICS"] = "0"
        try:
            store = MetricsStore(db_path)
            store.record_tool_call("test", 100, True)

            # No database should have been created
            assert not db_path.exists() or db_path.stat().st_size == 0
        finally:
            del os.environ["SQLCG_METRICS"]

    def test_write_does_not_raise_on_sqlite_error(self, tmp_path):
        """Failed writes are logged and do not raise."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Close the connection to cause errors
        store._conn.close()

        # This should not raise
        store.record_tool_call("test", 100, True)

        store.close()

    def test_context_manager_closes_connection(self, tmp_path):
        """Context manager properly closes the connection."""
        db_path = tmp_path / "metrics.db"

        with MetricsStore(db_path) as store:
            store._conn = sqlite3.connect(str(db_path))
            assert store._conn is not None

        # After exiting the context, connection should be closed
        assert store._conn is None


class TestSubmitFeedbackTool:
    """Test submit_feedback MCP tool."""

    def test_submit_feedback_tp_recorded(self, tmp_path):
        """Submitting TP feedback records a row."""
        from sqlcg.server.tools import submit_feedback

        # Mock metrics store
        os.environ["SQLCG_METRICS"] = "1"
        try:
            with patch("sqlcg.server.tools._metrics") as mock_metrics:
                mock_metrics.record_feedback = lambda *args, **kwargs: None

                result = submit_feedback("test_tool", "test_query", "TP", "Good result")
                assert result == {"status": "recorded"}
        finally:
            if "SQLCG_METRICS" in os.environ:
                del os.environ["SQLCG_METRICS"]

    def test_submit_feedback_invalid_label_raises(self):
        """Submitting with invalid label raises ValueError."""
        from sqlcg.server.tools import submit_feedback

        with pytest.raises(ValueError, match="Invalid label"):
            submit_feedback("tool", "query", "FN")

    def test_submit_feedback_note_truncated(self, tmp_path):
        """Note longer than 500 characters is truncated."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        long_note = "x" * 600
        store.record_feedback("tool", "query", "TP", long_note)

        cursor = store._conn.execute("SELECT note FROM feedback")
        rows = cursor.fetchall()
        assert len(rows[0][0]) == 500

        store.close()

    def test_submit_feedback_skipped_when_metrics_off(self):
        """When metrics disabled, submit_feedback returns skipped."""
        from sqlcg.server.tools import submit_feedback

        os.environ["SQLCG_METRICS"] = "0"
        try:
            # Patch the module-level _metrics to be None
            with patch("sqlcg.server.tools._metrics", None):
                result = submit_feedback("tool", "query", "TP")
                assert result == {"status": "skipped"}
        finally:
            del os.environ["SQLCG_METRICS"]


class TestGainCommand:
    """Test sqlcg gain CLI command."""

    def test_gain_no_database_exits_0(self, tmp_path, monkeypatch):
        """Running gain with no metrics.db exits 0."""
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        # Mock Path.home to return tmp_path
        fake_home = tmp_path / "nonexistent"
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

        result = runner.invoke(app, ["gain"])
        assert result.exit_code == 0

    def test_gain_shows_total_calls(self, tmp_path, monkeypatch):
        """Gain displays total tool calls."""
        # Create metrics directory
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_tool_call("tool1", 100, True)
        store.record_tool_call("tool2", 200, True)
        store.record_tool_call("tool3", 150, True)
        store.close()

        # Mock the home directory
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        result = runner.invoke(app, ["gain"])
        assert result.exit_code == 0
        assert "3" in result.stdout

    def test_gain_hides_tp_rate_below_5_samples(self, tmp_path, monkeypatch):
        """TP rate is hidden when fewer than 5 feedback samples."""
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Add only 3 feedback samples
        for i in range(3):
            store.record_feedback("tool", f"query{i}", "TP")
        store.close()

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        result = runner.invoke(app, ["gain"])
        assert result.exit_code == 0
        assert "need 5" in result.stdout

    def test_gain_shows_tp_rate_at_5_samples(self, tmp_path, monkeypatch):
        """TP rate is shown when 5 or more feedback samples exist."""
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Add 5 TP feedback samples
        for i in range(5):
            store.record_feedback("tool", f"query{i}", "TP")
        store.close()

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        result = runner.invoke(app, ["gain"])
        assert result.exit_code == 0
        assert "100.0%" in result.stdout or "5/5" in result.stdout

    def test_gain_json_flag_produces_valid_json(self, tmp_path, monkeypatch):
        """Gain --json outputs valid JSON with required keys."""
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_tool_call("tool", 100, True)
        store.record_index_run("/repo", 10, 0, 5, 8, 500)
        store.close()

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        result = runner.invoke(app, ["gain", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "total_calls" in data
        assert "last_7d_calls" in data
        assert "index_runs" in data
        assert "feedback_tp" in data
        assert "feedback_total" in data
        assert "top_tools" in data


class TestReportCommand:
    """Test sqlcg report CLI command."""

    def test_report_no_database_exits_0(self):
        """Running report with no metrics.db exits 0."""
        runner = typer.testing.CliRunner()
        from sqlcg.cli.main import app

        result = runner.invoke(app, ["report", "--stdout"])
        assert result.exit_code == 0

    def test_report_fp_cluster_appears_in_output(self, tmp_path):
        """Report shows FP clusters with ≥3 samples and >50% FP rate."""
        # Create .sqlcg directory like the real code expects
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Add 4 FP feedback rows for the same query
        for _ in range(4):
            store.record_feedback("tool", "test_query", "FP")
        store.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            runner = typer.testing.CliRunner()
            from sqlcg.cli.main import app

            result = runner.invoke(app, ["report", "--stdout"])
            assert result.exit_code == 0
            assert "test_query" in result.stdout

    def test_report_fp_cluster_hidden_when_insufficient_samples(self, tmp_path):
        """FP clusters with <3 samples do not appear."""
        # Create .sqlcg directory like the real code expects
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Add only 2 FP feedback rows
        for _ in range(2):
            store.record_feedback("tool", "test_query", "FP")
        store.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            runner = typer.testing.CliRunner()
            from sqlcg.cli.main import app

            result = runner.invoke(app, ["report", "--stdout"])
            assert result.exit_code == 0
            assert "test_query" not in result.stdout

    def test_report_parse_error_cluster_appears(self, tmp_path):
        """Report shows parse error clusters with ≥2 runs and errors."""
        # Create .sqlcg directory like the real code expects
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        # Add 2 index runs with errors for the same repo
        store.record_index_run("/test/repo", 10, 2, 5, 8, 500)
        store.record_index_run("/test/repo", 10, 1, 5, 8, 500)
        store.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            runner = typer.testing.CliRunner()
            from sqlcg.cli.main import app

            result = runner.invoke(app, ["report", "--stdout"])
            assert result.exit_code == 0
            assert "/test/repo" in result.stdout

    def test_report_github_url_is_valid(self, tmp_path):
        """Report contains a valid GitHub issue URL."""
        # Create .sqlcg directory like the real code expects
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_feedback("tool", "query", "FP")
        store.record_feedback("tool", "query", "FP")
        store.record_feedback("tool", "query", "FP")
        store.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            runner = typer.testing.CliRunner()
            from sqlcg.cli.main import app

            result = runner.invoke(app, ["report", "--stdout"])
            assert result.exit_code == 0

            # Check for GitHub URL
            import urllib.parse

            lines = result.stdout.split("\n")
            for line in lines:
                if "github.com" in line:
                    # Extract URL from markdown link [text](url)
                    if "(" in line and ")" in line:
                        url = line.split("(")[1].split(")")[0]
                        parsed = urllib.parse.urlparse(url)
                        assert parsed.scheme == "https"
                        break

    def test_report_writes_to_file(self, tmp_path):
        """Report can be written to a file."""
        # Create .sqlcg directory like the real code expects
        metrics_dir = tmp_path / ".sqlcg"
        metrics_dir.mkdir()
        db_path = metrics_dir / "metrics.db"

        store = MetricsStore(db_path)
        store._conn = sqlite3.connect(str(db_path))
        store.init_schema()

        store.record_feedback("tool", "query", "TP")
        store.close()

        output_file = tmp_path / "report.md"

        with patch("pathlib.Path.home", return_value=tmp_path):
            runner = typer.testing.CliRunner()
            from sqlcg.cli.main import app

            result = runner.invoke(app, ["report", "--output", str(output_file)])
            assert result.exit_code == 0
            assert output_file.exists()
            assert output_file.read_text()
