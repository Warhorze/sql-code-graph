"""Noise filter for backup tables and schema-alias mirrors."""

from pathlib import Path

from sqlcg.core.config import (
    get_ignore_table_regexes,
    get_ignored_tables,
    get_noise_filter_patterns,
    get_schema_aliases,
)
from sqlcg.core.noise_match import TableNameMatcher, table_short_name


def _table_short_name(table_qualified: str) -> str:
    """Extract the bare table name from a qualified key, handling CTE namespacing.

    Thin delegate to :func:`sqlcg.core.noise_match.table_short_name` (the single
    core-resident implementation shared by the read-time filter and the
    index-time exclusion).  Kept as a module-level name because tests and
    callers reference it.
    """
    return table_short_name(table_qualified)


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
        # The match logic is owned by the core-resident TableNameMatcher so the
        # read-time hide (here) and the index-time exclusion (PR-2b) never drift.
        self._matcher = TableNameMatcher(
            patterns=patterns,
            ignored_tables=ignored_tables,
            ignore_regexes=ignore_regexes,
        )
        # Preserved attributes for back-compat with callers/tests that inspect them.
        self.ignored_tables = self._matcher.ignored_tables
        self.ignore_regexes = self._matcher.ignore_regexes

    def is_noise(self, table_qualified: str) -> bool:
        """Return True if table_qualified is noise.

        Delegates to the core-resident :class:`TableNameMatcher`, which applies
        the three ignore layers (exact name / glob / regex).  A node is noise
        when any layer matches.

        Args:
            table_qualified: A qualified table name (schema.table).

        Returns:
            True if the table matches any ignore layer.
        """
        return self._matcher.matches(table_qualified)

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
