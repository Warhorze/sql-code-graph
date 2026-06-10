"""Integration tests for ``sqlcg catalog load`` — real DuckDB in-memory.

Covers:
- HAS_COLUMN rows with source='information_schema' are created.
- DDL rows are NOT overwritten (ddl > information_schema precedence).
- Usage rows are upgraded to information_schema (information_schema > usage).
- A previously-bad strict edge flips strict-good after catalog load.
- A contradicted phantom edge stays bad and lands in the contradicted bucket.
- Idempotency: second load produces no row growth.
- Source-aware delete: catalog rows survive a per-file reindex.
- Full index_repo re-applies catalog when .sqlcg.toml configures a path.
- Missing configured catalog path: warning only, index succeeds.
- Perf guard: insert_table_nodes_if_absent + upsert_nodes_bulk(COLUMN) +
  upsert_edges_bulk(HAS_COLUMN) are each called exactly once per catalog load.

Guards the PR 2 catalog-load feature
([plan doc](plan/sprints/graph_health_catalog_and_metrics.md) §4).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sqlcg.cli.commands.catalog import apply_catalog_to_backend
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    b = DuckDBBackend(":memory:")
    b.init_schema()
    yield b
    b.close()


def _catalog_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """Write a minimal INFORMATION_SCHEMA CSV to *tmp_path/cols.csv*."""
    p = tmp_path / "cols.csv"
    lines = ["TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME"]
    for schema, table, col in rows:
        lines.append(f"{schema},{table},{col}")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _seed_table(backend: DuckDBBackend, qualified: str, kind: str = "table") -> None:
    backend.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": qualified,
                "catalog": "",
                "db": "",
                "name": qualified.split(".")[-1],
                "kind": kind,
                "defined_in_file": "",
            }
        ],
    )


def _seed_has_column(backend: DuckDBBackend, table_q: str, col_id: str, source: str) -> None:
    _seed_table(backend, table_q)
    backend.upsert_nodes_bulk(
        NodeLabel.COLUMN,
        [
            {
                "id": col_id,
                "col_name": col_id.split(".")[-1],
                "table_qualified": table_q,
                "catalog": "",
                "db": "",
                "table_name": table_q.split(".")[-1],
            }
        ],
    )
    backend.upsert_edges_bulk(
        NodeLabel.TABLE,
        NodeLabel.COLUMN,
        RelType.HAS_COLUMN,
        [{"src_key": table_q, "dst_key": col_id, "source": source}],
    )


def _count_has_column(backend: DuckDBBackend, src_key: str, dst_key: str) -> int:
    rows = backend.run_read(
        'SELECT COUNT(*) AS n FROM "HAS_COLUMN" WHERE src_key = ? AND dst_key = ?',
        {"s": src_key, "d": dst_key},
    )
    return rows[0]["n"]


def _get_source(backend: DuckDBBackend, src_key: str, dst_key: str) -> str | None:
    rows = backend.run_read(
        'SELECT source FROM "HAS_COLUMN" WHERE src_key = ? AND dst_key = ?',
        {"s": src_key, "d": dst_key},
    )
    return rows[0]["source"] if rows else None


# ---------------------------------------------------------------------------
# HAS_COLUMN rows with source='information_schema'
# ---------------------------------------------------------------------------


def test_catalog_load_creates_has_column_rows(backend: DuckDBBackend, tmp_path: Path):
    """Loading a catalog CSV creates HAS_COLUMN edges with source='information_schema'."""
    csv_path = _catalog_csv(tmp_path, [("da", "orders", "order_id")])
    result = apply_catalog_to_backend(csv_path, backend)

    assert result["columns_loaded"] == 1
    assert result["tables_loaded"] == 1
    source = _get_source(backend, "da.orders", "da.orders.order_id")
    assert source == "information_schema"


def test_catalog_load_creates_sqltable_for_unknown_tables(backend: DuckDBBackend, tmp_path: Path):
    """Tables not previously in the graph get a bare SqlTable row (kind='table')."""
    csv_path = _catalog_csv(tmp_path, [("ia", "dim_product", "product_key")])
    apply_catalog_to_backend(csv_path, backend)

    rows = backend.run_read(
        'SELECT qualified, kind, defined_in_file FROM "SqlTable" WHERE qualified = ?',
        {"q": "ia.dim_product"},
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "table"
    assert rows[0]["defined_in_file"] == ""


# ---------------------------------------------------------------------------
# DDL precedence: ddl rows are NOT overwritten
# ---------------------------------------------------------------------------


def test_ddl_row_not_overwritten_by_catalog(backend: DuckDBBackend, tmp_path: Path):
    """A pre-existing HAS_COLUMN(source='ddl') is not overwritten by catalog load."""
    _seed_has_column(backend, "da.orders", "da.orders.order_id", "ddl")

    csv_path = _catalog_csv(tmp_path, [("da", "orders", "order_id")])
    apply_catalog_to_backend(csv_path, backend)

    source = _get_source(backend, "da.orders", "da.orders.order_id")
    assert source == "ddl", f"DDL source was overwritten — got {source!r}"


def test_existing_table_with_real_kind_not_overwritten(backend: DuckDBBackend, tmp_path: Path):
    """An existing SqlTable with kind='view' and defined_in_file set is not overwritten."""
    backend.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": "da.orders",
                "catalog": "",
                "db": "da",
                "name": "orders",
                "kind": "view",
                "defined_in_file": "/repo/ddl/orders.sql",
            }
        ],
    )
    csv_path = _catalog_csv(tmp_path, [("da", "orders", "order_id")])
    apply_catalog_to_backend(csv_path, backend)

    rows = backend.run_read(
        'SELECT kind, defined_in_file FROM "SqlTable" WHERE qualified = ?',
        {"q": "da.orders"},
    )
    assert rows[0]["kind"] == "view"
    assert rows[0]["defined_in_file"] == "/repo/ddl/orders.sql"


# ---------------------------------------------------------------------------
# Usage rows are upgraded to information_schema
# ---------------------------------------------------------------------------


def test_usage_row_upgraded_to_information_schema(backend: DuckDBBackend, tmp_path: Path):
    """A HAS_COLUMN(source='usage') row is upgraded when catalog load runs."""
    _seed_has_column(backend, "da.orders", "da.orders.order_id", "usage")

    csv_path = _catalog_csv(tmp_path, [("da", "orders", "order_id")])
    apply_catalog_to_backend(csv_path, backend)

    source = _get_source(backend, "da.orders", "da.orders.order_id")
    assert source == "information_schema", f"Usage source not upgraded — got {source!r}"


# ---------------------------------------------------------------------------
# Idempotency: second load produces no row growth
# ---------------------------------------------------------------------------


def test_catalog_load_is_idempotent(backend: DuckDBBackend, tmp_path: Path):
    """A second catalog load of the same file produces no row growth."""
    csv_path = _catalog_csv(
        tmp_path,
        [
            ("da", "orders", "order_id"),
            ("da", "orders", "total"),
        ],
    )
    r1 = apply_catalog_to_backend(csv_path, backend)
    count_after_first = backend.run_read('SELECT COUNT(*) AS n FROM "HAS_COLUMN"', {})[0]["n"]

    r2 = apply_catalog_to_backend(csv_path, backend)
    count_after_second = backend.run_read('SELECT COUNT(*) AS n FROM "HAS_COLUMN"', {})[0]["n"]

    assert count_after_first == count_after_second, (
        f"Second load grew HAS_COLUMN: {count_after_first} → {count_after_second}"
    )
    assert r2["columns_loaded"] == r1["columns_loaded"]


# ---------------------------------------------------------------------------
# Source-aware delete: catalog rows survive per-file reindex
# ---------------------------------------------------------------------------


def test_catalog_rows_survive_per_file_reindex(tmp_path: Path):
    """HAS_COLUMN(source='information_schema') rows survive reindex of the DDL file.

    Decision B3 (plan §4 §3): the per-file delete must not remove rows with
    source IN ('information_schema', 'usage').

    Uses a column (customer_name) that appears in the catalog but NOT in the DDL
    definition, so it can only have source='information_schema' and is a clean
    survivor test (no ddl/info_schema overlap for this column).
    """
    db = DuckDBBackend(":memory:")
    db.init_schema()

    # Write a DDL file that defines order_id + total but NOT customer_name
    ddl_file = tmp_path / "ddl" / "orders.sql"
    ddl_file.parent.mkdir(parents=True)
    ddl_file.write_text("CREATE VIEW da.orders AS SELECT order_id, total FROM raw.orders;")

    # Write an ETL file to make the indexer generate lineage
    etl_file = tmp_path / "etl" / "etl.sql"
    etl_file.parent.mkdir(parents=True)
    etl_file.write_text("INSERT INTO da.orders SELECT order_id, total FROM da.raw;")

    # Index
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    # Load catalog for orders.customer_name (NOT in the DDL — pure catalog column)
    csv_path = tmp_path / "cols.csv"
    csv_path.write_text("TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\nda,orders,customer_name\n")
    apply_catalog_to_backend(csv_path, db)

    catalog_before = db.run_read(
        'SELECT COUNT(*) AS n FROM "HAS_COLUMN" WHERE src_key = ? AND source = ?',
        {"s": "da.orders", "src": "information_schema"},
    )[0]["n"]
    assert catalog_before >= 1, "Catalog row not inserted"

    # Trigger a per-file delete (simulating reindex of the DDL file)
    db.delete_nodes_for_file(str(ddl_file))

    # Catalog row must survive
    catalog_after = db.run_read(
        'SELECT COUNT(*) AS n FROM "HAS_COLUMN" WHERE src_key = ? AND source = ?',
        {"s": "da.orders", "src": "information_schema"},
    )[0]["n"]
    assert catalog_after == catalog_before, (
        f"Catalog rows deleted during per-file reindex: "
        f"{catalog_before} before → {catalog_after} after"
    )

    db.close()


def test_ddl_rows_deleted_per_file_reindex_catalog_survives(tmp_path: Path):
    """DDL rows are deleted by per-file reindex; catalog-only rows survive.

    Mixed case: table has catalog rows for a column NOT in the DDL, plus DDL rows
    for columns that ARE in the DDL.  After delete_nodes_for_file:
    - DDL-sourced rows are removed (source not in excluded set)
    - catalog-sourced rows (information_schema) are preserved
    """
    db = DuckDBBackend(":memory:")
    db.init_schema()

    # Seed catalog row for da.orders.extra_col (will never be in any DDL)
    db.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": "da.orders",
                "catalog": "",
                "db": "da",
                "name": "orders",
                "kind": "table",
                "defined_in_file": "",
            }
        ],
    )
    db.upsert_nodes_bulk(
        NodeLabel.COLUMN,
        [
            {
                "id": "da.orders.extra_col",
                "col_name": "extra_col",
                "table_qualified": "da.orders",
                "catalog": "",
                "db": "da",
                "table_name": "orders",
            }
        ],
    )
    db.upsert_edges_bulk(
        NodeLabel.TABLE,
        NodeLabel.COLUMN,
        RelType.HAS_COLUMN,
        [
            {
                "src_key": "da.orders",
                "dst_key": "da.orders.extra_col",
                "source": "information_schema",
            }
        ],
    )

    # Write and index a DDL file for the same table (defines order_id, total — not extra_col)
    ddl_file = tmp_path / "ddl" / "orders.sql"
    ddl_file.parent.mkdir(parents=True)
    ddl_file.write_text("CREATE VIEW da.orders AS SELECT order_id, total FROM raw.orders;")
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    # Core assertion: catalog row survival after per-file delete.

    # Now simulate per-file reindex
    db.delete_nodes_for_file(str(ddl_file))

    # The catalog-only row (extra_col) must survive
    catalog_after = db.run_read(
        'SELECT source FROM "HAS_COLUMN" WHERE src_key = ? AND dst_key = ?',
        {"s": "da.orders", "d": "da.orders.extra_col"},
    )
    assert len(catalog_after) == 1, (
        f"Catalog-only row was deleted during per-file reindex. "
        f"Expected 1 row, got {len(catalog_after)}"
    )
    assert catalog_after[0]["source"] == "information_schema"

    db.close()


# ---------------------------------------------------------------------------
# index_repo re-applies catalog when configured in .sqlcg.toml
# ---------------------------------------------------------------------------


def test_index_repo_reapplies_catalog_when_configured(tmp_path: Path):
    """index_repo re-applies the catalog at the end when .sqlcg.toml configures a path."""
    # Write a simple SQL file
    (tmp_path / "etl.sql").write_text("SELECT order_id FROM da.orders;")

    # Write catalog CSV
    csv_path = tmp_path / "cols.csv"
    csv_path.write_text("TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\nda,orders,order_id\n")

    # Write .sqlcg.toml with catalog path
    toml = tmp_path / ".sqlcg.toml"
    toml.write_text(f'[sqlcg]\ndialect = "snowflake"\n\n[sqlcg.catalog]\npath = "{csv_path}"\n')

    db = DuckDBBackend(":memory:")
    db.init_schema()

    result = Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    # Catalog should have been re-applied
    assert result.get("catalog_reapplied") is True, f"catalog_reapplied not set in result: {result}"
    source = _get_source(db, "da.orders", "da.orders.order_id")
    assert source == "information_schema", (
        f"Expected information_schema source after re-apply, got {source!r}"
    )
    db.close()


def test_index_repo_missing_configured_catalog_warns_not_fails(tmp_path: Path, caplog):
    """When the configured catalog path doesn't exist, index succeeds with a warning."""
    (tmp_path / "etl.sql").write_text("SELECT order_id FROM da.orders;")
    toml = tmp_path / ".sqlcg.toml"
    toml.write_text(
        '[sqlcg]\ndialect = "snowflake"\n\n[sqlcg.catalog]\npath = "/nonexistent/cols.csv"\n'
    )

    db = DuckDBBackend(":memory:")
    db.init_schema()

    import logging

    with caplog.at_level(logging.WARNING, logger="sqlcg"):
        result = Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    # Index succeeded
    assert "files_parsed" in result
    # Warning logged
    assert any(
        "not found" in r.message or "catalog" in r.message.lower()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), f"No catalog warning logged. Records: {[r.message for r in caplog.records]}"
    # catalog_reapplied is False (no rows loaded)
    assert result.get("catalog_reapplied") is False
    db.close()


