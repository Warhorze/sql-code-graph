"""Integration tests for the empty-impact blast-radius engine (PR 1 — v1.22.0).

Uses real in-memory DuckDB graphs (Indexer().index_repo). All tests assert on
observable output (specific table/column ids), never just "no exception".

Fixtures use schema-qualified, aliased SQL shapes matching the live corpus
(idealized plain-table fixtures hide real failure modes — project history).

Guards plan/sprints/unfilled_table_impact.md PR 1 Test Strategy.
"""

from __future__ import annotations

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.server.tools import _compute_empty_propagation

# ---------------------------------------------------------------------------
# Shared corpus helpers
# ---------------------------------------------------------------------------


def _index(tmp_path, files: dict[str, str]) -> DuckDBBackend:
    """Write files to tmp_path, index them, wire tools._backend, return backend."""
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    tools._backend = backend
    tools._metrics = None
    return backend


def _col_ids(db: DuckDBBackend) -> set[str]:
    rows = db.run_read('SELECT id FROM "SqlColumn"', {})
    return {r["id"] for r in rows}


def _selects_from_targets(db: DuckDBBackend) -> list[dict]:
    return db.run_read(
        "SELECT sf.dst_key AS src_tbl, q.target_table AS dest_tbl"
        ' FROM "SELECTS_FROM" sf'
        ' JOIN "SqlQuery" q ON q.id = sf.src_key'
        " WHERE q.target_table <> ''",
        {},
    )


# ---------------------------------------------------------------------------
# T0a — direct gating join (branch (b) — CAUGHT)
# ---------------------------------------------------------------------------
#
# INSERT INTO da.d SELECT da_other.x FROM da.other AS da_other
#   JOIN da.x AS v ON da_other.k = v.k
#
# d.x is COLUMN_LINEAGE-derived from da.other.x, NOT from da.x.
# But da.x is a direct non-CTE source (SELECTS_FROM) → for source da.x:
#   - d in row_empty_tables (branch (b): gating-join read)
#   - d.x NOT in value_empty_columns (not derived from da.x)


_T0A_DDL = """\
CREATE TABLE da.x (k INT);
CREATE TABLE da.other (k INT, x INT);
CREATE TABLE da.d (x INT);
"""

_T0A_ETL = """\
INSERT INTO da.d (x)
SELECT da_other.x
FROM da.other AS da_other
JOIN da.x AS v ON da_other.k = v.k;
"""


@pytest.fixture
def t0a_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _T0A_DDL, "etl.sql": _T0A_ETL})


def test_direct_gating_join_caught_in_row_empty_tables(t0a_db):
    """For source da.x (gating-join case), da.d appears in row_empty_tables.

    T0a: view 1 value-add — the direct inner-join case that view 2 misses.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T0a.
    """
    result = _compute_empty_propagation(t0a_db, ["da.x"], None)
    assert "da.d" in result.row_empty_tables, (
        f"Expected da.d in row_empty_tables for source da.x. Got: {result.row_empty_tables}"
    )


def test_direct_gating_join_column_not_in_value_empty(t0a_db):
    """da.d.x derives from da.other.x (not from da.x), so da.d.x is NOT in
    value_empty_columns when da.x is the source.

    T0a: confirms view 1 value-add over view 2 — view 2 correctly does NOT flag
    da.d when da.x is the source, because no column of da.d derives from da.x.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T0a.
    """
    result = _compute_empty_propagation(t0a_db, ["da.x"], None)
    # No value_empty_column should have table_qualified = da.d (columns of da.d
    # derive from da.other, not from da.x).
    d_value_cols = [c for c in result.value_empty_columns if c.startswith("da.d.")]
    assert d_value_cols == [], (
        f"Expected no da.d.* in value_empty_columns for source da.x. Got: {d_value_cols}"
    )


def test_t0a_row_superset_of_value_affected(t0a_db):
    """row_empty_tables ⊇ value_affected_tables by construction.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Acceptance Criteria (set-containment).
    """
    result = _compute_empty_propagation(t0a_db, ["da.x"], None)
    value_set = set(result.value_affected_tables)
    row_set = set(result.row_empty_tables)
    assert value_set <= row_set, (
        f"row_empty_tables must be a superset of value_affected_tables. "
        f"Missing: {value_set - row_set}"
    )


