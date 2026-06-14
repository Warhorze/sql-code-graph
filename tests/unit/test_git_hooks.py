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
    # Run-time resolution (#126/#1): the binary is invoked via $SQLCG_BIN, resolved
    # by the preamble (preferred path -> command -v sqlcg -> loud error), not a fixed
    # embedded literal.
    assert '"$SQLCG_BIN" reindex' in content
    assert "command -v sqlcg" in content
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


def test_post_merge_contains_orig_head_guard(temp_git_repo):
    """post-merge hook contains the ORIG_HEAD guard and routes through the server.

    PR-E fix: post-merge now passes ORIG_HEAD as --from so --notify can route
    through a running MCP server (same path as post-checkout). A fallback branch
    for when ORIG_HEAD is unset (first-ever merge / gc'd) is also present.
    """
    hook_path = temp_git_repo / ".git" / "hooks" / "post-merge"

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    content = hook_path.read_text()
    assert '"$SQLCG_BIN" reindex' in content
    assert "command -v sqlcg" in content
    # ORIG_HEAD guard — reads pre-merge HEAD via git rev-parse
    assert "git rev-parse --verify --quiet ORIG_HEAD" in content
    # If-branch: passes explicit SHAs to enable server routing
    assert '--from "$PREV"' in content
    assert "--to HEAD" in content
    assert "--notify" in content
    # Fallback branch: standalone reindex without --from when ORIG_HEAD is unset
    assert '[ -n "$PREV" ]' in content
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
# Scenario C/D — ORIG_HEAD guard behavioural tests (shell execution with shim)
# ---------------------------------------------------------------------------


def _write_sqlcg_shim(bin_dir: Path, argv_log: Path) -> None:
    """Write a sqlcg shim that records its argv to argv_log and exits 0."""
    shim = bin_dir / "sqlcg"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{argv_log}"\nexit 0\n')
    shim.chmod(0o755)


def _init_git_repo_for_shell(repo_path: Path) -> str:
    """Initialize a real git repo; return the HEAD SHA."""
    import subprocess

    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    (repo_path / "f.sql").write_text("SELECT 1")
    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_post_merge_orig_head_unset_invokes_fallback(tmp_path):
    """Scenario C: when ORIG_HEAD is unset, the hook invokes sqlcg without --from.

    The fallback branch (else) must run and must NOT pass --from to sqlcg, so the
    standalone stored-SHA delta path is taken (not an empty --from broken payload).
    """
    import os
    import subprocess

    import sqlcg.cli.commands.git as _git_mod

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo_for_shell(repo_path)

    # Set up shim BEFORE installing hooks so _resolve_sqlcg_bin returns the shim path.
    # This mirrors real usage: the shim is the "installed sqlcg" on PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_sqlcg_shim(bin_dir, argv_log)
    shim_path = str(bin_dir / "sqlcg")

    # Patch _resolve_sqlcg_bin so the hook embeds the shim path
    original_resolve = _git_mod._resolve_sqlcg_bin
    _git_mod._resolve_sqlcg_bin = lambda: shim_path
    try:
        runner.invoke(app, ["git", "install-hooks", "--repo", str(repo_path)])
    finally:
        _git_mod._resolve_sqlcg_bin = original_resolve

    # Ensure ORIG_HEAD is unset (fresh repo after git init has no ORIG_HEAD)
    hook_path = repo_path / ".git" / "hooks" / "post-merge"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["sh", str(hook_path)],
        cwd=str(repo_path),
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    argv_recorded = argv_log.read_text()
    # Fallback branch: sqlcg must be called with 'reindex' but WITHOUT '--from'
    assert "reindex" in argv_recorded
    assert "--from" not in argv_recorded


def test_post_merge_orig_head_present_routes_with_shas(tmp_path):
    """Scenario D: when ORIG_HEAD is set, the hook passes --from ORIG_HEAD --to HEAD --notify.

    The if-branch must run and must pass all three: --from <sha>, --to HEAD, --notify.
    This verifies the fix routes through the running server (same path as post-checkout).
    """
    import os
    import subprocess

    import sqlcg.cli.commands.git as _git_mod

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    orig_sha = _init_git_repo_for_shell(repo_path)

    # Simulate what git sets after a merge: write ORIG_HEAD to .git/ORIG_HEAD
    (repo_path / ".git" / "ORIG_HEAD").write_text(orig_sha + "\n")

    # Set up shim BEFORE installing hooks (same pattern as Scenario C above)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_sqlcg_shim(bin_dir, argv_log)
    shim_path = str(bin_dir / "sqlcg")

    original_resolve = _git_mod._resolve_sqlcg_bin
    _git_mod._resolve_sqlcg_bin = lambda: shim_path
    try:
        runner.invoke(app, ["git", "install-hooks", "--repo", str(repo_path)])
    finally:
        _git_mod._resolve_sqlcg_bin = original_resolve

    hook_path = repo_path / ".git" / "hooks" / "post-merge"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["sh", str(hook_path)],
        cwd=str(repo_path),
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    argv_recorded = argv_log.read_text()
    # If-branch: sqlcg must be called with explicit SHAs and --notify
    assert "reindex" in argv_recorded
    assert "--from" in argv_recorded
    assert orig_sha in argv_recorded
    assert "--to" in argv_recorded
    assert "HEAD" in argv_recorded
    assert "--notify" in argv_recorded


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


