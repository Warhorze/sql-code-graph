"""Issue #38 (residual) — backfill mis-ordering + synthetic-node leak regression
suite, plus a permanent cross-tool consistency invariant for the table-graph
MCP tools (get_change_scope, get_backfill_order, diff_impact, scope_change).

Confirmed-real defects (see plan/sprints/issue-38-backfill-cte-bridge.md, Phase 1
Verdict + Deviation 1) for CTE-wrapped ``INSERT ... WITH cte AS (...) SELECT
... FROM cte`` chains:

1. Mis-ordering — ``get_backfill_order`` degraded to alphabetical ordering
   because no ``SELECTS_FROM`` adjacency is emitted for CTE-wrapped
   INSERT...SELECT statements (the real source table is nested in the CTE's
   child scope and never surfaces into the statement's top-level sources).
   Fixed by deriving Kahn adjacency from the COLUMN_LINEAGE closure instead
   (Option A, with synthetic-node-hop contraction — see Deviation 1: the raw
   topology bridges producer -> consumer via a two-hop relay through the
   synthetic cte node, NOT a parallel direct edge as an earlier draft of the
   plan assumed).
2. Synthetic-node leak — the synthetic ``cte``/``derived`` SqlTable nodes
   were emitted as rebuildable tables in all four tools' output. Fixed by
   ``_exclude_synthetic_tables()``, keyed on the authoritative
   ``SqlTable.kind`` (never the alias string).

Sibling-tool defect-sharing (stated explicitly per plan Step 3.2):
  - get_change_scope / get_backfill_order: both defects, pinned directly here.
  - diff_impact: shares both defects (runs the same closure +
    _kahn_topological_sort per changed file, unioned).
  - scope_change: shares both defects TRANSITIVELY — it delegates to
    get_change_scope + get_backfill_order and reassembles their results with
    no additional filtering of its own.

These tests use a real in-memory DuckDB graph (Indexer().index_repo) and
assert on observable output — concrete id sets, raw edge rows, and
list-index comparisons — never just "no exception raised", mirroring
tests/integration/test_bare_column_cte_lineage.py.
"""

from __future__ import annotations

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixture SQL corpus — a 3-table CTE-wrapped INSERT...SELECT producer chain
# (s.tablea -> s.tableb -> s.tablec) plus a direct CREATE-VIEW consumer as a
# control (proves the test isn't trivially passing on an empty radius).
#
# 3 tables are REQUIRED (not a 2-table bridge): both get_change_scope and
# get_backfill_order filter the TARGET out of its own affected set
# (tools.py: `affected_tables_all = [... if t != target]`), so with T =
# s.tablea, s.tablea can never appear in its own backfill_order — the chain
# gives two assertable downstream non-target members (s.tableb, s.tablec).
#
# Distinct CTE alias names per statement (cte_b / cte_c) are deliberate: an
# earlier exploratory fixture using the same alias `cte` in both INSERTs
# collapsed both synthetic nodes into a single shared hub (a parsing
# artifact of alias-name collision across files), corrupting the per-link
# topology this suite pins. Distinct aliases give the clean per-statement
# two-hop relay the plan's corrected (Deviation 1) topology describes.
# ---------------------------------------------------------------------------

_DDL_A_SQL = """\
CREATE TABLE s.tablea (id INT, val INT);
"""

_DDL_BC_SQL = """\
CREATE TABLE s.tableb (id INT, val INT);
CREATE TABLE s.tablec (id INT, val INT);
"""

_INSERT_B_SQL = """\
INSERT INTO s.tableb (id, val)
WITH cte_b AS (
    SELECT a.id AS id, a.val AS val FROM s.tablea AS a
)
SELECT cte_b.id, cte_b.val FROM cte_b;
"""

_INSERT_C_SQL = """\
INSERT INTO s.tablec (id, val)
WITH cte_c AS (
    SELECT b.id AS id, b.val AS val FROM s.tableb AS b
)
SELECT cte_c.id, cte_c.val FROM cte_c;
"""

_VIEW_SQL = """\
CREATE VIEW s.v_consumer AS SELECT id, val FROM s.tablea;
"""

