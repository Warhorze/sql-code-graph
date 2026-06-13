"""SQL file walker with ignore pattern support."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pathspec

from sqlcg.utils.ignore import is_ignored


def _git_sql_files(root: Path) -> list[Path] | None:
    """Return tracked .sql files via git ls-files, or None if git unavailable."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return [root / f for f in result.stdout.splitlines() if f.endswith(".sql")]
    except (subprocess.CalledProcessError, OSError):
        return None


def walk_sql_files(root: Path, spec: pathspec.PathSpec, use_git: bool = True) -> Iterator[Path]:
    """Walk directory tree and yield SQL files not matching ignore patterns.

    When use_git=True (default) and git is available, only tracked files are
    returned — this prevents flooding from build artefacts, node_modules, and
    other untracked directories. Falls back to rglob when git is unavailable
    or the directory is not a git repository.

    Args:
        root: Root directory to walk
        spec: PathSpec object with ignore patterns
        use_git: Use git ls-files instead of rglob (default True)

    Yields:
        Path objects for .sql files not matching ignore patterns
    """
    # Resolve once so both branches yield absolute paths and is_ignored's
    # path.relative_to(root) never encounters a relative-vs-absolute mismatch.
    root = Path(root).resolve()
    if use_git:
        git_files = _git_sql_files(root)
        if git_files is not None:
            for path in sorted(git_files):
                if path.exists() and not is_ignored(path, root, spec):
                    yield path
            return

    for path in sorted(root.rglob("*.sql")):
        if not is_ignored(path, root, spec):
            yield path
