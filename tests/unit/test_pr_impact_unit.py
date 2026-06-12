"""Unit tests for the PR-impact detector helpers (PR 2 — v1.23.0).

Tests _capture_producer_set, _diff_lost_producers (genuine loss + rename
classification), and the get_pr_impact mismatch path. Uses mock backends —
no real DuckDB or git required.

Guards plan/sprints/unfilled_table_impact.md PR 2 Unit test strategy.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlcg.server.tools import (
    _RENAME_OVERLAP_THRESHOLD,
    _capture_producer_set,
    _diff_lost_producers,
    _jaccard,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(
    producer_tables: list[str] | None = None,
    adjacency_rows: list[dict] | None = None,
    kind_rows: list[dict] | None = None,
    columns_map: dict[str, list[str]] | None = None,
    target_tables_for_file: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Build a mock GraphBackend for PR 2 helpers."""
    db = MagicMock()
    producers = producer_tables or []
    adj = adjacency_rows or []
    kinds = kind_rows or []
    cols = columns_map or {}
    tgt_by_file = target_tables_for_file or {}

    def _run_read(query: str, params: dict):
        # GET_PRODUCER_TABLES
        if "producer_table" in query and "SqlQuery" in query and "SELECTS_FROM" not in query:
            return [{"producer_table": t} for t in producers]
        # GET_TABLE_READS_ADJACENCY (adjacency for consumer lookup)
        if "SELECTS_FROM" in query and "STAR_SOURCE" in query and "UNION" in query:
            return adj
        # GET_TABLE_KINDS_BATCH
        if '"SqlTable"' in query and "ANY(?)" in query and "kind" in query:
            requested = params.get("table_qualifieds", [])
            return [r for r in kinds if r["table_qualified"] in requested]
        # GET_COLUMNS_FOR_TABLE (returns col_id rows)
        if "HAS_COLUMN" in query:
            tq = params.get("table_qualified", "")
            names = cols.get(tq, [])
            # Return col_id = table.col_name format
            return [{"col_id": f"{tq}.{c}"} for c in names]
        # GET_TARGET_TABLES_FOR_FILE
        if "QUERY_DEFINED_IN" in query:
            fp = params.get("file_path", "")
            tbls = tgt_by_file.get(fp, [])
            return [{"table_qualified": t} for t in tbls]
        return []

    db.run_read.side_effect = _run_read
    return db


# ---------------------------------------------------------------------------
# _jaccard helper
# ---------------------------------------------------------------------------


def test_jaccard_identical_sets_returns_1():
    """Jaccard of identical non-empty sets is 1.0."""
    assert _jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0


def test_jaccard_disjoint_sets_returns_0():
    """Jaccard of disjoint sets is 0.0."""
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_both_empty_returns_0():
    """Jaccard of two empty sets returns 0.0 (no division by zero)."""
    assert _jaccard(set(), set()) == 0.0


