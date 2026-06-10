"""Watch command for monitoring file changes."""

import time
from pathlib import Path

import typer
from rich.console import Console
from watchdog.observers import Observer

from sqlcg.core.config import get_backend, get_db_path, get_dialect
from sqlcg.core.jobs import WatchJobManager
from sqlcg.indexer.indexer import Indexer
from sqlcg.indexer.watcher import SqlFileEventHandler
from sqlcg.utils.ignore import load_ignore_spec

console = Console()


def watch_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Directory to watch"),  # noqa: B008
    dialect: str | None = typer.Option(  # noqa: B008
        "auto", "--dialect", "-d", help="SQL dialect ('auto' reads from .sqlcg.toml, default)"
    ),
) -> None:
    """Watch a directory and re-index on SQL file changes."""
    # Resolve path early and resolve "auto" -> .sqlcg.toml dialect before the
    # initial index and before WatchJobManager / SqlFileEventHandler are
    # constructed (mirrors the index/reindex Bug A fix — the sentinel "auto"
    # must never reach the indexer or the watch helpers).
    path = path.resolve()
    if dialect == "auto":
        dialect = get_dialect(path)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_backend() as backend:
        backend.init_schema()

        # Check schema version — must match current build
        from sqlcg.core.schema import SCHEMA_VERSION

        stored = backend.get_schema_version()
        if stored != SCHEMA_VERSION:
            console.print(
                f"[red]Database schema is v{stored}; this build requires v{SCHEMA_VERSION}. "
                "Run 'sqlcg db reset && sqlcg db init' to re-initialize.[/red]"
            )
            raise typer.Exit(1)

        indexer = Indexer()

        # Initial full index
        console.print(f"Indexing {path}...")
        indexer.index_repo(path, dialect, backend)

        spec = load_ignore_spec(path)
        job_manager = WatchJobManager(indexer, backend, dialect)
        handler = SqlFileEventHandler(
            job_manager, backend, spec, path, indexer=indexer, dialect=dialect
        )
        observer = Observer()
        observer.schedule(handler, str(path), recursive=True)
        observer.start()
        console.print(f"[green]Watching[/green] {path} — press Ctrl+C to stop")
        try:
            while observer.is_alive():
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            observer.stop()
            observer.join(timeout=5)
            job_manager.cancel_all()
            if handler._branch_monitor is not None:
                handler._branch_monitor.stop()
                handler._branch_monitor.join(timeout=5)
            console.print("Stopped.")
