"""Version-skew detector and self-heal trigger for the MCP server (issue #76).

PR 1 shipped ``read_ondisk_version()`` and ``detect_skew()`` — pure detection,
no re-exec machinery.  PR 2 adds ``maybe_self_heal()`` (throttled trigger) and
the ``register_self_heal_event()`` injection point used by ``server.py``.

The re-exec sequence itself (``_reexec``, ``_self_heal_watcher``) lives in
``server.py`` because it needs direct access to ``backend_lock``,
``shutdown_requested``, and the ``writer_queue``.

See plan/sprints/mcp_server_self_healing.md §Design D1–D3.
"""

from __future__ import annotations

import importlib.metadata as im
import os
import time
from typing import TYPE_CHECKING

from sqlcg import __version__
from sqlcg.utils.logging import getLogger

if TYPE_CHECKING:
    import anyio

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Throttle constant
# ---------------------------------------------------------------------------


def _get_skew_check_interval() -> float:
    """Return the skew check interval in seconds.

    Reads ``SQLCG_SKEW_CHECK_INTERVAL_S`` env var for testing overrides;
    falls back to the production default of 30 s (D2).
    """
    try:
        return float(os.environ.get("SQLCG_SKEW_CHECK_INTERVAL_S", "30.0"))
    except (ValueError, TypeError):
        return 30.0


_SKEW_CHECK_INTERVAL_S: float = 30.0
"""Minimum seconds between on-disk version checks (D2).

A ``distributions()`` scan takes ~1–5 ms; bounding it to once / 30 s makes
it invisible even under rapid tool-call bursts, while still re-execing
within 30 s of a reinstall on the next activity.

Override with ``SQLCG_SKEW_CHECK_INTERVAL_S`` env var (used in e2e tests).
"""

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_last_skew_check: float = 0.0
"""Monotonic timestamp of the last on-disk version scan."""

_self_heal_event: anyio.Event | None = None
"""Injected by server.py once the anyio event loop is running (D3).

``maybe_self_heal()`` sets this event to signal ``_self_heal_watcher``
to begin the drain-safe quiescence + re-exec sequence.
"""


def register_self_heal_event(event: anyio.Event) -> None:
    """Inject the anyio.Event that maybe_self_heal() will set on detected skew.

    Called by ``server.py`` ``main()`` after ``anyio.run`` starts the event loop
    (before the task group is created), so ``maybe_self_heal()`` can signal
    ``_self_heal_watcher`` without blocking the event loop.

    Args:
        event: The ``anyio.Event`` whose ``.set()`` triggers the self-heal watcher.
    """
    global _self_heal_event
    _self_heal_event = event


# ---------------------------------------------------------------------------
# On-disk version reader (D1, A1)
# ---------------------------------------------------------------------------


def read_ondisk_version() -> str | None:
    """Return the version of the sql-code-graph distribution on disk RIGHT NOW.

    A long-lived process caches distribution metadata in memory, so a plain
    ``importlib.metadata.version("sql-code-graph")`` may return the old version
    from the in-process ``PathDistribution`` cache, defeating skew detection.

    We invalidate the metadata-finder cache first, then re-scan via
    ``im.distributions()``, so a reinstall under the running interpreter is
    observed regardless of the ``.dist-info`` mtime.

    **Why ``invalidate_caches()`` is required (D1, A1):**
    ``im.distributions()`` maps ``FastPath`` over each ``sys.path`` entry.
    ``FastPath.__new__`` is ``@functools.lru_cache``'d per path string, and
    ``FastPath.lookup`` is a ``@method_cache`` keyed on the directory mtime.
    When ``uv tool install --force`` rewrites the ``.dist-info`` in place the
    directory mtime changes, so ``lookup`` re-keys and the child scan runs
    fresh — a reinstall IS observed in the common case without a cache bust.
    However, if the ``.dist-info`` is replaced with the SAME mtime (coarse-grained
    filesystem, same-second CI write), the mtime-keyed lookup would not re-key.
    ``im.MetadataPathFinder.invalidate_caches()`` clears the ``FastPath.__new__``
    lru_cache, forcing a fresh path-list read regardless of mtime.  This is the
    defensive call that makes the detector correct in all cases.

    Returns ``None`` if the distribution cannot be located (treated as "no skew"
    by ``detect_skew`` — never re-exec on an unknown version; see F1 in the plan).
    """
    try:
        # Clears the @lru_cache on FastPath.__new__ so the path list is re-read
        # from disk even when the .dist-info was replaced with the SAME mtime.
        im.MetadataPathFinder.invalidate_caches()
        for dist in im.distributions():
            if (dist.metadata["Name"] or "").lower() == "sql-code-graph":
                return dist.version
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Skew detector (D1)
# ---------------------------------------------------------------------------


def detect_skew() -> tuple[bool, str, str | None]:
    """Compare the on-disk distribution version against the running process's baked-in version.

    Returns a three-tuple ``(skew, running_version, ondisk_version)`` where:
    - ``skew`` is ``True`` when the on-disk version differs from ``__version__``
      AND the on-disk version is readable (``None`` → no skew, see F1).
    - ``running_version`` is ``sqlcg.__version__`` — the constant baked in at
      import time; it does NOT change when disk changes (that asymmetry is the
      skew signal).
    - ``ondisk_version`` is the result of ``read_ondisk_version()``.

    Guards plan/sprints/mcp_server_self_healing.md §D1 / Step 1.3.
    """
    ondisk = read_ondisk_version()
    if ondisk is None:
        return (False, __version__, None)
    skew = ondisk != __version__
    return (skew, __version__, ondisk)


# ---------------------------------------------------------------------------
# Throttled trigger (D2, D3, Step 2.1)
# ---------------------------------------------------------------------------


def maybe_self_heal() -> None:
    """Throttled version-skew check called at MCP tool entry and control-socket status.

    **Common (no-skew) path** — returns immediately with one monotonic subtract.
    Scans disk at most once per ``_SKEW_CHECK_INTERVAL_S`` seconds (D2).

    On detected skew: logs at WARNING with both versions and sets
    ``_self_heal_event`` to signal ``_self_heal_watcher`` in ``server.py`` to
    begin the drain-safe quiescence + re-exec sequence (D3, D5).

    **Threading safety:** this function is synchronous and non-blocking.  It
    only sets an ``anyio.Event``; the heavy drain + exec happens in the async
    ``_self_heal_watcher`` task.  Never call this from inside the drain body.

    F1 guard: if ``read_ondisk_version()`` returns ``None`` (distribution
    unreadable), no event is set — never re-exec on an unknown version.

    F7: on Windows, ``_self_heal_event`` is ``None`` (watcher not started).
    The disk check still fires (and is throttled) so ``mcp status`` can observe
    skew; we just cannot act on it with re-exec.  We log at WARNING and return.

    Guards plan/sprints/mcp_server_self_healing.md §D2 / §Step 2.1.
    """
    global _last_skew_check

    # Throttle — first statement so a hot caller pays only a monotonic subtract.
    now = time.monotonic()
    if now - _last_skew_check < _get_skew_check_interval():
        return
    _last_skew_check = now

    skew, running, ondisk = detect_skew()
    if not skew:
        return

    # Skew detected.
    logger.warning(
        "version skew detected: running=%s ondisk=%s — scheduling re-exec",
        running,
        ondisk,
    )

    if _self_heal_event is None:
        # Windows or event not yet registered — cannot re-exec; log and return.
        logger.warning(
            "self-heal event not registered (Windows or pre-startup call) — "
            "re-exec skipped; restart the MCP server manually"
        )
        return

    _self_heal_event.set()
