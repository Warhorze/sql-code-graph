"""Unit tests for qualify_failed per-statement tracking (PR-C, #118).

Tests verify:
- Scenario C: _classify_error still returns "qualify_failed" for the existing error string
  format (no format change under the amended approach).
- qualify_failed=True is set on QueryNode when qualify() raises.
- LineageExtraction.qualify_failed is propagated to QueryNode.qualify_failed.

Plan: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_classify_error_qualify_failed_prefix_intact():
    """Scenario C: _classify_error returns 'qualify_failed' for the existing error string.

    The amended approach (PR-C) does NOT change the error string format — it remains
    'col_lineage_skip:qualify_failed:<ExcType>'.  This test confirms error_classify.py:119
    prefix match is intact after adding qualify_failed to the dataclass.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C wiring check.
    """
    from sqlcg.indexer.error_classify import _classify_error

    # Standard format (unchanged)
    assert _classify_error("col_lineage_skip:qualify_failed:ValueError") == "qualify_failed"
    # With exception class variations
    assert _classify_error("col_lineage_skip:qualify_failed:SqlglotError") == "qualify_failed"
    assert _classify_error("col_lineage_skip:qualify_failed:OptimizeError") == "qualify_failed"


def test_qualify_failed_flag_set_on_query_node_when_qualify_raises():
    """QueryNode.qualify_failed=True when qualify() raises during column-lineage extraction.

    Patches qualify() to raise, then parses a simple SELECT statement and checks
    that the resulting QueryNode has qualify_failed=True.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C amended spec.
    """
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver(dialect="ansi")
    parser = AnsiParser(resolver)

    def exploding_qualify(*args, **kwargs):
        raise ValueError("synthetic qualify failure")

    with patch("sqlcg.parsers.base.qualify", exploding_qualify):
        result = parser.parse_file(Path("test.sql"), "SELECT a FROM t;")

    assert len(result.statements) >= 1, "Expected at least one parsed statement"
    stmt = result.statements[0]
    assert stmt.qualify_failed is True, (
        f"Expected qualify_failed=True on QueryNode when qualify() raises, "
        f"got qualify_failed={stmt.qualify_failed}"
    )


def test_qualify_failed_false_when_no_qualify_exception():
    """QueryNode.qualify_failed=False for a statement that parses cleanly.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C — clean-path default.
    """
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver(dialect="ansi")
    parser = AnsiParser(resolver)

    result = parser.parse_file(Path("test.sql"), "SELECT id FROM users;")

    assert len(result.statements) >= 1, "Expected at least one parsed statement"
    for stmt in result.statements:
        assert stmt.qualify_failed is False, (
            f"Expected qualify_failed=False for clean-parsing statement, "
            f"got qualify_failed={stmt.qualify_failed} for stmt.kind={stmt.kind}"
        )


def test_lineage_extraction_qualify_failed_field_exists():
    """LineageExtraction has a qualify_failed field that defaults to False.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C dataclass spec.
    """
    from sqlcg.parsers.base import LineageExtraction

    # Default value
    ext = LineageExtraction(edges=[], star_sources=[])
    assert ext.qualify_failed is False

    # Explicit True
    ext_failed = LineageExtraction(edges=[], star_sources=[], qualify_failed=True)
    assert ext_failed.qualify_failed is True


def test_query_node_qualify_failed_field_exists():
    """QueryNode has a qualify_failed field that defaults to False.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C dataclass spec.
    """
    from sqlcg.parsers.base import QueryNode

    node = QueryNode(file=Path("test.sql"), statement_index=0, sql="SELECT 1")
    assert node.qualify_failed is False

    node_failed = QueryNode(
        file=Path("test.sql"), statement_index=0, sql="SELECT 1", qualify_failed=True
    )
    assert node_failed.qualify_failed is True


def test_no_stmt_index_tag_in_error_string():
    """The error string format has NO :stmt=<idx> suffix (amended approach, not spec).

    The spec's step 1 (appending ':stmt=<idx>') is forbidden. This test confirms
    the emitted error string never contains 'stmt=' — it remains the plain prefix form.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C plan-review amendment.
    """
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver(dialect="ansi")
    parser = AnsiParser(resolver)

    def exploding_qualify(*args, **kwargs):
        raise ValueError("synthetic qualify failure")

    with patch("sqlcg.parsers.base.qualify", exploding_qualify):
        result = parser.parse_file(Path("test.sql"), "SELECT a FROM t;")

    qualify_errors = [e for e in result.errors if "qualify_failed" in e]
    assert len(qualify_errors) >= 1, f"No qualify_failed error found in: {result.errors}"

    for err in qualify_errors:
        assert "stmt=" not in err, (
            f"Error string must NOT contain 'stmt=' (forbidden approach): {err!r}"
        )
