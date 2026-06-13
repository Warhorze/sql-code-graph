"""Unit tests for the E8 temp-chain key-mismatch fix (v1.25.8).

The E8 class (`no_edges_from_root` / `dynamic_source`) was caused by a KEY-MISMATCH
between the `sources_map` entry (bare key: `"tmp_x"`) and the normalised form that
`exp.expand()` computed from the qualified AST node (`"schema.tmp_x"`) after
`_qualify_bare_tables` ran on the Snowflake USE SCHEMA path.

Fix: also register the schema-qualified key in `sources_map` alongside the bare key
so `exp.expand()` can resolve temp bodies when the USE SCHEMA qualifier is active.

The GRAPH NODE key remains separate: the namespaced `<rel>::<db>.<name>` `role='temp'`
key via `_lineage_node_to_table_ref` is unchanged — the v1.21.1 dual-write footgun
must not be reopened.

Guards plan/sprints/coverage_parse_failures.md §PR-3 (fix/e8-temp-chain, v1.25.8).
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _make_snowflake_parser() -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"))


def _make_ansi_parser() -> AnsiParser:
    return AnsiParser(SchemaResolver(dialect=None))


# ---------------------------------------------------------------------------
# Core reproduction: the exact key-mismatch scenario
# ---------------------------------------------------------------------------


class TestE8TempChainKeyMismatchFix:
    """The USE SCHEMA + CREATE TEMP TABLE chain resolves through the temp body.

    Before the fix: sources_map held bare key 'tmp_x'; exp.expand() normalised
    the qualified AST node (db='da', name='tmp_x') to 'da.tmp_x' — no match →
    dynamic_source → E8.

    After the fix: sources_map also holds 'da.tmp_x' → exp.expand() finds it →
    lineage resolves through the temp body to the leaf source table.

    Guards plan/sprints/coverage_parse_failures.md §PR-3 / Step 3.1.
    """

    def test_temp_chain_with_use_schema_resolves_to_leaf(self, tmp_path):
        """INSERT selecting from a temp created under USE SCHEMA resolves to the leaf.

        Reproduces the live pattern: USE SCHEMA da sets the active schema; a bare
        CREATE TEMP TABLE tmp_inkoop is qualified to da.tmp_inkoop by _qualify_bare_tables;
        a subsequent INSERT ... FROM tmp_inkoop is also qualified to FROM da.tmp_inkoop.
        exp.expand() must find 'da.tmp_inkoop' in sources_map to resolve the body.

        Asserts: at least one COLUMN_LINEAGE edge exists from the leaf source table to
        the final target, resolving through the temp body.  The dynamic_source skip
        count for the column is 0.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.1).
        """
        sql = (
            "USE SCHEMA da;\n"
            "CREATE TEMP TABLE tmp_inkoop AS SELECT col FROM ba.leaf_table;\n"
            "INSERT INTO da.final_target SELECT col FROM tmp_inkoop;\n"
        )
        rel = "etl/sql/fact/wtfa_inkoop.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        # Collect COLUMN_LINEAGE edges from all statements
        all_edges = []
        for stmt in result.statements:
            all_edges.extend(stmt.column_lineage)

        # The INSERT should produce at least one edge that traces to ba.leaf_table
        leaf_edges = [e for e in all_edges if e.src.table.name == "leaf_table"]
        assert leaf_edges, (
            f"Expected COLUMN_LINEAGE edge from ba.leaf_table to final_target, "
            f"but none found. All edge src tables: "
            f"{[e.src.table.full_id for e in all_edges]!r}. "
            f"This indicates the E8 key-mismatch is not fixed: exp.expand() could "
            f"not find 'da.tmp_inkoop' in sources_map and the temp body was not "
            f"inlined, so _lineage_node_to_edges found no leaf → dynamic_source."
        )

        # Verify no dynamic_source skips for the column (errors are on ParsedFile)
        dynamic_source_errors = [e for e in result.errors if "dynamic_source" in e and "col" in e]
        assert not dynamic_source_errors, (
            f"Unexpected dynamic_source skip errors after E8 fix: {dynamic_source_errors!r}"
        )

    def test_temp_chain_qualified_sources_map_key_registered(self, tmp_path):
        """sources_map receives both the bare AND the qualified key after _qualify_bare_tables.

        The bare key 'tmp_inkoop' is required for the non-qualified ANSI path.
        The qualified key 'da.tmp_inkoop' is required for exp.expand() after
        _qualify_bare_tables prefixes the name with the USE SCHEMA-active schema.

        Asserts the fix: both keys exist in sources_map by the time the INSERT runs,
        evidenced by the INSERT statement producing edges (which requires expand to work).

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.1, key-mismatch fix).
        """
        sql = (
            "USE SCHEMA da;\n"
            "CREATE TEMP TABLE tmp_x AS SELECT a FROM ba.src;\n"
            "INSERT INTO da.result_tbl SELECT a FROM tmp_x;\n"
        )
        rel = "etl/sql/fact/test.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        # If the INSERT produces any non-skip COLUMN_LINEAGE edge whose src resolves
        # beyond the temp (i.e. to ba.src), the qualified key lookup succeeded.
        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        assert insert_stmts, "INSERT statement not found in result"

        insert_stmt = insert_stmts[0]
        non_skip_edges = [e for e in insert_stmt.column_lineage if e.src.table.name == "src"]
        edge_srcs = [(e.src.table.full_id, e.dst.table.name) for e in insert_stmt.column_lineage]
        assert non_skip_edges, (
            f"INSERT should resolve through tmp_x to ba.src, but no such edge found. "
            f"Insert edges: {edge_srcs!r}. File errors: {result.errors!r}"
        )


# ---------------------------------------------------------------------------
# Footgun guard: no bare tmp_* kind='table' node created
# (v1.21.1 dual-write regression guard — must not be reopened)
# ---------------------------------------------------------------------------


class TestE8NoBareTableNodeCreated:
    """Temp chain fix must NOT create bare tmp_* kind='table' nodes.

    The v1.21.1 fix required that intermediate temp nodes are emitted as
    `role='temp'` with a namespaced key via `_lineage_node_to_table_ref`.
    A bare `role='table'` node for a temp is the dual-write footgun.

    Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.2).
    Guards fix/temp-namespacing-dual-write (v1.21.1).
    """

    def test_no_bare_tmp_table_role_in_defined_tables(self, tmp_path):
        """The temp target appears in defined_tables as role='temp', NOT role='table'.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.2, footgun guard).
        """
        sql = "USE SCHEMA da;\nCREATE TEMP TABLE tmp_inkoop AS SELECT col FROM ba.leaf;\n"
        rel = "etl/sql/fact/w.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        bare_table_tmps = [
            t for t in result.defined_tables if t.role == "table" and "tmp_" in t.name.lower()
        ]
        assert not bare_table_tmps, (
            f"No bare tmp_* kind='table' node should be created; "
            f"found: {[t.full_id for t in bare_table_tmps]!r}. "
            f"This reopens the v1.21.1 dual-write footgun (fix/temp-namespacing-dual-write). "
            f"Temp tables must carry role='temp' with namespaced full_id."
        )

    def test_temp_target_is_namespaced_not_bare(self, tmp_path):
        """The temp's defined_table entry is namespaced (contains '::'), not bare.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.2).
        """
        sql = "USE SCHEMA da;\nCREATE TEMP TABLE tmp_x AS SELECT a FROM ba.src;\n"
        rel = "etl/sql/fact/ns_test.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        assert result.defined_tables, "Expected defined_tables from CREATE TEMP TABLE"
        temp_targets = [t for t in result.defined_tables if "tmp_x" in t.name.lower()]
        assert temp_targets, "No tmp_x in defined_tables"

        for t in temp_targets:
            assert "::" in t.full_id, (
                f"Temp target must be namespaced (contain '::'): got {t.full_id!r}. "
                f"Bare temp nodes re-open the v1.21.1 dual-write footgun."
            )
            assert t.role == "temp", (
                f"Temp target must have role='temp': got role={t.role!r} for {t.full_id!r}"
            )

    def test_column_lineage_src_is_namespaced_temp_not_bare_table(self, tmp_path):
        """COLUMN_LINEAGE edges whose src is the temp carry the namespaced key.

        When the temp body is NOT expanded (e.g. the temp is an intermediate node
        in the graph), the edge src should use the namespaced role='temp' key —
        NOT a bare kind='table' node.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (Step 3.2).
        """
        sql = (
            "USE SCHEMA da;\n"
            "CREATE TEMP TABLE tmp_x AS SELECT a FROM ba.src;\n"
            "INSERT INTO da.dst SELECT a FROM tmp_x;\n"
        )
        rel = "etl/sql/fact/cl_test.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        all_edges = []
        for stmt in result.statements:
            all_edges.extend(stmt.column_lineage)

        # Any edge touching the temp must have a namespaced src (not bare 'da.tmp_x')
        bare_tmp_edges = [
            e for e in all_edges if e.src.table.name == "tmp_x" and e.src.table.role == "table"
        ]
        assert not bare_tmp_edges, (
            f"Found edges with bare kind='table' src for tmp_x — "
            f"this is the v1.21.1 dual-write regression: "
            f"{[(e.src.table.full_id, e.dst.name) for e in bare_tmp_edges]!r}"
        )


# ---------------------------------------------------------------------------
# Mixed scenario: star column still skips (PR-3 does not fix star)
# ---------------------------------------------------------------------------


class TestE8StarColumnStillSkips:
    """PR-3 fixes dynamic_source temp chains but does NOT fix star columns.

    A file with both a temp chain (fixed) and a SELECT * expansion (not fixed)
    should see: the temp column resolves, the star column still emits col_lineage_skip:star.

    Guards plan/sprints/coverage_parse_failures.md §PR-3 Non-Goals (star/merge_branch residual).
    """

    def test_temp_column_resolves_while_star_still_skips(self, tmp_path):
        """In a file with both temp-chain and star columns, the non-star column resolves.

        The temp column gets COLUMN_LINEAGE; the star column emits col_lineage_skip:star.
        This documents that PR-3 recovers dynamic_source only — star is deferred.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 Non-Goals.
        """
        # File: CREATE TEMP TABLE with one real column + INSERT with both a
        # real column (resolved via temp chain) and a star column (deferred).
        # We can't literally mix "SELECT a, SELECT *" in the same expression, so
        # we use two separate INSERT statements: one with the named column (should
        # resolve) and one with star (should skip).
        sql = (
            "USE SCHEMA da;\n"
            "CREATE TEMP TABLE tmp_x AS SELECT col FROM ba.leaf;\n"
            # Named column via temp chain — should resolve
            "INSERT INTO da.target1 SELECT col FROM tmp_x;\n"
            # Star expansion — should skip with star marker (PR-3 does not fix this)
            "INSERT INTO da.target2 SELECT * FROM ba.other;\n"
        )
        rel = "etl/sql/fact/mixed.sql"
        parser = _make_snowflake_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        # The first INSERT (tmp_x chain) should resolve to a leaf edge
        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        assert len(insert_stmts) >= 1

        target1_stmt = next(
            (s for s in insert_stmts if s.target and "target1" in s.target.name), None
        )
        if target1_stmt is not None:
            leaf_edges = [e for e in target1_stmt.column_lineage if e.src.table.name == "leaf"]
            t1_edges = [(e.src.table.full_id, e.dst.name) for e in target1_stmt.column_lineage]
            assert leaf_edges, (
                f"Expected edge from ba.leaf to da.target1 via tmp_x; "
                f"edges: {t1_edges!r}; file errors: {result.errors!r}"
            )

        # Star column: any INSERT with SELECT * should emit the star skip marker
        # (errors are on ParsedFile, not on individual QueryNode)
        star_skips = [e for e in result.errors if "col_lineage_skip:star" in e]
        assert star_skips, (
            f"Expected at least one col_lineage_skip:star error (SELECT * not fixed by PR-3); "
            f"all file errors: {result.errors!r}"
        )


# ---------------------------------------------------------------------------
# ANSI path: bare temp (no db) still works correctly (no regression)
# ---------------------------------------------------------------------------


class TestE8AnsiPathNoRegression:
    """The ANSI bare-temp path (db=None) is not regressed by the E8 fix.

    The fix only adds a qualified key when target_db is truthy.  When db=None
    (ANSI path, no USE SCHEMA), only the bare key is registered — same as before.

    Guards plan/sprints/coverage_parse_failures.md §PR-3 (no regression).
    """

    def test_ansi_bare_temp_chain_still_resolves(self, tmp_path):
        """ANSI CREATE TEMP TABLE (no db) chain still resolves column lineage.

        Guards plan/sprints/coverage_parse_failures.md §PR-3 (ANSI no-regression).
        """
        sql = (
            "CREATE TEMPORARY TABLE tmp_x AS SELECT a FROM ba.src;\n"
            "INSERT INTO ba.dst SELECT a FROM tmp_x;\n"
        )
        rel = "etl/ansi.sql"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        assert insert_stmts, "INSERT not found"

        # With ANSI (no USE SCHEMA), tmp_x stays bare; sources_map has 'tmp_x' → body.
        # exp.expand() sees bare 'tmp_x' in AST, matches bare 'tmp_x' in sources_map → OK.
        # Check that no dynamic_source error appears for column 'a' (errors are on ParsedFile).
        dynamic_errors = [e for e in result.errors if "dynamic_source" in e]
        assert not dynamic_errors, (
            f"ANSI bare-temp chain produced dynamic_source errors (regression): {dynamic_errors!r}."
        )
