"""Unit tests for HAS_COLUMN source-precedence upsert (PR 3 of graph-health plan).

Guards the precedence policy ddl > information_schema > usage in
upsert_edges_bulk for RelType.HAS_COLUMN.

Plan doc: plan/sprints/graph_health_catalog_and_metrics.md §5 (Piece 1)
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel, RelType


@pytest.fixture
def backend():
    """In-memory DuckDB backend with schema initialised."""
    b = DuckDBBackend(":memory:")
    b.init_schema()
    # Seed minimal SqlTable + SqlColumn nodes so FK constraints are satisfied
    b.run_write(
        'INSERT INTO "SqlTable" (qualified, catalog, db, name, kind, defined_in_file)'
        " VALUES (?, ?, ?, ?, ?, ?)",
        {
            "qualified": "db.sch.orders",
            "catalog": "db",
            "db": "sch",
            "name": "orders",
            "kind": "table",
            "defined_in_file": "",
        },
    )
    b.run_write(
        'INSERT INTO "SqlColumn" (id, catalog, db, table_name, col_name, table_qualified)'
        " VALUES (?, ?, ?, ?, ?, ?)",
        {
            "id": "db.sch.orders.amount",
            "catalog": "db",
            "db": "sch",
            "table_name": "orders",
            "col_name": "amount",
            "table_qualified": "db.sch.orders",
        },
    )
    yield b
    b.close()


def _seed_has_column(backend: DuckDBBackend, source: str) -> None:
    """Insert a single HAS_COLUMN row via bulk upsert."""
    backend.upsert_edges_bulk(
        NodeLabel.TABLE,
        NodeLabel.COLUMN,
        RelType.HAS_COLUMN,
        [{"src_key": "db.sch.orders", "dst_key": "db.sch.orders.amount", "source": source}],
    )


def _read_source(backend: DuckDBBackend) -> str | None:
    rows = backend.run_read(
        'SELECT source FROM "HAS_COLUMN" WHERE src_key = ? AND dst_key = ?',
        {"src_key": "db.sch.orders", "dst_key": "db.sch.orders.amount"},
    )
    return rows[0]["source"] if rows else None


def test_ddl_row_not_overwritten_by_usage(backend):
    """A ddl row must survive a usage upsert — ddl wins.

    Guards precedence: ddl > usage.
    """
    _seed_has_column(backend, "ddl")
    _seed_has_column(backend, "usage")
    assert _read_source(backend) == "ddl", "usage must not overwrite ddl"


def test_usage_row_overwritten_by_ddl(backend):
    """A usage row is overwritten when ddl arrives — ddl wins.

    Guards precedence: ddl > usage.
    """
    _seed_has_column(backend, "usage")
    _seed_has_column(backend, "ddl")
    assert _read_source(backend) == "ddl", "ddl must overwrite usage"


def test_idempotent_same_source_no_change(backend):
    """Re-upserting the same source value is a no-op.

    Guards idempotency: second upsert with source='ddl' must keep source='ddl'.
    """
    _seed_has_column(backend, "ddl")
    _seed_has_column(backend, "ddl")
    assert _read_source(backend) == "ddl", "idempotent re-upsert must keep source unchanged"


def test_ddl_row_not_overwritten_by_information_schema(backend):
    """A ddl row must survive an information_schema upsert.

    Guards precedence: ddl > information_schema.
    """
    _seed_has_column(backend, "ddl")
    _seed_has_column(backend, "information_schema")
    assert _read_source(backend) == "ddl", "information_schema must not overwrite ddl"


def test_information_schema_overwrites_usage(backend):
    """An information_schema row overwrites an existing usage row.

    Guards precedence: information_schema > usage.
    """
    _seed_has_column(backend, "usage")
    _seed_has_column(backend, "information_schema")
    assert _read_source(backend) == "information_schema", "information_schema must overwrite usage"


def test_usage_does_not_overwrite_information_schema(backend):
    """A usage row must not overwrite an existing information_schema row.

    Guards precedence: information_schema > usage.
    """
    _seed_has_column(backend, "information_schema")
    _seed_has_column(backend, "usage")
    assert _read_source(backend) == "information_schema", (
        "usage must not overwrite information_schema"
    )


def test_bulk_upsert_many_rows_in_one_call(backend):
    """upsert_edges_bulk for HAS_COLUMN handles multiple rows in a single call.

    Seeds SqlColumn nodes for both columns, then bulk-upserts two rows.
    Verifies all rows are persisted (observable output test).
    """
    # Seed second column node
    backend.run_write(
        'INSERT INTO "SqlColumn" (id, catalog, db, table_name, col_name, table_qualified)'
        " VALUES (?, ?, ?, ?, ?, ?)",
        {
            "id": "db.sch.orders.qty",
            "catalog": "db",
            "db": "sch",
            "table_name": "orders",
            "col_name": "qty",
            "table_qualified": "db.sch.orders",
        },
    )
    rows = [
        {"src_key": "db.sch.orders", "dst_key": "db.sch.orders.amount", "source": "usage"},
        {"src_key": "db.sch.orders", "dst_key": "db.sch.orders.qty", "source": "usage"},
    ]
    backend.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.COLUMN, RelType.HAS_COLUMN, rows)
    result = backend.run_read(
        'SELECT dst_key FROM "HAS_COLUMN" WHERE src_key = ? ORDER BY dst_key',
        {"src_key": "db.sch.orders"},
    )
    col_ids = [r["dst_key"] for r in result]
    assert "db.sch.orders.amount" in col_ids, "amount column must be persisted"
    assert "db.sch.orders.qty" in col_ids, "qty column must be persisted"
    assert len(col_ids) == 2, f"expected 2 rows, got {len(col_ids)}"
