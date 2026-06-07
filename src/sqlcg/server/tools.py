"""MCP tools for SQL code graph queries and indexing."""

import functools
import re
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anyio

from sqlcg.core.config import get_db_path, get_presentation_prefixes
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.freshness import compute_freshness
from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.queries import (
    ANALYZE_UNUSED_TABLES_QUERY,
    FIND_DEFINITION_QUERY,
    FIND_TABLE_USAGES_QUERY,
    GET_COLUMNS_FOR_TABLE_QUERY,
    GET_DOWNSTREAM_DEPENDENCIES_QUERY,
    GET_TABLE_ADJACENCY_FOR_COLUMNS_QUERY,
    GET_TABLE_DEFINING_FILES_QUERY,
    GET_TABLE_DIRECT_UPSTREAMS_QUERY,
    GET_TABLE_KINDS_BATCH_QUERY,
    GET_TABLES_DEFINED_IN_FILE_QUERY,
    GET_TABLES_EXTERNAL_CONSUMERS_BATCH_QUERY,
    GET_TARGET_TABLES_FOR_FILE_QUERY,
    GET_UPSTREAM_DEPENDENCIES_QUERY,
    HUB_RANKING_QUERY,
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
    HubEntry,
    HubRankingResult,
    Judgement,
    LineageNode,
    LineageResult,
    PresentationCandidate,
    ScopeChangeResult,
    SqlPatternMatch,
    SqlPatternResult,
    TableUsage,
    TableUsageResult,
    UnusedCandidate,
    UnusedTablesResult,
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

# Module-level singleton backend (DuckDB single R/W handle for the process lifetime)
_backend: GraphBackend | None = None

# Module-level metrics store singleton
_metrics: MetricsStore | None = None

# Module-level backend lock — injected by server.py _run_with_control so that
# MCP write tools (index_repo) share the same lock as the drain loop.
# None when no server event-loop is running (unit tests, direct DB access).
_backend_lock: "anyio.Lock | None" = None

# The path that init_backend() actually opened.  Captured at init time so
# MCP write tools use this path, not get_db_path() which returns the default
# ~/.sqlcg/graph.db regardless of what was passed to init_backend.
_init_db_path: str | None = None


def _set_backend_lock(lock: "anyio.Lock | None") -> None:
    """Register the backend lock from the server's task group.

    Called by server.py _run_with_control so MCP write tools use the same
    lock as the drain loop — ensuring no concurrent RW access.
    """
    global _backend_lock
    _backend_lock = lock


def init_backend(db_path: str | None = None) -> None:
    """Initialize the module-level backend singleton.

    Startup sequence (OD-2 — measured on kuzu 0.11.3):
      1. Open read-write → create schema if absent (init_schema is a no-op on
         an already-initialized DB — it does NOT migrate).
      2. Run the schema-version gate (Step 1.4): refuse non-zero if the stored
         version differs from the current build's SCHEMA_VERSION.
      3. Close the RW backend.
      4. Reopen read-only and store as the serving singleton.

    This ensures ``init_schema()`` — which issues DDL — never runs on the RO
    connection (DDL raises on RO; ``Cannot create an empty database under READ
    ONLY mode.`` is raised on a non-existent DB opened RO).

    Args:
        db_path: Path to DuckDB database. If None, uses get_db_path().

    Raises:
        RuntimeError: If backend initialization fails or schema version
            is stale (the caller must not swallow this — server must exit).
    """
    global _backend, _metrics, _init_db_path
    path = db_path or str(get_db_path())
    _init_db_path = path

    # DuckDB: single R/W handle for the process lifetime — no RO/RW escalation.
    # init_schema is idempotent; transaction() wraps the DDL in one commit.
    rw_backend = DuckDBBackend(path)
    try:
        rw_backend.init_schema()
    except Exception as exc:
        rw_backend.close()
        raise RuntimeError(f"Backend initialization failed: {exc}") from exc

    # Step 2 — schema-version gate (Step 1.4).
    _assert_schema_current(rw_backend, path)

    # DuckDB: the same handle is used for reads and writes (MVCC).
    _backend = rw_backend
    logger.debug(f"Backend initialized (DuckDB R/W): {path}")

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
    global _backend, _metrics, _init_db_path
    if _backend is not None:
        _backend.close()
        _backend = None
        logger.debug("Backend shut down")
    if _metrics is not None:
        _metrics.close()
        _metrics = None
    _init_db_path = None


def _get_backend() -> GraphBackend:
    """Get the initialized backend.

    Raises:
        RuntimeError: If backend not initialized via init_backend().
    """
    if _backend is None:
        raise RuntimeError("Backend not initialized. Call init_backend() before using tools.")
    return _backend


def _assert_schema_current(backend: GraphBackend, path: str) -> None:
    """Refuse to start when the stored schema version differs from the current build.

    Called inside the RW-ensure window of init_backend (Step 1.4) after
    init_schema() has run the create-if-absent step.

    Args:
        backend: An open (RW) backend to query.
        path: The db_path string — included in the error message for context.

    Raises:
        RuntimeError: Stored version present and != current SCHEMA_VERSION.
            Message names both versions and the sqlcg db reset remedy.
    """
    from sqlcg.core.schema import SCHEMA_VERSION

    stored = backend.get_schema_version()
    if stored is not None and stored != SCHEMA_VERSION:
        msg = (
            f"Database schema is v{stored}, but this build expects v{SCHEMA_VERSION} — "
            f"run 'sqlcg db reset && sqlcg index <path>' to re-index."
        )
        raise RuntimeError(msg)


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
    """Check that the graph has indexed content (repos or files).

    Accepts a graph with Repo nodes (normal post-index_cmd state) OR with File
    nodes but no Repo (e.g. a graph populated directly via Indexer without the CLI
    wrapper). Returns without error when either is present.

    Args:
        db: GraphBackend instance

    Raises:
        NotIndexedError: If no repos or files have been indexed
    """
    rows = db.run_read('SELECT count(*) AS n FROM "Repo"', {})
    if rows and rows[0]["n"] > 0:
        return
    # Fallback: accept a graph with File nodes but no Repo (test-only or partial state).
    file_rows = db.run_read('SELECT count(*) AS n FROM "File"', {})
    if file_rows and file_rows[0]["n"] > 0:
        logger.debug(
            "File nodes present but no Repo node — accepting as test-only/partial graph; "
            "a production graph should always have a Repo node"
        )
        return
    raise NotIndexedError("No repos indexed. Run 'sqlcg db init' then 'sqlcg index <path>' first.")


def _indexed_root(db: GraphBackend) -> Path | None:
    """Return the indexed root path stored on the first Repo node, or None.

    Reads the persisted ``Repo.path`` primary key (set at index time by ``index_cmd``).
    Uses the same query pattern as ``db_info``'s freshness computation.  Returns ``None``
    when no Repo node exists or the path field is empty — callers fall back to
    ``Path.cwd()`` via ``_indexed_root(db) or Path.cwd()``.

    Multi-Repo: first Repo node wins (LIMIT 1), matching the existing freshness path.

    Args:
        db: GraphBackend instance

    Returns:
        Absolute Path of the indexed root, or None if unavailable.
    """
    try:
        rows = db.run_read('SELECT path FROM "Repo" LIMIT 1', {})
        if rows and rows[0].get("path"):
            return Path(rows[0]["path"])
    except Exception:
        pass
    return None


def _bare_ref(ref: str) -> str:
    """Strip schema prefix from a ref string, keeping table.column.

    For a 3-part ref ("mart.fact_t.amount") this returns "fact_t.amount".
    For a 2-part ref ("fact_t.amount") this returns the ref unchanged.
    Never uses rsplit — that would yield only the column name for 3-part refs.
    """
    parts = ref.split(".")
    if len(parts) >= 3:
        return ".".join(parts[1:])  # drop schema, keep table.column
    return ref  # already bare (no schema prefix)


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


# Synthetic SqlTable.kind values that must never surface as rebuildable tables —
# the authoritative marker per indexer.py (kind="cte" / kind="derived"); see
# _exclude_synthetic_tables. Keying on kind (not the alias string) avoids dropping
# a real table that happens to share a name with a CTE alias.
_SYNTHETIC_TABLE_KINDS = frozenset({"cte", "derived"})


def _exclude_synthetic_tables(db: GraphBackend, tables: list[str]) -> tuple[list[str], list[str]]:
    """Split *tables* into (real, synthetic) using the authoritative SqlTable.kind.

    Synthetic CTE/derived nodes (``kind in {"cte", "derived"}``) are emitted into
    the graph for lineage-tracing purposes but must never surface as rebuildable
    tables in get_change_scope / get_backfill_order / diff_impact / scope_change
    (issue #38 synthetic-node leak). Exclusion is keyed on ``kind`` — the
    authoritative marker set in indexer.py — never on the alias string, so a real
    table that happens to share a name with a CTE alias is never dropped.

    Args:
        db: Graph backend.
        tables: Candidate table_qualified strings (already noise-filtered).

    Returns:
        (real_tables, synthetic_excluded) — both order-preserving, deduplicated.
    """
    if not tables:
        return [], []

    rows = db.run_read(GET_TABLE_KINDS_BATCH_QUERY, {"table_qualifieds": tables})
    synthetic_ids = {
        row["table_qualified"] for row in rows if row["kind"] in _SYNTHETIC_TABLE_KINDS
    }

    if not synthetic_ids:
        return tables, []

    real: list[str] = []
    synthetic: list[str] = []
    for t in tables:
        if t in synthetic_ids:
            synthetic.append(t)
        else:
            real.append(t)
    return real, synthetic


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


def _kahn_topological_sort(
    affected_tables: list[str], db: GraphBackend, closure_col_ids: list[str]
) -> tuple[list[str], bool]:
    """Order affected tables so producers come before consumers (rebuild order).

    Builds adjacency from the **COLUMN_LINEAGE closure** (Option A — issue #38),
    not from ``SELECTS_FROM``: CTE-wrapped ``INSERT ... WITH cte AS (...) SELECT
    ... FROM cte`` statements emit no ``SELECTS_FROM`` adjacency at all (the real
    source table is nested in the CTE's child scope and never surfaces into the
    statement's top-level ``sources``), which previously left every table at
    indegree 0 and degraded ordering to alphabetical.

    ``COLUMN_LINEAGE`` *does* carry the producer -> consumer connectivity, but —
    contrary to an earlier (unreproduced) assumption of a parallel direct edge —
    it bridges producer and consumer via a **two-hop path through the synthetic
    cte/derived node** (``producer -> cte -> consumer``), not a parallel direct
    edge bypassing it. Rolling the raw closure up to table level therefore yields
    edges *to and from* synthetic nodes, not real producer -> consumer adjacency
    directly. This function **contracts** those synthetic-node hops in one pass
    over the (small) rolled-up edge set — ``real -> synthetic -> ... -> real``
    collapses to ``real -> real`` — yielding the true causal adjacency. Still
    **one** aggregate query + one contraction pass per closure (never
    per-table/per-column; replaces the old N x GET_TABLE_DIRECT_UPSTREAMS loop).

    Runs Kahn's algorithm over the contracted adjacency. Returns (ordered_tables,
    had_cycle). On a cycle the acyclic prefix is emitted in topological order
    and the remaining cyclic tables are appended in input order — no exception
    is raised.

    Args:
        affected_tables: Noise-filtered, synthetic-excluded table set to order.
        db: Graph backend.
        closure_col_ids: Full column-id set of the closure (start columns plus
            their transitive downstream columns) — both endpoints of every
            producer -> consumer hop (including synthetic bridges) must be
            present here for the adjacency query to find the connecting path.
    """
    table_set = set(affected_tables)
    successors: dict[str, set[str]] = {t: set() for t in affected_tables}
    indegree: dict[str, int] = {t: 0 for t in affected_tables}

    if closure_col_ids:
        rows = db.run_read(
            GET_TABLE_ADJACENCY_FOR_COLUMNS_QUERY,
            {"col_ids": closure_col_ids, "col_ids2": closure_col_ids},
        )
        # Raw rolled-up adjacency, including synthetic (cte/derived) endpoints.
        raw_successors: dict[str, set[str]] = {}
        synthetic_nodes: set[str] = set()
        for row in rows:
            src, dst = row["upstream_table"], row["downstream_table"]
            if src == dst:
                continue
            raw_successors.setdefault(src, set()).add(dst)
            if row["upstream_kind"] in _SYNTHETIC_TABLE_KINDS:
                synthetic_nodes.add(src)
            if row["downstream_kind"] in _SYNTHETIC_TABLE_KINDS:
                synthetic_nodes.add(dst)

        # Contract synthetic-node hops: from each real producer, BFS through
        # any chain of synthetic nodes to find the real consumers it reaches —
        # `real -> synthetic [-> synthetic ...] -> real` collapses to `real -> real`.
        for src in table_set:
            seen_chain: set[str] = set()
            frontier = deque(raw_successors.get(src, ()))
            while frontier:
                node = frontier.popleft()
                if node in seen_chain:
                    continue
                seen_chain.add(node)
                if node in table_set:
                    if node != src and node not in successors[src]:
                        successors[src].add(node)
                        indegree[node] += 1
                elif node in synthetic_nodes:
                    frontier.extend(raw_successors.get(node, ()))
                # Real nodes outside table_set (e.g. noise-filtered) are dead ends —
                # do not traverse through them; only synthetic bridges are contracted.

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
        path = Path(repo_path).resolve()
        if not path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        if not path.is_dir():
            raise ValueError(f"Repository path is not a directory: {repo_path}")

        # DuckDB: single R/W handle for the process lifetime — use directly.
        rw_db = _get_backend()

        indexer = Indexer()
        # Ensure the Repo node exists for this repository
        from sqlcg.core.schema import NodeLabel, RelType

        abs_path = str(path)
        rw_db.upsert_node(
            NodeLabel.REPO,
            abs_path,
            {
                "path": abs_path,
                "name": path.name,
            },
        )

        # Index the repository (with absolute path)
        result = indexer.index_repo(path, dialect, rw_db)

        # Create BELONGS_TO relationships from File nodes to Repo node
        # Query for all File nodes in this repo and link them to the Repo
        repo_prefix = abs_path.rstrip("/") + "/"
        file_rows = rw_db.run_read(INDEX_REPO_FILES_QUERY, {"repo_prefix": repo_prefix})
        for row in file_rows:
            rw_db.upsert_edge(
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


_REASON_MAP: dict[tuple[str | None, float | None], str] = {
    ("STAR_EXPANSION", 0.8): "star-expansion: columns inferred from source table schema",
    ("UNKNOWN", 0.5): "column not found in resolved schema",
    (None, 0.3): "scripting-block fallback: column lineage approximate",
    ("UNKNOWN", 0.0): "lineage extraction failed at index time",
}


def _reason_for(transform: str | None, confidence: float | None) -> str | None:
    """Return a human-readable reason for an inferred lineage edge.

    Returns None for fact edges (confidence >= 1.0 or confidence is None).
    Returns a descriptive string for inferred edges (confidence < 1.0).

    Note: confidence values from KuzuDB are stored as FLOAT (32-bit) and may
    differ from the Python float by up to ~1e-7. We round to 1 decimal place
    for the _REASON_MAP lookup to tolerate float32 storage imprecision.
    """
    if confidence is None or confidence >= 1.0:
        return None
    rounded = round(confidence, 1)
    return _REASON_MAP.get((transform, rounded)) or f"inferred edge (confidence={confidence:.2f})"


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
                                table=row.get("table_qualified"),
                                file=row.get("file"),
                                confidence=row.get("confidence"),
                                line=row.get("line") or None,
                                expression=row.get("expression"),
                                reason=_reason_for(row.get("transform"), row.get("confidence")),
                                table_kind=row.get("table_kind"),
                            )
                        )
                    queue.append((node_id, depth + 1))

        mermaid = _build_mermaid(col_id, edges) if edges else None

        # Bare-name fallback: when the primary query returns empty and the ref has a
        # schema component (3+ parts), retry with the schema prefix stripped.
        # This handles unqualified INSERT targets indexed without a schema prefix.
        bare_fallback_used = False
        if not lineage and len(table_col.split(".")) >= 3:
            bare = _bare_ref(table_col)
            bare_queue: deque[tuple[str, int]] = deque([(bare, 0)])
            bare_visited: set[str] = set()
            bare_emitted: set[str] = set()
            while bare_queue:
                current_id, depth = bare_queue.popleft()
                if current_id in bare_visited or (max_depth is not None and depth > max_depth):
                    continue
                if len(bare_visited) >= max_nodes:
                    break
                bare_visited.add(current_id)
                rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current_id})
                for row in rows:
                    node_id = row["id"]
                    edges.append((node_id, current_id, row.get("transform") or "SELECT"))
                    if node_id not in bare_visited and node_id not in bare_emitted:
                        bare_emitted.add(node_id)
                        lineage.append(
                            LineageNode(
                                name=row.get("col_name", ""),
                                kind="column",
                                table=row.get("table_qualified"),
                                file=row.get("file"),
                                confidence=row.get("confidence"),
                                line=row.get("line") or None,
                                expression=row.get("expression"),
                                reason=_reason_for(row.get("transform"), row.get("confidence")),
                                table_kind=row.get("table_kind"),
                            )
                        )
                    if node_id not in bare_visited:
                        bare_queue.append((node_id, depth + 1))
            if lineage:
                bare_fallback_used = True
                mermaid = _build_mermaid(bare, edges) if edges else None

        # Populate hint if result is empty (Step 4.1)
        hint = None
        if bare_fallback_used:
            bare = _bare_ref(table_col)
            hint = (
                f"No results for '{table_col}'. Found lineage under bare name '{bare}'. "
                "The INSERT target may have been indexed without a schema prefix. "
                "Multiple tables with the same unqualified name in different schemas "
                "would all match — re-index with an explicit schema for precise results."
            )
        elif not lineage:
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

        up_rows = db.run_read(
            GET_TABLE_DIRECT_UPSTREAMS_QUERY,
            {"table_qualified": target, "table_qualified2": target},
        )
        upstream_raw = _dedup_preserve_order(
            [r["upstream_table"] for r in up_rows if r["upstream_table"]]
        )
        upstream_tables, _ = noise_filter.filter_nodes(upstream_raw)

        col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": target})
        start_cols = [r["col_id"] for r in col_rows]
        affected_cols, _depth, truncated = _affected_columns_closure(db, start_cols, max_depth=None)

        affected_tables_all = [t for t in _rollup_to_tables(affected_cols) if t != target]
        affected_tables_noise_filtered, noise_excluded = noise_filter.filter_nodes(
            affected_tables_all
        )
        affected_tables, synthetic_excluded = _exclude_synthetic_tables(
            db, affected_tables_noise_filtered
        )
        noise_excluded = [*noise_excluded, *synthetic_excluded]
        kept_tables = set(affected_tables)
        affected_columns = [c for c in affected_cols if c.rsplit(".", 1)[0] in kept_tables]

        n = len(affected_tables)
        risk = Judgement(
            assertion_type="heuristic",
            label=_risk_label(n),
            confidence=0.6,
            reason=(
                f"{n} downstream dependent table(s); thresholds safe=0/low<=5/medium<=20/high>20"
            ),
        )

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
            downstream_count=n,
            risk=risk,
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
        affected_tables_noise_filtered, noise_excluded = noise_filter.filter_nodes(
            affected_tables_all
        )
        affected_tables, synthetic_excluded = _exclude_synthetic_tables(
            db, affected_tables_noise_filtered
        )
        noise_excluded = [*noise_excluded, *synthetic_excluded]
        kept_tables = set(affected_tables)
        affected_columns = [c for c in affected_cols if c.rsplit(".", 1)[0] in kept_tables]

        # Option A (issue #38): adjacency is derived from the same COLUMN_LINEAGE
        # closure as membership — pass the full id set (start + affected) so the
        # direct producer->consumer edge endpoints are both present.
        closure_col_ids = _dedup_preserve_order([*start_cols, *affected_cols])
        order, had_cycle = _kahn_topological_sort(affected_tables, db, closure_col_ids)

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
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)
        prefixes = get_presentation_prefixes(root)

        changed_tables: list[str] = []
        seen_changed: set[str] = set()
        for file_path in changed_files:
            # Resolve relative paths against the indexed root so callers can pass
            # relative names from the repo root (e.g. "etl/my_file.sql").
            fp = Path(file_path)
            if not fp.is_absolute():
                fp = root / fp
            for query in (GET_TABLES_DEFINED_IN_FILE_QUERY, GET_TARGET_TABLES_FOR_FILE_QUERY):
                rows = db.run_read(query, {"file_path": str(fp)})
                for row in rows:
                    tq = row["table_qualified"]
                    if tq and tq not in seen_changed:
                        seen_changed.add(tq)
                        changed_tables.append(tq)

        all_affected_cols: list[str] = []
        affected_seen: set[str] = set()
        all_start_cols: list[str] = []
        start_seen: set[str] = set()
        truncated = False
        for tq in changed_tables:
            col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": tq})
            start_cols = [r["col_id"] for r in col_rows]
            for col in start_cols:
                if col not in start_seen:
                    start_seen.add(col)
                    all_start_cols.append(col)
            cols, _depth, tr = _affected_columns_closure(db, start_cols, max_depth=None)
            truncated = truncated or tr
            for col in cols:
                if col not in affected_seen:
                    affected_seen.add(col)
                    all_affected_cols.append(col)

        affected_tables_all = [
            t for t in _rollup_to_tables(all_affected_cols) if t not in seen_changed
        ]
        affected_tables_noise_filtered, noise_excluded = noise_filter.filter_nodes(
            affected_tables_all
        )
        affected_tables, synthetic_excluded = _exclude_synthetic_tables(
            db, affected_tables_noise_filtered
        )
        noise_excluded = [*noise_excluded, *synthetic_excluded]
        presentation_facing = [t for t in affected_tables if any(t.startswith(p) for p in prefixes)]
        # Option A (issue #38): same closure-derived adjacency as get_backfill_order —
        # union of every changed table's start columns plus the unioned affected set.
        closure_col_ids = _dedup_preserve_order([*all_start_cols, *all_affected_cols])
        order, had_cycle = _kahn_topological_sort(affected_tables, db, closure_col_ids)

        # Resolve external consumers for the blast radius in a single BATCH query (not per-table).
        external_consumers: list[str] = []
        all_blast_tables = list(set(changed_tables) | set(affected_tables))
        if all_blast_tables:
            consumer_rows = db.run_read(
                GET_TABLES_EXTERNAL_CONSUMERS_BATCH_QUERY,
                {"table_qualifieds": all_blast_tables},
            )
            seen_consumers: set[str] = set()
            for crow in consumer_rows:
                cname = crow["name"]
                if cname not in seen_consumers:
                    seen_consumers.add(cname)
                    external_consumers.append(cname)

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
            external_consumers=external_consumers,
            noise_excluded=noise_excluded,
            truncated=truncated,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("scope_change")
