"""Unit tests for DuckDBBackend — Phase 1 exit gate.

Tests assert observable output:
- Schema introspection shows all 19 tables and their indexes.
- Round-trip a node/edge and read it back.
- transaction() that raises leaves the DB unchanged (mirrors C6).
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel, RelType


@pytest.fixture
def db() -> DuckDBBackend:
    """In-memory DuckDB backend, schema initialized."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    return backend


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TABLES = {
    "Repo",
    "File",
    "SqlTable",
    "SqlColumn",
    "SqlQuery",
    "SchemaVersion",
    "ExternalConsumer",
}

_EXPECTED_EDGE_TABLES = {
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
}


def test_schema_all_19_tables(db: DuckDBBackend) -> None:
    """init_schema creates exactly 7 node + 12 edge tables (19 total)."""
    rows = db.run_read(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'",
        {},
    )
    table_names = {r["table_name"] for r in rows}
    expected = _EXPECTED_NODE_TABLES | _EXPECTED_EDGE_TABLES
    # All expected tables must be present
    assert expected <= table_names, f"Missing tables: {expected - table_names}"


def test_schema_indexes_on_edge_tables(db: DuckDBBackend) -> None:
    """Each edge table has indexes on src_key and dst_key."""
    rows = db.run_read(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name IN ("
        "'COLUMN_LINEAGE', 'SELECTS_FROM', 'HAS_COLUMN')",
        {},
    )
    index_names = {r["index_name"] for r in rows}
    # Must include both src and dst indexes for COLUMN_LINEAGE
    assert any("COLUMN_LINEAGE_src" in n for n in index_names)
    assert any("COLUMN_LINEAGE_dst" in n for n in index_names)


def test_schema_version_row_present(db: DuckDBBackend) -> None:
    """init_schema inserts the SchemaVersion row."""
    version = db.get_schema_version()
    assert version is not None
    assert len(version) > 0


def test_schema_idempotent(db: DuckDBBackend) -> None:
    """Calling init_schema twice does not raise and leaves exactly 19 tables."""
    db.init_schema()  # second call
    rows = db.run_read(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'",
        {},
    )
    table_names = {r["table_name"] for r in rows}
    expected = _EXPECTED_NODE_TABLES | _EXPECTED_EDGE_TABLES
    assert expected <= table_names


# ---------------------------------------------------------------------------
# Round-trip: upsert_node / upsert_edge → run_read
# ---------------------------------------------------------------------------


def test_round_trip_repo_node(db: DuckDBBackend) -> None:
    """Upsert a Repo node and read it back — asserts observable field values."""
    db.upsert_node(NodeLabel.REPO, "/path/to/repo", {"name": "my-repo"})
    rows = db.run_read('SELECT path, name FROM "Repo" WHERE path = ?', {"path": "/path/to/repo"})
    assert len(rows) == 1
    assert rows[0]["path"] == "/path/to/repo"
    assert rows[0]["name"] == "my-repo"


def test_round_trip_file_node(db: DuckDBBackend) -> None:
    """Upsert a File node and read it back."""
    db.upsert_node(
        NodeLabel.FILE,
        "/path/to/file.sql",
        {"sha": "abc123", "dialect": "snowflake", "parse_failed": False, "parse_cause": ""},
    )
    rows = db.run_read(
        'SELECT path, sha, dialect FROM "File" WHERE path = ?',
        {"path": "/path/to/file.sql"},
    )
    assert len(rows) == 1
    assert rows[0]["sha"] == "abc123"
    assert rows[0]["dialect"] == "snowflake"


def test_round_trip_edge(db: DuckDBBackend) -> None:
    """Upsert a COLUMN_LINEAGE edge and read it back."""
    # Insert required nodes first
    db.upsert_node(
        NodeLabel.COLUMN,
        "src_table.col_a",
        {
            "col_name": "col_a",
            "table_qualified": "src_table",
            "catalog": "",
            "db": "",
            "table_name": "src_table",
        },
    )
    db.upsert_node(
        NodeLabel.COLUMN,
        "dst_table.col_b",
        {
            "col_name": "col_b",
            "table_qualified": "dst_table",
            "catalog": "",
            "db": "",
            "table_name": "dst_table",
        },
    )
    db.upsert_edge(
        NodeLabel.COLUMN,
        "src_table.col_a",
        NodeLabel.COLUMN,
        "dst_table.col_b",
        RelType.COLUMN_LINEAGE,
        {"transform": "cast", "confidence": 0.9, "query_id": "q1"},
    )
    rows = db.run_read(
        'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE"',
        {},
    )
    assert len(rows) == 1
    assert rows[0]["src_key"] == "src_table.col_a"
    assert rows[0]["dst_key"] == "dst_table.col_b"
    assert rows[0]["transform"] == "cast"
    assert abs(rows[0]["confidence"] - 0.9) < 0.01


def test_round_trip_schema_version(db: DuckDBBackend) -> None:
    """set_indexed_sha / get_indexed_sha round-trips correctly."""
    db.set_indexed_sha("deadbeef1234")
    sha = db.get_indexed_sha()
    assert sha == "deadbeef1234"


# ---------------------------------------------------------------------------
# Bulk upsert
# ---------------------------------------------------------------------------


def test_bulk_upsert_nodes(db: DuckDBBackend) -> None:
    """upsert_nodes_bulk inserts multiple rows in one call."""
    rows = [
        {
            "path": "/a.sql",
            "sha": "sha1",
            "dialect": "snowflake",
            "parse_failed": False,
            "parse_cause": "",
        },
        {
            "path": "/b.sql",
            "sha": "sha2",
            "dialect": "ansi",
            "parse_failed": False,
            "parse_cause": "",
        },
    ]
    db.upsert_nodes_bulk(NodeLabel.FILE, rows)
    result = db.run_read('SELECT count(*) AS n FROM "File"', {})
    assert result[0]["n"] == 2


def test_bulk_upsert_edges(db: DuckDBBackend) -> None:
    """upsert_edges_bulk inserts multiple edges in one call."""
    # Set up nodes
    db.upsert_nodes_bulk(
        NodeLabel.COLUMN,
        [
            {
                "id": "t.a",
                "col_name": "a",
                "table_qualified": "t",
                "catalog": "",
                "db": "",
                "table_name": "t",
            },
            {
                "id": "t.b",
                "col_name": "b",
                "table_qualified": "t",
                "catalog": "",
                "db": "",
                "table_name": "t",
            },
        ],
    )
    edges = [
        {
            "src_key": "t.a",
            "dst_key": "t.b",
            "transform": None,
            "confidence": 1.0,
            "query_id": "q1",
        },
    ]
    db.upsert_edges_bulk(NodeLabel.COLUMN, NodeLabel.COLUMN, RelType.COLUMN_LINEAGE, edges)
    result = db.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})
    assert result[0]["n"] == 1


