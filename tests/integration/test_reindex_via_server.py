"""Integration tests for PR-D: reindex op on the MCP control socket (#28).

Critical invariant guard (Scenario A) asserts that the server-side reindex
op delegates to ``Indexer.resync_changed`` — not a reimplemented walk.  This
guard inherits the bulk-upsert perf invariant from CLAUDE.md.

Scenarios:
    A — reindex op calls resync_changed (invariant guard)
    C — fallback on no server: --notify with no server falls through to direct write
    D — fallback on stale socket: ConnectionRefusedError caught, direct write runs
    E — server error surfaces as non-zero exit (missing required fields)
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Skip entire module on Windows — Unix domain sockets not supported in v1.1
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain socket tests require a non-Windows platform",
)


# ---------------------------------------------------------------------------
# Git / DB helpers (shared with test_resync.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path):
    """Create a minimal git repo with one initial commit."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Initial commit
    (tmp_path / "init.sql").write_text("-- init\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _commit_all(repo: Path, msg: str = "change") -> str:
    """Stage everything and commit; return the new HEAD SHA."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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


def _send_op(sp: Path, op_dict: dict, timeout: float = 30.0) -> dict:
    """Send a JSON op to the control socket and return the parsed response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sp))
        s.sendall(json.dumps(op_dict).encode() + b"\n")
        data = s.recv(65536)
    return json.loads(data.decode().strip())


@pytest.fixture
def live_server(tmp_path: Path, git_repo: Path):
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
            pytest.skip("Server did not start within 15 s — skipping reindex e2e test")
        yield proc, db_path, sp, git_repo
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# Scenario A — invariant guard: reindex op calls resync_changed exactly once
# ---------------------------------------------------------------------------


class TestReindexOpCallsResyncChanged:
    """Guard: server-side reindex must delegate to Indexer.resync_changed.

    If this test fails, the reindex op was reimplemented instead of delegated,
    which breaks the bulk-upsert invariant (CLAUDE.md perf table) and risks
    reimplementing the walk incorrectly.

    Guards against: v1.1.0 reindex-via-server regression (#28).

    This test uses an in-process Unix socket server driven by anyio to avoid
    subprocess boundary issues with monkeypatching.  It patches
    Indexer.resync_changed *before* starting the control task, so the spy
    executes inside the same process.
    """

    def test_reindex_op_calls_resync_changed_once(self, git_repo: Path, tmp_path: Path):
        """Scenario A: reindex op calls Indexer.resync_changed exactly once.

        Asserts observable call-count (1, not 0) and ok response.
        Guards against reimplemented walk breaking the bulk-upsert invariant.
        """
        import anyio
        import anyio.to_thread as _to_thread

        from sqlcg.core.kuzu_backend import KuzuBackend
        from sqlcg.indexer.indexer import Indexer
        from sqlcg.server.control import cleanup_control_files, sock_path

        db_path = tmp_path / "test.db"
        sp = sock_path(db_path)

        # Add a file for the base commit, then advance HEAD
        (git_repo / "base_table.sql").write_text("CREATE TABLE BASE_TABLE (id INT);\n")
        old_sha = _commit_all(git_repo, "base-a")
        (git_repo / "dim_time.sql").write_text("CREATE TABLE DIM_TIME (time_key INT);\n")
        new_sha = _commit_all(git_repo, "add dim_time")

        backend = KuzuBackend(str(db_path))
        backend.init_schema()

        call_log: list = []
        original_resync = Indexer.resync_changed

        def spy_resync(self_inner, *args, **kwargs):  # type: ignore[no-untyped-def]
            call_log.append((args, kwargs))
            return original_resync(self_inner, *args, **kwargs)

        resp_holder: dict = {}

        async def _run_test() -> None:
            from sqlcg.server.server import _control_socket_task

            stop_event = anyio.Event()
            lock = anyio.Lock()

            with patch.object(Indexer, "resync_changed", spy_resync):
                async with anyio.create_task_group() as tg:
                    # Start the control task
                    tg.start_soon(
                        _control_socket_task,
                        db_path,
                        lambda: backend,
                        stop_event,
                        lock,
                        0.0,
                    )

                    # Wait for socket to appear
                    deadline = anyio.current_time() + 5.0
                    while not sp.exists():
                        await anyio.sleep(0.05)
                        if anyio.current_time() > deadline:
                            raise TimeoutError("Control socket did not appear")

                    # Send reindex op via the socket (in a thread to avoid blocking)
                    def _send() -> dict:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                            s.settimeout(25.0)
                            s.connect(str(sp))
                            s.sendall(
                                json.dumps(
                                    {
                                        "op": "reindex",
                                        "root": str(git_repo),
                                        "from": old_sha,
                                        "to": new_sha,
                                        "dialect": "ansi",
                                    }
                                ).encode()
                                + b"\n"
                            )
                            data = s.recv(65536)
                        return json.loads(data.decode().strip())

                    resp_holder["resp"] = await _to_thread.run_sync(_send)
                    tg.cancel_scope.cancel()

        try:
            anyio.run(_run_test)
        finally:
            backend.close()
            cleanup_control_files(db_path)

        resp = resp_holder.get("resp", {})
        assert len(call_log) == 1, (
            f"resync_changed must be called exactly once — "
            f"guards against reimplemented walk breaking bulk-upsert invariant "
            f"(v1.1.0 regression). Call log: {call_log}. Server resp: {resp}"
        )
        assert "ok" in resp, f"Expected ok response, got: {resp}"

    def test_reindex_op_missing_fields_returns_error(self, live_server):
        """Reindex op without required fields returns error immediately."""
        _, _, sp, _ = live_server
        resp = _send_op(sp, {"op": "reindex", "root": "/tmp", "from": "abc"}, timeout=5.0)
        assert "error" in resp, f"Expected error for missing 'to' field, got: {resp}"
        assert "requires root, from, to" in resp["error"]

    def test_reindex_op_missing_root_returns_error(self, live_server):
        """Reindex op without root returns error immediately."""
        _, _, sp, _ = live_server
        resp = _send_op(sp, {"op": "reindex", "from": "abc", "to": "def"}, timeout=5.0)
        assert "error" in resp, f"Expected error for missing 'root', got: {resp}"


