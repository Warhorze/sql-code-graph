"""Pydantic models for MCP tool return types."""

from pydantic import BaseModel, Field


class LineageNode(BaseModel):
    """Node in a lineage graph."""

    name: str = Field(..., description="Name of the node (table or column)")
    kind: str = Field(..., description="Kind of node (table, column, query, etc.)")
    table: str | None = Field(
        None, description="Qualified table the column belongs to (schema.table)"
    )
    file: str | None = Field(None, description="Source file path, if applicable")
    confidence: float | None = Field(None, description="Confidence score 0.0-1.0")


class LineageEdge(BaseModel):
    """Edge in a lineage graph."""

    src: str = Field(..., description="Source column id (table_qualified.col_name)")
    dst: str = Field(..., description="Destination column id (table_qualified.col_name)")
    transform: str | None = Field(None, description="Transform applied (e.g. SELECT, CAST)")


class LineageResult(BaseModel):
    """Result of trace_column_lineage query."""

    column: str = Field(..., description="Column reference (table.column)")
    lineage: list[LineageNode] = Field(
        default_factory=list, description="List of nodes in the lineage"
    )
    mermaid: str | None = Field(
        None,
        description="Mermaid flowchart diagram of the lineage graph. "
        "Render with ```mermaid ... ``` in any Markdown viewer.",
    )
    hint: str | None = Field(
        None,
        description="Diagnostic hint when result list is empty. Explains the likely cause "
        "and suggests a next step.",
    )


class TableUsage(BaseModel):
    """Usage of a table in a query."""

    query_file: str = Field(..., description="File path where query is defined")
    sql: str | None = Field(None, description="SQL of the query")
    kind: str | None = Field(None, description="Kind of query (SELECT, INSERT, etc.)")


class TableUsageResult(BaseModel):
    """Result of find_table_usages query."""

    table: str = Field(..., description="Table name")
    usages: list[TableUsage] = Field(default_factory=list, description="List of usages")
    hint: str | None = Field(
        None,
        description="Diagnostic hint when result list is empty. Explains the likely cause "
        "and suggests a next step.",
    )


class DependencyNode(BaseModel):
    """Node in a dependency graph."""

    name: str = Field(..., description="Name of the node")
    kind: str = Field(..., description="Kind of node (table, column, etc.)")
    table: str | None = Field(
        None, description="Qualified table the column belongs to (schema.table)"
    )


class DependencyResult(BaseModel):
    """Result of dependency traversal queries."""

    root: str = Field(..., description="Root column or table")
    nodes: list[DependencyNode] = Field(default_factory=list, description="List of dependent nodes")
    truncated: bool = Field(
        False,
        description="True if traversal was stopped by max_depth limit or 50k-node safety cap. "
        "False for full-closure runs.",
    )
    depth_reached: int = Field(
        0,
        description="Maximum depth reached before truncation (0 if no nodes, "
        "or if full closure completed).",
    )
    hint: str | None = Field(
        None,
        description="Diagnostic hint when result list is empty. Explains the likely cause "
        "and suggests a next step.",
    )


class SqlPatternMatch(BaseModel):
    """Match for a SQL pattern search."""

    file: str = Field(..., description="File path containing the match")
    sql: str = Field(..., description="SQL text of the match")
    kind: str | None = Field(None, description="Kind of statement")


class SqlPatternResult(BaseModel):
    """Result of search_sql_pattern query."""

    pattern: str = Field(..., description="Pattern searched for")
    matches: list[SqlPatternMatch] = Field(
        default_factory=list, description="List of matching queries"
    )
    hint: str | None = Field(
        None,
        description="Diagnostic hint when result list is empty. Explains the likely cause "
        "and suggests a next step.",
    )


class DialectRepo(BaseModel):
    """Repository with dialect information."""

    path: str = Field(..., description="Repository path")
    name: str | None = Field(None, description="Repository name")
    dialects: list[str] = Field(default_factory=list, description="Dialects used in this repo")


class DialectRepoResult(BaseModel):
    """Result of list_dialects_and_repos query."""

    repos: list[DialectRepo] = Field(
        default_factory=list, description="List of indexed repositories"
    )


class DbInfoResult(BaseModel):
    """Result of db_info tool — graph health and parse quality diagnostics."""

    schema_version: str = Field(..., description="Graph schema version")
    node_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Node counts per label (Repo, SqlTable, SqlQuery, SqlColumn, SqlFile)",
    )
    column_lineage_edges: int = Field(0, description="Number of COLUMN_LINEAGE edges in the graph")
    parse_quality: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Query count by parsing_mode: 'sqlglot' = standard path, "
            "'scripting_block' = tokenizer fallback (column lineage limited)"
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Health warnings. Empty means the graph is in a healthy state.",
    )


class DefinitionFile(BaseModel):
    """A file in which a table is defined."""

    file_path: str = Field(..., description="Path to the SQL file containing the DDL")
    kind: str | None = Field(None, description="Table kind (TABLE, VIEW, etc.)")
    is_authoritative: bool = Field(
        ...,
        description="True when this is the single, non-backup definition. "
        "False when the table is a backup snapshot or defined in multiple files.",
    )
    is_backup: bool = Field(
        ...,
        description="True when the table name matches a configured backup pattern (e.g. *_bck).",
    )


