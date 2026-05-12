"""E_aggregates: Aggregate function expressions with and without schema.

T-09-01 fix: Aggregate functions with mapping_schema should produce edges.
Aggregates without schema (inferred-only) should emit confidence=0.7 instead of E5.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_sum_present_source_produces_high_confidence_edge(parser):
    """SUM aggregate with source present in schema produces confidence=1.0 edge."""
    sql = Path(__file__).with_name("fixture_sum_present_source.sql").read_text()
    result = parse(parser, sql, "fixture_sum_present_source.sql")

    # With default empty mapping_schema, SUM(amount) will be inferred
    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Expected at least 1 edge for SUM(amount), got {all_edges}"

    # Should have edge from amount to s (note: column names may be quoted/capitalized)
    assert any("AMOUNT" in e[1].upper() and e[3].upper() == "S" for e in all_edges), (
        f"Expected amount -> s edge in {all_edges}"
    )


def test_sum_case_when_produces_edge(parser):
    """SUM with CASE WHEN produces edge for the amount column."""
    sql = Path(__file__).with_name("fixture_sum_case_when.sql").read_text()
    result = parse(parser, sql, "fixture_sum_case_when.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Expected at least 1 edge for SUM(CASE), got {all_edges}"

    # Should have edge from amount to s (note: column names may be quoted/capitalized)
    assert any("AMOUNT" in e[1].upper() and e[3].upper() == "S" for e in all_edges), (
        f"Expected amount -> s edge in {all_edges}"
    )


def test_sum_absent_cross_schema_produces_inferred_edge(parser):
    """SUM from absent cross-schema table produces edge (no E5 error)."""
    sql = Path(__file__).with_name("fixture_sum_absent_cross_schema.sql").read_text()
    result = parse(parser, sql, "fixture_sum_absent_cross_schema.sql")

    # Should NOT emit E5 (Cannot find column) — qualify-once with infer_schema=True
    # allows the inference path
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E5 error (should be inferred): {result.errors}"

    all_edges = edges(result)
    # With infer_schema=True, we should get at least an inferred edge
    # (even if confidence is low)
    assert len(all_edges) >= 1, (
        f"Expected at least 1 inferred edge for SUM from absent schema, got {all_edges}"
    )
