"""End-to-end tests for git hook installation and execution."""

import os
import shutil
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


def test_hook_runs_sqlcg_reindex_on_branch_checkout(temp_git_repo):
    """E2E: test that hook calls sqlcg reindex with SHA args on branch checkout ($3=1)."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-checkout"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert hook_path.exists()

    hook_content = hook_path.read_text()

    # post-checkout must call reindex via the run-time-resolved $SQLCG_BIN (#126/#1):
    # preferred path -> command -v sqlcg -> loud error.
    assert '"$SQLCG_BIN" reindex' in hook_content
    assert "command -v sqlcg" in hook_content
    assert '--from "$1"' in hook_content
    assert '--to "$2"' in hook_content
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
    hook_lines = content.count('"$SQLCG_BIN" reindex')
    assert hook_lines == 1, f"Expected 1 reindex line, found {hook_lines}"


# ---------------------------------------------------------------------------
# PR-E: post-merge hook ORIG_HEAD routing tests (#28)
# ---------------------------------------------------------------------------


def test_post_merge_hook_content_orig_head_guard(temp_git_repo):
    """Scenario A: generated post-merge hook contains ORIG_HEAD guard and correct flags."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result.exit_code == 0
    assert hook_path.exists()

    content = hook_path.read_text()

    # Sentinel present (R9 idempotency relies on this exact string)
    assert "# sqlcg post-merge hook" in content

    # ORIG_HEAD guard
    assert "git rev-parse --verify --quiet ORIG_HEAD" in content
    assert '[ -n "$PREV" ]' in content

    # Positive branch: routes through server with explicit SHAs
    assert '--from "$PREV"' in content
    assert "--to HEAD" in content
    assert "--notify" in content

    # Fallback branch: no --from (standalone stored-SHA path)
    # The else branch must contain a reindex call without --from
    else_idx = content.index("else")
    fallback_section = content[else_idx:]
    assert '"$SQLCG_BIN" reindex' in fallback_section


def test_post_merge_hook_idempotent(temp_git_repo):
    """Scenario B: install-hooks is idempotent — second run skips silently."""
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    # First install
    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result.exit_code == 0
    content_after_first = hook_path.read_text()

    # Second install — must not change file content
    result2 = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result2.exit_code == 0
    content_after_second = hook_path.read_text()

    assert content_after_first == content_after_second
    assert content_after_second.count("# sqlcg post-merge hook") == 1


