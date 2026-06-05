"""Guard 1 (#40): Surface-recall anchor for CTE lineage recall fix (#38 + #39).

Tests the exact filtered CLI query path (not raw COLUMN_LINEAGE edges — that is the
blind pattern #40 rejects) and the MCP get_upstream_dependencies / trace_column_lineage
paths on fixtures that would have been falsely green with a single-hop CTE.

Fixtures:
  - multi_hop_cte: a >=2-CTE-hop chain so single-hop tests cannot silently pass.
  - union_all_branch: a UNION ALL CTE to catch branch-miss bugs.

Guard asymmetry (from plan §tests for Cluster 1):
  - The HALF-B sentinel is ``test_half_b_keeps_node_less_physical_source`` below: it
    builds the true #38 "no-reindex" scenario (a source column whose SqlTable node is
    ABSENT) and runs the REAL production filter imported from analyze._kind_filter, so
    reverting Half B (inner-join filter) drops the node-less source and reds the test.
    The node-present fixtures alone cannot sentinel Half B: with the SqlTable node there
    (via Half A) both the inner-join and the OPTIONAL-MATCH filter return the source.
  - Guard 2 (graph-completeness invariant, test_cte_source_node_invariant.py) is the
    HALF-A sentinel: reverting Half A (dropping src-table emission) makes it red.
  - The filtered-query helper imports analyze._kind_filter rather than mirroring the
    string, so the guards exercise production code (the #40 lesson: a hardcoded mirror
    cannot catch a regression in the real query).

Observable-output contract: every assertion checks returned id sets or
table_kind field values, not "no exception raised".
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import GET_UPSTREAM_DEPENDENCIES_QUERY, TRACE_COLUMN_LINEAGE_QUERY
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

# >=2-CTE-hop chain: staging.src_a -> cte_a -> cte_b -> mart.dst_col
# NOTE: staging.src_a is NOT declared via CREATE TABLE — it is ONLY referenced inside
# a CTE body.  This is the exact #39 scenario: before Half A, no SqlTable node would
# exist for staging.src_a (stmt.sources is empty for CTE queries; the parser collapses
# the chain to direct src->dst edges in column_lineage).
_MULTI_HOP_SQL = """\
INSERT INTO mart.dst
WITH cte_a AS (SELECT col_a FROM staging.src_a),
     cte_b AS (SELECT col_a FROM cte_a)
SELECT col_a FROM cte_b;
"""

# UNION ALL branch CTE: staging.src_a and staging.src_b feed a unioned CTE -> mart.dst_col
# Neither source is declared via CREATE TABLE — both are only in CTE bodies.
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
# Helpers: build the exact CLI filtered-query string (mirrors analyze.py)
# ---------------------------------------------------------------------------


def _upstream_filtered_query(depth: int = 5) -> str:
    """Build the exact upstream SQL query analyze.py constructs with kind_filter ON.

    Uses production _upstream_sql() so reverting the kind filter in analyze.py reds
    these tests (replaces the old _kind_filter() Cypher helper, Phase 3 port).
    """
    return _upstream_sql(depth, include_intermediate=False)


# ---------------------------------------------------------------------------
# Guard 1a — >=2-CTE-hop chain, CLI filtered query
# ---------------------------------------------------------------------------


def test_multi_hop_cte_cli_filtered_returns_physical_source(db, tmp_path):
    """CLI filtered upstream query returns the real physical source through 2 CTE hops.

    This is the recall anchor (asserts the production filtered query, not raw edges).
    It is NOT the Half-B sentinel: with Half A present the SqlTable node exists, so
    both the inner-join and OPTIONAL-MATCH filters return the source.  The dedicated
    Half-B sentinel is ``test_half_b_keeps_node_less_physical_source`` below.
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.dst.col_a"})

    returned_ids = {r["id"] for r in rows}

    # Physical source must appear in filtered results
    assert "staging.src_a.col_a" in returned_ids, (
        f"Physical source 'staging.src_a.col_a' not returned by CLI-filtered upstream query.\n"
        f"Returned ids: {sorted(returned_ids)}\n"
        "If empty, Half B (OPTIONAL MATCH filter inversion) may have been reverted."
    )

    # CTE intermediates must NOT appear in filtered results
    assert not any("cte_a" in rid or "cte_b" in rid for rid in returned_ids), (
        f"CTE intermediates appeared in filtered upstream results: "
        f"{[rid for rid in returned_ids if 'cte_a' in rid or 'cte_b' in rid]}"
    )


