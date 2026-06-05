"""MCP server for SQL Code Graph.

Exposes FastMCP tools for lineage queries, pattern search, and indexing.
MCP protocol uses stdout (fd 1) for JSON-RPC message transport. This module
captures fd 1 as a raw binary buffer BEFORE any logging redirection so that
the captured buffer can be passed explicitly to stdio_server(). This ensures
JSON-RPC frames always go to fd 1 regardless of what sys.stdout points to
at call time.

Ordering invariant (must not change):
  1. os.dup(1) → _real_stdout_buffer        (first — before everything)
  2. from mcp.server import FastMCP          (module-level import)
  3. mcp = FastMCP("SQL Code Graph")         (module-level; tools.py registers here)
  4. main() calls _configure_mcp_logging()   (not at module scope)
"""

import os
import sys

# Capture the real fd 1 binary stream FIRST — before _configure_mcp_logging()
# (which replaces sys.stdout) AND before FastMCP("SQL Code Graph") construction.
# stdio_server() receives this explicitly so JSON-RPC frames go to fd 1
# regardless of what sys.stdout points to afterward.
# Guards against the v1.0.0/v1.0.1 regression where frames went to fd 2.
_real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)

from dotenv import load_dotenv  # noqa: E402
from mcp.server import FastMCP  # noqa: E402

from sqlcg.utils.logging import getLogger  # noqa: E402

logger = getLogger(__name__)

# Create FastMCP instance at module scope so tools.py can import and register with it.
# This is safe because _real_stdout_buffer has already captured fd 1 above.
mcp = FastMCP("SQL Code Graph")


def _configure_mcp_logging() -> None:
    """Redirect sys.stdout to sys.stderr and configure logging to stderr.

    sys.stdout is replaced with sys.stderr so that any stray print() call
    does not pollute fd 1 (reserved for MCP JSON-RPC frames).
    The real fd 1 binary stream is captured in _real_stdout_buffer at module
    top before this replacement and passed explicitly to stdio_server().

    Must be called inside main(), not at module scope, so that
    _real_stdout_buffer captures fd 1 before the redirect.
    """
    import logging

    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


async def _run_stdio_async_with_real_stdout() -> None:
    """Run the MCP server loop with JSON-RPC frames explicitly on fd 1.

    Bypasses FastMCP.run_stdio_async() (which uses sys.stdout at call time)
    and drives the server loop directly with the captured _real_stdout_buffer.
    """
    from io import TextIOWrapper

    import anyio
    from mcp.server.stdio import stdio_server

    stdout_text = TextIOWrapper(_real_stdout_buffer, encoding="utf-8", line_buffering=False)
    async with stdio_server(stdout=anyio.wrap_file(stdout_text)) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