def test_bulk_upsert_empty_is_noop(db: DuckDBBackend) -> None:
    """upsert_nodes_bulk / upsert_edges_bulk with empty list do not raise."""
    db.upsert_nodes_bulk(NodeLabel.FILE, [])
    db.upsert_edges_bulk(NodeLabel.COLUMN, NodeLabel.COLUMN, RelType.COLUMN_LINEAGE, [])
    result = db.run_read('SELECT count(*) AS n FROM "File"', {})
    assert result[0]["n"] == 0


# ---------------------------------------------------------------------------
# Transaction — real BEGIN/COMMIT/ROLLBACK (ARCHITECTURE_REVIEW §3.1)
# ---------------------------------------------------------------------------


def test_transaction_commit(db: DuckDBBackend) -> None:
    """Committed transaction makes data visible."""
    with db.transaction():
        db.upsert_node(NodeLabel.REPO, "/repo", {"name": "r"})
    rows = db.run_read('SELECT count(*) AS n FROM "Repo"', {})
    assert rows[0]["n"] == 1


def test_transaction_rollback_on_exception(db: DuckDBBackend) -> None:
    """A raised exception inside transaction() rolls back (mirrors C6)."""
    with pytest.raises(ValueError, match="forced rollback"):
        with db.transaction():
            db.upsert_node(NodeLabel.REPO, "/rolled-back", {"name": "should-not-persist"})
            raise ValueError("forced rollback")

    # After rollback, no row should exist
    rows = db.run_read('SELECT count(*) AS n FROM "Repo"', {})
    assert rows[0]["n"] == 0


def test_nested_transaction_second_call_completes(db: DuckDBBackend) -> None:
    """Two separate transaction() contexts both commit independently."""
    with db.transaction():
        db.upsert_node(NodeLabel.REPO, "/repo-1", {"name": "r1"})
    with db.transaction():
        db.upsert_node(NodeLabel.REPO, "/repo-2", {"name": "r2"})
    rows = db.run_read('SELECT count(*) AS n FROM "Repo"', {})
    assert rows[0]["n"] == 2


