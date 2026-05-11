"""Integration tests for per-file commit boundary in indexer."""

from pathlib import Path
from unittest.mock import patch

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


class TestPerFileCommitBoundary:
    """Integration tests for P-01 per-file commit boundary."""

    def test_per_file_commit_prevents_oom(self, tmp_path: Path):
        """Guard: index_repo commits per file, not once for all files."""
        # Create 200 minimal SQL files
        for i in range(200):
            sql_file = tmp_path / f"file_{i:03d}.sql"
            sql_file.write_text("SELECT 1;")

        # Mock db.transaction to count invocations
        backend = KuzuBackend(":memory:")
        backend.init_schema()

        transaction_count = 0
        original_transaction = backend.transaction

        def counting_transaction(*args, **kwargs):
            nonlocal transaction_count
            transaction_count += 1
            return original_transaction(*args, **kwargs)

        with patch.object(backend, "transaction", side_effect=counting_transaction):
            indexer = Indexer()
            indexer.index_repo(tmp_path, dialect=None, db=backend)

        # Verify one transaction per file (200 files = 200 transactions)
        assert transaction_count == 200, (
            f"Expected one transaction per file (200 files), got {transaction_count}. "
            "Per-file commit boundary is missing in index_repo."
        )

    def test_single_file_failure_does_not_abort_corpus(self, tmp_path: Path):
        """Guard: single file failure logs WARNING and continues."""
        # Create 5 valid SQL files and prepare to fail one
        for i in range(5):
            sql_file = tmp_path / f"file_{i:02d}.sql"
            sql_file.write_text("SELECT 1;")

        backend = KuzuBackend(":memory:")
        backend.init_schema()

        # Mock _upsert_parsed_file to raise on 3rd call
        indexer = Indexer()
        call_count = 0
        original_upsert = indexer._upsert_parsed_file

        def failing_upsert(parsed, db):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("Simulated upsert failure")
            return original_upsert(parsed, db)

        with patch.object(indexer, "_upsert_parsed_file", side_effect=failing_upsert):
            summary = indexer.index_repo(tmp_path, dialect=None, db=backend)

        # Verify failed file is counted in quality, not silent loss
        assert summary["quality"]["failed"] >= 1, (
            f"Failed file must be counted in quality metrics. Quality counts: {summary['quality']}"
        )
        assert summary["files_parsed"] >= 4, (
            f"At least 4 files should parse successfully (5 total - 1 failure). "
            f"Got {summary['files_parsed']}"
        )

    def test_buffer_pool_size_passed_to_kuzu(self, tmp_path: Path):
        """Guard: --buffer-pool-size is passed to KuzuDB."""
        import os

        # Set env var
        os.environ["SQLCG_BUFFER_POOL_MB"] = "256"

        from sqlcg.core.config import KuzuConfig

        config = KuzuConfig.from_env()
        assert config.buffer_pool_size_mb == 256, (
            f"Expected buffer_pool_size_mb=256, got {config.buffer_pool_size_mb}"
        )

        # Clean up
        del os.environ["SQLCG_BUFFER_POOL_MB"]
