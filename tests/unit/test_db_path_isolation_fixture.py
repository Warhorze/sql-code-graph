"""Meta-tests proving the autouse DB-path isolation fixture works correctly.

The autouse fixture in ``tests/conftest.py`` sets ``SQLCG_DB_PATH`` and
``SQLCG_LOG_PATH`` to paths under ``tmp_path`` for every test.  These tests
verify that the plumbing works end-to-end so reviewers can trust the fixture
without inspecting every downstream test.

Guards the test-isolation requirement from
`plan/sprints/sprint_postmortem_fixes.md` §PR-1 Step 1.1.
"""

from __future__ import annotations

from pathlib import Path


def test_get_db_path_resolves_under_tmp(tmp_path: Path) -> None:
    """get_db_path() must resolve to the tmp_path isolation directory, not ~/.sqlcg.

    The autouse ``_isolate_sqlcg_db_paths`` fixture sets SQLCG_DB_PATH to a
    path under ``tmp_path``.  This test verifies that get_db_path() — which
    calls DbConfig.from_env() — returns that overridden value.

    Observable output: the returned path string contains the tmp_path prefix,
    confirming the real ``~/.sqlcg/graph.db`` is never reached.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.1.
    """
    from sqlcg.core.config import get_db_path

    db_path = get_db_path()
    assert str(tmp_path) in str(db_path), (
        f"get_db_path() must resolve under tmp_path ({tmp_path}), "
        f"got {db_path!r}.  The autouse isolation fixture did not take effect."
    )
    assert str(Path.home()) not in str(db_path), (
        f"get_db_path() must NOT resolve under Path.home(), got {db_path!r}."
    )


def test_dbconfig_from_env_resolves_under_tmp(tmp_path: Path) -> None:
    """DbConfig.from_env() must return paths under tmp_path, not ~/.sqlcg.

    Verifies both db_path and log_path are under the isolation directory.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.1.
    """
    from sqlcg.core.config import DbConfig

    config = DbConfig.from_env()
    assert str(tmp_path) in str(config.db_path), (
        f"DbConfig.from_env().db_path must be under tmp_path, got {config.db_path!r}."
    )
    assert str(tmp_path) in str(config.log_path), (
        f"DbConfig.from_env().log_path must be under tmp_path, got {config.log_path!r}."
    )
    assert str(Path.home()) not in str(config.db_path), "db_path must not point under Path.home()."
    assert str(Path.home()) not in str(config.log_path), (
        "log_path must not point under Path.home()."
    )
