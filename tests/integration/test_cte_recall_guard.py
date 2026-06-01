"""Guard 1 (#40): Surface-recall anchor for CTE lineage recall fix (#38 + #39).

Tests the exact filtered CLI query path (not raw COLUMN_LINEAGE edges — that is the
blind pattern #40 rejects) and the MCP get_upstream_dependencies / trace_column_lineage
paths on fixtures that would have been falsely green with a single-hop CTE.

Fixtures:
  - multi_hop_cte: a >=2-CTE-hop chain so single-hop tests cannot silently pass.
  - union_all_branch: a UNION ALL CTE to catch branch-miss bugs.

Guard asymmetry (from plan §tests for Cluster 1):
  - This guard (surface-recall) is the HALF-B sentinel: reverting Half B (restoring
    the inner-join filter) makes it red because physical sources are dropped by the
    MATCH...WHERE kind IN [...] when the SqlTable node is missing.
  - Guard 2 (graph-completeness invariant, test_cte_source_node_invariant.py) is the
    HALF-A sentinel: reverting Half A (dropping src-table emission) makes it red.

Observable-output contract: every assertion checks returned id sets or
table_kind field values, not "no exception raised".
"""

from __future__ import annotations

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.queries import GET_UPSTREAM_DEPENDENCIES_QUERY, TRACE_COLUMN_LINEAGE_QUERY
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
    """Build the exact upstream query analyze.py constructs with kind_filter ON."""
    kind_filter = (
        "OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified}) "
        "WITH c, src, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
    )
    return (
        f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
        f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src:{NodeLabel.COLUMN}) "
        f"{kind_filter}"
        f"OPTIONAL MATCH (src)-[direct:{RelType.COLUMN_LINEAGE}]->(c) "
        "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
        "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100"
    )


# ---------------------------------------------------------------------------
# Guard 1a — >=2-CTE-hop chain, CLI filtered query
# ---------------------------------------------------------------------------


def test_multi_hop_cte_cli_filtered_returns_physical_source(db, tmp_path):
    """CLI filtered upstream query returns the real physical source through 2 CTE hops.

    Reverting Half B (restoring inner-join filter) makes this red because the
    source SqlTable node is absent in an already-broken graph.
    Reverting Half A (dropping src-table emission) makes this red via Guard 2, not here —
    but the query would also fail to find kind='table' without the node.
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
