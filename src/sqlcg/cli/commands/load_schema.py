"""Load INFORMATION_SCHEMA CSV into the graph as authoritative HAS_COLUMN edges.

Sprint: sprint_star_resolution.md  Ticket: T-08

Provides a CLI command to load production column schemas from an INFORMATION_SCHEMA
export, making them authoritative (superseding DDL-inferred columns). This enables
accurate star projection resolution when DDL files are incomplete or unavailable.
"""

import csv
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from sqlcg.core.config import get_backend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)
console = Console()


def _make_qualified(catalog: str, schema: str, table: str, include_catalog: bool) -> str:
    """Construct a lowercased qualified table name from INFORMATION_SCHEMA columns.

    All parts are lowercased before joining. Empty strings and None values are
    omitted. The default (include_catalog=False) produces schema.table names
    matching the graph's convention (e.g. 'BA.src'). Pass include_catalog=True
    for 3-part references (catalog.schema.table).

    Args:
        catalog: TABLE_CATALOG value (may be empty)
        schema: TABLE_SCHEMA value (may be empty)
        table: TABLE_NAME value (required)
        include_catalog: If True, prefix with lowercased catalog; if False, omit it

    Returns:
        Lowercased, dot-separated qualified table name
    """
    parts = [p for p in ([catalog] if include_catalog else []) + [schema, table] if p]
    return ".".join(p.lower() for p in parts)


def _load_schema_into_graph(csv_path: Path, include_catalog: bool, db: Any) -> tuple[int, int]:
    """Internal implementation of load-schema logic.

    Args:
        csv_path: Path to INFORMATION_SCHEMA.COLUMNS CSV file
        include_catalog: If True, use 3-part qualified names (catalog.schema.table)
        db: KuzuBackend instance (required)

    Raises:
        ValueError: If CSV is missing required columns
        FileNotFoundError: If CSV file doesn't exist
    """
    required = {"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION"}
    tables: dict[str, list[tuple[int, str]]] = {}

    # Read and parse the CSV
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = required - set(reader.fieldnames or [])
            raise ValueError(f"CSV missing required columns: {missing}")
        for row in reader:
            qualified = _make_qualified(
                row["TABLE_CATALOG"],
                row["TABLE_SCHEMA"],
                row["TABLE_NAME"],
                include_catalog=include_catalog,
            )
            tables.setdefault(qualified, []).append(
                (int(row["ORDINAL_POSITION"]), row["COLUMN_NAME"])
            )

    cols_written = 0
    for qualified, raw_cols in tables.items():
        cols = [c for _, c in sorted(raw_cols)]
        name_parts = qualified.rsplit(".", maxsplit=1)
        table_name = name_parts[-1]
        db_part = name_parts[0] if len(name_parts) == 2 else ""

        # Upsert SqlTable node
        db.upsert_node(
            NodeLabel.TABLE,
            qualified,
            {
                "qualified": qualified,
                "name": table_name,
                "catalog": "",
                "db": db_part,
                "kind": "TABLE",
                "defined_in_file": "",
            },
        )

        # Upsert SqlColumn nodes and HAS_COLUMN edges
        for col_name in cols:
            col_id = f"{qualified}.{col_name}"
            db.upsert_node(
                NodeLabel.COLUMN,
                col_id,
                {
                    "id": col_id,
                    "col_name": col_name,
                    "table_qualified": qualified,
                    "catalog": "",
                    "db": db_part,
                    "table_name": table_name,
                },
            )
            db.upsert_edge(
                NodeLabel.TABLE,
                qualified,
                NodeLabel.COLUMN,
                col_id,
                RelType.HAS_COLUMN,
                {"source": "information_schema"},
            )
            cols_written += 1

    logger.info("Loaded %d tables, %d columns from %s", len(tables), cols_written, csv_path)
    return len(tables), cols_written


def load_schema_cmd(  # noqa: B008
    csv_path: Path = typer.Argument(..., help="Path to INFORMATION_SCHEMA.COLUMNS CSV"),  # noqa: B008
    include_catalog: bool = typer.Option(  # noqa: B008
        False,
        "--include-catalog",
        help="Prefix qualified names with TABLE_CATALOG (use for 3-part references).",
    ),
) -> None:
    """Load production column schema from an INFORMATION_SCHEMA CSV.

    Writes HAS_COLUMN edges tagged source='information_schema'. Run this before
    'sqlcg index' so DDL-inferred columns are suppressed for covered tables.
    Idempotent: safe to run multiple times.

    For automatic pickup, place the CSV at <repo>/.sqlcg/schema.csv — 'sqlcg index'
    will load it without needing this command explicitly.

    Generate the CSV from Snowflake:

        SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME,
               COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
        ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;

    Export as CSV and drop at .sqlcg/schema.csv in your SQL repo root.

    Args:
        csv_path: Path to INFORMATION_SCHEMA.COLUMNS CSV file
        include_catalog: If True, use 3-part qualified names (catalog.schema.table)

    Raises:
        SystemExit: On any error (via typer.Exit)
    """
    backend = get_backend()

    try:
        with backend:
            _load_schema_into_graph(csv_path, include_catalog, backend)
        console.print(f"[green]Loaded[/green] schema from {csv_path}")
    except FileNotFoundError as e:
        console.print(f"[red]CSV file not found: {csv_path}[/red]")
        raise typer.Exit(1) from e
    except ValueError as e:
        console.print(f"[red]CSV error: {e}[/red]")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Error writing to database: {e}[/red]")
        logger.exception("Failed to load schema CSV")
        raise typer.Exit(1) from e


# Testable version that accepts db parameter
def load_schema_cmd_test(
    csv_path: Path | str, include_catalog: bool = False, db: Any | None = None
) -> None:
    """Load schema command with optional db parameter for testing.

    This variant allows tests to pass a KuzuBackend instance directly,
    bypassing get_backend().

    Args:
        csv_path: Path to INFORMATION_SCHEMA.COLUMNS CSV file
        include_catalog: If True, use 3-part qualified names
        db: KuzuBackend instance (for testing)
    """
    if db is None:
        backend = get_backend()
        use_context = True
    else:
        backend = db
        use_context = False

    try:
        if use_context:
            with backend:
                _load_schema_into_graph(Path(csv_path), include_catalog, backend)
        else:
            _load_schema_into_graph(Path(csv_path), include_catalog, backend)
        if use_context:
            console.print(f"[green]Loaded[/green] schema from {csv_path}")
    except FileNotFoundError as e:
        if use_context:
            console.print(f"[red]CSV file not found: {csv_path}[/red]")
            raise typer.Exit(1) from e
        raise
    except ValueError as e:
        if use_context:
            console.print(f"[red]CSV error: {e}[/red]")
            raise typer.Exit(1) from e
        raise