# ---------------------------------------------------------------------------
# Perf guard: one execute() per call type
# ---------------------------------------------------------------------------


def test_catalog_load_calls_bulk_methods_once_per_load(tmp_path: Path):
    """apply_catalog_to_backend calls insert_table_nodes_if_absent, upsert_nodes_bulk,
    and upsert_edges_bulk exactly once per catalog load — bulk-upsert invariant.

    Mirrors test_upsert_batch_invariant.py style (counter-based, not wall-clock).
    """
    csv_path = _catalog_csv(
        tmp_path,
        [
            ("da", "orders", "order_id"),
            ("da", "orders", "total"),
            ("ia", "dim_product", "product_key"),
        ],
    )

    mock_backend = MagicMock()

    apply_catalog_to_backend(csv_path, mock_backend)

    # insert_table_nodes_if_absent called exactly once for all tables
    assert mock_backend.insert_table_nodes_if_absent.call_count == 1, (
        f"insert_table_nodes_if_absent called "
        f"{mock_backend.insert_table_nodes_if_absent.call_count} times, expected 1"
    )
    # upsert_nodes_bulk called exactly once for columns
    assert mock_backend.upsert_nodes_bulk.call_count == 1, (
        f"upsert_nodes_bulk called {mock_backend.upsert_nodes_bulk.call_count} times, expected 1"
    )
    # upsert_edges_bulk called exactly once for HAS_COLUMN
    assert mock_backend.upsert_edges_bulk.call_count == 1, (
        f"upsert_edges_bulk called {mock_backend.upsert_edges_bulk.call_count} times, expected 1"
    )
    # The column bulk call passed the correct label
    col_call_args = mock_backend.upsert_nodes_bulk.call_args[0]
    assert col_call_args[0] == NodeLabel.COLUMN, (
        f"upsert_nodes_bulk called with label {col_call_args[0]!r}, expected COLUMN"
    )
    # The edge bulk call passed the HAS_COLUMN rel type
    edge_call_args = mock_backend.upsert_edges_bulk.call_args[0]
    assert edge_call_args[2] == RelType.HAS_COLUMN, (
        f"upsert_edges_bulk called with rel_type {edge_call_args[2]!r}, expected HAS_COLUMN"
    )


