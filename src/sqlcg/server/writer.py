"""Single-writer queue, escalation primitive, and drain task for the MCP server.

Owns the RO→RW escalation (close the read-only backend, reopen read-write for
the duration of a drain, then de-escalate back to read-only) and the
coalescing WriterQueue that serialises all write ops through the server.

Constants below are server-side transport/escalation parameters — NOT
KuzuConfig values.  Same convention as _NOTIFY_SOCKET_TIMEOUT_S in reindex.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlcg.utils.logging import getLogger

if TYPE_CHECKING:
    import anyio

    from sqlcg.core.graph_db import GraphBackend
    from sqlcg.metrics.store import MetricsStore

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Escalation constants — not config-owned
# ---------------------------------------------------------------------------

# Total retry budget for RW reopen on lock error.
_ESCALATION_RETRY_BUDGET_S = 5.0
# Initial backoff in seconds; doubles each attempt, capped at _BACKOFF_CAP_S.
_BACKOFF_START_S = 0.02
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP_S = 0.5

# ---------------------------------------------------------------------------
# Coalesce reason constants — single source of truth for status, logs, metrics
# ---------------------------------------------------------------------------

COALESCE_SUPERSEDED_BY_INDEX = "superseded_by_index"
COALESCE_COLLAPSED_INTO_PENDING_REINDEX = "collapsed_into_pending_reindex"
COALESCE_REINDEX_DROPPED_INDEX_PENDING = "reindex_dropped_index_pending"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EscalationLockError(RuntimeError):
    """Raised when escalate_to_rw exhausts its retry budget.

    The C3 message names the PID hint and the SQLCG_DB_PATH side-DB workaround.
    """


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class WriterRequest:
    """A single enqueued write request."""

    op: Literal["index", "reindex"]
    root: str
    dialect: str | None
    from_sha: str | None
    to_sha: str | None
    requested_by: str  # "cli" or "hook"
    queued_at: float = field(default_factory=time.time)
    # Subscribers waiting for this request to complete (for wait=true clients).
    # Each entry is a (send_channel, ) pair written by the drain task.
    _waiters: list = field(default_factory=list, repr=False, compare=False)


class WriterQueue:
    """Serialised, coalescing write request queue for the single-writer model.

    Coalescing rules (enforced at enqueue time under ``_lock``):

    1. A full ``index`` enqueue drops ALL pending requests (it supersedes).
    2. A ``reindex`` arriving while another ``reindex`` is pending collapses
       into that pending request (the pending one executes against HEAD at
       drain time).
    3. A ``reindex`` arriving while a ``index`` is pending is dropped (the
       pending ``index`` subsumes it).
    4. Write lock is held only while draining — enforced by the drain task.

    The *active* request (currently draining) is never mutated by coalescing.
    """

    def __init__(self, metrics: MetricsStore | None = None) -> None:
        import anyio

        self._pending: list[WriterRequest] = []
        self._active: WriterRequest | None = None
        self._active_progress: dict = {}
        self._coalesced: dict[str, int] = {
            COALESCE_SUPERSEDED_BY_INDEX: 0,
            COALESCE_COLLAPSED_INTO_PENDING_REINDEX: 0,
            COALESCE_REINDEX_DROPPED_INDEX_PENDING: 0,
        }
        self._last_coalesce_at: float | None = None
        self._last_coalesce_reason: str | None = None
        self._lock: anyio.Lock = anyio.Lock()
        self._wake: anyio.Event = anyio.Event()
        self._metrics = metrics

    async def enqueue(self, req: WriterRequest) -> int:
        """Enqueue *req* and return its position (1-indexed, after coalescing).

        Applies coalescing rules 1–3.  Returns the pending-queue position
        (1 = next to drain) so callers can report it in ``{queued: true}``.
        """
        async with self._lock:
            if req.op == "index":
                # Rule 1: full index supersedes all pending requests.
                n_superseded = len(self._pending)
                if n_superseded > 0:
                    # Collect waiters from superseded requests to relay terminal frame
                    superseded_waiters: list = []
                    for old_req in self._pending:
                        superseded_waiters.extend(old_req._waiters)
                    self._pending.clear()
                    for _ in range(n_superseded):
                        self._record_coalesce(COALESCE_SUPERSEDED_BY_INDEX)
                    # Notify superseded waiters that their request was coalesced away
                    for ch in superseded_waiters:
                        try:
                            await ch.send(
                                {
                                    "ok": True,
                                    "done": True,
                                    "coalesced": True,
                                    "summary": {"note": "superseded by a newer full index"},
                                }
                            )
                        except Exception:
                            pass
                self._pending.append(req)
            elif req.op == "reindex":
                # Rule 3: reindex behind a pending index → drop.
                if any(r.op == "index" for r in self._pending):
                    self._record_coalesce(COALESCE_REINDEX_DROPPED_INDEX_PENDING)
                    # Notify waiter immediately
                    for ch in req._waiters:
                        try:
                            await ch.send(
                                {
                                    "ok": True,
                                    "done": True,
                                    "coalesced": True,
                                    "summary": {"note": "dropped — full index already pending"},
                                }
                            )
                        except Exception:
                            pass
                    return len(self._pending)
                # Rule 2: reindex collapses into existing pending reindex.
                existing = next((r for r in self._pending if r.op == "reindex"), None)
                if existing is not None:
                    self._record_coalesce(COALESCE_COLLAPSED_INTO_PENDING_REINDEX)
                    # Transfer waiters to the existing request
                    existing._waiters.extend(req._waiters)
                    return len(self._pending)
                self._pending.append(req)
            else:
                self._pending.append(req)

            # Wake the drain task
            if not self._wake.is_set():
                self._wake.set()

            logger.info(
                f"enqueued op={req.op} root={req.root!r} by={req.requested_by} "
                f"position={len(self._pending)}"
            )
            self._record_enqueue(req, len(self._pending))
            return len(self._pending)

    async def pop_next(self) -> WriterRequest | None:
        """Pop the next pending request (or None if empty).

        Resets the wake Event so the drain can wait again.
        """
        async with self._lock:
            if not self._pending:
                self._wake = __import__("anyio").Event()
                return None
            req = self._pending.pop(0)
            self._active = req
            self._active_progress = {
                "state": "running",
                "op": req.op,
                "started_at": time.time(),
                "files_done": 0,
                "files_total": None,
            }
            if not self._pending:
                self._wake = __import__("anyio").Event()
            return req

    def mark_active_done(self, summary: dict) -> None:
        """Called by the drain task after a successful drain."""
        self._active = None
        self._active_progress = {
            "state": "done",
            "finished_at": time.time(),
            "summary": summary,
        }

    def mark_active_failed(self, error: str) -> None:
        """Called by the drain task when the indexer body raises."""
        self._active = None
        self._active_progress = {
            "state": "failed",
            "error": error,
            "finished_at": time.time(),
        }

    def update_progress(self, files_done: int, files_total: int) -> None:
        """Update the active drain's file-level progress."""
        if self._active_progress.get("state") == "running":
            self._active_progress["files_done"] = files_done
            self._active_progress["files_total"] = files_total

    def coalesce_view(self) -> dict:
        """Return a consistent snapshot of queue state for status responses.

        Synchronous — reads are safe without the async lock because mutations
        only add to counts; no torn reads on int fields in CPython.
        """
        total = sum(self._coalesced.values())
        return {
            "active": (
                {
                    "op": self._active.op,
                    "root": self._active.root,
                    "requested_by": self._active.requested_by,
                    "queued_at": self._active.queued_at,
                }
                if self._active is not None
                else None
            ),
            "active_progress": dict(self._active_progress),
            "pending": [
                {
                    "op": r.op,
                    "root": r.root,
                    "requested_by": r.requested_by,
                    "queued_at": r.queued_at,
                }
                for r in self._pending
            ],
            "coalesced_since_start": total,
            "coalesced_by_reason": dict(self._coalesced),
            "last_coalesce_at": self._last_coalesce_at,
            "last_coalesce_reason": self._last_coalesce_reason,
        }

    def _record_coalesce(self, reason: str) -> None:
        """Bump per-reason coalesce counter and emit lifecycle log + metrics."""
        self._coalesced[reason] = self._coalesced.get(reason, 0) + 1
        self._last_coalesce_at = time.time()
        self._last_coalesce_reason = reason
        logger.info(f"coalesced (reason={reason})")
        if self._metrics is not None:
            try:
                self._metrics.record_queue_event(
                    "coalesced",
                    reason=reason,
                    queue_depth=len(self._pending),
                )
            except Exception:
                pass

    def _record_enqueue(self, req: WriterRequest, depth: int) -> None:
        """Record an enqueue event to MetricsStore (best-effort)."""
        if self._metrics is not None:
            try:
                self._metrics.record_queue_event(
                    "enqueued",
                    op=req.op,
                    queue_depth=depth,
                )
            except Exception:
                pass

    def record_drained(self, op: str, duration_ms: float) -> None:
        """Record a completed drain to MetricsStore (best-effort)."""
        logger.info(f"drained op={op} duration_ms={duration_ms:.1f}")
        if self._metrics is not None:
            try:
                self._metrics.record_queue_event(
                    "drained",
                    op=op,
                    duration_ms=duration_ms,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Escalation primitive
# ---------------------------------------------------------------------------


def escalate_to_rw(
    db_path: str,
    *,
    current: GraphBackend | None = None,
    opener=None,
    retry_budget_s: float = _ESCALATION_RETRY_BUDGET_S,
) -> GraphBackend:
    """Close *current* RO backend and reopen read-write with bounded retry.

    This is the single RO→RW escalation path — reused by both the startup
    schema-ensure window (Step 1.3) and every drain (Phase 2).

    Args:
        db_path: Path to the KùzuDB database file.
        current: The currently-open backend to close before reopening RW.
            Pass ``None`` when no handle exists yet (startup path).
        opener: Callable ``(path: str, read_only: bool) -> GraphBackend``.
            Defaults to ``KuzuBackend``.  Tests inject a fake opener to
            exercise retry/backoff deterministically without real lock races
            — never patch a module global (parallel-test-safe).
        retry_budget_s: Total retry budget in seconds.  Default is the
            module constant.  Override via this parameter in tests — never
            via a global patch.

    Returns:
        A new read-write ``GraphBackend`` stored in ``tools._backend``.

    Raises:
        EscalationLockError: Budget exhausted — the C3 message names the
            PID hint and the ``SQLCG_DB_PATH`` side-DB workaround.
        RuntimeError: Non-lock error from the opener (non-retryable).
    """
    import random

    import sqlcg.server.tools as _tools
    from sqlcg.core.kuzu_backend import KuzuBackend, find_lock_holder

    if opener is None:
        opener = KuzuBackend

    # Close the current backend (if any) before attempting RW open.
    if current is not None:
        try:
            current.close()
        except Exception:
            pass
        # Ensure the module singleton no longer points at the closed handle.
        if _tools._backend is current:
            _tools._backend = None

    deadline = time.monotonic() + retry_budget_s
    backoff = _BACKOFF_START_S
    attempts = 0

    while True:
        attempts += 1
        try:
            rw_backend = opener(db_path, read_only=False)
            _tools._backend = rw_backend
            logger.debug(f"escalated to RW db_path={db_path!r} attempts={attempts}")
            return rw_backend
        except RuntimeError as exc:
            exc_str = str(exc)
            is_lock = "Could not set lock" in exc_str or "lock" in exc_str.lower()
            if not is_lock:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Budget exhausted — emit ERROR log then raise C3.
                pid_hint = find_lock_holder(db_path)
                logger.error(
                    f"escalation failed: attempts={attempts} "
                    f"budget={retry_budget_s:.1f}s db_path={db_path!r} "
                    f"holder={pid_hint}"
                )
                # Reopen RO so the server keeps serving reads.
                try:
                    ro = opener(db_path, read_only=True)
                    _tools._backend = ro
                except Exception:
                    pass
                msg = (
                    f"Could not acquire the write lock to reindex after "
                    f"{retry_budget_s:.1f}s — another process is holding the "
                    f"database ({pid_hint}). The graph was not updated. "
                    f"To run a one-off index without the server, point at a side DB:\n"
                    f"    SQLCG_DB_PATH=/tmp/sqlcg-cli/graph.db sqlcg index <path>\n"
                    f"or stop the server first ('sqlcg mcp stop')."
                )
                raise EscalationLockError(msg) from exc

            # Exponential backoff with jitter, capped at _BACKOFF_CAP_S.
            jitter = random.uniform(0, backoff * 0.1)
            sleep_for = min(backoff + jitter, min(remaining, _BACKOFF_CAP_S))
            time.sleep(sleep_for)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_CAP_S)


