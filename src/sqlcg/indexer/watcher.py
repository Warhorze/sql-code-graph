"""File system watcher for SQL file changes."""

import subprocess
import threading
import time
from pathlib import Path

import pathspec
from watchdog.events import FileSystemEventHandler

from sqlcg.utils.ignore import is_ignored
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class SqlFileEventHandler(FileSystemEventHandler):
    """Watchdog event handler for SQL file changes."""

    def __init__(self, job_manager, db, ignore_spec: pathspec.PathSpec, root: Path, indexer=None):
        """Initialize the event handler.

        Args:
            job_manager: WatchJobManager instance
            db: GraphBackend instance
            ignore_spec: PathSpec with ignore patterns
            root: Root directory being watched
            indexer: Indexer instance (used by BranchMonitor)
        """
        super().__init__()
        self._jobs = job_manager
        self._db = db
        self._spec = ignore_spec
        self._root = root
        # Create and start BranchMonitor if indexer is provided
        self._branch_monitor: BranchMonitor | None = None
        if indexer is not None:
            self._branch_monitor = BranchMonitor(root, job_manager, indexer, db)
            self._branch_monitor.start()

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


class BranchMonitor(threading.Thread):
    """Background thread that detects branch changes and triggers full resyncs.

    Polls `git rev-parse --abbrev-ref HEAD` every 2 seconds. When the branch
    changes, pauses the job manager, runs a full reindex, then resumes and
    drains queued file events.
    """

    def __init__(
        self, watched_path: Path, job_manager, indexer, db, _poll_interval: float = 2.0
    ):
        """Initialize the branch monitor.

        Args:
            watched_path: Path being watched (used to find git root)
            job_manager: WatchJobManager instance
            indexer: Indexer instance
            db: GraphBackend instance
            _poll_interval: Polling interval in seconds (for testing)
        """
        # daemon=False ensures that if index_repo() is in-flight when shutdown is requested,
        # the process will wait (via join(timeout=5) in watch.py) up to 5 seconds before exiting.
        # This avoids data loss from killing an in-progress resync.
        super().__init__(daemon=False)
        self._watched_path = watched_path
        self._job_manager = job_manager
        self._indexer = indexer
        self._db = db
        self._stop_event = threading.Event()
        self._current_branch: str | None = None
        self._poll_interval = _poll_interval

    def run(self) -> None:
        """Poll git branch and trigger resync on change."""
        while not self._stop_event.is_set():
            try:
                branch = self._get_current_branch()
                if branch is not None and branch != self._current_branch:
                    logger.debug(
                        "Branch change detected: %s -> %s", self._current_branch, branch
                    )
                    self._current_branch = branch
                    self._on_branch_change()
            except subprocess.CalledProcessError:
                # Not a git repo or git not available
                logger.debug("Could not get current branch (not a git repo or git unavailable)")
                self._stop_event.set()
                break
            except Exception as exc:
                logger.debug("BranchMonitor error: %s", exc)

            # Sleep in small increments to allow quick shutdown
            for _ in range(int(self._poll_interval * 10)):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _get_current_branch(self) -> str | None:
        """Get the current git branch name.

        Returns:
            Branch name, or None if git command fails

        Raises:
            subprocess.CalledProcessError if git command fails
        """
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(self._watched_path),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _on_branch_change(self) -> None:
        """Handle branch change: pause, resync, resume, drain queue."""
        # Pause new file events
        self._job_manager.set_paused(True)

        # Cancel pending file timers
        self._job_manager.cancel_all()

        # Run full resync
        try:
            self._indexer.index_repo(self._watched_path, dialect=None, db=self._db)
        except Exception as exc:
            logger.error("Branch change resync failed: %s", exc)
        finally:
            # Resume and drain queued events
            self._job_manager.set_paused(False)
            self._job_manager.drain_queued()

    def stop(self) -> None:
        """Signal the thread to stop."""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the thread to stop.

        Args:
            timeout: Maximum time to wait in seconds
        """
        super().join(timeout=timeout)
