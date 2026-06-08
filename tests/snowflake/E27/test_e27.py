"""E27: UDF as column source — now resolved by sqlglot.

UDF arguments were previously untraceable; sqlglot now produces edges through
the UDF boundary. These tests assert the expected edges are present.

See plan/sprints/sprint_07_open_ecodes.md § T-07-05 for the deferred-decision rationale.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e27_udf_edge(parser):
    """UDF argument produces column lineage through the UDF boundary."""
    sql = Path(__file__).with_name("e27_udf.sql").read_text()
    result = parse(parser, sql, "e27_udf.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in edges_list), (
        f"Expected src.src_col -> dst_col: {edges_list}"
    )


def test_e27_nested_udf_edge(parser):
    """Nested UDF arguments produce column lineage through the UDF boundary."""
    sql = Path(__file__).with_name("e27_nested_udf.sql").read_text()
    result = parse(parser, sql, "e27_nested_udf.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in edges_list), (
        f"Expected nested UDFs: src.src_col -> dst_col: {edges_list}"
    )
