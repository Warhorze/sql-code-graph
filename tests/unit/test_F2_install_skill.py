"""Failing acceptance tests for F2 — install provisions skill (phase 3).

These tests MUST FAIL before the developer extends install.py.
They encode the acceptance criteria from plan/sprints/bundle_claude_skill.md phase 3.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


def _which_sqlcg(cmd: str) -> str | None:
    if cmd == "sqlcg":
        return "/usr/local/bin/sqlcg"
    return None


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path so tests never touch ~/.claude."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Step 3.1 — global scope writes SKILL.md with expected content
# ---------------------------------------------------------------------------


def test_F2_install_global_scope_writes_skill_file(fake_home: Path) -> None:
    """Step 3.1 AC: 'install --scope global' writes ~/.claude/skills/sqlcg/SKILL.md."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        result = runner.invoke(app, ["install", "--scope", "global"])
    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    assert skill_path.exists(), (
        f"install --scope global must write {skill_path}; exit_code={result.exit_code}, "
        f"output={result.output}"
    )


def test_F2_install_global_scope_skill_has_frontmatter(fake_home: Path) -> None:
    """Step 3.1 AC: skill file starts with YAML frontmatter '---'."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    if not skill_path.exists():
        pytest.skip("Skill file not written — install --scope global not implemented")
    content = skill_path.read_text()
    assert content.startswith("---\n"), (
        f"Skill file must start with YAML frontmatter '---\\n', got: {repr(content[:40])}"
    )


def test_F2_install_global_scope_skill_contains_all_18_tools(fake_home: Path) -> None:
    """Step 3.1 AC: skill body lists all 18 MCP tool names (v1.23.0 adds get_pr_impact).

    v1.22.0 added get_empty_propagation (17); v1.23.0 added get_pr_impact (18).
    Guards that the installed SKILL.md stays in sync with the live registry count.
    """
    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    if not skill_path.exists():
        pytest.skip("Skill file not written — install --scope global not implemented")
    content = skill_path.read_text()
    expected_tools = [
        "trace_column_lineage",
        "find_table_usages",
        "find_definition",
        "get_downstream_dependencies",
        "get_upstream_dependencies",
        "get_backfill_order",
        "diff_impact",
        "search_sql_pattern",
        "list_dialects_and_repos",
        "get_hub_ranking",
        "get_change_scope",
        "scope_change",
        "analyze_unused",
        "index_repo",
        "execute_sql",
        "submit_feedback",
        "get_empty_propagation",
        "get_pr_impact",
    ]
    assert len(expected_tools) == 18, "Test fixture must list exactly 18 tools"
    missing = [t for t in expected_tools if t not in content]
    assert not missing, f"Skill file is missing these tool names: {missing}"


def test_F2_install_global_scope_idempotent(fake_home: Path) -> None:
    """Step 3.1 AC: re-running install --scope global produces exactly one SKILL.md."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])
        runner.invoke(app, ["install", "--scope", "global"])
    skill_dir = fake_home / ".claude" / "skills" / "sqlcg"
    if not skill_dir.exists():
        pytest.skip("Skill dir not created — install --scope global not implemented")
    skill_files = list(skill_dir.rglob("SKILL.md"))
    assert len(skill_files) == 1, (
        f"Idempotent install must produce exactly 1 SKILL.md, found: {skill_files}"
    )


def test_F2_install_dry_run_writes_no_skill_file(fake_home: Path) -> None:
    """Step 3.1 AC: 'install --dry-run --scope global' writes no SKILL.md."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        result = runner.invoke(app, ["install", "--dry-run", "--scope", "global"])
    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    assert not skill_path.exists(), (
        f"install --dry-run must NOT write the skill file; exit={result.exit_code}"
    )


# ---------------------------------------------------------------------------
# Step 3.2 — project scope resolves under --repo
# ---------------------------------------------------------------------------


def test_F2_install_project_scope_writes_under_repo(fake_home: Path, tmp_path: Path) -> None:
    """Step 3.2 AC: install --scope project --repo <tmp> writes under <tmp>/.claude/skills/."""
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    with patch("shutil.which", side_effect=_which_sqlcg):
        result = runner.invoke(app, ["install", "--scope", "project", "--repo", str(repo_root)])
    skill_path = repo_root / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    assert skill_path.exists(), (
        f"install --scope project --repo {repo_root} must write {skill_path}; "
        f"exit={result.exit_code}, output={result.output}"
    )


# ---------------------------------------------------------------------------
# D2 — non-TTY without --scope must error (not guess)
# ---------------------------------------------------------------------------


def test_F2_install_no_scope_non_tty_errors(fake_home: Path) -> None:
    """D2 AC: omitting --scope on non-TTY stdin must exit non-zero with a clear message."""
    with patch("shutil.which", side_effect=_which_sqlcg):
        # CliRunner uses a non-TTY stdin by default
        result = runner.invoke(app, ["install"])
    # With --scope not implemented, install currently succeeds (exit 0).
    # After F2 Phase 3, it must fail with non-zero exit when --scope is absent on non-TTY.
    # This test will fail until the --scope option is wired.
    assert result.exit_code != 0, (
        f"install without --scope on non-TTY must exit non-zero (got {result.exit_code}). "
        "Pass --scope {{project|global}} to avoid this error."
    )
    assert "--scope" in result.output.lower() or "scope" in result.output.lower(), (
        f"Error message must mention '--scope'; got: {result.output!r}"
    )
