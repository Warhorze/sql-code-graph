"""Anchor 1: OMLOOPSNELHEID temp-table drop pattern (sprint gate).

This test documents the exact behavior of temp-table usage and column drop
patterns. It serves as a regression gate for cross-file temp-table resolution
(E36 cross-file scope).

Note: Both GEMIDDELDE_VRD and AFZET appear with mixed case in edges depending
on how they flow through the expression. All assertions use .upper() normalization
to match sqlglot's lineage walker output (bare unquoted identifiers become uppercase).
"""

from pathlib import Path

import pytest

from tests.snowflake.conftest import edges, parse


@pytest.fixture
def parser():
    """Import fresh parser for each test."""
    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.snowflake_parser import SnowflakeParser

    return SnowflakeParser(schema_resolver=SchemaResolver())


def test_anchor_omloopsnelheid_temp_table_drop(parser):
    """Assert the OMLOOPSNELHEID column drop pattern is correctly detected.

    The fixture creates tmp_a and tmp_b with the computed omloopsnelheid column,
    but the INSERT target (persistent_target) only accepts afzet and gemiddelde_vrd,
    deliberately omitting omloopsnelheid. This tests that:
    1. The temp tables capture the computation
    2. The final INSERT does not include the dropped column
    """
    sql = Path(__file__).with_name("fixture_omloopsnelheid.sql").read_text()
    result = parse(parser, sql, "fixture_omloopsnelheid.sql")

    all_edges = edges(result)
    assert len(all_edges) > 0, "Temp-table CTAS must produce edges"

    # Assertion 1: stg_a -> tmp_a edges for both operands of the division
    assert any(
        e[0].upper() == "STG_A" and e[1].upper() == "AFZET" and e[2] == "tmp_a" for e in all_edges
    ), f"Expected STG_A.AFZET -> tmp_a in {all_edges}"

    assert any(
        e[0].upper() == "STG_A" and e[1].upper() == "GEMIDDELDE_VRD" and e[2] == "tmp_a"
        for e in all_edges
    ), f"Expected STG_A.GEMIDDELDE_VRD -> tmp_a in {all_edges}"

    # Assertion 2: stg_b -> tmp_b (symmetric)
    assert any(
        e[0].upper() == "STG_B" and e[1].upper() == "AFZET" and e[2] == "tmp_b" for e in all_edges
    ), f"Expected STG_B.AFZET -> tmp_b in {all_edges}"

    assert any(
        e[0].upper() == "STG_B" and e[1].upper() == "GEMIDDELDE_VRD" and e[2] == "tmp_b"
        for e in all_edges
    ), f"Expected STG_B.GEMIDDELDE_VRD -> tmp_b in {all_edges}"

    # Assertion 4: NO edge with dst_table="persistent_target" and dst_col="omloopsnelheid"
    # (column was not included in INSERT column list)
    persistent_omloop_edges = [
        e
        for e in all_edges
        if e[2].lower() == "persistent_target" and e[3].lower() == "omloopsnelheid"
    ]
    assert persistent_omloop_edges == [], (
        f"Column omloopsnelheid must not appear in INSERT to persistent_target; "
        f"found: {persistent_omloop_edges}"
    )

    # Assertion 5: NO edge where src_col="omloopsnelheid" (column does not leak forward)
    omloop_src_edges = [e for e in all_edges if e[1].lower() == "omloopsnelheid"]
    assert omloop_src_edges == [], (
        f"Column omloopsnelheid must not appear as a source in any edge; found: {omloop_src_edges}"
    )
