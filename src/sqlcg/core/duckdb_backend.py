"""DuckDB implementation of GraphBackend.

Replaces KuzuDB with a relational model: one table per node label, one table
per edge type (mirroring core/schema.py). Lineage traversal uses recursive CTEs
with a cycle guard and bounded depth — proven equivalent to Kuzu on the real DWH
corpus (Phase 0, 2026-06-05).

Concurrency contract (DuckDB single-process MVCC):
  - One R/W connection held for the process lifetime (no RO/RW escalation needed).
  - Readers see a consistent snapshot of committed data (MVCC) — never blocked by
    an in-flight write transaction.
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
    RelType.COLUMN_LINEAGE: ["transform", "confidence", "query_id"],
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
        try:
            self._conn = duckdb.connect(db_path)
        except duckdb.IOException as exc:
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

        # Build column arrays from the row list (None for missing fields).
        # DuckDB: INSERT OR REPLACE via unnest(?::VARCHAR[]) — one execute() per label
        # per batch (CLAUDE.md perf invariant: _flush_row_batch calls each bulk once).
        arrays = [[row.get(c) for row in rows] for c in cols]
        col_list = ", ".join(f'"{c}"' for c in cols)
        unnest_cols = ", ".join(f'unnest(?::VARCHAR[]) AS "{c}"' for c in cols)
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

        Deletion order (mirrors kuzu_backend.py):
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
            # Remove edges where queries from this file are src or dst
            for edge_table in (
                "SELECTS_FROM",
                "INSERTS_INTO",
                "DELETES_FROM",
                "UPDATES",
                "DECLARES",
                "STAR_SOURCE",
                "QUERY_DEFINED_IN",
            ):
                self._conn.execute(
                    f'DELETE FROM "{edge_table}" WHERE src_key IN ('
                    f'  SELECT id FROM "SqlQuery" WHERE file_path = ?'
                    f")",
                    [file_path],
                )
            # COLUMN_LINEAGE edges referencing queries from this file
            self._conn.execute(
                'DELETE FROM "COLUMN_LINEAGE" WHERE query_id IN ('
                '  SELECT id FROM "SqlQuery" WHERE file_path = ?'
                ")",
                [file_path],
            )
            # Delete query nodes
            self._conn.execute(
                'DELETE FROM "SqlQuery" WHERE file_path = ?',
                [file_path],
            )

            # C: table nodes + their DEFINED_IN edges
            self._conn.execute(
                'DELETE FROM "DEFINED_IN" WHERE dst_key = ?',
                [file_path],
            )
            # Note: SqlTable nodes that are NOT defined in other files should be removed.
            # We only remove tables whose ONLY definition is this file. Tables referenced
            # from other files but defined here: we remove the DEFINED_IN edge above, which
            # clears the DDL provenance. The SqlTable node stays for cross-file references.
            # This mirrors Kuzu's DETACH DELETE semantics on the DEFINED_IN match.
            self._conn.execute(
                'DELETE FROM "SqlTable" WHERE qualified IN ('
                '  SELECT t.qualified FROM "SqlTable" t '
                "  WHERE t.defined_in_file = ?"
                ")",
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
        """
        self._conn.execute("BEGIN")
        try:
            yield self
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as rb_err:
                logger.debug("ROLLBACK failed: %s", rb_err)
            raise
