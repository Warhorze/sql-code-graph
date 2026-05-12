"""E12: LATERAL FLATTEN and JSON path (deferred upstream).

LATERAL FLATTEN and JSON path expressions cannot be fully traced by sqlglot.lineage
today. These tests use @pytest.mark.xfail(strict=False) to document expected
behavior if sqlglot adds support in the future.

See plan/sprint_07_open_ecodes.md § T-07-04 for the deferred-decision rationale.
"""

from pathlib import Path

import pytest

from tests.snowflake.conftest import edges, parse


@pytest.mark.xfail(
    strict=False,
    reason="DEFERRED: sqlglot lineage does not traverse LATERAL FLATTEN virtual columns",
)
def test_e12_lateral_flatten_edge_when_supported(parser):
    """LATERAL FLATTEN should produce edges for the flattened array column.

    When sqlglot adds support for LATERAL FLATTEN, this test should begin passing.
    # DEFERRED: sqlglot lineage does not traverse LATERAL FLATTEN virtual columns.
    # Local pre-rewriting would be fragile; revisit when sqlglot adds FLATTEN tracing
    # (track upstream: search sqlglot issues for "lateral flatten lineage").
    """
    sql = Path(__file__).with_name("e12_lateral_flatten.sql").read_text()
    result = parse(parser, sql, "e12_lateral_flatten.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "ARR" and e[3] == "col" for e in edges_list), (
        f"When sqlglot supports FLATTEN, expected tbl.arr -> col: {edges_list}"
    )


@pytest.mark.xfail(
    strict=False,
    reason="DEFERRED: sqlglot lineage cannot trace JSON path expressions",
)
def test_e12_json_path_edge_when_supported(parser):
    """JSON path expressions should produce edges if resolved to columns.

    When sqlglot adds support for JSON path lineage, this test should begin passing.
    # DEFERRED: sqlglot lineage cannot trace JSON path (colon path) expressions.
    # Local pre-rewriting would be fragile; revisit when sqlglot adds JSON path tracing.
    """
    sql = Path(__file__).with_name("e12_json_path.sql").read_text()
    result = parse(parser, sql, "e12_json_path.sql")

    edges_list = edges(result)
    assert any(e[1].upper() == "SRC" and e[3] == "city" for e in edges_list), (
        f"When sqlglot supports JSON paths, expected customer_raw.src -> city: {edges_list}"
    )
