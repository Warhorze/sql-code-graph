"""Failing acceptance tests for gain_coverage_metrics plan.

Tests are named after the deliverable so `pytest -k coverage_metrics` runs them.
All tests must FAIL (or SKIP on missing symbol) before the developer implements
the feature.

Plan: plan/sprints/gain_coverage_metrics.md
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
        edge_health_colour,
        phantom_colour,
    )
except ImportError:
    pytest.skip("gain_coverage_metrics not yet implemented", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CANNED_ROWS = [
    # q1 — catalog_coverage
    [{"catalogued_tables": 528, "total_tables": 2944}],
    # q2 — edge_health
    [{"good_edges": 14884, "total_edges": 51434}],
    # q3 — phantom_rate
    [{"phantom": 13412, "total": 51434}],
    # q4 — blindspot_tables
    [{"blindspot": 1681}],
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
    is patched to return four canned result sets.
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_coverage_side_effect(_CANNED_ROWS),
    ):
        stats = collect_coverage()

    assert stats is not None, "collect_coverage() returned None but should return CoverageStats"
    assert stats.catalogued_tables == 528
    assert stats.total_tables == 2944
    assert stats.good_edges == 14884
    assert stats.total_edges == 51434
    assert stats.phantom_edges == 13412
    assert stats.blindspot_tables == 1681

    # percent properties
    assert abs(stats.catalog_pct - (528 / 2944 * 100)) < 0.1
    assert abs(stats.edge_health_pct - (14884 / 51434 * 100)) < 0.1
    assert abs(stats.phantom_pct - (13412 / 51434 * 100)) < 0.1


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
        [{"phantom": 0, "total": 0}],
        [{"blindspot": 0}],
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
