"""E36: Cross-statement temp table (intra-file resolution works).

The parser correctly resolves temp tables within a single file via sources_map.
The edge shows the transitive expansion (SRC → dst, not t → dst).
The E36 bug is a cross-file problem, documented as an inversion target.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e36_temp_table(parser):
    """Single-file CTAS and INSERT shows intra-file temp table resolution."""
    sql = Path(__file__).with_name("e36_temp_table.sql").read_text()
    result = parse(parser, sql, "e36_temp_table.sql")

    all_edges = edges(result)
    assert len(all_edges) > 0, "Intra-file temp table must produce at least one edge"

    # CTAS edge: SRC.A -> t.a
    assert any(e[0].upper() == "SRC" and e[2] == "t" for e in all_edges), (
        f"Expected CTAS edge SRC.A → t.a: {all_edges}"
    )

    # Resolved INSERT edge: SRC.A -> dst.a (t expanded via sources_map)
    assert any(e[0].upper() == "SRC" and e[2] == "dst" for e in all_edges), (
        f"Expected resolved INSERT edge SRC.A → dst.a: {all_edges}"
    )

    # t must NOT appear as a source table (it is expanded away)
    assert not any(e[0] == "t" for e in all_edges), (
        f"t must not appear as a raw source; edges: {all_edges}"
    )

    # Cross-file case covered by tests/snowflake/E36/test_e36_xfile.py


def test_e36_multiple_temp_uses(parser):
    """Multiple consumer INSERTs from single CTAS both resolve via sources_map."""
    sql = Path(__file__).with_name("e36_temp_multi_use.sql").read_text()
    result = parse(parser, sql, "e36_temp_multi_use.sql")

    all_edges = edges(result)

    # First INSERT resolves through t
    assert any(e[0].upper() == "SRC" and e[2] == "dst_one" for e in all_edges), (
        f"Expected SRC → dst_one edge: {all_edges}"
    )

    # Second INSERT also resolves through t
    assert any(e[0].upper() == "SRC" and e[2] == "dst_two" for e in all_edges), (
        f"Expected SRC → dst_two edge: {all_edges}"
    )

    # t is expanded in both, never appears as a source
    assert not any(e[0] == "t" for e in all_edges), (
        f"t must not appear as a source in multiple-use scenario: {all_edges}"
    )