_TARGET = "s.tablea"
_PRODUCER = "s.tableb"
_CONSUMER = "s.tablec"
_CONTROL = "s.v_consumer"
_CTE_B = "cte_b"
_CTE_C = "cte_c"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def indexed_db(tmp_path, monkeypatch):
    """Index the 3-table CTE-chain + control-view corpus into a fresh
    in-memory DuckDB graph and wire it as the tools backend.

    chdir into tmp_path so NoiseFilter / presentation config resolve from a
    clean root (default patterns, no stray repo .sqlcg.toml) — mirrors
    test_anchor_tools._index_fixture.
    """
    # DDL split across two files — s.tablea defined alone — so diff_impact's
    # per-file "changed_tables" set is exactly {s.tablea}, not {a, b, c}; the
    # latter would make b/c "changed" rather than "affected" and collapse the
    # very radius this suite pins (see test_diff_impact_shares_and_resolves_*).
    (tmp_path / "00a_ddl_tablea.sql").write_text(_DDL_A_SQL)
    (tmp_path / "00b_ddl_tablebc.sql").write_text(_DDL_BC_SQL)
    (tmp_path / "01_insert_b.sql").write_text(_INSERT_B_SQL)
    (tmp_path / "02_insert_c.sql").write_text(_INSERT_C_SQL)
    (tmp_path / "03_view.sql").write_text(_VIEW_SQL)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend, tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_lineage_edges(db: DuckDBBackend) -> list[dict]:
    return db.run_read(
        'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE"'
        " ORDER BY src_key, dst_key",
        {},
    )


def _selects_from_for_target(db: DuckDBBackend, target_table: str) -> list[dict]:
    return db.run_read(
        'SELECT sf.src_key, sf.dst_key FROM "SELECTS_FROM" sf'
        ' JOIN "SqlQuery" q ON q.id = sf.src_key'
        " WHERE q.target_table = ?",
        {"target_table": target_table},
    )


def _has_outgoing_lineage(edges: list[dict], table_qualified: str) -> bool:
    return any(e["src_key"].startswith(f"{table_qualified}.") for e in edges)


# ---------------------------------------------------------------------------
# Raw-topology pre-asserts (Step 2.1 acceptance) — run BEFORE any tool
# assertion, so a future parser change that alters this topology fails
# loudly here rather than silently passing downstream.
# ---------------------------------------------------------------------------


def test_raw_topology_two_hop_relay_through_synthetic_cte_nodes(indexed_db):
    """Pin the corrected (Deviation 1) raw topology: each CTE-wrapped INSERT
    produces a TWO-HOP relay producer -> cte -> consumer (NOT a parallel
    direct producer -> consumer edge with a disconnected dead-end cte leaf,
    as an earlier draft of the plan assumed and Phase 1 found does not
    reproduce).

    PR 3 (sprint_lineage_identity_and_session_context.md §PR 3): CTE keys are
    now namespaced with the defining file path (e.g. "/tmp/.../01_insert_b.sql::cte_b").
    The two-hop relay topology is unchanged; only the key format changed.

    PR-2 (#38 island-lever fix, plan/sprints/issue-38-selects-from-island-lever.md):
    _real_tables() now descends CTE child scopes, so the previously-empty SELECTS_FROM
    for CTE-wrapped inserts is now populated.  The assertion flips from "empty" to
    "non-empty" here; the Kahn topological sort (get_backfill_order) still reads
    COLUMN_LINEAGE adjacency and is unaffected.
    """
    db, tmp_path = indexed_db
    edges = _raw_lineage_edges(db)
    pairs = {(e["src_key"], e["dst_key"]) for e in edges}

    # PR 3: look up the actual namespaced CTE qualified keys from the graph.
    # The keys are now "{abs_path_to_file}::cte_alias" rather than bare "cte_b".
    cte_rows = db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte' ORDER BY qualified",
        {},
    )
    cte_keys = {r["qualified"] for r in cte_rows}
    cte_b_key = next(k for k in cte_keys if k.endswith("::cte_b"))
    cte_c_key = next(k for k in cte_keys if k.endswith("::cte_c"))

    # Two-hop relay for the tableA -> tableB link: tablea -> cte_b -> tableb.
    assert ("s.tablea.id", f"{cte_b_key}.id") in pairs, (
        f"producer -> cte_b leg missing. cte_b_key={cte_b_key!r}, pairs={sorted(pairs)[:10]}"
    )
    assert (f"{cte_b_key}.id", "s.tableb.id") in pairs, (
        f"cte_b -> consumer leg missing. cte_b_key={cte_b_key!r}"
    )
    # NOT a parallel direct edge (the unreproduced "topology fact" — Deviation 1).
    assert ("s.tablea.id", "s.tableb.id") not in pairs, (
        "no direct producer->consumer edge exists; the bridge is two-hop through cte_b"
    )

    # Two-hop relay for the tableB -> tableC link: tableb -> cte_c -> tablec.
    assert ("s.tableb.id", f"{cte_c_key}.id") in pairs, (
        f"producer -> cte_c leg missing. cte_c_key={cte_c_key!r}"
    )
    assert (f"{cte_c_key}.id", "s.tablec.id") in pairs, (
        f"cte_c -> consumer leg missing. cte_c_key={cte_c_key!r}"
    )
    assert ("s.tableb.id", "s.tablec.id") not in pairs, (
        "no direct producer->consumer edge exists; the bridge is two-hop through cte_c"
    )

    # The synthetic nodes are RELAYS (have outgoing edges), not dead-end leaves.
    assert _has_outgoing_lineage(edges, cte_b_key), (
        "cte_b must have an outgoing edge (it is a relay)"
    )
    assert _has_outgoing_lineage(edges, cte_c_key), (
        "cte_c must have an outgoing edge (it is a relay)"
    )

    # Control: the direct view edge exists (proves the corpus has a non-CTE consumer).
    assert ("s.tablea.id", "s.v_consumer.id") in pairs, "control view edge missing"

    # PR-2 (#38 island-lever fix, plan/sprints/issue-38-selects-from-island-lever.md):
    # _real_tables() now descends CTE child scopes, so CTE-wrapped INSERT...SELECT
    # statements emit SELECTS_FROM source rows.  The empty-SELECTS_FROM signature that
    # was the root cause of defect 1 (Kahn alphabetical fallback) is now FIXED.
    # The Kahn topological sort still uses COLUMN_LINEAGE (not SELECTS_FROM) for adjacency
    # and remains correct; SELECTS_FROM now also has the source rows for island resolution.
    sf_producer = _selects_from_for_target(db, _PRODUCER)
    sf_consumer = _selects_from_for_target(db, _CONSUMER)
    assert len(sf_producer) >= 1, (
        "SELECTS_FROM must be non-empty for the CTE-wrapped s.tableb target after PR-2 fix.\n"
        "PR-2 (_real_tables CTE-body descent) should have emitted source rows.\n"
        f"Got: {sf_producer}"
    )
    assert len(sf_consumer) >= 1, (
        "SELECTS_FROM must be non-empty for the CTE-wrapped s.tablec target after PR-2 fix.\n"
        "PR-2 (_real_tables CTE-body descent) should have emitted source rows.\n"
        f"Got: {sf_consumer}"
    )


