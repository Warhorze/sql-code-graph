"""Anchor 2: MA_AANTAL_OP_ORDER 3-file chain (sprint gate).

This test documents the complete lineage chain across three files:
1. fixture_source.sql: CREATE TABLE source_facts with ma_order_aantal column
2. fixture_etl.sql: INSERT INTO wtfs_openstaande_orders via CTEs
3. fixture_semantic.sql: CREATE VIEW "Openstaande orders"

Each of the 6 links in the chain is tested with xfail where the parser currently
fails to resolve the connection. When a link is fixed (parser enhanced), remove
the xfail decorator and the test will pass automatically.

Current state: All links require E5 (CTE resolution with SUM aggregation) and
chain resolution to pass. Link 6 additionally requires view column preservation
with quoted identifiers.
"""

from pathlib import Path

import pytest
import sqlglot

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


# Shared fixtures for all 6 link tests
@pytest.fixture(scope="module")
def chain_edges():
    """Parse all three chain files and return aggregated edges.

    Wires CrossFileAggregator to enable cross-file CTAS propagation (T-07-01),
    so that the semantic view (fixture_semantic.sql) can resolve the temp table
    wtfs_openstaande_orders defined in fixture_etl.sql.
    """
    source_path = Path(__file__).parent / "fixture_source.sql"
    source_sql = source_path.read_text()

    resolver = SchemaResolver(dialect="snowflake")
    stmts = sqlglot.parse(source_sql, dialect="snowflake")
    for stmt in stmts:
        resolver.add_create_table(stmt)

    parser = SnowflakeParser(schema_resolver=resolver)
    aggregator = CrossFileAggregator()

    etl_path = Path(__file__).parent / "fixture_etl.sql"
    etl_sql = etl_path.read_text()
    etl_result = parser.parse_file(etl_path, etl_sql)
    aggregator.register_pass1(etl_result)

    # Wire cross-file sources (T-07-01) so the semantic view can see the ETL's temp table
    resolver.register_cross_file_sources(aggregator.cross_file_sources)

    semantic_path = Path(__file__).parent / "fixture_semantic.sql"
    semantic_sql = semantic_path.read_text()
    semantic_result = parser.parse_file(semantic_path, semantic_sql)

    # Aggregate all edges
    all_edges = [
        (edge.src.table.name, edge.src.name, edge.dst.table.name, edge.dst.name)
        for result in [etl_result, semantic_result]
        for stmt in result.statements
        for edge in stmt.column_lineage
        if edge.confidence > 0.0
    ]

    return all_edges


@pytest.mark.xfail(
    strict=True, reason="E5: CTE resolution with SUM aggregation not yet implemented"
)
def test_anchor_ma_chain_link_1_source_facts_to_bm_orders(chain_edges):
    """Link 1: source_facts.ma_order_aantal -> bm_orders.aantal_op_order."""
    link_1 = [
        e
        for e in chain_edges
        if e[0].upper() == "SOURCE_FACTS"
        and e[1].upper() == "MA_ORDER_AANTAL"
        and e[2].lower() == "bm_orders"
        and e[3].lower() == "aantal_op_order"
    ]
    assert len(link_1) >= 1, (
        f"Expected source_facts.ma_order_aantal -> bm_orders.aantal_op_order; got {chain_edges}"
    )


@pytest.mark.xfail(
    strict=True, reason="E5: CTE resolution with SUM aggregation not yet implemented"
)
def test_anchor_ma_chain_link_2_source_facts_to_igdc(chain_edges):
    """Link 2: source_facts.ma_order_aantal -> igdc_openstaand.aantal_op_order."""
    link_2 = [
        e
        for e in chain_edges
        if e[0].upper() == "SOURCE_FACTS"
        and e[1].upper() == "MA_ORDER_AANTAL"
        and e[2].lower() == "igdc_openstaand"
        and e[3].lower() == "aantal_op_order"
    ]
    assert len(link_2) >= 1, (
        f"Expected source_facts.ma_order_aantal -> igdc_openstaand.aantal_op_order; "
        f"got {chain_edges}"
    )


@pytest.mark.xfail(strict=True, reason="E5: CTE-to-CTE column resolution not yet implemented")
def test_anchor_ma_chain_link_3_bm_to_combined(chain_edges):
    """Link 3: bm_orders.aantal_op_order -> openstaand_combined.aantal_op_order."""
    link_3 = [
        e
        for e in chain_edges
        if e[0].lower() == "bm_orders"
        and e[1].lower() == "aantal_op_order"
        and e[2].lower() == "openstaand_combined"
        and e[3].lower() == "aantal_op_order"
    ]
    assert len(link_3) >= 1, (
        f"Expected bm_orders.aantal_op_order -> openstaand_combined.aantal_op_order; "
        f"got {chain_edges}"
    )


@pytest.mark.xfail(strict=True, reason="E5: CTE-to-CTE column resolution not yet implemented")
def test_anchor_ma_chain_link_4_igdc_to_combined(chain_edges):
    """Link 4: igdc_openstaand.aantal_op_order -> openstaand_combined.aantal_op_order."""
    link_4 = [
        e
        for e in chain_edges
        if e[0].lower() == "igdc_openstaand"
        and e[1].lower() == "aantal_op_order"
        and e[2].lower() == "openstaand_combined"
        and e[3].lower() == "aantal_op_order"
    ]
    assert len(link_4) >= 1, (
        f"Expected igdc_openstaand.aantal_op_order -> openstaand_combined.aantal_op_order; "
        f"got {chain_edges}"
    )


@pytest.mark.xfail(strict=True, reason="E5: CTE-to-INSERT column resolution not yet implemented")
def test_anchor_ma_chain_link_5_combined_to_target(chain_edges):
    """Link 5: openstaand_combined.aantal_op_order -> wtfs_openstaande_orders.ma_aantal_op_order."""
    link_5 = [
        e
        for e in chain_edges
        if e[0].lower() == "openstaand_combined"
        and e[1].lower() == "aantal_op_order"
        and e[2].lower() == "wtfs_openstaande_orders"
        and e[3].lower() == "ma_aantal_op_order"
    ]
    assert len(link_5) >= 1, (
        "Expected openstaand_combined.aantal_op_order -> "
        "wtfs_openstaande_orders.ma_aantal_op_order; "
        f"got {chain_edges}"
    )


@pytest.mark.xfail(
    strict=True, reason="E5: View column resolution with quoted identifiers not yet implemented"
)
def test_anchor_ma_chain_link_6_target_to_view(chain_edges):
    """Link 6: wtfs_openstaande_orders.ma_aantal_op_order -> Openstaande orders.Aantal op order."""
    link_6 = [
        e
        for e in chain_edges
        if e[0].lower() == "wtfs_openstaande_orders"
        and e[1].lower() == "ma_aantal_op_order"
        and e[2] == "Openstaande orders"
        and e[3] == "Aantal op order"
    ]
    assert len(link_6) >= 1, (
        "Expected wtfs_openstaande_orders.ma_aantal_op_order -> "
        "Openstaande orders.Aantal op order; "
        f"got {chain_edges}"
    )
