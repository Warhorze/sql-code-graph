"""Tests for read-only KùzuDB connection cross-process lock semantics (#28 read path).

KùzuDB uses a *native exclusive write lock* — the lock is process-level, not
thread-level.  Measured cross-process behavior (verified on real indexed DB):

  - A read-WRITE process holding the DB → every other open fails, including
    ``read_only=True``.  Reads during an active writer are NOT supported by
    KùzuDB's design; this is a known limitation (see plan/v1_1_2_bugfix.md §Cluster 2).
  - A read-ONLY process holding the DB → other ``read_only=True`` opens succeed.
    This is the genuine #28 benefit: *reader/reader concurrency*.  Read commands
    opened with ``read_only=True`` no longer take an exclusive write lock, so
    multiple CLI reads can run concurrently without blocking one another or a
    pending reindex.

Tests in this module use actual separate OS processes (``multiprocessing``) to
exercise real cross-process lock behavior.  Same-process opens are insufficient
because this KùzuDB build does not enforce the lock within a single process.

Also covers:
  - Call-site wiring: each CLI read command opens get_backend(read_only=True).
  - Missing-DB degradation: read-only open of a never-indexed DB shows the
    empty-DB hint, not a raw KùzuDB stacktrace.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.core.config import get_backend
from sqlcg.core.kuzu_backend import KuzuBackend

# ---------------------------------------------------------------------------
# Cross-process lock-semantics helpers
# ---------------------------------------------------------------------------


def _child_hold_db(
    db_path: str,
    read_only: bool,
    ready_event: multiprocessing.Event,  # type: ignore[type-arg]
    release_event: multiprocessing.Event,  # type: ignore[type-arg]
    error_queue: multiprocessing.Queue[str],  # type: ignore[type-arg]
) -> None:
    """Child-process worker: open the DB, signal ready, wait for release.

    On any error during open the error string is put on *error_queue* so the
    parent can re-raise a meaningful assertion failure.
    """
    try:
        backend = KuzuBackend(db_path, read_only=read_only)
        ready_event.set()
        release_event.wait(timeout=10)
        backend.close()
    except Exception as exc:  # noqa: BLE001
        error_queue.put(str(exc))
        ready_event.set()  # unblock parent even on error


def _spawn_db_holder(
    db_path: str,
    read_only: bool,
) -> tuple[
    multiprocessing.Process,
    multiprocessing.Event,  # type: ignore[type-arg]
    multiprocessing.Queue[str],  # type: ignore[type-arg]
]:
    """Spawn a child process that holds the DB open and return (proc, release_event, error_queue).

    Waits until the child signals it has the DB open (or errored out).
    The caller must call ``release_event.set()`` then ``proc.join()`` to clean up.
    """
    ctx = multiprocessing.get_context("spawn")
    ready_event = ctx.Event()
    release_event = ctx.Event()
    error_queue: multiprocessing.Queue[str] = ctx.Queue()

    proc = ctx.Process(
        target=_child_hold_db,
        args=(db_path, read_only, ready_event, release_event, error_queue),
        daemon=True,
    )
    proc.start()
    ready_event.wait(timeout=10)
    return proc, release_event, error_queue


# ---------------------------------------------------------------------------
# Cross-process invariant 1: reader + reader concurrency (the real #28 benefit)
# ---------------------------------------------------------------------------


def test_cross_process_reader_reader_concurrency() -> None:
    """Two read-only processes can open the same DB concurrently.

    This is the genuine #28 benefit: CLI reads opened with read_only=True no
    longer take an exclusive write lock, so multiple ``analyze`` / ``find`` /
    ``db info`` invocations can run at the same time.

    Cross-process verification: a *spawned* child holds the DB read-only while
    the main process opens it read-only and queries rows.  Observable output:
    the main process must return the row that was written before both opens.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "reader_reader.db")

        # Seed the DB (writer is fully closed before any reader opens).
        seeder = KuzuBackend(db_path)
        seeder.init_schema()
        seeder.run_write("MERGE (r:Repo {path: '/rr/repo', name: 'rr'})", {})
        seeder.close()

        # Child process holds the DB read-only.
        proc, release_event, error_queue = _spawn_db_holder(db_path, read_only=True)
        try:
            # Child must have opened without error.
            assert error_queue.empty(), (
                f"Child reader failed to open DB: {error_queue.get_nowait()}"
            )

            # Main process: open read-only while child holds it read-only.
            reader = KuzuBackend(db_path, read_only=True)
            try:
                rows = reader.run_read("MATCH (r:Repo) RETURN r.path AS path", {})
                # Observable output: must return the seeded row.
                assert len(rows) == 1, (
                    f"Expected 1 Repo row from concurrent read-only open, got {rows}"
                )
                assert rows[0]["path"] == "/rr/repo", f"Unexpected path: {rows[0]['path']}"
            finally:
                reader.close()
        finally:
            release_event.set()
            proc.join(timeout=5)


