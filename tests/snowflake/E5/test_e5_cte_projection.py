"""E5: CTE projection emits an edge into the CTE name as destination.

Guards the Step 2.1 behaviour of T-07-02 in isolation so a regression there
is bisectable without running the heavier anchor suite.
"""

from tests.snowflake.conftest import edges, parse


def test_e5_cte_as_destination(parser):
    """WITH x AS (SELECT a FROM src) SELECT a FROM x emits a CTE-as-destination edge.

    CTE_PROJECTION edge: (SRC, A, x, a)
    Outer edge: (SRC, A, <output>, a)
    """
    sql = "WITH x AS (SELECT a FROM src) SELECT a FROM x"
    result = parse(parser, sql, "e5_cte_projection.sql")

    all_edges = edges(result)
    cte_edges = [e for e in all_edges if e[2].lower() == "x" and e[3].lower() == "a"]
    assert len(cte_edges) >= 1, f"Expected CTE-as-destination edge (SRC, A, x, a): {all_edges}"
    assert any(e[0].upper() == "SRC" for e in cte_edges), (
        f"CTE edge source must be SRC: {cte_edges}"
    )


def test_e5_cte_union_as_destination(parser):
    """UNION ALL CTE emits edges from both branches into the CTE name."""
    sql = (
        "WITH a AS (SELECT col FROM left_tbl), "
        "b AS (SELECT col FROM right_tbl), "
        "combined AS (SELECT col FROM a UNION ALL SELECT col FROM b) "
        "SELECT col FROM combined"
    )
    result = parse(parser, sql, "e5_cte_union.sql")

    all_edges = edges(result)
    combined_edges = [e for e in all_edges if e[2].lower() == "combined"]
    assert len(combined_edges) >= 2, (
        f"UNION ALL CTE must have edges from both branches: {all_edges}"
    )
    sources = {e[0].upper() for e in combined_edges}
    assert "A" in sources or "LEFT_TBL" in sources, (
        f"Expected left branch source in combined edges: {combined_edges}"
    )
    assert "B" in sources or "RIGHT_TBL" in sources, (
        f"Expected right branch source in combined edges: {combined_edges}"
    )
