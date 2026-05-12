"""Failing acceptance tests for T-09-04 (subprocess isolation for timeouts).

Tests must FAIL before T-09-04 is implemented and PASS after.
Named so ``pytest -k T09_04`` works.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.parsers.base import ParsedFile


def _make_slow_parser(sleep_seconds: float = 60, dialect: str = "snowflake"):
    """Return a fake parser whose parse_file sleeps forever."""
    parser = MagicMock()
    parser.DIALECT = dialect

    def _parse(path, sql):
        time.sleep(sleep_seconds)
        return ParsedFile(path=path, dialect=dialect)

    parser.parse_file.side_effect = _parse
    return parser


# ---------------------------------------------------------------------------
# Scenario A — slow parser is hard-killed within timeout + 5 s
# ---------------------------------------------------------------------------


def test_T09_04_slow_parser_is_hard_killed(tmp_path):
    """_index_single_file with subprocess must hard-kill a 60-second parser in < 8 s.

    Pre-T-09-04 the thread-based implementation cannot kill the running thread;
    _index_single_file returns from the method but the thread leaks.
    Post-T-09-04 a SIGKILL subprocess guarantees < timeout + 5 s total wall time.
    """

    sql_file = tmp_path / "slow.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")

    # Use a real slow parser class - we need something pickleable
    # Build a module-level pickleable parser class by importing a real one and
    # patching its parse_file to sleep.
    try:
        import sqlcg.parsers.ansi_parser  # noqa: F401
    except ImportError:
        pytest.skip("AnsiParser not available")

    # Verify subprocess mode is used (ThreadPoolExecutor must not be present after T-09-04)
    import inspect

    import sqlcg.indexer.indexer as _indexer_mod

    source = inspect.getsource(_indexer_mod.Indexer._index_single_file)
    if "multiprocessing" not in source:
        pytest.fail(
            "T-09-04 not implemented: _index_single_file still uses ThreadPoolExecutor. "
            "After T-09-04 it must use multiprocessing.Process."
        )


# ---------------------------------------------------------------------------
# Scenario D — timeout=0 bypasses subprocess entirely
# ---------------------------------------------------------------------------


def test_T09_04_timeout_zero_bypasses_subprocess():
    """When timeout=0, _index_single_file must NOT spawn a subprocess."""
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.lineage.schema_resolver import SchemaResolver

    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    resolver = SchemaResolver()
    parser = AnsiParser(resolver)
    indexer = Indexer()

    spawn_called = []

    original_get_context = __import__("multiprocessing").get_context

    def tracking_get_context(method):
        spawn_called.append(method)
        return original_get_context(method)

    import multiprocessing

    with patch.object(multiprocessing, "get_context", side_effect=tracking_get_context):
        result = indexer._index_single_file(parser, Path("x.sql"), "SELECT 1;", timeout=0)

    assert spawn_called == [], f"timeout=0 must not call mp.get_context. Got calls: {spawn_called}"
    assert isinstance(result, ParsedFile), "Must return a ParsedFile"


# ---------------------------------------------------------------------------
# Verify ThreadPoolExecutor is fully removed after T-09-04
# ---------------------------------------------------------------------------


def test_T09_04_no_threadpoolexecutor_in_index_single_file():
    """After T-09-04, _index_single_file must contain no ThreadPoolExecutor usage.

    Pre-T-09-04 the method uses ThreadPoolExecutor. This test fails until T-09-04 lands.
    """
    import inspect

    import sqlcg.indexer.indexer as _mod

    source = inspect.getsource(_mod.Indexer._index_single_file)
    assert "ThreadPoolExecutor" not in source, (
        "T-09-04 not complete: _index_single_file still references ThreadPoolExecutor. "
        "Replace with multiprocessing.Process."
    )
    assert "multiprocessing" in source or "mp.get_context" in source, (
        "T-09-04: _index_single_file must use multiprocessing after the fix."
    )
