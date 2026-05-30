"""Benchmarks for SQL indexer performance and correctness.

Measures:
- Parse latency per file (p50, p95)
- Indexing throughput (files/second)
- Parse success rate

Run with: pytest tests/benchmarks --benchmark-only
"""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

TPCH_DIR = Path(__file__).parent / "tpch"
ADVERSARIAL_DIR = Path(__file__).parent / "adversarial"
SYNTHETIC_DIR = Path(__file__).parent.parent / "fixtures" / "synthetic"


@pytest.fixture
def tmp_db(tmp_path):
    """Temporary KùzuDB instance for benchmarking."""
    db = KuzuBackend(str(tmp_path / "bench.db"))
    db.init_schema()
    yield db
    db.close()


class TestBenchIndexer:
    """Benchmark suite for Indexer performance."""

    def test_bench_tpch_parse_latency(self, benchmark, tmp_db):
        """Measure p95 per-file latency on TPC-H queries.

        Target: p95 < 500ms per file on typical queries.
        """
        indexer = Indexer()

        def run():
            return indexer.index_repo(TPCH_DIR, "ansi", tmp_db)

        result = benchmark(run)
        # TPC-H queries reference tables but don't define them
        # (they're reference queries, not CREATE statements)
        assert result["files_parsed"] == 5
        # TPC-H queries use correlated subqueries that produce dynamic-source skips;
        # assert the count is stable rather than zero.
        # T-05 (literal column skip) reduced false E1 errors from 16 to 14.
        assert result["parse_errors"] == 14

    def test_bench_synthetic_throughput(self, benchmark, tmp_db):
        """Measure throughput on synthetic fixtures.

        Target: >= 50 files/sec on typical multi-file fixtures.
        """
        indexer = Indexer()

        def run():
            return indexer.index_repo(SYNTHETIC_DIR, "ansi", tmp_db)

        result = benchmark(run)
        # Synthetic has 3 files; ensure they're all parsed
        assert result["files_parsed"] == 3
        assert result["tables_found"] > 0

    def test_bench_adversarial_timeout(self, tmp_db):
        """Verify adversarial files complete without hanging.

        Adversarial files may hit timeout; just assert process completes.
        Does not use benchmark() fixture (not a latency measurement).
        """
        indexer = Indexer()
        # Adversarial files should either parse fast or hit timeout — not hang
        result = indexer.index_repo(ADVERSARIAL_DIR, "ansi", tmp_db, timeout_per_file=30)
        # Adversarial files may have parse errors; just assert the process completes
        assert result is not None
        assert "files_parsed" in result
        # Both 200_join.sql and 500_union.sql should be present
        assert result["files_parsed"] == 2
