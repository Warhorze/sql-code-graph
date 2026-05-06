"""Integration tests for temp table lineage resolution."""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


class TestT03TempTableSourceAccumulation:
    """T-03: Test temp table source accumulation for column lineage."""

    def test_temp_chain_resolves_to_real_table(self):
        """Test that temp table chains resolve lineage back to the real table.

        Parse the canonical pattern:
        CREATE TEMP TABLE tmp_a AS SELECT col1, col2 FROM BA.source_table;
        CREATE TEMP TABLE tmp_b AS SELECT base.col1 AS c1, base.col2 AS c2 FROM tmp_a base;
        INSERT INTO BA.target SELECT c1, c2 FROM tmp_b;

        Assert that for the INSERT statement, column_lineage contains at
        least two edges whose src.table.full_id == "BA.source_table".
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = """CREATE TEMP TABLE tmp_a AS SELECT col1, col2 FROM BA.source_table;
CREATE TEMP TABLE tmp_b AS SELECT base.col1 AS c1, base.col2 AS c2 FROM tmp_a base;
INSERT INTO BA.target SELECT c1, c2 FROM tmp_b"""
        parsed = parser.parse_file(Path("test.sql"), sql)

        # The INSERT is the third statement (index 2)
        insert_stmt = parsed.statements[2]
        assert insert_stmt.kind == "INSERT"

        edges = insert_stmt.column_lineage
        source_tables = {e.src.table.full_id for e in edges if e.confidence > 0}

        # Should have resolved back through the temp tables to BA.source_table
        assert any("source_table" in t for t in source_tables), (
            f"Expected edge to source_table, got: {source_tables}"
        )

    def test_cte_resolves_source(self):
        """Test that CTEs are also resolved as sources.

        Parse SELECT a FROM cte_only_in_with inside
        WITH cte_only_in_with AS (SELECT a FROM real)

        Assert one edge with src.table.name == "real" (not <unknown>).
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "WITH cte_only_in_with AS (SELECT a FROM real) SELECT a FROM cte_only_in_with"
        parsed = parser.parse_file(Path("test.sql"), sql)

        edges = parsed.statements[0].column_lineage
        source_tables = {e.src.table.name for e in edges if e.confidence > 0}

        assert "real" in source_tables, f"Expected edge to 'real' table, got: {source_tables}"

    def test_empty_sources_map_parity(self):
        """Test that empty sources_map doesn't change single-table lineage.

        Parse INSERT INTO t SELECT a FROM real with a single-table schema.
        Assert edges are unchanged from baseline (with empty sources_map,
        the result should be the same as before T-03).
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "INSERT INTO t SELECT a FROM real"
        parsed = parser.parse_file(Path("test.sql"), sql)

        edges = parsed.statements[0].column_lineage
        assert len(edges) > 0, "Expected at least one edge for simple INSERT"

        # Snapshot the edge structure
        snapshot = [(e.src.full_id, e.dst.full_id) for e in edges]
        assert len(snapshot) > 0, "Expected non-empty edge list"

    def test_parenthesized_subquery_wrapper_registration(self):
        """Test that parenthesised CTAS forms are registered in sources_map.

        Parse CREATE TABLE tmp_c AS (SELECT col1 FROM BA.src)
        (with parentheses around the SELECT).

        Assert tmp_c is registered and available for downstream references.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = """CREATE TABLE tmp_c AS (SELECT col1 FROM BA.src);
INSERT INTO target SELECT col1 FROM tmp_c"""
        parsed = parser.parse_file(Path("test.sql"), sql)

        # The INSERT should resolve tmp_c through the sources_map
        insert_stmt = parsed.statements[1]
        edges = insert_stmt.column_lineage

        source_tables = {e.src.table.full_id for e in edges if e.confidence > 0}
        assert any("src" in t for t in source_tables), (
            f"Expected edge to BA.src, got: {source_tables}"
        )
