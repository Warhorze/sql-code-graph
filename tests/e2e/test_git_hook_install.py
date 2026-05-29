"""End-to-end tests for git hook installation and execution."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


@pytest.fixture
def temp_git_repo():
    """Create a real temporary git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize a real git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        yield repo_path


def test_install_hooks_creates_executable_hook_e2e(temp_git_repo):
    """E2E: test that sqlcg git install-hooks creates an executable hook."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert hook_path.exists()

    # Check file is executable
    stat_mode = hook_path.stat().st_mode
    assert stat_mode & 0o111  # At least one execute bit set

    # Check hook is a proper shell script
    content = hook_path.read_text()
    assert content.startswith("#!/bin/sh")


def test_hook_skips_on_file_checkout(temp_git_repo):
    """E2E: test that hook exits 0 on file checkout ($3=0)."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert hook_path.exists()

    # Run the hook with $3=0 (file checkout)
    # Git passes args as: $1=prev_head, $2=new_head, $3=branch_flag
    env = os.environ.copy()
    result = subprocess.run(
        ["sh", str(hook_path), "abc123", "def456", "0"],
        cwd=str(temp_git_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    # Hook should exit 0 without attempting to index
    assert result.returncode == 0


def test_hook_runs_sqlcg_on_branch_checkout(temp_git_repo):
    """E2E: test that hook calls sqlcg index on branch checkout ($3=1)."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert hook_path.exists()

    # The hook script should call sqlcg index when $3=1
    hook_content = hook_path.read_text()

    # Verify the hook would call sqlcg index
    assert "sqlcg index" in hook_content
    assert "--dialect auto" in hook_content
    assert "--quiet" in hook_content

    # Verify the condition check for branch checkout
    assert '[ "$3" = "1" ]' in hook_content


def test_idempotency_no_duplicate_hook_lines(temp_git_repo):
    """E2E: test that running install-hooks multiple times is idempotent."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    # Install hooks three times
    for _ in range(3):
        result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
        assert result.exit_code == 0

    # Should have exactly one hook file with the sentinel once
    content = hook_path.read_text()
    sentinel_count = content.count("# sqlcg post-checkout hook")
    assert sentinel_count == 1, f"Expected 1 sentinel, found {sentinel_count}"

    # All three installs should report success/idempotency
    hook_lines = content.count("sqlcg index")
    assert hook_lines == 1, f"Expected 1 sqlcg index line, found {hook_lines}"
