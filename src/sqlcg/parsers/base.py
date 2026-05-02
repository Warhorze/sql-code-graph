"""Base data models for SQL parsing and lineage extraction."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TableRef:
    """A reference to a table (immutable).

    Attributes:
        catalog: Optional catalog name (e.g., "my_warehouse")
        db: Optional database/schema name (e.g., "public")
        name: Table name (always required)
        alias: Optional alias used in the query context
    """

    catalog: Optional[str] = None
    db: Optional[str] = None
    name: str = ""
    alias: Optional[str] = None

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
    query_id: Optional[str] = None

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
    target: Optional[TableRef] = None
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
    """

    path: Path
    dialect: Optional[str] = None
    statements: list[QueryNode] = field(default_factory=list)
    defined_tables: list[TableRef] = field(default_factory=list)
    referenced_tables: list[TableRef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
