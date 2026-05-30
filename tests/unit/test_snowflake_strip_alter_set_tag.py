"""T-C: Verify SnowflakeParser strips ALTER ... SET TAG statements (Gap 4b).

This test confirms that standalone ALTER TABLE/VIEW ... MODIFY COLUMN ... SET TAG ...
statements are removed by _preprocess_snowflake_sql, preventing sqlglot from emitting
exp.Command nodes and "unsupported syntax" errors.
"""

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def test_preprocess_strips_alter_set_tag():
    """Verify _preprocess_snowflake_sql removes standalone ALTER SET TAG statements.

    Use the fixture SQL from production (real Snowflake statement):
        ALTER VIEW IA_SEMANTIC."Budget voorraad IGDC BIP" MODIFY COLUMN
        "Week koppelcode" SET TAG IA_SEMANTIC.weekcode='wk';

    Assert that after preprocessing, the result is empty or whitespace only.
    """
    fixture = (
        'ALTER VIEW IA_SEMANTIC."Budget voorraad IGDC BIP" '
        "MODIFY COLUMN \"Week koppelcode\" SET TAG IA_SEMANTIC.weekcode='wk';"
    )

    result = SnowflakeParser._preprocess_snowflake_sql(fixture)

    assert result.strip() == "", (
        f"ALTER ... SET TAG should be completely stripped, but got: {repr(result)}"
    )


def test_parse_excludes_alter_set_tag_from_ast():
    """Verify SnowflakeParser.parse_file() excludes ALTER SET TAG statements from AST.

    Build a buffer with:
    - one CREATE VIEW statement
    - two ALTER ... SET TAG statements

    Assert:
    - exactly one CREATE VIEW statement in the result (via parse_file statements)
    - zero errors mentioning "unsupported syntax" or "Command"
    """
    from pathlib import Path

    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(resolver)

    buffer_text = """
    CREATE VIEW test_view AS SELECT 1 AS col_a;
    ALTER TABLE test_view MODIFY COLUMN col_a SET TAG some_tag='value1';
    ALTER VIEW test_view MODIFY COLUMN col_a SET TAG another_tag='value2';
    """

    # Parse the buffer via parse_file (which applies preprocessing)
    result = parser.parse_file(Path("test.sql"), buffer_text)

    # Assert exactly one statement (the CREATE VIEW; ALTER statements should be stripped)
    assert len(result.statements) == 1, (
        f"Expected exactly 1 statement after preprocessing, got {len(result.statements)}"
    )

    # Assert the statement is a CREATE (CREATE VIEW)
    assert result.statements[0].kind == "CREATE_VIEW", (
        f"Expected CREATE_VIEW, got {result.statements[0].kind}"
    )

    # Assert zero errors mentioning "unsupported syntax" or "Command"
    unsupported_errors = [
        e for e in result.errors if "unsupported syntax" in e.lower() or "command" in e.lower()
    ]
    assert len(unsupported_errors) == 0, (
        f"Expected no 'unsupported syntax' or 'Command' errors, but found: {unsupported_errors}"
    )


def test_preprocess_does_not_strip_unrelated_alter():
    """Verify that unrelated ALTER statements (no MODIFY COLUMN SET TAG) survive preprocessing.

    Use a plain ALTER TABLE ... ADD COLUMN statement (no SET TAG).
    Assert it survives the preprocess step unchanged.
    """
    unrelated_alter = "ALTER TABLE foo ADD COLUMN bar INT;"

    result = SnowflakeParser._preprocess_snowflake_sql(unrelated_alter)

    assert result == unrelated_alter, (
        f"ALTER without SET TAG should not be stripped, but result differs. "
        f"Expected: {repr(unrelated_alter)}, got: {repr(result)}"
    )
