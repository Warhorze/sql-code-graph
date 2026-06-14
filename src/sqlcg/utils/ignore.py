"""Utilities for handling .sqlcgignore patterns."""

from pathlib import Path

import pathspec

from sqlcg.core.config import get_file_ignore_defaults


def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    """Load file-discovery ignore patterns for a repository root.

    The returned spec combines, in order:

    1. Built-in default backup/snapshot globs (issue #27a) from
       ``get_file_ignore_defaults`` — so backup SQL *files* (``*_bck.sql``,
       ``*_backup*.sql``) are skipped at discovery without any user config.
       Conservative and configurable: ``[sqlcg.file_discovery] exclude_backups``
       disables them, ``exclude_dated_backups`` opts into dated forms.
    2. The user's ``.sqlcgignore`` lines, appended last so they extend (or, via a
       ``!`` negation, override) the built-in defaults.

    This is the only file-discovery ignore layer; the walker (``walk_sql_files``),
    the watcher, and git-delta reindex all consume this spec.

    Args:
        root: Root directory to search for .sqlcgignore / .sqlcg.toml

    Returns:
        PathSpec object for matching ignore patterns
    """
    root = Path(root).resolve()  # guard: caller may pass a relative path (e.g. Path("."))
    patterns = get_file_ignore_defaults(root)
    ignore_file = root / ".sqlcgignore"
    if ignore_file.exists():
        patterns = patterns + ignore_file.read_text().splitlines()
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
    root = Path(root).resolve()  # guard: ensure root is absolute before relative_to()
    return spec.match_file(str(path.relative_to(root)))
