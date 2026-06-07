"""Tests for sqlcg install command and mcp setup --write (Claude Code MCP registration).

PR-02 (#23): install now writes via `claude mcp add -s user` when the claude binary
is on PATH, and falls back to ~/.claude.json when it is not. The previous target
~/.claude/settings.json has been removed.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands import mcp as mcp_commands
from sqlcg.cli.main import app

runner = CliRunner()


def _which_sqlcg(cmd: str) -> str | None:
    return "/usr/local/bin/sqlcg" if cmd == "sqlcg" else None


def _which_uvx(cmd: str) -> str | None:
    return "/usr/bin/uvx" if cmd == "uvx" else None


def _which_nothing(_cmd: str) -> None:
    return None


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path so tests never touch ~/.claude."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# PR-02 Scenario A — claude available, claude mcp add succeeds
# ---------------------------------------------------------------------------


def test_scenario_a_claude_available_runs_claude_mcp_add(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When claude is on PATH and claude mcp add returns 0, no file is written."""

    def which_impl(cmd: str) -> str | None:
        if cmd in ("sqlcg", "claude"):
            return f"/usr/local/bin/{cmd}"
        return None

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stderr = ""

    with patch("shutil.which", side_effect=which_impl):
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"

    # claude mcp add must have been called
    subprocess_calls = mock_run.call_args_list
    claude_call = next(
        (c for c in subprocess_calls if c.args[0][0] == "claude"),
        None,
    )
    assert claude_call is not None, (
        "subprocess.run must be called with ['claude', 'mcp', 'add', ...]. "
        f"Actual calls: {subprocess_calls}"
    )
    call_args = claude_call.args[0]
    assert "mcp" in call_args and "add" in call_args and "-s" in call_args, (
        f"claude mcp add -s user must be in call args: {call_args}"
    )
    assert "sql-code-graph" in call_args, f"Server key missing from args: {call_args}"

    # No ~/.claude.json should have been written
    assert not (fake_home / ".claude.json").exists(), (
        "No ~/.claude.json should be written when claude mcp add succeeds"
    )


# ---------------------------------------------------------------------------
# PR-02 Scenario B — claude not available, falls back to ~/.claude.json
# ---------------------------------------------------------------------------


def test_scenario_b_claude_not_available_writes_claude_json(
    fake_home: Path,
) -> None:
    """When claude is not on PATH, install falls back to writing ~/.claude.json."""

    def which_impl(cmd: str) -> str | None:
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        return None  # claude not available

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"

    claude_json = fake_home / ".claude.json"
    assert claude_json.exists(), "~/.claude.json must be written as fallback"

    data = json.loads(claude_json.read_text())
    entry = data.get("mcpServers", {}).get("user", {}).get("sql-code-graph")
    assert entry is not None, "mcpServers.user.sql-code-graph must be present in ~/.claude.json"
    assert entry["command"] == "sqlcg"
    assert entry["args"] == ["mcp", "start"]

    # Old path must not have been touched
    assert not (fake_home / ".claude" / "settings.json").exists(), (
        "~/.claude/settings.json must never be written by install_cmd"
    )


# ---------------------------------------------------------------------------
# PR-02 Scenario C — --dry-run prints command, writes nothing
# ---------------------------------------------------------------------------


def test_scenario_c_dry_run_prints_command_no_write(
    fake_home: Path,
) -> None:
    """--dry-run shows what would be run, writes no files."""

    def which_impl(cmd: str) -> str | None:
        if cmd in ("sqlcg", "claude"):
            return f"/usr/local/bin/{cmd}"
        return None

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--dry-run", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert not (fake_home / ".claude.json").exists(), "No file should be written with --dry-run"
    assert not (fake_home / ".claude" / "settings.json").exists(), (
        "No settings.json should be written with --dry-run"
    )
    # Output should mention the claude mcp add command or sql-code-graph
    assert "sql-code-graph" in result.output, (
        f"Output must mention the server key. Got: {result.output}"
    )


# ---------------------------------------------------------------------------
# Fallback path: neither sqlcg nor uvx on PATH → error
# ---------------------------------------------------------------------------


def test_neither_on_path_exits_with_error(fake_home: Path) -> None:
    """Exit with error when neither sqlcg nor uvx is on PATH."""
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code != 0, "Expected non-zero exit when neither tool is on PATH"
    assert "found on PATH" in result.output or "Neither" in result.output


# ---------------------------------------------------------------------------
# Fallback path: ~/.claude.json merge behaviour
# ---------------------------------------------------------------------------


def test_fallback_merges_into_existing_claude_json(
    fake_home: Path,
) -> None:
    """Existing keys in ~/.claude.json are preserved when adding the sqlcg entry."""
    claude_json = fake_home / ".claude.json"
    claude_json.write_text(json.dumps({"theme": "dark", "mcpServers": {"user": {"other": {}}}}))

    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])

    data = json.loads(claude_json.read_text())
    assert data["theme"] == "dark"
    assert "other" in data["mcpServers"]["user"]
    assert "sql-code-graph" in data["mcpServers"]["user"]


