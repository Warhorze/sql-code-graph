"""Database management commands."""

import shutil

import typer
from rich.console import Console

from sqlcg.core.config import get_backend, get_db_path
from sqlcg.core.schema import NodeLabel
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

app = typer.Typer(help="Database management commands")
console = Console()


@app.command("init")
def db_init() -> None:
    """Initialise the graph database (idempotent)."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_backend() as backend:
        backend.init_schema()
        version = backend.get_schema_version()
        console.print(f"[green]Database initialised[/green] at {db_path} (schema v{version})")


@app.command("reset")
def db_reset(  # noqa: B008
    repo: str | None = typer.Option(None, "--repo", help="Reset only this repo path"),  # noqa: B008
) -> None:
    """Wipe the database or a single repo's subgraph."""
    if repo:
        # Delete all nodes for this repo (use run_write for mutation)
        with get_backend() as backend:
            backend.run_write(
                "MATCH (r:Repo {path: $p}) DETACH DELETE r",
                {"p": repo},
            )
        console.print(f"[yellow]Reset repo[/yellow] {repo}")
    else:
        # Full reset — delete the DB file (close backend first to release file handle)
        db_path = get_db_path()
        if db_path.exists():
            shutil.rmtree(str(db_path), ignore_errors=True)
        console.print("[red]Database wiped[/red]")


@app.command("info")
def db_info() -> None:
    """Show database stats."""
    with get_backend() as backend:
        version = backend.get_schema_version() or "unknown"
        console.print(f"Schema version: {version}")

        # Show node counts for all labels
        for label in NodeLabel:
            try:
                result = backend.run_read(f"MATCH (n:{label}) RETURN COUNT(*) AS count", {})
                count = result[0]["count"] if result else 0
                console.print(f"  {label}: {count}")
            except Exception as e:
                # Log unexpected exceptions instead of silently skipping
                logger.error(f"Error getting count for {label}: {e}")
                console.print(f"  [red]{label}: error[/red]")

        # Health check section
        repo_count_result = backend.run_read("MATCH (n:Repo) RETURN COUNT(n) AS count", {})
        repo_count = repo_count_result[0]["count"] if repo_count_result else 0

        if repo_count == 0:
            console.print(  # noqa: E501
                "[red]Database is empty. Run 'sqlcg db init' and 'sqlcg index <path>' first.[/red]"
            )
        else:
            query_count_result = backend.run_read("MATCH (n:SqlQuery) RETURN COUNT(n) AS count", {})
            query_count = query_count_result[0]["count"] if query_count_result else 0

            if query_count == 0:
                console.print(
                    "[yellow]No queries indexed. Run 'sqlcg index <path>' to populate "
                    "the graph.[/yellow]"
                )
            else:
                col_count_result = backend.run_read(
                    "MATCH (n:SqlColumn) RETURN COUNT(n) AS count", {}
                )
                col_count = col_count_result[0]["count"] if col_count_result else 0

                if col_count == 0:
                    console.print(
                        "[yellow]Column lineage not available. Tools trace_column_lineage, "
                        "get_downstream_dependencies, and get_upstream_dependencies "
                        "will return empty results.[/yellow]"
                    )

        # Print COLUMN_LINEAGE edges count
        edges_result = backend.run_read(
            "MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count", {}
        )
        edges_count = edges_result[0]["count"] if edges_result else 0
        console.print(f"  COLUMN_LINEAGE edges: {edges_count}")

        # Print parsing mode distribution
        mode_rows = backend.run_read(
            "MATCH (q:SqlQuery) RETURN q.parsing_mode AS mode, COUNT(q) AS cnt ORDER BY cnt DESC", {}
        )
        if mode_rows:
            console.print("
  Parsing mode distribution:")
            for row in mode_rows:
                console.print(f"    {row['mode']}: {row['cnt']}")


@app.command("list-repos")
def list_repos() -> None:
    """List all indexed repositories."""
    with get_backend() as backend:
        result = backend.run_read("MATCH (r:Repo) RETURN r.path AS path, r.name AS name", {})

        if not result:
            console.print("[yellow]No repositories indexed[/yellow]")
        else:
            from rich.table import Table

            table = Table("Path", "Name")
            for row in result:
                table.add_row(str(row.get("path", "")), str(row.get("name", "")))
            console.print(table)
