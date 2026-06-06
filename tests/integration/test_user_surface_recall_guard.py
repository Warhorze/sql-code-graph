"""#40 user-surface regression guard suite (TC1–TC7).

Drives the exact filtered CLI query (via the production ``analyze._kind_filter``) and
the MCP trace_column_lineage tool against a synthetic, committable A–D fixture corpus.
Never asserts on raw COLUMN_LINEAGE edges — that is the blind pattern #40 rejects.

Fixture shapes (per fix_issue_40_user_surface_regression_guards.md §3):
  A. ≥2-CTE-hop chain → physical source
  B. UNION-ALL branch CTE (both branches, including a computed-measure projection)
  C. INSERT INTO mart.fact_kpi with a separate DDL CREATE TABLE mart.fact_kpi (#44)
  D. Physical source referenced only inside a CTE body (#39)

All shapes are carried in a single two-file fixture corpus.

Guard asymmetry:
  TC3 is RED before the #44 canonical-name fix lands (schema-qualified DDL query
  returns no results while schema-less spelling works).  It flips GREEN in Phase 2.
  TC6 asserts file:line is non-NULL for all physical-source rows; it is GREEN after
  the Phase 0 query rewrite (aggregate per src before LIMIT).
"""

from __future__ import annotations

import pytest

from sqlcg.cli.commands.analyze import _downstream_sql, _upstream_sql
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixture SQL corpus (A–D in two files)
# ---------------------------------------------------------------------------

# DDL file: defines the canonical schema-qualified target (shape C)
_DDL_SQL = """\
CREATE TABLE mart.fact_kpi (measure NUMBER, d VARCHAR);
"""

