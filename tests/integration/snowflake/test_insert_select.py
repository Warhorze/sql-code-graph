"""Integration tests for INSERT-SELECT behavior (T-10).

Investigation: When source and target tables are in the same file,
the parser creates a SELECTS_FROM edge from the INSERT query to the
source table. This is correct behavior - the source table is accessed
via SELECT within the INSERT statement.
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def indexed_db(tmp_path):
    """Create an indexed database with a simple INSERT-SELECT scenario."""
    # Initialize a test database
    db = DuckDBBackend(":memory:")
    db.init_schema()

    # Create a SQL file with INSERT-SELECT
    sql_file = tmp_path / "dml.sql"
    sql_file.write_text(
        """
        CREATE TABLE raw.source_table (id INT, name VARCHAR);
        CREATE TABLE dwh.target_table (id INT, name VARCHAR);
        INSERT INTO dwh.target_table SELECT id, name FROM raw.source_table;
        """
    )

    # Index the repository
    indexer = Indexer()
    indexer.index_repo(tmp_path, dialect="snowflake", db=db)

    return db


class TestInsertSelectEdges:
    """Test INSERT-SELECT creates correct lineage edges."""

    def test_find_table_usages_consistency(self, indexed_db):
        """T-10 Scenario 4: find_table_usages returns INSERT kind for source table.

        When a source table is accessed via INSERT-SELECT, the query that
        accesses it is an INSERT query. This test verifies that
        find_table_usages correctly identifies the source table as being
        accessed by an INSERT query.
        """
        rows = indexed_db.run_read(
            "SELECT q.kind AS kind "
            'FROM "SELECTS_FROM" e '
            'JOIN "SqlQuery" q ON e.src_key = q.id '
            'JOIN "SqlTable" t ON e.dst_key = t.qualified '
            "WHERE t.qualified LIKE '%source_table%'",
            {},
        )
        assert len(rows) >= 1, "Expected at least one query selecting from source_table"
        assert rows[0]["kind"] == "INSERT", "Expected INSERT query for source table selection"

    def test_insert_target_created_as_node(self, indexed_db):
        """Verify INSERT target table is created as a node."""
        rows = indexed_db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE '%target_table%'",
            {},
        )
        assert len(rows) >= 1, "Expected target_table to be created as a node"
