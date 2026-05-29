"""MCP tools for SQL code graph queries and indexing."""

import functools
import re
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from sqlcg.core.config import get_db_path, get_presentation_prefixes
from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.queries import (
    FIND_DEFINITION_QUERY,
    FIND_TABLE_USAGES_QUERY,
    GET_COLUMNS_FOR_TABLE_QUERY,
    GET_DOWNSTREAM_DEPENDENCIES_QUERY,
    GET_TABLE_DEFINING_FILES_QUERY,
    GET_TABLE_DIRECT_UPSTREAMS_QUERY,
    GET_TABLES_DEFINED_IN_FILE_QUERY,
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
    BackfillOrderResult,
    ChangeScopeResult,
    DbInfoResult,
    DefinitionFile,
    DefinitionResult,
    DependencyNode,
    DependencyResult,
    DialectRepo,
    DialectRepoResult,
    DiffImpactResult,
    LineageNode,
    LineageResult,
    SqlPatternMatch,
    SqlPatternResult,
    TableUsage,
    TableUsageResult,
)
from sqlcg.server.noise_filter import NoiseFilter


def _build_mermaid(root_id: str, edges: list[tuple[str, str, str]]) -> str:
    """Build a Mermaid LR flowchart from a list of (src_id, dst_id, transform) edges."""

    def _nid(col_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", col_id)

    def _label(col_id: str) -> str:
        parts = col_id.rsplit(".", 1)
        if len(parts) == 2:
            return f"{parts[0]}\n{parts[1]}"
        return col_id

    lines = ["flowchart LR"]
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    for src, dst, transform in edges:
        for col_id in (src, dst):
            nid = _nid(col_id)
            if nid not in seen_nodes:
                seen_nodes.add(nid)
                lines.append(f'    {nid}["{_label(col_id)}"]')
        edge_key = (_nid(src), _nid(dst))
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            label = f"|{transform}|" if transform else ""
            lines.append(f"    {_nid(src)} -->{label} {_nid(dst)}")
    return "\n".join(lines)


from sqlcg.server.server import mcp  # noqa: E402, F401
from sqlcg.utils.logging import getLogger  # noqa: E402

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


@contextmanager
def _open_backend():
    """Context manager to get the initialized backend.

    Used for testing and context-manager-style access patterns.
    """
    backend = _get_backend()
    try:
        yield backend
    finally:
        pass


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
        col_ref: Column reference string (e.g., "ba.table.column")

    Returns:
        Tuple of (table_id, column_name)

    Raises:
        InvalidColumnRefError: If format is invalid
    """
    # Step 1.4b: Lowercase the input to match lowercased graph keys (C2)
    col_ref = col_ref.lower()

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


# Shared safety cap for all BFS closures (matches the per-tool guard in PR-01).
_MAX_CLOSURE_NODES = 50000


def _affected_columns_closure(
    db: GraphBackend, start_col_ids: list[str], max_depth: int | None = None
) -> tuple[list[str], int, bool]:
    """BFS downstream over COLUMN_LINEAGE from a set of starting columns.

    Returns (affected_col_ids, depth_reached, truncated). The starting columns
    themselves are NOT included in affected_col_ids — only their transitive
    downstream consumers. Honours the same 50k-node safety cap as the per-column
    dependency tools (PR-01).
    """
    visited: set[str] = set()
    emitted: set[str] = set()
    affected: list[str] = []
    queue: deque[tuple[str, int]] = deque((cid, 0) for cid in start_col_ids)
    depth_reached = 0
    truncated = False

    while queue:
        current_id, depth = queue.popleft()

        if current_id in visited or (max_depth is not None and depth > max_depth):
            continue

        if len(visited) >= _MAX_CLOSURE_NODES:
            truncated = True
            break

        visited.add(current_id)
        depth_reached = max(depth_reached, depth)

        rows = db.run_read(GET_DOWNSTREAM_DEPENDENCIES_QUERY, {"id": current_id})
        for row in rows:
            node_id = row["id"]
            next_depth = depth + 1
            if node_id not in visited and (max_depth is None or next_depth <= max_depth):
                if node_id not in emitted:
                    emitted.add(node_id)
                    affected.append(node_id)
                queue.append((node_id, next_depth))
            elif node_id not in visited and max_depth is not None and next_depth > max_depth:
                truncated = True

    return affected, depth_reached, truncated


def _rollup_to_tables(col_ids: list[str]) -> list[str]:
    """Roll up a list of column ids (table_qualified.col_name) to a deduplicated,
    order-preserving list of table_qualified strings."""
    tables: list[str] = []
    seen: set[str] = set()
    for cid in col_ids:
        table_qualified = cid.rsplit(".", 1)[0]
        if table_qualified not in seen:
            seen.add(table_qualified)
            tables.append(table_qualified)
    return tables


def _dedup_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate a list while preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _risk_label(affected_table_count: int) -> str:
    """Map a downstream affected-table count to a coarse risk label.

    0 -> "safe", 1-5 -> "low", 6-20 -> "medium", >20 -> "high".
    """
    if affected_table_count == 0:
        return "safe"
    if affected_table_count <= 5:
        return "low"
    if affected_table_count <= 20:
        return "medium"
    return "high"


def _kahn_topological_sort(affected_tables: list[str], db: GraphBackend) -> tuple[list[str], bool]:
    """Order affected tables so producers come before consumers (rebuild order).

    Builds an adjacency list from SELECTS_FROM edges *within* the affected set
    (a table's producing query selecting from another affected table is an edge
    producer -> consumer) and runs Kahn's algorithm. Returns
    (ordered_tables, had_cycle). On a cycle the acyclic prefix is emitted in
    topological order and the remaining cyclic tables are appended in input
    order — no exception is raised.
    """
    table_set = set(affected_tables)
    successors: dict[str, set[str]] = {t: set() for t in affected_tables}
    indegree: dict[str, int] = {t: 0 for t in affected_tables}

    for table in affected_tables:
        rows = db.run_read(GET_TABLE_DIRECT_UPSTREAMS_QUERY, {"table_qualified": table})
        for row in rows:
            src = row["upstream_table"]
            if src in table_set and src != table and table not in successors[src]:
                successors[src].add(table)
                indegree[table] += 1

    # Sort the zero-indegree frontier for deterministic output.
    ready: deque[str] = deque(sorted(t for t in affected_tables if indegree[t] == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for consumer in sorted(successors[node]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)

    if len(order) != len(affected_tables):
        remaining = [t for t in affected_tables if t not in set(order)]
        logger.warning(
            "Cycle detected among %d affected tables; appending in input order: %s",
            len(remaining),
            remaining,
        )
        order.extend(remaining)
        return order, True

    return order, False


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
        @functools.wraps(func)
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


@mcp.tool()
@_timed_tool("trace_column_lineage")
def trace_column_lineage(table_col: str, max_depth: int | None = None) -> LineageResult:
    """Trace upstream lineage of a column.

    Traverses COLUMN_LINEAGE edges backward. When max_depth is None, computes
    the full transitive closure. When max_depth is an integer, stops at that depth.

    Require schema-qualified input (e.g., ba.table_name.column_name) for accurate
    lineage lookups. Column references must include both the table and column names.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column" (e.g., "ba.table_name.column_name")
        max_depth: Maximum number of hops to traverse. If None, computes full closure.
                   Safety cap: stops at 50,000 nodes to prevent pathological graphs from
                   hanging the server.

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

        # Construct the full column id
        col_id = f"{table_id}.{col_name}"

        lineage: list[LineageNode] = []
        edges: list[tuple[str, str, str]] = []
        visited: set[str] = set()
        emitted: set[str] = set()  # Step 3.1: track emitted nodes to prevent duplicates
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])
        max_nodes = 50000

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or (max_depth is not None and depth > max_depth):
                continue

            # Safety cap: stop if we've visited too many nodes
            if len(visited) >= max_nodes:
                break

            visited.add(current_id)

            # Query for upstream columns (reverse direction)
            rows = db.run_read(
                TRACE_COLUMN_LINEAGE_QUERY,
                {"id": current_id},
            )

            for row in rows:
                node_id = row["id"]
                edges.append((node_id, current_id, row.get("transform") or "SELECT"))
                if node_id not in visited:
                    # Step 3.1: only emit each node_id once
                    if node_id not in emitted:
                        emitted.add(node_id)
                        lineage.append(
                            LineageNode(
                                name=row.get("col_name", ""),
                                kind="column",
                                file=None,
                                confidence=row.get("confidence"),
                            )
                        )
                    queue.append((node_id, depth + 1))

        mermaid = _build_mermaid(col_id, edges) if edges else None

        # Populate hint if result is empty (Step 4.1)
        hint = None
        if not lineage:
            hint = (
                "No lineage found. Ensure the column reference includes the schema prefix "
                "(e.g., ba.table_name.column_name). Check that 'sqlcg db info' shows "
                "SqlColumn > 0. If SqlColumn is 0, column lineage was not extracted — "
                "check parse errors. Submit feedback with submit_feedback tool if this "
                "was a false negative."
            )

        return LineageResult(column=table_col, lineage=lineage, mermaid=mermaid, hint=hint)


