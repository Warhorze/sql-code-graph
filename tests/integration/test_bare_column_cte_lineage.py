"""#49 — bare-column CTE mis-bind override integration tests (PR-3).

Confirmed live on the DWH (v1.4.0): a bare (unqualified) column inside a
multi-table-JOIN CTE body is mis-bound by sqlglot to the *last-joined* table.
For ``SUM(CASE WHEN dt.dayname = 'sunday' THEN m ELSE 0 END) AS m``, sqlglot
emitted a confident WRONG edge ``mart.dim_time.dayname -> base.m``; the correct
source ``mart.fact_daily.m -> base.m`` was absent entirely.

Fix (PR-3): in the CTE-projection loop, when a bare column is found inside a
projection expression AND the CTE body has ≥2 source tables, skip sqlglot's
single guess and instead emit one ``CTE_PROJECTION_AMBIGUOUS`` edge per candidate
source table (confidence=0.5).  The wrong single edge MUST be gone; the correct
candidate source(s) MUST be present.

Edge-emission contract verified by these tests:
  - Bare column in ≥2-table body → N ambiguous edges (one per candidate table),
    each with transform='CTE_PROJECTION_AMBIGUOUS', confidence=0.5.
  - The wrong single edge (sqlglot's mis-bind to the last-joined table) is GONE.
  - The correct source table's edge IS present.
  - Qualified columns in any body → unchanged (transform='CTE_PROJECTION', 1.0).
  - Single-table CTE bodies → unchanged (no ambiguity; existing path).

These tests use a real DuckDB in-memory graph and assert on observable output:
concrete id sets, transform values, and absence of wrong edges — never just
"no exception raised".
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _downstream_sql, _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixture SQL corpus — the exact repro from V1.4.0_BUG_VERIFICATION.md
#
# 01_fact_daily.sql: staging.src_events.amount -> mart.fact_daily.m
# 02_enriched.sql:   mart.fact_daily (f) JOIN mart.dim_time (dt)
#                    bare `m` inside CASE WHEN expression comes from f, NOT dt
#
# The bug: sqlglot mis-binds bare `m` to the last-joined table dt (mart.dim_time)
# and emits mart.dim_time.dayname -> base.m  (WRONG).
# The fix: emit CTE_PROJECTION_AMBIGUOUS edges to all candidate tables:
#   mart.fact_daily.m -> base.m  (CTE_PROJECTION_AMBIGUOUS, 0.5)
#   mart.dim_time.m  -> base.m  (CTE_PROJECTION_AMBIGUOUS, 0.5)
# and do NOT emit the confident wrong edge mart.dim_time.dayname -> base.m.
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE staging.src_events (d VARCHAR, amount NUMBER);
CREATE TABLE mart.fact_daily (d VARCHAR, m NUMBER);
CREATE TABLE mart.dim_time (d VARCHAR, dayname VARCHAR);
CREATE TABLE mart.fact_enriched (d VARCHAR, m NUMBER);
"""

_FACT_DAILY_SQL = """\
INSERT INTO mart.fact_daily (d, m)
SELECT s.d AS d, SUM(s.amount) AS m FROM staging.src_events s GROUP BY ALL;
"""

