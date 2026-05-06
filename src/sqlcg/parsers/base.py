"""Base data models and abstract parser for SQL parsing and lineage extraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlcg.lineage.schema_resolver import SchemaResolver


class QueryKind(StrEnum):
    """SQL query classification types."""

    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE_TABLE = "CREATE_TABLE"
    CREATE_VIEW = "CREATE_VIEW"
    CREATE_PROC = "CREATE_PROC"
    MERGE = "MERGE"
    OTHER = "OTHER"


class ParseQuality(StrEnum):
    """File-level parse quality assessment."""

    FULL = "FULL"
    TABLE_ONLY = "TABLE_ONLY"
    SCRIPTING_FALLBACK = "SCRIPTING_FALLBACK"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TableRef:
    """A reference to a table (immutable).

    Attributes:
        catalog: Optional catalog name (e.g., "my_warehouse")
        db: Optional database/schema name (e.g., "public")
        name: Table name (always required)
        alias: Optional alias used in the query context
    """

    catalog: str | None = None
    db: str | None = None
    name: str = ""
    alias: str | None = None

    @property
    def full_id(self) -> str:
        """Return the fully qualified table identifier.

        Includes all non-None components: catalog.db.name or db.name or name.
        Used as the Table.qualified key in the graph.
        """
        parts = [p for p in (self.catalog, self.db, self.name) if p]
        return ".".join(parts)

    @property
    def qualified(self) -> str:
        """Alias for full_id for compatibility."""
        return self.full_id


@dataclass(frozen=True)
class ColumnRef:
    """A reference to a column (immutable).

    Attributes:
        table: The TableRef this column belongs to
        name: Column name
    """

    table: TableRef
    name: str = ""

    @property
    def full_id(self) -> str:
        """Return the fully qualified column identifier.

        Format: table.full_id.column_name
        Used as the Column.id key in the graph (finding 5.4).
        """
        return f"{self.table.full_id}.{self.name}"


@dataclass(frozen=True)
class LineageEdge:
    """A column-level lineage edge (immutable by design for hashing).

    Attributes:
        src: Source ColumnRef
        dst: Destination ColumnRef
        transform: Description of the transformation applied (e.g., "PASS_THROUGH", "AGGREGATION")
        confidence: Confidence score (0.0 to 1.0)
        query_id: Optional identifier of the query that created this edge
    """

    src: ColumnRef
    dst: ColumnRef
    transform: str = "UNKNOWN"
    confidence: float = 1.0
    query_id: str | None = None

    def __hash__(self) -> int:
        """Support hashing for use in sets/dicts."""
        return hash((self.src, self.dst, self.transform, self.confidence, self.query_id))

    def __eq__(self, other: object) -> bool:
        """Support equality comparison."""
        if not isinstance(other, LineageEdge):
            return NotImplemented
        return (
            self.src == other.src
            and self.dst == other.dst
            and self.transform == other.transform
            and self.confidence == other.confidence
            and self.query_id == other.query_id
        )


@dataclass
class QueryNode:
    """A parsed SQL query node (mutable by design for pass-2 patching).

    This class is intentionally mutable to support the two-pass resolution strategy.
    Pass 1 extracts initial lineage; pass 2 patches QueryNode fields directly.
    Do not freeze this class.

    Note: Do not rely on QueryNode identity after pass 2; use dataclasses.replace()
    to create a new instance if mutation semantics are a concern.

    Attributes:
        file: Path to the source file
        statement_index: 0-based index of the statement in the file
        sql: The original SQL text
        kind: Query type (SELECT, INSERT, UPDATE, DELETE, CREATE_TABLE, CREATE_VIEW, etc.)
        target: Optional TableRef for INSERT/CREATE targets
        sources: List of source TableRefs
        ctes: Dict of CTE names to their TableRef definitions
        column_lineage: List of LineageEdge objects
        parse_failed: Whether parsing failed (fallback to table-only lineage)
        confidence: Overall confidence score for this query's lineage
        parsing_mode: How the query was parsed (e.g., "sqlglot", "fallback", "scripting")
    """

    file: Path
    statement_index: int
    sql: str
    kind: str = ""
    target: TableRef | None = None
    sources: list[TableRef] = field(default_factory=list)
    ctes: dict[str, TableRef] = field(default_factory=dict)
    column_lineage: list[LineageEdge] = field(default_factory=list)
    parse_failed: bool = False
    confidence: float = 1.0
    parsing_mode: str = "sqlglot"


@dataclass
class ParsedFile:
    """Result of parsing a single SQL file (mutable for aggregation).

    Attributes:
        path: Path to the source file
        dialect: SQL dialect used for parsing
        statements: List of QueryNode objects parsed from the file
        defined_tables: List of TableRef for tables defined in this file
        referenced_tables: List of TableRef for tables referenced in this file
        errors: List of error messages encountered during parsing
        parse_quality: File-level quality assessment
    """

    path: Path
    dialect: str | None = None
    statements: list[QueryNode] = field(default_factory=list)
    defined_tables: list[TableRef] = field(default_factory=list)
    referenced_tables: list[TableRef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    parse_quality: ParseQuality = ParseQuality.TABLE_ONLY

    @property
    def path_str(self) -> str:
        """Return the path as a string."""
        return str(self.path)


class SqlParser(ABC):
    """Abstract base class for SQL parsers.

    Attributes:
        DIALECT: SQL dialect identifier (None for ANSI, "snowflake", "bigquery", etc.)
        _schema: SchemaResolver instance for table/column lookups
        _log: Logger instance for this parser
    """

    DIALECT: str | None = None

    def __init__(self, schema_resolver: "SchemaResolver"):
        """Initialize parser with schema resolver.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
        """
        from sqlcg.utils.logging import getLogger

        self._schema = schema_resolver
        self._log = getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def parse_file(self, path: Path, sql: str) -> ParsedFile:
        """Parse SQL text and return a ParsedFile with all statements.

        Args:
            path: Path to the source file
            sql: SQL text to parse

        Returns:
            ParsedFile containing parsed statements and metadata
        """
        ...

    def _classify(self, stmt: Any) -> str:
        """Classify a SQL statement into a kind string.

        Args:
            stmt: sqlglot AST node

        Returns:
            One of: SELECT, INSERT, UPDATE, DELETE, CREATE_TABLE, CREATE_VIEW,
            CREATE_PROC, MERGE, OTHER
        """
        import sqlglot.expressions as exp

        match stmt:
            case exp.Select():
                return QueryKind.SELECT
            case exp.Insert():
                return QueryKind.INSERT
            case exp.Update():
                return QueryKind.UPDATE
            case exp.Delete():
                return QueryKind.DELETE
            case exp.Create():
                match stmt.kind:
                    case "TABLE":
                        return QueryKind.CREATE_TABLE
                    case "VIEW":
                        return QueryKind.CREATE_VIEW
                    case "PROCEDURE" | "FUNCTION":
                        return QueryKind.CREATE_PROC
                    case _:
                        return QueryKind.CREATE_TABLE  # Default to table
            case exp.Merge():
                return QueryKind.MERGE
            case _:
                return QueryKind.OTHER

    def _real_tables(self, scope: Any) -> list[TableRef]:
        """Return real (non-CTE) tables referenced in a scope.

        Args:
            scope: sqlglot scope object

        Returns:
            List of TableRef objects for non-CTE tables
        """
        if not scope:
            return []

        cte_sources = set(scope.cte_sources.keys()) if hasattr(scope, "cte_sources") else set()
        tables = []

        if hasattr(scope, "tables"):
            # scope.tables is a list of Table expressions
            table_list = scope.tables
            if isinstance(table_list, list):
                for table_expr in table_list:
                    # Extract the table name to check if it's a CTE
                    table_name = None
                    if hasattr(table_expr, "name"):
                        table_name = table_expr.name

                    if table_name not in cte_sources:
                        # Convert table expression to TableRef
                        ref = self._convert_table_expr_to_ref(table_expr)
                        if ref:
                            tables.append(ref)

        return tables

    @staticmethod
    def _convert_table_expr_to_ref(table_expr: Any) -> "TableRef | None":
        """Convert a table expression to a TableRef.

        Args:
            table_expr: Table expression (e.g., Table, Identifier, Schema)

        Returns:
            TableRef or None
        """
        import sqlglot.expressions as exp

        match table_expr:
            case exp.Table():
                return TableRef(
                    catalog=table_expr.catalog,
                    db=table_expr.db,
                    name=table_expr.name,
                )
            case exp.Identifier():
                return TableRef(name=table_expr.name)
            case exp.Schema():
                # Schema wraps the actual table
                return SqlParser._convert_table_expr_to_ref(table_expr.this)
            case _:
                # Try to extract name from string representation
                name = str(table_expr)
                if name:
                    return TableRef(name=name)
                return None

    def _lineage_node_to_edges(
        self,
        root: Any,
        dst_col_name: str,
        dst_table: "TableRef | None",
        path: Path,
        out: ParsedFile,
    ) -> list[LineageEdge]:
        """Walk the sqlglot LineageNode tree and emit LineageEdge objects.

        sqlglot.lineage.Node has attributes:
        - name: column name at this node
        - source: upstream source expression (usually a Table node)
        - downstream: list of child Node objects (columns this node feeds)

        Each leaf in the tree represents a source column. The walk stops at
        nodes whose source is a Table (a real table reference, not a CTE alias).

        Args:
            root: The LineageNode returned by sg_lineage()
            dst_col_name: The output column name (destination)
            path: Source file path (for error recording)
            out: ParsedFile for error recording

        Returns:
            List of LineageEdge objects (may be empty if tree is malformed)
        """

        edges: list[LineageEdge] = []
        visited: set[int] = set()  # guard against cycles

        def _walk(node: Any) -> None:
            if id(node) in visited:
                return
            visited.add(id(node))

            # If this node has downstream nodes, recurse
            if hasattr(node, "downstream") and node.downstream:
                for child in node.downstream:
                    _walk(child)
            else:
                # Leaf node — extract the source table and column
                try:
                    src_table_ref = self._lineage_node_to_table_ref(node)
                    if src_table_ref is None:
                        return
                    src_col_name = (
                        node.name.split(".")[-1]
                        if hasattr(node, "name") and node.name
                        else dst_col_name
                    )
                    dst_tbl = dst_table if dst_table else TableRef(name="<output>")
                    edges.append(
                        LineageEdge(
                            src=ColumnRef(src_table_ref, src_col_name),
                            dst=ColumnRef(dst_tbl, dst_col_name),
                            transform="SELECT",
                            confidence=0.9,
                        )
                    )
                except Exception as exc:
                    out.errors.append(f"col_lineage:tree_walk:{dst_col_name}:{exc}")
                    self._log.warning(
                        "LineageNode walk failed: file=%s col=%s error=%s",
                        path,
                        dst_col_name,
                        exc,
                    )

        _walk(root)
        return edges

    def _lineage_node_to_table_ref(self, node: Any) -> "TableRef | None":
        """Extract a TableRef from a sqlglot LineageNode's source attribute.

        Args:
            node: sqlglot.lineage.Node instance

        Returns:
            TableRef if source is a real Table, None for subqueries or CTEs
        """
        import sqlglot.expressions as exp

        source = getattr(node, "source", None)
        if source is None:
            return None
        if isinstance(source, exp.Table):
            return TableRef(
                catalog=source.catalog if hasattr(source, "catalog") else None,
                db=source.db if hasattr(source, "db") else None,
                name=source.name if hasattr(source, "name") else str(source),
            )
        # Subquery or CTE — return None (cannot resolve to a concrete table)
        return None

    def _extract_column_lineage(
        self,
        stmt: Any,
        path: Path,
        out: ParsedFile,
        schema: dict | None,
        dst_table: "TableRef | None" = None,
        sources: dict[str, Any] | None = None,
    ) -> list[LineageEdge]:
        """Extract column-level lineage with structured error recording.

        On sqlglot.lineage failure: log WARNING, append to ParsedFile.errors,
        emit LineageEdge with confidence=0.0, continue (do NOT raise or skip silently).

        For columns not found in the schema, emits edges with reduced confidence (0.5)
        rather than skipping silently (only applies when schema is non-empty).

        Args:
            stmt: sqlglot AST node (Select/Insert/Create)
            path: Path to the source file
            out: ParsedFile object to append errors to
            schema: Schema dict from _schema.as_dict()
            dst_table: Target table for lineage edges (e.g., for INSERT/CREATE)
            sources: Map of table names to SELECT bodies for temp table resolution

        Returns:
            List of LineageEdge objects
        """
        import sqlglot.expressions as exp
        from sqlglot.lineage import lineage as sg_lineage

        edges: list[LineageEdge] = []

        # Only extract column lineage for certain statement types
        if not isinstance(stmt, (exp.Select, exp.Insert, exp.Create)):
            return edges

        # Extract column references from SELECT list or target
        try:
            # Get the body of the query for lineage extraction
            if isinstance(stmt, exp.Select):
                body = stmt
                col_expressions = stmt.expressions
            elif isinstance(stmt, exp.Create) and isinstance(
                stmt.expression, (exp.Select, exp.Subquery)
            ):
                # CREATE VIEW/TABLE AS SELECT — extract from the SELECT body
                # Handle both bare SELECT and parenthesised (SELECT) forms
                body = stmt.expression
                if isinstance(body, exp.Subquery):
                    body = body.this
                col_expressions = body.expressions
            elif isinstance(stmt, exp.Insert) and isinstance(stmt.expression, exp.Select):
                # INSERT INTO ... SELECT — extract from the SELECT body
                body = stmt.expression
                col_expressions = body.expressions
            else:
                return edges

            # Extract output columns
            for col_expr in col_expressions:
                # Skip star projections — sg_lineage requires a concrete column name.
                if isinstance(col_expr, exp.Star) or (
                    isinstance(col_expr, exp.Column) and isinstance(col_expr.this, exp.Star)
                ):
                    qualifier = col_expr.table if isinstance(col_expr, exp.Column) else None
                    out.errors.append(f"col_lineage_skip:star:{qualifier or '<unqualified>'}")
                    continue

                if col_expr.alias:
                    col_name = col_expr.alias
                elif isinstance(col_expr, exp.Column):
                    col_name = col_expr.name
                    if not col_name or col_name == "*":
                        out.errors.append("col_lineage_skip:star:<empty_name>")
                        continue
                else:
                    # Expression with no resolvable name (e.g. ROUND(...), CAST(...))
                    # — sg_lineage requires a plain column name, skip these
                    continue
                # Schema validation: if schema is loaded and column isn't in it,
                # emit a reduced-confidence edge rather than a full-confidence one.
                if schema:
                    table_cols: list[str] | None = None
                    for _scope_name, cols in schema.items():
                        if isinstance(cols, list):
                            table_cols = cols
                            break
                        elif isinstance(cols, dict):
                            for _db, tables in cols.items():
                                if isinstance(tables, dict):
                                    for _tbl, tcols in tables.items():
                                        if col_name in tcols:
                                            table_cols = tcols
                                            break
                    if table_cols is not None and col_name not in table_cols:
                        self._log.warning(
                            "column %s not found in schema, emitting low-confidence edge",
                            col_name,
                        )
                        edges.append(
                            LineageEdge(
                                src=ColumnRef(TableRef(None, None, "<unknown>"), col_name),
                                dst=ColumnRef(TableRef(None, None, "<unknown>"), col_name),
                                transform="UNKNOWN",
                                confidence=0.5,
                            )
                        )
                        continue

                try:
                    root = sg_lineage(
                        col_name, body, schema=schema, sources=sources or {}, dialect=self.DIALECT
                    )
                    if root:
                        # Successfully extracted lineage — walk tree and emit edges
                        new_edges = self._lineage_node_to_edges(
                            root,
                            dst_col_name=col_name,
                            dst_table=dst_table,
                            path=path,
                            out=out,
                        )
                        edges.extend(new_edges)
                        if not new_edges:
                            self._log.debug(
                                "sg_lineage returned root but no edges emitted: file=%s col=%s",
                                path,
                                col_name,
                            )
                except Exception as exc:
                    self._log.warning(
                        "column lineage extraction failed: file=%s col=%s error=%s",
                        path,
                        col_name,
                        exc,
                    )
                    out.errors.append(f"col_lineage:{col_name}:{exc}")
                    # Emit a zero-confidence placeholder edge
                    edges.append(
                        LineageEdge(
                            src=ColumnRef(TableRef(None, None, "<unknown>"), col_name),
                            dst=ColumnRef(TableRef(None, None, "<unknown>"), col_name),
                            transform="UNKNOWN",
                            confidence=0.0,
                        )
                    )

        except Exception as exc:
            self._log.warning(
                "column lineage extraction failed for entire statement: file=%s error=%s",
                path,
                exc,
            )
            out.errors.append(f"col_lineage:statement:{exc}")

        return edges