def test_half_b_keeps_node_less_physical_source(db, tmp_path):
    """HALF-B SENTINEL: the production filter keeps a source whose SqlTable node is absent.

    This reproduces the #38 "no-reindex" scenario (Half B independence): a physical
    source column whose SqlTable node does NOT exist — exactly the state of a graph
    indexed before the #39 fix.  We index the fixture, then DELETE the staging.src_a
    SqlTable node to recreate that broken state while leaving the COLUMN_LINEAGE edges
    and the SqlColumn intact.

    Half B (OPTIONAL MATCH + ``t.kind IS NULL OR ...``) KEEPS the node-less source.
    Reverting Half B to an inner ``MATCH (t:SqlTable {...}) ... WHERE t.kind IN [...]``
    DROPS it — and because this test runs the real ``analyze._kind_filter`` via
    ``_upstream_filtered_query``, that revert in production reds this test.
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Precondition: Half A created the SqlTable node (non-vacuous — proves we exercise
    # the node-removal path rather than asserting on an already-absent node).
    before = db.run_read(
        'SELECT count(*) AS n FROM "SqlTable" WHERE qualified = ?', {"q": "staging.src_a"}
    )
    assert before[0]["n"] == 1, "Precondition failed: Half A did not emit staging.src_a node"

    # Recreate the pre-#39 broken state: source column exists + edges exist, but the
    # SqlTable node is gone.  DuckDB has no DETACH DELETE — delete related edges first.
    db.run_write('DELETE FROM "DEFINED_IN" WHERE src_key = ?', {"k": "staging.src_a"})
    db.run_write('DELETE FROM "HAS_COLUMN" WHERE src_key = ?', {"k": "staging.src_a"})
    db.run_write('DELETE FROM "SELECTS_FROM" WHERE dst_key = ?', {"k": "staging.src_a"})
    db.run_write('DELETE FROM "SqlTable" WHERE qualified = ?', {"k": "staging.src_a"})
    after = db.run_read(
        'SELECT count(*) AS n FROM "SqlTable" WHERE qualified = ?', {"q": "staging.src_a"}
    )
    assert after[0]["n"] == 0, "Node-less precondition not established"
    # The source COLUMN must still exist (only the SqlTable node was removed).
    col = db.run_read(
        'SELECT count(*) AS n FROM "SqlColumn" WHERE id = ?', {"id": "staging.src_a.col_a"}
    )
    assert col[0]["n"] == 1, "Source SqlColumn unexpectedly removed — fixture invalid"

    rows = db.run_read(_upstream_filtered_query(), {"ref": "mart.dst.col_a"})
    returned_ids = {r["id"] for r in rows}

    assert "staging.src_a.col_a" in returned_ids, (
        "Node-less physical source 'staging.src_a.col_a' was dropped by the filtered "
        "upstream query.\n"
        f"Returned ids: {sorted(returned_ids)}\n"
        "Half B (OPTIONAL MATCH + 'kind IS NULL') was reverted to the inner-join filter "
        "— a source whose SqlTable node is absent is silently dropped (regression #38)."
    )


def test_multi_hop_cte_mcp_upstream_returns_physical_source(db, tmp_path):
    """MCP get_upstream_dependencies returns the real physical source through 2 CTE hops.

    The MCP path is unfiltered; this asserts Half A made the source SqlTable exist
    (otherwise trace_column_lineage returns table_kind=None instead of 'table').
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Walk the MCP unfiltered upstream dependency graph
    visited: set[str] = set()
    found: set[str] = set()
    queue = ["mart.dst.col_a"]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        rows = db.run_read(GET_UPSTREAM_DEPENDENCIES_QUERY, {"id": current})
        for row in rows:
            node_id = row["id"]
            found.add(node_id)
            queue.append(node_id)

    assert "staging.src_a.col_a" in found, (
        f"MCP get_upstream_dependencies did not reach 'staging.src_a.col_a'.\n"
        f"Found ids: {sorted(found)}"
    )


