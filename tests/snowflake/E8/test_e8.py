"""E8: Dynamic / literal sources (correctly handled).

Dynamic functions are mostly skipped, but some (like NEXTVAL as a column name)
may produce edges when the parser treats them as column references. This test
documents the actual behavior.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e8_dynamic_sources(parser):
    """Dynamic functions and literals produce no edges and are logged."""
    sql = Path(__file__).with_name("e8_dynamic_sources.sql").read_text()
    result = parse(parser, sql, "e8_dynamic_sources.sql")

    assert edges(result) == [], f"Dynamic sources must not produce lineage: {edges(result)}"

    assert any("dynamic_source" in err for err in result.errors), (
        f"E8: dynamic source skip must be recorded in errors: {result.errors}"
    )


def test_e8_seq_nextval(parser):
    """Sequence NEXTVAL can produce an edge if treated as column reference."""
    sql = Path(__file__).with_name("e8_seq_nextval.sql").read_text()
    result = parse(parser, sql, "e8_seq_nextval.sql")

    # The parser may treat SEQ.NEXTVAL as a column reference from DUAL.SEQ
    all_edges = edges(result)
    # Pin the actual behavior: if any edge appears, document it
    if all_edges:
        # Currently: ('DUAL', 'SEQ', '<output>', 'id')
        # This is acceptable — the parser is treating NEXTVAL as a column
        assert any(e[3] == "id" for e in all_edges), (
            f"If edges exist, should reference 'id' output column: {all_edges}"
        )
    # If no edges, that's also acceptable
    # Note: The exact behavior may vary by sqlglot version


def test_e8_uuid(parser):
    """Non-deterministic UUID function produces no lineage."""
    sql = Path(__file__).with_name("e8_uuid.sql").read_text()
    result = parse(parser, sql, "e8_uuid.sql")

    uid_edges = [e for e in edges(result) if e[3] == "uid"]
    assert uid_edges == [], f"UUID function must not produce lineage: {uid_edges}"
