"""Tests for `sqlcg mcp best-practices` — surfaces the fact/heuristic boundary in the CLI."""

from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


def test_best_practices_prints_boundary_and_tools():
    """The command prints the boundary doc, the tool table, and the heuristic tools."""
    result = runner.invoke(app, ["mcp", "best-practices"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Boundary contract is present
    assert "Facts" in out and "Heuristics" in out
    # The three heuristic-bearing tools are named in the guidance
    for tool in ("analyze_unused", "get_change_scope", "scope_change"):
        assert tool in out, f"{tool} missing from best-practices output"
    # It is the body only — no YAML frontmatter leaks into CLI output
    assert not out.lstrip().startswith("---")


def test_best_practices_matches_skill_body():
    """CLI output is the single-source skill body (no drift between CLI and skill)."""
    from sqlcg.server.skill import render_body

    result = runner.invoke(app, ["mcp", "best-practices"])
    assert result.exit_code == 0, result.output
    assert render_body().strip() in result.output