def test_jaccard_partial_overlap():
    """Jaccard with partial overlap: |intersection| / |union|."""
    j = _jaccard({"a", "b", "c"}, {"b", "c", "d"})
    # intersection = {b,c} = 2; union = {a,b,c,d} = 4; 2/4 = 0.5
    assert abs(j - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# _capture_producer_set
# ---------------------------------------------------------------------------


def test_capture_producer_set_returns_producer_tables():
    """_capture_producer_set returns the lowercased set of producer tables.

    Guards plan/sprints/unfilled_table_impact.md PR 2 Step 2.2.
    """
    db = _make_db(producer_tables=["da.foo", "ba.bar", "da.baz"])
    result = _capture_producer_set(db)
    assert result == {"da.foo", "ba.bar", "da.baz"}


def test_capture_producer_set_empty_graph():
    """_capture_producer_set returns empty set when no queries write any table.

    Guards plan/sprints/unfilled_table_impact.md PR 2 Step 2.2.
    """
    db = _make_db(producer_tables=[])
    result = _capture_producer_set(db)
    assert result == set()


# ---------------------------------------------------------------------------
# _diff_lost_producers — genuine loss
# ---------------------------------------------------------------------------


def test_diff_lost_producers_genuine_loss_returns_orphaned_table():
    """A table in before but not after (no matching rename) is a genuine lost producer.

    Guards plan/sprints/unfilled_table_impact.md PR 2 Step 2.2 — basic detection.
    """
    # da.orphan was produced at base; nothing produces it at head.
    before = {"da.orphan", "da.survivor"}
    after = {"da.survivor"}
    # No kind_rows → _exclude_synthetic_tables returns everything.
    db = _make_db(
        producer_tables=list(after),
        columns_map={"da.orphan": [], "da.survivor": []},
    )
    genuine, attribution, renamed = _diff_lost_producers(before, after, db, None, None)
    assert "da.orphan" in genuine
    assert "da.survivor" not in genuine
    assert renamed == {}


def test_diff_lost_producers_still_produced_not_reported():
    """A table present in both before and after is NOT reported as lost.

    Guards plan/sprints/unfilled_table_impact.md PR 2 acceptance criteria.
    """
    before = {"da.table_a", "da.table_b"}
    after = {"da.table_a", "da.table_b"}
    db = _make_db(columns_map={"da.table_a": [], "da.table_b": []})
    genuine, attribution, renamed = _diff_lost_producers(before, after, db, None, None)
    assert genuine == []
    assert renamed == {}


# ---------------------------------------------------------------------------
# _diff_lost_producers — in-file table rename (Jaccard ≥ threshold on both axes)
# ---------------------------------------------------------------------------


def test_diff_lost_producers_infile_rename_both_jaccard_above_threshold_classified_as_rename():
    """In-file table rename: da.foo → da.bar — detected STRUCTURALLY (Jaccard 1.0 both axes).

    No git rename signal. da.foo in lost, da.bar in new, same columns and same
    downstream consumers. BOTH Jaccard ≥ _RENAME_OVERLAP_THRESHOLD.
    Asserts renamed_tables["da.foo"] == "da.bar" and da.foo NOT in genuine lost.

    Guards plan/sprints/unfilled_table_impact.md PR 2 rename handling.
    """
    before = {"da.foo", "da.other"}
    after = {"da.bar", "da.other"}

    # Both tables share the same column names and the same consumer adjacency.
    shared_cols = ["id", "amount", "created_at"]
    # Consumer: ba.consumer reads both (same consumer set → Jaccard 1.0)
    adjacency_rows = [
        {"source_table": "da.foo", "dest_table": "ba.consumer"},
        {"source_table": "da.bar", "dest_table": "ba.consumer"},
    ]
    db = _make_db(
        producer_tables=list(after),
        adjacency_rows=adjacency_rows,
        columns_map={
            "da.foo": shared_cols,
            "da.bar": shared_cols,
            "da.other": ["x"],
        },
    )
    genuine, attribution, renamed = _diff_lost_producers(before, after, db, None, None)

    assert renamed.get("da.foo") == "da.bar", (
        "da.foo should be classified as renamed to da.bar (both Jaccard 1.0 ≥ 0.6)"
    )
    assert "da.foo" not in genuine, "Renamed table must NOT appear in genuine lost set"


def test_diff_lost_producers_below_threshold_one_axis_stays_lost():
    """A candidate where only one Jaccard axis clears threshold is NOT a rename.

    Pins the AND-of-both-axes rule and _RENAME_OVERLAP_THRESHOLD.
    A lost L whose only candidate N clears column-Jaccard but NOT consumer-Jaccard
    stays in the genuine lost set.

    Guards plan/sprints/unfilled_table_impact.md PR 2 rename handling below-threshold.
    """
    before = {"da.foo"}
    after = {"da.bar"}

    # Columns: same → column Jaccard = 1.0 (above threshold).
    # Consumers: different → consumer Jaccard = 0.0 (below threshold).
    adjacency_rows = [
        {"source_table": "da.foo", "dest_table": "ba.consumer_old"},
        {"source_table": "da.bar", "dest_table": "ba.consumer_new"},
    ]
    db = _make_db(
        producer_tables=["da.bar"],
        adjacency_rows=adjacency_rows,
        columns_map={
            "da.foo": ["id", "amount"],
            "da.bar": ["id", "amount"],
        },
    )
    genuine, attribution, renamed = _diff_lost_producers(before, after, db, None, None)

    # Column Jaccard = 1.0 (above), Consumer Jaccard = 0.0 (below).
    # AND-of-both-axes: NOT a rename.
    assert "da.foo" in genuine, (
        "da.foo should stay in genuine lost (consumer Jaccard below threshold)"
    )
    assert renamed == {}, "No rename should be classified when one axis is below threshold"


def test_rename_overlap_threshold_value():
    """_RENAME_OVERLAP_THRESHOLD is 0.6 — the named constant from the plan.

    Guards plan/sprints/unfilled_table_impact.md § named constant.
    """
    assert _RENAME_OVERLAP_THRESHOLD == 0.6


# ---------------------------------------------------------------------------
# get_pr_impact — mismatch path (graph not indexed at base_ref)
# ---------------------------------------------------------------------------


def test_get_pr_impact_mismatch_returns_hint_no_resync():
    """When the graph is indexed at a different SHA than base_ref resolves to,
    get_pr_impact returns a hint and does NOT invoke resync_changed.

    Guards plan/sprints/unfilled_table_impact.md PR 2 Step 2.3 O2 contract.
    """
    from sqlcg.server.tools import get_pr_impact

    mock_db = MagicMock()
    mock_db.get_indexed_sha.return_value = "aaa111bbb222ccc333ddd444eee555fff66677788"
    mock_db.__enter__ = lambda self: self
    mock_db.__exit__ = MagicMock(return_value=False)

    # _resolve_git_ref returns a different SHA than what's indexed.
    with (
        patch("sqlcg.server.tools._open_backend") as mock_open,
        patch("sqlcg.server.tools._assert_indexed"),
        patch("sqlcg.server.tools._indexed_root", return_value=None),
        patch(
            "sqlcg.server.tools._resolve_git_ref",
            return_value="bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
        ),
        patch("sqlcg.server.tools.Indexer") as mock_indexer_cls,
    ):
        mock_open.return_value.__enter__ = lambda ctx: mock_db
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = get_pr_impact(base_ref="feature-branch")

    # resync_changed must NOT have been invoked.
    mock_indexer_cls.return_value.resync_changed.assert_not_called()

    assert result.hint is not None, "Should have a diagnostic hint"
    assert "base_ref" in result.hint or "indexed" in result.hint or "index" in result.hint.lower()
    assert result.lost_producer_tables == []
    assert result.renamed_tables == {}
