"""Unit tests for catalog CSV parsing — header detection, delimiter sniff,
case-folding, quoted-field handling, and malformed-row skip counting.

Guards the PR 2 catalog-load feature
([plan doc](plan/sprints/graph_health_catalog_and_metrics.md) §4).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sqlcg.cli.commands.catalog import _sniff_delimiter, load_catalog_csv

# ---------------------------------------------------------------------------
# _sniff_delimiter
# ---------------------------------------------------------------------------


def test_sniff_delimiter_prefers_comma():
    """A header with more commas than semicolons sniffs as comma."""
    assert _sniff_delimiter("TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME") == ","


def test_sniff_delimiter_prefers_semicolon():
    """A header with more semicolons than commas sniffs as semicolon."""
    assert _sniff_delimiter("TABLE_CATALOG;TABLE_SCHEMA;TABLE_NAME;COLUMN_NAME") == ";"


def test_sniff_delimiter_tie_defaults_to_comma():
    """A header with equal commas and semicolons defaults to comma."""
    # e.g. one comma, one semicolon
    assert _sniff_delimiter("A,B;C") == ","


# ---------------------------------------------------------------------------
# Header detection — required/optional columns
# ---------------------------------------------------------------------------


def _csv_file(content: str, tmp_path: Path) -> Path:
    p = tmp_path / "cols.csv"
    p.write_text(textwrap.dedent(content).strip(), encoding="utf-8")
    return p


def test_header_detection_with_catalog_column(tmp_path: Path):
    """All four columns present — no ValueError; catalog column is ignored."""
    csv_path = _csv_file(
        """
        TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        dwh_prd,da,orders,order_id
        """,
        tmp_path,
    )
    _, col_rows, edge_rows, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 1
    assert skipped == 0
    assert len(col_rows) == 1
    # col_id should be schema.table.col — table_catalog is dropped
    assert col_rows[0]["id"] == "da.orders.order_id"
    assert edge_rows[0]["src_key"] == "da.orders"
    assert edge_rows[0]["source"] == "information_schema"


def test_header_detection_without_catalog_column(tmp_path: Path):
    """Three-column CSV without TABLE_CATALOG is accepted."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        da,orders,order_id
        """,
        tmp_path,
    )
    _, col_rows, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 1
    assert skipped == 0
    assert col_rows[0]["id"] == "da.orders.order_id"


def test_header_detection_missing_required_column_raises(tmp_path: Path):
    """Missing TABLE_SCHEMA raises ValueError with informative message."""
    csv_path = _csv_file(
        """
        TABLE_NAME,COLUMN_NAME
        orders,order_id
        """,
        tmp_path,
    )
    with pytest.raises(ValueError, match="missing required columns"):
        load_catalog_csv(csv_path)


def test_extra_columns_are_ignored(tmp_path: Path):
    """ORDINAL_POSITION and DATA_TYPE are silently ignored."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE
        da,orders,order_id,1,NUMBER
        """,
        tmp_path,
    )
    _, col_rows, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 1
    assert len(col_rows) == 1


# ---------------------------------------------------------------------------
# Case-folding
# ---------------------------------------------------------------------------


def test_case_fold_uppercased_names(tmp_path: Path):
    """UPPERCASE schema, table, and column names are folded to lower."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        BA,ORDERS,ORDER_ID
        """,
        tmp_path,
    )
    _, col_rows, edge_rows, _, _, _ = load_catalog_csv(csv_path)
    assert col_rows[0]["id"] == "ba.orders.order_id"
    assert edge_rows[0]["src_key"] == "ba.orders"


def test_case_fold_mixed_case(tmp_path: Path):
    """Mixed-case names are fully lowercased."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        Da_Tmp,Orders_V2,Total_Amount
        """,
        tmp_path,
    )
    _, col_rows, _, _, _, _ = load_catalog_csv(csv_path)
    assert col_rows[0]["id"] == "da_tmp.orders_v2.total_amount"


# ---------------------------------------------------------------------------
# Quoted fields with embedded commas and dots (Dutch BI names)
# ---------------------------------------------------------------------------


def test_quoted_field_with_embedded_comma_and_dot(tmp_path: Path):
    """Quoted column names with embedded commas and dots are parsed correctly.

    Dutch BI tables contain column names like 'Aantal order incl. verhoudingsgetaal (R, K)'
    which embed both dots and commas inside double-quoted CSV fields.
    """
    # The column name contains a comma and a dot inside quotes
    csv_path = tmp_path / "cols.csv"
    csv_path.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\n"
        'ba,fact_sales,"Aantal order incl. verhoudingsgetaal (R, K)"\n',
        encoding="utf-8",
    )
    _, col_rows, edge_rows, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 1
    assert skipped == 0
    assert len(col_rows) == 1
    # Name is lowercased; the csv module strips surrounding quotes
    assert col_rows[0]["col_name"] == "aantal order incl. verhoudingsgetaal (r, k)"
    assert col_rows[0]["id"] == "ba.fact_sales.aantal order incl. verhoudingsgetaal (r, k)"


# ---------------------------------------------------------------------------
# Delimiter sniff — semicolon
# ---------------------------------------------------------------------------


def test_semicolon_delimited_csv(tmp_path: Path):
    """Semicolon-delimited exports are parsed correctly."""
    csv_path = tmp_path / "cols.csv"
    csv_path.write_text(
        "TABLE_SCHEMA;TABLE_NAME;COLUMN_NAME\nia;dim_product;product_key\n",
        encoding="utf-8",
    )
    _, col_rows, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 1
    assert skipped == 0
    assert col_rows[0]["id"] == "ia.dim_product.product_key"


# ---------------------------------------------------------------------------
# Malformed-row skip counting
# ---------------------------------------------------------------------------


def test_malformed_rows_empty_schema_skipped(tmp_path: Path):
    """Rows with empty schema are skipped and counted in malformed_skipped."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ,orders,order_id
        da,orders,total
        """,
        tmp_path,
    )
    _, col_rows, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 2
    assert skipped == 1
    assert len(col_rows) == 1


def test_malformed_rows_empty_column_skipped(tmp_path: Path):
    """Rows with empty column name are skipped and counted."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        da,orders,
        da,orders,order_id
        """,
        tmp_path,
    )
    _, col_rows, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 2
    assert skipped == 1
    assert skipped == rows_read - len(col_rows)


def test_malformed_row_count_is_observable(tmp_path: Path):
    """The malformed_skipped count is observable (non-zero when rows are bad)."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ,,
        ,,
        da,orders,order_id
        """,
        tmp_path,
    )
    _, _, _, rows_read, skipped, _ = load_catalog_csv(csv_path)
    assert rows_read == 3
    assert skipped == 2  # observable, not just "no exception"


# ---------------------------------------------------------------------------
# Deduplication across rows
# ---------------------------------------------------------------------------


def test_duplicate_columns_are_deduplicated(tmp_path: Path):
    """Duplicate (table, column) pairs in the CSV produce only one col row each."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        da,orders,order_id
        da,orders,order_id
        da,orders,total
        """,
        tmp_path,
    )
    _, col_rows, edge_rows, rows_read, _, _ = load_catalog_csv(csv_path)
    assert rows_read == 3
    ids = [r["id"] for r in col_rows]
    assert len(ids) == len(set(ids)), "Duplicate col_ids present"
    assert len(col_rows) == 2  # order_id + total


def test_duplicate_tables_produce_one_table_row(tmp_path: Path):
    """Multiple columns on the same table produce exactly one SqlTable row."""
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        da,orders,order_id
        da,orders,total
        da,orders,customer_id
        """,
        tmp_path,
    )
    table_rows, _, _, _, _, _ = load_catalog_csv(csv_path)
    assert len(table_rows) == 1
    assert table_rows[0]["qualified"] == "da.orders"


