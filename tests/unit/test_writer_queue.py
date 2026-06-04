"""Unit tests for WriterQueue coalescing rules and escalation retry.

Tests here are fully deterministic — no DB, no sockets, no wall-clock waits.
Queue tests run under @pytest.mark.anyio because enqueue/pop_next acquire the
anyio.Lock inside WriterQueue (W4).  coalesce_view() is synchronous and is
asserted directly.

Escalation tests inject a fake opener to simulate lock errors without needing
a real DB process holding the lock — the retry budget override is passed via
the retry_budget_s parameter, never a module-global patch (parallel-test-safe).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.server.writer import (
    COALESCE_COLLAPSED_INTO_PENDING_REINDEX,
    COALESCE_REINDEX_DROPPED_INDEX_PENDING,
    COALESCE_SUPERSEDED_BY_INDEX,
    EscalationLockError,
    WriterQueue,
    WriterRequest,
    escalate_to_rw,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(op="reindex", root="/tmp/repo", from_sha="abc", to_sha="def") -> WriterRequest:
    return WriterRequest(
        op=op,
        root=root,
        dialect=None,
        from_sha=from_sha,
        to_sha=to_sha,
        requested_by="cli",
    )


def _index_req(root="/tmp/repo") -> WriterRequest:
    return WriterRequest(
        op="index",
        root=root,
        dialect=None,
        from_sha=None,
        to_sha=None,
        requested_by="cli",
    )


# ---------------------------------------------------------------------------
# Coalescing rules (anyio-marked because enqueue acquires anyio.Lock)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rule1_index_supersedes_pending_reindexes():
    """Rule 1: full index supersedes all pending requests.

    Note: two consecutive reindexes collapse (Rule 2), so after both are
    enqueued the pending list has 1 reindex.  When the index arrives it
    supersedes that 1 reindex.
    """
    q = WriterQueue()
    await q.enqueue(_req(op="reindex"))
    await q.enqueue(_req(op="reindex"))
    # Rule 2 collapsed the second reindex — 1 pending reindex.
    assert len(q._pending) == 1

    await q.enqueue(_index_req())

    assert len(q._pending) == 1
    assert q._pending[0].op == "index"

    view = q.coalesce_view()
    # 1 reindex was collapsed (Rule 2) + 1 reindex was superseded (Rule 1) = 2 total
    assert view["coalesced_since_start"] == 2
    # At least 1 SUPERSEDED_BY_INDEX event (the remaining pending reindex).
    assert view["coalesced_by_reason"][COALESCE_SUPERSEDED_BY_INDEX] >= 1
    assert view["last_coalesce_reason"] == COALESCE_SUPERSEDED_BY_INDEX


@pytest.mark.anyio
async def test_rule2_reindex_collapses_into_pending_reindex():
    """Rule 2: a reindex collapses into an already-pending reindex."""
    q = WriterQueue()
    await q.enqueue(_req(op="reindex"))
    await q.enqueue(_req(op="reindex"))

    assert len(q._pending) == 1  # second was coalesced
    view = q.coalesce_view()
    assert view["coalesced_since_start"] == 1
    assert view["coalesced_by_reason"][COALESCE_COLLAPSED_INTO_PENDING_REINDEX] == 1


@pytest.mark.anyio
async def test_rule3_reindex_behind_index_is_dropped():
    """Rule 3: a reindex arriving behind a pending index is dropped."""
    q = WriterQueue()
    await q.enqueue(_index_req())
    await q.enqueue(_req(op="reindex"))

    assert len(q._pending) == 1  # reindex was dropped
    assert q._pending[0].op == "index"
    view = q.coalesce_view()
    assert view["coalesced_since_start"] == 1
    assert view["coalesced_by_reason"][COALESCE_REINDEX_DROPPED_INDEX_PENDING] == 1


@pytest.mark.anyio
async def test_active_request_is_never_mutated_by_coalescing():
    """Active request (currently draining) is exempt from all coalescing rules."""
    q = WriterQueue()
    await q.enqueue(_req(op="reindex"))
    active = await q.pop_next()
    assert active is not None
    assert q._active is active

    # A new index should coalesce pending but not touch the active.
    await q.enqueue(_index_req())
    assert q._active is active
    assert q._active.op == "reindex"


@pytest.mark.anyio
async def test_coalesce_view_sum_equals_total():
    """coalesced_since_start must equal the sum of all per-reason counts."""
    q = WriterQueue()
    await q.enqueue(_req())
    await q.enqueue(_req())  # collapse 1
    await q.enqueue(_req())  # collapse 2
    await q.enqueue(_index_req())  # supersedes the pending reindex → 1 more

    view = q.coalesce_view()
    total = sum(view["coalesced_by_reason"].values())
    assert view["coalesced_since_start"] == total
    assert total >= 3


@pytest.mark.anyio
async def test_pop_next_returns_none_on_empty_queue():
    """pop_next on an empty queue returns None without raising."""
    q = WriterQueue()
    result = await q.pop_next()
    assert result is None


@pytest.mark.anyio
async def test_coalesce_view_all_reason_keys_present_when_empty():
    """coalesced_by_reason always contains all three reason keys (zero-filled)."""
    q = WriterQueue()
    view = q.coalesce_view()
    assert COALESCE_SUPERSEDED_BY_INDEX in view["coalesced_by_reason"]
    assert COALESCE_COLLAPSED_INTO_PENDING_REINDEX in view["coalesced_by_reason"]
    assert COALESCE_REINDEX_DROPPED_INDEX_PENDING in view["coalesced_by_reason"]
    assert view["coalesced_since_start"] == 0
    assert view["last_coalesce_at"] is None
    assert view["last_coalesce_reason"] is None


# ---------------------------------------------------------------------------
# Escalation retry (unit, deterministic, injectable opener)
# ---------------------------------------------------------------------------


def _lock_error() -> RuntimeError:
    return RuntimeError("Could not set lock on file: /tmp/test.db")


def test_escalate_succeeds_within_budget_after_k_failures():
    """escalate_to_rw succeeds for K failures within the retry budget."""
    call_count = [0]
    mock_backend = MagicMock()
    mock_backend.read_only = False

    def fake_opener(path, read_only=False):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise _lock_error()
        return mock_backend

    with patch("sqlcg.server.tools._backend", None):
        import sqlcg.server.tools as _tools

        _tools._backend = None
        result = escalate_to_rw(
            "/tmp/test.db",
            current=None,
            opener=fake_opener,
            retry_budget_s=5.0,
        )

    assert result is mock_backend
    assert call_count[0] == 3  # 2 failures then success


def test_escalate_raises_escalation_lock_error_when_budget_exhausted():
    """escalate_to_rw raises EscalationLockError with side-DB workaround text on exhaustion."""

    def always_fail(path, read_only=False):
        raise _lock_error()

    with patch("sqlcg.server.tools._backend", None), patch(
        "sqlcg.core.kuzu_backend.find_lock_holder", return_value="PID 9999"
    ):
        import sqlcg.server.tools as _tools

        _tools._backend = None
        with pytest.raises(EscalationLockError) as exc_info:
            escalate_to_rw(
                "/tmp/test.db",
                current=None,
                opener=always_fail,
                retry_budget_s=0.05,  # tiny budget so the test finishes fast
            )

    msg = str(exc_info.value)
    assert "Could not acquire the write lock" in msg
    assert "SQLCG_DB_PATH" in msg  # side-DB workaround present
    assert "9999" in msg or "PID" in msg  # PID hint present


def test_escalate_emits_error_log_before_raising(caplog):
    """escalate_to_rw emits exactly one ERROR log with 'escalation failed:' prefix."""

    def always_fail(path, read_only=False):
        raise _lock_error()

    with caplog.at_level(logging.ERROR, logger="sqlcg.server.writer"), patch(
        "sqlcg.server.tools._backend", None
    ), patch("sqlcg.core.kuzu_backend.find_lock_holder", return_value="PID 1234"):
        import sqlcg.server.tools as _tools

        _tools._backend = None
        with pytest.raises(EscalationLockError):
            escalate_to_rw(
                "/tmp/test.db",
                current=None,
                opener=always_fail,
                retry_budget_s=0.05,
            )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1, (
        f"Expected exactly 1 ERROR log, got {len(error_records)}: "
        f"{[r.message for r in error_records]}"
    )
    assert "escalation failed:" in error_records[0].message
    assert "1234" in error_records[0].message or "PID" in error_records[0].message


def test_escalate_non_lock_error_propagates_immediately():
    """Non-lock errors from the opener are NOT retried — they propagate immediately."""
    call_count = [0]

    def one_non_disk_error(path, read_only=False):
        call_count[0] += 1
        raise RuntimeError("Disk full — cannot open database at all")

    with patch("sqlcg.server.tools._backend", None):
        import sqlcg.server.tools as _tools

        _tools._backend = None
        with pytest.raises(RuntimeError, match="Disk full"):
            escalate_to_rw(
                "/tmp/test.db",
                current=None,
                opener=one_non_disk_error,
                retry_budget_s=5.0,
            )

    assert call_count[0] == 1  # no retries


# ---------------------------------------------------------------------------
# Metrics injection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_queue_records_enqueue_to_metrics():
    """record_queue_event is called with 'enqueued' event after enqueue."""
    mock_metrics = MagicMock()
    q = WriterQueue(metrics=mock_metrics)

    await q.enqueue(_req(op="reindex"))

    mock_metrics.record_queue_event.assert_called_once()
    call_kwargs = mock_metrics.record_queue_event.call_args
    assert call_kwargs[0][0] == "enqueued"


@pytest.mark.anyio
async def test_queue_records_coalesce_to_metrics():
    """record_queue_event is called with 'coalesced' event and reason on coalesce."""
    mock_metrics = MagicMock()
    q = WriterQueue(metrics=mock_metrics)

    await q.enqueue(_req(op="reindex"))
    await q.enqueue(_req(op="reindex"))  # triggers collapse

    coalesced_calls = [
        c
        for c in mock_metrics.record_queue_event.call_args_list
        if c[0][0] == "coalesced"
    ]
    assert len(coalesced_calls) == 1
    assert coalesced_calls[0][1].get("reason") == COALESCE_COLLAPSED_INTO_PENDING_REINDEX


@pytest.mark.anyio
async def test_queue_none_metrics_does_not_raise():
    """WriterQueue with metrics=None works fine — no metrics calls."""
    q = WriterQueue(metrics=None)
    await q.enqueue(_req())
    await q.enqueue(_req())  # coalesce
    assert q._pending  # observable queue state
