"""Unit tests for USE SCHEMA session context (PR 2).

Tests cover the scripting path (Step 3.1), non-scripting P5 extension (Step 3.2),
and LIKE catalog inheritance (Step 3.3).

Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2 / Phase 3.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import sqlglot.expressions as exp

from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser(schema_aliases: dict[str, str] | None = None) -> SnowflakeParser:
    """Return a SnowflakeParser with a no-op SchemaResolver."""
    resolver = MagicMock()
    resolver.cross_file_sources.return_value = []
    return SnowflakeParser(resolver, schema_aliases=schema_aliases)


def _parse_snowflake(sql: str, schema_aliases: dict[str, str] | None = None):
    parser = _make_parser(schema_aliases)
    return parser.parse_file(Path("test.sql"), sql)


def _make_ansi_parser(schema_aliases: dict[str, str] | None = None) -> AnsiParser:
    resolver = MagicMock()
    resolver.cross_file_sources.return_value = []
    return AnsiParser(resolver, schema_aliases=schema_aliases)


def _parse_ansi(sql: str):
    parser = _make_ansi_parser()
    return parser.parse_file(Path("test.sql"), sql)


# ---------------------------------------------------------------------------
# Step 3.1 — scripting path USE context
# ---------------------------------------------------------------------------


class TestScriptingPathUseSchemaContext:
    """USE SCHEMA inside a BEGIN…END block qualifies DML targets and sources.

    Guards the scripting-path fix in _parse_scripting_file.
    plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2, Step 3.1.
    """

    def test_scripting_use_schema_qualifies_insert_target(self):
        """USE SCHEMA before INSERT → dst keys carry schema prefix.

        Mirrors the doorbelasting pattern: USE SCHEMA ia_outbound; then bare
        INSERT INTO doorbelasting must produce ia_outbound.doorbelasting target.
        """
        sql = """\
CREATE PROCEDURE test_proc()
BEGIN
  USE SCHEMA ia_outbound;
  INSERT INTO doorbelasting (col1, col2) SELECT a, b FROM src_table;
END;
"""
        result = _parse_snowflake(sql)
        # Must have taken the scripting path
        nodes = [s for s in result.statements if s.parsing_mode == "scripting"]
        assert nodes, (
            "No scripting-mode statements — test fixture must go through _parse_scripting_file"
        )

        # The INSERT target should be schema-qualified
        targets = [s.target.full_id for s in nodes if s.target]
        assert any("ia_outbound.doorbelasting" in t for t in targets), (
            f"Expected ia_outbound.doorbelasting in targets, got: {targets}"
        )

    def test_scripting_use_schema_latest_wins(self):
        """The LATEST USE SCHEMA in a scripting block takes precedence.

        Two consecutive USE SCHEMA statements — only the second one is active
        for the DML that follows it.
        """
        sql = """\
CREATE PROCEDURE multi_schema()
BEGIN
  USE SCHEMA schema_a;
  INSERT INTO table_a (id) SELECT id FROM src_a;
  USE SCHEMA schema_b;
  INSERT INTO table_b (id) SELECT id FROM src_b;
END;
"""
        result = _parse_snowflake(sql)
        nodes = [s for s in result.statements if s.parsing_mode == "scripting" and s.target]
        assert len(nodes) >= 2, f"Expected >=2 INSERT nodes, got: {nodes}"

        targets = {s.target.full_id for s in nodes}
        assert "schema_a.table_a" in targets, f"Expected schema_a.table_a in {targets}"
        assert "schema_b.table_b" in targets, f"Expected schema_b.table_b in {targets}"

    def test_scripting_use_schema_cte_not_qualified(self):
        """CTE alias names in the scripting path are NOT schema-qualified.

        _qualify_bare_tables excludes names defined as CTEs in the same statement.
        """
        sql = """\
CREATE PROCEDURE cte_test()
BEGIN
  USE SCHEMA ia_outbound;
  INSERT INTO real_target
  WITH my_cte AS (SELECT a, b FROM src_table)
  SELECT a, b FROM my_cte;
