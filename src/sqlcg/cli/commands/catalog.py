"""Catalog enrichment commands — load INFORMATION_SCHEMA column metadata."""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from sqlcg.core.config import get_backend, get_schema_aliases
from sqlcg.core.graph_db import indexed_repo_root
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

app = typer.Typer(help="Catalog enrichment commands")
console = Console()

# Required CSV columns (lower-cased after header detection).
_REQUIRED_COLS: frozenset[str] = frozenset({"table_schema", "table_name", "column_name"})
_OPTIONAL_COLS: frozenset[str] = frozenset({"table_catalog"})


def _sniff_delimiter(header_line: str) -> str:
    """Sniff CSV delimiter from the header line.

    Returns ',' or ';'.  Defaults to ',' when neither produces more than one
    column (single-column headers are treated as comma-delimited).
    """
    n_comma = header_line.count(",")
    n_semi = header_line.count(";")
    return ";" if n_semi > n_comma else ","


def load_catalog_csv(
    csv_path: Path,
    schema_aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int, int, int]:
    """Parse a INFORMATION_SCHEMA.COLUMNS CSV export into bulk-upsert row lists.

    Design (plan §4 §1):
    - Header-detected columns: ``table_catalog`` (optional), ``table_schema``,
      ``table_name``, ``column_name`` required; extra columns ignored.
    - Delimiter sniffed (comma or semicolon).
    - Names case-folded to lower to match graph keys.
    - Quoted fields with embedded commas/dots handled via Python's csv module.
    - Single catalog component (``table_catalog``) dropped from graph keys —
      graph keys are ``schema.table`` (two-part, lower-cased).

    ``schema_aliases`` (e.g. ``{"ba_tmp": "ba"}``, from
    ``[sqlcg.schema_aliases]`` in ``.sqlcg.toml``) folds staging-schema rows
    onto their canonical schema **before** dedup/qualified-name construction,
    so ``ba_tmp.foo`` rows upsert as ``ba.foo`` — the same identity the
    lineage edges already use. Defaults to an empty dict (no folding, today's
    verbatim behaviour).

    Returns:
        (table_rows, column_rows, has_column_edges, rows_read, malformed_skipped,
        folded_rows) where:
          - table_rows: SqlTable upsert dicts (bare, ``kind='table'``, ``defined_in_file=''``)
          - column_rows: SqlColumn upsert dicts
          - has_column_edges: HAS_COLUMN edge dicts with ``source='information_schema'``
          - rows_read: total data rows read (excl. header)
          - malformed_skipped: rows skipped for missing required fields
          - folded_rows: rows whose schema was rewritten via ``schema_aliases``
    """
    schema_aliases = schema_aliases or {}
    raw = csv_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    if not lines:
        raise ValueError(f"Catalog CSV is empty: {csv_path}")

    header_line = lines[0]
    delimiter = _sniff_delimiter(header_line)

    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    if reader.fieldnames is None:
        raise ValueError(f"Could not read header from catalog CSV: {csv_path}")

    # Case-fold field names for header detection.
    field_map: dict[str, str] = {f.strip().lower(): f for f in reader.fieldnames}
    missing = _REQUIRED_COLS - set(field_map)
    if missing:
        raise ValueError(
            f"Catalog CSV missing required columns: {sorted(missing)}. Found: {sorted(field_map)}"
        )

    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    has_column_edges: list[dict[str, Any]] = []
    rows_read = 0
    malformed_skipped = 0
    folded_rows = 0

    # Dedup tables — emit one SqlTable row per qualified table.
    seen_tables: set[str] = set()
    # Dedup columns — emit one SqlColumn row per column id.
    seen_col_ids: set[str] = set()

    # table_catalog field is optional and dropped; record presence for clarity.
    # (Used only for the "has_catalog_col" header-check path — value ignored.)
    schema_field = field_map["table_schema"]
    name_field = field_map["table_name"]
    col_field = field_map["column_name"]

    for row in reader:
        rows_read += 1

        schema_raw = row.get(schema_field, "") or ""
        table_raw = row.get(name_field, "") or ""
        col_raw = row.get(col_field, "") or ""

        # Case-fold to lower — graph keys are lower-cased.
        schema = schema_raw.strip().lower()
        table = table_raw.strip().lower()
        col = col_raw.strip().lower()

        # Fold staging schema onto its canonical schema (e.g. ba_tmp -> ba) before
        # building the qualified name, so folded rows share dedup/precedence with
        # their canonical counterparts (D2.2/D2.3).
        if schema in schema_aliases:
            schema = schema_aliases[schema]
            folded_rows += 1

        if not schema or not table or not col:
            malformed_skipped += 1
            logger.debug(
                "catalog load: skipping malformed row %d: schema=%r table=%r col=%r",
                rows_read,
                schema,
                table,
                col,
            )
            continue

        # Two-part qualified name: schema.table (catalog component dropped).
        table_qualified = f"{schema}.{table}"
        col_id = f"{table_qualified}.{col}"

        # SqlTable row (bare — kind='table', defined_in_file='').
        # upsert_nodes_bulk for TABLE uses ON CONFLICT DO UPDATE that only
        # overwrites kind/defined_in_file when they are empty, so existing rows
        # with real kind/defined_in_file are never downgraded.
        if table_qualified not in seen_tables:
            seen_tables.add(table_qualified)
            table_rows.append(
                {
                    "qualified": table_qualified,
                    "catalog": "",
                    "db": schema,
                    "name": table,
                    "kind": "table",
                    "defined_in_file": "",
                }
            )

        # SqlColumn row.
        if col_id not in seen_col_ids:
            seen_col_ids.add(col_id)
            column_rows.append(
                {
                    "id": col_id,
                    "col_name": col,
                    "table_qualified": table_qualified,
                    "catalog": "",
                    "db": schema,
                    "table_name": table,
                }
            )
            has_column_edges.append(
                {
                    "src_key": table_qualified,
                    "dst_key": col_id,
                    "source": "information_schema",
                }
            )

    return table_rows, column_rows, has_column_edges, rows_read, malformed_skipped, folded_rows