def test_post_merge_hook_orig_head_unset_invokes_without_from(temp_git_repo):
    """Scenario C: with ORIG_HEAD unset, hook invokes sqlcg reindex without --from."""
    import sqlcg.cli.commands.git as _git_mod

    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    # Create a sqlcg shim BEFORE installing hooks — patch _resolve_sqlcg_bin to
    # return the shim path so the hook embeds the absolute shim path.
    shim_dir = temp_git_repo / ".shim"
    shim_dir.mkdir()
    argv_file = shim_dir / "sqlcg_argv.txt"
    shim_path = shim_dir / "sqlcg"
    shim_path.write_text(f'#!/bin/sh\necho "$@" >> {argv_file}\nexit 0\n')
    shim_path.chmod(0o755)

    original_resolve = _git_mod._resolve_sqlcg_bin
    _git_mod._resolve_sqlcg_bin = lambda: str(shim_path)
    try:
        runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    finally:
        _git_mod._resolve_sqlcg_bin = original_resolve

    assert hook_path.exists()

    env = os.environ.copy()
    # Ensure ORIG_HEAD is not set
    env.pop("ORIG_HEAD", None)

    result = subprocess.run(
        ["sh", str(hook_path)],
        cwd=str(temp_git_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    # Hook must not abort with a non-zero exit from the guard itself
    # (the shim exits 0, so the hook exits 0)
    assert result.returncode == 0

    # The shim must have been called (argv file exists)
    assert argv_file.exists(), "sqlcg shim was never invoked"
    argv_recorded = argv_file.read_text().strip()

    # Fallback branch: must NOT contain --from
    assert "--from" not in argv_recorded, (
        f"Fallback branch must not pass --from; got: {argv_recorded!r}"
    )
    # Must contain reindex
    assert "reindex" in argv_recorded


def test_post_merge_hook_orig_head_set_routes_with_shas(temp_git_repo):
    """Scenario D: with ORIG_HEAD set, hook invokes sqlcg reindex with --from and --to HEAD."""
    import sqlcg.cli.commands.git as _git_mod

    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    # Need at least one commit so git rev-parse HEAD works
    dummy_file = temp_git_repo / "README.md"
    dummy_file.write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(temp_git_repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(temp_git_repo),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        },
    )

    shim_dir = temp_git_repo / ".shim"
    shim_dir.mkdir()
    argv_file = shim_dir / "sqlcg_argv.txt"
    shim_path = shim_dir / "sqlcg"
    shim_path.write_text(f'#!/bin/sh\necho "$@" >> {argv_file}\nexit 0\n')
    shim_path.chmod(0o755)

    # Patch _resolve_sqlcg_bin before installing hooks (same as Scenario C)
    original_resolve = _git_mod._resolve_sqlcg_bin
    _git_mod._resolve_sqlcg_bin = lambda: str(shim_path)
    try:
        runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    finally:
        _git_mod._resolve_sqlcg_bin = original_resolve

    assert hook_path.exists()

    prev_sha = "abc1234def5678901234567890abcdef12345678"

    env = os.environ.copy()

    # Simulate ORIG_HEAD by writing the ref file git normally sets
    orig_head_file = temp_git_repo / ".git" / "ORIG_HEAD"
    orig_head_file.write_text(prev_sha + "\n")

    result = subprocess.run(
        ["sh", str(hook_path)],
        cwd=str(temp_git_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert argv_file.exists(), "sqlcg shim was never invoked"
    argv_recorded = argv_file.read_text().strip()

    # Positive branch: must contain --from <prev_sha> and --to HEAD
    assert f"--from {prev_sha}" in argv_recorded, (
        f"Expected --from {prev_sha} in argv; got: {argv_recorded!r}"
    )
    assert "--to HEAD" in argv_recorded, f"Expected --to HEAD in argv; got: {argv_recorded!r}"
    assert "--notify" in argv_recorded


# ---------------------------------------------------------------------------
# #126 + #1 — run-time binary resolution survives a stale preferred path
# ---------------------------------------------------------------------------


def _install_hook_with_preferred_bin(repo: Path, preferred_bin: Path) -> Path:
    """Install the post-checkout hook with ``preferred_bin`` rendered as the entry."""
    import sqlcg.cli.commands.git as _git_mod

    original = _git_mod._resolve_sqlcg_bin
    _git_mod._resolve_sqlcg_bin = lambda: str(preferred_bin)
    try:
        runner.invoke(app, ["git", "install-hooks", "--repo", str(repo)])
    finally:
        _git_mod._resolve_sqlcg_bin = original
    return repo / ".git" / "hooks" / "post-checkout"


def test_hook_resolves_path_binary_after_preferred_path_deleted(temp_git_repo):
    """venv-rebuild sim: preferred path gone -> hook resolves a $PATH sqlcg, exit 0.

    Models an editable .venv install whose embedded path is deleted by a venv
    rebuild. The run-time preamble must fall back to `command -v sqlcg` and invoke
    the working $PATH binary. Guards the git-hook binary-resolution plan
    ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
    """
    venv_dir = temp_git_repo / ".venv" / "bin"
    venv_dir.mkdir(parents=True)
    preferred = venv_dir / "sqlcg"
    preferred.write_text("#!/bin/sh\nexit 0\n")
    preferred.chmod(0o755)

    hook_path = _install_hook_with_preferred_bin(temp_git_repo, preferred)
    assert f'if [ -x "{preferred}"' in hook_path.read_text()

    # Simulate the venv rebuild: the preferred path no longer exists.
    preferred.unlink()

    # Provide a working sqlcg on $PATH that records its invocation.
    path_dir = temp_git_repo / "pathbin"
    path_dir.mkdir()
    argv_file = temp_git_repo / "argv.txt"
    path_bin = path_dir / "sqlcg"
    path_bin.write_text(f'#!/bin/sh\necho "$@" >> {argv_file}\nexit 0\n')
    path_bin.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{path_dir}:{env['PATH']}"
    result = subprocess.run(
        ["sh", str(hook_path), "abc123", "def456", "1"],
        cwd=str(temp_git_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert argv_file.exists(), "the $PATH sqlcg binary was never invoked"
    assert "reindex" in argv_file.read_text()


def test_hook_loud_error_when_no_binary_resolvable_anywhere(temp_git_repo):
    """No sqlcg anywhere -> hook exits NON-ZERO with a stderr message, never silent 0.

    The exact #1 failure inversion at the hook-script layer. Guards the git-hook
    binary-resolution plan
    ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
    """
    missing = temp_git_repo / "gone" / "sqlcg"  # never created
    hook_path = _install_hook_with_preferred_bin(temp_git_repo, missing)

    # Run with a PATH that has the system shell dirs (so `sh`/`command` resolve)
    # but no sqlcg anywhere — i.e. drop the test runner's venv/user-base dirs.
    sh_bin = shutil.which("sh") or "/bin/sh"
    sh_dir = str(Path(sh_bin).parent)
    empty_path_dir = temp_git_repo / "emptybin"
    empty_path_dir.mkdir()
    env = os.environ.copy()
    env["PATH"] = f"{empty_path_dir}:{sh_dir}:/usr/bin:/bin"
    assert shutil.which("sqlcg", path=env["PATH"]) is None, "test setup: sqlcg still on PATH"
    result = subprocess.run(
        [sh_bin, str(hook_path), "abc123", "def456", "1"],
        cwd=str(temp_git_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, f"hook must exit non-zero when no binary resolves: {result!r}"
    assert "cannot find the sqlcg binary" in result.stderr
    assert "sqlcg git install-hooks" in result.stderr


def test_reinstall_upgrades_stale_template_hook(temp_git_repo):
    """A pre-#126 fixed-path hook is auto-upgraded to the run-time-resilient template.

    The self-upgrade branch (sentinel present, body differs -> overwrite) rewrites a
    stale v1.33.1-era hook and prints 'Upgraded git hook:'. Guards the git-hook
    binary-resolution plan
    ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
    """
    hooks_dir = temp_git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    stale = hooks_dir / "post-checkout"
    # Old v1.33.1-era template: fixed embedded literal, no run-time preamble.
    stale.write_text(
        "#!/bin/sh\n"
        "# sqlcg post-checkout hook — incremental resync after branch switch\n"
        '[ "$3" = "1" ] || exit 0\n'
        '/old/path/sqlcg reindex --from "$1" --to "$2" "$(git rev-parse --show-toplevel)"'
        " --dialect auto --quiet --notify\n"
    )
    stale.chmod(0o755)

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    assert result.exit_code == 0
    assert "Upgraded git hook:" in result.output
    upgraded = stale.read_text()
    assert "command -v sqlcg" in upgraded
    assert '"$SQLCG_BIN" reindex' in upgraded
    assert "/old/path/sqlcg reindex" not in upgraded
