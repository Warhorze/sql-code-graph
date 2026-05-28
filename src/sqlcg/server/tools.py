"""MCP tools for SQL code graph queries and indexing."""

import re
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlcg.core.config import get_db_path
from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.queries import (
    FIND_TABLE_USAGES_QUERY,
    GET_DOWNSTREAM_DEPENDENCIES_QUERY,
    GET_UPSTREAM_DEPENDENCIES_QUERY,
    INDEX_REPO_FILES_QUERY,
    LIST_DIALECTS_AND_REPOS_QUERY,
    SEARCH_SQL_PATTERN_QUERY,
    TRACE_COLUMN_LINEAGE_QUERY,
)
from sqlcg.core.schema import NodeLabel
from sqlcg.indexer.indexer import Indexer
from sqlcg.metrics.store import MetricsStore
from sqlcg.server.exceptions import InvalidColumnRefError, NotIndexedError
from sqlcg.server.models import (
    DbInfoResult,
    DependencyNode,
    DependencyResult,
    DialectRepo,
    DialectRepoResult,
    LineageNode,
    LineageResult,
    SqlPatternMatch,
    SqlPatternResult,
    TableUsage,
    TableUsageResult,
)
from sqlcg.server.server import mcp  # noqa: F401
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

# Module-level db path override (set by init_backend)
_db_path_override: str | None = None

# Module-level metrics store singleton
_metrics: MetricsStore | None = None


def init_backend(db_path: str | None = None) -> None:
    """Record the db path for per-request connections.

    The MCP server no longer holds a persistent KuzuDB connection so that
    sqlcg index can acquire the write lock between tool calls.
    """
    global _db_path_override, _metrics
    _db_path_override = db_path or str(get_db_path())
    logger.debug(f"Backend path set: {_db_path_override}")

    try:
        metrics_path = Path.home() / ".sqlcg" / "metrics.db"
        _metrics = MetricsStore(metrics_path)
        _metrics.init_schema()
    except Exception as exc:
        logger.warning(f"Failed to initialize metrics store: {exc}")


def shutdown_backend() -> None:
    """No-op — connections are now opened and closed per request."""
    global _metrics
    if _metrics is not None:
        _metrics.close()
        _metrics = None


@contextmanager
def _open_backend() -> Iterator[GraphBackend]:
    """Open a fresh read-only connection for one tool call, then close it."""
    path = _db_path_override or str(get_db_path())
    backend = KuzuBackend(path, read_only=True)
    backend.init_schema()
    try:
        yield backend
    finally:
        backend.close()


def _assert_indexed(db: GraphBackend) -> None:
    """Check that the graph has indexed repos.

    Args:
        db: GraphBackend instance

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    rows = db.run_read("MATCH (r:Repo) RETURN count(r) AS n", {})
    if not rows or rows[0]["n"] == 0:
        raise NotIndexedError(
            "No repos indexed. Run 'sqlcg db init' then 'sqlcg index <path>' first."
        )


def _parse_column_ref(col_ref: str) -> tuple[str, str]:
    """Parse column reference "table.column" or "catalog.db.table.column".

    Args:
        col_ref: Column reference string

    Returns:
        Tuple of (table_id, column_name)

    Raises:
        InvalidColumnRefError: If format is invalid
    """
    parts = col_ref.split(".")
    if len(parts) < 2:
        raise InvalidColumnRefError(
            f"Invalid column reference: {col_ref} (expected 'table.column' or "
            f"'catalog.db.table.column')"
        )
    # Last part is the column name, everything before is the table id
    column_name = parts[-1]
    table_id = ".".join(parts[:-1])
    return table_id, column_name


def _record_tool_call(tool_name: str, duration_ms: float, success: bool = True) -> None:
    """Record a tool call to metrics (best-effort).

    Args:
        tool_name: Name of the tool.
        duration_ms: Execution time in milliseconds.
        success: Whether the call succeeded.
    """
    global _metrics
    if _metrics is not None:
        try:
            _metrics.record_tool_call(tool_name, duration_ms, success)
        except Exception as exc:
            logger.warning(f"Failed to record metrics for {tool_name}: {exc}")


def _timed_tool(tool_name: str):
    """Decorator to record tool execution timing and success.

    Args:
        tool_name: Name of the tool to record.
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                _record_tool_call(tool_name, duration_ms, True)
                return result
            except Exception:
                duration_ms = (time.time() - start_time) * 1000
                _record_tool_call(tool_name, duration_ms, False)
                raise

        return wrapper

    return decorator


