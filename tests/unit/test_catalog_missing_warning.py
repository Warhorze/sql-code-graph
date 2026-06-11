"""Unit tests for PR 4 Step 4.3 — §G catalog-missing warning line.

render_coverage_lines emits a warning when the graph has zero
information_schema-sourced HAS_COLUMN rows:
- Plain hint wording when no root is available (or catalog not configured).
- Stronger "catalog path configured" wording when indexed_repo_root is set
  (N3: get_catalog_path found a configured path for that root).
- Warning is absent when info_schema_has_column_rows >= 1.

([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sqlcg.cli.coverage import (
    CoverageStats,
    collect_coverage,
    render_coverage_lines,
)

# ---------------------------------------------------------------------------
# Minimal CoverageStats factory
# ---------------------------------------------------------------------------


def _minimal_stats(**overrides: Any) -> CoverageStats:
    """Return a CoverageStats populated with benign defaults, with overrides applied."""
    defaults: dict[str, Any] = dict(
        catalogued_tables=100,
        total_tables=200,
        good_edges=500,
        total_edges=1000,
        phantom_edges=50,
        blindspot_tables=10,
    )
    defaults.update(overrides)
    return CoverageStats(**defaults)


# ---------------------------------------------------------------------------
# render_coverage_lines — catalog-missing warning
# ---------------------------------------------------------------------------


class TestCatalogMissingWarningRendering:
    """render_coverage_lines emits the §G catalog-missing warning iff info-schema rows == 0.

    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3)
    """

    def test_warning_present_when_zero_info_schema_rows_no_root(self):
        """Zero info-schema rows and no configured root → hint wording present.

        The plain hint variant fires when indexed_repo_root is None (either no
        root is stored or the catalog path is not configured for it).
        """
        stats = _minimal_stats(info_schema_has_column_rows=0, indexed_repo_root=None)
        lines = render_coverage_lines(stats)

        warning_lines = [
            ln for ln in lines if "catalog load" in ln.lower() or "information_schema" in ln.lower()
        ]
        assert warning_lines, (
            f"Expected a catalog-load hint line when info_schema_has_column_rows=0, "
            f"got no match in:\n{lines}"
        )
        # Should mention the sqlcg catalog load command.
        combined = " ".join(warning_lines)
        assert "sqlcg catalog load" in combined, (
            f"Expected 'sqlcg catalog load' in warning line(s): {warning_lines}"
        )

    def test_stronger_warning_when_catalog_path_configured(self):
        """Zero info-schema rows + catalog path configured → stronger wording.

        When indexed_repo_root is set (meaning get_catalog_path returned a path),
        the warning says "a catalog path is configured but the graph has no
        information_schema rows".

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3 N3)
        """
        stats = _minimal_stats(
            info_schema_has_column_rows=0,
            indexed_repo_root="/repo",  # non-None → stronger wording
        )
        lines = render_coverage_lines(stats)

        warning_lines = [ln for ln in lines if "catalog path is configured" in ln]
        assert warning_lines, (
            f"Expected 'catalog path is configured' warning when indexed_repo_root set, "
            f"got:\n{lines}"
        )
        assert any("information_schema" in ln for ln in warning_lines), (
            f"Warning must mention 'information_schema': {warning_lines}"
        )

    def test_warning_absent_when_info_schema_rows_present(self):
        """One or more info-schema rows → NO catalog-missing warning line.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3)
        """
        stats = _minimal_stats(info_schema_has_column_rows=42, indexed_repo_root=None)
        lines = render_coverage_lines(stats)

        hint_lines = [ln for ln in lines if "catalog load" in ln.lower() and "hint" in ln.lower()]
        warning_lines = [ln for ln in lines if "catalog path is configured" in ln]
        assert not hint_lines and not warning_lines, (
            f"Expected no catalog-load hint/warning when info_schema_has_column_rows=42, "
            f"got:\n{[ln for ln in lines if 'catalog' in ln.lower()]}"
        )

    def test_warning_absent_with_root_and_info_schema_rows(self):
        """Root configured + info-schema rows present → NO warning.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3)
        """
        stats = _minimal_stats(
            info_schema_has_column_rows=100,
            indexed_repo_root="/repo",
        )
        lines = render_coverage_lines(stats)

        warning_lines = [ln for ln in lines if "catalog path is configured" in ln]
        assert not warning_lines, (
            f"Expected no 'catalog path configured' warning with rows present:\n{lines}"
        )


# ---------------------------------------------------------------------------
# collect_coverage — info_schema_has_column_rows populated
# ---------------------------------------------------------------------------


class TestCollectCoverageInfoSchemaField:
    """collect_coverage populates info_schema_has_column_rows from _Q_INFO_SCHEMA_ROWS.

    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.3)
    """

    def _canned_side_effect(self, info_schema_rows: int, repo_path: str | None = None):
        """Return a run_read_routed side_effect yielding canned rows in call order."""
        # Same order as collect_coverage's queries (16 total: 14 existing + info_schema + Repo)
        responses = [
            [{"catalogued_tables": 10, "total_tables": 20}],  # 1 catalog_coverage
            [{"good_edges": 8, "total_edges": 10}],  # 2 edge_health
            [{"good_edges_strict": 7, "total_edges": 10}],  # 3 edge_health_strict
            [{"good_edges_scoped": 6, "total_edges_scoped": 9}],  # 4 edge_health_scoped
            [{"phantom": 2, "total": 10}],  # 5 phantom_rate
            [{"phantom_confirmed": 1, "phantom_contradicted": 0, "phantom_unverified": 1}],  # 6
            [{"blindspot": 2}],  # 7 blindspot
            [],  # 8 blindspot_weighted
            [{"files_indexed": 5, "indexed_sha": "x"}],  # 9 fingerprint
            [{"degraded": 0, "total": 5}],  # 10 degraded_parse_overall
            [],  # 11 degraded_parse_by_dir
            [{"zero_edge_writes": 0, "total_write_queries": 5}],  # 12 zero_edge_writes
            [{"cte_collisions": 0}],  # 13 cte_collisions
            [{"rescuable_unqualified": 0}],  # 14 rescuable_unqualified
            [{"info_schema_rows": info_schema_rows}],  # 15 info_schema
            [{"path": repo_path}] if repo_path else [],  # 16 Repo
        ]
        idx = [0]

        def _se(query: str, params: dict):
            i = idx[0]
            idx[0] += 1
            return responses[i] if i < len(responses) else []

        return _se

    def test_info_schema_rows_populated_from_query(self):
        """collect_coverage reads info_schema_rows and stores it in the stats object."""
        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=self._canned_side_effect(info_schema_rows=99),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
        ):
            stats = collect_coverage()

        assert stats is not None
        assert stats.info_schema_has_column_rows == 99, (
            f"Expected info_schema_has_column_rows=99, got {stats.info_schema_has_column_rows}"
        )

    def test_zero_info_schema_rows_triggers_warning_via_render(self):
        """collect_coverage + render_coverage_lines: zero rows → warning line present."""
        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=self._canned_side_effect(info_schema_rows=0),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
        ):
            stats = collect_coverage()

        assert stats is not None
        lines = render_coverage_lines(stats)
        hint_lines = [ln for ln in lines if "sqlcg catalog load" in ln]
        assert hint_lines, f"Expected catalog-load hint when rows=0, got none in:\n{lines}"

    def test_nonzero_info_schema_rows_no_warning_via_render(self):
        """collect_coverage + render_coverage_lines: non-zero rows → no warning line."""
        with (
            patch(
                "sqlcg.cli.coverage.run_read_routed",
                side_effect=self._canned_side_effect(info_schema_rows=50),
            ),
            patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
        ):
            stats = collect_coverage()

        assert stats is not None
        lines = render_coverage_lines(stats)
        hint_lines = [ln for ln in lines if "sqlcg catalog load" in ln and "Hint" in ln]
        assert not hint_lines, f"Expected no catalog-load hint when rows=50, got:\n{hint_lines}"
