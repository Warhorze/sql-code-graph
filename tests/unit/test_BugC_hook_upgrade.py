"""Regression tests for Bug C — install-hooks must upgrade stale sqlcg-owned hooks.

Live-DWH test (#29): v1.2.x post-checkout hooks (no --notify, blocking ~2min)
survived a v1.3.0 sqlcg install-hooks run because _install_single_hook silently
returned when the sentinel was present, regardless of content.

Fix: when the sentinel is present but content differs from the rendered template,
overwrite the hook and print "Upgraded git hook: .git/hooks/<name>".

Also covers Minor D: the new template text uses "(reindex failed)" not
"server busy/locked", so these tests also pin the reworded message.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


@pytest.fixture
def fake_git_repo():
    """Temp directory with a .git/hooks structure but no real git init."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / ".git" / "hooks").mkdir(parents=True)
        yield repo


# ---------------------------------------------------------------------------
# Bug C — upgrade stale hook
# ---------------------------------------------------------------------------


def test_BugC_stale_sqlcg_hook_is_upgraded(fake_git_repo: Path) -> None:
    """install-hooks overwrites a stale sqlcg-owned hook and prints 'Upgraded git hook:'.

    Pre-condition: post-checkout hook exists with the sentinel but the OLD
    'server busy/locked' message (simulating a v1.2.x hook).

    Observable: (1) file content equals the current rendered template, and
    (2) stdout contains 'Upgraded git hook:'.

    Before Bug C fix: silent return, file unchanged, no output.
    """
    from sqlcg.cli.commands.git import _HOOKS, _resolve_sqlcg_bin

    spec = next(s for s in _HOOKS if s.filename == "post-checkout")
    sqlcg_bin = _resolve_sqlcg_bin()
    current_script = spec.script_template.format(sqlcg_bin=sqlcg_bin)

    # Write a stale hook: has the sentinel but the OLD message text
    stale_content = (
        "#!/bin/sh\n"
        "# sqlcg post-checkout hook — incremental resync after branch switch\n"
        '[ "$3" = "1" ] || exit 0\n'
        f'{sqlcg_bin} reindex --from "$1" --to "$2" "$(git rev-parse --show-toplevel)"'
        " --dialect auto --quiet --notify"
        ' || echo "sqlcg: graph not updated (server busy/locked)'
        " -- run 'sqlcg mcp status'\" >&2\n"
    )
    hook_path = fake_git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.write_text(stale_content)

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(fake_git_repo)])

    assert result.exit_code == 0, f"install-hooks failed: {result.output!r}"

    # Observable 1: file was overwritten with the current rendered script
    actual_content = hook_path.read_text()
    assert actual_content == current_script, (
        f"Hook file was not upgraded to current template.\n"
        f"Expected:\n{current_script!r}\n"
        f"Got:\n{actual_content!r}"
    )

    # Observable 2: stdout reports the upgrade
    assert "Upgraded git hook:" in result.output, (
        f"Expected 'Upgraded git hook:' in stdout but got: {result.output!r}. "
        "Bug C: install-hooks silently skipped the upgrade instead of overwriting."
    )


def test_BugC_identical_hook_is_silent_skip(fake_git_repo: Path) -> None:
    """install-hooks over an identical (current) hook is silent (true idempotency).

    Pre-condition: post-checkout hook contains the current rendered script.

    Observable: (1) file content unchanged, (2) stdout does NOT contain 'Upgraded'.
    """
    from sqlcg.cli.commands.git import _HOOKS, _resolve_sqlcg_bin

    spec = next(s for s in _HOOKS if s.filename == "post-checkout")
    sqlcg_bin = _resolve_sqlcg_bin()
    current_script = spec.script_template.format(sqlcg_bin=sqlcg_bin)

    hook_path = fake_git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.write_text(current_script)
    mtime_before = hook_path.stat().st_mtime

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(fake_git_repo)])

    assert result.exit_code == 0, f"install-hooks failed: {result.output!r}"

    # Observable 1: file unchanged
    assert hook_path.read_text() == current_script, "File content changed on idempotent re-run"
    assert hook_path.stat().st_mtime == mtime_before, "File mtime changed on idempotent re-run"

    # Observable 2: no upgrade message
    assert "Upgraded git hook:" not in result.output, (
        f"Unexpected 'Upgraded git hook:' on idempotent run. Output: {result.output!r}"
    )


def test_BugC_foreign_hook_unchanged(fake_git_repo: Path) -> None:
    """install-hooks over a foreign hook (no sentinel) leaves the file untouched.

    The foreign-hook warning path must be unchanged by the Bug C fix.

    Observable: (1) file content unchanged, (2) stdout contains 'Warning'.
    """
    foreign_content = "#!/bin/sh\necho 'custom hook'\n"
    hook_path = fake_git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.write_text(foreign_content)

    result = runner.invoke(app, ["git", "install-hooks", "--repo", str(fake_git_repo)])

    assert result.exit_code == 0, f"install-hooks failed: {result.output!r}"

    # Observable 1: file NOT overwritten
    assert hook_path.read_text() == foreign_content, (
        "Foreign hook was overwritten — the warning path should leave it unchanged."
    )

    # Observable 2: warning printed
    assert "Warning" in result.output, (
        f"Expected 'Warning' in stdout for foreign hook, got: {result.output!r}"
    )
    assert "Upgraded git hook:" not in result.output, (
        "Foreign hook must NOT print 'Upgraded git hook:' — only the warning."
    )


# ---------------------------------------------------------------------------
# Minor D — hook template message text
# ---------------------------------------------------------------------------


def test_MinorD_no_template_contains_server_busy_locked() -> None:
    """No hook template contains the old 'server busy/locked' message.

    Before Minor D fix: templates contain "server busy/locked".
    After fix: templates contain "(reindex failed)".

    Observable: direct string search on _HOOKS templates.
    """
    from sqlcg.cli.commands.git import _HOOKS

    for spec in _HOOKS:
        assert "server busy/locked" not in spec.script_template, (
            f"Hook template '{spec.filename}' still contains 'server busy/locked'. "
            "Minor D: reword to '(reindex failed)'."
        )


def test_MinorD_all_templates_contain_reindex_failed() -> None:
    """All hook templates contain the new '(reindex failed)' message.

    Observable: direct string search on _HOOKS templates.
    Before fix: assertion fails because templates still have the old wording.
    """
    from sqlcg.cli.commands.git import _HOOKS

    for spec in _HOOKS:
        assert "(reindex failed)" in spec.script_template, (
            f"Hook template '{spec.filename}' does not contain '(reindex failed)'. "
            "Minor D fix not yet applied."
        )
