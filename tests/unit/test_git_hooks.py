"""Unit tests for git hook installation and removal."""

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


# ---------------------------------------------------------------------------
# post-checkout hook tests
# ---------------------------------------------------------------------------


def test_install_hooks_creates_executable_post_checkout(temp_git_repo):
    """install-hooks creates an executable post-checkout hook."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111


def test_post_checkout_contains_sentinel(temp_git_repo):
    """post-checkout hook contains the sqlcg sentinel."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    content = hook_path.read_text()
    assert "# sqlcg post-checkout hook" in content


def test_post_checkout_contains_reindex_with_sha_args(temp_git_repo):
    """post-checkout hook calls sqlcg reindex with --from $1 --to $2 SHA args."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    content = hook_path.read_text()
    assert "sqlcg reindex" in content
    assert '--from "$1"' in content
    assert '--to "$2"' in content
    assert "--dialect auto" in content
    assert "--quiet" in content
    assert '[ "$3" = "1" ] || exit 0' in content


# ---------------------------------------------------------------------------
# post-merge hook tests
# ---------------------------------------------------------------------------


def test_install_hooks_creates_executable_post_merge(temp_git_repo):
    """install-hooks creates an executable post-merge hook."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111


def test_post_merge_contains_sentinel(temp_git_repo):
    """post-merge hook contains the sqlcg sentinel."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    content = hook_path.read_text()
    assert "# sqlcg post-merge hook" in content


def test_post_merge_contains_reindex_standalone(temp_git_repo):
    """post-merge hook calls sqlcg reindex in standalone mode (no SHA args)."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    content = hook_path.read_text()
    assert "sqlcg reindex" in content
    # post-merge does NOT pass --from/--to (standalone mode uses stored SHA)
    assert "--from" not in content
    assert "--to" not in content
    assert "--dialect auto" in content
    assert "--quiet" in content


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


def test_install_hooks_idempotent_post_checkout(temp_git_repo):
    """Running install-hooks twice does not duplicate the post-checkout block."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    content_first = hook_path.read_text()

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    content_second = hook_path.read_text()

    assert content_first.count("# sqlcg post-checkout hook") == 1
    assert content_second.count("# sqlcg post-checkout hook") == 1
    assert content_first == content_second


def test_install_hooks_idempotent_post_merge(temp_git_repo):
    """Running install-hooks twice does not duplicate the post-merge block."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    content_first = hook_path.read_text()

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    content_second = hook_path.read_text()

    assert content_first.count("# sqlcg post-merge hook") == 1
    assert content_second.count("# sqlcg post-merge hook") == 1
    assert content_first == content_second


# ---------------------------------------------------------------------------
# Foreign-hook warning path
# ---------------------------------------------------------------------------


def test_install_hooks_warns_on_existing_post_checkout(temp_git_repo):
    """install-hooks warns and skips when a pre-existing post-checkout hook exists."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.write_text("#!/bin/sh\necho 'existing hook'\n")

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    # Original content must be preserved
    assert "existing hook" in hook_path.read_text()
    # Warning must appear in output
    assert "Warning" in result.stdout or "existing" in result.stdout.lower()


def test_install_hooks_warns_on_existing_post_merge(temp_git_repo):
    """install-hooks warns and skips when a pre-existing post-merge hook exists."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"
    hook_path.write_text("#!/bin/sh\necho 'existing merge hook'\n")

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert "existing merge hook" in hook_path.read_text()
    assert "Warning" in result.stdout or "existing" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Uninstall strips both sentinels
# ---------------------------------------------------------------------------


def test_uninstall_strips_post_checkout_sentinel(temp_git_repo):
    """After install then uninstall, post-checkout hook no longer contains sqlcg sentinel."""
    from sqlcg.cli.commands.uninstall import _step3_remove_git_hook

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"
    assert "# sqlcg post-checkout hook" in hook_path.read_text()

    _step3_remove_git_hook(temp_git_repo)

    # File should be deleted (it was created by sqlcg only) or sentinel must be gone
    if hook_path.exists():
        assert "# sqlcg post-checkout hook" not in hook_path.read_text()


def test_uninstall_strips_post_merge_sentinel(temp_git_repo):
    """After install then uninstall, post-merge hook no longer contains sqlcg sentinel."""
    from sqlcg.cli.commands.uninstall import _step3_remove_git_hook

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"
    assert "# sqlcg post-merge hook" in hook_path.read_text()

    _step3_remove_git_hook(temp_git_repo)

    if hook_path.exists():
        assert "# sqlcg post-merge hook" not in hook_path.read_text()


def test_uninstall_removes_sqlcg_block_from_both_hooks(temp_git_repo):
    """Uninstall removes the sqlcg block from both hooks; neither sentinel remains."""
    from sqlcg.cli.commands.uninstall import _step3_remove_git_hook

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    _step3_remove_git_hook(temp_git_repo)

    co_hook = temp_git_repo / ".git" / "hooks" / "post-checkout"
    pm_hook = temp_git_repo / ".git" / "hooks" / "post-merge"

    # If a file still exists, the sentinel must be gone from it
    if co_hook.exists():
        assert "# sqlcg post-checkout hook" not in co_hook.read_text()
    if pm_hook.exists():
        assert "# sqlcg post-merge hook" not in pm_hook.read_text()


def test_uninstall_deletes_sentinel_only_hooks(temp_git_repo):
    """Uninstall deletes hooks that contain ONLY the sqlcg sentinel (no other content)."""
    from sqlcg.cli.commands.uninstall import _remove_single_hook

    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"
    # Write a hook with only the sqlcg sentinel (no foreign shebang or other content)
    hook_path.write_text(
        "# sqlcg post-merge hook — incremental resync after pull/merge\n"
        'sqlcg reindex "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true\n'
    )

    _remove_single_hook(temp_git_repo, "post-merge", "# sqlcg post-merge hook")

    assert not hook_path.exists(), "hook with only sqlcg content must be deleted"


def test_uninstall_preserves_foreign_content_in_post_checkout(temp_git_repo):
    """Uninstall preserves foreign content in post-checkout hook after stripping sqlcg block."""
    from sqlcg.cli.commands.uninstall import _step3_remove_git_hook

    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"
    # Write a foreign hook, then install sqlcg on top of it (would warn + skip for real hook,
    # so simulate by writing a hook with BOTH foreign content and sqlcg sentinel manually).
    hook_path.write_text(
        "#!/bin/sh\n"
        "echo 'foreign'\n"
        "# sqlcg post-checkout hook — incremental resync after branch switch\n"
        "# $3 == 1 means branch checkout (not file checkout); skip file checkouts\n"
        '[ "$3" = "1" ] || exit 0\n'
        'sqlcg reindex --from "$1" --to "$2" '
        '"$(git rev-parse --show-toplevel)" --dialect auto --quiet || true\n'
    )

    _step3_remove_git_hook(temp_git_repo)

    assert hook_path.exists(), "file with foreign content must not be deleted"
    remaining = hook_path.read_text()
    assert "foreign" in remaining
    assert "# sqlcg post-checkout hook" not in remaining


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_install_hooks_fails_outside_git_repo():
    """install-hooks fails when not in a git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        result = runner.invoke(app, ["git", "install-hooks", "--repo", str(repo_path)])

        assert result.exit_code != 0
        assert "not a git repository" in result.stdout.lower()
