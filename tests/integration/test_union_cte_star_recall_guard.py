"""Failing acceptance tests for v1.1.3 — SELECT * over N-branch UNION ALL of sibling CTEs.

These tests MUST FAIL before the developer implements the fix. They are the definition of
done for plan/v1_1_3_union_cte_star.md.

Root cause (confirmed empirically 2026-06-01):
  A CTE whose body is a 3-branch UNION ALL (A UNION ALL B UNION ALL C) produces
  zero incoming COLUMN_LINEAGE edges. `qualify()` mutates the AST in-place and
  expands `SELECT *` for a 2-branch union, but for N≥3 branches `cte_body.this`
  is a nested Union (not a Select), so `Union.expressions = []` and the inner
  column loop body never executes. `cte_union.*` become islands (the node is
  never even created as a lineage target).

The fix (HOP-PRESERVING — see plan):
  Walk to the deepest left-branch exp.Select (whose star the statement-level
  qualify already expanded into explicit columns) and run the EXISTING no-`sources=`
  sg_lineage call per column. With no `sources=`, sqlglot resolves to the BRANCH CTE
  columns (cte_a.total_amt, ...), so the emitted edge is
  `cte_union.col <- cte_X.col` — the CTE hop is PRESERVED, consistent with the
  explicit-column union path and the anchor tests. Physical sources are reached by
  MULTI-HOP traversal through the preserved cte_X nodes, NOT by collapsing the hop.
  No `sources=` is introduced anywhere.

Guard design (the #40 lesson):
  - Fixture is 3 sibling CTEs with identical explicit column lists, unioned via
    UNION ALL, star-selected by cte_union. This shape is a TRUE island today —
    `cte_union.total_amt` has 0 incoming edges (confirmed empirically).
  - All assertions check observable graph output (edge presence, specific ids),
    never "no exception raised".
  - The hop-preservation sentinel asserts the direct upstream is a BRANCH CTE
    column, not a collapsed physical source — it reds if a future `sources=`
    regression collapses the hop.
  - test_explicit_column_union_still_traces uses the 2-branch explicit-column
    fixture already proven green (test_cte_recall_guard.py) — anti-regression.

Observable-output contract: every assertion checks non-empty id sets or edge counts.
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared backend fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory DuckDB with schema initialised (ported from KuzuDB in Phase 3)."""
    backend = DuckDBBackend(":memory:")
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
    """CLI-filtered upstream SQL query — same pattern as test_cte_recall_guard.py.

    Uses production _upstream_sql() so filter regressions in analyze.py red these tests.
    """
    return _upstream_sql(depth, include_intermediate=False)


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

    GREEN after fix: Step 1.1 walks to the deepest left-branch Select (whose star
    qualify already expanded), and the EXISTING no-`sources=` sg_lineage call emits
    CTE_PROJECTION edges from the branch CTE columns — the hop is preserved.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    q = (
        'SELECT cl.src_key AS id FROM "COLUMN_LINEAGE" cl '
        "WHERE cl.dst_key = 'cte_union.total_amt'"
    )
    rows = db.run_read(q, {})
    incoming_ids = {r["id"] for r in rows}

    assert len(incoming_ids) > 0, (
        "cte_union.total_amt has 0 incoming COLUMN_LINEAGE edges — it is an island.\n"
        "This is the 3-branch UNION ALL star bug (plan/v1_1_3_union_cte_star.md).\n"
        "The fix must walk to the deepest left-branch Select (Step 1.1) and reuse the\n"
        "existing no-`sources=` sg_lineage call so the branch CTE columns are emitted."
    )


# ---------------------------------------------------------------------------
# Test 2 — HOP-PRESERVATION sentinel: direct upstream must be a BRANCH CTE column
#
# RED TODAY: no edges at all (0 rows from the query)
# GREEN AFTER FIX: edges emitted with transform=CTE_PROJECTION whose source is a
#   branch CTE column (cte_a.total_amt, ...), NOT a collapsed physical source.
#   This reds if a future sources= regression collapses the CTE hop.
# ---------------------------------------------------------------------------