def scope_change(target: str) -> ScopeChangeResult:
    """One-call synthesis of everything an LLM needs before changing a table.

    Delegates to the already-implemented anchor tools — find_definition,
    get_change_scope, get_backfill_order — and assembles their results into a
    single response. No query logic is reimplemented here. Each delegate call is
    isolated: a failure in one is logged and surfaced in ``hint`` without
    blanking the rest of the response.

    Args:
        target: Qualified table name being changed (e.g. "ba.wtfe_verkoopinfo")

    Returns:
        ScopeChangeResult bundling authoritative files, upstream inputs,
        downstream blast radius (table + column precise), rebuild order, and risk.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    authoritative_files: list[str] = []
    upstream_inputs: list[str] = []
    downstream_blast_radius: list[str] = []
    affected_columns: list[str] = []
    backfill_order: list[str] = []
    downstream_count = 0
    risk: Judgement = Judgement(
        assertion_type="heuristic",
        label="safe",
        confidence=0.6,
        reason="0 downstream dependent table(s); thresholds safe=0/low<=5/medium<=20/high>20",
    )
    noise_excluded: list[str] = []
    truncated = False
    hints: list[str] = []

    try:
        definition = find_definition(target)
        authoritative_files = [d.file_path for d in definition.definitions if d.is_authoritative]
        noise_excluded.extend(definition.noise_excluded)
        if definition.hint:
            hints.append(definition.hint)
    except Exception as exc:
        logger.warning("scope_change: find_definition(%s) failed: %s", target, exc)
        hints.append(f"find_definition failed: {exc}")

    try:
        scope = get_change_scope(target)
        upstream_inputs = scope.upstream_tables
        downstream_blast_radius = scope.affected_tables
        affected_columns = scope.affected_columns
        downstream_count = scope.downstream_count
        risk = scope.risk
        noise_excluded.extend(scope.noise_excluded)
        truncated = truncated or scope.truncated
        if scope.hint:
            hints.append(scope.hint)
    except Exception as exc:
        logger.warning("scope_change: get_change_scope(%s) failed: %s", target, exc)
        hints.append(f"get_change_scope failed: {exc}")

    try:
        backfill = get_backfill_order(target)
        backfill_order = backfill.backfill_order
        noise_excluded.extend(backfill.noise_excluded)
        truncated = truncated or backfill.truncated
        if backfill.hint:
            hints.append(backfill.hint)
    except Exception as exc:
        logger.warning("scope_change: get_backfill_order(%s) failed: %s", target, exc)
        hints.append(f"get_backfill_order failed: {exc}")

    noise_excluded = _dedup_preserve_order(noise_excluded)
    hint = " | ".join(hints) if hints else None

    return ScopeChangeResult(
        target=target,
        authoritative_files=authoritative_files,
        upstream_inputs=upstream_inputs,
        downstream_blast_radius=downstream_blast_radius,
        affected_columns=affected_columns,
        backfill_order=backfill_order,
        downstream_count=downstream_count,
        risk=risk,
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
                                table=row.get("table_qualified"),
                            )
                        )
                    queue.append((node_id, next_depth))
                elif node_id not in visited and max_depth is not None and next_depth > max_depth:
                    truncated = True

        # Append external consumer terminal nodes via a single BATCH query (not per-table loop).
        # Collect all terminal tables from the column closure then issue one round-trip.
        terminal_tables = list({n.table for n in nodes if n.table is not None})
        # Also include the root column's table when there are no intermediate nodes
        # (root may itself be terminal with a consumer but no further column hops).
        root_table = table_id  # table_id was extracted above from table_col
        if root_table not in terminal_tables:
            terminal_tables.append(root_table)

        if terminal_tables:
            consumer_rows = db.run_read(
                GET_TABLES_EXTERNAL_CONSUMERS_BATCH_QUERY,
                {"table_qualifieds": terminal_tables},
            )
            for crow in consumer_rows:
                nodes.append(
                    DependencyNode(
                        name=crow["name"],
                        kind="external_consumer",
                        table=crow["table_qualified"],
                    )
                )

        # Populate hint if result is empty (Step 4.2)
        # Only fire the "terminal output" hint when there are also no external consumers.
        hint = None
        column_nodes = [n for n in nodes if n.kind == "column"]
        consumer_nodes = [n for n in nodes if n.kind == "external_consumer"]
        if not column_nodes and not consumer_nodes:
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
                                table=row.get("table_qualified"),
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
        result = db.run_read(f'SELECT count(*) AS count FROM "{label}"', {})
        node_counts[str(label)] = result[0]["count"] if result else 0

    edges_result = db.run_read('SELECT count(*) AS count FROM "COLUMN_LINEAGE"', {})
    column_lineage_edges = edges_result[0]["count"] if edges_result else 0

    mode_rows = db.run_read(
        'SELECT parsing_mode AS mode, count(*) AS cnt FROM "SqlQuery" '
        "GROUP BY parsing_mode ORDER BY cnt DESC",
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

    # Compute freshness from the stored SHA + first Repo node path
    _freshness_kwargs: dict = {}
    try:
        _indexed_sha = db.get_indexed_sha()
        _repo_rows = db.run_read('SELECT path FROM "Repo" LIMIT 1', {})
        if _repo_rows and _indexed_sha is not None and _repo_rows[0].get("path"):
            _root = Path(_repo_rows[0]["path"])
            _f = compute_freshness(_root, _indexed_sha)
            _freshness_kwargs = {
                "indexed_sha": _f.indexed_sha,
                "head_sha": _f.head_sha,
                "stale_by_commits": _f.stale_by_commits,
                "dirty": _f.dirty,
            }
        elif _indexed_sha is not None:
            _freshness_kwargs = {"indexed_sha": str(_indexed_sha)}
    except NotImplementedError:
        # Neo4j backend raises NotImplementedError for get_indexed_sha — report null
        pass
    except Exception:
        # Any unexpected error must not crash the db_info tool
        pass

    return DbInfoResult(
        schema_version=schema_version,
        node_counts=node_counts,
        column_lineage_edges=column_lineage_edges,
        parse_quality=parse_quality,
        warnings=warnings,
        **_freshness_kwargs,
    )


@mcp.tool()
@_timed_tool("execute_sql")
def execute_sql(query: str) -> list[dict]:
    """Execute a read-only SQL query against the graph (DuckDB).

    This tool allows direct SQL queries for advanced users. It enforces
    read-only mode by stripping quoted literals and checking for write
    operation keywords. A LIMIT clause is automatically appended if missing.

    **Important Security Note**: This tool strips single and double-quoted
    string literals before checking for write operations. String literals
    containing mutation keywords (e.g., 'DROP TABLE') will NOT trigger the
    write-operation blocker. This is by design to allow querying SQL text
    that contains such keywords.

    Args:
        query: DuckDB SQL query string (read-only SELECT only)

    Returns:
        List of result dictionaries from the query

    Raises:
        ValueError: If the query contains write operations (INSERT, UPDATE,
                   DELETE, CREATE, DROP, TRUNCATE, MERGE)
    """
    db = _get_backend()

    # Strip quoted string literals before blocklist check
    stripped = re.sub(r"'(?:''|[^'])*'", "", query)
    stripped = re.sub(r'"(?:""|[^"])*"', "", stripped)

    # Check for write operations (case-insensitive)
    if re.search(
        r"\b(INSERT|UPDATE|DELETE|CREATE|MERGE|DROP|TRUNCATE)\b",
        stripped,
        re.IGNORECASE,
    ):
        raise ValueError(
            "Write operations are not permitted via execute_sql. "
            "Use the CLI or dedicated tools instead."
        )

    # Auto-append LIMIT if missing
    q = query.rstrip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if "limit" not in stripped.lower():
        q = q + " LIMIT 500"

    try:
        return db.run_read(q, {})
    except Exception as e:
        logger.error(f"SQL execution failed: {e}")
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


def _matched_presentation_prefix(table_qualified: str, prefixes: list[str]) -> str | None:
    """Return the first configured presentation prefix the table matches, else None.

    Prefixes are pre-lowercased by get_presentation_prefixes; compare lowercased so an
    ``IA_ANALYTICS.foo`` table matches an ``ia_`` prefix.
    """
    tql = table_qualified.lower()
    for p in prefixes:
        if tql.startswith(p):
            return p
    return None


@mcp.tool()
@_timed_tool("analyze_unused")
def analyze_unused() -> UnusedTablesResult:
    """Find tables with no within-corpus consumers (dead-code candidates).

    Identifies SqlTable nodes that no SqlQuery selects from (zero SELECTS_FROM
    incoming edges). Each candidate carries a heuristic dead_code Judgement
    (confidence=0.5) because the table may be consumed externally — by BI tools,
    an API layer, or COPY INTO statements — which are invisible to the corpus.

    Backup/noise tables are excluded via NoiseFilter so snapshot tables do not
    pollute the results.

    Returns:
        UnusedTablesResult with the dead-code candidate list and total table count.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)

        # Single aggregation — no Python per-row graph traversal.
        unused_rows = db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        total_rows = db.run_read('SELECT count(*) AS n FROM "SqlTable"', {})
        total_tables_scanned = total_rows[0]["n"] if total_rows else 0

        prefixes = get_presentation_prefixes(root)

        # Build a set of tables that have at least one CONSUMED_BY edge, using a single
        # BATCH query over all unused tables (one round-trip, not one per table).
        unused_table_names = [row["table_qualified"] for row in unused_rows]
        consumed_tables: set[str] = set()
        if unused_table_names:
            batch_rows = db.run_read(
                GET_TABLES_EXTERNAL_CONSUMERS_BATCH_QUERY,
                {"table_qualifieds": unused_table_names},
            )
            for crow in batch_rows:
                consumed_tables.add(crow["table_qualified"])

        candidates: list[UnusedCandidate] = []
        presentation_facing: list[PresentationCandidate] = []
        for row in unused_rows:
            tq = row["table_qualified"]
            if noise_filter.is_noise(tq):
                continue
            has_consumer = tq in consumed_tables
            matched = _matched_presentation_prefix(tq, prefixes)
            if matched is not None:
                presentation_facing.append(
                    PresentationCandidate(
                        table_qualified=tq,
                        matched_prefix=matched,
                        has_external_consumer=has_consumer,
                    )
                )
                continue
            candidates.append(
                UnusedCandidate(
                    table_qualified=tq,
                    within_corpus_references=0,
                    dead_code=Judgement(
                        assertion_type="heuristic",
                        label="dead_code_candidate",
                        confidence=0.5,
                        reason=(
                            "zero within-corpus SELECTS_FROM references; "
                            "may be consumed externally (BI, API, COPY INTO)"
                        ),
                    ),
                )
            )

        hint = None
        if not candidates and not presentation_facing:
            hint = (
                "No unused-table candidates found. All indexed tables have at least one "
                "within-corpus consumer, or the graph is empty."
            )
        elif not candidates and presentation_facing:
            hint = (
                f"No dead-code candidates outside the declared egress layer. "
                f"{len(presentation_facing)} terminal/presentation leaf(s) are reported "
                f"separately (expected to have no in-corpus consumer)."
            )

        return UnusedTablesResult(
            candidates=candidates,
            presentation_facing=presentation_facing,
            total_tables_scanned=total_tables_scanned,
            hint=hint,
        )


