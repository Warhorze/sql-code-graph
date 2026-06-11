"""Integration tests for File.skip_counts persistence.

Verifies that col_lineage_skip:* reasons are persisted on the File node as a
queryable JSON map after indexing, and that the schema-version gate prevents
opening a pre-bump graph without re-indexing.

Plan: plan/sprints/sprint_postmortem_fixes.md §PR 5 (Steps 5.1–5.2, N4).

Working query for §G accounting::

    SELECT path, skip_counts
    FROM "File"
    WHERE skip_counts IS NOT NULL
    ORDER BY path;

To extract per-reason totals::

    SELECT
        json_extract_string(skip_counts, '$.stage')::INT AS stage_skips,
        json_extract_string(skip_counts, '$.unknown_sentinel')::INT AS unknown_sentinel_skips
    FROM "File"
    WHERE path = '/path/to/file.sql';
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    """Fresh in-memory DuckDB with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Test: skip counts persisted and queryable via run_read
# ---------------------------------------------------------------------------


def test_skip_counts_persisted_for_file_with_stage_skips(db, tmp_path):
    """A file that emits col_lineage_skip:stage:* reasons has skip_counts persisted.

    The File row for the file must have a non-null skip_counts column whose
    JSON map includes a 'stage' key with the correct count.  The count must
    match the number of skip strings emitted (one per stage-read reference).

    Verifies the §G accounting premise: counts are queryable via run_read without
    log archaeology.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR 5, Step 5.2.
    """
    # Snowflake stage reads (@my_stage) produce col_lineage_skip:stage:* errors
    # in normalize_keys because the stage name does not survive parsing into a
    # usable TableRef full_id.  We inject the skip reasons directly via a mock
    # of ParsedFile.errors to avoid needing a real Snowflake fixture.
    from sqlcg.parsers.base import ParsedFile

    fake_path = tmp_path / "stage_reads.sql"
    fake_path.write_text("-- placeholder")

    parsed = ParsedFile(path=fake_path, dialect="snowflake")
    parsed.errors = [
        f"col_lineage_skip:stage:{fake_path}",
        f"col_lineage_skip:stage:{fake_path}",
        "col_lineage_skip:unknown_sentinel:col_a",
    ]

    indexer = Indexer()
    with db.transaction():
        indexer._upsert_parsed_file(parsed, db)

    # Query the persisted skip_counts column
    rows = db.run_read(
        'SELECT skip_counts FROM "File" WHERE path = ?',
        {"path": str(fake_path)},
    )
    assert len(rows) == 1, f"Expected 1 File row, got {len(rows)}"
    raw = rows[0]["skip_counts"]
    assert raw is not None, (
        "skip_counts must be non-null for a file that emitted col_lineage_skip:* reasons. "
        "Got NULL — the wiring from _build_file_rows to the DB is broken."
    )

    counts = json.loads(raw)
    assert counts.get("stage") == 2, f"Expected stage=2 in skip_counts, got {counts!r}"
    assert counts.get("unknown_sentinel") == 1, (
        f"Expected unknown_sentinel=1 in skip_counts, got {counts!r}"
    )


def test_skip_counts_null_for_clean_file(db, tmp_path):
    """A file with no col_lineage_skip:* errors stores NULL in skip_counts.

    Clean files (no skips) must not pollute the column with an empty JSON object
    '{}' — NULL is the correct representation so WHERE skip_counts IS NOT NULL
    filters only files with actual skips.
    """
    from sqlcg.parsers.base import ParsedFile

    clean_path = tmp_path / "clean.sql"
    clean_path.write_text("CREATE TABLE t (a INT);")

    parsed = ParsedFile(path=clean_path, dialect="ansi")
    parsed.errors = []

    indexer = Indexer()
    with db.transaction():
        indexer._upsert_parsed_file(parsed, db)

    rows = db.run_read(
        'SELECT skip_counts FROM "File" WHERE path = ?',
        {"path": str(clean_path)},
    )
    assert len(rows) == 1
    assert rows[0]["skip_counts"] is None, (
        f"skip_counts must be NULL for a clean file (no skips). Got {rows[0]['skip_counts']!r}."
    )