def test_raw_topology_synthetic_nodes_are_cte_kind(indexed_db):
    """The synthetic nodes carry the authoritative kind="cte" marker
    (the basis for _exclude_synthetic_tables — never alias-string matching).

    PR 3: keys are now namespaced; look them up from the graph rather than
    comparing to the static bare-name constants.
    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """
    db, _ = indexed_db
    rows = db.run_read(
        "SELECT qualified, kind FROM \"SqlTable\" WHERE kind = 'cte' "
        "AND (qualified LIKE '%::cte_b' OR qualified LIKE '%::cte_c')",
        {},
    )
    kinds = {r["qualified"].split("::")[-1]: r["kind"] for r in rows}
    assert kinds == {"cte_b": "cte", "cte_c": "cte"}, (
        f"Expected two CTE nodes with kind='cte', got: {rows}"
    )


# ---------------------------------------------------------------------------
# Defect 1 — mis-ordering (causal, not alphabetical)
# ---------------------------------------------------------------------------


def test_backfill_order_is_causal_not_alphabetical(indexed_db):
    """get_backfill_order(s.tablea): producer (s.tableb) precedes consumer
    (s.tablec) — the causal order, not the alphabetical fallback (which would
    happen to coincide here only by naming accident; the assertion is on
    *index* ordering, the same invariant that fails on non-alphabetical names
    per the Phase 1 verdict reproduction)."""
    result = tools.get_backfill_order(_TARGET)

    assert _PRODUCER in result.backfill_order
    assert _CONSUMER in result.backfill_order
    assert result.backfill_order.index(_PRODUCER) < result.backfill_order.index(_CONSUMER), (
        f"producer must precede consumer in causal rebuild order: {result.backfill_order}"
    )
    # Target is correctly absent from its own backfill set (tools.py filters it out).
    assert _TARGET not in result.backfill_order
    assert result.hint is None or "cycle" not in result.hint.lower()


# ---------------------------------------------------------------------------
# Defect 2 — synthetic-node exclusion (both directions, all tools)
# ---------------------------------------------------------------------------


