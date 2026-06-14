"""Unit tests for the JoinColResolve parse-time marker (Bug #5, PR-5 option b).

The parser emits a JoinColResolve marker (and SUPPRESSES the sqlglot mis-bind
edge) for bare unqualified columns in a >=2-physical-table INSERT/CREATE-SELECT
join. Qualified projections take the existing path unchanged. Resolution itself
is deferred to a post-index SQL pass (tested in
tests/integration/test_join_col_resolution.py and
tests/unit/test_resolve_join_columns_sql.py).

Guards the PR-5 parser section of
[plan/sprints/bugfix_lineage_correctness_validation.md](../../plan/sprints/bugfix_lineage_correctness_validation.md).
"""

import dataclasses
from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ColumnRef, JoinColResolve, TableRef


def test_join_col_resolve_dataclass_fields():
    """JoinColResolve exposes .dst (ColumnRef) and .bare_col (str)."""
    jcr = JoinColResolve(
        dst=ColumnRef(TableRef(db="sales", name="tgt"), "status"), bare_col="status"
    )
    assert jcr.dst == ColumnRef(TableRef(db="sales", name="tgt"), "status")
    assert jcr.bare_col == "status"


def test_join_col_resolve_is_frozen():
    """JoinColResolve is a frozen dataclass."""
    jcr = JoinColResolve(dst=ColumnRef(TableRef(name="tgt"), "c"), bare_col="c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        jcr.bare_col = "oops"  # type: ignore[misc]


def test_two_table_join_bare_column_emits_marker_and_suppresses_edge():
    """A bare column in a 2-table INSERT join emits a marker, not a sqlglot edge.

    Guards the parse-time marker acceptance criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    parser = AnsiParser(SchemaResolver())
    sql = (
        "INSERT INTO sales.tgt SELECT amount, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id"
    )
    parsed = parser.parse_file(Path("t.sql"), sql)
    stmt = parsed.statements[0]

    markers = {(m.dst.full_id, m.bare_col) for m in stmt.join_col_resolves}
    assert ("sales.tgt.amount", "amount") in markers
    assert ("sales.tgt.status", "status") in markers

    # The mis-bind edge for the bare columns must be SUPPRESSED — no COLUMN_LINEAGE
    # edge is emitted at parse time for these projections.
    assert stmt.column_lineage == []


def test_qualified_join_emits_no_marker():
    """A fully-qualified 2-table join emits ZERO markers (safe-identifier regression).

    Guards the safe-identifier criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    parser = AnsiParser(SchemaResolver())
    sql = (
        "INSERT INTO sales.tgt SELECT o.amount, c.status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id"
    )
    parsed = parser.parse_file(Path("t.sql"), sql)
    stmt = parsed.statements[0]
    assert stmt.join_col_resolves == []


def test_single_table_bare_column_emits_no_marker():
    """A bare column with only ONE source table is not ambiguous — no marker.

    The marker fires only when the join has >=2 physical source tables.
    """
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO sales.tgt SELECT amount, status FROM sales.orders"
    parsed = parser.parse_file(Path("t.sql"), sql)
    stmt = parsed.statements[0]
    assert stmt.join_col_resolves == []


def test_marker_dst_key_matches_target_column_id():
    """Marker dst_key equals <target_table>.<dst_col> (the SqlColumn id the projection produces)."""
    parser = AnsiParser(SchemaResolver())
    sql = (
        "INSERT INTO sales.tgt SELECT amount AS total "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id"
    )
    parsed = parser.parse_file(Path("t.sql"), sql)
    stmt = parsed.statements[0]
    # The aliased projection's dst column name is the alias ('total'); the bare
    # source column ('amount') is what we resolve against source tables.
    markers = {(m.dst.full_id, m.bare_col) for m in stmt.join_col_resolves}
    assert ("sales.tgt.total", "amount") in markers