# ---------------------------------------------------------------------------
# T0b — CTE-wrapped pure-gating read (DOCUMENTED GAP — NOT caught)
# ---------------------------------------------------------------------------
#
# INSERT INTO da.d
# WITH cte AS (SELECT 1 AS k FROM da.x)
# SELECT da_other.y
# FROM da.other AS da_other JOIN cte ON da_other.k = cte.k
#
# No column derives from da.x, and da.x is only read inside a CTE.
# Neither view detects da.d as affected. This is an accepted known gap
# (parser _real_tables excludes CTE-sourced names — M1 in the plan header).


_T0B_DDL = """\
CREATE TABLE da.x (k INT);
CREATE TABLE da.other (k INT, y INT);
CREATE TABLE da.d (y INT);
"""

_T0B_ETL = """\
INSERT INTO da.d (y)
WITH cte AS (
    SELECT 1 AS k FROM da.x
)
SELECT da_other.y
FROM da.other AS da_other
JOIN cte ON da_other.k = cte.k;
"""


@pytest.fixture
def t0b_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _T0B_DDL, "etl.sql": _T0B_ETL})


def test_cte_wrapped_pure_gating_not_caught_in_row_empty_tables(t0b_db):
    """CTE-wrapped pure-gating read of da.x does NOT appear in row_empty_tables.

    T0b: documents the accepted known gap. The parser's _real_tables excludes
    CTE-sourced names (base.py L604, performance-invariant hot path, out of scope).
    Neither view detects this case — it is a documented, accepted limitation.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T0b.
    """
    result = _compute_empty_propagation(t0b_db, ["da.x"], None)
    assert "da.d" not in result.row_empty_tables, (
        f"da.d should NOT be in row_empty_tables for CTE-wrapped pure-gating read of da.x. "
        f"Got: {result.row_empty_tables}. This is the documented accepted gap (T0b)."
    )


def test_cte_wrapped_pure_gating_not_caught_in_value_empty(t0b_db):
    """CTE-wrapped pure-gating: no da.d.* column is in value_empty_columns.

    T0b: both views miss this case — accepted gap.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T0b.
    """
    result = _compute_empty_propagation(t0b_db, ["da.x"], None)
    d_value_cols = [c for c in result.value_empty_columns if c.startswith("da.d.")]
    assert d_value_cols == [], (
        f"Expected no da.d.* in value_empty_columns for CTE-wrapped pure-gating source da.x. "
        f"Got: {d_value_cols}"
    )


# ---------------------------------------------------------------------------
# T3 — partial impact (view 2 precision)
# ---------------------------------------------------------------------------
#
# da.d has columns x (derived from da.s.a — unfilled source) and y (derived
# from da.f.b — still-filled source). Only d.x goes empty; d.y stays.
# da.d should be in value_partially_empty_tables, NOT value_fully_empty_tables.


_T3_DDL = """\
CREATE TABLE da.s (a INT);
CREATE TABLE da.f (b INT);
CREATE TABLE da.d (x INT, y INT);
"""

_T3_ETL = """\
INSERT INTO da.d (x, y)
SELECT da_s.a AS x, da_f.b AS y
FROM da.s AS da_s
CROSS JOIN da.f AS da_f;
"""


@pytest.fixture
def t3_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _T3_DDL, "etl.sql": _T3_ETL})


def test_partial_impact_affected_column_in_value_empty_cols(t3_db):
    """da.d.x derives from da.s.a — it appears in value_empty_columns when source=da.s.

    T3: view 2 partial-impact correctness.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T3.
    """
    result = _compute_empty_propagation(t3_db, ["da.s"], None)
    assert "da.d.x" in result.value_empty_columns, (
        f"Expected da.d.x in value_empty_columns. Got: {result.value_empty_columns}"
    )


def test_partial_impact_non_affected_column_not_in_value_empty_cols(t3_db):
    """da.d.y derives from da.f.b (still-filled), NOT in value_empty_columns when source=da.s.

    T3: view 2 partial-impact correctness.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T3.
    """
    result = _compute_empty_propagation(t3_db, ["da.s"], None)
    assert "da.d.y" not in result.value_empty_columns, (
        f"da.d.y should NOT be in value_empty_columns when source is da.s. "
        f"Got: {result.value_empty_columns}"
    )


