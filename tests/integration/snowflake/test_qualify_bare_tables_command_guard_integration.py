"""Integration tests for the _qualify_bare_tables Command-node fix (PR 2).

Indexes a small corpus that includes one crash-pattern file (USE SCHEMA + ALTER
EXTERNAL TABLE … REFRESH) and one genuinely-broken file.  After indexing:

- The crash-pattern file contributes statements to the graph (non-zero SqlQuery
  rows for that file) — previously it contributed zero because the crash nuked
  every statement.
- The genuinely-broken file is stored with ``parse_failed=True`` and a non-empty
  ``parse_cause`` (worker_error or E-code bucket).

Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Steps 2.1 + 2.2.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_read(backend, sql: str, params: dict | None = None) -> list[dict]:
    return backend.run_read(sql, params or {})


def _count_queries_for_file(backend, file_path: str) -> int:
    """Return the number of SqlQuery rows for a given file path."""
    rows = _run_read(
        backend,
        'SELECT count(*) AS n FROM "SqlQuery" WHERE file_path = ?',
        {"file_path": file_path},
    )
    return rows[0]["n"]


def _get_file_row(backend, file_path: str) -> dict | None:
    """Return the File node row for *file_path*, or None if not found."""
    rows = _run_read(
        backend,
        'SELECT path, parse_failed, parse_cause FROM "File" WHERE path = ?',
        {"path": file_path},
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_crash_pattern_file_contributes_statements_after_fix(tmp_path: Path) -> None:
    """Crash-pattern file (USE SCHEMA + ALTER EXTERNAL TABLE REFRESH) lands in graph.

    Before the fix, _qualify_bare_tables raised on the Command node, propagating
    out and nuking every statement in the file — zero SqlQuery rows.

    After the fix, the file parses cleanly and its statements are indexed.

    Observable output: SqlQuery count for the crash-pattern file is >= 1.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer

    # Build a small corpus: one crash-pattern file + one plain DDL file.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    crash_file = repo_root / "crash_pattern.sql"
    crash_file.write_text(
        "USE SCHEMA da;\n"
        "ALTER EXTERNAL TABLE da.x REFRESH;\n"
        "INSERT INTO da.tgt SELECT id FROM da.src;\n",
        encoding="utf-8",
    )

    plain_file = repo_root / "plain.sql"
    plain_file.write_text(
        "CREATE TABLE da.my_table (id INTEGER, name VARCHAR);\n",
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    indexer = Indexer()
    indexer.index_repo(repo_root, "snowflake", backend)

    crash_path_str = str(crash_file)
    n_queries = _count_queries_for_file(backend, crash_path_str)

    assert n_queries >= 1, (
        f"Expected >=1 SqlQuery rows for crash-pattern file, got {n_queries}. "
        "The _qualify_bare_tables fix may not have landed correctly."
    )

    backend.close()


def test_worker_level_failure_marks_file_parse_failed(tmp_path: Path) -> None:
    """A file that triggers a worker-level error is stored with parse_failed=True.

    We inject a file whose parse will return an _error_file ParsedFile (the
    worker_error path).  After indexing, the File node must have
    parse_failed=True and a non-empty parse_cause.

    We achieve this by monkeypatching the pool to return _error_file for a
    specific path, simulating what happens when a worker raises an unhandled
    exception.

    Observable output: File.parse_failed == True and File.parse_cause != "".

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.2.
    """
    from unittest.mock import patch

    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.indexer.pool import _error_file

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # A valid SQL file — it will normally parse fine.
    normal_file = repo_root / "normal.sql"
    normal_file.write_text(
        "CREATE TABLE schema1.tbl (id INTEGER);\n",
        encoding="utf-8",
    )

    # The "broken" file — we will intercept its result and substitute _error_file.
    broken_file = repo_root / "broken.sql"
    broken_file.write_text(
        "SELECT 1;\n",  # valid SQL, but we'll override the pool result
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    broken_path_str = str(broken_file)

    # Patch HardKillPool.map to intercept results for the broken file.
    original_map = None

    def patched_map(self, tasks, per_task_timeout, **kwargs):
        results = original_map(self, tasks, per_task_timeout, **kwargs)
        for i, task in enumerate(tasks):
            if task.get("path") == broken_path_str:
                results[i] = _error_file(
                    broken_path_str,
                    "snowflake",
                    "AttributeError:'str' object has no attribute 'args'",
                )
        return results

    from sqlcg.indexer import pool as pool_mod

    original_map = pool_mod.HardKillPool.map
    with patch.object(pool_mod.HardKillPool, "map", patched_map):
        indexer = Indexer()
        indexer.index_repo(repo_root, "snowflake", backend)

    file_row = _get_file_row(backend, broken_path_str)
    assert file_row is not None, f"Expected a File row for {broken_path_str!r} but none was found."
    assert file_row["parse_failed"] is True, (
        f"Expected parse_failed=True for worker-error file, got {file_row['parse_failed']!r}. "
        "The worker_error classification fix may not have landed."
    )
    assert file_row["parse_cause"] != "", (
        f"Expected non-empty parse_cause for worker-error file, got {file_row['parse_cause']!r}."
    )

    backend.close()
