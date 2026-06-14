"""Regression tests for #63 — DuckDB single-file exclusive lock.

An empirical probe (see PR body / ``plan/measurements/pr3_coopen_probe.txt``)
established that DuckDB 1.x takes a **single-file exclusive lock**: while a live
R/W writer (the MCP server) holds the database, a second process CANNOT co-open
it read-only — both ``read_only=True`` and ``config={'access_mode':
'READ_ONLY'}`` raise ``IOException`` ("Conflicting lock is held").

Because a read-only co-open is impossible, the fix is the *graceful* path, not
``access_mode='READ_ONLY'``:

1. ``mcp start`` pre-checks for a live server (PID file + alive PID) and exits
   with a clear message instead of letting the DuckDB open throw an opaque
   ``IOException``.
2. ``run_read_routed`` surfaces a clear "server is running, retry" message when
   a live server is detected but its socket query came back empty, instead of
   silently attempting a doomed direct open.

These tests assert the observable behaviour (exit code + message), per the
"assert observable output, not just no-exception" house rule.
"""

import os
from pathlib import Path

import pytest
import typer

from sqlcg.server.control import write_pid


class TestMcpStartPreCheck:
    """``mcp start`` refuses to start a 2nd server when one is already live."""

    def test_start_exits_when_live_server_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """A live PID file makes ``mcp start`` exit (code 0) with a clear message,
        and it must NOT call into ``server.main`` (which would hit the lock)."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        # Write a PID file naming the current (alive) process — the live server.
        write_pid(db)

        from sqlcg.cli.commands import mcp as mcp_cmd

        # If the pre-check fails, control would reach server.main; make that
        # explode so the test fails loudly rather than blocking on a real start.
        def _boom(*_a, **_k):
            raise AssertionError("server.main must not be called when a server is live")

        monkeypatch.setattr("sqlcg.server.server.main", _boom)

        with pytest.raises(typer.Exit) as exc:
            mcp_cmd.mcp_start()

        assert exc.value.exit_code == 0
        out = capsys.readouterr().out
        assert "already running" in out
        assert str(os.getpid()) in out
        assert "mcp stop" in out

    def test_start_proceeds_when_pid_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A stale PID file (process gone) must NOT block a fresh start — the
        lock has been released, so ``server.main`` is reached."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands import mcp as mcp_cmd

        # Live-server detector reports a dead PID → start should proceed.
        monkeypatch.setattr(mcp_cmd, "_live_server_pid", lambda: None)

        called = {"main": False}

        def _fake_main(*_a, **_k):
            called["main"] = True

        monkeypatch.setattr("sqlcg.server.server.main", _fake_main)

        mcp_cmd.mcp_start()
        assert called["main"] is True

    def test_live_server_pid_returns_none_when_no_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No PID file → no live server detected."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands.mcp import _live_server_pid

        assert _live_server_pid() is None

    def test_live_server_pid_returns_pid_when_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """PID file naming a live process → that PID is returned."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))
        write_pid(db)

        from sqlcg.cli.commands.mcp import _live_server_pid

        assert _live_server_pid() == os.getpid()

    def test_live_server_pid_returns_none_when_pid_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """PID file naming a dead process → treated as no live server."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands import mcp as mcp_cmd

        monkeypatch.setattr(mcp_cmd, "read_pid", None, raising=False)
        # read_pid is imported inside _live_server_pid; patch is_pid_alive instead.
        monkeypatch.setattr("sqlcg.server.control.is_pid_alive", lambda _pid: False)
        write_pid(db)

        assert mcp_cmd._live_server_pid() is None


class TestReadRoutedLiveServerMessage:
    """``run_read_routed`` surfaces a clear message instead of a doomed open
    when a live server is detected but the socket query came back empty."""

    def test_routed_read_surfaces_message_when_server_live_but_socket_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))
        write_pid(db)  # live server (current process)

        from sqlcg.server import read_client

        # Simulate: socket query returns None (socket momentarily unavailable),
        # but the PID is alive. The direct open must NOT be attempted.
        monkeypatch.setattr(read_client, "query_via_server", lambda *a, **k: None)

        def _boom(*_a, **_k):
            raise AssertionError("must not attempt a direct open while a writer is live")

        monkeypatch.setattr("sqlcg.core.config.get_backend", _boom)

        with pytest.raises(typer.Exit) as exc:
            read_client.run_read_routed("MATCH (n) RETURN n", {}, db_path=db)

        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "server is running" in err.lower()
        assert str(os.getpid()) in err
