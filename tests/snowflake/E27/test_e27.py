"""E27: UDF as column source (deferred upstream).

User-defined function bodies are not available to sqlglot.lineage, so UDF
arguments cannot be traced beyond the UDF boundary. These tests use
@pytest.mark.xfail(strict=False) to document expected behavior if sqlglot
adds UDF body support in the future.

See plan/sprint_07_open_ecodes.md § T-07-05 for the deferred-decision rationale.
"""

from pathlib import Path

import pytest

from tests.snowflake.conftest import edges, parse


@pytest.mark.xfail(
    strict=False,
    reason="DEFERRED: sqlglot lineage cannot inline UDF bodies",
)
def test_e27_udf_edge_when_supported(parser):
    """UDF argument should produce column lineage when UDF body is available.

    When sqlglot adds UDF body inlining support, this test should pass.
    # DEFERRED: sqlglot lineage cannot inline UDF bodies. When UDF definitions
    # are available, arguments could be traced through the boundary.
    """
    sql = Path(__file__).with_name("e27_udf.sql").read_text()
    result = parse(parser, sql, "e27_udf.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in edges_list), (
        f"When sqlglot supports UDFs, expected src.src_col -> dst_col: {edges_list}"
    )


@pytest.mark.xfail(
    strict=False,
    reason="DEFERRED: sqlglot lineage cannot inline UDF bodies",
)
def test_e27_nested_udf_edge_when_supported(parser):
    """Nested UDF arguments should produce column lineage when supported.

    When sqlglot adds UDF body inlining support, nested UDFs should resolve.
    # DEFERRED: nested UDFs cannot be traced without UDF body definitions.
    """
    sql = Path(__file__).with_name("e27_nested_udf.sql").read_text()
    result = parse(parser, sql, "e27_nested_udf.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC_COL" and e[3] == "dst_col" for e in edges_list), (
        f"When sqlglot supports UDFs, nested UDFs should resolve: {edges_list}"
    )
