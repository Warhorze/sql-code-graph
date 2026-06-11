"""Unit tests for the identity-health coverage counters added in PR 4.

Guards: CTE key collisions and rescuable unqualified edges in CoverageStats,
collect_coverage, render_coverage_lines, and coverage_to_json.

Plan: plan/sprints/sprint_lineage_identity_and_session_context.md §PR 4
(Phase 1, Step 1.1)
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
# Helpers
#
# collect_coverage() issues 14 run_read_routed calls in this order (PR 1 + PR 4):
#   1  catalog_coverage
#   2  edge_health (legacy)
#   3  edge_health_strict
#   4  edge_health_scoped
#   5  phantom_rate (legacy)
#   6  phantom_split
#   7  blindspot (legacy)
#   8  blindspot_weighted
#   9  fingerprint
#   10 degraded_parse_overall
#   11 degraded_parse_by_dir
#   12 zero_edge_writes
#   13 cte_collisions   <- PR 4 new
#   14 rescuable_unqualified  <- PR 4 new
# ---------------------------------------------------------------------------

_BASE_ROWS: list[list[dict[str, Any]]] = [
    [{"catalogued_tables": 100, "total_tables": 200}],
    [{"good_edges": 50, "total_edges": 100}],
    [{"good_edges_strict": 40, "total_edges": 100}],
    [{"good_edges_scoped": 38, "total_edges_scoped": 90}],
    [{"phantom": 20, "total": 100}],
    [{"phantom_confirmed": 10, "phantom_contradicted": 5, "phantom_unverified": 5}],
    [{"blindspot": 50}],
    [],
    [{"files_indexed": 200, "indexed_sha": "abc"}],
    [{"degraded": 10, "total": 100}],
    [],
    [{"zero_edge_writes": 5, "total_write_queries": 50}],
]

_ROWS_WITH_COLLISIONS: list[list[dict[str, Any]]] = _BASE_ROWS + [
    [{"cte_collisions": 218}],
    [{"rescuable_unqualified": 828}],
]

_ROWS_ZERO_IDENTITY: list[list[dict[str, Any]]] = _BASE_ROWS + [
    [{"cte_collisions": 0}],
    [{"rescuable_unqualified": 0}],
]


def _make_side_effect(rows: list[list[dict[str, Any]]]):
    """Return run_read_routed side_effect that returns rows in order."""
    call_count = [0]

    def _se(query: str, params: dict) -> list[dict]:
        idx = call_count[0]
        call_count[0] += 1
        return rows[idx] if idx < len(rows) else []

    return _se


# ---------------------------------------------------------------------------
# collect_coverage — new fields populated
# ---------------------------------------------------------------------------


def test_collect_coverage_populates_cte_key_collisions():
    """collect_coverage returns cte_key_collisions from the 13th query result.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    The DWH baseline is 218; this test uses a canned value to verify the field
    is wired from the right query and column name (cte_collisions).
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_side_effect(_ROWS_WITH_COLLISIONS),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.cte_key_collisions == 218, (
        f"Expected cte_key_collisions=218 from canned row, got {stats.cte_key_collisions}"
    )


