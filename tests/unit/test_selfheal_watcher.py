"""Unit tests for _self_heal_watcher drain ordering and F2 recovery.

Covers T7 and T-F2 from plan/sprints/mcp_server_self_healing.md §Test Strategy:

- T7: drain-ordering — with an active drain holding backend_lock, the watcher
  does NOT call shutdown_backend() until the lock is released.
- T-F2: failed-exec recovery — _reexec returning without exec'ing triggers F2:
  backend is re-opened, server keeps serving (no deadlock; drain loop kept alive).

Guards plan/sprints/mcp_server_self_healing.md §D5 / §Step 2.2.
"""

from __future__ import annotations

from unittest.mock import patch

import anyio
import pytest

import sqlcg.server.server as srv
import sqlcg.server.tools as _tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_writer_queue():
    """Return a minimal WriterQueue with an async lock and empty _pending."""
    from sqlcg.server.writer import WriterQueue

    wq = WriterQueue()
    return wq


# ---------------------------------------------------------------------------
# T7 — drain ordering: shutdown_backend called only after backend_lock released
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_drain_ordering_shutdown_after_lock_release():
    """shutdown_backend is NOT called while backend_lock is held by a drain.

    Simulates an active drain holding backend_lock while _self_heal_watcher
    waits; asserts that shutdown_backend fires only after the drain releases
    the lock (step 3 → step 5 ordering in D5).

    Guards plan/sprints/mcp_server_self_healing.md §D5 / T7.
    """
    from pathlib import Path

    call_order: list[str] = []

    # Shared lock to simulate a drain holding backend_lock.
    backend_lock = anyio.Lock()
    shutdown_requested = anyio.Event()
    self_heal_event = anyio.Event()
    db_path = Path("/tmp/test_ordering.db")

    writer_queue = _make_writer_queue()

    def fake_shutdown():
        call_order.append("shutdown_backend")

    # Override init_backend (F2 path) to record and succeed.
    def fake_init(path=None):
        call_order.append("init_backend_f2")

    # Override cleanup_control_files.
    def fake_cleanup(p=None):
        call_order.append("cleanup_control_files")

    # Override _reexec to return without exec'ing (F2 path).
    def fake_reexec():
        call_order.append("reexec_called")
        # Returns without exec'ing → triggers F2 recovery.

    async def fake_drain():
        """Simulate a drain holding backend_lock for a short time, then releasing."""
        async with backend_lock:
            call_order.append("drain_acquired_lock")
            await anyio.sleep(0.05)  # hold the lock briefly
            call_order.append("drain_released_lock")

    async def trigger():
        """Wait a moment then fire the self-heal event."""
        await anyio.sleep(0.01)
        self_heal_event.set()

    with (
        patch.object(_tools, "shutdown_backend", fake_shutdown),
        patch.object(_tools, "init_backend", fake_init),
        patch.object(srv, "_reexec", fake_reexec),
        patch("sqlcg.server.control.cleanup_control_files", fake_cleanup),
    ):
        async with anyio.create_task_group() as tg:
            tg.start_soon(fake_drain)
            tg.start_soon(trigger)
            tg.start_soon(
                srv._self_heal_watcher,
                self_heal_event,
                db_path,
                backend_lock,
                shutdown_requested,
                writer_queue,
            )

    # Verify ordering: drain released the lock before shutdown_backend was called.
    assert "drain_acquired_lock" in call_order, "Drain must have acquired lock"
    assert "drain_released_lock" in call_order, "Drain must have released lock"
    assert "shutdown_backend" in call_order, "shutdown_backend must have been called"

    drain_rel_idx = call_order.index("drain_released_lock")
    shutdown_idx = call_order.index("shutdown_backend")
    assert shutdown_idx > drain_rel_idx, (
        f"shutdown_backend (pos {shutdown_idx}) must come AFTER drain released lock "
        f"(pos {drain_rel_idx}); order: {call_order}"
    )


