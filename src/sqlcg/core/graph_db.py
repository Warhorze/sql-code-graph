"""Abstract base class for graph database backends."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlcg.core.schema import NodeLabel
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class GraphBackend(ABC):
    """Abstract interface for graph database operations.

    All upsert operations are idempotent (MERGE-based, not INSERT).
    Transaction support is optional; the default no-op logs a warning
    if not overridden by a subclass.

    All methods must be idempotent when called multiple times with the
    same inputs.
    """

    @abstractmethod
    def init_schema(self) -> None:
        """Initialize the database schema if not already present.

        Creates all node and relationship tables from the schema definition.
        Idempotent: safe to call multiple times.
        """

    @abstractmethod
    def upsert_node(self, label: str, key: str, properties: dict[str, Any]) -> None:
        """Upsert a node with the given label and properties.

        Idempotent MERGE: if the node exists, update its properties;
        otherwise create it.

        Args:
            label: Node label (e.g., "Table", "Column")
            key: Primary key value for identifying the node
            properties: Dict of properties to set/update on the node
        """

    @abstractmethod
    def upsert_edge(
        self,
        src_label: str,
        src_key: str,
        dst_label: str,
        dst_key: str,
        rel_type: str,
        properties: dict[str, Any],
    ) -> None:
        """Upsert a relationship between two nodes.

        Idempotent MERGE: if the relationship exists, update its properties;
        otherwise create it.

        Args:
            src_label: Source node label
            src_key: Source node primary key
            dst_label: Destination node label
            dst_key: Destination node primary key
            rel_type: Relationship type (e.g., "COLUMN_LINEAGE")
            properties: Dict of properties to set/update on the relationship
        """

    @abstractmethod
    def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read-only query and return results.

        Args:
            query: Query string (Cypher for KùzuDB/Neo4j)
            params: Parameters to bind in the query

        Returns:
            List of result dicts (one dict per row)
        """

    @abstractmethod
    def run_write(self, query: str, params: dict[str, Any]) -> None:
        """Execute a write query (mutation).

        Args:
            query: Query string (Cypher for KùzuDB/Neo4j)
            params: Parameters to bind in the query
        """

    @abstractmethod
    def delete_nodes_for_file(self, file_path: str) -> None:
        """Delete all nodes associated with a file and its relationships.

        Removes:
        - Column nodes for tables defined in this file
        - Query nodes defined in this file
        - Table nodes defined in this file
        - The File node itself

        This operation is used when re-indexing a file to ensure a clean re-parse.

        Args:
            file_path: Absolute path to the file
        """

    @abstractmethod
    def get_schema_version(self) -> str | None:
        """Get the stored schema version from the database.

        Returns:
            The schema version string, or None if not set.
        """

    @abstractmethod
    def close(self) -> None:
        """Close the database connection."""

    def __enter__(self) -> "GraphBackend":
        """Context manager entry point."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit point — closes the database connection."""
        self.close()

    @staticmethod
    def _pk_field(label: str) -> str:
        """Return the primary key field name for a node label.

        Args:
            label: Node label (e.g., "Repo", "File", "SqlTable", "SqlColumn", "SqlQuery")

        Returns:
            Primary key field name for the label
        """
        match label:
            case NodeLabel.REPO | NodeLabel.FILE:
                return "path"
            case NodeLabel.TABLE:
                return "qualified"
            case _:
                return "id"

    @staticmethod
    def _validate_props(properties: dict[str, Any]) -> None:
        """Validate that all property keys are safe identifiers.

        Guards against Cypher injection via property key interpolation.

        Args:
            properties: Dictionary of properties to validate

        Raises:
            ValueError: If any property key is not a valid identifier
        """
        for key in properties:
            if not key.isidentifier():
                raise ValueError(f"Invalid property key: {key!r}")

    @contextmanager
    def transaction(self) -> Iterator["GraphBackend"]:
        """Context manager for database transactions.

        The base implementation is a no-op that logs a warning.
        Subclasses should override to provide ACID guarantees.

        Yields:
            self (the GraphBackend instance)

        Raises:
            Any exception raised in the context is logged; the caller
            must decide whether to re-raise.
        """
        logger.warning("transaction() not overridden — no rollback guarantee")
        try:
            yield self
        except Exception:
            raise
