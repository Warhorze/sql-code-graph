"""Find command for searching the graph."""

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.config import get_db_path
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NodeLabel

app = typer.Typer(help="Search the graph")
console = Console()


@app.command("table")
def find_table(  # noqa: B008
    name: str = typer.Argument(..., help="Table name to search for"),  # noqa: B008
) -> None:
    """Find a table by name."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH (t:{NodeLabel.TABLE}) WHERE t.qualified CONTAINS $name "
        "RETURN t.qualified AS qualified, t.kind AS kind LIMIT 50",
        {"name": name},
    )
    _print_table(results, ["qualified", "kind"])
    backend.close()


@app.command("column")
def find_column(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference (table.column)"),  # noqa: B008
) -> None:
    """Find a column by table.column reference."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH (c:{NodeLabel.COLUMN}) WHERE c.id CONTAINS $ref RETURN c.id AS id LIMIT 50",
        {"ref": ref},
    )
    _print_table(results, ["id"])
    backend.close()


@app.command("pattern")
def find_pattern(  # noqa: B008
    pattern: str = typer.Argument(..., help="SQL pattern to search for"),  # noqa: B008
) -> None:
    """Find queries containing a SQL pattern."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH (q:{NodeLabel.QUERY}) WHERE q.sql CONTAINS $pattern "
        "RETURN q.id AS id, q.kind AS kind LIMIT 50",
        {"pattern": pattern},
    )
    _print_table(results, ["id", "kind"])
    backend.close()


def _print_table(rows: list[dict], columns: list[str]) -> None:
    """Print results as a Rich table."""
    if not rows:
        console.print("[yellow]No results[/yellow]")
        return
    t = Table(*columns)
    for row in rows:
        t.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(t)