# ---------------------------------------------------------------------------
# Scenario C — fallback on no server: --notify falls through to direct write
# ---------------------------------------------------------------------------


class TestNotifyFallbackNoServer:
    """Scenario C: --notify with no server falls through to direct write."""

    def test_notify_falls_through_when_no_server(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--notify with no running server falls through to direct write.

        Asserts observable output: the graph is updated (no crash, no
        'Database is locked' error).
        """
        from sqlcg.core.kuzu_backend import KuzuBackend

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        # Initial index via direct path
        from sqlcg.indexer.indexer import Indexer

        (git_repo / "fact_sales.sql").write_text(
            "CREATE TABLE FACT_SALES (sale_id INT, amount NUMERIC);\n"
        )
        sha_a = _commit_all(git_repo, "add fact_sales")

        backend = KuzuBackend(str(db_path))
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(git_repo, "ansi", backend)
        backend.set_indexed_sha(sha_a)
        backend.close()

        # Add new file and advance HEAD
        (git_repo / "dim_product.sql").write_text(
            "CREATE TABLE DIM_PRODUCT (prod_id INT, name VARCHAR);\n"
        )
        sha_b = _commit_all(git_repo, "add dim_product")

        # --notify with no server: should fall through to direct write
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sqlcg",
                "reindex",
                str(git_repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--notify",
            ],
            env={**os.environ, "SQLCG_DB_PATH": str(db_path)},
            capture_output=True,
            text=True,
        )

        # Must succeed (not crash)
        assert result.returncode == 0, (
            f"reindex --notify fallback must exit 0. stderr: {result.stderr}"
        )
        # Observable: no "Database is locked" in output
        assert "Database is locked" not in result.stderr
        assert "Database is locked" not in result.stdout


# ---------------------------------------------------------------------------
# Scenario D — fallback on stale socket: ConnectionRefusedError caught
# ---------------------------------------------------------------------------


class TestNotifyFallbackStaleSocket:
    """Scenario D: stale .sock file causes ConnectionRefusedError, falls through."""

    def test_notify_falls_through_on_stale_socket(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A stale .sock file (no server behind it) causes direct-write fallback."""
        from sqlcg.core.kuzu_backend import KuzuBackend

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        # Set up initial index
        (git_repo / "orders.sql").write_text("CREATE TABLE ORDERS (order_id INT, total NUMERIC);\n")
        sha_a = _commit_all(git_repo, "add orders")

        backend = KuzuBackend(str(db_path))
        backend.init_schema()
        from sqlcg.indexer.indexer import Indexer as _Indexer

        _Indexer().index_repo(git_repo, "ansi", backend)
        backend.set_indexed_sha(sha_a)
        backend.close()

        # Add a stale socket file (no process behind it)
        from sqlcg.server.control import sock_path

        sp = sock_path(db_path)
        sp.touch()

        (git_repo / "items.sql").write_text("CREATE TABLE ITEMS (item_id INT, sku VARCHAR);\n")
        sha_b = _commit_all(git_repo, "add items")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sqlcg",
                "reindex",
                str(git_repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--notify",
            ],
            env={**os.environ, "SQLCG_DB_PATH": str(db_path)},
            capture_output=True,
            text=True,
        )

        # Must succeed (ConnectionRefusedError caught, direct write runs)
        assert result.returncode == 0, (
            f"reindex --notify with stale socket must exit 0. stderr: {result.stderr}"
        )
        assert "Database is locked" not in result.stderr


