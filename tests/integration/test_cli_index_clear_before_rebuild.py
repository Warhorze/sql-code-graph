"""Integration tests for the CLI direct-index clear-before-rebuild fix.

Prior to v1.21.2, the direct CLI path (no MCP server live) called
``index_repo()`` with no prior clear, leaving stale keys from earlier
runs permanently in the graph.  The fix routes through
``atomic_full_index()`` — the same shared helper used by the MCP drain —
which wraps ``clear_all_tables()`` + ``index_repo()`` + empty-root guard
in a single ``db.transaction()``.

Guards the fix described in plan/sprints/temp_table_namespacing.md
§Deviations Deviation 3 (fossil-key stale-graph artifact).
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_graph(backend, n_tables: int = 2) -> None:
    """Seed a minimal graph so we can measure pre/post row counts."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    assert isinstance(backend, DuckDBBackend)
    backend._conn.execute(
        'INSERT OR REPLACE INTO "Repo" (path, name) VALUES (?, ?)',
        ["/stale_repo", "stale_repo"],
    )
    for i in range(n_tables):
        backend._conn.execute(
            'INSERT OR REPLACE INTO "SqlTable" (qualified, name, kind, defined_in_file) '
            "VALUES (?, ?, ?, ?)",
            [f"old_schema.stale_table_{i}", f"stale_table_{i}", "table", "/stale_repo/ddl.sql"],
        )


def _count_rows(backend, table: str) -> int:
    rows = backend.run_read(f'SELECT count(*) AS n FROM "{table}"', {})
    return rows[0]["n"]


def _qualified_keys(backend) -> set[str]:
    rows = backend.run_read('SELECT qualified FROM "SqlTable"', {})
    return {r["qualified"] for r in rows}


# ---------------------------------------------------------------------------
# Regression: stale keys are gone after reindex via atomic_full_index
# ---------------------------------------------------------------------------


def test_stale_keys_absent_after_cli_reindex(tmp_path: Path) -> None:
    """Stale SqlTable rows from a prior index must be absent after reindex.

    Scenario: the graph contains old_schema.stale_table_0 and
    old_schema.stale_table_1 from a previous index run.  A fresh SQL corpus
    defines only new_schema.fresh_table.  After reindex via atomic_full_index
    (the CLI path), the stale rows must be gone and the new row present.

    This is the regression test for the fossil-key bug: before the fix,
    _run_index called index_repo() with no clear, so the stale rows were
    never deleted.

    Guards plan/sprints/temp_table_namespacing.md §Deviations Deviation 3.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer, atomic_full_index

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed stale rows as if a prior index run left them.
    _seed_graph(backend, n_tables=2)
    pre_stale_keys = _qualified_keys(backend)
    assert pre_stale_keys == {"old_schema.stale_table_0", "old_schema.stale_table_1"}, (
        f"Pre-condition failed — expected 2 stale rows, got: {pre_stale_keys}"
    )

    # Create a fresh SQL corpus with a completely different table.
    repo_root = tmp_path / "new_repo"
    repo_root.mkdir()
    (repo_root / "fresh.sql").write_text(
        "CREATE TABLE new_schema.fresh_table (id INTEGER, label VARCHAR);\n"
    )

    # Run the shared CLI+server helper.
    indexer = Indexer()
    summary = atomic_full_index(indexer, repo_root, "snowflake", backend)

    # Observable output 1: the stale keys must be absent.
    post_keys = _qualified_keys(backend)
    stale_still_present = pre_stale_keys & post_keys
    assert not stale_still_present, (
        f"Stale SqlTable rows were NOT removed after reindex: {stale_still_present}. "
        "atomic_full_index must clear the graph before rebuilding."
    )

    # Observable output 2: the new table must be present.
    assert any("fresh_table" in k for k in post_keys), (
        f"Expected 'fresh_table' in post-reindex SqlTable keys, got: {post_keys}. "
        "The fresh corpus was not indexed."
    )

    # Observable output 3: summary files_found >= 1.
    assert summary.get("files_found", 0) >= 1, (
        f"summary['files_found'] must be >= 1, got {summary.get('files_found')}"
    )

    backend.close()


# ---------------------------------------------------------------------------
# Guard: empty root over a populated graph rolls back and raises
# ---------------------------------------------------------------------------


def test_cli_empty_dir_rollback_preserves_graph(tmp_path: Path) -> None:
    """CLI full-index of an empty dir must roll back and leave the prior graph intact.

    When atomic_full_index finds zero SQL files it raises ValueError inside
    the transaction, so clear_all_tables() is rolled back.  The prior
    graph is preserved and the error message contains the canonical guard text.

    Guards plan/sprints/temp_table_namespacing.md §Deviations Deviation 3.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import _EMPTY_ROOT_ERROR, Indexer, atomic_full_index

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed a populated graph.
    _seed_graph(backend, n_tables=3)
    pre_count = _count_rows(backend, "SqlTable")
    assert pre_count == 3, f"Expected 3 pre-run SqlTable rows, got {pre_count}"

    # An empty root directory — no SQL files.
    empty_root = tmp_path / "empty_repo"
    empty_root.mkdir()

    indexer = Indexer()
    raised_error = ""
    try:
        atomic_full_index(indexer, empty_root, "snowflake", backend)
    except ValueError as exc:
        raised_error = str(exc)

    # Observable output 1: the guard error was raised.
    assert _EMPTY_ROOT_ERROR in raised_error, (
        f"Expected '{_EMPTY_ROOT_ERROR}' in raised error, got: {raised_error!r}"
    )

    # Observable output 2: SqlTable count is unchanged (rollback succeeded).
    post_count = _count_rows(backend, "SqlTable")
    assert post_count == pre_count, (
        f"SqlTable row count must be unchanged after empty-root rollback. "
        f"Expected {pre_count}, got {post_count}. "
        "The clear_all_tables() was not rolled back."
    )

    backend.close()


# ---------------------------------------------------------------------------
# Normal path: CLI index via atomic_full_index succeeds end-to-end
# ---------------------------------------------------------------------------


def test_cli_normal_index_via_atomic_full_index(tmp_path: Path) -> None:
    """A normal CLI full-index via atomic_full_index commits and yields a non-empty graph.

    Regression guard: atomic_full_index must not block or break the happy path.
    Verifies: (a) no exception raised, (b) SqlTable count > 0, (c) files_found > 0.

    Guards plan/sprints/temp_table_namespacing.md §Deviations Deviation 3.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer, atomic_full_index

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "ddl.sql").write_text(
        "CREATE TABLE my_schema.orders (id INTEGER, amount FLOAT);\n"
        "CREATE TABLE my_schema.customers (id INTEGER, name VARCHAR);\n"
    )

    indexer = Indexer()
    summary = atomic_full_index(indexer, repo_root, "snowflake", backend)

    # Observable output 1: no exception; summary has files_found >= 1.
    assert summary.get("files_found", 0) >= 1, (
        f"summary['files_found'] must be >= 1, got {summary.get('files_found')}"
    )

    # Observable output 2: at least one SqlTable row was committed.
    post_count = _count_rows(backend, "SqlTable")
    assert post_count > 0, (
        f"SqlTable must be non-empty after a successful normal index. Got {post_count} rows."
    )

    backend.close()
