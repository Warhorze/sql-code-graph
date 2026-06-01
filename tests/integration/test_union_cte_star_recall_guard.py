"""Failing acceptance tests for v1.1.3 — SELECT * over N-branch UNION ALL of sibling CTEs.

These tests MUST FAIL before the developer implements the fix. They are the definition of
done for plan/v1_1_3_union_cte_star.md.

Root cause (confirmed by plan-reviewer 2026-06-01):
  A CTE whose body is a 3-branch UNION ALL (A UNION ALL B UNION ALL C) produces
  zero incoming COLUMN_LINEAGE edges. `qualify()` mutates the AST in-place and
  expands `SELECT *` for a 2-branch union, but for N≥3 branches `cte_body.this`
  is a nested Union (not a Select), so `Union.expressions = []` and the inner
  column loop body never executes. `cte_union.*` become islands.

Guard design (the #40 lesson):
  - Fixture is 3 sibling CTEs with identical explicit column lists, unioned via
    UNION ALL, star-selected by cte_union. This shape is a TRUE island today —
    `cte_union.total_amt` has 0 incoming edges (confirmed empirically).
  - All assertions check observable graph output (edge presence, specific ids),
    never "no exception raised".
  - test_explicit_column_union_still_traces uses the 2-branch explicit-column
    fixture already proven green (test_cte_recall_guard.py) — anti-regression.

Observable-output contract: every assertion checks non-empty id sets or edge counts.
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _kind_filter
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.queries import GET_UPSTREAM_DEPENDENCIES_QUERY
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared backend fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Fixtures SQL
# ---------------------------------------------------------------------------

# 3-branch UNION ALL: three sibling CTEs with identical explicit column lists,
# star-selected by cte_union.
#
# A 3-branch fixture is required (not 2-branch) because qualify() mutates the
# AST in-place and expands SELECT * for 2-branch unions, masking the bug.
# For N=3, cte_body.this is a nested Union (not a Select), so Union.expressions
# = [] and the CTE-projection loop body never executes — cte_union.* are islands.
_UNION_ALL_STAR_SQL = """\
INSERT INTO mart.union_star_dst
WITH cte_a AS (
    SELECT k AS grp_key, SUM(amount) AS total_amt
      FROM staging.src_a GROUP BY k
),
cte_b AS (
    SELECT k AS grp_key, SUM(amount) AS total_amt
      FROM staging.src_b GROUP BY k
),
cte_c AS (
    SELECT k AS grp_key, SUM(amount) AS total_amt
      FROM staging.src_c GROUP BY k
),
cte_union AS (
    SELECT * FROM cte_a
    UNION ALL SELECT * FROM cte_b
    UNION ALL SELECT * FROM cte_c
)
SELECT grp_key, total_amt FROM cte_union;
"""

# Explicit-column 2-branch union (already works; used as anti-regression)
# This is the same shape as test_cte_recall_guard._UNION_ALL_SQL but inline here
# so this test file is self-contained.
_EXPLICIT_UNION_SQL = """\
INSERT INTO mart.union_explicit_dst
WITH unioned AS (
    SELECT val FROM staging.src_a
    UNION ALL
    SELECT val FROM staging.src_b
)
SELECT val FROM unioned;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upstream_query(depth: int = 5) -> str:
    """CLI-filtered upstream query — same pattern as test_cte_recall_guard.py."""
    kind_filter = _kind_filter("src", include_intermediate=False)
    return (
        f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
        f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src:{NodeLabel.COLUMN}) "
        f"{kind_filter}"
        f"OPTIONAL MATCH (src)-[direct:{RelType.COLUMN_LINEAGE}]->(c) "
        "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
        "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100"
    )


# ---------------------------------------------------------------------------
# Test 1 — central island guard: cte_union.total_amt must have ≥1 incoming edge
#
# RED TODAY: 3-branch UNION ALL produces 0 incoming edges to cte_union.total_amt
# GREEN AFTER FIX: fix walks to deepest left-branch Select, builds union_cte_sources,
#   passes sources= to sg_lineage, which emits CTE_PROJECTION edges from physical sources
# ---------------------------------------------------------------------------


