"""Integration tests for tool-version stamp (#151).

Exercises the real DuckDB backend (in-memory) to verify:
- A fresh index writes non-null sqlcg_version == sqlcg.__version__ (AC-5)
- get_indexed_version() reads it back correctly
- The schema initialises cleanly at SCHEMA_VERSION == "10" (AC-6)
- Tool-version staleness round-trips through collect_coverage (AC-4) via
  a patched run_read_routed that delegates to the fixture backend

Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A acceptance criteria.
"""

from __future__ import annotations

import sqlcg
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Schema version gate — must be "10" after this PR
# ---------------------------------------------------------------------------


def test_schema_version_is_ten() -> None:
    """SCHEMA_VERSION constant equals '10' after the v9→v10 bump.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-6.
    """
    assert SCHEMA_VERSION == "10"


def test_schema_initialises_cleanly_at_v10() -> None:
    """Opening a fresh in-memory graph at schema v10 succeeds without error.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-6.
    """
    b = DuckDBBackend(":memory:")
    b.init_schema()
    stored = b.get_schema_version()
    b.close()
    assert stored == "10"


# ---------------------------------------------------------------------------
# set_indexed_sha writes sqlcg_version; get_indexed_version reads it back
# ---------------------------------------------------------------------------


def test_fresh_index_writes_nonnull_sqlcg_version() -> None:
    """set_indexed_sha() writes __version__ into sqlcg_version column.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-5:
    'A fresh index writes a non-null sqlcg_version equal to sqlcg.__version__.'
    """
    b = DuckDBBackend(":memory:")
    b.init_schema()
    b.set_indexed_sha("abc12345")
    indexed_version = b.get_indexed_version()
    b.close()

    assert indexed_version is not None
    assert indexed_version == sqlcg.__version__


def test_get_indexed_version_before_index_returns_none() -> None:
    """get_indexed_version() returns None on a graph that was never indexed.

    The schema-init upsert uses NULL-preserving COALESCE so the sqlcg_version
    column stays NULL until set_indexed_sha() is called.
    """
    b = DuckDBBackend(":memory:")
    b.init_schema()
    # Do NOT call set_indexed_sha
    indexed_version = b.get_indexed_version()
    b.close()

    assert indexed_version is None


def test_get_indexed_version_matches_current_version() -> None:
    """get_indexed_version() returns the version string from __init__.py."""
    b = DuckDBBackend(":memory:")
    b.init_schema()
    b.set_indexed_sha("deadbeef")
    ver = b.get_indexed_version()
    b.close()

    assert ver == sqlcg.__version__


# ---------------------------------------------------------------------------
# collect_coverage with real backend via patched run_read_routed
# ---------------------------------------------------------------------------


def test_collect_coverage_derives_tool_version_stale_from_backend() -> None:
    """collect_coverage() sees tool_version_stale==True when backend has an older version.

    Sets sqlcg_version to a fake "0.0.0" in the SchemaVersion row, then patches
    run_read_routed to route through the fixture backend so the fingerprint
    query returns indexed_sqlcg_version="0.0.0" while the running version is
    sqlcg.__version__ (e.g. "1.33.0").

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-4.
    """
    from unittest.mock import patch

    from sqlcg.cli.coverage import collect_coverage

    b = DuckDBBackend(":memory:")
    b.init_schema()
    # Manually write a fake old version to simulate an older index
    b._conn.execute(
        'UPDATE "SchemaVersion" SET sqlcg_version = ? WHERE version = ?',
        ["0.0.0", SCHEMA_VERSION],
    )

    def _route(query: str, params: dict):
        return b.run_read(query, params)

    with (
        patch("sqlcg.cli.coverage.run_read_routed", side_effect=_route),
        patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
    ):
        stats = collect_coverage()

    b.close()

    assert stats is not None
    assert stats.tool_version_stale is True
    assert stats.indexed_tool_version == "0.0.0"
    assert stats.running_tool_version == sqlcg.__version__


def test_collect_coverage_tool_version_stale_false_when_same_version() -> None:
    """collect_coverage() returns tool_version_stale==False when indexed == running.

    Guards plan/sprints/bugfix_audit_findings_151_153.md §PR-A AC-1:
    'Index with the running version → compute_freshness(...) returns
    tool_version_stale == False.'
    """
    from unittest.mock import patch

    from sqlcg.cli.coverage import collect_coverage

    b = DuckDBBackend(":memory:")
    b.init_schema()
    # Write the current version (as if just indexed)
    b.set_indexed_sha("cafebabe")

    def _route(query: str, params: dict):
        return b.run_read(query, params)

    with (
        patch("sqlcg.cli.coverage.run_read_routed", side_effect=_route),
        patch("sqlcg.cli.coverage.get_catalog_path", return_value=None),
    ):
        stats = collect_coverage()

    b.close()

    assert stats is not None
    assert stats.tool_version_stale is False
    assert stats.indexed_tool_version == sqlcg.__version__
