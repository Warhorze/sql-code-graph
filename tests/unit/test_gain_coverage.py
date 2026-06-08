"""Failing acceptance tests for gain Section G (gain_coverage_metrics plan).

Tests must FAIL (or SKIP on missing symbol) before the developer implements the feature.
Plan: plan/sprints/gain_coverage_metrics.md  §"Unit tests — gain Section G"
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.cli.commands.gain import gain_cmd

try:
    from sqlcg.cli.coverage import CoverageStats  # introduced by gain_coverage_metrics
except ImportError:
    pytest.skip("gain_coverage_metrics not yet implemented", allow_module_level=True)

# ---------------------------------------------------------------------------
# Helpers — metrics store mock (mirrors test_gain_ratio.py)
# ---------------------------------------------------------------------------


def _make_metrics_mock():
    """Return a MetricsStore mock that responds to all gain queries."""
    mock = MagicMock()

    def _execute(query: str, params=None):
        if "COUNT(*) as count FROM tool_calls" in query and "WHERE timestamp" not in query:
            return [[10]]
        if "WHERE timestamp" in query:
            return [[3]]
        if "index_runs" in query:
            return []
        if "feedback" in query:
            return [[0, 0]]
        if "GROUP BY tool_name" in query:
            return [["trace_column_lineage", 10]]
        if "execute_sql" in query and "WHERE" in query:
            return [[1]]
        return []

    mock.execute_query.side_effect = _execute
    mock.init_schema.return_value = None
    return mock


_CANNED_STATS = CoverageStats(
    catalogued_tables=528,
    total_tables=2944,
    good_edges=14884,
    total_edges=51434,
    phantom_edges=13412,
    blindspot_tables=1681,
)


# ---------------------------------------------------------------------------
# Test: gain prints Section G in human-readable output
# ---------------------------------------------------------------------------


def test_gain_coverage_prints_section_G(tmp_path, capsys):
    """gain prints G. Coverage with the four metrics when collect_coverage returns stats.

    Plan: new Section G in gain after Section F.
    """
    metrics_db = tmp_path / "metrics.db"
    metrics_db.touch()

    with (
        patch("sqlcg.metrics.store.MetricsStore", return_value=_make_metrics_mock()),
        patch("sqlcg.cli.commands.gain.collect_coverage", return_value=_CANNED_STATS),
        patch("sqlcg.cli.commands.gain.run_read_routed", return_value=[]),
    ):
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with patch("sqlcg.cli.commands.gain.console", Console(file=buf, highlight=False)):
            gain_cmd(_metrics_path=metrics_db)
        output = buf.getvalue()

    assert "G. Coverage" in output or "Coverage" in output, (
        f"Expected 'G. Coverage' heading in gain output.\nOutput:\n{output}"
    )
    assert "Tables with catalog" in output or "catalogued" in output.lower()
    assert "Edge health" in output
    assert "528" in output
    assert "1681" in output


# ---------------------------------------------------------------------------
# Test: gain --json includes coverage object
# ---------------------------------------------------------------------------


def test_gain_coverage_json_includes_coverage_object(tmp_path, capsys):
    """gain --json includes a 'coverage' object with the six integer fields.

    Plan: add payload["coverage"] = {...} only when coverage is not None.
    """
    metrics_db = tmp_path / "metrics.db"
    metrics_db.touch()

    with (
        patch("sqlcg.metrics.store.MetricsStore", return_value=_make_metrics_mock()),
        patch("sqlcg.cli.commands.gain.collect_coverage", return_value=_CANNED_STATS),
        patch("sqlcg.cli.commands.gain.run_read_routed", return_value=[]),
    ):
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with patch("sqlcg.cli.commands.gain.console", Console(file=buf, highlight=False)):
            gain_cmd(json_output=True, _metrics_path=metrics_db)
        raw = buf.getvalue().strip()

    payload = json.loads(raw)
    assert "coverage" in payload, f"Expected 'coverage' key in JSON payload.\nPayload: {payload}"
    cov = payload["coverage"]
    assert cov["catalogued_tables"] == 528
    assert cov["total_tables"] == 2944
    assert cov["good_edges"] == 14884
    assert cov["total_edges"] == 51434
    assert cov["phantom_edges"] == 13412
    assert cov["blindspot_tables"] == 1681


# ---------------------------------------------------------------------------
# Test: gain omits Section G when coverage unavailable
# ---------------------------------------------------------------------------


def test_gain_coverage_omits_section_G_when_none(tmp_path):
    """gain omits Section G, exits 0, and existing sections still print when collect_coverage
    returns None. Regression guard: Section G failure must not take down the rest of gain.

    Plan: omit Section G when coverage is None, matching Section F semantics.
    """
    metrics_db = tmp_path / "metrics.db"
    metrics_db.touch()

    with (
        patch("sqlcg.metrics.store.MetricsStore", return_value=_make_metrics_mock()),
        patch("sqlcg.cli.commands.gain.collect_coverage", return_value=None),
        patch("sqlcg.cli.commands.gain.run_read_routed", return_value=[]),
    ):
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with patch("sqlcg.cli.commands.gain.console", Console(file=buf, highlight=False)):
            gain_cmd(_metrics_path=metrics_db)
        output = buf.getvalue()

    assert "G. Coverage" not in output, (
        "Expected NO G. Coverage section when collect_coverage returns None"
    )
    # Existing sections still appear (Section A is always printed)
    assert "Tool Calls" in output or "A." in output, (
        "Expected existing sections to still print even when coverage is None"
    )
