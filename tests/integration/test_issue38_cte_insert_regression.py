"""Regression test for GitHub issue #38 — CTE-built INSERT target dead-end.

Issue #38 reported that when a target column is populated via a CTE
(``INSERT INTO t (...) WITH agg AS (SELECT SUM(s.x) AS m FROM src s) SELECT m FROM agg``),
``analyze upstream t.col`` resolved only to the synthetic ``agg.col`` CTE node, and that
node had **no upstream edges** — severing the chain to the real source tables.

This test covers the **exact verbatim repro** from the issue, which is NOT exercised by the
existing CTE recall guards:

- ``test_cte_recall_guard.py`` — multi-hop column pass-through (``col_a FROM staging.src_a``),
  not a single-CTE aggregation (``SUM(s.x)``).
- ``test_v141_surface_guards.py`` — 4-hop chain + UNION ALL, all column pass-throughs.

The aggregation CTE with ``SUM()`` has a different sqlglot AST structure.  A future
regression that drops single-hop aggregation CTEs would not be caught by the existing guards.

Fixed by: ``fix(#38,#39,#40)`` commit (v1.1.2) — Half A emits src SqlTable nodes;
Half B inverts the kind-filter to OPTIONAL MATCH so node-less sources still pass.
Confirmed NOT reproducing on v1.4.1.
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Exact verbatim repro from issue #38
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE staging.src (x NUMBER);
CREATE TABLE mart.fact_t (m NUMBER);
CREATE TABLE mart.fact_t2 (c NUMBER);
"""

# CTE-built target (was BROKEN in v1.1.0 — the dead-end case)
_CTE_INSERT_SQL = """\
INSERT INTO mart.fact_t (m)
WITH agg AS (SELECT SUM(s.x) AS m FROM staging.src s)
SELECT m FROM agg;
"""

# Direct INSERT (control — was always correct)
_DIRECT_INSERT_SQL = """\
INSERT INTO mart.fact_t2 (c) SELECT s.x AS c FROM staging.src s;
"""


@pytest.fixture
def db():
    """Fresh in-memory DuckDB with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def indexed_db(db, tmp_path):
    """Index the exact #38 repro corpus; return (db, tmp_path)."""
    (tmp_path / "ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "etl_cte.sql").write_text(_CTE_INSERT_SQL)
    (tmp_path / "etl_direct.sql").write_text(_DIRECT_INSERT_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db, tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_issue38_cte_insert_filtered_upstream_reaches_source(indexed_db):
    """Filtered upstream of CTE-built mart.fact_t.m reaches staging.src.x.

    In v1.1.0 this returned "No results" because the agg.m CTE node had no
    inbound edges.  Fixed in v1.1.2.  A regression here means the single-CTE
    aggregation INSERT chain is severed.
    """
    db, _ = indexed_db
    query = _upstream_sql(5, include_intermediate=False)
    rows = db.run_read(query, {"ref": "mart.fact_t.m"})
    ids = {r["id"] for r in rows}

    assert ids, (
        "Filtered upstream of mart.fact_t.m returned no results.\n"
        "In v1.1.0 this was the #38 dead-end: agg.m had no inbound edges.\n"
        "Check that Half A (src-table SqlTable emission) + Half B (OPTIONAL MATCH filter) "
        "are both present."
    )
    assert "staging.src.x" in ids, (
        f"staging.src.x not in filtered upstream of mart.fact_t.m.\n"
        f"Returned: {sorted(ids)}\n"
        "The CTE-insert chain is severed — regression of issue #38."
    )


def test_issue38_cte_node_is_not_dead_end(indexed_db):
    """The intermediate CTE node agg.m has inbound edges (is not a dead-end).

    The v1.1.0 bug: agg.m had 0 incoming COLUMN_LINEAGE edges — it was an island.
    With --include-intermediate the walker reached agg.m but could go no further.
    This test asserts the CTE node is connected to its source.
    """
    db, _ = indexed_db
    rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": "agg.m"})
    upstream_ids = {r["id"] for r in rows}

    assert upstream_ids, (
        "agg.m has no upstream nodes via TRACE_COLUMN_LINEAGE — it is a dead-end.\n"
        "This reproduces the exact v1.1.0 bug reported in issue #38.\n"
        "Returned: []"
    )
    assert "staging.src.x" in upstream_ids, (
        f"agg.m upstream via TRACE_COLUMN_LINEAGE does not include staging.src.x.\n"
        f"Returned: {sorted(upstream_ids)}"
    )


def test_issue38_include_intermediate_shows_agg_and_source(indexed_db):
    """With --include-intermediate, upstream shows both agg.m and staging.src.x.

    In v1.1.0 with --include-intermediate --raw, only agg.m appeared (then traversal
    stopped).  Now both the CTE node and its source must be present.
    """
    db, _ = indexed_db
    query = _upstream_sql(5, include_intermediate=True)
    rows = db.run_read(query, {"ref": "mart.fact_t.m"})
    ids = {r["id"] for r in rows}

    assert ids, "Upstream with include_intermediate=True returned no results for mart.fact_t.m."
    assert "agg.m" in ids, (
        f"CTE intermediate agg.m missing from include_intermediate upstream.\n"
        f"Returned: {sorted(ids)}"
    )
    assert "staging.src.x" in ids, (
        f"Physical source staging.src.x missing from include_intermediate upstream.\n"
        f"Returned: {sorted(ids)}"
    )


def test_issue38_direct_insert_control_still_works(indexed_db):
    """Direct INSERT (no CTE) still resolves to staging.src.x — the control case.

    In v1.1.0 direct INSERT still worked; this guards against a fix that
    breaks the non-CTE path as a side-effect.
    """
    db, _ = indexed_db
    query = _upstream_sql(5, include_intermediate=False)
    rows = db.run_read(query, {"ref": "mart.fact_t2.c"})
    ids = {r["id"] for r in rows}

    assert ids, (
        "Filtered upstream of mart.fact_t2.c (direct INSERT) returned no results.\n"
        "The direct INSERT path should always work — something broke the non-CTE case."
    )
    assert "staging.src.x" in ids, (
        f"staging.src.x not in filtered upstream of mart.fact_t2.c.\nReturned: {sorted(ids)}"
    )
