"""Failing acceptance tests for T-03 (timeout-per-file cancellation fix).

These tests fail until the developer lands T-03. Named after the ticket so
`pytest -k T-03` works.
"""

import time
from unittest.mock import MagicMock, patch

from sqlcg.indexer.indexer import Indexer
from sqlcg.parsers.base import ParsedFile


def _make_fake_parser(sleep_seconds: float = 0, dialect: str = "snowflake"):
    """Return a fake parser whose parse_file sleeps for sleep_seconds."""
    parser = MagicMock()
    parser.DIALECT = dialect

    def _parse(path, sql):
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        result = ParsedFile(path=path, dialect=dialect)
        return result

    parser.parse_file.side_effect = _parse
    return parser


# ---------------------------------------------------------------------------
# Scenario A — slow parser is cut off; indexer returns within timeout + 1s
# ---------------------------------------------------------------------------


def test_T03_slow_parser_returns_within_timeout(tmp_path):
    """_index_single_file with timeout=1 must return in < 2 s for a 10 s parser.

    The current implementation uses 'with ThreadPoolExecutor' which blocks on
    __exit__ until the slow worker finishes, making timeout a no-op.  After T-03
    the explicit try/finally + shutdown(wait=False) lets the method return without
    waiting.
    """
    fake_parser = _make_fake_parser(sleep_seconds=10)
    indexer = Indexer()
    sql_file = tmp_path / "slow.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    start = time.perf_counter()
    result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=1)
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, (
        f"_index_single_file took {elapsed:.2f}s for a 1s timeout on a 10s sleep — "
        "timeout is still a no-op (T-03 not yet implemented). "
        "Expected < 2.0s (timeout + 1s headroom)."
    )
    assert result.errors == ["timeout:1s"], f"Expected errors=['timeout:1s'], got {result.errors}"


# ---------------------------------------------------------------------------
# Scenario B — fast parser returns normally
# ---------------------------------------------------------------------------


def test_T03_fast_parser_returns_normally(tmp_path):
    """_index_single_file with timeout=5 and instant parser must return fast."""
    fake_parser = _make_fake_parser(sleep_seconds=0)
    fake_result = ParsedFile(path=tmp_path / "fast.sql", dialect="snowflake")
    fake_parser.parse_file.side_effect = None
    fake_parser.parse_file.return_value = fake_result

    indexer = Indexer()
    sql_file = tmp_path / "fast.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    start = time.perf_counter()
    result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=5)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Fast parser took {elapsed:.2f}s — should be near-instant"
    assert result.errors == [], f"Expected no errors, got {result.errors}"
    assert fake_parser.parse_file.call_count == 1


# ---------------------------------------------------------------------------
# Scenario C — timeout=0 bypasses the executor entirely
# ---------------------------------------------------------------------------


def test_T03_timeout_zero_bypasses_executor(tmp_path):
    """timeout=0 must call parser.parse_file directly without using ThreadPoolExecutor."""
    fake_parser = _make_fake_parser()
    fake_result = ParsedFile(path=tmp_path / "direct.sql", dialect="snowflake")
    fake_parser.parse_file.side_effect = None
    fake_parser.parse_file.return_value = fake_result

    indexer = Indexer()
    sql_file = tmp_path / "direct.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    with patch("sqlcg.indexer.indexer.ThreadPoolExecutor") as mock_executor_cls:
        result = indexer._index_single_file(fake_parser, sql_file, "SELECT 1;", timeout=0)

    # ThreadPoolExecutor must NOT be instantiated for timeout=0
    assert mock_executor_cls.call_count == 0, (
        f"ThreadPoolExecutor was instantiated {mock_executor_cls.call_count} times "
        "for timeout=0 — the short-circuit path is not working"
    )
    assert result is fake_result
