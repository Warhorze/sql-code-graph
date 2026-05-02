"""Analyze command for lineage analysis."""

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.config import get_db_path
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NodeLabel, RelType

app = typer.Typer(help="Lineage analysis")
console = Console()


@app.command("upstream")
def upstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
) -> None:
    """Trace upstream column lineage."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH p=(c:{NodeLabel.COLUMN} {{id: $ref}})"
        f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src) "
        "RETURN src.id AS id LIMIT 100",
        {"ref": ref},
    )
    _print_table(results, ["id"])
    backend.close()


@app.command("downstream")
def downstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
) -> None:
    """Trace downstream column lineage."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH p=(c:{NodeLabel.COLUMN} {{id: $ref}})"
        f"-[:{RelType.COLUMN_LINEAGE}*1..{depth}]->(dst) "
        "RETURN dst.id AS id LIMIT 100",
        {"ref": ref},
    )
    _print_table(results, ["id"])
    backend.close()


@app.command("impact")
def impact(  # noqa: B008
    table: str = typer.Argument(..., help="Table name to analyze"),  # noqa: B008
) -> None:
    """Show all queries impacted by a table."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH (t:{NodeLabel.TABLE} {{qualified: $t}})"
        f"<-[:{RelType.SELECTS_FROM}]-(q:{NodeLabel.QUERY}) "
        "RETURN q.id AS id, q.kind AS kind LIMIT 100",
        {"t": table},
    )
    _print_table(results, ["id", "kind"])
    backend.close()


@app.command("unused")
def unused(
    threshold: int = typer.Option(0, "--threshold", help="Minimum reference count threshold"),
) -> None:
    """Find tables with no query references."""
    backend = KuzuBackend(str(get_db_path()))
    results = backend.run_read(
        f"MATCH (t:{NodeLabel.TABLE}) WHERE NOT (t)<-[:{RelType.SELECTS_FROM}]-() "
        "RETURN t.qualified AS qualified LIMIT 100",
        {},
    )
    _print_table(results, ["qualified"])
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
