"""IDENTIFIER($var) table-indirection resolution (PR-A).

``FROM IDENTIFIER($v)`` / ``INSERT INTO IDENTIFIER($v)`` parse to an ``exp.Table``
whose ``.this`` is an ``exp.Anonymous`` named ``identifier`` wrapping an
``exp.Parameter`` — so ``table.name == ''`` and the empty-id guard in ``base.py``
silently drops the source/target.  When the bind variable is set earlier in the file
via ``SET v='da.x'`` (``exp.Set``) or ``v := 'da.x'`` (``exp.PropertyEQ``), PR-A
rewrites the IDENTIFIER node to that real table before qualification, recovering the
column-lineage and SELECTS_FROM edge.

Guards the PR-A IDENTIFIER($var) indirection plan
([plan doc](../../plan/sprints/sprint_snowflake_lineage_patterns.md)).

Asserts observable edges / qualified targets, never "no exception raised".
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


def _edge_pairs(result) -> set[tuple[str, str]]:
    return {(e.src.table.full_id, e.dst.table.full_id) for e in _all_edges(result)}


def test_identifier_from_set_form_resolves_source_edge():
    """AC-A1: SET v='da.x' + FROM identifier($v) emits a real source edge.

    BEFORE (master): the IDENTIFIER source parses to an empty-id table and the edge
    src is empty-id / not resolved.  AFTER: src.table.full_id == 'da.stibat_x'.
    """
    sql = (
        "SET TABLE_STIBAT='da.stibat_x';\n"
        "INSERT INTO da.sink SELECT sti.id AS id FROM identifier($TABLE_STIBAT) sti;"
    )
    result = _snow().parse_file("/tmp/x.sql", sql)
    edges = _all_edges(result)
    matching = [
        e
        for e in edges
        if e.src.table.full_id == "da.stibat_x" and e.dst.table.full_id == "da.sink"
    ]
    assert matching, f"expected da.stibat_x -> da.sink edge, got {_edge_pairs(result)}"
    # The empty-id source must NOT survive anywhere as an edge endpoint.
    assert all(e.src.table.full_id for e in edges), "an empty-id source edge leaked"


def test_identifier_insert_target_set_form_resolves_target():
    """AC-A2: SET tgt='da.result' + INSERT INTO IDENTIFIER($tgt) resolves the target.

    INSERT target (exp.Insert.this is an exp.Table) is rewritten by the same walk.
    """
    sql = "SET tgt='da.result';\nINSERT INTO IDENTIFIER($tgt) SELECT a AS a FROM raw.src;"
    result = _snow().parse_file("/tmp/x.sql", sql)
    insert_stmt = next(s for s in result.statements if s.target is not None)
    assert insert_stmt.target is not None
    assert insert_stmt.target.full_id == "da.result"
    assert any(r.full_id == "raw.src" for r in insert_stmt.sources)


def test_identifier_unresolved_var_left_as_empty_id():
    """AC-A3: an unset $var is left unrewritten — its source stays empty-id, no crash.

    Gate-corrected BEFORE-state: at the parse layer the unresolved IDENTIFIER still
    EMITS an empty-id edge (''.a -> da.sink.a); it is not fully dropped pre-edge (the
    empty-id `_norm_ref` drop + col_lineage_skip:stage: happen later in the
    indexer's normalize_keys pass).  Assert no resolved edge and the empty-id source
    is present — graceful degradation, not a raise.
    """
    sql = "INSERT INTO da.sink SELECT x.a AS a FROM identifier($UNSET) x;"
    result = _snow().parse_file("/tmp/x.sql", sql)
    edges = _all_edges(result)
    # No edge whose source resolved to a real table for the unset var.
    assert all(e.src.table.full_id != "unset" for e in edges)
    # The unresolved IDENTIFIER source remains empty-id (BEFORE-state, gate-corrected).
    assert any(e.src.table.full_id == "" for e in edges), (
        f"expected an empty-id source edge for the unresolved var, got "
        f"{[(e.src.table.full_id, e.dst.table.full_id) for e in edges]}"
    )


def test_identifier_use_schema_applies_to_resolved_bare_literal():
    """AC-A4: a bare resolved literal still inherits the active USE SCHEMA prefix.

    Rewrite happens BEFORE _qualify_bare_tables, so 'stibat_x' becomes 'da.stibat_x'.
    """
    sql = (
        "USE SCHEMA da;\n"
        "SET t='stibat_x';\n"
        "INSERT INTO da.sink SELECT s.id AS id FROM identifier($t) s;"
    )
    result = _snow().parse_file("/tmp/x.sql", sql)
    assert ("da.stibat_x", "da.sink") in _edge_pairs(result), _edge_pairs(result)


def test_identifier_let_propertyeq_form_still_resolves():
    """AC-A5: the LET / := form (exp.PropertyEQ) resolves identically (regression guard)."""
    sql = "v := 'da.x';\nINSERT INTO da.sink SELECT s.a AS a FROM identifier($v) s;"
    result = _snow().parse_file("/tmp/x.sql", sql)
    pairs = _edge_pairs(result)
    assert ("da.x", "da.sink") in pairs, pairs


def test_identifier_alias_preserved_on_resolution():
    """The FROM alias survives the rewrite (identifier($T) sti -> da.x AS sti).

    Asserted indirectly: the alias-qualified column sti.id resolves to the rewritten
    source da.x, which only works if the alias was carried onto the new table.
    """
    sql = "SET T='da.x';\nINSERT INTO da.sink SELECT sti.id AS id FROM identifier($T) sti;"
    result = _snow().parse_file("/tmp/x.sql", sql)
    edges = _all_edges(result)
    assert any(e.src.table.full_id == "da.x" and e.src.name == "id" for e in edges), [
        (e.src.table.full_id, e.src.name) for e in edges
    ]


def test_identifier_non_literal_rhs_degrades_to_drop():
    """Non-literal SET RHS (a subquery) is not captured; the IDENTIFIER falls through.

    No wrong edge is emitted for the unresolved var (degrade-to-drop, no regression).
    """
    sql = (
        "SET v = (SELECT CURRENT_DATABASE());\n"
        "INSERT INTO da.sink SELECT s.a AS a FROM identifier($v) s;"
    )
    result = _snow().parse_file("/tmp/x.sql", sql)
    edges = _all_edges(result)
    # No edge resolved to a real literal table for v; the source stays empty-id
    # (degrade-to-drop), no wrong edge is emitted.
    assert all(e.src.table.full_id != "current_database" for e in edges)
    assert any(e.src.table.full_id == "" for e in edges), (
        f"expected empty-id source for non-literal RHS, got "
        f"{[(e.src.table.full_id, e.dst.table.full_id) for e in edges]}"
    )


def test_bare_set_emits_no_lineage_edge_but_feeds_var_map():
    """A bare SET stays a lineage-free node yet still feeds the var-literal map.

    The SET node is preserved (existing behaviour; a SET parses to a lineage-free
    kind=OTHER node — see test_parse_failed_classification); it produces no
    column-lineage edge of its own, while its literal is harvested so the following
    IDENTIFIER($v) source still resolves.
    """
    sql = "SET v='da.x';\nINSERT INTO da.sink SELECT s.a AS a FROM identifier($v) s;"
    result = _snow().parse_file("/tmp/x.sql", sql)
    # The real lineage still lands.
    assert ("da.x", "da.sink") in _edge_pairs(result)
    # The SET statement itself contributes no lineage edge.
    set_stmts = [s for s in result.statements if s.target is None and not s.sources]
    for s in set_stmts:
        assert s.column_lineage == []
