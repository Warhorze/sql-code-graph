"""PR-6 — static-literal EXECUTE IMMEDIATE unwrap (Phase 1) + #142 temp composition (Phase 2).

Phase 1 surfaces a static-literal ``EXECUTE IMMEDIATE '<DDL>'`` (inline) or
``var := '<DDL>'; EXECUTE IMMEDIATE (:var)`` (variable form) inner statement into the
file's ``out.statements`` via the normal ``_parse_statement`` path, inheriting the
file's ``USE SCHEMA`` session context.

Phase 2 reuses the #142 ``_emit_transitive_temp_edges`` engine: because the inner
statements land in ``out.statements`` BEFORE the composition pass, an inner
``CREATE TEMP TABLE`` is picked up automatically and the transitive ``source→sink``
edge is emitted with ``transform='TEMP_INLINE'`` while the temp node stays visible.

Asserts observable edges/nodes, never "no exception raised".
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _snow(aliases: dict[str, str] | None = None) -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"), schema_aliases=aliases or {})


def _all_edges(result) -> list:
    edges: list = []
    for stmt in result.statements:
        edges.extend(stmt.column_lineage)
    return edges


# WTFA shape: DECLARE/BEGIN scripting block holding a static-literal CREATE OR REPLACE
# DYNAMIC TABLE in a STRING var, with a `|| $WAREHOUSENAME ||` concatenation in the
# WAREHOUSE clause ONLY (the AS WITH … SELECT body is fully static).
_WTFA_SHAPE = """--liquibase formatted sql
USE SCHEMA BA;

SET WAREHOUSENAME = (SELECT CURRENT_WAREHOUSE());

DECLARE
   ddl_statement STRING;
BEGIN
   ddl_statement := 'CREATE OR REPLACE DYNAMIC TABLE BA.WTFA_ARTIKEL_FORMULE_DAGOMZET
                     ( DN_DATUM, MA_OMZET )
                      TARGET_LAG = \\'1 hours\\' refresh_mode = AUTO
                      INITIALIZE = ON_CREATE
                      WAREHOUSE = '|| $WAREHOUSENAME ||
                     ' AS
                     WITH datum AS (SELECT min(dn_datum) startdatum_l5y
                                      from ba.wtda_datum
                                     where da_jaren_geleden_kalender = 5)
                     , artikel_verkoop_bouwmarkt AS
                     (SELECT dn_datum, SUM(ma_bedrag_ex) AS ma_omzet
                        FROM ba.wtfe_verkoopinfo
                        JOIN datum d ON DN_DATUM >= startdatum_l5y
                       GROUP BY ALL)
                     SELECT * FROM artikel_verkoop_bouwmarkt ORDER BY 1';

    EXECUTE IMMEDIATE (:ddl_statement);
    RETURN ddl_statement;
