"""Unit tests for lineage aggregator (T-05)."""

import logging

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


class TestResolvePass2DeletedFile:
    """Test resolve_pass2 behavior when file is deleted during pass 2."""

    def test_deleted_file_during_pass2_logs_warning(self, caplog, tmp_path):
        """Test that deleting a file during pass2 logs WARNING and returns original result.

        After T-02 (pass-2 skip predicate): this test must force a re-parse by adding
        a cross-file dependency. Otherwise the file will be skipped (no re-read attempted),
        and the warning will never be logged.
        """
        caplog.set_level(logging.WARNING)

        # Write two SQL files: one defines a table, one references it
        sql_def = tmp_path / "define.sql"
        sql_def.write_text("CREATE TABLE raw_orders (id INT, amount DECIMAL);")

        sql_ref = tmp_path / "reference.sql"
        sql_ref.write_text("SELECT * FROM raw_orders;")

        # Parse both with pass 1
        schema = SchemaResolver()
        parser = get_parser(None, schema)
        pass1_def = parser.parse_file(sql_def, sql_def.read_text())
        pass1_ref = parser.parse_file(sql_ref, sql_ref.read_text())

        # Register both with aggregator — this makes pass1_ref need pass2 because
        # it references raw_orders which is defined in pass1_def
        aggregator = CrossFileAggregator()
        aggregator.register_pass1(pass1_def)
        aggregator.register_pass1(pass1_ref)

        # Delete the reference file before pass 2
        sql_ref.unlink()

        # Call resolve_pass2 on the reference file — this will try to re-read it
        result = aggregator.resolve_pass2(parser, pass1_ref)

        # Assert result is the original pass1_ref (unchanged, returned on file-not-found)
        assert result is pass1_ref

        # Assert WARNING was logged with "resolve_pass2" and "cannot re-read"
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "resolve_pass2" in record.message and "cannot re-read" in record.message
            for record in warning_records
        ), f"Expected warning not found. Got: {[r.message for r in warning_records]}"
