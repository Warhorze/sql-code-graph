"""SQLite-based metrics storage for SQL Code Graph.

Importable without a graph backend. All writes are wrapped in try/except with
WARNING-level logging on failure. Opt-out via SQLCG_METRICS=0.
"""

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class MetricsStore:
    """SQLite metrics store with append-only design.

    All write operations are best-effort: failures are logged as WARNING
    and do not raise. This ensures that metrics collection never crashes
    the application.

    Use as a context manager to ensure proper cleanup.
    """

    def __init__(self, db_path: Path | str) -> None:
        """Initialize metrics store.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

        # Check if metrics are disabled
        if os.environ.get("SQLCG_METRICS", "").strip().lower() == "0":
            self._enabled = False
        else:
            self._enabled = True
            # Ensure parent directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=True,
                timeout=5.0,
            )
            # Enable WAL mode for better concurrency
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass  # Silently ignore pragma failures

    def __enter__(self) -> "MetricsStore":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit with cleanup."""
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass  # Silently ignore close errors
            finally:
                self._conn = None

    def init_schema(self) -> None:
        """Initialize database schema (idempotent).

        Creates three tables if they don't exist:
        - tool_calls: MCP tool invocations with timing
        - index_runs: Indexer.index_repo() invocations with parse stats
        - feedback: User feedback labels (TP/FP) for queries
        """
        if not self._enabled or self._conn is None:
            return

        try:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    success INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    files_parsed INTEGER NOT NULL,
                    parse_errors INTEGER NOT NULL,
                    tables_found INTEGER NOT NULL,
                    lineage_edges INTEGER NOT NULL,
                    duration_ms REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    query TEXT NOT NULL,
                    label TEXT NOT NULL,
                    note TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS writer_queue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    op TEXT,
                    reason TEXT,
                    queue_depth INTEGER,
                    duration_ms REAL
                )
                """
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to initialize metrics schema: {e}")

    def record_tool_call(
        self,
        tool_name: str,
        duration_ms: float,
        success: bool = True,
    ) -> None:
        """Record a tool call with timing information.

        Args:
            tool_name: Name of the MCP tool.
            duration_ms: Execution time in milliseconds.
            success: Whether the call succeeded.
        """
        if not self._enabled or self._conn is None:
            return

        try:
            timestamp = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                INSERT INTO tool_calls (timestamp, tool_name, duration_ms, success)
                VALUES (?, ?, ?, ?)
                """,
                (timestamp, tool_name, duration_ms, int(success)),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to record tool call: {e}")

    def record_index_run(
        self,
        repo_path: str,
        files_parsed: int,
        parse_errors: int,
        tables_found: int,
        lineage_edges: int,
        duration_ms: float,
    ) -> None:
        """Record an index run with parse statistics.

        Args:
            repo_path: Path to the indexed repository.
            files_parsed: Number of SQL files parsed.
            parse_errors: Number of files with parse errors.
            tables_found: Number of table/view nodes created.
            lineage_edges: Number of lineage edges created.
            duration_ms: Total duration in milliseconds.
        """
        if not self._enabled or self._conn is None:
            return

        try:
            timestamp = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                INSERT INTO index_runs
                (timestamp, repo_path, files_parsed, parse_errors, tables_found,
                 lineage_edges, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    repo_path,
                    files_parsed,
                    parse_errors,
                    tables_found,
                    lineage_edges,
                    duration_ms,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to record index run: {e}")

    def record_feedback(
        self,
        tool_name: str,
        query: str,
        label: str,
        note: str = "",
    ) -> None:
        """Record feedback on a tool result.

        Args:
            tool_name: Name of the tool being evaluated.
            query: The query or pattern that was evaluated.
            label: Feedback label: "TP" or "FP".
            note: Optional user note (truncated to 500 chars).
        """
        if not self._enabled or self._conn is None:
            return

        try:
            timestamp = datetime.now(UTC).isoformat()
            # Truncate note to 500 characters
            truncated_note = note[:500] if note else None
            self._conn.execute(
                """
                INSERT INTO feedback (timestamp, tool_name, query, label, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, tool_name, query, label, truncated_note),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to record feedback: {e}")

    def record_queue_event(
        self,
        event: str,
        *,
        op: str | None = None,
        reason: str | None = None,
        queue_depth: int | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Record a writer-queue lifecycle event.

        Args:
            event: One of ``"enqueued"``, ``"coalesced"``, ``"drained"``.
            op: The write op type (``"index"`` or ``"reindex"``).
            reason: Coalesce reason constant (set for ``"coalesced"`` events).
            queue_depth: Pending-queue depth at event time.
            duration_ms: Drain wall-clock duration (set for ``"drained"``).
        """
        if not self._enabled or self._conn is None:
            return

        try:
            timestamp = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                INSERT INTO writer_queue_events
                (timestamp, event, op, reason, queue_depth, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, event, op, reason, queue_depth, duration_ms),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to record queue event: {e}")

    def execute_query(self, query: str, params: tuple | None = None) -> list[tuple]:
        """Execute a read-only query.

        Args:
            query: SQL SELECT query.
            params: Optional tuple of query parameters for placeholders.

        Returns:
            List of result tuples, or empty list on error.
        """
        if not self._enabled or self._conn is None:
            return []

        try:
            if params:
                cursor = self._conn.execute(query, params)
            else:
                cursor = self._conn.execute(query)
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.warning(f"Failed to execute query: {e}")
            return []

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database.

        Args:
            table_name: Name of the table.

        Returns:
            True if table exists, False otherwise.
        """
        if not self._enabled or self._conn is None:
            return False

        try:
            cursor = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            return cursor.fetchone() is not None
        except sqlite3.Error:
            return False