def test_partial_impact_table_in_partially_empty(t3_db):
    """da.d is in value_partially_empty_tables (not fully empty) when source=da.s.

    T3: view 2 partial split.
    Guards plan/sprints/unfilled_table_impact.md PR 1 T3.
    """
    result = _compute_empty_propagation(t3_db, ["da.s"], None)
    assert "da.d" in result.value_partially_empty_tables, (
        f"Expected da.d in value_partially_empty_tables. Got: {result.value_partially_empty_tables}"
    )
    assert "da.d" not in result.value_fully_empty_tables, (
        f"da.d should NOT be in value_fully_empty_tables. Got: {result.value_fully_empty_tables}"
    )


# ---------------------------------------------------------------------------
# Value-fully-empty fixture
# ---------------------------------------------------------------------------
#
# da.d (x, y) — both columns derive from the unfilled source da.s.
# → da.d should be in value_fully_empty_tables.


_FULLY_DDL = """\
CREATE TABLE da.s (a INT, b INT);
CREATE TABLE da.d (x INT, y INT);
"""

_FULLY_ETL = """\
INSERT INTO da.d (x, y)
SELECT da_s.a AS x, da_s.b AS y
FROM da.s AS da_s;
"""


@pytest.fixture
def fully_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _FULLY_DDL, "etl.sql": _FULLY_ETL})


def test_value_fully_empty_table_when_all_columns_derived(fully_db):
    """When all of da.d's columns derive from da.s, da.d is in value_fully_empty_tables.

    Guards plan/sprints/unfilled_table_impact.md PR 1 value-fully-empty fixture.
    """
    result = _compute_empty_propagation(fully_db, ["da.s"], None)
    assert "da.d" in result.value_fully_empty_tables, (
        f"Expected da.d in value_fully_empty_tables. Got: {result.value_fully_empty_tables}"
    )
    assert "da.d" not in result.value_partially_empty_tables


# ---------------------------------------------------------------------------
# N3 — propagation order through a CTE/derived bridge (exercises W1)
# ---------------------------------------------------------------------------
#
# Multi-hop chain: da.s → da.m → da.d
# The hop s→m routes THROUGH a CTE/derived bridge (synthetic node).
# The hop m→d is a direct column-lineage edge.
# Expected propagation_order: [da.m, da.d] (closest-to-source first).
#
# W1 requirement: passing affected_cols alone to _kahn_topological_sort would
# degrade ordering for chains through CTE bridges because the start_col endpoints
# would be absent from the adjacency query's id set. The bridge test is
# deliberately constructed so passing affected_cols alone makes this test fail.


_N3_DDL = """\
CREATE TABLE da.s (id INT, val INT);
CREATE TABLE da.m (id INT, val INT);
CREATE TABLE da.d (id INT, val INT);
"""

_N3_ETL_M = """\
INSERT INTO da.m (id, val)
WITH cte_s AS (
    SELECT da_s.id AS id, da_s.val AS val FROM da.s AS da_s
)
SELECT cte_s.id, cte_s.val FROM cte_s;
"""

_N3_ETL_D = """\
INSERT INTO da.d (id, val)
SELECT da_m.id, da_m.val FROM da.m AS da_m;
"""


@pytest.fixture
def n3_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(
        tmp_path,
        {
            "ddl.sql": _N3_DDL,
            "etl_m.sql": _N3_ETL_M,
            "etl_d.sql": _N3_ETL_D,
        },
    )


def test_propagation_order_through_cte_bridge_closest_first(n3_db):
    """Multi-hop chain da.s→da.m→da.d returns [da.m, da.d] in propagation_order.

    N3: the hop s→m routes through a CTE bridge — exercises the W1 requirement
    that _kahn_topological_sort receives _dedup_preserve_order([*start_cols,
    *affected_cols]). The bridge fixture ensures passing affected_cols alone
    would degrade ordering to alphabetical.
    Guards plan/sprints/unfilled_table_impact.md PR 1 N3 / W1.
    """
    result = _compute_empty_propagation(n3_db, ["da.s"], None)
    order = result.propagation_order

    assert "da.m" in order, f"da.m missing from propagation_order: {order}"
    assert "da.d" in order, f"da.d missing from propagation_order: {order}"
    # da.m must come before da.d (producer before consumer).
    assert order.index("da.m") < order.index("da.d"), (
        f"da.m must precede da.d in propagation_order. Got: {order}"
    )
    # Sources must not appear in propagation_order.
    assert "da.s" not in order, f"Source da.s should not be in propagation_order: {order}"


