"""Utilities for handling .sqlcgignore patterns."""

from pathlib import Path

import pathspec


def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    """Load .sqlcgignore patterns from root directory.

    Args:
        root: Root directory to search for .sqlcgignore

    Returns:
        PathSpec object for matching ignore patterns
    """
    ignore_file = root / ".sqlcgignore"
    if ignore_file.exists():
        patterns = ignore_file.read_text().splitlines()
    else:
        patterns = []
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def is_ignored(path: Path, root: Path, spec: pathspec.PathSpec) -> bool:
    """Check if a path matches any ignore patterns.

    Args:
        path: Path to check
        root: Root directory (for relative path calculation)
        spec: PathSpec object with patterns

    Returns:
        True if the path matches any ignore pattern
    """
    return spec.match_file(str(path.relative_to(root)))
