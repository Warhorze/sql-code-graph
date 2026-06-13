"""Unit tests for CREATE VIEW classification (SqlTable.kind='view').

The parser has always known a statement is a view (QueryKind.CREATE_VIEW on the
SqlQuery node) but never propagated that to the SqlTable node — so views landed
in the graph indistinguishable from tables (kind='table'). These tests pin the
fix: a CREATE VIEW target is stamped role='view', which the indexer emits as
SqlTable.kind='view'.

Downstream already expects 'view' as a valid kind:
  - indexer._USAGE_ELIGIBLE_KINDS = {'table', 'view'}
  - lineage/schema_resolver.py ('table', 'view') resource-type check
  - duckdb_backend kind-upgrade guard: kind IN ('table','view') is never
    downgraded to 'derived'
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _make_ansi_parser() -> AnsiParser:
    return AnsiParser(SchemaResolver(dialect=None))


def _make_snowflake_parser() -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"))


class TestCreateViewRole:
    """A CREATE VIEW target is stamped role='view' in defined_tables."""

    def test_create_view_target_role_is_view(self, tmp_path):
        """CREATE VIEW v AS SELECT ... → defined target has role='view'."""
        sql = "CREATE VIEW ba.v AS SELECT a FROM ba.src"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "v.sql", sql, rel_path="v.sql")

        assert result.defined_tables, "view target missing from defined_tables"
        roles = {t.name: t.role for t in result.defined_tables}
        assert roles.get("v") == "view", f"expected role='view', got {roles}"

    def test_create_or_replace_view_role_is_view(self, tmp_path):
        """CREATE OR REPLACE VIEW is also stamped role='view'."""
        sql = "CREATE OR REPLACE VIEW ba.v AS SELECT a FROM ba.src"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "v.sql", sql, rel_path="v.sql")

        roles = {t.name: t.role for t in result.defined_tables}
        assert roles.get("v") == "view", f"expected role='view', got {roles}"

    def test_create_table_target_stays_role_table(self, tmp_path):
        """Regression guard: a real CREATE TABLE is NOT misclassified as a view."""
        sql = "CREATE TABLE ba.t AS SELECT a FROM ba.src"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "t.sql", sql, rel_path="t.sql")

        roles = {t.name: t.role for t in result.defined_tables}
        assert roles.get("t") == "table", f"expected role='table', got {roles}"

    def test_temporary_view_keeps_role_temp(self, tmp_path):
        """A TEMPORARY VIEW keeps role='temp' — the temp block wins over view.

        The view stamping is guarded on role == 'table', so the temp namespacing
        (which runs first) is not overwritten.
        """
        sql = "CREATE TEMPORARY VIEW tmp_v AS SELECT a FROM ba.src"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "tv.sql", sql, rel_path="tv.sql")

        roles = {t.name: t.role for t in result.defined_tables}
        assert roles.get("tmp_v") == "temp", f"expected role='temp', got {roles}"

    def test_snowflake_create_view_role_is_view(self, tmp_path):
        """SnowflakeParser delegates to AnsiParser._parse_statement → same behaviour."""
        sql = "CREATE VIEW ba.v AS SELECT a FROM ba.src"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / "v.sql", sql, rel_path="v.sql")

        roles = {t.name: t.role for t in result.defined_tables}
        assert roles.get("v") == "view", f"expected role='view', got {roles}"
