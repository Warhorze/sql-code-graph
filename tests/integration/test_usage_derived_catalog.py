"""Integration tests for usage-derived catalog harvest (PR 3 of graph-health plan).

Tests that _flush_row_batch generates HAS_COLUMN(source='usage') rows from
COLUMN_LINEAGE src endpoints for physical tables, and that CTEs do NOT produce
usage rows.

Plan doc: plan/sprints/graph_health_catalog_and_metrics.md §5 (Piece 2)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def db():
    """In-memory DuckDB backend with schema initialised."""
    b = DuckDBBackend(":memory:")
    b.init_schema()
    yield b
    b.close()


def _index_sql(db: DuckDBBackend, sql: str, filename: str = "test.sql") -> dict:
    """Index a single SQL string into the backend."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / filename).write_text(sql)
        indexer = Indexer()
        return indexer.index_repo(Path(tmpdir), dialect=None, db=db, use_git=False)


def _has_column_rows(db: DuckDBBackend, table_qualified: str) -> list[dict]:
    """Return all HAS_COLUMN rows for the given table."""
    return db.run_read(
        'SELECT src_key, dst_key, source FROM "HAS_COLUMN" WHERE src_key = ? ORDER BY dst_key',
        {"src_key": table_qualified},
    )


def test_physical_table_read_generates_usage_has_column_rows(db):
    """Reading columns from a physical table emits HAS_COLUMN(source='usage') per column.

    No DDL anywhere — the table exists only because it is read by an ETL query.
    Guards Piece 2: usage rows appear for src endpoints of COLUMN_LINEAGE edges.
    """
    _index_sql(db, "INSERT INTO mydb.sch.dst_table SELECT a, b FROM mydb.sch.src_table")

    rows = _has_column_rows(db, "mydb.sch.src_table")
    assert len(rows) == 2, (
        f"Expected 2 HAS_COLUMN rows for mydb.sch.src_table, got {len(rows)}: {rows}"
    )
    dst_cols = {r["dst_key"] for r in rows}
    assert "mydb.sch.src_table.a" in dst_cols, "column 'a' must be harvested as usage"
    assert "mydb.sch.src_table.b" in dst_cols, "column 'b' must be harvested as usage"
    for r in rows:
        assert r["source"] == "usage", f"expected source='usage', got {r['source']!r}"


def test_cte_read_produces_no_usage_rows(db):
    """Columns read from a CTE alias must NOT produce HAS_COLUMN(source='usage') rows.

    Guards Piece 2 honesty: CTEs (kind='cte') are filtered out of usage harvest.
    The real table (kind='table') being read by the CTE body DOES get usage rows.
    """
    sql = """
    WITH my_cte AS (SELECT x FROM mydb.sch.real_tbl)
    INSERT INTO mydb.sch.dst_tbl
    SELECT x FROM my_cte
    """
    _index_sql(db, sql)

    # Look up all HAS_COLUMN rows
    all_hc = db.run_read('SELECT src_key, dst_key, source FROM "HAS_COLUMN"', {})

    # CTE alias rows must not appear — src_key would be the CTE's qualified name
    # which sqlglot emits as the bare CTE name
    cte_usage_rows = [r for r in all_hc if r["source"] == "usage" and "my_cte" in r["src_key"]]
    assert len(cte_usage_rows) == 0, (
        f"CTE alias reads must not generate usage HAS_COLUMN rows, got: {cte_usage_rows}"
    )


def test_ddl_defined_column_source_stays_ddl_not_overwritten_by_usage(db):
    """When a DDL file defines a column and a query reads it, source stays 'ddl'.

    Guards Piece 1 precedence in end-to-end indexing: usage never overwrites ddl.
    """
    ddl_sql = "CREATE TABLE mydb.sch.orders (order_id INT, amount FLOAT)"
    etl_sql = "INSERT INTO mydb.sch.summary SELECT order_id, amount FROM mydb.sch.orders"

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "ddl.sql").write_text(ddl_sql)
        (Path(tmpdir) / "etl.sql").write_text(etl_sql)
        indexer = Indexer()
        indexer.index_repo(Path(tmpdir), dialect=None, db=db, use_git=False)

    orders_rows = _has_column_rows(db, "mydb.sch.orders")
    # Must have rows for both DDL-defined columns
    assert len(orders_rows) >= 2, (
        f"Expected at least 2 HAS_COLUMN rows for orders, got {len(orders_rows)}"
    )
    # All rows for orders must have source='ddl' (never overwritten by usage)
    for r in orders_rows:
        assert r["source"] == "ddl", (
            f"DDL-defined column {r['dst_key']!r} must have source='ddl', "
            f"got {r['source']!r} — usage must not overwrite ddl"
        )


