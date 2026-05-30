"""Analyze command for lineage analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.config import get_backend
from sqlcg.core.schema import NodeLabel, RelType

if TYPE_CHECKING:
    from sqlcg.server.noise_filter import NoiseFilter

app = typer.Typer(help="Lineage analysis")
console = Console()


@app.command("upstream")
def upstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Trace upstream column lineage."""
    # Bounds check for depth to prevent performance DoS
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH p=(c:{NodeLabel.COLUMN} {{id: $ref}})"
            f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src) "
            "RETURN src.id AS id LIMIT 100",
            {"ref": ref},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
            results = _filter_column_results(results, nf)
        _print_table(results, ["id"])


@app.command("downstream")
def downstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Trace downstream column lineage."""
    # Bounds check for depth to prevent performance DoS
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH p=(c:{NodeLabel.COLUMN} {{id: $ref}})"
            f"-[:{RelType.COLUMN_LINEAGE}*1..{depth}]->(dst) "
            "RETURN dst.id AS id LIMIT 100",
            {"ref": ref},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
            results = _filter_column_results(results, nf)
        _print_table(results, ["id"])


@app.command("impact")
def impact(  # noqa: B008
    table: str = typer.Argument(..., help="Table name to analyze"),  # noqa: B008
) -> None:
    """Show all queries impacted by a table."""
    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE} {{qualified: $t}})"
            f"<-[:{RelType.SELECTS_FROM}]-(q:{NodeLabel.QUERY}) "
            "RETURN q.id AS id, q.kind AS kind LIMIT 100",
            {"t": table},
        )
        _print_table(results, ["id", "kind"])


@app.command("failures")
def failures(
    cause: str | None = typer.Option(  # noqa: B008
        None, "--cause", help="Filter by E-code bucket (e.g. E5, timeout)"
    ),
    limit: int = typer.Option(100, "--limit", help="Maximum rows to return"),  # noqa: B008
) -> None:
    """List files that failed to parse, with their dominant cause (E-code bucket).

    Requires a graph indexed with sqlcg >= v3 (schema version 3). Re-index
    with 'sqlcg db reset && sqlcg index <path>' if the graph was built with
    an earlier version.
    """
    with get_backend() as backend:
        cypher = (
            f"MATCH (f:{NodeLabel.FILE}) WHERE f.parse_failed = true "
            "AND ($cause IS NULL OR f.parse_cause = $cause) "
            "RETURN f.path AS path, f.parse_cause AS cause "
            f"ORDER BY f.parse_cause LIMIT {limit}"
        )
        rows = backend.run_read(cypher, {"cause": cause})
        _print_table(rows, ["path", "cause"])


@app.command("unused")
def unused(
    threshold: int = typer.Option(0, "--threshold", help="Minimum reference count threshold"),
) -> None:
    """Find tables with no query references."""
    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE}) WHERE NOT (t)<-[:{RelType.SELECTS_FROM}]-() "
            "RETURN t.qualified AS qualified LIMIT 100",
            {},
        )
        _print_table(results, ["qualified"])


def _col_id_to_table(col_id: str) -> str:
    """Extract the table-qualified part from a column ID (schema.table.col → schema.table).

    Column IDs follow the format: schema.table.column or table.column.
    The table part is everything except the last component.

    Args:
        col_id: A column ID string from the graph.

    Returns:
        The table-qualified portion (all but the last dotted component).
    """
    parts = col_id.rsplit(".", 1)
    return parts[0] if len(parts) == 2 else col_id


def _filter_column_results(
    results: list[dict],
    nf: NoiseFilter,  # type: ignore[name-defined]
) -> list[dict]:
    """Filter column-ID result rows by NoiseFilter, dropping rows whose table is noise."""
    return [r for r in results if not nf.is_noise(_col_id_to_table(r["id"]))]


def _print_table(rows: list[dict], columns: list[str]) -> None:
    """Print results as a Rich table."""
    if not rows:
        console.print("[yellow]No results[/yellow]")
        return
    t = Table(*columns)
    for row in rows:
        t.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(t)