class DefinitionResult(BaseModel):
    """Result of find_definition — where a table is authoritatively defined."""

    table_qualified: str = Field(..., description="Qualified table name that was looked up")
    definitions: list[DefinitionFile] = Field(
        default_factory=list,
        description="All definition files found, including backups (flagged via is_backup).",
    )
    duplicate_ddl: bool = Field(
        False, description="True when the same table is defined in more than one file."
    )
    noise_excluded: list[str] = Field(
        default_factory=list,
        description="Definition file paths that were flagged as backup/noise.",
    )
    hint: str | None = Field(
        None,
        description="Diagnostic hint when no definition is found.",
    )


class ChangeScopeResult(BaseModel):
    """Result of get_change_scope — the minimal reading set + risk for a change."""

    target: str = Field(..., description="Qualified table name that was scoped")
    defining_files: list[str] = Field(
        default_factory=list, description="Files that define the target table"
    )
    upstream_tables: list[str] = Field(
        default_factory=list,
        description="Direct upstream input tables (one hop, not full closure).",
    )
    affected_columns: list[str] = Field(
        default_factory=list,
        description="Downstream column ids (full closure) consuming the target's columns.",
    )
    affected_tables: list[str] = Field(
        default_factory=list,
        description="Table rollup of affected_columns (noise-filtered).",
    )
    risk_label: str = Field(
        ...,
        description="Change risk: 'safe' (0), 'low' (1-5), 'medium' (6-20), 'high' (>20) "
        "by affected table count.",
    )
    risk_weight: int = Field(0, description="Count of affected downstream tables.")
    noise_excluded: list[str] = Field(
        default_factory=list,
        description="Affected tables excluded as backup/noise.",
    )
    truncated: bool = Field(False, description="True if the 50k-node closure safety cap was hit.")
    hint: str | None = Field(None, description="Diagnostic hint when scope is empty.")


class BackfillOrderResult(BaseModel):
    """Result of get_backfill_order — topological rebuild order for a change."""

    target: str = Field(..., description="Qualified table name that was scoped")
    backfill_order: list[str] = Field(
        default_factory=list,
        description="Affected tables in topological rebuild order (producers before consumers).",
    )
    affected_columns: list[str] = Field(
        default_factory=list,
        description="Downstream column ids (full closure), for column-precise understanding.",
    )
    noise_excluded: list[str] = Field(
        default_factory=list, description="Affected tables excluded as backup/noise."
    )
    truncated: bool = Field(False, description="True if the 50k-node closure safety cap was hit.")
    hint: str | None = Field(
        None,
        description="Diagnostic hint — set when a dependency cycle was detected "
        "(order is approximate) or there is nothing to backfill.",
    )


class DiffImpactResult(BaseModel):
    """Result of diff_impact — union blast radius across changed files."""

    changed_files: list[str] = Field(
        default_factory=list, description="Input file paths that were analysed"
    )
    changed_tables: list[str] = Field(
        default_factory=list, description="Tables defined in the changed files"
    )
    affected_tables: list[str] = Field(
        default_factory=list,
        description="Union downstream blast radius across all changed tables (noise-filtered).",
    )
    presentation_facing: list[str] = Field(
        default_factory=list,
        description="Subset of affected_tables whose schema matches a configured "
        "presentation prefix ([sqlcg.presentation] schema_prefixes). Empty when unconfigured.",
    )
    backfill_order: list[str] = Field(
        default_factory=list,
        description="Affected tables in topological rebuild order across the union blast radius.",
    )
    noise_excluded: list[str] = Field(
        default_factory=list, description="Affected tables excluded as backup/noise."
    )
    truncated: bool = Field(False, description="True if the 50k-node closure safety cap was hit.")
    hint: str | None = Field(
        None, description="Diagnostic hint when result is empty or approximate."
    )


class ScopeChangeResult(BaseModel):
    """Result of scope_change — single-call synthesis of everything an LLM needs
    before changing a table."""

    target: str = Field(..., description="Qualified table name being changed")
    authoritative_files: list[str] = Field(
        default_factory=list, description="Authoritative (non-backup) defining files."
    )
    upstream_inputs: list[str] = Field(
        default_factory=list, description="Direct upstream input tables."
    )
    downstream_blast_radius: list[str] = Field(
        default_factory=list, description="Full-depth affected tables (noise-filtered)."
    )
    affected_columns: list[str] = Field(
        default_factory=list, description="Column-precise downstream blast radius."
    )
    backfill_order: list[str] = Field(
        default_factory=list, description="Topological rebuild order for the blast radius."
    )
    risk_label: str = Field(..., description="Change risk: 'safe' | 'low' | 'medium' | 'high'.")
    risk_weight: int = Field(0, description="Count of affected downstream tables.")
    noise_excluded: list[str] = Field(
        default_factory=list, description="Backup/noise tables and files excluded from the answer."
    )
    truncated: bool = Field(
        False, description="True if any underlying closure hit the 50k-node safety cap."
    )
    hint: str | None = Field(
        None, description="Combined hints from the underlying tools (joined with ' | ')."
    )
