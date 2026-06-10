"""Acceptance tests for the gain coverage metrics modules.

Tests are named after the deliverable so `pytest -k coverage_metrics` runs them.

Plan (legacy 6 fields): plan/sprints/gain_coverage_metrics.md
Plan (PR 1 — strict/scoped/phantom-split/fingerprint/recall):
  plan/sprints/graph_health_catalog_and_metrics.md §3
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import typer

# ---------------------------------------------------------------------------
# Guard: symbols introduced by this plan
# ---------------------------------------------------------------------------

try:
    from sqlcg.cli.coverage import (  # introduced by gain_coverage_metrics
        CoverageStats,  # noqa: F401  — import validates symbol exists; skip guard
        blindspot_colour,
        collect_coverage,
        contradicted_colour,
        edge_health_colour,
        phantom_colour,
    )
except ImportError:
    pytest.skip("gain_coverage_metrics not yet implemented", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
#
# collect_coverage() issues 12 run_read_routed calls in this order (PR 1):
#   1  catalog_coverage          {catalogued_tables, total_tables}
#   2  edge_health (legacy)      {good_edges, total_edges}
#   3  edge_health_strict        {good_edges_strict, total_edges}
#   4  edge_health_scoped        {good_edges_scoped, total_edges_scoped}
#   5  phantom_rate (legacy)     {phantom, total}
#   6  phantom_split             {phantom_confirmed, phantom_contradicted, phantom_unverified}
#   7  blindspot (legacy)        {blindspot}
#   8  blindspot_weighted        list of {dst_table, bad_count, rn, running_total, grand_total}
#   9  fingerprint               {files_indexed, indexed_sha}
#   10 degraded_parse_overall    {degraded, total}
#   11 degraded_parse_by_dir     list of {top_dir, degraded, total}
#   12 zero_edge_writes          {zero_edge_writes, total_write_queries}
# ---------------------------------------------------------------------------

_CANNED_ROWS = [
    [{"catalogued_tables": 528, "total_tables": 2944}],
    [{"good_edges": 14884, "total_edges": 51434}],
    [{"good_edges_strict": 14000, "total_edges": 51434}],
    [{"good_edges_scoped": 13500, "total_edges_scoped": 45000}],
    [{"phantom": 13412, "total": 51434}],
    [{"phantom_confirmed": 5990, "phantom_contradicted": 1028, "phantom_unverified": 11300}],
    [{"blindspot": 1681}],
    [
        {"dst_table": "tbl_a", "bad_count": 100, "rn": 1, "running_total": 100, "grand_total": 200},
        {"dst_table": "tbl_b", "bad_count": 100, "rn": 2, "running_total": 200, "grand_total": 200},
    ],
    [{"files_indexed": 1340, "indexed_sha": "abc123"}],
    [{"degraded": 1855, "total": 3225}],
    [
        {"top_dir": "etl", "degraded": 1855, "total": 3225},
        {"top_dir": "ddl", "degraded": 100, "total": 130},
    ],
    [{"zero_edge_writes": 92, "total_write_queries": 500}],
]


def _make_coverage_side_effect(rows: list[list[dict[str, Any]]]):
    """Return rows in order for successive run_read_routed calls."""
    call_count = [0]

    def _se(query: str, params: dict) -> list[dict]:
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(rows):
            return rows[idx]
        return []

    return _se


# ---------------------------------------------------------------------------
# collect_coverage — populated result
# ---------------------------------------------------------------------------


def test_coverage_metrics_collect_returns_populated_stats():
    """collect_coverage returns a CoverageStats with correct integers and percentages.

    Plan: collect_coverage() returns populated CoverageStats when run_read_routed
    is patched to return canned result sets (legacy 6 + PR 1 fields).
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_coverage_side_effect(_CANNED_ROWS),
    ):
        stats = collect_coverage()

    assert stats is not None, "collect_coverage() returned None but should return CoverageStats"
    # legacy fields, unchanged meaning
    assert stats.catalogued_tables == 528
    assert stats.total_tables == 2944
    assert stats.good_edges == 14884
    assert stats.total_edges == 51434
    assert stats.phantom_edges == 13412
    assert stats.blindspot_tables == 1681

    # percent properties (legacy)
    assert abs(stats.catalog_pct - (528 / 2944 * 100)) < 0.1
    assert abs(stats.edge_health_pct - (14884 / 51434 * 100)) < 0.1
    assert abs(stats.phantom_pct - (13412 / 51434 * 100)) < 0.1

    # PR 1 — strict / scoped column-level health
    assert stats.good_edges_strict == 14000
    assert abs(stats.edge_health_strict_pct - (14000 / 51434 * 100)) < 0.1
    assert stats.good_edges_scoped == 13500
    assert stats.total_edges_scoped == 45000
    assert abs(stats.edge_health_scoped_pct - (13500 / 45000 * 100)) < 0.1

    # PR 1 — phantom three-way split
    assert stats.phantom_confirmed == 5990
    assert stats.phantom_contradicted == 1028
    assert stats.phantom_unverified == 11300
    assert abs(stats.phantom_contradicted_pct - (1028 / 51434 * 100)) < 0.1

    # PR 1 — edge-weighted blindspot
    assert len(stats.top_blindspot_tables) == 2
    assert stats.top_blindspot_tables[0].table == "tbl_a"
    assert stats.top_blindspot_tables[0].bad_edges == 100
    assert stats.blindspot_tables_for_80pct == 2  # 200*0.8=160; first table (100) < 160

    # PR 1 — corpus fingerprint
    assert stats.files_indexed == 1340
    assert stats.indexed_sha == "abc123"
    assert stats.db_path  # populated from get_db_path(), never empty

    # PR 1 — recall lines
    assert stats.degraded_parse_total == 3225
    assert stats.degraded_parse_queries == 1855
    assert abs(stats.degraded_parse_pct - (1855 / 3225 * 100)) < 0.1
    assert stats.degraded_parse_by_dir["etl"] == (1855, 3225)
    assert stats.degraded_parse_by_dir["ddl"] == (100, 130)
    assert stats.zero_edge_write_queries == 92
    assert stats.total_write_queries == 500


