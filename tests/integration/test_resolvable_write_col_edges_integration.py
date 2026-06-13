"""Integration tests for the resolvable_write_col_edges metric.

Uses a real in-memory DuckDB graph so the SQL is exercised against actual data.
Amendment A4: asserts field > 0 AND equals an independent direct COUNT(*).

Plan: plan/sprints/column_lineage_recall_metric.md §Test Strategy (Amendment A4).
"""

from __future__ import annotations

import pytest

from sqlcg.cli.coverage import collect_coverage, coverage_to_json
from sqlcg.core.duckdb_backend import DuckDBBackend

# ---------------------------------------------------------------------------
# Fixture graph — non-thin, designed to produce resolvable_write_col_edges > 0.
#
# SqlQuery rows:
#   (a) id="q_insert_with_source:0", kind='INSERT', target_table='mydb.sch.dst',
#       + SELECTS_FROM row (src='mydb.sch.src')
#       + 3 COLUMN_LINEAGE edges → counted (3 edges)
#   (b) id="q_insert_no_source:0",  kind='INSERT', target_table='mydb.sch.dst2'
#       NO SELECTS_FROM row
#       + 2 COLUMN_LINEAGE edges → NOT counted (no resolvable source)
#   (c) id="q_insert_no_edges:0",   kind='INSERT', target_table='mydb.sch.dst3',
#       + SELECTS_FROM row (src='mydb.sch.src3')
#       0 COLUMN_LINEAGE edges → NOT counted (no edges)
#   (d) id="q_select:0",            kind='SELECT'
#       + SELECTS_FROM row (src='mydb.sch.src4')
#       + 1 COLUMN_LINEAGE edge → NOT counted (non-write kind)
#   (e) id="q_write_empty_target:0", kind='INSERT', target_table='' (empty string),
#       + SELECTS_FROM row (src='mydb.sch.src5')
#       + 2 COLUMN_LINEAGE edges → EXCLUDED by target_table <> '' discriminator
#
# Expected resolvable_write_col_edges = 3 (only edges from query (a)).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rwce_backend():
    """Build and yield an in-memory DuckDB graph for the recall-metric tests."""
    b = DuckDBBackend(":memory:")
    b.init_schema()

    # Insert SqlQuery rows
    queries = [
        # (id, kind, target_table)
        ("q_insert_with_source:0", "INSERT", "mydb.sch.dst"),
        ("q_insert_no_source:0", "INSERT", "mydb.sch.dst2"),
        ("q_insert_no_edges:0", "INSERT", "mydb.sch.dst3"),
        ("q_select:0", "SELECT", ""),
        ("q_write_empty_target:0", "INSERT", ""),  # case (e) — excluded by target_table <> ''
    ]
    for qid, kind, target_table in queries:
        b.run_write(
            'INSERT INTO "SqlQuery" (id, file_path, statement_index, sql, kind,'
            " target_table, parse_failed, confidence, parsing_mode, start_line)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            {
                "id": qid,
                "file_path": "/repo/etl/test.sql",
                "statement_index": 0,
                "sql": "SELECT 1",
                "kind": kind,
                "target_table": target_table,
                "parse_failed": False,
                "confidence": 1.0,
                "parsing_mode": "sqlglot",
                "start_line": 1,
            },
        )

    # SELECTS_FROM rows — only for queries (a), (c), (d), (e)
    selects_from_rows = [
        ("q_insert_with_source:0", "mydb.sch.src"),
        ("q_insert_no_edges:0", "mydb.sch.src3"),
        ("q_select:0", "mydb.sch.src4"),
        ("q_write_empty_target:0", "mydb.sch.src5"),
    ]
    for src_key, dst_key in selects_from_rows:
        b.run_write(
            'INSERT INTO "SELECTS_FROM" (src_key, dst_key) VALUES (?, ?)',
            {"src_key": src_key, "dst_key": dst_key},
        )

    # COLUMN_LINEAGE edges
    # (a): 3 edges on q_insert_with_source → should be counted
    # (b): 2 edges on q_insert_no_source → NOT counted (no SELECTS_FROM)
    # (d): 1 edge on q_select → NOT counted (SELECT kind)
    # (e): 2 edges on q_write_empty_target → NOT counted (target_table='')
    lineage_rows = [
        # query_id, src_key, dst_key
        ("q_insert_with_source:0", "mydb.sch.src.col_a", "mydb.sch.dst.col_a"),
        ("q_insert_with_source:0", "mydb.sch.src.col_b", "mydb.sch.dst.col_b"),
        ("q_insert_with_source:0", "mydb.sch.src.col_c", "mydb.sch.dst.col_c"),
        ("q_insert_no_source:0", "mydb.sch.src2.col_x", "mydb.sch.dst2.col_x"),
        ("q_insert_no_source:0", "mydb.sch.src2.col_y", "mydb.sch.dst2.col_y"),
        ("q_select:0", "mydb.sch.src4.col_p", "mydb.sch.dst4.col_p"),
        ("q_write_empty_target:0", "mydb.sch.src5.col_m", "mydb.sch.anon.col_m"),
        ("q_write_empty_target:0", "mydb.sch.src5.col_n", "mydb.sch.anon.col_n"),
    ]
    for query_id, src_key, dst_key in lineage_rows:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE"'
            " (src_key, dst_key, inferred_from_source_name, query_id)"
            " VALUES (?, ?, ?, ?)",
            {
                "src_key": src_key,
                "dst_key": dst_key,
                "inferred_from_source_name": False,
                "query_id": query_id,
            },
        )

    yield b
    b.close()


