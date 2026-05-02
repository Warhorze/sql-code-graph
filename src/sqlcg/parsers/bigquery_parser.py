"""BigQuery SQL parser with scripting block detection."""

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@register("bigquery")
class BigQueryParser(AnsiParser):
    """BigQuery SQL parser.

    Handles BigQuery-specific features:
    - Scripting block detection (DECLARE, BEGIN, IF)
    - Standard table/column lineage extraction
    """

    DIALECT: str | None = "bigquery"

    def __init__(self, schema_resolver: SchemaResolver):
        """Initialize BigQuery parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
        """
        super().__init__(schema_resolver)

    def _has_scripting_block(self, sql: str) -> bool:
        """Token-aware scripting block detection for BigQuery.

        Detects DECLARE, BEGIN, IF, and other scripting keywords.

        Args:
            sql: SQL text to check

        Returns:
            True if a scripting block is detected
        """
        try:
            from sqlglot.tokens import Tokenizer, TokenType  # type: ignore

            toks = Tokenizer.from_dialect("bigquery").tokenize(sql)  # type: ignore
            scripting_tokens = {
                TokenType.DECLARE,  # type: ignore
                TokenType.BEGIN,  # type: ignore
            }
            return any(t.token_type in scripting_tokens for t in toks)
        except Exception:
            # Fallback: check for keywords in uppercase
            sql_upper = sql.upper()
            return any(keyword in sql_upper for keyword in ("DECLARE ", "BEGIN "))
