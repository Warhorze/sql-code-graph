"""E_date_functions: Unaliased date function expressions.

T-09-03 fix: Unaliased date functions (DATE, YEAR, DATEDIFF) should emit
col_lineage_skip:func_fallback markers instead of passing malformed strings
to sg_lineage. Aliased variants should still produce edges.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_date_unaliased_emits_skip_marker(parser):
    """Unaliased DATE function emits skip marker, no E2 error."""
    sql = Path(__file__).with_name("fixture_date_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_date_unaliased.sql")

    # Should emit skip marker for unaliased function
    assert any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Expected col_lineage_skip:func_fallback in errors: {result.errors}"
    )

    # Should NOT emit E2 (col_lineage: Cannot find column)
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"


def test_date_aliased_produces_edge(parser):
    """Aliased DATE function produces edge for the source column."""
    sql = Path(__file__).with_name("fixture_date_aliased.sql").read_text()
    result = parse(parser, sql, "fixture_date_aliased.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Expected at least 1 edge for aliased DATE, got {all_edges}"

    # Should have edge from boekdatum_fis to d (note: column names may be quoted)
    assert any("BOEKDATUM_FIS" in e[1].upper() and e[3].upper() == "D" for e in all_edges), (
        f"Expected boekdatum_fis -> d edge in {all_edges}"
    )


def test_year_unaliased_emits_skip_marker(parser):
    """Unaliased YEAR function emits skip marker."""
    sql = Path(__file__).with_name("fixture_year_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_year_unaliased.sql")

    # Should emit skip marker for unaliased function
    assert any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Expected col_lineage_skip:func_fallback in errors: {result.errors}"
    )

    # Should NOT emit E2
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"


def test_datediff_unaliased_emits_skip_marker(parser):
    """Unaliased DATEDIFF function emits skip marker."""
    sql = Path(__file__).with_name("fixture_datediff_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_datediff_unaliased.sql")

    # Should emit skip marker for unaliased function
    assert any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Expected col_lineage_skip:func_fallback in errors: {result.errors}"
    )

    # Should NOT emit E2
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"
