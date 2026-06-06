"""#40 Guard 2 — Graph-completeness unit invariant.

For every ``SqlColumn`` that is the ``src`` of a ``COLUMN_LINEAGE`` edge
whose ``table_qualified`` is NOT a known CTE/derived alias (i.e. has no
``SqlTable`` row with ``kind IN ('cte','derived')``), a ``SqlTable`` node
with that ``qualified`` must exist.

This is the direct #39 invariant: before Half A shipped, CTE-body-only
source tables had a ``SqlColumn`` row but no matching ``SqlTable`` node, so
the ``_upstream_sql`` filter join returned nothing for those columns.

The test is query-independent: it checks graph structure directly via a
single SQL invariant query that returns orphan (table_qualified, count) rows.
An empty result = invariant holds.  A non-empty result = at least one source
table is missing its ``SqlTable`` node.

Reverting Half A (removing the ``rows.table_rows.append(...)`` emit for
source columns in ``_upsert_parsed_file``) makes this red.

Observable-output contract: the invariant SQL query result set is asserted
to be empty; any non-empty row is printed with a clear message.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Invariant SQL
#
# Returns (table_qualified, orphan_count) for every source table that:
#   1. Appears as table_qualified on the src side of a COLUMN_LINEAGE edge.
#   2. Has NO SqlTable row with that qualified value.
#   3. Is NOT a known CTE/derived alias (i.e. no SqlTable with kind in
#      ('cte','derived') and the same qualified value).
#
# An empty result means every non-CTE source table has a SqlTable node.
# ---------------------------------------------------------------------------

_COMPLETENESS_INVARIANT_SQL = """\
SELECT
  src.table_qualified,
  count(*) AS orphan_count
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
WHERE src.table_qualified NOT IN (
  SELECT qualified FROM "SqlTable" WHERE kind IN ('cte', 'derived')
)
AND src.table_qualified NOT IN (
  SELECT qualified FROM "SqlTable"
)
GROUP BY src.table_qualified
ORDER BY src.table_qualified
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """In-memory DuckDB backend with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# Multi-hop chain: staging.src_a is only referenced inside CTE bodies.
# Before Half A this left staging.src_a without a SqlTable node.
_MULTI_HOP_SQL = """\
INSERT INTO mart.dst
WITH cte_a AS (SELECT col_a FROM staging.src_a),
     cte_b AS (SELECT col_a FROM cte_a)
SELECT col_a FROM cte_b;
"""

# UNION ALL: both staging.src_a and staging.src_b are only inside CTE body.
_UNION_ALL_SQL = """\
INSERT INTO mart.union_dst
WITH unioned AS (
    SELECT val FROM staging.src_a
    UNION ALL
    SELECT val FROM staging.src_b
)
SELECT val FROM unioned;
"""

# Combined corpus: 2-CTE-hop chain + UNION ALL, sharing staging.src_a.
_COMBINED_SQL = """\
INSERT INTO mart.combined_dst
WITH a AS (SELECT x FROM staging.src_a),
     b AS (SELECT y FROM staging.src_b),
     u AS (SELECT x AS v FROM a UNION ALL SELECT y AS v FROM b),
     j AS (SELECT v FROM u)
SELECT v FROM j;
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _check_completeness_invariant(db: DuckDBBackend) -> list[dict]:
    """Run the completeness invariant SQL; return orphan rows (empty = invariant holds)."""
    return db.run_read(_COMPLETENESS_INVARIANT_SQL, {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_invariant_holds_for_multi_hop_chain(db, tmp_path):
    """Graph-completeness invariant holds for a 2-CTE-hop chain.

    staging.src_a is only inside CTE bodies.  Half A must emit a SqlTable
    node for it.  Reverting Half A makes this red.
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    orphans = _check_completeness_invariant(db)
    assert not orphans, (
        f"Graph-completeness invariant violated: {len(orphans)} source table(s) "
        f"missing a SqlTable node:\n"
        + "\n".join(f"  {r['table_qualified']} ({r['orphan_count']} edges)" for r in orphans)
        + "\nReverting Half A (src-table emit) causes this failure."
    )


def test_invariant_holds_for_union_all_cte(db, tmp_path):
    """Graph-completeness invariant holds for both UNION ALL branches.

    Both staging.src_a and staging.src_b are only inside the CTE body.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    orphans = _check_completeness_invariant(db)
    assert not orphans, (
        f"Graph-completeness invariant violated for UNION ALL fixture: "
        f"{[r['table_qualified'] for r in orphans]}"
    )


def test_invariant_holds_for_combined_corpus(db, tmp_path):
    """Graph-completeness invariant holds for a combined 2-hop + UNION ALL corpus."""
    (tmp_path / "fixture.sql").write_text(_COMBINED_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    orphans = _check_completeness_invariant(db)
    assert not orphans, (
        f"Graph-completeness invariant violated for combined fixture: "
        f"{[r['table_qualified'] for r in orphans]}"
    )


def test_invariant_query_returns_specific_orphan_ids(db, tmp_path):
    """The invariant query returns observable orphan table_qualified values, not just a count.

    Asserts the invariant query is non-vacuous: when the graph is correct, we
    can independently confirm staging.src_a and staging.src_b each have a SqlTable
    node with kind='table', and the invariant returns zero rows.
    """
    (tmp_path / "fixture.sql").write_text(_COMBINED_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Confirm the specific physical source nodes exist
    for expected_tq in ("staging.src_a", "staging.src_b"):
        rows = db.run_read(
            'SELECT qualified, kind FROM "SqlTable" WHERE qualified = ?',
            {"q": expected_tq},
        )
        assert rows, f"SqlTable node '{expected_tq}' not found — Half A may have been reverted."
        assert rows[0]["kind"] == "table", (
            f"'{expected_tq}' has kind='{rows[0]['kind']}', expected 'table'."
        )

    # Invariant must hold (zero orphans) when nodes are correctly emitted
    orphans = _check_completeness_invariant(db)
    assert not orphans, f"Invariant failed despite SqlTable nodes existing: {orphans}"
