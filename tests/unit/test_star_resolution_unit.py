"""Unit tests for star-projection resolution data models and parser behaviour.

Sprint: sprint_star_resolution.md  Tickets: T-01, T-02, T-03

These tests are marked xfail because the implementation does not exist yet.
They will automatically flip to XPASS (and must be promoted to plain passes)
once the sprint lands.
"""

import dataclasses
from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import TableRef


# ---------------------------------------------------------------------------
# T-02 — StarSource dataclass
# ---------------------------------------------------------------------------


def test_star_source_dataclass_fields():
    """StarSource must expose .source (TableRef) and .qualifier (str | None)."""
    from sqlcg.parsers.base import StarSource

    ss = StarSource(source=TableRef(db="BA", name="src"), qualifier="base")
    assert ss.source == TableRef(db="BA", name="src")
    assert ss.qualifier == "base"


def test_star_source_is_frozen():
    """StarSource must be a frozen dataclass (mutation raises FrozenInstanceError)."""
    from sqlcg.parsers.base import StarSource

    ss = StarSource(source=TableRef(name="t"), qualifier=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ss.qualifier = "oops"  # type: ignore[misc]


def test_star_source_default_qualifier_is_none():
    """StarSource qualifier defaults to None."""
    from sqlcg.parsers.base import StarSource

    ss = StarSource(source=TableRef(name="t"))
    assert ss.qualifier is None


# ---------------------------------------------------------------------------
# T-01 — QueryNode.defined_columns
# ---------------------------------------------------------------------------


def test_query_node_has_defined_columns_field():
    """QueryNode must have a defined_columns list field defaulting to []."""
    from sqlcg.parsers.base import QueryNode

    node = QueryNode(file=Path("t.sql"), statement_index=0, sql="SELECT 1")
    assert hasattr(node, "defined_columns")
    assert node.defined_columns == []


def test_create_table_extracts_column_names():
    """CREATE TABLE parses column names into QueryNode.defined_columns in source order."""
    parser = AnsiParser(SchemaResolver())
    sql = "CREATE TABLE BA.t (a INT, b STRING, c DATE)"
    parsed = parser.parse_file(Path("t.sql"), sql)

    assert len(parsed.statements) == 1
    stmt = parsed.statements[0]
    assert stmt.kind == "CREATE_TABLE"
    assert stmt.defined_columns == ["a", "b", "c"]


def test_non_create_table_has_empty_defined_columns():
    """INSERT INTO ... SELECT must not populate defined_columns."""
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO BA.tgt SELECT col1 FROM BA.src"
    parsed = parser.parse_file(Path("t.sql"), sql)

    assert len(parsed.statements) == 1
    assert parsed.statements[0].defined_columns == []


# ---------------------------------------------------------------------------
# T-02 — QueryNode.star_sources field
# ---------------------------------------------------------------------------


def test_query_node_has_star_sources_field():
    """QueryNode must have a star_sources list field defaulting to []."""
    from sqlcg.parsers.base import QueryNode

    node = QueryNode(file=Path("t.sql"), statement_index=0, sql="SELECT 1")
    assert hasattr(node, "star_sources")
    assert node.star_sources == []


# ---------------------------------------------------------------------------
# T-03 — Parser emits StarSource markers
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="_extract_column_lineage StarSource emission not implemented — T-03", strict=True)
def test_select_star_records_star_source():
    """INSERT INTO ... SELECT * FROM src must populate star_sources on the QueryNode."""
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO BA.target SELECT * FROM BA.src"
    parsed = parser.parse_file(Path("t.sql"), sql)

    insert = parsed.statements[0]
    assert len(insert.star_sources) == 1
    assert insert.star_sources[0].source.full_id == "BA.src"
    assert insert.star_sources[0].qualifier is None
    # Parser must not produce concrete column lineage edges for star projections
    assert insert.column_lineage == []


@pytest.mark.xfail(reason="_extract_column_lineage StarSource emission not implemented — T-03", strict=True)
def test_select_alias_star_records_qualifier():
    """SELECT base.* FROM src AS base must set qualifier='base' and resolve source."""
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO BA.target SELECT base.* FROM BA.src AS base"
    parsed = parser.parse_file(Path("t.sql"), sql)

    insert = parsed.statements[0]
    assert len(insert.star_sources) == 1
    assert insert.star_sources[0].source.full_id == "BA.src"
    assert insert.star_sources[0].qualifier == "base"


