"""Unit tests for ParseQuality assignment (T-09)."""

from pathlib import Path
from unittest.mock import patch

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParseQuality


def test_parse_quality_full_when_column_lineage_extracted():
    """Test that parse_quality is set to FULL when column lineage is extracted.
    
    When a QueryNode with non-empty column_lineage is added to ParsedFile,
    the parse_quality should be upgraded to FULL.
    """
    schema = SchemaResolver()
    parser = AnsiParser(schema)

    # Create a simple SELECT query
    sql = "SELECT col1 FROM table1"
    
    # Mock the _parse_statement to return a QueryNode with column lineage
    with patch.object(parser, '_parse_statement') as mock_parse:
        from sqlcg.parsers.base import QueryNode, LineageEdge, ColumnRef, TableRef
        
        # Create a QueryNode with column lineage
        query_node = QueryNode(
            file=Path("test.sql"),
            statement_index=0,
            sql=sql,
            kind="SELECT",
            target=None,
            sources=[TableRef(None, None, "table1")],
            ctes={},
            column_lineage=[
                LineageEdge(
                    src=ColumnRef(TableRef(None, None, "table1"), "col1"),
                    dst=ColumnRef(TableRef(None, None, "<output>"), "col1"),
                    transform="PASS_THROUGH",
                    confidence=1.0,
                )
            ],
            parse_failed=False,
            confidence=1.0,
            parsing_mode="sqlglot",
        )
        mock_parse.return_value = query_node
        
        # Parse the file
        parsed = parser.parse_file(Path("test.sql"), sql)
        
        # Verify parse_quality is FULL
        assert parsed.parse_quality == ParseQuality.FULL, \
            f"Expected FULL, got {parsed.parse_quality}"


def test_parse_quality_table_only_without_column_lineage():
    """Test that parse_quality remains TABLE_ONLY when no column lineage is extracted."""
    schema = SchemaResolver()
    parser = AnsiParser(schema)

    # Parse a simple query without column lineage extraction
    sql = "SELECT 1"
    parsed = parser.parse_file(Path("test.sql"), sql)
    
    # Verify parse_quality is TABLE_ONLY (default)
    assert parsed.parse_quality == ParseQuality.TABLE_ONLY, \
        f"Expected TABLE_ONLY, got {parsed.parse_quality}"
