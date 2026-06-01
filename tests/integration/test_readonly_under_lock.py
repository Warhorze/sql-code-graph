"""Tests for read-only KùzuDB connection under write lock (#28 read path).

Regression guard: with a writer holding the write lock, a read-only open +
read query must succeed and return expected rows.  Reverting the read_only=True
call-site flag makes it fail "Database is locked" — that is the primary #28
read-path invariant.

Also covers:
  - Call-site wiring: each CLI read command opens get_backend(read_only=True).
  - Missing-DB degradation: read-only open of a never-indexed DB shows the
    empty-DB hint, not a raw KùzuDB stacktrace.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.core.config import get_backend
from sqlcg.core.kuzu_backend import KuzuBackend

# ---------------------------------------------------------------------------
# Lock-contention regression test
# ---------------------------------------------------------------------------


def test_readonly_open_succeeds_while_writer_holds_lock() -> None:
    """With a writer open (holding the write lock), a read-only open must succeed.

    This is the primary #28 read-path regression: before the fix, every CLI
    read command called get_backend() without read_only=True, which attempted
    a second writer open and raised "Database is locked" in cross-process use.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")

        # Writer: open read-write, initialise schema, insert a Repo node.
        writer = KuzuBackend(db_path)
        writer.init_schema()
        writer.run_write("MERGE (r:Repo {path: '/test/repo', name: 'test'})", {})

        try:
            # Reader: open read-only while the writer is still open.
            reader = KuzuBackend(db_path, read_only=True)
            try:
                rows = reader.run_read("MATCH (r:Repo) RETURN r.path AS path", {})
                # Observable output: must return the row the writer inserted.
                assert len(rows) == 1, f"Expected 1 Repo row, got {len(rows)}"
                assert rows[0]["path"] == "/test/repo"
            finally:
                reader.close()
        finally:
            writer.close()


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
