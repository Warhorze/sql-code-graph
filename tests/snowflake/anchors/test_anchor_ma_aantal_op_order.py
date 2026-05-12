"""Anchor 2: MA_AANTAL_OP_ORDER 3-file chain (cross-file lineage with schema).

Tests a complete lineage chain spanning a source DDL, ETL with nested CTEs,
and a semantic layer view. All six links must trace end-to-end.
"""

from pathlib import Path

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser
from tests.snowflake.conftest import edges, parse


def test_anchor_ma_aantal_op_order_complete_chain():
    """Test the MA_AANTAL_OP_ORDER anchor: 3-file lineage chain with schema.

    Setup:
    1. Build SchemaResolver and register source_facts via sqlglot.parse()
    2. Instantiate SnowflakeParser(schema_resolver=resolver)
    3. Parse fixture_etl.sql and fixture_semantic.sql in order
    4. Aggregate edges

    Expected chain (all 6 must be present or xfailed per reason):
    1. source_facts.ma_order_aantal -> bm_orders.aantal_op_order
    2. source_facts.ma_order_aantal -> igdc_openstaand.aantal_op_order
    3. bm_orders.aantal_op_order -> openstaand_combined.aantal_op_order
    4. igdc_openstaand.aantal_op_order -> openstaand_combined.aantal_op_order
    5. openstaand_combined.aantal_op_order -> wtfs_openstaande_orders.ma_aantal_op_order
    6. wtfs_openstaande_orders.ma_aantal_op_order -> "Openstaande orders"."Aantal op order"
    """
    base_dir = Path(__file__).parent

    # Step 1: Build SchemaResolver and register source_facts
    resolver = SchemaResolver(dialect="snowflake")
    source_sql = (base_dir / "fixture_source.sql").read_text()
    source_stmts = sqlglot.parse(source_sql, dialect="snowflake")
    if source_stmts:
        resolver.add_create_table(source_stmts[0])

    # Step 2: Instantiate parser with schema
    parser = SnowflakeParser(schema_resolver=resolver)

    # Step 3: Parse ETL and semantic fixtures in order
    etl_sql = (base_dir / "fixture_etl.sql").read_text()
    semantic_sql = (base_dir / "fixture_semantic.sql").read_text()

    etl_result = parse(parser, etl_sql, filename="fixture_etl.sql")
    semantic_result = parse(parser, semantic_sql, filename="fixture_semantic.sql")

    # Step 4: Aggregate edges from both results
    all_edges = edges(etl_result) + edges(semantic_result)

    # Normalize for easier assertion (case-insensitive comparison)
    all_edges_lower = [(e[0].lower(), e[1].lower(), e[2].lower(), e[3].lower()) for e in all_edges]

    # 1. source_facts.ma_order_aantal -> bm_orders.aantal_op_order
    assert any(
        e[0] == "source_facts"
        and e[1] == "ma_order_aantal"
        and e[2] == "bm_orders"
        and e[3] == "aantal_op_order"
        for e in all_edges_lower
    ), (
        f"Link 1 FAILED: source_facts.ma_order_aantal -> bm_orders.aantal_op_order\n"
        f"Edges: {all_edges}"
    )

    # 2. source_facts.ma_order_aantal -> igdc_openstaand.aantal_op_order
    assert any(
        e[0] == "source_facts"
        and e[1] == "ma_order_aantal"
        and e[2] == "igdc_openstaand"
        and e[3] == "aantal_op_order"
        for e in all_edges_lower
    ), (
        f"Link 2 FAILED: source_facts.ma_order_aantal -> igdc_openstaand.aantal_op_order\n"
        f"Edges: {all_edges}"
    )

    # 3. bm_orders.aantal_op_order -> openstaand_combined.aantal_op_order
    assert any(
        e[0] == "bm_orders"
        and e[1] == "aantal_op_order"
        and e[2] == "openstaand_combined"
        and e[3] == "aantal_op_order"
        for e in all_edges_lower
    ), (
        f"Link 3 FAILED: bm_orders.aantal_op_order -> openstaand_combined.aantal_op_order\n"
        f"Edges: {all_edges}"
    )

    # 4. igdc_openstaand.aantal_op_order -> openstaand_combined.aantal_op_order
    assert any(
        e[0] == "igdc_openstaand"
        and e[1] == "aantal_op_order"
        and e[2] == "openstaand_combined"
        and e[3] == "aantal_op_order"
        for e in all_edges_lower
    ), (
        f"Link 4 FAILED: igdc_openstaand.aantal_op_order -> openstaand_combined.aantal_op_order\n"
        f"Edges: {all_edges}"
    )

    # 5. openstaand_combined.aantal_op_order -> wtfs_openstaande_orders.ma_aantal_op_order
    assert any(
        e[0] == "openstaand_combined"
        and e[1] == "aantal_op_order"
        and e[2] == "wtfs_openstaande_orders"
        and e[3] == "ma_aantal_op_order"
        for e in all_edges_lower
    ), (
        "Link 5 FAILED: openstaand_combined.aantal_op_order ->"
        " wtfs_openstaande_orders.ma_aantal_op_order\n"
        f"Edges: {all_edges}"
    )

    # 6. wtfs_openstaande_orders.ma_aantal_op_order -> "Openstaande orders"."Aantal op order"
    # Note: quoted identifiers may preserve case; check both exact and normalized
    assert any(
        (e[0].lower() == "wtfs_openstaande_orders" and e[1].lower() == "ma_aantal_op_order")
        and (e[2].lower() == "openstaande orders" and e[3].lower() == "aantal op order")
        for e in all_edges
    ), (
        "Link 6 FAILED: wtfs_openstaande_orders.ma_aantal_op_order ->"
        " Openstaande orders.Aantal op order\n"
        f"Edges: {all_edges}"
    )
