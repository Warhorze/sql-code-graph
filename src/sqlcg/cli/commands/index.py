"""Index command for scanning and indexing SQL files."""

import os
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.core.config import get_backend, get_db_path, get_dialect
from sqlcg.indexer.indexer import Indexer

console = Console()


def index_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Directory to index"),  # noqa: B008
    dialect: str | None = typer.Option(  # noqa: B008
        None, "--dialect", "-d", help="SQL dialect (or 'auto' to read from .sqlcg.toml)"
    ),
    dbt_manifest: Path | None = typer.Option(  # noqa: B008
        None, "--dbt-manifest", help="Path to dbt manifest"
    ),
    timeout_per_file: int = typer.Option(  # noqa: B008
        30, "--timeout-per-file", help="Timeout per file in seconds"
    ),
    buffer_pool_size: int = typer.Option(  # noqa: B008
        0,
        "--buffer-pool-size",
        help="KuzuDB buffer pool size in MB (0 = default). "
        "Set to 256-512 on memory-constrained machines.",
    ),
    no_ddl: bool = typer.Option(  # noqa: B008
        False, "--no-ddl", help="Skip DDL statements (not yet fully implemented)"
    ),
    schema_from_info_schema: str | None = typer.Option(  # noqa: B008
        None, "--schema-from-info-schema", hidden=True, help="(Not yet implemented)"
    ),
    quiet: bool = typer.Option(  # noqa: B008
        False, "--quiet", "-q", help="Suppress summary console output"
    ),
) -> None:
    """Index SQL files in a directory."""
    if schema_from_info_schema:
        console.print("[red]--schema-from-info-schema is not yet implemented (v2)[/red]")
        raise typer.Exit(1)

    # TODO: wire no_ddl through to the indexer once it supports the parameter
    if no_ddl:
        console.print("[yellow]Note: --no-ddl is not yet fully implemented[/yellow]")

    # Set buffer pool size via env var if specified
    if buffer_pool_size > 0:
        os.environ["SQLCG_BUFFER_POOL_MB"] = str(buffer_pool_size)

    # Resolve dialect: 'auto' reads from .sqlcg.toml, otherwise use provided value
    if dialect == "auto":
        dialect = get_dialect(path)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_backend() as backend:
        backend.init_schema()

        # Check schema version — must match current build
        from sqlcg.core.schema import SCHEMA_VERSION, NodeLabel

        stored = backend.get_schema_version()
        if stored != SCHEMA_VERSION:
            console.print(
                f"[red]Database schema is v{stored}; this build requires v{SCHEMA_VERSION}. "
                "Run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-index.[/red]"
            )
            raise typer.Exit(1)

        abs_path = str(path.resolve())
        backend.upsert_node(
            NodeLabel.REPO,
            abs_path,
            {
                "path": abs_path,
                "name": path.name,
            },
        )

        # Index the repository
        indexer = Indexer()

        # Determine total files for progress callback
        from sqlcg.indexer.walker import walk_sql_files
        from sqlcg.utils.ignore import load_ignore_spec

        spec = load_ignore_spec(path)
        files = list(walk_sql_files(path, spec, use_git=True))
        total_files = len(files)

        # Define progress callback
        def _make_progress_callback(total: int):
            """Create a progress callback that prints progress every 100 files.

            The callback is only invoked every 100 files, so with fewer than 100 files
            in the repository, no progress line is printed.
            """

            def callback(n: int, total_n: int) -> None:
                console.print(f"\r  Indexed {n}/{total_n} files...", end="", highlight=False)

            return callback

        summary = indexer.index_repo(
            path,
            dialect,
            backend,
            dbt_manifest,
            timeout_per_file,
            progress_callback=_make_progress_callback(total_files),
        )
        console.print()  # newline after carriage return progress line

        # Connect files to repo
        from sqlcg.core.schema import RelType

        files_query = "MATCH (f:File) WHERE f.path STARTS WITH $repo_prefix RETURN f.path AS path"
        file_rows = backend.run_read(files_query, {"repo_prefix": abs_path})
        for row in file_rows:
            backend.upsert_edge(
                NodeLabel.FILE,
                row["path"],
                NodeLabel.REPO,
                abs_path,
                RelType.BELONGS_TO,
                {},
            )

        # Print summary unless --quiet is specified
        if not quiet:
            console.print(
                f"[green]Indexed[/green] {summary['files_parsed']} files — "
                f"{summary['tables_found']} tables, {summary['lineage_edges_created']} edges, "
                f"{summary['parse_errors']} errors"
            )
            if summary.get("lineage_edges_created", 0) == 0:
                console.print(
                    "[yellow]Warning: 0 lineage edges extracted — column lineage "
                    "unavailable.[/yellow]"
                )
