"""Noise filter for backup tables and schema-alias mirrors."""

import fnmatch
import re
from pathlib import Path

from sqlcg.core.config import (
    get_ignore_table_regexes,
    get_ignored_tables,
    get_noise_filter_patterns,
    get_schema_aliases,
)


class NoiseFilter:
    """Config-driven filter for backup tables and schema-alias mirrors.

    Loaded once per tool call from DbConfig.from_env().db_path parent dir
    (falls back to cwd). All methods are pure — no graph calls.
    """

    def __init__(
        self,
        patterns: list[str],
        schema_aliases: dict[str, str],
        ignored_tables: list[str] | None = None,
        ignore_regexes: list[str] | None = None,
    ) -> None:
        """Initialize the noise filter.

        Args:
            patterns: List of glob patterns for backup table names (matched on the
                table-name part, anchored as fnmatch globs — e.g. ``*_bck``).
            schema_aliases: Dict mapping staging schema names to canonical schema names.
            ignored_tables: Exact qualified table names (schema.table) to drop,
                matched case-insensitively. Complements ``patterns`` for tables
                that do not follow a backup naming convention.
            ignore_regexes: Regular expressions matched (``re.search``,
                case-insensitive) against the full qualified name. Complements the
                anchored globs for conventions they cannot express — e.g. a
                ``_bck`` marker appearing anywhere in the name. Invalid patterns
                are skipped (they never match) rather than raising.
        """
        self.patterns = patterns
        self.schema_aliases = schema_aliases
        self.ignored_tables = {t.lower() for t in (ignored_tables or [])}
        self.ignore_regexes: list[re.Pattern[str]] = []
        for pattern in ignore_regexes or []:
            try:
                self.ignore_regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                # A malformed user regex must not break filtering for every table.
                continue

    def is_noise(self, table_qualified: str) -> bool:
        """Return True if table_qualified is noise.

        A node is noise when any of the three layers matches:
        1. its qualified name is in the ``ignored_tables`` exact list (case-insensitive);
        2. its table-name part matches a backup glob pattern (fnmatch);
        3. its full qualified name matches an ``ignore_regexes`` pattern (re.search).

        Args:
            table_qualified: A qualified table name (schema.table).

        Returns:
            True if the table matches any ignore layer.
        """
        # Exact qualified-name match against the explicit ignore list.
        if table_qualified.lower() in self.ignored_tables:
            return True

        # Extract just the table part (after the last dot) for glob matching.
        if "." in table_qualified:
            table_name = table_qualified.split(".")[-1]
        else:
            table_name = table_qualified

        for pattern in self.patterns:
            if fnmatch.fnmatch(table_name, pattern):
                return True

        # Regex layer: match against the full qualified name (unanchored).
        for regex in self.ignore_regexes:
            if regex.search(table_qualified):
                return True

        return False

    def canonical(self, table_qualified: str) -> str:
        """Return the canonical form of a table_qualified, resolving alias mirrors.

        Alias resolution applies at the schema level only. If a table's schema
        matches an entry in schema_aliases, no structural rewrite occurs — the
        alias is purely for identity unification. The table_qualified is returned
        unchanged (alias applies to schema, not table prefix).

        Args:
            table_qualified: A qualified table name (schema.table).

        Returns:
            The canonical form of the table_qualified (alias resolution at schema level).
        """
        # For schema-level unification: aliases do not rewrite structure.
        # This method returns the input unchanged because the alias mapping
        # (e.g., ia_analytics -> ba) applies at schema-level identity, not at
        # structural table-prefix rewriting. Per Scenario B in the plan:
        # canonical("ia_analytics.ba_wtfe_verkoopinfo") returns unchanged
        # (no structural rewrite, schema-level alias unification only).
        return table_qualified

    def filter_nodes(
        self,
        nodes: list[str],
        report_excluded: bool = True,
    ) -> tuple[list[str], list[str]]:
        """Return (kept, excluded). Excluded contains noise nodes.

        Args:
            nodes: List of table_qualified or col_id strings.
            report_excluded: If True, noise nodes are included in the excluded list.

        Returns:
            Tuple of (kept nodes, excluded noise nodes).
        """
        kept = []
        excluded = []

        for node in nodes:
            if self.is_noise(node):
                excluded.append(node)
            else:
                kept.append(node)

        return kept, excluded

    @classmethod
    def from_config(cls, repo_root: Path | None = None) -> "NoiseFilter":
        """Construct a NoiseFilter from config files.

        Calls get_noise_filter_patterns(), get_schema_aliases(),
        get_ignored_tables(), and get_ignore_table_regexes() from config.py and
        constructs a NoiseFilter.

        Args:
            repo_root: Root directory to search for .sqlcg.toml. Falls back to
                cwd when None, same convention as get_dialect(path).

        Returns:
            A configured NoiseFilter instance.
        """
        if repo_root is None:
            repo_root = Path.cwd()
        else:
            repo_root = Path(repo_root)

        patterns = get_noise_filter_patterns(repo_root)
        schema_aliases = get_schema_aliases(repo_root)
        ignored_tables = get_ignored_tables(repo_root)
        ignore_regexes = get_ignore_table_regexes(repo_root)

        return cls(
            patterns=patterns,
            schema_aliases=schema_aliases,
            ignored_tables=ignored_tables,
            ignore_regexes=ignore_regexes,
        )
