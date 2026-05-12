"""Failing acceptance tests for T-09-02 (pure-DDL file skip).

T-09-02 is substantially pre-implemented: _is_pure_ddl_file() and skip_column_lineage
exist in ansi_parser.py. However the plan specifies the error marker must be
"col_lineage_skip:pure_ddl_file" (consistent with the E-code taxonomy). Currently
the code emits "parse_mode:pure_ddl_skip" instead.

These tests fail on the MARKER format until T-09-02 aligns the error code.
Named so ``pytest -k T09_02`` works.
"""

from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver


def _get_parser():
    try:
        from sqlcg.parsers.ansi_parser import AnsiParser
    except ImportError:
        pytest.skip("AnsiParser not importable")
    return AnsiParser(SchemaResolver())


# ---------------------------------------------------------------------------
# Scenario A — Pure DDL file emits the correct skip marker
# ---------------------------------------------------------------------------


def test_T09_02_pure_ddl_file_emits_correct_skip_marker():
    """A pure-DDL file must emit col_lineage_skip:pure_ddl_file (not parse_mode:pure_ddl_skip).

    The plan (T-09-02 Step 2.2) specifies the marker must use the col_lineage_skip:
    prefix to be picked up by _classify_error() in T-09-06 and counted in error_summary.
    Pre-T-09-02 the marker is 'parse_mode:pure_ddl_skip' (wrong bucket prefix).
    """
    parser = _get_parser()
    sql = "CREATE TABLE db.s.t (a INT, b TEXT); CREATE TABLE db.s.u (c DATE);"
    result = parser.parse_file(Path("ddl.sql"), sql)

    # Check for the plan-specified marker (will fail if old marker is used)
    correct_skip = [e for e in result.errors if e == "col_lineage_skip:pure_ddl_file"]
    old_skip = [e for e in result.errors if "pure_ddl_skip" in e]

    assert len(correct_skip) >= 1, (
        f"Expected 'col_lineage_skip:pure_ddl_file' in errors. "
        f"Got errors: {result.errors}. "
        f"(old marker 'parse_mode:pure_ddl_skip' present: {bool(old_skip)})"
    )

    # Defined tables still captured
    assert len(result.defined_tables) >= 2, (
        f"DDL skip must not suppress defined_tables. Got: {result.defined_tables}"
    )

    # No column lineage attempted
    all_edges = [e for stmt in result.statements for e in stmt.column_lineage]
    assert all_edges == [], (
        f"Pure-DDL file must produce zero column lineage edges. Got {len(all_edges)} edges."
    )


# ---------------------------------------------------------------------------
# Scenario B — CTAS is NOT skipped (regression guard)
# ---------------------------------------------------------------------------


def test_T09_02_ctas_not_skipped():
    """CREATE TABLE ... AS SELECT must NOT be classified as pure-DDL.

    This is a positive regression guard — T-09-02 must not break CTAS.
    """
    parser = _get_parser()
    sql = "CREATE TABLE db.s.t AS SELECT src.a FROM db.s.src;"
    result = parser.parse_file(Path("ctas.sql"), sql)

    skip_errors = [e for e in result.errors if "pure_ddl_file" in e]
    assert len(skip_errors) == 0, "CTAS must not be treated as pure-DDL. Got skip marker in errors."

    # CTAS should attempt lineage (may or may not produce edges depending on schema)
    assert not any("pure_ddl_file" in e for e in result.errors), (
        "pure_ddl_file marker must not appear for CTAS."
    )


# ---------------------------------------------------------------------------
# Scenario C — Mixed DDL + DML is NOT skipped (regression guard)
# ---------------------------------------------------------------------------


def test_T09_02_mixed_ddl_dml_not_skipped():
    """A file with both DDL and DML must not be classified as pure-DDL."""
    parser = _get_parser()
    sql = "CREATE TABLE db.s.t (a INT); INSERT INTO db.s.t (a) SELECT src.a FROM db.s.src;"
    result = parser.parse_file(Path("mixed.sql"), sql)

    skip_errors = [e for e in result.errors if "col_lineage_skip:pure_ddl_file" in e]
    assert len(skip_errors) == 0, "Mixed DDL+DML file must not be treated as pure-DDL."
