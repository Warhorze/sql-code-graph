"""SQL file walker with ignore pattern support."""

from collections.abc import Iterator
from pathlib import Path

import pathspec

from sqlcg.utils.ignore import is_ignored


def walk_sql_files(root: Path, spec: pathspec.PathSpec) -> Iterator[Path]:
    """Walk directory tree and yield SQL files not matching ignore patterns.

    Args:
        root: Root directory to walk
        spec: PathSpec object with ignore patterns

    Yields:
        Path objects for .sql files not matching ignore patterns
    """
    for path in root.rglob("*.sql"):
        if not is_ignored(path, root, spec):
            yield path
