"""E12: LATERAL FLATTEN and JSON path — now resolved by sqlglot.

Both patterns previously produced zero edges; sqlglot now traces them correctly.
These tests assert the expected edges are present.

See plan/sprints/sprint_07_open_ecodes.md § T-07-04 for the deferred-decision rationale.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e12_lateral_flatten_edge(parser):
    """LATERAL FLATTEN produces an edge for the flattened array column."""
    sql = Path(__file__).with_name("e12_lateral_flatten.sql").read_text()
    result = parse(parser, sql, "e12_lateral_flatten.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "ARR" and e[3] == "col" for e in edges_list), (
        f"Expected tbl.arr -> col: {edges_list}"
    )


def test_e12_json_path_edge(parser):
    """JSON path expression produces an edge to the destination column."""
    sql = Path(__file__).with_name("e12_json_path.sql").read_text()
    result = parse(parser, sql, "e12_json_path.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC" and e[3] == "city" for e in edges_list), (
        f"Expected customer_raw.src -> city: {edges_list}"
    )
