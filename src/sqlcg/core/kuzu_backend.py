"""KùzuDB implementation of GraphBackend."""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import kuzu

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.schema import (
    SCHEMA_DDL,
    SCHEMA_VERSION,
    NODE_FILE,
    NODE_TABLE,
    NODE_QUERY,
    NODE_COLUMN,
    NODE_REPO,
    REL_DEFINED_IN,
    REL_HAS_COLUMN,
    REL_SELECTS_FROM,
    REL_INSERTS_INTO,
    REL_DELETES_FROM,
    REL_UPDATES,
    REL_COLUMN_LINEAGE,
    REL_DECLARES,
)
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class KuzuBackend(GraphBackend):
    """KùzuDB implementation of the graph database backend."""

    def __init__(self, db_path: str):
        """Initialize KùzuDB backend.

        Args:
            db_path: Path to the KùzuDB database file (or ':memory:' for in-memory)
        """
        self._db_path = db_path
        self._db = kuzu.database.Database(db_path)
        self._conn = kuzu.Connection(self._db)
        self._in_transaction = False

    def init_schema(self) -> None:
        """Initialize the database schema if not already present.

        Creates all node and relationship tables from the schema DDL.
        """
        # Check if Repo node table exists (first table in DDL)
        try:
            self._conn.execute(
                f"MATCH (n:{NODE_REPO}) RETURN COUNT(*) as count LIMIT 1"
            )
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

        # Execute each statement
        for stmt in raw_statements:
            if stmt.strip():
                try:
                    self._conn.execute(stmt)
                    logger.debug(f"Executed DDL: {stmt[:50]}...")
                except Exception as e:
                    logger.error(f"DDL execution failed: {stmt[:50]}...: {e}")
                    raise

    def upsert_node(
        self, label: str, key: str, properties: dict[str, Any]
    ) -> None:
        """Upsert a node with the given label and properties.

        Note: The key parameter is used for the primary key field. The actual
        primary key field name depends on the label. For now, we use the key
        as the primary key identifier.

        Note: Properties that match the primary key field are skipped in the SET clause.
        """
        # Map label to its primary key field name
        pk_field_map = {
            NODE_REPO: "path",
            NODE_FILE: "path",
            NODE_TABLE: "qualified",
            NODE_COLUMN: "id",
            NODE_QUERY: "id",
        }

        pk_field = pk_field_map.get(label, "id")

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
        # Map label to its primary key field name
        pk_field_map = {
            NODE_REPO: "path",
            NODE_FILE: "path",
            NODE_TABLE: "qualified",
            NODE_COLUMN: "id",
            NODE_QUERY: "id",
        }

        src_pk_field = pk_field_map.get(src_label, "id")
        dst_pk_field = pk_field_map.get(dst_label, "id")

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
            logger.error(
                f"upsert_edge failed: {src_label} -> {rel_type} -> {dst_label}: {e}"
            )
            raise

    def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read-only query and return results."""
        try:
            result = self._conn.execute(query, params)
            # KùzuDB returns a QueryResult that we need to convert to list of dicts
            rows = []
            column_names = result.get_column_names()
            for row in result:
                # Each row is a tuple-like object with column names
                rows.append(dict(zip(column_names, row)))
            return rows
        except Exception as e:
            logger.error(f"run_read failed: {e}")
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
            query_a = f"""
                MATCH (f:{NODE_FILE} {{path: $path}})<-[:{REL_DEFINED_IN}]-(t:{NODE_TABLE})-[:{REL_HAS_COLUMN}]->(c:{NODE_COLUMN})
                DETACH DELETE c
            """
            self._conn.execute(query_a, {"path": file_path})
            logger.debug(f"Deleted Column nodes for {file_path}")

            # Step B: Delete Query nodes and their edges
            query_b = f"""
                MATCH (f:{NODE_FILE} {{path: $path}})<-[:QUERY_DEFINED_IN]-(q:{NODE_QUERY})
                DETACH DELETE q
            """
            self._conn.execute(query_b, {"path": file_path})
            logger.debug(f"Deleted Query nodes for {file_path}")

            # Step C: Delete Table nodes defined in this file
            query_c = f"""
                MATCH (f:{NODE_FILE} {{path: $path}})<-[:{REL_DEFINED_IN}]-(t:{NODE_TABLE})
                DETACH DELETE t
            """
            self._conn.execute(query_c, {"path": file_path})
            logger.debug(f"Deleted Table nodes for {file_path}")

            # Step D: Delete the File node itself
            query_d = f"""
                MATCH (f:{NODE_FILE} {{path: $path}})
                DETACH DELETE f
            """
            self._conn.execute(query_d, {"path": file_path})
            logger.debug(f"Deleted File node for {file_path}")

        except Exception as e:
            logger.error(f"delete_nodes_for_file failed for {file_path}: {e}")
            raise

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
            except Exception as rollback_err:
                logger.error(f"Rollback failed: {rollback_err}")
            self._in_transaction = False
            raise