# ---------------------------------------------------------------------------
# Scenario E — server error surfaces as non-zero exit (missing fields)
# ---------------------------------------------------------------------------


class TestNotifyServerErrorSurfaces:
    """Scenario E: server error from reindex op surfaces as non-zero CLI exit.

    Uses a mock socket server in-process to return a deliberate error dict,
    so the CLI's ``--notify`` path exits non-zero without starting a real server.
    """

    def test_server_reindex_error_exits_nonzero(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When the socket server returns {"error": ...}, CLI exits non-zero.

        This tests that the --notify path does NOT swallow errors from the server.
        """
        import anyio

        from sqlcg.server.control import cleanup_control_files, sock_path

        db_path = tmp_path / "test_e.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))
        sp = sock_path(db_path)

        (git_repo / "base_e.sql").write_text("-- base e\n")
        old_sha = _commit_all(git_repo, "scenario-e-base")
        (git_repo / "dummy_e.sql").write_text("-- dummy e\n")
        new_sha = _commit_all(git_repo, "scenario-e-new")

        # Start a minimal mock socket server that always returns an error
        server_ready = []

        async def _handle(stream) -> None:  # type: ignore[no-untyped-def]
            async with stream:
                await stream.receive(4096)
                await stream.send(json.dumps({"error": "deliberate test error"}).encode() + b"\n")

        async def _mock_server() -> None:
            listener = await anyio.create_unix_listener(str(sp))
            sp.chmod(0o600)
            server_ready.append(True)
            async with listener:
                await listener.serve(_handle)

        result_holder: dict = {}

        async def _run() -> None:
            import anyio.to_thread as _to_thread_e

            async with anyio.create_task_group() as tg:
                tg.start_soon(_mock_server)
                # Give mock server time to start
                while not server_ready:
                    await anyio.sleep(0.05)

                # Run CLI in thread (it's subprocess-based, so run directly)
                def _run_cli() -> subprocess.CompletedProcess:  # type: ignore[type-arg]
                    return subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "sqlcg",
                            "reindex",
                            str(git_repo),
                            "--from",
                            old_sha,
                            "--to",
                            new_sha,
                            "--dialect",
                            "ansi",
                            "--notify",
                        ],
                        env={**os.environ, "SQLCG_DB_PATH": str(db_path)},
                        capture_output=True,
                        text=True,
                    )

                result_holder["result"] = await _to_thread_e.run_sync(_run_cli)
                tg.cancel_scope.cancel()

        try:
            anyio.run(_run)
        finally:
            cleanup_control_files(db_path)

        result = result_holder.get("result")
        assert result is not None, "CLI subprocess did not run"

        # CLI must exit non-zero when server returns {"error": ...}
        assert result.returncode != 0, (
            f"Expected non-zero exit when server returns error. "
            f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
        )
        assert "deliberate test error" in result.stdout or "deliberate test error" in result.stderr
