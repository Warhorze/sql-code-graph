"""Unit tests for T-03: pure-DDL file early skip (FIX-DDL-SKIP)."""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser


class TestPureDDLFileSkip:
    """Test pure-DDL file detection and early skip."""

    def test_pure_ddl_file_returns_early_with_skip_marker(self):
        """Pure-DDL file must be marked with col_lineage_skip:pure_ddl_file and skip lineage."""
        sql = "ALTER TABLE t1 ADD COLUMN c1 VARCHAR; ALTER TABLE t2 DROP COLUMN c2;"
        parser = SnowflakeParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        # Error marker must be present
        assert "col_lineage_skip:pure_ddl_file" in parsed.errors
        # Statements should be processed (for table definitions) but have no lineage
        assert len(parsed.statements) > 0
        # Column lineage edges should be empty (skipped for DDL-only files)
        for stmt in parsed.statements:
            assert stmt.column_lineage == []

    def test_hybrid_ddl_dml_file_not_skipped(self):
        """Hybrid DDL+DML file must NOT be skipped."""
        sql = "CREATE TABLE t1 (id INT); INSERT INTO t2 SELECT id FROM t1;"
        parser = SnowflakeParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        # Must not skip: statements should be processed
        assert len(parsed.statements) > 0
        # Error marker must NOT be present
        assert not any("pure_ddl_skip" in e for e in parsed.errors)

    def test_ctas_not_treated_as_pure_ddl(self):
        """CTAS (CREATE TABLE AS SELECT) must NOT be treated as pure-DDL."""
        sql = "CREATE OR REPLACE VIEW v AS SELECT id FROM t1;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        # CTAS with SELECT body should not be skipped
        assert len(parsed.statements) > 0
        assert not any("pure_ddl_skip" in e for e in parsed.errors)

    def test_pure_create_table_no_body_is_ddl(self):
        """CREATE TABLE with column definitions but no SELECT is pure-DDL."""
        sql = "CREATE TABLE t1 (id INT, name VARCHAR);"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        # Pure CREATE TABLE without body should be marked as pure-DDL
        assert "col_lineage_skip:pure_ddl_file" in parsed.errors
        # Statements should be processed (for table definitions)
        assert len(parsed.statements) > 0
        # But no column lineage should be extracted
        for stmt in parsed.statements:
            assert stmt.column_lineage == []

    def test_ansi_ddl_commands_are_skipped(self):
        """Standard ANSI DDL commands should be marked as pure-DDL."""
        sql = "DROP TABLE IF EXISTS old_table; CREATE INDEX idx ON t1(id);"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        # DDL-only file should be marked as pure-DDL
        assert "col_lineage_skip:pure_ddl_file" in parsed.errors
        # Statements should be processed
        assert len(parsed.statements) > 0
        # But no column lineage
        for stmt in parsed.statements:
            assert stmt.column_lineage == []
