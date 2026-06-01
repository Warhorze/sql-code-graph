"""Control-file helpers for the MCP server lifecycle.

Manages the ``.pid`` and ``.sock`` files that allow a second CLI process to
discover, query, and stop the running MCP server.

All paths are derived from ``get_db_path()`` (i.e. ``KuzuConfig.from_env().db_path``)
so that two servers on two different databases do not collide on a single control
file.  Callers may pass an explicit ``db_path`` to override the default; this also
makes unit tests straightforward (set ``SQLCG_DB_PATH=/tmp/test.db``, assert paths
are ``/tmp/test.sock`` and ``/tmp/test.pid``).

Path convention::

    graph.db  →  graph.db.sock   (Unix domain socket, owner 0o600)
               →  graph.db.pid   (JSON record, owner 0o600)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlcg.core.config import get_db_path

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def sock_path(db_path: Path | None = None) -> Path:
    """Return the Unix domain socket path for *db_path*.

    Derives from ``get_db_path()`` when *db_path* is ``None``.
    Example: ``~/.sqlcg/graph.db`` → ``~/.sqlcg/graph.db.sock``.
    """
    p = db_path or get_db_path()
    return p.with_suffix(".sock")


def pid_path(db_path: Path | None = None) -> Path:
    """Return the PID file path for *db_path*.

    Derives from ``get_db_path()`` when *db_path* is ``None``.
    Example: ``~/.sqlcg/graph.db`` → ``~/.sqlcg/graph.db.pid``.
    """
    p = db_path or get_db_path()
    return p.with_suffix(".pid")


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------


def write_pid(db_path: Path | None = None) -> None:
    """Write a JSON PID record: ``{pid, db_path, started_at}``.

    The file is created with mode ``0o600`` (owner read/write only) to limit
    access on multi-user machines.

    Args:
        db_path: Explicit database path. Defaults to ``get_db_path()``.
    """
    resolved = db_path or get_db_path()
    pp = pid_path(resolved)
    pp.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "db_path": str(resolved),
                "started_at": time.time(),
            }
        )
    )
    pp.chmod(0o600)


def read_pid(db_path: Path | None = None) -> dict | None:
    """Return the PID record dict, or ``None`` if the file is missing or corrupt.

    Args:
        db_path: Explicit database path. Defaults to ``get_db_path()``.

    Returns:
        Parsed JSON dict with keys ``pid``, ``db_path``, ``started_at``, or
        ``None`` on any read/parse error.
    """
    pp = pid_path(db_path)
    try:
        return json.loads(pp.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_control_files(db_path: Path | None = None) -> None:
    """Remove ``.sock`` and ``.pid`` files silently.

    Called from ``server.main`` ``finally`` block so control files are always
    removed on clean exit or on uncaught exception.

    Args:
        db_path: Explicit database path. Defaults to ``get_db_path()``.
    """
    for p in (sock_path(db_path), pid_path(db_path)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Process liveness
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with *pid* currently exists.

    Uses ``os.kill(pid, 0)`` — signal 0 checks existence without sending a
    real signal.  Returns ``False`` if the process does not exist
    (``ProcessLookupError``) or is owned by another user (``PermissionError``
    means it exists but we cannot signal it, so we treat it as alive).

    Args:
        pid: Process ID to probe.

    Returns:
        ``True`` if the process exists, ``False`` if it does not.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by someone else — treat as alive
        return True