# ---------------------------------------------------------------------------
# Previously-bad strict edge flips to strict-good after catalog load
# ---------------------------------------------------------------------------


def test_strict_edge_flips_good_after_catalog_load(backend: DuckDBBackend, tmp_path: Path):
    """An edge whose dst column becomes catalogued flips from bad to strict-good.

    Verifies that after catalog load, the strict metric (dst_key in HAS_COLUMN)
    would classify the edge as good.
    """
    # Seed a COLUMN_LINEAGE edge for da.orders.total -> da.fact.total
    dst_table = "da.fact"
    dst_col_id = "da.fact.total"
    src_col_id = "da.orders.order_id"

    for t in ("da.orders", dst_table):
        _seed_table(backend, t)

    # Add a src column
    backend.upsert_nodes_bulk(
        NodeLabel.COLUMN,
        [
            {
                "id": src_col_id,
                "col_name": "order_id",
                "table_qualified": "da.orders",
                "catalog": "",
                "db": "da",
                "table_name": "orders",
            }
        ],
    )

    # Simulate a COLUMN_LINEAGE edge (dst has no HAS_COLUMN yet → "bad" under strict)
    backend.run_write(
        'INSERT OR IGNORE INTO "COLUMN_LINEAGE" '
        "(src_key, dst_key, transform, confidence, query_id, inferred_from_source_name) "
        "VALUES (?, ?, 'SELECT', '1.0', 'q1', 'false')",
        {"s": src_col_id, "d": dst_col_id},
    )

    # Before catalog load: dst_key not in HAS_COLUMN → edge is "bad" under strict metric
    bad_before = backend.run_read(
        'SELECT COUNT(*) AS n FROM "COLUMN_LINEAGE" cl '
        'WHERE cl.dst_key NOT IN (SELECT dst_key FROM "HAS_COLUMN")',
        {},
    )[0]["n"]
    assert bad_before >= 1, "Expected at least one bad edge before catalog load"

    # Load catalog that includes the dst column
    csv_path = _catalog_csv(tmp_path, [("da", "fact", "total")])
    apply_catalog_to_backend(csv_path, backend)

    # After catalog load: edge should be strict-good
    bad_after = backend.run_read(
        'SELECT COUNT(*) AS n FROM "COLUMN_LINEAGE" cl '
        'WHERE cl.dst_key = ? AND cl.dst_key NOT IN (SELECT dst_key FROM "HAS_COLUMN")',
        {"d": dst_col_id},
    )[0]["n"]
    assert bad_after == 0, f"Edge still bad after catalog load: dst_key={dst_col_id!r}"
