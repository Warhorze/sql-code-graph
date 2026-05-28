"""Persistent spawn-mode worker pool with per-task hard-kill timeouts.

Each worker holds two pre-built parsers (pass-1 empty schema, pass-2 full
schema).  The coordinator kills workers that exceed their per-task wall-clock
budget and respawns replacements — concurrent.futures cannot do this because
its timeout only cancels the *wait*, not the running work.

KuzuDB stays exclusively in the main process: workers use spawn mode, so
they inherit no file descriptors from the parent.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.connection import wait as _mp_wait
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # SpawnProcess is the concrete type returned by spawn-context .Process();
    # the public mp.Process is an abstract base.  We alias it to avoid a
    # pyright "cannot assign SpawnProcess to Process" error.
    from multiprocessing.context import SpawnProcess as _AnyProcess
else:
    _AnyProcess = Any

from sqlcg.parsers.base import ParsedFile
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

_SHUTDOWN = "__sqlcg_pool_shutdown__"  # string sentinel: survives pickle across spawn boundary


# ---------------------------------------------------------------------------
# Worker subprocess
# ---------------------------------------------------------------------------


def _worker_main(
    conn: Connection,
    dialect: str | None,
    schema_csv_path: str | None,
    schema_aliases: dict[str, str] | None = None,
) -> None:
    """Entry point for each pool worker.  Runs in a spawned child process.

    Builds both parsers once, then loops receiving task dicts and sending
    ParsedFile results back.  Never raises — exceptions are caught and
    forwarded as exception objects so the coordinator can handle them.
    """
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.registry import get_parser

    schema_p1 = SchemaResolver(dialect=dialect)
    parser_p1 = get_parser(dialect, schema_p1, schema_aliases=schema_aliases)

    schema_p2 = SchemaResolver(dialect=dialect)
    if schema_csv_path:
        # INERT (measured 2026-05-28): see SchemaResolver.add_information_schema.
        schema_p2.add_information_schema(schema_csv_path)
    parser_p2 = get_parser(dialect, schema_p2, schema_aliases=schema_aliases)

    try:
        while True:
            try:
                task = conn.recv()
            except EOFError:
                break
            if task == _SHUTDOWN:
                break
            try:
                result = _run_task(task, parser_p1, parser_p2, dialect)
                # Strip parent back-references from defined_body ASTs so that
                # pickle doesn't drag the entire Create statement into the payload.
                _strip_defined_body_parents(result)
                conn.send(result)
            except BaseException as exc:  # noqa: BLE001
                try:
                    conn.send(exc)
                except Exception:
                    conn.send(RuntimeError(f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _run_task(
    task: dict,
    parser_p1: Any,
    parser_p2: Any,
    dialect: str | None,
) -> ParsedFile:
    """Dispatch a single task to the appropriate parser."""
    path = Path(task["path"])
    sql: str = task["sql"]

    if task["type"] == "parse_pass1":
        return parser_p1.parse_file(path, sql)

    # parse_pass2
    dep_filter: set[str] | None = task.get("dependency_filter")
    xfile_sql: dict[str, str] = task.get("xfile_sql") or {}

    if xfile_sql:
        import sqlglot
        import sqlglot.expressions as exp

        rebuilt: dict[str, Any] = {}
        for name, sql_str in xfile_sql.items():
            try:
                ast = sqlglot.parse_one(sql_str, dialect=dialect, into=exp.Select)
                rebuilt[name] = ast
            except Exception as exc:
                logger.warning("Failed to rebuild cross-file source %s: %s", name, exc)
        parser_p2._schema.register_cross_file_sources(rebuilt)

    try:
        return parser_p2.parse_file(path, sql, dependency_filter=dep_filter)
    finally:
        if xfile_sql:
            # Reset so the next task starts from a clean slate.
            parser_p2._schema.register_cross_file_sources({})


def _strip_defined_body_parents(parsed: ParsedFile) -> None:
    """Clear parent back-pointers on defined_body ASTs to reduce pickle size.

    sqlglot Expression nodes have a .parent attribute that forms a chain back
    to the enclosing Create statement.  Pickling a bare exp.Select through a
    Pipe would drag in the full Create AST.  Calling .copy() resets parent to
    None and produces a clean, self-contained node.
    """
    for stmt in parsed.statements:
        if stmt.defined_body is not None:
            try:
                stmt.defined_body = stmt.defined_body.copy()
            except Exception:
                stmt.defined_body = None


# ---------------------------------------------------------------------------
# Worker state (coordinator side)
# ---------------------------------------------------------------------------


@dataclass
class _WorkerState:
    conn: Connection
    process: _AnyProcess
    busy: bool = False
    task_idx: int = -1
    task_path: str = ""
    task_start: float = 0.0
    # Track which worker slot this occupies (set by HardKillPool)
    slot: int = -1
    # Per-path consecutive timeout count (carried to respawned replacement)
    kill_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class HardKillPool:
    """Persistent spawn-mode worker pool with per-task hard-kill timeouts.

    Usage::

        with HardKillPool(dialect, schema_csv_path, n_workers=8) as pool:
            results_p1 = pool.map(p1_tasks, per_task_timeout=5.0)
            results_p2 = pool.map(p2_tasks, per_task_timeout=15.0)

    The same pool instance can be reused for multiple ``map`` calls (e.g. one
    per pass) without paying the warm-up cost twice.
    """

    def __init__(
        self,
        dialect: str | None,
        schema_csv_path: str | None = None,
        n_workers: int | None = None,
        schema_aliases: dict[str, str] | None = None,
    ) -> None:
        self._dialect = dialect
        self._schema_csv_path = schema_csv_path
        self._schema_aliases: dict[str, str] = schema_aliases or {}
        self._n = n_workers or os.cpu_count() or 4
        self._ctx = mp.get_context("spawn")
        self._workers: list[_WorkerState] = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> HardKillPool:
        self._spawn_all()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _spawn_one(self, slot: int) -> _WorkerState:
        coord_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_worker_main,
            args=(child_conn, self._dialect, self._schema_csv_path, self._schema_aliases),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # coordinator holds the other end only
        return _WorkerState(conn=coord_conn, process=proc, slot=slot)

    def _spawn_all(self) -> None:
        for i in range(self._n):
            self._workers.append(self._spawn_one(slot=i))

    def _respawn(self, old: _WorkerState) -> _WorkerState:
        """SIGKILL old worker and return a fresh replacement in the same slot."""
        try:
            old.conn.close()
        except Exception:
            pass
        if old.process.is_alive():
            old.process.kill()
            old.process.join(timeout=5)
        new = self._spawn_one(slot=old.slot)
        new.kill_counts = old.kill_counts  # carry accumulated kill counts
        self._workers[old.slot] = new
        return new

    # ------------------------------------------------------------------
    # Map
    # ------------------------------------------------------------------

    def map(
        self,
        tasks: list[dict],
        per_task_timeout: float,
        poison_retries: int = 2,
    ) -> list[ParsedFile | None]:
        """Dispatch all tasks; return results in the same order as *tasks*.

        Workers that exceed *per_task_timeout* wall-clock seconds are
        SIGKILL'd and immediately replaced by a fresh spawn.  A path that
        triggers *poison_retries* consecutive kills is marked as poisoned and
        all remaining tasks for that path are skipped.
        """
        if not tasks:
            return []

        n_tasks = len(tasks)
        results: list[ParsedFile | None] = [None] * n_tasks
        queue: list[int] = list(range(n_tasks))  # indices of un-dispatched tasks

        # Maps task path -> consecutive timeout count (resets on success)
        kill_counts: dict[str, int] = {}
        # Worker slots currently busy
        busy: set[int] = set()

        def _next_non_poisoned_idx() -> int | None:
            """Pop and return the next non-poisoned task index, or None."""
            while queue:
                tidx = queue.pop(0)
                path = tasks[tidx].get("path", "")
                if kill_counts.get(path, 0) >= poison_retries:
                    results[tidx] = _timeout_file(path, self._dialect, poison=True)
                    logger.warning("Skipping %s — poisoned after %d kills", path, poison_retries)
                    continue
                return tidx
            return None

        def _assign(slot: int) -> None:
            """Send the next available task to the worker in *slot*.

            On pipe send failure: records the task as an error, respawns the
            worker, and retries the next task — iteratively, not recursively,
            so a sequence of broken pipes drains the queue safely rather than
            leaving the slot permanently idle.
            """
            while True:
                tidx = _next_non_poisoned_idx()
                if tidx is None:
                    return  # nothing left; worker idles
                task = tasks[tidx]
                path = task.get("path", "")
                w = self._workers[slot]
                try:
                    w.conn.send(task)
                    break  # send succeeded; fall through to mark worker busy
                except Exception as exc:
                    logger.warning(
                        "Pipe send error slot %d for %s: %s — recording failure, respawning",
                        slot,
                        path,
                        exc,
                    )
                    results[tidx] = _error_file(path, self._dialect, "send_failed")
                    self._respawn(w)
                    # Loop: try the next task on the fresh worker

            w = self._workers[slot]  # re-fetch after any respawn
            w.busy = True
            w.task_idx = tidx
            w.task_path = path
            w.task_start = time.monotonic()
            busy.add(slot)

        # Initial dispatch: fill all worker slots
        for i in range(min(self._n, n_tasks)):
            _assign(i)

        while busy:
            # 1. Check for timeouts
            now = time.monotonic()
            timed_out = [
                slot
                for slot in list(busy)
                if now - self._workers[slot].task_start > per_task_timeout
            ]
            for slot in timed_out:
                w = self._workers[slot]
                path = w.task_path
                tidx = w.task_idx
                kill_counts[path] = kill_counts.get(path, 0) + 1
                logger.warning(
                    "Timeout %s (>%.1fs) — killing worker slot %d (kill #%d for this path)",
                    path,
                    per_task_timeout,
                    slot,
                    kill_counts[path],
                )
                results[tidx] = _timeout_file(path, self._dialect)
                self._respawn(w)
                busy.discard(slot)
                _assign(slot)

            if not busy:
                break

            # 2. Wait for any worker to produce a result (short poll interval)
            ready_conns = _mp_wait(
                [self._workers[s].conn for s in busy],
                timeout=0.05,
            )

            for conn in ready_conns:
                if not isinstance(conn, Connection):
                    continue  # ignore non-Connection handles (sockets, raw fds)
                # Identify which slot this connection belongs to
                slot = next(
                    (s for s in busy if self._workers[s].conn is conn),
                    None,
                )
                if slot is None:
                    continue  # stale connection from a respawn; skip

                w = self._workers[slot]
                try:
                    result = conn.recv()
                except (EOFError, OSError) as exc:
                    logger.warning("Pipe recv error slot %d: %s — respawning", slot, exc)
                    result = RuntimeError(f"pipe_error:{exc}")
                    self._respawn(w)

                tidx = w.task_idx
                path = w.task_path

                if isinstance(result, BaseException):
                    logger.warning("Worker error %s: %s", path, result)
                    results[tidx] = _error_file(
                        path, self._dialect, f"{type(result).__name__}:{result}"
                    )
                else:
                    results[tidx] = result
                    kill_counts.pop(path, None)  # success resets consecutive kills

                w.busy = False
                busy.discard(slot)
                _assign(slot)

        return results

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Gracefully stop all workers, then force-kill any that linger."""
        for w in self._workers:
            try:
                w.conn.send(_SHUTDOWN)
            except Exception:
                pass
            try:
                w.conn.close()
            except Exception:
                pass
        for w in self._workers:
            w.process.join(timeout=2)
            if w.process.is_alive():
                w.process.kill()
                w.process.join(timeout=5)
        self._workers.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timeout_file(
    path: str,
    dialect: str | None,
    poison: bool = False,
) -> ParsedFile:
    pf = ParsedFile(path=Path(path), dialect=dialect)
    msg = "skipped:poison" if poison else "timeout"
    pf.errors.append(f"{msg} file={Path(path).name}")
    return pf


def _error_file(path: str, dialect: str | None, detail: str) -> ParsedFile:
    pf = ParsedFile(path=Path(path), dialect=dialect)
    pf.errors.append(f"worker_error:{detail}")
    return pf
