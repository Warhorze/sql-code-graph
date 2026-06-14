"""Bug #6 gate: ``analyze impact <T>`` must not under-report consumers that
``downstream`` / ``empty-impact`` surface.

Governing plan: [bugfix_impact_consumer_union]
([plan doc](plan/sprints/bugfix_impact_consumer_union.md), branch
``plan/impact-consumer-union``).

GATE OUTCOME: REPRODUCED at DWH scale. The bug is real: the live DWH node
``ba.all_rows_in_selection`` is a *cross-file qualified-name-promoted* inner-CTE
intermediate — ``kind='table'``, **non-namespaced** (no ``::``), **0
SELECTS_FROM in-edges**, **55 COLUMN_LINEAGE out-edges**. ``analyze impact``
joined only SELECTS_FROM, so it returned "No results" for that node while
``empty-impact`` / ``downstream`` (which follow COLUMN_LINEAGE) found the real
consumer.

Why the earlier "NOT REPRODUCED" claim was wrong (PR #156): the disproven
hypothesis was that *only namespaced CTE intermediates* (``kind='cte'``) ever
have a COLUMN_LINEAGE/STAR read without a SELECTS_FROM edge. A from-scratch
in-memory index of a nested-CTE INSERT…SELECT does indeed only emit a namespaced
``kind='cte'`` gap node — but the live divergence arises from a **cross-file**
promotion effect (a source-ref node defaults to ``kind='table'`` and is only
upgraded to ``kind='cte'`` when the *same* qualified name is later confirmed a
CTE destination in the same pass; the single-file case namespaces+confirms →
``cte``). A unit/indexer fixture cannot reproduce the non-namespaced
``kind='table'`` shape, so the authoritative repro below is **graph-layer
seeded**: it writes the exact divergence shape directly, with no indexer. The
nested-CTE indexer fixture is kept only as a *no-regression* case (it proves
``impact`` on a physical source is unchanged), NOT as the bug repro.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import IMPACT_CONSUMERS_VIA_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer

# The exact query the OLD `analyze impact <table>` ran (SELECTS_FROM only): a
# single hop SqlTable -> SELECTS_FROM -> SqlQuery. Kept here as the negative
# control — the seeded repro asserts this returns ∅ for the promoted node.
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

# (case-id, consumer-SQL, physical-target-table) — every shape the validation
# sprint named, plus subquery / CTE / view / star variants.
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
    """OLD `impact`: direct SELECTS_FROM consumers only (the under-reporting query)."""
    return {r["id"] for r in db.run_read(_IMPACT_SQL, {"t": table})}


def _union_consumers(db: DuckDBBackend, table: str) -> set[str]:
    """The full consumer set across SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE.

    This is the parity yardstick the fixed ``impact`` reaches.
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


def _merged_impact_consumers(db: DuckDBBackend, table: str) -> list[dict]:
    """The NEW `impact` consumer set: direct (SELECTS_FROM) merged with via-lineage
    (COLUMN_LINEAGE ∪ STAR_SOURCE), deduped on q.id — exactly what
    ``analyze.py:impact()`` builds. Two ``?`` both bound to the qualified name via
    an ordered 2-entry dict (``_params_list`` returns ``list(values())``).
    """
    direct = db.run_read(_IMPACT_SQL, {"t": table})
    via = db.run_read(IMPACT_CONSUMERS_VIA_LINEAGE_QUERY, {"t": table, "t2": table})
    seen = {r["id"] for r in direct}
    return (direct + [r for r in via if r["id"] not in seen])[:100]


