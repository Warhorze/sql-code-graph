"""Core, layer-neutral table-name noise predicate.

This is the single source of truth for "is this qualified table name noise?".
It is intentionally located in ``core/`` (not ``server/``) so that BOTH the
read-side ``server.noise_filter.NoiseFilter`` and the index-side filter in
``indexer.indexer`` can import it **downward** into ``core`` — an
``indexer -> server`` import would be a layering violation / cycle risk.

The three match layers mirror the three existing config readers in
``core.config``:

1. exact qualified-name match against ``ignored_tables`` (case-insensitive);
2. ``fnmatch`` glob match against the bare table-name part
   (``ignore_table_patterns``);
3. ``re.search`` (case-insensitive) of ``ignore_table_regexes`` against the
   full qualified name.

Both the query-time hide (``NoiseFilter``) and the destructive index-time
exclusion (PR-2b) delegate here so the two never drift.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from sqlcg.core.config import (
    get_ignore_table_regexes,
    get_ignored_tables,
    get_noise_filter_patterns,
)


def table_short_name(table_qualified: str) -> str:
    """Extract the bare table name from a qualified key, handling CTE namespacing.

    Namespaced CTE keys have the form ``path/file.sql::cte_name``.  A naive
    ``split(".")[-1]`` would yield ``sql::cte_name`` instead of ``cte_name``.

    Resolution order:
    1. If ``::`` is present, return the segment after the last ``::``.
    2. Otherwise, return the segment after the last ``.``.
    3. If neither delimiter is present, return the whole string.
    """
    if "::" in table_qualified:
        return table_qualified.rsplit("::", 1)[-1]
    if "." in table_qualified:
        return table_qualified.rsplit(".", 1)[-1]
    return table_qualified


class TableNameMatcher:
    """Pure predicate: does a qualified table name match any ignore layer?

    Construct directly from already-read config lists, or via
    :meth:`from_config` to read ``.sqlcg.toml`` once.  No graph calls.
    """

    def __init__(
        self,
        patterns: list[str] | None = None,
        ignored_tables: list[str] | None = None,
        ignore_regexes: list[str] | None = None,
    ) -> None:
        """Initialize the matcher.

        Args:
            patterns: Glob patterns for backup table names, matched on the bare
                table-name part (anchored as ``fnmatch`` globs — e.g. ``*_bck``).
            ignored_tables: Exact qualified names (schema.table) to drop, matched
                case-insensitively.
            ignore_regexes: Regular expressions matched (``re.search``,
                case-insensitive) against the full qualified name.  Invalid
                patterns are skipped (they never match) rather than raising.
        """
        self.patterns = list(patterns or [])
        self.ignored_tables = {t.lower() for t in (ignored_tables or [])}
        self.ignore_regexes: list[re.Pattern[str]] = []
        for pattern in ignore_regexes or []:
            try:
                self.ignore_regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                # A malformed user regex must not break filtering for every table.
                continue

    def matches(self, table_qualified: str) -> bool:
        """Return True if *table_qualified* matches any ignore layer.

        A name is noise when any of the three layers matches:
        1. its qualified name is in ``ignored_tables`` (case-insensitive);
        2. its bare table-name part matches a glob pattern (``fnmatch``);
        3. its full qualified name matches an ``ignore_regexes`` pattern.
        """
        if table_qualified.lower() in self.ignored_tables:
            return True

        table_name = table_short_name(table_qualified)
        for pattern in self.patterns:
            if fnmatch.fnmatch(table_name, pattern):
                return True

        for regex in self.ignore_regexes:
            if regex.search(table_qualified):
                return True

        return False

    # Allow `matcher(name)` as a shorthand predicate.
    __call__ = matches

    @classmethod
    def from_config(cls, repo_root: Path | None = None) -> TableNameMatcher:
        """Build a matcher from the three ``[sqlcg.noise_filter]`` config readers.

        Args:
            repo_root: Root directory to search for ``.sqlcg.toml``.  Falls back
                to cwd when None.
        """
        repo_root = Path.cwd() if repo_root is None else Path(repo_root)
        return cls(
            patterns=get_noise_filter_patterns(repo_root),
            ignored_tables=get_ignored_tables(repo_root),
            ignore_regexes=get_ignore_table_regexes(repo_root),
        )
