"""MCP tools for SQL code graph queries and indexing."""

import re
import time
from collections import deque
from pathlib import Path

from sqlcg.core.config import get_db_path
from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.metrics.store import MetricsStore
from sqlcg.server.exceptions import InvalidColumnRefError, NotIndexedError
from sqlcg.server.models import (
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

# Module-level singleton backend (KùzuDB single-writer model)
_backend: GraphBackend | None = None

# Module-level metrics store singleton
_metrics: MetricsStore | None = None


def init_backend(db_path: str | None = None) -> None:
    """Initialize the module-level backend singleton.

    Args:
        db_path: Path to KùzuDB database. If None, uses get_db_path().

    Raises:
        RuntimeError: If backend initialization fails
    """
    global _backend, _metrics
    path = db_path or str(get_db_path())
    backend = KuzuBackend(path)
    try:
        backend.init_schema()
    except Exception as exc:
        backend.close()
        raise RuntimeError(f"Backend initialization failed: {exc}") from exc
    _backend = backend
    logger.debug(f"Backend initialized: {path}")

    # Initialize metrics store (best-effort, failures are logged as WARNING)
    try:
        metrics_path = Path.home() / ".sqlcg" / "metrics.db"
        _metrics = MetricsStore(metrics_path)
        _metrics.init_schema()
    except Exception as exc:
        logger.warning(f"Failed to initialize metrics store: {exc}")


def shutdown_backend() -> None:
    """Shutdown the module-level backend singleton.

    Closes the database connection and clears the global reference.
    Safe to call multiple times.
    """
    global _backend, _metrics
    if _backend is not None:
        _backend.close()
        _backend = None
        logger.debug("Backend shut down")
    if _metrics is not None:
        _metrics.close()
        _metrics = None


def _get_backend() -> GraphBackend:
    """Get the initialized backend.

    Raises:
        RuntimeError: If backend not initialized via init_backend().
    """
    if _backend is None:
        raise RuntimeError("Backend not initialized. Call init_backend() before using tools.")
    return _backend


def _assert_indexed(db: GraphBackend) -> None:
    """Check that the graph has indexed repos.

    Args:
        db: GraphBackend instance

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    rows = db.run_read("MATCH (r:Repo) RETURN count(r) AS n", {})
    if not rows or rows[0]["n"] == 0:
        raise NotIndexedError("No repos have been indexed. Run `sqlcg index <path>` first.")


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
    lineage edges. Results are persisted to the graph database.

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
        db = _get_backend()
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
        # Query for all File nodes in this repo and link them to the Repo
        files_query = """
        MATCH (f:File) WHERE f.path STARTS WITH $repo_prefix
        RETURN f.path AS path
        """
        repo_prefix = abs_path.rstrip("/") + "/"
        file_rows = db.run_read(files_query, {"repo_prefix": repo_prefix})
        for row in file_rows:
            db.upsert_edge(
                NodeLabel.FILE,
                row["path"],
                NodeLabel.REPO,
                abs_path,
                RelType.BELONGS_TO,
                {},
            )

        logger.info(f"Indexed {result['files_parsed']} files with {result['tables_found']} tables")

        # Record metrics
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


@_timed_tool("trace_column_lineage")
@mcp.tool()
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
    db = _get_backend()
    _assert_indexed(db)

    try:
        table_id, col_name = _parse_column_ref(table_col)
    except InvalidColumnRefError:
        raise

    # Construct the full column id
    col_id = f"{table_id}.{col_name}"

    lineage: list[LineageNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(col_id, 0)])

    while queue:
        current_id, depth = queue.popleft()

        if current_id in visited or depth > max_depth:
            continue

        visited.add(current_id)

        # Query for upstream columns (reverse direction)
        rows = db.run_read(
            """
            MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
            RETURN src.id AS id, src.col_name AS col_name
            """,
            {"id": current_id},
        )

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

    return LineageResult(column=table_col, lineage=lineage)


@_timed_tool("find_table_usages")
@mcp.tool()
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
    db = _get_backend()
    _assert_indexed(db)

    rows = db.run_read(
        """
        MATCH (t:SqlTable {name: $name})<-[:SELECTS_FROM]-(q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)
        RETURN f.path AS file, q.sql AS sql, q.kind AS kind
        """,
        {"name": table_name},
    )

    usages: list[TableUsage] = []
    for row in rows:
        usages.append(
            TableUsage(
                query_file=row["file"],
                sql=row.get("sql"),
                kind=row.get("kind"),
            )
        )

    return TableUsageResult(table=table_name, usages=usages)


@_timed_tool("get_downstream_dependencies")
@mcp.tool()
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
    db = _get_backend()
    _assert_indexed(db)

    try:
        table_id, col_name = _parse_column_ref(table_col)
    except InvalidColumnRefError:
        raise

    # Construct the full column id
    col_id = f"{table_id}.{col_name}"

    nodes: list[DependencyNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(col_id, 0)])

    while queue:
        current_id, depth = queue.popleft()

        if current_id in visited or depth > max_depth:
            continue

        visited.add(current_id)

        # Query for downstream columns (forward direction)
        rows = db.run_read(
            """
            MATCH (src:SqlColumn {id: $id})-[:COLUMN_LINEAGE]->(dst:SqlColumn)
            RETURN dst.id AS id, dst.col_name AS col_name
            """,
            {"id": current_id},
        )

        for row in rows:
            node_id = row["id"]
            if node_id not in visited:
                nodes.append(
                    DependencyNode(
                        name=row.get("col_name", ""),
                        kind="column",
                    )
                )
                queue.append((node_id, depth + 1))

    return DependencyResult(root=table_col, nodes=nodes)


@_timed_tool("get_upstream_dependencies")
@mcp.tool()
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
    db = _get_backend()
    _assert_indexed(db)

    try:
        table_id, col_name = _parse_column_ref(table_col)
    except InvalidColumnRefError:
        raise

    # Construct the full column id
    col_id = f"{table_id}.{col_name}"

    nodes: list[DependencyNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(col_id, 0)])

    while queue:
        current_id, depth = queue.popleft()

        if current_id in visited or depth > max_depth:
            continue

        visited.add(current_id)

        # Query for upstream columns (reverse direction)
        rows = db.run_read(
            """
            MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
            RETURN src.id AS id, src.col_name AS col_name
            """,
            {"id": current_id},
        )

        for row in rows:
            node_id = row["id"]
            if node_id not in visited:
                nodes.append(
                    DependencyNode(
                        name=row.get("col_name", ""),
                        kind="column",
                    )
                )
                queue.append((node_id, depth + 1))

    return DependencyResult(root=table_col, nodes=nodes)


@_timed_tool("search_sql_pattern")
@mcp.tool()
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
    db = _get_backend()
    _assert_indexed(db)

    rows = db.run_read(
        """
        MATCH (q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)
        WHERE contains(q.sql, $query)
        RETURN f.path AS file, q.sql AS sql, q.kind AS kind
        LIMIT $limit
        """,
        {"query": query, "limit": limit},
    )

    matches: list[SqlPatternMatch] = []
    for row in rows:
        matches.append(
            SqlPatternMatch(
                file=row["file"],
                sql=row.get("sql", ""),
                kind=row.get("kind"),
            )
        )

    return SqlPatternResult(pattern=query, matches=matches)


@_timed_tool("list_dialects_and_repos")
@mcp.tool()
def list_dialects_and_repos() -> DialectRepoResult:
    """List all indexed repositories and their SQL dialects.

    Returns:
        DialectRepoResult with list of repositories and their dialects

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    db = _get_backend()
    _assert_indexed(db)

    rows = db.run_read(
        """
        MATCH (r:Repo)<-[:BELONGS_TO]-(f:File)
        RETURN r.path AS path, r.name AS name, collect(DISTINCT f.dialect) AS dialects
        """,
        {},
    )

    repos: list[DialectRepo] = []
    for row in rows:
        repos.append(
            DialectRepo(
                path=row["path"],
                name=row.get("name"),
                dialects=row.get("dialects", []),
            )
        )

    return DialectRepoResult(repos=repos)


@_timed_tool("execute_cypher")
@mcp.tool()
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
    db = _get_backend()

    # Strip quoted string literals before blocklist check
    # This prevents mutation commands hiding inside strings from triggering the blocker
    # Handle escaped quotes: '' in single quotes, "" in double quotes
    stripped = re.sub(r"'(?:''|[^'])*'", "", query)
    stripped = re.sub(r'"(?:""|[^"])*"', "", stripped)

    # Check for write operations (case-insensitive)
    if re.search(
        r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|TRUNCATE)\b",
        stripped,
        re.IGNORECASE,
    ):
        raise ValueError(
            "Write operations are not permitted via execute_cypher. "
            "Use the CLI or dedicated tools instead."
        )

    # Auto-append LIMIT if missing
    q = query.rstrip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if "limit" not in stripped.lower():  # use stripped, not q.lower()
        q = q + " LIMIT 500"

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
    "that's correct", call with label="TP". Use the query or pattern as
    the 'query' argument and include any user feedback in the 'note'.

    Args:
        tool_name: Name of the tool being evaluated (e.g., "trace_column_lineage")
        query: The query or pattern that was evaluated
        label: Feedback label: "TP" (true positive) or "FP" (false positive)
        note: Optional user note (truncated to 500 chars)

    Returns:
        Dict with status: "recorded" or "skipped"

    Raises:
        ValueError: If label is not "TP" or "FP"
    """
    global _metrics

    if label not in ("TP", "FP"):
        raise ValueError(f"Invalid label: {label}. Must be 'TP' or 'FP'.")

    if _metrics is not None:
        try:
            _metrics.record_feedback(tool_name, query, label, note)
            return {"status": "recorded"}
        except Exception as exc:
            logger.warning(f"Failed to record feedback: {exc}")
            return {"status": "skipped"}
    else:
        return {"status": "skipped"}
