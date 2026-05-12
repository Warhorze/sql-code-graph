"""E3: DDL-only file (handled correctly).

DDL statements that do not produce lineage edges are expected to return
parse_quality in {TABLE_ONLY, SCRIPTING_FALLBACK} and zero high-confidence edges.
"""

from pathlib import Path

from sqlcg.parsers.base import ParseQuality
from tests.snowflake.conftest import edges, parse


def test_e3_alter_dynamic_table(parser):
    """ALTER DYNAMIC TABLE RESUME produces no edges, acceptable parse quality."""
    sql = Path(__file__).with_name("e3_ddl_only.sql").read_text()
    result = parse(parser, sql, "e3_ddl_only.sql")

    assert edges(result) == [], f"DDL-only file must produce no edges: {edges(result)}"

    assert result.parse_quality in {
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, (
        f"DDL-only file parse_quality must be TABLE_ONLY or SCRIPTING_FALLBACK, "
        f"got {result.parse_quality}"
    )

    assert not any("Exception" in e or "crash" in e for e in result.errors), (
        f"No exceptions in errors: {result.errors}"
    )


def test_e3_alter_table(parser):
    """ALTER TABLE ADD COLUMN produces no edges."""
    sql = Path(__file__).with_name("e3_alter_table.sql").read_text()
    result = parse(parser, sql, "e3_alter_table.sql")

    assert edges(result) == [], f"DDL-only file must produce no edges: {edges(result)}"

    assert result.parse_quality in {
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, f"Acceptable DDL quality, got {result.parse_quality}"


def test_e3_create_sequence(parser):
    """CREATE SEQUENCE produces no edges."""
    sql = Path(__file__).with_name("e3_create_sequence.sql").read_text()
    result = parse(parser, sql, "e3_create_sequence.sql")

    assert edges(result) == [], f"DDL-only file must produce no edges: {edges(result)}"

    assert result.parse_quality in {
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, f"Acceptable DDL quality, got {result.parse_quality}"
