"""E2: Unaliased non-Column expression.

Tests that aliased expressions (not simple column references) correctly
produce lineage edges for their component columns.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e2_binary_add_alias(parser):
    """Test binary addition expression with alias.

    Aliased arithmetic expressions should produce edges for source columns.
    """
    sql = Path(__file__).with_name("e2_expr_alias.sql").read_text()
    result = parse(parser, sql, filename="e2_expr_alias.sql")

    edge_list = edges(result)

    # Assert at least one edge exists with output column "my_col"
    assert len([e for e in edge_list if e[3] == "my_col"]) >= 1
    # Assert edge from X to my_col
    assert any(e[1].upper() == "X" and e[3] == "my_col" for e in edge_list)
    # Assert edge from Y to my_col
    assert any(e[1].upper() == "Y" and e[3] == "my_col" for e in edge_list)


def test_e2_arithmetic_alias(parser):
    """Test multiplication expression with alias."""
    sql = Path(__file__).with_name("e2_multiply_alias.sql").read_text()
    result = parse(parser, sql, filename="e2_multiply_alias.sql")

    edge_list = edges(result)

    # Assert edges from both operands
    assert any(e[1].upper() == "PRICE" and e[3] == "revenue" for e in edge_list)
    assert any(e[1].upper() == "QTY" and e[3] == "revenue" for e in edge_list)


def test_e2_function_alias(parser):
    """Test function expression with alias."""
    sql = Path(__file__).with_name("e2_function_alias.sql").read_text()
    result = parse(parser, sql, filename="e2_function_alias.sql")

    edge_list = edges(result)

    # Assert edge from NAME column to name_upper
    assert any(e[1].upper() == "NAME" and e[3] == "name_upper" for e in edge_list)
    # Assert source table is CUSTOMERS
    assert any(e[0].upper() == "CUSTOMERS" for e in edge_list)
