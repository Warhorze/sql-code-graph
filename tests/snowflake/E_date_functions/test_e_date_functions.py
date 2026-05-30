"""E_date_functions: Date function expressions in INSERTs and views.

T-09-03 introduced col_lineage_skip:func_fallback for *unaliased* function
expressions that have no output column name (a plain SELECT / CREATE VIEW).

#25 added positional INSERT mapping: `INSERT INTO t (a) SELECT DATE(col)` DOES
have an output column name — the INSERT column list supplies it (`a`). So these
unaliased-function-in-positional-INSERT cases now RESOLVE the source column
(col -> a), exactly like the aliased form `DATE(col) AS a`. func_fallback is only
correct when there is genuinely no output name (covered by test_t02 CREATE VIEW
cases). See base.py positional INSERT block (the removed "Guard 3").
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_date_unaliased_in_positional_insert_resolves(parser):
    """Unaliased DATE in a positional INSERT resolves col -> insert column (no func_fallback).

    Fixture: INSERT INTO db.s.t (a) SELECT DATE(boekdatum_fis) FROM db.s.src;
    The INSERT column list supplies the output name `a`, so the source column resolves.
    """
    sql = Path(__file__).with_name("fixture_date_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_date_unaliased.sql")

    all_edges = edges(result)
    assert any("BOEKDATUM_FIS" in e[1].upper() and e[3].upper() == "A" for e in all_edges), (
        f"Expected boekdatum_fis -> a edge from positional INSERT, got {all_edges}"
    )

    # The INSERT column supplies the output name — no func_fallback skip should be emitted.
    assert not any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Unexpected func_fallback skip — positional INSERT supplies the name: {result.errors}"
    )
    # Should NOT emit E2 (col_lineage: Cannot find column)
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"


def test_date_aliased_produces_edge(parser):
    """Aliased DATE function produces edge for the source column."""
    sql = Path(__file__).with_name("fixture_date_aliased.sql").read_text()
    result = parse(parser, sql, "fixture_date_aliased.sql")

    all_edges = edges(result)
    assert len(all_edges) >= 1, f"Expected at least 1 edge for aliased DATE, got {all_edges}"

    # Should have edge from boekdatum_fis to d (note: column names may be quoted)
    assert any("BOEKDATUM_FIS" in e[1].upper() and e[3].upper() == "D" for e in all_edges), (
        f"Expected boekdatum_fis -> d edge in {all_edges}"
    )


def test_year_unaliased_in_positional_insert_resolves(parser):
    """Unaliased YEAR in a positional INSERT resolves boekdatum_fis -> a (no func_fallback)."""
    sql = Path(__file__).with_name("fixture_year_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_year_unaliased.sql")

    all_edges = edges(result)
    assert any("BOEKDATUM_FIS" in e[1].upper() and e[3].upper() == "A" for e in all_edges), (
        f"Expected boekdatum_fis -> a edge from positional INSERT, got {all_edges}"
    )
    assert not any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Unexpected func_fallback skip — positional INSERT supplies the name: {result.errors}"
    )
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"


def test_datediff_unaliased_in_positional_insert_resolves(parser):
    """Unaliased DATEDIFF in a positional INSERT resolves its source columns -> a.

    Fixture: INSERT INTO db.s.t (a) SELECT DATEDIFF(day, d1, d2) FROM db.s.src;
    Both date arguments (d1, d2) are real source columns and resolve to `a`.
    """
    sql = Path(__file__).with_name("fixture_datediff_unaliased.sql").read_text()
    result = parse(parser, sql, "fixture_datediff_unaliased.sql")

    all_edges = edges(result)
    resolved_srcs = {e[1].upper() for e in all_edges if e[3].upper() == "A"}
    assert {"D1", "D2"} & resolved_srcs, (
        f"Expected d1/d2 -> a edges from positional INSERT, got {all_edges}"
    )
    assert not any("col_lineage_skip:func_fallback" in err for err in result.errors), (
        f"Unexpected func_fallback skip — positional INSERT supplies the name: {result.errors}"
    )
    assert not any(
        "col_lineage:" in err and "Cannot find column" in err for err in result.errors
    ), f"Unexpected E2 error in {result.errors}"
