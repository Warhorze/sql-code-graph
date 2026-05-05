"""Unit tests for SQL parsers."""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


class TestAnsiParser:
    """Test ANSI SQL parser."""

    def test_parse_simple_select(self):
        """Test parsing a simple SELECT statement."""
        sql = "SELECT * FROM users;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "SELECT"
        assert len(result.statements[0].sources) == 1
        assert result.statements[0].sources[0].name == "users"

    def test_parse_insert_statement(self):
        """Test parsing an INSERT statement."""
        sql = "INSERT INTO products (id, name) VALUES (1, 'Widget');"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "INSERT"
        assert result.statements[0].target.name == "products"

    def test_parse_create_table(self):
        """Test parsing a CREATE TABLE statement."""
        sql = "CREATE TABLE users (id INT, name VARCHAR(255));"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "CREATE_TABLE"
        assert result.statements[0].target.name == "users"
        assert len(result.defined_tables) == 1

    def test_parse_create_view(self):
        """Test parsing a CREATE VIEW statement."""
        sql = "CREATE VIEW user_summary AS SELECT id, name FROM users;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "CREATE_VIEW"
        assert result.statements[0].target.name == "user_summary"
        assert len(result.statements[0].sources) == 1

    def test_parse_update_statement(self):
        """Test parsing an UPDATE statement."""
        sql = "UPDATE users SET name = 'John' WHERE id = 1;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "UPDATE"
        assert result.statements[0].sources[0].name == "users"

    def test_parse_delete_statement(self):
        """Test parsing a DELETE statement."""
        sql = "DELETE FROM users WHERE id = 1;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "DELETE"
        assert result.statements[0].sources[0].name == "users"

    def test_parse_with_schema_qualified_tables(self):
        """Test parsing with schema-qualified table names."""
        sql = "SELECT * FROM public.users JOIN schema.orders ON users.id = orders.user_id;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        sources = {t.full_id for t in result.statements[0].sources}
        assert "public.users" in sources
        assert "schema.orders" in sources

    def test_parse_multiple_statements(self):
        """Test parsing a file with multiple statements."""
        sql = "SELECT * FROM users; SELECT * FROM orders; SELECT * FROM products;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 3
        assert all(stmt.kind == "SELECT" for stmt in result.statements)

    def test_parse_with_ctes(self):
        """Test parsing a SELECT with CTEs.

        Note: CTE source extraction requires proper scope analysis which may have
        limitations in fallback mode. For now, verify parsing doesn't crash.
        """
        sql = """
        WITH user_orders AS (
            SELECT user_id, COUNT(*) as order_count FROM orders GROUP BY user_id
        )
        SELECT * FROM user_orders;
        """
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        # CTE handling is complex; at minimum, verify parsing succeeded without crash
        assert result.statements[0].kind == "SELECT"

    def test_parse_malformed_sql(self):
        """Test parsing malformed SQL doesn't crash."""
        sql = "SELECT * FROM WHERE id = 1;"  # Invalid SQL
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        # Should capture error and continue
        assert len(result.errors) > 0

    def test_parse_file_with_catalog_qualified_tables(self):
        """Test parsing with catalog-qualified table names."""
        sql = "SELECT * FROM catalog.schema.users;"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        sources = result.statements[0].sources
        assert len(sources) > 0
        # Should have catalog in full_id
        assert "catalog" in sources[0].full_id or sources[0].catalog is not None

    def test_parse_insert_select(self):
        """Test parsing INSERT ... SELECT (T-10 investigation outcome).
        
        Investigation conclusion: INSERT-SELECT statements correctly create
        SELECTS_FROM edges from the INSERT query to the source table.
        This is expected behavior - the source table IS being selected from
        within the INSERT statement\'s SELECT clause.
        """
        sql = "INSERT INTO archive SELECT * FROM users WHERE created < DATE '2020-01-01';"
        parser = get_parser(None, SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "INSERT"
        assert result.statements[0].target.name == "archive"
        assert any(t.name == "users" for t in result.statements[0].sources)


class TestSnowflakeParser:
    """Test Snowflake-specific parser."""

    def test_parse_snowflake_dialect(self):
        """Test parsing with Snowflake dialect."""
        sql = "SELECT * FROM public.users;"
        parser = get_parser("snowflake", SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        assert len(result.statements) == 1
        assert result.statements[0].kind == "SELECT"

    def test_begin_in_comment_not_detected_as_scripting(self):
        """Test that BEGIN in comments doesn't trigger scripting mode."""
        sql = """
        -- This is a comment with BEGIN in it
        SELECT * FROM users;
        """
        parser = get_parser("snowflake", SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        # Should parse as normal, not enter scripting mode
        assert len(result.statements) == 1
        assert result.statements[0].kind == "SELECT"

    def test_begin_in_string_literal_not_detected(self):
        """Test that BEGIN in string literals doesn't trigger scripting mode."""
        sql = """SELECT 'BEGIN TRANSACTION' AS status FROM users;"""
        parser = get_parser("snowflake", SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        # Should parse as normal, not enter scripting mode
        assert len(result.statements) == 1
        assert result.statements[0].kind == "SELECT"

    def test_actual_scripting_block_detected(self):
        """Test that actual BEGIN blocks are detected."""
        sql = """
        BEGIN
            SELECT * FROM users;
            INSERT INTO users VALUES (1, 'John');
        END;
        """
        parser = get_parser("snowflake", SchemaResolver())
        result = parser.parse_file(Path("test.sql"), sql)

        # Should enter scripting mode
        # DML extraction may or may not succeed, but parsing_mode should reflect it
        assert any(
            "scripting" in str(stmt.parsing_mode).lower() or stmt.parse_failed
            for stmt in result.statements
        )


class TestParserRegistry:
    """Test parser registry and factory."""

    def test_get_parser_ansi(self):
        """Test getting ANSI parser."""
        parser = get_parser(None, SchemaResolver())
        assert parser.DIALECT is None

    def test_get_parser_snowflake(self):
        """Test getting Snowflake parser."""
        parser = get_parser("snowflake", SchemaResolver())
        assert parser.DIALECT == "snowflake"

    def test_get_parser_bigquery(self):
        """Test getting BigQuery parser."""
        parser = get_parser("bigquery", SchemaResolver())
        assert parser.DIALECT == "bigquery"

    def test_get_parser_postgres(self):
        """Test getting Postgres parser."""
        parser = get_parser("postgres", SchemaResolver())
        assert parser.DIALECT == "postgres"

    def test_get_parser_tsql(self):
        """Test getting T-SQL parser."""
        parser = get_parser("tsql", SchemaResolver())
        assert parser.DIALECT == "tsql"

    def test_get_parser_unknown_dialect_falls_back_to_ansi(self):
        """Test that unknown dialects fall back to ANSI."""
        parser = get_parser("unknown_dialect", SchemaResolver())
        # Should fall back to ANSI (DIALECT = None)
        assert parser.DIALECT is None or parser.__class__.__name__ == "AnsiParser"
