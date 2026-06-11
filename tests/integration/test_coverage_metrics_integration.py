"""Integration tests for the gain coverage metrics module.

Uses a real in-memory DuckDB fixture graph so the queries are exercised against
actual data — not mocked.

Plan (legacy 6 fields): plan/sprints/gain_coverage_metrics.md §Integration test
Plan (PR 1 — strict/scoped/phantom-split/recall):
  plan/sprints/graph_health_catalog_and_metrics.md §3
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
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
            {
                "qualified": f"mydb.sch.{name}",
                "name": name,
                "catalog": "mydb",
                "db": "mydb",
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


# ---------------------------------------------------------------------------
# PR 1 fixture — strict/scoped/phantom-split/recall
#
# A second, independent fixture so the legacy exact-count assertions above
# (catalogued_tables=2, total_tables=5, good_edges=3, total_edges=6,
# phantom_edges=2, blindspot_tables=2) are not perturbed.
#
# Tables (4 total):
#   - "mydb.sch.t_strict"  kind='table', HAS_COLUMN: t_strict.id, t_strict.name
#   - "mydb.sch.t_contra"  kind='table', HAS_COLUMN: t_contra.id  (NOT t_contra.name)
#   - "mydb.sch.cte_mid"   kind='cte'    (no HAS_COLUMN — chain intermediate)
#   - "mydb.sch.t_unverif" NOT in SqlTable at all (uncatalogued)
#
# COLUMN_LINEAGE (4 edges):
#   (a) strict-good:        stg.id        -> t_strict.id     (inferred=False)
#       — exact dst_key exists in HAS_COLUMN.dst_key.
#   (b) contradicted phantom: stg.name    -> t_contra.name   (inferred=True)
#       — t_contra IS catalogued (t_contra.id exists) but t_contra.name does not:
#         table-level legacy = good, strict = bad, phantom = contradicted.
#   (c) CTE chain:          stg.amount    -> cte_mid.amount   (inferred=False)
#       — cte_mid.kind='cte' => excluded from the scoped denominator.
#   (d) phantom-confirmed:  stg.id2       -> t_strict.id2     (inferred=True)
#       — t_strict.id2 IS in HAS_COLUMN.dst_key => phantom_confirmed AND
#         strict-good (the two classifications are independent: a phantom edge
#         can still be strict-good if the guessed column happens to exist).
#
# HAS_COLUMN (3 rows):
#   t_strict -> t_strict.id    (source='ddl')
#   t_strict -> t_strict.id2   (source='ddl')
#   t_contra -> t_contra.id    (source='ddl')
#
# Recall fixture:
#   - Repo "myrepo" at /repo, File "/repo/etl/q1.sql" (parse_failed=False),
#     File "/repo/etl/q2.sql" (parse_failed=True), File "/repo/ddl/d1.sql" (parse_failed=False)
#   - SqlQuery "/repo/etl/q1.sql:0" kind='SELECT' parse_failed=False (lineage edge a's query_id)
#   - SqlQuery "/repo/etl/q2.sql:0" kind='SELECT' parse_failed=True  (degraded)
#   - SqlQuery "/repo/etl/q3.sql:0" kind='INSERT' parse_failed=False, ZERO outgoing
#     COLUMN_LINEAGE rows -> counts toward zero_edge_write_queries
#   - SchemaVersion indexed_sha = 'deadbeef'
#
# Expected (PR 1 fields):
#   good_edges_strict   = 2   (edges a and d — both have an exact HAS_COLUMN.dst_key match)
#   total_edges         = 4
#   good_edges_scoped   = 2, total_edges_scoped = 3  (edge c excluded — cte_mid)
#   phantom_confirmed    = 1  (edge d)
#   phantom_contradicted = 1  (edge b)
#   phantom_unverified    = 0
#   degraded_parse_total   = 3 (q1, q2, q3)
#   degraded_parse_queries = 1 (q2)
#   zero_edge_write_queries = 1 (q3 — INSERT with no outgoing COLUMN_LINEAGE)
#   total_write_queries    = 1
#   files_indexed = 3
#   indexed_sha = 'deadbeef'
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pr1_fixture_backend():
    """Build and yield an in-memory DuckDB graph exercising PR 1 metrics."""
    b = DuckDBBackend(":memory:")
    b.init_schema()

    # SqlTable nodes
    tables = [
        ("mydb.sch.t_strict", "t_strict", "table"),
        ("mydb.sch.t_contra", "t_contra", "table"),
        ("mydb.sch.cte_mid", "cte_mid", "cte"),
    ]
    for qualified, name, kind in tables:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
            {"qualified": qualified, "name": name, "catalog": "mydb", "db": "mydb", "kind": kind},
        )

    # HAS_COLUMN rows (catalog)
    has_column_rows = [
        ("mydb.sch.t_strict", "mydb.sch.t_strict.id", "ddl"),
        ("mydb.sch.t_strict", "mydb.sch.t_strict.id2", "ddl"),
        ("mydb.sch.t_contra", "mydb.sch.t_contra.id", "ddl"),
    ]
    for src, dst, source in has_column_rows:
        b.run_write(
            'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
            {"src_key": src, "dst_key": dst, "source": source},
        )

    # Repo + File rows for the corpus fingerprint and recall-by-dir queries
    b.run_write(
        'INSERT INTO "Repo" (path, name) VALUES (?, ?)',
        {"path": "/repo", "name": "myrepo"},
    )
    files = [
        ("/repo/etl/q1.sql", False),
        ("/repo/etl/q2.sql", True),
        ("/repo/ddl/d1.sql", False),
    ]
    for path, parse_failed in files:
        b.run_write(
            'INSERT INTO "File" (path, repo_path, sha, dialect, parse_failed)'
            " VALUES (?, ?, ?, ?, ?)",
            {
                "path": path,
                "repo_path": "/repo",
                "sha": "abc",
                "dialect": "snowflake",
                "parse_failed": parse_failed,
            },
        )

    # SqlQuery rows
    queries = [
        # (id, file_path, kind, parse_failed)
        ("/repo/etl/q1.sql:0", "/repo/etl/q1.sql", "SELECT", False),
        ("/repo/etl/q2.sql:0", "/repo/etl/q2.sql", "SELECT", True),
        ("/repo/etl/q3.sql:0", "/repo/etl/q3.sql", "INSERT", False),
    ]
    for qid, file_path, kind, parse_failed in queries:
        b.run_write(
            'INSERT INTO "SqlQuery" (id, file_path, statement_index, sql, kind,'
            " target_table, parse_failed, confidence, parsing_mode, start_line)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            {
                "id": qid,
                "file_path": file_path,
                "statement_index": 0,
                "sql": "SELECT 1",
                "kind": kind,
                "target_table": "",
                "parse_failed": parse_failed,
                "confidence": 1.0,
                "parsing_mode": "sqlglot",
                "start_line": 1,
            },
        )

    # COLUMN_LINEAGE edges
    lineage_rows = [
        # (src_key, dst_key, inferred_from_source_name, query_id)
        ("mydb.sch.stg.id", "mydb.sch.t_strict.id", False, "/repo/etl/q1.sql:0"),
        ("mydb.sch.stg.name", "mydb.sch.t_contra.name", True, "/repo/etl/q1.sql:0"),
        ("mydb.sch.stg.amount", "mydb.sch.cte_mid.amount", False, "/repo/etl/q1.sql:0"),
        ("mydb.sch.stg.id2", "mydb.sch.t_strict.id2", True, "/repo/etl/q1.sql:0"),
    ]
    for src, dst, inferred, query_id in lineage_rows:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, inferred_from_source_name, query_id)'
            " VALUES (?, ?, ?, ?)",
            {
                "src_key": src,
                "dst_key": dst,
                "inferred_from_source_name": inferred,
                "query_id": query_id,
            },
        )

    # SchemaVersion / indexed_sha
    b.set_indexed_sha("deadbeef")

    yield b
    b.close()


def collect_coverage_with(backend: DuckDBBackend):
    """Run collect_coverage() routed to *backend* via run_read_routed patch."""
    from unittest.mock import patch

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed):
        return collect_coverage()


def test_coverage_strict_health_counts_only_exact_dst_column_match(pr1_fixture_backend):
    """Strict column-level health requires cl.dst_key to exist verbatim in HAS_COLUMN.dst_key.

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).

    Of the 4 seeded edges, edges (a) and (d) (stg.id -> t_strict.id and
    stg.id2 -> t_strict.id2) have an exact HAS_COLUMN.dst_key match. Edge (b)
    lands on a catalogued table (t_contra) but the exact column t_contra.name
    is absent — strict-bad despite being table-level-good.
    """
    stats = collect_coverage_with(pr1_fixture_backend)

    assert stats is not None
    assert stats.total_edges == 4
    assert stats.good_edges_strict == 2, (
        f"Expected 2 strict-good edges, got {stats.good_edges_strict}"
    )
    # Legacy table-level health counts edge (b) as good too (t_contra is catalogued):
    # good_edges = a, b, d = 3 > good_edges_strict = a, d = 2.
    assert stats.good_edges > stats.good_edges_strict


def test_coverage_scoped_health_excludes_cte_destination_edges(pr1_fixture_backend):
    """Scoped health excludes edges whose dst table has kind IN ('cte','derived').

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).

    Edge (c) (stg.amount -> cte_mid.amount) targets a kind='cte' table and must
    be excluded from both numerator and denominator of the scoped metric.
    """
    stats = collect_coverage_with(pr1_fixture_backend)

    assert stats is not None
    assert stats.total_edges_scoped == 3, (
        f"Expected 3 scoped edges (4 total minus the CTE-dst edge), got {stats.total_edges_scoped}"
    )
    assert stats.good_edges_scoped == 2, (
        f"Expected 2 scoped-good edges (a and d), got {stats.good_edges_scoped}"
    )


def test_coverage_phantom_split_buckets_confirmed_contradicted_unverified(pr1_fixture_backend):
    """Phantom edges split into confirmed / contradicted / unverified verdicts.

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).

    - Edge (d) (stg.id2 -> t_strict.id2, inferred=True): t_strict.id2 IS in
      HAS_COLUMN.dst_key -> confirmed.
    - Edge (b) (stg.name -> t_contra.name, inferred=True): t_contra is
      catalogued (t_contra.id exists) but t_contra.name does not -> contradicted,
      the worst class of edge (counted "good" by the legacy table-level metric).
    """
    stats = collect_coverage_with(pr1_fixture_backend)

    assert stats is not None
    assert stats.phantom_confirmed == 1, f"Expected 1 confirmed, got {stats.phantom_confirmed}"
    assert stats.phantom_contradicted == 1, (
        f"Expected 1 contradicted, got {stats.phantom_contradicted}"
    )
    assert stats.phantom_unverified == 0, f"Expected 0 unverified, got {stats.phantom_unverified}"


def test_coverage_recall_lines_count_degraded_parse_and_zero_edge_writes(pr1_fixture_backend):
    """Recall lines surface degraded-parse rate and zero-edge write queries.

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).

    - q2 has parse_failed=True -> degraded_parse_queries=1 / degraded_parse_total=3.
    - q3 is an INSERT with zero outgoing COLUMN_LINEAGE -> zero_edge_write_queries=1
      / total_write_queries=1.
    - degraded_parse_by_dir splits by the top-level directory of file_path
      relative to the Repo root. All 3 SqlQuery rows (q1, q2, q3) live under
      etl/ in this fixture (only ddl/d1.sql has no SqlQuery row), so etl/ holds
      the full 1/3 split.
    """
    stats = collect_coverage_with(pr1_fixture_backend)

    assert stats is not None
    assert stats.degraded_parse_total == 3
    assert stats.degraded_parse_queries == 1
    assert stats.zero_edge_write_queries == 1
    assert stats.total_write_queries == 1
    assert "etl" in stats.degraded_parse_by_dir
    assert stats.degraded_parse_by_dir["etl"] == (1, 3)


def test_coverage_corpus_fingerprint_exposes_files_and_indexed_sha(pr1_fixture_backend):
    """Corpus fingerprint reports files indexed, indexed_sha, and a non-empty db_path.

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).
    """
    stats = collect_coverage_with(pr1_fixture_backend)

    assert stats is not None
    assert stats.files_indexed == 3
    assert stats.indexed_sha == "deadbeef"
    assert stats.db_path  # never empty — derived from get_db_path(), not hardcoded


def test_coverage_json_exposes_new_keys_and_legacy_key_unchanged(pr1_fixture_backend):
    """coverage_to_json exposes all PR 1 keys and the legacy six keys, unchanged.

    Guards the honest-metrics PR
    ([plan doc](plan/sprints/graph_health_catalog_and_metrics.md)).
    """
    from sqlcg.cli.coverage import coverage_to_json

    stats = collect_coverage_with(pr1_fixture_backend)
    assert stats is not None

    payload = coverage_to_json(stats)

    # Legacy keys, unchanged meaning
    for key in (
        "catalogued_tables",
        "total_tables",
        "good_edges",
        "total_edges",
        "phantom_edges",
        "blindspot_tables",
    ):
        assert key in payload, f"Legacy key {key!r} missing from coverage_to_json"
    assert payload["good_edges"] == stats.good_edges
    assert payload["total_edges"] == stats.total_edges == 4

    # New PR 1 keys
    for key in (
        "good_edges_strict",
        "edge_health_strict_pct",
        "good_edges_scoped",
        "total_edges_scoped",
        "edge_health_scoped_pct",
        "phantom_confirmed",
        "phantom_contradicted",
        "phantom_unverified",
        "top_blindspot_tables",
        "blindspot_tables_for_80pct",
        "files_indexed",
        "indexed_sha",
        "db_path",
        "index_timestamp",
        "degraded_parse_total",
        "degraded_parse_queries",
        "degraded_parse_by_dir",
        "zero_edge_write_queries",
        "total_write_queries",
    ):
        assert key in payload, f"PR 1 key {key!r} missing from coverage_to_json"

    # PR 4 identity health keys
    for key in ("cte_key_collisions", "rescuable_unqualified_edges"):
        assert key in payload, f"PR 4 key {key!r} missing from coverage_to_json"

    assert payload["good_edges_strict"] == 2
    assert payload["phantom_confirmed"] == 1
    assert payload["phantom_contradicted"] == 1
    assert payload["indexed_sha"] == "deadbeef"
