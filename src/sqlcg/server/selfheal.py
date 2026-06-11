"""Version-skew detector for the MCP server self-healing feature (issue #76).

PR 1 ships `read_ondisk_version()` and `detect_skew()` — pure detection, no
re-exec machinery.  The re-exec path (`maybe_self_heal`, `_self_heal_watcher`,
`_reexec`) ships in PR 2 under the same 1.20.0 version.

See plan/sprints/mcp_server_self_healing.md §Design D1.
"""

from __future__ import annotations

import importlib.metadata as im

from sqlcg import __version__


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


def detect_skew() -> tuple[bool, str, str | None]:
    """Compare the on-disk distribution version against the running process's baked-in version.

    Returns a three-tuple ``(skew, running_version, ondisk_version)`` where:
    - ``skew`` is ``True`` when the on-disk version differs from ``__version__``
      AND the on-disk version is readable (``None`` → no skew, see F1).
    - ``running_version`` is ``sqlcg.__version__`` — the constant baked in at
      import time; it does NOT change when disk changes (that asymmetry is the
      skew signal).
    - ``ondisk_version`` is the result of ``read_ondisk_version()``.

    Guards PR-plan/sprints/mcp_server_self_healing.md §D1 / Step 1.3.
    """
    ondisk = read_ondisk_version()
    if ondisk is None:
        return (False, __version__, None)
    skew = ondisk != __version__
    return (skew, __version__, ondisk)
