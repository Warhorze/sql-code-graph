"""Unit tests for Indexer progress callback (T-08)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from sqlcg.indexer.indexer import Indexer


def test_progress_callback_invoked_at_100_file_boundary():
    """Test that progress_callback is invoked at 100-file boundary.

    This test verifies that the Indexer calls the progress callback
    every 100 files during parsing.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create 105 tiny .sql files
        for i in range(105):
            sql_file = temp_path / f"test_{i:03d}.sql"
            sql_file.write_text(f"SELECT {i};")

        # Create a progress callback that records invocations
        callback_calls = []

        def progress_callback(current: int, total: int) -> None:
            """Record callback invocations."""
            callback_calls.append((current, total))

        # Create a mock backend
        mock_backend = MagicMock()
        mock_backend.upsert_node = MagicMock()
        mock_backend.upsert_edge = MagicMock()
        mock_backend.transaction = MagicMock()
        mock_backend.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_backend.transaction.return_value.__exit__ = MagicMock(return_value=None)

        # Index the repo with progress callback
        indexer = Indexer()
        result = indexer.index_repo(
            temp_path,
            dialect=None,
            db=mock_backend,
            progress_callback=progress_callback,
        )

        # Verify callback was called for 100-file boundary
        assert len(callback_calls) >= 1, "Progress callback should be invoked at least once"

        # Check that at least one call records reaching exactly 100 files
        current_values = [c[0] for c, t in callback_calls]
        assert any(c == 100 for c in current_values), \
            f"Progress callback should be invoked at 100-file boundary. Got calls: {callback_calls}"

        # Verify total is correct
        for current, total in callback_calls:
            assert total == 105, f"Total should be 105, got {total}"
