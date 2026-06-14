"""Git-diff helper: compute the set of changed SQL files between two SHAs.

Returns a Delta namedtuple (added, modified, deleted) containing sets of
absolute Path objects. Returns None on git failure, shallow clone, or unknown SHA
so callers fall back to a full index_repo.

Only .sql files that are not excluded by the repo's ignore spec are included —
the same predicate used by the file watcher's SqlFileEventHandler._is_sql.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from sqlcg.utils.ignore import is_ignored, load_ignore_spec
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@dataclass
class Delta:
    """Sets of absolute paths produced by a git diff between two SHAs."""

    added: set[Path] = field(default_factory=set)
    modified: set[Path] = field(default_factory=set)
    deleted: set[Path] = field(default_factory=set)


def _is_sql_not_ignored(path: Path, root: Path, spec: pathspec.PathSpec) -> bool:
    """Return True iff path is a .sql file not excluded by the ignore spec."""
    return path.suffix == ".sql" and not is_ignored(path, root, spec)


def _get_current_head(root: Path) -> str | None:
    """Return ``git rev-parse HEAD`` for *root*, or None on any failure.

    Never raises — on any subprocess error or non-zero exit code, returns None.
    Mirrors ``freshness._git`` to avoid a core-layer import here.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def git_name_status_delta(root: Path, old_sha: str, new_sha: str) -> Delta | None:
    """Compute added/modified/deleted .sql file sets between two git SHAs.

    Runs ``git diff --name-status <old_sha> <new_sha>`` in *root*, filters to
    non-ignored .sql files, and returns absolute paths.  Renames are split into a
    delete of the old path and an add of the new path.

    Args:
        root:    Repository root (passed as cwd to git).
        old_sha: Base commit SHA (the previously-indexed state).
        new_sha: New commit SHA (the current HEAD or branch tip).

    Returns:
        Delta with absolute Path sets, or None on any git failure (including
        unknown SHA, shallow clone, or git not available).  Callers MUST fall
        back to a full index_repo when None is returned.
    """
    root = root.resolve()  # guard: caller may pass a relative path (e.g. Path("."))
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", old_sha, new_sha],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.debug("git not found — cannot compute delta")
        return None
    except Exception as exc:
        logger.debug("git diff failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.debug(
            "git diff --name-status %s %s failed (rc=%d): %s",
            old_sha,
            new_sha,
            result.returncode,
            result.stderr.strip(),
        )
        return None

    spec = load_ignore_spec(root)
    delta = Delta()

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        # Format: <status>\t<path>  (or R<score>\t<old>\t<new> for renames)
        parts = line.split("\t")
        status = parts[0].upper()

        if status.startswith("R") and len(parts) == 3:
            # Rename: treat as delete old + add new
            old_rel = parts[1]
            new_rel = parts[2]
            old_abs = (root / old_rel).resolve()
            new_abs = (root / new_rel).resolve()
            if _is_sql_not_ignored(old_abs, root, spec):
                delta.deleted.add(old_abs)
            if _is_sql_not_ignored(new_abs, root, spec):
                delta.added.add(new_abs)
        elif len(parts) == 2:
            rel_path = parts[1]
            abs_path = (root / rel_path).resolve()
            if not _is_sql_not_ignored(abs_path, root, spec):
                continue
            if status == "A":
                delta.added.add(abs_path)
            elif status in ("M", "T"):
                # T = type-change (e.g. symlink -> file); treat as modified
                delta.modified.add(abs_path)
            elif status == "D":
                delta.deleted.add(abs_path)
            elif status.startswith("C") and len(parts) == 3:
                # Copy: new file is added; original unchanged
                new_abs = (root / parts[2]).resolve()
                if _is_sql_not_ignored(new_abs, root, spec):
                    delta.added.add(new_abs)
            # Unknown status codes are silently ignored — conservative fallback
        else:
            logger.debug("Unrecognised git diff line: %r", line)

    return delta
