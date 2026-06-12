"""Unit tests for the empty-impact blast-radius engine helpers.

Tests _table_row_reachability, _compute_empty_propagation edge-cases, and model
field defaults. Uses hand-built mock backends — no real DuckDB required.

Guards plan/sprints/unfilled_table_impact.md PR 1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import sqlcg.server.tools as tools
from sqlcg.server.models import EmptyPropagationResult
from sqlcg.server.tools import (
    _compute_empty_propagation,
    _table_row_reachability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(
    adjacency_rows: list[dict] | None = None,
    kind_rows: list[dict] | None = None,
    columns_rows: dict[str, list[dict]] | None = None,
    downstream_rows: dict[str, list[dict]] | None = None,
) -> MagicMock:
    """Build a mock GraphBackend that responds to the queries used by the engine."""
    db = MagicMock()
    adj = adjacency_rows or []
    kinds = kind_rows or []
    cols = columns_rows or {}
    ds = downstream_rows or {}

    def _run_read(query: str, params: dict):
        # GET_TABLE_READS_ADJACENCY (no params)
        if "SELECTS_FROM" in query and "STAR_SOURCE" in query and "UNION" in query:
            return adj
        # GET_TABLE_KINDS_BATCH
        if (
            '"SqlTable"' in query
            and "ANY(?)" in query
            and "kind" in query
            and "HAS_COLUMN" not in query
        ):
            requested = params.get("table_qualifieds", [])
            return [r for r in kinds if r["table_qualified"] in requested]
        # GET_COLUMNS_FOR_TABLE
        if "HAS_COLUMN" in query:
            tq = params.get("table_qualified", "")
            return cols.get(tq, [])
        # GET_DOWNSTREAM_DEPENDENCIES
        if (
            "COLUMN_LINEAGE" in query
            and "dst" in query
            and "src_key" in query
            and "src" not in query.split("JOIN")[0]
        ):
            cid = params.get("id", "")
            return ds.get(cid, [])
        # GET_TABLE_ADJACENCY_FOR_COLUMNS
        if "COLUMN_LINEAGE" in query and "upstream_table" in query:
            return []
        # Repo lookup
        if '"Repo"' in query:
            return [{"n": 1}]
        if '"File"' in query:
            return [{"n": 0}]
        return []

    db.run_read.side_effect = _run_read
    return db


# ---------------------------------------------------------------------------
# Unit: _table_row_reachability
# ---------------------------------------------------------------------------


def test_table_row_reachability_returns_direct_successor():
    """A direct adjacency edge source_table → dest_table is returned.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.2.
    """
    db = _make_db(
        adjacency_rows=[{"source_table": "s.x", "dest_table": "s.d"}],
        kind_rows=[
            {"table_qualified": "s.x", "kind": "table"},
            {"table_qualified": "s.d", "kind": "table"},
        ],
    )
    reachable, truncated = _table_row_reachability(db, ["s.x"])
    assert "s.d" in reachable
    assert not truncated


def test_table_row_reachability_excludes_source():
    """The named source table is never in the reachable output (E7).

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.2.
    """
    db = _make_db(
        adjacency_rows=[
            {"source_table": "s.x", "dest_table": "s.d"},
            {"source_table": "s.x", "dest_table": "s.x"},  # self-edge
        ],
        kind_rows=[
            {"table_qualified": "s.x", "kind": "table"},
            {"table_qualified": "s.d", "kind": "table"},
        ],
    )
    reachable, _ = _table_row_reachability(db, ["s.x"])
    assert "s.x" not in reachable
    assert "s.d" in reachable


def test_table_row_reachability_contracts_synthetic_bridge():
    """A real→synthetic→real path is bridged into real→real; the synthetic node
    never appears in the output (M4).

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.2 (M4).
    """
    db = _make_db(
        adjacency_rows=[
            {"source_table": "s.x", "dest_table": "s.cte_bridge"},
            {"source_table": "s.cte_bridge", "dest_table": "s.d"},
        ],
        kind_rows=[
            {"table_qualified": "s.x", "kind": "table"},
            {"table_qualified": "s.cte_bridge", "kind": "cte"},
            {"table_qualified": "s.d", "kind": "table"},
        ],
    )
    reachable, _ = _table_row_reachability(db, ["s.x"])
    assert "s.d" in reachable
    assert "s.cte_bridge" not in reachable


def test_table_row_reachability_cycle_safe():
    """A cycle in the adjacency does not cause infinite BFS.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.2 (cycle-safety).
    """
    db = _make_db(
        adjacency_rows=[
            {"source_table": "s.x", "dest_table": "s.a"},
            {"source_table": "s.a", "dest_table": "s.b"},
            {"source_table": "s.b", "dest_table": "s.a"},  # cycle
        ],
        kind_rows=[
            {"table_qualified": "s.x", "kind": "table"},
            {"table_qualified": "s.a", "kind": "table"},
            {"table_qualified": "s.b", "kind": "table"},
        ],
    )
    reachable, truncated = _table_row_reachability(db, ["s.x"])
    # Should terminate; both a and b reachable.
    assert "s.a" in reachable
    assert "s.b" in reachable
    assert not truncated


def test_table_row_reachability_empty_adjacency_returns_empty():
    """No adjacency edges → no reachable tables.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.2.
    """
    db = _make_db(adjacency_rows=[], kind_rows=[])
    reachable, truncated = _table_row_reachability(db, ["s.x"])
    assert reachable == []
    assert not truncated


# ---------------------------------------------------------------------------
# Unit: _compute_empty_propagation — table-not-found
# ---------------------------------------------------------------------------


def test_compute_empty_propagation_not_found_sets_hint():
    """A table that has no columns in the graph sets the hint and echoes the
    source but contributes nothing to the affected sets.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E1.
    """
    db = _make_db(
        adjacency_rows=[],
        kind_rows=[],
        columns_rows={},  # no columns for "s.missing"
    )
    with (
        patch.object(tools, "_indexed_root", return_value=None),
        patch("sqlcg.server.tools.NoiseFilter") as mock_nf,
    ):
        mock_nf.from_config.return_value.filter_nodes.side_effect = lambda lst: (lst, [])
        result = _compute_empty_propagation(db, ["s.missing"], None)

    assert "s.missing" in result.sources
    assert result.value_empty_columns == []
    assert result.value_affected_tables == []
    assert result.row_empty_tables == []
    assert result.hint is not None
    assert "not found" in result.hint.lower() or "s.missing" in result.hint


def test_compute_empty_propagation_no_downstream_sets_hint():
    """A table with columns but no downstream edges sets the 'nothing propagates' hint.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E2.
    """
    db = _make_db(
        adjacency_rows=[],
        kind_rows=[{"table_qualified": "s.src", "kind": "table"}],
        columns_rows={"s.src": [{"col_id": "s.src.id", "col_name": "id"}]},
        downstream_rows={"s.src.id": []},  # no downstream
    )
    with (
        patch.object(tools, "_indexed_root", return_value=None),
        patch("sqlcg.server.tools.NoiseFilter") as mock_nf,
    ):
        mock_nf.from_config.return_value.filter_nodes.side_effect = lambda lst: (lst, [])
        result = _compute_empty_propagation(db, ["s.src"], None)

    assert result.value_empty_columns == []
    assert result.row_empty_tables == []
    assert result.hint is not None
    assert "nothing propagates" in result.hint.lower() or "no downstream" in result.hint.lower()


# ---------------------------------------------------------------------------
# Unit: EmptyPropagationResult model defaults
# ---------------------------------------------------------------------------


def test_empty_propagation_result_model_defaults():
    """EmptyPropagationResult can be instantiated with minimal arguments.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 1.1 model defaults.
    """
    result = EmptyPropagationResult(sources=["s.x"])
    assert result.row_empty_tables == []
    assert result.value_empty_columns == []
    assert result.value_affected_tables == []
    assert result.value_fully_empty_tables == []
    assert result.value_partially_empty_tables == []
    assert result.propagation_order == []
    assert result.downstream_count == 0
    assert result.noise_excluded == []
    assert result.truncated is False
    assert result.hint is None


def test_empty_propagation_result_row_label_contains_both_caveats():
    """The row_empty_tables field description carries both required caveats:
    'CTE-wrapped pure-gating reads are NOT detected (known gap)' and
    'depends on join type'.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Acceptance Criteria (string assertion).
    """
    field = EmptyPropagationResult.model_fields["row_empty_tables"]
    desc = field.description or ""
    assert "CTE-wrapped pure-gating reads are NOT detected (known gap)" in desc, (
        f"Missing required caveat string in row_empty_tables docstring. Got: {desc!r}"
    )
    assert "depends on join type" in desc, (
        f"Missing 'depends on join type' caveat in row_empty_tables docstring. Got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# Unit: multi-source union semantics
# ---------------------------------------------------------------------------


def test_compute_empty_propagation_multi_source_deduplicates():
    """Multiple identical table names in the input are deduplicated in sources.

    Guards plan/sprints/unfilled_table_impact.md PR 1 multi-source union semantics.
    """
    db = _make_db(adjacency_rows=[], kind_rows=[], columns_rows={})
    with (
        patch.object(tools, "_indexed_root", return_value=None),
        patch("sqlcg.server.tools.NoiseFilter") as mock_nf,
    ):
        mock_nf.from_config.return_value.filter_nodes.side_effect = lambda lst: (lst, [])
        result = _compute_empty_propagation(db, ["s.x", "s.x"], None)
    # Deduplicated to one entry.
    assert result.sources.count("s.x") == 1


# ---------------------------------------------------------------------------
# Unit: truncation flag
# ---------------------------------------------------------------------------


def test_compute_empty_propagation_truncation_when_cap_hit(monkeypatch):
    """When either traversal hits the 50k-node cap, truncated=True and hint
    mentions the cap.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E5 (truncation).
    """
    # Stub _affected_columns_closure to return truncated=True.
    monkeypatch.setattr(
        tools,
        "_affected_columns_closure",
        lambda db, start_cols, max_depth: ([], 0, True),
    )
    db = _make_db(
        adjacency_rows=[],
        kind_rows=[{"table_qualified": "s.src", "kind": "table"}],
        columns_rows={"s.src": [{"col_id": "s.src.id", "col_name": "id"}]},
    )
    with (
        patch.object(tools, "_indexed_root", return_value=None),
        patch("sqlcg.server.tools.NoiseFilter") as mock_nf,
    ):
        mock_nf.from_config.return_value.filter_nodes.side_effect = lambda lst: (lst, [])
        result = _compute_empty_propagation(db, ["s.src"], None)

    assert result.truncated is True
    assert result.hint is not None
    assert (
        "50k" in result.hint.lower()
        or "cap" in result.hint.lower()
        or "safety" in result.hint.lower()
    )
