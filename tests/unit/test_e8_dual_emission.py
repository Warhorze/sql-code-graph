"""Unit tests for E8 dual-emission (transitive temp-edge composition).

Dual-emission keeps the structural temp hops (src→temp, temp→sink) AND additively
emits a derived, transform='TEMP_INLINE'-tagged transitive src→sink edge, so consumers
pick the view. See plan/research/e8_dual_emission_feasibility.md and
plan/sprints/e8_dual_emission.md.

Covers:
- (a) all three edges coexist on the alias fixture
- (b) self-reload produces NO TEMP_INLINE self-loop (the §4 self-loop trap)
- (c) the recall metric query WITH the transform filter counts STRUCTURAL only
- gap A: a TEMP_INLINE edge never shadows a real structural edge on (src,dst) collision
- gap B: multi-temp chains compose to a leaf→sink TEMP_INLINE closure edge
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _snow(aliases: dict[str, str] | None = None) -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"), schema_aliases=aliases or {})


def _ansi(aliases: dict[str, str] | None = None) -> AnsiParser:
    return AnsiParser(SchemaResolver(dialect=None), schema_aliases=aliases or {})


def _all_edges(result) -> list:
    edges = []
    for stmt in result.statements:
        edges.extend(stmt.column_lineage)
    return edges


class TestDualEmissionThreeEdgesCoexist:
    """(a) On the alias fixture, src→temp, temp→sink, and src→sink all coexist."""

    def test_dual_emission_three_edges_coexist_on_alias_fixture(self, tmp_path):
        sql = (
            "USE SCHEMA BA_TMP;\n"
            "CREATE OR REPLACE TEMPORARY TABLE tmp_inkoop AS SELECT col FROM da.leaf_src;\n"
            "INSERT INTO ba_tmp.final_tgt SELECT col FROM tmp_inkoop;\n"
        )
        rel = "etl/sql/fact/wtfa_inkoop.sql"
        result = _snow({"ba_tmp": "ba"}).parse_file(tmp_path / rel, sql, rel_path=rel)
        edges = _all_edges(result)

        # Structural hop INTO the temp: da.leaf_src.col -> <ns>::ba.tmp_inkoop.col
        into_temp = [
            e
            for e in edges
            if e.src.table.name == "leaf_src"
            and e.dst.table.role == "temp"
            and e.transform != "TEMP_INLINE"
        ]
        # Structural hop OUT of the temp: <ns>::ba.tmp_inkoop.col -> ba.final_tgt.col
        out_of_temp = [
            e
            for e in edges
            if e.src.table.role == "temp"
            and e.dst.table.name == "final_tgt"
            and e.transform != "TEMP_INLINE"
        ]
        # Derived transitive shortcut: da.leaf_src.col -> ba.final_tgt.col [TEMP_INLINE]
        transitive = [
            e
            for e in edges
            if e.transform == "TEMP_INLINE"
            and e.src.table.name == "leaf_src"
            and e.dst.table.name == "final_tgt"
        ]

        assert into_temp, f"missing structural src→temp hop; edges={_dump(edges)}"
        assert out_of_temp, f"missing structural temp→sink hop; edges={_dump(edges)}"
        assert transitive, f"missing derived TEMP_INLINE src→sink edge; edges={_dump(edges)}"

        # The transitive edge must NOT touch the temp on either endpoint (it is the
        # collapsed shortcut, distinct (src,dst) from both structural hops).
        t = transitive[0]
        assert t.src.table.role != "temp"
        assert t.dst.table.role != "temp"


class TestDualEmissionNoSelfLoop:
    """(b) A temp self-reload produces NO TEMP_INLINE self-loop (§4 self-loop trap)."""

    def test_dual_emission_self_reload_no_temp_inline_self_loop(self, tmp_path):
        sql = (
            "CREATE TEMP TABLE tmp_x AS SELECT col FROM da.t;\n"
            "INSERT INTO da.t SELECT col FROM tmp_x;\n"
        )
        rel = "etl/sql/fact/self_reload.sql"
        result = _ansi().parse_file(tmp_path / rel, sql, rel_path=rel)
        edges = _all_edges(result)

        self_loops = [
            e
            for e in edges
            if e.transform == "TEMP_INLINE"
            and e.src.table.full_id == e.dst.table.full_id
            and e.src.name == e.dst.name
        ]
        assert not self_loops, (
            f"self-reload emitted a spurious TEMP_INLINE self-loop: {_dump(self_loops)}"
        )


class TestDualEmissionMultiTempChain:
    """gap B: leaf→temp1→temp2→sink composes to a leaf→sink TEMP_INLINE closure edge."""

    def test_dual_emission_multi_temp_chain_emits_leaf_to_sink(self, tmp_path):
        sql = (
            "USE SCHEMA da;\n"
            "CREATE TEMP TABLE tmp1 AS SELECT col FROM da.leaf;\n"
            "CREATE TEMP TABLE tmp2 AS SELECT col FROM tmp1;\n"
            "INSERT INTO da.sink SELECT col FROM tmp2;\n"
        )
        rel = "etl/sql/fact/chain.sql"
        result = _snow().parse_file(tmp_path / rel, sql, rel_path=rel)
        edges = _all_edges(result)

        # The fully-composed transitive closure edge: leaf -> sink, neither endpoint temp.
        closure = [
            e
            for e in edges
            if e.transform == "TEMP_INLINE"
            and e.src.table.name == "leaf"
            and e.dst.table.name == "sink"
        ]
        assert closure, (
            f"multi-temp chain did not compose to a leaf→sink TEMP_INLINE closure edge "
            f"(gap B fixpoint); edges={_dump(edges)}"
        )

        # And the structural hops survive untouched.
        struct = [e for e in edges if e.transform != "TEMP_INLINE"]
        assert any(e.dst.table.name == "tmp1" for e in struct)
        assert any(e.src.table.role == "temp" and e.dst.table.name == "tmp2" for e in struct)
        assert any(e.src.table.role == "temp" and e.dst.table.name == "sink" for e in struct)


class TestDualEmissionGapACollisionPrecedence:
    """gap A: a TEMP_INLINE edge must never shadow a real structural edge on (src,dst).

    Exercises the transform-aware dedup with structural precedence in
    indexer.py `_aggregate_buffer` (the (src_key, dst_key) chokepoint).
    """

    def _row(self, src, dst, transform):
        return {
            "src_key": src,
            "dst_key": dst,
            "transform": transform,
            "confidence": 1.0,
            "query_id": "q1",
            "inferred_from_source_name": False,
        }

    def test_structural_edge_wins_over_temp_inline_on_collision(self):
        # A real PASS_THROUGH leaf→sink and a derived TEMP_INLINE leaf→sink sharing the
        # same (src_key, dst_key). Structural must survive — both orderings.
        from sqlcg.indexer.indexer import _dedup_column_lineage_edges

        s, d = "da.leaf.col", "da.sink.col"
        for rows in (
            [self._row(s, d, "PASS_THROUGH"), self._row(s, d, "TEMP_INLINE")],
            [self._row(s, d, "TEMP_INLINE"), self._row(s, d, "PASS_THROUGH")],
        ):
            out = _dedup_column_lineage_edges(rows)
            keyed = [r for r in out if (r["src_key"], r["dst_key"]) == (s, d)]
            assert len(keyed) == 1, f"dedup must collapse to one row, got {keyed}"
            assert keyed[0]["transform"] == "PASS_THROUGH", (
                f"structural edge must win over TEMP_INLINE on collision, got {keyed[0]}"
            )

    def test_temp_inline_survives_when_no_structural_collision(self):
        from sqlcg.indexer.indexer import _dedup_column_lineage_edges

        out = _dedup_column_lineage_edges([self._row("da.leaf.col", "da.sink.col", "TEMP_INLINE")])
        assert len(out) == 1
        assert out[0]["transform"] == "TEMP_INLINE"


class TestDualEmissionMetricFilter:
    """(c) The recall metric query WITH the transform filter counts STRUCTURAL only."""

    def test_resolvable_write_metric_excludes_temp_inline(self):
        import duckdb

        from sqlcg.cli.coverage import (
            _Q_RESOLVABLE_WRITE_COL_EDGES,
            _Q_TRANSITIVE_COL_EDGES,
        )

        con = duckdb.connect(":memory:")
        con.execute(
            'CREATE TABLE "COLUMN_LINEAGE" '
            "(src_key VARCHAR, dst_key VARCHAR, transform VARCHAR, query_id VARCHAR)"
        )
        con.execute('CREATE TABLE "SqlQuery" (id VARCHAR, kind VARCHAR, target_table VARCHAR)')
        con.execute('CREATE TABLE "SELECTS_FROM" (src_key VARCHAR, dst_key VARCHAR)')
        con.execute("INSERT INTO \"SqlQuery\" VALUES ('q1', 'INSERT', 'da.sink')")
        con.execute("INSERT INTO \"SELECTS_FROM\" VALUES ('q1', 'da.leaf')")
        # one structural edge + one derived TEMP_INLINE edge on the same write query
        con.execute(
            'INSERT INTO "COLUMN_LINEAGE" VALUES '
            "('da.leaf.col', 'da.tmp.col', 'PASS_THROUGH', 'q1'),"
            "('da.leaf.col', 'da.sink.col', 'TEMP_INLINE', 'q1')"
        )

        rwce = con.execute(_Q_RESOLVABLE_WRITE_COL_EDGES).fetchone()[0]
        transitive = con.execute(_Q_TRANSITIVE_COL_EDGES).fetchone()[0]

        assert rwce == 1, (
            f"resolvable_write_col_edges must count STRUCTURAL only "
            f"(exclude TEMP_INLINE); got {rwce}"
        )
        assert transitive == 1, f"transitive counter must count TEMP_INLINE only; got {transitive}"


class TestTraceColumnLineageViewSelector:
    """The consumer view-selector (tools.trace_column_lineage `view` param, §6.3).

    - default 'structural' filters OUT TEMP_INLINE rows (the shortcut adds no
      reachability over the structural hops, only clutter).
    - 'source_to_sink' keeps ONLY TEMP_INLINE rows (temps-elided one-hop view).
    """

    def _rows(self):
        # One structural parent (the temp) and one derived TEMP_INLINE parent (the leaf).
        return [
            {
                "id": "da.tmp.col",
                "col_name": "col",
                "table_qualified": "da.tmp",
                "transform": "PASS_THROUGH",
            },
            {
                "id": "da.leaf.col",
                "col_name": "col",
                "table_qualified": "da.leaf",
                "transform": "TEMP_INLINE",
            },
        ]

    def _run(self, view):
        from unittest.mock import MagicMock, patch

        from sqlcg.server.tools import trace_column_lineage

        with patch("sqlcg.server.tools._open_backend") as mock_backend:
            mock_db = MagicMock()
            mock_backend.return_value.__enter__.return_value = mock_db
            mock_db.run_read.side_effect = [
                [{"n": 1}],  # _assert_indexed
                self._rows(),  # trace query for the root col
                [],  # next level (tmp parents)
                [],  # next level (leaf parents)
                [],
            ]
            return trace_column_lineage("da.sink.col", max_depth=1, view=view)

    def test_structural_view_excludes_temp_inline_parent(self):
        result = self._run("structural")
        names = {n.table for n in result.lineage}
        ids = {(n.table, n.name) for n in result.lineage}
        assert ("da.leaf", "col") not in ids, (
            f"structural view must drop the TEMP_INLINE leaf parent; got {ids}"
        )
        # the structural temp parent is kept
        assert any("tmp" in (t or "") for t in names), f"structural temp parent missing; {names}"

    def test_source_to_sink_view_keeps_only_temp_inline(self):
        result = self._run("source_to_sink")
        ids = {(n.table, n.name) for n in result.lineage}
        assert ("da.leaf", "col") in ids, f"source_to_sink must keep TEMP_INLINE leaf; got {ids}"
        assert not any("tmp" in (t or "") for t, _ in ids), (
            f"source_to_sink must drop the structural temp parent; got {ids}"
        )

    def test_invalid_view_raises(self):
        import pytest

        from sqlcg.server.tools import trace_column_lineage

        with pytest.raises(ValueError):
            trace_column_lineage("da.sink.col", view="bogus")


def _dump(edges) -> str:
    return repr(
        [
            (e.src.table.full_id, e.src.name, e.dst.table.full_id, e.dst.name, e.transform)
            for e in edges
        ]
    )
