"""MCP tools for SQL code graph queries and indexing."""

import functools
import re
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from sqlcg.core.config import get_db_path, get_presentation_prefixes
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.freshness import compute_freshness
from sqlcg.core.graph_db import GraphBackend, indexed_repo_root
from sqlcg.core.queries import (
    ANALYZE_UNUSED_TABLES_QUERY,
    FIND_DEFINITION_QUERY,
    FIND_TABLE_USAGES_QUERY,
    FIND_TABLE_USAGES_VIA_LINEAGE_QUERY,
    GET_COLUMNS_FOR_TABLE_QUERY,
    GET_DOWNSTREAM_DEPENDENCIES_QUERY,
    GET_PRODUCER_FILES_FOR_TABLE_QUERY,
    GET_PRODUCER_TABLES_QUERY,
    GET_TABLE_ADJACENCY_FOR_COLUMNS_QUERY,
    GET_TABLE_DEFINING_FILES_QUERY,
    GET_TABLE_DIRECT_UPSTREAMS_QUERY,
    GET_TABLE_KINDS_BATCH_QUERY,
    GET_TABLE_READS_ADJACENCY_QUERY,
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
    EmptyPropagationResult,
    HubEntry,
    HubRankingResult,
    Judgement,
    LineageNode,
    LineageResult,
    PresentationCandidate,
    PrImpactResult,
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

# The path that init_backend() actually opened.  Captured at init time so
# MCP write tools use this path, not get_db_path() which returns the default
# ~/.sqlcg/graph.db regardless of what was passed to init_backend.
_init_db_path: str | None = None


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


# Single backend-handle implementation lives in graph_db.py (v1.14.0 Fix 3
# Step 3.4 de-duplication) — shared with sqlcg.cli.commands.catalog. Keep the
# `_indexed_root` name here since it's used at 6 call sites below.
_indexed_root = indexed_repo_root


def _bare_ref(ref: str) -> str:
    """Strip schema prefix from a ref string, keeping table.column.

    For a 3-part ref ("mart.fact_t.amount") this returns "fact_t.amount".
    For a 2-part ref ("fact_t.amount") this returns the ref unchanged.
    Never uses rsplit — that would yield only the column name for 3-part refs.

    PR 3 (sprint_lineage_identity_and_session_context.md §PR 3): namespaced CTE
    column keys carry ``::`` (e.g. "path/file.sql::final.col") and contain dots
    from the file path.  They have no schema prefix to strip, so return them
    unchanged when ``::`` is present.
    """
    if "::" in ref:
        return ref  # namespaced CTE key — no schema prefix to strip
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
# temp_table_namespacing Step 4.2: 'temp' added — per-file session temps are
# synthetic intermediates (bridges), not durable affected tables.  Impact analysis
# already contracts cte/derived bridges; temp inherits the same behaviour so
# per-file session temps are contracted rather than reported as distinct affected tables.
_SYNTHETIC_TABLE_KINDS = frozenset({"cte", "derived", "temp"})


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


def _empty_closure_hint(db: GraphBackend, table_id: str, cols: list[dict] | None = None) -> str:
    """Return the hint string for an empty lineage-closure result.

    Disambiguates two distinct "empty" situations (plan/sprints/positional_insert_clone_blindspot.md
    Part A1):

    - The table has **zero** catalogued columns (``HAS_COLUMN`` returns no rows). This is
      the CLONE / no-column-list-positional-INSERT blind spot: sqlcg has no authoritative
      target column names for this table, so an empty closure here is *unproven*, not a
      genuine negative — raw ``COLUMN_LINEAGE`` edges may still exist keyed under a
      source-derived column name.
    - The table **has** a column catalog and the closure is still empty — the column is
      genuinely terminal (no further upstream/downstream lineage to report).

    Args:
        db: GraphBackend to read from.
        table_id: Qualified table id (schema.table or catalog.schema.table).
        cols: optional pre-fetched ``GET_COLUMNS_FOR_TABLE_QUERY`` result. Pass this when
            the caller already queried the catalog (e.g. to branch on it for its own
            messaging) so this helper does not issue a redundant duplicate query.
            When ``None``, the helper queries the catalog itself.

    Returns:
        A diagnostic hint string naming which situation applies.
    """
    if cols is None:
        cols = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": table_id})
    if not cols:
        return (
            f"No column catalog for '{table_id}' (0 catalogued columns). This table was "
            "likely created via CREATE TABLE ... CLONE and/or loaded by a positional "
            "INSERT ... SELECT with no column list, so sqlcg has no authoritative target "
            "column names. Lineage edges may still exist keyed on a SOURCE-derived column "
            "name — check raw COLUMN_LINEAGE for this table. Empty here is UNPROVEN, not "
            "negative."
        )
    return (
        "No lineage found. Ensure the column reference includes the schema "
        "prefix (e.g., ba.table_name.column_name). Check that 'sqlcg db info' "
        "shows SqlColumn > 0. If SqlColumn is 0, column lineage was not "
        "extracted — check parse errors. Submit feedback with "
        "submit_feedback tool if this was a false negative."
    )


@mcp.tool()
@_timed_tool("trace_column_lineage")
def trace_column_lineage(
    table_col: str, max_depth: int | None = None, view: str = "structural"
) -> LineageResult:
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
        view: Lineage view selector (E8 dual-emission, plan/research/
              e8_dual_emission_feasibility.md §6.3).
              - "structural" (default): the real per-hop graph; filters OUT the derived
                transform='TEMP_INLINE' shortcut edges. The BFS still reaches every leaf
                via the structural temp hops, so the shortcut adds no reachability — only
                diagram clutter — and is elided.
              - "source_to_sink": the temps-elided view; keeps ONLY the TEMP_INLINE edges,
                answering "what real source table feeds this column?" in one hop.

    Returns:
        LineageResult with list of upstream column nodes

    Raises:
        NotIndexedError: If no repos have been indexed
        InvalidColumnRefError: If column reference format is invalid
    """
    if view not in ("structural", "source_to_sink"):
        raise ValueError(f"invalid view {view!r}; expected 'structural' or 'source_to_sink'")

    def _select_rows(rows: list) -> list:
        """E8 dual-emission view selector: keep structural OR TEMP_INLINE rows."""
        if view == "source_to_sink":
            return [r for r in rows if (r.get("transform") or "") == "TEMP_INLINE"]
        return [r for r in rows if (r.get("transform") or "") != "TEMP_INLINE"]

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
            rows = _select_rows(
                db.run_read(
                    TRACE_COLUMN_LINEAGE_QUERY,
                    {"id": current_id},
                )
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
        # PR 3: namespaced CTE keys contain "::" and dots from the file path but have
        # no schema prefix — _bare_ref returns them unchanged, so exclude them from
        # the fallback to avoid a no-op retry.
        bare_fallback_used = False
        if not lineage and "::" not in table_col and len(table_col.split(".")) >= 3:
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
                rows = _select_rows(db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current_id}))
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
            hint = _empty_closure_hint(db, table_id)

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
        name_lower = table_name.lower()

        # Direct reads: SELECTS_FROM and STAR_SOURCE (D1 + D2).
        direct_rows = db.run_read(
            FIND_TABLE_USAGES_QUERY,
            {"name": name_lower},
        )

        usages: list[TableUsage] = []
        # Track (query_file, sql) pairs already added so we can dedup below.
        # When the same query appears in both direct and via-lineage, prefer "direct".
        seen: set[tuple[str, str | None]] = set()
        for row in direct_rows:
            usage = TableUsage(
                query_file=row["file"],
                sql=row.get("sql"),
                kind=row.get("kind"),
                usage_kind="direct",
            )
            usages.append(usage)
            seen.add((row["file"], row.get("sql")))

        # Via-lineage reads: COLUMN_LINEAGE source-table rollup (D3).
        # Captures CTE-wrapped derived reads that emit no top-level SELECTS_FROM edge.
        lineage_rows = db.run_read(
            FIND_TABLE_USAGES_VIA_LINEAGE_QUERY,
            {"name": name_lower},
        )
        for row in lineage_rows:
            key = (row["file"], row.get("sql"))
            if key in seen:
                # Already reported as a direct usage — skip to preserve "direct" label.
                continue
            usages.append(
                TableUsage(
                    query_file=row["file"],
                    sql=row.get("sql"),
                    kind=row.get("kind"),
                    usage_kind="via_lineage",
                )
            )
            seen.add(key)

        # Populate hint if result is empty.
        hint = None
        if not usages:
            hint = (
                "No usages found for this table. Possible causes (in order): "
                "(1) No direct SELECTS_FROM or STAR_SOURCE read found in any indexed SQL file. "
                "(2) No column-lineage value flows out of this table "
                "(CTE-wrapped derived reads are captured via COLUMN_LINEAGE). "
                "(3) The table may be read ONLY inside a CTE without deriving any column "
                "(a pure-gating read) — this is NOT detected by any signal (known gap). "
                "(4) The table may be consumed externally (BI tools, APIs) without any "
                "indexed SQL referencing it. "
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

        target = table_qualified.lower()
        rows = db.run_read(FIND_DEFINITION_QUERY, {"table_qualified": target})
        producer_rows = db.run_read(GET_PRODUCER_FILES_FOR_TABLE_QUERY, {"table_qualified": target})
        producer_files = _dedup_preserve_order([r["file_path"] for r in producer_rows])
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)

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
            producer_files=producer_files,
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

    The ``gating_join_tables`` field supplements the column-lineage blast radius
    with tables that read the target via a DIRECT (non-CTE) gating join
    (SELECTS_FROM/STAR_SOURCE) but derive NO column from it.
    KNOWN GAP: CTE-wrapped pure-gating reads are NOT detected.
    UPPER BOUND: depends on join type (not recorded on SELECTS_FROM).

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
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)

        def_rows = db.run_read(GET_TABLE_DEFINING_FILES_QUERY, {"table_qualified": target})
        producer_rows = db.run_read(GET_PRODUCER_FILES_FOR_TABLE_QUERY, {"table_qualified": target})
        defining_files = _dedup_preserve_order(
            [r["file_path"] for r in def_rows] + [r["file_path"] for r in producer_rows]
        )

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

        # PR 3 (v1.24.0) — gating-join supplement: tables that read the target
        # via a DIRECT (non-CTE) gating join but derive NO column from it.
        # Does NOT change affected_tables or downstream_count (decision B).
        gating_join_tables, gj_truncated = _table_row_reachability(db, [target], max_depth=None)
        truncated = truncated or gj_truncated

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
            gating_join_tables=gating_join_tables,
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
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)

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

    The ``gating_join_tables`` field supplements the column-lineage blast radius
    with tables that read any of the changed tables via a DIRECT (non-CTE) gating
    join (SELECTS_FROM/STAR_SOURCE) but derive NO column from them.
    KNOWN GAP: CTE-wrapped pure-gating reads are NOT detected.
    UPPER BOUND: depends on join type (not recorded on SELECTS_FROM).

    Args:
        changed_files: SQL file paths that changed (must match indexed File.path)

    Returns:
        DiffImpactResult with changed tables, union blast radius, presentation
        subset, rebuild order, and gating_join_tables supplement.

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

        # PR 3 (v1.24.0) — gating-join supplement: tables that read any changed
        # table via a DIRECT (non-CTE) gating join but derive NO column from them.
        # Does NOT change affected_tables, order, downstream_count, presentation_facing,
        # or external_consumers (decision B).
        gating_join_tables: list[str] = []
        if changed_tables:
            gj_tables, gj_truncated = _table_row_reachability(db, changed_tables, max_depth=None)
            gating_join_tables = gj_tables
            truncated = truncated or gj_truncated

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
            gating_join_tables=gating_join_tables,
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
            cols = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": table_id})
            if not cols:
                hint = _empty_closure_hint(db, table_id, cols=cols)
            else:
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

        # Populate hint if result is empty (Step 4.1; Part A1 disambiguates no-catalog)
        hint = None
        if not nodes:
            hint = _empty_closure_hint(db, table_id)

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

    `sqlcg_version` is the installed *package* build (sqlcg.__version__) — use
    it to self-check you are talking to the build you expect (the MCP protocol
    `serverInfo.version` is not reliably visible to the agent persona). This is
    a different axis from `schema_version`, which tracks the graph schema, not
    the package: do not conflate the two.

    Parse quality legend (parsing_mode per SqlQuery node):
      sqlglot          — standard path; column lineage available if extracted
      scripting_block  — tokenizer fallback; column lineage unavailable

    Returns:
        DbInfoResult with sqlcg/schema versions, node counts, parse quality,
        and warnings
    """
    from sqlcg import __version__

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
        sqlcg_version=__version__,
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
        root = _indexed_root(db) or Path.cwd()
        noise_filter = NoiseFilter.from_config(repo_root=root)

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


# ---------------------------------------------------------------------------
# empty-impact blast-radius engine (PR 1 — v1.22.0)
# ---------------------------------------------------------------------------


def _table_row_reachability(
    db: GraphBackend,
    start_tables: list[str],
    max_depth: int | None = None,
) -> tuple[list[str], bool]:
    """BFS over the table-reads adjacency for View 1 (row_empty_tables branch (b)).

    Loads the full ``SELECTS_FROM ∪ STAR_SOURCE`` adjacency once (source_table →
    dest_table via ``SqlQuery.target_table``), then contracts synthetic nodes (kind
    in {cte, derived, temp}) OUT OF the adjacency itself — bridging real→synthetic→real
    paths into direct real→real successor edges — so synthetic bridges never gate or
    pollute the traversal.

    M4 (plan §Step 1.2): the contraction happens IN the adjacency map before the BFS,
    not only as a post-filter.  ``_exclude_synthetic_tables`` is applied to the output
    as a belt-and-braces second pass.

    Args:
        db: Graph backend.
        start_tables: Named source tables (lowercased).
        max_depth: Optional depth cap (None = unbounded).

    Returns:
        (reachable_tables, truncated): order-preserving list of reachable tables
        excluding the named sources and synthetic nodes, plus a bool cap flag.
    """
    # 1. Load the full source → dest adjacency in one query.
    rows = db.run_read(GET_TABLE_READS_ADJACENCY_QUERY, {})
    raw_successors: dict[str, set[str]] = {}
    all_endpoints: set[str] = set()
    for row in rows:
        src, dst = row["source_table"], row["dest_table"]
        raw_successors.setdefault(src, set()).add(dst)
        all_endpoints.add(src)
        all_endpoints.add(dst)

    # 2. Identify synthetic nodes among the endpoints.
    synthetic_ids: set[str] = set()
    if all_endpoints:
        kind_rows = db.run_read(
            GET_TABLE_KINDS_BATCH_QUERY, {"table_qualifieds": list(all_endpoints)}
        )
        synthetic_ids = {
            r["table_qualified"] for r in kind_rows if r["kind"] in _SYNTHETIC_TABLE_KINDS
        }

    # 3. Build contracted adjacency: for each real source, BFS through any chain
    #    of synthetic nodes to find the real consumers — real→synthetic→...→real
    #    becomes a direct real→real successor edge.
    def _real_successors(node: str, visited_chain: set[str] | None = None) -> set[str]:
        """Return the set of real table successors reachable from node via synthetic bridges."""
        if visited_chain is None:
            visited_chain = set()
        result: set[str] = set()
        for nxt in raw_successors.get(node, ()):
            if nxt in visited_chain:
                continue
            visited_chain.add(nxt)
            if nxt not in synthetic_ids:
                result.add(nxt)
            else:
                result.update(_real_successors(nxt, visited_chain))
        return result

    contracted: dict[str, set[str]] = {}
    for src in all_endpoints:
        if src not in synthetic_ids:
            contracted[src] = _real_successors(src)

    # 4. Cycle-safe BFS from start_tables over the contracted adjacency.
    source_set = set(start_tables)
    visited: set[str] = set(start_tables)  # seed with sources so they aren't re-queued
    reachable: list[str] = []
    emitted: set[str] = set()
    queue: deque[tuple[str, int]] = deque((t, 0) for t in start_tables)
    truncated = False

    while queue:
        current, depth = queue.popleft()

        if max_depth is not None and depth >= max_depth:
            # Enqueue nothing; flag truncated if there would be successors.
            if contracted.get(current):
                truncated = True
            continue

        for nxt in contracted.get(current, ()):
            if nxt in visited:
                continue
            if len(visited) >= _MAX_CLOSURE_NODES:
                truncated = True
                break
            visited.add(nxt)
            if nxt not in source_set and nxt not in emitted:
                emitted.add(nxt)
                reachable.append(nxt)
            queue.append((nxt, depth + 1))

    # 5. Belt-and-braces: exclude any synthetic node that slipped through.
    real_reachable, synthetic_excluded = _exclude_synthetic_tables(db, reachable)
    return real_reachable, truncated


def _compute_empty_propagation(
    db: GraphBackend,
    tables: list[str],
    max_depth: int | None,
) -> EmptyPropagationResult:
    """Compute the two-view empty-propagation blast radius for named source tables.

    View 2 (PRIMARY — column-lineage refinement) is built first; it is the reliable
    headline answer. View 1 (SUPPLEMENT — row_empty_tables) is the UNION of
    View 2's affected-table rollup (branch a) and the direct ``SELECTS_FROM``/
    ``STAR_SOURCE`` reachability result (branch b).

    W1 (plan §Step 1.3): ``_kahn_topological_sort`` receives
    ``_dedup_preserve_order([*start_cols, *affected_cols])`` — both endpoints of
    every producer→consumer hop — so the synthetic-node contraction inside Kahn
    can find the adjacency edges.  Passing ``affected_cols`` alone degrades
    ordering for multi-hop chains routed through CTE/derived bridges (N3).

    Args:
        db: Graph backend (already open).
        tables: One or more lowercased qualified table names.
        max_depth: Maximum BFS depth (None = unbounded).

    Returns:
        EmptyPropagationResult with both views populated.
    """
    # Normalise: lowercase, dedup, preserve order.
    sources = _dedup_preserve_order([t.lower() for t in tables])
    source_set = set(sources)

    # Collect start columns (union of all source tables' known columns).
    start_cols: list[str] = []
    start_seen: set[str] = set()
    not_found: list[str] = []
    for source in sources:
        col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": source})
        if not col_rows:
            not_found.append(source)
        for r in col_rows:
            cid = r["col_id"]
            if cid not in start_seen:
                start_seen.add(cid)
                start_cols.append(cid)

    # --- View 2: column-lineage closure (PRIMARY) ---
    root = _indexed_root(db) or Path.cwd()
    noise_filter = NoiseFilter.from_config(repo_root=root)

    affected_cols, _depth, truncated_v2 = _affected_columns_closure(db, start_cols, max_depth)

    # Roll up to tables, exclude sources, noise-filter, exclude synthetic.
    affected_tables_raw = [t for t in _rollup_to_tables(affected_cols) if t not in source_set]
    affected_tables_nf, noise_excluded_v2 = noise_filter.filter_nodes(affected_tables_raw)
    value_affected_tables, synthetic_excluded_v2 = _exclude_synthetic_tables(db, affected_tables_nf)
    noise_excluded = _dedup_preserve_order([*noise_excluded_v2, *synthetic_excluded_v2])

    # Filter value_empty_columns to only kept tables.
    kept_set = set(value_affected_tables)
    value_empty_columns = [c for c in affected_cols if c.rsplit(".", 1)[0] in kept_set]

    # Fully vs. partially empty tables: compare per-table value-empty columns against
    # the full set of known columns for that table.
    value_fully_empty_tables: list[str] = []
    value_partially_empty_tables: list[str] = []
    affected_col_set = set(value_empty_columns)
    for tbl in value_affected_tables:
        all_col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": tbl})
        all_col_ids = {r["col_id"] for r in all_col_rows}
        if not all_col_ids:
            # No known columns in graph — cannot classify; treat as partial.
            value_partially_empty_tables.append(tbl)
            continue
        affected_here = all_col_ids & affected_col_set
        if affected_here >= all_col_ids:
            value_fully_empty_tables.append(tbl)
        else:
            value_partially_empty_tables.append(tbl)

    # --- View 1: row_empty_tables (SUPPLEMENT) ---
    # branch (a) = value_affected_tables (already computed above)
    # branch (b) = _table_row_reachability BFS result
    branch_b_reachable, truncated_v1 = _table_row_reachability(db, sources, max_depth)

    truncated = truncated_v1 or truncated_v2

    # Union of branch (a) and branch (b), then noise-filter and exclude synthetic.
    row_union_raw = _dedup_preserve_order([*value_affected_tables, *branch_b_reachable])
    # branch_b may include noise that didn't go through value filter.
    row_union_nf, noise_excl_b = noise_filter.filter_nodes(row_union_raw)
    row_empty_tables, synthetic_excl_b = _exclude_synthetic_tables(db, row_union_nf)
    # Merge any additional exclusions from branch (b) into noise_excluded.
    noise_excluded = _dedup_preserve_order([*noise_excluded, *noise_excl_b, *synthetic_excl_b])
    # Ensure sources are excluded from both views.
    row_empty_tables = [t for t in row_empty_tables if t not in source_set]

    # --- Propagation order (W1) ---
    # order_tables = union of both views, sources excluded.
    order_tables_raw = _dedup_preserve_order([*row_empty_tables, *value_affected_tables])
    order_tables = [t for t in order_tables_raw if t not in source_set]

    # W1: pass _dedup_preserve_order([*start_cols, *affected_cols]) to Kahn.
    closure_col_ids = _dedup_preserve_order([*start_cols, *affected_cols])
    propagation_order, had_cycle = _kahn_topological_sort(order_tables, db, closure_col_ids)

    # Build hint.
    hints: list[str] = []
    if not_found:
        hints.append(f"Table(s) not found in graph: {', '.join(not_found)}.")
    if not order_tables and not not_found:
        hints.append("No downstream consumers — nothing propagates.")
    elif had_cycle:
        hints.append(
            "Dependency cycle detected; propagation_order is approximate (cyclic tables appended)."
        )
    if truncated:
        hints.append("Traversal stopped at 50k-node safety cap.")

    downstream_count = len(set(row_empty_tables) | set(value_affected_tables))

    return EmptyPropagationResult(
        sources=sources,
        row_empty_tables=row_empty_tables,
        value_empty_columns=value_empty_columns,
        value_affected_tables=value_affected_tables,
        value_fully_empty_tables=value_fully_empty_tables,
        value_partially_empty_tables=value_partially_empty_tables,
        propagation_order=propagation_order,
        downstream_count=downstream_count,
        noise_excluded=noise_excluded,
        truncated=truncated,
        hint=" | ".join(hints) if hints else None,
    )


@mcp.tool()
@_timed_tool("get_empty_propagation")
def get_empty_propagation(tables: list[str]) -> EmptyPropagationResult:
    """Return the downstream blast radius when one or more tables are empty.

    Answers the operational question "a job failed and left table(s) empty —
    what downstream is at risk?" using TWO complementary views:

    **View 2 (PRIMARY — column-lineage refinement)**: which downstream COLUMNS lose
    their source VALUES because they are transitively derived from the named table's
    columns (``value_empty_columns``, rolled up to ``value_affected_tables``).  This
    is the reliable headline answer — it captures CTE-wrapped derived reads.

    **View 1 (SUPPLEMENT — row reachability)**: ``row_empty_tables`` is the UNION
    of (a) View 2's affected tables and (b) tables with a DIRECT
    ``SELECTS_FROM``/``STAR_SOURCE`` read of a source (the inner-join gating case).
    KNOWN GAP: CTE-wrapped pure-gating reads are NOT detected (the parser's
    ``_real_tables`` excludes CTE-sourced names; this is a documented, accepted gap).
    UPPER BOUND: depends on join type (not recorded on edges).

    Args:
        tables: One or more qualified table names (e.g., ["ba.fact_sales"]).
                Graph keys are lowercased at index time; inputs are folded.

    Returns:
        EmptyPropagationResult with both views, propagation order, and diagnostics.

    Raises:
        NotIndexedError: If no repos have been indexed.
    """
    with _open_backend() as db:
        _assert_indexed(db)
        return _compute_empty_propagation(db, tables, max_depth=None)


# ---------------------------------------------------------------------------
# PR 2 — PR-impact detector (v1.23.0)
# ---------------------------------------------------------------------------

# Threshold for Jaccard similarity on BOTH column-set and downstream-consumer axes.
# A lost producer L and a new producer N are classified as a RENAME iff BOTH
# column-set Jaccard AND downstream-consumer Jaccard ≥ this value.
# AND-of-both-axes minimises false-rename rate: a true rename keeps same schema AND
# same consumers (~1.0 each); unrelated coincidences rarely clear 0.6 on both axes.
_RENAME_OVERLAP_THRESHOLD = 0.6


def _resolve_git_ref(root: Path, ref: str) -> str | None:
    """Resolve a git ref (branch, tag, SHA, HEAD) to a 40-char SHA.

    Returns None if git is unavailable or the ref cannot be resolved.
    Unlike the reindex CLI helper, this does NOT raise typer.Exit — callers
    convert None to a hint message.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha if sha else None
    except FileNotFoundError:
        return None


def _capture_producer_set(db: GraphBackend) -> set[str]:
    """Return the set of tables that have a feeding query in the current graph state.

    Runs ``GET_PRODUCER_TABLES`` (``SELECT DISTINCT target_table FROM "SqlQuery"
    WHERE target_table <> ''``).  Pure read; no SHA logic.

    Args:
        db: Graph backend (already open).

    Returns:
        Set of lowercased qualified table names that currently have at least one
        writing query in the graph.
    """
    rows = db.run_read(GET_PRODUCER_TABLES_QUERY, {})
    return {r["producer_table"].lower() for r in rows if r["producer_table"]}


def _get_table_consumers(db: GraphBackend, table: str) -> set[str]:
    """Return the set of tables that directly read ``table`` via SELECTS_FROM or STAR_SOURCE.

    Used for rename classification: the downstream-consumer overlap axis.
    """
    rows = db.run_read(GET_TABLE_READS_ADJACENCY_QUERY, {})
    return {r["dest_table"].lower() for r in rows if r["source_table"].lower() == table.lower()}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two sets. Returns 0.0 when both are empty."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _diff_lost_producers(
    before: set[str],
    after: set[str],
    db: GraphBackend,
    base_sha: str | None,
    head_sha: str | None,
    *,
    pre_resync_file_targets: dict[str, list[str]] | None = None,
) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    """Compute lost producers, attribution, and rename classification.

    Computes ``before − after`` (lost) and ``after − before`` (new), excludes
    synthetic via ``_exclude_synthetic_tables``, then classifies each lost
    producer against new producers using BOTH column-set AND downstream-consumer
    Jaccard ≥ ``_RENAME_OVERLAP_THRESHOLD`` (AND-combined — both axes must clear
    the threshold). Classified renames are removed from the genuine-lost set.

    Option C attribution: for each genuine lost producer, attributes which files
    dropped the producer via ``git_name_status_delta`` ``deleted ∪ modified`` paths.
    The git-delta lookup is scoped to the changed-file set only (bounded by the diff,
    not the corpus — no hot-path violation).

    Args:
        before: Producer-table set at base state.
        after:  Producer-table set at head state.
        db:     Graph backend (open at head state, for column/consumer lookups).
        base_sha: Base SHA for git-delta attribution (None → skip attribution).
        head_sha: Head SHA for git-delta attribution (None → skip attribution).

    Returns:
        (genuine_lost_producer_tables, attribution, renamed_tables) where:
        - genuine_lost_producer_tables: tables confirmed lost (renames removed), sorted.
        - attribution: dict[lost_table, list[files that dropped its producer]].
        - renamed_tables: dict[old_name, new_name] for classified renames.
    """
    from sqlcg.indexer.git_delta import git_name_status_delta

    lost_raw = before - after
    new_raw = after - before

    # Exclude synthetic from both sides.
    if lost_raw:
        lost_cleaned, _ = _exclude_synthetic_tables(db, sorted(lost_raw))
        lost_set = set(lost_cleaned)
    else:
        lost_set = set()

    if new_raw:
        new_cleaned, _ = _exclude_synthetic_tables(db, sorted(new_raw))
        new_set = set(new_cleaned)
    else:
        new_set = set()

    # --- Rename classification ---
    # For each L in lost_set, try to find a matching N in new_set with
    # BOTH column-set AND consumer Jaccard ≥ _RENAME_OVERLAP_THRESHOLD.
    renamed: dict[str, str] = {}
    matched_new: set[str] = set()  # prevent one N from matching multiple Ls

    for lost_table in sorted(lost_set):
        # Fetch L's columns from the current (head) graph.  Since L was removed
        # at head, its columns may no longer be present — use what's available.
        l_col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": lost_table})
        l_cols = {r["col_id"].rsplit(".", 1)[-1].lower() for r in l_col_rows}
        l_consumers = _get_table_consumers(db, lost_table)

        best_match: str | None = None
        for new_table in sorted(new_set - matched_new):
            n_col_rows = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": new_table})
            n_cols = {r["col_id"].rsplit(".", 1)[-1].lower() for r in n_col_rows}
            n_consumers = _get_table_consumers(db, new_table)

            col_j = _jaccard(l_cols, n_cols)
            cons_j = _jaccard(l_consumers, n_consumers)

            if col_j >= _RENAME_OVERLAP_THRESHOLD and cons_j >= _RENAME_OVERLAP_THRESHOLD:
                best_match = new_table
                break  # first match wins

        if best_match is not None:
            renamed[lost_table] = best_match
            matched_new.add(best_match)

    genuine_lost = sorted(lost_set - set(renamed.keys()))

    # --- Option C attribution ---
    # For each genuine lost producer, identify which changed files dropped it.
    # Parse the git delta: deleted ∪ modified files could have been the producer.
    # Pre-resync file targets (passed from get_pr_impact) are used first so that
    # deleted-file target tables are attributable even after resync removes them.
    attribution: dict[str, list[str]] = {t: [] for t in genuine_lost}
    if base_sha and head_sha and genuine_lost:
        root = _indexed_root(db) or Path.cwd()
        delta = git_name_status_delta(root, base_sha, head_sha)
        if delta is not None:
            changed_files = [str(p) for p in delta.deleted | delta.modified]
            genuine_lost_set = set(genuine_lost)

            # Build file → target-table map: prefer pre-resync snapshot for deleted
            # files; fall back to live graph query for modified (still-present) files.
            pre = pre_resync_file_targets or {}
            for file_path in changed_files:
                if file_path in pre:
                    file_targets = set(pre[file_path])
                else:
                    try:
                        tgt_rows = db.run_read(
                            GET_TARGET_TABLES_FOR_FILE_QUERY, {"file_path": file_path}
                        )
                        file_targets = {
                            r["table_qualified"].lower()
                            for r in tgt_rows
                            if r.get("table_qualified")
                        }
                    except Exception:  # noqa: BLE001
                        file_targets = set()
                for tbl in genuine_lost_set & file_targets:
                    attribution[tbl].append(file_path)

    return genuine_lost, attribution, renamed


def _aggregate_candidate_blast(
    per_candidate: dict[str, "EmptyPropagationResult"],
    genuine_lost: list[str],
    max_depth: int | None,
) -> "EmptyPropagationResult":
    """Aggregate pre-computed per-candidate blast radii for the genuine-lost set.

    After resync destroys the lost producers' nodes, we cannot re-run the engine.
    Instead, union the blast results that were computed pre-resync for each table
    in ``genuine_lost``.

    ``genuine_lost ⊆ per_candidate.keys()`` by construction (a lost table's
    producer file must be in ``deleted ∪ modified``).  For any table not in
    ``per_candidate`` (rare edge case), we silently skip it — still better than
    querying a post-resync graph where nodes are gone.

    Args:
        per_candidate: Map of table → blast result computed on the intact base graph.
        genuine_lost: Genuinely-lost producer tables (renames excluded).
        max_depth: Passed through to build a default empty result when needed.

    Returns:
        A merged ``EmptyPropagationResult`` for the union of all genuine-lost sources.
    """
    if not genuine_lost:
        return _compute_empty_propagation_empty(genuine_lost)

    relevant = [per_candidate[t] for t in genuine_lost if t in per_candidate]
    if not relevant:
        return _compute_empty_propagation_empty(genuine_lost)

    # Union all per-source results.
    sources: list[str] = []
    row_empty: list[str] = []
    value_cols: list[str] = []
    value_affected: list[str] = []
    value_fully: list[str] = []
    value_partially: list[str] = []
    prop_order: list[str] = []
    noise_excluded: list[str] = []
    truncated = False

    seen_sources: set[str] = set()
    seen_row: set[str] = set()
    seen_vcols: set[str] = set()
    seen_vaff: set[str] = set()
    seen_vfull: set[str] = set()
    seen_vpart: set[str] = set()
    seen_order: set[str] = set()
    seen_noise: set[str] = set()

    for r in relevant:
        for s in r.sources:
            if s not in seen_sources:
                sources.append(s)
                seen_sources.add(s)
        for t in r.row_empty_tables:
            if t not in seen_row:
                row_empty.append(t)
                seen_row.add(t)
        for c in r.value_empty_columns:
            if c not in seen_vcols:
                value_cols.append(c)
                seen_vcols.add(c)
        for t in r.value_affected_tables:
            if t not in seen_vaff:
                value_affected.append(t)
                seen_vaff.add(t)
        for t in r.value_fully_empty_tables:
            if t not in seen_vfull:
                value_fully.append(t)
                seen_vfull.add(t)
        for t in r.value_partially_empty_tables:
            if t not in seen_vpart:
                value_partially.append(t)
                seen_vpart.add(t)
        for t in r.propagation_order:
            if t not in seen_order:
                prop_order.append(t)
                seen_order.add(t)
        for t in r.noise_excluded:
            if t not in seen_noise:
                noise_excluded.append(t)
                seen_noise.add(t)
        truncated = truncated or r.truncated

    # Recompute downstream_count as the union of both view table sets.
    all_tables = seen_row | seen_vaff
    downstream_count = len(all_tables)

    # Build a composite hint.
    hints = [r.hint for r in relevant if r.hint]
    hint = " | ".join(hints) if hints else None

    return EmptyPropagationResult(
        sources=sources,
        row_empty_tables=row_empty,
        value_empty_columns=value_cols,
        value_affected_tables=value_affected,
        value_fully_empty_tables=value_fully,
        value_partially_empty_tables=value_partially,
        propagation_order=prop_order,
        downstream_count=downstream_count,
        noise_excluded=noise_excluded,
        truncated=truncated,
        hint=hint,
    )


def _compute_empty_propagation_empty(sources: list[str]) -> "EmptyPropagationResult":
    """Return an empty EmptyPropagationResult for the given source list."""
    return EmptyPropagationResult(
        sources=sources,
        downstream_count=0,
        truncated=False,
        hint="No producers lost — no data-loss blast radius.",
    )


def _reindex_to_sha(db: GraphBackend, root: Path, target_sha: str) -> bool:
    """Incrementally reindex the graph from its current SHA to *target_sha*.

    Uses ``Indexer.resync_changed`` (the same incremental path as the CLI
    ``sqlcg reindex`` command) to move the graph from the SHA it is currently
    indexed at to *target_sha*.

    Args:
        db:         An open, writable GraphBackend.
        root:       Repository root directory.
        target_sha: The git commit SHA the graph should be at after the call.

    Returns:
        ``True`` when ``db.get_indexed_sha() == target_sha`` after the reindex;
        ``False`` on any failure (exception or SHA mismatch after the call).
    """
    from_sha = db.get_indexed_sha()
    if from_sha is None:
        logger.error(
            "_reindex_to_sha: db.get_indexed_sha() returned None — cannot compute git delta"
        )
        return False
    try:
        dialect: str | None = None
        try:
            from sqlcg.core.config import get_dialect

            dialect = get_dialect(root)
        except Exception:  # noqa: BLE001
            pass

        indexer = Indexer()
        indexer.resync_changed(root, from_sha, target_sha, db, dialect)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "_reindex_to_sha: resync_changed from '%s' to '%s' raised: %s",
            from_sha,
            target_sha,
            exc,
            exc_info=True,
        )
        return False
    actual_sha = db.get_indexed_sha()
    if actual_sha != target_sha:
        logger.error(
            "_reindex_to_sha: reindex completed but get_indexed_sha()='%s' != target='%s'",
            actual_sha,
            target_sha,
        )
        return False
    return True


def _compute_pr_impact(
    db: GraphBackend,
    base_ref: str,
    max_depth: int | None,
) -> PrImpactResult:
    """Core pr-impact computation on an explicit backend handle.

    Extracted from ``get_pr_impact`` so the CLI can supply its own backend (via
    ``get_backend()``) and the MCP tool can supply its server-singleton backend
    (via ``_open_backend()``).  The logic is identical in both cases.

    **BUG-B FIX (Option i, v1.24.2 — shepherd-directed):** The blast radius is
    computed on the INTACT base graph BEFORE ``resync_changed`` advances the
    graph to HEAD.  The approach:

    1.  Before resync, build the candidate set ``C`` = producer tables whose
        generating file appears in ``deleted ∪ modified`` of the git delta.
    2.  For each candidate ``c ∈ C``, run ``_compute_empty_propagation(db, [c],
        max_depth)`` against the still-intact base graph and store the result.
    3.  Resync to HEAD; capture ``producers_after``; diff and classify renames
        (logic unchanged from PR 2).
    4.  ``genuine_lost ⊆ C`` — assemble the final blast radius by unioning the
        pre-computed per-candidate results for exactly the tables in
        ``genuine_lost``.

    **KNOWN LIMITATION:** the tool still resyncs the graph to HEAD as a side
    effect — ``db.get_indexed_sha()`` will reflect HEAD after the call.  Making
    detection non-destructive (Option ii — removing the internal resync) is a
    follow-up tracked in plan/sprints/unfilled_table_impact.md §Deviations and
    the O2 note.

    **SELF-HEALING REINDEX:** When the graph is at a different SHA than
    ``base_ref``, this function attempts to automatically reindex to ``base_sha``
    via ``_reindex_to_sha`` before running the analysis.  The full analysis
    (including the ``resync_changed`` restore to HEAD) runs after a successful
    heal, so the graph is always left at HEAD on exit.

    **KNOWN RACE — hook interference:** installed ``post-checkout`` /
    ``post-merge`` git hooks trigger background ``sqlcg reindex`` calls on every
    branch switch.  These background reindexes race ``pr-impact`` off the
    base-ref mid-analysis, defeating the self-heal.  Users who run ``pr-impact``
    frequently should consider disabling background hooks with
    ``sqlcg git uninstall-hooks`` or scheduling ``pr-impact`` runs outside
    hook-active windows.  Full hook-coordination is a follow-up.

    **CODE REGRESSION DETECTION ONLY** — ``detection_only_code_regression`` is
    always ``True``.  An edge disappears only when the SQL that produced it is
    removed, renamed, or broken.  Runtime row-count monitoring is OUT OF SCOPE.

    Args:
        db: An open GraphBackend (R/W — resync requires write access).
        base_ref: Git ref (branch, tag, or SHA) the current graph is indexed at.
        max_depth: Optional BFS depth cap for the blast-radius traversal
            (None = unbounded).

    Returns:
        PrImpactResult with lost producers, renames, attribution, and blast radius.

    Raises:
        NotIndexedError: If no repos have been indexed (via ``_assert_indexed``).
    """
    _assert_indexed(db)

    root = _indexed_root(db) or Path.cwd()

    # 1. Resolve base_ref → base_sha.
    base_sha = _resolve_git_ref(root, base_ref)
    if base_sha is None:
        return PrImpactResult(
            base_ref=base_ref,
            base_sha=None,
            head_sha=None,
            detection_only_code_regression=True,
            hint=(
                f"Could not resolve ref '{base_ref}' — ensure git is available and the ref exists."
            ),
        )

    # 2. Assert graph is indexed at base_sha; self-heal when it is not.
    indexed_sha = db.get_indexed_sha()
    if indexed_sha != base_sha:
        healed = _reindex_to_sha(db, root, base_sha)
        if not healed:
            return PrImpactResult(
                base_ref=base_ref,
                base_sha=base_sha,
                head_sha=None,
                detection_only_code_regression=True,
                hint=(
                    f"Graph is at '{indexed_sha}'; auto-reindex to '{base_sha}' failed. "
                    "Run 'sqlcg reindex --from HEAD --to <base>' manually, then retry."
                ),
            )

    # 3. Capture producers_before.
    producers_before = _capture_producer_set(db)

    # 4. Resolve HEAD.
    head_sha = _resolve_git_ref(root, "HEAD")
    if head_sha is None:
        return PrImpactResult(
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=None,
            detection_only_code_regression=True,
            hint="Could not resolve HEAD SHA — ensure git is available.",
        )

    if head_sha == base_sha:
        # No change — nothing to detect.
        return PrImpactResult(
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            detection_only_code_regression=True,
            hint="HEAD is the same as base_ref — no changes to detect.",
            blast_radius=_compute_empty_propagation(db, [], max_depth),
        )

    # Read dialect from the stored .sqlcg.toml for the repo root.
    try:
        from sqlcg.core.config import get_dialect

        dialect: str | None = get_dialect(root)
    except Exception:  # noqa: BLE001
        dialect = None

    # Pre-resync: capture file → target_table map for the changed-file set so
    # attribution can find which file wrote each lost producer (even for deleted
    # files whose graph entries will be removed by resync_changed).
    from sqlcg.indexer.git_delta import git_name_status_delta as _gns_delta

    pre_resync_file_targets: dict[str, list[str]] = {}
    pre_delta = _gns_delta(root, base_sha, head_sha)
    if pre_delta is not None:
        changed_files_pre = [str(p) for p in pre_delta.deleted | pre_delta.modified]
        for fp in changed_files_pre:
            try:
                rows = db.run_read(GET_TARGET_TABLES_FOR_FILE_QUERY, {"file_path": fp})
                pre_resync_file_targets[fp] = [
                    r["table_qualified"].lower() for r in rows if r.get("table_qualified")
                ]
            except Exception:  # noqa: BLE001
                pre_resync_file_targets[fp] = []

    # BUG-B FIX (Option i) — Step 2: compute blast radius per candidate on the
    # still-intact base graph BEFORE resync destroys the lost producer's nodes.
    # Candidate set C = union of pre_resync_file_targets values (producer tables
    # whose generating file is in deleted ∪ modified).
    candidate_set: set[str] = set()
    for targets in pre_resync_file_targets.values():
        candidate_set.update(targets)

    # Per-candidate blast radius on the intact base graph.
    per_candidate_blast: dict[str, EmptyPropagationResult] = {}
    for candidate in sorted(candidate_set):
        per_candidate_blast[candidate] = _compute_empty_propagation(db, [candidate], max_depth)

    # 4b. Orchestrate resync to HEAD (graph-only; advances indexed_sha to HEAD).
    indexer = Indexer()
    indexer.resync_changed(root, base_sha, head_sha, db, dialect)

    # 5. Capture producers_after.
    producers_after = _capture_producer_set(db)

    # 6. Diff. Pass pre_resync_file_targets for attribution (deleted files).
    genuine_lost, attribution, renamed = _diff_lost_producers(
        producers_before,
        producers_after,
        db,
        base_sha,
        head_sha,
        pre_resync_file_targets=pre_resync_file_targets,
    )

    # 7. Assemble blast radius from pre-computed per-candidate results for exactly
    # the tables in genuine_lost.  genuine_lost ⊆ candidate_set by construction
    # (a lost table's base producer file must be in deleted ∪ modified).
    # For tables not in candidate_set (edge case: modified file still had a query
    # writing the table, but that query disappeared — rare), fall back to an empty
    # radius so we never run the engine on the post-resync graph where nodes are gone.
    blast = _aggregate_candidate_blast(per_candidate_blast, genuine_lost, max_depth)

    # Build hint.
    hints: list[str] = []
    if not genuine_lost and not renamed:
        hints.append("No producers lost — no data-loss blast radius.")
    elif not genuine_lost and renamed:
        hints.append(
            f"{len(renamed)} table(s) renamed (see renamed_tables) — no genuine data loss detected."
        )
    hints.append(
        "DETECTION ONLY: this tool detects code regressions (SQL removed/broken), "
        "NOT runtime emptiness (a job that produces 0 rows while SQL is unchanged)."
    )

    return PrImpactResult(
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=head_sha,
        lost_producer_tables=genuine_lost,
        renamed_tables=renamed,
        attribution=attribution,
        blast_radius=blast,
        detection_only_code_regression=True,
        hint=" | ".join(hints),
    )


@mcp.tool()
@_timed_tool("get_pr_impact")
def get_pr_impact(base_ref: str, max_depth: int | None = None) -> PrImpactResult:
    """Detect tables that lose their producer between ``base_ref`` and HEAD, then
    return the blast-radius (PR 1 engine) for those genuinely lost producers.

    **CODE REGRESSION DETECTION ONLY** — ``detection_only_code_regression`` is always
    ``True``.  An edge disappears only when the SQL that produced it is removed,
    renamed, or broken.  If a job runs and produces 0 rows while the SQL is unchanged,
    NO edge disappears and this tool detects nothing.  Runtime row-count monitoring is
    OUT OF SCOPE.

    Rename handling: raw ``producers(base) − producers(head)`` would cry wolf on every
    rename.  This tool classifies renames using BOTH column-set AND downstream-consumer
    Jaccard ≥ 0.6 (AND-combined) and puts them in ``renamed_tables`` (excluded from
    the blast radius).  This is a heuristic — callers should verify ``renamed_tables``.

    **BUG-B KNOWN LIMITATION (v1.24.2):** the blast radius is computed on the intact
    base graph (before resync) via the pre-candidate approach (Option i).  The tool
    still resyncs the graph to HEAD as a side effect — making detection non-destructive
    (Option ii, dropping the internal resync) is a follow-up.

    Args:
        base_ref: Git ref (branch, tag, or SHA) that the current graph is indexed at.
        max_depth: Optional BFS depth cap for the blast-radius traversal (None = unbounded).

    Returns:
        PrImpactResult with lost producers, renames, attribution, and blast radius.

    Raises:
        NotIndexedError: If no repos have been indexed.
    """
    with _open_backend() as db:
        return _compute_pr_impact(db, base_ref, max_depth)
