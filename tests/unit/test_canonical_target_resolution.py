"""Unit tests for #44 INSERT-target canonical-name resolution.

Exercises the _build_file_rows path in Indexer: when a canonical_by_bare index
is supplied, unqualified INSERT targets whose bare name maps unambiguously to a
DDL-defined table are rewritten to the canonical full_id with kind='table'.
INSERT targets with no canonical match are emitted with kind='derived'.
Ambiguous bare names (defined in >1 schema) are left untouched.

These tests drive _build_file_rows directly (pure, no db) and assert on the
emitted table_rows dicts — no graph backend required.
"""

from __future__ import annotations

from pathlib import Path

from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.parsers.base import ParsedFile, QueryNode, TableRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parsed_with_insert_target(
    tmp_path: Path,
    target_name: str,
    target_db: str = "",
    target_catalog: str = "",
) -> ParsedFile:
    """Build a minimal ParsedFile whose single statement has an INSERT target."""
    f = tmp_path / "test.sql"
    f.write_text(f"INSERT INTO {target_db + '.' if target_db else ''}{target_name} SELECT 1 AS x;")
    pf = ParsedFile(path=f, dialect=None)
    stmt = QueryNode(
        file=f,
        statement_index=0,
        sql="INSERT INTO ...",
        kind="INSERT",
        target=TableRef(catalog=target_catalog, db=target_db, name=target_name),
        sources=[],
    )
    pf.statements = [stmt]
    pf.defined_tables = []
    return pf


def _make_ddl_parsed(
    tmp_path: Path,
    table_name: str,
    table_db: str,
    table_catalog: str = "",
) -> ParsedFile:
    """Build a minimal ParsedFile with a single DDL-defined table."""
    f = tmp_path / f"ddl_{table_name}.sql"
    full = f"{table_db}.{table_name}" if table_db else table_name
    f.write_text(f"CREATE TABLE {full} (x INT);")
    pf = ParsedFile(path=f, dialect=None)
    pf.defined_tables = [TableRef(catalog=table_catalog, db=table_db, name=table_name)]
    pf.statements = []
    return pf


def _table_rows_for_qualified(rows: list[dict], qualified: str) -> list[dict]:
    return [r for r in rows if r["qualified"] == qualified]


# ---------------------------------------------------------------------------
# Test 1: sole DDL match → rewrite to canonical, kind='table'
# ---------------------------------------------------------------------------


def test_insert_target_rewritten_to_canonical_full_id(tmp_path):
    """When bare name maps to a single DDL canonical, the emitted row uses the canonical full_id.

    Scenario: DDL defines 'mart.fact_kpi'; INSERT target is 'ba_tmp.fact_kpi'.
    Expected: table_rows contains a row with qualified='mart.fact_kpi', kind='table'.
    The schema-less duplicate 'ba_tmp.fact_kpi' must not be emitted.
    """
    # DDL file defines mart.fact_kpi
    ddl_parsed = _make_ddl_parsed(tmp_path, table_name="fact_kpi", table_db="mart")
    aggregator = CrossFileAggregator()
    aggregator.register_pass1(ddl_parsed)

    # ETL file inserts into ba_tmp.fact_kpi (wrong schema)
    etl_parsed = _make_parsed_with_insert_target(tmp_path, "fact_kpi", target_db="ba_tmp")

    indexer = Indexer()
    file_rows = indexer._build_file_rows(
        etl_parsed,
        defined_table_registry=None,
        canonical_by_bare=aggregator.canonical_by_bare,
        ambiguous_bare=aggregator._ambiguous_bare,
    )

    table_qualifieds = [r["qualified"] for r in file_rows.table_rows]

    # Canonical node must be emitted
    assert "mart.fact_kpi" in table_qualifieds, (
        f"Canonical 'mart.fact_kpi' not found in table_rows.\n"
        f"Emitted qualifieds: {table_qualifieds}"
    )

    # The wrong-schema duplicate must NOT be emitted
    assert "ba_tmp.fact_kpi" not in table_qualifieds, (
        f"Wrong-schema duplicate 'ba_tmp.fact_kpi' was emitted — rewrite did not apply.\n"
        f"Emitted qualifieds: {table_qualifieds}"
    )

    # The canonical row must have kind='table'
    canonical_rows = _table_rows_for_qualified(file_rows.table_rows, "mart.fact_kpi")
    assert any(r["kind"] == "table" for r in canonical_rows), (
        f"Canonical row has unexpected kind: {canonical_rows}"
    )


# ---------------------------------------------------------------------------
# Test 2: ambiguous bare name → not rewritten
# ---------------------------------------------------------------------------


