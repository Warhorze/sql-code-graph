"""#40 surface-recall guards (v1.4.1 PR-1 acceptance file).

Three guards that close the gaps identified in #40:

Guard 1 — Surface-recall (CLI + MCP filtered surface, kind-filter ON):
  Runs the exact filtered query ``analyze upstream`` uses
  (``_upstream_sql(depth, include_intermediate=False)``) and the MCP
  ``TRACE_COLUMN_LINEAGE`` multi-hop walk post-filtered by
  ``table_kind IN ('table', 'external', None)``.  Fixtures include:
    (a) a ≥2-CTE-hop chain (staging.src_a → a → u → j → mart.fact_kpi.measure)
    (b) a UNION-ALL branch CTE (both src_a and src_b feed u)
  Asserts both physical sources are present on BOTH surfaces; a reintroduced
  #38 (inner-join kind filter) reds at least this guard.

  MCP parity note: ``TRACE_COLUMN_LINEAGE`` / ``GET_UPSTREAM_DEPENDENCIES``
  are raw (no kind IN filter).  Guard 1 post-filters the raw MCP rows
  test-side by ``table_kind IN ('table','external', None)`` (the
  ``table_kind`` column is already returned by ``TRACE_COLUMN_LINEAGE``) and
  asserts the resulting physical-source set equals the CLI filtered set.  No
  production code change required.

Guard 4 — cte-leak / kind-suppression guard (folded in from #45
  verify-and-close):
  Index a fixture that produces ``SqlTable`` rows with ``kind='cte'`` or
  ``kind='derived'``; confirm via a raw-query pre-assertion BEFORE the
  filtered-output assertion.  Then assert those structural-kind rows are
  absent from default ``analyze upstream``/``downstream`` filtered output
  while physical sources remain.  Removing the ``kind IN ('table','external')``
  filter from ``_upstream_sql`` / ``_downstream_sql`` must red this guard.

Observable-output contract: all assertions check concrete id sets, never raw
``COLUMN_LINEAGE`` edges.
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _downstream_sql, _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixture SQL corpus
#
# Shape A: ≥2-CTE-hop chain (staging.src_a → a → u → j → mart.fact_kpi)
# Shape B: UNION ALL branch (both staging.src_a and staging.src_b feed u)
# Shape C: INSERT with DDL CREATE TABLE (canonical-name join)
# Shape D: staging.src_a and staging.src_b only inside CTE bodies
#
# CTE intermediates a, b, u, j will have kind='cte' in SqlTable (verified by
# the Guard 4 pre-assertion below).  Physical sources staging.src_a,
# staging.src_b will have kind='table'.
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE mart.fact_kpi (measure NUMBER, d VARCHAR);
"""