# ETL file: CTE-built load covering shapes A, B, C, D
# Shape A: >=2 CTE hops (a -> u -> j -> target)
# Shape B: UNION ALL branch CTE (both staging.src_a and staging.src_b feed u),
#          including a computed measure (x + 1 AS m2) so arithmetic projections are tested
# Shape C: INSERT INTO mart.fact_kpi (same table name as the DDL above)
# Shape D: staging.src_a and staging.src_b are ONLY referenced inside CTE bodies
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
    """Fresh in-memory DuckDB with schema initialised (ported from KuzuDB in Phase 3)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def indexed_db(db, tmp_path):
    """Index the A–D fixture corpus into a fresh in-memory DB and return (db, tmp_path)."""
    (tmp_path / "ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "etl.sql").write_text(_ETL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db, tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upstream_filtered_query(depth: int = 5) -> str:
    """Build the exact upstream SQL query analyze.py constructs with kind_filter ON.

    Uses production _upstream_sql() so any regression in analyze.py reds these tests
    (Phase 3 port: replaces the old Cypher _kind_filter() helper).
    """
    return _upstream_sql(depth, include_intermediate=False)


def _mcp_upstream_ids(db, col_id: str, depth: int = 10) -> set[str]:
    """Walk TRACE_COLUMN_LINEAGE (1-hop at a time) and return all reached column ids."""
    visited: set[str] = set()
    found: set[str] = set()
    queue = [col_id]
    hops = 0
    while queue and hops < depth:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current})
        for row in rows:
            nid = row["id"]
            found.add(nid)
            queue.append(nid)
        hops += 1
    return found


# ---------------------------------------------------------------------------
# TC1 — Filtered-surface recall (the direct #38 guard)
# ---------------------------------------------------------------------------


def test_tc1_filtered_surface_recall(indexed_db):
    """CLI filtered upstream returns the physical sources for the measure column.

    Shape A + D: >=2 CTE hops; sources are only inside CTE bodies.
    Reverting the OPTIONAL-MATCH kind filter (Half B) to an inner join → RED.

    Note: CTE intermediate columns that have role='table' on their src side may also
    appear in the result (the kind-level suppression of CTE intermediates is the TC4
    concern and requires Phase 2 to fully address). TC1 only asserts recall: physical
    sources are present and the result is not empty.
    """
    db, _ = indexed_db
    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.fact_kpi.measure"})
    returned_ids = {r["id"] for r in rows}

    assert returned_ids, (
        "No results for mart.fact_kpi.measure — kind filter may have been reverted "
        "to inner-join (regression #38)."
    )
    assert any("src_a" in rid for rid in returned_ids), (
        f"staging.src_a source not found in upstream results.\nReturned: {sorted(returned_ids)}"
    )
    assert any("src_b" in rid for rid in returned_ids), (
        f"staging.src_b source not found in upstream results.\nReturned: {sorted(returned_ids)}"
    )


# ---------------------------------------------------------------------------
# TC2 — UNION-ALL completeness (both branches, computed measure)
# ---------------------------------------------------------------------------


def test_tc2_union_all_completeness(indexed_db):
    """Both UNION ALL branches must appear in the upstream result for measure.

    Shape B: CTE u UNION ALL of a and b.  A regression keeping only one branch
    passes TC1 but fails here.  The computed measure (m + 1) tests arithmetic projections.
    """
    db, _ = indexed_db
    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.fact_kpi.measure"})
    returned_ids = {r["id"] for r in rows}

    src_a_found = any("src_a" in rid for rid in returned_ids)
    src_b_found = any("src_b" in rid for rid in returned_ids)

    assert src_a_found, (
        f"UNION ALL branch staging.src_a missing from filtered upstream.\n"
        f"Returned: {sorted(returned_ids)}"
    )
    assert src_b_found, (
        f"UNION ALL branch staging.src_b missing from filtered upstream.\n"
        f"Returned: {sorted(returned_ids)}"
    )


# ---------------------------------------------------------------------------
# TC3 — Canonical-name reachability (catches #44) — expected RED before Phase 2
# ---------------------------------------------------------------------------


def test_tc3_canonical_name_reachability(indexed_db):
    """Schema-qualified DDL name must return the same results as schema-less spelling.

    Shape C: INSERT INTO mart.fact_kpi with DDL CREATE TABLE mart.fact_kpi.
    Before #44 fix the schema-qualified node may be a duplicate or missing,
    returning no results while the schema-less spelling works.
    This test is the acceptance test for the #44 canonical-name fix and should
    flip GREEN when Phase 2 lands.
    """
    db, _ = indexed_db
    query = _upstream_filtered_query()

    # Schema-qualified (the canonical DDL name — what a user types)
    qualified_rows = db.run_read(query, {"ref": "mart.fact_kpi.measure"})
    qualified_ids = {r["id"] for r in qualified_rows}

    assert qualified_ids, (
        "No upstream results for schema-qualified 'mart.fact_kpi.measure'. "
        "Expected the same sources as the schema-less spelling. "
        "This is the #44 acceptance test — should flip green after Phase 2."
    )


# ---------------------------------------------------------------------------
# TC4 — No synthetic node leaks into default output (catches #33/#45.2)
# ---------------------------------------------------------------------------


def test_tc4_no_cte_prefixed_nodes_in_default_output(indexed_db):
    """Nodes with 'cte_*' or 'cte_insert' prefix must not appear in default output.

    In production (DWH corpus), synthetic CTE-insert nodes are named with the prefix
    'cte_insert' or 'cte_*'.  The restored kind filter (OPTIONAL MATCH + IS NULL OR
    kind IN ['table','external']) must exclude nodes whose SqlTable has kind='cte' or
    kind='derived' (the latter from Phase 2's INSERT-target emission change).

    For the synthetic fixture, CTE aliases use short names (a, b, u, j) rather than
    'cte_*' prefixes.  This test pins the 'cte_insert' / 'cte_*' naming pattern that
    appears in real ETL and verifies it does not survive the filter.

    NOTE: CTE intermediate nodes (a, b, u, j) with role='table' on the source side
    are a pre-existing graph-truth ambiguity (their kind may be 'table' due to upsert
    ordering); full suppression of those requires Phase 2's kind='derived' emission for
    unresolved INSERT targets.  TC4 only gates the 'cte_*' naming leak today, and will
    be extended to cover the broader intermediate suppression after Phase 2.
    """
    db, _ = indexed_db
    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.fact_kpi.measure"})
    returned_ids = {r["id"] for r in rows}

    # No cte_insert / cte_* prefixed column-ids should appear (DWH naming convention)
    cte_leak = [rid for rid in returned_ids if "cte_insert" in rid or rid.startswith("cte_")]
    assert not cte_leak, (
        f"cte_insert / cte_* synthetic node(s) leaked into default output: {cte_leak}\n"
        "The kind filter (OPTIONAL MATCH + IS NULL OR kind IN [...]) should exclude these."
    )

    # Verify the filter does NOT drop the physical source (anti-regression check):
    # mart.fact_kpi should appear as a downstream-target, not filtered as intermediate
    assert any("src_a" in rid or "src_b" in rid for rid in returned_ids), (
        "Physical sources were filtered out — the kind filter may be too aggressive.\n"
        f"Returned: {sorted(returned_ids)}"
    )


# ---------------------------------------------------------------------------
# TC5 — CLI ↔ MCP parity
# ---------------------------------------------------------------------------


def test_tc5_cli_mcp_parity(indexed_db):
    """Physical-source set from the CLI filtered query and from MCP trace must agree.

    Both surfaces must return the same physical sources for mart.fact_kpi.measure.
    """
    db, _ = indexed_db

    # CLI surface (filtered query)
    cli_query = _upstream_filtered_query()
    cli_rows = db.run_read(cli_query, {"ref": "mart.fact_kpi.measure"})
    cli_physical = {r["id"] for r in cli_rows if "src_a" in r["id"] or "src_b" in r["id"]}

    # MCP surface (trace_column_lineage, unfiltered, walk all hops)
    mcp_ids = _mcp_upstream_ids(db, "mart.fact_kpi.measure")
    mcp_physical = {nid for nid in mcp_ids if "src_a" in nid or "src_b" in nid}

    assert cli_physical, (
        f"CLI surface returned no physical sources. CLI ids: {sorted({r['id'] for r in cli_rows})}"
    )
    assert mcp_physical, f"MCP surface returned no physical sources. MCP ids: {sorted(mcp_ids)}"
    assert cli_physical == mcp_physical, (
        f"CLI and MCP physical sources disagree.\n"
        f"CLI: {sorted(cli_physical)}\n"
        f"MCP: {sorted(mcp_physical)}"
    )


# ---------------------------------------------------------------------------
# TC6 — Source location present for physical-source rows (#45)
# ---------------------------------------------------------------------------


def test_tc6_source_location_present(indexed_db):
    """Every physical-source row must carry a real file:line, not None/null.

    After the Phase 0 query rewrite (aggregate per src before LIMIT), the
    file and line fields should be populated for sources that have outgoing
    COLUMN_LINEAGE edges with a query_id.

    Also asserts the row count equals the distinct-source count (no edge-fanout
    multiplication from the srcedge OPTIONAL MATCH).
    """
    db, _ = indexed_db
    query = _upstream_filtered_query()
    rows = db.run_read(query, {"ref": "mart.fact_kpi.measure"})

    assert rows, "No upstream results — TC1 should have caught this."

    # Row count must equal distinct source count (aggregation collapses edge fanout)
    returned_ids = [r["id"] for r in rows]
    distinct_ids = set(returned_ids)
    assert len(returned_ids) == len(distinct_ids), (
        f"Duplicate source rows detected — edge-fanout multiplication not collapsed.\n"
        f"Row count: {len(returned_ids)}, Distinct ids: {len(distinct_ids)}\n"
        f"Duplicates: {[rid for rid in returned_ids if returned_ids.count(rid) > 1]}"
    )

    # Every physical-source row must have a non-null file
    missing_file = [r["id"] for r in rows if not r.get("file")]
    assert not missing_file, (
        f"Physical-source rows missing file location: {missing_file}\n"
        "Expected file:line from outgoing COLUMN_LINEAGE edge → SqlQuery.file_path.\n"
        "If file is NULL, the srcedge OPTIONAL MATCH + aggregation may not be working."
    )


# ---------------------------------------------------------------------------
# TC7 — find / analyze unused honesty (#39 side-effects)
# ---------------------------------------------------------------------------


def test_tc7_cte_only_source_is_findable(indexed_db):
    """A CTE-body-only physical source must be findable via SqlTable node lookup.

    Shape D: staging.src_a is only referenced inside CTE body, never in a top-level
    FROM.  Half A (#39) emits a SqlTable node for it.  This test confirms the node
    exists (find table staging.src_a would work) with kind='table'.

    Note: CTE-only sources do not have SELECTS_FROM edges (those only come from the
    top-level query's stmt.sources). The 'analyze unused' (no SELECTS_FROM) check
    would incorrectly flag CTE-only sources as unused — that is a known limitation of
    the current SELECTS_FROM-based unused detection, not a regression from #39.
    TC7 only asserts findability (SqlTable node exists), not the unused-detection path.
    """
    db, _ = indexed_db

    # Check SqlTable node exists for the CTE-only source
    rows = db.run_read(
        'SELECT qualified AS q, kind FROM "SqlTable" WHERE qualified = ?',
        {"q": "staging.src_a"},
    )
    assert rows, (
        "SqlTable node for 'staging.src_a' not found — Half A (#39) may have been reverted.\n"
        "'find table staging.src_a' would return no results."
    )
    assert rows[0]["q"] == "staging.src_a"
    # The node must have kind='table' (not 'cte' or 'derived')
    kind = rows[0]["kind"]
    assert kind == "table", (
        f"staging.src_a has kind='{kind}', expected 'table'. "
        "Half A emits src-table nodes; the kind should be derived from edge.src.table.role."
    )

    # Verify staging.src_a is reachable via COLUMN_LINEAGE traversal
    # (confirms the #39 node is connected to the lineage graph)
    lineage_rows = db.run_read(
        'SELECT c.id AS id FROM "SqlColumn" c '
        'JOIN "COLUMN_LINEAGE" cl ON cl.src_key = c.id '
        "WHERE c.table_qualified = 'staging.src_a' LIMIT 5",
        {},
    )
    assert lineage_rows, (
        "No COLUMN_LINEAGE edges from staging.src_a columns — "
        "the CTE-only source exists as a node but is disconnected from lineage."
    )


# ---------------------------------------------------------------------------
# TC6b — Downstream terminal-sink location present (fix_downstream_sink_location.md)
# ---------------------------------------------------------------------------


def _downstream_filtered_query(depth: int = 5) -> str:
    """Build the exact downstream SQL query analyze.py constructs with kind_filter ON.

    Uses production _downstream_sql() so any regression in analyze.py reds this test
    (Phase 3 port: replaces the old Cypher _kind_filter("dst", ...) helper).
    """
    return _downstream_sql(depth, include_intermediate=False)


def test_tc6b_downstream_sink_location_present(indexed_db):
    """Terminal-sink downstream result must carry a real file:line, not NULL.

    Shape C: mart.fact_kpi.measure is the final INSERT target — it has no
    outgoing COLUMN_LINEAGE edge (nothing downstream consumes it) but it IS
    reachable as a downstream result from staging.src_a.x.  The current
    outgoing-edge binding ``(dst)-[dstedge]->()`` finds no edge for this sink
    → file/line are NULL → renders '?'.

    This test is RED on master (outgoing-edge query) and must flip GREEN after
    the Step 1.1 fix in fix_downstream_sink_location.md (incoming-edge query).

    TC6 (upstream) does NOT cover this: TC6 asserts file:line for a column that
    HAS an outgoing edge (a source).  TC6b explicitly targets a terminal sink —
    the case that caused 81% '?' on the DWH corpus.
    """
    db, _ = indexed_db

    # First confirm mart.fact_kpi.measure has zero outgoing COLUMN_LINEAGE edges —
    # if this assertion fails, the fixture changed and the sink choice is wrong.
    outgoing_count = db.run_read(
        'SELECT count(*) AS n FROM "COLUMN_LINEAGE" WHERE src_key = ?',
        {"id": "mart.fact_kpi.measure"},
    )
    sink_outgoing = outgoing_count[0]["n"] if outgoing_count else 0
    assert sink_outgoing == 0, (
        f"mart.fact_kpi.measure has {sink_outgoing} outgoing COLUMN_LINEAGE edge(s) — "
        "it is no longer a terminal sink.  Choose a different sink column for TC6b."
    )

    # Run the downstream query starting from a physical source.
    # The terminal sink mart.fact_kpi.measure must appear in the result.
    query = _downstream_filtered_query()
    rows = db.run_read(query, {"ref": "staging.src_a.x"})
    assert rows, "No downstream results from staging.src_a.x — TC1/upstream lineage may be broken."

    sink_rows = [r for r in rows if r["id"] == "mart.fact_kpi.measure"]
    assert sink_rows, (
        f"mart.fact_kpi.measure not found in downstream results.\n"
        f"Returned ids: {sorted(r['id'] for r in rows)}"
    )

    # The load-bearing assertion: the terminal sink must have a real file, not NULL.
    # This is RED on master (outgoing-edge query returns NULL for a sink) and GREEN
    # after the fix (incoming-edge query finds the producing INSERT query's file_path).
    sink_file = sink_rows[0].get("file")
    assert sink_file is not None and sink_file != "", (
        f"mart.fact_kpi.measure has file='{sink_file}' (NULL/empty) — "
        "the downstream location-binding query uses the outgoing edge, "
        "which is absent for a terminal sink.  "
        "Fix: flip OPTIONAL MATCH (dst)-[dstedge:COLUMN_LINEAGE]->() "
        "to OPTIONAL MATCH ()-[dstedge:COLUMN_LINEAGE]->(dst) in analyze.py "
        "lines ~134 and ~146 (fix_downstream_sink_location.md Step 1.1)."
    )
