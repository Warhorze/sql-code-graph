"""Guard: _flush_row_batch must call upsert_*_bulk once per batch, not once per file.

Named regression (v1.1.1): per-file flush fires ~13,400 execute() for 1,340 files
(10/file); per-batch flush fires ~270 (10/batch). Guard: bulk-call count must be flat
across N vs 2N files in a single batch.

This test file is a FAILING acceptance test — it will fail (or skip on the ImportError
guard) until PR-1 (F1) is implemented. Once _flush_row_batch exists and _flush_batch
accumulates rows across the batch before flushing, both assertions become green.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

try:
    from sqlcg.indexer.indexer import (  # type: ignore[attr-defined]  # noqa: F401
        Indexer,
        _flush_row_batch,
    )

    _FLUSH_ROW_BATCH_EXISTS = True
except ImportError:
    _flush_row_batch = None  # type: ignore[assignment]
    _FLUSH_ROW_BATCH_EXISTS = False

import pytest

from sqlcg.indexer.indexer import Indexer


def _make_mock_db() -> MagicMock:
    """Minimal mock db that satisfies index_repo without a real KuzuDB."""
    mock_db = MagicMock()
    mock_db.run_read = MagicMock(return_value=[])
    mock_db.transaction = MagicMock()
    mock_db.transaction.return_value.__enter__ = MagicMock(return_value=None)
    mock_db.transaction.return_value.__exit__ = MagicMock(return_value=False)
    return mock_db


def _count_bulk_calls(mock_db: MagicMock) -> int:
    """Total upsert_nodes_bulk + upsert_edges_bulk call count."""
    return mock_db.upsert_nodes_bulk.call_count + mock_db.upsert_edges_bulk.call_count


def _index_n_files(n: int, batch_size: int) -> MagicMock:
    """Create a temp dir with n SQL files, index them with the given batch_size."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(n):
            (Path(tmpdir) / f"t{i}.sql").write_text(f"SELECT id, name FROM customers_{i}")
        mock_db = _make_mock_db()
        indexer = Indexer()
        indexer.index_repo(
            Path(tmpdir),
            dialect=None,
            db=mock_db,
            batch_size=batch_size,
            use_git=False,
        )
    return mock_db


def test_v1_1_1_F1_flat_bulk_call_count_N_vs_2N_in_single_batch():
    """After F1: bulk-call count is flat (same) for N and 2N files in one batch.

    Before F1 (per-file flush): count scales with N (10*N calls for N files).
    After F1 (per-batch flush): count = 10 per batch regardless of N files in it.

    This test MUST FAIL before F1 is implemented.
    """
    N = 4
    count_n = _count_bulk_calls(_index_n_files(N, batch_size=N * 2))
    count_2n = _count_bulk_calls(_index_n_files(N * 2, batch_size=N * 4))

    assert count_n == count_2n, (
        f"Bulk call count is NOT flat: {N} files → {count_n} calls, "
        f"{N * 2} files → {count_2n} calls. "
        "Expected equal counts (10 per batch) after F1 batch-flush refactor. "
        "If count_2n == 2*count_n, _flush_batch still flushes per-file."
    )
    assert count_n <= 12, (
        f"Bulk call count for a single batch is {count_n} — expected <= 12 "
        "(4 upsert_nodes_bulk + 6 upsert_edges_bulk = 10; slack 2 for empty groups)."
    )


