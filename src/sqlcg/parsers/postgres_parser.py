"""PostgreSQL SQL parser."""

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@register("postgres")
class PostgresParser(AnsiParser):
    """PostgreSQL SQL parser.

    Uses standard ANSI parsing with Postgres dialect for v1.
    No special handling for scripting blocks in v1.
    """

    DIALECT: str | None = "postgres"

    def __init__(self, schema_resolver: SchemaResolver):
        """Initialize Postgres parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
        """
        super().__init__(schema_resolver)
