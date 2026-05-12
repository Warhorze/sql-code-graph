"""E18: IFF / DECODE / NVL2 (mostly working correctly).

Conditional functions like IFF, DECODE, and NVL2 extract edges for all branches.
This test documents the current correct behavior for most cases.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e18_iff(parser):
    """IFF condition extracts edges for both branches."""
    sql = Path(__file__).with_name("e18_iff_decode.sql").read_text()
    result = parse(parser, sql, "e18_iff_decode.sql")

    all_edges = edges(result)

    assert any(e[1].upper() == "A" and e[3] == "col" for e in all_edges), (
        f"Expected A operand in IFF: {all_edges}"
    )

    assert any(e[1].upper() == "B" and e[3] == "col" for e in all_edges), (
        f"Expected B operand in IFF: {all_edges}"
    )

    # Note: IFF both branches produce edges; this is working as of the current
    # sqlglot version. If a future version regresses, add xfail.


def test_e18_decode(parser):
    """DECODE with multiple branches extracts edges for all case values."""
    sql = Path(__file__).with_name("e18_decode.sql").read_text()
    result = parse(parser, sql, "e18_decode.sql")

    all_edges = edges(result)

    assert any(e[1].upper() == "ACTIVE_COL" and e[3] == "result" for e in all_edges), (
        f"Expected active_col in DECODE: {all_edges}"
    )

    assert any(e[1].upper() == "BACKUP_COL" and e[3] == "result" for e in all_edges), (
        f"Expected backup_col in DECODE: {all_edges}"
    )

    assert any(e[1].upper() == "DEFAULT_COL" and e[3] == "result" for e in all_edges), (
        f"Expected default_col in DECODE: {all_edges}"
    )


def test_e18_nvl2(parser):
    """NVL2 extracts edges for both true and false branches."""
    sql = Path(__file__).with_name("e18_nvl2.sql").read_text()
    result = parse(parser, sql, "e18_nvl2.sql")

    all_edges = edges(result)

    assert any(e[1].upper() == "A" and e[3] == "result" for e in all_edges), (
        f"Expected A branch in NVL2: {all_edges}"
    )

    assert any(e[1].upper() == "B" and e[3] == "result" for e in all_edges), (
        f"Expected B branch in NVL2: {all_edges}"
    )