# ---------------------------------------------------------------------------
# T-F2 — failed-exec recovery: server re-opens backend without deadlock
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_failed_exec_recovery_reopens_backend_outside_lock():
    """When _reexec returns without exec'ing, F2 path re-opens backend outside backend_lock.

    Verifies:
    1. _reexec is called.
    2. init_backend is called after _reexec returns (F2 recovery).
    3. init_backend call happens AFTER the backend_lock is released (no deadlock).

    Guards plan/sprints/mcp_server_self_healing.md §F2 / W2 / T-F2.
    """
    from pathlib import Path

    call_order: list[str] = []
    lock_held_at_init = {"held": None}

    backend_lock = anyio.Lock()
    shutdown_requested = anyio.Event()
    self_heal_event = anyio.Event()
    db_path = Path("/tmp/test_f2_recovery.db")
    writer_queue = _make_writer_queue()

    def fake_shutdown():
        call_order.append("shutdown_backend")

    def fake_init(path=None):
        # Record whether the backend_lock is held when init_backend is called.
        # We try to acquire the lock with nowait; if it raises, it's held.
        lock_held_at_init["held"] = backend_lock.locked()
        call_order.append("init_backend")

    def fake_reexec():
        call_order.append("reexec_called")
        # Intentionally returns without exec'ing to trigger F2.

    def fake_cleanup(p=None):
        call_order.append("cleanup_control_files")

    # Fire the event immediately.
    self_heal_event.set()

    with (
        patch.object(_tools, "shutdown_backend", fake_shutdown),
        patch.object(_tools, "init_backend", fake_init),
        patch.object(srv, "_reexec", fake_reexec),
        patch("sqlcg.server.control.cleanup_control_files", fake_cleanup),
    ):
        await srv._self_heal_watcher(
            self_heal_event,
            db_path,
            backend_lock,
            shutdown_requested,
            writer_queue,
        )

    assert "reexec_called" in call_order, "_reexec must be called"
    assert "init_backend" in call_order, "init_backend must be called (F2 recovery)"

    reexec_idx = call_order.index("reexec_called")
    init_idx = call_order.index("init_backend")
    assert init_idx > reexec_idx, (
        f"init_backend (F2 recovery) must happen AFTER _reexec returns; order: {call_order}"
    )

    # W2: backend_lock must NOT be held when init_backend is called.
    assert lock_held_at_init["held"] is False, (
        "W2 violation: backend_lock was held when init_backend (F2) was called — "
        "this would deadlock the drain loop"
    )


# ---------------------------------------------------------------------------
# A6 — waiter relay: pending wait=true waiters receive terminal frame
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_waiter_relay_sends_terminal_frame_to_pending_waiters():
    """A6: pending wait=true waiters receive a terminal frame during self-heal.

    Guards plan/sprints/mcp_server_self_healing.md §A6.
    """
    from pathlib import Path

    from sqlcg.server.writer import WriterRequest

    backend_lock = anyio.Lock()
    shutdown_requested = anyio.Event()
    self_heal_event = anyio.Event()
    db_path = Path("/tmp/test_a6_relay.db")
    writer_queue = _make_writer_queue()

    # Add a fake pending request with a waiter channel.
    send_ch, recv_ch = anyio.create_memory_object_stream(max_buffer_size=8)
    fake_req = WriterRequest(
        op="index",
        root="/fake/root",
        dialect=None,
        from_sha=None,
        to_sha=None,
        requested_by="test",
    )
    fake_req._waiters.append(send_ch)
    writer_queue._pending.append(fake_req)

    # Fire the event immediately.
    self_heal_event.set()

    received_frames: list[dict] = []

    async def collect_frames():
        async with recv_ch:
            async for frame in recv_ch:
                received_frames.append(frame)
                if frame.get("done"):
                    break

    def fake_shutdown():
        pass

    def fake_init(path=None):
        pass

    def fake_reexec():
        pass  # F2 path

    def fake_cleanup(p=None):
        pass

    with (
        patch.object(_tools, "shutdown_backend", fake_shutdown),
        patch.object(_tools, "init_backend", fake_init),
        patch.object(srv, "_reexec", fake_reexec),
        patch("sqlcg.server.control.cleanup_control_files", fake_cleanup),
    ):
        async with anyio.create_task_group() as tg:
            tg.start_soon(collect_frames)
            await srv._self_heal_watcher(
                self_heal_event,
                db_path,
                backend_lock,
                shutdown_requested,
                writer_queue,
            )

    assert len(received_frames) >= 1, "Expected at least one terminal frame for the waiter"
    terminal = received_frames[-1]
    assert terminal.get("done") is True, f"Terminal frame must have done=True; got: {terminal}"
    assert terminal.get("ok") is False, (
        f"Terminal frame must have ok=False for self-heal; got: {terminal}"
    )
    assert "self-healing" in (terminal.get("error") or ""), (
        f"Terminal frame error must mention self-healing; got: {terminal}"
    )
