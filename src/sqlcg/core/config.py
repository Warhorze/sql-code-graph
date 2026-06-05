"""Configuration management for sqlcg."""

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sqlcg.core.graph_db import GraphBackend


class DbConfig(BaseModel):
    """Configuration for the DuckDB backend."""

    db_path: Path = Field(default_factory=lambda: Path.home() / ".sqlcg" / "graph.db")
    log_path: Path = Field(
        default_factory=lambda: Path.home() / ".sqlcg" / "index.log",
        description="Path for parse-warning log file written during indexing",
    )

    @classmethod
    def from_env(cls) -> "DbConfig":
        """Load database config from environment variables.

        Returns:
            DbConfig instance with environment-overridden values if present.
        """
        env_path = os.getenv("SQLCG_DB_PATH")
        env_log = os.getenv("SQLCG_LOG_PATH")
        return cls(
            db_path=Path(env_path) if env_path else Path.home() / ".sqlcg" / "graph.db",
            log_path=Path(env_log) if env_log else Path.home() / ".sqlcg" / "index.log",
        )


def get_db_path() -> Path:
    """Get the database path from environment or use default.

    Returns:
        Path to the DuckDB database file
    """
    return DbConfig.from_env().db_path


def config_file_present(path: Path) -> bool:
    """Return True when a .sqlcg.toml file exists at the given directory.

    Single source of truth for the config filename so callers never hard-code
    ".sqlcg.toml" independently.

    Args:
        path: Directory to check for .sqlcg.toml

    Returns:
        True if path/.sqlcg.toml exists, False otherwise.
    """
    return (Path(path) / ".sqlcg.toml").exists()


def get_dialect(path: Path) -> str:
    """Get the SQL dialect from .sqlcg.toml or fall back to snowflake.

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        SQL dialect string (e.g., "snowflake", "bigquery", "postgres")
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            dialect = config.get("sqlcg", {}).get("dialect")
            if dialect:
                return dialect
        except Exception:
            pass
    return "snowflake"


def get_schema_aliases(path: Path) -> dict[str, str]:
    """Get schema alias mappings from .sqlcg.toml.

    Reads [sqlcg.schema_aliases] and returns a lowercased staging-schema →
    canonical-schema dict.  Use this when a staging area sits under a different
    schema but the table names are identical, e.g.::

        [sqlcg.schema_aliases]
        da_tmp = "da"
        ba_tmp = "ba"

    Any table reference whose schema part matches a key is rewritten to use the
    canonical schema instead, so ``da_tmp.my_table`` is traced as ``da.my_table``.

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        Dict mapping staging schema name (lowercase) to its canonical replacement
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("schema_aliases", {})
            if isinstance(raw, dict):
                return {k.lower(): v for k, v in raw.items() if isinstance(v, str)}
        except Exception:
            pass
    return {}


def get_noise_filter_patterns(path: Path) -> list[str]:
    """Get backup table ignore patterns from .sqlcg.toml.

    Reads [sqlcg.noise_filter] -> ignore_table_patterns (a list of glob strings)
    from .sqlcg.toml. Returns the list lowercased. When the key is absent,
    returns a built-in default list::

        [sqlcg.noise_filter]
        ignore_table_patterns = ["*_bck", "*_bck_us", "*_bck_[0-9]*"]

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        List of glob patterns (all lowercased). Defaults to built-in backup patterns.
    """
    default_patterns = [
        "*_bck",
        "*_bck_*",  # catches mid-suffix variants e.g. foo_bck_us39553, bar_bck_archive
        "*_bck_us",
        "*_bck_[0-9]*",
        "*_backup",
        "*_backup_[0-9]*",
    ]
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("noise_filter", {}).get("ignore_table_patterns")
            if isinstance(raw, list):
                return [p.lower() if isinstance(p, str) else p for p in raw]
        except Exception:
            pass
    return default_patterns


