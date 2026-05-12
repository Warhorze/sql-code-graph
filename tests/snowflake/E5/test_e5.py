"""E5: CTE with missing source (current behavior documented).

CTEs with unresolved source tables are resolved with confidence=0.9 by sqlglot
and appear in high-confidence edges. This test documents the current state and
marks the cross-file resolution improvement as an inversion target.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e5_cte_missing_source(parser):
    """CTE resolution traces back to the missing source table."""
    sql = Path(__file__).with_name("e5_cte_missing_source.sql").read_text()
    result = parse(parser, sql, "e5_cte_missing_source.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, (
        f"CTE with missing source must still resolve to an edge: {all_edges}"
    )

    assert any(e[0].upper() == "TABLE_NOT_IN_SCHEMA" for e in all_edges), (
        f"Edge must trace back to the missing table name: {all_edges}"
    )

    # INVERSION TARGET: when E5 cross-file resolution lands, assert
    # the src_table resolves to the *actual* file-qualified table, not
    # the raw name from the missing schema reference.


def test_e5_multi_cte(parser):
    """Multiple CTEs with absent sources both appear in edges."""
    sql = Path(__file__).with_name("e5_multi_cte.sql").read_text()
    result = parse(parser, sql, "e5_multi_cte.sql")

    all_edges = edges(result)
    assert any(e[0].upper() == "ABSENT_ONE" for e in all_edges), (
        f"Expected ABSENT_ONE in edges: {all_edges}"
    )

    assert any(e[0].upper() == "ABSENT_TWO" for e in all_edges), (
        f"Expected ABSENT_TWO in edges: {all_edges}"
    )


def test_e5_nested_cte(parser):
    """Nested CTEs resolve all the way down to the absent root source."""
    sql = Path(__file__).with_name("e5_nested_cte.sql").read_text()
    result = parse(parser, sql, "e5_nested_cte.sql")

    all_edges = edges(result)
    assert any(e[0].upper() == "ABSENT_ROOT" for e in all_edges), (
        f"Nested CTE must resolve to absent root source: {all_edges}"
    )
