"""Unit tests for WriterQueue coalescing rules.

Tests here are fully deterministic — no DB, no sockets, no wall-clock waits.
Queue tests run under @pytest.mark.anyio because enqueue/pop_next acquire the
anyio.Lock inside WriterQueue (W4).  coalesce_view() is synchronous and is
asserted directly.

The RO→RW escalation machinery (escalate_to_rw, EscalationLockError,
de_escalate_to_ro) was deleted in Phase 4 of the DuckDB migration — DuckDB
holds a single R/W handle for the process lifetime, so no mode-switching is
needed.  The corresponding tests were removed alongside the deleted code.
"""

from __future__ import annotations

import pytest

from sqlcg.server.writer import (
    COALESCE_COLLAPSED_INTO_PENDING_REINDEX,
    COALESCE_REINDEX_DROPPED_INDEX_PENDING,
    COALESCE_SUPERSEDED_BY_INDEX,
    WriterQueue,
    WriterRequest,
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
# Metrics injection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_queue_records_enqueue_to_metrics():
    """record_queue_event is called with 'enqueued' event after enqueue."""
    from unittest.mock import MagicMock

    mock_metrics = MagicMock()
    q = WriterQueue(metrics=mock_metrics)

    await q.enqueue(_req(op="reindex"))

    mock_metrics.record_queue_event.assert_called_once()
    call_kwargs = mock_metrics.record_queue_event.call_args
    assert call_kwargs[0][0] == "enqueued"


@pytest.mark.anyio
async def test_queue_records_coalesce_to_metrics():
    """record_queue_event is called with 'coalesced' event and reason on coalesce."""
    from unittest.mock import MagicMock

    mock_metrics = MagicMock()
    q = WriterQueue(metrics=mock_metrics)

    await q.enqueue(_req(op="reindex"))
    await q.enqueue(_req(op="reindex"))  # triggers collapse

    coalesced_calls = [
        c for c in mock_metrics.record_queue_event.call_args_list if c[0][0] == "coalesced"
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
