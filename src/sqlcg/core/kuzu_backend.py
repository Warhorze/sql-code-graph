"""KùzuDB implementation of GraphBackend."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import kuzu

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.queries import (
    DELETE_COLUMNS_FOR_FILE,
    DELETE_FILE,
    DELETE_QUERIES_FOR_FILE,
    DELETE_TABLES_FOR_FILE,
)
from sqlcg.core.schema import (
    NODE_REPO,
    SCHEMA_DDL,
    SCHEMA_VERSION,
)
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


def _find_lock_holder(db_path: str) -> str:
    """Return a human-readable PID string for the process holding the DB lock.

    Uses lsof on Linux/macOS. Returns a descriptive fallback if lsof is
    unavailable or returns no results.
    """
    import shutil
    import subprocess

    if not shutil.which("lsof"):
        return "PID unknown (lsof not available)"
    try:
        result = subprocess.run(
            ["lsof", "-t", db_path],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pids = result.stdout.strip().split()
        if pids:
            return f"PID {', '.join(pids)}"
    except Exception:
        pass
    return "PID unknown"


class KuzuBackend(GraphBackend):
    """KùzuDB implementation of the graph database backend."""

    def __init__(self, db_path: str, buffer_pool_size_mb: int = 0):
        """Initialize KùzuDB backend.

        Args:
            db_path: Path to the KùzuDB database file (or ':memory:' for in-memory)
            buffer_pool_size_mb: Buffer pool size in MB (0 = use KuzuDB default)

        Raises:
            RuntimeError: If the database is locked or cannot be opened.
        """
        self._db_path = db_path
        try:
            kwargs = {}
            if buffer_pool_size_mb > 0:
                kwargs["buffer_pool_size"] = buffer_pool_size_mb * 1024 * 1024
            self._db = kuzu.database.Database(db_path, **kwargs)
        except RuntimeError as exc:
            if "Could not set lock" in str(exc) or "lock" in str(exc).lower():
                # Attempt to find the holding PID via lsof
                pid_hint = _find_lock_holder(db_path)
                pid_str = pid_hint.split()[-1] if pid_hint else "<PID>"
                msg = (
                    f"Database is locked — another sqlcg process is running "
                    f"({pid_hint}). "
                    f"Wait for it to finish or kill it with: kill {pid_str}"
                )
                raise RuntimeError(msg) from exc
            raise
        self._conn = kuzu.Connection(self._db)
        self._in_transaction = False

    def init_schema(self) -> None:
        """Initialize the database schema if not already present.

        Creates all node and relationship tables from the schema DDL.
        """
        # Check if Repo node table exists (first table in DDL)
        try:
            self._conn.execute(f"MATCH (n:{NODE_REPO}) RETURN COUNT(*) as count LIMIT 1")
            # If we get here, the schema is already initialized
            logger.debug("Schema already initialized")
            return
        except Exception:
            # Schema not initialized, proceed with initialization
            pass

        # Remove comments and split statements
        # Split by ";" to get individual statements
        raw_statements = []
        current = []

        for line in SCHEMA_DDL.split("\n"):
            line = line.strip()
            if line and not line.startswith("--"):
                current.append(line)
                if line.endswith(";"):
                    raw_statements.append(" ".join(current))
                    current = []

        # Execute all DDL statements and schema version in a transaction
        with self.transaction():
            # Execute each statement
            for stmt in raw_statements:
                if stmt.strip():
                    try:
                        self._conn.execute(stmt)
                        logger.debug(f"Executed DDL: {stmt[:50]}...")
                    except Exception as e:
                        logger.error(f"DDL execution failed: {stmt[:50]}...: {e}")
                        raise

            # Upsert the schema version
            try:
                self._conn.execute(
                    "MERGE (v:SchemaVersion {version: $v})",
                    {"v": SCHEMA_VERSION},
                )
                logger.debug(f"Wrote schema version: {SCHEMA_VERSION}")
            except Exception as e:
                logger.error(f"Failed to write schema version: {e}")
                raise

    def upsert_node(self, label: str, key: str, properties: dict[str, Any]) -> None:
        """Upsert a node with the given label and properties.

        Note: The key parameter is used for the primary key field. The actual
        primary key field name depends on the label. For now, we use the key
        as the primary key identifier.

        Note: Properties that match the primary key field are skipped in the SET clause.
        """
        # Validate property keys to prevent Cypher injection
        self._validate_props(properties)

        pk_field = self._pk_field(label)

        # Build the MERGE statement
        # Format: MERGE (n:Label {pk_field: $key}) SET n.field = $field, ...
        params = {"key": key}

        # Filter out the primary key field from properties (cannot be updated via SET)
        set_properties = {k: v for k, v in properties.items() if k != pk_field}

        for k, v in set_properties.items():
            params[k] = v

        query = f"MERGE (n:{label} {{{pk_field}: $key}})"
        if set_properties:
            set_parts = [f"n.{k} = ${k}" for k in set_properties.keys()]
            query += f" SET {', '.join(set_parts)}"

        try:
            self._conn.execute(query, params)
        except Exception as e:
            logger.error(f"upsert_node failed: {label} {key}: {e}")
            raise

    def upsert_edge(
        self,
        src_label: str,
        src_key: str,
        dst_label: str,
        dst_key: str,
        rel_type: str,
        properties: dict[str, Any],
    ) -> None:
        """Upsert a relationship between two nodes."""
        # Validate property keys to prevent Cypher injection
        self._validate_props(properties)

        src_pk_field = self._pk_field(src_label)
        dst_pk_field = self._pk_field(dst_label)

        # langchain_kuzu incompatible with our typed DDL schema.
        query = f"""
            MATCH (src:{src_label} {{{src_pk_field}: $src_key}})
            MATCH (dst:{dst_label} {{{dst_pk_field}: $dst_key}})
            MERGE (src)-[r:{rel_type}]->(dst)
        """

        params = {"src_key": src_key, "dst_key": dst_key}

        if properties:
            set_parts = [f"r.{k} = ${k}" for k in properties.keys()]
            query += f" SET {', '.join(set_parts)}"
            for k, v in properties.items():
                params[k] = v

        try:
            self._conn.execute(query, params)
        except Exception as e:
            logger.error(f"upsert_edge failed: {src_label} -> {rel_type} -> {dst_label}: {e}")
            raise

    def upsert_nodes_bulk(self, label: str, rows: list[dict[str, Any]]) -> None:
        """Bulk-upsert nodes of one label in a single backend round-trip."""
        if not rows:
            return
        # Validate all property keys across all rows (same guard as upsert_node)
        for row in rows:
            self._validate_props(row)

        pk_field = self._pk_field(label)
        # Determine the property key set from the first row; require homogeneity.
        keys = list(rows[0].keys())
        if pk_field not in keys:
            raise ValueError(
                f"upsert_nodes_bulk({label}): every row must include primary key '{pk_field}'"
            )
        for i, row in enumerate(rows[1:], 1):
            if set(row.keys()) != set(keys):
                raise ValueError(
                    f"upsert_nodes_bulk({label}): row {i} has property keys "
                    f"{sorted(row.keys())}, expected {sorted(keys)}"
                )

        set_keys = [k for k in keys if k != pk_field]
        # UNWIND $rows AS row MERGE (n:Label {pk: row.pk}) SET n.k = row.k, ...
        query = f"UNWIND $rows AS row MERGE (n:{label} {{{pk_field}: row.{pk_field}}})"
        if set_keys:
            set_parts = [f"n.{k} = row.{k}" for k in set_keys]
            query += f" SET {', '.join(set_parts)}"

        try:
            self._conn.execute(query, {"rows": rows})
        except Exception as e:
            logger.error(f"upsert_nodes_bulk failed: {label} ({len(rows)} rows): {e}")
            raise

    def upsert_edges_bulk(
        self,
        src_label: str,
        dst_label: str,
        rel_type: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Bulk-upsert edges of one (src_label, rel_type, dst_label) triple."""
        if not rows:
            return
        for row in rows:
            # src_key/dst_key are not graph properties; validate the remainder.
            props = {k: v for k, v in row.items() if k not in ("src_key", "dst_key")}
            self._validate_props(props)

        src_pk = self._pk_field(src_label)
        dst_pk = self._pk_field(dst_label)

        keys = list(rows[0].keys())
        for required in ("src_key", "dst_key"):
            if required not in keys:
                raise ValueError(
                    f"upsert_edges_bulk({src_label}->{rel_type}->{dst_label}): "
                    f"every row must include '{required}'"
                )
        for i, row in enumerate(rows[1:], 1):
            if set(row.keys()) != set(keys):
                raise ValueError(
                    f"upsert_edges_bulk: row {i} has property keys {sorted(row.keys())}, "
                    f"expected {sorted(keys)}"
                )

        prop_keys = [k for k in keys if k not in ("src_key", "dst_key")]
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (src:{src_label} {{{src_pk}: row.src_key}}) "
            f"MATCH (dst:{dst_label} {{{dst_pk}: row.dst_key}}) "
            f"MERGE (src)-[r:{rel_type}]->(dst)"
        )
        if prop_keys:
            set_parts = [f"r.{k} = row.{k}" for k in prop_keys]
            query += f" SET {', '.join(set_parts)}"

        try:
            self._conn.execute(query, {"rows": rows})
        except Exception as e:
            logger.error(
                f"upsert_edges_bulk failed: {src_label}->{rel_type}->{dst_label} "
                f"({len(rows)} rows): {e}"
            )
            raise

    def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read-only query and return results."""
        try:
            result = self._conn.execute(query, params)
            # KùzuDB returns a QueryResult that we need to convert to list of dicts
            rows = []
            column_names = result.get_column_names()  # type: ignore[union-attr]
            for row in result:
                # Each row is a tuple-like object with column names
                rows.append(dict(zip(column_names, row, strict=True)))
            return rows
        except Exception as e:
            logger.error(f"run_read failed: {e}")
            raise

    def run_write(self, query: str, params: dict[str, Any]) -> None:
        """Execute a write query (mutation)."""
        try:
            self._conn.execute(query, params)
        except Exception as e:
            logger.error(f"run_write failed: {e}")
            raise

    def delete_nodes_for_file(self, file_path: str) -> None:
        """Delete all nodes and relationships associated with a file.

        This executes four separate Cypher statements:
        1. Delete Column nodes for tables defined in this file
        2. Delete Query nodes and their edges
        3. Delete Table nodes defined in this file
        4. Delete the File node itself

        KùzuDB does not support multiple statements in a single execute() call,
        so each statement is executed separately within the active transaction.
        """
        try:
            # Step A: Delete Column nodes for tables defined in this file
            self._conn.execute(DELETE_COLUMNS_FOR_FILE, {"path": file_path})
            logger.debug(f"Deleted Column nodes for {file_path}")

            # Step B: Delete Query nodes and their edges
            self._conn.execute(DELETE_QUERIES_FOR_FILE, {"path": file_path})
            logger.debug(f"Deleted Query nodes for {file_path}")

            # Step C: Delete Table nodes defined in this file
            self._conn.execute(DELETE_TABLES_FOR_FILE, {"path": file_path})
            logger.debug(f"Deleted Table nodes for {file_path}")

            # Step D: Delete the File node itself
            self._conn.execute(DELETE_FILE, {"path": file_path})
            logger.debug(f"Deleted File node for {file_path}")

        except Exception as e:
            logger.error(f"delete_nodes_for_file failed for {file_path}: {e}")
            raise

    def get_schema_version(self) -> str | None:
        """Get the stored schema version from the database.

        Returns:
            The schema version string, or None if not set.
        """
        try:
            result = self.run_read(
                "MATCH (v:SchemaVersion) RETURN v.version AS version LIMIT 1", {}
            )
            return result[0]["version"] if result else None
        except Exception as e:
            logger.warning(f"Failed to read schema version: {e}")
            return None

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
            self._db.close()
            logger.debug("KuzuBackend connection closed")
        except Exception as e:
            logger.error(f"Error closing KuzuBackend: {e}")
            raise

    @contextmanager
    def transaction(self) -> Iterator["KuzuBackend"]:
        """Context manager for KùzuDB transactions.

        Uses Cypher's BEGIN TRANSACTION / COMMIT / ROLLBACK commands.
        KùzuDB 0.11.3 transaction API: execute("BEGIN TRANSACTION"),
        then execute("COMMIT") or execute("ROLLBACK").

        Yields:
            self (the KuzuBackend instance)

        Raises:
            Any exception raised in the context triggers ROLLBACK.
        """
        try:
            self._conn.execute("BEGIN TRANSACTION")
            self._in_transaction = True
            yield self
            self._conn.execute("COMMIT")
            self._in_transaction = False
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
                self._in_transaction = False
            except Exception as rollback_err:
                logger.error(f"Rollback failed: {rollback_err}")
                self._in_transaction = False  # defensive reset
            raise
