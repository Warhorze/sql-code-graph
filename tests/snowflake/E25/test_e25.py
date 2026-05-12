"""E25: Cross-database reference (qualifier preservation).

Three-part identifiers (db.schema.table) are captured in TableRef.catalog/db
and accessible via TableRef.full_id. The edges() helper returns only the bare
name (src.table.name), while edges_full_id() returns the full_id for assertions
that require the qualifier.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, edges_full_id, parse


def test_e25_three_part_qualifier(parser):
    """Three-part identifier preserves qualifier via TableRef.full_id."""
    sql = Path(__file__).with_name("e25_cross_db.sql").read_text()
    result = parse(parser, sql, "e25_cross_db.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Three-part identifier must produce an edge: {all_edges}"

    # The bare table name is still TBL (as sqlglot normalises bare identifiers to UPPERCASE)
    assert any(e[0].upper() == "TBL" for e in all_edges), (
        f"Expected bare table name TBL: {all_edges}"
    )

    # But the fully-qualified name is preserved via edges_full_id()
    all_edges_full = edges_full_id(result)
    assert any(e[0].lower() == "other_db.public.tbl" for e in all_edges_full), (
        f"Fully-qualified name must be preserved via TableRef.full_id: {all_edges_full}"
    )


def test_e25_two_part_qualifier(parser):
    """Two-part identifier (schema.table) preserves qualifier via TableRef.full_id."""
    sql = Path(__file__).with_name("e25_two_part.sql").read_text()
    result = parse(parser, sql, "e25_two_part.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Two-part identifier must produce edge: {all_edges}"

    assert any(e[0].upper() == "TBL" for e in all_edges), (
        f"Expected bare table name TBL: {all_edges}"
    )

    # The fully-qualified name is preserved via edges_full_id()
    all_edges_full = edges_full_id(result)
    assert any(e[0].lower() == "other_schema.tbl" for e in all_edges_full), (
        f"Two-part qualifier must be preserved via TableRef.full_id: {all_edges_full}"
    )
