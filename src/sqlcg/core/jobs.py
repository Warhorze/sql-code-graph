"""Watch job manager for file-change-triggered reindexing."""

import threading
from collections.abc import Callable

from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class WatchJobManager:
    """Manages debounced reindex jobs for file changes.

    Uses per-file threading.Timer instances with debouncing. Rapid changes
    to the same file cancel the previous timer and schedule a new one.
    """

    def __init__(
        self,
        indexer,
        db,
        dialect: str | None,
        debounce_seconds: float = 2.0,
        _timer_factory: Callable | None = None,
    ):
        """Initialize the watch job manager.

        Args:
            indexer: Indexer instance
            db: GraphBackend instance
            dialect: SQL dialect
            debounce_seconds: Debounce delay in seconds
            _timer_factory: Optional timer factory (for testing)
        """
        self._indexer = indexer
        self._db = db
        self._dialect = dialect
        self._debounce = debounce_seconds
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._timer_factory = _timer_factory or threading.Timer
        self._paused = False
        self._queued: list[str] = []

    def schedule(self, file_path: str) -> None:
        """Schedule a reindex job for a file.

        If a job is already scheduled for this path, it is canceled and
        a new one is scheduled. If the manager is paused, the path is queued
        instead of starting a timer.

        Args:
            file_path: Path to the file to reindex
        """
        with self._lock:
            if self._paused:
                # Queue the path for later processing
                if file_path not in self._queued:
                    self._queued.append(file_path)
                return

            if file_path in self._timers:
                self._timers[file_path].cancel()
            t = self._timer_factory(self._debounce, self._run_job, args=[file_path])
            self._timers[file_path] = t
            t.start()

    def _run_job(self, file_path: str) -> None:
        """Execute a reindex job (called after debounce delay).

        Args:
            file_path: Path to the file to reindex
        """
        try:
            self._indexer.reindex_file(file_path, self._db, self._dialect)
        except Exception as exc:
            logger.error("reindex_file failed: %s: %s", file_path, exc)
        finally:
            with self._lock:
                self._timers.pop(file_path, None)

    def cancel_all(self) -> None:
        """Cancel all pending timers."""
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    def set_paused(self, paused: bool) -> None:
        """Set the paused state.

        Args:
            paused: True to pause scheduling, False to resume
        """
        with self._lock:
            self._paused = paused

    def drain_queued(self) -> None:
        """Drain queued file paths and schedule them for reindexing."""
        with self._lock:
            queued_copy = self._queued.copy()
            self._queued.clear()

        for file_path in queued_copy:
            self.schedule(file_path)