# ---------------------------------------------------------------------------
# collect_coverage — degrade to None on exception
# ---------------------------------------------------------------------------


def test_coverage_metrics_collect_degrades_to_none_on_typer_exit():
    """collect_coverage returns None when run_read_routed raises typer.Exit.

    Plan: wraps all four reads in try/except Exception -> None.
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=typer.Exit(1),
    ):
        result = collect_coverage()

    assert result is None, "collect_coverage() must return None when run_read_routed raises"


# ---------------------------------------------------------------------------
# collect_coverage — zero-denominator safety
# ---------------------------------------------------------------------------


def test_coverage_metrics_collect_handles_empty_graph():
    """collect_coverage returns 0.0 percentages on an empty graph (no ZeroDivisionError).

    Plan: percent properties return 0.0 when denominator is 0.
    """
    empty_rows = [
        [{"catalogued_tables": 0, "total_tables": 0}],
        [{"good_edges": 0, "total_edges": 0}],
        [{"good_edges_strict": 0, "total_edges": 0}],
        [{"good_edges_scoped": 0, "total_edges_scoped": 0}],
        [{"phantom": 0, "total": 0}],
        [{"phantom_confirmed": 0, "phantom_contradicted": 0, "phantom_unverified": 0}],
        [{"blindspot": 0}],
        [],
        [{"files_indexed": 0, "indexed_sha": None}],
        [{"degraded": 0, "total": 0}],
        [],
        [{"zero_edge_writes": 0, "total_write_queries": 0}],
    ]
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_coverage_side_effect(empty_rows),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.total_edges == 0
    assert stats.catalog_pct == 0.0
    assert stats.edge_health_pct == 0.0
    assert stats.phantom_pct == 0.0
    # PR 1 zero-division guards
    assert stats.edge_health_strict_pct == 0.0
    assert stats.edge_health_scoped_pct == 0.0
    assert stats.phantom_contradicted_pct == 0.0
    assert stats.degraded_parse_pct == 0.0
    assert stats.top_blindspot_tables == []
    assert stats.blindspot_tables_for_80pct == 0


# ---------------------------------------------------------------------------
# Colour helpers — edge health boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [
        (29.9, "red"),  # below 30 → red
        (30.0, "yellow"),  # exactly 30 → yellow (boundary: not red)
        (49.9, "yellow"),  # below 50 → yellow
        (50.0, "green"),  # exactly 50 → green (boundary: not yellow)
        (80.0, "green"),  # well above 50 → green
    ],
)
def test_coverage_metrics_edge_health_colour_boundaries(pct, expected):
    """edge_health_colour returns correct colour at each threshold boundary.

    Plan: red below 30%, yellow [30%, 50%), green >= 50%.
    """
    result = edge_health_colour(pct)
    assert result == expected, (
        f"edge_health_colour({pct}) returned {result!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Colour helpers — phantom rate boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [
        (10.0, "green"),  # exactly 10 → green
        (10.1, "yellow"),  # just above 10 → yellow
        (20.0, "yellow"),  # exactly 20 → yellow
        (20.1, "red"),  # just above 20 → red
        (50.0, "red"),  # well above 20 → red
    ],
)
def test_coverage_metrics_phantom_colour_boundaries(pct, expected):
    """phantom_colour returns correct colour at each threshold boundary.

    Plan: green <= 10%, yellow (10%, 20%], red > 20%.
    """
    result = phantom_colour(pct)
    assert result == expected, f"phantom_colour({pct}) returned {result!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# Colour helpers — blindspot boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected_non_plain",
    [
        (500, False),  # at threshold → no colour tag (plain)
        (501, True),  # just over → yellow
    ],
)
def test_coverage_metrics_blindspot_colour_boundary(count, expected_non_plain):
    """blindspot_colour returns yellow above 500, plain (non-yellow) at/below.

    Plan: yellow when > 500, no threshold for red.
    """
    result = blindspot_colour(count)
    is_non_plain = result != "plain" and result != "" and result is not None
    assert is_non_plain == expected_non_plain, (
        f"blindspot_colour({count}) returned {result!r}; "
        f"expected {'non-plain (yellow)' if expected_non_plain else 'plain'}"
    )


# ---------------------------------------------------------------------------
# Colour helpers — contradicted-phantom boundary (PR 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [
        (0.0, "green"),
        (0.5, "green"),  # exactly 0.5% → green (boundary: not red)
        (0.50001, "red"),  # just above 0.5% → red
        (5.0, "red"),
    ],
)
def test_coverage_metrics_contradicted_colour_boundary(pct, expected):
    """contradicted_colour returns red when contradicted edges exceed 0.5% of total.

    Plan §3.3: contradicted is the headline trust number — render red > 0.5%.
    """
    result = contradicted_colour(pct)
    assert result == expected, (
        f"contradicted_colour({pct}) returned {result!r}, expected {expected!r}"
    )
