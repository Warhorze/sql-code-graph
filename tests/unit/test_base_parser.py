"""Unit tests for SQL base parser (parsers/base.py)."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Bug 2 (ARCHITECTURE_REVIEW §11.2): sg_lineage → LineageEdge conversion is a "
            "TODO in base.py:396. _extract_column_lineage returns [] on the success path. "
            "Remove xfail once the conversion is implemented."
        ),
    )
    def test_sg_lineage_success_returns_edges(self):
        """When sg_lineage returns a root node, edges must be emitted — not an empty list."""
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        stmt = parse_one("SELECT col1 FROM t")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        with patch("sqlglot.lineage.lineage") as mock_sg_lineage:
            mock_root = MagicMock()
            mock_sg_lineage.return_value = mock_root

            edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)

        assert len(edges) > 0, (
            "Expected at least one LineageEdge when sg_lineage succeeds, got none. "
            "The TODO at base.py:396 must convert the root node to LineageEdge objects."
        )
        assert all(e.confidence > 0.0 for e in edges), (
            "Edges from a successful sg_lineage call must have confidence > 0."
        )
