"""Parser registry and factory for dialect-specific SQL parsers."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.base import SqlParser

# Global registry of dialect -> parser class mapping
PARSERS: dict[str | None, type["SqlParser"]] = {}


def register(dialect: str | None):
    """Decorator to register a parser class for a dialect.

    Args:
        dialect: SQL dialect identifier (None for ANSI, "snowflake", etc.)

    Returns:
        Decorator function
    """

    def decorator(cls: type["SqlParser"]) -> type["SqlParser"]:
        PARSERS[dialect] = cls
        return cls

    return decorator


def get_parser(dialect: str | None, schema_resolver: "SchemaResolver") -> "SqlParser":
    """Get a parser instance for the given dialect.

    Args:
        dialect: SQL dialect identifier (None for ANSI, "snowflake", etc.)
        schema_resolver: SchemaResolver instance for table/column lookups

    Returns:
        SqlParser instance for the given dialect

    Raises:
        ValueError: If no parser is registered for the dialect
    """
    cls = PARSERS.get(dialect) or PARSERS.get(None)
    if cls is None:
        raise ValueError(f"No parser registered for dialect {dialect!r}")
    return cls(schema_resolver)