def de_escalate_to_ro(
    db_path: str,
    *,
    shutdown_requested: anyio.Event | None = None,
    opener=None,
) -> None:
    """Close the current RW backend and reopen read-only.

    Always runs in the drain's ``finally`` block.  When *shutdown_requested*
    is set, skips the RO reopen and leaves ``tools._backend = None`` so
    ``shutdown_backend()`` can finish teardown cleanly (B2 guard).

    Args:
        db_path: Path to the KùzuDB database file.
        shutdown_requested: An ``anyio.Event`` that, when set, tells this
            function to skip the RO reopen (B2 shutdown ordering).
        opener: Injectable backend constructor (default ``KuzuBackend``).
    """
    import sqlcg.server.tools as _tools
    from sqlcg.core.kuzu_backend import KuzuBackend

    if opener is None:
        opener = KuzuBackend

    current = _tools._backend
    if current is not None:
        try:
            current.close()
        except Exception:
            pass
        _tools._backend = None

    if shutdown_requested is not None and shutdown_requested.is_set():
        # B2: shutdown in progress — do not reopen RO; leave _backend = None
        # so shutdown_backend() finds a clean state.
        logger.debug("de_escalate_to_ro: shutdown requested — skipping RO reopen")
        return

    try:
        ro = opener(db_path, read_only=True)
        _tools._backend = ro
        logger.debug(f"de-escalated to RO db_path={db_path!r}")
    except Exception as exc:
        logger.error(f"de_escalate_to_ro: failed to reopen RO: {exc}")


