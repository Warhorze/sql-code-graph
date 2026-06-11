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


class TestGetDbPath:
    """Test _get_db_path function deriving fallback from DbConfig."""

    def test_scenario_graph_db_exists_when_no_env_var(self, tmp_path, monkeypatch):
        """Guard: _get_db_path returns graph.db (not kuzu.db) when no env var is set."""
        # Create ~/.sqlcg/graph.db directory structure
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        sqlcg_dir = fake_home / ".sqlcg"
        sqlcg_dir.mkdir()
        graph_db_dir = sqlcg_dir / "graph.db"
        graph_db_dir.mkdir()

        monkeypatch.setenv("HOME", str(fake_home))
        # Ensure SQLCG_DB_PATH is not set
        monkeypatch.delenv("SQLCG_DB_PATH", raising=False)

        result = uninstall._get_db_path()
        assert result is not None, "Expected _get_db_path to find graph.db"
        assert "graph.db" in result, f"Expected path to contain 'graph.db', got {result}"
        assert result.endswith("graph.db"), f"Expected path to end with 'graph.db', got {result}"

    def test_scenario_path_does_not_exist(self, tmp_path, monkeypatch):
        """Guard: _get_db_path returns None when path doesn't exist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        # Don't create .sqlcg directory

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("SQLCG_DB_PATH", raising=False)

        result = uninstall._get_db_path()
        assert result is None, f"Expected None when DB path does not exist, got {result}"

    def test_scenario_sqlcg_db_path_env_var_set(self, tmp_path, monkeypatch):
        """Guard: _get_db_path uses SQLCG_DB_PATH env var when set."""
        db_dir = tmp_path / "custom_db"
        db_dir.mkdir()

        monkeypatch.setenv("SQLCG_DB_PATH", str(db_dir))

        result = uninstall._get_db_path()
        assert result == str(db_dir), f"Expected env var path {db_dir}, got {result}"


class TestDatabaseDeletion:
    """Test database deletion logic."""

    def test_scenario_c_keep_db_flag(self, tmp_path):
        """Scenario C: user declines prompt — database is kept."""
        db_file = tmp_path / "graph.db"
        db_file.write_text("fake-db")

        with patch.object(uninstall, "_get_db_path", return_value=str(db_file)):
            # When force=False, it will prompt; user declines
            with patch("typer.confirm", return_value=False):
                uninstall._step2_delete_database(force=False)

        # The file should still exist because we declined deletion
        assert db_file.exists()

    def test_scenario_d_force_flag(self, tmp_path, monkeypatch):
        """Scenario D: --force flag deletes without prompting.

        Patched-home sentinel is removed; a stand-in sentinel under a distinct
        directory survives (proving only the patched home is touched, not some
        real-home analog).

        N1: assertion is on filesystem state, not "the patch target was called".
        Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.3.
        """
        db_file = tmp_path / "graph.db"
        db_file.write_text("fake-db")

        # Mock home directory for metrics.db location
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        metrics_dir = fake_home / ".sqlcg"
        metrics_dir.mkdir(parents=True)
        metrics_file = metrics_dir / "metrics.db"
        metrics_file.write_text("metrics")

        # A "real-home stand-in" that must NOT be touched by the force delete.
        standin_home = tmp_path / "standin_home"
        standin_home.mkdir()
        standin_metrics_dir = standin_home / ".sqlcg"
        standin_metrics_dir.mkdir(parents=True)
        standin_metrics_file = standin_metrics_dir / "metrics.db"
        standin_metrics_file.write_text("standin-metrics")

        monkeypatch.setenv("HOME", str(fake_home))

        with patch.object(uninstall, "_get_db_path", return_value=str(db_file)):
            uninstall._step2_delete_database(force=True)

        # Verify database file and patched-home metrics were deleted.
        assert not db_file.exists()
        assert not metrics_file.exists(), (
            "metrics.db under patched home must be deleted by --force."
        )
        # The stand-in sentinel under a distinct dir must still exist — this
        # proves the production code only reaches Path.home() (which was
        # redirected), not any other path.
        assert standin_metrics_file.exists(), (
            "metrics.db under the stand-in home must survive — only the "
            "patched home is affected by the force-delete path."
        )

    def test_scenario_d_force_flag_also_deletes_wal(self, tmp_path, monkeypatch):
        """Scenario D+: --force flag also deletes the .wal sibling if present."""
        db_file = tmp_path / "graph.db"
        db_file.write_text("fake-db")
        wal_file = tmp_path / "graph.db.wal"
        wal_file.write_text("fake-wal")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        with patch.object(uninstall, "_get_db_path", return_value=str(db_file)):
            uninstall._step2_delete_database(force=True)

        assert not db_file.exists(), "DuckDB file must be deleted"
        assert not wal_file.exists(), ".wal sibling must also be deleted"


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

        db_file = tmp_path / "graph.db"
        db_file.write_text("fake-db")

        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            with patch.object(uninstall, "_get_db_path", return_value=str(db_file)):
                uninstall.uninstall_cmd(keep_db=True, force=False, repo=tmp_path)

        assert db_file.exists()  # Database should be kept

    def test_uninstall_cmd_with_force_flag(self, tmp_path, monkeypatch):
        """Test uninstall with --force flag.

        Patches Path.home() so the real ~/.sqlcg/metrics.db is never touched.
        Filesystem assertion: patched-home metrics.db is absent after the run;
        a stand-in sentinel under a distinct directory survives.

        N1: assertion is on filesystem state, not "the patch target was called".
        Guards plan/sprints/sprint_postmortem_fixes.md §PR-1 Step 1.3.
        """
        settings_file = tmp_path / "settings.json"
        settings_data = {
            "mcpServers": {"sql-code-graph": {"command": "sqlcg", "args": ["mcp", "start"]}}
        }
        settings_file.write_text(json.dumps(settings_data))

        db_file = tmp_path / "graph.db"
        db_file.write_text("fake-db")

        # Create a sentinel metrics.db under a fake home that the force-delete
        # should remove.
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        fake_sqlcg = fake_home / ".sqlcg"
        fake_sqlcg.mkdir(parents=True)
        fake_metrics = fake_sqlcg / "metrics.db"
        fake_metrics.write_text("fake-metrics")

        # Create a stand-in under a *different* directory that must survive.
        standin_home = tmp_path / "standin_home"
        standin_home.mkdir()
        standin_sqlcg = standin_home / ".sqlcg"
        standin_sqlcg.mkdir(parents=True)
        standin_metrics = standin_sqlcg / "metrics.db"
        standin_metrics.write_text("standin-metrics")

        # Redirect Path.home() to fake_home so the real ~/.sqlcg/metrics.db
        # is never reached.  monkeypatch.setenv("HOME") achieves this without
        # needing to patch the Path class itself.
        monkeypatch.setenv("HOME", str(fake_home))

        with patch.object(uninstall, "_SETTINGS_PATH", settings_file):
            with patch.object(uninstall, "_get_db_path", return_value=str(db_file)):
                uninstall.uninstall_cmd(keep_db=False, force=True, repo=tmp_path)

        assert not db_file.exists(), "Database file must be deleted with --force."
        assert not fake_metrics.exists(), (
            "metrics.db under patched home must be removed by the force-delete path."
        )
        assert standin_metrics.exists(), (
            "metrics.db under the stand-in home must survive — only the patched home is affected."
        )