END;
"""


class TestPhase1WtfaUnwrap:
    """Phase 1 — the static-literal CREATE DYNAMIC TABLE is recovered with both edges."""

    def test_wtfa_inner_create_produces_sqlquery_and_both_edges(self, tmp_path):
        rel = "ddl/changelogs/BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql"
        result = _snow().parse_file(tmp_path / "wtfa.sql", _WTFA_SHAPE, rel_path=rel)

        # A SqlQuery row exists for the inner CREATE.
        creates = [s for s in result.statements if s.kind == "CREATE_TABLE"]
        assert len(creates) == 1, (
            f"expected 1 inner CREATE, got {[s.kind for s in result.statements]}"
        )
        create = creates[0]

        # Target resolves in schema BA (not unqualified) via the session-context reuse.
        assert create.target is not None
        assert create.target.db == "ba"
        assert create.target.name == "wtfa_artikel_formule_dagomzet"

        # SELECTS_FROM edges (source table refs) into BOTH base tables, both in schema BA.
        source_ids = {(s.db, s.name) for s in create.sources}
        assert ("ba", "wtfe_verkoopinfo") in source_ids, source_ids
        assert ("ba", "wtda_datum") in source_ids, source_ids

    def test_warehouse_concat_yields_no_spurious_source(self, tmp_path):
        """The `|| $WAREHOUSENAME ||` WAREHOUSE concatenation produces no source edge."""
        result = _snow().parse_file(tmp_path / "wtfa.sql", _WTFA_SHAPE, rel_path="wtfa.sql")
        all_source_names = {s.name for st in result.statements for s in st.sources}
        # Only the two base tables (and CTE alias 'datum' may appear as a derived ref but
        # never WAREHOUSENAME / the placeholder).
        assert "warehousename" not in all_source_names
        assert "__sqlcg_dynamic_runtime__" not in {n.lower() for n in all_source_names}


class TestPhase1Negative:
    """Negative — concatenated / bind-variable EXECUTE IMMEDIATE is skipped, not mis-extracted."""

    def test_concatenated_body_yields_no_nodes(self, tmp_path):
        """MSSPR_COMPARE_DATA shape: `... AS SELECT * FROM (' || :sql_source || ') ...`."""
        sql = (
            "USE SCHEMA BA;\n"
            "BEGIN\n"
            "  EXECUTE IMMEDIATE 'CREATE OR REPLACE TEMP TABLE BA_TMP._cmp_src AS "
            "SELECT * FROM (' || :sql_source || ') LIMIT 0';\n"
            "END;\n"
        )
        result = _snow().parse_file(tmp_path / "cmp.sql", sql, rel_path="cmp.sql")
        # No CREATE recovered — the concatenation is INSIDE the AS body → non-recoverable.
        assert [s for s in result.statements if s.kind == "CREATE_TABLE"] == []

    def test_bind_variable_only_yields_no_nodes(self, tmp_path):
        """`EXECUTE IMMEDIATE :sql` with no resolvable literal assignment is skipped."""
        sql = "USE SCHEMA BA;\nBEGIN\n  EXECUTE IMMEDIATE :sql;\nEND;\n"
        result = _snow().parse_file(tmp_path / "ext.sql", sql, rel_path="ext.sql")
        assert [s for s in result.statements if s.kind == "CREATE_TABLE"] == []


class TestPhase2TempComposition:
    """Phase 2 — a static-literal EXECUTE IMMEDIATE of a TEMP CREATE composes a TEMP_INLINE edge."""

    def test_inline_temp_inside_dynamic_sql_composes_transitive_edge(self, tmp_path):
        # Synthetic fixture: a static-literal EXECUTE IMMEDIATE creating a TEMP table,
        # then a sink reading that temp. No such case exists in the live corpus today.
        sql = (
            "USE SCHEMA BA;\n"
            "EXECUTE IMMEDIATE 'CREATE OR REPLACE TEMPORARY TABLE ba.tmp_stage "
            "AS SELECT col FROM ba.leaf_src';\n"
            "INSERT INTO ba.final_sink SELECT col FROM ba.tmp_stage;\n"
        )
        rel = "etl/sql/dyn/tmp_stage.sql"
        result = _snow().parse_file(tmp_path / "tmp_stage.sql", sql, rel_path=rel)
        edges = _all_edges(result)

        # Structural hop OUT of the temp: ba.tmp_stage.col -> ba.final_sink.col
        out_of_temp = [
            e
            for e in edges
            if e.src.table.role == "temp"
            and e.dst.table.name == "final_sink"
            and e.transform != "TEMP_INLINE"
        ]
        assert out_of_temp, (
            "missing structural temp→sink hop (Phase 1 did not surface the inner temp)"
        )

        # Transitive composed edge: ba.leaf_src.col -> ba.final_sink.col tagged TEMP_INLINE.
        transitive = [
            e
            for e in edges
            if e.transform == "TEMP_INLINE"
            and e.src.table.name == "leaf_src"
            and e.dst.table.name == "final_sink"
        ]
        assert transitive, (
            "missing transitive TEMP_INLINE leaf_src→final_sink edge (Phase 2 not wired)"
        )

        # The intermediate temp node is NOT deleted — structural hops to/from it survive.
        temp_dsts = [
            e for e in edges if e.dst.table.role == "temp" and e.dst.table.name == "tmp_stage"
        ]
        assert temp_dsts, "intermediate temp node was removed — must stay visible"