def test_usage_rows_enable_strict_good_src_cataloguing(db):
    """A COLUMN_LINEAGE src column with no DDL gains a HAS_COLUMN row via usage harvest.

    Metric coupling: an edge whose src was previously uncatalogued (no HAS_COLUMN row)
    now has its src column visible in the catalog, which can flip the
    strict-good metric when the dst column is also catalogued.
    """
    # Seed a destination table with DDL so the dst_key is catalogued
    ddl_sql = "CREATE TABLE mydb.sch.dst_table (col_x INT)"
    etl_sql = "INSERT INTO mydb.sch.dst_table SELECT col_x FROM mydb.sch.fact_table"

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "ddl.sql").write_text(ddl_sql)
        (Path(tmpdir) / "etl.sql").write_text(etl_sql)
        indexer = Indexer()
        indexer.index_repo(Path(tmpdir), dialect=None, db=db, use_git=False)

    # After indexing, the src column (fact_table.col_x) must have a HAS_COLUMN row
    rows = _has_column_rows(db, "mydb.sch.fact_table")
    assert any(r["dst_key"] == "mydb.sch.fact_table.col_x" for r in rows), (
        "fact_table.col_x must appear in HAS_COLUMN after usage harvest"
    )
    usage_src_row = next(r for r in rows if r["dst_key"] == "mydb.sch.fact_table.col_x")
    assert usage_src_row["source"] == "usage"

    # The dst column (dst_table.col_x) is catalogued from DDL
    dst_rows = _has_column_rows(db, "mydb.sch.dst_table")
    assert any(r["dst_key"] == "mydb.sch.dst_table.col_x" for r in dst_rows), (
        "dst_table.col_x must be catalogued from DDL"
    )
    ddl_row = next(r for r in dst_rows if r["dst_key"] == "mydb.sch.dst_table.col_x")
    assert ddl_row["source"] == "ddl"

    # Verify the lineage edge exists
    lineage = db.run_read(
        'SELECT * FROM "COLUMN_LINEAGE" WHERE src_key = ? AND dst_key = ?',
        {"src_key": "mydb.sch.fact_table.col_x", "dst_key": "mydb.sch.dst_table.col_x"},
    )
    assert len(lineage) == 1, "COLUMN_LINEAGE edge must exist for col_x"

    # Strict-good: dst_key must be in HAS_COLUMN.dst_key (it is, via DDL)
    # This confirms the edge is now strict-good end-to-end
    strict_check = db.run_read(
        'SELECT COUNT(*) AS n FROM "COLUMN_LINEAGE" cl '
        'WHERE cl.dst_key IN (SELECT dst_key FROM "HAS_COLUMN") '
        "AND cl.src_key = ?",
        {"src_key": "mydb.sch.fact_table.col_x"},
    )
    assert strict_check[0]["n"] == 1, (
        "Edge from fact_table.col_x must be strict-good (dst catalogued from DDL)"
    )


