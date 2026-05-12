"""E2: Unaliased non-Column expression (handled correctly).

Bare unquoted identifiers in sqlglot lineage output appear as UPPERCASE.
This test suite documents the current correct behavior where aliased
expressions produce edges for all operand columns.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e2_binary_add_alias(parser):
    """Aliased addition expression produces edges for both operands."""
    sql = Path(__file__).with_name("e2_expr_alias.sql").read_text()
    result = parse(parser, sql, "e2_expr_alias.sql")

    all_edges = edges(result)
    assert len([e for e in all_edges if e[3] == "my_col"]) >= 1

    assert any(e[1].upper() == "X" and e[3] == "my_col" for e in all_edges), (
        f"Expected X operand in edges: {all_edges}"
    )

    assert any(e[1].upper() == "Y" and e[3] == "my_col" for e in all_edges), (
        f"Expected Y operand in edges: {all_edges}"
    )


def test_e2_arithmetic_alias(parser):
    """Multiplication alias produces edges for price and qty."""
    sql = Path(__file__).with_name("e2_multiply_alias.sql").read_text()
    result = parse(parser, sql, "e2_multiply_alias.sql")

    all_edges = edges(result)
    assert any(e[1].upper() == "PRICE" and e[3] == "revenue" for e in all_edges), (
        f"Expected PRICE column in edges: {all_edges}"
    )

    assert any(e[1].upper() == "QTY" and e[3] == "revenue" for e in all_edges), (
        f"Expected QTY column in edges: {all_edges}"
    )


def test_e2_function_alias(parser):
    """Function call on column produces edge for the argument column."""
    sql = Path(__file__).with_name("e2_function_alias.sql").read_text()
    result = parse(parser, sql, "e2_function_alias.sql")

    all_edges = edges(result)
    assert any(e[1].upper() == "NAME" and e[3] == "name_upper" for e in all_edges), (
        f"Expected NAME column in edges: {all_edges}"
    )

    assert any(e[0].upper() == "CUSTOMERS" for e in all_edges), (
        f"Expected CUSTOMERS table in edges: {all_edges}"
    )
