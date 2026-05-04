"""Parser module for SQL lineage extraction."""

# Import all dialect parsers to register them
from sqlcg.parsers.ansi_parser import AnsiParser  # noqa: F401
from sqlcg.parsers.base import (
    ColumnRef,
    LineageEdge,
    ParsedFile,
    QueryNode,
    SqlParser,
    TableRef,
)
from sqlcg.parsers.bigquery_parser import BigQueryParser  # noqa: F401
from sqlcg.parsers.postgres_parser import PostgresParser  # noqa: F401
from sqlcg.parsers.snowflake_parser import SnowflakeParser  # noqa: F401
from sqlcg.parsers.tsql_parser import TsqlParser  # noqa: F401

__all__ = [
    "TableRef",
    "ColumnRef",
    "LineageEdge",
    "QueryNode",
    "ParsedFile",
    "SqlParser",
    "AnsiParser",
    "BigQueryParser",
    "PostgresParser",
    "SnowflakeParser",
    "TsqlParser",
]