def test_usage_harvest_rides_existing_bulk_calls(db):
    """Usage rows ride the existing bulk upsert calls without adding extra execute() calls.

    Guards the batch-invariant: usage rows are appended to the existing has_column_edges
    list before the single upsert_edges_bulk(HAS_COLUMN) call. The total bulk-call count
    for a batch that produces usage rows must equal the count for one with no usage rows.

    Uses a MockDB-style approach: count upsert_edges_bulk calls for HAS_COLUMN.
    """
    from unittest.mock import patch

    from sqlcg.indexer.indexer import BatchRowBuffer, _flush_row_batch

    # Simulate a batch with one column lineage edge from a physical table
    buf = BatchRowBuffer()
    buf.table_rows = [
        {
            "qualified": "db.s.tbl",
            "name": "tbl",
            "catalog": "db",
            "db": "s",
            "kind": "table",
            "defined_in_file": "",
        }
    ]
    buf.column_lineage_edges = [
        {
            "src_key": "db.s.tbl.col1",
            "dst_key": "db.s.dst.out",
            "transform": "SELECT",
            "confidence": 1.0,
            "query_id": "f.sql:0",
            "inferred_from_source_name": False,
        }
    ]
    buf.column_rows = [
        {
            "id": "db.s.tbl.col1",
            "col_name": "col1",
            "table_qualified": "db.s.tbl",
            "catalog": "db",
            "db": "s",
            "table_name": "tbl",
        },
        {
            "id": "db.s.dst.out",
            "col_name": "out",
            "table_qualified": "db.s.dst",
            "catalog": "db",
            "db": "s",
            "table_name": "dst",
        },
    ]
    buf.file_rows = [
        {
            "path": "f.sql",
            "repo_path": "",
            "sha": "",
            "dialect": None,
            "parse_failed": False,
            "parse_cause": None,
        }
    ]
    buf.table_rows.append(
        {
            "qualified": "db.s.dst",
            "name": "dst",
            "catalog": "db",
            "db": "s",
            "kind": "table",
            "defined_in_file": "",
        }
    )
    buf.query_rows = [
        {
            "id": "f.sql:0",
            "file_path": "f.sql",
            "statement_index": 0,
            "sql": "SELECT col1 FROM db.s.tbl",
            "kind": "SELECT",
            "target_table": "",
            "parse_failed": False,
            "confidence": 1.0,
            "parsing_mode": "full",
            "start_line": 1,
        }
    ]
    buf.selects_from_edges = [{"src_key": "f.sql:0", "dst_key": "db.s.tbl"}]
    buf.query_defined_in_edges = [{"src_key": "f.sql:0", "dst_key": "f.sql"}]

    # Capture how many times upsert_edges_bulk is called with HAS_COLUMN
    has_column_call_count = 0
    original_upsert_edges_bulk = db.upsert_edges_bulk

    def counting_upsert_edges_bulk(src_label, dst_label, rel_type, rows):
        nonlocal has_column_call_count
        if rel_type == "HAS_COLUMN":
            has_column_call_count += 1
        return original_upsert_edges_bulk(src_label, dst_label, rel_type, rows)

    with patch.object(db, "upsert_edges_bulk", side_effect=counting_upsert_edges_bulk):
        with db.transaction():
            _flush_row_batch(db, buf)

    assert has_column_call_count == 1, (
        f"upsert_edges_bulk(HAS_COLUMN) must be called exactly once per batch "
        f"(usage rows ride along), got {has_column_call_count}"
    )

    # And the usage row must actually be in the DB
    rows = db.run_read(
        'SELECT * FROM "HAS_COLUMN" WHERE src_key = ?',
        {"src_key": "db.s.tbl"},
    )
    assert len(rows) == 1, f"Expected 1 usage HAS_COLUMN row, got {len(rows)}"
    assert rows[0]["source"] == "usage"
    assert rows[0]["dst_key"] == "db.s.tbl.col1"


def test_multiple_files_same_physical_table_deduped(db):
    """Multiple files reading same table column produce a single HAS_COLUMN row.

    Guards that set-dedup works correctly across batches.
    """
    sql1 = "INSERT INTO mydb.s.dst1 SELECT amount FROM mydb.s.orders"
    sql2 = "INSERT INTO mydb.s.dst2 SELECT amount, order_id FROM mydb.s.orders"

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "f1.sql").write_text(sql1)
        (Path(tmpdir) / "f2.sql").write_text(sql2)
        indexer = Indexer()
        indexer.index_repo(Path(tmpdir), dialect=None, db=db, use_git=False)

    rows = _has_column_rows(db, "mydb.s.orders")
    dst_cols = {r["dst_key"] for r in rows}

    # amount appears in both files — must appear exactly once
    assert "mydb.s.orders.amount" in dst_cols
    # order_id from f2 only
    assert "mydb.s.orders.order_id" in dst_cols

    # No duplicates
    assert len(rows) == len(dst_cols), "Duplicate HAS_COLUMN rows detected"
