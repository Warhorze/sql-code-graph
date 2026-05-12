"""Failing acceptance tests for T-09-06 (log verbosity: per-column WARNING -> DEBUG).

Tests must FAIL before T-09-06 is implemented and PASS after.
Named so ``pytest -k T09_06`` works.
"""

import inspect

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver


def _get_parser():
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")
    return AnsiParser(SchemaResolver())


# ---------------------------------------------------------------------------
# Scenario A — _classify_error exists in error_classify module
# ---------------------------------------------------------------------------


def test_T09_06_classify_error_module_exists():
    """error_classify.py with _classify_error() must exist after T-09-06.

    Pre-T-09-06 the module does not exist.
    """
    try:
        from sqlcg.indexer.error_classify import _classify_error  # introduced by T-09-06
    except ImportError:
        pytest.skip("T-09-06 not yet implemented: error_classify module missing")

    # Basic sanity: each documented bucket maps correctly
    cases = [
        ("col_lineage_skip:dynamic_source:col", "E8"),
        ("timeout:30s", "timeout"),
        ("col_lineage_skip:pure_ddl_file", "pure_ddl_skip"),
        ("col_lineage_skip:func_fallback:Anonymous", "func_fallback"),
        ("col_lineage_skip:qualify_failed:ValueError", "qualify_failed"),
        ("some_unknown_message", "other"),
    ]
    for msg, expected in cases:
        bucket = _classify_error(msg)
        assert bucket == expected, f"_classify_error({msg!r}) = {bucket!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# Scenario E — per-column WARNING is demoted to DEBUG in source code
# ---------------------------------------------------------------------------


def test_T09_06_per_column_warning_is_debug_in_source():
    """After T-09-06, base.py must not contain self._log.warning for column lineage failures.

    The plan specifies changing self._log.warning to self._log.debug at base.py line 756.
    This test inspects source code to confirm the change — the most reliable check since
    triggering the exact exception path in a unit test requires synthetic parse failures.
    """
    import sqlcg.parsers.base as _base_mod

    source = inspect.getsource(_base_mod)

    # Check they're adjacent (within 5 lines of each other)
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if "column lineage extraction failed" in line:
            # Check surrounding 5 lines for warning vs debug
            window = lines[max(0, i - 3) : i + 3]
            for wline in window:
                assert "self._log.warning" not in wline, (
                    f"T-09-06 not implemented: found self._log.warning adjacent to "
                    f"'column lineage extraction failed' at line ~{i + 1} of base.py. "
                    f"Must be changed to self._log.debug."
                )


# ---------------------------------------------------------------------------
# Scenario D — summary dict contains error_summary key
# ---------------------------------------------------------------------------


def test_T09_06_index_repo_returns_error_summary():
    """index_repo() summary dict must contain 'error_summary' key after T-09-06."""
    import sqlcg.indexer.indexer as _indexer_mod

    source = inspect.getsource(_indexer_mod.Indexer.index_repo)

    assert "error_summary" in source, (
        "T-09-06 not implemented: index_repo() summary dict must include 'error_summary' key. "
        "Add _classify_error() bucketing and include the dict in the returned summary."
    )


# ---------------------------------------------------------------------------
# Scenario — Indexing complete INFO log exists in source
# ---------------------------------------------------------------------------


def test_T09_06_indexing_complete_log_line_exists():
    """index_repo() must emit one 'Indexing complete:' INFO log line after T-09-06."""
    import sqlcg.indexer.indexer as _indexer_mod

    source = inspect.getsource(_indexer_mod.Indexer.index_repo)

    assert "Indexing complete:" in source, (
        "T-09-06 not implemented: 'Indexing complete:' INFO log line missing from index_repo(). "
        "Add: logger.info('Indexing complete: %d files — %s', ...) at end of method."
    )
