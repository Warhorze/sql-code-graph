"""Statements with no scope-buildable body must not be marked parse_failed.

Guards the PR5 measurement fix
([plan doc](../../plan/sprints/graph_health_catalog_and_metrics.md), §7
"PR 5 — ETL extraction recall"):
`build_scope()` returns `None` for nearly all non-`exp.Select`-rooted
statements (TRUNCATE, USE, SET, COMMENT, ALTER, DROP, CREATE ... LIKE/CLONE/
column-defs, INSERT ... VALUES, UPDATE, DELETE, MERGE) by sqlglot design — none
of these can ever produce column lineage, so a falsy/raising `build_scope()` for
them is not a degraded extraction and must not set `parse_failed=True`.
"""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


def _parse(sql: str, dialect: str | None = None):
    parser = get_parser(dialect, SchemaResolver())
    return parser.parse_file(Path("test.sql"), sql)


class TestStructurallyLineageFreeStatementsNotMarkedFailed:
    """OTHER-kind and body-less DDL/DML statements: parse_failed must be False."""

    def test_truncate_table_not_parse_failed(self):
        result = _parse("TRUNCATE TABLE ba.wtfe_verkoopinfo;", dialect="snowflake")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_use_schema_consumed_as_context_not_parse_failed(self):
        # P5 (USE SCHEMA bare-name resolution): SnowflakeParser._transform_statements
        # consumes USE SCHEMA as qualification context — it is not emitted as a
        # statement at all, so it can never be marked parse_failed.
        result = _parse("USE SCHEMA ba_tmp;", dialect="snowflake")
        assert result.statements == []
        assert result.errors == []

    def test_use_schema_context_qualifies_following_bare_ref(self):
        # The consumed USE SCHEMA must actually act as context for what follows.
        result = _parse(
            "USE SCHEMA ba_tmp;\nINSERT INTO target_t SELECT a FROM src_t;",
            dialect="snowflake",
        )
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.parse_failed is False
        referenced = {t.full_id for t in result.referenced_tables}
        assert "ba_tmp.src_t" in referenced, f"expected qualified src, got {referenced}"

    def test_use_statement_ansi_not_parse_failed(self):
        # Non-Snowflake dialects have no _transform_statements override: USE remains
        # an emitted OTHER-kind statement and must not be flagged parse_failed.
        result = _parse("USE my_db;")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_comment_on_table_not_parse_failed(self):
        result = _parse("COMMENT ON TABLE foo IS 'bar';")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_set_session_variable_not_parse_failed(self):
        result = _parse("SET var_delta_value = 5;", dialect="snowflake")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_alter_table_not_parse_failed(self):
        result = _parse("ALTER TABLE foo ADD COLUMN bar INT;", dialect="snowflake")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_drop_table_not_parse_failed(self):
        result = _parse("DROP TABLE foo;", dialect="snowflake")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "OTHER"
        assert stmt.parse_failed is False

    def test_create_table_like_not_parse_failed(self):
        """CREATE TABLE ... LIKE has no AS-SELECT body — never produces lineage."""
        result = _parse(
            "CREATE OR REPLACE TABLE ba_tmp.wtdh_klant LIKE ba.wtdh_klant;",
            dialect="snowflake",
        )
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        assert stmt.parse_failed is False

    def test_create_table_clone_not_parse_failed(self):
        result = _parse(
            "CREATE OR REPLACE TABLE ba.wtfs_clone CLONE ba.wtfs_source;",
            dialect="snowflake",
        )
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        assert stmt.parse_failed is False

    def test_create_table_column_defs_not_parse_failed(self):
        result = _parse("CREATE TABLE users (id INT, name VARCHAR(255));")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        assert stmt.parse_failed is False

    def test_insert_values_not_parse_failed(self):
        """INSERT ... VALUES has no SELECT body — never produces lineage."""
        result = _parse("INSERT INTO products (id, name) VALUES (1, 'Widget');")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "INSERT"
        assert stmt.parse_failed is False

    def test_update_set_from_not_parse_failed(self):
        """UPDATE ... SET ... FROM ... has no extraction path (yet) but is not 'failed'."""
        result = _parse(
            "UPDATE ba.t1 AS t1 SET t1.col = t2.col FROM ba.t2 AS t2 WHERE t1.id = t2.id;",
            dialect="snowflake",
        )
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "UPDATE"
        assert stmt.parse_failed is False

    def test_delete_using_not_parse_failed(self):
        result = _parse(
            "DELETE FROM ba.t1 AS a USING ba.t2 AS b WHERE a.id = b.id;",
            dialect="snowflake",
        )
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "DELETE"
        assert stmt.parse_failed is False


class TestQueryShapedStatementsStillMarkedFailedOnDegradedScope:
    """Select/CTAS-shaped statements: parse_failed semantics are unchanged."""

    def test_simple_select_not_parse_failed(self):
        """A normal SELECT builds a scope fine — parse_failed stays False."""
        result = _parse("SELECT id, name FROM users;")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "SELECT"
        assert stmt.parse_failed is False

    def test_ctas_not_parse_failed(self):
        """CREATE TABLE ... AS SELECT builds a scope fine — parse_failed stays False."""
        result = _parse("CREATE TABLE summary AS SELECT id, name FROM users;")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        assert stmt.parse_failed is False

    def test_insert_select_not_parse_failed(self):
        """INSERT INTO ... SELECT builds a scope fine — parse_failed stays False."""
        result = _parse("INSERT INTO summary SELECT id, name FROM users;")
        assert len(result.statements) == 1
        stmt = result.statements[0]
        assert stmt.kind == "INSERT"
        assert stmt.parse_failed is False
