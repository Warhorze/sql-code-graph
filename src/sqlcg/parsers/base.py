"""Base data models and abstract parser for SQL parsing and lineage extraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

# T-09-01: Import qualify and build_scope at module level so they can be mocked
from sqlglot.optimizer.qualify import qualify as sqlglot_qualify
from sqlglot.optimizer.scope import build_scope as sqlglot_build_scope

# Aliases for clarity in qualify-once pattern
qualify = sqlglot_qualify
build_scope = sqlglot_build_scope

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


@dataclass(frozen=True)
class StarSource:
    """A SELECT * marker for graph-backend resolution.

    Attributes:
        source: The TableRef the star projects (e.g. 'BA.source_table' or alias)
        qualifier: The alias used in the SQL (None for bare 'SELECT *')
    """

    source: TableRef
    qualifier: str | None = None


@dataclass(frozen=True)
class LineageExtraction:
    """Result of column lineage extraction, including both edges and star markers.

    Attributes:
        edges: List of LineageEdge objects extracted from the statement
        star_sources: List of StarSource markers for SELECT * projections
    """

    edges: list[LineageEdge]
    star_sources: list[StarSource]


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
        defined_body: For CTAS statements, the exp.Select or exp.Subquery body being created
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
    star_sources: list[StarSource] = field(default_factory=list)
    defined_columns: list[str] = field(default_factory=list)
    defined_body: Any | None = None


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

    @staticmethod
    def _is_pure_ddl_file(statements: list[Any]) -> bool:
        """Return True if every non-None statement is a DDL Command or pure DDL node
        with no embedded SELECT/INSERT/MERGE/CTAS body.

        A file is pure-DDL when it will never produce column lineage regardless of
        how many times sg_lineage() is called on it. Such files should be skipped
        early to avoid O(N_statements) scope-build and lineage calls.

        Files that mix DDL and DML (e.g., a changelog with CREATE TABLE followed by
        INSERT) are NOT pure-DDL and must not be skipped.
        """
        import sqlglot.expressions as exp

        has_any_stmt = False
        for stmt in statements:
            if stmt is None:
                continue
            has_any_stmt = True
            # Command = sqlglot fallback for unsupported DDL
            if isinstance(stmt, exp.Command):
                continue
            # ALTER, DROP, and other DDL-only statements
            if isinstance(stmt, (exp.Alter, exp.Drop)):
                continue
            # Pure DDL creates (no CTAS body)
            if isinstance(stmt, exp.Create) and not isinstance(
                stmt.expression, (exp.Select, exp.Subquery)
            ):
                continue
            # Anything else (SELECT, INSERT, MERGE, CTAS) — file is NOT pure-DDL
            return False
        return has_any_stmt  # must have at least one statement

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
        mapping_schema_tables: set[tuple[str | None, str | None, str]] | None = None,
    ) -> list[LineageEdge]:
        """Walk the sqlglot LineageNode tree and emit LineageEdge objects.

        sqlglot.lineage.Node has attributes:
        - name: column name at this node
        - source: upstream source expression (usually a Table node)
        - downstream: list of child Node objects (columns this node feeds)

        Each leaf in the tree represents a source column. The walk stops at
        nodes whose source is a Table (a real table reference, not a CTE alias).

        T-09-01: Confidence scoring based on mapping_schema presence.
        - Source table is in mapping_schema → confidence=1.0
        - Source table is not in mapping_schema (inferred) → confidence=0.7

        Args:
            root: The LineageNode returned by sg_lineage()
            dst_col_name: The output column name (destination)
            dst_table: Target table for the destination column
            path: Source file path (for error recording)
            out: ParsedFile for error recording
            mapping_schema_tables: Set of (catalog, db, table) tuples from mapping_schema.
                When None, defaults to empty set (all edges get confidence 0.7).

        Returns:
            List of LineageEdge objects (may be empty if tree is malformed)
        """
        if mapping_schema_tables is None:
            mapping_schema_tables = set()

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

                    # T-09-01: Determine confidence based on schema presence
                    # Check if source table is in mapping_schema
                    schema_key = (src_table_ref.catalog, src_table_ref.db, src_table_ref.name)
                    if schema_key in mapping_schema_tables:
                        confidence = 1.0
                    else:
                        confidence = 0.7

                    edges.append(
                        LineageEdge(
                            src=ColumnRef(src_table_ref, src_col_name),
                            dst=ColumnRef(dst_tbl, dst_col_name),
                            transform="SELECT",
                            confidence=confidence,
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
        query_sources: list["TableRef"] | None = None,
        schema_sources: dict[str, Any] | None = None,
    ) -> "LineageExtraction":
        """Extract column-level lineage with structured error recording.

        T-09-01: Implements qualify-once pattern. The body is qualified exactly once
        (not per column), and the scope is built and cached for reuse across all
        column extraction calls within a single statement.

        On sqlglot.lineage failure: log DEBUG, append to ParsedFile.errors,
        emit LineageEdge with confidence=0.0, continue (do NOT raise or skip silently).

        Args:
            stmt: sqlglot AST node (Select/Insert/Create)
            path: Path to the source file
            out: ParsedFile object to append errors to
            schema: Schema dict from _schema.as_dict()
            dst_table: Target table for lineage edges (e.g., for INSERT/CREATE)
            sources: Map of table names to SELECT bodies for temp table resolution
            query_sources: List of TableRef for source tables used for star resolution
            schema_sources: Map of table names to parsed exp.Select nodes from INFORMATION_SCHEMA

        Returns:
            LineageExtraction with edges and star_sources
        """
        import sqlglot.expressions as exp
        from sqlglot.lineage import lineage as sg_lineage

        edges: list[LineageEdge] = []
        star_sources: list[StarSource] = []

        # NEW (T-07-06): Record MERGE statements explicitly as deferred.
        # sqlglot's lineage() API does not handle MERGE branches; implementing
        # multi-branch lineage is deferred (see plan/sprint_07_open_ecodes.md § T-07-06).
        # TODO: Remove when sqlglot adds MERGE lineage support (T-07-06).
        if isinstance(stmt, exp.Merge):
            dst_name = None
            if stmt.this is not None:
                try:
                    dst_name = stmt.this.name
                except Exception:
                    dst_name = None
            out.errors.append(f"col_lineage_skip:merge_branch:{dst_name or '<unknown>'}")
            return LineageExtraction(edges=edges, star_sources=star_sources)

        # Only extract column lineage for certain statement types
        if not isinstance(stmt, (exp.Select, exp.Insert, exp.Create)):
            return LineageExtraction(edges=edges, star_sources=star_sources)

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
                return LineageExtraction(edges=edges, star_sources=star_sources)

            # T-09-01: Qualify body once and cache scope for reuse across all columns.
            # Replaces the per-column qualify() call that previously dominated wall-time
            # on wide SELECT files (live profiling 2026-05-12: 176-col file × 11 joins
            # → 7-minute stagnation with old per-column pattern).
            combined_sources = {**(sources or {}), **(schema_sources or {})}

            # NEW (T-07-02): Add CTE bodies to combined_sources so that outer columns
            # can resolve through CTE names. This makes CTE-to-CTE chains resolvable.
            # Example: WITH x AS (SELECT a FROM src), y AS (SELECT a FROM x)
            # When processing the outer SELECT referencing y, we need both x and y
            # to be in combined_sources so sqlglot can expand them.
            if isinstance(body, exp.Select) and body.args.get("with_"):
                with_clause = body.args.get("with_")
                if with_clause and hasattr(with_clause, "expressions"):
                    cte_expressions = getattr(with_clause, "expressions", None)
                    if cte_expressions:
                        for cte in cte_expressions:
                            cte_alias = cte.alias
                            # Accept both Select and Union as CTE bodies
                            if cte_alias and isinstance(cte.this, (exp.Select, exp.Union)):
                                # Use lowercase key to match the sources_map convention
                                key = cte_alias.lower()
                                combined_sources[key] = cte.this

            # T-09-01: Qualify the body once and cache the scope for reuse across all columns.
            # This replaces the per-column qualify() rebuild that was happening inside
            # sg_lineage for every column (see historic code around line 688).
            mapping_schema = (
                self._schema.mapping_schema()
                if hasattr(self, "_schema") and self._schema is not None
                else {}
            )

            # T-09-01: Extract the set of (catalog, db, table) tuples from mapping_schema
            # for confidence scoring in _lineage_node_to_edges.
            mapping_schema_tables: set[tuple[str | None, str | None, str]] = set()
            for catalog, db_dict in (mapping_schema or {}).items():
                if isinstance(db_dict, dict):
                    for db, table_dict in db_dict.items():
                        if isinstance(table_dict, dict):
                            for table_name in table_dict.keys():
                                mapping_schema_tables.add((catalog, db, table_name))

            # Get qualified body and cached scope
            qualified_body = body
            cached_scope = None
            try:
                # Expand sources first to inline CTE/temp table definitions
                expanded_body = body
                if combined_sources:
                    sources_to_expand = {
                        k: v
                        for k, v in combined_sources.items()
                        if hasattr(v, "args")  # Filter for AST nodes
                    }
                    if sources_to_expand:
                        expanded_body = exp.expand(
                            body, sources_to_expand, dialect=self.DIALECT, copy=True
                        )
                # Qualify with mapping_schema + infer_schema=True for cross-schema CTE resolution
                qualified_body = qualify(
                    expanded_body,
                    dialect=self.DIALECT,
                    schema=mapping_schema if mapping_schema else None,  # type: ignore
                    validate_qualify_columns=False,
                    infer_schema=True,
                )
                # Build scope once for reuse
                cached_scope = build_scope(qualified_body)
            except Exception as exc:
                # qualify() may fail on dialect edge cases; record and fall back to the
                # raw body. sg_lineage will attempt its own internal qualification per
                # column — slow but correct.
                out.errors.append(f"col_lineage_skip:qualify_failed:{type(exc).__name__}")
                qualified_body = body
                cached_scope = None

            # Extract output columns
            for col_expr in col_expressions:
                # Skip star projections — sg_lineage requires a concrete column name.
                if isinstance(col_expr, exp.Star) or (
                    isinstance(col_expr, exp.Column) and isinstance(col_expr.this, exp.Star)
                ):
                    qualifier = col_expr.table if isinstance(col_expr, exp.Column) else None
                    out.errors.append(f"col_lineage_skip:star:{qualifier or '<unqualified>'}")

                    # NEW: also record a StarSource for graph-backend expansion.
                    # Resolve the source table by alias match, fall back to first source.
                    star_src_table = self._resolve_star_source(
                        qualifier=qualifier or None,
                        sources=query_sources or [],
                    )
                    if star_src_table is not None:
                        star_sources.append(
                            StarSource(source=star_src_table, qualifier=qualifier or None)
                        )
                    continue

                # Skip pure-literal expressions — NULL, numeric/string literals, zero-argument
                # functions like CURRENT_TIMESTAMP(), and arithmetic on literals only. None of
                # these have a source column, so the downstream sg_lineage(...) call would raise
                # "Cannot find column '<literal>' in query" and emit a noise error (E1).
                # An expression containing at least one real exp.Column descendant is NOT skipped
                # and reaches the regular code path unchanged.
                if not list(col_expr.find_all(exp.Column)):
                    continue

                if col_expr.alias:
                    col_name = col_expr.alias
                elif isinstance(col_expr, exp.Column):
                    col_name = col_expr.name
                    if not col_name or col_name == "*":
                        out.errors.append("col_lineage_skip:star:<empty_name>")
                        continue
                else:
                    # Best-effort: try .alias, then .name, then str representation.
                    # sg_lineage may still fail; the existing exception handler records it.
                    col_name = (
                        col_expr.alias
                        or (col_expr.name if hasattr(col_expr, "name") else None)
                        or str(col_expr)[:40]
                    )
                    if not col_name or col_name == "*":
                        out.errors.append("col_lineage_skip:expr_no_name:<empty>")
                        continue
                    out.errors.append(
                        f"col_lineage_attempt:expr_fallback:{type(col_expr).__name__}"
                    )
                # Schema validation: if schema is loaded and column isn't in it,
                # emit a reduced-confidence edge rather than a full-confidence one.
                # Skip when the expression has an explicit alias — the alias is the
                # output column name (e.g. view projections), not a source column, so
                # schema membership is irrelevant and sg_lineage should still run.
                if schema and not col_expr.alias:
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
                    # T-09-01: Use the pre-qualified body and cached scope (built once before
                    # the column loop, not per column). This is the qualify-once pattern.
                    sg_kwargs = {"sources": combined_sources, "dialect": self.DIALECT}
                    if cached_scope is not None:
                        sg_kwargs["scope"] = cached_scope
                    root = sg_lineage(col_name, qualified_body, **sg_kwargs)
                    if root:
                        # Successfully extracted lineage — walk tree and emit edges
                        new_edges = self._lineage_node_to_edges(
                            root,
                            dst_col_name=col_name,
                            dst_table=dst_table,
                            path=path,
                            out=out,
                            mapping_schema_tables=mapping_schema_tables,
                        )
                        edges.extend(new_edges)
                        if not new_edges:
                            out.errors.append(f"col_lineage_skip:dynamic_source:{col_name}")
                            self._log.debug(
                                "sg_lineage returned root but no edges emitted: file=%s col=%s",
                                path,
                                col_name,
                            )
                except Exception as exc:
                    self._log.debug(
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

            # NEW: Extract column lineage for CTE projections as additional destinations
            # (Step 2.1 of T-07-02 — FIX-E5-XFILE-CHAIN).
            # CTEs are intermediate destinations in the lineage chain. We emit edges
            # from sources into each CTE's projected columns, so that queries selecting
            # from the CTE can resolve through the CTE name as an intermediate table.
            # Example: WITH x AS (SELECT a FROM src) SELECT a FROM x
            # produces edges: (SRC, A, x, a) + (SRC, A, <output>, a)
            if isinstance(body, exp.Select) and body.args.get("with_"):
                with_clause = body.args.get("with_")
                if with_clause and hasattr(with_clause, "expressions"):
                    cte_expressions = getattr(with_clause, "expressions", None)
                    if cte_expressions:
                        for cte in cte_expressions:
                            cte_alias = cte.alias
                            if not cte_alias:
                                continue
                            # Treat the CTE as a synthetic destination table
                            cte_dst_table = TableRef(name=cte_alias)
                            # The CTE's body is its SELECT expression (or UNION ALL, etc.)
                            cte_body = cte.this
                            # Accept both Select and Union (UNION ALL / UNION DISTINCT)
                            if not isinstance(cte_body, (exp.Select, exp.Union)):
                                continue

                            # For Union bodies, use the left branch's projections.
                            # Union.expressions is always empty; projections are on Union.this.
                            if isinstance(cte_body, exp.Union):
                                projection_source = cte_body.this
                            else:
                                projection_source = cte_body

                            # For each projection in the CTE, extract lineage.
                            # Only iterate projections from left branch for column names, but pass
                            # entire Union body to sg_lineage so sqlglot resolves both branches.
                            cte_projections = projection_source.expressions
                            for cte_col_expr in cte_projections:
                                # Skip stars in CTEs
                                if isinstance(cte_col_expr, exp.Star) or (
                                    isinstance(cte_col_expr, exp.Column)
                                    and isinstance(cte_col_expr.this, exp.Star)
                                ):
                                    continue
                                # Extract column name (alias or raw column name)
                                cte_col_name = (
                                    cte_col_expr.alias
                                    if cte_col_expr.alias
                                    else (
                                        cte_col_expr.name
                                        if isinstance(cte_col_expr, exp.Column)
                                        else str(cte_col_expr)[:40]
                                    )
                                )
                                if not cte_col_name or cte_col_name == "*":
                                    continue
                                try:
                                    # Serialize to SQL string before sg_lineage.
                                    # Must serialize: passing the AST subtree causes a segfault
                                    # (parent pointers create cycles in sqlglot's qualify()).
                                    cte_body_sql = cte_body.sql(dialect=self.DIALECT)
                                    # No schema: resolver.as_dict() {table:[cols]} triggers
                                    # sqlglot nesting-level errors on fresh string parses.
                                    # sqlglot resolves CTE bodies structurally without schema.
                                    root = sg_lineage(
                                        cte_col_name,
                                        cte_body_sql,
                                        dialect=self.DIALECT,
                                    )
                                    if root:
                                        # Emit edges with dst = CTE name
                                        cte_edges = self._lineage_node_to_edges(
                                            root,
                                            dst_col_name=cte_col_name,
                                            dst_table=cte_dst_table,
                                            path=path,
                                            out=out,
                                            mapping_schema_tables=mapping_schema_tables,
                                        )
                                        # Tag edges as CTE projections
                                        # (transform is read-only, so create new edges)
                                        for edge in cte_edges:
                                            tagged_edge = LineageEdge(
                                                src=edge.src,
                                                dst=edge.dst,
                                                transform="CTE_PROJECTION",
                                                confidence=edge.confidence,
                                            )
                                            edges.append(tagged_edge)
                                except Exception:
                                    self._log.debug(
                                        "CTE lineage failed: %s/%s/%s",
                                        path,
                                        cte_alias,
                                        cte_col_name,
                                    )

            # INSERT column-list aliasing (T-07-02 link 5).
            # When an INSERT has an explicit column list and the SELECT expression has
            # no alias (e.g. SELECT SUM(x) FROM cte), the INSERT column at the same
            # position provides the destination col name. Stripping the WITH clause
            # stops sg_lineage at the CTE name boundary (doesn't expand into bodies).
            if isinstance(stmt, exp.Insert) and isinstance(stmt.this, exp.Schema):
                insert_cols = [c.name for c in stmt.this.expressions]
                for idx, col_expr in enumerate(col_expressions):
                    if idx >= len(insert_cols):
                        break
                    if col_expr.alias:
                        continue  # already handled by the main col loop
                    insert_col = insert_cols[idx]
                    if not insert_col:
                        continue
                    # Build a patched SELECT: strip WITH, alias the expression with the
                    # INSERT column name so sg_lineage can trace it.
                    body_no_with = body.copy()
                    body_no_with.set("with_", None)
                    aliased = exp.Alias(this=col_expr.copy(), alias=insert_col)
                    body_no_with.set("expressions", [aliased])
                    patched_sql = body_no_with.sql(dialect=self.DIALECT)
                    try:
                        root = sg_lineage(insert_col, patched_sql, dialect=self.DIALECT)
                        if root:
                            new_edges = self._lineage_node_to_edges(
                                root,
                                dst_col_name=insert_col,
                                dst_table=dst_table,
                                path=path,
                                out=out,
                                mapping_schema_tables=mapping_schema_tables,
                            )
                            edges.extend(new_edges)
                    except Exception:
                        pass

        except Exception as exc:
            self._log.debug(
                "column lineage extraction failed for entire statement: file=%s error=%s",
                path,
                exc,
            )
            out.errors.append(f"col_lineage:statement:{exc}")

        return LineageExtraction(edges=edges, star_sources=star_sources)

    def _resolve_star_source(
        self,
        qualifier: str | None,
        sources: list["TableRef"],
    ) -> "TableRef | None":
        """Match a star qualifier (alias) to one of the statement's source tables.

        Returns the matched TableRef, or the first source as a fallback when the
        qualifier doesn't match any alias and the SELECT has at least one source.
        Returns None when there are no sources to attach the star to.

        Args:
            qualifier: The alias used in the SQL (e.g., 'base' for SELECT base.*)
            sources: List of source TableRef objects from the statement

        Returns:
            Matched TableRef, first source as fallback, or None if no sources
        """
        if not sources:
            return None
        if qualifier:
            q_lower = qualifier.lower()
            for s in sources:
                # alias match on TableRef.alias OR name match (e.g. SELECT BA.src.*)
                if (s.alias and s.alias.lower() == q_lower) or s.name.lower() == q_lower:
                    return s
        return sources[0]