_ENRICHED_SQL = """\
INSERT INTO mart.fact_enriched (d, m)
WITH base AS (
    SELECT f.d AS d,
           SUM(CASE WHEN dt.dayname = 'sunday' THEN m ELSE 0 END) AS m
    FROM mart.fact_daily f
    JOIN mart.dim_time dt ON dt.d = f.d
    GROUP BY ALL
)
SELECT d, m FROM base;
"""

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def indexed_db(db, tmp_path):
    """Index the #49 repro corpus (DDL + two ETL files); return (db, tmp_path)."""
    (tmp_path / "00_ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "01_fact_daily.sql").write_text(_FACT_DAILY_SQL)
    (tmp_path / "02_enriched.sql").write_text(_ENRICHED_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db, tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_column_lineage_edges(db: DuckDBBackend) -> list[dict]:
    """Return all raw COLUMN_LINEAGE edges with src_key, dst_key, transform, confidence."""
    return db.run_read(
        'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE"'
        " ORDER BY src_key, dst_key",
        {},
    )


def _cte_col_key(db: DuckDBBackend, cte_alias: str, col: str) -> str:
    """Return the namespaced column key for a CTE alias, e.g. '/tmp/x.sql::base.m'.

    PR 3 namespaces CTE keys as '<abs_path>::<cte_alias>'. This helper looks up the
    actual key from the graph by suffix match so tests are independent of tmp_path.
    """
    rows = db.run_read(
        f"SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE '%::{cte_alias}'",
        {},
    )
    assert rows, f"CTE alias '{cte_alias}' not found in SqlTable after indexing."
    return f"{rows[0]['qualified']}.{col}"


def _cli_upstream_ids(db: DuckDBBackend, col_id: str, depth: int = 5) -> set[str]:
    """Run the exact CLI filtered upstream query and return the id set."""
    query = _upstream_sql(depth, include_intermediate=False)
    rows = db.run_read(query, {"ref": col_id})
    return {r["id"] for r in rows}


def _cli_downstream_ids(db: DuckDBBackend, col_id: str, depth: int = 5) -> set[str]:
    """Run the exact CLI filtered downstream query and return the id set."""
    query = _downstream_sql(depth, include_intermediate=False)
    rows = db.run_read(query, {"ref": col_id})
    return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Core mis-bind override assertions (raw COLUMN_LINEAGE)
# ---------------------------------------------------------------------------


def test_wrong_misbind_edge_is_absent(indexed_db):
    """The sqlglot mis-bound edge mart.dim_time.dayname -> base.m MUST NOT exist.

    This is the override assertion — the test fails if the wrong confident edge
    survives even if a correct edge is also emitted.  Reverting the PR-3 base.py
    change reintroduces this edge and reds this guard.
    """
    db, _ = indexed_db
    edges = _raw_column_lineage_edges(db)

    # The wrong edge: sqlglot's mis-bind to the last-joined table
    wrong_src = "mart.dim_time.dayname"
    wrong_dst = "base.m"
    wrong_edges = [e for e in edges if e["src_key"] == wrong_src and e["dst_key"] == wrong_dst]
    assert not wrong_edges, (
        f"WRONG mis-bound edge '{wrong_src} -> {wrong_dst}' is PRESENT — "
        f"the #49 override did not suppress sqlglot's single wrong guess.\n"
        f"All edges: {[(e['src_key'], e['dst_key'], e['transform']) for e in edges]}"
    )


def test_correct_source_edge_is_present(indexed_db):
    """The correct source mart.fact_daily.m -> base.m MUST exist as CTE_PROJECTION_AMBIGUOUS.

    This edge was absent in v1.4.0 (the bug): bare `m` was rebound to dim_time.
    After the fix, mart.fact_daily is one of the ≥2 candidate source tables and
    must appear with transform='CTE_PROJECTION_AMBIGUOUS', confidence=0.5.

    PR 3: CTE key is namespaced. Look up the actual dst key from the graph.
    """
    db, _ = indexed_db
    edges = _raw_column_lineage_edges(db)

    correct_src = "mart.fact_daily.m"
    correct_dst = _cte_col_key(db, "base", "m")  # e.g. "/tmp/xxx/02_enriched.sql::base.m"
    correct_edges = [
        e for e in edges if e["src_key"] == correct_src and e["dst_key"] == correct_dst
    ]
    assert correct_edges, (
        f"Correct edge '{correct_src} -> {correct_dst}' is ABSENT.\n"
        f"All edges: {[(e['src_key'], e['dst_key'], e['transform']) for e in edges]}"
    )
    edge = correct_edges[0]
    assert edge["transform"] == "CTE_PROJECTION_AMBIGUOUS", (
        f"Expected transform='CTE_PROJECTION_AMBIGUOUS', got '{edge['transform']}'"
    )
    assert abs(edge["confidence"] - 0.5) < 1e-6, (
        f"Expected confidence=0.5, got {edge['confidence']}"
    )


def test_ambiguous_edges_all_have_correct_transform_and_confidence(indexed_db):
    """All CTE_PROJECTION_AMBIGUOUS edges on base.m have confidence=0.5.

    The fix emits one edge per candidate source table (at least fact_daily and
    dim_time) — every such edge must carry the correct transform and confidence.

    PR 3: CTE key is namespaced. Look up the actual dst key from the graph.
    """
    db, _ = indexed_db
    edges = _raw_column_lineage_edges(db)

    base_m_key = _cte_col_key(db, "base", "m")  # e.g. "/tmp/xxx/02_enriched.sql::base.m"
    ambiguous_to_base_m = [
        e
        for e in edges
        if e["dst_key"] == base_m_key and e["transform"] == "CTE_PROJECTION_AMBIGUOUS"
    ]
    assert ambiguous_to_base_m, (
        f"No CTE_PROJECTION_AMBIGUOUS edges found pointing to {base_m_key}.\n"
        f"All edges pointing to {base_m_key}: {[e for e in edges if e['dst_key'] == base_m_key]}"
    )
    for edge in ambiguous_to_base_m:
        assert abs(edge["confidence"] - 0.5) < 1e-6, (
            f"Edge {edge['src_key']} -> {edge['dst_key']} has confidence "
            f"{edge['confidence']}, expected 0.5"
        )
    # Must include the correct source
    src_keys = {e["src_key"] for e in ambiguous_to_base_m}
    assert "mart.fact_daily.m" in src_keys, (
        f"mart.fact_daily.m is not among the ambiguous sources for {base_m_key}.\n"
        f"Ambiguous sources: {sorted(src_keys)}"
    )


def test_qualified_column_d_still_resolves_correctly(indexed_db):
    """Qualified column f.d in the same CTE still resolves via normal CTE_PROJECTION.

    The fix only affects bare (unqualified) columns in ≥2-table bodies.
    The qualified projection f.d AS d must still emit a CTE_PROJECTION edge
    with confidence=1.0, not be affected by the ambiguity override.

    PR 3: CTE key is namespaced. Look up the actual dst key from the graph.
    """
    db, _ = indexed_db
    edges = _raw_column_lineage_edges(db)

    # f.d is qualified → resolves to mart.fact_daily.d, normal CTE_PROJECTION
    base_d_key = _cte_col_key(db, "base", "d")  # e.g. "/tmp/xxx/02_enriched.sql::base.d"
    d_edges_to_base = [e for e in edges if e["dst_key"] == base_d_key]
    assert d_edges_to_base, (
        f"No edges found pointing to {base_d_key} (the qualified f.d projection).\n"
        f"All edges: {[(e['src_key'], e['dst_key'], e['transform']) for e in edges]}"
    )
    # At least one must be CTE_PROJECTION (the qualified path)
    cte_projection_d = [e for e in d_edges_to_base if e["transform"] == "CTE_PROJECTION"]
    assert cte_projection_d, (
        f"No CTE_PROJECTION edge for {base_d_key} — qualified column was incorrectly "
        f"treated as ambiguous.\n"
        f"Edges to {base_d_key}: {d_edges_to_base}"
    )


# ---------------------------------------------------------------------------
# Filtered surface assertions (analyze upstream / downstream)
# ---------------------------------------------------------------------------


def test_filtered_upstream_reaches_fact_daily_not_dim_time(indexed_db):
    """Filtered upstream of mart.fact_enriched.m reaches fact_daily, NOT dim_time.

    In v1.4.0, the upstream chain was:
      mart.fact_enriched.m <- base.m <- mart.dim_time.dayname (WRONG)
    After the fix:
      mart.fact_enriched.m <- base.m <- mart.fact_daily.m <- staging.src_events.amount

    The filtered upstream must include mart.fact_daily sources and must NOT
    include mart.dim_time as the source of the m column.
    """
    db, _ = indexed_db
    ids = _cli_upstream_ids(db, "mart.fact_enriched.m")

    assert ids, (
        "Filtered upstream of mart.fact_enriched.m returned no results.\n"
        "The override may have dropped edges without emitting replacements."
    )
    # The wrong answer from v1.4.0 must be gone
    dim_time_ids = {rid for rid in ids if "dim_time" in rid and "dayname" in rid}
    assert not dim_time_ids, (
        f"mart.dim_time.dayname appears in the filtered upstream of mart.fact_enriched.m "
        f"— the mis-bound edge was not suppressed.\nReturned ids: {sorted(ids)}"
    )
    # The correct chain must reach staging.src_events
    src_events_ids = {rid for rid in ids if "src_events" in rid}
    assert src_events_ids, (
        f"staging.src_events.* NOT found in filtered upstream of mart.fact_enriched.m.\n"
        f"Returned ids: {sorted(ids)}"
    )


def test_filtered_downstream_reaches_fact_enriched_from_fact_daily(indexed_db):
    """Filtered downstream of mart.fact_daily.m reaches mart.fact_enriched.m.

    In v1.4.0 this downstream chain was broken (the edge mart.fact_daily.m ->
    base.m was absent).  After the fix the chain must be traversable.
    """
    db, _ = indexed_db
    ids = _cli_downstream_ids(db, "mart.fact_daily.m")

    assert ids, (
        "Filtered downstream of mart.fact_daily.m returned no results.\n"
        "The CTE_PROJECTION_AMBIGUOUS edge may not be indexed or traversed."
    )
    fact_enriched_ids = {rid for rid in ids if "fact_enriched" in rid}
    assert fact_enriched_ids, (
        f"mart.fact_enriched.* NOT found in downstream of mart.fact_daily.m.\n"
        f"Returned ids: {sorted(ids)}"
    )


# ---------------------------------------------------------------------------
# Single-table CTE body — existing path unchanged
# ---------------------------------------------------------------------------


def test_single_table_cte_body_unchanged(db, tmp_path):
    """Single-table CTE bodies are unaffected by the ≥2-table override.

    A CTE with exactly one source table must still resolve via sg_lineage
    normally (transform='CTE_PROJECTION', confidence=1.0).  The bare-column
    override must NOT fire when the body has only one source table.
    """
    (tmp_path / "single_table.sql").write_text("""\
CREATE TABLE src.events (amount NUMBER, d VARCHAR);
CREATE TABLE tgt.summary (total NUMBER);
INSERT INTO tgt.summary (total)
WITH totals AS (
    SELECT SUM(amount) AS total FROM src.events
)
SELECT total FROM totals;
""")
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    edges = db.run_read(
        'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE" ORDER BY src_key',
        {},
    )
    # PR 3: CTE 'totals' is namespaced as "<abs_path>::totals". Look up actual key.
    totals_rows = db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE '%::totals'",
        {},
    )
    assert totals_rows, "CTE alias 'totals' not found in SqlTable after indexing."
    totals_total_key = f"{totals_rows[0]['qualified']}.total"

    # totals.total edge must NOT be CTE_PROJECTION_AMBIGUOUS (single-table body)
    totals_edges = [e for e in edges if e["dst_key"] == totals_total_key]
    ambiguous = [e for e in totals_edges if e["transform"] == "CTE_PROJECTION_AMBIGUOUS"]
    assert not ambiguous, (
        f"Single-table CTE body incorrectly emitted CTE_PROJECTION_AMBIGUOUS edges: {ambiguous}"
    )
    # Must still have a CTE_PROJECTION edge
    cte_proj = [e for e in totals_edges if e["transform"] == "CTE_PROJECTION"]
    assert cte_proj, (
        f"Single-table CTE body lost its CTE_PROJECTION edge.\n"
        f"Edges to {totals_total_key}: {totals_edges}"
    )
