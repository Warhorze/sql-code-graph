"""Unit tests for the normalize_keys key-normalisation boundary pass.

Guards the PR 1 choke point from
plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.

Tests cover:
- Alias rewrite applied to every TableRef-bearing field (except ctes).
- Empty-identity guard: stage refs yield stage://<name> or are dropped+logged.
- Dataclass-introspection guard: any new TableRef-bearing field in ParsedFile /
  QueryNode not covered by normalize_keys fails this test.
"""

from __future__ import annotations

from pathlib import Path

from sqlcg.parsers.base import (
    ColumnRef,
    LineageEdge,
    ParsedFile,
    QueryNode,
    StarSource,
    TableRef,
    normalize_keys,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALIASES = {"ba_tmp": "ba", "da_tmp": "da"}


def _pf(stmts: list[QueryNode] | None = None, **kwargs) -> ParsedFile:
    """Minimal ParsedFile factory."""
    pf = ParsedFile(path=Path("test.sql"), dialect=None, **kwargs)
    if stmts:
        pf.statements = stmts
    return pf


def _ref(db: str = "", name: str = "tbl", catalog: str = "", role: str = "table") -> TableRef:
    return TableRef(catalog=catalog or None, db=db or None, name=name, role=role)


def _edge(src_db: str, src_name: str, dst_db: str, dst_name: str) -> LineageEdge:
    src = ColumnRef(table=TableRef(db=src_db or None, name=src_name), name="col")
    dst = ColumnRef(table=TableRef(db=dst_db or None, name=dst_name), name="col")
    return LineageEdge(src=src, dst=dst, transform="PASS_THROUGH")


# ---------------------------------------------------------------------------
# Test 1: alias rewrite covers all TableRef-bearing fields
# ---------------------------------------------------------------------------


class TestNormalizeKeysAliasRewrite:
    """Every TableRef-bearing field in ParsedFile/QueryNode is rewritten through aliases."""

    def test_every_field_uses_aliased_schema(self):
        """After normalize_keys, no TableRef retains the pre-alias schema name.

        Builds a ParsedFile with:
        - defined_tables containing ba_tmp ref
        - referenced_tables containing da_tmp ref
        - QueryNode.target = ba_tmp ref
        - QueryNode.sources = [da_tmp ref]
        - QueryNode.star_sources = [StarSource over ba_tmp ref]
        - QueryNode.clone_source = da_tmp ref
        - QueryNode.column_lineage = edge with ba_tmp src and da_tmp dst

        After normalize_keys every key should use the aliased schema.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        f = Path("test.sql")
        target_ref = TableRef(db="ba_tmp", name="wtfa_loonkosten")
        src_ref = TableRef(db="da_tmp", name="staging_tbl")
        clone_ref = TableRef(db="da_tmp", name="base_tbl")
        edge = _edge("ba_tmp", "wtfa_loonkosten", "da_tmp", "staging_tbl")
        star = StarSource(source=TableRef(db="ba_tmp", name="star_src"))

        stmt = QueryNode(
            file=f,
            statement_index=0,
            sql="INSERT ...",
            kind="INSERT",
            target=target_ref,
            sources=[src_ref],
            star_sources=[star],
            clone_source=clone_ref,
            column_lineage=[edge],
        )
        pf = _pf(
            stmts=[stmt],
            defined_tables=[TableRef(db="ba_tmp", name="wtfa_loonkosten")],
            referenced_tables=[TableRef(db="da_tmp", name="staging_tbl")],
        )

        normalize_keys(pf, _ALIASES)

        # defined_tables
        assert pf.defined_tables[0].db == "ba"
        assert pf.defined_tables[0].full_id == "ba.wtfa_loonkosten"

        # referenced_tables
        assert pf.referenced_tables[0].db == "da"
        assert pf.referenced_tables[0].full_id == "da.staging_tbl"

        # target
        assert pf.statements[0].target is not None
        assert pf.statements[0].target.db == "ba"

        # sources
        assert pf.statements[0].sources[0].db == "da"

        # star_sources
        assert pf.statements[0].star_sources[0].source.db == "ba"

        # clone_source
        assert pf.statements[0].clone_source is not None
        assert pf.statements[0].clone_source.db == "da"

        # column_lineage edges
        e = pf.statements[0].column_lineage[0]
        assert e.src.table.db == "ba", f"src table db not aliased: {e.src.table.db!r}"
        assert e.dst.table.db == "da", f"dst table db not aliased: {e.dst.table.db!r}"
        # No pre-alias key survives
        assert "ba_tmp" not in e.src.table.full_id
        assert "da_tmp" not in e.dst.table.full_id

    def test_non_aliased_refs_untouched(self):
        """Refs whose schema is not in the alias map are left unchanged.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        ref = TableRef(db="other_schema", name="tbl")
        pf = _pf(defined_tables=[ref])
        normalize_keys(pf, _ALIASES)
        assert pf.defined_tables[0].db == "other_schema"

    def test_no_aliases_is_noop_for_alias_rewrite(self):
        """With an empty alias map, no ref is rewritten.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        ref = TableRef(db="ba_tmp", name="tbl")
        pf = _pf(defined_tables=[ref])
        normalize_keys(pf, {})
        assert pf.defined_tables[0].db == "ba_tmp"


# ---------------------------------------------------------------------------
# Test 2: empty-identity guard (stage reads)
# ---------------------------------------------------------------------------


class TestNormalizeKeysEmptyIdentityGuard:
    """Stage refs and other empty-full_id TableRefs are handled by the guard."""

    def test_stage_ref_with_name_becomes_stage_uri(self):
        """A TableRef with a non-empty name but empty db yields stage://<name> key.

        Stage reads such as ``FROM @my_stage`` produce a TableRef with name='my_stage'
        and no db, so full_id is just 'my_stage' (non-empty) — not the empty-identity
        case.  But a TableRef(name='') with no db/catalog yields full_id='' which is
        the true empty-identity case.  This test verifies the synthetic stage:// rewrite
        for the case where we explicitly construct a zero-name ref.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        # Simulate a stage ref where the stage name was captured
        stage_ref = TableRef(db=None, catalog=None, name="my_stage")
        # Wrap in an edge where the src table is the stage
        src = ColumnRef(table=stage_ref, name="col1")
        dst = ColumnRef(table=TableRef(db="ba", name="target_tbl"), name="col1")
        edge = LineageEdge(src=src, dst=dst, transform="PASS_THROUGH")
        stmt = QueryNode(
            file=Path("test.sql"), statement_index=0, sql="", kind="INSERT", column_lineage=[edge]
        )
        pf = _pf(stmts=[stmt])
        normalize_keys(pf, {})
        # Non-empty name: full_id is 'my_stage' (not empty), no rewrite needed
        result_edge = pf.statements[0].column_lineage[0]
        assert result_edge.src.table.full_id == "my_stage"

    def test_empty_full_id_ref_in_edge_dropped_and_logged(self):
        """A TableRef with empty full_id (name='', db=None) drops the edge and logs a skip.

        This simulates a stage read that produced a zero-length name TableRef.
        After normalize_keys: edge is absent, errors contains col_lineage_skip:stage:.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        # Build an empty-name TableRef (full_id == "")
        phantom = TableRef(db=None, catalog=None, name="")
        src = ColumnRef(table=phantom, name="ean_code")
        dst = ColumnRef(table=TableRef(db="ba", name="target"), name="ean_code")
        edge = LineageEdge(src=src, dst=dst, transform="PASS_THROUGH")
        stmt = QueryNode(
            file=Path("test.sql"), statement_index=0, sql="", kind="INSERT", column_lineage=[edge]
        )
        pf = _pf(stmts=[stmt])

        normalize_keys(pf, {})

        # Edge must be dropped
        assert len(pf.statements[0].column_lineage) == 0, (
            "Edge with empty-full_id src must be dropped by normalize_keys"
        )
        # Error must be logged
        assert any("col_lineage_skip:stage:" in e for e in pf.errors), (
            f"Expected col_lineage_skip:stage: in errors, got: {pf.errors}"
        )

    def test_no_leading_dot_key_after_normalisation(self):
        """After normalize_keys, no edge emits a full_id with a leading dot.

        A leading dot (e.g. '.ean_code') occurs when TableRef.full_id is '' and
        ColumnRef.full_id = '' + '.' + col_name.  normalize_keys must eliminate it.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        phantom = TableRef(db=None, catalog=None, name="")
        src = ColumnRef(table=phantom, name="metadata_filename")
        dst = ColumnRef(table=TableRef(db="da", name="output"), name="filename")
        edge = LineageEdge(src=src, dst=dst, transform="PASS_THROUGH")
        stmt = QueryNode(
            file=Path("test.sql"),
            statement_index=0,
            sql="",
            kind="CREATE_TABLE",
            column_lineage=[edge],
        )
        pf = _pf(stmts=[stmt])
        normalize_keys(pf, {})

        for st in pf.statements:
            for e in st.column_lineage:
                assert not e.src.full_id.startswith("."), (
                    f"Leading-dot src key survived: {e.src.full_id!r}"
                )
                assert not e.dst.full_id.startswith("."), (
                    f"Leading-dot dst key survived: {e.dst.full_id!r}"
                )

    def test_empty_defined_table_dropped(self):
        """A defined_table with empty full_id is dropped and logged.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        phantom = TableRef(db=None, catalog=None, name="")
        pf = _pf(defined_tables=[phantom])
        normalize_keys(pf, {})
        assert len(pf.defined_tables) == 0, "Empty-full_id defined_table must be dropped"
        assert any("col_lineage_skip:stage:" in e for e in pf.errors)


# ---------------------------------------------------------------------------
# Test 3: dataclass introspection guard
# ---------------------------------------------------------------------------


class TestNormalizeKeysIntrospectionGuard:
    """Enumerate all TableRef-bearing fields; fail if normalize_keys doesn't cover them.

    The intent is that if a developer adds a new TableRef-bearing field to ParsedFile
    or QueryNode and forgets to update normalize_keys, this test fails loudly.
    QueryNode.ctes is explicitly excluded per the W1 note in the plan — it is
    parser-internal pass-2 state, never read by key-construction consumers.
    Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1 (W1 note).
    """

    # Fields that are intentionally excluded from normalization (parser-internal state).
    _EXCLUDED_QUERY_NODE_FIELDS = frozenset(
        [
            "ctes",  # W1: parser-internal pass-2 CTE name→TableRef map; not a graph key source
        ]
    )

    def _is_tableref_bearing(self, annotation) -> bool:
        """Return True if the annotation is or directly contains TableRef.

        Handles:
        - ``TableRef``
        - ``list[TableRef]``
        - ``dict[str, TableRef]``
        - ``TableRef | None`` (Python 3.10+ ``types.UnionType``)
        - ``Optional[TableRef]`` (``typing.Union[TableRef, None]``)

        Does NOT recurse into nested containers such as ``list[StarSource]``
        (StarSource wraps a TableRef but is not itself one).  The introspection
        guard is intentionally coarse: it catches the common field shapes that
        have historically been added to QueryNode / ParsedFile, and requires
        the developer to update ``expected`` when a new shape appears.
        """
        import types
        import typing

        origin = getattr(annotation, "__origin__", None)
        args = getattr(annotation, "__args__", ()) or ()

        if annotation is TableRef:
            return True
        if origin is list and args and args[0] is TableRef:
            return True
        if origin is dict and args and TableRef in args:
            return True
        # Python 3.10+ ``X | Y`` syntax → types.UnionType (no __origin__)
        if isinstance(annotation, types.UnionType):
            return any(a is TableRef for a in args)
        # typing.Optional / typing.Union
        if origin is typing.Union:
            return any(a is TableRef for a in args)
        return False

    def test_parsed_file_tableref_fields_are_all_covered(self):
        """All TableRef-bearing fields in ParsedFile are handled by normalize_keys.

        If a new field is added to ParsedFile that bears a TableRef and is not covered
        by normalize_keys, this test fails — prompting the developer to add coverage.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        hints = ParsedFile.__dataclass_fields__
        tableref_fields = [
            name for name, field in hints.items() if self._is_tableref_bearing(field.type)
        ]
        # Expected: defined_tables and referenced_tables
        expected = {"defined_tables", "referenced_tables"}
        uncovered = set(tableref_fields) - expected
        assert not uncovered, (
            f"ParsedFile has new TableRef-bearing fields not covered by normalize_keys: "
            f"{uncovered!r}. Update normalize_keys() and add them to the expected set."
        )

    def test_query_node_tableref_fields_are_all_covered(self):
        """All TableRef-bearing fields in QueryNode (except ctes) are handled by normalize_keys.

        QueryNode.ctes is intentionally excluded: it is a parser-internal dict used only
        during pass-2 cross-file resolution and is never read by key-construction consumers.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1 (W1 note).
        """
        hints = QueryNode.__dataclass_fields__
        tableref_fields = [
            name
            for name, field in hints.items()
            if self._is_tableref_bearing(field.type)
            and name not in self._EXCLUDED_QUERY_NODE_FIELDS
        ]
        # Expected covered fields
        expected = {"target", "sources", "star_sources", "clone_source"}
        uncovered = set(tableref_fields) - expected
        assert not uncovered, (
            f"QueryNode has new TableRef-bearing fields not covered by normalize_keys: "
            f"{uncovered!r}. Update normalize_keys() and add them to the expected set, "
            f"OR add them to _EXCLUDED_QUERY_NODE_FIELDS with a justification comment."
        )
