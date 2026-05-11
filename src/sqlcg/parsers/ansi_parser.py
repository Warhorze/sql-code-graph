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

        # Parse all statements in the file using the hook (allows subclass overrides)
        statements, parse_errors = self._do_parse(sql)
        out.errors.extend(parse_errors)
        if not statements:
            logger.warning("Failed to parse file %s", path)
            out.parse_quality = ParseQuality.FAILED
            return out

        # Check for pure-DDL files (still parse table definitions but skip lineage)
        is_pure_ddl = self._is_pure_ddl_file(statements)
        if is_pure_ddl:
            out.errors.append("parse_mode:pure_ddl_skip")

        # Check for scripting fallback
        for stmt in statements:
            if stmt is not None and isinstance(stmt, exp.Command):
                out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
                break

        # Build scope for all statements at once to avoid O(N) traversals
        from sqlglot.optimizer.scope import build_scope

        file_scopes: list[Any] = []
        for stmt in statements:
            try:
                file_scopes.append(build_scope(stmt) if stmt is not None else None)
            except Exception:
                file_scopes.append(None)

        # Compute schema sources once per file
        schema_sources = self._schema.as_sources_dict() if self._schema else {}

        # Initialize sources_map to accumulate temp table definitions
        sources_map: dict[str, Any] = {}

        # Process each statement
        for stmt_index, stmt in enumerate(statements):
            if stmt is None:
                continue

            try:
                scope = file_scopes[stmt_index] if stmt_index < len(file_scopes) else None
                query_node = self._parse_statement(
                    stmt,
                    path,
                    stmt_index,
                    out,
                    sources_map,
                    scope=scope,
                    schema_sources=schema_sources,
                    skip_column_lineage=is_pure_ddl,
                )
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

    def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
        """Parse SQL text into statements, returning both statements and any parse errors.

        This method is a hook that can be overridden by subclasses (e.g., SnowflakeParser)
        to implement dialect-specific recovery or pre-processing logic.

        Args:
            sql: SQL text to parse

        Returns:
            Tuple of (statements list, error strings list). Errors are non-fatal;
            recovery may have collected partial statements even on parse failure.
        """
        try:
            return sqlglot.parse(sql, dialect=self.DIALECT), []
        except Exception as exc:
            return [], [f"parse_error:{exc}"]

    def _parse_statement(
        self,
        stmt: Any,
        path: Path,
        stmt_index: int,
        out: ParsedFile,
        sources_map: dict[str, Any] | None = None,
        scope: Any = None,
        schema_sources: dict[str, Any] | None = None,
        skip_column_lineage: bool = False,
    ) -> QueryNode:
        """Parse a single SQL statement into a QueryNode.

        Args:
            stmt: sqlglot AST node
            path: Path to the source file
            stmt_index: Statement index in the file
            out: ParsedFile object to append errors to
            sources_map: Map of temp table names to SELECT bodies for resolution
            scope: Pre-built sqlglot Scope for the statement (optional optimization)
            schema_sources: Map of table names to parsed exp.Select nodes from INFORMATION_SCHEMA
            skip_column_lineage: When True, skip column lineage extraction (pure-DDL files)

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
            # Use pre-built scope if provided, otherwise build it here (fallback)
            root_scope = scope
            if root_scope is None:
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

        # Extract column lineage (skip for pure-DDL files)
        if skip_column_lineage:
            from sqlcg.parsers.base import LineageExtraction

            extraction = LineageExtraction(edges=[], star_sources=[])
        else:
            schema = self._schema.as_dict() if self._schema else {}
            extraction = self._extract_column_lineage(
                stmt,
                path,
                out,
                schema,
                dst_table=target,
                sources=sources_map,
                query_sources=sources,
                schema_sources=schema_sources,
            )
        column_lineage = extraction.edges
        star_sources = extraction.star_sources

        # Extract defined columns for CREATE TABLE statements
        defined_columns: list[str] = []
        if isinstance(stmt, exp.Create) and stmt.kind == "TABLE":
            defined_columns = self._extract_defined_columns(stmt)

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
            defined_columns=defined_columns,
            star_sources=star_sources,
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
    def _extract_defined_columns(create_stmt: exp.Create) -> list[str]:
        """Extract column names from a CREATE TABLE statement.

        Args:
            create_stmt: CREATE expression (must be a TABLE create)

        Returns:
            List of column names in source order (as they appear in the SQL)
        """
        columns: list[str] = []
        # Find all ColumnDef nodes in the CREATE statement
        for col_def in create_stmt.find_all(exp.ColumnDef):
            col_name = col_def.this.name if col_def.this else None
            if col_name:
                columns.append(col_name)
        return columns

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
