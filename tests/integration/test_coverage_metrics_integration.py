"""Failing acceptance integration tests for gain_coverage_metrics plan.

Uses a real in-memory DuckDB fixture graph so the four queries are exercised against
actual data — not mocked. Tests must FAIL (or SKIP on missing symbol) before the
developer implements the feature.

Plan: plan/sprints/gain_coverage_metrics.md  §Integration test
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend

try:
    from sqlcg.cli.coverage import collect_coverage  # introduced by gain_coverage_metrics
except ImportError:
    pytest.skip("gain_coverage_metrics not yet implemented", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixture graph
#
# Tables (5 total):
#   - "mydb.sch.orders"       has HAS_COLUMN rows  (catalogued)
#   - "mydb.sch.customers"    has HAS_COLUMN rows  (catalogued)
#   - "mydb.sch.ghost_a"      NO HAS_COLUMN         (blindspot)
#   - "mydb.sch.ghost_b"      NO HAS_COLUMN         (blindspot)
#   - "mydb.sch.extra"        NO HAS_COLUMN, no edges (uncatalogued, not blindspot)
#
# HAS_COLUMN (2 catalogued tables):
#   - src: "mydb.sch.orders",    dst: "mydb.sch.orders.order_id"
#   - src: "mydb.sch.orders",    dst: "mydb.sch.orders.amount"
#   - src: "mydb.sch.customers", dst: "mydb.sch.customers.cust_id"
#
# COLUMN_LINEAGE (6 edges):
#   dst -> catalogued:
#     "mydb.sch.orders.order_id"     (src: "mydb.sch.stg.order_id",   inferred=False)
#     "mydb.sch.orders.amount"       (src: "mydb.sch.stg.amount",      inferred=False)
#     "mydb.sch.customers.cust_id"   (src: "mydb.sch.stg.cust_id",     inferred=True)
#   dst -> uncatalogued (ghost_a / ghost_b):
#     "mydb.sch.ghost_a.col1"        (src: "mydb.sch.stg.col1",        inferred=False)
#     "mydb.sch.ghost_b.col2"        (src: "mydb.sch.stg.col2",        inferred=True)
#     "mydb.sch.ghost_b.col3"        (src: "mydb.sch.stg.col3",        inferred=False)
#                                        ^^^^ same ghost_b table — must count ONCE
#
# Expected results:
#   catalogued_tables = 2  (orders + customers)
#   total_tables      = 5
#   good_edges        = 3  (dst strips to a catalogued table)
#   total_edges       = 6
#   phantom_edges     = 2  (inferred=True: customers.cust_id + ghost_b.col2)
#   blindspot_tables  = 2  (ghost_a + ghost_b — ghost_b counted ONCE despite 2 edges)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_backend():
    """Build and yield an in-memory DuckDB graph with known coverage data."""
    b = DuckDBBackend(":memory:")
    b.init_schema()

    # Insert SqlTable nodes
    for name in ("orders", "customers", "ghost_a", "ghost_b", "extra"):
        b.run_write(
            'INSERT INTO "SqlTable" (id, name, qualified, catalog, db, schema_name, kind)'
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            {
                "id": f"mydb.sch.{name}",
                "name": name,
                "qualified": f"mydb.sch.{name}",
                "catalog": "mydb",
                "db": "mydb",
                "schema_name": "sch",
                "kind": "table",
            },
        )

    # Insert HAS_COLUMN edges (catalogued tables only)
    has_column_rows = [
        ("mydb.sch.orders", "mydb.sch.orders.order_id", "ddl"),
        ("mydb.sch.orders", "mydb.sch.orders.amount", "ddl"),
        ("mydb.sch.customers", "mydb.sch.customers.cust_id", "ddl"),
    ]
    for src, dst, source in has_column_rows:
        b.run_write(
            'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
            {"src_key": src, "dst_key": dst, "source": source},
        )

    # Insert COLUMN_LINEAGE edges
    lineage_rows = [
        # (src_key, dst_key, inferred_from_source_name)
        ("mydb.sch.stg.order_id", "mydb.sch.orders.order_id", False),
        ("mydb.sch.stg.amount", "mydb.sch.orders.amount", False),
        ("mydb.sch.stg.cust_id", "mydb.sch.customers.cust_id", True),
        ("mydb.sch.stg.col1", "mydb.sch.ghost_a.col1", False),
        ("mydb.sch.stg.col2", "mydb.sch.ghost_b.col2", True),
        ("mydb.sch.stg.col3", "mydb.sch.ghost_b.col3", False),
    ]
    for src, dst, inferred in lineage_rows:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, inferred_from_source_name)'
            " VALUES (?, ?, ?)",
            {"src_key": src, "dst_key": dst, "inferred_from_source_name": inferred},
        )

    yield b
    b.close()


def _make_collect(backend: DuckDBBackend):
    """Return a patched collect_coverage that routes to the fixture backend."""

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    return _routed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_coverage_metrics_integration_catalog_coverage(fixture_backend):
    """Catalog coverage equals catalogued-table count / total SqlTable rows.

    Plan: 2 catalogued / 5 total.
    """
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_collect(fixture_backend),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.catalogued_tables == 2, (
        f"Expected 2 catalogued tables, got {stats.catalogued_tables}"
    )
    assert stats.total_tables == 5, f"Expected 5 total tables, got {stats.total_tables}"


def test_coverage_metrics_integration_edge_health(fixture_backend):
    """Edge health counts only edges whose stripped dst key has a HAS_COLUMN row.

    Plan: good_edges=3, total_edges=6. Proves left(dst_key, ...) strip joins correctly
    to HAS_COLUMN.src_key.
    """
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_collect(fixture_backend),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.good_edges == 3, f"Expected 3 good edges, got {stats.good_edges}"
    assert stats.total_edges == 6, f"Expected 6 total edges, got {stats.total_edges}"


def test_coverage_metrics_integration_phantom_count(fixture_backend):
    """Phantom count equals rows flagged inferred_from_source_name=True.

    Plan: phantom_edges=2.
    """
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_collect(fixture_backend),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.phantom_edges == 2, f"Expected 2 phantom edges, got {stats.phantom_edges}"


def test_coverage_metrics_integration_blindspot_distinct_tables(fixture_backend):
    """Blindspot count is distinct dst tables with edges but no HAS_COLUMN — ghost_b counted once.

    Plan: blindspot_tables=2. This is the load-bearing assertion: two edges point at
    ghost_b but it must be counted once (COUNT DISTINCT).
    """
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "sqlcg.cli.coverage.run_read_routed",
        side_effect=_make_collect(fixture_backend),
    ):
        stats = collect_coverage()

    assert stats is not None
    assert stats.blindspot_tables == 2, (
        f"Expected 2 blindspot tables (ghost_a + ghost_b counted once), "
        f"got {stats.blindspot_tables}"
    )
