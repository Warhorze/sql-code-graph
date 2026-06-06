"""E2E failing acceptance tests for F2 — full install/uninstall skill cycle.

These tests MUST FAIL before the developer implements F2 phases 1-4.
They exercise the full on-disk workflow: install writes SKILL.md, uninstall removes it.
No graph backend is needed — pure filesystem + registry.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()

EXPECTED_TOOLS = [
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
]


def _which_sqlcg(cmd: str) -> str | None:
    if cmd == "sqlcg":
        return "/usr/local/bin/sqlcg"
    return None


def _which_none(cmd: str) -> str | None:
    return None


@pytest.fixture()
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Monkeypatched home + settings path for e2e tests."""
    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr("sqlcg.cli.commands.uninstall._SETTINGS_PATH", settings_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return {"home": tmp_path, "settings": settings_path}


def test_F2_e2e_install_global_then_uninstall(fake_env: dict) -> None:
    """E2E: install --scope global writes SKILL.md; uninstall removes it."""
    fake_home = fake_env["home"]

    # Phase A: install
    with patch("shutil.which", side_effect=_which_sqlcg):
        install_result = runner.invoke(app, ["install", "--scope", "global"])

    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    assert skill_path.exists(), (
        f"install --scope global must write {skill_path}; "
        f"exit={install_result.exit_code}, output={install_result.output!r}"
    )

    # Phase B: verify skill content
    content = skill_path.read_text()
    assert content.startswith("---\n"), "Skill must start with YAML frontmatter"
    assert "name: sqlcg" in content, "Skill must contain 'name: sqlcg'"
    assert "version:" in content, "Skill must contain a version field"
    missing_tools = [t for t in EXPECTED_TOOLS if t not in content]
    assert not missing_tools, f"Skill is missing tool names: {missing_tools}"

    # Phase C: uninstall
    with patch("shutil.which", side_effect=_which_none):
        uninstall_result = runner.invoke(app, ["uninstall", "--keep-db"])

    skill_dir = fake_home / ".claude" / "skills" / "sqlcg"
    assert not skill_dir.exists(), (
        f"uninstall must remove {skill_dir}; "
        f"exit={uninstall_result.exit_code}, output={uninstall_result.output!r}"
    )


def test_F2_e2e_install_project_scope_under_repo(fake_env: dict, tmp_path: Path) -> None:
    """E2E: install --scope project --repo <path> writes skill under the repo."""
    repo_root = tmp_path / "myproject"
    repo_root.mkdir()

    with patch("shutil.which", side_effect=_which_sqlcg):
        result = runner.invoke(app, ["install", "--scope", "project", "--repo", str(repo_root)])

    skill_path = repo_root / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    assert skill_path.exists(), (
        f"install --scope project --repo {repo_root} must write {skill_path}; "
        f"exit={result.exit_code}, output={result.output!r}"
    )
    content = skill_path.read_text()
    assert "name: sqlcg" in content


def test_F2_e2e_skill_yaml_frontmatter_parseable(fake_env: dict) -> None:
    """E2E: the SKILL.md YAML frontmatter is parseable (valid YAML between --- markers)."""
    fake_home = fake_env["home"]

    with patch("shutil.which", side_effect=_which_sqlcg):
        runner.invoke(app, ["install", "--scope", "global"])

    skill_path = fake_home / ".claude" / "skills" / "sqlcg" / "SKILL.md"
    if not skill_path.exists():
        pytest.skip("Skill file not written — install --scope global not implemented")

    content = skill_path.read_text()
    # Extract YAML frontmatter between first and second ---
    lines = content.split("\n")
    assert lines[0] == "---", "First line must be '---'"
    end_idx = None
    for i, line in enumerate(lines[1:], 1):
        if line == "---":
            end_idx = i
            break
    assert end_idx is not None, "YAML frontmatter must be closed with a second '---'"
    yaml_block = "\n".join(lines[1:end_idx])

    # Parse with yaml if available, else just check keys are present
    try:
        import yaml

        frontmatter = yaml.safe_load(yaml_block)
        assert isinstance(frontmatter, dict), "Frontmatter must parse as a dict"
        assert "name" in frontmatter, "Frontmatter must have 'name' key"
        assert frontmatter["name"] == "sqlcg", f"name must be 'sqlcg', got {frontmatter['name']}"
        assert "version" in frontmatter, "Frontmatter must have 'version' key"
    except ImportError:
        # yaml not available — just check key presence in the block
        assert "name: sqlcg" in yaml_block or "name:" in yaml_block
        assert "version:" in yaml_block
