"""Failing acceptance tests for db info Coverage block (gain_coverage_metrics plan).

Tests must FAIL (or SKIP on missing symbol) before the developer implements the feature.
Plan: plan/sprints/gain_coverage_metrics.md  §"Unit tests — db info Coverage block"
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands.db import app

try:
    from sqlcg.cli.coverage import CoverageStats  # introduced by gain_coverage_metrics
except ImportError:
    pytest.skip("gain_coverage_metrics not yet implemented", allow_module_level=True)

# ---------------------------------------------------------------------------
# Shared mock for the existing db.py run_read_routed calls (non-coverage)
# ---------------------------------------------------------------------------


def _db_run_read_side_effect(query: str, _params: dict):
    """Return minimal non-error responses for all existing db info queries."""
    if "SchemaVersion" in query and "version" in query and "count" not in query.lower():
        return [{"version": "6"}]
    if "indexed_sha" in query:
        return [{"sha": None}]
    if "Repo" in query and "path" in query and "name" not in query and "count" not in query.lower():
        return []
    if "STAR_SOURCE" in query:
        return [{"n": 0}]
    if "STAR_EXPANSION" in query:
        return [{"n": 0}]
    if "Repo" in query and "count" in query.lower():
        return [{"count": 1}]
    if "SqlQuery" in query and "count" in query.lower():
        return [{"count": 10}]
    if "SqlColumn" in query and "count" in query.lower():
        return [{"count": 50}]
    if "COLUMN_LINEAGE" in query and "count" in query.lower():
        return [{"count": 100}]
    if "parsing_mode" in query:
        return []
    return [{"count": 0}]


# ---------------------------------------------------------------------------
# Test: db info prints the Coverage block
# ---------------------------------------------------------------------------


def test_db_info_coverage_prints_coverage_block():
    """db info prints a Coverage block when collect_coverage returns populated stats.

    Plan: append Coverage block in db_info() after the parsing-mode block when
    collect_coverage() returns non-None.
    """
    runner = CliRunner()
    canned_stats = CoverageStats(
        catalogued_tables=528,
        total_tables=2944,
        good_edges=14884,
        total_edges=51434,
        phantom_edges=13412,
        blindspot_tables=1681,
    )

    with (
        patch("sqlcg.cli.commands.db.run_read_routed", side_effect=_db_run_read_side_effect),
        patch("sqlcg.cli.commands.db.collect_coverage", return_value=canned_stats),
    ):
        result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}\n{result.output}"
    assert "Coverage" in result.output, "Expected 'Coverage' heading in db info output"
    assert "Tables with catalog" in result.output
    assert "Edge health" in result.output
    assert "Phantom edges" in result.output
    assert "Blindspot tables" in result.output
    # Spot-check a number that appears only in the coverage section
    assert "528" in result.output
    assert "1681" in result.output


# ---------------------------------------------------------------------------
# Test: db info omits the Coverage block when collect_coverage returns None
# ---------------------------------------------------------------------------


def test_db_info_coverage_omits_block_when_none():
    """db info prints no Coverage block and exits 0 when collect_coverage returns None.

    Plan: when collect_coverage() returns None, print nothing (no error line),
    keeps the command green on an empty graph.
    """
    runner = CliRunner()

    with (
        patch("sqlcg.cli.commands.db.run_read_routed", side_effect=_db_run_read_side_effect),
        patch("sqlcg.cli.commands.db.collect_coverage", return_value=None),
    ):
        result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}\n{result.output}"
    assert "Coverage" not in result.output, (
        "Expected NO Coverage block when collect_coverage returns None"
    )
