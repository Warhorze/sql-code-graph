"""Database management commands."""

import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.core.config import get_backend, get_db_path
from sqlcg.core.freshness import compute_freshness, render_freshness_line
from sqlcg.core.schema import NodeLabel
from sqlcg.server.read_client import run_read_routed
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

app = typer.Typer(help="Database management commands")
console = Console()


@app.command("init")
def db_init(
    buffer_pool_size: int = typer.Option(
        0,
        "--buffer-pool-size",
        help="KuzuDB buffer pool size in MB (0 = default). "
        "Set to 256-512 on memory-constrained machines.",
    ),
) -> None:
    """Initialise the graph database (idempotent)."""
    if buffer_pool_size > 0:
        os.environ["SQLCG_BUFFER_POOL_MB"] = str(buffer_pool_size)

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
        # Full reset — delete the DB. Kuzu may store it as a single file (current,
        # e.g. 0.11.x) or a directory (older versions); also drop the .wal sidecar.
        # shutil.rmtree silently no-ops on a regular file (NotADirectoryError +
        # ignore_errors), so dispatch on the actual filesystem type.
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
    # db info is a read-only command.  All Cypher reads route through the live
    # server (run_read_routed) to avoid "Database is locked" while the MCP server
    # holds the write lock.  get_schema_version / get_indexed_sha are inlined as
    # run_read_routed calls using their known Cypher so they too route through the
    # socket when a server is live; this avoids a direct-open that would hit the lock.

    # Schema version
    schema_rows = run_read_routed("MATCH (v:SchemaVersion) RETURN v.version AS version LIMIT 1", {})
    version = (schema_rows[0]["version"] if schema_rows else None) or "unknown"
    console.print(f"Schema version: {version}")

    # Freshness block — only shown when the DB has been indexed from a git repo
    try:
        sha_rows = run_read_routed(
            "MATCH (v:SchemaVersion) RETURN v.indexed_sha AS sha LIMIT 1", {}
        )
        indexed_sha = sha_rows[0]["sha"] if sha_rows else None
        repo_rows = run_read_routed("MATCH (r:Repo) RETURN r.path AS path LIMIT 1", {})
        if repo_rows and indexed_sha is not None and repo_rows[0].get("path"):
            repo_root = Path(repo_rows[0]["path"])
            f = compute_freshness(repo_root, indexed_sha)
            console.print(render_freshness_line(f))
    except NotImplementedError:
        # Neo4j backend raises NotImplementedError for get_indexed_sha — skip silently
        pass
    except Exception as e:
        # Any unexpected error in the freshness block must not crash db info
        logger.debug(f"Freshness check skipped: {e}")

    # Show node counts for all labels
    for label in NodeLabel:
        try:
            result = run_read_routed(f"MATCH (n:{label}) RETURN COUNT(*) AS count", {})
            count = result[0]["count"] if result else 0
            console.print(f"  {label}: {count}")
        except Exception as e:
            # Log unexpected exceptions instead of silently skipping
            logger.error(f"Error getting count for {label}: {e}")
            console.print(f"  [red]{label}: error[/red]")

    # Health check section
    repo_count_result = run_read_routed("MATCH (n:Repo) RETURN COUNT(n) AS count", {})
    repo_count = repo_count_result[0]["count"] if repo_count_result else 0

    if repo_count == 0:
        console.print(  # noqa: E501
            "[red]Database is empty. Run 'sqlcg db init' and 'sqlcg index <path>' first.[/red]"
        )
    else:
        query_count_result = run_read_routed("MATCH (n:SqlQuery) RETURN COUNT(n) AS count", {})
        query_count = query_count_result[0]["count"] if query_count_result else 0

        if query_count == 0:
            console.print(
                "[yellow]No queries indexed. Run 'sqlcg index <path>' to populate "
                "the graph.[/yellow]"
            )
        else:
            col_count_result = run_read_routed("MATCH (n:SqlColumn) RETURN COUNT(n) AS count", {})
            col_count = col_count_result[0]["count"] if col_count_result else 0

            if col_count == 0:
                console.print(
                    "[yellow]Column lineage not available. Tools trace_column_lineage, "
                    "get_downstream_dependencies, and get_upstream_dependencies "
                    "will return empty results.[/yellow]"
                )

    # Print COLUMN_LINEAGE edges count
    edges_result = run_read_routed("MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count", {})
    edges_count = edges_result[0]["count"] if edges_result else 0
    console.print(f"  COLUMN_LINEAGE edges: {edges_count}")

    # Print star resolution metrics (T-07)
    from sqlcg.core.queries import COUNT_STAR_EXPANSIONS_QUERY, COUNT_STAR_SOURCES_QUERY

    star_source_result = run_read_routed(COUNT_STAR_SOURCES_QUERY, {})
    star_source_count = star_source_result[0]["n"] if star_source_result else 0
    console.print(f"  STAR_SOURCE edges: {star_source_count}")

    star_expansion_result = run_read_routed(COUNT_STAR_EXPANSIONS_QUERY, {})
    star_expansion_count = star_expansion_result[0]["n"] if star_expansion_result else 0
    console.print(f"  STAR_EXPANSION lineage edges: {star_expansion_count}")

    # Print parsing mode distribution
    mode_query = (
        "MATCH (q:SqlQuery) RETURN q.parsing_mode AS mode, COUNT(q) AS cnt ORDER BY cnt DESC"
    )
    mode_rows = run_read_routed(mode_query, {})
    if mode_rows and "mode" in mode_rows[0]:
        console.print("\n  Parsing mode distribution:")
        for row in mode_rows:
            console.print(f"    {row['mode']}: {row['cnt']}")


@app.command("list-repos")
def list_repos() -> None:
    """List all indexed repositories."""
    result = run_read_routed("MATCH (r:Repo) RETURN r.path AS path, r.name AS name", {})

    if not result:
        console.print("[yellow]No repositories indexed[/yellow]")
    else:
        from rich.table import Table

        table = Table("Path", "Name")
        for row in result:
            table.add_row(str(row.get("path", "")), str(row.get("name", "")))
        console.print(table)
