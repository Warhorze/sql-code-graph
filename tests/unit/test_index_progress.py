"""Unit tests for progress callback wiring in sqlcg index."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

runner = CliRunner()


class TestProgressCallbackUnit:
    """Unit tests for progress callback invocation."""

    def test_callback_invoked_at_100_file_boundary(self, tmp_path: Path):
        """Guard: progress callback is invoked every 100 files during indexing."""
        # Create 105 minimal SQL files
        sql_files = []
        for i in range(105):
            sql_file = tmp_path / f"file_{i:03d}.sql"
            sql_file.write_text("SELECT 1;")
            sql_files.append(sql_file)

        # Track callback invocations
        calls = []

        def track_callback(n: int, total: int) -> None:
            calls.append((n, total))

        # Index the files
        backend = KuzuBackend(":memory:")
        backend.init_schema()
        indexer = Indexer()
        summary = indexer.index_repo(
            tmp_path,
            dialect=None,
            db=backend,
            progress_callback=track_callback,
        )

        # Verify callback was called at 100-file boundary
        assert any(n == 100 for n, t in calls), (
            f"Progress callback must be invoked at 100-file boundary. "
            f"Calls: {calls}"
        )
        assert all(t == 105 for n, t in calls), (
            f"Total argument must be 105 in all calls. Calls: {calls}"
        )

    def test_no_callback_invocation_below_100_files(self, tmp_path: Path):
        """Guard: progress callback is not invoked when less than 100 files exist."""
        # Create 50 minimal SQL files
        for i in range(50):
            sql_file = tmp_path / f"file_{i:02d}.sql"
            sql_file.write_text("SELECT 1;")

        # Track callback invocations
        calls = []

        def track_callback(n: int, total: int) -> None:
            calls.append((n, total))

        # Index the files
        backend = KuzuBackend(":memory:")
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(
            tmp_path,
            dialect=None,
            db=backend,
            progress_callback=track_callback,
        )

        # Verify callback was never called (below 100 files)
        assert len(calls) == 0, (
            f"Progress callback must not be invoked for < 100 files. "
            f"Got {len(calls)} calls."
        )


class TestProgressCallbackCLIWiring:
    """Integration tests for progress callback wiring in CLI."""

    def test_cli_progress_line_printed_with_100_plus_files(self, tmp_path: Path):
        """Guard: CLI prints progress line when indexing 100+ files."""
        # Create 105 minimal SQL files
        for i in range(105):
            sql_file = tmp_path / f"file_{i:03d}.sql"
            sql_file.write_text("SELECT 1;")

        # Use a fresh database for this test
        test_db_path = tmp_path / ".sqlcg" / "graph.db"
        test_db_path.parent.mkdir(parents=True, exist_ok=True)

        # Run the index command with a fresh database
        old_db_path = os.environ.get("SQLCG_DB_PATH")
        try:
            os.environ["SQLCG_DB_PATH"] = str(test_db_path)
            result = runner.invoke(app, ["index", str(tmp_path)])
        finally:
            if old_db_path is not None:
                os.environ["SQLCG_DB_PATH"] = old_db_path
            else:
                os.environ.pop("SQLCG_DB_PATH", None)

        # Verify exit code is success
        assert result.exit_code == 0, f"Command failed: {result.output}"

        # Verify progress line appears in output
        assert "Indexed" in result.output, (
            f"Expected 'Indexed' in progress output. Output: {result.output}"
        )
