"""E25: Cross-database reference (bare name preserved, qualifier lost).

Three-part identifiers (db.schema.table) are parsed but the catalog/database
parts are lost in the LineageEdge because the src.table.name is just the bare
table name. This test documents the current limitation.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e25_three_part_qualifier(parser):
    """Three-part identifier produces edge with bare table name only."""
    sql = Path(__file__).with_name("e25_cross_db.sql").read_text()
    result = parse(parser, sql, "e25_cross_db.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Three-part identifier must produce an edge: {all_edges}"

    assert any(e[0].upper() == "TBL" for e in all_edges), (
        f"Expected bare table name TBL: {all_edges}"
    )

    assert not any(e[0] == "other_db.public.tbl" for e in all_edges), (
        f"Fully-qualified name is NOT preserved (E25 bug): {all_edges}"
    )

    # INVERSION TARGET: when E25 cross-db resolution lands, assert
    # src_table carries the fully-qualified "other_db.public.tbl" name.


def test_e25_two_part_qualifier(parser):
    """Two-part identifier (schema.table) also produces bare name only."""
    sql = Path(__file__).with_name("e25_two_part.sql").read_text()
    result = parse(parser, sql, "e25_two_part.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Two-part identifier must produce edge: {all_edges}"

    assert any(e[0].upper() == "TBL" for e in all_edges), (
        f"Expected bare table name TBL: {all_edges}"
    )

    # Document whether schema is preserved
    schema_preserved = any("OTHER_SCHEMA" in e[0].upper() for e in all_edges)
    if not schema_preserved:
        # INVERSION TARGET: when E25 schema qualifier lands, assert src_table
        # carries "other_schema.tbl"
        pass