# ---------------------------------------------------------------------------
# Cross-process invariant 2: exclusive writer blocks all opens (incl. read-only)
# ---------------------------------------------------------------------------


def test_cross_process_writer_blocks_readonly_open() -> None:
    """A read-write writer process holding the DB blocks all opens, read-only included.

    This documents the known KùzuDB limitation: KùzuDB's exclusive write lock
    is process-level.  When a writer (MCP server / active reindex) holds the lock,
    even ``read_only=True`` opens fail with "Database is locked".  The #28 fix
    (read_only=True on CLI read commands) provides *reader/reader* concurrency
    but cannot bypass an active writer's exclusive lock.

    Cross-process verification: a *spawned* child holds the DB read-write while
    the main process attempts a read-only open.  Observable output: the open must
    raise RuntimeError with "Database is locked" in the message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "writer_lock.db")

        # Seed the DB, then close so the child can open read-write.
        seeder = KuzuBackend(db_path)
        seeder.init_schema()
        seeder.run_write("MERGE (r:Repo {path: '/wr/repo', name: 'wr'})", {})
        seeder.close()

        # Child process holds the DB read-WRITE (exclusive lock).
        proc, release_event, error_queue = _spawn_db_holder(db_path, read_only=False)
        try:
            # Child must have opened without error.
            assert error_queue.empty(), (
                f"Child writer failed to open DB: {error_queue.get_nowait()}"
            )

            # Main process: attempt read-only open while child holds write lock.
            with pytest.raises(RuntimeError) as exc_info:
                KuzuBackend(db_path, read_only=True)

            msg = str(exc_info.value)
            # Must surface the "Database is locked" message, NOT a Python traceback.
            assert "Database is locked" in msg, (
                f"Expected 'Database is locked' in error from read-only open "
                f"while writer holds lock, got: {msg!r}"
            )
        finally:
            release_event.set()
            proc.join(timeout=5)


def test_readonly_returns_expected_rows() -> None:
    """Read-only open queries the same data the writer committed.

    Observable-output check: the read-only reader must return non-empty rows
    that match what the writer inserted.  This pins that read_only=True does
    not silently suppress results.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test2.db")

        # Writer: insert two repos.
        writer = KuzuBackend(db_path)
        writer.init_schema()
        writer.run_write("MERGE (r:Repo {path: '/repo/a', name: 'a'})", {})
        writer.run_write("MERGE (r:Repo {path: '/repo/b', name: 'b'})", {})
        writer.close()

        # Reader: open separately, assert both rows visible.
        reader = KuzuBackend(db_path, read_only=True)
        try:
            rows = reader.run_read("MATCH (r:Repo) RETURN r.path AS path ORDER BY r.path", {})
            assert len(rows) == 2, f"Expected 2 Repo rows, got {len(rows)}: {rows}"
            assert rows[0]["path"] == "/repo/a"
            assert rows[1]["path"] == "/repo/b"
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Missing-DB read-only degradation test (Step 2.3)
# ---------------------------------------------------------------------------


