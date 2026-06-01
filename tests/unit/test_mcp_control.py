"""Unit tests for the MCP server control channel (PR-C, #29).

Covers:
    Scenario A — path derivation honours SQLCG_DB_PATH, never hardcoded
    Scenario B — mcp_status with no server: {"running": false}
    Scenario C — mcp_status degraded: pid alive but no socket
    Scenario F — stale socket: ConnectionRefusedError falls through to {"running": false}
"""

import json
import os
from pathlib import Path

import pytest

from sqlcg.server.control import (
    cleanup_control_files,
    is_pid_alive,
    pid_path,
    read_pid,
    sock_path,
    write_pid,
)

# ---------------------------------------------------------------------------
# Scenario A — path derivation
# ---------------------------------------------------------------------------


class TestPathDerivation:
    """Paths derive from get_db_path() (SQLCG_DB_PATH), never hardcoded."""

    def test_sock_path_uses_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """sock_path() derives from SQLCG_DB_PATH env var."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))
        # Force re-evaluation of get_db_path
        result = sock_path()
        assert result == db.with_suffix(".sock"), (
            f"Expected {db.with_suffix('.sock')}, got {result}"
        )

    def test_pid_path_uses_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """pid_path() derives from SQLCG_DB_PATH env var."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))
        result = pid_path()
        assert result == db.with_suffix(".pid"), f"Expected {db.with_suffix('.pid')}, got {result}"

    def test_sock_path_explicit_db_path(self, tmp_path: Path):
        """sock_path(explicit_path) does not call get_db_path at all."""
        db = tmp_path / "custom.db"
        result = sock_path(db)
        assert result == tmp_path / "custom.sock"

    def test_pid_path_explicit_db_path(self, tmp_path: Path):
        """pid_path(explicit_path) does not call get_db_path at all."""
        db = tmp_path / "custom.db"
        result = pid_path(db)
        assert result == tmp_path / "custom.pid"

    def test_two_dbs_different_paths(self, tmp_path: Path):
        """Two different db paths produce distinct sock/pid paths (no collision)."""
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        assert sock_path(db_a) != sock_path(db_b)
        assert pid_path(db_a) != pid_path(db_b)


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


class TestPidFile:
    """write_pid / read_pid / cleanup round-trip."""

    def test_write_and_read_pid(self, tmp_path: Path):
        """write_pid writes a readable JSON record with pid, db_path, started_at."""
        db = tmp_path / "test.db"
        write_pid(db)
        rec = read_pid(db)
        assert rec is not None
        assert rec["pid"] == os.getpid()
        assert rec["db_path"] == str(db)
        assert "started_at" in rec

    def test_read_pid_missing_file(self, tmp_path: Path):
        """read_pid returns None when the pid file does not exist."""
        db = tmp_path / "nofile.db"
        result = read_pid(db)
        assert result is None

    def test_read_pid_corrupt_file(self, tmp_path: Path):
        """read_pid returns None on corrupt JSON."""
        db = tmp_path / "corrupt.db"
        pid_path(db).write_text("not json{{")
        result = read_pid(db)
        assert result is None

    def test_pid_file_mode_600(self, tmp_path: Path):
        """PID file is created with mode 0o600."""
        db = tmp_path / "test.db"
        write_pid(db)
        pp = pid_path(db)
        mode = pp.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_cleanup_removes_both_files(self, tmp_path: Path):
        """cleanup_control_files removes .pid and .sock silently."""
        db = tmp_path / "test.db"
        write_pid(db)
        sock_path(db).touch()
        cleanup_control_files(db)
        assert not pid_path(db).exists()
        assert not sock_path(db).exists()

    def test_cleanup_no_error_on_missing_files(self, tmp_path: Path):
        """cleanup_control_files does not raise when files are missing."""
        db = tmp_path / "nofiles.db"
        cleanup_control_files(db)  # must not raise


# ---------------------------------------------------------------------------
# is_pid_alive
# ---------------------------------------------------------------------------


