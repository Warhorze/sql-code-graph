"""Integration tests for qualify_failed per-statement persistence (PR-C, #118).

Tests verify that the qualify_failed flag is correctly set in the graph after indexing:
- Scenario A: a statement that triggers qualify() failure gets qualify_failed=TRUE in SqlQuery.
- Scenario B: a clean-parsing file has zero rows with qualify_failed=TRUE.
- Migration: an old-schema (v8) DB raises the refuse-to-start message when a v9 build opens it.

The tests use in-process parsing (via _upsert_parsed_file) rather than the full
index_repo subprocess pool, so the qualify() patch reaches the parser correctly.

Plan: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


def _parse_and_upsert(sql: str, db: DuckDBBackend) -> None:
    """Parse SQL text in-process and upsert into the DB via _upsert_parsed_file.

    This bypasses the subprocess pool so patches applied to sqlcg.parsers.base.*
    in the test process are visible to the parser.
    """
    resolver = SchemaResolver(dialect=None)
    parser = AnsiParser(resolver)
    parsed = parser.parse_file(Path("fixture.sql"), sql)

    indexer = Indexer()
    with db.transaction():
        indexer._upsert_parsed_file(parsed, db)


@pytest.fixture
def db():
    """In-memory DuckDB backend, schema initialized, closed after test."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def test_qualify_failed_flagged_on_failing_statement(db: DuckDBBackend):
    """Scenario A: qualify_failed=TRUE is persisted in SqlQuery for a failing statement.

    Patches qualify() to raise on the first schema-qualify call (statement index 0),
    then parses two statements in-process and upserts the results.  Asserts that:
    - Statement 0 has qualify_failed=TRUE in SqlQuery.
    - At least one qualify_failed=TRUE row exists.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C acceptance criterion.
    """
    sql = "SELECT a FROM src1;\nSELECT b FROM src2;\n"

    call_count = [0]

    def patched_qualify_once(*args, **kwargs):
        from sqlglot.optimizer.qualify import qualify as real_qualify

        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("synthetic qualify failure for test")
        return real_qualify(*args, **kwargs)

    with patch("sqlcg.parsers.base.qualify", patched_qualify_once):
        _parse_and_upsert(sql, db)

    rows = db.run_read(
        'SELECT id, statement_index, qualify_failed FROM "SqlQuery" ORDER BY statement_index',
        {},
    )
    assert len(rows) >= 1, f"Expected at least 1 SqlQuery row, got: {rows}"

    failed_rows = [r for r in rows if r["qualify_failed"]]
    assert len(failed_rows) >= 1, (
        f"Expected at least one qualify_failed=TRUE row after injected qualify failure. "
        f"Got rows: {rows}"
    )

    stmt0 = next((r for r in rows if r["statement_index"] == 0), None)
    assert stmt0 is not None, "Statement 0 not found in SqlQuery"
    assert stmt0["qualify_failed"] is True, (
        f"Statement 0 should have qualify_failed=TRUE (qualify raised on call 1), got: {stmt0}"
    )


def test_qualify_failed_zero_on_clean_file(db: DuckDBBackend):
    """Scenario B: qualify_failed=TRUE count is 0 for a clean-parsing fixture.

    Parses a simple single-statement SQL file that should qualify successfully,
    upserts it, and asserts no SqlQuery rows have qualify_failed=TRUE.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C acceptance criterion.
    """
    sql = "SELECT id, name FROM customers WHERE active = TRUE;\n"
    _parse_and_upsert(sql, db)

    rows = db.run_read(
        'SELECT COUNT(*) AS n FROM "SqlQuery" WHERE qualify_failed = TRUE',
        {},
    )
    count = rows[0]["n"]
    assert count == 0, (
        f"Expected 0 qualify_failed=TRUE rows for a clean-parsing fixture, got: {count}"
    )


def test_qualify_failed_column_exists_in_schema():
    """The qualify_failed BOOLEAN column is present in SqlQuery after init_schema().

    Asserts observable output: the column appears in DuckDB information_schema.columns.
    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C wiring check.
    """
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    try:
        rows = backend.run_read(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'SqlQuery' AND column_name = 'qualify_failed'",
            {},
        )
        assert len(rows) == 1, (
            "qualify_failed column not found in SqlQuery — schema CREATE TABLE not updated"
        )
        assert rows[0]["data_type"].upper() in ("BOOLEAN", "BOOL"), (
            f"Expected BOOLEAN type, got: {rows[0]['data_type']}"
        )
    finally:
        backend.close()


def test_old_schema_db_raises_refuse_to_start_message():
    """Migration: an old-schema (v8) DB raises the refuse-to-start message on v9 build.

    The guard at tools.py:197-219 (_assert_schema_current) checks the stored
    SCHEMA_VERSION and raises RuntimeError with a 'db reset && index' message on
    mismatch.  This test creates a DB with SchemaVersion.version='8' (simulating
    a pre-PR-C database) then calls _assert_schema_current and asserts the error
    message contains the expected remedy instructions.

    Guards: plan/sprints/sprint_backlog_q2_bugfix.md §PR-C migration blocker test.
    """
    from sqlcg.server.tools import _assert_schema_current

    backend = DuckDBBackend(":memory:")
    backend.init_schema()

    # Clear the schema version and insert v8 to simulate an old-schema DB.
    # init_schema() inserts v9 as the only row; we delete it and insert v8 so
    # get_schema_version() (LIMIT 1) returns "8", not "9".
    backend.run_write('DELETE FROM "SchemaVersion"', {})
    backend.run_write(
        'INSERT INTO "SchemaVersion" (version, indexed_sha) VALUES (?, ?)',
        {"version": "8", "indexed_sha": None},
    )

    try:
        with pytest.raises(RuntimeError) as exc_info:
            _assert_schema_current(backend, ":memory:")
        msg = str(exc_info.value)
        assert "db reset" in msg, f"Expected 'db reset' in refuse-to-start message, got: {msg!r}"
        assert "v8" in msg, f"Expected 'v8' (old version) in message, got: {msg!r}"
        assert "v9" in msg, f"Expected 'v9' (new version) in message, got: {msg!r}"
    finally:
        backend.close()