def test_ambiguous_bare_name_not_rewritten(tmp_path):
    """When two schemas define the same bare name, the INSERT target is left as-is.

    Scenario: both 'mart.fact_kpi' and 'staging.fact_kpi' exist; INSERT target is
    'ba_tmp.fact_kpi'.  Since the bare name 'fact_kpi' is ambiguous, no rewrite happens.
    """
    ddl_a = _make_ddl_parsed(tmp_path, table_name="fact_kpi", table_db="mart")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    ddl_b = _make_ddl_parsed(subdir, table_name="fact_kpi", table_db="staging")

    aggregator = CrossFileAggregator()
    aggregator.register_pass1(ddl_a)
    aggregator.register_pass1(ddl_b)

    # fact_kpi should now be in _ambiguous_bare
    assert "fact_kpi" in aggregator._ambiguous_bare, (
        f"Expected 'fact_kpi' in _ambiguous_bare. canonical_by_bare={aggregator.canonical_by_bare}"
    )

    etl_parsed = _make_parsed_with_insert_target(tmp_path, "fact_kpi", target_db="ba_tmp")

    indexer = Indexer()
    file_rows = indexer._build_file_rows(
        etl_parsed,
        defined_table_registry=None,
        canonical_by_bare=aggregator.canonical_by_bare,
        ambiguous_bare=aggregator._ambiguous_bare,
    )
    table_qualifieds = [r["qualified"] for r in file_rows.table_rows]

    # Ambiguous — original is NOT rewritten to either canonical
    assert "mart.fact_kpi" not in table_qualifieds or "staging.fact_kpi" not in table_qualifieds, (
        "Ambiguous bare name should not be rewritten to either schema-qualified canonical."
    )
    # Since ambiguous, target is emitted as 'derived' (not 'table') — no silent merge
    ba_tmp_rows = _table_rows_for_qualified(file_rows.table_rows, "ba_tmp.fact_kpi")
    if ba_tmp_rows:
        assert all(r["kind"] == "derived" for r in ba_tmp_rows), (
            f"Ambiguous target should be kind='derived', got: {ba_tmp_rows}"
        )


# ---------------------------------------------------------------------------
# Test 3: no canonical match → kind='derived'
# ---------------------------------------------------------------------------


def test_unresolved_insert_target_emitted_as_derived(tmp_path):
    """When no DDL canonical exists for the bare name, INSERT target is kind='derived'.

    Scenario: INSERT target 'ba_tmp.synthetic_cte' has no corresponding DDL table.
    The emitted row must have kind='derived' so the read-surface filter excludes it.
    """
    aggregator = CrossFileAggregator()  # empty — no DDL tables registered

    etl_parsed = _make_parsed_with_insert_target(tmp_path, "synthetic_cte", target_db="ba_tmp")

    indexer = Indexer()
    file_rows = indexer._build_file_rows(
        etl_parsed,
        defined_table_registry=None,
        canonical_by_bare=aggregator.canonical_by_bare,
        ambiguous_bare=aggregator._ambiguous_bare,
    )
    table_rows_for_target = [
        r for r in file_rows.table_rows if r["qualified"] == "ba_tmp.synthetic_cte"
    ]

    assert table_rows_for_target, (
        "No table_row emitted for 'ba_tmp.synthetic_cte' — INSERT target not emitted at all."
    )
    assert all(r["kind"] == "derived" for r in table_rows_for_target), (
        f"Unresolved INSERT target should be kind='derived', got: {table_rows_for_target}"
    )


# ---------------------------------------------------------------------------
# Test 4: without canonical index (None), old behaviour preserved
# ---------------------------------------------------------------------------


def test_without_canonical_index_old_behaviour(tmp_path):
    """When canonical_by_bare=None (single-file / reindex_file path), no rewrite occurs.

    This is the degradation path: reindex_file does not thread the aggregator,
    so the canonical-name rewrite is a no-op and kind defaults to 'table'.
    """
    etl_parsed = _make_parsed_with_insert_target(tmp_path, "fact_kpi", target_db="ba_tmp")

    indexer = Indexer()
    file_rows = indexer._build_file_rows(
        etl_parsed,
        defined_table_registry=None,
        canonical_by_bare=None,  # no index — degrade to no-op
        ambiguous_bare=None,
    )
    table_qualifieds = [r["qualified"] for r in file_rows.table_rows]

    # Original target is emitted unchanged with kind='table'
    assert "ba_tmp.fact_kpi" in table_qualifieds, (
        f"Original target 'ba_tmp.fact_kpi' not emitted with canonical_by_bare=None.\n"
        f"Emitted: {table_qualifieds}"
    )
    orig_rows = _table_rows_for_qualified(file_rows.table_rows, "ba_tmp.fact_kpi")
    assert any(r["kind"] == "table" for r in orig_rows), (
        f"Without canonical_by_bare, INSERT target should have kind='table': {orig_rows}"
    )


# ---------------------------------------------------------------------------
# Test 5: emitted canonical row's qualified == DDL full_id
# ---------------------------------------------------------------------------


def test_emitted_canonical_qualified_matches_ddl_full_id(tmp_path):
    """The emitted row's 'qualified' must be identical to the DDL table's full_id.

    Ensures the SqlTable node, SqlColumn.table_qualified, and the DDL node all share
    the same identity (the CLAUDE.md alias rule for #39 Half A discipline).
    """
    ddl_parsed = _make_ddl_parsed(tmp_path, table_name="orders", table_db="sales")
    aggregator = CrossFileAggregator()
    aggregator.register_pass1(ddl_parsed)

    # DDL table full_id should be 'sales.orders'
    assert aggregator.canonical_by_bare.get("orders") == "sales.orders", (
        f"Unexpected canonical_by_bare entry: {aggregator.canonical_by_bare}"
    )

    # INSERT target is schema-less 'orders'
    etl_parsed = _make_parsed_with_insert_target(tmp_path, "orders")

    indexer = Indexer()
    file_rows = indexer._build_file_rows(
        etl_parsed,
        defined_table_registry=None,
        canonical_by_bare=aggregator.canonical_by_bare,
        ambiguous_bare=aggregator._ambiguous_bare,
    )
    table_qualifieds = [r["qualified"] for r in file_rows.table_rows]

    assert "sales.orders" in table_qualifieds, (
        f"Canonical full_id 'sales.orders' not in emitted table_rows.\nEmitted: {table_qualifieds}"
    )
    assert "orders" not in table_qualifieds or any(
        r["qualified"] == "sales.orders" for r in file_rows.table_rows
    ), "Schema-less 'orders' emitted instead of canonical 'sales.orders'."
