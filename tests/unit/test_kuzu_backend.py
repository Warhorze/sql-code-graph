"""Unit tests for KuzuBackend."""

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NODE_COLUMN, NODE_FILE, NODE_QUERY, NODE_TABLE


@pytest.fixture
def backend():
    """Create an in-memory KuzuBackend for testing."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


class TestKuzuBackendUpsert:
    """Test idempotent upsert operations."""

    def test_upsert_node_idempotent(self, backend):
        """Test that upserting the same node twice is idempotent."""
        # Insert a node
        backend.upsert_node(NODE_TABLE, "test.table", {"name": "test_table"})

        # Query to verify it exists
        result = backend.run_read(
            f"MATCH (n:{NODE_TABLE} {{qualified: $id}}) RETURN n.name as name",
            {"id": "test.table"},
        )
        assert len(result) == 1
        assert result[0]["name"] == "test_table"

        # Upsert with updated properties
        backend.upsert_node(NODE_TABLE, "test.table", {"name": "updated_table"})

        # Query again
        result = backend.run_read(
            f"MATCH (n:{NODE_TABLE} {{qualified: $id}}) RETURN n.name as name",
            {"id": "test.table"},
        )
        assert len(result) == 1
        assert result[0]["name"] == "updated_table"

    def test_upsert_edge_idempotent(self, backend):
        """Test that upserting the same edge twice is idempotent."""
        # Create source and destination nodes
        backend.upsert_node(NODE_QUERY, "query1", {})
        backend.upsert_node(NODE_TABLE, "table1", {})

        # Create an edge
        backend.upsert_edge(
            NODE_QUERY,
            "query1",
            NODE_TABLE,
            "table1",
            "SELECTS_FROM",
            {},
        )

        # Query to verify the edge exists
        _edge_q = (
            f"MATCH (q:{NODE_QUERY} {{id: $src}})"
            f"-[r:SELECTS_FROM]->(t:{NODE_TABLE} {{qualified: $dst}})"
            " RETURN COUNT(*) as count"
        )
        result = backend.run_read(_edge_q, {"src": "query1", "dst": "table1"})
        assert len(result) == 1
        assert result[0]["count"] == 1

        # Upsert again to verify idempotency
        backend.upsert_edge(
            NODE_QUERY,
            "query1",
            NODE_TABLE,
            "table1",
            "SELECTS_FROM",
            {},
        )

        # Query again to make sure still only one edge
        result = backend.run_read(_edge_q, {"src": "query1", "dst": "table1"})
        assert len(result) == 1
        assert result[0]["count"] == 1


class TestKuzuBackendTransaction:
    """Test transaction support."""

    def test_transaction_commit(self, backend):
        """Test that changes are committed in a transaction."""
        with backend.transaction():
            backend.upsert_node(NODE_TABLE, "table1", {"name": "test"})

        # Verify the node exists
        result = backend.run_read(
            f"MATCH (n:{NODE_TABLE} {{qualified: $id}}) RETURN COUNT(*) as count",
            {"id": "table1"},
        )
        assert result[0]["count"] == 1

    def test_transaction_rollback(self, backend):
        """Test that changes are rolled back on exception."""
        # Upsert a node outside the transaction
        backend.upsert_node(NODE_TABLE, "table1", {"name": "original"})

        # Start a transaction and make a change, then raise an exception
        try:
            with backend.transaction():
                backend.upsert_node(NODE_TABLE, "table1", {"name": "modified"})
                raise ValueError("Test rollback")
        except ValueError:
            pass

        # Verify the original value is still there
        result = backend.run_read(
            f"MATCH (n:{NODE_TABLE} {{qualified: $id}}) RETURN n.name as name",
            {"id": "table1"},
        )
        assert result[0]["name"] == "original"


class TestKuzuBackendDeleteFile:
    """Test delete_nodes_for_file."""

    def test_delete_nodes_for_file(self, backend):
        """Test that delete_nodes_for_file removes all associated nodes."""
        # Create a file node
        backend.upsert_node(NODE_FILE, "/test.sql", {"path": "/test.sql"})

        # Create a table node and link it to the file
        backend.upsert_node(NODE_TABLE, "table1", {"qualified": "table1"})
        backend.upsert_edge(NODE_TABLE, "table1", NODE_FILE, "/test.sql", "DEFINED_IN", {})

        # Create a column node and link it to the table
        backend.upsert_node(NODE_COLUMN, "table1.col1", {"id": "table1.col1"})
        backend.upsert_edge(NODE_TABLE, "table1", NODE_COLUMN, "table1.col1", "HAS_COLUMN", {})

        # Create a query node and link it to the file
        backend.upsert_node(NODE_QUERY, "query1", {"id": "query1"})
        backend.upsert_edge(NODE_QUERY, "query1", NODE_FILE, "/test.sql", "QUERY_DEFINED_IN", {})

        # Verify all nodes exist
        _q = (
            "MATCH (n) WHERE n.qualified = 'table1'"
            " OR n.id = 'table1.col1' OR n.id = 'query1'"
            " RETURN COUNT(*) as count"
        )
        result = backend.run_read(_q, {})
        assert result[0]["count"] == 3

        # Delete nodes for the file
        backend.delete_nodes_for_file("/test.sql")

        # Verify all nodes are gone
        result = backend.run_read(_q, {})
        assert result[0]["count"] == 0

    def test_delete_nodes_for_file_cleans_columns(self, backend):
        """Test that delete_nodes_for_file cleans up Column nodes."""
        # Create file and table nodes
        backend.upsert_node(NODE_FILE, "/test.sql", {"path": "/test.sql"})
        backend.upsert_node(NODE_TABLE, "table1", {"qualified": "table1"})
        backend.upsert_edge(NODE_TABLE, "table1", NODE_FILE, "/test.sql", "DEFINED_IN", {})

        # Create column nodes
        backend.upsert_node(NODE_COLUMN, "table1.col1", {"id": "table1.col1"})
        backend.upsert_edge(NODE_TABLE, "table1", NODE_COLUMN, "table1.col1", "HAS_COLUMN", {})

        backend.upsert_node(NODE_COLUMN, "table1.col2", {"id": "table1.col2"})
        backend.upsert_edge(NODE_TABLE, "table1", NODE_COLUMN, "table1.col2", "HAS_COLUMN", {})

        # Verify columns exist
        result = backend.run_read(f"MATCH (c:{NODE_COLUMN}) RETURN COUNT(*) as count", {})
        assert result[0]["count"] == 2

        # Delete the file
        backend.delete_nodes_for_file("/test.sql")

        # Verify columns are deleted
        result = backend.run_read(f"MATCH (c:{NODE_COLUMN}) RETURN COUNT(*) as count", {})
        assert result[0]["count"] == 0