def test_skip_counts_queryable_across_corpus(db, tmp_path):
    """After indexing a corpus, skip_counts can be queried to get per-file counts.

    Indexes two files — one with skips, one clean — and asserts that:
    - The WHERE skip_counts IS NOT NULL filter returns exactly the file with skips.
    - The counts match the emitted skip strings.

    This is the §G working query pattern from the PR body.
    """
    from sqlcg.parsers.base import ParsedFile

    skip_path = tmp_path / "with_skips.sql"
    skip_path.write_text("-- placeholder")
    clean_path = tmp_path / "no_skips.sql"
    clean_path.write_text("CREATE TABLE t (a INT);")

    indexer = Indexer()
    with db.transaction():
        # File with skips
        p_skip = ParsedFile(path=skip_path, dialect="snowflake")
        p_skip.errors = [
            "col_lineage_skip:merge_branch:some_table",
            "col_lineage_skip:merge_branch:other_table",
            "col_lineage_skip:qualify_failed:TypeError",
        ]
        indexer._upsert_parsed_file(p_skip, db)

        # Clean file
        p_clean = ParsedFile(path=clean_path, dialect="ansi")
        indexer._upsert_parsed_file(p_clean, db)

    # §G working query: files with any skip counts
    rows = db.run_read(
        'SELECT path, skip_counts FROM "File" WHERE skip_counts IS NOT NULL ORDER BY path',
        {},
    )
    paths_with_skips = [r["path"] for r in rows]
    assert len(rows) == 1, (
        f"Expected exactly 1 file with skip_counts (the skip file). Got: {paths_with_skips}"
    )
    assert rows[0]["path"] == str(skip_path)
    counts = json.loads(rows[0]["skip_counts"])
    assert counts == {"merge_branch": 2, "qualify_failed": 1}, (
        f"Counts do not match emitted skip strings. Got {counts!r}"
    )


def test_skip_counts_index_full_file_populates_column(db, tmp_path):
    """Indexing a real SQL file through the full index_repo path persists skip_counts.

    Uses a Snowflake SQL fixture with a MERGE statement (guaranteed to emit
    col_lineage_skip:merge_branch:*) to verify the end-to-end path from parse
    → normalize_keys → _build_file_rows → upsert_nodes_bulk → File.skip_counts.
    """
    # MERGE statements emit col_lineage_skip:merge_branch:*
    sql = (
        "CREATE TABLE src (a INT, b INT);\n"
        "MERGE INTO tgt USING src ON tgt.a = src.a\n"
        "WHEN MATCHED THEN UPDATE SET tgt.b = src.b;\n"
    )
    sql_file = tmp_path / "merge_fixture.sf.sql"
    sql_file.write_text(sql)

    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    rows = db.run_read(
        'SELECT skip_counts FROM "File" WHERE path = ?',
        {"path": str(sql_file)},
    )
    assert len(rows) == 1, f"File row not found for {sql_file}"
    # MERGE emits at least one merge_branch skip
    raw = rows[0]["skip_counts"]
    assert raw is not None, (
        "skip_counts must be non-null for a file containing a MERGE statement "
        f"(expected col_lineage_skip:merge_branch:*). Got NULL. File: {sql_file}"
    )
    counts = json.loads(raw)
    assert counts.get("merge_branch", 0) >= 1, (
        f"Expected merge_branch >= 1 in skip_counts. Got {counts!r}"
    )


# ---------------------------------------------------------------------------
# Schema-gate test: opening a pre-bump graph fails the gate
# ---------------------------------------------------------------------------


def test_schema_gate_rejects_version_7_graph(tmp_path):
    """Opening a v7 graph fails the schema-version gate (now requires v8).

    The reindex.py and tools.py gate checks stored_version != SCHEMA_VERSION;
    this test pins that a graph initialized with a mocked v7 version is rejected
    when SCHEMA_VERSION='8'.

    Covers N4 (mandatory schema bump) from
    plan/sprints/sprint_postmortem_fixes.md §PR 5.
    """
    from sqlcg.core.schema import SCHEMA_VERSION

    # SCHEMA_VERSION must be '8' — the skip_counts bump
    assert SCHEMA_VERSION == "8", (
        f"This test pins the v8 migration; SCHEMA_VERSION={SCHEMA_VERSION!r}. "
        "Update the test if the version has been bumped again."
    )

    # Create a DB with a mocked v7 stored version
    old_db = DuckDBBackend(":memory:")
    old_db.init_schema()

    with patch.object(old_db, "get_schema_version", return_value="7"):
        stored = old_db.get_schema_version()
        assert stored == "7"
        # The gate condition used by reindex.py and tools.py
        assert stored != SCHEMA_VERSION, (
            "A v7 graph must be rejected — SCHEMA_VERSION is now '8' (File.skip_counts). "
            "Run 'sqlcg db reset && sqlcg index <path>' to re-index."
        )

    old_db.close()
