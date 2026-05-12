"""E23: Stored procedure body (scripting fallback extraction).

Stored procedures with embedded DML are parsed via regex extraction from the
procedural block. The extracted DML is parsed with SCRIPTING_FALLBACK quality
and produces expected column lineage.
"""

from pathlib import Path

from sqlcg.parsers.base import ParseQuality
from tests.snowflake.conftest import edges, parse


def test_e23_single_insert(parser):
    """Single INSERT in stored procedure is extracted and parsed."""
    sql = Path(__file__).with_name("e23_stored_proc.sql").read_text()
    result = parse(parser, sql, "e23_stored_proc.sql")

    all_edges = edges(result)

    assert any(
        e[0].upper() == "SRC" and e[1].upper() == "A" and e[2] == "dst" and e[3] == "a"
        for e in all_edges
    ), f"Expected SRC.A → dst.a edge: {all_edges}"

    assert result.parse_quality == ParseQuality.SCRIPTING_FALLBACK, (
        f"Stored proc must be marked SCRIPTING_FALLBACK: {result.parse_quality}"
    )


def test_e23_multiple_inserts(parser):
    """Multiple INSERTs in stored procedure are all extracted."""
    sql = Path(__file__).with_name("e23_stored_proc_multi.sql").read_text()
    result = parse(parser, sql, "e23_stored_proc_multi.sql")

    all_edges = edges(result)

    assert any(e[0].upper() == "SRC_ONE" and e[2] == "dst_one" for e in all_edges), (
        f"Expected SRC_ONE → dst_one edge: {all_edges}"
    )

    assert any(e[0].upper() == "SRC_TWO" and e[2] == "dst_two" for e in all_edges), (
        f"Expected SRC_TWO → dst_two edge: {all_edges}"
    )

    assert result.parse_quality == ParseQuality.SCRIPTING_FALLBACK
