"""Watch command for monitoring file changes."""

import time
from pathlib import Path

import typer
from rich.console import Console
from watchdog.observers import Observer

from sqlcg.core.config import get_db_path
from sqlcg.core.jobs import WatchJobManager
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.indexer.watcher import SqlFileEventHandler
from sqlcg.utils.ignore import load_ignore_spec

console = Console()


def watch_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Directory to watch"),  # noqa: B008
    dialect: str | None = typer.Option(  # noqa: B008
        None, "--dialect", "-d", help="SQL dialect"
    ),
) -> None:
    """Watch a directory and re-index on SQL file changes."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = KuzuBackend(str(db_path))
    backend.init_schema()

    indexer = Indexer()

    # Initial full index
    console.print(f"Indexing {path}...")
    indexer.index_repo(path, dialect, backend)

    spec = load_ignore_spec(path)
    job_manager = WatchJobManager(indexer, backend, dialect)
    handler = SqlFileEventHandler(job_manager, backend, spec, path)
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
        console.print("Stopped.")
        backend.close()