async def _control_socket_task(
    db_path: "Path",
    backend_ref: "Callable[[], GraphBackend | None]",
    stop_event: "anyio.Event",
    backend_lock: "anyio.Lock",
    start_time: float,
    writer_queue: "WriterQueue",
) -> None:
    """Accept control connections on ``<db>.sock`` and dispatch ops.

    Supported ops:

    - ``{"op": "status"}`` → running state, pid, db_path, freshness, uptime,
      writer_queue block.  **Length-prefixed framing** (v1.3.0, B3): the
      response uses ``<decimal-byte-length>\\n<json-body>`` so large queue
      payloads are read in full by the recv-exactly client.
    - ``{"op": "stop"}`` → sends ``{"ok": true}`` then signals stop via
      *stop_event*.  Unframed (mcp_stop uses s.recv(128) — do NOT change).
    - ``{"op": "index", "root", "dialect", "wait"}`` → enqueues a full index
      onto *writer_queue* (rule 1 — supersedes all pending).  Supports
      ``wait=true`` (stream progress frames + terminal ``done:true``) and
      ``wait=false`` (immediate ``{ok, queued, position}``).
    - ``{"op": "reindex", "root", "from", "to", "dialect", "wait"}`` →
      enqueues an incremental resync (coalescing rules 2–3). ``from`` may be
      ``null``/omitted to resolve at drain start (W3).  Same ``wait`` semantics
      as ``index``.  The handler enqueues only — it never touches the backend
      (B1 invariant: only the drain task resolves a backend, under backend_lock).
    - ``{"op": "query", "cypher": ..., "params": ...}`` → executes a
      read-only Cypher query on the single backend connection, serialised
      behind *backend_lock*.  **Length-prefixed framing** (v1.2.0):
      ``<decimal-byte-length>\\n<json-body>`` on both request and response.

    Framing protocol:
      Requests: a bare decimal integer on the first line → framed.  Unframed
      JSON always starts with ``{``, so the sniff is unambiguous.
      Responses: framed (``<len>\\n<body>``) for ``query`` and ``status``;
      unframed for ``stop``/``reindex``/``index`` (unless ``wait=true`` which
      uses the multi-frame streaming protocol).

    Multi-frame streaming protocol (``index``/``reindex`` with ``wait=true``):
      The server sends a sequence of length-prefixed frames on the same
      connection.  Progress frames carry ``{done: false, files_done, files_total}``.
      The terminal frame carries ``{ok: true, done: true, summary: {...}}`` on
      success or ``{ok: false, done: true, error: ...}`` on failure (W7).
      The client reads frames in a loop and stops when it sees ``done == true``
      — it does NOT rely on EOF as the terminator.

    R2 (single connection): all backend operations go through ``backend_lock``
    so concurrent calls never touch the single Kuzu connection simultaneously.

    R8 teardown ordering: the caller must cancel this task BEFORE calling
    ``shutdown_backend()``.  This is guaranteed by the ``anyio.CancelScope``
    wrapping this task in ``_run_with_control`` — the scope is cancelled when
    the stdio loop exits, before ``main`` calls ``shutdown_backend()``.
    """
    import json
    import time
    from pathlib import Path as _Path

    import anyio
    import anyio.abc as _anyio_abc
    import anyio.to_thread as _to_thread
    from anyio.streams.buffered import BufferedByteReceiveStream

    from sqlcg.core.config import get_db_path as _get_db_path
    from sqlcg.server.writer import WriterRequest

    # Read-only keyword allow-list for the ``query`` op.  Only SELECT and WITH
    # (CTE preamble) are permitted — anything that starts with a write keyword
    # is rejected before execution.  This is a guard against accidental mutation,
    # not a security boundary (the socket is already 0o600 / owner-only).
    _QUERY_ALLOWED_KEYWORDS = frozenset({"SELECT", "WITH", "VALUES", "TABLE"})

    def _is_read_only_sql(sql: str) -> bool:
        """Return True iff the leading keyword is in the read-only allow-list."""
        import re

        m = re.match(r"\s*(?:--[^\n]*)?\s*(\w+)", sql, re.IGNORECASE)
        if not m:
            return False
        return m.group(1).upper() in _QUERY_ALLOWED_KEYWORDS

    sp = sock_path(db_path)

    listener = await anyio.create_unix_listener(str(sp))
    sp.chmod(0o600)

    async def _handle_connection(stream: _anyio_abc.SocketStream) -> None:
        async with stream:
            try:
                # Sniff for framed vs unframed request.
                # Framed (query op, v1.2.0): ``<decimal-len>\n<json-body>``
                # Unframed (legacy status/stop/reindex): JSON object starting with ``{``
                # The sniff is unambiguous: unframed JSON always starts with ``{``,
                # never a bare decimal digit.
                buf = BufferedByteReceiveStream(stream)
                first_line = await buf.receive_until(b"\n", max_bytes=64)

                try:
                    body_len = int(first_line.strip())
                    framed = True
                except ValueError:
                    framed = False

                if framed:
                    # Framed request: read exactly body_len bytes then parse.
                    raw_body = await buf.receive_exactly(body_len)
                    req = json.loads(raw_body)
                else:
                    # Unframed request (legacy ops): first_line IS the JSON
                    # (terminated by \n as sent by the client).
                    req = json.loads(first_line)

                op = req.get("op")

                if op == "status":
                    from sqlcg.core.freshness import compute_freshness

                    db = backend_ref()
                    indexed_sha: str | None = None
                    head_sha: str | None = None
                    stale: int | None = None

                    if db is not None:
                        try:
                            indexed_sha = db.get_indexed_sha()
                        except Exception:
                            indexed_sha = None

                        if indexed_sha is not None:
                            try:
                                rows = db.run_read(
                                    'SELECT path FROM "Repo" LIMIT 1',
                                    {},
                                )
                                if rows:
                                    f = compute_freshness(_Path(rows[0]["path"]), indexed_sha)
                                    head_sha = f.head_sha
                                    stale = f.stale_by_commits
                            except Exception:
                                pass

                    resp: dict = {
                        "running": True,
                        "pid": os.getpid(),
                        "db_path": str(db_path or _get_db_path()),
                        "indexed_sha": indexed_sha,
                        "head_sha": head_sha,
                        "stale_by_commits": stale,
                        "connected_clients": 1,  # stdio transport = 1 by design
                        "uptime": time.time() - start_time,
                        "writer_queue": writer_queue.coalesce_view(),
                    }
                    # status response is framed (B3, v1.3.0) — same framing as query
                    # so recv-exactly clients read it in full regardless of payload size.
                    resp_bytes = json.dumps(resp).encode()
                    await stream.send(f"{len(resp_bytes)}\n".encode() + resp_bytes)
                    return

                elif op == "stop":
                    resp = {"ok": True}
                    await stream.send(json.dumps(resp).encode() + b"\n")
                    # Trigger graceful stop: close stdin (triggers EOF in MCP loop)
                    # then cancel the task group.  Cleanup is still done in the
                    # main() finally block via cleanup_control_files().
                    stop_event.set()
                    return

                elif op == "index":
                    # Step 3.1 — enqueue a full index; never touches the backend here (B1).
                    root = req.get("root")
                    dialect = req.get("dialect")
                    wait = req.get("wait", False)
                    requested_by = req.get("requested_by", "cli")
                    if not root:
                        resp = {"error": "index op requires root"}
                        await stream.send(json.dumps(resp).encode() + b"\n")
                        return

                    writer_req = WriterRequest(
                        op="index",
                        root=root,
                        dialect=dialect,
                        from_sha=None,
                        to_sha=None,
                        requested_by=requested_by,
                    )

                    if wait:
                        # Attach-and-wait: register a memory channel then stream frames.
                        send_ch, recv_ch = anyio.create_memory_object_stream(max_buffer_size=64)
                        writer_req._waiters.append(send_ch)
                        position = await writer_queue.enqueue(writer_req)
                        # Send the queued acknowledgement frame first.
                        queued_frame = json.dumps(
                            {"ok": True, "done": False, "queued": True, "position": position}
                        ).encode()
                        await stream.send(f"{len(queued_frame)}\n".encode() + queued_frame)
                        # Stream progress frames until done:true terminal frame.
                        async with recv_ch:
                            async for terminal in recv_ch:
                                frame_bytes = json.dumps(terminal).encode()
                                await stream.send(f"{len(frame_bytes)}\n".encode() + frame_bytes)
                                if terminal.get("done"):
                                    break
                    else:
                        position = await writer_queue.enqueue(writer_req)
                        resp = {"ok": True, "queued": True, "position": position}
                        await stream.send(json.dumps(resp).encode() + b"\n")
                    return

                elif op == "reindex":
                    # Step 2.3 (B1) — enqueue; the drain is the only backend consumer.
                    # The handler NEVER calls backend_ref() (B1 invariant).
                    root = req.get("root")
                    from_sha = req.get("from")  # may be None (W3 — server resolves at drain)
                    to_sha = req.get("to")
                    dialect = req.get("dialect")
                    wait = req.get("wait", False)
                    requested_by = req.get("requested_by", "cli")
                    if not root:
                        resp = {"error": "reindex op requires root"}
                        await stream.send(json.dumps(resp).encode() + b"\n")
                        return

                    writer_req = WriterRequest(
                        op="reindex",
                        root=root,
                        dialect=dialect,
                        from_sha=from_sha,
                        to_sha=to_sha,
                        requested_by=requested_by,
                    )

                    if wait:
                        send_ch, recv_ch = anyio.create_memory_object_stream(max_buffer_size=64)
                        writer_req._waiters.append(send_ch)
                        position = await writer_queue.enqueue(writer_req)
                        queued_frame = json.dumps(
                            {"ok": True, "done": False, "queued": True, "position": position}
                        ).encode()
                        await stream.send(f"{len(queued_frame)}\n".encode() + queued_frame)
                        async with recv_ch:
                            async for terminal in recv_ch:
                                frame_bytes = json.dumps(terminal).encode()
                                await stream.send(f"{len(frame_bytes)}\n".encode() + frame_bytes)
                                if terminal.get("done"):
                                    break
                    else:
                        position = await writer_queue.enqueue(writer_req)
                        resp = {"ok": True, "queued": True, "position": position}
                        await stream.send(json.dumps(resp).encode() + b"\n")
                    return

                elif op == "query":
                    # Framed op (v1.2.0): read-only SQL query over the socket.
                    # Must only be called with a framed request (sniff above sets framed=True).
                    # Accept both "cypher" (legacy field name) and "sql" keys.
                    sql = req.get("sql") or req.get("cypher", "")
                    params = req.get("params") or {}
                    if not _is_read_only_sql(sql):
                        resp = {"error": "query op is read-only"}
                    else:
                        db = backend_ref()
                        if db is None:
                            resp = {"error": "backend not available"}
                        else:

                            def _do_query() -> list:
                                return db.run_read(sql, params)

                            async with backend_lock:
                                # R1: run off event-loop thread; R2: lock serialises
                                # reads and writes on the single DuckDB connection.
                                rows = await _to_thread.run_sync(_do_query)
                            resp = {"ok": True, "rows": rows}

                else:
                    resp = {"error": f"unknown op: {op!r}"}

                # Send response: framed for framed requests, unframed for legacy ops.
                resp_bytes = json.dumps(resp).encode()
                if framed:
                    await stream.send(f"{len(resp_bytes)}\n".encode() + resp_bytes)
                else:
                    await stream.send(resp_bytes + b"\n")

            except Exception as exc:
                try:
                    await stream.send(json.dumps({"error": str(exc)}).encode() + b"\n")
                except Exception:
                    pass

    async with listener:
        await listener.serve(_handle_connection)


