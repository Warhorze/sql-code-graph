"""Bug #6 gate: does `analyze impact <T>` under-report consumers that
`downstream` / `empty-impact` surface?

Governing plan: PR-B of the impact-analysis validation sprint
([plan doc](plan/sprints/bugfix_impact_analysis_validation.md), branch
``plan/bugfix-impact-analysis``).

GATE OUTCOME: NOT REPRODUCED. The bug did not reproduce in standalone fixtures
during planning, and a bounded developer-side capture effort (this file) could
not reproduce it either. These tests pin the *parity invariant* that actually
holds for every shape the plan named, so a future regression that breaks it
would turn the ``parametrize`` cases red — and the live shape, if ever isolated,
can be dropped straight into ``IMPACT_PARITY_CASES``.

Structural reason it cannot reproduce (verified, see the sprint plan §"Structural
root cause" and ``_real_tables`` in src/sqlcg/parsers/base.py): the SELECTS_FROM
source set that ``analyze impact`` joins on is extracted from the SAME sqlglot
scope tree (top-level FROM + every CTE body + every subscope via
``traverse_scope``) that COLUMN_LINEAGE is derived from. STAR_SOURCE reads are by
construction a subset of those FROM tables. So for any *physical* table, the
SELECTS_FROM edge set is a superset of, or equal to, the COLUMN_LINEAGE/STAR
consumer set. The only tables that have a COLUMN_LINEAGE/STAR read with no
SELECTS_FROM edge are namespaced CTE intermediates (kind='cte'), which are never
valid ``analyze impact`` targets.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# The exact query `analyze impact <table>` runs (analyze.py:316-323): a single
# hop SqlTable -> SELECTS_FROM -> SqlQuery.
_IMPACT_SQL = (
    "SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target"
    ' FROM "SqlTable" t'
    ' JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified'
    ' JOIN "SqlQuery" q ON q.id = sf.src_key'
    " WHERE t.qualified = ? LIMIT 100"
)

# DDL shared by all parity cases.
_DDL = (
    "CREATE TABLE ba.src (col1 INT, col2 INT);\n"
    "CREATE TABLE ba.tgt (col1 INT, col2 INT);\n"
    "CREATE TABLE ba.main (col1 INT, id INT);\n"
    "CREATE TABLE ba.lookup (x INT);\n"
    "CREATE TABLE ba.filt (id INT);\n"
)

# (case-id, consumer-SQL, physical-target-table) — every shape PR-B named, plus
# subquery / CTE / view / star variants probed during the capture effort.
IMPACT_PARITY_CASES = [
    ("plain_insert_select", "INSERT INTO ba.tgt SELECT col1, col2 FROM ba.src;\n", "ba.src"),
    ("insert_select_star", "INSERT INTO ba.tgt SELECT * FROM ba.src;\n", "ba.src"),
    (
        "cte_wrapped_insert",
        "INSERT INTO ba.tgt WITH c AS (SELECT col1, col2 FROM ba.src) SELECT col1, col2 FROM c;\n",
        "ba.src",
    ),
    (
        "cte_wrapped_create_view",
        "CREATE VIEW ba.v AS WITH c AS (SELECT col1 FROM ba.src) SELECT col1 FROM c;\n",
        "ba.src",
    ),
    (
        "cte_wrapped_star",
        "INSERT INTO ba.tgt WITH c AS (SELECT * FROM ba.src) SELECT * FROM c;\n",
        "ba.src",
    ),
    (
        "nested_cte_source_deep",
        "INSERT INTO ba.tgt WITH a AS (SELECT col1 FROM ba.src), "
        "b AS (SELECT col1 FROM a) SELECT col1 FROM b;\n",
        "ba.src",
    ),
    (
        "scalar_subquery_source",
        "INSERT INTO ba.tgt SELECT col1, (SELECT max(x) FROM ba.lookup) FROM ba.main;\n",
        "ba.lookup",
    ),
    (
        "where_in_subquery_source",
        "INSERT INTO ba.tgt SELECT col1 FROM ba.main WHERE id IN (SELECT id FROM ba.filt);\n",
        "ba.filt",
    ),
]


def _index(tmp_path, files: dict[str, str]) -> DuckDBBackend:
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    db = DuckDBBackend(":memory:")
    db.init_schema()
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db


def _impact_consumers(db: DuckDBBackend, table: str) -> set[str]:
    return {r["id"] for r in db.run_read(_IMPACT_SQL, {"t": table})}


def _union_consumers(db: DuckDBBackend, table: str) -> set[str]:
    """The full consumer set across SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE.

    This is the parity yardstick PR-B proposes ``impact`` should reach.
    """
    sf = db.run_read(
        'SELECT DISTINCT src_key AS q FROM "SELECTS_FROM" WHERE dst_key = ?', {"t": table}
    )
    ss = db.run_read(
        'SELECT DISTINCT src_key AS q FROM "STAR_SOURCE" WHERE dst_key = ?', {"t": table}
    )
    cl = db.run_read(
        'SELECT DISTINCT cl.query_id AS q FROM "COLUMN_LINEAGE" cl '
        'JOIN "SqlColumn" s ON s.id = cl.src_key WHERE s.table_qualified = ?',
        {"t": table},
    )
    return {r["q"] for r in sf} | {r["q"] for r in ss} | {r["q"] for r in cl}


@pytest.mark.parametrize(
    ("case_id", "consumer_sql", "target"),
    IMPACT_PARITY_CASES,
    ids=[c[0] for c in IMPACT_PARITY_CASES],
)
def test_impact_matches_full_consumer_set_for_physical_target(
    tmp_path, case_id, consumer_sql, target
):
    """`analyze impact <physical table>` lists the same consumer as the full
    SELECTS_FROM union.

    Guards PR-B's B2 parity criterion. NOT-REPRODUCED gate evidence: for every
    shape the plan named, ``impact`` already covers the full
    SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE union (it is never a strict
    subset), so the under-report does not occur on physical tables.
    """
    db = _index(tmp_path, {"ddl.sql": _DDL, "etl.sql": consumer_sql})
    try:
        impact = _impact_consumers(db, target)
        union = _union_consumers(db, target)
        assert impact, f"[{case_id}] impact found no consumer for {target}"
        # The bug would manifest as `union - impact` being non-empty (impact a
        # strict subset). It is empty for every reproducible shape.
        assert not (union - impact), (
            f"[{case_id}] impact UNDER-reports for {target}: "
            f"union has {sorted(union)}, impact has {sorted(impact)}, "
            f"missed {sorted(union - impact)}"
        )
    finally:
        db.close()


def test_only_cte_intermediates_have_lineage_read_without_selects_from(tmp_path):
    """The sole tables with a COLUMN_LINEAGE/STAR read but no SELECTS_FROM edge
    are namespaced CTE intermediates (kind='cte') — never valid impact targets.

    This pins WHY Bug #6 cannot reproduce on physical tables: the recursive-CTE
    known gap in ``_real_tables`` only leaks the CTE alias node, not the real
    physical source. Uses the documented recursive-CTE gap shape from the plan.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE ba.emp (id INT, mgr INT, name STRING);\n"
            "CREATE TABLE ba.tgt (id INT, name STRING);\n",
            "etl.sql": "INSERT INTO ba.tgt WITH RECURSIVE r(id, mgr, name) AS ("
            "SELECT id, mgr, name FROM ba.emp WHERE mgr IS NULL "
            "UNION ALL SELECT e.id, e.mgr, e.name FROM ba.emp e JOIN r ON e.mgr = r.id) "
            "SELECT id, name FROM r;\n",
        },
    )
    try:
        cl = {
            r["tq"]
            for r in db.run_read(
                'SELECT DISTINCT s.table_qualified AS tq FROM "COLUMN_LINEAGE" cl '
                'JOIN "SqlColumn" s ON s.id = cl.src_key '
                "WHERE s.table_qualified IS NOT NULL AND s.table_qualified <> ''",
                {},
            )
        }
        star = {
            r["tq"] for r in db.run_read('SELECT DISTINCT dst_key AS tq FROM "STAR_SOURCE"', {})
        }
        sf = {r["tq"] for r in db.run_read('SELECT DISTINCT dst_key AS tq FROM "SELECTS_FROM"', {})}
        gap = (cl | star) - sf
        # The real physical source ba.emp IS covered by SELECTS_FROM.
        assert "ba.emp" in sf, "physical recursive-CTE source must have a SELECTS_FROM edge"
        assert "ba.emp" not in gap
        # Every leaked-read-only table is a namespaced CTE alias (contains '::').
        assert all("::" in g for g in gap), (
            f"a non-CTE table leaked a lineage read without SELECTS_FROM: {sorted(gap)} "
            "— this would be a genuine Bug #6 reproduction; capture it as a fixture."
        )
    finally:
        db.close()


