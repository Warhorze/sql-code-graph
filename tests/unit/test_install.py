"""Tests for sqlcg install command and mcp setup --write (Claude Code MCP registration)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands import mcp as mcp_commands
from sqlcg.cli.main import app

runner = CliRunner()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path so tests never touch ~/.claude."""
    monkeypatch.setattr(
        "sqlcg.cli.commands.install._SETTINGS_PATH",
        tmp_path / ".claude" / "settings.json",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# uvx / pip command selection
# ---------------------------------------------------------------------------

def test_uses_uvx_when_available(fake_home: Path) -> None:
    with patch("shutil.which", return_value="/usr/bin/uvx"):
        result = runner.invoke(app, ["install"])
    assert result.exit_code == 0
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    entry = settings["mcpServers"]["sql-code-graph"]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["sql-code-graph", "mcp", "start"]


def test_falls_back_to_sqlcg_when_no_uvx(fake_home: Path) -> None:
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install"])
    assert result.exit_code == 0
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    entry = settings["mcpServers"]["sql-code-graph"]
    assert entry["command"] == "sqlcg"
    assert entry["args"] == ["mcp", "start"]


# ---------------------------------------------------------------------------
# File creation and merging
# ---------------------------------------------------------------------------

def test_creates_settings_file_when_absent(fake_home: Path) -> None:
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install"])
    assert result.exit_code == 0
    assert (fake_home / ".claude" / "settings.json").exists()


def test_creates_parent_directory_when_absent(fake_home: Path) -> None:
    assert not (fake_home / ".claude").exists()
    with patch("shutil.which", return_value=None):
        runner.invoke(app, ["install"])
    assert (fake_home / ".claude").is_dir()


def test_merges_into_existing_settings(fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "dark", "otherKey": 42}) + "\n")

    with patch("shutil.which", return_value=None):
        runner.invoke(app, ["install"])

    settings = json.loads(settings_path.read_text())
    assert settings["theme"] == "dark"
    assert settings["otherKey"] == 42
    assert "sql-code-graph" in settings["mcpServers"]


def test_does_not_clobber_existing_mcp_servers(fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    existing = {"mcpServers": {"other-tool": {"command": "other", "args": []}}}
    settings_path.write_text(json.dumps(existing))

    with patch("shutil.which", return_value=None):
        runner.invoke(app, ["install"])

    settings = json.loads(settings_path.read_text())
    assert "other-tool" in settings["mcpServers"]
    assert "sql-code-graph" in settings["mcpServers"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotent_uvx(fake_home: Path) -> None:
    with patch("shutil.which", return_value="/usr/bin/uvx"):
        runner.invoke(app, ["install"])
        result = runner.invoke(app, ["install"])
    assert result.exit_code == 0
    assert "Already configured" in result.output


def test_idempotent_does_not_duplicate_key(fake_home: Path) -> None:
    with patch("shutil.which", return_value=None):
        runner.invoke(app, ["install"])
        runner.invoke(app, ["install"])

    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    keys = list(settings["mcpServers"].keys())
    assert keys.count("sql-code-graph") == 1


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_prints_config_without_writing(fake_home: Path) -> None:
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install", "--dry-run"])
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

    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["install"])
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
    with patch("shutil.which", return_value=None):
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
    with patch("shutil.which", return_value=None):
        CliRunner().invoke(mcp_commands.app, ["setup", "--write"])

    assert not (tmp_path / ".claude" / "mcp.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
