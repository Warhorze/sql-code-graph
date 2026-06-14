"""Unit tests for the PR-impact detector helpers (PR 2 — v1.23.0, PR-F — v1.28.3).

Tests _capture_producer_set, _diff_lost_producers (genuine loss + rename
classification), the get_pr_impact mismatch path, and the PR-F self-healing
reindex behaviour.  Uses mock backends — no real DuckDB or git required.

Guards plan/sprints/unfilled_table_impact.md PR 2 Unit test strategy and
plan/sprints/sprint_backlog_q2_bugfix.md §PR-F.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.server.tools import (
    _RENAME_OVERLAP_THRESHOLD,
    _capture_producer_set,
    _diff_lost_producers,
    _jaccard,
    _reindex_to_sha,
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


def test_get_pr_impact_mismatch_self_heal_fails_returns_manually_hint():
    """When the graph SHA mismatches and the self-heal reindex fails,
    _compute_pr_impact returns the 'manually' error hint.

    Previously (pre-PR-F) this returned immediately without reindexing.
    Now it attempts self-heal; when that fails it returns the 'manually' hint.

    Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F (fail path).
    """
    from sqlcg.server.tools import get_pr_impact

    stale_sha = "aaa111bbb222ccc333ddd444eee555fff66677788"
    base_sha = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"

    mock_db = MagicMock()
    # get_indexed_sha always returns stale_sha → heal will fail the post-check
    mock_db.get_indexed_sha.return_value = stale_sha
    mock_db.__enter__ = lambda self: self
    mock_db.__exit__ = MagicMock(return_value=False)

    with (
        patch("sqlcg.server.tools._open_backend") as mock_open,
        patch("sqlcg.server.tools._assert_indexed"),
        patch("sqlcg.server.tools._indexed_root", return_value=None),
        patch(
            "sqlcg.server.tools._resolve_git_ref",
            return_value=base_sha,
        ),
        patch("sqlcg.server.tools.Indexer") as mock_indexer_cls,
    ):
        mock_open.return_value.__enter__ = lambda ctx: mock_db
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = get_pr_impact(base_ref="feature-branch")

    # resync_changed WAS attempted (self-heal fires).
    mock_indexer_cls.return_value.resync_changed.assert_called_once()

    # Because get_indexed_sha still returns stale_sha after the reindex,
    # _reindex_to_sha returns False → error hint contains "manually".
    assert result.hint is not None, "Should have a diagnostic hint"
    assert "manually" in result.hint, (
        f"Hint should contain 'manually' on heal failure; got: {result.hint!r}"
    )
    assert result.lost_producer_tables == []
    assert result.renamed_tables == {}


# ---------------------------------------------------------------------------
# PR-F — self-healing reindex scenarios (v1.28.3)
# Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F
# ---------------------------------------------------------------------------


def test_self_heal_fires_on_sha_mismatch_and_result_has_no_manually_hint():
    """Scenario A: stale graph SHA triggers self-heal; when _reindex_to_sha
    succeeds the result carries no 'manually' hint and is not the error-exit path.

    After the heal, HEAD==base_sha so _compute_pr_impact returns the
    'same commit' early exit (not an error), proving the error path is bypassed.

    Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F Scenario A.
    """
    from sqlcg.server.tools import get_pr_impact

    stale_sha = "aaa000aaa000aaa000aaa000aaa000aaa000aaa0"
    base_sha = "bbb111bbb111bbb111bbb111bbb111bbb111bbb1"

    mock_db = MagicMock()
    # get_indexed_sha: first call (pre-heal check) returns stale_sha;
    # second call (post-heal verification in _reindex_to_sha) returns base_sha
    # to simulate a successful heal.
    mock_db.get_indexed_sha.side_effect = [stale_sha, base_sha, base_sha]

    with (
        patch("sqlcg.server.tools._open_backend") as mock_open,
        patch("sqlcg.server.tools._assert_indexed"),
        patch("sqlcg.server.tools._indexed_root", return_value=Path("/fake/root")),
        patch("sqlcg.server.tools._resolve_git_ref") as mock_resolve,
        patch("sqlcg.server.tools.Indexer") as mock_indexer_cls,
        patch("sqlcg.server.tools._capture_producer_set", return_value=set()),
        patch("sqlcg.server.tools.get_dialect", return_value=None, create=True),
    ):
        # base_sha for base_ref, then base_sha for HEAD → same commit path
        mock_resolve.side_effect = [base_sha, base_sha]
        mock_open.return_value.__enter__ = lambda ctx: mock_db
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = get_pr_impact(base_ref="main")

    # Self-heal was attempted.
    mock_indexer_cls.return_value.resync_changed.assert_called_once()

    # Result must not carry the "manually" error hint.
    assert result.hint is None or "manually" not in result.hint, (
        f"Result should not contain 'manually' hint after successful heal; got hint={result.hint!r}"
    )
    # base_sha is set in the result.
    assert result.base_sha == base_sha


def test_reindex_to_sha_returns_true_on_success():
    """_reindex_to_sha returns True when get_indexed_sha equals target_sha after reindex.

    Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F _reindex_to_sha contract.
    """
    target_sha = "cccc3333cccc3333cccc3333cccc3333cccc3333"
    from_sha = "dddd4444dddd4444dddd4444dddd4444dddd4444"

    mock_db = MagicMock()
    # First call in _reindex_to_sha (from_sha); second call post-reindex (should match target).
    mock_db.get_indexed_sha.side_effect = [from_sha, target_sha]

    with (
        patch("sqlcg.server.tools.Indexer") as mock_indexer_cls,
        patch("sqlcg.core.config.get_dialect", return_value=None),
    ):
        result = _reindex_to_sha(mock_db, Path("/fake/root"), target_sha)

    assert result is True
    # Verify resync_changed was called with the right positional args;
    # dialect may be None or whatever get_dialect returned (patched to None here).
    actual_call = mock_indexer_cls.return_value.resync_changed.call_args
    assert actual_call is not None
    args = actual_call[0]
    assert args[0] == Path("/fake/root")
    assert args[1] == from_sha
    assert args[2] == target_sha
    assert args[3] is mock_db


def test_reindex_to_sha_returns_false_when_sha_still_mismatches_after_reindex():
    """_reindex_to_sha returns False when get_indexed_sha != target_sha after reindex.

    Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F _reindex_to_sha fail path.
    """
    target_sha = "eeee5555eeee5555eeee5555eeee5555eeee5555"
    from_sha = "ffff6666ffff6666ffff6666ffff6666ffff6666"
    wrong_sha = "0000000000000000000000000000000000000000"

    mock_db = MagicMock()
    mock_db.get_indexed_sha.side_effect = [from_sha, wrong_sha]

    with patch("sqlcg.server.tools.Indexer"):
        result = _reindex_to_sha(mock_db, Path("/fake/root"), target_sha)

    assert result is False


def test_reindex_to_sha_returns_false_on_exception():
    """_reindex_to_sha returns False (not raises) when resync_changed raises.

    Guards plan/sprints/sprint_backlog_q2_bugfix.md §PR-F no silent exception swallow.
    """
    target_sha = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
    from_sha = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"

    mock_db = MagicMock()
    mock_db.get_indexed_sha.return_value = from_sha

    with patch("sqlcg.server.tools.Indexer") as mock_indexer_cls:
        mock_indexer_cls.return_value.resync_changed.side_effect = RuntimeError("git gone")
        result = _reindex_to_sha(mock_db, Path("/fake/root"), target_sha)

    assert result is False


# ---------------------------------------------------------------------------
# resync_changed fallback dispatch (OPEN-2 — index-at-SHA via detached worktree)
#
# Per decision (b): these assert ONLY the dispatch decision and the False-on-
# failure contract. They do NOT fake the stamp (no set_indexed_sha side-effect).
# The real-stamp proof is the integration test in tests/integration/test_resync.py.
# ---------------------------------------------------------------------------


def test_resync_fallback_dispatches_to_index_at_sha_when_backward():
    """When the live tree is NOT at new_sha, the git-delta-None fallback
    materializes new_sha via _index_repo_at_sha (not the bare index_repo)."""
    from sqlcg.indexer.indexer import Indexer

    old_sha = "1111111111111111111111111111111111111111"
    new_sha = "2222222222222222222222222222222222222222"
    head_sha = "3333333333333333333333333333333333333333"  # tree NOT at new_sha

    indexer = Indexer()
    mock_db = MagicMock()
    with (
        patch("sqlcg.indexer.git_delta.git_name_status_delta", return_value=None),
        patch("sqlcg.indexer.git_delta._get_current_head", return_value=head_sha),
        patch.object(Indexer, "_index_repo_at_sha") as spy_at_sha,
        patch.object(Indexer, "index_repo") as spy_index_repo,
    ):
        summary = indexer.resync_changed(Path("/fake/root"), old_sha, new_sha, mock_db, None)

    assert summary["fell_back_to_full"] is True
    spy_at_sha.assert_called_once()
    # The bare-root index_repo call must NOT fire on the backward path.
    spy_index_repo.assert_not_called()
    # Materialization targets new_sha.
    assert spy_at_sha.call_args[0][1] == new_sha


def test_resync_fallback_dispatches_to_index_repo_when_forward():
    """When the live tree IS already at new_sha, the fallback uses the bare
    index_repo(root, ...) — no worktree materialization."""
    from sqlcg.indexer.indexer import Indexer

    old_sha = "1111111111111111111111111111111111111111"
    new_sha = "2222222222222222222222222222222222222222"

    indexer = Indexer()
    mock_db = MagicMock()
    with (
        patch("sqlcg.indexer.git_delta.git_name_status_delta", return_value=None),
        patch("sqlcg.indexer.git_delta._get_current_head", return_value=new_sha),
        patch.object(Indexer, "_index_repo_at_sha") as spy_at_sha,
        patch.object(Indexer, "index_repo") as spy_index_repo,
    ):
        summary = indexer.resync_changed(Path("/fake/root"), old_sha, new_sha, mock_db, None)

    assert summary["fell_back_to_full"] is True
    spy_index_repo.assert_called_once()
    spy_at_sha.assert_not_called()


def test_reindex_to_sha_returns_false_when_index_at_sha_raises():
    """_reindex_to_sha returns False (no propagation) when the worktree
    materialization raises CalledProcessError (e.g. unknown SHA / shallow clone)."""
    import subprocess

    target_sha = "9999999999999999999999999999999999999999"
    from_sha = "8888888888888888888888888888888888888888"

    mock_db = MagicMock()
    mock_db.get_indexed_sha.return_value = from_sha

    with (
        patch("sqlcg.indexer.git_delta.git_name_status_delta", return_value=None),
        patch("sqlcg.indexer.git_delta._get_current_head", return_value=from_sha),
        patch(
            "sqlcg.indexer.indexer.Indexer._index_repo_at_sha",
            side_effect=subprocess.CalledProcessError(1, ["git", "worktree", "add"]),
        ),
        patch("sqlcg.core.config.get_dialect", return_value=None),
    ):
        result = _reindex_to_sha(mock_db, Path("/fake/root"), target_sha)

    assert result is False
