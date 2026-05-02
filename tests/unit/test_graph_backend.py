"""Unit tests for GraphBackend ABC."""

import pytest

from sqlcg.core.graph_db import GraphBackend


def test_cannot_instantiate_abstract_class():
    """Test that GraphBackend cannot be instantiated directly."""
    with pytest.raises(TypeError):
        GraphBackend()


def test_abstract_methods():
    """Test that subclasses must implement all abstract methods."""

    class IncompleteBackend(GraphBackend):
        """A backend that only implements some methods."""

        def upsert_node(self, label, key, properties):
            pass

        def upsert_edge(self, src_label, src_key, dst_label, dst_key, rel_type, properties):
            pass

    # Should still fail because run_read, delete_nodes_for_file, and close are not implemented
    with pytest.raises(TypeError):
        IncompleteBackend()


def test_transaction_default_warning(caplog):
    """Test that the default transaction() implementation logs a warning."""
    import logging

    class MinimalBackend(GraphBackend):
        """A minimal backend implementation."""

        def init_schema(self):
            pass

        def upsert_node(self, label, key, properties):
            pass

        def upsert_edge(self, src_label, src_key, dst_label, dst_key, rel_type, properties):
            pass

        def run_read(self, query, params):
            return []

        def run_write(self, query, params):
            pass

        def delete_nodes_for_file(self, file_path):
            pass

        def get_schema_version(self):
            return None

        def close(self):
            pass

    backend = MinimalBackend()

    # Capture logs
    with caplog.at_level(logging.WARNING):
        with backend.transaction():
            pass

    # Check that warning was logged
    assert any("transaction() not overridden" in record.message for record in caplog.records)
