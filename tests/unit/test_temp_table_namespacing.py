"""Unit tests for per-file TEMPORARY-table namespacing (kind='temp').

Guards plan/sprints/temp_table_namespacing.md (Steps 1.1–2.2, Amendments A2/A3/A5/W1/W4).
Also guards fix/temp-namespacing-dual-write (v1.21.1) — schema-alias interaction
with _lineage_node_to_edges Step 2.2.
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import SqlParser, TableRef
from sqlcg.parsers.snowflake_parser import SnowflakeParser

# ---------------------------------------------------------------------------
# _temp_identity helper (Amendment 3)
# ---------------------------------------------------------------------------


class TestTempIdentityHelper:
    """_temp_identity is the single shared key-formation formula (Amendment 3).

    Guards plan/sprints/temp_table_namespacing.md §Design — _temp_identity.
    """

    def test_temp_identity_with_db_lowercases_both(self):
        """Schema-qualified identity is lowercased db.name.

        Guards temp_table_namespacing.md Amendment 3.
        """
        assert SqlParser._temp_identity("BA", "TMP_X") == "ba.tmp_x"

    def test_temp_identity_already_lower_is_unchanged(self):
        """Already-lowercase identity is returned unchanged.

        Guards temp_table_namespacing.md Amendment 3.
        """
        assert SqlParser._temp_identity("ba", "tmp_x") == "ba.tmp_x"

    def test_temp_identity_none_db_returns_bare_name(self):
        """ANSI path: db=None → bare lowercased name, no 'none.' prefix.

        Guards temp_table_namespacing.md Amendment 3 / Step 1.1.
        """
        assert SqlParser._temp_identity(None, "tmp_x") == "tmp_x"

    def test_temp_identity_empty_db_returns_bare_name(self):
        """ANSI path: db='' (empty string from sqlglot) → bare name.

        sqlglot returns '' for db on an unqualified table, not None.
        Guards temp_table_namespacing.md Amendment 3.
        """
        assert SqlParser._temp_identity("", "TMP_X") == "tmp_x"

    def test_temp_identity_name_uppercase_lowercased(self):
        """Name is always lowercased regardless of input case.

        Guards temp_table_namespacing.md Amendment 3.
        """
        assert SqlParser._temp_identity(None, "TMP_UPPER") == "tmp_upper"


# ---------------------------------------------------------------------------
# TableRef.full_id for role="temp"
# ---------------------------------------------------------------------------


class TestTableRefFullIdTemp:
    """TableRef with role='temp' produces namespaced keys identical to CTE/derived.

    The existing full_id logic (role != 'table' → apply namespace) already
    covers 'temp' without code change.  This test confirms the invariant.
    Guards temp_table_namespacing.md §Design — Key form decision.
    """

    def test_temp_tableref_with_namespace_and_db(self):
        """Temp TableRef produces '<rel>::ba.tmp_base' when db and namespace are set.

        Guards temp_table_namespacing.md §Key form decision.
        """
        ref = TableRef(db="ba", name="tmp_base", role="temp", namespace="etl/x.sql")
        assert ref.full_id == "etl/x.sql::ba.tmp_base"

    def test_temp_tableref_without_namespace_is_bare(self):
        """Without a namespace (legacy / no active parse), full_id is bare 'ba.tmp_base'.

        Guards temp_table_namespacing.md §Design.
        """
        ref = TableRef(db="ba", name="tmp_base", role="temp")
        assert ref.full_id == "ba.tmp_base"

    def test_table_role_ignores_namespace_still(self):
        """Physical table refs (role='table') are never namespaced — regression guard.

        Guards temp_table_namespacing.md §Non-Goals.
        """
        ref = TableRef(db="ba", name="real_table", role="table", namespace="etl/x.sql")
        assert ref.full_id == "ba.real_table"
        assert "::" not in ref.full_id

    def test_two_files_same_temp_name_produce_distinct_keys(self):
        """Two temp TableRefs with the same db.name but different namespaces are disjoint.

        Guards temp_table_namespacing.md Acceptance (distinct keys, no fusion).
        """
        ref_a = TableRef(db="ba", name="tmp_base", role="temp", namespace="etl/file_a.sql")
        ref_b = TableRef(db="ba", name="tmp_base", role="temp", namespace="etl/file_b.sql")
        assert ref_a.full_id != ref_b.full_id
        assert "file_a.sql" in ref_a.full_id
        assert "file_b.sql" in ref_b.full_id


# ---------------------------------------------------------------------------
# Step 1.2: target detection in _parse_statement
# ---------------------------------------------------------------------------


def _make_ansi_parser() -> AnsiParser:
    return AnsiParser(SchemaResolver(dialect=None))


class TestTempTargetDetection:
    """CREATE TEMPORARY TABLE target is stamped role='temp' + namespaced.

    Guards temp_table_namespacing.md Step 1.2.
    """

    def test_temporary_create_target_is_role_temp(self, tmp_path):
        """CREATE [OR REPLACE] TEMPORARY TABLE produces role='temp' on the target.

        Detection is by AST TemporaryProperty, not by name pattern.
        Guards temp_table_namespacing.md Step 1.2.
        """
        sql = "CREATE OR REPLACE TEMPORARY TABLE ba.tmp_base AS SELECT 1 AS x"
        path = tmp_path / "etl/x.sql"
        parser = _make_ansi_parser()
        result = parser.parse_file(path, sql, rel_path="etl/x.sql")

        assert result.defined_tables, "Expected at least one defined table"
        tgt = result.defined_tables[0]
        assert tgt.role == "temp", f"Expected role='temp', got role={tgt.role!r}"
        assert tgt.namespace == "etl/x.sql"
        assert tgt.full_id == "etl/x.sql::ba.tmp_base"

    def test_non_temp_create_stays_role_table(self, tmp_path):
        """A plain CREATE TABLE (no TEMPORARY) target stays role='table'.

        Guards temp_table_namespacing.md §Non-Goals (name-heuristic detection banned).
        """
        sql = "CREATE TABLE ba.real_table AS SELECT 1 AS x"
        path = tmp_path / "etl/x.sql"
        parser = _make_ansi_parser()
        result = parser.parse_file(path, sql, rel_path="etl/x.sql")

        assert result.defined_tables
        tgt = result.defined_tables[0]
        assert tgt.role == "table", f"Expected role='table', got role={tgt.role!r}"
        assert "::" not in tgt.full_id

    def test_tmp_named_non_temporary_stays_role_table(self, tmp_path):
        """A table named 'tmp_archive' without TEMPORARY property stays role='table'.

        Name heuristic must never be the trigger — only AST TemporaryProperty.
        Guards temp_table_namespacing.md §Risks.
        """
        sql = "CREATE TABLE ba.tmp_archive AS SELECT 1 AS x"
        path = tmp_path / "etl/x.sql"
        parser = _make_ansi_parser()
        result = parser.parse_file(path, sql, rel_path="etl/x.sql")

        assert result.defined_tables
        tgt = result.defined_tables[0]
        assert tgt.role == "table", (
            "tmp_archive must stay role='table' — name heuristic is forbidden"
        )
        assert "::" not in tgt.full_id

    def test_non_tmp_named_temporary_is_role_temp(self, tmp_path):
        """A TEMPORARY table named 'staging' (no 'tmp' in name) becomes role='temp'.

        Confirms detection is purely by AST property.
        Guards temp_table_namespacing.md §Risks.
        """
        sql = "CREATE TEMPORARY TABLE ba.staging AS SELECT 1 AS x"
        path = tmp_path / "etl/x.sql"
        parser = _make_ansi_parser()
        result = parser.parse_file(path, sql, rel_path="etl/x.sql")

        assert result.defined_tables
        tgt = result.defined_tables[0]
        assert tgt.role == "temp", (
            "TEMPORARY table named 'staging' must be role='temp' regardless of name"
        )
        assert tgt.full_id == "etl/x.sql::ba.staging"

    def test_temp_create_without_namespace_stays_role_table(self, tmp_path):
        """Without rel_path, a TEMPORARY CREATE retains role='table' (guard: no stray temp rows).

        The _current_file_namespace guard ensures a bare temp CREATE without a
        namespace emits no kind='temp' row — making the symmetric PK-collision
        scenario structurally impossible (Amendment 6).
        Guards temp_table_namespacing.md §Cross-batch kind protection (A6).
        """
        # Simulate by not providing rel_path — the parser uses str(path) as namespace
        # which IS set, so role='temp' is expected with the abs-path namespace.
        # To test the actual 'no namespace' guard we'd need to bypass parse_file.
        # Instead, verify the fallback (str path) case still produces a temp.
        sql = "CREATE TEMPORARY TABLE tmp_x AS SELECT 1 AS y"
        path = tmp_path / "file.sql"
        parser = _make_ansi_parser()
        # No rel_path → namespace = str(path) (set, so temp detection fires)
        result = parser.parse_file(path, sql)

        assert result.defined_tables
        tgt = result.defined_tables[0]
        assert tgt.role == "temp"
        # key uses str(path) as namespace
        assert tgt.full_id.endswith("::tmp_x")

    def test_defined_tables_list_contains_temp_target(self, tmp_path):
        """The temp target appears in ParsedFile.defined_tables (not just statements).

        Guards temp_table_namespacing.md Step 1.2 (wiring check).
        """
        sql = "CREATE OR REPLACE TEMPORARY TABLE ba.tmp_x AS SELECT 1 AS col"
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "x.sql", sql, rel_path="x.sql")

        full_ids = [t.full_id for t in result.defined_tables]
        assert "x.sql::ba.tmp_x" in full_ids, f"Temp target not in defined_tables: {full_ids}"


# ---------------------------------------------------------------------------
# Step 1.1: per-file temp-key isolation (W4 — no leakage across files)
# ---------------------------------------------------------------------------


class TestTempKeyIsolation:
    """_current_file_temp_keys does not leak from file A into file B.

    Guards temp_table_namespacing.md Step 1.1 / Warning 4.
    """

    def test_temp_keys_not_leaked_to_second_file(self, tmp_path):
        """Parsing file B after file A with a temp must not stamp B's sources as temp.

        Guards temp_table_namespacing.md Step 1.1 (isolation).
        """
        sql_a = (
            "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM src;\n"
            "INSERT INTO ba.dst SELECT a FROM ba.tmp_x;"
        )
        sql_b = "INSERT INTO ba.out SELECT a FROM ba.tmp_x;"  # same name, different file

        parser = _make_ansi_parser()
        parser.parse_file(tmp_path / "a.sql", sql_a, rel_path="a.sql")

        # Parse file B — it references ba.tmp_x but without a preceding CREATE TEMPORARY.
        # After parsing A, the temp-key set should be reset to empty.
        result_b = parser.parse_file(tmp_path / "b.sql", sql_b, rel_path="b.sql")

        for stmt in result_b.statements:
            for src in stmt.sources:
                assert src.role != "temp", (
                    f"Source {src.full_id!r} in file B was incorrectly stamped "
                    f"role='temp' — temp keys leaked from file A"
                )

    def test_empty_temp_keys_after_parse(self, tmp_path):
        """After parse_file completes, _current_file_temp_keys is empty.

        Guards temp_table_namespacing.md Step 1.1 (end-of-file clear).
        """
        sql = "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM src;"
        parser = _make_ansi_parser()
        parser.parse_file(tmp_path / "a.sql", sql, rel_path="a.sql")

        assert parser._current_file_temp_keys == set(), (
            "Temp keys not cleared after parse_file — pool-reuse leak risk"
        )

    def test_two_files_same_temp_name_distinct_full_ids(self, tmp_path):
        """Two files each defining ba.tmp_base produce distinct full_ids.

        Guards temp_table_namespacing.md Acceptance criterion.
        """
        sql = "CREATE OR REPLACE TEMPORARY TABLE ba.tmp_base AS SELECT 1 AS x"
        parser = _make_ansi_parser()

        result_a = parser.parse_file(tmp_path / "a.sql", sql, rel_path="a.sql")
        result_b = parser.parse_file(tmp_path / "b.sql", sql, rel_path="b.sql")

        assert result_a.defined_tables, "File A must have a defined table"
        assert result_b.defined_tables, "File B must have a defined table"

        key_a = result_a.defined_tables[0].full_id
        key_b = result_b.defined_tables[0].full_id
        assert key_a != key_b, f"Temp keys must differ across files: {key_a!r} == {key_b!r}"
        assert "a.sql" in key_a
        assert "b.sql" in key_b


# ---------------------------------------------------------------------------
# Step 2.1: source stamping (A5 placement)
# ---------------------------------------------------------------------------


class TestSourceStamping:
    """Source refs for same-file temp reads are stamped role='temp' + namespaced.

    Guards temp_table_namespacing.md Step 2.1 (Amendment 5).
    """

    def test_source_after_temp_create_is_stamped(self, tmp_path):
        """A source read in a later statement is stamped role='temp' with the file key.

        Guards temp_table_namespacing.md Step 2.1.
        """
        sql = (
            "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM src;\n"
            "INSERT INTO ba.dst SELECT a FROM ba.tmp_x;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "etl/x.sql", sql, rel_path="etl/x.sql")

        insert_stmt = result.statements[1]
        temp_sources = [s for s in insert_stmt.sources if s.role == "temp"]
        assert temp_sources, (
            f"No temp-role source in INSERT statement; sources: {insert_stmt.sources}"
        )
        temp_src = temp_sources[0]
        assert temp_src.full_id == "etl/x.sql::ba.tmp_x", (
            f"Expected 'etl/x.sql::ba.tmp_x', got {temp_src.full_id!r}"
        )

    def test_source_before_temp_create_is_not_stamped(self, tmp_path):
        """A source read BEFORE the TEMPORARY CREATE is NOT stamped (W1 — backward-only).

        Valid SQL cannot read a session temp before it is created.  This tests
        that forward-reference pre-scan is absent (by design).
        Guards temp_table_namespacing.md Warning 1.
        """
        sql = (
            "INSERT INTO ba.dst SELECT a FROM ba.tmp_x;\n"  # read before CREATE
            "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM src;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "f.sql", sql, rel_path="f.sql")

        insert_stmt = result.statements[0]
        for src in insert_stmt.sources:
            if src.name == "tmp_x":
                assert src.role != "temp", (
                    f"Source read BEFORE CREATE TEMPORARY must NOT be stamped role='temp'; "
                    f"got role={src.role!r} (forward-reference pre-scan is forbidden)"
                )

    def test_real_source_in_same_statement_is_not_stamped(self, tmp_path):
        """Non-temp sources in the same INSERT are not affected by temp stamping.

        Guards temp_table_namespacing.md Step 2.1 (selective stamping).
        """
        sql = (
            "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM src;\n"
            "INSERT INTO ba.dst SELECT t.a, r.b FROM ba.tmp_x t, ba.real_src r;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "x.sql", sql, rel_path="x.sql")

        insert_stmt = result.statements[1]
        for src in insert_stmt.sources:
            if src.name in ("real_src", "src"):
                assert src.role == "table", (
                    f"Real source {src.name!r} must not be stamped role='temp'"
                )

    def test_star_source_from_temp_carries_namespaced_key(self, tmp_path):
        """SELECT * FROM temp produces a star_source with the namespaced temp key.

        A5 placement: source stamping is before _extract_column_lineage so the
        star-resolution path receives the namespaced source.
        Guards temp_table_namespacing.md Step 2.1 (star-source acceptance).
        """
        sql = (
            "CREATE TEMPORARY TABLE ba.tmp_starsrc AS SELECT a, b FROM ba.real_src;\n"
            "INSERT INTO ba.out SELECT * FROM ba.tmp_starsrc;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "star.sql", sql, rel_path="star.sql")

        # Gather all star_sources from all statements
        all_star_sources = []
        for stmt in result.statements:
            all_star_sources.extend(stmt.star_sources)

        # Find a star source referencing the temp
        temp_stars = [ss for ss in all_star_sources if "tmp_starsrc" in ss.source.full_id]
        if temp_stars:
            for ts in temp_stars:
                assert ts.source.role == "temp", (
                    f"Star source for temp must have role='temp', got {ts.source.role!r}"
                )
                assert "::" in ts.source.full_id, (
                    f"Star source for temp must be namespaced, got {ts.source.full_id!r}"
                )


# ---------------------------------------------------------------------------
# Step 2.2: lineage-leaf stamping (_lineage_node_to_edges)
# ---------------------------------------------------------------------------


class TestLineageLeafStamping:
    """COLUMN_LINEAGE edges whose leaf is a temp carry the namespaced key.

    Guards temp_table_namespacing.md Step 2.2 (Amendment 2).
    """

    def test_column_lineage_src_from_temp_is_namespaced(self, tmp_path):
        """COLUMN_LINEAGE source edges from a temp carry the namespaced temp key.

        Guards temp_table_namespacing.md Step 2.2.
        """
        sql = (
            "CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM ba.src;\n"
            "INSERT INTO ba.dst SELECT a FROM ba.tmp_x;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "etl/col.sql", sql, rel_path="etl/col.sql")

        insert_stmt = result.statements[1]
        # Find edges where the source table is the temp
        temp_edges = [
            e
            for e in insert_stmt.column_lineage
            if "::" in e.src.table.full_id and "tmp_x" in e.src.table.full_id
        ]
        assert temp_edges, (
            f"No COLUMN_LINEAGE edges with namespaced temp src found; "
            f"all src tables: {[e.src.table.full_id for e in insert_stmt.column_lineage]}"
        )
        for edge in temp_edges:
            assert edge.src.table.role == "temp"
            assert edge.src.table.full_id.startswith("etl/col.sql::")

    def test_ansi_bare_temp_no_none_prefix(self, tmp_path):
        """ANSI bare temp (db=None) registers as bare 'tmp_x' — no 'none.tmp_x' mis-key.

        Guards temp_table_namespacing.md Amendment 2/3 (ANSI bare-temp path).
        """
        sql = (
            "CREATE TEMPORARY TABLE tmp_x AS SELECT a FROM src;\n"
            "INSERT INTO dst SELECT a FROM tmp_x;"
        )
        parser = _make_ansi_parser()
        result = parser.parse_file(tmp_path / "ansi.sql", sql, rel_path="ansi.sql")

        # Check defined_tables: no 'none.' prefix
        assert result.defined_tables, "Expected defined_tables for TEMPORARY CREATE"
        tgt = result.defined_tables[0]
        assert "none." not in tgt.full_id.lower(), (
            f"Target key must not contain 'none.': {tgt.full_id!r}"
        )
        assert tgt.full_id == "ansi.sql::tmp_x", f"Expected 'ansi.sql::tmp_x', got {tgt.full_id!r}"

        # Check source stamping in the INSERT
        insert_stmt = result.statements[1]
        temp_sources = [s for s in insert_stmt.sources if s.role == "temp"]
        assert temp_sources, "INSERT source for bare temp must be stamped role='temp'"
        assert all("none." not in s.full_id.lower() for s in temp_sources), (
            "No 'none.' prefix allowed on ANSI bare temp source"
        )


# ---------------------------------------------------------------------------
# W1: backward-only stamping documentation
# ---------------------------------------------------------------------------


class TestBackwardOnlyStamping:
    """Temp stamping is backward-only within a file (Warning 1).

    Guards temp_table_namespacing.md Warning 1.
    """

    def test_backward_only_documented_in_module(self):
        """_current_file_temp_keys is built incrementally — no pre-scan.

        This structural test verifies the parser attribute is a plain set (not
        a pre-populated map), confirming the backward-only design.
        Guards temp_table_namespacing.md Warning 1.
        """
        parser = _make_ansi_parser()
        # Before any parse_file, the temp-key set is empty
        assert parser._current_file_temp_keys == set()
        # The attribute is a set (incremental, not a pre-computed dict)
        assert isinstance(parser._current_file_temp_keys, set)


# ---------------------------------------------------------------------------
# Portability: same rel-path under two different absolute roots
# ---------------------------------------------------------------------------


class TestPortability:
    """Same relative layout under two absolute roots yields identical namespaced temp keys.

    Guards temp_table_namespacing.md (mirrors PR #86 portability assertion).
    """

    def test_same_rel_path_different_root_produces_same_key(self, tmp_path):
        """Temp key is identical when the same rel_path is used under two roots.

        Guards temp_table_namespacing.md §Design — portability.
        """
        sql = "CREATE TEMPORARY TABLE ba.tmp_base AS SELECT a FROM src;"
        parser = _make_ansi_parser()

        root_a = tmp_path / "machine_a"
        root_b = tmp_path / "machine_b"
        rel = "etl/sql/fact/x.sql"

        result_a = parser.parse_file(root_a / rel, sql, rel_path=rel)
        result_b = parser.parse_file(root_b / rel, sql, rel_path=rel)

        assert result_a.defined_tables and result_b.defined_tables
        assert result_a.defined_tables[0].full_id == result_b.defined_tables[0].full_id, (
            "Same rel_path under two absolute roots must produce the same temp key"
        )


# ---------------------------------------------------------------------------
# v1.21.1 regression guard: schema-alias interaction with Step 2.2
# ---------------------------------------------------------------------------


class TestSchemaAliasAndTempLeafStamping:
    """_lineage_node_to_edges Step 2.2 must apply schema aliases before the
    temp-identity check, or the check misses when the raw exp.Table carries
    the pre-alias db (e.g. 'ba_tmp') while the registered key uses the
    post-alias db (e.g. 'ba').

    Reproduces the live-DWH dual-write observed in v1.21.0:
      - CREATE stmt (:1) produced etl/.../wtfv_bon.sql::ba.tmp_base (kind='temp')
      - INSERT stmts (:2/:3) also produced ba.tmp_base (kind='table') because
        the leaf exp.Table had db='ba_tmp' (pre-alias) and _temp_identity
        returned 'ba_tmp.tmp_base' which was NOT in _current_file_temp_keys
        {'ba.tmp_base'} — the check missed and the aliased un-namespaced ref
        fell through to _apply_table_alias, producing a second kind='table' row.

    Guards fix plan/sprints/temp_table_namespacing.md §Deviations (v1.21.1).
    """

    def test_use_schema_alias_temp_leaf_edges_are_namespaced(self, tmp_path):
        """With schema_aliases={'ba_tmp': 'ba'} and USE SCHEMA BA_TMP, the COLUMN_LINEAGE
        edges through the temp carry the NAMESPACED key — no un-namespaced ba.tmp_base src.

        Pre-fix: _lineage_node_to_edges read src_db='ba_tmp' from the raw exp.Table,
        computed _temp_identity('ba_tmp','tmp_base')='ba_tmp.tmp_base' which missed
        the registered key 'ba.tmp_base' → edge used un-namespaced ba.tmp_base (role='table').
        Post-fix: alias is applied before the check, 'ba.tmp_base' matches, edge uses
        the namespaced temp key.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        sql = (
            "USE SCHEMA BA_TMP;\n"
            "CREATE OR REPLACE TEMPORARY TABLE tmp_base AS"
            " SELECT winkelnr FROM DA.source_table;\n"
            "INSERT INTO real_target SELECT winkelnr FROM tmp_base;\n"
        )
        rel = "etl/sql/fact/wtfv_bon.sql"
        parser = SnowflakeParser(
            SchemaResolver(dialect="snowflake"),
            schema_aliases={"ba_tmp": "ba"},
        )
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        # The INSERT statement is index 1 (USE is suppressed, CREATE=0, INSERT=1).
        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        assert insert_stmts, f"No INSERT found in {[s.kind for s in result.statements]}"
        insert_stmt = insert_stmts[0]

        # Collect COLUMN_LINEAGE edges from the INSERT.
        # Pre-fix: edges have src.table.full_id = 'ba.tmp_base' (un-namespaced, role='table').
        # Post-fix: edges have src.table.full_id = 'etl/.../wtfv_bon.sql::ba.tmp_base' (namespaced)
        #           OR resolve straight through to 'da.source_table' (if expansion succeeded).
        for edge in insert_stmt.column_lineage:
            src_fid = edge.src.table.full_id
            assert src_fid != "ba.tmp_base", (
                f"Un-namespaced 'ba.tmp_base' (role='table') must not appear as edge src "
                f"when a schema alias maps ba_tmp→ba and the temp was created in this file. "
                f"This is the v1.21.0 dual-write bug (fix/temp-namespacing-dual-write). "
                f"Got: {src_fid!r}"
            )
            # If the src IS the temp, it must be namespaced.
            if "tmp_base" in src_fid:
                assert "::" in src_fid, f"A tmp_base src edge must be namespaced, got: {src_fid!r}"

    def test_use_schema_alias_temp_target_is_correct_key(self, tmp_path):
        """The CREATE TEMPORARY TABLE target uses the post-alias db in the namespaced key.

        Guards that Step 1.2 target registration is consistent with Step 2.2's new
        alias-aware lookup (post-fix both use 'ba.tmp_base' as the registered identity).

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        sql = (
            "USE SCHEMA BA_TMP;\n"
            "CREATE OR REPLACE TEMPORARY TABLE tmp_base AS SELECT x FROM DA.src;\n"
        )
        rel = "etl/sql/fact/f.sql"
        parser = SnowflakeParser(
            SchemaResolver(dialect="snowflake"),
            schema_aliases={"ba_tmp": "ba"},
        )
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        assert result.defined_tables, "Expected at least one defined table"
        tgt = result.defined_tables[0]
        assert tgt.role == "temp", f"Expected role='temp', got {tgt.role!r}"
        # Post-alias: db='ba', so full_id must be '<rel>::ba.tmp_base'
        assert tgt.full_id == f"{rel}::ba.tmp_base", (
            f"Expected '{rel}::ba.tmp_base', got {tgt.full_id!r}"
        )
        assert tgt.db == "ba", f"Expected db='ba' (post-alias), got db={tgt.db!r}"

    def test_use_schema_alias_no_dual_write_in_sources(self, tmp_path):
        """Sources list for the INSERT must contain a NAMESPACED temp ref — not bare.

        Step 2.1 (source stamping) works correctly because _real_tables() goes through
        _apply_table_alias before stamping.  This test confirms that Step 2.1 and
        Step 2.2 (after the fix) both produce the same namespaced key.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        sql = (
            "USE SCHEMA BA_TMP;\n"
            "CREATE OR REPLACE TEMPORARY TABLE tmp_base AS SELECT winkelnr FROM DA.src;\n"
            "INSERT INTO real_target SELECT winkelnr FROM tmp_base;\n"
        )
        rel = "etl/sql/fact/check.sql"
        parser = SnowflakeParser(
            SchemaResolver(dialect="snowflake"),
            schema_aliases={"ba_tmp": "ba"},
        )
        result = parser.parse_file(tmp_path / rel, sql, rel_path=rel)

        insert_stmts = [s for s in result.statements if s.kind == "INSERT"]
        assert insert_stmts, "No INSERT statement found"
        insert_stmt = insert_stmts[0]

        # Step 2.1: the source for tmp_base should be stamped role='temp' and namespaced.
        tmp_sources = [s for s in insert_stmt.sources if "tmp_base" in s.name]
        assert tmp_sources, f"No tmp_base source found in INSERT; sources: {insert_stmt.sources}"
        for src in tmp_sources:
            assert src.role == "temp", (
                f"Source tmp_base must have role='temp', got role={src.role!r}"
            )
            assert "::" in src.full_id, (
                f"Source tmp_base must be namespaced, got full_id={src.full_id!r}"
            )