# ---------------------------------------------------------------------------
# PR-F Scenario E — absolute path embedded in hooks when shutil.which succeeds
# ---------------------------------------------------------------------------


def test_install_hooks_embeds_absolute_binary_path(temp_git_repo, monkeypatch):
    """Scenario E: the resolved absolute path is the PREFERRED run-time entry.

    Run-time resolution (#126/#1): the absolute path is no longer the literal that
    invokes reindex. It is rendered into the preamble's preferred branch
    (`if [ -x "<abs>" ]`); the body invokes `$SQLCG_BIN`, with `command -v sqlcg`
    as the run-time fallback. Asserts:
    - the resolved absolute path appears as the preferred preamble entry
    - the body invokes "$SQLCG_BIN" reindex (not a fixed literal that can go stale)
    - sentinel comments are present (R9 idempotency preserved)
    """
    import sqlcg.cli.commands.git as _git_mod

    abs_bin = "/opt/venv/bin/sqlcg"
    monkeypatch.setattr(_git_mod, "_resolve_sqlcg_bin", lambda: abs_bin)

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    assert result.exit_code == 0

    co_hook = temp_git_repo / ".git" / "hooks" / "post-checkout"
    pm_hook = temp_git_repo / ".git" / "hooks" / "post-merge"
    co_content = co_hook.read_text()
    pm_content = pm_hook.read_text()

    # The absolute path is the preferred run-time entry in the resolution preamble.
    assert f'if [ -x "{abs_bin}" ]' in co_content, (
        f"post-checkout must prefer the absolute path. Got:\n{co_content}"
    )
    assert f'if [ -x "{abs_bin}" ]' in pm_content, (
        f"post-merge must prefer the absolute path. Got:\n{pm_content}"
    )

    # The body invokes the resolved $SQLCG_BIN, with command -v sqlcg as the fallback.
    assert '"$SQLCG_BIN" reindex' in co_content
    assert '"$SQLCG_BIN" reindex' in pm_content
    assert "command -v sqlcg" in co_content
    assert "command -v sqlcg" in pm_content

    # Sentinels must be unchanged (R9 idempotency)
    assert "# sqlcg post-checkout hook" in co_content
    assert "# sqlcg post-merge hook" in pm_content


# ---------------------------------------------------------------------------
# PR-F Scenario F — idempotency still holds with absolute path
# ---------------------------------------------------------------------------


def test_install_hooks_idempotent_with_absolute_path(temp_git_repo, monkeypatch):
    """Scenario F: second install-hooks run silently skips (sentinel already present).

    The sentinel check is byte-for-byte, so idempotency is preserved regardless of
    whether the command line uses an absolute path or bare sqlcg.
    """
    import sqlcg.cli.commands.git as _git_mod

    abs_bin = "/opt/venv/bin/sqlcg"
    monkeypatch.setattr(_git_mod, "_resolve_sqlcg_bin", lambda: abs_bin)

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    co_content_first = (temp_git_repo / ".git" / "hooks" / "post-checkout").read_text()
    pm_content_first = (temp_git_repo / ".git" / "hooks" / "post-merge").read_text()

    runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])
    co_content_second = (temp_git_repo / ".git" / "hooks" / "post-checkout").read_text()
    pm_content_second = (temp_git_repo / ".git" / "hooks" / "post-merge").read_text()

    assert co_content_first == co_content_second, "post-checkout must not change on second install"
    assert pm_content_first == pm_content_second, "post-merge must not change on second install"


# ---------------------------------------------------------------------------
# PR-F Scenario G — no binary resolvable → bare fallback + warning, no hard fail
# ---------------------------------------------------------------------------