def test_multi_hop_cte_trace_lineage_table_kind_is_table(db, tmp_path):
    """trace_column_lineage returns table_kind='table' for the physical source.

    Half A emits a SqlTable node for the CTE-body source, so TRACE_COLUMN_LINEAGE
    can join it and return table_kind='table'.  Without Half A the node is absent
    and table_kind returns None.
    """
    (tmp_path / "fixture.sql").write_text(_MULTI_HOP_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Walk using TRACE_COLUMN_LINEAGE_QUERY (1-hop at a time, like tools.py does)
    visited: set[str] = set()
    table_kinds_by_id: dict[str, str | None] = {}
    queue = ["mart.dst.col_a"]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current})
        for row in rows:
            node_id = row["id"]
            table_kinds_by_id[node_id] = row.get("table_kind")
            queue.append(node_id)

    assert "staging.src_a.col_a" in table_kinds_by_id, (
        f"staging.src_a.col_a not reached by trace_column_lineage traversal.\n"
        f"Visited: {sorted(table_kinds_by_id.keys())}"
    )
    assert table_kinds_by_id["staging.src_a.col_a"] == "table", (
        f"Expected table_kind='table' for staging.src_a.col_a, "
        f"got '{table_kinds_by_id['staging.src_a.col_a']}'.\n"
        "If None, Half A (src-table emission) may have been reverted."
    )


# ---------------------------------------------------------------------------
# Guard 1b — UNION ALL branch CTE, CLI filtered query
# ---------------------------------------------------------------------------


def test_union_all_cte_cli_filtered_returns_both_physical_sources(db, tmp_path):
    """CLI filtered upstream query returns BOTH branches of a UNION ALL CTE.

    Guards against branch-miss bugs where only the first branch of a UNION ALL
    is traced.  Both staging.src_a and staging.src_b must appear.
    """
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.union_dst.val"})

    returned_ids = {r["id"] for r in rows}

    # Both physical sources must appear
    assert "staging.src_a.val" in returned_ids, (
        f"UNION ALL branch 'staging.src_a.val' missing from filtered results.\n"
        f"Returned: {sorted(returned_ids)}"
    )
    assert "staging.src_b.val" in returned_ids, (
        f"UNION ALL branch 'staging.src_b.val' missing from filtered results.\n"
        f"Returned: {sorted(returned_ids)}"
    )

    # CTE intermediate must not appear
    assert not any("unioned" in rid for rid in returned_ids), (
        f"CTE 'unioned' leaked into filtered results: "
        f"{[rid for rid in returned_ids if 'unioned' in rid]}"
    )


def test_union_all_cte_mcp_upstream_returns_both_branches(db, tmp_path):
    """MCP get_upstream_dependencies returns both UNION ALL branches."""
    (tmp_path / "fixture.sql").write_text(_UNION_ALL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    visited: set[str] = set()
    found: set[str] = set()
    queue = ["mart.union_dst.val"]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        rows = db.run_read(GET_UPSTREAM_DEPENDENCIES_QUERY, {"id": current})
        for row in rows:
            node_id = row["id"]
            found.add(node_id)
            queue.append(node_id)

    assert "staging.src_a.val" in found, (
        f"MCP upstream did not find UNION ALL branch staging.src_a.val.\nFound: {sorted(found)}"
    )
    assert "staging.src_b.val" in found, (
        f"MCP upstream did not find UNION ALL branch staging.src_b.val.\nFound: {sorted(found)}"
    )