def test_synthetic_cte_nodes_excluded_from_change_scope_and_backfill(indexed_db):
    """The synthetic cte_b/cte_c nodes appear in NEITHER get_change_scope's
    affected_tables NOR get_backfill_order's backfill_order — and ARE
    reported under noise_excluded so the information is not silently dropped.

    PR 3: CTE keys are now namespaced (e.g. "/tmp/.../01_insert_b.sql::cte_b"),
    so we check noise_excluded using suffix matching instead of bare-name equality.
    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """
    scope = tools.get_change_scope(_TARGET)
    backfill = tools.get_backfill_order(_TARGET)

    for synthetic in (_CTE_B, _CTE_C):
        # bare name must not appear (neither bare nor namespaced should surface as rebuildable)
        assert synthetic not in scope.affected_tables, (
            f"{synthetic} (kind=cte) must not surface as a rebuildable table: "
            f"{scope.affected_tables}"
        )
        assert synthetic not in backfill.backfill_order, (
            f"{synthetic} (kind=cte) must not surface in backfill order: {backfill.backfill_order}"
        )
        # PR 3: noise_excluded contains namespaced keys; check by suffix
        assert any(k == synthetic or k.endswith(f"::{synthetic}") for k in scope.noise_excluded), (
            f"Expected '{synthetic}' (or namespaced variant) in "
            f"noise_excluded: {scope.noise_excluded}"
        )
        assert any(
            k == synthetic or k.endswith(f"::{synthetic}") for k in backfill.noise_excluded
        ), (
            f"Expected '{synthetic}' (or namespaced variant) in backfill.noise_excluded: "
            f"{backfill.noise_excluded}"
        )

    # Real downstream members and the control view ARE present (not over-excluded).
    assert _PRODUCER in scope.affected_tables
    assert _CONSUMER in scope.affected_tables
    assert _CONTROL in scope.affected_tables
    assert _PRODUCER in backfill.backfill_order
    assert _CONSUMER in backfill.backfill_order
    assert _CONTROL in backfill.backfill_order


# ---------------------------------------------------------------------------
# Cross-tool consistency invariant (Step 3.1) — containment + ordering +
# synthetic exclusion. NOT set-equality (affected_tables is an unordered set,
# backfill_order an ordered list — they are legitimately asymmetric).
# ---------------------------------------------------------------------------


def test_change_scope_and_backfill_order_consistency_invariant(indexed_db):
    """The permanent cross-tool consistency regression (Step 3.1):

    - Containment: every real table in get_change_scope's downstream radius
      appears in get_backfill_order's order.
    - Ordering: producer precedes consumer among the downstream non-target
      members (s.tablea is filtered from its own set — unassertable).
    - Synthetic exclusion (both directions): cte_b/cte_c appear in NEITHER.
    - Control: the direct view consumer appears in both (radius isn't empty).
    """
    scope = tools.get_change_scope(_TARGET)
    backfill = tools.get_backfill_order(_TARGET)

    # Containment (not set-equality): every real affected table is in backfill_order.
    backfill_set = set(backfill.backfill_order)
    missing = [t for t in scope.affected_tables if t not in backfill_set]
    assert not missing, f"tables present in change_scope but missing from backfill_order: {missing}"

    # Ordering over downstream non-target members.
    assert backfill.backfill_order.index(_PRODUCER) < backfill.backfill_order.index(_CONSUMER)

    # Synthetic exclusion, both directions, both tools.
    for synthetic in (_CTE_B, _CTE_C):
        assert synthetic not in scope.affected_tables
        assert synthetic not in backfill.backfill_order

    # Control: proves containment isn't trivially satisfied by an empty radius.
    assert _CONTROL in scope.affected_tables
    assert _CONTROL in backfill.backfill_order


# ---------------------------------------------------------------------------
# Step 3.2 — extend the consistency invariant to scope_change and diff_impact
# ---------------------------------------------------------------------------


def test_scope_change_shares_and_resolves_both_defects_transitively(indexed_db):
    """scope_change DELEGATES to get_change_scope + get_backfill_order and
    reassembles their results with no additional filtering — it shares BOTH
    defects transitively, and is fixed transitively by the same two fixes."""
    result = tools.scope_change(_TARGET)

    # Containment + ordering over the delegated radius/order.
    assert _PRODUCER in result.downstream_blast_radius
    assert _CONSUMER in result.downstream_blast_radius
    assert _CONTROL in result.downstream_blast_radius
    assert result.backfill_order.index(_PRODUCER) < result.backfill_order.index(_CONSUMER)

    # Synthetic exclusion, both directions.
    # PR 3: noise_excluded contains namespaced keys; check by suffix
    for synthetic in (_CTE_B, _CTE_C):
        assert synthetic not in result.downstream_blast_radius
        assert synthetic not in result.backfill_order
        assert any(k == synthetic or k.endswith(f"::{synthetic}") for k in result.noise_excluded), (
            f"Expected '{synthetic}' (or namespaced variant) in "
            f"scope_change.noise_excluded: {result.noise_excluded}"
        )