async def _stop_watcher(
    stop_event: "anyio.Event",
    db_path: "Path",
    backend_lock: "anyio.Lock",
    shutdown_requested: "anyio.Event",
) -> None:
    """Wait for stop_event then perform graceful shutdown.

    B2 shutdown ordering:
      1. Set shutdown_requested so the drain loop exits cleanly and
         de_escalate_to_ro skips the RO reopen.
      2. Acquire backend_lock — waits until any active drain has fully
         de-escalated (so the in-flight RW write is committed, not torn).
      3. Call shutdown_backend() under the lock.
      4. Release backend_lock.
      5. Remove control files.
      6. Call os._exit(0).

    We use ``os._exit(0)`` because the MCP ``stdio_server`` blocks on a pipe
    read (``anyio.to_thread.run_sync`` with ``abandon_on_cancel=False``).
    We cannot interrupt it without killing the process.
    """
    import sqlcg.server.tools as _tools
    from sqlcg.server.control import cleanup_control_files

    await stop_event.wait()
    # B2(b): signal de_escalate_to_ro to skip the RO reopen.
    shutdown_requested.set()
    # B2(a): wait for any active drain to finish (acquires backend_lock).
    async with backend_lock:
        try:
            _tools.shutdown_backend()
        except Exception:
            pass
    try:
        cleanup_control_files(db_path)
    except Exception:
        pass
    os._exit(0)


