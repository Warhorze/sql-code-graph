"""Unit tests for lineage aggregator (T-05)."""

import logging

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


class TestResolvePass2DeletedFile:
    """Test resolve_pass2 behavior when file is deleted during pass 2."""

    def test_deleted_file_during_pass2_logs_warning(self, caplog, tmp_path):
        """Test that deleting a file during pass2 logs WARNING and returns original result."""
        caplog.set_level(logging.WARNING)

        # Write a SQL file
        sql_file = tmp_path / "source.sql"
        sql_file.write_text("CREATE TABLE raw_orders (id INT, amount DECIMAL);")

        # Parse with pass 1
        schema = SchemaResolver()
        parser = get_parser(None, schema)
        pass1_result = parser.parse_file(sql_file, sql_file.read_text())

        # Register with aggregator
        aggregator = CrossFileAggregator()
        aggregator.register_pass1(pass1_result)

        # Delete the file before pass 2
        sql_file.unlink()

        # Call resolve_pass2
        result = aggregator.resolve_pass2(parser, pass1_result)

        # Assert result is the original pass1_result (unchanged)
        assert result is pass1_result

        # Assert WARNING was logged with "resolve_pass2" and "cannot re-read"
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "resolve_pass2" in record.message and "cannot re-read" in record.message
            for record in warning_records
        ), f"Expected warning not found. Got: {[r.message for r in warning_records]}"
