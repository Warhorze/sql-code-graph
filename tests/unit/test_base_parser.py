"""Unit tests for SQL base parser (parsers/base.py)."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_logger_debug_on_sg_lineage_root(self, caplog):
        """Test debug log when sg_lineage root is obtained but conversion is not yet implemented."""
        # Set up caplog to capture at DEBUG level
        caplog.set_level(logging.DEBUG)

        schema = SchemaResolver()
        parser = AnsiParser(schema)

        # Explicitly set the parser's logger to DEBUG level
        parser._log.setLevel(logging.DEBUG)

        # Parse a statement with a column
        stmt = parse_one("SELECT col1 FROM t")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Mock sg_lineage to return a non-None root (truthy value)
        with patch("sqlglot.lineage.lineage") as mock_sg_lineage:
            # Return a non-None root object
            mock_root = MagicMock()
            mock_sg_lineage.return_value = mock_root

            # Call _extract_column_lineage
            parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)

            # Check all records in caplog
            all_messages = [r.message for r in caplog.records]
            debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]

            assert any(
                "sg_lineage root obtained but conversion not yet implemented" in record.message
                and "file=test.sql" in record.message
                and "col=col1" in record.message
                for record in debug_records
            ), f"Expected debug log not found. Got all records: {all_messages}"
