"""Base data models and abstract parser for SQL parsing and lineage extraction."""

import dataclasses
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
    namespace: str | None = None  # PR 3: per-file CTE/derived namespacing

    def __post_init__(self) -> None:
        """Normalize identity components to lowercase.

        C2 design: lowercase catalog, db, and name so that full_id and graph keys
        are lowercase, matching the schema side (ARCHITECTURE_REVIEW §2.6).
        alias and namespace are NOT lowercased — they are not identity components
        that need case-folding (namespace is a file path, already lowercase-safe;
        alias is not an identity field).
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

        For real tables: catalog.db.name or db.name or name.
        For CTE/derived nodes (role in ("cte", "derived")) with a namespace set:
        prefixes ``{namespace}::`` to produce a per-file-scoped key, e.g.
        ``etl/sql/fact/wtfa_loonkosten.sql::final``.  This prevents same-named
        CTEs in different files from sharing a graph node (F3 trust defect,
        sprint_lineage_identity_and_session_context.md §PR 3).

        ``db/catalog`` semantics are untouched — namespace only fires when
        role != "table" so physical tables remain fully-qualified as before.
        """
        parts = [p for p in (self.catalog, self.db, self.name) if p]
        bare = ".".join(parts)
        if self.namespace and self.role != "table":
            return f"{self.namespace}::{bare}"
        return bare

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
        inferred_from_source_name: True when dst's column name was taken from the
            SOURCE expression (not an authoritative target catalog) because this is a
            no-column-list positional INSERT with no resolvable target-column catalog.
            Default False — only set True on the genuinely source-named degrade path
            (plan/sprints/positional_insert_clone_blindspot.md Part A2).
    """

    src: ColumnRef
    dst: ColumnRef
    transform: str = "UNKNOWN"
    confidence: float = 1.0
    query_id: str | None = None
    inferred_from_source_name: bool = False

    def __hash__(self) -> int:
        """Support hashing for use in sets/dicts."""
        return hash(
            (
                self.src,
                self.dst,
                self.transform,
                self.confidence,
                self.query_id,
                self.inferred_from_source_name,
            )
        )

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
            and self.inferred_from_source_name == other.inferred_from_source_name
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
        clone_source: For `CREATE TABLE t CLONE src` statements, the TableRef of the
            CLONE source `src` (None for non-CLONE creates). Set by C1
            (plan/sprints/positional_insert_clone_blindspot.md Part C).
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
    clone_source: TableRef | None = None


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


# ---------------------------------------------------------------------------
# Key-normalisation boundary pass (PR 1 — sprint_lineage_identity_and_session_context)
# ---------------------------------------------------------------------------


def _apply_alias_to_ref(ref: "TableRef", aliases: dict[str, str]) -> "TableRef":
    """Rewrite ref.db through the alias map; return the same ref if unchanged.

    Only rewrites when ref.db matches an alias key.  Never rewrites CTE/derived
    refs that have no db (their identity is the bare name, not a schema).
    Does NOT rewrite refs whose full_id is already empty — those are handled by
    the empty-identity guard in normalize_keys.
    """
    if not aliases or not ref.db:
        return ref
    aliased = aliases.get(ref.db.lower())
    if aliased is None:
        return ref
    return dataclasses.replace(ref, db=aliased)


def normalize_keys(parsed: "ParsedFile", aliases: dict[str, str]) -> None:
    """Normalize every TableRef-bearing field in *parsed* through *aliases* in-place.

    This is the key-normalization boundary pass (PR 1 of the lineage-identity sprint,
    plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1).

    Responsibilities:
    1. Apply schema_aliases to every TableRef in the parse result so that no un-aliased
       key can reach the graph, regardless of which parser branch produced the ref.
    2. Reject any TableRef whose full_id is empty (stage reads — the stage name does
       not survive parsing): drop the ref/edge and log a ``col_lineage_skip:stage:``
       error. Never emit a key with a leading dot, an empty table component, or an
       empty SqlTable.qualified.

    Fields rewritten (all TableRef-bearing fields in ParsedFile and QueryNode):
    - ParsedFile.defined_tables  (list[TableRef])
    - ParsedFile.referenced_tables  (list[TableRef])
    - QueryNode.target  (TableRef | None)
    - QueryNode.sources  (list[TableRef])
    - QueryNode.star_sources  (list[StarSource] — via StarSource.source)
    - QueryNode.clone_source  (TableRef | None)
    - LineageEdge.src.table and .dst.table  (via ColumnRef, via dataclasses.replace)

    Intentionally EXCLUDED (see W1 note in the plan):
    - QueryNode.ctes  (dict[str, TableRef]): parser-internal pass-2 state; never read
      by key-construction consumers; excluded deliberately.  The introspection guard
      (TestNormalizeKeysIntrospectionGuard in tests/unit/test_normalize_keys.py)
      enumerates all TableRef-bearing fields and explicitly skips ``ctes``.

    This function mutates *parsed* in-place.  It is O(edges) and runs once per file,
    far outside the per-column hot loop — no perf invariant is affected.

    Args:
        parsed: ParsedFile to normalise (mutated in-place).
        aliases: Schema-alias map from .sqlcg.toml (e.g. ``{"ba_tmp": "ba"}``).
            May be empty; the function is a no-op on empty aliases but still applies
            the empty-identity guard.
    """
    file_path = parsed.path_str

    # ---- ParsedFile-level TableRef lists ------------------------------------

    def _norm_ref(ref: "TableRef") -> "TableRef | None":
        """Alias + empty-identity guard for a single TableRef.

        full_id is empty only when every identity component (catalog, db, name)
        is empty — true for Snowflake stage reads (``FROM @stage``), where the
        stage name does not survive parsing.  The honest representation is to
        drop the ref and log a skip: an empty-keyed node would falsely join
        unrelated stage-loading pipelines.  (The plan's alternative
        ``stage://<name>`` key requires a recoverable name; there is none —
        see PR body §Stage-key decision.)
        """
        ref = _apply_alias_to_ref(ref, aliases)
        if not ref.full_id:
            parsed.errors.append(f"col_lineage_skip:stage:{file_path}")
            return None
        return ref

    parsed.defined_tables = [
        r for ref in parsed.defined_tables if (r := _norm_ref(ref)) is not None
    ]
    parsed.referenced_tables = [
        r for ref in parsed.referenced_tables if (r := _norm_ref(ref)) is not None
    ]

    # ---- QueryNode-level fields ---------------------------------------------

    for stmt in parsed.statements:
        # target
        if stmt.target is not None:
            new_target = _norm_ref(stmt.target)
            stmt.target = new_target  # None is valid (target was a phantom ref)

        # sources
        stmt.sources = [r for ref in stmt.sources if (r := _norm_ref(ref)) is not None]

        # star_sources (StarSource.source is a TableRef)
        new_star_sources: list[StarSource] = []
        for ss in stmt.star_sources:
            new_src = _norm_ref(ss.source)
            if new_src is not None:
                new_star_sources.append(dataclasses.replace(ss, source=new_src))
            # else: drop the star source silently (edge will be absent from graph)
        stmt.star_sources = new_star_sources

        # clone_source
        if stmt.clone_source is not None:
            stmt.clone_source = _norm_ref(stmt.clone_source)

        # column_lineage — LineageEdge and ColumnRef are frozen; rewrite via replace
        new_edges: list[LineageEdge] = []
        for edge in stmt.column_lineage:
            new_src_table = _norm_ref(edge.src.table)
            new_dst_table = _norm_ref(edge.dst.table)
            if new_src_table is None or new_dst_table is None:
                # At least one endpoint resolved to an empty-identity ref; drop edge.
                # The skip was already logged by _guard_stage_ref above.
                continue
            # Rebuild ColumnRef and LineageEdge only when something changed.
            if new_src_table is not edge.src.table or new_dst_table is not edge.dst.table:
                new_src = dataclasses.replace(edge.src, table=new_src_table)
                new_dst = dataclasses.replace(edge.dst, table=new_dst_table)
                edge = dataclasses.replace(edge, src=new_src, dst=new_dst)
            new_edges.append(edge)
        stmt.column_lineage = new_edges

        # NOTE: QueryNode.ctes is intentionally excluded — it is parser-internal
        # pass-2 state (CTE name → TableRef mapping) that is never read by
        # key-construction consumers.  A new TableRef-bearing field added to
        # QueryNode or ParsedFile in the future MUST be added here too; the
        # dataclass-introspection guard test enforces this.


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
        # PR 3 — CTE/derived namespacing: repo-relative path of the file currently
        # being parsed.  Set at the start of parse_file; consumed by
        # _extract_column_lineage when constructing CTE/derived TableRefs.
        # None outside of an active parse_file call.
        self._current_file_namespace: str | None = None
        # temp_table_namespacing — per-file temp identity set; persists across
        # statements within a file (unlike CTE aliases which are per-statement).
        # Populated in _parse_statement when a TEMPORARY CREATE is seen; consumed
        # by source-stamping (ansi_parser.py) and _lineage_node_to_edges (base.py).
        # Reset at every namespace assignment site (file start + file end).
        self._current_file_temp_keys: set[str] = set()

    @abstractmethod
    def parse_file(
        self,
        path: Path,
        sql: str,
        rel_path: str | None = None,
    ) -> ParsedFile:
        """Parse SQL text and return a ParsedFile with all statements.

        Args:
            path: Path to the source file
            sql: SQL text to parse
            rel_path: Repo-relative posix path for CTE/derived namespace keying
                (e.g. "etl/sql/fact/wtfa.sql").  Supplied by index_repo via the
                task dict; falls back to str(path) when None.
                See plan/sprints/sprint_postmortem_fixes.md §PR 3 Step 3.1.

        Returns:
            ParsedFile containing parsed statements and metadata
        """
        ...

    @staticmethod
    def _temp_identity(db: str | None, name: str) -> str:
        """Lowercased schema-qualified temp identity; bare name when db is falsy.

        The ANSI path may have ``db=None`` (no USE-SCHEMA qualification), so the
        key is the bare ``tmp_x``.  The ``f"{db}.{name}"`` form WITHOUT this guard
        would wrongly produce ``"none.tmp_x"``.

        Used byte-identically at three sites (Amendment 3 — temp_table_namespacing.md):
          Step 1.2 — target registration in _parse_statement
          Step 2.1 — source stamping in _parse_statement
          Step 2.2 — lineage-leaf stamping in _lineage_node_to_edges
        """
        name = name.lower()
        return f"{db.lower()}.{name}" if db else name

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

    @staticmethod
    def _can_have_column_lineage(stmt: Any) -> bool:
        """Return True if `_extract_column_lineage` would attempt extraction for `stmt`.

        Mirrors the type/body checks `_extract_column_lineage` (below, this class)
        already performs: only `exp.Select`, `exp.Insert` with a `SELECT` body, and
        `exp.Create` with a `Select`/`Subquery` body (CTAS / CREATE VIEW AS SELECT)
        can ever produce column-lineage edges. `exp.Merge` is explicitly skipped there
        (T-07-06, deferred). Everything else (Update, Delete, Use, Set, Comment, Drop,
        Alter, Command, TruncateTable, CREATE ... LIKE/CLONE/column-defs, INSERT ...
        VALUES) is structurally lineage-free regardless of `build_scope`'s outcome.

        Used by `AnsiParser._parse_statement` to avoid marking `parse_failed=True`
        for statement kinds that were never going to produce column lineage in the
        first place — `build_scope()` returns `None` for nearly all non-`exp.Select`-
        rooted statements by sqlglot design, which is not a degraded extraction for
        these kinds.
        """
        import sqlglot.expressions as exp

        if isinstance(stmt, exp.Select):
            return True
        if isinstance(stmt, exp.Insert):
            return isinstance(stmt.expression, exp.Select)
        if isinstance(stmt, exp.Create):
            return isinstance(stmt.expression, (exp.Select, exp.Subquery))
        return False

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
        return dataclasses.replace(ref, db=aliased)

    @staticmethod
    def _classify_transform(col_expr: Any) -> str:
        """Classify the transform kind for a SELECT-list projection expression.

        AST-only — no serialization, no qualify/build_scope/exp.expand calls.
        One bounded ``find`` on the *single projection expression* (not the body).
        Called once per column in the main loop, before sg_lineage.

        Classification rules (evaluated in order):
        - ``PASS_THROUGH`` — bare ``exp.Column`` with no alias, or alias equal to
          the column name (case-insensitive). The value flows unchanged.
        - ``RENAME`` — bare ``exp.Column`` with an alias that differs from the
          column name. No transformation, just a rename.
        - ``AGGREGATION`` — projection contains at least one ``exp.AggFunc``
          descendant (e.g. SUM, COUNT, MAX). Uses a bounded ``find`` on the single
          projection node — never traverses the whole body.
        - ``EXPRESSION`` — everything else (arithmetic, CAST, CASE WHEN, function
          calls without AggFunc, etc.).

        ``CTE_PROJECTION``/``CTE_PROJECTION_AMBIGUOUS``/``UNKNOWN`` edges are
        produced outside this method and are never passed through it.
        """
        import sqlglot.expressions as exp

        # Unwrap alias to inspect the inner expression.
        inner = col_expr.this if isinstance(col_expr, exp.Alias) else col_expr
        alias: str | None = col_expr.alias if isinstance(col_expr, exp.Alias) else None

        if isinstance(inner, exp.Column):
            col_name = inner.name or ""
            if alias is None or alias.lower() == col_name.lower():
                return "PASS_THROUGH"
            return "RENAME"

        # Bounded find on the single projection — never on the body.
        # exp.Expression.find() returns the matching node directly (or None).
        if col_expr.find(exp.AggFunc) is not None:
            return "AGGREGATION"

        return "EXPRESSION"

    def _lineage_node_to_edges(
        self,
        root: Any,
        dst_col_name: str,
        dst_table: "TableRef | None",
        path: Path,
        out: ParsedFile,
        inferred_from_source_name: bool = False,
        transform: str = "SELECT",
        cte_alias_names: frozenset[str] | None = None,
    ) -> list[LineageEdge]:
        """Walk the sqlglot LineageNode tree and emit LineageEdge objects.

        sqlglot.lineage.Node has attributes:
        - name: column name at this node
        - source: upstream source expression (usually a Table node)
        - downstream: list of child Node objects (columns this node feeds)

        Each leaf in the tree represents a source column. The walk stops at
        nodes whose source is a Table (a real table reference, not a CTE alias).

        Args (new):
            cte_alias_names: names of CTEs defined in the same statement body.
                When provided and a leaf source matches a CTE alias name, the
                TableRef is constructed with role="cte" and namespace=
                self._current_file_namespace (PR 3). This ensures that when
                scope-building fails and sg_lineage returns a CTE name as an
                opaque table, the emitted src key uses the same namespaced form
                as the CTE projection edges — keeping the chain joinable.

        Args:
            root: The LineageNode returned by sg_lineage()
            dst_col_name: The output column name (destination)
            path: Source file path (for error recording)
            out: ParsedFile for error recording
            inferred_from_source_name: Propagated onto every emitted LineageEdge.
                True only for the no-column-list positional-INSERT degrade path where
                dst_col_name was taken from the SOURCE expression because no
                authoritative target-column catalog resolved
                (plan/sprints/positional_insert_clone_blindspot.md Part A2).

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
                    src_table_ref = self._lineage_node_to_table_ref(
                        node, cte_alias_names=cte_alias_names
                    )
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
                            transform=transform,
                            confidence=1.0,
                            inferred_from_source_name=inferred_from_source_name,
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

    def _lineage_node_to_table_ref(
        self,
        node: Any,
        cte_alias_names: "frozenset[str] | None" = None,
    ) -> "TableRef | None":
        """Extract a TableRef from a sqlglot LineageNode's source attribute.

        Args:
            node: sqlglot.lineage.Node instance
            cte_alias_names: Optional set of CTE alias names in the current statement.
                When provided, a leaf source whose name matches a CTE alias is returned
                as a namespaced CTE TableRef (role="cte", namespace=
                self._current_file_namespace) rather than a plain table ref.
                This ensures outer-SELECT edges use the same namespaced key as the
                CTE projection edges, keeping the hop chain joinable (PR 3).

        Returns:
            TableRef if source is a real Table (or a CTE alias when cte_alias_names
            is given), None for subqueries or anonymous CTEs
        """
        import sqlglot.expressions as exp

        source = getattr(node, "source", None)
        if source is None:
            return None
        if isinstance(source, exp.Table):
            name = source.name if hasattr(source, "name") else str(source)
            src_db = source.db if hasattr(source, "db") else None
            # PR 3: if this table name matches a CTE alias in the current statement
            # AND we have a namespace, produce a namespaced CTE TableRef so the
            # outer-SELECT edge key matches the CTE projection edge's dst key.
            if (
                cte_alias_names is not None
                and name.lower() in cte_alias_names
                and self._current_file_namespace is not None
            ):
                return TableRef(
                    name=name,
                    role="cte",
                    namespace=self._current_file_namespace,
                )
            # temp_table_namespacing (Step 2.2 / Amendment 2): if this leaf's
            # schema-qualified identity is registered as a temp in the current file,
            # stamp it as role="temp" with the current file namespace.  Uses the same
            # _temp_identity helper as registration (Step 1.2) so the key is
            # byte-identical — a mismatch here would break the lineage chain.
            #
            # BUGFIX (v1.21.1): registration (Step 1.2) uses the POST-ALIAS db
            # (via _apply_table_alias on the parsed target), but raw exp.Table nodes
            # from sg_lineage carry the PRE-ALIAS db (e.g. 'ba_tmp' before the
            # 'ba_tmp' → 'ba' schema alias).  Without normalising src_db through the
            # alias map first, _temp_identity("ba_tmp","tmp_base") = "ba_tmp.tmp_base"
            # which is NOT in _current_file_temp_keys {"ba.tmp_base"} → the check
            # misses → the un-aliased non-namespaced TableRef falls through to
            # _apply_table_alias → emits a second kind='table' row for the same
            # temp under the aliased key, producing the dual-write observed on the
            # live DWH (483 residual un-namespaced tmp_* kind='table' nodes).
            # Fix: apply the schema alias to src_db before _temp_identity, and use
            # the aliased db in the returned TableRef (same as Step 1.2 does).
            aliased_db: str | None
            if src_db and self._schema_aliases:
                aliased_db = self._schema_aliases.get(src_db.lower(), src_db)
            else:
                aliased_db = src_db
            if (
                self._current_file_temp_keys
                and self._current_file_namespace is not None
                and self._temp_identity(aliased_db, name) in self._current_file_temp_keys
            ):
                return TableRef(
                    catalog=source.catalog if hasattr(source, "catalog") else None,
                    db=aliased_db,
                    name=name,
                    role="temp",
                    namespace=self._current_file_namespace,
                )
            return self._apply_table_alias(
                TableRef(
                    catalog=source.catalog if hasattr(source, "catalog") else None,
                    db=src_db,
                    name=name,
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
        ddl_columns_by_bare: dict[str, list[str]] | None = None,
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
            ddl_columns_by_bare: optional bare-name → ordered DDL column catalog index
                (Part B-catalog; includes CLONE-inherited entries resolved by
                `CrossFileAggregator.resolve_clone_catalogs` before pass-2 dispatch).
                Consulted ONCE PER STATEMENT — never per column — for the no-column-list
                positional-INSERT ordinal mapping
                (plan/sprints/positional_insert_clone_blindspot.md Part B). `None` when
                no aggregator is available (single-file / `reindex_file` path); the
                ordinal mapping then degrades to the source-named +
                `inferred_from_source_name=True` fallback.

        Returns:
            LineageExtraction with edges and star_sources
        """
        import sqlglot.expressions as exp
        from sqlglot.lineage import lineage as sg_lineage

        edges: list[LineageEdge] = []
        star_sources: list[StarSource] = []

        # NEW (T-07-06): Record MERGE statements explicitly as deferred.
        # sqlglot's lineage() API does not handle MERGE branches; implementing
        # multi-branch lineage is deferred (see plan/sprints/sprint_07_open_ecodes.md § T-07-06).
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
            # PR 3: track CTE alias names so _lineage_node_to_edges can namespace them
            # when sg_lineage returns a CTE name as an opaque table source (fallback path).
            cte_alias_names: frozenset[str] = frozenset()
            if isinstance(body, exp.Select) and body.args.get("with_"):
                with_clause = body.args.get("with_")
                if with_clause and hasattr(with_clause, "expressions"):
                    cte_expressions = getattr(with_clause, "expressions", None)
                    if cte_expressions:
                        _cte_names: set[str] = set()
                        for cte in cte_expressions:
                            cte_alias = cte.alias
                            # Accept both Select and Union as CTE bodies
                            if cte_alias and isinstance(cte.this, (exp.Select, exp.Union)):
                                # Use lowercase key to match the sources_map convention
                                key = cte_alias.lower()
                                combined_sources[key] = cte.this
                                _cte_names.add(key)
                        cte_alias_names = frozenset(_cte_names)

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
                    _ins_transform = self._classify_transform(_col_expr)
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
                                transform=_ins_transform,
                                cte_alias_names=cte_alias_names,
                            )
                            edges.extend(_new_edges)
                    except Exception:
                        pass

            # Part B — no-column-list positional-INSERT ordinal mapping.
            # Mirrors the #25 block above (body-copy-once, per-position guards) for the
            # sibling case: `INSERT INTO t SELECT ...` with NO column list
            # (`stmt.this` is not an `exp.Schema`). The SELECT alias is meaningless to
            # the INSERT target here too, but unlike #25 there is no explicit column
            # list to read the authoritative name from — it must come from `t`'s
            # ordered DDL column catalog (`ddl_columns_by_bare`, including
            # CLONE-inherited entries resolved before pass-2 dispatch).
            #
            # Resolved ONCE per statement (a single dict lookup + map build, identical
            # cardinality to #25 — no new per-column qualify/scope/expand work; CLAUDE.md
            # invariants preserved, see plan Performance-invariants table).
            #
            # When no catalog resolves for `t`, every dst the main loop emits for this
            # statement is named from the SOURCE expression — A2 (below, in the main
            # loop) marks those edges `inferred_from_source_name=True`. When the catalog
            # DOES resolve, the mapped positions here carry the authoritative target
            # name and are NOT flagged; only positions beyond the catalog
            # (length-mismatch overflow) fall through to the main loop and ARE flagged
            # (R2 degrade — map the overlap, never raise).
            if (
                isinstance(stmt, exp.Insert)
                and not isinstance(stmt.this, exp.Schema)
                and dst_table is not None
            ):
                _ordinal_catalog: list[str] | None = None
                if ddl_columns_by_bare:
                    _bare = (dst_table.name or "").lower()
                    if _bare:
                        _ordinal_catalog = ddl_columns_by_bare.get(_bare)

                if _ordinal_catalog:
                    # Build the WITH-stripped body ONCE here (mirrors #25); only the
                    # single projection is swapped per mapped column below.
                    _body_no_with_b = body.copy()
                    _body_no_with_b.set("with_", None)
                    for _pos_idx, _pcol_expr in enumerate(col_expressions):
                        if _pos_idx >= len(_ordinal_catalog):
                            break  # overflow positions fall through to the main loop (R2)
                        _target_col = _ordinal_catalog[_pos_idx]
                        if not _target_col:
                            continue
                        positional_col_names[_pos_idx] = _target_col

                        # Guard 1: Star projection — mirror the main loop's handling
                        # EXACTLY, including StarSource registration for graph-backend
                        # expansion (EXPAND_STAR_SOURCES). Registering this position in
                        # `positional_col_names` (above) already makes the main loop
                        # skip it — without this mirror, `INSERT INTO t SELECT * FROM s`
                        # into a catalogued target would silently drop its STAR_SOURCE
                        # edge (regression guard: tests/integration/test_star_resolution.py).
                        _b_inner_guard = (
                            _pcol_expr.this if isinstance(_pcol_expr, exp.Alias) else _pcol_expr
                        )
                        if isinstance(_b_inner_guard, exp.Star) or (
                            isinstance(_b_inner_guard, exp.Column)
                            and isinstance(_b_inner_guard.this, exp.Star)
                        ):
                            _b_qualifier = (
                                _b_inner_guard.table
                                if isinstance(_b_inner_guard, exp.Column)
                                else None
                            )
                            out.errors.append(
                                f"col_lineage_skip:star:{_b_qualifier or '<unqualified>'}"
                            )
                            _b_star_src_table = self._resolve_star_source(
                                qualifier=_b_qualifier or None,
                                sources=query_sources or [],
                            )
                            if _b_star_src_table is not None:
                                star_sources.append(
                                    StarSource(
                                        source=_b_star_src_table, qualifier=_b_qualifier or None
                                    )
                                )
                            continue

                        # Guard 2: Pure-literal — no Column descendants, nothing to trace.
                        if not list(_pcol_expr.find_all(exp.Column)):
                            continue

                        # Wrap as Alias(inner, target_col) — unwrap any existing alias
                        # first to avoid Alias(Alias(...)) (mirrors #25).
                        _b_inner = (
                            _pcol_expr.this if isinstance(_pcol_expr, exp.Alias) else _pcol_expr
                        )
                        _b_aliased = exp.Alias(this=_b_inner.copy(), alias=_target_col)
                        _body_no_with_b.set("expressions", [_b_aliased])
                        _b_patched_sql = _body_no_with_b.sql(dialect=self.DIALECT)
                        _b_transform = self._classify_transform(_pcol_expr)
                        try:
                            _b_root = sg_lineage(
                                _target_col,
                                _b_patched_sql,
                                dialect=self.DIALECT,
                                sources=sources or {},
                            )
                            if _b_root:
                                _b_new_edges = self._lineage_node_to_edges(
                                    _b_root,
                                    dst_col_name=_target_col,
                                    dst_table=dst_table,
                                    path=path,
                                    out=out,
                                    inferred_from_source_name=False,
                                    transform=_b_transform,
                                    cte_alias_names=cte_alias_names,
                                )
                                edges.extend(_b_new_edges)
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

                # _sg_col_arg is what we pass to sg_lineage: a quoted exp.Column for
                # aliased expressions (E5 fix), or the plain string col_name otherwise.
                if col_expr.alias:
                    col_name = col_expr.alias
                    # E5 fix (PR-2, coverage_parse_failures.md): qualify() uppercases
                    # single-token unquoted-looking aliases (e.g. "Afdelingsnummer" →
                    # AFDELINGSNUMMER in the scope) so sg_lineage("Afdelingsnummer", ...)
                    # fails with "Cannot find column 'AFDELINGSNUMMER'". Space-containing
                    # aliases are unaffected because spaces prevent normalization.
                    #
                    # Fix: when the alias identifier is QUOTED in the source SQL
                    # (alias_node.quoted=True), pass a quoted exp.Column expression so
                    # normalize_identifiers() preserves case through the scope lookup.
                    # For UNQUOTED aliases (e.g. `d.id AS dim_listing_id`), the scope
                    # stores the uppercase form (DIM_LISTING_ID), so we pass the plain
                    # string so sg_lineage uppercases it to match — the pre-fix behavior.
                    # This O(1) guard (one attribute read + one tiny AST node for quoted
                    # aliases) does not touch body_scope or qualify/build_scope.
                    # dst_col_name in _lineage_node_to_edges uses col_name (original case).
                    _alias_node = col_expr.args.get("alias")
                    _alias_is_quoted = _alias_node is not None and getattr(
                        _alias_node, "quoted", False
                    )
                    if _alias_is_quoted:
                        _sg_col_arg = exp.Column(this=exp.Identifier(this=col_name, quoted=True))
                    else:
                        _sg_col_arg = col_name
                elif isinstance(col_expr, exp.Column):
                    col_name = col_expr.name
                    _sg_col_arg = col_name
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
                        self._log.debug(
                            "column %s not found in schema, skipping edge (unknown_sentinel)",
                            col_name,
                        )
                        # Honest drop: skip the self-edge rather than emitting a
                        # <unknown>→<unknown> self-edge that carries no lineage
                        # information (PR 4, sprint_postmortem_fixes.md §Step 4.1).
                        out.errors.append(f"col_lineage_skip:unknown_sentinel:{col_name}")
                        continue

                # Classify the transform kind once per column — AST-only, no serialization,
                # no qualify/build_scope/exp.expand/sg_lineage calls (CLAUDE.md invariants).
                # Result is passed through to _lineage_node_to_edges for every emitted edge.
                col_transform = self._classify_transform(col_expr)

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
                    root = sg_lineage(_sg_col_arg, body, **sg_kwargs)
                    if root:
                        # Successfully extracted lineage — walk tree and emit edges.
                        #
                        # A2 provenance: a no-column-list positional INSERT names its
                        # dst from the SOURCE expression (col_name, derived above from
                        # col_expr.alias/.name) whenever it reaches this main loop —
                        # either because no target-column catalog resolved at all, or
                        # because this position is beyond the catalog's length (R2
                        # overflow degrade; Part B registered only the in-range
                        # positions in positional_col_names). Both are genuinely
                        # source-named — flag them
                        # (plan/sprints/positional_insert_clone_blindspot.md Part A2).
                        _is_unmapped_no_list_insert = (
                            isinstance(stmt, exp.Insert)
                            and not isinstance(stmt.this, exp.Schema)
                            and dst_table is not None
                        )
                        new_edges = self._lineage_node_to_edges(
                            root,
                            dst_col_name=col_name,
                            dst_table=dst_table,
                            path=path,
                            out=out,
                            inferred_from_source_name=_is_unmapped_no_list_insert,
                            transform=col_transform,
                            cte_alias_names=cte_alias_names,
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
                    # Honest drop: skip the <unknown>→<unknown> self-edge; it carries
                    # no lineage information and pollutes strict-bad-edge counts
                    # (PR 4, sprint_postmortem_fixes.md §Step 4.1).
                    out.errors.append(f"col_lineage_skip:unknown_sentinel:{col_name}")

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
                            # Treat the CTE as a synthetic destination table (role="cte").
                            # PR 3: set namespace so each CTE gets a per-file-scoped key
                            # (e.g. "etl/sql/fact/wtfa.sql::final") preventing same-named
                            # CTEs in different files from sharing one graph node.
                            cte_dst_table = TableRef(
                                name=cte_alias,
                                role="cte",
                                namespace=self._current_file_namespace,
                            )
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
                            # PR 3: when a table name matches a CTE alias, produce a
                            # namespaced CTE TableRef so the ambiguous-edge src key matches
                            # the CTE projection's dst key (keeping the chain joinable).
                            cte_source_tables: list[TableRef] = []
                            for _t in cte_body.find_all(exp.Table):
                                if not _t.name:
                                    continue
                                _tname = _t.name.lower()
                                if (
                                    _tname in cte_alias_names
                                    and self._current_file_namespace is not None
                                ):
                                    cte_source_tables.append(
                                        TableRef(
                                            name=_t.name,
                                            role="cte",
                                            namespace=self._current_file_namespace,
                                        )
                                    )
                                else:
                                    _ref = self._table_node_to_ref(_t)
                                    if _ref is not None:
                                        cte_source_tables.append(_ref)

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
                                        # Emit edges with dst = CTE name.
                                        # PR 3: pass cte_alias_names so that CTE-to-CTE
                                        # source refs in this body are namespaced, keeping
                                        # the chain joinable (e.g. j's body references u →
                                        # src key must be namespace::u.m not bare u.m).
                                        cte_edges = self._lineage_node_to_edges(
                                            root,
                                            dst_col_name=cte_col_name,
                                            dst_table=cte_dst_table,
                                            path=path,
                                            out=out,
                                            cte_alias_names=cte_alias_names,
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
