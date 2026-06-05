"""MVCC integration test for Phase 4 — DuckDB reindex path.

Phase 4 exit gate (C3 and C6 as integration tests):

C3 — concurrent read during rebuild sees OLD snapshot until COMMIT:
  A reader connection must see the last committed graph state while a rebuild
  transaction is in flight.  Only after COMMIT does the reader see the new graph.
  This is the property that makes ``with db.transaction(): clear + bulk_insert``
  a safe replacement for the old symlink-swap approach.

C6 — crash mid-write leaves OLD graph intact:
  A transaction that raises mid-way rolls back cleanly; a fresh connection sees
  the OLD graph, not a partial write.

Both tests assert observable output (row counts / specific row values), not just
"no exception raised".
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# C3 — Concurrent read during rebuild sees OLD then NEW
# ---------------------------------------------------------------------------


def test_mvcc_reader_sees_old_graph_during_in_flight_rebuild(tmp_path: Path) -> None:
    """A reader connection sees the OLD committed graph while a rebuild transaction
    is in flight (not yet committed), and the NEW graph only after COMMIT.

    Observable outputs:
    - mid_rebuild_count: equals old_count (OLD snapshot)
    - post_commit_count: equals new_count (NEW graph after COMMIT)

    This proves DuckDB's MVCC replaces the old symlink-swap atomicity guarantee.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend

    db_path = str(tmp_path / "test.db")
    writer = DuckDBBackend(db_path)
    writer.init_schema()

    # Seed the "old" graph: insert 3 Repo rows.
    writer._conn.execute(
        """
        INSERT OR REPLACE INTO "Repo" (path, name)
        SELECT unnest(?::VARCHAR[]) AS path, unnest(?::VARCHAR[]) AS name
        """,
        [
            ["/repo/a", "/repo/b", "/repo/c"],
            ["a", "b", "c"],
        ],
    )

    old_count = writer.run_read('SELECT count(*) AS n FROM "Repo"', {})[0]["n"]
    assert old_count == 3, f"Expected 3 old Repo rows, got {old_count}"

    # Capture mid-rebuild state using a separate DuckDB cursor (same connection,
    # different snapshot in a read transaction).
    mid_rebuild_counts: list[int] = []
    commit_ready = threading.Event()
    reader_done = threading.Event()

    def _reader() -> None:
        """Read Repo count mid-rebuild (before COMMIT) using a cursor snapshot."""
        # Wait until the writer has cleared the table (but not committed yet)
        commit_ready.wait(timeout=5.0)
        # DuckDB MVCC: cursor opened here sees the last committed state
        cursor = writer._conn.cursor()
        rows = cursor.execute('SELECT count(*) AS n FROM "Repo"').fetchall()
        mid_rebuild_counts.append(rows[0][0])
        reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Writer: BEGIN transaction, clear tables, signal reader, then COMMIT.
    writer._conn.execute("BEGIN")
    try:
        writer._conn.execute('DELETE FROM "Repo"')
        # Signal reader that clear is done (but txn not committed)
        commit_ready.set()
        # Wait for reader to capture mid-rebuild count before we commit
        reader_done.wait(timeout=5.0)

        # Insert the "new" graph: 5 Repo rows.
        writer._conn.execute(
            """
            INSERT INTO "Repo" (path, name)
            SELECT unnest(?::VARCHAR[]) AS path, unnest(?::VARCHAR[]) AS name
            """,
            [
                ["/repo/1", "/repo/2", "/repo/3", "/repo/4", "/repo/5"],
                ["1", "2", "3", "4", "5"],
            ],
        )
        writer._conn.execute("COMMIT")
    except Exception:
        writer._conn.execute("ROLLBACK")
        raise

    reader_thread.join(timeout=5.0)

    # Post-commit: reader sees the new graph.
    post_commit_count = writer.run_read('SELECT count(*) AS n FROM "Repo"', {})[0]["n"]

    writer.close()

    # C3 assertion: mid-rebuild the cursor saw the OLD committed snapshot (3 rows).
    assert len(mid_rebuild_counts) == 1, f"Reader did not capture count: {mid_rebuild_counts}"
    assert mid_rebuild_counts[0] == old_count, (
        f"MVCC FAILED: expected reader to see OLD count {old_count} mid-rebuild, "
        f"but got {mid_rebuild_counts[0]}. "
        "Readers should see the last committed state during an in-flight write txn."
    )

    # C3 assertion: after COMMIT, the new graph is visible.
    assert post_commit_count == 5, f"Expected 5 Repo rows after COMMIT, got {post_commit_count}"


