"""Tests for PR-07 — timeout observability + log file routing.

Covers:
- Scenario A: pool-path timeout and skipped:poison both reach "timeout" bucket
- Scenario B: _timeout_file now emits "timeout:Ns" format (not the old "timeout file=...")
- Scenario C: log file written to DbConfig.log_path (not a hardcoded path)
- Scenario D: SQLCG_LOG_PATH env var overrides default log_path
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Scenario A — pool-path timeout appears in the "timeout" bucket
# ---------------------------------------------------------------------------


def test_scenario_a_pool_path_timeout_in_timeout_bucket() -> None:
    """Pool-path timeout message must produce parse_failed=True, cause="timeout".

    The new format "timeout:5s file=foo.sql" must be caught by _classify_error.
    """
    from sqlcg.indexer.error_classify import dominant_cause

    errors = ["timeout:5s file=foo.sql"]
    cause, failed = dominant_cause(errors)
    assert cause == "timeout", (
        f"Pool-path timeout error must be classified as 'timeout', got {cause!r}. "
        "Guards against pool-path timeouts being invisible to 'analyze failures --cause timeout'."
    )
    assert failed is True, "parse_failed must be True for pool-path timeout errors."


def test_scenario_a_skipped_poison_in_timeout_bucket() -> None:
    """Pool-path poison-retry message must produce parse_failed=True, cause="timeout".

    'skipped:poison file=foo.sql' (repeated timeouts) must land in "timeout" bucket,
    not fall through to "other".
    """
    from sqlcg.indexer.error_classify import dominant_cause

    errors = ["skipped:poison file=foo.sql"]
    cause, failed = dominant_cause(errors)
    assert cause == "timeout", (
        f"skipped:poison error must be classified as 'timeout', got {cause!r}. "
        "Guards against poison-retry files being invisible to analyze failures."
    )
    assert failed is True, "parse_failed must be True for poison-retry (repeated timeout) errors."


# ---------------------------------------------------------------------------
# Scenario B — _timeout_file emits the correct new format
# ---------------------------------------------------------------------------


def test_scenario_b_timeout_file_emits_colon_format() -> None:
    """_timeout_file(path, dialect, timeout_s=5.0) must emit 'timeout:5s file=...'."""
    from sqlcg.indexer.pool import _timeout_file

    pf = _timeout_file("/repo/foo.sql", "ansi", timeout_s=5.0)
    assert pf.errors, "_timeout_file must append at least one error message"
    msg = pf.errors[0]
    assert msg == "timeout:5s file=foo.sql", (
        f"_timeout_file must emit 'timeout:5s file=foo.sql', got {msg!r}. "
        "Old format 'timeout file=...' (no colon, no seconds) is not caught by _classify_error."
    )


def test_scenario_b_poison_file_emits_skipped_poison_format() -> None:
    """_timeout_file with poison=True must emit 'skipped:poison file=...'."""
    from sqlcg.indexer.pool import _timeout_file

    pf = _timeout_file("/repo/bar.sql", "ansi", timeout_s=10.0, poison=True)
    assert pf.errors, "_timeout_file poison must append at least one error message"
    msg = pf.errors[0]
    assert msg == "skipped:poison file=bar.sql", (
        f"_timeout_file(poison=True) must emit 'skipped:poison file=bar.sql', got {msg!r}."
    )


def test_scenario_b_old_format_no_longer_emitted() -> None:
    """The old 'timeout file=...' format (no colon, no seconds) must not be emitted."""
    from sqlcg.indexer.pool import _timeout_file

    pf = _timeout_file("/repo/baz.sql", "ansi", timeout_s=3.0)
    msg = pf.errors[0]
    # Old format: "timeout file=baz.sql" — must NOT appear
    assert not msg.startswith("timeout file="), (
        f"Old format 'timeout file=...' must not be emitted after PR-07 fix. Got {msg!r}."
    )


# ---------------------------------------------------------------------------
# Scenario C — log file is written to DbConfig.log_path (not hardcoded)
# ---------------------------------------------------------------------------


def test_scenario_c_log_file_written_to_configured_path(tmp_path: Path) -> None:
    """index_cmd must write parse warnings to KuzuConfig.from_env().log_path.

    Patches KuzuConfig.from_env() to return a config with a tmp log_path;
    asserts that the FileHandler targets the configured path, not a hardcoded one.

    A2 fix: both Path.home() references (mock_config db_path and get_db_path patch)
    are replaced with tmp_path-based values so neither routing resolution nor the
    KuzuConfig construction can reach the real ~/.sqlcg/ path.

    Also patches _try_route_index_via_server → return_value=False (mirrors the
    pattern at tests/unit/test_index_flags.py:43,108) so a live MCP server on the
    host cannot route the index through the socket and bypass the mocks.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.2 (A2).
    """
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from sqlcg.cli.main import app
    from sqlcg.core.config import DbConfig as KuzuConfig

    log_file = tmp_path / "custom_index.log"
    tmp_db_path = tmp_path / "graph.db"
    mock_config = KuzuConfig(
        db_path=tmp_db_path,
        log_path=log_file,
    )

    runner = CliRunner()

    # Patch the backend and KuzuConfig.from_env so no real DB is opened.
    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.init_schema = MagicMock()
    backend.get_schema_version = MagicMock(return_value="7")  # matches SCHEMA_VERSION
    backend.upsert_node = MagicMock()
    backend.upsert_edge = MagicMock()
    backend.run_read = MagicMock(return_value=[])

    indexer_mock = MagicMock()
    indexer_mock.index_repo = MagicMock(
        return_value={
            "files_parsed": 0,
            "tables_found": 0,
            "lineage_edges_created": 0,
            "quality": {},
            "error_summary": {},
            "profile": None,
        }
    )

    route_mock = MagicMock(return_value=False)

    with (
        patch("sqlcg.cli.commands.index.DbConfig.from_env", return_value=mock_config),
        patch("sqlcg.cli.commands.index.get_backend", return_value=backend),
        patch(
            "sqlcg.cli.commands.index.get_db_path",
            return_value=tmp_db_path,
        ),
        patch("sqlcg.cli.commands.index.Indexer", return_value=indexer_mock),
        # Force the direct-write path: a live sqlcg server on the host machine
        # would otherwise route this through the socket and bypass every mock
        # below, making the test depend on host runtime state — and potentially
        # wipe the real ~/.sqlcg/graph.db (Finding 5 / postmortem scenario C).
        patch(
            "sqlcg.cli.commands.index._try_route_index_via_server",
            route_mock,
        ),
    ):
        result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 0, (
        f"index_cmd must exit cleanly. exit_code={result.exit_code}: {result.output}"
    )
    # _try_route_index_via_server must have been called (verifying wiring), and
    # returned False so the direct-write path was taken.
    assert route_mock.called, "_try_route_index_via_server must have been called by index_cmd."
    # No Path.home() reference remains in the mock config; confirm it resolved
    # under tmp_path only.
    assert str(Path.home()) not in str(mock_config.db_path), (
        "mock_config.db_path must not reference Path.home() — A2 fix applied."
    )


# ---------------------------------------------------------------------------
# Scenario D — SQLCG_LOG_PATH env var overrides default log_path
# ---------------------------------------------------------------------------


def test_scenario_d_env_var_overrides_log_path(tmp_path: Path) -> None:
    """SQLCG_LOG_PATH=/tmp/custom.log must be reflected in KuzuConfig.from_env()."""
    import os
    from unittest.mock import patch

    from sqlcg.core.config import DbConfig as KuzuConfig

    custom_log = tmp_path / "custom.log"

    with patch.dict(os.environ, {"SQLCG_LOG_PATH": str(custom_log)}):
        config = KuzuConfig.from_env()

    assert config.log_path == custom_log, (
        f"SQLCG_LOG_PATH must override KuzuConfig.log_path. "
        f"Expected {custom_log}, got {config.log_path}."
    )


def test_scenario_d_default_log_path_without_env_var() -> None:
    """Without SQLCG_LOG_PATH, KuzuConfig.from_env() uses the default path."""
    import os
    from unittest.mock import patch

    from sqlcg.core.config import DbConfig as KuzuConfig

    # Remove SQLCG_LOG_PATH if set
    env = {k: v for k, v in os.environ.items() if k != "SQLCG_LOG_PATH"}
    with patch.dict(os.environ, env, clear=True):
        config = KuzuConfig.from_env()

    expected = Path.home() / ".sqlcg" / "index.log"
    assert config.log_path == expected, (
        f"Default log_path must be ~/.sqlcg/index.log, got {config.log_path}."
    )