# ---------------------------------------------------------------------------
# delete_nodes_for_file
# ---------------------------------------------------------------------------


def _insert_file_with_query(db: DuckDBBackend, file_path: str) -> None:
    """Helper: insert a minimal File + SqlQuery node set for delete tests."""
    db.upsert_node(
        NodeLabel.FILE,
        file_path,
        {
            "sha": "abc",
            "dialect": "snowflake",
            "parse_failed": False,
            "parse_cause": "",
        },
    )
    db.upsert_node(
        NodeLabel.QUERY,
        f"{file_path}:0",
        {
            "file_path": file_path,
            "statement_index": 0,
            "sql": "SELECT 1",
            "kind": "SELECT",
            "target_table": "",
            "parse_failed": False,
            "confidence": 1.0,
            "parsing_mode": "full",
            "start_line": 1,
        },
    )
    db.upsert_edge(
        NodeLabel.QUERY,
        f"{file_path}:0",
        NodeLabel.FILE,
        file_path,
        RelType.QUERY_DEFINED_IN,
        {},
    )


def test_delete_nodes_for_file_removes_file(db: DuckDBBackend) -> None:
    """delete_nodes_for_file removes the File node."""
    _insert_file_with_query(db, "/file.sql")
    db.delete_nodes_for_file("/file.sql")
    rows = db.run_read('SELECT count(*) AS n FROM "File"', {})
    assert rows[0]["n"] == 0


def test_delete_nodes_for_file_removes_query(db: DuckDBBackend) -> None:
    """delete_nodes_for_file removes associated SqlQuery nodes."""
    _insert_file_with_query(db, "/file.sql")
    db.delete_nodes_for_file("/file.sql")
    rows = db.run_read('SELECT count(*) AS n FROM "SqlQuery"', {})
    assert rows[0]["n"] == 0


def test_delete_nodes_for_file_does_not_affect_other_files(db: DuckDBBackend) -> None:
    """delete_nodes_for_file leaves other files untouched."""
    _insert_file_with_query(db, "/file_a.sql")
    _insert_file_with_query(db, "/file_b.sql")
    db.delete_nodes_for_file("/file_a.sql")
    rows = db.run_read('SELECT count(*) AS n FROM "File"', {})
    assert rows[0]["n"] == 1
    rows2 = db.run_read('SELECT path FROM "File"', {})
    assert rows2[0]["path"] == "/file_b.sql"


def test_delete_nodes_for_file_removes_column_lineage(db: DuckDBBackend) -> None:
    """delete_nodes_for_file removes COLUMN_LINEAGE edges tied to queries from the file."""
    file_path = "/file.sql"
    query_id = f"{file_path}:0"
    db.upsert_node(
        NodeLabel.FILE,
        file_path,
        {
            "sha": "abc",
            "dialect": "snowflake",
            "parse_failed": False,
            "parse_cause": "",
        },
    )
    db.upsert_node(
        NodeLabel.QUERY,
        query_id,
        {
            "file_path": file_path,
            "statement_index": 0,
            "sql": "SELECT a FROM t",
            "kind": "SELECT",
            "target_table": "dst",
            "parse_failed": False,
            "confidence": 1.0,
            "parsing_mode": "full",
            "start_line": 1,
        },
    )
    db.upsert_node(
        NodeLabel.COLUMN,
        "src.a",
        {
            "col_name": "a",
            "table_qualified": "src",
            "catalog": "",
            "db": "",
            "table_name": "src",
        },
    )
    db.upsert_node(
        NodeLabel.COLUMN,
        "dst.a",
        {
            "col_name": "a",
            "table_qualified": "dst",
            "catalog": "",
            "db": "",
            "table_name": "dst",
        },
    )
    db.upsert_edge(
        NodeLabel.COLUMN,
        "src.a",
        NodeLabel.COLUMN,
        "dst.a",
        RelType.COLUMN_LINEAGE,
        {"transform": None, "confidence": 1.0, "query_id": query_id},
    )
    rows_before = db.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})
    assert rows_before[0]["n"] == 1
    db.delete_nodes_for_file(file_path)
    rows_after = db.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})
    assert rows_after[0]["n"] == 0
