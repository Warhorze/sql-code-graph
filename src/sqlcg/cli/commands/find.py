"""Find command for searching the graph."""

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.config import get_backend
from sqlcg.core.schema import NodeLabel

app = typer.Typer(help="Search the graph")
console = Console()


@app.command("table")
def find_table(  # noqa: B008
    name: str = typer.Argument(..., help="Table name to search for"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Find a table by name."""
    name = name.lower()  # graph keys are lowercased at index time (C2 normalization)
    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE}) WHERE t.qualified CONTAINS $name "
            "RETURN t.qualified AS qualified, t.kind AS kind LIMIT 50",
            {"name": name},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
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
    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (c:{NodeLabel.COLUMN}) WHERE c.id CONTAINS $ref RETURN c.id AS id LIMIT 50",
            {"ref": ref},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
            # Filter on the schema.table portion of each column id (schema.table.column)
            results = [r for r in results if not nf.is_noise(r["id"].rsplit(".", 1)[0])]
        _print_table(results, ["id"])


@app.command("pattern")
def find_pattern(  # noqa: B008
    pattern: str = typer.Argument(..., help="SQL pattern to search for"),  # noqa: B008
) -> None:
    """Find queries containing a SQL pattern."""
    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (q:{NodeLabel.QUERY}) WHERE q.sql CONTAINS $pattern "
            "RETURN q.id AS id, q.kind AS kind LIMIT 50",
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