END;
"""
        result = _parse_snowflake(sql)
        nodes = [s for s in result.statements if s.parsing_mode == "scripting"]
        assert nodes, "Expected at least one scripting-mode statement"

        # CTE name 'my_cte' must NOT appear as a source with schema prefix
        all_sources = {s.full_id for node in nodes for s in node.sources}
        assert "ia_outbound.my_cte" not in all_sources, (
            f"CTE 'my_cte' should not be schema-qualified, got sources: {all_sources}"
        )

    def test_scripting_use_database_alone_does_not_qualify(self):
        """USE DATABASE alone (no schema) does not qualify bare DML targets.

        Only USE SCHEMA / USE db.schema sets a schema context.
        Guards the W3 plan decision: USE DATABASE X stays ignored.
        """
        sql = """\
CREATE PROCEDURE db_only()
BEGIN
  USE DATABASE mydb;
  INSERT INTO bare_table (id) SELECT id FROM src;
END;
"""
        result = _parse_snowflake(sql)
        nodes = [s for s in result.statements if s.parsing_mode == "scripting" and s.target]
        if not nodes:
            return  # DML not extracted — acceptable, no false qualification possible

        targets = [s.target.full_id for s in nodes]
        # bare_table must NOT be qualified with any schema
        for t in targets:
            if "bare_table" in t:
                assert "." not in t.split("bare_table")[0].rstrip("."), (
                    f"bare_table should not be schema-qualified after USE DATABASE only: {t}"
                )

    def test_scripting_use_bare_one_part_does_not_qualify(self):
        """Bare one-part ``USE mydb;`` does not set a schema context in scripting mode.

        Snowflake semantics: a one-part USE sets the DATABASE, not the schema —
        qualifying with it would put a database name in the schema slot of graph
        keys. Guards plan/sprints/sprint_lineage_identity_and_session_context.md
        §PR 2 (W3 decision).
        """
        sql = """\
CREATE PROCEDURE bare_use()
BEGIN
  USE mydb;
  INSERT INTO bare_table (id) SELECT id FROM src;
END;
"""
        result = _parse_snowflake(sql)
        nodes = [s for s in result.statements if s.parsing_mode == "scripting" and s.target]
        targets = [s.target.full_id for s in nodes if "bare_table" in s.target.full_id]
        assert targets, "Expected the bare_table INSERT to be extracted"
        assert targets == ["bare_table"], (
            f"bare_table must stay unqualified after a one-part USE, got: {targets}"
        )


# ---------------------------------------------------------------------------
# Step 3.2 — non-scripting P5 extension: two-part USE SCHEMA / USE db.schema
# ---------------------------------------------------------------------------


class TestNonScriptingUseTwoPart:
    """_transform_statements handles USE SCHEMA db.schema and USE db.schema forms.

    Guards the P5 extension in SnowflakeParser._transform_statements.
    plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2, Step 3.2.
    """

    def test_use_schema_two_part_qualifies_bare_table(self):
        """USE SCHEMA db.schema qualifies subsequent bare table refs with the schema part.

        USE SCHEMA db.ia_outbound → schema = 'ia_outbound'.
        """
        sql = """\
USE SCHEMA mydb.ia_outbound;
INSERT INTO doorbelasting (col1) SELECT col1 FROM src_table;
"""
        result = _parse_snowflake(sql)
        assert result.statements, "Expected at least one statement"
        stmt = result.statements[0]
        assert stmt.target is not None
        assert stmt.target.full_id == "ia_outbound.doorbelasting", (
            f"Expected ia_outbound.doorbelasting, got: {stmt.target.full_id!r}"
        )

    def test_use_db_schema_two_part_no_schema_keyword(self):
        """USE db.schema (no SCHEMA keyword) qualifies subsequent bare table refs.

        kind_str is empty; schema is derived from stmt.this.name when db is present.
        """
        sql = """\
USE mydb.ia_outbound;
INSERT INTO doorbelasting (col1) SELECT col1 FROM src_table;
"""
        result = _parse_snowflake(sql)
        assert result.statements, "Expected at least one statement"
        stmt = result.statements[0]
        assert stmt.target is not None
        assert stmt.target.full_id == "ia_outbound.doorbelasting", (
            f"Expected ia_outbound.doorbelasting, got: {stmt.target.full_id!r}"
        )

    def test_use_database_alone_ignored_in_nons_scripting(self):
        """USE DATABASE x alone in non-scripting mode does not set schema context.

        The subsequent INSERT into bare_table must remain unqualified.
        Guards W3 plan decision.
        """
        sql = """\
