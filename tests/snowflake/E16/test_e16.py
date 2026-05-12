"""E16: MERGE multi-branch (zero column lineage, documented).

MERGE statements currently produce zero column lineage because the parser does
not extract branch-specific column assignments. This test documents the current
zero-edge state and marks full MERGE support as an inversion target.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e16_merge_match_and_insert(parser):
    """MERGE with MATCHED UPDATE and NOT MATCHED INSERT produces zero edges."""
    sql = Path(__file__).with_name("e16_merge.sql").read_text()
    result = parse(parser, sql, "e16_merge.sql")

    all_edges = edges(result)
    assert all_edges == [], (
        "E16: MERGE currently produces no column lineage; if this fails, E16 was partially fixed"
    )

    # INVERSION TARGET: when E16 fixed, assert at least one edge with
    # dst_table == "DST" and dst_col == "col" from src.col_a (MATCHED branch)
    # and src.col_b (NOT MATCHED branch) — srcs == {"COL_A", "COL_B"}.


def test_e16_merge_delete(parser):
    """MERGE with DELETE branch still produces zero column lineage."""
    sql = Path(__file__).with_name("e16_merge_delete.sql").read_text()
    result = parse(parser, sql, "e16_merge_delete.sql")

    all_edges = edges(result)
    assert all_edges == [], f"E16: MERGE with DELETE still produces no column lineage: {all_edges}"

    # INVERSION TARGET: when E16 fixed, the DELETE branch should not produce
    # column edges, but UPDATE and INSERT branches should.
