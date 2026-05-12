"""Failing acceptance tests for plan/snowflake_en_test_suite.md.

Each test corresponds to a corrected assertion in the reviewed plan.
Tests must FAIL (or skip) before the developer implements the fixture/test files.
Once fixture + test files from each step land, these gate tests can be retired.

Named test_<step>_<description> so `pytest -k plan_review` runs the full set.
"""

from pathlib import Path

import pytest

try:
    from sqlcg.lineage.schema_resolver import SchemaResolver  # noqa: F401
    from sqlcg.parsers.base import ParseQuality  # noqa: F401
    from sqlcg.parsers.snowflake_parser import SnowflakeParser  # noqa: F401
except ImportError:
    pytest.skip("core imports unavailable", allow_module_level=True)


def _parser():
    return SnowflakeParser(schema_resolver=SchemaResolver())


def _edges(result):
    return [
        (edge.src.table.name, edge.src.name, edge.dst.table.name, edge.dst.name)
        for stmt in result.statements
        for edge in stmt.column_lineage
        if edge.confidence > 0.0
    ]


# ---------------------------------------------------------------------------
# BLOCKER-1 (E3): ALTER DYNAMIC TABLE produces SCRIPTING_FALLBACK, not TABLE_ONLY
# Gate: test that both TABLE_ONLY and SCRIPTING_FALLBACK are acceptable for DDL-only.
# ---------------------------------------------------------------------------
def test_plan_review_e3_ddl_quality_allows_scripting_fallback():
    """E3 fixture must accept SCRIPTING_FALLBACK as a valid DDL-only outcome.

    ALTER DYNAMIC TABLE RESUME falls through to exp.Command in sqlglot, which
    causes AnsiParser to set parse_quality = SCRIPTING_FALLBACK, not TABLE_ONLY.
    The plan was corrected to allow both; this test gates that assertion.
    """
    p = _parser()
    sql = "ALTER DYNAMIC TABLE my_table RESUME;"
    result = p.parse_file(Path("test_e3.sql"), sql)

    assert _edges(result) == [], "DDL-only file must produce no edges"
    assert result.parse_quality in {
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, (
        f"DDL-only file parse_quality must be TABLE_ONLY or SCRIPTING_FALLBACK, "
        f"got {result.parse_quality!r}"
    )
    # Must not hard-crash
    assert not any("Exception" in e or "crash" in e for e in result.errors)


# ---------------------------------------------------------------------------
# BLOCKER-2 (E16): MERGE produces zero column lineage — not "at least one edge"
# ---------------------------------------------------------------------------
def test_plan_review_e16_merge_produces_zero_edges():
    """E16 MERGE must produce zero high-confidence edges (current state).

    The original plan asserted 'at least one edge exists', which is wrong —
    MERGE statements produce no column_lineage. This test gates the corrected
    zero-edge assertion with INVERSION TARGET.
    """
    p = _parser()
    sql = (
        "MERGE INTO dst USING src ON dst.id = src.id\n"
        "WHEN MATCHED THEN UPDATE SET col = src.col_a\n"
        "WHEN NOT MATCHED THEN INSERT (col) VALUES (src.col_b);"
    )
    result = p.parse_file(Path("test_e16.sql"), sql)

    # INVERSION TARGET: when E16 fixed, assert edges with dst_table=="DST"
    # and srcs == {"COL_A", "COL_B"} (both branches captured).
    assert _edges(result) == [], (
        "E16: MERGE currently produces no column lineage; "
        "if this fails, E16 was partially fixed and the test must be updated"
    )


# ---------------------------------------------------------------------------
# BLOCKER-3 (E36): intra-file temp table IS resolved; E36 bug is cross-file only
# ---------------------------------------------------------------------------
def test_plan_review_e36_intrafile_resolves_ctas_through_sources_map():
    """E36 single-file fixture shows intra-file resolution works via sources_map.

    The original plan asserted ('t', 'a', 'dst', 'a') is ABSENT. It is absent,
    but ('SRC', 'A', 'dst', 'a') IS present (t is expanded, not forwarded raw).
    This test pins the correct observable state.
    """
    p = _parser()
    sql = "CREATE TEMP TABLE t AS SELECT a FROM src;\nINSERT INTO dst SELECT a FROM t;"
    result = p.parse_file(Path("test_e36.sql"), sql)

    all_edges = _edges(result)
    assert len(all_edges) > 0, "E36: intra-file sources_map must produce at least one edge"

    # CTAS edge: SRC.A -> t.a
    assert any(e[0].upper() == "SRC" and e[2] == "t" for e in all_edges), (
        f"Expected CTAS edge SRC.A -> t.a in {all_edges}"
    )

    # Resolved INSERT edge: SRC.A -> dst.a  (t expanded via sources_map)
    assert any(e[0].upper() == "SRC" and e[2] == "dst" for e in all_edges), (
        f"Expected resolved INSERT edge SRC.A -> dst.a in {all_edges}"
    )

    # t must NOT appear as a source table (it is expanded away)
    assert not any(e[0] == "t" for e in all_edges), (
        f"t must not appear as a raw source table; edges: {all_edges}"
    )

    # Cross-file case covered by tests/snowflake/E36/test_e36_xfile.py


# ---------------------------------------------------------------------------
# BLOCKER-4 (E5): CTE resolves to src table with confidence=0.9 — not confidence==0.0
# ---------------------------------------------------------------------------
def test_plan_review_e5_cte_resolves_with_positive_confidence():
    """E5: CTE referencing a missing source resolves with confidence=0.9, not 0.0.

    The original plan counted placeholders with confidence==0.0. Actual behaviour:
    sqlglot traces the CTE body to the underlying table with 0.9 confidence.
    The plan was corrected to assert the edge IS present with the source table name.
    """
    p = _parser()
    sql = "WITH x AS (SELECT a FROM table_not_in_schema)\nSELECT a FROM x;"
    result = p.parse_file(Path("test_e5.sql"), sql)

    all_edges = _edges(result)
    assert len(all_edges) >= 1, (
        "E5: CTE with missing source must resolve to an edge (not zero edges)"
    )
    assert any(e[0].upper() == "TABLE_NOT_IN_SCHEMA" for e in all_edges), (
        f"E5: src_table must be TABLE_NOT_IN_SCHEMA (traced through CTE); "
        f"got {[e[0] for e in all_edges]}"
    )

    # INVERSION TARGET: when E5 cross-file resolution lands, assert src_table
    # resolves to the file-qualified table, not just the raw missing-schema name.


# ---------------------------------------------------------------------------
# BLOCKER-5 (case sensitivity): bare identifiers returned UPPERCASE from lineage walker
# ---------------------------------------------------------------------------
def test_plan_review_case_sensitivity_bare_identifiers_are_uppercase():
    """Edge tuples from the edges() helper use UPPERCASE for bare unquoted identifiers.

    The original plan used lowercase ('src', 'x', ...) which would always fail.
    This test pins the actual casing so assertion authors know what to compare against.
    """
    p = _parser()
    sql = "SELECT x + y AS my_col FROM src;"
    result = p.parse_file(Path("test_e2.sql"), sql)

    all_edges = _edges(result)
    assert len(all_edges) >= 1, "E2: aliased expression must produce edges"

    src_tables = {e[0] for e in all_edges}
    src_cols = {e[1] for e in all_edges}
    dst_cols = {e[3] for e in all_edges}

    # Bare table 'src' -> 'SRC' in lineage walker output
    assert "SRC" in src_tables, (
        f"Bare table 'src' must appear as 'SRC' (uppercase) in edges; got {src_tables}"
    )
    # Bare column 'x' -> 'X'
    assert "X" in src_cols, (
        f"Bare column 'x' must appear as 'X' (uppercase) in edges; got {src_cols}"
    )
    # Aliased output 'my_col' retains its declared case
    assert "my_col" in dst_cols, (
        f"Aliased column 'my_col' must retain its declared case; got {dst_cols}"
    )


# ---------------------------------------------------------------------------
# Structural gate: plan requires these test files to exist
# Tests below FAIL until the developer creates the files in Phase 1 + Phase 2.
# ---------------------------------------------------------------------------
_REQUIRED_TEST_FILES = [
    "anchors/__init__.py",
    "anchors/test_anchor_omloopsnelheid.py",
    "anchors/test_anchor_ma_aantal_op_order.py",
    "E2/test_e2.py",
    "E3/test_e3.py",
    "E4/test_e4.py",
    "E5/test_e5.py",
    "E8/test_e8.py",
    "E12/test_e12.py",
    "E16/test_e16.py",
    "E18/test_e18.py",
    "E21/test_e21.py",
    "E23/test_e23.py",
    "E25/test_e25.py",
    "E27/test_e27.py",
    "E36/test_e36.py",
]

_REQUIRED_FIXTURE_FILES = [
    "anchors/fixture_omloopsnelheid.sql",
    "anchors/fixture_source.sql",
    "anchors/fixture_etl.sql",
    "anchors/fixture_semantic.sql",
    "E2/e2_expr_alias.sql",
    "E3/e3_ddl_only.sql",
    "E4/e4_if_not_exists.sql",
    "E4/e4_unpivot.sql",
    "E4/e4_unexpected_token.sql",
    "E5/e5_cte_missing_source.sql",
    "E8/e8_dynamic_sources.sql",
    "E12/e12_lateral_flatten.sql",
    "E16/e16_merge.sql",
    "E18/e18_iff_decode.sql",
    "E21/e21_alias_forward_ref.sql",
    "E23/e23_stored_proc.sql",
    "E25/e25_cross_db.sql",
    "E27/e27_udf.sql",
    "E36/e36_temp_table.sql",
]

_SUITE_ROOT = Path(__file__).parent


@pytest.mark.parametrize("rel_path", _REQUIRED_TEST_FILES)
def test_plan_review_required_test_file_exists(rel_path):
    """Each test file listed in the plan must exist before the suite is complete."""
    target = _SUITE_ROOT / rel_path
    assert target.exists(), (
        f"Missing required test file: {target}\n"
        f"Implement Phase 0/1/2 of plan/snowflake_en_test_suite.md"
    )


@pytest.mark.parametrize("rel_path", _REQUIRED_FIXTURE_FILES)
def test_plan_review_required_fixture_file_exists(rel_path):
    """Each SQL fixture listed in the plan must exist before the suite is complete."""
    target = _SUITE_ROOT / rel_path
    assert target.exists(), (
        f"Missing required SQL fixture: {target}\n"
        f"Implement Phase 0/1/2 of plan/snowflake_en_test_suite.md"
    )