@mcp.tool()
def index_repo(repo_path: str, dialect: str = "ansi") -> dict:
    """Index a repository of SQL files.

    Parses SQL files, extracts table and column definitions, and builds
    lineage edges. Results are persisted to the graph database. Only
    git-tracked files are indexed when the directory is a git repo —
    untracked files, build artifacts, and node_modules are ignored
    automatically. Falls back to a full directory scan when git is
    unavailable.

    Binary is `sqlcg`; PyPI package is `sql-code-graph`.

    Args:
        repo_path: Root directory path to index
        dialect: SQL dialect (ansi, snowflake, bigquery, postgres, tsql)

    Returns:
        Dict with keys: files_parsed, parse_errors, tables_found, lineage_edges_created
    """
    global _metrics
    start_time = time.time()
    success = True

    try:
        with _open_backend() as db:
            indexer = Indexer()
            path = Path(repo_path).resolve()
            if not path.exists():
                raise ValueError(f"Repository path does not exist: {repo_path}")
            if not path.is_dir():
                raise ValueError(f"Repository path is not a directory: {repo_path}")

            # Ensure the Repo node exists for this repository
            from sqlcg.core.schema import NodeLabel, RelType

            abs_path = str(path)
            db.upsert_node(
                NodeLabel.REPO,
                abs_path,
                {
                    "path": abs_path,
                    "name": path.name,
                },
            )

            # Index the repository (with absolute path)
            result = indexer.index_repo(path, dialect, db)

            # Create BELONGS_TO relationships from File nodes to Repo node
            repo_prefix = abs_path.rstrip("/") + "/"
            file_rows = db.run_read(INDEX_REPO_FILES_QUERY, {"repo_prefix": repo_prefix})
            for row in file_rows:
                db.upsert_edge(
                    NodeLabel.FILE,
                    row["path"],
                    NodeLabel.REPO,
                    abs_path,
                    RelType.BELONGS_TO,
                    {},
                )

            logger.info(
                f"Indexed {result['files_parsed']} files with {result['tables_found']} tables"
            )

            duration_ms = (time.time() - start_time) * 1000
            if _metrics is not None:
                try:
                    _metrics.record_index_run(
                        abs_path,
                        result.get("files_parsed", 0),
                        result.get("parse_errors", 0),
                        result.get("tables_found", 0),
                        result.get("lineage_edges_created", 0),
                        duration_ms,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to record index run metrics: {exc}")

            return result
    except Exception:
        success = False
        duration_ms = (time.time() - start_time) * 1000
        _record_tool_call("index_repo", duration_ms, success)
        raise


@mcp.tool()
@_timed_tool("trace_column_lineage")
def trace_column_lineage(table_col: str, max_depth: int = 5) -> LineageResult:
    """Trace upstream lineage of a column.

    Traverses COLUMN_LINEAGE edges backward up to max_depth levels.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column"
        max_depth: Maximum number of hops to traverse

    Returns:
        LineageResult with list of upstream column nodes

    Raises:
        NotIndexedError: If no repos have been indexed
        InvalidColumnRefError: If column reference format is invalid
    """
    with _open_backend() as db:
        _assert_indexed(db)

        try:
            table_id, col_name = _parse_column_ref(table_col)
        except InvalidColumnRefError:
            raise

        col_id = f"{table_id}.{col_name}"

        lineage: list[LineageNode] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or depth > max_depth:
                continue

            visited.add(current_id)

            rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current_id})

            for row in rows:
                node_id = row["id"]
                if node_id not in visited:
                    lineage.append(
                        LineageNode(
                            name=row.get("col_name", ""),
                            kind="column",
                            file=None,
                            confidence=None,
                        )
                    )
                    queue.append((node_id, depth + 1))

        hint = None
        if not lineage:
            hint = (
                "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0. "
                "If SqlColumn is 0, column lineage was not extracted — check parse errors. "
                "Submit feedback with submit_feedback tool if this was a false negative."
            )

        return LineageResult(column=table_col, lineage=lineage, hint=hint)


