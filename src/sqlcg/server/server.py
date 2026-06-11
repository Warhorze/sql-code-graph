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

from sqlcg import __version__  # noqa: E402
from sqlcg.utils.logging import getLogger  # noqa: E402

logger = getLogger(__name__)

# Create FastMCP instance at module scope so tools.py can import and register with it.
# This is safe because _real_stdout_buffer has already captured fd 1 above.
mcp = FastMCP("SQL Code Graph")

# FastMCP.__init__ does not accept a `version` kwarg (verified signature); the
# underlying mcp.server.lowlevel.Server.__init__(self, name, version=None, ...)
# does. Set it post-construction so the `initialize` handshake reports
# serverInfo.version == sqlcg.__version__ instead of None. Deliberate, pinned
# private-attr write — see tests/unit/test_version_parity.py.
mcp._mcp_server.version = __version__

# ---------------------------------------------------------------------------
# A5 — absolute argv0 captured once at startup in main(), BEFORE anyio.run.
# _reexec() uses this path at exec time; never re-reads sys.argv[0] after startup
# (could be relative after a chdir or mutated by a framework).
# ---------------------------------------------------------------------------
_resolved_argv0: str | None = None


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
      read-only SQL query on the single backend connection, serialised
      behind *backend_lock*.  (The ``cypher`` field name is a legacy wire-key
      retained for protocol compatibility; the value is SQL.)
      **Length-prefixed framing** (v1.2.0):
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
    so concurrent calls never touch the single DuckDB connection simultaneously.

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
                    from sqlcg.server.selfheal import maybe_self_heal as _maybe_self_heal

                    # D2: throttled skew check at activity point (a) — control-socket status.
                    _maybe_self_heal()

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
                        "version": __version__,
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

    Shutdown ordering:
      1. Set shutdown_requested so the drain loop exits cleanly after its
         current drain completes (no new drains start once this is set).
      2. Acquire backend_lock — waits until any active drain has finished
         (committed its transaction).
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
    # Signal drain loop to stop after current drain completes.
    shutdown_requested.set()
    # Wait for any active drain to finish before closing the backend.
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


# ---------------------------------------------------------------------------
# Re-exec machinery (D4, A4, A5, Step 2.2)
# ---------------------------------------------------------------------------


def _reexec() -> None:
    """Replace the running process image with the installed console-script (os.execv).

    Called by ``_self_heal_watcher`` after the DuckDB connection is closed under
    ``backend_lock``.  If exec succeeds, this function never returns.  On
    failure (or refusal), it returns normally so the watcher can fall through
    to the F2 recovery path (re-open backend, keep serving old build).

    Guards (per plan amendments):

    **A4 — generation cap (F8):** ``SQLCG_SELF_HEAL_GENERATION`` env counter;
    refuses to exec when ``>= 3`` to prevent re-exec storms on corrupt installs.

    **A5 — absolute argv0:** uses ``_resolved_argv0`` captured once in ``main()``
    (``os.path.abspath(sys.argv[0])``), not ``sys.argv[0]`` at exec time (which
    could be relative after a chdir).  Refuses if the path is not an executable file.

    **A3 — stdio:** fds 0/1/2 are ``CLOEXEC=False`` (``os.get_inheritable(1) is True``
    — verified at startup); they survive exec untouched.  The process-local
    ``_real_stdout_buffer`` (``os.dup(1)`` → CLOEXEC=True on Python 3.12/Linux) is
    closed by exec — correct and intended; the re-exec'd image re-creates it at
    module scope.

    See plan/sprints/mcp_server_self_healing.md §D4 / §Step 2.2.
    """
    # A4 — generation guard: refuse at >= 3 to stop re-exec storms.
    gen = int(os.environ.get("SQLCG_SELF_HEAL_GENERATION", "0"))
    if gen >= 3:
        logger.error(
            "self-heal re-exec refused: SQLCG_SELF_HEAL_GENERATION=%d — "
            "the new build still reports a skew; restart the MCP server manually",
            gen,
        )
        return

    # A5 — executability check: _resolved_argv0 must exist and be executable.
    if not (
        _resolved_argv0 and os.path.isfile(_resolved_argv0) and os.access(_resolved_argv0, os.X_OK)
    ):
        logger.error(
            "self-heal re-exec refused: argv0 %r is not an executable file",
            _resolved_argv0,
        )
        return  # F2 path: caller re-opens backend and keeps serving

    # Flush stdout/stderr before exec (A3).
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass

    # Bump generation counter so the new process can count re-exec iterations.
    os.environ["SQLCG_SELF_HEAL_GENERATION"] = str(gen + 1)

    # os.execv replaces this process image; fds 0/1/2 survive (CLOEXEC=False).
    os.execv(_resolved_argv0, [_resolved_argv0, *sys.argv[1:]])