def test_diff_impact_shares_and_resolves_both_defects(indexed_db):
    """diff_impact runs the SAME closure + _kahn_topological_sort per changed
    file (unioned) — it shares BOTH defects and is fixed by the same two
    helpers (_kahn_topological_sort's contracted adjacency,
    _exclude_synthetic_tables)."""
    _, tmp_path = indexed_db
    result = tools.diff_impact([str(tmp_path / "00a_ddl_tablea.sql")])

    assert _TARGET in result.changed_tables

    assert _PRODUCER in result.affected_tables
    assert _CONSUMER in result.affected_tables
    assert _CONTROL in result.affected_tables
    assert result.backfill_order.index(_PRODUCER) < result.backfill_order.index(_CONSUMER), (
        f"producer must precede consumer: {result.backfill_order}"
    )

    # PR 3: noise_excluded contains namespaced keys; check by suffix
    for synthetic in (_CTE_B, _CTE_C):
        assert synthetic not in result.affected_tables, (
            f"{synthetic} (kind=cte) must not surface in diff_impact.affected_tables: "
            f"{result.affected_tables}"
        )
        assert synthetic not in result.backfill_order
        assert any(k == synthetic or k.endswith(f"::{synthetic}") for k in result.noise_excluded), (
            f"Expected '{synthetic}' (or namespaced variant) in "
            f"diff_impact.noise_excluded: {result.noise_excluded}"
        )


# ---------------------------------------------------------------------------
# Cycle-risk mitigation (plan Risks/Step 2.2 acceptance) — Option A's
# rollup-based adjacency must NOT introduce table-level cycles the old
# SELECTS_FROM graph did not have. Pin a benign multi-producer fan-in
# (two independent producers feeding one consumer, both reachable from the
# target) and assert backfill_order does not collapse into the cycle fallback.
# ---------------------------------------------------------------------------

_FANIN_DDL_SQL = """\
CREATE TABLE f.src1 (id INT);
CREATE TABLE f.src2 (id INT);
CREATE TABLE f.merged (id INT);
"""

_FANIN_MERGE_SQL = """\
INSERT INTO f.merged (id)
WITH cte_m AS (
    SELECT s1.id AS id FROM f.src1 AS s1
    UNION ALL
    SELECT s2.id AS id FROM f.src2 AS s2
)
SELECT cte_m.id FROM cte_m;
"""


@pytest.fixture
def indexed_fanin_db(tmp_path, monkeypatch):
    """Index a benign multi-producer fan-in corpus: two independent source
    tables both feed one CTE-wrapped INSERT target — a shape that a naive
    rollup-based adjacency could mistake for a cycle (multiple producers,
    same consumer, no SELECTS_FROM adjacency to anchor on)."""
    (tmp_path / "00_ddl.sql").write_text(_FANIN_DDL_SQL)
    (tmp_path / "01_merge.sql").write_text(_FANIN_MERGE_SQL)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend, tmp_path


def test_multi_producer_fanin_introduces_no_spurious_cycle(indexed_fanin_db):
    """Option A must not turn a benign multi-producer fan-in (two independent
    sources feeding one CTE-wrapped consumer) into a spurious table-level
    cycle. f.merged has two independent producers (f.src1, f.src2); from the
    perspective of either source's backfill, the radius is a simple one-hop
    fan-in with no cycle — the cycle-degradation contract must not fire."""
    result = tools.get_backfill_order("f.src1")

    assert "f.merged" in result.backfill_order
    assert result.hint is None or "cycle" not in result.hint.lower(), (
        f"benign multi-producer fan-in must not degrade to the cycle fallback: {result.hint}"
    )
    # The synthetic merge-CTE node must still be excluded from this radius too.
    # PR 3: noise_excluded contains namespaced keys; check by suffix.
    assert "cte_m" not in result.backfill_order
    assert any(k == "cte_m" or k.endswith("::cte_m") for k in result.noise_excluded), (
        f"Expected 'cte_m' (or namespaced variant) in noise_excluded: {result.noise_excluded}"
    )
