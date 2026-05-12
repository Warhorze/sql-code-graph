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
        self._cross_file_sources: dict[str, Any] = {}  # str -> exp.Select for CTAS
        # T-09-01: Track catalog per (db, table) for mapping_schema() reconstruction
        self._table_catalogs: dict[tuple[str | None, str | None], str | None] = {}
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

    def register_cross_file_sources(self, sources: dict[str, Any]) -> None:
        """Register CTAS bodies harvested across pass-1 files for cross-file resolution.

        Called once by CrossFileAggregator at the boundary between pass 1 and pass 2.
        The dict is keyed by lowercased bare table name (matches sources_map convention).

        Args:
            sources: Mapping of lowercased table names to exp.Select bodies
        """
        with self._lock:
            self._cross_file_sources = dict(sources)
            self._cache = None  # Invalidate cache

    def cross_file_sources(self) -> dict[str, Any]:
        """Return a copy of cross-file CTAS bodies for seeding sources_map in pass 2.

        Returns:
            Dict of lowercased table names to exp.Select bodies
        """
        with self._lock:
            return dict(self._cross_file_sources)

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

    def add_information_schema(self, csv_path: str | Path) -> int:
        """Load table schemas from an INFORMATION_SCHEMA.COLUMNS CSV.

        Expected columns: TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME,
        COLUMN_NAME, ORDINAL_POSITION. Returns the number of tables loaded.

        Args:
            csv_path: Path to CSV file or file-like object (io.StringIO, etc.)

        Returns:
            Number of tables loaded

        Raises:
            ValueError: If CSV is missing required columns
        """
        import csv as _csv

        required = {
            "TABLE_CATALOG",
            "TABLE_SCHEMA",
            "TABLE_NAME",
            "COLUMN_NAME",
            "ORDINAL_POSITION",
        }
        tables: dict[tuple[str | None, str | None, str], list[tuple[int, str]]] = {}

        # Handle both file paths and file-like objects
        if isinstance(csv_path, (str, Path)):
            f = open(Path(csv_path), newline="", encoding="utf-8")
            should_close = True
        else:
            # Assume it's a file-like object
            f = csv_path
            should_close = False

        catalogs: dict[tuple[str | None, str | None], str | None] = {}
        try:
            reader = _csv.DictReader(f)
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                missing = required - set(reader.fieldnames or [])
                raise ValueError(f"CSV missing required columns: {missing}")
            for row in reader:
                # Store 2-part keys (db, table) in _tables for backward compat
                # T-09-01: Also track catalog separately for mapping_schema()
                db_name = row["TABLE_SCHEMA"] or None
                table_name = row["TABLE_NAME"]
                key_2part = (db_name, table_name)
                key_3part = (None, db_name, table_name)
                tables.setdefault(key_3part, []).append(
                    (int(row["ORDINAL_POSITION"]), row["COLUMN_NAME"])
                )
                # Track catalog for later reconstruction in mapping_schema()
                catalogs[key_2part] = row["TABLE_CATALOG"] or None
        finally:
            if should_close:
                f.close()

        with self._lock:
            for key, cols in tables.items():
                self._tables[key] = [c for _, c in sorted(cols)]
                # Track catalog using (db, table) tuple
                db_name = key[1]
                table_name = key[2]
                self._table_catalogs[(db_name, table_name)] = catalogs[(db_name, table_name)]
            self._cache = None

        return len(catalogs)

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

    def as_sources_dict(self) -> dict[str, Any]:
        """Return a sources= dict for sg_lineage(): {table_name: parsed_SELECT_node}.

        For each table known to the resolver, creates a parsed exp.Select AST node:
            SELECT col1, col2, ... FROM table_name
        This lets qualify() inside sg_lineage() expand CTE references to cross-file
        views whose columns are known from INFORMATION_SCHEMA but whose SQL body is
        not available in the current file.

        Returns:
            Dict mapping table_name (last component) to a parsed exp.Select AST node.
            Keys are lowercased to match sqlglot's qualify() normalisation.

        Note: Parsing once at construction time is preferable to parsing on-demand
        per-call. With ~5k tables and ~144k columns, the memory footprint of ~5k
        parsed exp.Select nodes is acceptable (<50 MB).
        """
        import sqlglot
        import sqlglot.expressions as exp

        with self._lock:
            result: dict[str, Any] = {}
            for (_, db, name), cols in self._tables.items():
                if not cols:
                    continue
                col_list = ", ".join(cols)
                qualified = f"{db}.{name}" if db else name
                sql = f"SELECT {col_list} FROM {qualified}"

                try:
                    parsed = sqlglot.parse_one(sql, dialect=self.dialect, into=exp.Select)
                    # Key is the bare table name (lowercased) — sqlglot expand() uses
                    # the scope name which comes from the CTE or alias, not the full path.
                    result[name.lower()] = parsed
                    # Also register under schema.table key for fully-qualified CTE sources
                    if db:
                        result[f"{db}.{name}".lower()] = parsed
                except Exception:
                    # Parsing failure shouldn't block the resolver; log and skip
                    logger.warning(f"Failed to parse synthetic SELECT for {qualified}: {sql}")

            return result

    def mapping_schema(self) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
        """Return schema in sqlglot's mapping_schema format.

        Shape: ``{catalog: {db: {table: {col: type}}}}``. Used by qualify() to
        resolve cross-schema column references during pass-2 lineage extraction.

        Distinct from as_dict(), which returns the depth-1 ``{table: [cols]}``
        shape used by parser-internal source maps (see base.py).

        Empty dict when no schema has been loaded — qualify() then operates in
        infer-only mode (validate_qualify_columns=False + infer_schema=True),
        which is the small-repo default.

        Returns:
            Nested dictionary {catalog: {db: {table: {col: type}}}}.
            Column types are all "UNKNOWN" (type is not validated by qualify).
        """
        with self._lock:
            out: dict = {}
            for (cat, db, name), cols in self._tables.items():
                # Skip entries without a schema
                if not db:
                    continue

                # T-09-01: Use tracked catalog from add_information_schema if available,
                # otherwise use the (cat, db) key's catalog (which should be None for
                # backward compat entries). For entries loaded from CSV, we look up
                # the catalog from _table_catalogs using (db, name) key.
                catalog = self._table_catalogs.get((db, name), cat)
                if not catalog:
                    # Entries without a catalog cannot be represented in mapping_schema
                    continue

                # Build nested structure
                if catalog not in out:
                    out[catalog] = {}
                if db not in out[catalog]:
                    out[catalog][db] = {}

                # Add table with column->type mapping
                col_dict = {col: "UNKNOWN" for col in cols}
                out[catalog][db][name] = col_dict

            return out

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
