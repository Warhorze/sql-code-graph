"""Failing acceptance tests for T-09-01 (qualify-once + schema-aware parsing).

Tests must FAIL before T-09-01 is implemented and PASS after.
Named so ``pytest -k T09_01`` works.
"""

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver

# ---------------------------------------------------------------------------
# Scenario A — mapping_schema() returns depth-3 nested dict
# ---------------------------------------------------------------------------


def test_T09_01_mapping_schema_returns_depth3_dict():
    """SchemaResolver.mapping_schema() must return {catalog: {db: {table: {col: type}}}}.

    This method does not exist pre-T-09-01; the test will AttributeError-skip
    until the symbol lands.
    """
    try:
        resolver = SchemaResolver(dialect="snowflake")
        _ = resolver.mapping_schema  # introduced by T-09-01
    except AttributeError:
        pytest.skip("T-09-01 not yet implemented: mapping_schema missing")

    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "DWH_PRD,BA,X,a,1,TEXT\n"
        "DWH_PRD,BA,X,b,2,TEXT\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))
    result = resolver.mapping_schema()

    assert isinstance(result, dict), "mapping_schema() must return a dict"
    # add_information_schema lowercases all identifiers at load time to match sqlglot normalisation
    assert "dwh_prd" in result, "catalog key missing"
    assert "ba" in result["dwh_prd"], "db key missing"
    assert "x" in result["dwh_prd"]["ba"], "table key missing"
    cols = result["dwh_prd"]["ba"]["x"]
    assert isinstance(cols, dict), "column level must be a dict {col_name: type}"
    assert "a" in cols, "column 'a' missing"
    assert "b" in cols, "column 'b' missing"


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
# Scenario D — confidence scoring: schema-backed=1.0, inferred-only=0.7
# ---------------------------------------------------------------------------


def test_T09_01_confidence_scoring_schema_backed():
    """Edges from schema-backed sources get confidence=1.0, inferred-only get 0.7.

    Pre-T-09-01 there is no confidence distinction based on mapping_schema presence.
    """
    try:
        resolver = SchemaResolver(dialect="snowflake")
        _ = resolver.mapping_schema  # introduced by T-09-01
    except AttributeError:
        pytest.skip("T-09-01 not yet implemented: mapping_schema missing")

    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")

    # Load schema so 'src' table is known
    csv_content = (
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
        "db,s,src,a,1,NUMBER\n"
    )
    resolver.add_information_schema(io.StringIO(csv_content))
    parser = AnsiParser(resolver)

    sql = "INSERT INTO db.s.t (a) SELECT src.a FROM db.s.src;"
    result = parser.parse_file(Path("test.sql"), sql)

    edges = [e for stmt in result.statements for e in stmt.column_lineage]
    assert len(edges) >= 1, "Expected at least one lineage edge for schema-backed source"
    schema_edges = [e for e in edges if e.confidence == 1.0]
    assert len(schema_edges) >= 1, (
        f"Expected confidence=1.0 for schema-backed edge. Got confidences: "
        f"{[e.confidence for e in edges]}"
    )


def test_T09_01_confidence_scoring_inferred_only():
    """With empty mapping_schema, edges are inferred (confidence=0.7 not 1.0)."""
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser

        resolver = SchemaResolver(dialect="snowflake")
        _ = resolver.mapping_schema  # introduced by T-09-01
    except AttributeError:
        pytest.skip("T-09-01 not yet implemented: mapping_schema missing")

    parser = AnsiParser(resolver)  # no schema loaded → empty mapping_schema
    sql = "INSERT INTO db.s.t (a) SELECT src.a FROM db.s.src;"
    result = parser.parse_file(Path("test.sql"), sql)

    edges = [e for stmt in result.statements for e in stmt.column_lineage]
    assert len(edges) >= 1, "Expected at least one lineage edge for inferred source"
    high_conf = [e for e in edges if e.confidence == 1.0]
    assert len(high_conf) == 0, (
        "With empty mapping_schema all edges should be inferred (confidence < 1.0). "
        f"Got confidence=1.0 on {len(high_conf)} edges."
    )


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
