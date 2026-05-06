"""ANSI SQL parser implementation."""

from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile, ParseQuality, QueryNode, SqlParser, TableRef
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@register(None)  # None = ANSI/default dialect
class AnsiParser(SqlParser):
    """ANSI SQL parser for standard SQL dialects.

    Attributes:
        DIALECT: Set to None for ANSI/default dialect
    """

    DIALECT: str | None = None

    def __init__(self, schema_resolver: SchemaResolver):
        """Initialize ANSI parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
        """
        super().__init__(schema_resolver)

    def parse_file(self, path: Path, sql: str) -> ParsedFile:
        """Parse SQL file and extract table/column lineage.

        Args:
            path: Path to the source file
            sql: SQL text to parse

        Returns:
            ParsedFile with parsed statements and metadata
        """
        out = ParsedFile(path=path, dialect=self.DIALECT)

        # Parse all statements in the file
        try:
            statements = sqlglot.parse(sql, dialect=self.DIALECT)
        except Exception as exc:
            logger.warning("Failed to parse file %s: %s", path, exc)
            out.errors.append(f"parse_error:{exc}")
            out.parse_quality = ParseQuality.FAILED
            return out

        # Check for scripting fallback
        for stmt in statements:
            if stmt is not None and isinstance(stmt, exp.Command):
                out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
                break

        # Initialize sources_map to accumulate temp table definitions
        sources_map: dict[str, Any] = {}

        # Process each statement
        for stmt_index, stmt in enumerate(statements):
            if stmt is None:
                continue

            try:
                query_node = self._parse_statement(stmt, path, stmt_index, out, sources_map)
                out.statements.append(query_node)

                # Register CTAS bodies in sources_map for downstream temp table references
                if isinstance(stmt, exp.Create):
                    # Unwrap Subquery wrapper: CREATE TABLE t AS (SELECT ...) has
                    # stmt.expression = exp.Subquery, not exp.Select directly
                    _expr = stmt.expression
                    if isinstance(_expr, exp.Subquery):
                        _expr = _expr.this
                    if isinstance(_expr, exp.Select) and query_node.target:
                        target_name = (query_node.target.name or "").lower()
                        if target_name:
                            sources_map[target_name] = _expr

                # Track defined and referenced tables
                if query_node.kind in ("CREATE_TABLE", "CREATE_VIEW"):
                    if query_node.target:
                        out.defined_tables.append(query_node.target)

                out.referenced_tables.extend(query_node.sources)

                # Upgrade to FULL if column lineage exists
                if query_node.column_lineage:
                    out.parse_quality = ParseQuality.FULL

            except Exception as exc:
                logger.warning("Failed to process statement %d in %s: %s", stmt_index, path, exc)
                out.errors.append(f"statement_error:{stmt_index}:{exc}")

        return out

    def _parse_statement(
        self,
        stmt: Any,
        path: Path,
        stmt_index: int,
        out: ParsedFile,
        sources_map: dict[str, Any] | None = None,
    ) -> QueryNode:
        """Parse a single SQL statement into a QueryNode.

        Args:
            stmt: sqlglot AST node
            path: Path to the source file
            stmt_index: Statement index in the file
            out: ParsedFile object to append errors to
            sources_map: Map of temp table names to SELECT bodies for resolution

        Returns:
            QueryNode with extracted metadata
        """
        kind = self._classify(stmt)
        sql = stmt.sql(dialect=self.DIALECT)

        # Extract target table for CREATE/INSERT statements
        target = None
        if isinstance(stmt, exp.Create):
            target = self._extract_target_table(stmt)
        elif isinstance(stmt, exp.Insert):
            target = self._extract_insert_target(stmt)

        # Extract source tables
        sources = []
        ctes = {}
        confidence = 1.0
        parse_failed = False

        # Try to extract table references using scope analysis
        try:
            from sqlglot.optimizer.scope import build_scope

            root_scope = build_scope(stmt)
            if root_scope:
                sources = self._real_tables(root_scope)
            else:
                # Fallback to basic table extraction
                sources = self._fallback_table_scan(stmt)
                parse_failed = True
        except Exception as exc:
            logger.warning(
                "Failed to build scope for statement %d in %s: %s", stmt_index, path, exc
            )
            sources = self._fallback_table_scan(stmt)
            parse_failed = True

        # Remove target from sources if present (CREATE/INSERT shouldn't select from target)
        if target:
            sources = [src for src in sources if src.full_id != target.full_id]

        # Extract column lineage
        schema = self._schema.as_dict() if self._schema else {}
        column_lineage = self._extract_column_lineage(
            stmt, path, out, schema, dst_table=target, sources=sources_map
        )

        # Remove duplicates while preserving order
        sources = self._deduplicate_table_refs(sources)

        return QueryNode(
            file=path,
            statement_index=stmt_index,
            sql=sql,
            kind=kind,
            target=target,
            sources=sources,
            ctes=ctes,
            column_lineage=column_lineage,
            parse_failed=parse_failed,
            confidence=confidence,
            parsing_mode="sqlglot",
        )

    @staticmethod
    def _extract_target_table(create_stmt: exp.Create) -> TableRef | None:
        """Extract the target table from a CREATE statement.

        Args:
            create_stmt: CREATE expression

        Returns:
            TableRef or None if not a table create
        """
        if create_stmt.kind not in ("TABLE", "VIEW"):
            return None

        if not create_stmt.this:
            return None

        return AnsiParser._convert_table_expr_to_ref(create_stmt.this)

    @staticmethod
    def _extract_insert_target(insert_stmt: exp.Insert) -> TableRef | None:
        """Extract the target table from an INSERT statement.

        Args:
            insert_stmt: INSERT expression

        Returns:
            TableRef or None
        """
        if not insert_stmt.this:
            return None

        return AnsiParser._convert_table_expr_to_ref(insert_stmt.this)

    @staticmethod
    def _fallback_table_scan(stmt: Any) -> list[TableRef]:
        """Fallback table extraction when scope analysis fails.

        Walks the AST tree to find all Table nodes.

        Args:
            stmt: SQL expression

        Returns:
            List of TableRef objects found
        """
        tables = []
        seen = set()

        # Use find_all to iterate through all nodes of type Table
        for table_node in stmt.find_all(exp.Table):
            ref = AnsiParser._convert_table_expr_to_ref(table_node)
            if ref and ref.full_id not in seen:
                tables.append(ref)
                seen.add(ref.full_id)

        return tables

    @staticmethod
    def _deduplicate_table_refs(tables: list[TableRef]) -> list[TableRef]:
        """Remove duplicate table references while preserving order.

        Args:
            tables: List of TableRef objects

        Returns:
            Deduplicated list
        """
        seen = set()
        result = []
        for table in tables:
            if table.full_id not in seen:
                result.append(table)
                seen.add(table.full_id)
        return result
