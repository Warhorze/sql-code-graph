"""Unit tests for AnsiParser._extract_select_output_columns.

Pins the naming contract for CTAS / CREATE VIEW column derivation independently
of the full index path, so a future refactor of the projection walk cannot silently
drop columns or change case normalization.

Guards the P3/P4 coverage fixes
([plan doc](plan/sprints/coverage_p1_p3_p4.md)).
"""

from __future__ import annotations

import sqlglot
import sqlglot.expressions as exp

from sqlcg.parsers.ansi_parser import AnsiParser


def _parse_select(sql: str, dialect: str | None = None) -> exp.Select:
    """Parse a bare SELECT SQL into an exp.Select node."""
    stmts = sqlglot.parse(sql, dialect=dialect)
    assert stmts, f"Could not parse: {sql!r}"
    stmt = stmts[0]
    assert isinstance(stmt, exp.Select), f"Expected Select, got {type(stmt)}"
    return stmt


# ---------------------------------------------------------------------------
# Alias-wins cases
# ---------------------------------------------------------------------------


def test_extract_select_output_columns_aliases_win():
    """Aliased projections return the alias, not the source column name.

    SELECT x AS col_a, y AS col_b, z AS col_c → ['col_a', 'col_b', 'col_c']
    """
    sel = _parse_select("SELECT x AS col_a, y AS col_b, z AS col_c FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["col_a", "col_b", "col_c"]


def test_extract_select_output_columns_bare_columns_use_name():
    """Unaliased qualified column refs use the bare column name (drop qualifier).

    SELECT d.dn_datum, s.col_x → ['dn_datum', 'col_x']
    """
    sel = _parse_select("SELECT d.dn_datum, s.col_x FROM t d JOIN s ON d.id = s.id")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["dn_datum", "col_x"]


def test_extract_select_output_columns_unqualified_bare_column():
    """Bare (unqualified) column reference → column name.

    SELECT val → ['val']
    """
    sel = _parse_select("SELECT val FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["val"]


# ---------------------------------------------------------------------------
# Star projections — skip (cannot statically resolve)
# ---------------------------------------------------------------------------


def test_extract_select_output_columns_star_yields_nothing():
    """SELECT * produces an empty list (cannot statically expand).

    This is the graceful degrade path for CTAS with SELECT *.
    """
    sel = _parse_select("SELECT * FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == []


def test_extract_select_output_columns_qualified_star_yields_nothing():
    """SELECT t.* also produces an empty list."""
    sel = _parse_select("SELECT t.* FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == []


# ---------------------------------------------------------------------------
# Unnamed literal / function — skip unless aliased
# ---------------------------------------------------------------------------


def test_extract_select_output_columns_unnamed_literal_skipped():
    """An unnamed literal expression (e.g. SELECT 1) is skipped.

    SELECT 1 AS lit → ['lit'] (alias present → included)
    SELECT 1 → [] (no alias → skipped)
    """
    # With alias: included
    sel_aliased = _parse_select("SELECT 1 AS lit FROM t")
    assert AnsiParser._extract_select_output_columns(sel_aliased) == ["lit"]

    # Without alias: skipped
    sel_bare = _parse_select("SELECT 1 FROM t")
    assert AnsiParser._extract_select_output_columns(sel_bare) == []


def test_extract_select_output_columns_unnamed_function_skipped():
    """Unnamed aggregate function without alias is skipped.

    COUNT(*) with no alias → skipped; only aliased expressions survive.
    SELECT 1 AS lit, COUNT(*) FROM s → ['lit']
    """
    sel = _parse_select("SELECT 1 AS lit, COUNT(*) FROM s")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["lit"]


def test_extract_select_output_columns_named_function_alias_included():
    """A function WITH an alias is included.

    SELECT SUM(amount) AS total_amount → ['total_amount']
    """
    sel = _parse_select("SELECT SUM(amount) AS total_amount FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["total_amount"]


# ---------------------------------------------------------------------------
# Normalization — lowercase + quote-strip
# ---------------------------------------------------------------------------


def test_extract_select_output_columns_names_lowercased():
    """All output names are lowercased.

    SELECT x AS Col_A, y AS COL_B → ['col_a', 'col_b']
    """
    sel = _parse_select('SELECT x AS "Col_A", y AS COL_B FROM t')
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["col_a", "col_b"]


def test_extract_select_output_columns_quoted_alias_unquoted():
    """Double-quoted alias: surrounding quotes are stripped, name is lowercased.

    SELECT code AS "DN_ARTIKEL_NUMMER" → ['dn_artikel_nummer']
    """
    sel = _parse_select('SELECT code AS "DN_ARTIKEL_NUMMER" FROM t', dialect="snowflake")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["dn_artikel_nummer"]


# ---------------------------------------------------------------------------
# Mixed projection lists (alias, bare col, star together)
# ---------------------------------------------------------------------------


def test_extract_select_output_columns_mixed_projection_skips_star():
    """Stars are skipped while named projections are returned.

    SELECT a AS x, *, b → ['x', 'b']
    """
    sel = _parse_select("SELECT a AS x, *, b FROM t")
    result = AnsiParser._extract_select_output_columns(sel)
    assert result == ["x", "b"]


def test_extract_select_output_columns_empty_select_returns_empty():
    """A projection list with only stars (or empty-ish) returns empty list."""
    sel = _parse_select("SELECT * FROM t")
    assert AnsiParser._extract_select_output_columns(sel) == []