class TestIsPidAlive:
    def test_current_process_is_alive(self):
        """os.getpid() is always alive."""
        assert is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self):
        """A PID that cannot exist (very large number) reports not alive."""
        # PID 999999999 is almost certainly not a running process
        assert is_pid_alive(999999999) is False


# ---------------------------------------------------------------------------
# Scenario B — mcp_status with no server
# ---------------------------------------------------------------------------


class TestMcpStatusNoServer:
    """mcp_status with no socket and no pid file: {"running": false}."""

    def test_status_no_server_prints_running_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """mcp_status prints {"running": false} when no socket and no pid file."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands.mcp import mcp_status

        mcp_status()
        # The console.print_json goes to stdout
        captured = capsys.readouterr()
        # Rich's print_json may include ANSI escapes; parse the JSON from the line
        output = captured.out.strip()
        # Strip ANSI escape sequences for assertion
        import re

        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        data = json.loads(clean)
        assert data["running"] is False


# ---------------------------------------------------------------------------
# Scenario C — mcp_status degraded: pid alive but socket unavailable
# ---------------------------------------------------------------------------


class TestMcpStatusDegraded:
    """When PID file has a live PID but no socket: degraded response."""

    def test_status_degraded_pid_alive_no_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """mcp_status returns degraded JSON when pid is alive but socket is absent."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        # Write a pid file pointing to the current process (which is alive)
        write_pid(db)

        from sqlcg.cli.commands.mcp import mcp_status

        mcp_status()
        captured = capsys.readouterr()
        import re

        clean = re.sub(r"\x1b\[[0-9;]*m", "", captured.out.strip())
        data = json.loads(clean)
        assert data["running"] is True
        assert data["degraded"] == "socket unavailable"
        assert data["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Scenario F — stale socket falls through cleanly
# ---------------------------------------------------------------------------


class TestMcpStatusStaleSocket:
    """mcp_status falls through to {"running": false} on a stale socket."""

    def test_stale_socket_no_pid_prints_running_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """A stale .sock file with no process behind it → {"running": false}."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        # Create a stale socket file that accepts no connections
        sp = sock_path(db)
        sp.touch()

        from sqlcg.cli.commands.mcp import mcp_status

        mcp_status()  # must not hang or raise
        captured = capsys.readouterr()
        import re

        clean = re.sub(r"\x1b\[[0-9;]*m", "", captured.out.strip())
        data = json.loads(clean)
        assert data["running"] is False

    def test_stale_socket_with_dead_pid_prints_running_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """A stale socket + dead-pid pid file → {"running": false}."""
        db = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        # Write pid file with a PID that is definitely not running
        pp = pid_path(db)
        pp.write_text(json.dumps({"pid": 999999999, "db_path": str(db), "started_at": 0.0}))

        # Create a stale socket file (no server behind it)
        sp = sock_path(db)
        sp.touch()

        from sqlcg.cli.commands.mcp import mcp_status

        mcp_status()
        captured = capsys.readouterr()
        import re

        clean = re.sub(r"\x1b\[[0-9;]*m", "", captured.out.strip())
        data = json.loads(clean)
        assert data["running"] is False


# ---------------------------------------------------------------------------
# mcp_stop with no server — prints "No server found to stop"
# ---------------------------------------------------------------------------


class TestMcpStopNoServer:
    def test_stop_no_server_prints_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """mcp_stop with no server found prints a non-crash message."""
        db = tmp_path / "noserver.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands.mcp import mcp_stop

        mcp_stop()  # must not raise
        captured = capsys.readouterr()
        assert "No server found to stop" in captured.out


# ---------------------------------------------------------------------------
# mcp_restart prints client-respawn caveat
# ---------------------------------------------------------------------------


class TestMcpRestart:
    def test_restart_prints_respawn_caveat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """mcp_restart prints the v1.1 client-respawn caveat (no true restart)."""
        db = tmp_path / "noserver.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db))

        from sqlcg.cli.commands.mcp import mcp_restart

        mcp_restart()
        captured = capsys.readouterr()
        # Must mention restarting via editor and reference v1.2 deferral
        assert "editor" in captured.out.lower() or "MCP" in captured.out
        assert "v1.2" in captured.out
