"""Base data models and abstract parser for SQL parsing and lineage extraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Module-level imports so tests can patch sqlcg.parsers.base.qualify / build_scope
from sqlglot.optimizer.qualify import qualify as sqlglot_qualify
from sqlglot.optimizer.scope import build_scope as sqlglot_build_scope

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
    role: str = "table"

    def __post_init__(self) -> None:
        """Normalize identity components to lowercase.

        C2 design: lowercase catalog, db, and name so that full_id and graph keys
        are lowercase, matching the schema side (ARCHITECTURE_REVIEW §2.6).
        alias is NOT lowercased — it is not an identity field.
        """
        if self.catalog is not None:
            object.__setattr__(self, "catalog", self.catalog.lower())
        if self.db is not None:
            object.__setattr__(self, "db", self.db.lower())
        if self.name:
            object.__setattr__(self, "name", self.name.lower())

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

    def __post_init__(self) -> None:
        """Normalize the column name to lowercase and strip surrounding quotes.

        C2 design: lowercase the name so that full_id and graph keys
        are lowercase, matching the schema side. sqlglot's lineage API
        preserves surrounding double-quotes on quoted identifiers; strip them
        so `"ma_aantal"` and `ma_aantal` map to the same graph node.
        """
        if self.name:
            name = self.name
            if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                name = name[1:-1]
            object.__setattr__(self, "name", name.lower())

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
        start_line: 1-based start line of statement in file; 0 = unknown (scripting-path sentinel)
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
    start_line: int = 0  # 1-based start line of statement in file; 0 = unknown


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

    def __init__(
        self,
        schema_resolver: "SchemaResolver",
        schema_aliases: dict[str, str] | None = None,
    ):
        """Initialize parser with schema resolver.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
            schema_aliases: Optional staging-schema → canonical-schema map
                (e.g. {"ba_tmp": "ba"}) applied at TableRef construction time
        """
        from sqlcg.utils.logging import getLogger

        self._schema = schema_resolver
        self._schema_aliases: dict[str, str] = schema_aliases or {}
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
                        ref = self._apply_table_alias(self._convert_table_expr_to_ref(table_expr))
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

    def _apply_table_alias(self, ref: "TableRef | None") -> "TableRef | None":
        """Remap ref.db if it matches a schema alias (e.g. ba_tmp → ba)."""
        if ref is None or not self._schema_aliases or not ref.db:
            return ref
        aliased = self._schema_aliases.get(ref.db.lower())
        if aliased is None:
            return ref
        import dataclasses

        return dataclasses.replace(ref, db=aliased)

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
                            confidence=1.0,
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

    def _table_node_to_ref(self, table_node: Any) -> "TableRef | None":
        """Convert a sqlglot exp.Table AST node to a TableRef.

        Used by the #49 mis-bind override to enumerate candidate source tables
        from a CTE body's FROM/JOIN once per CTE body (before the per-projection
        loop).  Does NOT call qualify/build_scope — preserves the O(1)-per-body
        perf invariant.

        Schema aliases are applied via _apply_table_alias so the emitted edges
        carry the canonical (post-alias) table identity.

        Args:
            table_node: sqlglot.expressions.Table AST node

        Returns:
            TableRef with catalog/db/name extracted and alias-resolved, or None
            if the alias resolution returns None (treated as an unresolvable ref).
        """
        return self._apply_table_alias(
            TableRef(
                catalog=table_node.catalog if table_node.catalog else None,
                db=table_node.db if table_node.db else None,
                name=table_node.name if table_node.name else str(table_node),
            )
        )

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
            return self._apply_table_alias(
                TableRef(
                    catalog=source.catalog if hasattr(source, "catalog") else None,
                    db=source.db if hasattr(source, "db") else None,
                    name=source.name if hasattr(source, "name") else str(source),
                )
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
        scope: Any | None = None,
    ) -> "LineageExtraction":
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
            query_sources: List of TableRef for source tables used for star resolution
            scope: Pre-built sqlglot Scope for the query body (optional optimization).
                When provided, sg_lineage() will reuse this scope instead of re-qualifying.
                If not provided, a scope will be built from the extracted body before each
                lineage extraction. The scope must be built from the same body as passed to
                sg_lineage(). Passing an incorrect scope will produce wrong lineage silently.

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

            body_scope = None
            combined_sources = {**(sources or {})}

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

            # Build body_scope ONCE per statement, before the column loop, and reuse
            # it for every column (CLAUDE.md invariant: "body_scope built once per
            # statement"). If schema-qualify fails, retry schema-free so we STILL get
            # a scope for the copy=False fast path; only if both fail do we fall back
            # to the per-column sources= path. Building this lazily inside the loop
            # (regressed in 4234e5d) meant a single qualify failure re-ran
            # expand+qualify+build_scope for EVERY column → O(N_cols) full-body
            # deepcopies per statement (measured: 229 qualify calls on one 460-line file).
            if scope is None:
                expanded_body = body
                expand_sources = {
                    k: v for k, v in (sources or {}).items() if isinstance(v, exp.Query)
                }
                if expand_sources:
                    try:
                        expanded_body = exp.expand(
                            body,
                            expand_sources,  # type: ignore
                            dialect=self.DIALECT,
                            copy=True,
                        )
                    except Exception:
                        expanded_body = body
                try:
                    qualified_body = qualify(
                        expanded_body,
                        dialect=self.DIALECT,
                        schema=schema,
                        validate_qualify_columns=False,
                        identify=False,
                    )
                    body_scope = build_scope(qualified_body)
                except Exception as _qualify_exc:
                    out.errors.append(
                        f"col_lineage_skip:qualify_failed:{type(_qualify_exc).__name__}"
                    )
                    # Schema-free retry: still yields a scope for the copy=False path.
                    try:
                        qualified_body = qualify(
                            expanded_body,
                            dialect=self.DIALECT,
                            validate_qualify_columns=False,
                            identify=False,
                        )
                        body_scope = build_scope(qualified_body)
                    except Exception:
                        body_scope = None

            # INSERT positional column-list mapping (#25 fix).
            # Compute the positional_col_names skip-set BEFORE the main column loop
            # so the main loop can skip positions already handled here.
            #
            # When an INSERT has an explicit column list (INSERT INTO t (c1, c2) SELECT ...),
            # the target column name at position idx is authoritative — the SELECT alias is
            # cosmetic for the SELECT and meaningless to the INSERT target. This block
            # overrides alias attribution for ALL positions (aliased or not).
            #
            # Guards applied here mirror the main column loop to preserve skip markers:
            #   - Star expressions → emit col_lineage_skip:star, register pos, skip sg_lineage
            #   - Pure-literal (no Column descendant) → register pos, skip sg_lineage (silent)
            #   - Unaliased non-Column (func/arith/CASE) → emit col_lineage_skip:func_fallback,
            #     register pos, skip sg_lineage
            #   - Plain Column / aliased expression → call sg_lineage (the #25 happy path)
            #
            # CLAUDE.md invariant: body_no_with = body.copy() + strip-WITH happens ONCE
            # before the inner loop; only the single projection is swapped per column.
            positional_col_names: dict[int, str] = {}  # idx → insert_col_name
            if isinstance(stmt, exp.Insert) and isinstance(stmt.this, exp.Schema):
                insert_cols_list = [c.name for c in stmt.this.expressions]
                # Build the WITH-stripped body ONCE here, before any per-column loop.
                # Only the single projection is swapped per column below.
                body_no_with = body.copy()
                body_no_with.set("with_", None)
                for _ins_idx, _col_expr in enumerate(col_expressions):
                    if _ins_idx >= len(insert_cols_list):
                        break
                    _insert_col = insert_cols_list[_ins_idx]
                    if not _insert_col:
                        continue
                    # Register position first so the main loop always skips it,
                    # regardless of which guard fires below.
                    positional_col_names[_ins_idx] = _insert_col

                    # Guard 1: Star projection — emit skip marker (same as main loop).
                    _inner_for_guard = (
                        _col_expr.this if isinstance(_col_expr, exp.Alias) else _col_expr
                    )
                    if isinstance(_inner_for_guard, exp.Star) or (
                        isinstance(_inner_for_guard, exp.Column)
                        and isinstance(_inner_for_guard.this, exp.Star)
                    ):
                        _qualifier = (
                            _inner_for_guard.table
                            if isinstance(_inner_for_guard, exp.Column)
                            else None
                        )
                        out.errors.append(f"col_lineage_skip:star:{_qualifier or '<unqualified>'}")
                        continue  # no sg_lineage for star

                    # Guard 2: Pure-literal — no Column descendants, nothing to trace.
                    if not list(_col_expr.find_all(exp.Column)):
                        continue  # silent skip, no sg_lineage

                    # NOTE: do NOT emit func_fallback here for unaliased non-Column
                    # expressions (functions, arithmetic, CASE …). The main loop emits
                    # func_fallback for such expressions because a plain SELECT/CREATE VIEW
                    # gives them no output column name. The positional INSERT column list
                    # DOES supply that name (_insert_col): below we wrap the expression as
                    # Alias(expr, _insert_col) and let sg_lineage trace through it — exactly
                    # as the aliased form (e.g. `DATE(col) AS a`) already resolves. Guard 2
                    # (above) already dropped genuinely-untraceable pure-literal expressions
                    # (no Column descendant). Skipping column-containing expressions here would
                    # make the #25 positional feature do its work and then discard the result,
                    # dropping real lineage edges (regressed by eb19f29; broke COALESCE).

                    # Positional mapping always wins — replace (or add) the alias with the
                    # INSERT target column name regardless of SELECT alias.
                    if _col_expr.alias and _col_expr.alias != _insert_col:
                        self._log.debug(
                            "INSERT positional override: SELECT alias %r → INSERT col %r"
                            " at position %d",
                            _col_expr.alias,
                            _insert_col,
                            _ins_idx,
                        )
                    # If the expression is already an Alias(inner, old_alias), unwrap it
                    # before re-wrapping — otherwise we produce Alias(Alias(inner, x), c1)
                    # which serialises as "inner AS x AS c1" (syntax error).
                    _inner = _col_expr.this if isinstance(_col_expr, exp.Alias) else _col_expr
                    _aliased = exp.Alias(this=_inner.copy(), alias=_insert_col)
                    body_no_with.set("expressions", [_aliased])
                    _patched_sql = body_no_with.sql(dialect=self.DIALECT)
                    # Pass sources= (not scope=) here: the patched SQL is a freshly
                    # serialised string — the scope was built from the original body AST
                    # and does not correspond to this new string.
                    #
                    # Use `sources` (the cross-statement temp/CTAS map), NOT
                    # `combined_sources`. combined_sources additionally carries the
                    # SAME-STATEMENT CTE bodies (added above). Since body_no_with strips
                    # the WITH clause from the patched SQL, those CTE names become opaque
                    # source relations — passing their bodies as sources= would expand them
                    # away, collapsing intermediate CTE→target hops into the deepest source
                    # (regressed by eb19f29; broke the MA_AANTAL_OP_ORDER anchor link 5).
                    # Cross-statement temps (e.g. CREATE TEMP TABLE t) live in `sources`
                    # and SHOULD still expand (E36 multi-temp: t → src).
                    try:
                        _root = sg_lineage(
                            _insert_col,
                            _patched_sql,
                            dialect=self.DIALECT,
                            sources=sources or {},
                        )
                        if _root:
                            _new_edges = self._lineage_node_to_edges(
                                _root,
                                dst_col_name=_insert_col,
                                dst_table=dst_table,
                                path=path,
                                out=out,
                            )
                            edges.extend(_new_edges)
                    except Exception:
                        pass

            # Extract output columns — skip positions handled by the positional INSERT block
            for loop_idx, col_expr in enumerate(col_expressions):
                if loop_idx in positional_col_names:
                    continue  # positional INSERT block already emitted this column
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

                # Skip pure-literal expressions (NULL, numeric/string constants,
                # zero-arg functions like CURRENT_TIMESTAMP()). sg_lineage would raise
                # "Cannot find column '<literal>'" and emit a noise E1 error. An
                # expression with at least one real exp.Column descendant is NOT skipped.
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
                    # Unaliased expression (function call, arithmetic, CASE WHEN, …).
                    # The historical fallback str(col_expr)[:40] passed garbage like
                    # "YEAR(BOEKDATUM_FIS)" into sg_lineage and produced E2 noise.
                    # Record a structured skip and continue — the target column is unnamed,
                    # so the downstream graph cannot model the lineage anyway.
                    # (T-09-03: FIX-E2-FUNC-FALLBACK)
                    expr_type = type(col_expr).__name__
                    out.errors.append(f"col_lineage_skip:func_fallback:{expr_type}")
                    continue
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
                    # When a scope is available it embeds full column→table resolution.
                    # On the qualify-failed fallback path (no scope), pass only the small
                    # set of file-level sources so sg_lineage can resolve CTEs/CTAS bodies.
                    active_scope = scope if scope is not None else body_scope
                    sg_kwargs: dict = {"dialect": self.DIALECT}
                    if active_scope is not None:
                        # scope= path: the pre-built scope already embeds full
                        # column→table resolution. copy=False + trim_selects=False
                        # suppress sqlglot's per-call AST deepcopy and per-column
                        # trim — neither is needed when the scope is built once and
                        # reused across all columns. Dropping these (regressed in
                        # 4234e5d) makes lineage() deepcopy the whole scope per
                        # column → O(columns × scope_size) (measured: 3.2M deepcopy
                        # calls / ~3.8s on a 359-line file).
                        sg_kwargs["scope"] = active_scope
                        sg_kwargs["copy"] = False
                        sg_kwargs["trim_selects"] = False
                    else:
                        sg_kwargs["sources"] = sources or {}
                    root = sg_lineage(col_name, body, **sg_kwargs)
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
                            # Treat the CTE as a synthetic destination table (role="cte")
                            cte_dst_table = TableRef(name=cte_alias, role="cte")
                            # The CTE's body is its SELECT expression (or UNION ALL, etc.)
                            cte_body = cte.this
                            # Accept both Select and Union (UNION ALL / UNION DISTINCT)
                            if not isinstance(cte_body, (exp.Select, exp.Union)):
                                continue

                            # For Union bodies, use the deepest left-branch Select's projections.
                            # Union.expressions is always empty; projections are on Union.this.
                            # For N=2: cte_body.this is a Select — the while loop is a no-op.
                            # For N≥3: cte_body.this is a nested Union (A UNION ALL B UNION ALL C
                            # parses as Union(Union(A,B),C)), so we walk down to the deepest
                            # left-branch Select (whose star qualify() already expanded in place).
                            if isinstance(cte_body, exp.Union):
                                projection_source = cte_body.this
                                while isinstance(projection_source, exp.Union):
                                    projection_source = projection_source.this
                            else:
                                projection_source = cte_body

                            # Serialize CTE body once before the per-column loop.
                            # Must serialize: passing the AST subtree causes a segfault
                            # (parent pointers create cycles in sqlglot's qualify()).
                            # Hoisted here so sg_lineage receives the same pre-serialized
                            # string for every column rather than re-serializing O(N_cols) times.
                            cte_body_sql = cte_body.sql(dialect=self.DIALECT)

                            # Compute the candidate source-table set ONCE per CTE body
                            # (before the per-projection loop) — never inside it.
                            # Uses find_all(exp.Table) on the already-built AST; does NOT
                            # call qualify/build_scope, preserving O(1) per CTE body.
                            # This set is reused across all projections to detect ambiguity
                            # (#49 mis-bind override).
                            cte_source_tables: list[TableRef] = [
                                ref
                                for t in cte_body.find_all(exp.Table)
                                if t.name  # skip anonymous / subquery placeholders
                                for ref in (self._table_node_to_ref(t),)
                                if ref is not None
                            ]

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

                                # #49 mis-bind override: detect bare (unqualified) columns
                                # in a ≥2-table CTE body.
                                #
                                # sqlglot's sg_lineage (called with no schema and no scope)
                                # re-qualifies the CTE body internally and mis-binds bare
                                # columns to the last-joined table — emitting a confident
                                # WRONG edge (confirmed live: bare `m` from fact_daily was
                                # bound to dim_time). This is not a missing edge; it is an
                                # incorrect one.
                                #
                                # Override: when any bare column appears inside the projection
                                # expression AND the CTE body has ≥2 source tables, skip
                                # sg_lineage for this projection and instead emit one
                                # CTE_PROJECTION_AMBIGUOUS edge per candidate source table
                                # (confidence=0.5). Over-attribution is the safe failure mode
                                # for impact analysis; a wrong single edge is not acceptable.
                                #
                                # Single-table bodies: no ambiguity; existing path unchanged.
                                # Qualified columns in any body: no bare columns; existing path.
                                bare_cols_in_expr = [
                                    c
                                    for c in cte_col_expr.find_all(exp.Column)
                                    if not c.table  # bare = no qualifier
                                ]
                                if bare_cols_in_expr and len(cte_source_tables) >= 2:
                                    # Emit one ambiguous edge per candidate source table.
                                    # bare_col.name is the column name to attribute;
                                    # for aggregates/CASE the bare col name is the leaf.
                                    bare_col_name = bare_cols_in_expr[0].name or cte_col_name
                                    dst_col_ref = ColumnRef(cte_dst_table, cte_col_name)
                                    for src_tbl in cte_source_tables:
                                        edges.append(
                                            LineageEdge(
                                                src=ColumnRef(src_tbl, bare_col_name),
                                                dst=dst_col_ref,
                                                transform="CTE_PROJECTION_AMBIGUOUS",
                                                confidence=0.5,
                                            )
                                        )
                                    continue  # skip sg_lineage for this projection

                                try:
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
