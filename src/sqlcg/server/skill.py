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
    EmptyPropagationResult,
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
    "Use the sqlcg MCP tools for SQL lineage analysis, honoring the "
    "fact/heuristic boundary — never report a heuristic (dead-code candidates, "
    "risk ratings) as a fact."
)

_BOUNDARY = """\
# sqlcg — SQL lineage analysis

**Facts**: deterministic graph reads (counts, edges, paths). Ground truth.
**Heuristics**: interpretations carrying `confidence` (uncalibrated) + `reason`. \
Never present as fact — surface `reason`/`confidence` and have the user validate. \
Only `get_change_scope`/`scope_change` (`risk`) and `analyze_unused` \
(`dead_code`, confidence 0.5) are heuristics. `trace_column_lineage` returns \
deterministic fact edges (`confidence=1.0`, `reason=None`) for plainly-parsed \
SELECT statements; `confidence<1.0` with a non-null `reason` flags an inferred \
edge — surface `reason` and treat with caution.

**Provenance (#31)**: every `LineageNode` returned by `trace_column_lineage` carries \
`file` (source file path), `line` (1-based start line of the producing statement), and \
`expression` (SQL text of that statement, truncated). When explaining a lineage edge, \
always cite `file` and `line` so the user can navigate to the exact location — never assert \
an unsourced edge when provenance is available.

**Node kind (#33)**: `LineageNode.table_kind` distinguishes the structural role of the source: \
`"table"` = a real persisted table, `"cte"` = a named CTE, `"derived"` = an inline subquery, \
`"external"` = a source outside the indexed corpus. Never present a `"cte"` or `"derived"` \
intermediate as the ultimate source table — follow the chain until `table_kind == "table"` \
(or `"external"`) to name the true origin.
"""

_WORKFLOWS = """\
## Workflows

- **Change a table**: `scope_change(t)` → read `authoritative_files`, follow `backfill_order`; treat `risk` as heuristic.
- **Trace lineage**: `trace_column_lineage(schema.table.column)` (fact). Empty = unresolved at index time, not "no lineage" — check `hint`. Each node carries `file`/`line`/`expression` provenance — cite them when reporting. Use `table_kind` to distinguish real tables (`"table"`) from intermediate CTEs (`"cte"`) or subqueries (`"derived"`).
- **Dead code**: `analyze_unused()` → `candidates` (`dead_code`, heuristic, confidence 0.5) are orphans OUTSIDE the declared egress layer; `presentation_facing` are terminal/egress leaves (config `[sqlcg.presentation]`) expected to have no in-corpus consumer — do NOT suggest deleting those. `has_external_consumer=True` on a `presentation_facing` entry means a declared external egress consumer (e.g. Tableau, reverse-ETL) is attached via `CONSUMED_BY` — it is a provable egress point. Show `reason` before suggesting deletion of any candidate.
- **CI impact**: `diff_impact(changed_files)` (fact); `presentation_facing` flags user-visible tables; `external_consumers` lists any declared external egress consumers (e.g. Tableau, BI tools) attached to tables in the blast radius via `CONSUMED_BY` edges — injected at index time from `[[sqlcg.external_consumers]]` in `.sqlcg.toml`.
- **Downstream egress**: `get_downstream_dependencies(col)` appends `DependencyNode(kind="external_consumer")` entries for any declared external consumers attached to terminal tables — these represent data leaving the modeled SQL corpus.
- **Hub topology**: `get_hub_ranking(k)` (fact); high hub = high-risk change target, quantify via `scope_change`.
"""

# ---------------------------------------------------------------------------
# Explicit tool → return-model map (single source for fact/heuristic label)
# The map covers all 17 @mcp.tool()-decorated functions.
# For tools returning plain dict/list[dict] (no Pydantic model), we use
# BaseModel directly — it has no Judgement field → tagged as fact.
# ---------------------------------------------------------------------------

TOOL_RETURN_MODELS: dict[str, type[BaseModel]] = {
    # Operational tools — return plain dict, no Judgement
    "index_repo": BaseModel,
    "execute_sql": BaseModel,
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
    # Empty-impact blast radius (two-view, fact-only)
    "get_empty_propagation": EmptyPropagationResult,
}


# ---------------------------------------------------------------------------
# Tool purpose descriptions (one-liner per tool for the generated table)
# ---------------------------------------------------------------------------

_TOOL_PURPOSE: dict[str, str] = {
    "index_repo": "Index a SQL repository into the graph",
    "execute_sql": "Execute a read-only SQL query against the graph (DuckDB)",
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
    "get_empty_propagation": "Downstream blast radius (two views) when named table(s) are empty",
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
    """Generate the compact Markdown tool table from the live registry.

    Rows are grouped facts-first then heuristics-last so the boundary is
    visible from the table alone (no separate heuristic list needed).
    """
    facts: list[str] = []
    heuristics: list[str] = []
    for tool_name in list_registered_tools():
        model = TOOL_RETURN_MODELS.get(tool_name, BaseModel)
        purpose = _TOOL_PURPOSE.get(tool_name, "")
        if _tool_is_heuristic(model):
            heuristics.append(f"| `{tool_name}` | {purpose} | heuristic |")
        else:
            facts.append(f"| `{tool_name}` | {purpose} | fact |")
    lines = ["## Tools", "", "| Tool | Purpose | Type |", "|---|---|---|"]
    lines.extend(facts)
    lines.extend(heuristics)
    lines.append("")
    return "\n".join(lines)


def render_body() -> str:
    """Return the skill body — boundary + generated tool table + workflows.

    This is the human/agent-facing guidance without YAML frontmatter. Reused by
    the `sqlcg mcp best-practices` CLI command so the boundary doc has a single
    source and cannot drift from the bundled skill.
    """
    return _BOUNDARY + "\n" + _generate_tool_table() + "\n" + _WORKFLOWS


def render_skill(version: str) -> str:
    """Return the full SKILL.md text.

    Produces: YAML frontmatter + the shared body (boundary prose + generated
    tool table + workflow prose). This is the single entry point the installer
    calls.

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

    return frontmatter + "\n" + render_body()