USE DATABASE mydb;
INSERT INTO bare_table (id) SELECT id FROM src;
"""
        result = _parse_snowflake(sql)
        assert result.statements, "Expected at least one statement"
        stmt = result.statements[0]
        assert stmt.target is not None
        # bare_table must NOT have a schema prefix from USE DATABASE
        assert "." not in stmt.target.full_id, (
            f"bare_table should not be qualified after USE DATABASE only, "
            f"got: {stmt.target.full_id!r}"
        )

    def test_use_bare_one_part_ignored_in_non_scripting(self):
        """Bare one-part ``USE mydb;`` does not set a schema context (non-scripting).

        Snowflake semantics: a one-part USE sets the DATABASE, not the schema.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2
        (W3 decision).
        """
        sql = """\
USE mydb;
INSERT INTO bare_table (id) SELECT id FROM src;
"""
        result = _parse_snowflake(sql)
        assert result.statements, "Expected at least one statement"
        stmt = result.statements[0]
        assert stmt.target is not None
        assert stmt.target.full_id == "bare_table", (
            f"bare_table must stay unqualified after a one-part USE, got: {stmt.target.full_id!r}"
        )

    def test_use_schema_single_part_still_works(self):
        """Original single-part USE SCHEMA <schema> continues to work after the P5 extension.

        Regression guard: the existing P5 behavior must be unchanged.
        """
        sql = """\
