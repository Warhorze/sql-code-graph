"""Regression tests for Bug A — dialect 'auto' must be resolved BEFORE socket routing.

Live-DWH test (#29) observed 307/683 serialization failures because WriterRequest
carried literal "auto" instead of the resolved dialect (e.g. "snowflake").

These tests spin up a minimal fake Unix-socket server in a thread, call reindex_cmd
/ index_cmd with dialect="auto" and a .sqlcg.toml fixture, and assert that the
on-wire payload["dialect"] is the resolved value, never "auto".

Must be in tests/integration/ (not tests/unit/) because they require a live socket.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain socket tests require a non-Windows platform",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_toml_repo(tmp_path: Path, dialect: str) -> Path:
    """Create a minimal repo directory with a .sqlcg.toml declaring the given dialect."""
    repo = tmp_path / "repo"
    repo.mkdir()
    toml = repo / ".sqlcg.toml"
    toml.write_text(f'[sqlcg]\ndialect = "{dialect}"\n')
    return repo


def _fake_socket_server(sock_path: Path, received: list) -> threading.Thread:
    """Start a fake Unix-socket server that accepts one connection, reads one framed
    payload, appends it to ``received``, and exits.

    Returns the Thread (already started).
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(10.0)

    def _serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                f = conn.makefile("rb")
                length_line = f.readline()
                if not length_line:
                    return
                body_len = int(length_line.strip())
                body = f.read(body_len)
                received.append(json.loads(body))
                # Send a minimal error response so the CLI exits cleanly
                resp = json.dumps({"error": "test-server-done"}).encode()
                conn.sendall(f"{len(resp)}\n".encode() + resp)
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Bug A — reindex_cmd must resolve "auto" before routing
# ---------------------------------------------------------------------------


def test_BugA_reindex_dialect_auto_resolved_on_wire(tmp_path: Path) -> None:
    """reindex_cmd with --dialect auto sends the resolved dialect, never 'auto'.

    Before the fix: payload["dialect"] == "auto".
    After the fix: payload["dialect"] == "postgres" (from .sqlcg.toml).

    Observable assertion: the exact dialect string in the framed socket payload.
    """
    from typer.testing import CliRunner

    from sqlcg.cli.main import app
    from sqlcg.server.control import sock_path

    repo = _make_toml_repo(tmp_path, "postgres")
    db_path = tmp_path / "test.db"
    sp = sock_path(db_path)

    received: list = []
    t = _fake_socket_server(sp, received)

    try:
        runner = CliRunner()
        # The CLI reads SQLCG_DB_PATH to locate the socket
        result = runner.invoke(
            app,
            ["reindex", "--dialect", "auto", "--quiet", str(repo)],
            env={"SQLCG_DB_PATH": str(db_path)},
            catch_exceptions=False,
        )
        t.join(timeout=5.0)

        assert len(received) == 1, (
            f"Expected 1 framed payload received by fake server, got {len(received)}. "
            f"CLI exit={result.exit_code}, output={result.output!r}"
        )
        payload = received[0]
        assert payload.get("dialect") == "postgres", (
            f"Expected dialect='postgres' in routed payload but got {payload.get('dialect')!r}. "
            "Bug A: 'auto' was sent instead of the resolved dialect."
        )
        assert payload.get("dialect") != "auto", (
            "dialect='auto' must never appear on the wire — it is a client-side sentinel."
        )
    finally:
        sp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Bug A — index_cmd must resolve "auto" before routing
# ---------------------------------------------------------------------------


def test_BugA_index_dialect_auto_resolved_on_wire(tmp_path: Path) -> None:
    """index_cmd with --dialect auto sends the resolved dialect, never 'auto'.

    Before the fix: payload["dialect"] == "auto".
    After the fix: payload["dialect"] == "snowflake" (from .sqlcg.toml).

    Observable assertion: the exact dialect string in the framed socket payload.
    """
    from typer.testing import CliRunner

    from sqlcg.cli.main import app
    from sqlcg.server.control import sock_path

    repo = _make_toml_repo(tmp_path, "snowflake")
    db_path = tmp_path / "test.db"
    sp = sock_path(db_path)

    received: list = []
    t = _fake_socket_server(sp, received)

    try:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["index", "--dialect", "auto", "--quiet", str(repo)],
            env={"SQLCG_DB_PATH": str(db_path)},
            catch_exceptions=False,
        )
        t.join(timeout=5.0)

        assert len(received) == 1, (
            f"Expected 1 framed payload received by fake server, got {len(received)}. "
            f"CLI exit={result.exit_code}, output={result.output!r}"
        )
        payload = received[0]
        assert payload.get("dialect") == "snowflake", (
            f"Expected dialect='snowflake' in routed payload but got {payload.get('dialect')!r}. "
            "Bug A: 'auto' was sent instead of the resolved dialect."
        )
        assert payload.get("dialect") != "auto", (
            "dialect='auto' must never appear on the wire — it is a client-side sentinel."
        )
    finally:
        sp.unlink(missing_ok=True)
