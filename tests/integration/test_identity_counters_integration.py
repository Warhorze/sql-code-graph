"""Integration tests for identity-health coverage counters (PR 4).

Uses a real in-memory DuckDB fixture to verify the two new queries run against
actual data and produce the expected counts.

Plan: plan/sprints/sprint_lineage_identity_and_session_context.md §PR 4 / Phase 1
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.cli.coverage import collect_coverage
from sqlcg.core.duckdb_backend import DuckDBBackend


def _collect_with(backend: DuckDBBackend):
    """Run collect_coverage() routed to *backend* via run_read_routed patch."""

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed):
        return collect_coverage()


# ---------------------------------------------------------------------------
# CTE key collisions fixture
#
# Graph:
#   SqlTable "cte_shared"  kind='cte'   (the colliding key)
#   SqlTable "real_a"      kind='table'
#   SqlTable "real_b"      kind='table'
#
# SqlQuery:
#   "file_a.sql:0"  file_path="file_a.sql"
#   "file_b.sql:0"  file_path="file_b.sql"
#
# COLUMN_LINEAGE:
#   (a) real_a.x -> cte_shared.x  query_id=file_a.sql:0  (file_a writes to cte_shared)
#   (b) real_b.x -> cte_shared.x  query_id=file_b.sql:0  (file_b also writes to cte_shared)
#   (c) real_a.y -> cte_shared.y  query_id=file_a.sql:0  (second col — still only one
#                                                          colliding key pair, dst=cte_shared.y)
#   (d) real_a.z -> real_b.z      query_id=file_a.sql:0  (non-CTE edge — not counted)
#
# Expected:
#   cte_key_collisions = 1
#     (cte_shared.x is the one dst_key with >1 distinct file_path)
#     (cte_shared.y has only file_a.sql — one file, not colliding)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cte_collision_backend():
    """In-memory DuckDB with a known CTE key collision."""
    b = DuckDBBackend(":memory:")
    b.init_schema()

    for qualified, name, kind in [
        ("cte_shared", "cte_shared", "cte"),
        ("real_a", "real_a", "table"),
        ("real_b", "real_b", "table"),
    ]:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
            {"qualified": qualified, "name": name, "catalog": "", "db": "", "kind": kind},
        )

    # Two SqlQuery rows from different files
    for qid, file_path in [
        ("file_a.sql:0", "file_a.sql"),
        ("file_b.sql:0", "file_b.sql"),
    ]:
        b.run_write(
            'INSERT INTO "SqlQuery" (id, file_path, statement_index, sql, kind,'
            " target_table, parse_failed, confidence, parsing_mode, start_line)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            {
                "id": qid,
                "file_path": file_path,
                "statement_index": 0,
                "sql": "SELECT 1",
                "kind": "INSERT",
                "target_table": "",
                "parse_failed": False,
                "confidence": 1.0,
                "parsing_mode": "sqlglot",
                "start_line": 1,
            },
        )

    # COLUMN_LINEAGE rows
    for src, dst, query_id in [
        ("real_a.x", "cte_shared.x", "file_a.sql:0"),  # file_a -> cte_shared.x
        ("real_b.x", "cte_shared.x", "file_b.sql:0"),  # file_b -> cte_shared.x (collision!)
        ("real_a.y", "cte_shared.y", "file_a.sql:0"),  # file_a -> cte_shared.y (no collision)
        ("real_a.z", "real_b.z", "file_a.sql:0"),  # non-CTE edge
    ]:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, inferred_from_source_name, query_id)'
            " VALUES (?, ?, ?, ?)",
            {
                "src_key": src,
                "dst_key": dst,
                "inferred_from_source_name": False,
                "query_id": query_id,
            },
        )

    yield b
    b.close()


def test_cte_collision_counter_counts_keys_with_multiple_file_writers(cte_collision_backend):
    """CTE collision counter equals the number of CTE dst keys written from >1 file.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Fixture: cte_shared.x receives edges from two files (file_a, file_b) — counts as
    1 collision. cte_shared.y receives edges only from file_a — not a collision.
    The non-CTE edge (real_a.z -> real_b.z) is excluded by the kind filter.
    """
    stats = _collect_with(cte_collision_backend)

    assert stats is not None
    assert stats.cte_key_collisions == 1, (
        f"Expected 1 CTE key collision (cte_shared.x from 2 files), got {stats.cte_key_collisions}"
    )


def test_cte_collision_counter_ignores_non_cte_edges(cte_collision_backend):
    """The CTE collision counter only counts keys that land on kind='cte' or 'derived' tables.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    The edge real_a.z -> real_b.z points at a kind='table' node. Even if it were written
    from two files, it must not appear in cte_collisions.
    """
    stats = _collect_with(cte_collision_backend)

    assert stats is not None
    # Total CTE dst edges: cte_shared.x (×2) + cte_shared.y (×1) = 3.
    # Only cte_shared.x has nf>1, so cte_collisions=1 — not 2 (cte_shared.y is single-file).
    assert stats.cte_key_collisions == 1, (
        f"Expected exactly 1 collision (cte_shared.y must not be counted), "
        f"got {stats.cte_key_collisions}"
    )


# ---------------------------------------------------------------------------
# Rescuable unqualified edges fixture
#
# Graph:
#   SqlTable "doorbelasting"            kind='table'  (bare — no schema)
#   SqlTable "ia_outbound.doorbelasting" kind='table' (schema-qualified)
#   SqlTable "real_qualified"            kind='table'  (qualified, not bare)
#
# HAS_COLUMN:
#   src="ia_outbound.doorbelasting"  dst="ia_outbound.doorbelasting.col1"
#   src="real_qualified"             dst="real_qualified.col1"
#
# COLUMN_LINEAGE:
#   (a) stg.col1 -> doorbelasting.col1            (strict-bad: bare dst, rescuable
#                                                   because 'ia_outbound.doorbelasting'
#                                                   exists schema-qualified in HAS_COLUMN)
#   (b) stg.col1 -> real_qualified.col1            (strict-good: dst in HAS_COLUMN, not counted)
#   (c) stg.col2 -> unknown_bare.col2              (strict-bad, bare, but NO schema-qualified
#                                                   match in HAS_COLUMN — not rescuable)
#
# Expected:
#   rescuable_unqualified_edges = 1  (only edge a)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rescuable_unqualified_backend():
    """In-memory DuckDB with a known rescuable-unqualified edge pattern."""
    b = DuckDBBackend(":memory:")
    b.init_schema()

    for qualified, name, kind in [
        ("doorbelasting", "doorbelasting", "table"),
        ("ia_outbound.doorbelasting", "doorbelasting", "table"),
        ("real_qualified", "real_qualified", "table"),
    ]:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
            {"qualified": qualified, "name": name, "catalog": "", "db": "", "kind": kind},
        )

    # HAS_COLUMN — only the schema-qualified version is catalogued, not the bare one
    for src, dst in [
        ("ia_outbound.doorbelasting", "ia_outbound.doorbelasting.col1"),
        ("real_qualified", "real_qualified.col1"),
    ]:
        b.run_write(
            'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
            {"src_key": src, "dst_key": dst, "source": "ddl"},
        )

    # COLUMN_LINEAGE
    for src, dst, inferred in [
        ("stg.col1", "doorbelasting.col1", False),  # (a) bare rescuable
        ("stg.col1", "real_qualified.col1", False),  # (b) strict-good
        ("stg.col2", "unknown_bare.col2", False),  # (c) bare but not rescuable
    ]:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, inferred_from_source_name)'
            " VALUES (?, ?, ?)",
            {"src_key": src, "dst_key": dst, "inferred_from_source_name": inferred},
        )

    yield b
    b.close()


def test_rescuable_unqualified_counter_counts_bare_dst_with_schema_qualified_catalog_match(
    rescuable_unqualified_backend,
):
    """Rescuable-unqualified counter counts strict-bad bare-dst edges fixable by USE SCHEMA.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Edge (a) lands on 'doorbelasting' (no dot, strict-bad) but 'ia_outbound.doorbelasting'
    exists in HAS_COLUMN — this is a rescuable USE SCHEMA miss.
    Edge (b) is strict-good (dst_key exists in HAS_COLUMN) — not counted.
    Edge (c) is bare but no schema-qualified match exists — not rescuable.
    """
    stats = _collect_with(rescuable_unqualified_backend)

    assert stats is not None
    assert stats.rescuable_unqualified_edges == 1, (
        f"Expected 1 rescuable unqualified edge (edge a), got {stats.rescuable_unqualified_edges}"
    )


def test_rescuable_unqualified_counter_excludes_strict_good_edges(rescuable_unqualified_backend):
    """Rescuable-unqualified counter excludes edges whose dst_key exists verbatim in HAS_COLUMN.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Edge (b) (stg.col1 -> real_qualified.col1) is strict-good — not counted even though
    real_qualified has no dot when stripped to the table name alone. The query filters on
    NOT EXISTS (HAS_COLUMN.dst_key = cl.dst_key) first.
    """
    stats = _collect_with(rescuable_unqualified_backend)

    assert stats is not None
    # If strict-good edges were counted, the total would be >= 2; the rescuable
    # edge (a) alone yields exactly 1.
    assert stats.rescuable_unqualified_edges == 1, (
        f"Expected exactly 1 (strict-good edge b must be excluded), "
        f"got {stats.rescuable_unqualified_edges}"
    )


def test_rescuable_unqualified_counter_excludes_bare_with_no_schema_qualified_match(
    rescuable_unqualified_backend,
):
    """Rescuable-unqualified counter excludes bare-dst edges with no schema-qualified catalog match.

    Guards the identity-counter plan
    ([plan doc](plan/sprints/sprint_lineage_identity_and_session_context.md)).

    Edge (c) (stg.col2 -> unknown_bare.col2): dst table 'unknown_bare' has no dot and
    no schema-qualified entry in HAS_COLUMN — not rescuable, so not counted.
    """
    stats = _collect_with(rescuable_unqualified_backend)

    assert stats is not None
    # If edge (c) were counted, we'd get 2. The correct answer is 1.
    assert stats.rescuable_unqualified_edges == 1, (
        f"Expected 1 (edge c with no schema match excluded), "
        f"got {stats.rescuable_unqualified_edges}"
    )
