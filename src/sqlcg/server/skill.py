"""Skill renderer for F2 — generates the sqlcg SKILL.md Claude skill file.

Pure functions, no I/O. The installer calls render_skill() to produce the
SKILL.md text, which is then written to disk atomically.
"""

import typing

from pydantic import BaseModel

from sqlcg.server.models import (
    BackfillOrderResult,
    ChangeScopeResult,
    DefinitionResult,
    DependencyResult,
    DialectRepoResult,
    DiffImpactResult,
    HubRankingResult,
    Judgement,
    LineageResult,
    ScopeChangeResult,
    SqlPatternResult,
    TableUsageResult,
    UnusedTablesResult,
)

# ---------------------------------------------------------------------------
# Curated prose constants (the single source for hand-written sections)
# ---------------------------------------------------------------------------

SKILL_NAME = "sqlcg"

_FRONTMATTER_DESCRIPTION = (
    "Teach Claude how to use the sqlcg MCP tools for SQL lineage analysis. "
    "Encodes the fact/heuristic boundary so Claude never reports heuristic "
    "output (dead-code candidates, risk ratings) as deterministic facts."
)

_PHILOSOPHY = """\
## Philosophy: Facts vs. Heuristics

sqlcg tools return two categories of output:

**Facts** are deterministic graph reads — counts, edges, file paths derived
directly from the indexed SQL corpus. They are always accurate relative to
what has been indexed. Treat them as ground truth.

**Heuristics** are interpretations layered on top of facts. They carry a
`confidence` score (an uncalibrated coarse self-assessment, not a calibrated
probability) and a `reason` that cites the concrete facts it was derived from.
You MUST NOT report heuristics to the user as facts. Always surface the
`reason` and `confidence` alongside the interpretation, and make clear that
the user should validate before acting.

### Which outputs are heuristics?

| Tool | Heuristic field | Why it is heuristic |
|------|----------------|---------------------|
| `analyze_unused` | `dead_code` per candidate | May be consumed externally. confidence=0.5. |
| `get_change_scope` | `risk` | Downstream count vs. uncalibrated threshold. |
| `scope_change` | `risk` | Same as get_change_scope. |

All other tool outputs (lineage paths, dependency lists, file definitions,
hub rankings, pattern matches) are **facts**: direct reads from the graph.
"""

_WORKFLOWS = """\
## Recommended Workflows

### Before changing a table
1. Call `scope_change(<table>)` — returns authoritative files, upstream
   inputs, downstream blast radius, backfill order, and a heuristic `risk`.
2. Read the `authoritative_files` (the source of truth DDL/CTAS files).
3. Follow `backfill_order` for the rebuild sequence.
4. Surface `risk` to the user as a **heuristic** with its `reason` and
   `confidence` — do not report it as a fact.

### Tracing column lineage
1. Call `trace_column_lineage(<schema.table.column>)`.
2. The result is a **fact**: a direct graph traversal. An empty result
   means the column was not resolved at index time, not that it has no
   lineage — check `hint` for the likely cause.

### Finding dead-code candidates
1. Call `analyze_unused()`.
2. Each `dead_code` field is a **heuristic** (confidence=0.5). The table
   may be consumed by external BI tools, APIs, or COPY INTO statements.
3. Always present the `reason` to the user before recommending deletion.

### Assessing CI impact
1. Call `diff_impact(<list_of_changed_files>)`.
2. Result is a **fact**: union blast radius across all changed tables.
3. Use `presentation_facing` to identify user-visible tables first.

### Understanding the hub topology
1. Call `get_hub_ranking(k=10)` to find the most-depended-on tables.
2. All fields are **facts**: downstream_dependents is a direct edge count.
3. High hub tables are high-risk targets for changes — but quantifying
   that risk still requires `scope_change` for the threshold heuristic.
"""

# ---------------------------------------------------------------------------
# Explicit tool → return-model map (single source for fact/heuristic label)
# The map covers all 16 @mcp.tool()-decorated functions.
# For tools returning plain dict/list[dict] (no Pydantic model), we use
# BaseModel directly — it has no Judgement field → tagged as fact.
# ---------------------------------------------------------------------------

TOOL_RETURN_MODELS: dict[str, type[BaseModel]] = {
    # Operational tools — return plain dict, no Judgement
    "index_repo": BaseModel,
    "execute_cypher": BaseModel,
    "submit_feedback": BaseModel,
    # Lineage / dependency facts
    "trace_column_lineage": LineageResult,
    "find_table_usages": TableUsageResult,
    "find_definition": DefinitionResult,
    "get_downstream_dependencies": DependencyResult,
    "get_upstream_dependencies": DependencyResult,
    "get_backfill_order": BackfillOrderResult,
    "diff_impact": DiffImpactResult,
    "search_sql_pattern": SqlPatternResult,
    "list_dialects_and_repos": DialectRepoResult,
    "get_hub_ranking": HubRankingResult,
    # Fact + embedded heuristic (risk: Judgement)
    "get_change_scope": ChangeScopeResult,
    "scope_change": ScopeChangeResult,
    # Heuristic-bearing (dead_code: Judgement)
    "analyze_unused": UnusedTablesResult,
}


# ---------------------------------------------------------------------------
# Tool purpose descriptions (one-liner per tool for the generated table)
# ---------------------------------------------------------------------------