# ---------------------------------------------------------------------------
# Drain task
# ---------------------------------------------------------------------------


async def drain_loop(
    queue: WriterQueue,
    db_path: str,
    backend_lock: anyio.Lock,
    shutdown_requested: anyio.Event,
    opener=None,
) -> None:
    """Consume WriterQueue requests one at a time under backend_lock.

    This is the sole backend consumer — no other code path resolves or
    touches the backend while a drain holds backend_lock.

    The drain task:
      1. Waits on queue._wake.
      2. Pops the next request.
      3. Acquires backend_lock.
      4. Escalates RO→RW (escalate_to_rw).
      5. Runs the indexer op off the event-loop thread.
      6. De-escalates RW→RO in a finally (de_escalate_to_ro).
      7. Clears queue._active; records drain metrics.
      8. Repeats until shutdown_requested is set.

    W7 — drain body exception: non-EscalationLockError exceptions are caught,
    logged at ERROR, and the loop continues so one bad request cannot kill the
    drain task.
    """
    import anyio.to_thread as _to_thread

    from sqlcg.indexer.indexer import Indexer

    while not shutdown_requested.is_set():
        # Wait until there is work to do.
        await queue._wake.wait()
        if shutdown_requested.is_set():
            break

        req = await queue.pop_next()
        if req is None:
            continue

        logger.info(f"drain started op={req.op} root={req.root!r}")
        drain_start = time.monotonic()

        async with backend_lock:
            from pathlib import Path as _Path

            import sqlcg.server.tools as _tools

            try:
                rw = escalate_to_rw(
                    db_path,
                    current=_tools._backend,
                    opener=opener,
                )
            except EscalationLockError as exc:
                # C3: escalation failed — notify waiters and continue.
                queue.mark_active_failed(str(exc))
                for ch in req._waiters:
                    try:
                        await ch.send({"ok": False, "done": True, "error": str(exc)})
                    except Exception:
                        pass
                continue

            try:
                indexer = Indexer()

                if req.op == "index":
                    # Capture loop vars by value to satisfy B023.
                    _req = req
                    _rw = rw
                    _idx = indexer

                    def _do_index(_r=_req, _b=_rw, _q=queue, _P=_Path, _i=_idx) -> dict:
                        return _i.index_repo(
                            _P(_r.root),
                            _r.dialect,
                            _b,
                            progress_callback=_q.update_progress,
                        )

                    summary = await _to_thread.run_sync(_do_index)
                elif req.op == "reindex":
                    # Resolve from_sha / to_sha at drain time (rule 2 + W3).
                    effective_from = req.from_sha
                    effective_to = req.to_sha

                    if effective_from is None:
                        # Standalone mode — resolve stored SHA.
                        try:
                            effective_from = rw.get_indexed_sha()
                        except Exception:
                            effective_from = None
                        if effective_from is None:
                            err = "no prior index — run 'sqlcg index <path>' first"
                            queue.mark_active_failed(err)
                            for ch in req._waiters:
                                try:
                                    await ch.send({"ok": False, "done": True, "error": err})
                                except Exception:
                                    pass
                            continue

                    if effective_to is None:
                        # Resolve current HEAD.
                        import subprocess

                        try:
                            result = subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                cwd=req.root,
                                capture_output=True,
                                text=True,
                            )
                            effective_to = result.stdout.strip() if result.returncode == 0 else None
                        except Exception:
                            effective_to = None

                    if effective_to is None:
                        err = "could not resolve HEAD SHA for reindex"
                        queue.mark_active_failed(err)
                        for ch in req._waiters:
                            try:
                                await ch.send({"ok": False, "done": True, "error": err})
                            except Exception:
                                pass
                        continue

                    # Capture loop vars by value to satisfy B023.
                    _req2 = req
                    _rw2 = rw
                    _ef = effective_from
                    _et = effective_to
                    _idx2 = indexer

                    def _do_reindex(_r=_req2, _b=_rw2, _f=_ef, _t=_et, _P=_Path, _i=_idx2) -> dict:
                        return _i.resync_changed(
                            _P(_r.root),
                            _f,
                            _t,
                            _b,
                            _r.dialect,
                        )

                    summary = await _to_thread.run_sync(_do_reindex)
                else:
                    summary = {}

                queue.mark_active_done(summary)
                # Notify waiting clients with terminal frame.
                for ch in req._waiters:
                    try:
                        await ch.send({"ok": True, "done": True, "summary": summary})
                    except Exception:
                        pass

            except Exception as exc:
                # W7 — non-escalation failure: clear active, relay terminal frame.
                err_str = str(exc)
                logger.error(f"drain body raised: {err_str}")
                queue.mark_active_failed(err_str)
                for ch in req._waiters:
                    try:
                        await ch.send({"ok": False, "done": True, "error": err_str})
                    except Exception:
                        pass
            finally:
                de_escalate_to_ro(
                    db_path,
                    shutdown_requested=shutdown_requested,
                    opener=opener,
                )
                duration_ms = (time.monotonic() - drain_start) * 1000
                queue.record_drained(req.op, duration_ms)
