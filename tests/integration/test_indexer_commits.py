"""Integration tests for per-file commit boundary in indexer."""

from pathlib import Path
from unittest.mock import patch

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


class TestPerFileCommitBoundary:
    """Integration tests for P-01 per-file commit boundary."""

    def test_batched_commits_reduce_overhead(self, tmp_path: Path):
        """Guard: index_repo batches transactions instead of per-file commits.

        With batching (default batch_size=50), 200 files should produce
        ~4 transactions instead of 200. This prevents commit overhead from
        dominating latency on large corpora while still committing frequently
        enough to prevent OOM.
        """
        # Create 200 minimal SQL files
        for i in range(200):
            sql_file = tmp_path / f"file_{i:03d}.sql"
            sql_file.write_text("SELECT 1;")

        # Mock db.transaction to count invocations
        backend = DuckDBBackend(":memory:")
        backend.init_schema()

        transaction_count = 0
        original_transaction = backend.transaction

        def counting_transaction(*args, **kwargs):
            nonlocal transaction_count
            transaction_count += 1
            return original_transaction(*args, **kwargs)

        with patch.object(backend, "transaction", side_effect=counting_transaction):
            indexer = Indexer()
            # Default batch_size=50: 200 files / 50 = 4 batches
            indexer.index_repo(tmp_path, dialect=None, db=backend)

        # Verify batched commits: ~4 transactions for 200 files with batch_size=50
        # (not 200 per-file commits, not 1 monolithic commit)
        expected_batches = 200 // 50 + (1 if 200 % 50 else 0)  # 4
        assert 1 < transaction_count < 200, (
            f"Expected batched transactions (between 1 and 200), got {transaction_count}. "
            f"Should be ~{expected_batches} batches for 200 files with batch_size=50."
        )

    def test_single_file_failure_does_not_abort_corpus(self, tmp_path: Path):
        """Guard: single file build failure logs WARNING and continues.

        After v1.1.1 (batch-flush refactor), failure isolation is at _build_file_rows
        (pure, per-file, no db access). A bad file is excluded from the batch buffer;
        the remaining files flush together. Patch target is _build_file_rows, not
        _upsert_parsed_file (which is now the single-file wrapper used only by
        reindex_file, not by _flush_batch).
        """
        # Create 5 valid SQL files and prepare to fail one
        for i in range(5):
            sql_file = tmp_path / f"file_{i:02d}.sql"
            sql_file.write_text("SELECT 1;")

        backend = DuckDBBackend(":memory:")
        backend.init_schema()

        # Mock _build_file_rows to raise on 3rd call
        indexer = Indexer()
        call_count = 0
        original_build = indexer._build_file_rows

        def failing_build(
            parsed,
            defined_table_registry=None,
            canonical_by_bare=None,
            ambiguous_bare=None,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("Simulated build failure")
            return original_build(parsed, defined_table_registry, canonical_by_bare, ambiguous_bare)

        with patch.object(indexer, "_build_file_rows", side_effect=failing_build):
            summary = indexer.index_repo(tmp_path, dialect=None, db=backend)

        # Verify failed file is counted in quality, not silent loss
        assert summary["quality"]["failed"] >= 1, (
            f"Failed file must be counted in quality metrics. Quality counts: {summary['quality']}"
        )
        assert summary["files_parsed"] >= 4, (
            f"At least 4 files should parse successfully (5 total - 1 failure). "
            f"Got {summary['files_parsed']}"
        )