# ---------------------------------------------------------------------------
# Schema-alias folding (PR 2 of v1.14.0 dialect-and-query-config fix batch)
# ---------------------------------------------------------------------------


def test_schema_alias_folds_staging_schema_to_canonical(tmp_path: Path):
    """A row in an aliased staging schema (ba_tmp) folds to its canonical
    schema (ba) before the qualified name is built.

    Guards Fix 2 of the dialect-and-query-config fix batch
    ([plan doc](plan/sprints/v1.14.0_dialect_and_query_config_fixes.md) §Fix 2).
    """
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ba_tmp,foo,col1
        """,
        tmp_path,
    )
    table_rows, col_rows, edge_rows, _, _, folded_rows = load_catalog_csv(
        csv_path, schema_aliases={"ba_tmp": "ba"}
    )
    assert folded_rows == 1
    assert table_rows[0]["qualified"] == "ba.foo"
    assert col_rows[0]["id"] == "ba.foo.col1"
    assert edge_rows[0]["src_key"] == "ba.foo"


def test_schema_alias_dedup_when_both_canonical_and_staging_present(tmp_path: Path):
    """A CSV containing both ba.x.c and ba_tmp.x.c (which folds to ba.x.c)
    collapses to a single canonical column row (D2.3 first-wins dedup).

    Guards Fix 2 of the dialect-and-query-config fix batch
    ([plan doc](plan/sprints/v1.14.0_dialect_and_query_config_fixes.md) §Fix 2 D2.3).
    """
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ba,x,c
        ba_tmp,x,c
        """,
        tmp_path,
    )
    table_rows, col_rows, edge_rows, rows_read, skipped, folded_rows = load_catalog_csv(
        csv_path, schema_aliases={"ba_tmp": "ba"}
    )
    assert rows_read == 2
    assert skipped == 0
    assert folded_rows == 1

    qualified_names = {r["qualified"] for r in table_rows}
    assert qualified_names == {"ba.x"}, f"Expected only ba.x, got {qualified_names!r}"

    col_ids = [r["id"] for r in col_rows]
    assert col_ids == ["ba.x.c"], f"Expected single deduped ba.x.c, got {col_ids!r}"

    edge_dst_keys = [r["dst_key"] for r in edge_rows]
    assert edge_dst_keys == ["ba.x.c"]


