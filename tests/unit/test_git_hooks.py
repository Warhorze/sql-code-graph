"""Unit tests for git hook installation."""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        git_dir = repo_path / ".git"
        git_dir.mkdir()
        (git_dir / "hooks").mkdir()
        yield repo_path


def test_install_hooks_creates_executable_hook(temp_git_repo):
    """Test that install-hooks creates an executable post-checkout hook."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert hook_path.exists()
    # Check if file is executable (Unix permission bit +x)
    assert hook_path.stat().st_mode & 0o111  # At least one execute bit set


def test_hook_contains_sentinel(temp_git_repo):
    """Test that the hook contains the sqlcg sentinel string."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    hook_content = hook_path.read_text()
    assert "# sqlcg post-checkout hook" in hook_content


def test_hook_contains_correct_content(temp_git_repo):
    """Test that the hook script contains the expected commands."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    hook_content = hook_path.read_text()
    assert '[ "$3" = "1" ] || exit 0' in hook_content
    assert "sqlcg index" in hook_content
    assert "--dialect auto" in hook_content
    assert "--quiet" in hook_content


def test_install_hooks_is_idempotent(temp_git_repo):
    """Test that running install-hooks twice produces one hook line."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    # Install hooks first time
    result1 = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result1.exit_code == 0
    content_after_first = hook_path.read_text()
    first_sentinel_count = content_after_first.count("# sqlcg post-checkout hook")

    # Install hooks second time
    result2 = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result2.exit_code == 0
    content_after_second = hook_path.read_text()
    second_sentinel_count = content_after_second.count("# sqlcg post-checkout hook")

    # Should have exactly one sentinel in both cases
    assert first_sentinel_count == 1
    assert second_sentinel_count == 1
    # Content should be identical
    assert content_after_first == content_after_second


def test_install_hooks_warns_on_existing_hook(temp_git_repo, capsys):
    """Test that install-hooks warns when a pre-existing hook exists."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    # Create a pre-existing hook without sqlcg sentinel
    hook_path.write_text("#!/bin/sh\necho 'existing hook'\n")

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    # Should exit with 0 (not an error, just a warning)
    assert result.exit_code == 0
    # Hook should not be overwritten
    assert "existing hook" in hook_path.read_text()
    # Output should contain warning
    assert "Warning" in result.stdout or "existing" in result.stdout.lower()


def test_install_hooks_fails_outside_git_repo():
    """Test that install-hooks fails when not in a git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # No .git directory

        result = runner.invoke(app, ["git", "install-hooks", "--repo", str(repo_path)])

        assert result.exit_code != 0
        assert "not a git repository" in result.stdout.lower()