async def _sigterm_watcher(
    stop_event: "anyio.Event",
) -> None:
    """Watch for SIGTERM and trigger the same graceful stop as the socket stop op.

    Fires ``stop_event`` which causes ``_stop_watcher`` to shut down cleanly.
    Not started on Windows (no Unix signals).
    """
    import signal

    import anyio

    with anyio.open_signal_receiver(signal.SIGTERM) as signals:
        async for _sig in signals:
            stop_event.set()
            return


async def _run_with_control(db_path: "Path", start_time: float) -> None:
    """Run the stdio MCP loop and the control-socket task in a shared TaskGroup.

    Stop mechanism (B2 teardown ordering):
    - Control socket ``stop`` op → ``stop_event.set()`` → ``_stop_watcher``
      sets shutdown_requested, acquires backend_lock (waits for active drain),
      shuts down backend, removes control files, calls ``os._exit(0)``.
    - External SIGTERM → ``_sigterm_watcher`` → same path via ``stop_event``.
    - Normal EOF on stdin (editor closes connection) → stdio loop returns →
      ``tg.cancel_scope.cancel()`` → tasks cancelled → ``main()`` finally
      block does cleanup.

    ``os._exit(0)`` is used for the stop-op path because the MCP
    ``stdio_server`` blocks on a pipe read (``anyio.to_thread.run_sync``
    with ``abandon_on_cancel=False``).  We cannot interrupt it without
    killing the process; ``_stop_watcher`` does cleanup first.

    ``backend_lock`` is created once here and passed into both
    ``_control_socket_task`` and the ``drain_loop`` task so that:
    - concurrent control ops (reindex, query) are serialised (R2), and
    - _stop_watcher can acquire the lock to wait for an active drain (B2).
    """
    import anyio

    import sqlcg.server.tools as _tools
    from sqlcg.server.writer import WriterQueue, drain_loop

    stop_event = anyio.Event()
    shutdown_requested = anyio.Event()
    backend_lock = anyio.Lock()  # R2 + B2: serialise all backend ops

    # Inject metrics into the queue so coalesce/drain events are persisted.
    writer_queue = WriterQueue(metrics=_tools._metrics)

    db_path_str = str(db_path)

    async with anyio.create_task_group() as tg:
        if sys.platform != "win32":
            # Drain task: consumes WriterQueue; sole backend consumer (B1).
            tg.start_soon(
                drain_loop,
                writer_queue,
                db_path_str,
                backend_lock,
                shutdown_requested,
            )
            # Spawn control socket alongside the stdio loop.
            tg.start_soon(
                _control_socket_task,
                db_path,
                lambda: _tools._backend,
                stop_event,
                backend_lock,
                start_time,
                writer_queue,
            )
            # Watch stop_event; shuts down and calls os._exit(0).
            tg.start_soon(_stop_watcher, stop_event, db_path, backend_lock, shutdown_requested)
            # Watch for SIGTERM; fires stop_event for same clean path.
            tg.start_soon(_sigterm_watcher, stop_event)

        # stdio loop — when it returns (EOF/error), cancel remaining tasks.
        await _run_stdio_async_with_real_stdout()
        tg.cancel_scope.cancel()


