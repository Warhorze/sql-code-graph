"""Snowflake SQL parser with scripting block detection and DML extraction."""

import re
from pathlib import Path
from typing import Any

import sqlglot

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
        # Check for scripting blocks
        if self._has_scripting_block(sql):
            logger.info("Snowflake scripting block detected in %s, using DML extraction", path)
            return self._parse_scripting_file(path, sql)

        # Otherwise use standard ANSI parsing with Snowflake dialect
        return AnsiParser.parse_file(self, path, sql)  # type: ignore

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
                            self, stmt, path, stmt_index, out
                        )
                        # Mark as parse_failed since we're in scripting mode
                        query_node.parse_failed = True
                        query_node.confidence = 0.3
                        query_node.parsing_mode = "scripting"
                        out.statements.append(query_node)
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
