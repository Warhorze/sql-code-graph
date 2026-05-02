"""Neo4j implementation of GraphBackend."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.queries import (
    DELETE_COLUMNS_FOR_FILE,
    DELETE_FILE,
    DELETE_QUERIES_FOR_FILE,
    DELETE_TABLES_FOR_FILE,
)
from sqlcg.core.schema import NODE_COLUMN, NODE_FILE, NODE_QUERY, NODE_REPO, NODE_TABLE
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

try:
    from neo4j import GraphDatabase as _GraphDatabase

    GraphDatabase = _GraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
    GraphDatabase = None  # type: ignore[assignment,misc]
    NEO4J_AVAILABLE = False


class Neo4jBackend(GraphBackend):
    """Neo4j implementation of the graph database backend."""

    def __init__(self, uri: str, user: str, password: str):
        """Initialize Neo4j backend.

        Args:
            uri: Neo4j connection URI (e.g., "bolt://localhost:7687")
            user: Neo4j username
            password: Neo4j password

        Raises:
            ImportError: If the neo4j package is not installed
        """
        if not NEO4J_AVAILABLE:
            raise ImportError(
                "neo4j package is not installed. "
                "Install it with: pip install 'sql-code-graph[neo4j]'"
            )

        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._session = self._driver.session()

    def init_schema(self) -> None:
        """Initialize the database schema if not already present.

        Creates indexes and constraints for efficient querying.
        """
        # Create indexes on primary key fields — uses renamed labels (Deviation 1)
        indexes = [
            f"CREATE INDEX idx_repo_path IF NOT EXISTS FOR (r:{NODE_REPO}) ON (r.path)",
            f"CREATE INDEX idx_file_path IF NOT EXISTS FOR (f:{NODE_FILE}) ON (f.path)",
            f"CREATE INDEX idx_table_qualified IF NOT EXISTS FOR (t:{NODE_TABLE}) ON (t.qualified)",
            f"CREATE INDEX idx_column_id IF NOT EXISTS FOR (c:{NODE_COLUMN}) ON (c.id)",
            f"CREATE INDEX idx_query_id IF NOT EXISTS FOR (q:{NODE_QUERY}) ON (q.id)",
        ]

        for index_query in indexes:
            try:
                self._session.run(index_query)
                logger.debug(f"Created index: {index_query[:50]}...")
            except Exception as e:
                logger.warning(f"Index creation skipped: {e}")

    def upsert_node(self, label: str, key: str, properties: dict[str, Any]) -> None:
        """Upsert a node with the given label and properties."""
        # Validate property keys to prevent Cypher injection
        self._validate_props(properties)

        pk_field = self._pk_field(label)
        query = f"MERGE (n:{label} {{{pk_field}: $key}}) SET n += $props"
        try:
            self._session.run(query, {"key": key, "props": properties})
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

        src_pk = self._pk_field(src_label)
        dst_pk = self._pk_field(dst_label)
        query = (
            f"MATCH (src:{src_label} {{{src_pk}: $src_key}})"
            f" MATCH (dst:{dst_label} {{{dst_pk}: $dst_key}})"
            f" MERGE (src)-[r:{rel_type}]->(dst)"
            " SET r += $props"
        )
        try:
            self._session.run(query, {"src_key": src_key, "dst_key": dst_key, "props": properties})
        except Exception as e:
            logger.error(f"upsert_edge failed: {src_label} -> {rel_type} -> {dst_label}: {e}")
            raise

    def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read-only query and return results."""
        try:
            result = self._session.run(query, params)
            rows = [dict(record) for record in result]
            return rows
        except Exception as e:
            logger.error(f"run_read failed: {e}")
            raise

    def delete_nodes_for_file(self, file_path: str) -> None:
        """Delete all nodes and relationships associated with a file."""
        params = {"path": file_path}
        try:
            # Step A: Delete SqlColumn nodes for tables defined in this file
            self._session.run(DELETE_COLUMNS_FOR_FILE, params)
            # Step B: Delete SqlQuery nodes
            self._session.run(DELETE_QUERIES_FOR_FILE, params)
            # Step C: Delete SqlTable nodes defined in this file
            self._session.run(DELETE_TABLES_FOR_FILE, params)
            # Step D: Delete the File node itself
            self._session.run(DELETE_FILE, params)
            logger.debug(f"Deleted all nodes for {file_path}")
        except Exception as e:
            logger.error(f"delete_nodes_for_file failed for {file_path}: {e}")
            raise

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._session.close()
            self._driver.close()
            logger.debug("Neo4jBackend connection closed")
        except Exception as e:
            logger.error(f"Error closing Neo4jBackend: {e}")
            raise

    @contextmanager
    def transaction(self) -> Iterator["Neo4jBackend"]:
        """Context manager for Neo4j transactions.

        Creates a fresh session per transaction to avoid issues with shared
        long-lived sessions that may be closed externally.

        Yields:
            self (the Neo4jBackend instance)

        Raises:
            Any exception raised in the context triggers ROLLBACK.
        """
        session = self._driver.session()
        tx = session.begin_transaction()
        try:
            yield self
            tx.commit()
        except Exception:
            tx.rollback()
            raise
        finally:
            session.close()