_TOOL_PURPOSE: dict[str, str] = {
    "index_repo": "Index a SQL repository into the graph",
    "execute_cypher": "Execute a read-only Cypher query against the graph",
    "submit_feedback": "Submit feedback (TP/FP/FN) on a tool result",
    "trace_column_lineage": "Trace upstream column lineage to its source",
    "find_table_usages": "Find all queries that consume a given table",
    "find_definition": "Find the authoritative DDL file(s) for a table",
    "get_downstream_dependencies": "Get full-depth downstream dependents of a column/table",
    "get_upstream_dependencies": "Get full-depth upstream dependencies of a column/table",
    "get_backfill_order": "Return topological rebuild order for a table change",
    "diff_impact": "Union blast radius across a set of changed files",
    "search_sql_pattern": "Substring search across all indexed SQL text",
    "list_dialects_and_repos": "List indexed repositories and their SQL dialects",
    "get_hub_ranking": "Rank tables by downstream dependent count (hub topology)",
    "get_change_scope": "Minimal reading set + heuristic risk for a table change",
    "scope_change": "Single-call synthesis: files, upstreams, blast radius, risk",
    "analyze_unused": "Find tables with no within-corpus consumers (dead-code candidates)",
}


# ---------------------------------------------------------------------------
# Core helper: determine if a model carries a Judgement-typed field
# ---------------------------------------------------------------------------


def _tool_is_heuristic(model: type[BaseModel]) -> bool:
    """Return True iff the model or any nested Pydantic model it references
    contains a Judgement-typed field.

    The check is structural: it inspects the model's type annotations for a
    direct Judgement annotation, including nested models one level deep (e.g.
    UnusedTablesResult.candidates → list[UnusedCandidate] → dead_code:
    Judgement). This keeps the fact/heuristic label derived from the actual
    model surface, not a hand-maintained list.
    """

    def _has_judgement(m: type[BaseModel], visited: set[type]) -> bool:
        if m in visited:
            return False
        visited.add(m)
        try:
            hints = typing.get_type_hints(m)
        except Exception:
            return False
        for field_type in hints.values():
            # Direct annotation: field: Judgement
            if field_type is Judgement:
                return True
            # Optional[Judgement] / Judgement | None
            origin = getattr(field_type, "__origin__", None)
            if origin is typing.Union:
                args = getattr(field_type, "__args__", ())
                if Judgement in args:
                    return True
                # Recurse into Union args that are BaseModel subclasses
                for arg in args:
                    if isinstance(arg, type) and issubclass(arg, BaseModel):
                        if _has_judgement(arg, visited):
                            return True
            # list[SomeModel] — recurse into the item type
            if origin is list:
                args = getattr(field_type, "__args__", ())
                for arg in args:
                    if isinstance(arg, type) and issubclass(arg, BaseModel):
                        if _has_judgement(arg, visited):
                            return True
            # Direct nested BaseModel
            if isinstance(field_type, type) and issubclass(field_type, BaseModel):
                if _has_judgement(field_type, visited):
                    return True
        return False

    return _has_judgement(model, set())


# ---------------------------------------------------------------------------
# Live registry reader
# ---------------------------------------------------------------------------


def list_registered_tools() -> list[str]:
    """Return tool names from the live FastMCP registry.

    Reads `mcp._tool_manager._tools` (a dict keyed by tool name). The import
    of `sqlcg.server.tools` is deferred to avoid circular imports at module
    load time — tools.py imports from server.py which creates the `mcp`
    instance.

    Fallback: if the private FastMCP accessor is unavailable (e.g. FastMCP
    upgrade renames the attr), returns `list(TOOL_RETURN_MODELS.keys())`.
    The drift-guard test cross-checks these against the live registry, so the
    fallback stays correct even if the accessor is renamed.
    """
    # Trigger tool registration by importing tools module
    import sqlcg.server.tools as _tools_mod  # noqa: F401
    from sqlcg.server.server import mcp as _mcp

    try:
        return list(_mcp._tool_manager._tools.keys())
    except AttributeError:
        # FastMCP API changed — fall back to the explicit map
        return list(TOOL_RETURN_MODELS.keys())


# ---------------------------------------------------------------------------
# Skill renderer — single entry point called by install
# ---------------------------------------------------------------------------


def _generate_tool_table() -> str:
    """Generate the Markdown tool table from the live registry and return models."""
    registered = list_registered_tools()
    lines: list[str] = [
        "## Tool Reference",
        "",
        "| Tool | Purpose | Type |",
        "|------|---------|------|",
    ]
    for tool_name in registered:
        model = TOOL_RETURN_MODELS.get(tool_name, BaseModel)
        tag = "heuristic" if _tool_is_heuristic(model) else "fact"
        purpose = _TOOL_PURPOSE.get(tool_name, "")
        lines.append(f"| `{tool_name}` | {purpose} | {tag} |")
    lines.append("")
    return "\n".join(lines)


def render_skill(version: str) -> str:
    """Return the full SKILL.md text.

    Produces: YAML frontmatter + philosophy prose + generated tool table +
    workflow prose. This is the single entry point the installer calls.

    Args:
        version: Version string to embed in the frontmatter (e.g. "0.3.1").

    Returns:
        Complete SKILL.md text as a string, starting with '---\\n'.
    """
    frontmatter = (
        "---\n"
        f"name: {SKILL_NAME}\n"
        f"description: {_FRONTMATTER_DESCRIPTION}\n"
        f"version: {version}\n"
        "---\n"
    )

    tool_table = _generate_tool_table()

    return frontmatter + "\n" + _PHILOSOPHY + "\n" + tool_table + "\n" + _WORKFLOWS
