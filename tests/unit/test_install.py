"""Tests for sqlcg install command and mcp setup --write (Claude Code MCP registration)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands import mcp as mcp_commands
from sqlcg.cli.main import app

runner = CliRunner()


def _which_fallback_to_sqlcg(cmd: str) -> str | None:
    """Mock implementation: return sqlcg if asked, None otherwise."""
    if cmd == "sqlcg":
        return "/usr/local/bin/sqlcg"
    return None


def _which_fallback_to_uvx(cmd: str) -> str | None:
    """Mock implementation: return uvx if asked, None otherwise."""
    if cmd == "uvx":
        return "/usr/bin/uvx"
    return None


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path so tests never touch ~/.claude."""
    monkeypatch.setattr(
        "sqlcg.cli.commands.install._SETTINGS_PATH",
        tmp_path / ".claude" / "settings.json",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# sqlcg / uvx command selection — priority order
# ---------------------------------------------------------------------------


def test_scenario_a_prefers_sqlcg_when_available(fake_home: Path) -> None:
    """Guard: sqlcg binary takes priority over uvx when both are on PATH."""

    def which_impl(cmd: str) -> str | None:
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        elif cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}: {result.output}"
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    entry = settings["mcpServers"]["sql-code-graph"]
    assert entry["command"] == "sqlcg", (
        f"When sqlcg is on PATH, entry command must be 'sqlcg', got {entry['command']}"
    )
    assert entry["args"] == ["mcp", "start"]


def test_scenario_b_falls_back_to_uvx_when_no_sqlcg(fake_home: Path) -> None:
    """Guard: uvx is used as fallback when sqlcg is not on PATH."""

    def which_impl(cmd: str) -> str | None:
        if cmd == "sqlcg":
            return None
        elif cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    entry = settings["mcpServers"]["sql-code-graph"]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["sql-code-graph", "mcp", "start"]


def test_scenario_c_updates_uvx_to_sqlcg(fake_home: Path) -> None:
    """Guard: existing uvx entry is updated to sqlcg when sqlcg becomes available."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    # Pre-populate with uvx entry
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sql-code-graph": {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
                }
            }
        )
    )

    def which_impl(cmd: str) -> str | None:
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        elif cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0
    assert "Updating" in result.output or "faster startup" in result.output, (
        "When switching from uvx to sqlcg, upgrade notice should be printed. "
        f"Output: {result.output}"
    )
    settings = json.loads(settings_path.read_text())
    entry = settings["mcpServers"]["sql-code-graph"]
    assert entry["command"] == "sqlcg", (
        f"Entry command should be updated to 'sqlcg', got {entry['command']}"
    )


def test_scenario_d_neither_on_path(fake_home: Path) -> None:
    """Guard: exit with error when neither sqlcg nor uvx is on PATH."""
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code != 0, "Expected non-zero exit when neither tool is on PATH"
    assert "found on PATH" in result.output or "Neither" in result.output, (
        f"Error message should mention tools not on PATH. Output: {result.output}"
    )


def test_scenario_e_dry_run_with_sqlcg(fake_home: Path) -> None:
    """Guard: --dry-run shows correct config when sqlcg is available."""
    with patch("shutil.which", return_value="/usr/local/bin/sqlcg"):
        result = runner.invoke(app, ["install", "--dry-run", "--scope", "global"])
    assert result.exit_code == 0
    assert not (fake_home / ".claude" / "settings.json").exists(), (
        "No file should be written with --dry-run"
    )
    assert "sqlcg" in result.output


# ---------------------------------------------------------------------------
# File creation and merging
# ---------------------------------------------------------------------------


def test_creates_settings_file_when_absent(fake_home: Path) -> None:
    def which_impl(cmd: str) -> str | None:
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        return None

    with patch("shutil.which", side_effect=which_impl):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0
    assert (fake_home / ".claude" / "settings.json").exists()


def test_creates_parent_directory_when_absent(fake_home: Path) -> None:
    assert not (fake_home / ".claude").exists()
    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
    assert (fake_home / ".claude").is_dir()


def test_merges_into_existing_settings(fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "dark", "otherKey": 42}) + "\n")

    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])

    settings = json.loads(settings_path.read_text())
    assert settings["theme"] == "dark"
    assert settings["otherKey"] == 42
    assert "sql-code-graph" in settings["mcpServers"]


def test_does_not_clobber_existing_mcp_servers(fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    existing = {"mcpServers": {"other-tool": {"command": "other", "args": []}}}
    settings_path.write_text(json.dumps(existing))

    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])

    settings = json.loads(settings_path.read_text())
    assert "other-tool" in settings["mcpServers"]
    assert "sql-code-graph" in settings["mcpServers"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_uvx(fake_home: Path) -> None:
    with patch("shutil.which", side_effect=_which_fallback_to_uvx):
        runner.invoke(app, ["install", "--scope", "global"])
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0
    assert "Already configured" in result.output


def test_idempotent_does_not_duplicate_key(fake_home: Path) -> None:
    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
        runner.invoke(app, ["install", "--scope", "global"])

    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    keys = list(settings["mcpServers"].keys())
    assert keys.count("sql-code-graph") == 1


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_config_without_writing(fake_home: Path) -> None:
    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        result = runner.invoke(app, ["install", "--dry-run", "--scope", "global"])
    assert result.exit_code == 0
    assert not (fake_home / ".claude" / "settings.json").exists()
    assert "sql-code-graph" in result.output


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


def test_survives_malformed_existing_json(fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{ not valid json !!!")

    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        result = runner.invoke(app, ["install", "--scope", "global"])
    assert result.exit_code == 0
    settings = json.loads(settings_path.read_text())
    assert "sql-code-graph" in settings["mcpServers"]


# ---------------------------------------------------------------------------
# mcp setup --write
# ---------------------------------------------------------------------------


def test_mcp_setup_write_merges_into_settings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {}}}))

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        result = CliRunner().invoke(mcp_commands.app, ["setup", "--write"])

    assert result.exit_code == 0
    settings = json.loads(settings_path.read_text())
    assert settings["theme"] == "dark"
    assert "other" in settings["mcpServers"]
    assert "sql-code-graph" in settings["mcpServers"]
    assert settings["mcpServers"]["sql-code-graph"]["command"] == "sqlcg"


def test_mcp_setup_write_does_not_write_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("shutil.which", side_effect=_which_fallback_to_sqlcg):
        CliRunner().invoke(mcp_commands.app, ["setup", "--write"])

    assert not (tmp_path / ".claude" / "mcp.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