def test_union_star_cte_gets_incoming_edges(db, tmp_path):
    """3-branch star-union CTE must receive ≥1 incoming COLUMN_LINEAGE edge.

    RED on current code: cte_union.total_amt is a 0-edge island (N-branch union
    where cte_body.this is a nested Union; Union.expressions=[] so the CTE-projection
    loop body never executes).

    GREEN after fix: Step 1.1 walks to deepest left-branch Select; Step 1.2 passes
    union_cte_sources= to sg_lineage so the star expands to physical source columns.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    q = (
        f"MATCH (s:{NodeLabel.COLUMN})-[:{RelType.COLUMN_LINEAGE}]->"
        f"(c:{NodeLabel.COLUMN} {{id: 'cte_union.total_amt'}}) RETURN s.id AS id"
    )
    rows = db.run_read(q, {})
    incoming_ids = {r["id"] for r in rows}

    assert len(incoming_ids) > 0, (
        "cte_union.total_amt has 0 incoming COLUMN_LINEAGE edges — it is an island.\n"
        "This is the 3-branch UNION ALL star bug (plan/v1_1_3_union_cte_star.md).\n"
        "The fix must walk to the deepest left-branch Select (Step 1.1) and pass\n"
        "union_cte_sources= to sg_lineage (Step 1.2) so the star expands."
    )


# ---------------------------------------------------------------------------
# Test 2 — edge shape: incoming edges must carry CTE_PROJECTION + physical sources
#
# RED TODAY: no edges at all (0 rows from the query above)
# GREEN AFTER FIX: edges emitted with transform=CTE_PROJECTION from physical sources
# ---------------------------------------------------------------------------


def test_union_star_cte_incoming_edges_are_cte_projection(db, tmp_path):
    """After the fix, incoming edges to cte_union carry transform='CTE_PROJECTION'.

    Also verifies that at least one physical source (staging.src_*) is a direct
    upstream of cte_union — not just another CTE intermediate — because sg_lineage
    with sources= collapses the CTE hop to the physical source.

    RED TODAY: 0 edges, assertion on non-empty set fails.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    q = (
        f"MATCH (s:{NodeLabel.COLUMN})-[r:{RelType.COLUMN_LINEAGE}]->"
        f"(c:{NodeLabel.COLUMN} {{id: 'cte_union.total_amt'}}) "
        "RETURN s.id AS id, r.transform AS transform"
    )
    rows = db.run_read(q, {})

    assert len(rows) > 0, (
        "cte_union.total_amt has 0 incoming edges. Expected CTE_PROJECTION edges from "
        "physical sources after the star-over-N-branch-union fix."
    )

    transforms = {r["transform"] for r in rows}
    assert "CTE_PROJECTION" in transforms, (
        f"Expected transform='CTE_PROJECTION' on incoming edges to cte_union.total_amt, "
        f"got: {transforms}"
    )

    physical_src_ids = {r["id"] for r in rows if "staging." in r["id"]}
    assert len(physical_src_ids) > 0, (
        "No physical source (staging.*) in direct upstream of cte_union.total_amt.\n"
        f"Upstream ids: {sorted(r['id'] for r in rows)}\n"
        "After fix, sg_lineage with sources= should collapse CTE hops to physical sources."
    )


# ---------------------------------------------------------------------------
# Test 3 — MCP upstream of cte_union.total_amt must return physical sources
#
# RED TODAY: GET_UPSTREAM_DEPENDENCIES_QUERY starting at cte_union.total_amt
#   returns [] — the node has no incoming edges (island). MCP cannot answer
#   "where does cte_union.total_amt come from?"
# GREEN AFTER FIX: returns staging.src_a/b/c.amount from the CTE_PROJECTION edges.
# ---------------------------------------------------------------------------


def test_union_star_cte_mcp_upstream_returns_physical_sources(db, tmp_path):
    """MCP get_upstream_dependencies starting at cte_union.total_amt returns physical sources.

    RED TODAY: cte_union.total_amt is an island — GET_UPSTREAM_DEPENDENCIES_QUERY
    returns [] because the node has 0 incoming COLUMN_LINEAGE edges.

    GREEN AFTER FIX: incoming CTE_PROJECTION edges exist from physical sources
    (sg_lineage with sources= collapses CTE hops to staging.src_*).

    Note: this is a more precise test than walking from the final mart.* target,
    because the outer SELECT body_scope already emits direct mart.* <- staging.*
    edges that would make that traversal pass vacuously even without the fix.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(GET_UPSTREAM_DEPENDENCIES_QUERY, {"id": "cte_union.total_amt"})
    upstream_ids = {r["id"] for r in rows}

    assert len(upstream_ids) > 0, (
        "GET_UPSTREAM_DEPENDENCIES_QUERY returned 0 results for cte_union.total_amt.\n"
        "The node is a lineage island — no incoming edges.\n"
        "After fix: sources= resolves the 3-branch union star and emits CTE_PROJECTION edges."
    )

    # At least one physical source must be reachable in the 1-hop upstream
    physical_sources = {uid for uid in upstream_ids if "staging." in uid}
    assert len(physical_sources) > 0, (
        f"MCP upstream of cte_union.total_amt did not reach any physical source.\n"
        f"Found: {sorted(upstream_ids)}\n"
        "Expected staging.src_* after sources= resolution collapses the CTE hop."
    )


# ---------------------------------------------------------------------------
# Test 4 — anti-regression: explicit-column 2-branch UNION ALL still traces
#
# This uses an explicit-column union (not SELECT *) which already works today.
# It must stay green through and after the fix (guards against regressions in
# the existing CTE-projection loop code path).
#
# GREEN TODAY and GREEN AFTER FIX.
# ---------------------------------------------------------------------------


def test_explicit_column_union_still_traces(db, tmp_path):
    """Explicit-column 2-branch UNION ALL CTE still produces correct lineage after the fix.

    This test is GREEN TODAY. It stays green after the fix to confirm the star-over-union
    path change did not alter the existing explicit-column code path.

    The fix must add sources= ONLY on the star-over-union path; the existing explicit-column
    path calling sg_lineage with no sources= is byte-for-byte unchanged.
    """
    (tmp_path / "explicit.sql").write_text(_EXPLICIT_UNION_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    q = (
        f"MATCH (s:{NodeLabel.COLUMN})-[:{RelType.COLUMN_LINEAGE}*1..5]->"
        f"(c:{NodeLabel.COLUMN} {{id: 'mart.union_explicit_dst.val'}}) RETURN s.id AS id"
    )
    rows = db.run_read(q, {})
    found = {r["id"] for r in rows}

    assert "staging.src_a.val" in found, (
        f"staging.src_a.val missing from explicit-union upstream. "
        f"Anti-regression: the fix must not break explicit-column unions.\nFound: {sorted(found)}"
    )
    assert "staging.src_b.val" in found, (
        f"staging.src_b.val missing from explicit-union upstream. "
        f"Anti-regression: the fix must not break explicit-column unions.\nFound: {sorted(found)}"
    )
