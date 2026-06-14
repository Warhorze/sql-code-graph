"""Unit tests for tool-version stamp (#151) — freshness.py + coverage.py.

Tests the stale-tool-version detection path added in v1.33.0:
- compute_freshness() sets tool_version_stale when indexed_version != running_version
- Null indexed_version (legacy graph) → False, no false positive
- render_freshness_line() appends the reindex hint when stale
- CoverageStats carries tool_version_stale, indexed_tool_version, running_tool_version
- collect_coverage() derives tool_version_stale from the fingerprint row
- render_coverage_lines() emits the stale warning when set
- coverage_to_json() includes the three new keys

Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlcg.core.freshness import Freshness, compute_freshness, render_freshness_line

# ---------------------------------------------------------------------------
# Helpers for collect_coverage patching
# ---------------------------------------------------------------------------


def _make_coverage_side_effect(rows: list[list[dict[str, Any]]]):
    """Return rows in order for successive run_read_routed calls."""
    call_count = [0]

    def _se(query: str, params: dict) -> list[dict]:
        idx = call_count[0]
        call_count[0] += 1
        return rows[idx] if idx < len(rows) else []

    return _se


# 20-call canned corpus where fingerprint (call 9) stamps version "1.32.1"
# and the running version is "1.33.0" → tool_version_stale == True.
_STALE_VERSION_ROWS: list[list[dict[str, Any]]] = [
    [{"catalogued_tables": 100, "total_tables": 200}],
    [{"good_edges": 500, "total_edges": 1000}],
    [{"good_edges_strict": 450, "total_edges": 1000}],
    [{"good_edges_scoped": 400, "total_edges_scoped": 900}],
    [{"phantom": 50, "total": 1000}],
    [{"phantom_confirmed": 30, "phantom_contradicted": 5, "phantom_unverified": 15}],
    [{"blindspot": 10}],
    [],  # blindspot_weighted — empty for simplicity
    # fingerprint row with older indexed_sqlcg_version
    [{"files_indexed": 100, "indexed_sha": "abc123", "indexed_sqlcg_version": "1.32.1"}],
    [{"degraded": 1, "total": 100}],
    [],  # degraded_parse_by_dir
    [{"zero_edge_writes": 5, "total_write_queries": 50}],
    [{"cte_collisions": 0}],
    [{"rescuable_unqualified": 0}],
    [{"info_schema_rows": 1000}],
    [{"cte_source_gap_writes": 0}],
    [{"resolvable_write_col_edges": 500}],
    [{"transitive_col_edges": 10}],
    [{"qualify_failed_statements": 0}],
    [],  # Repo path — empty so no catalog check
]

# Same as above but fingerprint has the same version as running → not stale
_FRESH_VERSION_ROWS: list[list[dict[str, Any]]] = [
    row
    if i != 8
    else [{"files_indexed": 100, "indexed_sha": "abc123", "indexed_sqlcg_version": "1.33.0"}]
    for i, row in enumerate(_STALE_VERSION_ROWS)
]

# Same as stale but fingerprint has no indexed_sqlcg_version → legacy graph → not stale
_NULL_VERSION_ROWS: list[list[dict[str, Any]]] = [
    row
    if i != 8
    else [{"files_indexed": 100, "indexed_sha": "abc123", "indexed_sqlcg_version": None}]
    for i, row in enumerate(_STALE_VERSION_ROWS)
]


# ---------------------------------------------------------------------------
# Freshness.tool_version_stale — unit assertions on the dataclass + function
# ---------------------------------------------------------------------------


class TestToolVersionStaleFlag:
    """compute_freshness correctly sets tool_version_stale.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-2 and AC-3.
    """

    def test_stale_when_indexed_version_differs_from_running(self, tmp_path: Path) -> None:
        """tool_version_stale is True when versions differ, even at stale_by_commits==0."""
        f = compute_freshness(
            tmp_path,
            indexed_sha=None,
            indexed_version="1.32.1",
            running_version="1.33.0",
        )
        assert f.tool_version_stale is True

    def test_not_stale_when_versions_match(self, tmp_path: Path) -> None:
        """tool_version_stale is False when indexed and running versions are equal."""
        f = compute_freshness(
            tmp_path,
            indexed_sha=None,
            indexed_version="1.33.0",
            running_version="1.33.0",
        )
        assert f.tool_version_stale is False

    def test_not_stale_when_indexed_version_is_none(self, tmp_path: Path) -> None:
        """Null indexed_version (legacy graph) → tool_version_stale == False.

        Degrade gracefully: never claim staleness we cannot prove.
        Guards AC-3: indexed_version is None → tool_version_stale is False.
        """
        f = compute_freshness(
            tmp_path,
            indexed_sha=None,
            indexed_version=None,
            running_version="1.33.0",
        )
        assert f.tool_version_stale is False

    def test_not_stale_when_running_version_is_none(self, tmp_path: Path) -> None:
        """Running version None → tool_version_stale == False (no proof)."""
        f = compute_freshness(
            tmp_path,
            indexed_sha=None,
            indexed_version="1.32.1",
            running_version=None,
        )
        assert f.tool_version_stale is False

    def test_versions_stored_in_freshness(self, tmp_path: Path) -> None:
        """Freshness carries both indexed_version and running_version."""
        f = compute_freshness(
            tmp_path,
            indexed_sha=None,
            indexed_version="1.32.1",
            running_version="1.33.0",
        )
        assert f.indexed_version == "1.32.1"
        assert f.running_version == "1.33.0"

    def test_stale_with_zero_commit_delta(self, tmp_path: Path) -> None:
        """tool_version_stale is independent of stale_by_commits — stale even at 0."""
        # AC-2: "stale_by_commits == 0" (up to date) but version still differs
        f = Freshness(
            indexed_sha="abc",
            head_sha="abc",
            stale_by_commits=0,
            dirty=False,
            branch="main",
            indexed_version="1.32.1",
            running_version="1.33.0",
            tool_version_stale=True,
        )
        assert f.stale_by_commits == 0
        assert f.tool_version_stale is True


# ---------------------------------------------------------------------------
# render_freshness_line — stale hint appended
# ---------------------------------------------------------------------------


class TestRenderFreshnessLineToolVersionStale:
    """render_freshness_line appends the reindex hint when tool_version_stale.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-2.
    """

    def test_stale_line_contains_reindex_hint(self) -> None:
        """The stale hint substring appears in the rendered line."""
        f = Freshness(
            indexed_sha="abc12345",
            head_sha="abc12345",
            stale_by_commits=0,
            dirty=False,
            branch="main",
            indexed_version="1.32.1",
            running_version="1.33.0",
            tool_version_stale=True,
        )
        line = render_freshness_line(f)
        assert "reindex to pick up parser improvements" in line
        assert "1.32.1" in line
        assert "1.33.0" in line

    def test_no_stale_hint_when_not_stale(self) -> None:
        """No hint appended when versions match."""
        f = Freshness(
            indexed_sha="abc12345",
            head_sha="abc12345",
            stale_by_commits=0,
            dirty=False,
            branch="main",
            indexed_version="1.33.0",
            running_version="1.33.0",
            tool_version_stale=False,
        )
        line = render_freshness_line(f)
        assert "reindex to pick up parser improvements" not in line

    def test_no_stale_hint_when_indexed_version_none(self) -> None:
        """No hint when indexed_version is None (legacy graph)."""
        f = Freshness(
            indexed_sha="abc12345",
            head_sha="abc12345",
            stale_by_commits=0,
            dirty=False,
            branch="main",
            indexed_version=None,
            running_version="1.33.0",
            tool_version_stale=False,
        )
        line = render_freshness_line(f)
        assert "reindex to pick up parser improvements" not in line


# ---------------------------------------------------------------------------
# CoverageStats tool_version_stale — collect_coverage derives it correctly
# ---------------------------------------------------------------------------


class TestCoverageStatsToolVersionStale:
    """collect_coverage() derives tool_version_stale from the fingerprint row.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-4.
    """

    def test_tool_version_stale_true_when_versions_differ(self) -> None:
        """collect_coverage() returns tool_version_stale==True when fingerprint shows old ver."""
        from sqlcg.cli.coverage import collect_coverage

        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=_make_coverage_side_effect(_STALE_VERSION_ROWS),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
            patch("sqlcg.cli.coverage._SQLCG_VERSION", "1.33.0"),
        ):
            stats = collect_coverage()

        assert stats is not None
        assert stats.tool_version_stale is True
        assert stats.indexed_tool_version == "1.32.1"
        assert stats.running_tool_version == "1.33.0"

    def test_tool_version_stale_false_when_versions_match(self) -> None:
        """collect_coverage() returns tool_version_stale==False when versions equal."""
        from sqlcg.cli.coverage import collect_coverage

        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=_make_coverage_side_effect(_FRESH_VERSION_ROWS),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
            patch("sqlcg.cli.coverage._SQLCG_VERSION", "1.33.0"),
        ):
            stats = collect_coverage()

        assert stats is not None
        assert stats.tool_version_stale is False

    def test_tool_version_stale_false_when_indexed_version_none(self) -> None:
        """Null indexed_sqlcg_version in fingerprint → tool_version_stale==False.

        Degrade gracefully on legacy graphs that pre-date v1.33.0.
        Guards AC-3.
        """
        from sqlcg.cli.coverage import collect_coverage

        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=_make_coverage_side_effect(_NULL_VERSION_ROWS),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
            patch("sqlcg.cli.coverage._SQLCG_VERSION", "1.33.0"),
        ):
            stats = collect_coverage()

        assert stats is not None
        assert stats.tool_version_stale is False
        assert stats.indexed_tool_version is None


# ---------------------------------------------------------------------------
# render_coverage_lines — stale warning line
# ---------------------------------------------------------------------------


def _minimal_coverage_stats(**kwargs):
    """Create a CoverageStats with zeroed legacy required fields + any overrides."""
    from sqlcg.cli.coverage import CoverageStats

    defaults = {
        "catalogued_tables": 0,
        "total_tables": 0,
        "good_edges": 0,
        "total_edges": 0,
        "phantom_edges": 0,
        "blindspot_tables": 0,
    }
    defaults.update(kwargs)
    return CoverageStats(**defaults)


class TestRenderCoverageLinesToolVersionStale:
    """render_coverage_lines() emits a warning when tool_version_stale is set.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-4.
    """

    def test_stale_warning_present_in_coverage_lines(self) -> None:
        """A stale-tool warning line appears in render_coverage_lines output."""
        from sqlcg.cli.coverage import render_coverage_lines

        stats = _minimal_coverage_stats(
            tool_version_stale=True,
            indexed_tool_version="1.32.1",
            running_tool_version="1.33.0",
        )
        lines = render_coverage_lines(stats)
        combined = " ".join(lines)
        assert "reindex to pick up parser improvements" in combined
        assert "1.32.1" in combined
        assert "1.33.0" in combined

    def test_no_stale_warning_when_versions_match(self) -> None:
        """No stale warning when tool_version_stale is False."""
        from sqlcg.cli.coverage import render_coverage_lines

        stats = _minimal_coverage_stats(
            tool_version_stale=False,
            indexed_tool_version="1.33.0",
            running_tool_version="1.33.0",
        )
        lines = render_coverage_lines(stats)
        combined = " ".join(lines)
        assert "reindex to pick up parser improvements" not in combined


# ---------------------------------------------------------------------------
# coverage_to_json — new keys present
# ---------------------------------------------------------------------------


class TestCoverageToJsonToolVersionStale:
    """coverage_to_json() carries the three new tool-version keys.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-4
    (gain --json output must carry tool_version_stale + version strings).
    """

    def test_json_contains_tool_version_stale_key(self) -> None:
        """coverage_to_json() returns tool_version_stale as a boolean."""
        from sqlcg.cli.coverage import coverage_to_json

        stats = _minimal_coverage_stats(
            tool_version_stale=True,
            indexed_tool_version="1.32.1",
            running_tool_version="1.33.0",
        )
        j = coverage_to_json(stats)
        assert "tool_version_stale" in j
        assert j["tool_version_stale"] is True

    def test_json_contains_indexed_tool_version_key(self) -> None:
        """coverage_to_json() returns indexed_tool_version."""
        from sqlcg.cli.coverage import coverage_to_json

        stats = _minimal_coverage_stats(indexed_tool_version="1.32.1")
        j = coverage_to_json(stats)
        assert "indexed_tool_version" in j
        assert j["indexed_tool_version"] == "1.32.1"

    def test_json_contains_running_tool_version_key(self) -> None:
        """coverage_to_json() returns running_tool_version."""
        from sqlcg.cli.coverage import coverage_to_json

        stats = _minimal_coverage_stats(running_tool_version="1.33.0")
        j = coverage_to_json(stats)
        assert "running_tool_version" in j
        assert j["running_tool_version"] == "1.33.0"

    def test_json_tool_version_stale_false_when_null_indexed(self) -> None:
        """JSON carries tool_version_stale=False when indexed_tool_version is None."""
        from sqlcg.cli.coverage import coverage_to_json

        stats = _minimal_coverage_stats(
            tool_version_stale=False,
            indexed_tool_version=None,
            running_tool_version="1.33.0",
        )
        j = coverage_to_json(stats)
        assert j["tool_version_stale"] is False
        assert j["indexed_tool_version"] is None
