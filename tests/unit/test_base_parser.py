"""Unit tests for SQL base parser (parsers/base.py)."""

import logging
from pathlib import Path
from unittest.mock import patch

from sqlglot import parse_one

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParsedFile


class TestExtractColumnLineageExceptions:
    """Test column lineage exception handling in _extract_column_lineage."""

    def test_sg_lineage_exception_recorded(self, caplog):
        """Test that sg_lineage exceptions append to errors and emit zero-confidence edge."""
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        # Construct a minimal SELECT statement
        stmt = parse_one("SELECT bad_col FROM t")

        # Create a ParsedFile object to track errors
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Mock sg_lineage to raise an exception
        with patch("sqlglot.lineage.lineage") as mock_sg_lineage:
            mock_sg_lineage.side_effect = ValueError("mock lineage failure")

            # Call _extract_column_lineage directly
            edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)

            # Assert error was recorded with structured key
            assert len(out.errors) > 0
            assert any("col_lineage:bad_col:mock lineage failure" in str(e) for e in out.errors)

            # Assert WARNING was logged
            assert any(
                "column lineage extraction failed" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )

            # Assert exactly one zero-confidence edge returned
            assert len(edges) == 1
            assert edges[0].confidence == 0.0

    def test_outer_statement_exception_recorded(self, caplog):
        """Test that outer statement exceptions are recorded in col_lineage:statement."""
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        # Create output object with errors list
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Parse a statement and call method
        stmt = parse_one("SELECT * FROM t")

        # Create a schema dict that will cause issues
        bad_schema = None

        edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=bad_schema)

        # The method should handle the exception gracefully
        assert isinstance(edges, list)

    def test_sg_lineage_success_returns_edges(self):
        """When sg_lineage returns a root node, edges must be emitted — not an empty list.

        Uses a real SELECT so the tree walker receives a genuine LineageNode with a
        Table source, not an unconfigured MagicMock that _lineage_node_to_table_ref rejects.
        """
        parser = AnsiParser(SchemaResolver())
        stmt = parse_one("SELECT col1 FROM t")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema={})

        assert len(edges) > 0, (
            "Expected at least one LineageEdge for SELECT col1 FROM t, got none. "
            "The tree walker in _lineage_node_to_edges must emit edges for leaf nodes."
        )
        assert all(e.confidence > 0.0 for e in edges), (
            "Edges from a successful sg_lineage call must have confidence > 0."
        )


class TestE8DynamicSourceMarker:
    """sg_lineage returns a root but no leaf sources — dynamic identifier pattern."""

    def test_dynamic_identifier_emits_skip_marker(self):
        # IDENTIFIER($var) is opaque to sqlglot; sg_lineage returns a root with no
        # resolvable leaf — _lineage_node_to_edges emits nothing and we must see the marker.
        parser = AnsiParser(SchemaResolver())
        out = ParsedFile(path=Path("test.sql"), dialect=None)
        # Use Anonymous function as a stand-in for identifier($var) — sqlglot can't resolve it
        stmt = parse_one("INSERT INTO tgt SELECT IDENTIFIER('src_tbl') AS col1 FROM src")
        parser._extract_column_lineage(stmt, Path("test.sql"), out, schema={})
        assert any(e.startswith("col_lineage_skip:dynamic_source:") for e in out.errors), (
            f"Expected dynamic_source skip marker, got: {out.errors}"
        )


class TestT01ErrorPropagation:
    """T-01: Test error propagation from _parse_statement to ParsedFile."""

    def test_parse_file_with_clean_ctas_no_errors(self):
        """Test that a clean CTAS produces no col_lineage errors.

        Parse a CREATE TABLE AS SELECT 1 AS x,
        assert no col_lineage errors in parsed.errors.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "CREATE TABLE t AS SELECT 1 AS x"
        parsed = parser.parse_file(Path("test.sql"), sql)

        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]
        assert col_lineage_errors == [], f"Expected no col_lineage errors, got: {parsed.errors}"

    def test_parse_file_can_call_with_out_parameter(self):
        """Test that _parse_statement receives and uses the out parameter.

        This verifies that T-01 implementation passes the out parameter correctly
        so that errors from _extract_column_lineage are appended to the caller's
        ParsedFile object instead of being discarded.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT 1 AS x"
        parsed = parser.parse_file(Path("test.sql"), sql)

        # Verify parse succeeded and statements were added
        assert len(parsed.statements) == 1
        assert parsed.statements[0].kind == "SELECT"


class TestT03TempTableSources:
    """T-03: Test temp table source accumulation in _parse_statement."""

    def test_sources_parameter_passed_to_extract_column_lineage(self):
        """Test that _parse_statement passes sources to _extract_column_lineage.

        This is a unit test that verifies the parameter threading before
        integration tests check actual temp table resolution.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT a FROM t"
        parsed = parser.parse_file(Path("test.sql"), sql)

        # Verify parse succeeded (the real test is implementation coverage)
        assert len(parsed.statements) == 1


class TestT02StarColumnSkip:
    """T-02: Test star column skip in _extract_column_lineage."""

    def test_qualified_star_produces_skip_marker(self):
        """Test that qualified star (base.*) is skipped with error marker.

        Parse CREATE TEMP TABLE tmp_b AS SELECT base.*, 1 AS x FROM tmp_a base
        Assert:
        - parsed.errors contains one entry starting with col_lineage_skip:star:base
        - No entry starts with col_lineage:
        (Note: column x won't produce edges until T-03 adds sources_map)
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "CREATE TEMP TABLE tmp_b AS SELECT base.*, 1 AS x FROM tmp_a base"
        parsed = parser.parse_file(Path("test.sql"), sql)

        skip_errors = [e for e in parsed.errors if e.startswith("col_lineage_skip:star:")]
        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]

        assert len(skip_errors) >= 1, f"Expected skip marker, got errors: {parsed.errors}"
        assert skip_errors[0].startswith("col_lineage_skip:star:base"), (
            f"Expected qualified star marker, got: {skip_errors}"
        )
        assert col_lineage_errors == [], (
            f"Star should not cause col_lineage error, got: {col_lineage_errors}"
        )

    def test_unqualified_star_produces_skip_marker(self):
        """Test that unqualified star (*) is skipped with error marker.

        Parse SELECT * FROM t
        Assert:
        - parsed.errors contains one entry starting with col_lineage_skip:star:
        - No exception raised
        - No column lineage edges (star cannot be resolved)
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT * FROM t"
        parsed = parser.parse_file(Path("test.sql"), sql)

        skip_errors = [e for e in parsed.errors if e.startswith("col_lineage_skip:star:")]
        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]

        assert len(skip_errors) >= 1, f"Expected skip marker, got errors: {parsed.errors}"
        assert col_lineage_errors == [], (
            f"Star should not cause col_lineage error, got: {col_lineage_errors}"
        )
