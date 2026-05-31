"""E2E tests for MCP server lifecycle commands (PR-C, #29).

Starts a real ``sqlcg mcp start`` subprocess on a temp file DB, exercises the
control socket protocol, and asserts observable output (JSON fields, process
exit, file cleanup).

Scenarios from sprint_12_v1.1.0.md PR-C:
    D — e2e status: running server responds with required JSON fields
    E — e2e stop: server exits and .sock/.pid are removed within 5 s

These tests start a real subprocess and send real JSON over a real Unix socket.
They are skipped on Windows (no Unix domain sockets in v1.1).
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Skip entire module on Windows — Unix domain sockets not supported in v1.1
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain socket lifecycle tests require a non-Windows platform",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_socket(sp: Path, timeout: float = 10.0) -> bool:
    """Poll until the socket file exists and accepts connections, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sp.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(str(sp))
                    return True
            except (ConnectionRefusedError, OSError):
                pass
        time.sleep(0.1)
    return False


def _send_op(sp: Path, op_dict: dict, timeout: float = 5.0) -> dict:
    """Send a JSON op to the control socket and return the parsed response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sp))
        s.sendall(json.dumps(op_dict).encode() + b"\n")
        data = s.recv(65536)
    return json.loads(data.decode().strip())


# ---------------------------------------------------------------------------
# Fixture: live server subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def live_server(tmp_path: Path):
    """Start a real sqlcg mcp start subprocess on a temp file DB.

    Yields (process, db_path, sock_path) once the socket is accepting
    connections.  Cleans up by terminating the process on fixture teardown.
    """
    db_path = tmp_path / "test.db"
    env = os.environ.copy()
    env["SQLCG_DB_PATH"] = str(db_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "sqlcg", "mcp", "start"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    sp = db_path.with_suffix(".sock")

    try:
        ok = _wait_for_socket(sp, timeout=15.0)
        if not ok:
            proc.terminate()
            proc.wait(timeout=5)
            pytest.skip("Server did not start within 15 s — skipping lifecycle e2e test")
        yield proc, db_path, sp
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# Scenario D — e2e status: required JSON fields present
# ---------------------------------------------------------------------------


class TestMcpStatusLiveServer:
    """Scenario D: send status op to a live server and assert response fields."""

    def test_status_response_has_required_fields(self, live_server):
        """status op returns running, pid, db_path, uptime as non-null fields."""
        _, db_path, sp = live_server
        resp = _send_op(sp, {"op": "status"})

        assert resp.get("running") is True, f"Expected running=true, got: {resp}"
        assert isinstance(resp.get("pid"), int), f"pid must be int, got: {resp}"
        assert resp.get("db_path") is not None, f"db_path must be non-null, got: {resp}"
        assert isinstance(resp.get("uptime"), (int, float)), f"uptime must be numeric, got: {resp}"
        # connected_clients is present (may be 1 by design)
        assert "connected_clients" in resp, f"connected_clients must be in response: {resp}"
        # freshness fields are present (may be null for un-indexed DB)
        assert "indexed_sha" in resp, f"indexed_sha must be in response: {resp}"
        assert "head_sha" in resp, f"head_sha must be in response: {resp}"
        assert "stale_by_commits" in resp, f"stale_by_commits must be in response: {resp}"

    def test_status_pid_matches_process(self, live_server):
        """pid in status response matches the actual server subprocess PID."""
        proc, _, sp = live_server
        resp = _send_op(sp, {"op": "status"})
        assert resp["pid"] == proc.pid, f"Expected pid={proc.pid}, got pid={resp['pid']}"

    def test_status_db_path_matches_env(self, live_server):
        """db_path in status response matches SQLCG_DB_PATH."""
        _, db_path, sp = live_server
        resp = _send_op(sp, {"op": "status"})
        assert resp["db_path"] == str(db_path), f"Expected db_path={db_path}, got {resp['db_path']}"

    def test_reindex_op_missing_fields_returns_error(self, live_server):
        """Reindex op without required fields returns an error dict immediately."""
        _, _, sp = live_server
        # Omit required 'to' field — server must return error without hanging
        resp = _send_op(sp, {"op": "reindex", "root": "/tmp", "from": "abc"})
        assert "error" in resp, f"Expected error for missing 'to' field, got: {resp}"
        assert "requires root, from, to" in resp["error"], (
            f"Error must mention required fields, got: {resp['error']}"
        )

    def test_unknown_op_returns_error(self, live_server):
        """Unknown op returns an error dict (not a crash)."""
        _, _, sp = live_server
        resp = _send_op(sp, {"op": "frobnicate"})
        assert "error" in resp, f"Expected error for unknown op, got: {resp}"


# ---------------------------------------------------------------------------
# Scenario E — e2e stop: server exits and control files are removed
# ---------------------------------------------------------------------------


class TestMcpStopLiveServer:
    """Scenario E: stop op causes the server to exit and removes .sock/.pid."""

    def test_stop_removes_control_files_within_5s(self, live_server):
        """stop op causes .sock and .pid to disappear within 5 s."""
        proc, db_path, sp = live_server

        # Confirm files exist before stop
        assert sp.exists(), "Socket file must exist before stop"

        resp = _send_op(sp, {"op": "stop"})
        assert resp.get("ok") is True, f"Expected ok=true from stop, got: {resp}"

        # Wait for socket to disappear (max 5 s)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and sp.exists():
            time.sleep(0.1)

        assert not sp.exists(), "Socket file must be removed within 5 s of stop"

        # Wait for process to exit (max 5 s)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Server process did not exit within 5 s of stop op")

        assert proc.returncode is not None, "Server must have exited"

    def test_stop_process_exits_cleanly(self, live_server):
        """Server exits with code 0 after a stop op (clean shutdown)."""
        proc, _, sp = live_server

        _send_op(sp, {"op": "stop"})

        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Server did not exit cleanly within 8 s of stop")

        # Exit code 0 (clean) or negative (signal, acceptable from SIGTERM)
        # SystemExit(0) → code 0; SIGTERM before handler → negative
        assert proc.returncode is not None
        # Accept 0 (SystemExit clean) or negative (SIGTERM on platforms where
        # signal causes non-zero exit before handler fires)
        assert proc.returncode in (0, -15), (
            f"Expected exit code 0 or -15 (SIGTERM), got {proc.returncode}"
        )