def test_n3_synthetic_nodes_not_in_any_output_list(n3_db):
    """The CTE bridge node between da.s and da.m never appears in any output list.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E4 / N3 synthetic exclusion.
    """
    result = _compute_empty_propagation(n3_db, ["da.s"], None)
    # Look up CTE-kind tables in the graph.
    cte_rows = n3_db.run_read("SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte'", {})
    cte_ids = {r["qualified"] for r in cte_rows}

    all_output_tables = set(
        result.row_empty_tables + result.value_affected_tables + result.propagation_order
    )
    leaked = cte_ids & all_output_tables
    assert not leaked, f"Synthetic CTE nodes leaked into output table lists: {leaked}"


# ---------------------------------------------------------------------------
# Star-source fixture
# ---------------------------------------------------------------------------
#
# da.d SELECT * FROM da.s → star_expansion creates concrete COLUMN_LINEAGE.
# da.d should appear in both views.


_STAR_DDL = """\
CREATE TABLE da.s (id INT, val INT);
CREATE TABLE da.d (id INT, val INT);
"""

# No INSERT column list + no alias: triggers STAR_SOURCE edge + expand_star_sources
# which materialises concrete COLUMN_LINEAGE at index time.
_STAR_ETL = """\
INSERT INTO da.d SELECT * FROM da.s;
"""


@pytest.fixture
def star_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _STAR_DDL, "etl.sql": _STAR_ETL})


def test_star_source_downstream_in_both_views(star_db):
    """SELECT * from da.s feeding da.d reports da.d in both row_empty_tables and
    value_affected_tables when da.s is the source.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E6 (star-source).
    """
    result = _compute_empty_propagation(star_db, ["da.s"], None)
    # View 2: star-expansion materialises COLUMN_LINEAGE at index time.
    assert "da.d" in result.value_affected_tables, (
        f"Expected da.d in value_affected_tables (star-expansion). "
        f"Got: {result.value_affected_tables}"
    )
    # View 1: STAR_SOURCE edge (or SELECTS_FROM) feeds row_empty_tables.
    assert "da.d" in result.row_empty_tables, (
        f"Expected da.d in row_empty_tables (star-source). Got: {result.row_empty_tables}"
    )


# ---------------------------------------------------------------------------
# Synthetic exclusion (standalone — belt-and-braces)
# ---------------------------------------------------------------------------
#
# A CTE/derived bridge between da.s and da.d must not appear in any output list,
# and da.d must still appear (the bridge is contracted through).


_SYNTH_DDL = """\
CREATE TABLE da.s (id INT, val INT);
CREATE TABLE da.d (id INT, val INT);
"""

_SYNTH_ETL = """\
INSERT INTO da.d (id, val)
WITH cte_bridge AS (
    SELECT da_s.id AS id, da_s.val AS val FROM da.s AS da_s
)
SELECT cte_bridge.id, cte_bridge.val FROM cte_bridge;
"""


@pytest.fixture
def synth_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _SYNTH_DDL, "etl.sql": _SYNTH_ETL})


def test_synthetic_bridge_excluded_da_d_still_in_value_affected(synth_db):
    """A CTE bridge between da.s and da.d is excluded from output; da.d is present.

    Guards plan/sprints/unfilled_table_impact.md PR 1 E4 / synthetic exclusion.
    """
    result = _compute_empty_propagation(synth_db, ["da.s"], None)
    # da.d must appear in value_affected_tables (value-derived through the bridge).
    assert "da.d" in result.value_affected_tables, (
        f"Expected da.d in value_affected_tables through synthetic bridge. "
        f"Got: {result.value_affected_tables}"
    )
    # No synthetic node should appear in output lists.
    cte_rows = synth_db.run_read("SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte'", {})
    cte_ids = {r["qualified"] for r in cte_rows}
    all_output = set(
        result.row_empty_tables + result.value_affected_tables + result.propagation_order
    )
    leaked = cte_ids & all_output
    assert not leaked, f"Synthetic CTE node(s) leaked into output: {leaked}"