def test_union_star_cte_incoming_edges_are_cte_projection(db, tmp_path):
    """After the fix, incoming edges to cte_union carry CTE_PROJECTION from BRANCH CTEs.

    Hop-preservation contract: the DIRECT (1-hop) upstream of cte_union.total_amt must be
    a branch CTE column (cte_a.total_amt / cte_b / cte_c), NOT a physical source. The fix
    uses the existing no-`sources=` sg_lineage call, which stops at the CTE boundary and
    preserves the hop — consistent with the explicit-column union path and the anchor tests.

    RED TODAY: 0 edges (island), assertion on non-empty set fails.
    GUARD: if a future change reintroduces `sources=` and collapses the hop to
    staging.src_*, the branch-CTE-column assertion below reds.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    q = (
        "SELECT cl.src_key AS id, cl.transform AS transform "
        'FROM "COLUMN_LINEAGE" cl '
        "WHERE cl.dst_key = 'cte_union.total_amt'"
    )
    rows = db.run_read(q, {})

    assert len(rows) > 0, (
        "cte_union.total_amt has 0 incoming edges (island).\n"
        "Expected CTE_PROJECTION edges from BRANCH CTE columns after the "
        "hop-preserving star-over-N-branch-union fix."
    )

    transforms = {r["transform"] for r in rows}
    assert "CTE_PROJECTION" in transforms, (
        f"Expected transform='CTE_PROJECTION' on incoming edges to cte_union.total_amt, "
        f"got: {transforms}"
    )

    upstream_ids = {r["id"] for r in rows}

    # Hop-preservation: the direct upstream MUST be branch CTE columns, not physical sources.
    branch_cte_ids = {
        uid
        for uid in upstream_ids
        if uid in {"cte_a.total_amt", "cte_b.total_amt", "cte_c.total_amt"}
    }
    assert len(branch_cte_ids) > 0, (
        "Direct upstream of cte_union.total_amt contains no branch CTE column "
        "(cte_a/cte_b/cte_c.total_amt).\n"
        f"Upstream ids: {sorted(upstream_ids)}\n"
        "The fix must PRESERVE the CTE hop (no `sources=`): the direct upstream is the "
        "branch CTE column, not a collapsed physical source."
    )

    # Negative: the hop must NOT be collapsed — no physical source as a DIRECT upstream.
    physical_src_ids = {uid for uid in upstream_ids if "staging." in uid}
    assert not physical_src_ids, (
        "A physical source appears as a DIRECT (1-hop) upstream of cte_union.total_amt: "
        f"{sorted(physical_src_ids)}.\n"
        "This means the CTE hop was COLLAPSED (a `sources=` regression). The fix must keep "
        "the branch CTE columns as the direct upstream; physical sources are reached only "
        "via multi-hop traversal (see test_union_star_cte_mcp_upstream_reaches_physical_sources)."
    )


# ---------------------------------------------------------------------------
# Test 3 — multi-hop upstream of cte_union.total_amt must reach physical sources
#          THROUGH the preserved cte_X hop
#
# RED TODAY: cte_union.total_amt is an island — a multi-hop upstream traversal
#   returns [] because the node has 0 incoming edges. The end-to-end chain
#   through cte_union is broken.
# GREEN AFTER FIX: the hop is preserved (cte_union <- cte_X <- staging.src_*), so a
#   multi-hop *1..N traversal reaches staging.src_a/b/c.amount through cte_X.
#
# NOTE: a 1-hop GET_UPSTREAM_DEPENDENCIES_QUERY would (correctly) return only the
# branch CTEs after the fix, since the hop is preserved. We therefore use a multi-hop
# CLI-shaped traversal (the production _kind_filter) to validate the end-to-end chain.
# ---------------------------------------------------------------------------


def test_union_star_cte_mcp_upstream_reaches_physical_sources(db, tmp_path):
    """Multi-hop upstream of cte_union.total_amt reaches physical sources via the preserved hop.

    RED TODAY: cte_union.total_amt is an island — the multi-hop traversal returns []
    because the node has 0 incoming COLUMN_LINEAGE edges. The chain through cte_union
    cannot be walked.

    GREEN AFTER FIX: the hop is preserved (cte_union <- cte_X.total_amt <- staging.src_*),
    so the *1..N traversal reaches staging.src_a/b/c.amount THROUGH the branch CTE nodes —
    no hop collapse.

    Uses the production analyze._kind_filter (as the v1.1.2 guards do), so a filter
    regression also reds this test.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_STAR_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(_upstream_query(depth=5), {"ref": "cte_union.total_amt"})
    upstream_ids = {r["id"] for r in rows}

    assert len(upstream_ids) > 0, (
        "Multi-hop upstream traversal returned 0 results for cte_union.total_amt.\n"
        "The node is a lineage island — no incoming edges.\n"
        "After fix: the deepest-Select walk reconnects cte_union via the preserved CTE hop."
    )

    # Physical sources must be reachable via multi-hop traversal THROUGH the cte_X hop.
    physical_sources = {uid for uid in upstream_ids if "staging." in uid}
    assert len(physical_sources) > 0, (
        f"Multi-hop upstream of cte_union.total_amt did not reach any physical source.\n"
        f"Found: {sorted(upstream_ids)}\n"
        "After fix, the chain cte_union <- cte_X.total_amt <- staging.src_*.amount must be "
        "walkable; physical sources are reached via the preserved hop, not by collapsing it."
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

    # Multi-hop upstream via recursive CTE (DuckDB SQL).
    q = _upstream_sql(5, include_intermediate=True)
    rows = db.run_read(q, {"ref": "mart.union_explicit_dst.val"})
    found = {r["id"] for r in rows}

    assert "staging.src_a.val" in found, (
        f"staging.src_a.val missing from explicit-union upstream. "
        f"Anti-regression: the fix must not break explicit-column unions.\nFound: {sorted(found)}"
    )
    assert "staging.src_b.val" in found, (
        f"staging.src_b.val missing from explicit-union upstream. "
        f"Anti-regression: the fix must not break explicit-column unions.\nFound: {sorted(found)}"
    )
