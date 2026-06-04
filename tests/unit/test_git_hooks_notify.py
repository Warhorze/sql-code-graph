"""Regression guard for git hook --notify visibility (#28 hook verification, PR 3).

Pins that the generated hook scripts:
  1. Contain --notify (so the server-routing path is activated in all branches).
  2. Contain a visible stderr warning (>&2) so hook failures are not silently swallowed.
  3. Do NOT contain a bare '|| true' swallow (which was the old silent-failure pattern).

These are content guards on the _HOOKS templates in git.py.  Any edit to the
templates that removes --notify or the >&2 warning, or re-introduces || true,
will turn these tests red.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from sqlcg.cli.commands.git import _HOOKS
from sqlcg.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Template-level guards (check the _HOOKS source without installing)
# ---------------------------------------------------------------------------


def test_post_checkout_template_contains_notify() -> None:
    """post-checkout template contains --notify."""
    spec = next(s for s in _HOOKS if s.filename == "post-checkout")
    assert "--notify" in spec.script_template, (
        "post-checkout hook template must contain --notify to route reindex "
        "through a running server and avoid the write lock"
    )


def test_post_checkout_template_contains_stderr_warning() -> None:
    """post-checkout template contains the visible stderr warning (>&2)."""
    spec = next(s for s in _HOOKS if s.filename == "post-checkout")
    assert ">&2" in spec.script_template, (
        "post-checkout hook template must contain '>&2' so the warning is "
        "visible on stderr and not silently swallowed"
    )


def test_post_checkout_template_no_bare_or_true() -> None:
    """post-checkout template does not contain a bare '|| true' swallow."""
    spec = next(s for s in _HOOKS if s.filename == "post-checkout")
    # '|| true' is the old silent-failure pattern; must not appear.
    assert "|| true" not in spec.script_template, (
        "post-checkout hook template must not contain '|| true' — failures "
        "should be visible via the stderr warning, not silently swallowed"
    )


def test_post_merge_template_contains_notify() -> None:
    """post-merge template contains --notify in both branches."""
    spec = next(s for s in _HOOKS if s.filename == "post-merge")
    # Count occurrences — both the if-branch (with --from) and the else-branch
    # (standalone fallback) must include --notify.
    count = spec.script_template.count("--notify")
    assert count >= 2, (
        f"post-merge hook template must contain --notify in both branches "
        f"(if ORIG_HEAD set AND else fallback), found {count} occurrence(s)"
    )


def test_post_merge_template_contains_stderr_warning() -> None:
    """post-merge template contains the visible stderr warning (>&2)."""
    spec = next(s for s in _HOOKS if s.filename == "post-merge")
    assert ">&2" in spec.script_template, (
        "post-merge hook template must contain '>&2' so the warning is "
        "visible on stderr and not silently swallowed"
    )


def test_post_merge_template_no_bare_or_true() -> None:
    """post-merge template does not contain a bare '|| true' swallow."""
    spec = next(s for s in _HOOKS if s.filename == "post-merge")
    assert "|| true" not in spec.script_template, (
        "post-merge hook template must not contain '|| true' — failures "
        "should be visible via the stderr warning, not silently swallowed"
    )


# ---------------------------------------------------------------------------
# Installed-script guards (check the rendered output via install-hooks)
# ---------------------------------------------------------------------------


def _install_and_read(hook_filename: str) -> str:
    """Install hooks into a temp git repo and return the rendered script."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        git_dir = repo_path / ".git"
        git_dir.mkdir()
        (git_dir / "hooks").mkdir()

        result = runner.invoke(app, ["git", "install-hooks", "--repo", str(repo_path)])
        assert result.exit_code == 0, f"install-hooks failed: {result.output}"

        hook_path = git_dir / "hooks" / hook_filename
        return hook_path.read_text()


def test_installed_post_checkout_contains_notify() -> None:
    """Installed post-checkout script contains --notify."""
    content = _install_and_read("post-checkout")
    assert "--notify" in content


def test_installed_post_checkout_contains_stderr_warning() -> None:
    """Installed post-checkout script contains >&2."""
    content = _install_and_read("post-checkout")
    assert ">&2" in content


def test_installed_post_checkout_no_bare_or_true() -> None:
    """Installed post-checkout script does not contain '|| true'."""
    content = _install_and_read("post-checkout")
    assert "|| true" not in content


def test_installed_post_merge_contains_notify() -> None:
    """Installed post-merge script contains --notify in both branches."""
    content = _install_and_read("post-merge")
    count = content.count("--notify")
    assert count >= 2, f"Expected --notify in both post-merge branches, found {count} occurrence(s)"


def test_installed_post_merge_contains_stderr_warning() -> None:
    """Installed post-merge script contains >&2."""
    content = _install_and_read("post-merge")
    assert ">&2" in content


def test_installed_post_merge_no_bare_or_true() -> None:
    """Installed post-merge script does not contain '|| true'."""
    content = _install_and_read("post-merge")
    assert "|| true" not in content


# ---------------------------------------------------------------------------
# Minor D guards — reworded fallback message (reindex failed, not server busy)
# ---------------------------------------------------------------------------


def test_no_template_contains_server_busy_locked() -> None:
    """No hook template contains the old 'server busy/locked' message.

    Minor D: reworded to '(reindex failed)' for failure-agnostic messaging.
    """
    for spec in _HOOKS:
        assert "server busy/locked" not in spec.script_template, (
            f"Hook template '{spec.filename}' still contains 'server busy/locked'. "
            "Minor D: reword to '(reindex failed)'."
        )


def test_all_templates_contain_reindex_failed() -> None:
    """All hook templates contain the new '(reindex failed)' fallback message.

    Minor D: failure-agnostic wording replaces the old 'server busy/locked'.
    """
    for spec in _HOOKS:
        assert "(reindex failed)" in spec.script_template, (
            f"Hook template '{spec.filename}' does not contain '(reindex failed)'. "
            "Minor D fix not yet applied."
        )