def test_schema_alias_only_staging_rows_produce_no_phantom_tables(tmp_path: Path):
    """A CSV consisting solely of *_tmp staging-schema rows produces zero
    *_tmp table rows once folded — the measured headline bug (2,907 phantom
    *_tmp tables, ~41% of all table nodes on the DWH).

    Guards Fix 2 of the dialect-and-query-config fix batch
    ([plan doc](plan/sprints/v1.14.0_dialect_and_query_config_fixes.md) §Fix 2).
    """
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ba_tmp,foo,col1
        ba_tmp,foo,col2
        ba_tmp,bar,col1
        """,
        tmp_path,
    )
    table_rows, col_rows, _, rows_read, skipped, folded_rows = load_catalog_csv(
        csv_path, schema_aliases={"ba_tmp": "ba"}
    )
    assert rows_read == 3
    assert skipped == 0
    assert folded_rows == 3

    qualified_names = {r["qualified"] for r in table_rows}
    assert qualified_names == {"ba.foo", "ba.bar"}
    assert not any(name.startswith("ba_tmp") for name in qualified_names), (
        f"No ba_tmp.* phantom table rows expected, got {qualified_names!r}"
    )

    col_ids = {r["id"] for r in col_rows}
    assert not any(cid.startswith("ba_tmp") for cid in col_ids), (
        f"No ba_tmp.* phantom column rows expected, got {col_ids!r}"
    )


def test_schema_alias_empty_map_is_verbatim(tmp_path: Path):
    """No schema_aliases (default empty dict) leaves rows unfolded — the
    zero-config path stays byte-identical to today's behaviour.

    Guards Fix 2 of the dialect-and-query-config fix batch
    ([plan doc](plan/sprints/v1.14.0_dialect_and_query_config_fixes.md) §Fix 2).
    """
    csv_path = _csv_file(
        """
        TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME
        ba_tmp,foo,col1
        """,
        tmp_path,
    )
    table_rows, col_rows, _, _, _, folded_rows = load_catalog_csv(csv_path)
    assert folded_rows == 0
    assert table_rows[0]["qualified"] == "ba_tmp.foo"
    assert col_rows[0]["id"] == "ba_tmp.foo.col1"
