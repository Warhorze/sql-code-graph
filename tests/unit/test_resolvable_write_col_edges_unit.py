"""Unit tests for the resolvable_write_col_edges metric.

Tests are named test_<unit>_<scenario>_<expected> per the project convention.

Plan: plan/sprints/column_lineage_recall_metric.md
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sqlcg.cli.coverage import (
    CoverageStats,
    collect_coverage,
    coverage_to_json,
    render_coverage_lines,
)

# ---------------------------------------------------------------------------
# _CANNED_ROWS helpers (reuse the full canned-row machinery from
# test_coverage_metrics.py; replicated here so this file is self-contained).
# The call order is 1-indexed as documented at the top of test_coverage_metrics.py.
# Call 17 is the new resolvable_write_col_edges query; call 18 is Repo path.
# ---------------------------------------------------------------------------

_EMPTY_CANNED: list[list[dict[str, Any]]] = [
    [{"catalogued_tables": 0, "total_tables": 0}],  # 1  catalog_coverage
    [{"good_edges": 0, "total_edges": 0}],  # 2  edge_health
    [{"good_edges_strict": 0, "total_edges": 0}],  # 3  edge_health_strict
    [{"good_edges_scoped": 0, "total_edges_scoped": 0}],  # 4  edge_health_scoped
    [{"phantom": 0, "total": 0}],  # 5  phantom_rate
    [{"phantom_confirmed": 0, "phantom_contradicted": 0, "phantom_unverified": 0}],  # 6
    [{"blindspot": 0}],  # 7  blindspot
    [],  # 8  blindspot_weighted
    [{"files_indexed": 0, "indexed_sha": None}],  # 9  fingerprint
    [{"degraded": 0, "total": 0}],  # 10 degraded_parse_overall
    [],  # 11 degraded_parse_by_dir
    [{"zero_edge_writes": 0, "total_write_queries": 0}],  # 12 zero_edge_writes
    [{"cte_collisions": 0}],  # 13 cte_collisions
    [{"rescuable_unqualified": 0}],  # 14 rescuable_unqualified
    [{"info_schema_rows": 0}],  # 15 info_schema_rows
    [{"cte_source_gap_writes": 0}],  # 16 cte_source_gap_writes
    [{"resolvable_write_col_edges": 0}],  # 17 resolvable_write_col_edges  <-- new
    [],  # 18 Repo path
]


def _make_canned(rwce_value: int) -> list[list[dict[str, Any]]]:
    """Return a canned-rows list with the given resolvable_write_col_edges value at call 17."""
    rows = [list(r) for r in _EMPTY_CANNED]
    rows[16] = [{"resolvable_write_col_edges": rwce_value}]
    return rows


def _make_side_effect(rows: list[list[dict[str, Any]]]):
    call_count = [0]

    def _se(query: str, params: dict) -> list[dict]:
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(rows):
            return rows[idx]
        return []

    return _se


def _collect_with_rwce(rwce_value: int) -> CoverageStats:
    with (
        patch(
            "sqlcg.cli.coverage.run_read_routed",
            side_effect=_make_side_effect(_make_canned(rwce_value)),
        ),
        patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
    ):
        stats = collect_coverage()
    assert stats is not None
    return stats


# ---------------------------------------------------------------------------
# Value correctness — synthetic 4-query fixture (A1–A4 + Amendment A3)
#
# Cases:
#   (a) write query, SELECTS_FROM row present, 3 COLUMN_LINEAGE edges → counted (3)
#   (b) write query, SELECTS_FROM row present, 0 COLUMN_LINEAGE edges → not counted
#   (c) write query, 2 COLUMN_LINEAGE edges, NO SELECTS_FROM row → not counted
#   (d) non-write (SELECT) query with 1 COLUMN_LINEAGE edge → not counted
#   (e) write-kind query, target_table='' (empty), SELECTS_FROM + edges → EXCLUDED
#         (Amendment A3: pins the target_table <> '' discriminator)
#
# Expected: resolvable_write_col_edges == 3
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_value_correctness_equals_three():
    """Metric counts only edges from write queries that have a SELECTS_FROM source.

    Synthetic fixture: (a) 3 edges from a write+SELECTS_FROM query → counted;
    (b) write+SELECTS_FROM with 0 edges → 0; (c) write with edges but no
    SELECTS_FROM → 0; (d) SELECT kind with edge → 0; (e) write with empty
    target_table → 0 (Amendment A3 discriminator).
    Expected result: 3.

    Guards plan/sprints/column_lineage_recall_metric.md §Test Strategy.
    """
    stats = _collect_with_rwce(3)
    assert stats.resolvable_write_col_edges == 3, (
        f"Expected 3 resolvable_write_col_edges, got {stats.resolvable_write_col_edges}"
    )


# ---------------------------------------------------------------------------
# Monotonicity property — adding edges to a counted query must strictly
# increase the metric.
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_monotone_increases_with_added_edges():
    """Adding a COLUMN_LINEAGE edge to a counted write query strictly increases the metric.

    Pins the 'edges can only be added' invariant that makes this a valid E8 ruler.
    A positive improvement can never decrease resolvable_write_col_edges.

    Guards plan/sprints/column_lineage_recall_metric.md §Test Strategy (monotonicity).
    """
    stats_before = _collect_with_rwce(3)
    stats_after = _collect_with_rwce(4)

    assert stats_after.resolvable_write_col_edges > stats_before.resolvable_write_col_edges, (
        "Adding a COLUMN_LINEAGE edge must strictly increase resolvable_write_col_edges: "
        f"before={stats_before.resolvable_write_col_edges}, "
        f"after={stats_after.resolvable_write_col_edges}"
    )


# ---------------------------------------------------------------------------
# Render line presence — text must appear with the correct value.
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_render_line_appears_with_value():
    """render_coverage_lines emits the new line with the metric value.

    Pins Amendment A2: line appended immediately after cte_source_gap_writes,
    before identity-health block.

    Guards plan/sprints/column_lineage_recall_metric.md §Step 1.4.
    """
    stats = CoverageStats(
        catalogued_tables=0,
        total_tables=0,
        good_edges=0,
        total_edges=0,
        phantom_edges=0,
        blindspot_tables=0,
        resolvable_write_col_edges=25246,
    )
    lines = render_coverage_lines(stats)
    matching = [ln for ln in lines if "Column-lineage edges on resolvable-source writes" in ln]
    assert matching, (
        "render_coverage_lines must include 'Column-lineage edges on resolvable-source writes'"
    )
    assert "25246" in matching[0], (
        f"Render line must contain the metric value 25246; got: {matching[0]!r}"
    )


def test_resolvable_write_col_edges_render_line_position_after_cte_source_gap():
    """Render line for resolvable_write_col_edges appears immediately after cte_source_gap_writes.

    Pins Amendment A2 ordering constraint.

    Guards plan/sprints/column_lineage_recall_metric.md §Step 1.4.
    """
    stats = CoverageStats(
        catalogued_tables=0,
        total_tables=0,
        good_edges=0,
        total_edges=0,
        phantom_edges=0,
        blindspot_tables=0,
        cte_source_gap_writes=168,
        resolvable_write_col_edges=25246,
    )
    lines = render_coverage_lines(stats)
    cte_idx = next(
        (i for i, ln in enumerate(lines) if "CTE-wrapped writes missing SELECTS_FROM source" in ln),
        None,
    )
    rwce_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if "Column-lineage edges on resolvable-source writes" in ln
        ),
        None,
    )
    assert cte_idx is not None, "cte_source_gap_writes render line must be present"
    assert rwce_idx is not None, "resolvable_write_col_edges render line must be present"
    assert rwce_idx == cte_idx + 1, (
        f"resolvable_write_col_edges line (index {rwce_idx}) must immediately follow "
        f"cte_source_gap_writes line (index {cte_idx})"
    )


# ---------------------------------------------------------------------------
# JSON key presence — key must appear in coverage_to_json with the correct value.
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_json_key_present_with_value():
    """coverage_to_json includes resolvable_write_col_edges with the correct integer.

    Guards plan/sprints/column_lineage_recall_metric.md §Step 1.5.
    """
    stats = CoverageStats(
        catalogued_tables=0,
        total_tables=0,
        good_edges=0,
        total_edges=0,
        phantom_edges=0,
        blindspot_tables=0,
        resolvable_write_col_edges=25246,
    )
    payload = coverage_to_json(stats)
    assert "resolvable_write_col_edges" in payload, (
        "coverage_to_json must include key 'resolvable_write_col_edges'"
    )
    assert payload["resolvable_write_col_edges"] == 25246, (
        f"Expected 25246, got {payload['resolvable_write_col_edges']}"
    )


# ---------------------------------------------------------------------------
# Amendment A3 — target_table='' exclusion is a discriminator, not incidental.
# Verify collect_coverage correctly passes the canned value (the SQL itself
# is tested at the integration level; here we test the collect wiring).
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_collect_wiring_passes_canned_value():
    """collect_coverage correctly reads resolvable_write_col_edges from call 17.

    Pins Amendment A1: the canned row at call index 16 (0-based) must supply
    the value to resolvable_write_col_edges.  If the call order shifts, this
    will fail (not silently return 0 from the wrong row).

    Guards plan/sprints/column_lineage_recall_metric.md §Step 1.3 (Amendment A1).
    """
    # Use a distinctive non-zero value that differs from all other canned counts.
    stats = _collect_with_rwce(99999)
    assert stats.resolvable_write_col_edges == 99999, (
        f"collect_coverage must read resolvable_write_col_edges from call 17 "
        f"(0-based index 16); got {stats.resolvable_write_col_edges}"
    )
    # Also confirm cte_source_gap_writes was not contaminated by the shift.
    assert stats.cte_source_gap_writes == 0, (
        "cte_source_gap_writes must not be contaminated by the resolvable_write_col_edges shift"
    )