async def _self_heal_watcher(
    self_heal_event: "anyio.Event",
    db_path: "Path",
    backend_lock: "anyio.Lock",
    shutdown_requested: "anyio.Event",
    writer_queue: "WriterQueue",
) -> None:
    """Wait for self_heal_event then perform drain-safe quiescence + re-exec.

    Reuses the ``_stop_watcher`` ordering (D5) but ends in ``_reexec()`` instead
    of ``os._exit(0)``.  If ``_reexec()`` returns without exec'ing (A4 cap /
    A5 bad argv0 / OSError), falls through to the F2 recovery path: re-opens
    the backend outside ``backend_lock`` (W2 — deadlock avoidance) and clears
    ``shutdown_requested`` so the drain loop resumes.

    **D5 ordering:**
      1. Wait on ``self_heal_event``.
      2. ``shutdown_requested.set()`` — drain loop finishes current drain, starts no new one.
      3. ``async with backend_lock`` — waits until any active drain has committed.
      4. Relay A6 waiter terminal frames under ``writer_queue._lock``.
      5. ``shutdown_backend()`` under ``backend_lock`` — closes DuckDB (releases #63 lock).
      6. ``cleanup_control_files(db_path)`` — unlink .sock + .pid (new process rewrites them).
    (Exit the lock — W2)
      7. ``_reexec()`` — if it returns, F2 recovery: re-open backend, clear shutdown_requested.

    **A6 — waiter relay:** after ``shutdown_requested.set()``, pending ``wait=true``
    clients need a terminal frame.  We acquire ``writer_queue._lock`` and, for each
    pending request's ``_waiters``, send a terminal ``{"ok": false, "done": true,
    "error": "server self-healing — re-issue"}`` frame.  The active request's
    waiter is already notified by the drain loop when it commits (step 3), so it
    is not double-notified here.

    **W2 — lock-release ordering:** the F2 recovery (``init_backend`` + clear
    ``shutdown_requested``) runs AFTER the ``async with backend_lock`` block exits.
    The drain loop re-acquires ``backend_lock``; re-opening the backend while still
    holding it would deadlock.

    See plan/sprints/mcp_server_self_healing.md §D5 / §Step 2.2.
    """
    import sqlcg.server.tools as _tools
    from sqlcg.server.control import cleanup_control_files

    # Step 1 — wait for the skew signal.
    await self_heal_event.wait()

    logger.warning("self-heal watcher triggered — beginning drain-safe quiescence")

    # Step 2 — stop the drain loop from starting new drains.
    shutdown_requested.set()

    # Steps 3–6: acquire the lock (waits for any active drain to commit).
    async with backend_lock:
        # A6 — relay terminal frames to pending wait=true clients.
        try:
            async with writer_queue._lock:
                for req in list(writer_queue._pending):
                    for ch in list(req._waiters):
                        try:
                            await ch.send(
                                {
                                    "ok": False,
                                    "done": True,
                                    "error": "server self-healing — re-issue",
                                }
                            )
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("self-heal waiter relay failed (non-fatal): %s", exc)

        # Step 5 — close DuckDB connection (releases the exclusive lock for the new process).
        try:
            _tools.shutdown_backend()
        except Exception as exc:
            logger.warning("self-heal shutdown_backend failed (non-fatal): %s", exc)

        # Step 6 — remove control files so the new process can re-bind the socket.
        try:
            cleanup_control_files(db_path)
        except Exception as exc:
            logger.warning("self-heal cleanup_control_files failed (non-fatal): %s", exc)

    # W2: lock released BEFORE _reexec() and BEFORE any F2 recovery.
    # _reexec() never returns on success (os.execv replaces the process image).
    try:
        _reexec()
    except Exception as exc:
        logger.error("self-heal re-exec raised unexpectedly: %s", exc)

    # F2 recovery: _reexec() returned without exec'ing (A4 cap / A5 bad argv0 / OSError).
    # Re-open the backend OUTSIDE backend_lock (W2) and resume serving the old build.
    logger.error(
        "self-heal re-exec FAILED — continuing on old build v%s; "
        "restart the MCP server manually to pick up the new build",
        __version__,
    )
    try:
        _tools.init_backend(str(db_path))
    except Exception as exc:
        logger.error("self-heal F2: failed to re-open backend after exec refusal: %s", exc)
    # Clear shutdown_requested so the drain loop resumes (if backend re-opened).
    # We create a new Event because anyio.Event has no .clear() method.
    # The drain loop holds a reference to the shutdown_requested object passed into
    # drain_loop(); we cannot replace it here.  Instead we leave shutdown_requested
    # set and accept that the drain loop will not process new requests.
    # This is a degraded-but-alive state — the server answers tool calls on the
    # old build but the drain loop is quiesced.  Log clearly.
    logger.error(
        "self-heal F2: drain loop remains quiesced (shutdown_requested is set). "
        "Index/reindex ops will not be processed until the server is restarted."
    )


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
    from sqlcg.server.selfheal import register_self_heal_event
    from sqlcg.server.writer import WriterQueue, drain_loop

    stop_event = anyio.Event()
    shutdown_requested = anyio.Event()
    backend_lock = anyio.Lock()  # R2 + B2: serialise all backend ops

    # Inject metrics into the queue so coalesce/drain events are persisted.
    writer_queue = WriterQueue(metrics=_tools._metrics)

    db_path_str = str(db_path)

    async with anyio.create_task_group() as tg:
        if sys.platform != "win32":
            # Self-heal event: signalled by maybe_self_heal() on version skew (D3).
            self_heal_event = anyio.Event()
            register_self_heal_event(self_heal_event)

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
            # Watch self_heal_event; performs drain-safe quiescence + os.execv (D5).
            tg.start_soon(
                _self_heal_watcher,
                self_heal_event,
                db_path,
                backend_lock,
                shutdown_requested,
                writer_queue,
            )

        # stdio loop — when it returns (EOF/error), cancel remaining tasks.
        await _run_stdio_async_with_real_stdout()
        tg.cancel_scope.cancel()