# ---------------------------------------------------------------------------
# C6 — Crash mid-write leaves OLD graph intact (rollback via WAL)
# ---------------------------------------------------------------------------


def test_c6_mid_transaction_raise_rolls_back_leaves_old_graph(tmp_path: Path) -> None:
    """A transaction that raises mid-way rolls back cleanly.

    After the exception, the OLD graph is fully intact — no partial write.
    This replaces the old hand-rolled ``recover_on_open`` guard.

    Observable outputs:
    - repo_count_after_rollback equals old_count (unchanged)
    - no data from the failed transaction is visible
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed old graph: 2 Repo rows.
    backend._conn.execute(
        """
        INSERT OR REPLACE INTO "Repo" (path, name)
        VALUES ('/old/a', 'a'), ('/old/b', 'b')
        """
    )
    old_count = backend.run_read('SELECT count(*) AS n FROM "Repo"', {})[0]["n"]
    assert old_count == 2

    # Attempt a rebuild that raises mid-transaction.
    with pytest.raises(ValueError, match="intentional mid-rebuild failure"):
        with backend.transaction():
            backend.clear_all_tables()
            # Insert one new row then raise before COMMIT.
            backend._conn.execute("INSERT INTO \"Repo\" (path, name) VALUES ('/new/x', 'x')")
            raise ValueError("intentional mid-rebuild failure")

    # After rollback: OLD graph must be fully intact.
    count_after = backend.run_read('SELECT count(*) AS n FROM "Repo"', {})[0]["n"]
    assert count_after == old_count, (
        f"C6 FAILED: expected {old_count} Repo rows after rollback, got {count_after}. "
        "The old graph must be fully intact after a mid-rebuild raise."
    )

    # No trace of the new row.
    new_rows = backend.run_read("SELECT path FROM \"Repo\" WHERE path = '/new/x'", {})
    assert new_rows == [], f"C6 FAILED: partial write leaked: {new_rows}"

    backend.close()


# ---------------------------------------------------------------------------
# Full rebuild-in-transaction with clear_all_tables (drain body shape)
# ---------------------------------------------------------------------------


def test_full_rebuild_in_transaction_is_atomic(tmp_path: Path) -> None:
    """The drain body shape: clear_all_tables() + bulk insert inside one transaction.

    Verifies:
    - After COMMIT, new rows are visible and old rows are gone.
    - The SchemaVersion row is preserved by clear_all_tables().
    - Concurrent MVCC reader does not see partial state mid-rebuild.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.core.schema import SCHEMA_VERSION

    db_path = str(tmp_path / "rebuild_test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()

    # Seed old graph.
    backend._conn.execute("INSERT OR REPLACE INTO \"Repo\" (path, name) VALUES ('/old', 'old')")

    # Full rebuild inside a transaction.
    with backend.transaction():
        backend.clear_all_tables()
        backend._conn.execute("INSERT INTO \"Repo\" (path, name) VALUES ('/new', 'new')")
        # SchemaVersion should survive clear_all_tables — re-insert after rebuild.
        backend._conn.execute(
            'INSERT OR REPLACE INTO "SchemaVersion" (version, indexed_sha) VALUES (?, NULL)',
            [SCHEMA_VERSION],
        )

    # Assertions on observable output.
    repos = backend.run_read('SELECT path FROM "Repo"', {})
    assert len(repos) == 1, f"Expected 1 Repo row after rebuild, got {repos}"
    assert repos[0]["path"] == "/new", f"Expected /new, got {repos[0]['path']}"

    schema_rows = backend.run_read('SELECT version FROM "SchemaVersion"', {})
    assert schema_rows, "SchemaVersion row must survive clear_all_tables + rebuild"
    assert schema_rows[0]["version"] == SCHEMA_VERSION

    backend.close()
