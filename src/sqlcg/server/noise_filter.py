"""Noise filter for backup tables and schema-alias mirrors."""

import fnmatch
from pathlib import Path

from sqlcg.core.config import get_noise_filter_patterns, get_schema_aliases


class NoiseFilter:
    """Config-driven filter for backup tables and schema-alias mirrors.

    Loaded once per tool call from KuzuConfig.from_env().db_path parent dir
    (falls back to cwd). All methods are pure — no graph calls.
    """

    def __init__(self, patterns: list[str], schema_aliases: dict[str, str]) -> None:
        """Initialize the noise filter.

        Args:
            patterns: List of glob patterns for backup table names.
            schema_aliases: Dict mapping staging schema names to canonical schema names.
        """
        self.patterns = patterns
        self.schema_aliases = schema_aliases

    def is_noise(self, table_qualified: str) -> bool:
        """Return True if table_qualified matches any backup pattern (fnmatch).

        Args:
            table_qualified: A qualified table name (schema.table).

        Returns:
            True if the table matches any backup pattern.
        """
        # Extract just the table part (after the last dot)
        if "." in table_qualified:
            table_name = table_qualified.split(".")[-1]
        else:
            table_name = table_qualified

        for pattern in self.patterns:
            if fnmatch.fnmatch(table_name, pattern):
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

        Calls get_noise_filter_patterns() and get_schema_aliases() from
        config.py and constructs a NoiseFilter.

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

        return cls(patterns=patterns, schema_aliases=schema_aliases)
