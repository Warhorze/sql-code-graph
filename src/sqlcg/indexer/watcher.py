"""File system watcher for SQL file changes."""

from pathlib import Path

import pathspec
from watchdog.events import FileSystemEventHandler

from sqlcg.utils.ignore import is_ignored


class SqlFileEventHandler(FileSystemEventHandler):
    """Watchdog event handler for SQL file changes."""

    def __init__(self, job_manager, db, ignore_spec: pathspec.PathSpec, root: Path):
        """Initialize the event handler.

        Args:
            job_manager: WatchJobManager instance
            db: GraphBackend instance
            ignore_spec: PathSpec with ignore patterns
            root: Root directory being watched
        """
        super().__init__()
        self._jobs = job_manager
        self._db = db
        self._spec = ignore_spec
        self._root = root

    def _is_sql(self, path: str | bytes) -> bool:
        """Check if a path is a non-ignored SQL file.

        Args:
            path: File path to check (str or bytes from watchdog)

        Returns:
            True if the path is a .sql file not matching ignore patterns
        """
        if isinstance(path, bytes):
            path = path.decode("utf-8")
        path_obj = Path(path)
        return path_obj.suffix == ".sql" and not is_ignored(path_obj, self._root, self._spec)

    def on_modified(self, event):
        """Handle file modification events.

        Args:
            event: Watchdog event object
        """
        if not event.is_directory and self._is_sql(event.src_path):
            self._jobs.schedule(event.src_path)

    def on_created(self, event):
        """Handle file creation events.

        Args:
            event: Watchdog event object
        """
        if not event.is_directory and self._is_sql(event.src_path):
            self._jobs.schedule(event.src_path)

    def on_moved(self, event):
        """Handle file move events.

        Args:
            event: Watchdog event object
        """
        if not event.is_directory and self._is_sql(event.dest_path):
            self._jobs.schedule(event.dest_path)

    def on_deleted(self, event):
        """Handle file deletion events.

        Args:
            event: Watchdog event object
        """
        if not event.is_directory and self._is_sql(event.src_path):
            self._db.delete_nodes_for_file(event.src_path)
