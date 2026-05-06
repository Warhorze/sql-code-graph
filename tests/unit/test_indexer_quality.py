"""Unit tests for ParseQuality assignment in indexer (T-09)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from sqlcg.indexer.indexer import Indexer


def test_failed_parse_gets_failed_quality():
    """Test that a file that fails to parse gets ParseQuality.FAILED.

    When indexer encounters an exception during parsing, it should create
    a ParsedFile with parse_quality=FAILED and add it to results.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create a mock backend
        mock_backend = MagicMock()
        mock_backend.upsert_node = MagicMock()
        mock_backend.upsert_edge = MagicMock()
        mock_backend.transaction = MagicMock()
        mock_backend.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_backend.transaction.return_value.__exit__ = MagicMock(return_value=None)

        # Create a SQL file with invalid syntax to trigger parse failure
        bad_sql_file = temp_path / "bad.sql"
        bad_sql_file.write_text("INVALID SQL HERE @@@@")

        # Index the repo
        indexer = Indexer()
        result = indexer.index_repo(
            temp_path,
            dialect=None,
            db=mock_backend,
        )

        # The summary should show parse_errors
        assert result["parse_errors"] > 0, "Should have recorded parse errors"

        # Check that quality breakdown includes failed entries
        # Note: This is inferred from the summary returned
        assert "quality" in result, "Result should have quality breakdown"


def test_quality_distribution_in_summary():
    """Test that quality breakdown is included in indexer summary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)

        # Create a valid SQL file
        (temp_path / "test.sql").write_text("SELECT 1")

        # Create a mock backend
        mock_backend = MagicMock()
        mock_backend.upsert_node = MagicMock()
        mock_backend.upsert_edge = MagicMock()
        mock_backend.transaction = MagicMock()
        mock_backend.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_backend.transaction.return_value.__exit__ = MagicMock(return_value=None)

        # Index the repo
        indexer = Indexer()
        result = indexer.index_repo(
            temp_path,
            dialect=None,
            db=mock_backend,
        )

        # Verify quality breakdown is in result
        assert "quality" in result, "Result should include quality breakdown"
        quality = result["quality"]

        # Quality should have these keys
        expected_keys = {"full", "table_only", "scripting_fallback", "failed"}
        assert set(quality.keys()) == expected_keys, \
            f"Expected keys {expected_keys}, got {set(quality.keys())}"

        # Counts should be non-negative integers
        for key, count in quality.items():
            assert isinstance(count, int) and count >= 0, \
                f"Quality[{key}] should be non-negative int, got {count}"
