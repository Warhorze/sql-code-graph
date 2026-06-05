"""Find command for searching the graph."""

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.server.read_client import run_read_routed

app = typer.Typer(help="Search the graph")
console = Console()


@app.command("table")
def find_table(  # noqa: B008
    name: str = typer.Argument(..., help="Table name to search for"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Find a table by name."""
    name = name.lower()  # graph keys are lowercased at index time (C2 normalization)
    results = run_read_routed(
        "SELECT qualified, kind FROM \"SqlTable\" WHERE qualified LIKE '%' || ? || '%' LIMIT 50",
        {"name": name},
    )
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        nf = NoiseFilter.from_config()
        ids = [r["qualified"] for r in results]
        kept, _ = nf.filter_nodes(ids)
        kept_set = set(kept)
        results = [r for r in results if r["qualified"] in kept_set]
    _print_table(results, ["qualified", "kind"])


@app.command("column")
def find_column(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference (table.column)"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Find a column by table.column reference."""
    ref = ref.lower()  # graph keys are lowercased at index time (C2 normalization)
    results = run_read_routed(
        "SELECT id FROM \"SqlColumn\" WHERE id LIKE '%' || ? || '%' LIMIT 50",
        {"ref": ref},
    )
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        nf = NoiseFilter.from_config()
        results = [r for r in results if not nf.is_noise(r["id"].rsplit(".", 1)[0])]
    _print_table(results, ["id"])


@app.command("pattern")
def find_pattern(  # noqa: B008
    pattern: str = typer.Argument(..., help="SQL pattern to search for"),  # noqa: B008
) -> None:
    """Find queries containing a SQL pattern."""
    results = run_read_routed(
        "SELECT id, kind FROM \"SqlQuery\" WHERE sql LIKE '%' || ? || '%' LIMIT 50",
        {"pattern": pattern},
    )
    _print_table(results, ["id", "kind"])


def _print_table(rows: list[dict], columns: list[str]) -> None:
    """Print results as a Rich table."""
    if not rows:
        console.print("[yellow]No results[/yellow]")
        return
    t = Table(*columns)
    for row in rows:
        t.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(t)
