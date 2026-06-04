"""Integration tests for v1.2.0: server-routed read proxy (read half of #28).

Proves that a CLI read can return real rows over the control socket while the
server process holds KuzuDB's process-level write lock — the exact situation
that makes a direct ``get_backend(read_only=True)`` fail with "Database is
locked".

Rather than spawn a full ``sqlcg mcp start`` subprocess, these tests drive
``_control_socket_task`` directly in an anyio task group with a real
``KuzuBackend`` that holds the lock (same approach as
``test_reindex_op_calls_resync_changed_once``).  This keeps the lock-holding
process in-test so the "locked" assertion is deterministic.

Scenarios:
    A — query op returns real rows while a writer holds the lock (#28 proof),
        AND a concurrent direct read-only open of the same DB raises "locked".
    D — query op rejects a write statement (read-only guard); graph unchanged.
    F — a > 64 KiB result set is returned in full over the framed protocol.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import anyio
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain socket tests require a non-Windows platform",
)


def _send_framed(sp: Path, req: dict, timeout: float = 25.0) -> dict:
    """Send a framed request (``<len>\\n<json>``) and read the framed response.

    Uses the same recv-exactly strategy as the production client
    (``makefile`` + ``readline`` + ``read(n)``) so this exercises the real
    framing contract, not a single ``recv``.
    """
    body = json.dumps(req).encode()
    frame = f"{len(body)}\n".encode() + body
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sp))
        s.sendall(frame)
        f = s.makefile("rb")
        length_line = f.readline()
        n = int(length_line.strip())
        resp_body = f.read(n)
    return json.loads(resp_body)


async def _serve_with_backend(db_path: Path, backend, do_requests):
    """Start ``_control_socket_task`` on ``backend`` and run ``do_requests``.

    ``do_requests(sp)`` is a sync callable executed in a worker thread (it does
    blocking socket I/O); its return value is handed back to the caller.
    """
    from sqlcg.server.control import sock_path
    from sqlcg.server.server import _control_socket_task

    sp = sock_path(db_path)
    stop_event = anyio.Event()
    lock = anyio.Lock()
    result_holder: dict = {}

    from sqlcg.server.writer import WriterQueue

    writer_queue = WriterQueue()

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            _control_socket_task, db_path, lambda: backend, stop_event, lock, 0.0, writer_queue
        )

        deadline = anyio.current_time() + 5.0
        while not sp.exists():
            await anyio.sleep(0.05)
            if anyio.current_time() > deadline:
                raise TimeoutError("Control socket did not appear")

        result_holder["value"] = await anyio.to_thread.run_sync(do_requests, sp)

        tg.cancel_scope.cancel()

    return result_holder["value"]


# ---------------------------------------------------------------------------
# Scenario A — read succeeds over the socket while a writer holds the lock
# ---------------------------------------------------------------------------


def test_scenario_a_query_returns_rows_while_writer_holds_lock(tmp_path: Path) -> None:
    """The #28 proof: real rows come back over the socket while the lock is held,
    and a concurrent direct read-only open of the same DB fails with 'locked'."""
    from sqlcg.core.kuzu_backend import KuzuBackend

    db_path = tmp_path / "read_a.db"

    backend = KuzuBackend(str(db_path))  # holds the process write lock
    backend.init_schema()
    # Seed a deterministic, JSON-scalar row to assert on.
    backend.run_write(
        "CREATE (:SqlTable {qualified: $q, name: $n, kind: 'TABLE'})",
        {"q": "ba.orders", "n": "orders"},
    )

    # Prove the lock is genuinely exclusive ACROSS PROCESSES: a separate
    # process opening the same DB read-only must fail with "locked" while this
    # process holds the write lock.  (A second open within THIS process does
    # not trip Kuzu's lock — the #28 failure is inherently cross-process, which
    # is why the proxy routes through the lock-holder.)
    probe = textwrap.dedent(
        f"""
        import kuzu
        try:
            kuzu.Database({str(db_path)!r}, read_only=True)
            print("OPENED")
        except Exception as e:
            print("LOCKED" if "lock" in str(e).lower() else "OTHER:" + str(e))
        """
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "LOCKED", (
        "a separate process opening the DB read-only must fail with a lock error "
        f"while a writer holds it (this is what makes the proxy necessary); "
        f"got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    def _do(sp: Path) -> dict:
        return _send_framed(
            sp,
            {
                "op": "query",
                "cypher": "MATCH (t:SqlTable) RETURN t.qualified AS qualified, t.kind AS kind",
                "params": {},
            },
        )

    resp = anyio.run(_serve_with_backend, db_path, backend, _do)

    assert resp.get("ok") is True, f"expected ok response, got: {resp}"
    assert resp["rows"] == [{"qualified": "ba.orders", "kind": "TABLE"}], (
        f"socket query must return the seeded row; got {resp['rows']}"
    )


# ---------------------------------------------------------------------------
# Scenario D — read-only guard rejects a write statement; graph unchanged
# ---------------------------------------------------------------------------


def test_scenario_d_write_statement_rejected_graph_unchanged(tmp_path: Path) -> None:
    from sqlcg.core.kuzu_backend import KuzuBackend

    db_path = tmp_path / "read_d.db"
    backend = KuzuBackend(str(db_path))
    backend.init_schema()
    backend.run_write(
        "CREATE (:SqlTable {qualified: $q, name: $n, kind: 'TABLE'})",
        {"q": "ba.seed", "n": "seed"},
    )

    def _do(sp: Path) -> dict:
        rejected = _send_framed(
            sp,
            {
                "op": "query",
                "cypher": "CREATE (:SqlTable {qualified: 'ba.injected', name: 'x', kind: 'TABLE'})",
                "params": {},
            },
        )
        # Follow-up read to confirm no mutation happened.
        count = _send_framed(
            sp,
            {
                "op": "query",
                "cypher": "MATCH (t:SqlTable) RETURN COUNT(t) AS c",
                "params": {},
            },
        )
        return {"rejected": rejected, "count": count}

    out = anyio.run(_serve_with_backend, db_path, backend, _do)

    assert out["rejected"].get("error") == "query op is read-only", (
        f"write statement must be rejected by the read-only guard; got {out['rejected']}"
    )
    assert out["count"]["rows"][0]["c"] == 1, (
        f"graph must be unchanged after a rejected write; count={out['count']['rows']}"
    )


# ---------------------------------------------------------------------------
# Scenario F — large result set returned in full over the framed protocol
# ---------------------------------------------------------------------------


def test_scenario_f_large_result_not_truncated(tmp_path: Path) -> None:
    """A result well over a single recv(65536) is returned in full (framing guard)."""
    from sqlcg.core.kuzu_backend import KuzuBackend

    db_path = tmp_path / "read_f.db"
    backend = KuzuBackend(str(db_path))
    backend.init_schema()

    # ~1000 rows; each path ~200 bytes → response body well over 64 KiB.
    n_rows = 1000
    for i in range(n_rows):
        backend.run_write(
            "CREATE (:SqlTable {qualified: $q, name: $n, kind: 'TABLE'})",
            {"q": f"schema.table_{i:05d}_" + ("x" * 180), "n": f"t{i}"},
        )

    def _do(sp: Path) -> dict:
        return _send_framed(
            sp,
            {"op": "query", "cypher": "MATCH (t:SqlTable) RETURN t.qualified AS q", "params": {}},
        )

    resp = anyio.run(_serve_with_backend, db_path, backend, _do)

    assert resp.get("ok") is True
    body_len = len(json.dumps(resp).encode())
    assert body_len > 65536, "fixture must exceed a single recv(65536) buffer to be meaningful"
    assert len(resp["rows"]) == n_rows, (
        f"all {n_rows} rows must survive framing; got {len(resp['rows'])} (truncation?)"
    )