def test_readonly_open_of_missing_db_raises_empty_db_hint() -> None:
    """Read-only open of a never-indexed DB raises a friendly RuntimeError.

    KùzuDB raises "Cannot create an empty database under READ ONLY mode" on a
    non-existent path.  get_backend(read_only=True) must translate this to the
    existing empty-DB guidance, not expose the raw KùzuDB message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        nonexistent = os.path.join(tmp, "never_indexed", "db")
        os.environ["SQLCG_DB_PATH"] = nonexistent
        try:
            with pytest.raises(RuntimeError) as exc_info:
                with get_backend(read_only=True):
                    pass
            msg = str(exc_info.value)
            # Must mention guidance for an un-initialised database.
            assert "db init" in msg.lower() or "not initialised" in msg.lower(), (
                f"Expected empty-DB guidance in error message, got: {msg}"
            )
            # Must NOT expose the raw KùzuDB message.
            assert "READ ONLY" not in msg, f"Raw KùzuDB message leaked through: {msg}"
        finally:
            # Restore env so other tests are unaffected.
            os.environ.pop("SQLCG_DB_PATH", None)


# ---------------------------------------------------------------------------
# Call-site wiring assertions (prevent future edits from dropping the flag)
# ---------------------------------------------------------------------------


def _make_mock_backend() -> MagicMock:
    """Return a mock backend context manager that returns an empty result."""
    mock_be = MagicMock()
    mock_be.__enter__ = MagicMock(return_value=mock_be)
    mock_be.__exit__ = MagicMock(return_value=False)
    mock_be.run_read = MagicMock(return_value=[])
    mock_be.get_schema_version = MagicMock(return_value="6")
    mock_be.get_indexed_sha = MagicMock(side_effect=NotImplementedError)
    return mock_be


# Each entry: (module_path, function_name, kwargs_to_invoke_the_function)
# kwargs must bypass typer.OptionInfo/ArgumentInfo default objects so the
# function body is actually reached (past any input validation).
_READ_CALL_SITES = [
    (
        "sqlcg.cli.commands.analyze",
        "upstream",
        {"ref": "s.t.c", "depth": 5, "raw": False, "include_intermediate": False},
    ),
    (
        "sqlcg.cli.commands.analyze",
        "downstream",
        {"ref": "s.t.c", "depth": 5, "raw": False, "include_intermediate": False},
    ),
    (
        "sqlcg.cli.commands.analyze",
        "impact",
        {"table": "schema.table", "raw": False},
    ),
    (
        "sqlcg.cli.commands.analyze",
        "failures",
        {"cause": None, "limit": 100},
    ),
    (
        "sqlcg.cli.commands.analyze",
        "unused",
        {"threshold": 0, "raw": False},
    ),
    (
        "sqlcg.cli.commands.find",
        "find_table",
        {"name": "my_table", "raw": False},
    ),
    (
        "sqlcg.cli.commands.find",
        "find_column",
        {"ref": "table.col", "raw": False},
    ),
    (
        "sqlcg.cli.commands.find",
        "find_pattern",
        {"pattern": "SELECT"},
    ),
    (
        "sqlcg.cli.commands.db",
        "db_info",
        {},
    ),
    (
        "sqlcg.cli.commands.db",
        "list_repos",
        {},
    ),
]


@pytest.mark.parametrize("module,func_name,call_kwargs", _READ_CALL_SITES)
def test_read_command_opens_readonly_backend(
    module: str, func_name: str, call_kwargs: dict
) -> None:
    """Assert each CLI read command calls get_backend(read_only=True).

    Patches get_backend at the import site inside the command module (not at
    the definition site in config.py) so the mock intercepts the actual call.
    Reverting read_only=True at any call site makes the corresponding case fail.
    """
    mod = importlib.import_module(module)
    func = getattr(mod, func_name)

    captured: list[dict] = []

    def _capturing(**kw):  # type: ignore[override]
        captured.append(kw)
        return _make_mock_backend()

    # Patch at the module where get_backend is used (imported-by-name), not at
    # the definition site (sqlcg.core.config).
    with patch(f"{module}.get_backend", _capturing):
        try:
            func(**call_kwargs)
        except SystemExit:
            pass  # typer.Exit is fine; kwarg capture is what matters
        except Exception:
            pass  # backend mock may cause downstream errors; kwarg capture is what matters

    assert captured, f"{func_name}: get_backend was not called"
    assert captured[0].get("read_only") is True, (
        f"{func_name}: expected get_backend(read_only=True), got get_backend(**{captured[0]})"
    )


def test_db_init_does_not_open_readonly_backend() -> None:
    """db init must NOT open read_only=True (it writes the schema).

    We verify by source inspection that the db_init call uses get_backend()
    without read_only=True, and rely on the call-site parametrized test to
    confirm that ONLY read commands set read_only=True.
    """
    import inspect

    from sqlcg.cli.commands import db as db_module

    # Read the source of db_init and verify no read_only=True appears in the
    # get_backend call inside it.
    source = inspect.getsource(db_module.db_init)
    assert (
        "get_backend()" in source
        or "get_backend(read_only=False)" in source
        or ("get_backend(read_only=True)" not in source)
    ), "db_init must not call get_backend(read_only=True)"


def test_db_reset_does_not_open_readonly_backend() -> None:
    """db reset must NOT open read_only=True (it writes data).

    We verify by source inspection that the db_reset call uses get_backend()
    without read_only=True.
    """
    import inspect

    from sqlcg.cli.commands import db as db_module

    source = inspect.getsource(db_module.db_reset)
    assert "get_backend(read_only=True)" not in source, (
        "db_reset must not call get_backend(read_only=True)"
    )


def test_gain_cmd_opens_readonly_backend() -> None:
    """gain (Section F parse-quality read) must open get_backend(read_only=True).

    gain_cmd's backend call is inside a conditional (only reached when the metrics DB
    exists), so it is pinned by source inspection rather than the parametrized invoke
    test above.  Reverting the flag at gain.py's read site reds this.
    """
    import inspect

    from sqlcg.cli.commands import gain as gain_module

    source = inspect.getsource(gain_module.gain_cmd)
    assert "get_backend(read_only=True)" in source, (
        "gain_cmd must open its parse-quality read with get_backend(read_only=True)"
    )
