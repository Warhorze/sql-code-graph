"""Failing acceptance tests for F2 — uninstall removes skill (phase 4).

These tests MUST FAIL before the developer extends uninstall.py.
They encode the acceptance criteria from plan/sprints/bundle_claude_skill.md phase 4.
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


def _which_none(cmd: str) -> str | None:
    return None


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _SETTINGS_PATH (uninstall) and Path.home() to tmp_path."""
    monkeypatch.setattr(
        "sqlcg.cli.commands.uninstall._SETTINGS_PATH",
        tmp_path / ".claude" / "settings.json",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Step 4.1 AC: uninstall removes the skill dir at global location
# ---------------------------------------------------------------------------


def test_F2_uninstall_removes_global_skill_dir(fake_home: Path) -> None:
    """Step 4.1 AC: after install --scope global then uninstall, ~/.claude/skills/sqlcg/ is gone."""
    # Pre-create skill directory as if install ran
    skill_dir = fake_home / ".claude" / "skills" / "sqlcg"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: sqlcg\n---\ntest content")

    with patch("shutil.which", side_effect=_which_none):
        # uninstall --keep-db to avoid db deletion prompt; --force to skip confirm
        result = runner.invoke(app, ["uninstall", "--keep-db"])

    assert not skill_dir.exists(), (
        f"~/.claude/skills/sqlcg/ must be removed by uninstall; "
        f"exit={result.exit_code}, output={result.output!r}"
    )


def test_F2_uninstall_preserves_foreign_sibling_skill(fake_home: Path) -> None:
    """Step 4.1 AC: uninstall removes sqlcg dir but not sibling foreign skill."""
    # Pre-create the sqlcg skill dir
    sqlcg_skill_dir = fake_home / ".claude" / "skills" / "sqlcg"
    sqlcg_skill_dir.mkdir(parents=True)
    (sqlcg_skill_dir / "SKILL.md").write_text("---\nname: sqlcg\n---\ntest")

    # Pre-create a foreign sibling skill
    other_skill_dir = fake_home / ".claude" / "skills" / "other-skill"
    other_skill_dir.mkdir(parents=True)
    foreign_skill = other_skill_dir / "SKILL.md"
    foreign_skill.write_text("---\nname: other-skill\n---\nforeign content")

    with patch("shutil.which", side_effect=_which_none):
        runner.invoke(app, ["uninstall", "--keep-db"])

    # sqlcg dir gone
    assert not sqlcg_skill_dir.exists(), "sqlcg skill directory must be removed by uninstall"
    # foreign sibling preserved
    assert foreign_skill.exists(), (
        f"Foreign sibling skill at {foreign_skill} must NOT be removed by uninstall"
    )
    assert foreign_skill.read_text() == "---\nname: other-skill\n---\nforeign content", (
        "Foreign skill content must be unchanged"
    )


def test_F2_uninstall_clean_machine_exits_zero(fake_home: Path) -> None:
    """Step 4.1 AC: uninstall on a clean machine (no skill dir) prints 'not found' and exits 0."""
    # No skill directory pre-created
    skill_dir = fake_home / ".claude" / "skills" / "sqlcg"
    assert not skill_dir.exists()

    with patch("shutil.which", side_effect=_which_none):
        result = runner.invoke(app, ["uninstall", "--keep-db"])

    assert result.exit_code == 0, (
        f"uninstall on clean machine must exit 0; got exit={result.exit_code}, "
        f"output={result.output!r}"
    )
    # The output should mention "not found" (mirrors the git hook idiom)
    assert "not found" in result.output.lower() or "no skill" in result.output.lower(), (
        f"uninstall on clean machine must print a 'not found' notice; got: {result.output!r}"
    )