# 4-hop chain: mart.fact_kpi.measure ← j.m2 ← u.m ← a.x ← staging.src_a.x
# UNION ALL: u also selects from b ← staging.src_b.y
_ETL_SQL = """\
INSERT INTO mart.fact_kpi (measure, d)
WITH a AS (SELECT s1.x AS x, s1.k AS k FROM staging.src_a s1),
     b AS (SELECT s2.y AS y, s2.k AS k FROM staging.src_b s2),
     u AS (SELECT x AS m, k FROM a UNION ALL SELECT y AS m, k FROM b),
     j AS (SELECT u.m + 1 AS m2, u.k AS d FROM u)
SELECT m2, d FROM j;
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
    """Index the A–D fixture corpus; return (db, tmp_path)."""
    (tmp_path / "ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "etl.sql").write_text(_ETL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db, tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _mcp_upstream_rows_bfs(db: DuckDBBackend, col_id: str, depth: int = 10) -> list[dict]:
    """BFS walk via TRACE_COLUMN_LINEAGE; return all raw rows (with table_kind)."""
    visited: set[str] = set()
    frontier = {col_id}
    all_rows: list[dict] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for current in frontier:
            if current in visited:
                continue
            visited.add(current)
            rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current})
            for row in rows:
                all_rows.append(row)
                next_frontier.add(row["id"])
        if not next_frontier - visited:
            break
        frontier = next_frontier
    return all_rows


def _mcp_post_filtered_ids(db: DuckDBBackend, col_id: str, depth: int = 10) -> set[str]:
    """Return physical-source ids from MCP BFS, post-filtered by table_kind.

    Mirrors the CLI kind-filter (kind IN ('table','external') or NULL) test-side,
    with zero production code change.  The ``table_kind`` column is already returned
    by ``TRACE_COLUMN_LINEAGE``.
    """
    all_rows = _mcp_upstream_rows_bfs(db, col_id, depth)
    return {r["id"] for r in all_rows if r.get("table_kind") in ("table", "external", None)}


# ---------------------------------------------------------------------------
# Guard 1 — Surface-recall (CLI + MCP filtered surface)
# ---------------------------------------------------------------------------


def test_g1_cli_filtered_upstream_recalls_physical_sources(indexed_db):
    """CLI filtered upstream returns both physical sources through ≥2-CTE-hop chain.

    Shape A + B: 4-hop chain (j←u←a←staging.src_a), UNION ALL branch (b←staging.src_b).
    Reverting the kind-filter from OPTIONAL JOIN to inner JOIN reds this test.
    """
    db, _ = indexed_db
    ids = _cli_upstream_ids(db, "mart.fact_kpi.measure")

    assert ids, (
        "CLI filtered upstream returned no results for mart.fact_kpi.measure — "
        "kind filter may have been reverted to inner-join (regression #38)."
    )
    assert "staging.src_a.x" in ids, (
        f"staging.src_a.x not in CLI filtered upstream.\nReturned: {sorted(ids)}"
    )
    assert "staging.src_b.y" in ids, (
        f"staging.src_b.y not in CLI filtered upstream.\nReturned: {sorted(ids)}"
    )


def test_g1_mcp_post_filtered_upstream_recalls_physical_sources(indexed_db):
    """MCP TRACE_COLUMN_LINEAGE BFS post-filtered by table_kind returns both physical sources.

    Post-filters raw MCP rows by table_kind IN ('table','external', None) test-side.
    This delivers filtered-surface parity with zero production code change.
    """
    db, _ = indexed_db
    ids = _mcp_post_filtered_ids(db, "mart.fact_kpi.measure")

    assert ids, "MCP post-filtered upstream returned no results for mart.fact_kpi.measure."
    assert "staging.src_a.x" in ids, (
        f"staging.src_a.x not in MCP post-filtered upstream.\nReturned: {sorted(ids)}"
    )
    assert "staging.src_b.y" in ids, (
        f"staging.src_b.y not in MCP post-filtered upstream.\nReturned: {sorted(ids)}"
    )


def test_g1_cli_mcp_parity_on_physical_sources(indexed_db):
    """CLI filtered physical-source set equals MCP post-filtered physical-source set.

    A divergence means either the CLI kind-filter or the MCP BFS walk is broken.
    The comparison is restricted to the physical sources (staging.*) to avoid
    noise from CTE intermediate differences in traversal order.
    """
    db, _ = indexed_db
    cli_ids = _cli_upstream_ids(db, "mart.fact_kpi.measure")
    mcp_ids = _mcp_post_filtered_ids(db, "mart.fact_kpi.measure")

    # Restrict to physical sources for the parity comparison
    cli_physical = {rid for rid in cli_ids if "staging" in rid}
    mcp_physical = {rid for rid in mcp_ids if "staging" in rid}

    assert cli_physical, (
        f"CLI surface returned no staging.* sources.\nAll CLI ids: {sorted(cli_ids)}"
    )
    assert mcp_physical, (
        f"MCP post-filtered surface returned no staging.* sources.\nAll MCP ids: {sorted(mcp_ids)}"
    )
    assert cli_physical == mcp_physical, (
        f"CLI and MCP physical-source sets disagree.\n"
        f"CLI:  {sorted(cli_physical)}\n"
        f"MCP:  {sorted(mcp_physical)}"
    )


def test_g1_union_all_both_branches_reachable(indexed_db):
    """Both UNION ALL branches must appear in the filtered upstream for the measure column.

    Shape B: CTE u = a UNION ALL b.  A regression dropping one UNION branch would
    pass the single-source test above but fail here.
    """
    db, _ = indexed_db
    cli_ids = _cli_upstream_ids(db, "mart.fact_kpi.measure")

    src_a_found = any("src_a" in rid for rid in cli_ids)
    src_b_found = any("src_b" in rid for rid in cli_ids)

    assert src_a_found, (
        f"UNION ALL branch staging.src_a missing from filtered upstream.\n"
        f"Returned: {sorted(cli_ids)}"
    )
    assert src_b_found, (
        f"UNION ALL branch staging.src_b missing from filtered upstream.\n"
        f"Returned: {sorted(cli_ids)}"
    )


# ---------------------------------------------------------------------------
# Guard 4 — cte-leak / kind-suppression (folded in from #45 verify-and-close)
# ---------------------------------------------------------------------------


def test_g4_cte_kind_nodes_exist_in_graph(indexed_db):
    """Pre-assertion: the fixture produces SqlTable rows with kind='cte'.

    This confirms the fixture exercises the structural-kind path so that
    the subsequent filtered-output assertion is meaningful.  Without this
    pre-assertion the guard would only cover the name-prefix path (TC4 in
    test_user_surface_recall_guard.py) and the kind filter would never fire.
    """
    db, _ = indexed_db
    rows = db.run_read(
        'SELECT qualified, kind FROM "SqlTable"'
        " WHERE kind IN ('cte', 'derived') ORDER BY qualified",
        {},
    )
    assert rows, (
        "No SqlTable rows with kind='cte' or kind='derived' found after indexing.\n"
        "The fixture must produce structural-kind nodes for Guard 4 to test the kind filter.\n"
        "Expected CTE intermediates (a, b, u, j) to have kind='cte'."
    )
    structural_qualifieds = {r["qualified"] for r in rows}
    # Specifically confirm at least one of the CTE aliases is present
    cte_aliases = {"a", "b", "u", "j"}
    assert structural_qualifieds & cte_aliases, (
        f"CTE aliases {cte_aliases} not found among structural-kind nodes.\n"
        f"Found: {structural_qualifieds}"
    )


def test_g4_cte_kind_nodes_absent_from_filtered_upstream(indexed_db):
    """CTE/derived structural-kind nodes must NOT appear in filtered upstream output.

    Reverting the kind IN ('table','external') filter in _upstream_sql causes CTE
    intermediates (a, b, u, j) to appear in the output — this guard goes red.
    Physical sources (staging.src_a, staging.src_b) must still appear.
    """
    db, _ = indexed_db

    # Get all structural-kind table qualifieds
    structural_rows = db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE kind IN ('cte', 'derived')",
        {},
    )
    structural_qualifieds = {r["qualified"] for r in structural_rows}
    assert structural_qualifieds, (
        "Pre-condition: structural-kind nodes must exist "
        "(see test_g4_cte_kind_nodes_exist_in_graph)."
    )

    # Run filtered upstream query
    cli_ids = _cli_upstream_ids(db, "mart.fact_kpi.measure")
    assert cli_ids, "Filtered upstream returned no results — kind filter may be broken."

    # Extract table_qualified from returned ids (format: table_qualified.col_name)
    returned_table_qualifieds = set()
    for col_id in cli_ids:
        parts = col_id.rsplit(".", 1)
        if len(parts) == 2:
            returned_table_qualifieds.add(parts[0])

    leaked = returned_table_qualifieds & structural_qualifieds
    assert not leaked, (
        f"Structural-kind (cte/derived) table(s) leaked into filtered upstream output: "
        f"{sorted(leaked)}\n"
        f"Returned ids: {sorted(cli_ids)}\n"
        "Remove the kind IN ('table','external') filter from _upstream_sql to reproduce."
    )

    # Anti-regression: physical sources must still be present
    assert any("staging" in rid for rid in cli_ids), (
        f"Physical sources were filtered out — kind filter may be too aggressive.\n"
        f"Returned: {sorted(cli_ids)}"
    )


def test_g4_cte_kind_nodes_absent_from_filtered_downstream(indexed_db):
    """CTE/derived structural-kind nodes must NOT appear in filtered downstream output.

    Mirrors test_g4_cte_kind_nodes_absent_from_filtered_upstream for the downstream
    direction.  Run filtered downstream from a physical source column.
    """
    db, _ = indexed_db

    # Get all structural-kind table qualifieds
    structural_rows = db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE kind IN ('cte', 'derived')",
        {},
    )
    structural_qualifieds = {r["qualified"] for r in structural_rows}
    assert structural_qualifieds, "Pre-condition: structural-kind nodes must exist."

    # Downstream from staging.src_a.x reaches through CTEs to mart.fact_kpi.measure
    cli_ids = _cli_downstream_ids(db, "staging.src_a.x")
    assert cli_ids, (
        "Filtered downstream from staging.src_a.x returned no results — "
        "kind filter or lineage may be broken."
    )

    # Extract table_qualified portions from returned ids
    returned_table_qualifieds = set()
    for col_id in cli_ids:
        parts = col_id.rsplit(".", 1)
        if len(parts) == 2:
            returned_table_qualifieds.add(parts[0])

    leaked = returned_table_qualifieds & structural_qualifieds
    assert not leaked, (
        f"Structural-kind (cte/derived) table(s) leaked into filtered downstream output: "
        f"{sorted(leaked)}\n"
        f"Returned ids: {sorted(cli_ids)}\n"
        "Remove the kind IN ('table','external') filter from _downstream_sql to reproduce."
    )

    # Anti-regression: the terminal sink must appear
    assert any("fact_kpi" in rid for rid in cli_ids), (
        f"Terminal sink mart.fact_kpi.measure not in downstream output.\n"
        f"Returned: {sorted(cli_ids)}"
    )