def test_collect_coverage_populates_rescuable_unqualified_edges():
    """collect_coverage returns rescuable_unqualified_edges from the 14th query result.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    The DWH baseline is 828; this test uses a canned value to verify the field
    is wired from the right query and column name (rescuable_unqualified).
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_side_effect(_ROWS_WITH_COLLISIONS),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.rescuable_unqualified_edges == 828, (
        f"Expected rescuable_unqualified_edges=828 from canned row, "
        f"got {stats.rescuable_unqualified_edges}"
    )


def test_collect_coverage_identity_fields_default_to_zero_on_empty_result():
    """collect_coverage returns 0 for identity fields when the query returns empty.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Graceful-degrade: `or 0` coercion means an empty row dict yields 0, not None.
    """
    with patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_side_effect(_ROWS_ZERO_IDENTITY),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.cte_key_collisions == 0
    assert stats.rescuable_unqualified_edges == 0


# ---------------------------------------------------------------------------
# render_coverage_lines — two new §G lines appear in output
# ---------------------------------------------------------------------------


def test_render_coverage_lines_includes_cte_key_collisions_line():
    """render_coverage_lines includes a 'CTE key collisions: N' line in §G output.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    The line must appear with the exact label so the user can grep for it.
    """
    stats = CoverageStats(
        catalogued_tables=10,
        total_tables=20,
        good_edges=8,
        total_edges=10,
        phantom_edges=2,
        blindspot_tables=3,
        cte_key_collisions=218,
        rescuable_unqualified_edges=828,
    )
    lines = render_coverage_lines(stats)
    collision_lines = [ln for ln in lines if "CTE key collisions" in ln]
    assert len(collision_lines) == 1, (
        f"Expected exactly 1 'CTE key collisions' line, got {collision_lines!r}"
    )
    assert "218" in collision_lines[0], (
        f"Expected count 218 in collision line, got: {collision_lines[0]!r}"
    )


def test_render_coverage_lines_includes_rescuable_unqualified_line():
    """render_coverage_lines includes a 'Rescuable unqualified edges: N' line in §G output.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    The line must appear with the exact label so the user can grep for it.
    """
    stats = CoverageStats(
        catalogued_tables=10,
        total_tables=20,
        good_edges=8,
        total_edges=10,
        phantom_edges=2,
        blindspot_tables=3,
        cte_key_collisions=218,
        rescuable_unqualified_edges=828,
    )
    lines = render_coverage_lines(stats)
    rescuable_lines = [ln for ln in lines if "Rescuable unqualified edges" in ln]
    assert len(rescuable_lines) == 1, (
        f"Expected exactly 1 'Rescuable unqualified edges' line, got {rescuable_lines!r}"
    )
    assert "828" in rescuable_lines[0], (
        f"Expected count 828 in rescuable line, got: {rescuable_lines[0]!r}"
    )


def test_render_coverage_lines_identity_lines_present_in_output():
    """Identity health lines appear in the §G render output.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Checks by content presence rather than positional index, because PR 4
    (sprint_postmortem_fixes.md §Step 4.3) may append a catalog-missing warning
    line after the identity counters when info_schema_has_column_rows=0.
    Set info_schema_has_column_rows=1 to suppress the warning for this test.
    """
    stats = CoverageStats(
        catalogued_tables=10,
        total_tables=20,
        good_edges=8,
        total_edges=10,
        phantom_edges=2,
        blindspot_tables=3,
        cte_key_collisions=5,
        rescuable_unqualified_edges=12,
        info_schema_has_column_rows=1,  # suppress catalog-missing warning
    )
    lines = render_coverage_lines(stats)
    cte_lines = [ln for ln in lines if "CTE key collisions" in ln]
    rescuable_lines = [ln for ln in lines if "Rescuable unqualified edges" in ln]
    assert cte_lines, f"Expected a 'CTE key collisions' line in render output, got:\n{lines}"
    assert rescuable_lines, (
        f"Expected a 'Rescuable unqualified edges' line in render output, got:\n{lines}"
    )
    assert "5" in cte_lines[0], f"Expected count 5 in CTE collisions line: {cte_lines[0]!r}"
    assert "12" in rescuable_lines[0], (
        f"Expected count 12 in rescuable line: {rescuable_lines[0]!r}"
    )


# ---------------------------------------------------------------------------
# coverage_to_json — new keys present
# ---------------------------------------------------------------------------


def test_coverage_to_json_exposes_cte_key_collisions():
    """coverage_to_json exposes cte_key_collisions as a top-level key.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).
    """
    stats = CoverageStats(
        catalogued_tables=10,
        total_tables=20,
        good_edges=8,
        total_edges=10,
        phantom_edges=2,
        blindspot_tables=3,
        cte_key_collisions=218,
        rescuable_unqualified_edges=828,
    )
    payload = coverage_to_json(stats)
    assert "cte_key_collisions" in payload, (
        "'cte_key_collisions' key missing from coverage_to_json output"
    )
    assert payload["cte_key_collisions"] == 218, (
        f"Expected 218, got {payload['cte_key_collisions']}"
    )


def test_coverage_to_json_exposes_rescuable_unqualified_edges():
    """coverage_to_json exposes rescuable_unqualified_edges as a top-level key.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).
    """
    stats = CoverageStats(
        catalogued_tables=10,
        total_tables=20,
        good_edges=8,
        total_edges=10,
        phantom_edges=2,
        blindspot_tables=3,
        cte_key_collisions=218,
        rescuable_unqualified_edges=828,
    )
    payload = coverage_to_json(stats)
    assert "rescuable_unqualified_edges" in payload, (
        "'rescuable_unqualified_edges' key missing from coverage_to_json output"
    )
    assert payload["rescuable_unqualified_edges"] == 828, (
        f"Expected 828, got {payload['rescuable_unqualified_edges']}"
    )