def _seed_promoted_intermediate(db: DuckDBBackend) -> tuple[str, str]:
    """Seed the live DWH divergence shape DIRECTLY (no indexer).

    Writes:
      * a non-namespaced ``kind='table'`` SqlTable (``ba.promoted_intermediate``);
      * a SqlColumn whose ``table_qualified`` is that table;
      * a consumer SqlQuery (INSERT INTO ba.consumer);
      * a COLUMN_LINEAGE edge column -> consumer-column with ``query_id`` = consumer;
      * NO SELECTS_FROM in-edge to the seeded table.

    Returns ``(table_qualified, consumer_query_id)``. This is the only shape that
    reproduces Bug #6 (cross-file promotion); an indexer cannot emit it.
    """
    table = "ba.promoted_intermediate"
    consumer_qid = "q:ba.consumer:0"
    src_col = f"{table}.dn_functie_naam"
    dst_col = "ba.consumer.dn_functie_naam"

    db.upsert_node(
        "SqlTable",
        table,
        {"catalog": None, "db": "ba", "name": "promoted_intermediate", "kind": "table"},
    )
    db.upsert_node(
        "SqlColumn",
        src_col,
        {
            "catalog": None,
            "db": "ba",
            "table_name": "promoted_intermediate",
            "col_name": "dn_functie_naam",
            "table_qualified": table,
        },
    )
    db.upsert_node(
        "SqlColumn",
        dst_col,
        {
            "catalog": None,
            "db": "ba",
            "table_name": "consumer",
            "col_name": "dn_functie_naam",
            "table_qualified": "ba.consumer",
        },
    )
    db.upsert_node(
        "SqlQuery",
        consumer_qid,
        {
            "file_path": "consumer.sql",
            "statement_index": 0,
            "sql": "INSERT INTO ba.consumer SELECT dn_functie_naam FROM ba.promoted_intermediate",
            "kind": "INSERT_SELECT",
            "target_table": "ba.consumer",
            "parse_failed": False,
            "start_line": 1,
        },
    )
    db.upsert_edge(
        "SqlColumn",
        src_col,
        "SqlColumn",
        dst_col,
        "COLUMN_LINEAGE",
        {"transform": "CTE_PROJECTION", "confidence": 1.0, "query_id": consumer_qid},
    )
    return table, consumer_qid


@pytest.mark.parametrize(
    ("case_id", "consumer_sql", "target"),
    IMPACT_PARITY_CASES,
    ids=[c[0] for c in IMPACT_PARITY_CASES],
)
def test_impact_matches_full_consumer_set_for_physical_target(
    tmp_path, case_id, consumer_sql, target
):
    """`analyze impact <physical table>` covers the full SELECTS_FROM ∪ STAR ∪
    COLUMN_LINEAGE consumer union (the union is a superset, never drops the
    physical consumer).

    Guards the union fix's superset property
    ([plan doc](plan/sprints/bugfix_impact_consumer_union.md), A3): for a physical
    table the merged ``impact`` set equals the full union — it neither under-reports
    nor adds spurious rows.
    """
    db = _index(tmp_path, {"ddl.sql": _DDL, "etl.sql": consumer_sql})
    try:
        merged = {r["id"] for r in _merged_impact_consumers(db, target)}
        union = _union_consumers(db, target)
        assert merged, f"[{case_id}] impact found no consumer for {target}"
        # Merged impact must equal the full union for a physical target.
        assert union - merged == set(), (
            f"[{case_id}] merged impact UNDER-reports for {target}: "
            f"union has {sorted(union)}, merged has {sorted(merged)}, "
            f"missed {sorted(union - merged)}"
        )
        assert merged - union == set(), (
            f"[{case_id}] merged impact OVER-reports for {target}: extra {sorted(merged - union)}"
        )
    finally:
        db.close()