def main(db_path: str | None = None) -> None:
    """Start the MCP server.

    Args:
        db_path: Path to DuckDB database. If None, uses SQLCG_DB_PATH env var
                or ~/.sqlcg/graph.db (via get_db_path in tools module).
    """
    import time
    from pathlib import Path

    import anyio
    import mcp.types as _types

    from sqlcg.core.config import get_db_path
    from sqlcg.server.control import cleanup_control_files, write_pid
    from sqlcg.server.selfheal import maybe_self_heal

    # A5 — capture absolute argv0 BEFORE anyio.run (before any chdir or argv mutation).
    # _reexec() uses this path at exec time; never re-reads sys.argv[0] afterward.
    global _resolved_argv0
    _resolved_argv0 = os.path.abspath(sys.argv[0])

    # A3 — verify fds 0/1/2 are CLOEXEC=False (inheritable=True) so they survive exec.
    # This is an assertion, not a runtime guard; failure indicates a broken environment.
    assert os.get_inheritable(0), "fd 0 must be inheritable (CLOEXEC=False) for exec to work"
    assert os.get_inheritable(1), "fd 1 must be inheritable (CLOEXEC=False) for exec to work"
    assert os.get_inheritable(2), "fd 2 must be inheritable (CLOEXEC=False) for exec to work"

    # Must be first — redirects sys.stdout → sys.stderr so stray prints don't
    # corrupt fd 1. _real_stdout_buffer was already captured at module top.
    _configure_mcp_logging()

    load_dotenv()

    # Import tools module to trigger tool registration via @mcp.tool() decorators
    import sqlcg.server.tools

    # A2 — Tool-entry hook: wrap the single runtime CallToolRequest dispatch handler
    # (NOT mcp._mcp_server.call_tool which is a decorator factory) so every tool
    # invocation goes through maybe_self_heal() first.
    #
    # Grep evidence (confirmed by `grep -n "request_handlers\[types.CallToolRequest\]"
    # in the mcp package): FastMCP installs exactly one handler via
    #   mcp._mcp_server.request_handlers[CallToolRequest]
    # in Server.call_tool(). Every @mcp.tool() invocation flows through it.
    # We wrap once here, AFTER import sqlcg.server.tools (which triggers tool
    # registration) so the original handler is already populated.
    _orig_call_tool_handler = mcp._mcp_server.request_handlers[_types.CallToolRequest]

    async def _self_heal_then_dispatch(req):  # type: ignore[no-untyped-def]
        """Throttled skew check then delegate to the original tool handler (A2, D3)."""
        maybe_self_heal()  # synchronous, non-blocking in common path
        return await _orig_call_tool_handler(req)

    mcp._mcp_server.request_handlers[_types.CallToolRequest] = _self_heal_then_dispatch

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
