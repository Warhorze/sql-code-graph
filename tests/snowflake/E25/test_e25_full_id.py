"""E25: verify that TableRef.full_id preserves qualifiers.

This diagnostic test confirms whether the qualifier information (catalog/schema)
is actually captured in TableRef.catalog/db fields.
"""

from pathlib import Path

from tests.snowflake.conftest import edges_full_id, parse


def test_e25_three_part_full_id(parser):
    """Verify three-part identifier is captured in TableRef.full_id."""
    sql = Path(__file__).with_name("e25_cross_db.sql").read_text()
    result = parse(parser, sql, "e25_cross_db.sql")
    edges = edges_full_id(result)
    assert any(e[0].lower() == "other_db.public.tbl" for e in edges), (
        f"Three-part qualifier must survive on TableRef.full_id: {edges}"
    )


def test_e25_two_part_full_id(parser):
    """Verify two-part identifier is captured in TableRef.full_id."""
    sql = Path(__file__).with_name("e25_two_part.sql").read_text()
    result = parse(parser, sql, "e25_two_part.sql")
    edges = edges_full_id(result)
    assert any(e[0].lower() == "other_schema.tbl" for e in edges), (
        f"Two-part qualifier must survive on TableRef.full_id: {edges}"
    )
