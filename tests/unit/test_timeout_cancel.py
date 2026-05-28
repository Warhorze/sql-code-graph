"""Unit tests for T-03 (timeout-per-file cancellation fix).

Note: With T-09-04's multiprocessing approach, fake parsers must be module-level
classes (not MagicMock instances) for pickle compatibility.
"""

import time
from unittest.mock import patch

from sqlcg.indexer.indexer import Indexer
from sqlcg.parsers.base import ParsedFile


# Module-level fake parsers for pickle compatibility with multiprocessing
class _FastParser:
    DIALECT = "snowflake"

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver

    def parse_file(self, path, sql):
        return ParsedFile(path=path, dialect=self.DIALECT)


class _SlowParser:
    DIALECT = "snowflake"

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver

    def parse_file(self, path, sql):
        time.sleep(10)
        return ParsedFile(path=path, dialect=self.DIALECT)


# ---------------------------------------------------------------------------
# Scenario A — slow parser is cut off; indexer returns within timeout + 1s
# ---------------------------------------------------------------------------


def test_T03_slow_parser_returns_within_timeout(tmp_path):
    """_index_single_file with timeout=1 must return in < 2 s for a 10 s parser.

    T-03/T-09-04: With multiprocessing.Process, the timeout properly sends SIGKILL
    to the subprocess when the timeout fires, allowing the method to return
    immediately without waiting for the slow parser to finish.
    """
    fake_parser = _SlowParser()
    indexer = Indexer()
    sql_file = tmp_path / "slow.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    start = time.perf_counter()
    result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=1)
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, (
        f"_index_single_file took {elapsed:.2f}s for a 1s timeout on a 10s sleep — "
        "expected hard kill within 2s (1s timeout + 5s join + overhead). "
        "Expected < 2.0s."
    )
    assert len(result.errors) == 1 and result.errors[0].startswith("timeout:1s"), (
        f"Expected errors starting with 'timeout:1s', got {result.errors}"
    )


# ---------------------------------------------------------------------------
# Scenario B — fast parser returns normally
# ---------------------------------------------------------------------------


def test_T03_fast_parser_returns_normally(tmp_path):
    """_index_single_file with timeout=5 and instant parser must return fast."""
    fake_parser = _FastParser()
    indexer = Indexer()
    sql_file = tmp_path / "fast.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    start = time.perf_counter()
    result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=5)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Fast parser took {elapsed:.2f}s — should be near-instant"
    assert result.errors == [], f"Expected no errors, got {result.errors}"
    # Note: call_count verification is not possible with subprocess approach


# ---------------------------------------------------------------------------
# Scenario C — timeout=0 bypasses the executor entirely
# ---------------------------------------------------------------------------


def test_T03_timeout_zero_bypasses_executor(tmp_path):
    """timeout=0 must call parser.parse_file directly without using multiprocessing."""
    fake_parser = _FastParser()
    indexer = Indexer()
    sql_file = tmp_path / "direct.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    with patch("sqlcg.indexer.indexer.mp.get_context") as mock_ctx:
        # multiprocessing should NOT be called when timeout=0
        mock_ctx.side_effect = RuntimeError("multiprocessing should not be used when timeout=0")
        result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=0)

    # multiprocessing.get_context must NOT be called for timeout=0
    mock_ctx.assert_not_called()
    assert result.errors == [], f"Expected no errors, got {result.errors}"
    assert result.dialect == "snowflake"
