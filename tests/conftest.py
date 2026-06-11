"""Top-level pytest configuration for the sqlcg test suite.

Provides an autouse fixture that pins the DuckDB and log paths to a per-test
``tmp_path`` directory via ``SQLCG_DB_PATH`` and ``SQLCG_LOG_PATH`` environment
variables.  This prevents any test from accidentally touching the user's real
``~/.sqlcg/`` graph or log.

The fixture is intentionally env-var-only; it does NOT patch ``Path.home()``,
which means it is a safety net — not a substitute for the targeted
``_try_route_index_via_server`` and ``Path.home()`` patches in tests that
exercise paths that resolve off the home directory directly.

Guards the test-isolation requirement from
`plan/sprints/sprint_postmortem_fixes.md` §PR-1 Step 1.1.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_sqlcg_db_paths(tmp_path, monkeypatch):
    """Pin SQLCG_DB_PATH and SQLCG_LOG_PATH under tmp_path for every test.

    ``DbConfig.from_env()`` and ``get_db_path()`` read these env vars, so
    this fixture ensures neither can resolve to the user's real
    ``~/.sqlcg/`` directory during any test run.

    The paths are placed under a ``sqlcg_isolation/`` sub-directory of the
    test's own ``tmp_path`` so they never collide with files the test itself
    creates directly under ``tmp_path``.

    Guards the test-isolation requirement from
    `plan/sprints/sprint_postmortem_fixes.md` §PR-1 Step 1.1.
    """
    isolation_dir = tmp_path / "sqlcg_isolation"
    isolation_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SQLCG_DB_PATH", str(isolation_dir / "graph.db"))
    monkeypatch.setenv("SQLCG_LOG_PATH", str(isolation_dir / "index.log"))
