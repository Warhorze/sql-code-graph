"""Database management commands."""

import shutil
from pathlib import Path

import typer
from rich.console import Console

import sqlcg as _sqlcg_pkg
from sqlcg.cli.coverage import (
    CoverageStats,  # noqa: F401 — re-exported for patch targets in tests
    collect_coverage,
    render_coverage_lines,
)
from sqlcg.core.config import get_backend, get_db_path
from sqlcg.core.freshness import compute_freshness, render_freshness_line
from sqlcg.core.schema import NodeLabel
from sqlcg.server.read_client import run_read_routed
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
    import socket as _socket

    from sqlcg.server.control import sock_path

    # Refuse cleanly when a server is live.
    sp = sock_path()
    if sp.exists():
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(str(sp))
            console.print(
                "[red]A server is running on this database; stop it first "
                "('sqlcg mcp stop') before resetting the database.[/red]"
            )
            raise typer.Exit(1)
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            pass

    if repo:
        # Delete all nodes for this repo: delete File nodes (cascades to all
        # related nodes via delete_nodes_for_file) and the Repo node itself.
        with get_backend() as backend:
            # Get all files for this repo
            file_rows = backend.run_read(
                'SELECT path FROM "File" WHERE repo_path = ?',
                {"repo_path": repo},
            )
            for fr in file_rows:
                backend.delete_nodes_for_file(fr["path"])
            backend.run_write('DELETE FROM "Repo" WHERE path = ?', {"p": repo})
        console.print(f"[yellow]Reset repo[/yellow] {repo}")
    else:
        # Full reset — delete the DuckDB file (single file, not a directory).
        db_path = get_db_path()
        removed = False
        for target in (db_path, db_path.with_name(db_path.name + ".wal")):
            if target.is_dir():
                shutil.rmtree(str(target), ignore_errors=True)
                removed = True
            elif target.exists():
                target.unlink()
                removed = True
        if removed:
            console.print("[red]Database wiped[/red]")
        else:
            console.print("[yellow]Nothing to wipe — database does not exist[/yellow]")


@app.command("info")
def db_info() -> None:
    """Show database stats."""
    # db info routes through the live server (run_read_routed) to avoid holding
    # the DuckDB file lock when the MCP server is running.

    # Schema version
    schema_rows = run_read_routed('SELECT version FROM "SchemaVersion" LIMIT 1', {})
    version = (schema_rows[0]["version"] if schema_rows else None) or "unknown"
    console.print(f"Schema version: {version}")

    # Freshness block
    try:
        sha_rows = run_read_routed(
            'SELECT indexed_sha AS sha, sqlcg_version FROM "SchemaVersion" LIMIT 1', {}
        )
        indexed_sha = sha_rows[0]["sha"] if sha_rows else None
        indexed_version = sha_rows[0].get("sqlcg_version") if sha_rows else None
        repo_rows = run_read_routed('SELECT path FROM "Repo" LIMIT 1', {})
        if repo_rows and indexed_sha is not None and repo_rows[0].get("path"):
            repo_root = Path(repo_rows[0]["path"])
            f = compute_freshness(
                repo_root,
                indexed_sha,
                indexed_version=indexed_version,
                running_version=_sqlcg_pkg.__version__,
            )
            console.print(render_freshness_line(f))
    except Exception as e:
        logger.debug(f"Freshness check skipped: {e}")

    # Node counts
    for label in NodeLabel:
        try:
            result = run_read_routed(f'SELECT count(*) AS count FROM "{label}"', {})
            count = result[0]["count"] if result else 0
            console.print(f"  {label}: {count}")
        except Exception as e:
            logger.error(f"Error getting count for {label}: {e}")
            console.print(f"  [red]{label}: error[/red]")

    # Health check
    repo_count_result = run_read_routed('SELECT count(*) AS count FROM "Repo"', {})
    repo_count = repo_count_result[0]["count"] if repo_count_result else 0

    if repo_count == 0:
        console.print(
            "[red]Database is empty. Run 'sqlcg db init' and 'sqlcg index <path>' first.[/red]"
        )
    else:
        query_count_result = run_read_routed('SELECT count(*) AS count FROM "SqlQuery"', {})
        query_count = query_count_result[0]["count"] if query_count_result else 0

        if query_count == 0:
            console.print(
                "[yellow]No queries indexed. Run 'sqlcg index <path>' to populate "
                "the graph.[/yellow]"
            )
        else:
            col_count_result = run_read_routed('SELECT count(*) AS count FROM "SqlColumn"', {})
            col_count = col_count_result[0]["count"] if col_count_result else 0

            if col_count == 0:
                console.print(
                    "[yellow]Column lineage not available. Tools trace_column_lineage, "
                    "get_downstream_dependencies, and get_upstream_dependencies "
                    "will return empty results.[/yellow]"
                )

    edges_result = run_read_routed('SELECT count(*) AS count FROM "COLUMN_LINEAGE"', {})
    edges_count = edges_result[0]["count"] if edges_result else 0
    console.print(f"  COLUMN_LINEAGE edges: {edges_count}")

    from sqlcg.core.queries import COUNT_STAR_EXPANSIONS_QUERY, COUNT_STAR_SOURCES_QUERY

    star_source_result = run_read_routed(COUNT_STAR_SOURCES_QUERY, {})
    star_source_count = star_source_result[0]["n"] if star_source_result else 0
    console.print(f"  STAR_SOURCE edges: {star_source_count}")

    star_expansion_result = run_read_routed(COUNT_STAR_EXPANSIONS_QUERY, {})
    star_expansion_count = star_expansion_result[0]["n"] if star_expansion_result else 0
    console.print(f"  STAR_EXPANSION lineage edges: {star_expansion_count}")

    mode_rows = run_read_routed(
        'SELECT parsing_mode AS mode, count(*) AS cnt FROM "SqlQuery"'
        " GROUP BY parsing_mode ORDER BY cnt DESC",
        {},
    )
    if mode_rows and "mode" in mode_rows[0]:
        console.print("\n  Parsing mode distribution:")
        for row in mode_rows:
            console.print(f"    {row['mode']}: {row['cnt']}")

    # Coverage block — omitted silently when graph unavailable or empty
    coverage = collect_coverage()
    if coverage is not None:
        console.print("\n  Coverage:")
        for line in render_coverage_lines(coverage, indent="    "):
            console.print(line)


@app.command("list-repos")
def list_repos() -> None:
    """List all indexed repositories."""
    result = run_read_routed('SELECT path, name FROM "Repo"', {})

    if not result:
        console.print("[yellow]No repositories indexed[/yellow]")
    else:
        from rich.table import Table

        table = Table("Path", "Name")
        for row in result:
            table.add_row(str(row.get("path", "")), str(row.get("name", "")))
        console.print(table)
