"""Issue #38 Phase 5 — column case-normalization seed regression.

The REAL live-DWH blocker behind #38: DDL ``CREATE TABLE`` columns were stored
in their source case (UPPERCASE on the DWH), while ETL ``INSERT...SELECT``
lineage columns are lowercased by ``ColumnRef.__post_init__``. The table-graph
tools seed their downstream closure from ``GET_COLUMNS_FOR_TABLE`` (HAS_COLUMN
dst ids = the DDL columns); when those are uppercase they are DISJOINT from the
lowercase ``COLUMN_LINEAGE`` endpoints, so the BFS dies at depth 1 and never
reaches the CTE-bridged downstream tables — PR #57's correct ordering /
contraction / exclusion are never even reached for those tables.

Fix (Phase 5): lowercase the DDL column-name component at its single origin
(``AnsiParser._extract_defined_columns``) so DDL columns share node identity
with lineage columns.

This fixture deliberately uses **UPPERCASE DDL columns** + a **lowercase ETL
CTE chain** — the exact shape that reproduces the seed starvation. It FAILS
before the Phase 5 fix (s.tableb absent from the closure) and PASSES after.
The existing test_backfill_impact_consistency.py uses same-case columns and so
cannot catch this bug. Asserts on observable output (tool results + raw graph
rows), never "no exception raised".
"""

from __future__ import annotations

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Corpus — UPPERCASE DDL columns (incl. a quoted mixed-case column) feeding a
# lowercase CTE-wrapped INSERT...SELECT bridge s.tablea -> cte_b -> s.tableb.
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE s.tablea (ID INT, MA_AMOUNT INT, "Quoted_Col" INT);
CREATE TABLE s.tableb (ID INT, MA_AMOUNT INT);
"""

_INSERT_B_SQL = """\
INSERT INTO s.tableb (id, ma_amount)
WITH cte_b AS (
    SELECT a.id AS id, a.ma_amount AS ma_amount FROM s.tablea AS a
)
SELECT cte_b.id, cte_b.ma_amount FROM cte_b;
"""

_TARGET = "s.tablea"
_DOWNSTREAM = "s.tableb"
_CTE_B = "cte_b"


@pytest.fixture
def indexed_db(tmp_path, monkeypatch):
    """Index the uppercase-DDL + lowercase-ETL corpus into a fresh in-memory
    DuckDB graph and wire it as the tools backend (mirrors
    test_backfill_impact_consistency.indexed_db)."""
    (tmp_path / "00_ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "01_insert_b.sql").write_text(_INSERT_B_SQL)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend, tmp_path


# ---------------------------------------------------------------------------
# Index-time normalization invariants
# ---------------------------------------------------------------------------


def test_all_sqlcolumn_ids_lowercase(indexed_db):
    """Whole-graph partial-normalization guard: every stored SqlColumn id is
    lowercase. A red here means some column-id construction site (DDL or
    lineage) skipped normalization and re-introduced a case split."""
    db, _ = indexed_db
    offenders = db.run_read('SELECT id FROM "SqlColumn" WHERE id <> lower(id)', {})
    assert offenders == [], f"non-lowercase SqlColumn ids leaked: {offenders}"


def test_uppercase_ddl_columns_stored_lowercase(indexed_db):
    """The UPPERCASE DDL columns (ID, MA_AMOUNT) and the quoted mixed-case
    column ("Quoted_Col") are all stored with lowercase, quote-stripped ids —
    identical to how lineage columns are normalized."""
    db, _ = indexed_db
    rows = db.run_read('SELECT dst_key FROM "HAS_COLUMN" WHERE src_key = ?', {"src_key": _TARGET})
    ids = {r["dst_key"] for r in rows}
    assert "s.tablea.id" in ids
    assert "s.tablea.ma_amount" in ids
    assert "s.tablea.quoted_col" in ids, f"quoted DDL column not lowercased/stripped: {ids}"
    # No uppercase or quoted survivors.
    assert all(i == i.lower() and '"' not in i for i in ids), ids


def test_ddl_and_lineage_columns_share_identity(indexed_db):
    """The DDL HAS_COLUMN dst id and the lineage COLUMN_LINEAGE src id for the
    same column are now the SAME node id (the precondition for a non-starved
    seed) — pre-fix they were s.tablea.ID vs s.tablea.id."""
    db, _ = indexed_db
    has_col = {
        r["dst_key"]
        for r in db.run_read(
            'SELECT dst_key FROM "HAS_COLUMN" WHERE src_key = ?', {"src_key": _TARGET}
        )
    }
    lineage_src = {
        r["src_key"]
        for r in db.run_read(
            'SELECT src_key FROM "COLUMN_LINEAGE" WHERE src_key LIKE ?',
            {"pattern": f"{_TARGET}.%"},
        )
    }
    assert lineage_src, "fixture produced no outgoing lineage from the target"
    assert lineage_src <= has_col, (
        f"lineage src columns not covered by HAS_COLUMN (seed still starved): "
        f"lineage={lineage_src} has_col={has_col}"
    )


# ---------------------------------------------------------------------------
# The payoff — the seed now reaches the CTE-bridged downstream table
# ---------------------------------------------------------------------------


def test_change_scope_reaches_cte_bridged_downstream(indexed_db):
    """get_change_scope(s.tablea) reaches the CTE-bridged s.tableb — the exact
    membership the live DWH was missing. FAILS pre-Phase-5 (uppercase seed
    disjoint from lowercase lineage)."""
    scope = tools.get_change_scope(_TARGET)
    assert _DOWNSTREAM in scope.affected_tables, (
        f"CTE-bridged downstream missing from change scope (seed starved?): {scope.affected_tables}"
    )
    # Synthetic relay node still never leaks as a rebuildable table.
    assert _CTE_B not in scope.affected_tables


def test_backfill_order_reaches_cte_bridged_downstream(indexed_db):
    """get_backfill_order(s.tablea) includes the CTE-bridged s.tableb in the
    rebuild order. FAILS pre-Phase-5 for the same seed-starvation reason."""
    backfill = tools.get_backfill_order(_TARGET)
    assert _DOWNSTREAM in backfill.backfill_order, (
        f"CTE-bridged downstream missing from backfill order: {backfill.backfill_order}"
    )
    assert _CTE_B not in backfill.backfill_order
    assert _TARGET not in backfill.backfill_order  # target filtered from its own set