@pytest.mark.xfail(
    reason=(
        "Bug #6 capture gate: no standalone fixture reproduces a physical table "
        "whose consumer is reachable via COLUMN_LINEAGE/STAR_SOURCE but NOT "
        "SELECTS_FROM. If the live DWH shape is ever isolated, replace this body "
        "with that fixture and remove the xfail — the assertion then fails on "
        "current `impact` (proving the under-report) and passes once the PR-B "
        "UNION fix lands. See plan/sprints/bugfix_impact_analysis_validation.md PR-B."
    ),
    strict=True,
)
def test_impact_under_reports_lineage_only_consumer_placeholder(tmp_path):
    """Placeholder for the live-derived reproducing fixture (PR-B B1).

    Intentionally xfail-strict: it asserts a condition (physical table with a
    lineage/star-only consumer that impact misses) that NO known fixture
    produces. The harness above is ready — fill in the captured SQL here.
    """
    db = _index(
        tmp_path,
        {"ddl.sql": _DDL, "etl.sql": "INSERT INTO ba.tgt SELECT col1 FROM ba.src;\n"},
    )
    try:
        impact = _impact_consumers(db, "ba.src")
        union = _union_consumers(db, "ba.src")
        # XFAIL: this difference is empty for every known shape.
        assert union - impact, "no known fixture makes impact a strict subset"
    finally:
        db.close()
