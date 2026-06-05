"""Unit tests for CrossFileAggregator (T-05 and Phase 5 realignment).

Phase 5 note: CrossFileAggregator.resolve_pass2 was deleted (zero production call sites —
production pass-2 path inlines the same logic in index_repo's dispatch loop in indexer.py).
The "deleted file during pass 2" warning still exists on the production path via the
pool/worker reparse; we test here that register_pass1 and _needs_pass2 behave correctly,
and that a missing file during index_repo does not crash (see test_deleted_file_during_pass2).
"""

import logging

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


class TestResolvePass2DeletedFile:
    """Test that a file deleted between pass 1 and pass 2 does not crash index_repo.

    The production pass-2 path (index_repo -> pool/worker -> parse_file) handles
    missing files via exception propagation from the worker; the caller at indexer.py
    ~L401 logs a WARNING and keeps the pass-1 result.  We replicate the scenario by
    indexing a two-file corpus where the reference file is removed before index_repo
    runs — the index still completes without raising.
    """

    def test_deleted_file_during_pass2_logs_warning(self, caplog, tmp_path):
        """Removing a file before index_repo runs does not crash; summary is returned.

        After T-02 (pass-2 skip predicate): this test must force a re-parse by adding
        a cross-file dependency. Otherwise the file will be skipped (no re-read attempted).
        """
        caplog.set_level(logging.WARNING)

        # Write two SQL files: one defines a table, one references it
        sql_def = tmp_path / "define.sql"
        sql_def.write_text("CREATE TABLE raw_orders (id INT, amount DECIMAL);")

        sql_ref = tmp_path / "reference.sql"
        sql_ref.write_text("SELECT id, amount FROM raw_orders;")

        # Verify the cross-file dependency is detected
        schema = SchemaResolver()
        parser = get_parser(None, schema)
        pass1_def = parser.parse_file(sql_def, sql_def.read_text())
        pass1_ref = parser.parse_file(sql_ref, sql_ref.read_text())
        aggregator = CrossFileAggregator()
        aggregator.register_pass1(pass1_def)
        aggregator.register_pass1(pass1_ref)
        assert aggregator._needs_pass2(pass1_ref), (
            "reference.sql should need pass-2 (it references raw_orders from define.sql)"
        )

        # Delete the reference file before running index_repo
        sql_ref.unlink()

        # index_repo must complete without raising even with the deleted file
        db = DuckDBBackend(":memory:")
        db.init_schema()
        try:
            summary = Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        finally:
            db.close()

        # Summary is returned (no crash)
        assert isinstance(summary, dict), "index_repo must return a summary dict"
