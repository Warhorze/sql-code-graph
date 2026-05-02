"""Database management commands."""

import typer
from rich.console import Console

from sqlcg.core.config import get_db_path
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NodeLabel

app = typer.Typer(help="Database management commands")
console = Console()


@app.command("init")
def db_init() -> None:
    """Initialise the graph database (idempotent)."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = KuzuBackend(str(db_path))
    backend.init_schema()
    version = backend.get_schema_version()
    console.print(f"[green]Database initialised[/green] at {db_path} (schema v{version})")
    backend.close()


@app.command("reset")
def db_reset(  # noqa: B008
    repo: str | None = typer.Option(None, "--repo", help="Reset only this repo path"),  # noqa: B008
) -> None:
    """Wipe the database or a single repo's subgraph."""
    backend = KuzuBackend(str(get_db_path()))
    if repo:
        # Delete all nodes for this repo
        backend.run_read(
            "MATCH (r:Repo {path: $p}) DETACH DELETE r",
            {"p": repo},
        )
        console.print(f"[yellow]Reset repo[/yellow] {repo}")
    else:
        # Full reset — delete the DB file
        import shutil

        db_path = get_db_path()
        shutil.rmtree(str(db_path), ignore_errors=True)
        console.print("[red]Database wiped[/red]")
    backend.close()


@app.command("info")
def db_info() -> None:
    """Show database stats."""
    backend = KuzuBackend(str(get_db_path()))
    version = backend.get_schema_version() or "unknown"
    console.print(f"Schema version: {version}")

    # Show node counts for all labels
    for label in NodeLabel:
        try:
            result = backend.run_read(f"MATCH (n:{label}) RETURN COUNT(*) AS count", {})
            count = result[0]["count"] if result else 0
            console.print(f"  {label}: {count}")
        except Exception:
            # Skip labels that don't have nodes
            pass

    backend.close()


@app.command("list-repos")
def list_repos() -> None:
    """List all indexed repositories."""
    backend = KuzuBackend(str(get_db_path()))
    result = backend.run_read("MATCH (r:Repo) RETURN r.path AS path, r.name AS name", {})

    if not result:
        console.print("[yellow]No repositories indexed[/yellow]")
    else:
        from rich.table import Table

        table = Table("Path", "Name")
        for row in result:
            table.add_row(str(row.get("path", "")), str(row.get("name", "")))
        console.print(table)

    backend.close()
