"""Snowflake SQL parser with scripting block detection and DML extraction."""

import re
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParsedFile, ParseQuality
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

# Regex for detecting scripting blocks (BEGIN/IF/LOOP)
# Used as fallback when tokenization fails
_SCRIPTING_BLOCK = re.compile(r"\bBEGIN\b", re.IGNORECASE)

# Regex for extracting DML statements from scripting blocks.
# Does not handle ';' inside string literals — tokenizer-based extraction deferred to v2.
_EMBEDDED_DML = re.compile(
    r"(SELECT\s+.+?(?=;|\Z)|INSERT\s+INTO.+?(?=;|\Z)|UPDATE\s+.+?(?=;|\Z)|DELETE\s+.+?(?=;|\Z)|MERGE\s+INTO.+?(?=;|\Z))",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)


@register("snowflake")
class SnowflakeParser(AnsiParser):
    """Snowflake SQL parser with scripting block handling.

    Handles Snowflake-specific features:
    - Token-aware scripting block detection (avoids false-positives)
    - DML extraction from scripting blocks
    - Colon-qualified identifiers (Gap 1)
    - LATERAL FLATTEN operations (Gap 2)
    - Dynamic identifiers (Gap 3)
    """

    DIALECT: str | None = "snowflake"

    def __init__(self, schema_resolver: SchemaResolver):
        """Initialize Snowflake parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
        """
        super().__init__(schema_resolver)

    def parse_file(self, path: Path, sql: str) -> ParsedFile:
        """Parse Snowflake SQL file with scripting block detection.

        Args:
            path: Path to the source file
            sql: SQL text to parse

        Returns:
            ParsedFile with parsed statements and metadata
        """
        # Apply Snowflake-specific preprocessing (Gap 1 + Gap 3 fixes)
        sql = self._preprocess_snowflake_sql(sql)

        # Check for scripting blocks
        if self._has_scripting_block(sql):
            logger.info("Snowflake scripting block detected in %s, using DML extraction", path)
            return self._parse_scripting_file(path, sql)

        # Otherwise use standard ANSI parsing with Snowflake dialect
        return AnsiParser.parse_file(self, path, sql)  # type: ignore

    @staticmethod
    def _preprocess_snowflake_sql(sql: str) -> str:
        """Apply Snowflake-specific pre-processing workarounds for sqlglot dialect gaps.

        Handles:
        1. Gap 1: Normalise CREATE TEMP TABLE t IF NOT EXISTS to CREATE TEMPORARY TABLE t
        2. Gap 3: Strip UNPIVOT clauses (destructive but acceptable for partial lineage extraction)

        Args:
            sql: Raw Snowflake SQL text

        Returns:
            Pre-processed SQL text
        """
        # Gap 1: CREATE TEMP/TEMPORARY TABLE name IF NOT EXISTS -> CREATE TEMPORARY TABLE name
        # This regex matches CREATE TEMP[ORARY] TABLE name IF NOT EXISTS and drops the IF NOT EXISTS
        sql = re.sub(
            r"CREATE\s+(TEMP(?:ORARY)?)\s+TABLE\s+(\w+)\s+IF\s+NOT\s+EXISTS",
            r"CREATE TEMPORARY TABLE \2",
            sql,
            flags=re.IGNORECASE,
        )

        # Gap 3: Strip UNPIVOT clauses if present
        # UNPIVOT (...) [AS alias] — strip the entire clause
        # Handle nested parentheses by counting parens
        if "UNPIVOT" in sql.upper():
            # Strategy: find UNPIVOT, then match everything until we close all parens
            # Use a more permissive pattern: UNPIVOT\s*\([^)]+\([^)]*\)[^)]*\)
            # This matches UNPIVOT (val FOR col IN (...)) AS alias
            sql = re.sub(
                r"\s+UNPIVOT\s*\([^)]*\([^)]*\)[^)]*\)\s*(?:AS\s+\w+)?",
                "",
                sql,
                flags=re.IGNORECASE,
            )

        return sql

    @staticmethod
    def _parse_with_recovery(sql: str, dialect: str | None) -> tuple[list[Any], list[str]]:
        """Parse SQL, falling back to per-chunk recovery on tokenisation failure (Gap 2).

        When the primary parse fails, splits on `;` and re-parses each chunk
        independently, collecting successful statements and recording errors
        for failed chunks.

        Args:
            sql: SQL text to parse
            dialect: Dialect string for sqlglot.parse

        Returns:
            Tuple of (statements list, recovery errors list). recovery_errors is non-empty
            when at least one chunk failed to parse.
        """
        try:
            return sqlglot.parse(sql, dialect=dialect), []
        except Exception as primary_exc:
            # Recovery: split on semicolons and parse each chunk independently
            errors = [f"parse_recovery:primary:{primary_exc}"]
            stmts: list[Any] = []
            for chunk in sql.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    parsed = sqlglot.parse(chunk, dialect=dialect)
                    stmts.extend(s for s in parsed if s is not None)
                except Exception as chunk_exc:
                    errors.append(f"parse_recovery:chunk:{chunk_exc}")
            return stmts, errors

    def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
        """Override _do_parse to apply Snowflake-specific recovery (Gap 2).

        For Snowflake dialect, uses per-chunk recovery on parse failure.
        All other dialects use the standard AnsiParser._do_parse.

        Args:
            sql: SQL text to parse

        Returns:
            Tuple of (statements list, error strings list)
        """
        return self._parse_with_recovery(sql, self.DIALECT)

    def _has_scripting_block(self, sql: str) -> bool:
        """Token-aware BEGIN detection — avoids false-positives on string literals and comments.

        Args:
            sql: SQL text to check

        Returns:
            True if a scripting block is detected
        """
        try:
            from sqlglot.tokens import Tokenizer, TokenType  # type: ignore

            toks = Tokenizer.from_dialect("snowflake").tokenize(sql)  # type: ignore
            return any(t.token_type == TokenType.BEGIN for t in toks)  # type: ignore
        except Exception:
            # Fallback to regex if tokenization fails
            return bool(_SCRIPTING_BLOCK.search(sql))

    def _parse_scripting_file(self, path: Path, sql: str) -> ParsedFile:
        """Parse a Snowflake file with scripting blocks using DML extraction.

        Args:
            path: Path to the source file
            sql: SQL text to parse

        Returns:
            ParsedFile with extracted DML statements
        """
        out = ParsedFile(path=path, dialect=self.DIALECT)
        out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
        out.errors.append("parse_mode:scripting_block")

        # Compute schema sources once per file
        schema_sources = self._schema.as_sources_dict() if self._schema else {}

        # Initialize sources_map for temp table resolution.
        # Seed with cross-file CTAS bodies from pass 1 (intra-file overrides).
        xfile_sources = dict(self._schema.cross_file_sources()) if self._schema else {}
        sources_map: dict[str, Any] = xfile_sources

        # Extract DML statements using regex
        dml_matches = _EMBEDDED_DML.finditer(sql)
        stmt_index = 0

        for match in dml_matches:
            dml_sql = match.group(1).strip()
            if not dml_sql:
                continue

            try:
                # Try to parse the extracted DML
                statements = sqlglot.parse(dml_sql, dialect=self.DIALECT)
                for stmt in statements:
                    if stmt is None:
                        continue

                    try:
                        # Call parent's _parse_statement method
                        query_node: Any = AnsiParser._parse_statement(  # type: ignore
                            self,
                            stmt,
                            path,
                            stmt_index,
                            out,
                            sources_map,
                            schema_sources=schema_sources,
                        )
                        # Mark as parse_failed since we're in scripting mode
                        query_node.parse_failed = True
                        query_node.confidence = 0.3
                        query_node.parsing_mode = "scripting"
                        out.statements.append(query_node)

                        # Register CTAS bodies in sources_map for downstream temp table refs
                        if isinstance(stmt, exp.Create):
                            _expr = stmt.expression
                            if isinstance(_expr, exp.Subquery):
                                _expr = _expr.this
                            if isinstance(_expr, exp.Select) and query_node.target:
                                target_name = (query_node.target.name or "").lower()
                                if target_name:
                                    sources_map[target_name] = _expr

                        stmt_index += 1

                        # Track table references
                        if query_node.kind in ("CREATE_TABLE", "CREATE_VIEW"):
                            if query_node.target:
                                out.defined_tables.append(query_node.target)
                        out.referenced_tables.extend(query_node.sources)

                    except Exception as exc:
                        logger.warning(
                            "Failed to process extracted DML statement %d in %s: %s",
                            stmt_index,
                            path,
                            exc,
                        )
                        out.errors.append(f"statement_error:{stmt_index}:{exc}")
                        stmt_index += 1

            except Exception as exc:
                logger.warning("Failed to parse extracted DML from %s: %s", path, exc)
                out.errors.append(f"dml_extraction_error:{exc}")

        return out
