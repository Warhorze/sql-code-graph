"""DuckDB implementation of GraphBackend.

Replaces KuzuDB with a relational model: one table per node label, one table
per edge type (mirroring core/schema.py). Lineage traversal uses recursive CTEs
with a cycle guard and bounded depth — proven equivalent to Kuzu on the real DWH
corpus (Phase 0, 2026-06-05).

Concurrency contract (DuckDB single-process MVCC):
  - One R/W connection held for the process lifetime (no RO/RW escalation needed).
  - DuckDB MVCC guarantees no torn reads: a read never observes a partially-applied
    rebuild — it sees either the prior committed graph or the post-COMMIT graph.
  - NOTE: in the server, reads and the write drain are serialized through a single
    ``backend_lock`` on the shared connection (server.py / writer.py), so a read
    issued *during* a rebuild waits for that rebuild to finish rather than being
    served the old snapshot concurrently. The reindex path is fast (seconds); a
    full re-index blocks reads for its duration. A cursor-per-read path to deliver
    true non-blocking reads during a rebuild is a tracked follow-up (v1.4.x).
  - Cross-process: whichever process opens the file first holds an exclusive lock;
    other processes cannot open it at all (even read-only). This is handled by the
    existing socket-routing layer (read_client.py / server.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import duckdb

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.schema import SCHEMA_VERSION, NodeLabel, RelType
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL — one CREATE TABLE per node label + edge type
# ---------------------------------------------------------------------------

_NODE_DDLS = [
    # --- node tables ---
    """
    CREATE TABLE IF NOT EXISTS "Repo" (
        path VARCHAR PRIMARY KEY,
        name VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "File" (
        path VARCHAR PRIMARY KEY,
        repo_path VARCHAR,
        sha VARCHAR,
        dialect VARCHAR,
        parse_failed BOOLEAN,
        parse_cause VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SqlTable" (
        qualified VARCHAR PRIMARY KEY,
        catalog VARCHAR,
        db VARCHAR,
        name VARCHAR,
        kind VARCHAR,
        defined_in_file VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SqlColumn" (
        id VARCHAR PRIMARY KEY,
        catalog VARCHAR,
        db VARCHAR,
        table_name VARCHAR,
        col_name VARCHAR,
        table_qualified VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SqlQuery" (
        id VARCHAR PRIMARY KEY,
        file_path VARCHAR,
        statement_index BIGINT,
        sql VARCHAR,
        kind VARCHAR,
        target_table VARCHAR,
        parse_failed BOOLEAN,
        confidence FLOAT,
        parsing_mode VARCHAR,
        start_line BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SchemaVersion" (
        version VARCHAR PRIMARY KEY,
        indexed_sha VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "ExternalConsumer" (
        name VARCHAR PRIMARY KEY,
        consumer_type VARCHAR
    )
    """,
]

_EDGE_DDLS = [
    """
    CREATE TABLE IF NOT EXISTS "BELONGS_TO" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "DEFINED_IN" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "QUERY_DEFINED_IN" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "HAS_COLUMN" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        source VARCHAR,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SELECTS_FROM" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "INSERTS_INTO" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "DELETES_FROM" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "UPDATES" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "COLUMN_LINEAGE" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        transform VARCHAR,
        confidence FLOAT,
        query_id VARCHAR,
        inferred_from_source_name BOOLEAN,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "DECLARES" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "STAR_SOURCE" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        qualifier VARCHAR,
        target_table VARCHAR,
        confidence FLOAT,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "CONSUMED_BY" (
        src_key VARCHAR NOT NULL,
        dst_key VARCHAR NOT NULL,
        PRIMARY KEY (src_key, dst_key)
    )
    """,
]

_INDEX_DDLS = [
    'CREATE INDEX IF NOT EXISTS idx_BELONGS_TO_src ON "BELONGS_TO" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_BELONGS_TO_dst ON "BELONGS_TO" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_DEFINED_IN_src ON "DEFINED_IN" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_DEFINED_IN_dst ON "DEFINED_IN" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_QUERY_DEFINED_IN_src ON "QUERY_DEFINED_IN" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_QUERY_DEFINED_IN_dst ON "QUERY_DEFINED_IN" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_HAS_COLUMN_src ON "HAS_COLUMN" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_HAS_COLUMN_dst ON "HAS_COLUMN" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_SELECTS_FROM_src ON "SELECTS_FROM" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_SELECTS_FROM_dst ON "SELECTS_FROM" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_INSERTS_INTO_src ON "INSERTS_INTO" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_INSERTS_INTO_dst ON "INSERTS_INTO" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_DELETES_FROM_src ON "DELETES_FROM" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_DELETES_FROM_dst ON "DELETES_FROM" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_UPDATES_src ON "UPDATES" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_UPDATES_dst ON "UPDATES" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_COLUMN_LINEAGE_src ON "COLUMN_LINEAGE" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_COLUMN_LINEAGE_dst ON "COLUMN_LINEAGE" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_DECLARES_src ON "DECLARES" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_DECLARES_dst ON "DECLARES" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_STAR_SOURCE_src ON "STAR_SOURCE" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_STAR_SOURCE_dst ON "STAR_SOURCE" (dst_key)',
    'CREATE INDEX IF NOT EXISTS idx_CONSUMED_BY_src ON "CONSUMED_BY" (src_key)',
    'CREATE INDEX IF NOT EXISTS idx_CONSUMED_BY_dst ON "CONSUMED_BY" (dst_key)',
]

# Node label → column list (all columns of that table, in order)
_NODE_COLUMNS: dict[str, list[str]] = {
    NodeLabel.REPO: ["path", "name"],
    NodeLabel.FILE: ["path", "repo_path", "sha", "dialect", "parse_failed", "parse_cause"],
    NodeLabel.TABLE: ["qualified", "catalog", "db", "name", "kind", "defined_in_file"],
    NodeLabel.COLUMN: ["id", "catalog", "db", "table_name", "col_name", "table_qualified"],
    NodeLabel.QUERY: [
        "id",
        "file_path",
        "statement_index",
        "sql",
        "kind",
        "target_table",
        "parse_failed",
        "confidence",
        "parsing_mode",
        "start_line",
    ],
    NodeLabel.SCHEMA_VERSION: ["version", "indexed_sha"],
    NodeLabel.EXTERNAL_CONSUMER: ["name", "consumer_type"],
}

# Edge rel type → extra property columns beyond (src_key, dst_key)
_EDGE_EXTRA_COLUMNS: dict[str, list[str]] = {
    RelType.BELONGS_TO: [],
    RelType.DEFINED_IN: [],
    RelType.QUERY_DEFINED_IN: [],
    RelType.HAS_COLUMN: ["source"],
    RelType.SELECTS_FROM: [],
    RelType.INSERTS_INTO: [],
    RelType.DELETES_FROM: [],
    RelType.UPDATES: [],
    RelType.COLUMN_LINEAGE: ["transform", "confidence", "query_id", "inferred_from_source_name"],
    RelType.DECLARES: [],
    RelType.STAR_SOURCE: ["qualifier", "target_table", "confidence"],
    RelType.CONSUMED_BY: [],
}


class DuckDBBackend(GraphBackend):
    """DuckDB implementation of the graph database backend.

    Uses a single R/W connection for the process lifetime. DuckDB's MVCC
    provides concurrent read-safety during write transactions: readers see
    the last committed state until COMMIT flips the new graph atomically.

    ``init_schema`` is idempotent (CREATE IF NOT EXISTS everywhere).
    ``transaction`` provides real BEGIN/COMMIT/ROLLBACK semantics —
    overriding the ABC no-op (ARCHITECTURE_REVIEW §3.1, HIGH).
    """

    def __init__(self, db_path: str) -> None:
        """Open (or create) a DuckDB database at *db_path*.

        Args:
            db_path: Path to the DuckDB file, or ``':memory:'`` for in-memory.

        Raises:
            duckdb.IOException: If the file is already held by another process.
        """
        self._db_path = db_path
        self._txn_depth = 0
        try:
            self._conn = duckdb.connect(db_path)
        except duckdb.IOException as exc:
            exc_str = str(exc)
            if "No such file or directory" in exc_str or "cannot open" in exc_str.lower():
                # Parent directory does not exist — database was never initialized.
                raise RuntimeError(
                    f"Database path does not exist: {db_path}. "
                    f"Run 'sqlcg db init' then 'sqlcg index <path>' to initialize."
                ) from exc
            msg = (
                f"Database is locked — another sqlcg process is running. "
                f"Wait for it to finish or stop it with: sqlcg mcp stop\n"
                f"(DuckDB path: {db_path})"
            )
            raise duckdb.IOException(msg) from exc

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create all node/edge tables and indexes (idempotent, one transaction).

        CREATE … IF NOT EXISTS everywhere so re-calling after schema exists
        is a no-op.  Wrapped in one transaction so a partial failure rolls
        back cleanly (ARCHITECTURE_REVIEW §3.1).
        """
        with self.transaction():
            for ddl in _NODE_DDLS:
                self._conn.execute(ddl)
            for ddl in _EDGE_DDLS:
                self._conn.execute(ddl)
            for ddl in _INDEX_DDLS:
                self._conn.execute(ddl)
            # Upsert the schema version row.
            self._conn.execute(
                'INSERT OR REPLACE INTO "SchemaVersion" (version, indexed_sha) '
                "VALUES (?, COALESCE("
                '  (SELECT indexed_sha FROM "SchemaVersion" WHERE version = ?), NULL'
                "))",
                [SCHEMA_VERSION, SCHEMA_VERSION],
            )
        logger.debug("DuckDB schema initialized (version %s)", SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Single-row upsert helpers (used by reindex_file / tests)
    # ------------------------------------------------------------------

    def upsert_node(self, label: str, key: str, properties: dict[str, Any]) -> None:
        """Upsert one node (MERGE semantics via INSERT OR REPLACE)."""
        self._validate_props(properties)
        pk = self._pk_field(label)
        cols = _NODE_COLUMNS.get(label)
        if cols is None:
            raise ValueError(f"Unknown node label: {label!r}")
        row = {pk: key}
        row.update(properties)
        # Build a full row with None for missing columns
        values = [row.get(c) for c in cols]
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(f'"{c}"' for c in cols)
        self._conn.execute(
            f'INSERT OR REPLACE INTO "{label}" ({col_list}) VALUES ({placeholders})',
            values,
        )

    def upsert_edge(
        self,
        src_label: str,
        src_key: str,
        dst_label: str,
        dst_key: str,
        rel_type: str,
        properties: dict[str, Any],
    ) -> None:
        """Upsert one edge (MERGE semantics via INSERT OR REPLACE)."""
        self._validate_props(properties)
        extra = _EDGE_EXTRA_COLUMNS.get(rel_type, [])
        all_cols = ["src_key", "dst_key"] + extra
        values = [src_key, dst_key] + [properties.get(c) for c in extra]
        placeholders = ", ".join("?" * len(all_cols))
        col_list = ", ".join(f'"{c}"' for c in all_cols)
        self._conn.execute(
            f'INSERT OR REPLACE INTO "{rel_type}" ({col_list}) VALUES ({placeholders})',
            values,
        )

    # ------------------------------------------------------------------
    # Bulk upsert (performance-critical path — CLAUDE.md perf invariant)
    # ------------------------------------------------------------------

    def upsert_nodes_bulk(self, label: str, rows: list[dict[str, Any]]) -> None:
        """Bulk-upsert nodes of one label in a single backend call.

        Uses INSERT OR REPLACE with unnest() — one execute() per label per batch.
        This is the bulk-upsert invariant from CLAUDE.md.
        """
        if not rows:
            return
        for row in rows:
            self._validate_props(row)

        cols = _NODE_COLUMNS.get(label)
        if cols is None:
            raise ValueError(f"Unknown node label: {label!r}")

        # Require homogeneous rows including the primary key — a heterogeneous
        # batch signals an upstream bug (the row builder dropped/added a field).
        pk_field = self._pk_field(label)
        keys = set(rows[0].keys())
        if pk_field not in keys:
            raise ValueError(
                f"upsert_nodes_bulk({label}): every row must include primary key '{pk_field}'"
            )
        for i, row in enumerate(rows[1:], 1):
            if set(row.keys()) != keys:
                raise ValueError(
                    f"upsert_nodes_bulk({label}): row {i} has property keys "
                    f"{sorted(row.keys())}, expected {sorted(keys)}"
                )

        # Build column arrays from the row list (None for missing fields).
        # DuckDB: INSERT OR REPLACE via unnest(?::VARCHAR[]) — one execute() per label
        # per batch (CLAUDE.md perf invariant: _flush_row_batch calls each bulk once).
        arrays = [[row.get(c) for row in rows] for c in cols]
        col_list = ", ".join(f'"{c}"' for c in cols)
        unnest_cols = ", ".join(f'unnest(?::VARCHAR[]) AS "{c}"' for c in cols)

        if label == NodeLabel.TABLE:
            # P1a (DWH 2026-06-09): guard against cross-batch kind downgrade.
            # A CTAS target written as kind='table' in one batch must not be overwritten
            # with kind='derived' when a later batch processes an INSERT into that table
            # and canonical_by_bare cannot resolve it.  The in-batch dedup in
            # _flush_row_batch already excludes 'derived' from upgrading 'table' within
            # a batch; this ON CONFLICT guard extends that rule across batch boundaries
            # for rows already committed to the database.
            # defined_in_file is similarly preserved: a row with DDL provenance keeps
            # its defined_in_file even when a later reference row arrives with ''.
            non_pk_cols = [c for c in cols if c != "qualified"]
            # Build per-column SET clauses; kind and defined_in_file get special guards.
            set_parts: list[str] = []
            for c in non_pk_cols:
                if c == "kind":
                    set_parts.append(
                        '"kind" = CASE '
                        "WHEN \"SqlTable\".\"kind\" IN ('table', 'view') "
                        "AND EXCLUDED.\"kind\" = 'derived' "
                        'THEN "SqlTable"."kind" '
                        'ELSE EXCLUDED."kind" END'
                    )
                elif c == "defined_in_file":
                    set_parts.append(
                        '"defined_in_file" = CASE '
                        "WHEN EXCLUDED.\"defined_in_file\" = '' "
                        'THEN "SqlTable"."defined_in_file" '
                        'ELSE EXCLUDED."defined_in_file" END'
                    )
                else:
                    set_parts.append(f'"{c}" = EXCLUDED."{c}"')
            set_clause = ", ".join(set_parts)
            self._conn.execute(
                f'INSERT INTO "{label}" ({col_list}) SELECT {unnest_cols} '
                f'ON CONFLICT ("qualified") DO UPDATE SET {set_clause}',
                arrays,
            )
        else:
            self._conn.execute(
                f'INSERT OR REPLACE INTO "{label}" ({col_list}) SELECT {unnest_cols}',
                arrays,
            )

    def upsert_edges_bulk(
        self,
        src_label: str,
        dst_label: str,
        rel_type: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Bulk-upsert edges of one (src_label, rel_type, dst_label) triple.

        One execute() per rel_type per batch — bulk-upsert invariant from CLAUDE.md.
        """
        if not rows:
            return
        for row in rows:
            props = {k: v for k, v in row.items() if k not in ("src_key", "dst_key")}
            self._validate_props(props)

        # Every edge row must carry src_key and dst_key, and the batch must be
        # homogeneous — guards against a malformed row reaching the NOT NULL
        # constraint as an opaque ConstraintException.
        keys = set(rows[0].keys())
        for required in ("src_key", "dst_key"):
            if required not in keys:
                raise ValueError(
                    f"upsert_edges_bulk({src_label}->{rel_type}->{dst_label}): "
                    f"every row must include '{required}'"
                )
        for i, row in enumerate(rows[1:], 1):
            if set(row.keys()) != keys:
                raise ValueError(
                    f"upsert_edges_bulk: row {i} has property keys {sorted(row.keys())}, "
                    f"expected {sorted(keys)}"
                )

        extra = _EDGE_EXTRA_COLUMNS.get(rel_type, [])
        all_cols = ["src_key", "dst_key"] + extra
        arrays = [[row.get(c) for row in rows] for c in all_cols]
        col_list = ", ".join(f'"{c}"' for c in all_cols)
        unnest_cols = ", ".join(f'unnest(?::VARCHAR[]) AS "{c}"' for c in all_cols)
        self._conn.execute(
            f'INSERT OR REPLACE INTO "{rel_type}" ({col_list}) SELECT {unnest_cols}',
            arrays,
        )

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    @staticmethod
    def _params_list(params: dict[str, Any]) -> list[Any]:
        """Convert a params dict to a positional list for DuckDB's ? placeholder.

        DuckDB expects positional parameters as a list.  Callers that use
        named-param dicts (``{"name": value}``) get values in insertion order
        (Python 3.7+ dicts).  Callers that need a raw list pass ``{0: v, …}``
        with integer keys — we return ``list(params.values())`` in both cases.
        """
        return list(params.values())

    def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read-only SQL query and return results as list of dicts."""
        try:
            if params:
                result = self._conn.execute(query, self._params_list(params))
            else:
                result = self._conn.execute(query)
            columns = [desc[0] for desc in result.description or []]
            return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
        except Exception as exc:
            logger.error("run_read failed: %s", exc)
            raise

    def run_write(self, query: str, params: dict[str, Any]) -> None:
        """Execute a write SQL statement."""
        try:
            if params:
                self._conn.execute(query, self._params_list(params))
            else:
                self._conn.execute(query)
        except Exception as exc:
            logger.error("run_write failed: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Delete helpers
    # ------------------------------------------------------------------

    def delete_nodes_for_file(self, file_path: str) -> None:
        """Delete all nodes and edges for *file_path*.

        Deletion order:
          A. SqlColumn nodes for tables DEFINED_IN this file
          B. SqlQuery nodes QUERY_DEFINED_IN this file (all their edges via cascades
             implemented as explicit DELETE on each edge table)
          C. SqlTable nodes DEFINED_IN this file
          D. The File node itself

        DuckDB has no DETACH DELETE — we delete edge rows first, then node rows.
        """
        try:
            # A: columns for tables defined in this file
            self._conn.execute(
                'DELETE FROM "SqlColumn" WHERE id IN ('
                '  SELECT dst_key FROM "HAS_COLUMN" WHERE src_key IN ('
                '    SELECT src_key FROM "DEFINED_IN" WHERE dst_key = ?'
                "  )"
                ")",
                [file_path],
            )
            # Remove HAS_COLUMN edges for those tables
            self._conn.execute(
                'DELETE FROM "HAS_COLUMN" WHERE src_key IN ('
                '  SELECT src_key FROM "DEFINED_IN" WHERE dst_key = ?'
                ")",
                [file_path],
            )

            # B: query edges + query nodes
            # Collect all query IDs for this file (via file_path column or QUERY_DEFINED_IN edge).
            query_ids_result = self._conn.execute(
                'SELECT id FROM "SqlQuery" WHERE file_path = ?'
                "  UNION"
                '  SELECT src_key FROM "QUERY_DEFINED_IN" WHERE dst_key = ?',
                [file_path, file_path],
            )
            query_ids = [row[0] for row in query_ids_result.fetchall()]

            if query_ids:
                placeholders = ", ".join("?" * len(query_ids))
                for edge_table in (
                    "SELECTS_FROM",
                    "INSERTS_INTO",
                    "DELETES_FROM",
                    "UPDATES",
                    "DECLARES",
                    "STAR_SOURCE",
                ):
                    self._conn.execute(
                        f'DELETE FROM "{edge_table}" WHERE src_key IN ({placeholders})',
                        query_ids,
                    )
                # COLUMN_LINEAGE edges referencing queries from this file
                self._conn.execute(
                    f'DELETE FROM "COLUMN_LINEAGE" WHERE query_id IN ({placeholders})',
                    query_ids,
                )
            # Remove QUERY_DEFINED_IN edges for this file
            self._conn.execute(
                'DELETE FROM "QUERY_DEFINED_IN" WHERE dst_key = ?',
                [file_path],
            )
            # Delete query nodes
            if query_ids:
                self._conn.execute(
                    f'DELETE FROM "SqlQuery" WHERE id IN ({placeholders})',
                    query_ids,
                )

            # C: table nodes + their DEFINED_IN edges
            # Remove SqlTable nodes that are defined in (or linked via DEFINED_IN to) this file.
            # We look at both the DEFINED_IN edge table and the defined_in_file column.
            self._conn.execute(
                'DELETE FROM "SqlTable" WHERE qualified IN ('
                '  SELECT src_key FROM "DEFINED_IN" WHERE dst_key = ?'
                "  UNION"
                '  SELECT qualified FROM "SqlTable" WHERE defined_in_file = ?'
                ")",
                [file_path, file_path],
            )
            self._conn.execute(
                'DELETE FROM "DEFINED_IN" WHERE dst_key = ?',
                [file_path],
            )

            # D: File node
            self._conn.execute('DELETE FROM "File" WHERE path = ?', [file_path])

            logger.debug("delete_nodes_for_file: %s", file_path)
        except Exception as exc:
            logger.error("delete_nodes_for_file failed for %s: %s", file_path, exc)
            raise

    # ------------------------------------------------------------------
    # Schema version / SHA tracking
    # ------------------------------------------------------------------

    def get_schema_version(self) -> str | None:
        """Return the stored schema version, or None."""
        try:
            result = self._conn.execute('SELECT version FROM "SchemaVersion" LIMIT 1')
            row = result.fetchone()
            return row[0] if row else None
        except Exception as exc:
            logger.warning("Failed to read schema version: %s", exc)
            return None

    def set_indexed_sha(self, sha: str) -> None:
        """Persist the git SHA of the last successful index."""
        try:
            self._conn.execute(
                'INSERT OR REPLACE INTO "SchemaVersion" (version, indexed_sha) VALUES (?, ?)',
                [SCHEMA_VERSION, sha],
            )
        except Exception as exc:
            logger.warning("Failed to write indexed_sha: %s", exc)

    def get_indexed_sha(self) -> str | None:
        """Return the stored git SHA, or None."""
        try:
            result = self._conn.execute('SELECT indexed_sha FROM "SchemaVersion" LIMIT 1')
            row = result.fetchone()
            return row[0] if row else None
        except Exception as exc:
            logger.warning("Failed to read indexed_sha: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Star expansion (replaces Cypher MERGE-based EXPAND_STAR_SOURCES)
    # ------------------------------------------------------------------

    def expand_star_sources(self) -> int:
        """Run the post-ingestion star expansion in three DML steps.

        Replaces the single Kuzu EXPAND_STAR_SOURCES Cypher query which used
        MERGE.  DuckDB uses INSERT OR REPLACE (idempotent) across three steps:
          1. Upsert destination SqlColumn nodes.
          2. Upsert HAS_COLUMN edges for the destination columns.
          3. Upsert COLUMN_LINEAGE edges with transform='STAR_EXPANSION'.

        Returns:
            Number of COLUMN_LINEAGE STAR_EXPANSION edges present after expansion.
        """
        from sqlcg.core.queries import (
            EXPAND_STAR_SOURCES_HAS_COLUMN_QUERY,
            EXPAND_STAR_SOURCES_LINEAGE_QUERY,
            EXPAND_STAR_SOURCES_QUERY,
        )

        self._conn.execute(EXPAND_STAR_SOURCES_QUERY)
        self._conn.execute(EXPAND_STAR_SOURCES_HAS_COLUMN_QUERY)
        self._conn.execute(EXPAND_STAR_SOURCES_LINEAGE_QUERY)
        result = self._conn.execute(
            'SELECT count(*) AS n FROM "COLUMN_LINEAGE" WHERE transform = ?',
            ["STAR_EXPANSION"],
        )
        row = result.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Full-rebuild helpers
    # ------------------------------------------------------------------

    def clear_all_tables(self) -> None:
        """Delete all node and edge rows, preserving the schema structure.

        Used by the reindex drain body (Phase 4): called inside a transaction
        before re-inserting all rows so readers on MVCC snapshots see the old
        graph until COMMIT flips to the new one atomically.

        ``SchemaVersion`` is preserved (its ``indexed_sha`` is updated by the
        indexer after the rebuild; its ``version`` row keeps the schema gate
        intact across re-opens).  All other node and edge tables are truncated.
        """
        node_tables = [
            "Repo",
            "File",
            "SqlTable",
            "SqlColumn",
            "SqlQuery",
            "ExternalConsumer",
        ]
        edge_tables = [
            "BELONGS_TO",
            "DEFINED_IN",
            "QUERY_DEFINED_IN",
            "HAS_COLUMN",
            "SELECTS_FROM",
            "INSERTS_INTO",
            "DELETES_FROM",
            "UPDATES",
            "COLUMN_LINEAGE",
            "DECLARES",
            "STAR_SOURCE",
            "CONSUMED_BY",
        ]
        for tbl in edge_tables + node_tables:
            self._conn.execute(f'DELETE FROM "{tbl}"')
        logger.debug("DuckDBBackend: all node/edge tables cleared")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the DuckDB connection."""
        try:
            self._conn.close()
            logger.debug("DuckDBBackend connection closed")
        except Exception as exc:
            logger.error("Error closing DuckDBBackend: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Transaction
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[DuckDBBackend]:
        """Real BEGIN … COMMIT / ROLLBACK transaction context manager.

        Overrides the ABC no-op (ARCHITECTURE_REVIEW §3.1, HIGH).
        DuckDB DDL is transactional so schema creation in init_schema
        is also covered.

        Reentrant: DuckDB rejects ``BEGIN`` inside an open transaction, so a
        nested call is a no-op that joins the outer transaction — the
        outermost level owns COMMIT/ROLLBACK.  This lets the drain body wrap
        ``clear_all_tables()`` + ``index_repo()`` (which opens its own
        per-batch transactions) in one atomic rebuild (Phase 4, C3/C6).
        """
        if self._txn_depth > 0:
            # Joined the outer transaction — an exception propagates out and
            # the outermost level rolls everything back.
            self._txn_depth += 1
            try:
                yield self
            finally:
                self._txn_depth -= 1
            return
        self._conn.execute("BEGIN")
        self._txn_depth = 1
        try:
            yield self
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as rb_err:
                logger.debug("ROLLBACK failed: %s", rb_err)
            raise
        finally:
            self._txn_depth = 0