def test_no_sources_skips_star_source():
    """Bare SELECT * with no resolvable source tables emits no StarSource but keeps error marker."""
    parser = AnsiParser(SchemaResolver())
    # A bare SELECT * has no FROM clause — sources list is empty
    sql = "SELECT *"
    parsed = parser.parse_file(Path("t.sql"), sql)

    stmt = parsed.statements[0]
    assert stmt.star_sources == []
    # The existing error marker must still be appended
    assert any("col_lineage_skip:star:" in e for e in parsed.errors)


@pytest.mark.xfail(reason="_extract_column_lineage StarSource emission not implemented — T-03", strict=True)
def test_existing_star_error_marker_retained_alongside_star_source():
    """After T-03, BOTH the error marker AND a StarSource must be present simultaneously.

    The StarSource emission is additive: the existing col_lineage_skip:star: entry
    in parsed.errors must survive alongside the new star_sources field on the QueryNode.
    """
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO BA.tgt SELECT * FROM BA.src"
    parsed = parser.parse_file(Path("t.sql"), sql)

    # Error marker must still be there (existing behaviour)
    star_errors = [e for e in parsed.errors if e.startswith("col_lineage_skip:star:")]
    assert len(star_errors) >= 1, (
        "col_lineage_skip:star: marker must be retained alongside StarSource emission"
    )
    # AND the new star_sources field must also be populated (T-03 behaviour)
    stmt = parsed.statements[0]
    assert len(stmt.star_sources) >= 1, (
        "star_sources must be populated when error marker is present (both must coexist)"
    )


@pytest.mark.xfail(reason="_extract_column_lineage StarSource emission not implemented — T-03", strict=True)
def test_multi_source_star_uses_first_source_as_fallback():
    """SELECT * FROM a JOIN b — with no qualifier, first source is the fallback."""
    parser = AnsiParser(SchemaResolver())
    sql = "INSERT INTO BA.tgt SELECT * FROM BA.src_a JOIN BA.src_b ON BA.src_a.id = BA.src_b.id"
    parsed = parser.parse_file(Path("t.sql"), sql)

    stmt = parsed.statements[0]
    # At least one StarSource must be emitted
    assert len(stmt.star_sources) >= 1
    # The first StarSource must point to one of the two sources (whichever comes first)
    assert stmt.star_sources[0].source.full_id in ("BA.src_a", "BA.src_b")
    assert stmt.star_sources[0].qualifier is None


@pytest.mark.xfail(reason="_resolve_star_source not yet implemented — T-03", strict=True)
def test_resolve_star_source_method_exists_on_parser():
    """SqlParser must have a _resolve_star_source instance method after T-03."""
    parser = AnsiParser(SchemaResolver())
    assert hasattr(parser, "_resolve_star_source"), (
        "_resolve_star_source must be defined on SqlParser base class"
    )
    # Call it directly to verify basic contract
    result = parser._resolve_star_source(
        qualifier=None,
        sources=[TableRef(db="BA", name="src")],
    )
    assert result is not None
    assert result.full_id == "BA.src"


@pytest.mark.xfail(reason="_resolve_star_source not yet implemented — T-03", strict=True)
def test_resolve_star_source_alias_match():
    """_resolve_star_source must resolve alias 'b' to the matching TableRef."""
    parser = AnsiParser(SchemaResolver())
    sources = [
        TableRef(db="BA", name="src_a", alias="a"),
        TableRef(db="BA", name="src_b", alias="b"),
    ]
    result = parser._resolve_star_source(qualifier="b", sources=sources)
    assert result is not None
    assert result.name == "src_b"


@pytest.mark.xfail(reason="_resolve_star_source not yet implemented — T-03", strict=True)
def test_resolve_star_source_empty_sources_returns_none():
    """_resolve_star_source must return None when sources list is empty."""
    parser = AnsiParser(SchemaResolver())
    result = parser._resolve_star_source(qualifier=None, sources=[])
    assert result is None