def test_install_hooks_bare_fallback_when_no_binary_resolvable(temp_git_repo, monkeypatch):
    """Scenario G: no resolvable binary at install -> bare 'sqlcg' preferred entry + warning.

    Run-time resolution (#126/#1): even when install-time resolution returns the bare
    'sqlcg' literal, the rendered hook still relies on the run-time preamble — the bare
    'sqlcg' is the preferred entry and `command -v sqlcg` is the run-time fallback, with
    a loud error branch if neither resolves. Asserts:
    - install-hooks does NOT hard-fail
    - the preamble renders the bare 'sqlcg' preferred entry + command -v fallback
    - the body invokes "$SQLCG_BIN" reindex
    - a one-line warning is printed to output
    """
    import sqlcg.cli.commands.git as _git_mod

    # Make shutil.which return None (no sqlcg on PATH)
    monkeypatch.setattr(_git_mod.shutil, "which", lambda name: None)
    # Make sys.argv[0] non-usable (not named sqlcg, not executable)
    monkeypatch.setattr(_git_mod.sys, "argv", ["/usr/bin/python3"])
    # Make site.getuserbase() point to a directory with no sqlcg binary (step 0 miss)
    monkeypatch.setattr(_git_mod.site, "getuserbase", lambda: "/tmp/no-user-base-here")

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(temp_git_repo)])

    # Must NOT hard-fail
    assert result.exit_code == 0, f"install-hooks must not hard-fail. output: {result.output}"

    co_content = (temp_git_repo / ".git" / "hooks" / "post-checkout").read_text()
    pm_content = (temp_git_repo / ".git" / "hooks" / "post-merge").read_text()

    # Bare 'sqlcg' is rendered as the preferred preamble entry; run-time fallback is PATH.
    assert 'if [ -x "sqlcg" ]' in co_content
    assert 'if [ -x "sqlcg" ]' in pm_content
    assert "command -v sqlcg" in co_content
    assert "command -v sqlcg" in pm_content
    assert '"$SQLCG_BIN" reindex' in co_content
    assert '"$SQLCG_BIN" reindex' in pm_content

    # Warning must be printed
    assert "warning" in result.output.lower() or "warn" in result.output.lower(), (
        f"Expected a warning when no binary resolvable. Output: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# PR-D Scenario A — .venv path warning emitted when which() resolves inside venv
# ---------------------------------------------------------------------------


def test_resolve_sqlcg_bin_warns_when_which_returns_venv_path(monkeypatch):
    """which() returning a .venv path emits a warning via console.print, not bare print.

    Scenario A from plan/sprints/sprint_backlog_q2_bugfix.md §PR-D:
    - shutil.which("sqlcg") returns a path containing '/.venv/'
    - site.getuserbase() returns a directory with no sqlcg binary present
    - _resolve_sqlcg_bin() must emit a warning containing '.venv' via console.print
    - the returned path must still be the venv path (fallback proceeds normally)

    The monkeypatch targets console.print (not bare print / typer.echo) because the
    PR-D reviewer amendment explicitly requires the module's console object as the
    warning surface, making this the canonical monkeypatch target.

    Guards #126.
    """
    import sqlcg.cli.commands.git as _git_mod

    venv_path = "/project/.venv/bin/sqlcg"
    monkeypatch.setattr(_git_mod.shutil, "which", lambda name: venv_path)

    # Patch site.getuserbase to a tmp dir that has no sqlcg binary
    monkeypatch.setattr(_git_mod.site, "getuserbase", lambda: "/tmp/no-user-base-here")

    printed: list[str] = []
    monkeypatch.setattr(
        _git_mod.console, "print", lambda msg, *args, **kwargs: printed.append(str(msg))
    )

    result = _git_mod._resolve_sqlcg_bin()

    # Returned path must be the venv path (not bare fallback)
    assert result == venv_path, f"Expected venv path, got: {result!r}"

    # Warning must have been printed and mention .venv
    assert printed, "No warning was printed — console.print was not called"
    warning_text = " ".join(printed)
    assert ".venv" in warning_text, f"Warning must mention '.venv'. Got: {warning_text!r}"


def test_resolve_sqlcg_bin_prefers_user_base_over_venv(monkeypatch, tmp_path):
    """user-base binary is preferred over a .venv path returned by shutil.which.

    When Path(site.getuserbase()) / 'bin' / 'sqlcg' exists and is executable,
    _resolve_sqlcg_bin() must return it without calling shutil.which at all —
    no warning should be emitted.

    Guards #126 resolution-order step 0 > step 1.
    """
    import sqlcg.cli.commands.git as _git_mod

    # Create a fake executable at the user-base bin path
    user_bin = tmp_path / "bin" / "sqlcg"
    user_bin.parent.mkdir(parents=True)
    user_bin.write_text('#!/bin/sh\nexec sqlcg "$@"\n')
    user_bin.chmod(0o755)

    monkeypatch.setattr(_git_mod.site, "getuserbase", lambda: str(tmp_path))

    # which() would return a .venv path — but it must NOT be called (user-base wins)
    venv_path = "/project/.venv/bin/sqlcg"
    monkeypatch.setattr(_git_mod.shutil, "which", lambda name: venv_path)

    printed: list[str] = []
    monkeypatch.setattr(
        _git_mod.console, "print", lambda msg, *args, **kwargs: printed.append(str(msg))
    )

    result = _git_mod._resolve_sqlcg_bin()

    assert result == str(user_bin), f"Expected user-base path, got: {result!r}"
    # No warning should have been printed
    assert not printed, f"Unexpected warning printed: {printed!r}"