@mcp.tool()
@_timed_tool("find_table_usages")
def find_table_usages(table_name: str) -> TableUsageResult:
    """Find all queries that use a given table.

    Searches for SELECTS_FROM relationships pointing to the table.

    Args:
        table_name: Table name to search for

    Returns:
        TableUsageResult with list of queries using this table

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)

        rows = db.run_read(FIND_TABLE_USAGES_QUERY, {"name": table_name})

        usages: list[TableUsage] = []
        for row in rows:
            usages.append(
                TableUsage(
                    query_file=row["file"],
                    sql=row.get("sql"),
                    kind=row.get("kind"),
                )
            )

        hint = None
        if not usages:
            hint = (
                "No usages found for this table. The table may not be referenced by any "
                "indexed SQL file, or it may be consumed externally (BI tools, APIs). "
                "Run 'analyze impact <table>' from the CLI to cross-check."
            )

        return TableUsageResult(table=table_name, usages=usages, hint=hint)


@mcp.tool()
@_timed_tool("get_downstream_dependencies")
def get_downstream_dependencies(table_col: str, max_depth: int = 5) -> DependencyResult:
    """Find all downstream dependencies of a column.

    Traverses COLUMN_LINEAGE edges forward to find columns that depend on this one.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column"
        max_depth: Maximum number of hops to traverse

    Returns:
        DependencyResult with list of downstream column nodes

    Raises:
        NotIndexedError: If no repos have been indexed
        InvalidColumnRefError: If column reference format is invalid
    """
    with _open_backend() as db:
        _assert_indexed(db)

        try:
            table_id, col_name = _parse_column_ref(table_col)
        except InvalidColumnRefError:
            raise

        col_id = f"{table_id}.{col_name}"

        nodes: list[DependencyNode] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or depth > max_depth:
                continue

            visited.add(current_id)

            rows = db.run_read(GET_DOWNSTREAM_DEPENDENCIES_QUERY, {"id": current_id})

            for row in rows:
                node_id = row["id"]
                if node_id not in visited:
                    nodes.append(DependencyNode(name=row.get("col_name", ""), kind="column"))
                    queue.append((node_id, depth + 1))

        hint = None
        if not nodes:
            hint = (
                "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0. "
                "If SqlColumn is 0, column lineage was not extracted — check parse errors. "
                "Submit feedback with submit_feedback tool if this was a false negative."
            )

        return DependencyResult(root=table_col, nodes=nodes, hint=hint)


@mcp.tool()
@_timed_tool("get_upstream_dependencies")
def get_upstream_dependencies(table_col: str, max_depth: int = 5) -> DependencyResult:
    """Find all upstream dependencies of a column.

    Traverses COLUMN_LINEAGE edges backward to find columns this one depends on.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column"
        max_depth: Maximum number of hops to traverse

    Returns:
        DependencyResult with list of upstream column nodes

    Raises:
        NotIndexedError: If no repos have been indexed
        InvalidColumnRefError: If column reference format is invalid
    """
    with _open_backend() as db:
        _assert_indexed(db)

        try:
            table_id, col_name = _parse_column_ref(table_col)
        except InvalidColumnRefError:
            raise

        col_id = f"{table_id}.{col_name}"

        nodes: list[DependencyNode] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or depth > max_depth:
                continue

            visited.add(current_id)

            rows = db.run_read(GET_UPSTREAM_DEPENDENCIES_QUERY, {"id": current_id})

            for row in rows:
                node_id = row["id"]
                if node_id not in visited:
                    nodes.append(DependencyNode(name=row.get("col_name", ""), kind="column"))
                    queue.append((node_id, depth + 1))

        hint = None
        if not nodes:
            hint = (
                "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0. "
                "If SqlColumn is 0, column lineage was not extracted — check parse errors. "
                "Submit feedback with submit_feedback tool if this was a false negative."
            )

        return DependencyResult(root=table_col, nodes=nodes, hint=hint)


@mcp.tool()
@_timed_tool("search_sql_pattern")
def search_sql_pattern(query: str, limit: int = 20) -> SqlPatternResult:
    """Search for SQL patterns in indexed queries.

    Uses substring matching on the query SQL text.

    Args:
        query: Pattern string to search for
        limit: Maximum number of results (default 20)

    Returns:
        SqlPatternResult with list of matching queries

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)

        rows = db.run_read(SEARCH_SQL_PATTERN_QUERY, {"query": query, "limit": limit})

        matches: list[SqlPatternMatch] = []
        for row in rows:
            matches.append(
                SqlPatternMatch(file=row["file"], sql=row.get("sql", ""), kind=row.get("kind"))
            )

        hint = None
        if not matches:
            hint = (
                "No matches found. Try a shorter or partial pattern. "
                "Pattern matching is case-sensitive substring search."
            )

        return SqlPatternResult(pattern=query, matches=matches, hint=hint)


