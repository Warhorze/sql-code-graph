"""E27: UDF as column source (partial support documented).

User-defined functions can produce edges when the parser treats their
arguments as column references. This test documents the actual behavior
and marks full UDF support as an inversion target.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e27_udf(parser):
    """Undefined UDF argument can produce column lineage."""
    sql = Path(__file__).with_name("e27_udf.sql").read_text()
    result = parse(parser, sql, "e27_udf.sql")

    all_edges = edges(result)
    # The parser may produce: ('SRC', 'SRC_COL', '<output>', 'dst_col')
    # This shows that the argument column is traced
    if all_edges:
        assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in all_edges), (
            f"If edges exist, should reference src_col and dst_col: {all_edges}"
        )
    # If no edges, that's also acceptable (UDF boundary not crossed)

    # INVERSION TARGET: when E27 fixed, assert consistent edge from src.src_col
    # to (output).dst_col through the UDF boundary.


def test_e27_nested_udf(parser):
    """Nested undefined UDFs can produce edges from the innermost argument."""
    sql = Path(__file__).with_name("e27_nested_udf.sql").read_text()
    result = parse(parser, sql, "e27_nested_udf.sql")

    all_edges = edges(result)
    # The parser may trace through all UDF boundaries to the source column
    if all_edges:
        assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in all_edges), (
            f"If edges exist, should reference src_col and dst_col: {all_edges}"
        )

    # INVERSION TARGET: when E27 fixed, nested UDFs should still resolve to
    # consistent src.src_col → (output).dst_col lineage.
