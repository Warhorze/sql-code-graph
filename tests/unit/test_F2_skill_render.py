"""Failing acceptance tests for F2 — skill renderer (phases 1 & 2).

These tests MUST FAIL before the developer implements skill.py.
They encode the acceptance criteria from plan/sprints/bundle_claude_skill.md phases 1 and 2.
"""

import pytest

# All symbols below are introduced by F2 Phase 1/2 — import with skip guard
try:
    from sqlcg.server.skill import (  # introduced by F2 Phase 1
        TOOL_RETURN_MODELS,
        _tool_is_heuristic,
        list_registered_tools,
        render_skill,
    )

    _SKILL_MODULE_AVAILABLE = True
except ImportError:
    _SKILL_MODULE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Phase 1: render_skill basic content (Step 1.1 acceptance criteria)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 1 not yet implemented")
def test_F2_render_skill_starts_with_yaml_frontmatter():
    """Step 1.1 AC: render_skill returns string starting with YAML frontmatter marker."""
    output = render_skill("0.3.1")
    assert output.startswith("---\n"), (
        f"render_skill output must start with '---\\n' (YAML frontmatter); got: {repr(output[:40])}"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 1 not yet implemented")
def test_F2_render_skill_frontmatter_contains_name_sqlcg():
    """Step 1.1 AC: frontmatter contains 'name: sqlcg'."""
    output = render_skill("0.3.1")
    assert "name: sqlcg" in output, (
        "render_skill output must contain 'name: sqlcg' in YAML frontmatter"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 1 not yet implemented")
def test_F2_render_skill_frontmatter_contains_version():
    """Step 1.1 AC: frontmatter contains the version passed in."""
    output = render_skill("0.3.1")
    assert "version: 0.3.1" in output, (
        "render_skill output must contain 'version: 0.3.1' when called with '0.3.1'"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 1 not yet implemented")
def test_F2_render_skill_contains_facts_section():
    """Step 1.1 AC: output contains 'facts' section (fact/heuristic contract)."""
    output = render_skill("0.3.1")
    assert "fact" in output.lower(), (
        "render_skill output must mention 'facts' in the fact/heuristic contract section"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 1 not yet implemented")
def test_F2_render_skill_contains_heuristics_section():
    """Step 1.1 AC: output contains 'heuristic' section."""
    output = render_skill("0.3.1")
    assert "heuristic" in output.lower(), (
        "render_skill output must mention 'heuristics' in the fact/heuristic contract section"
    )


# ---------------------------------------------------------------------------
# Phase 2: drift guard (Step 2.1 acceptance criteria)
# ---------------------------------------------------------------------------


def test_F2_skill_module_exists():
    """Gate: sqlcg.server.skill module must exist (F2 Phase 1 not yet landed)."""
    if _SKILL_MODULE_AVAILABLE:
        pytest.skip("F2 already implemented — this gate test is satisfied")
    # Force failure if module is missing
    pytest.fail(
        "sqlcg.server.skill does not exist — F2 Phase 1 not implemented. "
        "Create src/sqlcg/server/skill.py with render_skill(), list_registered_tools(), "
        "TOOL_RETURN_MODELS, and _tool_is_heuristic()."
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 2 not yet implemented")
def test_F2_drift_guard_all_registered_tools_in_skill():
    """Step 2.1 AC (drift guard part a): every registered @mcp.tool() name appears in the skill.

    v1.22.0: 17 tools (16 original + get_empty_propagation).
    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 2.1 tool-count.
    """
    registered = list_registered_tools()
    assert len(registered) > 0, "list_registered_tools() must return a non-empty list"
    assert len(registered) == 17, (
        f"Expected 17 registered tools (v1.22.0 adds get_empty_propagation), "
        f"got {len(registered)}: {registered}"
    )
    skill_text = render_skill("0.3.1")
    missing = [name for name in registered if name not in skill_text]
    assert not missing, (
        f"These registered tool names are missing from the rendered skill: {missing}"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 2 not yet implemented")
def test_F2_drift_guard_skill_names_no_extra_tools():
    """Step 2.1 AC (drift guard part b): the skill names no tool absent from the registry."""
    registered = set(list_registered_tools())
    # This test is structural — it requires inspecting the tool table in the output.
    # We verify via TOOL_RETURN_MODELS: all keys must be registered.
    extra = set(TOOL_RETURN_MODELS.keys()) - registered
    assert not extra, (
        f"TOOL_RETURN_MODELS contains keys not in the live registry: {extra}. "
        "These would produce ghost tool entries in the rendered skill."
    )
    missing_from_map = registered - set(TOOL_RETURN_MODELS.keys())
    assert not missing_from_map, (
        f"Live registry has tools not in TOOL_RETURN_MODELS: {missing_from_map}. "
        "All registered tools must have a return model mapped."
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 2 not yet implemented")
def test_F2_drift_guard_heuristic_tools_tagged_correctly():
    """Step 2.1 AC (drift guard part c): heuristic-bearing tools are tagged 'heuristic'."""
    skill_text = render_skill("0.3.1")
    # Tools that MUST appear with heuristic tag
    heuristic_tools = ["analyze_unused", "get_change_scope", "scope_change"]
    for tool_name in heuristic_tools:
        assert tool_name in skill_text, f"Tool '{tool_name}' must appear in rendered skill"
    # The heuristic label must appear near each heuristic tool
    # We verify via _tool_is_heuristic on the models
    from sqlcg.server.models import ChangeScopeResult, ScopeChangeResult, UnusedTablesResult

    for model, name in [
        (UnusedTablesResult, "analyze_unused"),
        (ChangeScopeResult, "get_change_scope"),
        (ScopeChangeResult, "scope_change"),
    ]:
        assert _tool_is_heuristic(model), (
            f"_tool_is_heuristic({model.__name__}) must return True for '{name}'"
        )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 2 not yet implemented")
def test_F2_drift_guard_fact_tools_tagged_correctly():
    """Step 2.1 AC (drift guard part c): pure-fact tools are NOT tagged heuristic."""
    from sqlcg.server.models import HubRankingResult, LineageResult

    assert not _tool_is_heuristic(HubRankingResult), (
        "_tool_is_heuristic(HubRankingResult) must return False — get_hub_ranking is a pure fact"
    )
    assert not _tool_is_heuristic(LineageResult), (
        "_tool_is_heuristic(LineageResult) must return False — trace_column_lineage is a pure fact"
    )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase 2 not yet implemented")
def test_F2_tool_return_models_covers_all_17_tools():
    """Step 2.1 AC: TOOL_RETURN_MODELS has exactly 17 entries matching the live registry.

    v1.22.0: 17 tools (16 original + get_empty_propagation).
    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 2.1 tool-count.
    """
    registered = list_registered_tools()
    assert len(TOOL_RETURN_MODELS) == 17, (
        f"TOOL_RETURN_MODELS must map exactly 17 tools, got {len(TOOL_RETURN_MODELS)}: "
        f"{list(TOOL_RETURN_MODELS.keys())}"
    )
    assert len(registered) == 17, (
        f"list_registered_tools() must return 17 tools, got {len(registered)}"
    )


# ---------------------------------------------------------------------------
# Phase A: provenance (#31) and node-kind (#33) prose assertions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase A not yet implemented")
def test_F2_render_skill_teaches_provenance_fields():
    """Phase A AC: rendered skill body mentions the #31 provenance fields file/line/expression.

    LineageNode carries file, line, and expression so the LLM can cite the exact
    source location of each lineage edge. The skill must teach this — asserting on
    rendered output, not merely on structure.
    """
    output = render_skill("0.3.1")
    for field in ("file", "line", "expression"):
        assert field in output, (
            f"render_skill output must mention the provenance field '{field}' "
            f"(LineageNode.{field} from #31) so the LLM cites source locations. "
            f"Field missing from rendered skill."
        )


@pytest.mark.skipif(not _SKILL_MODULE_AVAILABLE, reason="F2 Phase A not yet implemented")
def test_F2_render_skill_teaches_table_kind_distinction():
    """Phase A AC: rendered skill body teaches the #33 table_kind node-kind distinction.

    LineageNode.table_kind distinguishes 'table' / 'cte' / 'derived' / 'external'.
    The skill must teach the LLM to use table_kind to avoid presenting a CTE/derived
    intermediate as the ultimate source table.
    """
    output = render_skill("0.3.1")
    assert "table_kind" in output, (
        "render_skill output must mention 'table_kind' (#33) so the LLM can distinguish "
        "real tables from CTE/derived intermediates when interpreting lineage chains."
    )
    # At least one of the concrete kind values must appear to make the teaching actionable
    kind_values = {"cte", "derived", "external"}
    # Check in full output (not just word-split, to handle quoted/backtick forms)
    present_in_text = [k for k in kind_values if k in output]
    assert present_in_text, (
        f"render_skill output must name at least one concrete table_kind value "
        f"({kind_values}) so the LLM knows the enumeration. None found in rendered skill."
    )