def test_impact_surfaces_lineage_only_consumer_of_promoted_intermediate(tmp_path):
    """The fixed ``impact`` surfaces a consumer reachable ONLY via COLUMN_LINEAGE
    (no SELECTS_FROM edge) — the live ``ba.all_rows_in_selection`` shape.

    Authoritative Bug #6 repro, GRAPH-LAYER SEEDED
    ([plan doc](plan/sprints/bugfix_impact_consumer_union.md), A1). FAILS pre-fix
    (the OLD SELECTS_FROM-only query returns ∅), PASSES post-fix (the union
    surfaces the consumer). The negative control (``_impact_consumers`` == ∅) is
    asserted in the same test so the repro cannot silently pass for the wrong
    reason.
    """
    db = DuckDBBackend(":memory:")
    db.init_schema()
    try:
        table, consumer_qid = _seed_promoted_intermediate(db)

        # Negative control: the OLD SELECTS_FROM-only query finds nothing (the bug).
        old = _impact_consumers(db, table)
        assert old == set(), (
            f"negative control broken: the SELECTS_FROM-only impact query should "
            f"return ∅ for a promoted intermediate with no SELECTS_FROM in-edge, "
            f"got {sorted(old)}"
        )

        # The fix: the merged (direct ∪ via-lineage) impact surfaces the consumer.
        merged = {r["id"] for r in _merged_impact_consumers(db, table)}
        assert consumer_qid in merged, (
            f"fixed impact must surface the lineage-only consumer {consumer_qid}; "
            f"got {sorted(merged)}"
        )
    finally:
        db.close()


def test_impact_physical_source_unchanged_by_union(tmp_path):
    """``impact`` on a physical source of a nested-CTE INSERT…SELECT is UNCHANGED
    by the union (direct == merged).

    No-regression case ([plan doc](plan/sprints/bugfix_impact_consumer_union.md),
    A2). This nested-CTE indexer fixture does NOT reproduce Bug #6 (it emits only a
    namespaced ``kind='cte'`` gap node, and ``ba.src`` already has a SELECTS_FROM
    edge) — the live divergence needs cross-file promotion, covered by the seeded
    test above. Here it proves the union adds/drops nothing for a physical source.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE ba.src (col1 INT, col2 INT);\n"
            "CREATE TABLE ba.tgt (col1 INT, col2 INT);\n",
            "etl.sql": "INSERT INTO ba.tgt WITH outer_c AS ("
            "  WITH all_rows AS (SELECT col1, col2 FROM ba.src) SELECT col1, col2 FROM all_rows"
            ") SELECT col1, col2 FROM outer_c;\n",
        },
    )
    try:
        direct = _impact_consumers(db, "ba.src")
        merged = {r["id"] for r in _merged_impact_consumers(db, "ba.src")}
        assert direct, "physical source ba.src must have a direct SELECTS_FROM consumer"
        assert direct == merged, (
            f"union changed the consumer set for physical source ba.src: "
            f"direct {sorted(direct)} != merged {sorted(merged)}"
        )
    finally:
        db.close()


def test_impact_dedups_consumer_reachable_via_both_edges(tmp_path):
    """A consumer reachable via BOTH SELECTS_FROM and COLUMN_LINEAGE is listed
    exactly once (dedup on q.id).

    Guards A4 ([plan doc](plan/sprints/bugfix_impact_consumer_union.md)): the merge
    must not double-count. A plain ``INSERT … SELECT … FROM ba.src`` yields a
    consumer reachable both ways; assert it appears once.
    """
    db = _index(
        tmp_path,
        {"ddl.sql": _DDL, "etl.sql": "INSERT INTO ba.tgt SELECT col1 FROM ba.src;\n"},
    )
    try:
        rows = _merged_impact_consumers(db, "ba.src")
        ids = [r["id"] for r in rows]
        assert ids, "expected at least one consumer for ba.src"
        assert len(ids) == len(set(ids)), f"duplicate consumer rows in impact: {ids}"
    finally:
        db.close()


def test_impact_result_schema_has_id_and_kind(tmp_path):
    """The merged ``impact`` rows still expose ``id`` and ``kind`` keys (output
    schema unchanged).

    Guards A5 ([plan doc](plan/sprints/bugfix_impact_consumer_union.md)).
    """
    db = _index(
        tmp_path,
        {"ddl.sql": _DDL, "etl.sql": "INSERT INTO ba.tgt SELECT col1 FROM ba.src;\n"},
    )
    try:
        rows = _merged_impact_consumers(db, "ba.src")
        assert rows, "expected at least one consumer row"
        for r in rows:
            assert "id" in r and "kind" in r, f"missing id/kind in row keys: {sorted(r)}"
    finally:
        db.close()