def collect_coverage_with(backend: DuckDBBackend):
    """Run collect_coverage() routed to *backend* via run_read_routed patch."""
    from unittest.mock import patch

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed):
        return collect_coverage()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resolvable_write_col_edges_integration_nonzero_and_matches_direct_count(rwce_backend):
    """resolvable_write_col_edges is > 0 AND equals an independent direct COUNT(*).

    Amendment A4: guards against the .get() fallback silently returning 0 from a
    wrong/typo'd row key (if the query returns 0, the test would pass trivially).
    The direct COUNT(*) run against the same backend is the independent oracle.

    Guards plan/sprints/column_lineage_recall_metric.md §Test Strategy (Amendment A4).
    """
    from sqlcg.cli.coverage import _Q_RESOLVABLE_WRITE_COL_EDGES

    stats = collect_coverage_with(rwce_backend)
    assert stats is not None

    # Independent direct count from the same backend
    direct_rows = rwce_backend.run_read(_Q_RESOLVABLE_WRITE_COL_EDGES, {})
    assert direct_rows, "Direct COUNT(*) query returned empty result"
    direct_count = int(direct_rows[0]["resolvable_write_col_edges"])

    assert stats.resolvable_write_col_edges > 0, (
        "resolvable_write_col_edges must be > 0 on this fixture "
        "(guards against silent fallback returning 0 on wrong row key)"
    )
    assert stats.resolvable_write_col_edges == direct_count, (
        f"collect_coverage() returned {stats.resolvable_write_col_edges} but "
        f"direct COUNT(*) returned {direct_count}"
    )
    # Confirm the expected fixture value is 3 (only edges from case (a))
    assert stats.resolvable_write_col_edges == 3, (
        f"Expected 3 resolvable_write_col_edges (only query (a)'s 3 edges), "
        f"got {stats.resolvable_write_col_edges}"
    )


def test_resolvable_write_col_edges_integration_json_key_nonzero(rwce_backend):
    """coverage_to_json includes resolvable_write_col_edges key with a non-zero value.

    Guards plan/sprints/column_lineage_recall_metric.md §Step 1.5.
    """
    stats = collect_coverage_with(rwce_backend)
    assert stats is not None

    payload = coverage_to_json(stats)
    assert "resolvable_write_col_edges" in payload, (
        "coverage_to_json must include 'resolvable_write_col_edges' key"
    )
    assert payload["resolvable_write_col_edges"] == 3, (
        f"Expected 3, got {payload['resolvable_write_col_edges']}"
    )


def test_resolvable_write_col_edges_integration_excludes_no_selects_from(rwce_backend):
    """Edges from a write query without SELECTS_FROM are excluded from the metric.

    Case (b): q_insert_no_source has 2 COLUMN_LINEAGE edges but no SELECTS_FROM row.
    The metric must remain 3 (not 5).

    Guards the SELECTS_FROM EXISTS gate in the SQL.
    Guards plan/sprints/column_lineage_recall_metric.md §Test Strategy.
    """
    stats = collect_coverage_with(rwce_backend)
    assert stats is not None
    # 3 edges from (a) + 2 from (b) = 5 without the SELECTS_FROM gate;
    # correct answer is 3 (only (a) edges counted).
    assert stats.resolvable_write_col_edges == 3, (
        f"Expected 3 (b's 2 edges must be excluded); got {stats.resolvable_write_col_edges}"
    )


def test_resolvable_write_col_edges_integration_excludes_empty_target_table(rwce_backend):
    """Write-kind queries with target_table='' are excluded even if they have SELECTS_FROM + edges.

    Case (e): q_write_empty_target has SELECTS_FROM + 2 edges but target_table=''.
    The metric must remain 3.

    Guards the target_table <> '' discriminator (Amendment A3).
    Guards plan/sprints/column_lineage_recall_metric.md §Test Strategy (Amendment A3).
    """
    stats = collect_coverage_with(rwce_backend)
    assert stats is not None
    # Without the discriminator, (e)'s 2 edges would add to the count → 5.
    assert stats.resolvable_write_col_edges == 3, (
        f"Expected 3 (e's 2 edges must be excluded by target_table=''); "
        f"got {stats.resolvable_write_col_edges}"
    )