def get_ignored_tables(path: Path) -> list[str]:
    """Get explicitly-ignored qualified table names from .sqlcg.toml.

    Complements ``get_noise_filter_patterns`` (glob patterns) with an exact
    qualified-name list, for specific tables that do not follow a backup naming
    convention but should still be dropped from tool answers — e.g. a
    load-control / delta-bookkeeping table::

        [sqlcg.noise_filter]
        ignored_tables = ["ma.rtetl_delta", "ctl.load_log"]

    Names are matched exactly (case-insensitive) against ``schema.table``. The
    lineage engine still records these as real edges; this only lets a user
    declare them noise in config rather than baking the judgment into code.

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        List of qualified table names (all lowercased). Defaults to an empty list.
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("noise_filter", {}).get("ignored_tables")
            if isinstance(raw, list):
                return [t.lower() for t in raw if isinstance(t, str)]
        except Exception:
            pass
    return []


def get_ignore_table_regexes(path: Path) -> list[str]:
    """Get table-exclusion regexes from .sqlcg.toml.

    Complements ``get_noise_filter_patterns`` (anchored fnmatch globs) and
    ``get_ignored_tables`` (exact names) with full regular expressions, for
    backup conventions the globs cannot express — e.g. a ``_bck`` marker that
    can appear anywhere in the name, not just as a suffix::

        [sqlcg.noise_filter]
        ignore_table_regexes = ["_bck", "_tmp_[0-9]{8}"]

    Each pattern is matched (``re.search``, case-insensitive) against the full
    qualified ``schema.table`` name, so an unanchored ``_bck`` excludes
    ``ba.foo_bck`` and ``da.bar_bck_archive`` alike (the latter is missed by the
    suffix-anchored ``*_bck`` glob). The
    lineage engine still records these as real edges; this only lets a user
    declare them noise in config rather than baking the judgment into code.

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        List of regex strings (kept verbatim — not lowercased, so character
        classes survive). Defaults to an empty list.
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("noise_filter", {}).get("ignore_table_regexes")
            if isinstance(raw, list):
                return [r for r in raw if isinstance(r, str)]
        except Exception:
            pass
    return []


def get_presentation_prefixes(path: Path) -> list[str]:
    """Get presentation-facing schema prefixes from .sqlcg.toml.

    Reads [sqlcg.presentation] -> schema_prefixes (a list of strings) from
    .sqlcg.toml. Returns the list lowercased. **Defaults to an empty list** when
    the key is absent — when unset, presentation-facing detection is simply off
    (correct generic behaviour for any user). No schema prefix is hardcoded in
    shipped code; a DWH that wants ``ia_`` flagged must declare it::

        [sqlcg.presentation]
        schema_prefixes = ["ia_"]

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        List of schema prefixes (all lowercased). Defaults to an empty list.
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("presentation", {}).get("schema_prefixes")
            if isinstance(raw, list):
                return [p.lower() for p in raw if isinstance(p, str)]
        except Exception:
            pass
    return []


class ExternalConsumerSpec(BaseModel):
    """Specification for a single external downstream consumer declared in .sqlcg.toml."""

    name: str
    consumer_type: str
    consumes: list[str]


def get_external_consumers(path: Path) -> list[ExternalConsumerSpec]:
    """Get external downstream consumer declarations from .sqlcg.toml.

    Reads [[sqlcg.external_consumers]] array-of-tables from .sqlcg.toml. Each
    table must have ``name`` and ``consumes`` (non-empty list). Rows without a
    ``name`` or with an empty ``consumes`` list are silently skipped. The
    ``kind`` field is stored as ``consumer_type`` (lowercased). **Defaults to an
    empty list** when the section is absent — when unset, the ingestion pass is a
    no-op (correct generic behaviour for any user). No hardcoded fallback::

        [[sqlcg.external_consumers]]
        name = "Tableau: Sales Dashboard"
        kind = "tableau"
        consumes = ["ia_sales.fct_orders"]

    Args:
        path: Root directory to search for .sqlcg.toml

    Returns:
        List of ExternalConsumerSpec objects. Defaults to an empty list.
    """
    config_file = Path(path) / ".sqlcg.toml"
    if config_file.exists():
        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
            raw = config.get("sqlcg", {}).get("external_consumers", [])
            if not isinstance(raw, list):
                return []
            specs: list[ExternalConsumerSpec] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name", "")
                if not name or not isinstance(name, str):
                    continue
                consumes_raw = entry.get("consumes", [])
                if not isinstance(consumes_raw, list) or not consumes_raw:
                    continue
                consumes = [c.lower() for c in consumes_raw if isinstance(c, str)]
                if not consumes:
                    continue
                kind = entry.get("kind", "")
                consumer_type = kind.lower() if isinstance(kind, str) else ""
                specs.append(
                    ExternalConsumerSpec(
                        name=name,
                        consumer_type=consumer_type,
                        consumes=consumes,
                    )
                )
            return specs
        except Exception:
            pass
    return []


def get_backend(read_only: bool = False) -> "GraphBackend":
    """Get a DuckDBBackend instance.

    The ``read_only`` parameter is accepted for API compatibility but is
    ignored — DuckDB uses a single R/W handle for the process lifetime.
    Concurrent read safety is provided by DuckDB's MVCC (readers see a
    consistent snapshot during an in-flight write transaction).

    Cross-process access: whichever process opens the DuckDB file first holds
    an exclusive lock; other processes cannot open it at all (even read-only).
    CLI read commands therefore route through the live MCP server via
    ``read_client.run_read_routed`` (v1.2.0) when a server is live, and open
    the file directly only when no server is running.

    Args:
        read_only: Ignored for DuckDB. Accepted for API compatibility.

    Returns:
        A DuckDBBackend instance.

    Raises:
        duckdb.IOException: If the file is locked by another process.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend

    cfg = DbConfig.from_env()
    return DuckDBBackend(str(cfg.db_path))