def test_fallback_idempotent_already_configured(
    fake_home: Path,
) -> None:
    """Running install twice does not duplicate the key in ~/.claude.json."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
        result2 = runner.invoke(app, ["install", "--scope", "global"])

    assert result2.exit_code == 0
    # Check "Already configured" message
    assert "Already configured" in result2.output

    claude_json = fake_home / ".claude.json"
    data = json.loads(claude_json.read_text())
    keys = list(data.get("mcpServers", {}).get("user", {}).keys())
    assert keys.count("sql-code-graph") == 1


def test_fallback_survives_malformed_claude_json(
    fake_home: Path,
) -> None:
    """Malformed ~/.claude.json is overwritten with a valid structure."""
    claude_json = fake_home / ".claude.json"
    claude_json.write_text("{ not valid json !!!")

    with patch("shutil.which", side_effect=_which_sqlcg):
        result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0
    data = json.loads(claude_json.read_text())
    assert "sql-code-graph" in data["mcpServers"]["user"]


# ---------------------------------------------------------------------------
# mcp setup --write writes to ~/.claude.json (not settings.json)
# ---------------------------------------------------------------------------


def test_mcp_setup_write_writes_claude_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp setup --write writes to ~/.claude.json, not ~/.claude/settings.json."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("shutil.which", side_effect=_which_sqlcg):
        result = CliRunner().invoke(mcp_commands.app, ["setup", "--write"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    claude_json = tmp_path / ".claude.json"
    assert claude_json.exists(), "~/.claude.json must be written by mcp setup --write"
    data = json.loads(claude_json.read_text())
    assert "sql-code-graph" in data["mcpServers"]["user"]
    # Old settings.json path must not be touched
    assert not (tmp_path / ".claude" / "settings.json").exists()


# ---------------------------------------------------------------------------
# Phase 5 — install stops a stale running MCP server (version-parity-and-restart)
# ---------------------------------------------------------------------------


def test_install_stops_server_on_claude_cli_success(fake_home: Path) -> None:
    """`claude mcp add` success path calls stop_server() exactly once and reports it."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stderr = ""

    def which_impl(cmd: str) -> str | None:
        if cmd in ("sqlcg", "claude"):
            return f"/usr/local/bin/{cmd}"
        return None

    with patch("shutil.which", side_effect=which_impl):
        with patch("subprocess.run", return_value=mock_proc):
            with patch("sqlcg.cli.commands.install.stop_server", return_value=True) as mock_stop:
                result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    mock_stop.assert_called_once()
    assert "Stopped running MCP server" in result.output
    assert "respawn it on the new build" in result.output
    # The message must drop the (vX) suffix — stop_server() returns only a bool.
    assert "(v" not in result.output


def test_install_stops_server_on_already_configured(fake_home: Path) -> None:
    """The 'Already configured' branch still calls stop_server() exactly once.

    This is the reinstall case: the config did not change but the binary on
    disk did, so the stale running server still must be stopped.
    """
    with patch("shutil.which", side_effect=_which_sqlcg):
        with patch("sqlcg.cli.commands.install.stop_server", return_value=True) as mock_stop:
            runner.invoke(app, ["install", "--scope", "global"])
            mock_stop.reset_mock()
            result2 = runner.invoke(app, ["install", "--scope", "global"])

    assert "Already configured" in result2.output
    mock_stop.assert_called_once()
    assert "Stopped running MCP server" in result2.output


def test_install_stops_server_on_fallback_write_path(fake_home: Path) -> None:
    """The ~/.claude.json fallback-write success path calls stop_server() exactly once."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        with patch("sqlcg.cli.commands.install.stop_server", return_value=True) as mock_stop:
            result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    mock_stop.assert_called_once()
    assert "Stopped running MCP server" in result.output


def test_install_no_running_server_prints_no_op_message(fake_home: Path) -> None:
    """When stop_server() finds nothing, install prints the no-op refresh message."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        with patch("sqlcg.cli.commands.install.stop_server", return_value=False) as mock_stop:
            result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    mock_stop.assert_called_once()
    assert "No running MCP server to refresh." in result.output
    assert "Stopped running MCP server" not in result.output


def test_install_dry_run_never_stops_server(fake_home: Path) -> None:
    """--dry-run never calls stop_server() and prints the would-stop preview line."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        with patch("sqlcg.cli.commands.install.stop_server") as mock_stop:
            result = runner.invoke(app, ["install", "--dry-run", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    mock_stop.assert_not_called()
    assert "would stop running MCP server" in result.output


def test_mcp_setup_write_merges_into_existing_claude_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp setup --write preserves existing keys in ~/.claude.json."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"theme": "dark", "mcpServers": {"user": {"other": {}}}}))

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("shutil.which", side_effect=_which_sqlcg):
        result = CliRunner().invoke(mcp_commands.app, ["setup", "--write"])

    assert result.exit_code == 0
    data = json.loads(claude_json.read_text())
    assert data["theme"] == "dark"
    assert "other" in data["mcpServers"]["user"]
    assert "sql-code-graph" in data["mcpServers"]["user"]
    assert data["mcpServers"]["user"]["sql-code-graph"]["command"] == "sqlcg"
