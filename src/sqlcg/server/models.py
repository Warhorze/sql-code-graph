"""Pydantic models for MCP tool return types."""

from pydantic import BaseModel, Field


class LineageNode(BaseModel):
    """Node in a lineage graph."""

    name: str = Field(..., description="Name of the node (table or column)")
    kind: str = Field(..., description="Kind of node (table, column, query, etc.)")
    file: str | None = Field(None, description="Source file path, if applicable")
    confidence: float | None = Field(None, description="Confidence score 0.0-1.0")


class LineageResult(BaseModel):
    """Result of trace_column_lineage query."""

    column: str = Field(..., description="Column reference (table.column)")
    lineage: list[LineageNode] = Field(
        default_factory=list, description="List of nodes in the lineage"
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


class DependencyNode(BaseModel):
    """Node in a dependency graph."""

    name: str = Field(..., description="Name of the node")
    kind: str = Field(..., description="Kind of node (table, column, etc.)")


class DependencyResult(BaseModel):
    """Result of dependency traversal queries."""

    root: str = Field(..., description="Root column or table")
    nodes: list[DependencyNode] = Field(default_factory=list, description="List of dependent nodes")


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
    warnings: list[str] = Field(
        default_factory=list,
        description="Health warnings about the indexed graph state",
    )