@mcp.tool()
@_timed_tool("list_dialects_and_repos")
def list_dialects_and_repos() -> DialectRepoResult:
    """List all indexed repositories and their SQL dialects.

    Binary is `sqlcg`; PyPI package is `sql-code-graph`.

    Returns the catalogue of what has been indexed. For health and parse quality
    information use `db_info()` instead.

    Returns:
        DialectRepoResult with list of repositories and their dialects

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)

        rows = db.run_read(LIST_DIALECTS_AND_REPOS_QUERY, {})

        repos: list[DialectRepo] = []
        for row in rows:
            repos.append(
                DialectRepo(
                    path=row["path"], name=row.get("name"), dialects=row.get("dialects", [])
                )
            )

        return DialectRepoResult(repos=repos)


@_timed_tool("db_info")
def db_info() -> DbInfoResult:
    """Return graph health and parse quality diagnostics.

    Use this tool to understand the current state of the indexed graph before
    running lineage queries. Key signals:

    - `node_counts["SqlColumn"] == 0` → column lineage was not extracted;
      trace_column_lineage and dependency tools will return empty results.
    - `parse_quality["scripting_block"]` high → Snowflake/BigQuery scripting
      blocks were parsed via tokenizer fallback; column lineage limited for
      those files. Table-level lineage is still available.
    - `warnings` list — empty means the graph is healthy.

    Parse quality legend (parsing_mode per SqlQuery node):
      sqlglot          — standard path; column lineage available if extracted
      scripting_block  — tokenizer fallback; column lineage unavailable

    Returns:
        DbInfoResult with schema version, node counts, parse quality, and warnings
    """
    with _open_backend() as db:
        schema_version = db.get_schema_version() or "unknown"

        node_counts: dict[str, int] = {}
        for label in NodeLabel:
            result = db.run_read(f"MATCH (n:{label}) RETURN COUNT(*) AS count", {})
            node_counts[str(label)] = result[0]["count"] if result else 0

        edges_result = db.run_read("MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count", {})
        column_lineage_edges = edges_result[0]["count"] if edges_result else 0

        mode_rows = db.run_read(
            "MATCH (q:SqlQuery) RETURN q.parsing_mode AS mode, COUNT(q) AS cnt ORDER BY cnt DESC",
            {},
        )
        parse_quality: dict[str, int] = {}
        if mode_rows and "mode" in mode_rows[0]:
            parse_quality = {str(r["mode"]): int(r["cnt"]) for r in mode_rows}

        warnings: list[str] = []
        if node_counts.get("Repo", 0) == 0:
            warnings.append("Database is empty. Run 'sqlcg db init' then 'sqlcg index <path>'.")
        elif node_counts.get("SqlQuery", 0) == 0:
            warnings.append("No queries indexed. Run 'sqlcg index <path>' to populate the graph.")
        elif node_counts.get("SqlColumn", 0) == 0:
            warnings.append(
                "SqlColumn count is 0 — column lineage was not extracted. "
                "trace_column_lineage and dependency tools will return empty results."
            )

        total_queries = sum(parse_quality.values())
        scripting_count = parse_quality.get("scripting_block", 0)
        if total_queries > 0 and scripting_count > 0:
            pct = round(100 * scripting_count / total_queries)
            if pct > 20:
                warnings.append(
                    f"{pct}% of queries used scripting-block fallback — "
                    "column lineage may be incomplete for those files."
                )

        return DbInfoResult(
            schema_version=schema_version,
            node_counts=node_counts,
            column_lineage_edges=column_lineage_edges,
            parse_quality=parse_quality,
            warnings=warnings,
        )


@mcp.tool()
@_timed_tool("execute_cypher")
def execute_cypher(query: str) -> list[dict]:
    """Execute a read-only Cypher query against the graph.

    This tool allows direct Cypher queries for advanced users. It enforces
    read-only mode by stripping quoted literals and checking for write
    operation keywords. A LIMIT clause is automatically appended if missing.

    **Important Security Note**: This tool strips single and double-quoted
    string literals before checking for write operations. String literals
    containing mutation keywords (e.g., 'DROP TABLE') will NOT trigger the
    write-operation blocker. This is by design to allow querying SQL text
    that contains such keywords.

    Args:
        query: Cypher query string (read-only)

    Returns:
        List of result dictionaries from the query

    Raises:
        ValueError: If the query contains write operations (CREATE, MERGE,
                   DELETE, SET, REMOVE, DROP, TRUNCATE)
    """
    stripped = re.sub(r"'(?:''|[^'])*'", "", query)
    stripped = re.sub(r'"(?:""|[^"])*"', "", stripped)

    if re.search(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|TRUNCATE)\b", stripped, re.IGNORECASE):
        raise ValueError(
            "Write operations are not permitted via execute_cypher. "
            "Use the CLI or dedicated tools instead."
        )

    q = query.rstrip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if "limit" not in stripped.lower():
        q = q + " LIMIT 500"

    with _open_backend() as db:
        try:
            return db.run_read(q, {})
        except Exception as e:
            logger.error(f"Cypher execution failed: {e}")
            raise


@mcp.tool()
def submit_feedback(
    tool_name: str,
    query: str,
    label: str,
    note: str = "",
) -> dict:
    """Submit feedback on a tool result.

    This tool allows users to correct the MCP server's results. Feedback
    is collected and analyzed to identify patterns and false positives.

    **For Claude**: When a user says "that result was wrong" or "this is a
    false positive", call this tool with label="FP". When they confirm
    "that's correct", call with label="TP". When a tool should have
    returned a result but got empty, call with label="FN" (false negative).
    Use the query or pattern as the 'query' argument and include any user
    feedback in the 'note'.

    Args:
        tool_name: Name of the tool being evaluated (e.g., "trace_column_lineage")
        query: The query or pattern that was evaluated
        label: Feedback label: "TP" (true positive), "FP" (false positive), or
               "FN" (false negative — expected a result but got empty)
        note: Optional user note (truncated to 500 chars)

    Returns:
        Dict with status: "recorded" or "skipped"

    Raises:
        ValueError: If label is not "TP", "FP", or "FN"
    """
    global _metrics

    if label not in ("TP", "FP", "FN"):
        raise ValueError(f"Invalid label: {label}. Must be 'TP', 'FP', or 'FN'.")

    if _metrics is not None:
        try:
            _metrics.record_feedback(tool_name, query, label, note)
            return {"status": "recorded"}
        except Exception as exc:
            logger.warning(f"Failed to record feedback: {exc}")
            return {"status": "skipped"}
    else:
        return {"status": "skipped"}