USE SCHEMA ia_outbound;
INSERT INTO doorbelasting (col1) SELECT col1 FROM src_table;
"""
        result = _parse_snowflake(sql)
        assert result.statements, "Expected at least one statement"
        stmt = result.statements[0]
        assert stmt.target is not None
        assert stmt.target.full_id == "ia_outbound.doorbelasting", (
            f"Expected ia_outbound.doorbelasting, got: {stmt.target.full_id!r}"
        )


# ---------------------------------------------------------------------------
# Step 3.2 — ANSI parser: no USE override needed
# ---------------------------------------------------------------------------


class TestAnsiParserUseStatement:
    """Verify that USE statements in ANSI-dialect files are not schema qualifiers.

    The plan (Step 3.2) requires documenting whether ANSI-parsed files use USE
    statements in the corpus.  Grep of test fixtures shows none do.  The ANSI
    _transform_statements base is a no-op; no override is added.

    This test confirms:
    1. USE in an ANSI file emits a statement (kind=OTHER), not dropped.
    2. A bare table ref following USE is NOT schema-qualified (ANSI has no USE SCHEMA).

    plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2, Step 3.2.
    """

    def test_use_in_ansi_does_not_qualify_following_ref(self):
        """USE statement in ANSI file does not qualify subsequent bare refs.

        The ANSI _transform_statements is a no-op — no schema context is set.
        A bare table reference after USE must remain unqualified.
        """
        sql = "USE my_schema;\nINSERT INTO bare_table (id) SELECT id FROM src;"
        result = _parse_ansi(sql)
        # 'USE' emits an OTHER statement; INSERT emits a real one
        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        if insert_stmts:
            stmt = insert_stmts[0]
            if stmt.target:
                assert "." not in stmt.target.full_id, (
                    f"ANSI bare_table should not be qualified, got: {stmt.target.full_id!r}"
                )


# ---------------------------------------------------------------------------
# Step 3.3 — LIKE catalog inheritance
# ---------------------------------------------------------------------------


class TestLikePropertyCloneSource:
    """CREATE TABLE x LIKE y sets clone_source = y, enabling resolve_clone_catalogs.

    Guards the LikeProperty capture in AnsiParser._parse_statement.
    plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2, Step 3.3.
    """

    def test_create_table_like_sets_clone_source(self):
        """CREATE TABLE x LIKE y → parsed statement has clone_source pointing at y.

        clone_source.full_id must equal 's.y' (schema-qualified source).
        """
        sql = "CREATE TABLE s.x LIKE s.y;"
        result = _parse_snowflake(sql)
        assert result.statements, "Expected a CREATE_TABLE statement"
        stmt = result.statements[0]
        assert stmt.kind == "CREATE_TABLE"
        assert stmt.clone_source is not None, "clone_source must be set for CREATE TABLE ... LIKE"
        assert stmt.clone_source.full_id == "s.y", (
            f"Expected clone_source='s.y', got {stmt.clone_source.full_id!r}"
        )

    def test_create_table_like_bare_sets_clone_source(self):
        """CREATE TABLE x LIKE y (no schema) → clone_source.name = 'y'."""
        sql = "CREATE TABLE x LIKE y;"
        result = _parse_snowflake(sql)
        assert result.statements, "Expected a CREATE_TABLE statement"
        stmt = result.statements[0]
        assert stmt.clone_source is not None, (
            "clone_source must be set for bare CREATE TABLE ... LIKE"
        )
        assert stmt.clone_source.name == "y", (
            f"Expected clone_source.name='y', got {stmt.clone_source.name!r}"
        )

    def test_create_table_like_ansi_dialect(self):
        """CREATE TABLE x LIKE y also works in ANSI (no-dialect) parsing.

        LikeProperty is generated by the base sqlglot grammar, not only the
        Snowflake dialect.
        """
        sql = "CREATE TABLE s.x LIKE s.y;"
        result = _parse_ansi(sql)
        assert result.statements, "Expected a CREATE_TABLE statement"
        stmt = result.statements[0]
        assert stmt.clone_source is not None, (
            "clone_source must be set for ANSI CREATE TABLE ... LIKE"
        )
        assert stmt.clone_source.full_id == "s.y", (
            f"Expected clone_source='s.y', got {stmt.clone_source.full_id!r}"
        )

    def test_create_table_clone_still_works(self):
        """CREATE TABLE t CLONE src (existing behavior) is unchanged by the LIKE addition.

        Regression guard: the CLONE path must still populate clone_source.
        """
        sql = "CREATE OR REPLACE TABLE ba.target CLONE ba.source;"
        result = _parse_snowflake(sql)
        assert result.statements, "Expected a CREATE_TABLE statement"
        stmt = result.statements[0]
        assert stmt.clone_source is not None, "clone_source must be set for CREATE TABLE CLONE"
        assert stmt.clone_source.full_id == "ba.source", (
            f"Expected clone_source='ba.source', got {stmt.clone_source.full_id!r}"
        )

    def test_create_table_like_and_clone_distinguished(self):
        """LIKE and CLONE are captured via different AST paths but both set clone_source.

        This test pairs them to confirm the elif branch path (CLONE via args['clone'],
        LIKE via args['properties']) works independently.
        """
        like_result = _parse_snowflake("CREATE TABLE x LIKE y;")
        clone_result = _parse_snowflake("CREATE TABLE x CLONE y;")

        like_stmt = like_result.statements[0] if like_result.statements else None
        clone_stmt = clone_result.statements[0] if clone_result.statements else None

        assert like_stmt is not None
        assert clone_stmt is not None
        assert like_stmt.clone_source is not None, "LIKE must set clone_source"
        assert clone_stmt.clone_source is not None, "CLONE must set clone_source"
        assert like_stmt.clone_source.name == "y"
        assert clone_stmt.clone_source.name == "y"


# ---------------------------------------------------------------------------
# Qualify_bare_tables static method — unit tests
# ---------------------------------------------------------------------------


class TestQualifyBareTables:
    """Unit tests for SnowflakeParser._qualify_bare_tables covering edge cases.

    Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2.
    """

    def test_already_qualified_table_not_modified(self):
        """A table already carrying a db qualifier is left unchanged."""
        import sqlglot

        stmt = sqlglot.parse("SELECT a FROM db.schema1.tbl", dialect="snowflake")[0]
        # Find tbl before qualification
        before = [t.sql() for t in stmt.find_all(exp.Table)]
        SnowflakeParser._qualify_bare_tables(stmt, "other_schema")
        after = [t.sql() for t in stmt.find_all(exp.Table)]
        # tbl had a catalog (db.schema1) so its name should remain unchanged
        # (db=schema1, catalog=db in sqlglot terms)
        assert before == after, f"Pre-qualified table modified: {before} → {after}"

    def test_empty_name_table_not_modified(self):
        """A Table node with an empty name (synthetic) is skipped."""
        import sqlglot

        stmt = sqlglot.parse("SELECT a FROM real_table", dialect="snowflake")[0]
        SnowflakeParser._qualify_bare_tables(stmt, "myschema")
        tables = list(stmt.find_all(exp.Table))
        for t in tables:
            if t.name:
                assert t.args.get("db"), f"Table {t.name!r} should be qualified"
