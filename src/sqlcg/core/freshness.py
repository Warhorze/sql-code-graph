"""Shared freshness helper — computes indexed-vs-HEAD delta for any repo root.

This module is deliberately free of backend imports so it can be unit-tested
with only a real git repo and no KuzuDB instance.  Callers obtain the
``indexed_sha`` from the backend and pass it in.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Freshness:
    """Snapshot of how fresh the indexed graph is relative to the current HEAD."""

    indexed_sha: str | None
    """Git SHA recorded at the last successful index run (from SchemaVersion)."""

    head_sha: str | None
    """Current HEAD SHA of the repo at *root*. None when root is not a git repo."""

    stale_by_commits: int | None
    """How many commits HEAD is ahead of *indexed_sha*.

    - ``0``  → graph is up to date.
    - ``N>0`` → graph is N commits behind HEAD.
    - ``None`` → distance is unknown (indexed SHA not in history, shallow clone,
      or git unavailable).
    """

    dirty: bool
    """True when the working tree contains uncommitted changes (whole-tree check)."""

    branch: str | None
    """Current branch name (``git rev-parse --abbrev-ref HEAD``), or None."""


# ---------------------------------------------------------------------------
# Internal git helper
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    """Run a git command; return stdout stripped, or None on any failure.

    Never raises — on any subprocess error or non-zero exit code, returns None.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_freshness(root: Path, indexed_sha: str | None) -> Freshness:
    """Compute freshness relative to the git repo at *root*.

    Returns a :class:`Freshness` with all-None/False fields when *root* is not
    a git repo or git is unavailable — **never raises**.

    Args:
        root: Filesystem path used as the ``cwd`` for git commands.  This is
            typically the ``r.path`` value read from the ``Repo`` graph node.
        indexed_sha: The SHA recorded by ``DuckDBBackend.get_indexed_sha()``.
            May be ``None`` when the graph was never indexed, or a sentinel
            like ``"<head>+dirty"`` written by ``index --include-working-tree``.
    """
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    dirty_out = _git(root, "status", "--porcelain")
    dirty = bool(dirty_out)

    stale: int | None = None
    if head and indexed_sha:
        if indexed_sha == head:
            stale = 0
        else:
            raw = _git(root, "rev-list", "--count", f"{indexed_sha}..{head}")
            if raw is not None:
                try:
                    stale = int(raw)
                except ValueError:
                    stale = None
            # If indexed_sha is unknown (rebased/shallow), raw is None → stale stays None

    return Freshness(
        indexed_sha=indexed_sha,
        head_sha=head,
        stale_by_commits=stale,
        dirty=dirty,
        branch=branch,
    )


def render_freshness_line(f: Freshness) -> str:
    """Return a one-line human summary of graph freshness.

    Examples::

        "indexed at abc12345 (up to date)"
        "indexed at abc12345 (2 commit(s) behind HEAD)"
        "indexed at abc12345 (2 commit(s) behind HEAD, working tree dirty)"
        "indexed at abc12345 (commit distance unknown (SHA not in history))"
        "freshness: not available (graph was never indexed from a git repo)"
    """
    if f.indexed_sha is None:
        return "freshness: not available (graph was never indexed from a git repo)"

    parts: list[str] = []
    if f.stale_by_commits is None:
        parts.append("commit distance unknown (SHA not in history)")
    elif f.stale_by_commits == 0:
        parts.append("up to date")
    else:
        parts.append(f"{f.stale_by_commits} commit(s) behind HEAD")
    if f.dirty:
        parts.append("working tree dirty")

    summary = ", ".join(parts)
    sha8 = f.indexed_sha[:8]
    return f"indexed at {sha8} ({summary})"