@mcp.tool()
@_timed_tool("find_table_usages")
def find_table_usages(table_name: str) -> TableUsageResult:
    """Find all queries that use a given table.

    Searches for SELECTS_FROM relationships pointing to the table.

    Args:
        table_name: Table name to search for (e.g. "ba.table_name")

    Returns:
        TableUsageResult with list of queries using this table

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)

        # Step 1.4: Lowercase the input to match lowercased SqlTable.name (C2)
        rows = db.run_read(
            FIND_TABLE_USAGES_QUERY,
            {"name": table_name.lower()},
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

        # Populate hint if result is empty
        hint = None
        if not usages:
            hint = (
                "No usages found for this table. The table may not be referenced by any "
                "indexed SQL file, or it may be consumed externally (BI tools, APIs). "
                "Run 'analyze impact <table>' from the CLI to cross-check."
            )

        return TableUsageResult(table=table_name, usages=usages, hint=hint)


@mcp.tool()
@_timed_tool("find_definition")
def find_definition(table_qualified: str) -> DefinitionResult:
    """Find where a table is authoritatively defined.

    Returns every file that defines the table via a DEFINED_IN edge. Backup
    snapshots (e.g. ``*_bck``) are still returned but flagged ``is_backup=True``
    and listed in ``noise_excluded`` so the LLM can see them without being misled
    into treating a backup as the source of truth.

    Args:
        table_qualified: Qualified table name (e.g. "ba.wtfe_verkoopinfo")

    Returns:
        DefinitionResult listing definition files, duplicate-DDL flag, and noise.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)

        rows = db.run_read(FIND_DEFINITION_QUERY, {"table_qualified": table_qualified.lower()})
        noise_filter = NoiseFilter.from_config()

        duplicate_ddl = len(rows) > 1
        definitions: list[DefinitionFile] = []
        noise_excluded: list[str] = []
        for row in rows:
            is_backup = noise_filter.is_noise(row["table_qualified"])
            definitions.append(
                DefinitionFile(
                    file_path=row["file_path"],
                    kind=row.get("kind"),
                    is_backup=is_backup,
                    is_authoritative=(not is_backup and not duplicate_ddl),
                )
            )
            if is_backup:
                noise_excluded.append(row["file_path"])

        hint = None
        if not definitions:
            hint = (
                "No definition found. The table may not be indexed, or it is created "
                "dynamically (e.g. CREATE TABLE AS in a scripting block that did not parse). "
                "Confirm the qualified name (schema.table) and that the defining DDL file "
                "was indexed."
            )

        return DefinitionResult(
            table_qualified=table_qualified,
            definitions=definitions,
            duplicate_ddl=duplicate_ddl,
            noise_excluded=noise_excluded,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("get_change_scope")
def get_change_scope(table_qualified: str) -> ChangeScopeResult:
    """Return the minimal reading set and risk for changing a table.

    Bundles, in one call: the defining file(s), direct upstream input tables,
    the full-depth downstream blast radius (column-precise, rolled up to tables),
    and a coarse risk label. Backup/noise tables are excluded from the blast
    radius and reported in ``noise_excluded``.

    Args:
        table_qualified: Qualified table name (e.g. "ba.wtfe_verkoopinfo")

    Returns:
        ChangeScopeResult with defining files, upstreams, affected columns/tables,
        and risk label.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    target = table_qualified.lower()
    with _open_backend() as db:
        _assert_indexed(db)
        noise_filter = NoiseFilter.from_config()

        def_rows = db.run_read(GET_TABLE_DEFINING_FILES_QUERY, {"table_qualified": target})
        defining_files = _dedup_preserve_order([r["file_path"] for r in def_rows])

        up_rows = db.run_read(GET_TABLE_DIRECT_UPSTREAMS_QUERY, {"table_qualified": target})
        upstream_raw = _dedup_preserve_order(
            [r["upstream_table"] for r in up_rows if r["upstream_table"]]
        )
        upstream_tables, _ = noise_filter.filter_nodes(upstream_raw)

        col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": target})
        start_cols = [r["col_id"] for r in col_rows]
        affected_cols, _depth, truncated = _affected_columns_closure(db, start_cols, max_depth=None)

        affected_tables_all = [t for t in _rollup_to_tables(affected_cols) if t != target]
        affected_tables, noise_excluded = noise_filter.filter_nodes(affected_tables_all)
        kept_tables = set(affected_tables)
        affected_columns = [c for c in affected_cols if c.rsplit(".", 1)[0] in kept_tables]

        risk_label = _risk_label(len(affected_tables))

        hint = None
        if not defining_files and not affected_tables and not upstream_tables:
            hint = (
                "No scope found for this table. Confirm the qualified name (schema.table) "
                "and that the defining/consuming SQL files were indexed."
            )

        return ChangeScopeResult(
            target=table_qualified,
            defining_files=defining_files,
            upstream_tables=upstream_tables,
            affected_columns=affected_columns,
            affected_tables=affected_tables,
            risk_label=risk_label,
            risk_weight=len(affected_tables),
            noise_excluded=noise_excluded,
            truncated=truncated,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("get_backfill_order")
def get_backfill_order(table_qualified: str) -> BackfillOrderResult:
    """Return the topological rebuild order for a table's downstream blast radius.

    Computes the full-depth downstream affected table set (noise-filtered) and
    orders it so that producers are rebuilt before consumers. If a dependency
    cycle is present the order degrades gracefully (acyclic prefix first, cyclic
    remainder appended) and ``hint`` reports the cycle.

    Args:
        table_qualified: Qualified table name (e.g. "ba.wtfe_verkoopinfo")

    Returns:
        BackfillOrderResult with the rebuild order and column-precise blast radius.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    target = table_qualified.lower()
    with _open_backend() as db:
        _assert_indexed(db)
        noise_filter = NoiseFilter.from_config()

        col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": target})
        start_cols = [r["col_id"] for r in col_rows]
        affected_cols, _depth, truncated = _affected_columns_closure(db, start_cols, max_depth=None)

        affected_tables_all = [t for t in _rollup_to_tables(affected_cols) if t != target]
        affected_tables, noise_excluded = noise_filter.filter_nodes(affected_tables_all)
        kept_tables = set(affected_tables)
        affected_columns = [c for c in affected_cols if c.rsplit(".", 1)[0] in kept_tables]

        order, had_cycle = _kahn_topological_sort(affected_tables, db)

        hint = None
        if had_cycle:
            hint = (
                "Dependency cycle detected among affected tables; backfill order is "
                "approximate (cyclic tables appended in input order)."
            )
        elif not order:
            hint = "No downstream consumers — nothing to backfill."

        return BackfillOrderResult(
            target=table_qualified,
            backfill_order=order,
            affected_columns=affected_columns,
            noise_excluded=noise_excluded,
            truncated=truncated,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("diff_impact")
def diff_impact(changed_files: list[str]) -> DiffImpactResult:
    """Return the union downstream blast radius for a set of changed files.

    The CI / agent integration point: given the file paths that changed, resolve
    the tables defined in them, compute each table's full-depth downstream blast
    radius, union and noise-filter the result, flag presentation-facing tables
    (config-driven prefixes), and return a topological rebuild order.

    Args:
        changed_files: SQL file paths that changed (must match indexed File.path)

    Returns:
        DiffImpactResult with changed tables, union blast radius, presentation
        subset, and rebuild order.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)
        noise_filter = NoiseFilter.from_config()
        prefixes = get_presentation_prefixes(Path.cwd())

        changed_tables: list[str] = []
        seen_changed: set[str] = set()
        for file_path in changed_files:
            rows = db.run_read(GET_TABLES_DEFINED_IN_FILE_QUERY, {"file_path": file_path})
            for row in rows:
                tq = row["table_qualified"]
                if tq not in seen_changed:
                    seen_changed.add(tq)
                    changed_tables.append(tq)

        all_affected_cols: list[str] = []
        affected_seen: set[str] = set()
        truncated = False
        for tq in changed_tables:
            col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": tq})
            start_cols = [r["col_id"] for r in col_rows]
            cols, _depth, tr = _affected_columns_closure(db, start_cols, max_depth=None)
            truncated = truncated or tr
            for col in cols:
                if col not in affected_seen:
                    affected_seen.add(col)
                    all_affected_cols.append(col)

        affected_tables_all = [
            t for t in _rollup_to_tables(all_affected_cols) if t not in seen_changed
        ]
        affected_tables, noise_excluded = noise_filter.filter_nodes(affected_tables_all)
        presentation_facing = [t for t in affected_tables if any(t.startswith(p) for p in prefixes)]
        order, had_cycle = _kahn_topological_sort(affected_tables, db)

        hint = None
        if not changed_tables:
            hint = (
                "No tables are defined in the given files. Confirm the paths match indexed "
                "File.path values (absolute paths as stored by the indexer)."
            )
        elif had_cycle:
            hint = (
                "Dependency cycle detected among affected tables; backfill order is "
                "approximate (cyclic tables appended in input order)."
            )

        return DiffImpactResult(
            changed_files=changed_files,
            changed_tables=changed_tables,
            affected_tables=affected_tables,
            presentation_facing=presentation_facing,
            backfill_order=order,
            noise_excluded=noise_excluded,
            truncated=truncated,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("get_downstream_dependencies")
def get_downstream_dependencies(table_col: str, max_depth: int | None = None) -> DependencyResult:
    """Find all downstream dependencies of a column.

    Traverses COLUMN_LINEAGE edges forward to find columns that depend on this one.
    When max_depth is None, computes the full transitive closure. When max_depth is
    an integer, stops at that depth.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column" (e.g., "ba.table.column")
        max_depth: Maximum number of hops to traverse. If None, computes full closure.
                   Safety cap: stops at 50,000 nodes to prevent pathological graphs from
                   hanging the server.

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

        # Construct the full column id
        col_id = f"{table_id}.{col_name}"

        nodes: list[DependencyNode] = []
        visited: set[str] = set()
        emitted: set[str] = set()  # Step 3.1: track emitted nodes to prevent duplicates
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])
        max_nodes = 50000
        depth_reached = 0
        truncated = False

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or (max_depth is not None and depth > max_depth):
                continue

            # Safety cap: stop if we've visited too many nodes
            if len(visited) >= max_nodes:
                truncated = True
                break

            visited.add(current_id)
            depth_reached = max(depth_reached, depth)

            # Query for downstream columns (forward direction)
            rows = db.run_read(
                GET_DOWNSTREAM_DEPENDENCIES_QUERY,
                {"id": current_id},
            )

            for row in rows:
                node_id = row["id"]
                next_depth = depth + 1
                if node_id not in visited and (max_depth is None or next_depth <= max_depth):
                    # Step 3.1: only emit each node_id once
                    if node_id not in emitted:
                        emitted.add(node_id)
                        nodes.append(
                            DependencyNode(
                                name=row.get("col_name", ""),
                                kind="column",
                            )
                        )
                    queue.append((node_id, next_depth))
                elif node_id not in visited and max_depth is not None and next_depth > max_depth:
                    truncated = True

        # Populate hint if result is empty (Step 4.2)
        hint = None
        if not nodes:
            hint = (
                "No downstream consumers found — this column may be a terminal output. "
                "If you expected consumers, confirm the consuming files were indexed "
                "and that the column reference includes the schema prefix "
                "(e.g., ba.table_name.column_name)."
            )

        if truncated and max_depth is None:
            hint = "Traversal stopped at 50,000-node safety cap (exceeded max closure depth)."

        return DependencyResult(
            root=table_col,
            nodes=nodes,
            truncated=truncated,
            depth_reached=depth_reached,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("get_upstream_dependencies")
def get_upstream_dependencies(table_col: str, max_depth: int | None = None) -> DependencyResult:
    """Find all upstream dependencies of a column.

    Traverses COLUMN_LINEAGE edges backward to find columns this one depends on.
    When max_depth is None, computes the full transitive closure. When max_depth is
    an integer, stops at that depth.

    Require schema-qualified input (e.g., ba.table_name.column_name) for accurate
    lookups.

    Args:
        table_col: Column reference in format "table.column"
                   or "catalog.db.table.column" (e.g., "ba.table_name.column_name")
        max_depth: Maximum number of hops to traverse. If None, computes full closure.
                   Safety cap: stops at 50,000 nodes to prevent pathological graphs from
                   hanging the server.

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

        # Construct the full column id
        col_id = f"{table_id}.{col_name}"

        nodes: list[DependencyNode] = []
        visited: set[str] = set()
        emitted: set[str] = set()  # Step 3.1: track emitted nodes to prevent duplicates
        queue: deque[tuple[str, int]] = deque([(col_id, 0)])
        max_nodes = 50000
        depth_reached = 0
        truncated = False

        while queue:
            current_id, depth = queue.popleft()

            if current_id in visited or (max_depth is not None and depth > max_depth):
                continue

            # Safety cap: stop if we've visited too many nodes
            if len(visited) >= max_nodes:
                truncated = True
                break

            visited.add(current_id)
            depth_reached = max(depth_reached, depth)

            # Query for upstream columns (reverse direction)
            rows = db.run_read(
                GET_UPSTREAM_DEPENDENCIES_QUERY,
                {"id": current_id},
            )

            for row in rows:
                node_id = row["id"]
                next_depth = depth + 1
                if node_id not in visited and (max_depth is None or next_depth <= max_depth):
                    # Step 3.1: only emit each node_id once
                    if node_id not in emitted:
                        emitted.add(node_id)
                        nodes.append(
                            DependencyNode(
                                name=row.get("col_name", ""),
                                kind="column",
                            )
                        )
                    queue.append((node_id, next_depth))
                elif node_id not in visited and max_depth is not None and next_depth > max_depth:
                    truncated = True

        # Populate hint if result is empty (Step 4.1)
        hint = None
        if not nodes:
            hint = (
                "No lineage found. Ensure the column reference includes the schema "
                "prefix (e.g., ba.table_name.column_name). Check that 'sqlcg db info' "
                "shows SqlColumn > 0. If SqlColumn is 0, column lineage was not "
                "extracted — check parse errors. Submit feedback with "
                "submit_feedback tool if this was a false negative."
            )

        if truncated and max_depth is None:
            hint = "Traversal stopped at 50,000-node safety cap (exceeded max closure depth)."

        return DependencyResult(
            root=table_col,
            nodes=nodes,
            truncated=truncated,
            depth_reached=depth_reached,
            hint=hint,
        )


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
    db = _get_backend()
    _assert_indexed(db)

    rows = db.run_read(
        SEARCH_SQL_PATTERN_QUERY,
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

    # Populate hint if result is empty
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
    db = _get_backend()
    _assert_indexed(db)

    rows = db.run_read(LIST_DIALECTS_AND_REPOS_QUERY, {})

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
    db = _get_backend()

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