def main(db_path: str | None = None) -> None:
    """Start the MCP server.

    Args:
        db_path: Path to KùzuDB database. If None, uses SQLCG_DB_PATH env var
                or ~/.sqlcg/graph.db (via get_db_path in tools module).
    """
    import time
    from pathlib import Path

    import anyio

    from sqlcg.core.config import get_db_path
    from sqlcg.server.control import cleanup_control_files, write_pid

    # Must be first — redirects sys.stdout → sys.stderr so stray prints don't
    # corrupt fd 1. _real_stdout_buffer was already captured at module top.
    _configure_mcp_logging()

    load_dotenv()

    # Import tools module to trigger tool registration via @mcp.tool() decorators
    import sqlcg.server.tools

    # Initialize the backend singleton used by all tools
    sqlcg.server.tools.init_backend(db_path)

    _start_time = time.time()
    _db_path_obj = Path(db_path) if db_path else get_db_path()

    # Write PID file so CLI clients can discover and probe the server.
    write_pid(_db_path_obj)

    try:
        anyio.run(_run_with_control, _db_path_obj, _start_time)
    finally:
        # R8: control-socket task is cancelled inside _run_with_control before
        # this finally block runs, so no stop/reindex op can arrive on a closed
        # connection.
        sqlcg.server.tools.shutdown_backend()
        cleanup_control_files(_db_path_obj)


# ---------------------------------------------------------------------------
# Late imports pulled up to module scope for _control_socket_task
# ---------------------------------------------------------------------------
# These are referenced in the type annotations in _control_socket_task.
# We use TYPE_CHECKING guard to keep them from executing at import time.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import anyio

    from sqlcg.core.graph_db import GraphBackend
    from sqlcg.server.writer import WriterQueue

from sqlcg.server.control import sock_path  # noqa: E402  (used in _control_socket_task)
