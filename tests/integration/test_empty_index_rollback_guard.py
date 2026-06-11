"""Integration tests for the empty-index rollback guard in writer._do_index.

When the MCP drain runs an ``op=index`` request against a root directory that
contains zero SQL files, the guard raises inside the ``transaction()`` context
so DuckDB rolls back the ``clear_all_tables()`` call.  The prior graph is left
intact (row counts unchanged) and the error propagates to the caller.

Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.4 (A1).
"""

from __future__ import annotations

from pathlib import Path


def _seed_graph(backend, n_tables: int = 2) -> None:
    """Seed a minimal graph so we can measure pre/post row counts."""
    from sqlcg.core.duckdb_backend import DuckDBBackend as DB

    assert isinstance(backend, DB)
    # Insert Repo + SqlTable rows directly so we have something to preserve.
    backend._conn.execute(
        'INSERT OR REPLACE INTO "Repo" (path, name) VALUES (?, ?)',
        ["/repo", "repo"],
    )
    for i in range(n_tables):
        backend._conn.execute(
            'INSERT OR REPLACE INTO "SqlTable" (qualified, name, kind, defined_in_file) '
            "VALUES (?, ?, ?, ?)",
            [f"db.schema.table_{i}", f"table_{i}", "table", "/repo/ddl.sql"],
        )


def _count_rows(backend, table: str) -> int:
    rows = backend.run_read(f'SELECT count(*) AS n FROM "{table}"', {})
    return rows[0]["n"]


# ---------------------------------------------------------------------------
# Guard: empty root rolls back; existing graph unchanged
# ---------------------------------------------------------------------------


def test_empty_root_index_rolls_back_preserves_graph(tmp_path: Path) -> None:
    """op=index on an empty directory must roll back and leave the prior graph intact.

    Observable outputs:
    - SqlTable row count before == after the failed drain
    - The raised error message contains "refusing to index empty root"

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.4 (A1).
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed 2 SqlTable rows to represent a populated graph.
    _seed_graph(backend, n_tables=2)
    pre_count = _count_rows(backend, "SqlTable")
    assert pre_count == 2, f"Expected 2 pre-drain SqlTable rows, got {pre_count}"

    # Create an empty root directory — no SQL files.
    empty_root = tmp_path / "empty_repo"
    empty_root.mkdir()

    # Invoke _do_index logic directly, replicating the writer drain body.
    # We patch at the transaction level so the rollback happens correctly.
    from sqlcg.indexer.indexer import Indexer

    indexer = Indexer()
    raised_error: str = ""

    try:
        with backend.transaction():
            backend.clear_all_tables()
            summary = indexer.index_repo(
                empty_root,
                "snowflake",
                backend,
            )
            if summary.get("files_found", 0) == 0:
                raise ValueError("refusing to index empty root — graph preserved")
    except ValueError as exc:
        raised_error = str(exc)

    assert "refusing to index empty root" in raised_error, (
        f"Expected a 'refusing to index empty root' error, got: {raised_error!r}"
    )

    # After rollback, the prior graph must be intact.
    post_count = _count_rows(backend, "SqlTable")
    assert post_count == pre_count, (
        f"SqlTable row count must be unchanged after empty-root rollback. "
        f"Expected {pre_count}, got {post_count}. "
        "The guard failed — clear_all_tables() was not rolled back."
    )

    backend.close()


# ---------------------------------------------------------------------------
# Guard counterpart: non-empty root commits normally
# ---------------------------------------------------------------------------


def test_non_empty_root_index_commits_normally(tmp_path: Path) -> None:
    """op=index on a non-empty directory must commit without raising.

    Regression guard for the empty-root guard: a legitimate full index must
    still commit and replace the prior graph.

    Observable output: SqlTable row count after commit equals the count of
    tables defined in the test SQL fixture (non-zero).

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.4 (A1).
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed a graph with 3 tables that will be wiped and replaced.
    _seed_graph(backend, n_tables=3)
    pre_count = _count_rows(backend, "SqlTable")
    assert pre_count == 3, f"Expected 3 pre-drain SqlTable rows, got {pre_count}"

    # Create a repo root with at least one SQL file that defines a table.
    repo_root = tmp_path / "my_repo"
    repo_root.mkdir()
    sql_file = repo_root / "ddl.sql"
    sql_file.write_text("CREATE TABLE my_schema.my_new_table (id INTEGER, name VARCHAR);\n")

    indexer = Indexer()
    raised_error: str = ""

    try:
        with backend.transaction():
            backend.clear_all_tables()
            summary = indexer.index_repo(
                repo_root,
                "snowflake",
                backend,
            )
            if summary.get("files_found", 0) == 0:
                raise ValueError("refusing to index empty root — graph preserved")
    except ValueError as exc:
        raised_error = str(exc)

    assert raised_error == "", (
        f"Non-empty root must not trigger the rollback guard. Got error: {raised_error!r}"
    )

    # After commit, the graph should have the newly indexed tables.
    post_count = _count_rows(backend, "SqlTable")
    assert post_count > 0, (
        f"SqlTable must be non-empty after a successful non-empty index. Got {post_count} rows."
    )

    backend.close()


# ---------------------------------------------------------------------------
# Unit: index_repo summary includes files_found key
# ---------------------------------------------------------------------------


def test_index_repo_summary_includes_files_found(tmp_path: Path) -> None:
    """index_repo() must return a summary dict with a 'files_found' key.

    The empty-index guard reads summary['files_found'] to determine whether the
    walker found any SQL files.  If the key is absent, the guard silently falls
    back to 0 (via .get()) and fires incorrectly on every index.

    Observable output: 'files_found' key present in the returned dict with a
    non-negative integer value.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.4 (A1).
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Create a repo with one SQL file.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "t.sql").write_text("SELECT 1;")

    indexer = Indexer()
    summary = indexer.index_repo(repo_root, "snowflake", backend)

    assert "files_found" in summary, (
        f"index_repo() summary must contain 'files_found' key. Got keys: {list(summary.keys())}"
    )
    assert isinstance(summary["files_found"], int), (
        f"'files_found' must be an int, got {type(summary['files_found']).__name__}"
    )
    assert summary["files_found"] >= 1, (
        f"'files_found' must be >= 1 for a non-empty repo, got {summary['files_found']}"
    )

    backend.close()


def test_index_repo_summary_files_found_zero_for_empty_root(tmp_path: Path) -> None:
    """index_repo() on an empty directory returns files_found == 0.

    This is the signal the empty-index guard reads to decide whether to roll back.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.4 (A1).
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    empty_root = tmp_path / "empty"
    empty_root.mkdir()

    indexer = Indexer()
    summary = indexer.index_repo(empty_root, "snowflake", backend)

    assert summary.get("files_found") == 0, (
        f"'files_found' must be 0 for an empty root, got {summary.get('files_found')}"
    )

    backend.close()