@mcp.tool()
@_timed_tool("get_hub_ranking")
def get_hub_ranking(k: int = 10) -> HubRankingResult:
    """Return the top-k tables by downstream dependent count (hub/centrality ranking).

    Counts, for each table, the number of DISTINCT consuming tables whose SqlQuery
    selects from it. All fields are deterministic graph facts — no heuristic
    Judgement is attached. Re-ranks after noise filtering so rank is contiguous.

    Backup/noise tables are excluded via NoiseFilter.

    Args:
        k: Maximum number of results to return (default 10).

    Returns:
        HubRankingResult with the top-k ranked entries.

    Raises:
        NotIndexedError: If no repos have been indexed
    """
    with _open_backend() as db:
        _assert_indexed(db)
        noise_filter = NoiseFilter.from_config()

        # Single aggregation — no Python per-row graph calls.
        rows = db.run_read(HUB_RANKING_QUERY, {"k": k})

        top: list[HubEntry] = []
        rank = 1
        for row in rows:
            tq = row["table_qualified"]
            if noise_filter.is_noise(tq):
                continue
            top.append(
                HubEntry(
                    table_qualified=tq,
                    downstream_dependents=int(row["downstream_dependents"]),
                    rank=rank,
                )
            )
            rank += 1

        hint = None
        if not top:
            hint = (
                "No hub candidates found. Either no tables have downstream consumers, "
                "or all candidates were excluded by the noise filter."
            )

        return HubRankingResult(top=top, k=k, hint=hint)
