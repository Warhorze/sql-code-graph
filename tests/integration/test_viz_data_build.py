"""Integration tests for build_viz_data against a real in-memory DuckDB graph.

Guards the edge-model correctness of the graph-viz feature
([plan doc](plan/sprints/feature_graph_viz.md), plan-review B1 + Step 1.2):

* SELECTS_FROM / STAR_SOURCE are SqlQuery->SqlTable; the source table is the
  SqlTable end and the target is SqlQuery.target_table (no query node leaks
  into the link).
* COLUMN_LINEAGE is SqlColumn->SqlColumn; both endpoints collapse to table level.
* self-loops dropped; duplicate (src,dst) collapsed with a new ``n`` count.
* deg = distinct-neighbor count; adj = undirected neighbor map.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.viz.data import build_viz_data


@pytest.fixture
def seeded_backend():
    """In-memory DuckDB graph with mixed kinds, a catalogued table, and edges.

    Tables:
      ba.src_a   (table, catalogued)
      ba.src_b   (table)
      da.tgt     (view)
      da.cte_x   (cte)
      ia.stage   (temp)

    Edges (query-anchored + column):
      - SqlQuery q1 (target da.tgt) SELECTS_FROM ba.src_a   -> ba.src_a -> da.tgt
      - SqlQuery q1 (target da.tgt) SELECTS_FROM ba.src_b   -> ba.src_b -> da.tgt
      - SqlQuery q2 (target NULL)   SELECTS_FROM ba.src_a   -> dropped (no target)
      - STAR_SOURCE q3 (target ia.stage) FROM ba.src_b      -> ba.src_b -> ia.stage
      - COLUMN_LINEAGE ba.src_a.col1 -> da.tgt.colx         -> ba.src_a -> da.tgt (dup, n++)
      - COLUMN_LINEAGE da.tgt.colx -> da.tgt.coly           -> self-loop, dropped
    """
    b = DuckDBBackend(":memory:")
    b.init_schema()

    tables = [
        ("ba.src_a", "ba", "table"),
        ("ba.src_b", "ba", "table"),
        ("da.tgt", "da", "view"),
        ("da.cte_x", "da", "cte"),
        ("ia.stage", "ia", "temp"),
    ]
    for qualified, db, kind in tables:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?,?,?,?,?)',
            {
                "qualified": qualified,
                "name": qualified.split(".")[-1],
                "catalog": "c",
                "db": db,
                "kind": kind,
            },
        )

    # ba.src_a is catalogued (HAS_COLUMN row).
    b.run_write(
        'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?,?,?)',
        {"src_key": "ba.src_a", "dst_key": "ba.src_a.col1", "source": "ddl"},
    )

    # SqlQuery nodes: q1 writes da.tgt, q2 read-only (no target), q3 writes ia.stage.
    for qid, target in [("q1", "da.tgt"), ("q2", None), ("q3", "ia.stage")]:
        b.run_write(
            'INSERT INTO "SqlQuery" (id, file_path, statement_index, sql, kind, '
            "target_table, parse_failed, confidence, parsing_mode, start_line) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            {
                "id": qid,
                "file_path": "f.sql",
                "statement_index": 0,
                "sql": "...",
                "kind": "INSERT",
                "target_table": target,
                "parse_failed": False,
                "confidence": 1.0,
                "parsing_mode": "sqlglot",
                "start_line": 1,
            },
        )

    selects = [("q1", "ba.src_a"), ("q1", "ba.src_b"), ("q2", "ba.src_a")]
    for qid, src_tbl in selects:
        b.run_write(
            'INSERT INTO "SELECTS_FROM" (src_key, dst_key) VALUES (?,?)',
            {"src_key": qid, "dst_key": src_tbl},
        )

    b.run_write(
        'INSERT INTO "STAR_SOURCE" (src_key, dst_key, qualifier, target_table, confidence) '
        "VALUES (?,?,?,?,?)",
        {
            "src_key": "q3",
            "dst_key": "ba.src_b",
            "qualifier": "",
            "target_table": "ia.stage",
            "confidence": 1.0,
        },
    )

    col_edges = [
        ("ba.src_a.col1", "da.tgt.colx"),  # collapses to ba.src_a -> da.tgt (dup of SELECTS_FROM)
        ("da.tgt.colx", "da.tgt.coly"),  # self-loop after collapse -> dropped
    ]
    for src, dst in col_edges:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, transform, confidence, '
            "query_id, inferred_from_source_name) VALUES (?,?,?,?,?,?)",
            {
                "src_key": src,
                "dst_key": dst,
                "transform": "COPY",
                "confidence": 1.0,
                "query_id": "q1",
                "inferred_from_source_name": False,
            },
        )

    yield b
    b.close()


def _build_with(backend: DuckDBBackend) -> dict:
    def _routed(query, params, db_path=None):
        return backend.run_read(query, params)

    with patch("sqlcg.viz.data.run_read_routed", side_effect=_routed):
        return build_viz_data()


def test_nodes_carry_schema_kind_and_catalogued_flag(seeded_backend) -> None:
    """Each node exposes id/schema/kind/cat with values from SqlTable + HAS_COLUMN."""
    data = _build_with(seeded_backend)
    by_id = {n["id"]: n for n in data["nodes"]}
    assert len(by_id) == 5
    assert by_id["ba.src_a"]["schema"] == "ba"
    assert by_id["da.tgt"]["kind"] == "view"
    assert by_id["ia.stage"]["kind"] == "temp"
    assert by_id["ba.src_a"]["cat"] is True
    assert by_id["ba.src_b"]["cat"] is False


def test_query_anchored_select_produces_source_to_target_link(seeded_backend) -> None:
    """A SqlQuery-anchored SELECTS_FROM yields source->target (not a query node)."""
    data = _build_with(seeded_backend)
    pairs = {(lk["source"], lk["target"]): lk["n"] for lk in data["links"]}
    # No node id is a query id (q1/q2/q3) — only table ids appear.
    node_ids = {n["id"] for n in data["nodes"]}
    for lk in data["links"]:
        assert lk["source"] in node_ids and lk["target"] in node_ids
    assert ("ba.src_b", "da.tgt") in pairs


def test_read_only_select_with_no_target_contributes_no_link(seeded_backend) -> None:
    """q2 (target NULL) SELECTS_FROM ba.src_a emits no link."""
    data = _build_with(seeded_backend)
    # Only edges into da.tgt / ia.stage exist; nothing originates from the
    # targetless q2 beyond what q1 already produced.
    targets = {lk["target"] for lk in data["links"]}
    assert targets <= {"da.tgt", "ia.stage"}


def test_star_source_resolves_to_target_table(seeded_backend) -> None:
    """STAR_SOURCE q3 (target ia.stage) FROM ba.src_b yields ba.src_b->ia.stage."""
    data = _build_with(seeded_backend)
    pairs = {(lk["source"], lk["target"]) for lk in data["links"]}
    assert ("ba.src_b", "ia.stage") in pairs


def test_column_lineage_collapses_to_table_and_dedups_with_count(seeded_backend) -> None:
    """COLUMN_LINEAGE collapses both ends; the ba.src_a->da.tgt dup raises n to 2."""
    data = _build_with(seeded_backend)
    pairs = {(lk["source"], lk["target"]): lk["n"] for lk in data["links"]}
    # SELECTS_FROM(q1, ba.src_a) + COLUMN_LINEAGE(ba.src_a.col1->da.tgt.colx) both
    # collapse to ba.src_a -> da.tgt -> n == 2.
    assert pairs[("ba.src_a", "da.tgt")] == 2


def test_self_loop_dropped(seeded_backend) -> None:
    """The da.tgt.colx->da.tgt.coly column edge collapses to a self-loop and is dropped."""
    data = _build_with(seeded_backend)
    assert all(lk["source"] != lk["target"] for lk in data["links"])
    assert ("da.tgt", "da.tgt") not in {(lk["source"], lk["target"]) for lk in data["links"]}


def test_degree_equals_distinct_neighbor_count_and_adj_is_undirected(seeded_backend) -> None:
    """deg matches the undirected neighbor count; adj mirrors both directions."""
    data = _build_with(seeded_backend)
    by_id = {n["id"]: n for n in data["nodes"]}
    adj = data["adj"]
    # da.tgt has neighbors ba.src_a, ba.src_b -> deg 2.
    assert by_id["da.tgt"]["deg"] == 2
    assert set(adj["da.tgt"]) == {"ba.src_a", "ba.src_b"}
    # ba.src_b reaches da.tgt and ia.stage -> deg 2, undirected.
    assert by_id["ba.src_b"]["deg"] == 2
    assert "ba.src_b" in adj["ia.stage"]
    # da.cte_x has no edges -> deg 0, absent from adj.
    assert by_id["da.cte_x"]["deg"] == 0
    assert "da.cte_x" not in adj


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
