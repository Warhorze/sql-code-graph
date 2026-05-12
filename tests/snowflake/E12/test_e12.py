"""E12: LATERAL FLATTEN and JSON path (partial support documented).

LATERAL FLATTEN and JSON path expressions can produce edges in some cases
when the parser resolves them to column references. This test documents
the actual behavior and marks improvements as inversion targets.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e12_lateral_flatten(parser):
    """LATERAL FLATTEN can produce edges for the flattened array column."""
    sql = Path(__file__).with_name("e12_lateral_flatten.sql").read_text()
    result = parse(parser, sql, "e12_lateral_flatten.sql")

    all_edges = edges(result)
    # The parser may produce: ('TBL', 'ARR', '<output>', 'col')
    if all_edges:
        # If any edge, should involve the arr column
        assert any(e[1].upper() == "ARR" and e[3] == "col" for e in all_edges), (
            f"If edges exist, should reference arr column: {all_edges}"
        )
    # If no edges, that's also acceptable (FLATTEN not fully supported)

    # INVERSION TARGET: when E12 fixed, assert consistent edge from tbl.arr


def test_e12_json_path(parser):
    """JSON path expressions can produce edges if resolved to columns."""
    sql = Path(__file__).with_name("e12_json_path.sql").read_text()
    result = parse(parser, sql, "e12_json_path.sql")

    all_edges = edges(result)
    # The parser may produce: ('CUSTOMER_RAW', 'SRC', '<output>', 'city')
    # documenting that the src column is involved
    if all_edges:
        assert any(e[1].upper() == "SRC" and e[3] == "city" for e in all_edges), (
            f"If edges exist, should reference src column and city output: {all_edges}"
        )

    # INVERSION TARGET: when E12 semi-structured JSON path resolution lands,
    # assert consistent edge from customer_raw.src to (output).city
