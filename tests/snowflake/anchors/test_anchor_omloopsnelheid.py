"""Anchor 1: OMLOOPSNELHEID temp-table drop (silent column omission).

Tests that a computed column (omloopsnelheid) is correctly excluded from
a target table when not in the INSERT column list, and that the computation
through temp tables is traced correctly.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_anchor_omloopsnelheid_temp_cte_chain(parser):
    """Test the OMLOOPSNELHEID anchor: temp-table lineage with computed column drop.

    Assertions:
    1. Edge from stg_a/afzet to tmp_a/omloopsnelheid exists
    2. Edge from stg_a/gemiddelde_vrd to tmp_a/omloopsnelheid exists
    3. Symmetric edges for stg_b -> tmp_b
    4. Zero edges with dst_table=="persistent_target" and dst_col=="omloopsnelheid"
    5. Zero edges with src_col=="omloopsnelheid" (column does not leak forward)
    """
    sql = Path(__file__).with_name("fixture_omloopsnelheid.sql").read_text()
    result = parse(parser, sql, filename="fixture_omloopsnelheid.sql")

    edge_list = edges(result)

    # Normalize case for comparison
    edge_list_lower = [(e[0].lower(), e[1].lower(), e[2].lower(), e[3].lower()) for e in edge_list]

    # 1. Edge from stg_a/afzet to tmp_a/omloopsnelheid
    assert any(
        e[0] == "stg_a" and e[1] == "afzet" and e[2] == "tmp_a" and e[3] == "omloopsnelheid"
        for e in edge_list_lower
    ), f"Expected edge stg_a.afzet -> tmp_a.omloopsnelheid not found in {edge_list}"

    # 2. Edge from stg_a/gemiddelde_vrd to tmp_a/omloopsnelheid
    assert any(
        e[0] == "stg_a"
        and e[1] == "gemiddelde_vrd"
        and e[2] == "tmp_a"
        and e[3] == "omloopsnelheid"
        for e in edge_list_lower
    ), f"Expected edge stg_a.gemiddelde_vrd -> tmp_a.omloopsnelheid not found in {edge_list}"

    # 3. Symmetric edges for stg_b -> tmp_b (two assertions)
    assert any(
        e[0] == "stg_b" and e[1] == "afzet" and e[2] == "tmp_b" and e[3] == "omloopsnelheid"
        for e in edge_list_lower
    ), f"Expected edge stg_b.afzet -> tmp_b.omloopsnelheid not found in {edge_list}"

    assert any(
        e[0] == "stg_b"
        and e[1] == "gemiddelde_vrd"
        and e[2] == "tmp_b"
        and e[3] == "omloopsnelheid"
        for e in edge_list_lower
    ), f"Expected edge stg_b.gemiddelde_vrd -> tmp_b.omloopsnelheid not found in {edge_list}"

    # 4. Zero edges with dst_table=="persistent_target" and dst_col=="omloopsnelheid"
    assert not any(
        e[2].lower() == "persistent_target" and e[3].lower() == "omloopsnelheid"
        for e in edge_list_lower
    ), f"ERROR: omloopsnelheid leaked to persistent_target! Edges: {edge_list}"

    # 5. Zero edges with src_col=="omloopsnelheid" (column does not leak forward)
    assert not any(e[1].lower() == "omloopsnelheid" for e in edge_list_lower), (
        f"ERROR: omloopsnelheid appears as a source column! Edges: {edge_list}"
    )
