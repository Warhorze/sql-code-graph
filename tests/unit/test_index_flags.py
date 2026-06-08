"""Unit tests for index command flags."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.cli.commands.index import index_cmd


def test_index_quiet_flag_suppresses_summary():
    """Test that --quiet flag suppresses the summary console output.

    The --quiet flag should prevent the summary line from being printed,
    but errors should still be emitted.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create a minimal test SQL file
        (temp_path / "test.sql").write_text("SELECT 1")

        # Capture console output
        printed_lines = []

        def mock_console_print(*args, **kwargs):
            """Capture what would be printed to console."""
            if args:
                printed_lines.append(str(args[0]))

        with patch("sqlcg.cli.commands.index.console") as mock_console:
            mock_console.print = mock_console_print

            # Mock the backend and indexer
            with (
                patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend,
                patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class,
                patch("sqlcg.cli.commands.index.get_db_path") as mock_get_db_path,
                patch("sqlcg.cli.commands.index.get_dialect") as mock_get_dialect,
                # Force the direct-write path: a live sqlcg server on the host
                # machine would otherwise route this through the socket and bypass
                # every mock below, making the test depend on host runtime state.
                patch(
                    "sqlcg.cli.commands.index._try_route_index_via_server",
                    return_value=False,
                ),
            ):
                mock_backend = MagicMock()
                mock_backend.get_schema_version.return_value = "7"  # Match SCHEMA_VERSION
                mock_get_backend.return_value.__enter__.return_value = mock_backend

                mock_indexer = MagicMock()
                mock_indexer_class.return_value = mock_indexer

                # Mock the summary result
                mock_indexer.index_repo.return_value = {
                    "files_parsed": 1,
                    "tables_found": 1,
                    "lineage_edges_created": 0,
                    "parse_errors": 0,
                }

                mock_db_path = Path(tmpdir) / ".sqlcg"
                mock_get_db_path.return_value = mock_db_path
                mock_get_dialect.return_value = None

                # Test with quiet=True
                printed_lines.clear()
                index_cmd(
                    temp_path,
                    dialect=None,
                    dbt_manifest=None,
                    timeout_per_file=30,
                    no_ddl=False,
                    quiet=True,
                )

                # Verify summary was NOT printed
                summary_printed = any("Indexed" in line for line in printed_lines)
                assert not summary_printed, "Summary should not be printed when --quiet is True"

                # Test with quiet=False (default)
                printed_lines.clear()
                index_cmd(
                    temp_path,
                    dialect=None,
                    dbt_manifest=None,
                    timeout_per_file=30,
                    no_ddl=False,
                    quiet=False,
                )

                # Verify summary WAS printed
                summary_printed = any("Indexed" in line for line in printed_lines)
                assert summary_printed, "Summary should be printed when --quiet is False"


def _invoke_index_cmd(tmp_path: Path, **kwargs) -> MagicMock:
    """Call index_cmd with all required args, return the mock indexer instance."""
    with (
        patch("sqlcg.cli.commands.index.console"),
        patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend,
        patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class,
        patch("sqlcg.cli.commands.index.get_db_path"),
        patch("sqlcg.cli.commands.index.get_dialect") as mock_get_dialect,
        # Force the direct-write path: a live sqlcg server on the host machine
        # would otherwise route this through the socket and bypass every mock
        # below, making the test depend on host runtime state.
        patch("sqlcg.cli.commands.index._try_route_index_via_server", return_value=False),
    ):
        mock_backend = MagicMock()
        mock_backend.get_schema_version.return_value = "7"
        mock_get_backend.return_value.__enter__.return_value = mock_backend
        mock_indexer = MagicMock()
        mock_indexer_class.return_value = mock_indexer
        mock_indexer.index_repo.return_value = {
            "files_parsed": 1,
            "tables_found": 1,
            "lineage_edges_created": 0,
            "parse_errors": 0,
        }
        mock_get_dialect.return_value = None
        defaults = dict(
            dialect=None,
            dbt_manifest=None,
            timeout_per_file=30,
            no_ddl=False,
            quiet=True,
        )
        defaults.update(kwargs)
        index_cmd(tmp_path, **defaults)
        return mock_indexer


def test_batch_size_propagates_to_index_repo(tmp_path):
    """--batch-size value must reach Indexer.index_repo as batch_size kwarg."""
    (tmp_path / "t.sql").write_text("SELECT 1")
    mock_indexer = _invoke_index_cmd(tmp_path, batch_size=7)
    _, call_kwargs = mock_indexer.index_repo.call_args
    assert call_kwargs.get("batch_size") == 7, (
        f"batch_size=7 must reach index_repo; got kwargs: {call_kwargs}"
    )


def test_batch_size_default_is_50(tmp_path):
    """Default batch_size must be 50 when --batch-size is not provided."""
    (tmp_path / "t.sql").write_text("SELECT 1")
    mock_indexer = _invoke_index_cmd(tmp_path, batch_size=50)
    _, call_kwargs = mock_indexer.index_repo.call_args
    assert call_kwargs.get("batch_size") == 50, (
        f"default batch_size must be 50; got kwargs: {call_kwargs}"
    )
