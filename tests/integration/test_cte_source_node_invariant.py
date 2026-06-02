"""Guard 2 (#40): Graph-completeness invariant — query-independent.

For every SqlColumn that is a source in a COLUMN_LINEAGE edge whose source
table is NOT a known CTE/derived alias, a corresponding SqlTable node must
exist.  This is directly the #39 bug: CTE-body-only sources had a SqlColumn
but no SqlTable node.

This guard is query-independent: it checks graph structure directly, not via
the CLI or MCP query layer.

Guard asymmetry: this guard is the HALF-A sentinel.  Reverting Half A
(dropping the src-table emit in _build_file_rows) makes this red by leaving
source SqlColumn rows without matching SqlTable nodes.

Observable-output contract: assertions check concrete node id sets.
"""

from __future__ import annotations

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Multi-hop fixture (same as Guard 1)
# ---------------------------------------------------------------------------

# NOTE: source tables are NOT declared via CREATE TABLE — they are only referenced
# inside CTE bodies.  This is the exact #39 scenario that half A fixes.
_MULTI_HOP_SQL = """\
INSERT INTO mart.dst
WITH cte_a AS (SELECT col_a FROM staging.src_a),
     cte_b AS (SELECT col_a FROM cte_a)
SELECT col_a FROM cte_b;
"""

_UNION_ALL_SQL = """\
INSERT INTO mart.union_dst
WITH unioned AS (
    SELECT val FROM staging.src_a
    UNION ALL
    SELECT val FROM staging.src_b
)
SELECT val FROM unioned;
"""


# ---------------------------------------------------------------------------
# Invariant helper
# ---------------------------------------------------------------------------


def _check_completeness(db: KuzuBackend) -> list[str]:  # type: ignore[name-defined]
    """Return list of source table_qualifieds that lack a SqlTable node.

    Algorithm:
      1. Collect all distinct table_qualified values from source SqlColumn nodes
         (i.e. columns on the left side of a COLUMN_LINEAGE edge).
      2. Collect all distinct qualified values from SqlTable nodes whose kind is
         'cte' or 'derived' — these are intentionally-virtual nodes.
      3. For each source table_qualified that is NOT in the CTE/derived set, assert
         a SqlTable node exists with that qualified value.

    Returns a list of missing qualified values (empty = invariant holds).
    """
    # Step 1: distinct table_qualified values from source columns
    src_rows = db.run_read(
        "MATCH (src:SqlColumn)-[:COLUMN_LINEAGE]->() RETURN DISTINCT src.table_qualified AS tq",
        {},
    )
    src_table_qualifieds = {r["tq"] for r in src_rows if r["tq"]}

    # Step 2: known CTE/derived table qualifieds (intentionally virtual)
    cte_rows = db.run_read(
        "MATCH (t:SqlTable) WHERE t.kind IN ['cte', 'derived'] "
        "RETURN DISTINCT t.qualified AS qualified",
        {},
    )
    cte_qualifieds = {r["qualified"] for r in cte_rows if r["qualified"]}

    # Step 3: for each physical source tq, check SqlTable exists
    missing = []
    for tq in sorted(src_table_qualifieds - cte_qualifieds):
        node_rows = db.run_read(
            "MATCH (t:SqlTable {qualified: $tq}) RETURN t.qualified AS qualified LIMIT 1",
            {"tq": tq},
        )
        if not node_rows:
            missing.append(tq)
    return missing


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_multi_hop_cte_source_tables_have_nodes(db, tmp_path):
    """Every non-CTE/derived source table in a 2-CTE-hop chain has a SqlTable node.

    Directly tests the #39 root cause: staging.src_a is only referenced inside
    CTE bodies, so before Half A it had no SqlTable node.

    Reverting Half A (removing the src-table emit) makes this red.
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    missing = _check_completeness(db)
    assert not missing, (
        f"Graph-completeness invariant violated: {len(missing)} source table(s) "
        f"lack a SqlTable node: {missing}\n"
        "Reverting Half A (src-table emit in _build_file_rows) causes this failure."
    )

    # Also assert the specific node exists with the correct kind
    rows = db.run_read(
        "MATCH (t:SqlTable {qualified: 'staging.src_a'}) RETURN t.kind AS kind",
        {},
    )
    assert rows, "staging.src_a SqlTable node not found after indexing."
    assert rows[0]["kind"] == "table", (
        f"staging.src_a SqlTable.kind should be 'table', got '{rows[0]['kind']}'"
    )


def test_union_all_cte_all_source_tables_have_nodes(db, tmp_path):
    """Every source table in a UNION ALL CTE body has a SqlTable node.

    Both staging.src_a and staging.src_b are only referenced inside the CTE body,
    so before Half A they had no SqlTable node.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    missing = _check_completeness(db)
    assert not missing, (
        f"Graph-completeness invariant violated: {len(missing)} source table(s) "
        f"lack a SqlTable node: {missing}"
    )

    for tq in ("staging.src_a", "staging.src_b"):
        rows = db.run_read(
            "MATCH (t:SqlTable {qualified: $tq}) RETURN t.kind AS kind",
            {"tq": tq},
        )
        assert rows, f"{tq} SqlTable node not found after indexing."
        assert rows[0]["kind"] == "table", (
            f"{tq} SqlTable.kind should be 'table', got '{rows[0]['kind']}'"
        )


def test_completeness_invariant_fixture_agnostic(db, tmp_path):
    """The invariant helper works on a generic single-CTE fixture too.

    Confirms the invariant is fixture-agnostic — it derives CTE/derived aliases
    from the graph itself, not a hardcoded list.
    """
    # No CREATE TABLE — public.orders is only in the CTE body (the #39 scenario)
    sql = (
        "INSERT INTO public.summary\n"
        "WITH order_totals AS (SELECT amount FROM public.orders)\n"
        "SELECT amount FROM order_totals;\n"
    )
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    missing = _check_completeness(db)
    assert not missing, f"Completeness invariant violated on simple CTE fixture: {missing}"
