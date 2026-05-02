"""Neo4j implementation of GraphBackend."""

from contextlib import contextmanager
from typing import Any, Iterator

from sqlcg.core.graph_db import GraphBackend
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

try:
    from neo4j import GraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
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
        # Create indexes on primary key fields
        indexes = [
            "CREATE INDEX idx_repo_path IF NOT EXISTS FOR (r:Repo) ON (r.path)",
            "CREATE INDEX idx_file_path IF NOT EXISTS FOR (f:File) ON (f.path)",
            "CREATE INDEX idx_table_qualified IF NOT EXISTS FOR (t:Table) ON (t.qualified)",
            "CREATE INDEX idx_column_id IF NOT EXISTS FOR (c:Column) ON (c.id)",
            "CREATE INDEX idx_query_id IF NOT EXISTS FOR (q:Query) ON (q.id)",
        ]

        for index_query in indexes:
            try:
                self._session.run(index_query)
                logger.debug(f"Created index: {index_query[:50]}...")
            except Exception as e:
                logger.warning(f"Index creation skipped: {e}")

    def upsert_node(
        self, label: str, key: str, properties: dict[str, Any]
    ) -> None:
        """Upsert a node with the given label and properties."""
        # Use MERGE for idempotent upsert
        # Note: Neo4j uses {key: $key} where 'key' is the primary key field name
        # For simplicity, we'll use 'id' as the key field
        props_str = "{id: $key"
        params = {"key": key}

        for k, v in properties.items():
            props_str += f", {k}: ${k}"
            params[k] = v

        props_str += "}"

        query = f"MERGE (n:{label} {props_str})"
        if properties:
            set_parts = [f"n.{k} = ${k}" for k in properties.keys()]
            query += f" SET {', '.join(set_parts)}"

        try:
            self._session.run(query, params)
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
        query = f"""
            MATCH (src:{src_label} {{id: $src_key}})
            MATCH (dst:{dst_label} {{id: $dst_key}})
            MERGE (src)-[r:{rel_type}]->(dst)
        """

        params = {"src_key": src_key, "dst_key": dst_key}

        if properties:
            set_parts = [f"r.{k} = ${k}" for k in properties.keys()]
            query += f" SET {', '.join(set_parts)}"
            for k, v in properties.items():
                params[k] = v

        try:
            self._session.run(query, params)
        except Exception as e:
            logger.error(
                f"upsert_edge failed: {src_label} -> {rel_type} -> {dst_label}: {e}"
            )
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
        try:
            # Step A: Delete Column nodes
            self._session.run(
                """
                MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)-[:HAS_COLUMN]->(c:Column)
                DETACH DELETE c
                """,
                {"path": file_path},
            )

            # Step B: Delete Query nodes
            self._session.run(
                """
                MATCH (f:File {path: $path})<-[:DEFINED_IN]-(q:Query)
                DETACH DELETE q
                """,
                {"path": file_path},
            )

            # Step C: Delete Table nodes
            self._session.run(
                """
                MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)
                DETACH DELETE t
                """,
                {"path": file_path},
            )

            # Step D: Delete File node
            self._session.run(
                """
                MATCH (f:File {path: $path})
                DETACH DELETE f
                """,
                {"path": file_path},
            )

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

        Uses Neo4j's session.begin_transaction() API.

        Yields:
            self (the Neo4jBackend instance)

        Raises:
            Any exception raised in the context triggers ROLLBACK.
        """
        tx = self._session.begin_transaction()
        try:
            yield self
            tx.commit()
        except Exception:
            tx.rollback()
            raise
