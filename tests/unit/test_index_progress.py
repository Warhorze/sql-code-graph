"""Unit tests for progress callback wiring in sqlcg index."""

import os
from pathlib import Path

from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

runner = CliRunner()


class TestProgressCallbackUnit:
    """Unit tests for progress callback invocation."""

    def test_callback_invoked_for_every_file(self, tmp_path: Path):
        """Guard: progress callback is invoked once per file during indexing."""
        n_files = 15
        for i in range(n_files):
            (tmp_path / f"file_{i:02d}.sql").write_text("SELECT 1;")

        calls = []

        def track_callback(n: int, total: int) -> None:
            calls.append((n, total))

        backend = KuzuBackend(":memory:")
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(
            tmp_path,
            dialect=None,
            db=backend,
            progress_callback=track_callback,
        )

        assert len(calls) == n_files, (
            f"Expected {n_files} callback calls (one per file), got {len(calls)}: {calls}"
        )
        assert calls[-1] == (n_files, n_files), (
            f"Last call must be ({n_files}, {n_files}), got {calls[-1]}"
        )
        assert all(t == n_files for _, t in calls), (
            f"Total argument must be {n_files} in all calls. Calls: {calls}"
        )

    def test_callback_invoked_for_small_repos(self, tmp_path: Path):
        """Guard: progress callback fires even with fewer than 100 files."""
        for i in range(5):
            (tmp_path / f"file_{i}.sql").write_text("SELECT 1;")

        calls = []

        def track_callback(n: int, total: int) -> None:
            calls.append((n, total))

        backend = KuzuBackend(":memory:")
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(
            tmp_path,
            dialect=None,
            db=backend,
            progress_callback=track_callback,
        )

        assert len(calls) == 5, (
            f"Expected 5 callback calls for a 5-file repo, got {len(calls)}: {calls}"
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
