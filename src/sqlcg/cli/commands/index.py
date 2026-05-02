"""Index command for scanning and indexing SQL files."""

from pathlib import Path

import typer
from rich.console import Console

from sqlcg.core.config import get_db_path
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

console = Console()


def index_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Directory to index"),  # noqa: B008
    dialect: str | None = typer.Option(  # noqa: B008
        None, "--dialect", "-d", help="SQL dialect"
    ),
    dbt_manifest: Path | None = typer.Option(  # noqa: B008
        None, "--dbt-manifest", help="Path to dbt manifest"
    ),
    timeout_per_file: int = typer.Option(  # noqa: B008
        30, "--timeout-per-file", help="Timeout per file in seconds"
    ),
    schema_from_info_schema: str | None = typer.Option(  # noqa: B008
        None, "--schema-from-info-schema", hidden=True, help="(Not yet implemented)"
    ),
) -> None:
    """Index SQL files in a directory."""
    if schema_from_info_schema:
        console.print("[red]--schema-from-info-schema is not yet implemented (v2)[/red]")
        raise typer.Exit(1)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = KuzuBackend(str(db_path))
    backend.init_schema()

    indexer = Indexer()
    summary = indexer.index_repo(path, dialect, backend, dbt_manifest, timeout_per_file)
    console.print(
        f"[green]Indexed[/green] {summary['files_parsed']} files — "
        f"{summary['tables_found']} tables, {summary['lineage_edges_created']} edges, "
        f"{summary['parse_errors']} errors"
    )
    backend.close()
