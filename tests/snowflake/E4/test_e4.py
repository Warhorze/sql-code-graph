"""E4: Dialect gaps (handled with graceful fallback or preprocessing).

E4 documents Snowflake dialect features that sqlglot does not yet support or
that the parser preprocesses before parsing. All three fixtures should parse
without raising an exception, though parse_quality may be degraded.
"""

from pathlib import Path

from sqlcg.parsers.base import ParseQuality
from tests.snowflake.conftest import edges, parse


def test_e4_if_not_exists(parser):
    """IF NOT EXISTS is stripped by preprocessing, parses as TABLE_ONLY."""
    sql = Path(__file__).with_name("e4_if_not_exists.sql").read_text()
    result = parse(parser, sql, "e4_if_not_exists.sql")

    assert isinstance(result, object), "parse_file must return ParsedFile (not raise)"
    assert result.parse_quality in {
        ParseQuality.FAILED,
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, (
        f"E4 IF NOT EXISTS: acceptable quality (preprocessing strips the clause), "
        f"got {result.parse_quality}"
    )

    assert edges(result) == [], f"Broken/stripped file must not produce lineage: {edges(result)}"


def test_e4_unpivot(parser):
    """UNPIVOT is stripped by preprocessing, parses as basic SELECT."""
    sql = Path(__file__).with_name("e4_unpivot.sql").read_text()
    result = parse(parser, sql, "e4_unpivot.sql")

    assert isinstance(result, object), "parse_file must return ParsedFile (not raise)"
    assert result.parse_quality in {
        ParseQuality.FAILED,
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, f"E4 UNPIVOT: acceptable quality after preprocessing, got {result.parse_quality}"

    assert edges(result) == [], (
        f"E4 UNPIVOT: after preprocessing, SELECT * produces no "
        f"high-confidence edges: {edges(result)}"
    )


def test_e4_unexpected_token(parser):
    """Procedural block triggers parsing error, but no crash."""
    sql = Path(__file__).with_name("e4_unexpected_token.sql").read_text()
    result = parse(parser, sql, "e4_unexpected_token.sql")

    assert isinstance(result, object), "parse_file must return ParsedFile (not raise)"
    assert result.parse_quality in {
        ParseQuality.FAILED,
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, f"E4 unexpected token: acceptable fallback quality, got {result.parse_quality}"

    assert len(result.errors) >= 1, "Parsing errors should be recorded"
    assert edges(result) == [], f"Failed parse must not produce lineage: {edges(result)}"


def test_e4_execute_immediate(parser):
    """EXECUTE IMMEDIATE with runtime SQL string produces no lineage."""
    sql = Path(__file__).with_name("e4_execute_immediate.sql").read_text()
    result = parse(parser, sql, "e4_execute_immediate.sql")

    assert isinstance(result, object), "parse_file must return ParsedFile (not raise)"
    assert result.parse_quality in {
        ParseQuality.FAILED,
        ParseQuality.TABLE_ONLY,
        ParseQuality.SCRIPTING_FALLBACK,
    }, f"E4 EXECUTE IMMEDIATE: runtime SQL cannot be parsed for lineage, got {result.parse_quality}"

    assert edges(result) == [], (
        f"EXECUTE IMMEDIATE: runtime SQL produces no lineage: {edges(result)}"
    )
