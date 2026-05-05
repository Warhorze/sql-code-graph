"""Unit tests for sqlcg uninstall command."""

import json
from unittest.mock import patch

from sqlcg.cli.commands import uninstall


class TestMCPEntryRemoval:
    """Test MCP entry removal from settings.json."""

    def test_scenario_a_remove_mcp_entry(self, tmp_path):
        """Scenario A: MCP entry removal."""
        settings_file = tmp_path / "settings.json"
        settings_data = {
            "mcpServers": {
                "sql-code-graph": {"command": "sqlcg", "args": ["mcp", "start"]},
                "other-server": {"command": "other"},
            }
        }
        settings_file.write_text(json.dumps(settings_data))

        # Patch SETTINGS_PATH to use tmp_path
        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            uninstall._step1_remove_mcp_entry()

        # Verify the entry was removed
        result = json.loads(settings_file.read_text())
        assert "sql-code-graph" not in result["mcpServers"]
        assert "other-server" in result["mcpServers"]

    def test_scenario_b_mcp_entry_already_absent(self, tmp_path):
        """Scenario B: MCP entry already absent."""
        settings_file = tmp_path / "settings.json"
        settings_data = {"mcpServers": {}}
        settings_file.write_text(json.dumps(settings_data))

        # Patch SETTINGS_PATH to use tmp_path
        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            uninstall._step1_remove_mcp_entry()

        # Verify no error occurred and file is still valid
        result = json.loads(settings_file.read_text())
        assert "sql-code-graph" not in result["mcpServers"]


class TestDatabaseDeletion:
    """Test database deletion logic."""

    def test_scenario_c_keep_db_flag(self, tmp_path):
        """Scenario C: --keep-db flag skips database deletion."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        (db_dir / "test.file").write_text("test")

        with patch.object(uninstall, "_get_db_path", return_value=str(db_dir)):
            with patch.object(uninstall, "_is_kuzu_backend", return_value=True):
                # When force=False and --keep-db is not set, it will prompt
                # We mock typer.confirm to return False so it doesn't delete
                with patch("typer.confirm", return_value=False):
                    uninstall._step2_delete_database(force=False)

        # The directory should still exist because we declined deletion
        assert db_dir.exists()

    def test_scenario_d_force_flag(self, tmp_path, monkeypatch):
        """Scenario D: --force flag deletes without prompting."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        (db_dir / "test.file").write_text("test")

        # Mock home directory for metrics.db location
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        metrics_dir = fake_home / ".sqlcg"
        metrics_dir.mkdir(parents=True)
        metrics_file = metrics_dir / "metrics.db"
        metrics_file.write_text("metrics")

        monkeypatch.setenv("HOME", str(fake_home))

        with patch.object(uninstall, "_get_db_path", return_value=str(db_dir)):
            with patch.object(uninstall, "_is_kuzu_backend", return_value=True):
                uninstall._step2_delete_database(force=True)

        # Verify both database and metrics were deleted
        assert not db_dir.exists()
        assert not metrics_file.exists()

    def test_database_not_kuzu(self, tmp_path):
        """Test that non-kuzu backends are skipped."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()

        with patch.object(uninstall, "_get_db_path", return_value=str(db_dir)):
            with patch.object(uninstall, "_is_kuzu_backend", return_value=False):
                uninstall._step2_delete_database(force=False)

        # Database should still exist
        assert db_dir.exists()


class TestGitHookRemoval:
    """Test git hook sentinel block removal."""

    def test_scenario_e_git_hook_stripping(self, tmp_path):
        """Scenario E: Git hook sentinel block stripping with other content preserved."""
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        hook_file = git_dir / "post-checkout"

        hook_content = """#!/bin/bash
# Some other hook content
echo "Before hook"

# sqlcg post-checkout hook
set -e
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$HOOK_DIR/../.."
cd "$REPO_DIR"
sqlcg watch --lazy >/dev/null 2>&1 || true

# More content after hook
echo "After hook"
"""
        hook_file.write_text(hook_content)

        uninstall._step3_remove_git_hook(tmp_path)

        # Verify sentinel block is removed but other content preserved
        result = hook_file.read_text()
        assert "# sqlcg post-checkout hook" not in result
        assert "Before hook" in result
        assert "After hook" in result
        assert "sqlcg watch" not in result

    def test_scenario_f_git_hook_empty_after_stripping(self, tmp_path):
        """Scenario F: Git hook file is deleted when empty after stripping."""
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        hook_file = git_dir / "post-checkout"

        hook_content = """# sqlcg post-checkout hook
set -e
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$HOOK_DIR/../.."
cd "$REPO_DIR"
sqlcg watch --lazy >/dev/null 2>&1 || true
"""
        hook_file.write_text(hook_content)

        uninstall._step3_remove_git_hook(tmp_path)

        # Verify the file was deleted
        assert not hook_file.exists()

    def test_scenario_g_git_hook_absent(self, tmp_path):
        """Scenario G: Git hook file absent."""
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)

        # No post-checkout file created
        uninstall._step3_remove_git_hook(tmp_path)

        # Should not raise an error


class TestUninstallCmdFunction:
    """Tests for the uninstall_cmd typer function directly."""

    def test_uninstall_cmd_basic(self, tmp_path):
        """Test basic uninstall via function call."""
        settings_file = tmp_path / "settings.json"
        settings_data = {
            "mcpServers": {"sql-code-graph": {"command": "sqlcg", "args": ["mcp", "start"]}}
        }
        settings_file.write_text(json.dumps(settings_data))

        # Patch paths and environment
        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            with patch.object(uninstall, "_get_db_path", return_value=None):
                with patch.object(uninstall, "_is_kuzu_backend", return_value=True):
                    # Call the function directly
                    uninstall.uninstall_cmd(keep_db=False, force=False, repo=tmp_path)

        # Verify MCP entry was removed
        result = json.loads(settings_file.read_text())
        assert "sql-code-graph" not in result["mcpServers"]

    def test_uninstall_cmd_with_keep_db_flag(self, tmp_path):
        """Test uninstall with --keep-db flag."""
        settings_file = tmp_path / "settings.json"
        settings_data = {
            "mcpServers": {"sql-code-graph": {"command": "sqlcg", "args": ["mcp", "start"]}}
        }
        settings_file.write_text(json.dumps(settings_data))

        db_dir = tmp_path / "db"
        db_dir.mkdir()

        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            with patch.object(uninstall, "_get_db_path", return_value=str(db_dir)):
                with patch.object(uninstall, "_is_kuzu_backend", return_value=True):
                    uninstall.uninstall_cmd(keep_db=True, force=False, repo=tmp_path)

        assert db_dir.exists()  # Database should be kept

    def test_uninstall_cmd_with_force_flag(self, tmp_path):
        """Test uninstall with --force flag."""
        settings_file = tmp_path / "settings.json"
        settings_data = {
            "mcpServers": {"sql-code-graph": {"command": "sqlcg", "args": ["mcp", "start"]}}
        }
        settings_file.write_text(json.dumps(settings_data))

        db_dir = tmp_path / "db"
        db_dir.mkdir()

        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            with patch.object(uninstall, "_get_db_path", return_value=str(db_dir)):
                with patch.object(uninstall, "_is_kuzu_backend", return_value=True):
                    uninstall.uninstall_cmd(keep_db=False, force=True, repo=tmp_path)

        assert not db_dir.exists()  # Database should be deleted
