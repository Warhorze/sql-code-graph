"""Unit tests for DuckDBBackend.resolve_join_columns() SQL (Bug #5, PR-5 option b).

Seeds JOIN_COL_RESOLVE + SELECTS_FROM + HAS_COLUMN rows directly (no parser) and
asserts the resolver emits the correct COLUMN_LINEAGE rows for the one-owner,
two-owner (ambiguous), zero-owner, and case-mismatch cases.

Guards the PR-5 resolver section of
[plan/sprints/bugfix_lineage_correctness_validation.md](../../plan/sprints/bugfix_lineage_correctness_validation.md).
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend


@pytest.fixture
def db():
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def _seed_query_with_sources(db, query_id, source_tables):
    """Register SELECTS_FROM edges from query_id to each source table qualified name."""
    for tbl in source_tables:
        db._conn.execute(
            'INSERT INTO "SELECTS_FROM" (src_key, dst_key) VALUES (?, ?)', [query_id, tbl]
        )


def _seed_column(db, table_qualified, col_name, source="information_schema"):
    """Register a SqlColumn + HAS_COLUMN(source) row for table.col."""
    col_id = f"{table_qualified}.{col_name.lower()}"
    db._conn.execute(
        'INSERT OR REPLACE INTO "SqlColumn"'
        " (id, col_name, table_qualified, catalog, db, table_name)"
        " VALUES (?, ?, ?, '', '', ?)",
        [col_id, col_name, table_qualified, table_qualified],
    )
    db._conn.execute(
        'INSERT OR REPLACE INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
        [table_qualified, col_id, source],
    )
    return col_id


def _seed_marker(db, query_id, dst_col_id, bare_col):
    db._conn.execute(
        'INSERT OR REPLACE INTO "JOIN_COL_RESOLVE" (src_key, dst_key, bare_col) VALUES (?, ?, ?)',
        [query_id, dst_col_id, bare_col],
    )


def _resolved(db):
    return db._conn.execute(
        'SELECT src_key, dst_key, confidence FROM "COLUMN_LINEAGE" WHERE transform = ?',
        ["JOIN_COL_RESOLVED"],
    ).fetchall()


def test_one_owner_emits_single_high_confidence_edge(db):
    """Exactly one source table owns bare_col → one 0.9 edge to that owner."""
    q = "f.sql:0"
    _seed_query_with_sources(db, q, ["sales.orders", "sales.customers"])
    _seed_column(db, "sales.orders", "amount", source="ddl")
    _seed_column(db, "sales.customers", "status", source="information_schema")
    _seed_marker(db, q, "sales.tgt.status", "status")

    n = db.resolve_join_columns()
    assert n == 1
    rows = _resolved(db)
    assert len(rows) == 1
    src, dst, conf = rows[0]
    assert src == "sales.customers.status"
    assert dst == "sales.tgt.status"
    assert conf == pytest.approx(0.9)


def test_two_owners_emit_one_low_confidence_edge_each(db):
    """bare_col owned by BOTH sources → one 0.5 edge per owner (over-attribute)."""
    q = "f.sql:0"
    _seed_query_with_sources(db, q, ["sales.orders", "sales.customers"])
    _seed_column(db, "sales.orders", "status", source="ddl")
    _seed_column(db, "sales.customers", "status", source="information_schema")
    _seed_marker(db, q, "sales.tgt.status", "status")

    n = db.resolve_join_columns()
    assert n == 2
    rows = {(s, d): c for s, d, c in _resolved(db)}
    assert ("sales.orders.status", "sales.tgt.status") in rows
    assert ("sales.customers.status", "sales.tgt.status") in rows
    for conf in rows.values():
        assert conf == pytest.approx(0.5)


def test_zero_owners_emits_nothing(db):
    """No source table owns bare_col → no edge (honest empty, XML-DDL gap)."""
    q = "f.sql:0"
    _seed_query_with_sources(db, q, ["sales.orders", "sales.customers"])
    # Neither source has a 'status' column.
    _seed_column(db, "sales.orders", "amount", source="ddl")
    _seed_marker(db, q, "sales.tgt.status", "status")

    n = db.resolve_join_columns()
    assert n == 0
    assert _resolved(db) == []


def test_case_mismatched_owner_still_matches(db):
    """information_schema col_name 'STATUS' matches a lowercased bare 'status' marker.

    The resolver normalises casing on both sides of the owner-match join.
    """
    q = "f.sql:0"
    _seed_query_with_sources(db, q, ["sales.customers"])
    # Store the SqlColumn with EXPORTED upper casing in col_name.
    db._conn.execute(
        'INSERT OR REPLACE INTO "SqlColumn"'
        " (id, col_name, table_qualified, catalog, db, table_name)"
        " VALUES (?, ?, ?, '', '', ?)",
        ["sales.customers.status", "STATUS", "sales.customers", "sales.customers"],
    )
    db._conn.execute(
        'INSERT OR REPLACE INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
        ["sales.customers", "sales.customers.status", "information_schema"],
    )
    _seed_marker(db, q, "sales.tgt.status", "status")

    n = db.resolve_join_columns()
    assert n == 1
    rows = _resolved(db)
    assert rows[0][0] == "sales.customers.status"
    assert rows[0][1] == "sales.tgt.status"


def test_resolve_is_idempotent(db):
    """Re-running the resolver does not duplicate edges (INSERT OR REPLACE on PK)."""
    q = "f.sql:0"
    _seed_query_with_sources(db, q, ["sales.customers"])
    _seed_column(db, "sales.customers", "status", source="information_schema")
    _seed_marker(db, q, "sales.tgt.status", "status")

    assert db.resolve_join_columns() == 1
    assert db.resolve_join_columns() == 1
    assert len(_resolved(db)) == 1
