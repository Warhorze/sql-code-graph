"""Unit tests for index CLI command output (T-08)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.cli.commands.index import index_cmd


def test_zero_edges_warning_appears_in_output():
    """Test that zero-edges warning appears in CLI output.

    When Indexer.index_repo returns lineage_edges_created=0,
    the CLI should display a warning about this.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create a test SQL file
        (temp_path / "test.sql").write_text("SELECT 1")

        # Capture console output
        printed_output = []

        def mock_console_print(*args, **kwargs):
            """Capture what would be printed to console."""
            if args:
                printed_output.append(str(args[0]))

        with patch("sqlcg.cli.commands.index.console") as mock_console, \
             patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend, \
             patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class, \
             patch("sqlcg.cli.commands.index.get_db_path") as mock_get_db_path, \
             patch("sqlcg.cli.commands.index.get_dialect") as mock_get_dialect:

            mock_console.print = mock_console_print

            # Setup mock backend
            mock_backend = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_backend

            # Setup mock indexer with zero edges
            mock_indexer = MagicMock()
            mock_indexer_class.return_value = mock_indexer
            mock_indexer.index_repo.return_value = {
                "files_parsed": 1,
                "tables_found": 1,
                "lineage_edges_created": 0,  # Key: zero edges
                "parse_errors": 0,
                "quality": {"full": 0, "table_only": 1, "scripting_fallback": 0, "failed": 0},
            }

            mock_db_path = Path(tmpdir) / ".sqlcg"
            mock_get_db_path.return_value = mock_db_path
            mock_get_dialect.return_value = None

            # Call index_cmd
            index_cmd(
                temp_path,
                dialect=None,
                dbt_manifest=None,
                timeout_per_file=30,
                buffer_pool_size=0,
                no_ddl=False,
                schema_from_info_schema=None,
                quiet=False,
            )

            # Verify warning appears in output
            output_text = " ".join(printed_output).lower()
            assert "0 lineage edges" in output_text or "warning" in output_text, \
                f"Expected zero-edges warning in output. Got: {printed_output}"


def test_no_warning_when_edges_exist():
    """Test that no warning appears when lineage_edges_created > 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create a test SQL file
        (temp_path / "test.sql").write_text("SELECT 1")

        # Capture console output
        printed_output = []

        def mock_console_print(*args, **kwargs):
            """Capture what would be printed to console."""
            if args:
                printed_output.append(str(args[0]))

        with patch("sqlcg.cli.commands.index.console") as mock_console, \
             patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend, \
             patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class, \
             patch("sqlcg.cli.commands.index.get_db_path") as mock_get_db_path, \
             patch("sqlcg.cli.commands.index.get_dialect") as mock_get_dialect:

            mock_console.print = mock_console_print

            # Setup mock backend
            mock_backend = MagicMock()
            mock_get_backend.return_value.__enter__.return_value = mock_backend

            # Setup mock indexer with edges present
            mock_indexer = MagicMock()
            mock_indexer_class.return_value = mock_indexer
            mock_indexer.index_repo.return_value = {
                "files_parsed": 1,
                "tables_found": 1,
                "lineage_edges_created": 5,  # Key: edges exist
                "parse_errors": 0,
                "quality": {"full": 1, "table_only": 0, "scripting_fallback": 0, "failed": 0},
            }

            mock_db_path = Path(tmpdir) / ".sqlcg"
            mock_get_db_path.return_value = mock_db_path
            mock_get_dialect.return_value = None

            # Call index_cmd
            index_cmd(
                temp_path,
                dialect=None,
                dbt_manifest=None,
                timeout_per_file=30,
                buffer_pool_size=0,
                no_ddl=False,
                schema_from_info_schema=None,
                quiet=False,
            )

            # Verify no zero-edges warning
            output_text = " ".join(printed_output).lower()
            # When edges > 0, we should NOT see "0 lineage edges"
            assert "0 lineage edges" not in output_text, \
                f"Should not warn about 0 edges when edges exist. Got: {printed_output}"
