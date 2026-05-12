"""Shared fixtures for E* lineage failure pattern tests."""

from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


@pytest.fixture
def parser():
    return SnowflakeParser(schema_resolver=SchemaResolver())


def edges(result):
    """Return high-confidence column lineage edges as tuples.

    Returns edges as (src_table, src_col, dst_table, dst_col) tuples where
    confidence > 0.0.
    """
    return [
        (edge.src.table.name, edge.src.name, edge.dst.table.name, edge.dst.name)
        for stmt in result.statements
        for edge in stmt.column_lineage
        if edge.confidence > 0.0
    ]


def parse(parser, sql: str, filename: str = "test.sql"):
    return parser.parse_file(Path(filename), sql)
