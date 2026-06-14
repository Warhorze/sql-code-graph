"""Unit tests for ``SqlParser._classify`` on non-table ``CREATE`` DDL.

Guards the create-kind mislabel bugfix
([plan doc](plan/sprints/bugfix_create_kind_mislabel.md)).

Before the fix the ``exp.Create`` catch-all returned ``CREATE_TABLE`` for every
``Create.kind`` it did not explicitly handle, so ``CREATE SEQUENCE`` /
``CREATE STAGE`` / ``CREATE FILE FORMAT`` (and ``SCHEMA`` / ``INDEX``) were all
stored as ``CREATE_TABLE``. The catch-all now returns ``OTHER``.

Safety property under test (AC2): every statement that is *genuinely* a table —
plain ``CREATE TABLE``, ``TRANSIENT TABLE``, ``OR REPLACE TABLE`` and CTAS — has
``Create.kind == "TABLE"`` and so hits the explicit arm; the flip to ``OTHER``
cannot reclassify a real table.

Each assertion checks the observable returned kind string, not "no exception".
"""

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


def _classify(sql: str) -> str:
    """Parse ``sql`` (snowflake dialect) and return the stored query kind."""
    parser = AnsiParser(SchemaResolver(dialect="snowflake"))
    node = sqlglot.parse_one(sql, dialect="snowflake")
    return parser._classify(node)


class TestNonTableCreateClassifiesAsOther:
    """Non-table CREATE DDL must classify as OTHER, not CREATE_TABLE."""

    def test_create_sequence_classifies_as_other(self):
        """CREATE SEQUENCE (Create.kind == 'SEQUENCE') is OTHER."""
        assert _classify("CREATE SEQUENCE ba.s START 1") == "OTHER"

    def test_create_stage_classifies_as_other(self):
        """CREATE STAGE (Create.kind == 'STAGE') is OTHER."""
        assert _classify("CREATE STAGE ma.stg") == "OTHER"

    def test_create_file_format_classifies_as_other(self):
        """CREATE FILE FORMAT (Create.kind == 'FILE FORMAT') is OTHER."""
        assert _classify("CREATE FILE FORMAT ff TYPE=CSV") == "OTHER"

    def test_create_schema_classifies_as_other(self):
        """CREATE SCHEMA (Create.kind == 'SCHEMA') is OTHER."""
        assert _classify("CREATE SCHEMA s") == "OTHER"

    def test_create_index_classifies_as_other(self):
        """CREATE INDEX (Create.kind == 'INDEX') is OTHER."""
        assert _classify("CREATE INDEX i ON t(a)") == "OTHER"


class TestRealTableCreateStillClassifiesAsCreateTable:
    """Control: every genuine table CREATE keeps CREATE_TABLE (safety property)."""

    def test_plain_create_table_classifies_as_create_table(self):
        """Plain CREATE TABLE keeps CREATE_TABLE."""
        assert _classify("CREATE TABLE t (a INT)") == "CREATE_TABLE"

    def test_transient_table_classifies_as_create_table(self):
        """CREATE TRANSIENT TABLE has Create.kind == 'TABLE' → CREATE_TABLE."""
        assert _classify("CREATE TRANSIENT TABLE t (a INT)") == "CREATE_TABLE"

    def test_or_replace_table_classifies_as_create_table(self):
        """CREATE OR REPLACE TABLE has Create.kind == 'TABLE' → CREATE_TABLE."""
        assert _classify("CREATE OR REPLACE TABLE t (a INT)") == "CREATE_TABLE"

    def test_ctas_classifies_as_create_table(self):
        """CTAS (CREATE TABLE AS SELECT) has Create.kind == 'TABLE' → CREATE_TABLE."""
        assert _classify("CREATE TABLE t AS SELECT a FROM s") == "CREATE_TABLE"


class TestViewAndProcUnaffected:
    """VIEW / PROCEDURE explicit arms are unchanged by the catch-all flip."""

    def test_create_view_classifies_as_create_view(self):
        """CREATE VIEW keeps CREATE_VIEW."""
        assert _classify("CREATE VIEW v AS SELECT a FROM s") == "CREATE_VIEW"

    def test_create_procedure_classifies_as_create_proc(self):
        """CREATE PROCEDURE keeps CREATE_PROC."""
        assert (
            _classify("CREATE PROCEDURE p() RETURNS INT AS $$ BEGIN RETURN 1; END $$")
            == "CREATE_PROC"
        )