def test_v1_1_1_F1_batch_size_1_scales_linearly_non_vacuous_check():
    """With batch_size=1, bulk-call count scales with N (proves the spy is live).

    This is the non-vacuous companion to the flat test above. If the spy doesn't
    track calls at all, both tests would pass incorrectly. With batch_size=1,
    each file is a separate batch, so count(N) should be roughly N × count(1).
    """
    N = 3
    count_n = _count_bulk_calls(_index_n_files(N, batch_size=1))
    count_1 = _count_bulk_calls(_index_n_files(1, batch_size=1))

    # With per-file flush (batch_size=1), N files → ~N * (calls per file)
    # We don't assert an exact ratio because some calls are skipped for empty groups,
    # but N files should produce more calls than 1 file.
    assert count_n > count_1, (
        f"batch_size=1: {N} files produced {count_n} bulk calls, "
        f"1 file produced {count_1} calls. Expected count_n > count_1."
    )
    assert count_n >= N, (
        f"batch_size=1: {N} files produced only {count_n} bulk calls — "
        "expected at least N calls (minimum: 1 upsert_nodes_bulk per file for FILE node)."
    )


def test_v1_1_1_F1_bulk_not_per_row_invariant_preserved():
    """After F1: upsert_node / upsert_edge are never called (bulk-not-per-row preserved)."""
    mock_db = _index_n_files(4, batch_size=8)
    assert not mock_db.upsert_node.called, (
        "upsert_node was called — F1 must use upsert_nodes_bulk exclusively."
    )
    assert not mock_db.upsert_edge.called, (
        "upsert_edge was called — F1 must use upsert_edges_bulk exclusively."
    )


def test_v1_1_1_F1_flush_row_batch_method_exists():
    """After F1: _flush_row_batch must be a method on Indexer (or importable from indexer.py)."""
    if not _FLUSH_ROW_BATCH_EXISTS:
        pytest.skip("_flush_row_batch not yet implemented (T-F1 not landed)")
    assert callable(_flush_row_batch), "_flush_row_batch must be callable"


def test_usage_rows_ride_existing_bulk_calls_no_extra_execute():
    """Usage-derived catalog rows ride the existing HAS_COLUMN bulk call.

    PR 3 (usage-derived catalog): when _flush_row_batch produces usage HAS_COLUMN
    rows, they must be appended to the existing has_column_edges list before the
    single upsert_edges_bulk(HAS_COLUMN) call — not trigger an extra execute().
    Total bulk call count must remain flat (= 10) regardless of whether usage rows
    are present.

    Guards the batch-invariant: bulk entry points called once per batch.
    Plan doc: plan/sprints/graph_health_catalog_and_metrics.md §5
    """
    # An ETL file with a schema-qualified source table generates usage rows.
    # A DDL-only file does not. Both must produce the same bulk-call count.
    N = 4

    # Files with schema-qualified sources produce usage HAS_COLUMN rows
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(N):
            (Path(tmpdir) / f"t{i}.sql").write_text(
                f"INSERT INTO mydb.sch.dst_{i} SELECT col_a, col_b FROM mydb.sch.src_{i}"
            )
        mock_db = _make_mock_db()
        indexer = Indexer()
        indexer.index_repo(
            Path(tmpdir),
            dialect=None,
            db=mock_db,
            batch_size=N * 2,
            use_git=False,
        )
    count_with_usage = _count_bulk_calls(mock_db)

    # Files with no sources (SELECT constant only) produce no usage rows
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(N):
            (Path(tmpdir) / f"t{i}.sql").write_text(f"SELECT {i} AS val")
        mock_db_no_usage = _make_mock_db()
        indexer2 = Indexer()
        indexer2.index_repo(
            Path(tmpdir),
            dialect=None,
            db=mock_db_no_usage,
            batch_size=N * 2,
            use_git=False,
        )
    count_no_usage = _count_bulk_calls(mock_db_no_usage)

    # Both must produce the same call count — usage rows ride along, never add calls
    assert count_with_usage == count_no_usage, (
        f"Usage rows must not add extra bulk calls: "
        f"with_usage={count_with_usage}, no_usage={count_no_usage}. "
        "If count_with_usage > count_no_usage, usage harvest added extra execute() calls."
    )
    assert count_with_usage <= 12, (
        f"Bulk call count with usage rows is {count_with_usage} — expected <= 12 "
        "(4 upsert_nodes_bulk + 6 upsert_edges_bulk = 10; slack 2 for empty groups)"
    )