def apply_catalog_to_backend(
    csv_path: Path,
    backend: Any,
    schema_aliases: dict[str, str] | None = None,
) -> dict[str, int]:
    """Load catalog CSV rows into the graph backend.

    Emits SqlTable + SqlColumn + HAS_COLUMN(source='information_schema') via one
    bulk call per type — bulk-upsert invariant (CLAUDE.md) preserved.  The
    HAS_COLUMN precedence upsert in ``upsert_edges_bulk`` ensures ddl wins over
    information_schema wins over usage.

    Args:
        csv_path: Path to the INFORMATION_SCHEMA.COLUMNS CSV file.
        backend:  An open GraphBackend instance (DuckDBBackend).
        schema_aliases: Optional staging-schema -> canonical-schema map (from
            ``[sqlcg.schema_aliases]``); folds rows like ``ba_tmp.*`` onto
            ``ba.*`` before upsert. Defaults to no folding.

    Returns:
        Dict with keys: rows_read, malformed_skipped, tables_loaded, columns_loaded,
        folded_rows.
    """
    table_rows, column_rows, has_column_edges, rows_read, malformed_skipped, folded_rows = (
        load_catalog_csv(csv_path, schema_aliases=schema_aliases)
    )

    # SqlTable: INSERT OR IGNORE — never overwrite existing rows with real kind/
    # defined_in_file (plan §4 §1: "do NOT overwrite kind/defined_in_file of existing
    # tables").  SqlColumn + HAS_COLUMN: one bulk call each — precedence-aware upsert
    # in upsert_edges_bulk handles the ddl > information_schema > usage conflict.
    backend.insert_table_nodes_if_absent(table_rows)
    backend.upsert_nodes_bulk(NodeLabel.COLUMN, column_rows)
    backend.upsert_edges_bulk(
        NodeLabel.TABLE,
        NodeLabel.COLUMN,
        RelType.HAS_COLUMN,
        has_column_edges,
    )

    # Catalog-aware kind upgrade (A4, PR 4, sprint_postmortem_fixes.md §Step 4.2):
    # After info-schema rows are upserted, upgrade any SqlTable rows that are still
    # mis-kinded as 'derived' (DDL in un-indexed Liquibase XML etc.) to 'table'.
    # WHERE kind='derived' guard ensures view/table rows are never touched.
    catalogued_keys = [r["qualified"] for r in table_rows]
    upgraded = backend.upgrade_derived_to_table_for_keys(catalogued_keys)

    return {
        "rows_read": rows_read,
        "malformed_skipped": malformed_skipped,
        "tables_loaded": len(table_rows),
        "columns_loaded": len(column_rows),
        "folded_rows": folded_rows,
        "derived_upgraded": upgraded,
    }


@app.command("load")
def catalog_load(
    file: Path = typer.Argument(..., help="Path to INFORMATION_SCHEMA.COLUMNS CSV export"),  # noqa: B008
) -> None:
    """Load INFORMATION_SCHEMA column metadata from a CSV export.

    Enriches the graph with SqlColumn nodes and HAS_COLUMN edges sourced from
    ``information_schema``.  Idempotent — a second load produces no row growth.
    DDL-sourced rows are never overwritten (ddl > information_schema > usage
    precedence).

    Expected CSV columns (delimiter sniffed; extra columns ignored):
      table_catalog (optional), table_schema, table_name, column_name

    Names are case-folded to lower to match graph keys.
    """
    if not file.exists():
        console.print(f"[red]Error:[/red] file not found: {file}")
        raise typer.Exit(1)

    t0 = time.perf_counter()

    try:
        with get_backend() as backend:
            repo_root = indexed_repo_root(backend) or Path.cwd()
            schema_aliases = get_schema_aliases(repo_root)
            result = apply_catalog_to_backend(file, backend, schema_aliases=schema_aliases)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    elapsed = time.perf_counter() - t0

    console.print(
        f"[green]Catalog loaded[/green] in {elapsed:.2f}s — "
        f"{result['rows_read']} rows read, "
        f"{result['malformed_skipped']} malformed skipped, "
        f"{result['tables_loaded']} tables, "
        f"{result['columns_loaded']} columns."
    )
    if result["folded_rows"] > 0:
        console.print(
            f"{result['folded_rows']} rows folded via schema alias "
            f"({', '.join(f'{src} -> {dst}' for src, dst in schema_aliases.items())})."
        )
    if result["malformed_skipped"] > 0:
        console.print(
            f"[yellow]Warning:[/yellow] {result['malformed_skipped']} rows were skipped "
            "due to empty schema, table, or column name fields."
        )
