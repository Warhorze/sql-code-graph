"""Client helper for routing CLI read commands through the live MCP server.

When a server is live on the target DB, CLI read commands route their
``run_read(cypher, params)`` calls over the Unix control socket instead of
opening the DB directly.  This avoids "Database is locked" errors when the
server holds KuzuDB's process-level write lock.

With no server running (``query_via_server`` returns ``None``), the fallback
opens the DB with ``get_backend(read_only=True)`` — zero-config small-repo
invariant preserved.

Framing protocol (v1.2.0):
  Request:  ``<decimal-byte-length>\\n<json-body>``
  Response: ``<decimal-byte-length>\\n<json-body>``
  Only the ``query`` op uses this framing; legacy ops (status/stop/reindex)
  keep their unframed ``{...}\\n`` protocol.

Client receive strategy: after sending the framed request, read the length
line with ``f.readline()`` (blocking, will not return a partial line) then
read exactly that many bytes with ``f.read(n)``.  This is the recv-exactly
pattern required by BLOCKER 2 — a single ``s.recv(65536)`` would silently
truncate large result sets.  Do NOT copy reindex.py's single-recv pattern
here.

Server-busy behaviour (v1.1.0 F1 parity):
  If the server is alive but the lock is held (timeout waiting for the
  response), raise ``typer.Exit`` — a plain ``Exception`` subclass, NOT
  ``SystemExit`` / ``BaseException``.  This ensures gain.py's
  ``except Exception: pass`` handler catches it and degrades gracefully
  (skips the parse-quality section) instead of crashing.  Other read
  commands let the ``typer.Exit`` propagate to a clean non-zero CLI exit.
  Do NOT fall back to a direct open on timeout — the server is alive and
  holds the lock, so falling back would reproduce the "Database is locked"
  error (mirrors the F1 fix in reindex.py:127–142).
"""

from __future__ import annotations

import json
import socket as _socket
import sys
from pathlib import Path

import typer

# Client-side socket timeout for the query control-socket path.
# Sized to cover the longest in-flight reindex (~89 s DWH resync_changed)
# with headroom.  This is a CLI transport constant, NOT a KuzuConfig value —
# same convention as _NOTIFY_SOCKET_TIMEOUT_S in reindex.py.
_QUERY_SOCKET_TIMEOUT_S = 300


def query_via_server(
    cypher: str,
    params: dict,
    db_path: Path | None = None,
    timeout_s: float = _QUERY_SOCKET_TIMEOUT_S,
) -> list[dict] | None:
    """Send a read query over the control socket.

    Uses length-prefixed framing (v1.2.0): ``<len>\\n<json-body>`` for both
    request and response.  Reads the response with ``makefile`` + ``readline``
    + ``read(n)`` — NOT a single ``recv`` — so arbitrarily large result sets
    are returned in full without truncation (BLOCKER 2).

    Args:
        cypher: Cypher query string (must be read-only; server enforces).
        params: Query parameter dict.
        db_path: Explicit database path. Defaults to ``get_db_path()``.
        timeout_s: Socket timeout in seconds.  On timeout the server is alive
            and holds the lock — raises ``typer.Exit``, does NOT fall back to
            a direct open (which would reproduce the lock error).

    Returns:
        Row list (list[dict]) on success.
        None when NO server is live (caller should fall back to direct open).

    Raises:
        typer.Exit: Server is alive but busy (timeout waiting for response).
            Exception-derived, NOT SystemExit — caught by gain.py's
            ``except Exception: pass`` so parse-quality section degrades
            gracefully (WARNING 3).
        typer.Exit: Server returned ``{"error": ...}`` response.
    """
    from sqlcg.server.control import sock_path

    if sys.platform == "win32":
        # No Unix domain socket on Windows — fall through to direct open.
        return None

    sp = sock_path(db_path)
    if not sp.exists():
        return None

    req = {"op": "query", "cypher": cypher, "params": params}
    req_bytes = json.dumps(req).encode()
    frame = f"{len(req_bytes)}\n".encode() + req_bytes

    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(str(sp))
            s.sendall(frame)

            # Recv-exactly via makefile:
            # - f.readline() reads the length line (``<int>\n``) — will not
            #   return a partial line because makefile buffers internally.
            # - f.read(n) reads exactly n bytes — accumulates until complete.
            # A single s.recv(65536) would silently truncate large bodies
            # (BLOCKER 2 guard: this is the recv-exactly implementation).
            f = s.makefile("rb")
            length_line = f.readline()
            if not length_line:
                return None  # server closed connection unexpectedly
            try:
                body_len = int(length_line.strip())
            except ValueError:
                # Server sent an unframed response — protocol mismatch.
                return None
            body = f.read(body_len)

    except TimeoutError:
        # Server is alive and holding the lock.  Do NOT fall back to a direct
        # open — that would hit the held lock and produce "Database is locked"
        # (mirrors v1.1.0 F1 fix in reindex.py:127–142).
        from rich.console import Console

        Console(stderr=True).print(
            f"[red]Server is busy (reindex in progress); timed out after "
            f"{timeout_s:.0f}s. The graph will update when it finishes — "
            "check 'sqlcg mcp status'.[/red]"
        )
        raise typer.Exit(1) from None
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        # Socket absent or refused — no live server; caller falls back to
        # direct open.
        return None

    try:
        resp = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None  # malformed response; treat as no-server

    if "error" in resp:
        from rich.console import Console

        Console(stderr=True).print(f"[red]Server query error: {resp['error']}[/red]")
        raise typer.Exit(1)

    return resp.get("rows", [])


def run_read_routed(
    cypher: str,
    params: dict,
    db_path: Path | None = None,
) -> list[dict]:
    """Route through a live server if present, else direct read-only open.

    This is the single seam every CLI read command calls instead of building
    its own backend.  Centralises the fallback semantics:

    - ``query_via_server`` returns a list → server is live, use rows.
    - ``query_via_server`` returns None → no server, open DB directly with
      ``get_backend(read_only=True)`` (BLOCKER 1 — must pass read_only=True
      or the fallback opens read-write and reproduces lock contention).
    - ``query_via_server`` raises ``typer.Exit`` → server busy/error; let it
      propagate (do NOT fall back — lock is held).

    Args:
        cypher: Cypher query string.
        params: Query parameter dict.
        db_path: Explicit database path. Defaults to ``get_db_path()``.

    Returns:
        Row list from the server or from a direct read-only DB open.

    Raises:
        typer.Exit: Server busy or server error (propagated from
            ``query_via_server``).
    """
    rows = query_via_server(cypher, params, db_path=db_path)
    if rows is not None:
        return rows

    # No server live — fall back to a direct read-only open.
    # read_only=True is required: without it the fallback opens read-write
    # and any concurrent writer will produce "Database is locked" (BLOCKER 1).
    from sqlcg.core.config import get_backend

    with get_backend(read_only=True) as backend:
        return backend.run_read(cypher, params)
