"""SchemaResolver for managing table schema information and resolving table/view references.

Thread-safety: a Lock guards all cache mutations. The lock is re-entrant only
within a single thread. Do not share a SchemaResolver instance across concurrent
jobs — construct one per re-index job instead (see jobs.py).
"""

import copy
import threading
from pathlib import Path
from typing import Any

from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class SchemaResolver:
    """Manages table schema information for SQL parsing and column lineage.

    Attributes:
        dialect: SQL dialect (e.g., "snowflake", "bigquery", None for ANSI)
        _tables: Internal dict of (catalog, db, table) -> [col_names]
        _view_bodies: Mapping of view names to ParsedFile objects
        _lock: threading.Lock protecting mutations and cache
        _cache: Manual cache dict for as_dict() results
    """

    def __init__(self, dialect: str | None = None):
        """Initialize SchemaResolver.

        Args:
            dialect: Optional SQL dialect for normalization
        """
        self.dialect = dialect
        self._tables: dict[tuple[str | None, str | None, str], list[str]] = {}
        self._view_bodies: dict[str, Any] = {}  # str -> ParsedFile
        self._lock = threading.Lock()
        self._cache: dict | None = None

    def add_create_table(self, ast: Any) -> None:
        """Parse a CREATE TABLE AST node and register the table schema.

        Args:
            ast: sqlglot AST node (exp.Create)
        """
        import sqlglot.expressions as exp

        if not isinstance(ast, exp.Create):
            return

        # Extract table name (catalog, db, table)
        table_expr = ast.this
        if not table_expr:
            return

        # If this is a Schema node, extract the table from it
        if isinstance(table_expr, exp.Schema):
            actual_table = table_expr.this
        else:
            actual_table = table_expr

        # Parse the table reference
        catalog, db, table_name = self._extract_table_parts(actual_table)
        if not table_name:
            return

        # Extract column names from the CREATE statement
        # Walk the AST to find all ColumnDef nodes
        col_names = []
        for node in ast.walk():
            if isinstance(node, exp.ColumnDef):
                col_names.append(node.name)

        with self._lock:
            self._tables[(catalog, db, table_name)] = col_names
            self._cache = None  # Invalidate cache

    def add_view_sources(self, sources: dict[str, Any]) -> None:
        """Register view-to-source-table mapping.

        Args:
            sources: Mapping of view names to ParsedFile objects
        """
        with self._lock:
            self._view_bodies.update(sources)
            self._cache = None  # Invalidate cache

    def add_dbt_manifest(self, manifest_path: str | Path) -> None:
        """Load and register schemas from a dbt manifest.

        Args:
            manifest_path: Path to dbt manifest.json
        """
        try:
            import json
            from pathlib import Path

            manifest_path = Path(manifest_path)
            if not manifest_path.exists():
                logger.warning("dbt manifest not found: %s", manifest_path)
                return

            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)

            # Extract table schemas from manifest nodes
            nodes = manifest.get("nodes", {})
            with self._lock:
                for _node_id, node_data in nodes.items():
                    if node_data.get("resource_type") not in ("table", "view"):
                        continue

                    # Parse dbt node metadata
                    table_name = node_data.get("name", "")
                    database = node_data.get("database", "")
                    schema = node_data.get("schema", "")

                    if not table_name:
                        continue

                    # Extract column names from columns metadata
                    col_names = list(node_data.get("columns", {}).keys())

                    # Store in internal dict
                    key = (database if database else None, schema if schema else None, table_name)
                    self._tables[key] = col_names

                self._cache = None  # Invalidate cache

        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load dbt manifest %s: %s", manifest_path, exc)

    def add_information_schema(self, csv_path: str | Path) -> None:
        """Load table schemas from an information_schema CSV.

        Args:
            csv_path: Path to CSV file

        Raises:
            NotImplementedError: This feature is deferred to v2.
        """
        raise NotImplementedError("--schema-from-info-schema is not yet implemented (v2)")

    def as_dict(self) -> dict:
        """Return the schema as a nested dict: {catalog: {db: {table: [cols]}}}.

        Returns:
            A deep copy of the cached schema dict. Mutations by the caller
            do not affect the internal cache.
        """
        with self._lock:
            if self._cache is None:
                self._cache = self._build_dict()
            return copy.deepcopy(self._cache)

    def _build_dict(self) -> dict:
        """Build the nested schema dictionary (called only under self._lock).

        Returns:
            Nested dictionary structure
        """
        out: dict = {}
        for (cat, db, name), cols in self._tables.items():
            cur = out
            for k in [cat, db]:
                if k:
                    cur = cur.setdefault(k, {})
            cur[name] = cols
        return out

    @staticmethod
    def _extract_table_parts(table_expr: Any) -> tuple[str | None, str | None, str]:
        """Extract catalog, db, and table name from a table expression.

        Args:
            table_expr: sqlglot table expression

        Returns:
            Tuple of (catalog, db, table_name)
        """
        import sqlglot.expressions as exp

        match table_expr:
            case exp.Table():
                # table.name is the table identifier
                # table.db is the schema (if present)
                return (
                    table_expr.catalog,
                    table_expr.db,
                    table_expr.name,
                )
            case exp.Identifier():
                return (None, None, table_expr.name)
            case _:
                # Try to extract name from expression
                table_name = table_expr.name if hasattr(table_expr, "name") else ""
                return (None, None, table_name)
