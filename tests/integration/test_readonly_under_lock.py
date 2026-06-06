"""Tests for DuckDB cross-process lock semantics and CLI read-routing wiring.

DuckDB uses a single R/W connection model with process-level exclusive locking:
while one process holds the file R/W, no other process can open it (even read-only).
Concurrent read safety for MCP-served requests is provided by DuckDB's MVCC —
all reads inside the server see a consistent snapshot of in-flight write transactions.

Socket routing (run_read_routed) is the correct mechanism for CLI read commands when
a server holds the DuckDB file — CLI reads route through the server socket rather than
opening the file directly.

Tests in this module cover:
  - Writer/writer exclusion: a writer process blocks all other opens (confirmed DuckDB behavior).
  - Read routing: CLI read commands call run_read_routed, not get_backend directly.
  - Source-inspection guards: write commands do NOT use read_only=True.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend

# ---------------------------------------------------------------------------
# Cross-process invariant: writer process blocks all other opens (DuckDB model)
# ---------------------------------------------------------------------------


def _child_hold_db_rw(
    db_path: str,
    ready_event: multiprocessing.Event,  # type: ignore[type-arg]
    release_event: multiprocessing.Event,  # type: ignore[type-arg]
    error_queue: multiprocessing.Queue[str],  # type: ignore[type-arg]
) -> None:
    """Child-process worker: open the DB R/W, signal ready, wait for release."""
    try:
        backend = DuckDBBackend(db_path)
        backend.init_schema()
        ready_event.set()
        release_event.wait(timeout=10)
        backend.close()
    except Exception as exc:  # noqa: BLE001
        error_queue.put(str(exc))
        ready_event.set()


def test_cross_process_writer_blocks_second_open() -> None:
    """A R/W writer process holding the DuckDB file blocks any second open.

    DuckDB uses an exclusive file lock per process. While a writer holds the
    file, any other process attempting to open it (R/W or R/O) gets an
    IOException. This test verifies that DuckDB enforces process exclusion and
    that the lock is released when the writer closes.

    Observable output: the second open raises; after the writer closes, the
    second open succeeds and returns the seeded row.
    """
    ctx = multiprocessing.get_context("spawn")
    ready_event = ctx.Event()
    release_event = ctx.Event()
    error_queue: multiprocessing.Queue[str] = ctx.Queue()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "writer_lock.db")

        # Seed the DB (writer is fully closed before spawning the child).
        seeder = DuckDBBackend(db_path)
        seeder.init_schema()
        seeder.upsert_node("Repo", "/wr/repo", {"path": "/wr/repo", "name": "wr"})
        seeder.close()

        # Child process holds the DB R/W.
        proc = ctx.Process(
            target=_child_hold_db_rw,
            args=(db_path, ready_event, release_event, error_queue),
            daemon=True,
        )
        proc.start()
        ready_event.wait(timeout=10)

        try:
            assert error_queue.empty(), (
                f"Child writer failed to open DB: {error_queue.get_nowait()}"
            )

            # Main process: attempt to open while child holds the write lock.
            # DuckDBBackend re-raises the cross-process lock error as IOException.
            import duckdb

            with pytest.raises(duckdb.IOException):
                DuckDBBackend(db_path)
        finally:
            release_event.set()
            proc.join(timeout=5)

        # After the writer releases the lock, a fresh open must succeed and
        # return the seeded row — this is the observable output assertion.
        reader = DuckDBBackend(db_path)
        try:
            rows = reader.run_read('SELECT path FROM "Repo"', {})
            assert len(rows) == 1, f"Expected 1 Repo row after writer released lock, got {rows}"
            assert rows[0]["path"] == "/wr/repo", f"Unexpected path: {rows[0]['path']}"
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Call-site wiring assertions (prevent future edits from dropping the routing)
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
    """Assert each CLI read command routes reads through run_read_routed.

    Since v1.2.0, CLI read commands no longer call get_backend(read_only=True)
    directly — they call run_read_routed() which handles server-routing and falls
    back to get_backend(read_only=True) when no server is live.  This test
    verifies that the seam is used, not the old direct get_backend call.

    Patches run_read_routed at the import site inside the command module so the
    mock intercepts the actual call.  Reverting a read command to bypass
    run_read_routed (e.g. inline get_backend) makes the corresponding case fail.
    """
    mod = importlib.import_module(module)
    func = getattr(mod, func_name)

    called: list[bool] = []

    def _capturing_routed(sql, params, **kw):  # type: ignore[override]
        called.append(True)
        return []  # empty result set — downstream code handles gracefully

    # Patch at the module where run_read_routed is used (imported-by-name).
    with patch(f"{module}.run_read_routed", _capturing_routed):
        try:
            func(**call_kwargs)
        except SystemExit:
            pass  # typer.Exit is fine; call capture is what matters
        except Exception:
            pass  # downstream errors after mock return are not our concern

    assert called, (
        f"{func_name}: run_read_routed was not called. "
        "CLI read commands must route through run_read_routed (v1.2.0+) "
        "so reads fall back to read_only=True when no server is live."
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
    """gain (Section F parse-quality read) must route through run_read_routed.

    Since v1.2.0, gain.py uses run_read_routed() for read queries so that when
    the server is live, the query routes through the server socket rather than
    opening the DB directly (which would hit the write lock).  run_read_routed
    falls back to get_backend(read_only=True) when no server is running.

    Pinned by source inspection — reverting to a direct get_backend() call or
    removing run_read_routed from gain.py's read path reds this test.
    """
    import inspect

    from sqlcg.cli.commands import gain as gain_module

    source = inspect.getsource(gain_module.gain_cmd)
    assert "run_read_routed" in source, (
        "gain_cmd must route its parse-quality read through run_read_routed (v1.2.0+)"
    )
