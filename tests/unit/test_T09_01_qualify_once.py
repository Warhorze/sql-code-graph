"""Acceptance tests for T-09-01 qualify-once parsing.

Pins the qualify-once invariant: build_scope() runs at most once per statement
and a qualify() failure is caught as a structured error rather than crashing.
Named so ``pytest -k T09_01`` works.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver

# ---------------------------------------------------------------------------
# Scenario B — build_scope is called exactly once per _extract_column_lineage
# ---------------------------------------------------------------------------


def test_T09_01_build_scope_called_once_per_statement():
    """qualify-once: build_scope() must be called exactly once per _extract_column_lineage call.

    Pre-T-09-01 the code re-qualifies inside sg_lineage for every column.
    Post-T-09-01 the body is qualified once and scope reused.
    """
    try:
        from sqlglot.optimizer.scope import build_scope as _bs
    except ImportError:
        pytest.skip("sqlglot not available")

    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    resolver = SchemaResolver(dialect="snowflake")
    parser = AnsiParser(resolver)

    sql = (
        "INSERT INTO db.s.t ("
        + ", ".join(f"c{i}" for i in range(20))
        + ") SELECT "
        + ", ".join(f"src.c{i}" for i in range(20))
        + " FROM db.s.src;"
    )
    call_count = []

    original_bs = _bs

    def counting_build_scope(expr, *args, **kwargs):
        call_count.append(1)
        return original_bs(expr, *args, **kwargs)

    with patch("sqlcg.parsers.base.build_scope", counting_build_scope, create=True):
        with patch("sqlcg.parsers.ansi_parser.build_scope", counting_build_scope, create=True):
            _ = parser.parse_file(Path("test.sql"), sql)

    # Post-T-09-01: at most 1 build_scope call per statement (one INSERT here)
    assert len(call_count) <= 1, (
        f"build_scope was called {len(call_count)} times for 1 statement with 20 columns. "
        "T-09-01 qualify-once pattern should call it at most once."
    )
    assert len(call_count) >= 1, "build_scope was never called — qualify-once path not reached"


# ---------------------------------------------------------------------------
# Scenario F — qualify failure does not crash; structured error emitted
# ---------------------------------------------------------------------------


def test_T09_01_qualify_failure_does_not_crash():
    """If qualify() raises, _extract_column_lineage must not propagate the exception.

    It must emit exactly one col_lineage_skip:qualify_failed:<exc> error and
    still return a ParsedFile (possibly with zero edges).
    """
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    resolver = SchemaResolver(dialect="snowflake")
    parser = AnsiParser(resolver)

    # Force qualify() to raise by patching it
    def exploding_qualify(*args, **kwargs):
        raise ValueError("synthetic qualify failure")

    with patch("sqlcg.parsers.base.qualify", exploding_qualify, create=True):
        result = parser.parse_file(Path("test.sql"), "SELECT a FROM t;")

    qualify_errors = [e for e in result.errors if "qualify_failed" in e]
    assert len(qualify_errors) == 1, (
        f"Expected exactly one qualify_failed error, got: {result.errors}"
    )
    assert "ValueError" in qualify_errors[0], (
        f"Error should name the exception class: {qualify_errors[0]}"
    )
