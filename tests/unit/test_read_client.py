"""Unit tests for the v1.2.0 read-proxy client (``server/read_client.py``).

Covers the client half of the server-routed read proxy (#28):

- ``query_via_server`` — None when no socket (fallback signal); framed
  round-trip (incl. > 64 KiB, the BLOCKER 2 truncation guard); server-busy
  timeout raises ``typer.Exit`` (Exception-derived, NOT SystemExit — WARNING 3);
  ``{"error": ...}`` response raises.
- ``run_read_routed`` — falls back to ``get_backend(read_only=True)`` (BLOCKER 1)
  when no server is live.

The framed test server is a plain threaded Unix-socket listener that speaks
the v1.2.0 framing protocol (``<decimal-len>\\n<json-body>``), so these tests
exercise the real client recv-exactly path without spawning a full MCP server.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from sqlcg.server import read_client

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain socket tests require a non-Windows platform",
)


# ---------------------------------------------------------------------------
# Framed test server — speaks the v1.2.0 framing protocol on a Unix socket.
# ---------------------------------------------------------------------------


class _FramedServer:
    """Threaded Unix-socket server for one request.

    Behaviour is selected by ``mode``:
      - "echo_rows": reply framed ``{"ok": true, "rows": <rows>}``.
      - "error":     reply framed ``{"error": <error>}``.
      - "hang":      accept, read the request, then never reply (timeout test).
    """

    def __init__(self, sock_file: Path, mode: str, rows=None, error: str = "") -> None:
        self._sp = sock_file
        self._mode = mode
        self._rows = rows if rows is not None else []
        self._error = error
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_file))
        self._srv.listen(1)
        self.received_request: dict | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _FramedServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            f = conn.makefile("rb")
            length_line = f.readline()
            if not length_line:
                return
            body_len = int(length_line.strip())
            body = f.read(body_len)
            self.received_request = json.loads(body)

            if self._mode == "hang":
                # Never reply — client must time out and raise (no fallback).
                threading.Event().wait(5.0)
                return

            if self._mode == "error":
                resp = {"error": self._error}
            else:  # echo_rows
                resp = {"ok": True, "rows": self._rows}

            resp_bytes = json.dumps(resp).encode()
            conn.sendall(f"{len(resp_bytes)}\n".encode() + resp_bytes)


# ---------------------------------------------------------------------------
# query_via_server — fallback signal
# ---------------------------------------------------------------------------


def test_query_via_server_none_when_no_socket(tmp_path: Path) -> None:
    """No socket file → None (caller falls back to a direct open)."""
    db_path = tmp_path / "graph.db"
    assert read_client.query_via_server("MATCH (n) RETURN n", {}, db_path=db_path) is None


# ---------------------------------------------------------------------------
# query_via_server — framed round-trip (incl. > 64 KiB / BLOCKER 2)
# ---------------------------------------------------------------------------


def test_query_via_server_framed_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    rows = [{"path": "ba.t1", "id": 1}, {"path": "ba.t2", "id": 2}]
    with _FramedServer(sp, mode="echo_rows", rows=rows) as srv:
        out = read_client.query_via_server(
            "MATCH (n:SqlTable) RETURN n.path AS path", {"x": 1}, db_path=db_path
        )
    assert out == rows
    # The request the server received carries the op/cypher/params verbatim.
    assert srv.received_request == {
        "op": "query",
        "cypher": "MATCH (n:SqlTable) RETURN n.path AS path",
        "params": {"x": 1},
    }


def test_query_via_server_large_body_not_truncated(tmp_path: Path) -> None:
    """A response body well over 64 KiB is read in full (BLOCKER 2 truncation guard).

    A single ``s.recv(65536)`` would stop at 64 KiB; the recv-exactly client
    must return every row, including a sentinel near the tail.
    """
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    # ~200 KiB of rows: each row ~200 bytes, 1000 rows.
    rows = [{"path": f"schema.table_{i:05d}", "blob": "x" * 180} for i in range(1000)]
    rows[-1]["path"] = "TAIL_SENTINEL"
    body_len = len(json.dumps({"ok": True, "rows": rows}).encode())
    assert body_len > 65536, "fixture must exceed a single recv(65536) buffer"

    with _FramedServer(sp, mode="echo_rows", rows=rows):
        out = read_client.query_via_server("MATCH (n) RETURN n", {}, db_path=db_path)

    assert out is not None
    assert len(out) == 1000
    assert out[-1]["path"] == "TAIL_SENTINEL"  # tail survived — no truncation


# ---------------------------------------------------------------------------
# query_via_server — server busy (timeout) raises, does NOT return None
# ---------------------------------------------------------------------------


def test_query_via_server_timeout_raises_typer_exit(tmp_path: Path) -> None:
    """Server alive but never replies → raise typer.Exit; do NOT fall back.

    Asserts the raised type is Exception-derived (catchable by gain.py's
    ``except Exception``) and NOT SystemExit/BaseException (WARNING 3).
    """
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    with _FramedServer(sp, mode="hang"):
        with pytest.raises(typer.Exit) as exc_info:
            read_client.query_via_server("MATCH (n) RETURN n", {}, db_path=db_path, timeout_s=0.5)
    # Exception-derived (gain.py's `except Exception` catches it), not SystemExit.
    assert isinstance(exc_info.value, Exception)
    assert not isinstance(exc_info.value, SystemExit)


def test_query_via_server_timeout_caught_by_bare_except_exception(tmp_path: Path) -> None:
    """Mirror gain.py:134 — a bare ``except Exception`` swallows the server-busy raise."""
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    caught = False
    with _FramedServer(sp, mode="hang"):
        try:
            read_client.query_via_server("MATCH (n) RETURN n", {}, db_path=db_path, timeout_s=0.5)
        except Exception:  # noqa: BLE001 — mirrors gain.py's graceful-degrade handler
            caught = True
    assert caught, "server-busy raise must be catchable by a bare `except Exception`"


def test_query_via_server_error_response_raises(tmp_path: Path) -> None:
    """A framed ``{"error": ...}`` response raises typer.Exit (e.g. read-only guard)."""
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    with _FramedServer(sp, mode="error", error="query op is read-only"):
        with pytest.raises(typer.Exit):
            read_client.query_via_server("CREATE (:X)", {}, db_path=db_path)


# ---------------------------------------------------------------------------
# run_read_routed — fallback path (BLOCKER 1: read_only=True)
# ---------------------------------------------------------------------------


def test_run_read_routed_falls_back_read_only_when_no_server(tmp_path: Path) -> None:
    """No socket → direct open via get_backend(read_only=True), rows returned.

    Asserts the fallback passes ``read_only=True`` (BLOCKER 1) — without it the
    fallback opens read-WRITE and reproduces the lock contention this fixes.
    """
    db_path = tmp_path / "graph.db"  # no socket → no server live
    expected = [{"path": "ba.x"}]

    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(return_value=expected)

    with patch("sqlcg.core.config.get_backend", return_value=backend) as mock_gb:
        out = read_client.run_read_routed("MATCH (n) RETURN n", {}, db_path=db_path)

    assert out == expected
    mock_gb.assert_called_once_with(read_only=True)
    backend.run_read.assert_called_once_with("MATCH (n) RETURN n", {})


def test_run_read_routed_uses_server_rows_when_live(tmp_path: Path) -> None:
    """Server live → rows come from the socket; no direct backend open."""
    db_path = tmp_path / "graph.db"
    sp = db_path.with_suffix(".sock")
    rows = [{"path": "ba.from_server"}]
    with _FramedServer(sp, mode="echo_rows", rows=rows):
        with patch("sqlcg.core.config.get_backend") as mock_gb:
            out = read_client.run_read_routed("MATCH (n) RETURN n", {}, db_path=db_path)
    assert out == rows
    mock_gb.assert_not_called()  # routed through the server, no direct open
