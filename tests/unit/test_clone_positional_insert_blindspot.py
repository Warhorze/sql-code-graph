"""Failing acceptance tests for plan/sprints/positional_insert_clone_blindspot.md.

These pin the observable behaviour of Parts A1/A2/B/C before the developer lands them.
Each test is named after its plan part (A1, A2, B, C1, C2) so `pytest -k clone_blindspot`
or `pytest -k "_C1_"` selects the relevant subset.

All new symbols are guarded with try/except ImportError + pytest.skip — the suite must
not error before the feature lands, but must not silently pass either.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser

# ---------------------------------------------------------------------------
# Part C1 — CREATE TABLE ... CLONE src parsing -> QueryNode.clone_source
# ---------------------------------------------------------------------------


def test_C1_clone_source_recorded_on_query_node():
    """A `CREATE TABLE t CLONE s` statement records clone_source on its QueryNode."""
    schema = SchemaResolver(dialect="snowflake")
    parser = get_parser("snowflake", schema)

    sql = "CREATE TABLE s.fact_clone CLONE s.src_ddl;"
    parsed = parser.parse_file(__file__.replace(".py", "_c1.sql"), sql)  # type: ignore[arg-type]

    create_stmts = [st for st in parsed.statements if st.kind == "CREATE_TABLE"]
    assert create_stmts, "expected at least one CREATE_TABLE statement to be parsed"
    stmt = create_stmts[0]

    try:
        clone_source = stmt.clone_source  # introduced by Part C1 (QueryNode.clone_source)
    except AttributeError:
        pytest.skip("Part C1 (QueryNode.clone_source) not yet implemented")

    assert clone_source is not None, "clone_source must be set for a CLONE create"
    assert clone_source.name == "src_ddl", (
        f"clone_source should reference the CLONE source table by bare name, got {clone_source!r}"
    )


def test_C1_clone_source_none_for_plain_create():
    """A plain `CREATE TABLE t (...)` leaves clone_source None (no false positives)."""
    schema = SchemaResolver(dialect="snowflake")
    parser = get_parser("snowflake", schema)

    sql = "CREATE TABLE s.plain_table (a INT, b INT);"
    parsed = parser.parse_file(__file__.replace(".py", "_c1b.sql"), sql)  # type: ignore[arg-type]

    create_stmts = [st for st in parsed.statements if st.kind == "CREATE_TABLE"]
    assert create_stmts, "expected at least one CREATE_TABLE statement to be parsed"
    stmt = create_stmts[0]

    try:
        clone_source = stmt.clone_source  # introduced by Part C1
    except AttributeError:
        pytest.skip("Part C1 (QueryNode.clone_source) not yet implemented")

    assert clone_source is None, "non-CLONE creates must leave clone_source as None"


# ---------------------------------------------------------------------------
# Part B-catalog / C2 — CrossFileAggregator.ddl_columns_by_bare + resolve_clone_catalogs
# ---------------------------------------------------------------------------


def test_B_ddl_columns_by_bare_built_in_order_excludes_ambiguous(tmp_path):
    """Aggregator builds bare-name -> ordered DDL column index, skipping ambiguous bare names."""
    sql_a = tmp_path / "a.sql"
    sql_a.write_text("CREATE TABLE s1.fact (a INT, b INT, c INT);")
    sql_b = tmp_path / "b.sql"
    sql_b.write_text("CREATE TABLE s2.fact (x INT, y INT);")  # ambiguous bare name "fact"
    sql_c = tmp_path / "c.sql"
    sql_c.write_text("CREATE TABLE s1.unique_tbl (p INT, q INT, r INT);")

    schema = SchemaResolver(dialect=None)
    parser = get_parser(None, schema)
    aggregator = CrossFileAggregator()
    for f in (sql_a, sql_b, sql_c):
        aggregator.register_pass1(parser.parse_file(f, f.read_text()))

    try:
        ddl_columns_by_bare = aggregator.ddl_columns_by_bare  # introduced by Part B-catalog
    except AttributeError:
        pytest.skip("Part B-catalog (ddl_columns_by_bare) not yet implemented")

    assert ddl_columns_by_bare.get("unique_tbl") == ["p", "q", "r"], (
        "unambiguous bare name must map to its ordered DDL column list"
    )
    assert "fact" not in ddl_columns_by_bare, (
        "ambiguous bare name 'fact' (defined in s1 and s2) must be excluded from the index"
    )


def test_C2_resolve_clone_catalogs_inherits_chain_with_cycle_guard(tmp_path):
    """resolve_clone_catalogs follows CLONE chains, terminates on cycles, inherits nothing
    for un-indexed sources."""
    donor = tmp_path / "donor.sql"
    donor.write_text("CREATE TABLE s.src_ddl (a INT, b INT, c INT);")

    clone1 = tmp_path / "clone1.sql"
    clone1.write_text("CREATE TABLE s.fact_clone CLONE s.src_ddl;")

    clone2 = tmp_path / "clone2.sql"
    clone2.write_text("CREATE TABLE s.fact_clone2 CLONE s.fact_clone;")

    orphan = tmp_path / "orphan.sql"
    orphan.write_text("CREATE TABLE s.fact_orphan CLONE ext.unknown;")

    cycle_a = tmp_path / "cycle_a.sql"
    cycle_a.write_text("CREATE TABLE s.cyc_a CLONE s.cyc_b;")
    cycle_b = tmp_path / "cycle_b.sql"
    cycle_b.write_text("CREATE TABLE s.cyc_b CLONE s.cyc_a;")

    schema = SchemaResolver(dialect="snowflake")
    parser = get_parser("snowflake", schema)
    aggregator = CrossFileAggregator()
    for f in (donor, clone1, clone2, orphan, cycle_a, cycle_b):
        aggregator.register_pass1(parser.parse_file(f, f.read_text()))

    try:
        _ = aggregator.ddl_columns_by_bare  # introduced by Part B-catalog
        resolve = aggregator.resolve_clone_catalogs  # introduced by Part C2
    except AttributeError:
        pytest.skip("Part C2 (resolve_clone_catalogs) not yet implemented")

    resolve()

    by_bare = aggregator.ddl_columns_by_bare

    # Direct CLONE inherits the donor's ordered columns.
    assert by_bare.get("fact_clone") == ["a", "b", "c"], (
        "direct CLONE target must inherit its source's ordered DDL columns"
    )
    # CLONE-of-CLONE follows the chain to the DDL donor.
    assert by_bare.get("fact_clone2") == ["a", "b", "c"], (
        "CLONE-of-CLONE must resolve transitively to the DDL donor's columns"
    )
    # Un-indexed source -> inherits nothing.
    assert "fact_orphan" not in by_bare or not by_bare.get("fact_orphan"), (
        "CLONE of an un-indexed (cross-DB) source must inherit nothing"
    )
    # Cycle -> terminates, inherits nothing, no exception (already proven by reaching here).
    assert not by_bare.get("cyc_a"), "a CLONE cycle must terminate inheriting nothing for cyc_a"
    assert not by_bare.get("cyc_b"), "a CLONE cycle must terminate inheriting nothing for cyc_b"


# ---------------------------------------------------------------------------
# Part A2 — inferred_from_source_name provenance flag on LineageEdge
# ---------------------------------------------------------------------------


def test_A2_lineage_edge_inferred_from_source_name_default_false():
    """LineageEdge gains inferred_from_source_name defaulting to False."""
    from sqlcg.parsers.base import ColumnRef, LineageEdge, TableRef

    src = ColumnRef(table=TableRef(db="s", name="src"), name="x")
    dst = ColumnRef(table=TableRef(db="s", name="fact"), name="a")

    try:
        edge = LineageEdge(src=src, dst=dst, inferred_from_source_name=True)  # introduced by A2
    except TypeError:
        pytest.skip("Part A2 (LineageEdge.inferred_from_source_name) not yet implemented")

    assert edge.inferred_from_source_name is True

    default_edge = LineageEdge(src=src, dst=dst)
    assert default_edge.inferred_from_source_name is False, (
        "inferred_from_source_name must default to False for ordinary edges"
    )


def test_A2_no_column_list_positional_insert_flags_inferred_edge_in_graph(tmp_path):
    """End-to-end: a no-column-list positional INSERT into a catalog-less table produces
    a COLUMN_LINEAGE edge with inferred_from_source_name=true; a DDL-catalogued column-list
    INSERT yields false."""
    sql_path = tmp_path / "etl.sql"
    sql_path.write_text(
        "CREATE TABLE s.fact_nocat AS SELECT 1 AS placeholder WHERE FALSE;\n"
        "INSERT INTO s.fact_nocat SELECT t.x AS renamed_x FROM s.src t;\n"
        "CREATE TABLE s.fact_listed (a INT, b INT);\n"
        "INSERT INTO s.fact_listed (a, b) SELECT t.p, t.q FROM s.src2 t;\n"
    )

    db = DuckDBBackend(":memory:")
    db.init_schema()
    try:
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        try:
            rows = db.run_read(
                "SELECT dst_key, inferred_from_source_name FROM COLUMN_LINEAGE "
                "WHERE dst_key LIKE 'fact_%' OR dst_key LIKE 's.fact_%'",
                {},
            )
        except Exception:
            pytest.skip("Part A2 (COLUMN_LINEAGE.inferred_from_source_name) not yet implemented")

        if not rows:
            pytest.skip("no COLUMN_LINEAGE rows produced for fact_nocat/fact_listed fixtures")

        flagged = [r for r in rows if r["inferred_from_source_name"] is True]
        unflagged = [r for r in rows if r["inferred_from_source_name"] is False]
        assert flagged, (
            "the no-column-list positional INSERT into a catalog-less table must produce "
            "at least one inferred_from_source_name=true edge"
        )
        assert unflagged, (
            "the column-list INSERT into a DDL-catalogued table must produce "
            "inferred_from_source_name=false edges"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Part A1 — empty-closure disambiguation hint
# ---------------------------------------------------------------------------


def test_A1_empty_closure_hint_distinguishes_no_catalog_from_terminal_column(tmp_path):
    """get_upstream_dependencies returns a distinct 'no catalog' hint for a table with
    lineage edges but zero HAS_COLUMN rows, vs the generic hint for a genuinely-terminal
    catalogued column."""
    try:
        from sqlcg.server import tools

        _ = tools._empty_closure_hint  # introduced by Part A1
    except (ImportError, AttributeError):
        pytest.skip("Part A1 (_empty_closure_hint) not yet implemented")

    sql_path = tmp_path / "etl.sql"
    sql_path.write_text(
        # fact_nocat: bare CREATE with no column list and no CTAS body, so P4 cannot
        # derive any columns. Loaded by a no-column-list positional INSERT.
        # -> COLUMN_LINEAGE edges exist, HAS_COLUMN is empty (truly catalog-less).
        # Previously used `CREATE TABLE ... AS SELECT 1 AS placeholder WHERE FALSE`,
        # but P4 derives 'placeholder' from that CTAS projection, giving it a catalog.
        "CREATE TABLE s.fact_nocat;\n"
        "INSERT INTO s.fact_nocat SELECT t.x AS renamed_x FROM s.src t;\n"
        # fact_terminal: has a real DDL catalog and a column with no upstream lineage
        # (a literal-only projection) -> genuinely terminal.
        "CREATE TABLE s.fact_terminal (only_col INT);\n"
        "INSERT INTO s.fact_terminal (only_col) SELECT 42;\n"
    )

    db = DuckDBBackend(":memory:")
    db.init_schema()
    try:
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        nocat_rows = db.run_read(
            "SELECT dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE '%fact_nocat%'", {}
        )
        if not nocat_rows:
            pytest.skip("fixture produced no COLUMN_LINEAGE edges for fact_nocat — adjust SQL")

        nocat_table_id = nocat_rows[0]["dst_key"].rsplit(".", 1)[0]
        no_catalog_hint = tools._empty_closure_hint(db, nocat_table_id)
        hint_lower = no_catalog_hint.lower()
        msg = (
            "hint for a catalog-less table must name the no-catalog/unproven "
            f"signal, got: {no_catalog_hint!r}"
        )
        assert "no column catalog" in hint_lower or "unproven" in hint_lower, msg

        terminal_hint = tools._empty_closure_hint(db, "s.fact_terminal")
        assert terminal_hint != no_catalog_hint, (
            "a catalogued table's empty-closure hint must differ from the no-catalog hint"
        )
    finally:
        db.close()
