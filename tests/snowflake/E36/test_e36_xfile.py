"""E36 cross-file: temp table defined in file A is visible to file B in pass 2."""

from pathlib import Path

from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _edges(result):
    """Return edges from a single file parse."""
    return [
        (e.src.table.name, e.src.name, e.dst.table.name, e.dst.name)
        for stmt in result.statements
        for e in stmt.column_lineage
        if e.confidence > 0.0
    ]


def test_e36_cross_file_temp_table_resolves(tmp_path):
    """Scenario A: two-file CTAS-then-INSERT produces a cross-file edge.

    File A defines a TEMP TABLE via CTAS.
    File B INSERTs from it.
    Pass 2 resolves the edge without seeing the raw temp table name.
    """
    file_a = tmp_path / "file_a.sql"
    file_a.write_text("CREATE TEMP TABLE t AS SELECT a FROM src;")
    file_b = tmp_path / "file_b.sql"
    file_b.write_text("INSERT INTO dst SELECT a FROM t;")

    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(schema_resolver=resolver)
    aggregator = CrossFileAggregator()

    # Pass 1: parse both files without cross-file sources
    parsed_a = parser.parse_file(file_a, file_a.read_text())
    aggregator.register_pass1(parsed_a)
    parsed_b = parser.parse_file(file_b, file_b.read_text())
    aggregator.register_pass1(parsed_b)

    # Wire cross-file sources, then re-parse file_b (pass 2)
    resolver.register_cross_file_sources(aggregator.cross_file_sources)
    parsed_b_p2 = parser.parse_file(file_b, file_b.read_text())

    edges_result = _edges(parsed_b_p2)
    # The resolved edge: SRC.A → dst.a (t expanded via cross-file sources_map)
    assert any(e[0].upper() == "SRC" and e[2] == "dst" for e in edges_result), (
        f"Cross-file temp table did not resolve: {edges_result}"
    )
    # t must NOT appear as a raw source (it is expanded)
    assert not any(e[0] == "t" for e in edges_result), (
        f"t must be expanded, not forwarded raw: {edges_result}"
    )


def test_e36_cross_file_no_sources_without_seeding(tmp_path):
    """Scenario B: negative control. File B without cross-file seeding produces no SRC→dst."""
    file_a = tmp_path / "file_a.sql"
    file_a.write_text("CREATE TEMP TABLE t AS SELECT a FROM src;")
    file_b = tmp_path / "file_b.sql"
    file_b.write_text("INSERT INTO dst SELECT a FROM t;")

    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(schema_resolver=resolver)

    # Parse file_b WITHOUT registering cross-file sources
    parsed_b = parser.parse_file(file_b, file_b.read_text())

    edges_result = _edges(parsed_b)
    # Without cross-file sources, t is unknown and the edge should not resolve to SRC
    assert not any(e[0].upper() == "SRC" and e[2] == "dst" for e in edges_result), (
        f"Without cross-file sources, edge should not resolve: {edges_result}"
    )


def test_e36_aggregator_harvests_ctas_bodies():
    """Scenario C: aggregator harvest captures CTAS bodies in file order."""
    from sqlcg.parsers.base import ParsedFile, QueryNode, TableRef

    # Construct a ParsedFile with two CTAS statements
    stmt1 = QueryNode(
        file=Path("test.sql"),
        statement_index=0,
        sql="CREATE TEMP TABLE t1 AS SELECT a FROM src1;",
        kind="CREATE_TABLE",
        target=TableRef(name="t1"),
        defined_body="mock_select_1",  # In real code, this would be exp.Select
    )
    stmt2 = QueryNode(
        file=Path("test.sql"),
        statement_index=1,
        sql="CREATE TEMP TABLE t2 AS SELECT b FROM src2;",
        kind="CREATE_TABLE",
        target=TableRef(name="t2"),
        defined_body="mock_select_2",  # In real code, this would be exp.Select
    )
    parsed = ParsedFile(path=Path("test.sql"), statements=[stmt1, stmt2])

    aggregator = CrossFileAggregator()
    aggregator.register_pass1(parsed)

    # Both lowercased keys should exist with non-None bodies
    assert "t1" in aggregator.cross_file_sources, "t1 not harvested"
    assert aggregator.cross_file_sources["t1"] == "mock_select_1"
    assert "t2" in aggregator.cross_file_sources, "t2 not harvested"
    assert aggregator.cross_file_sources["t2"] == "mock_select_2"


def test_e36_intra_file_override(tmp_path):
    """Scenario D: intra-file CTAS overrides cross-file CTAS of same name."""
    file_a = tmp_path / "file_a.sql"
    file_a.write_text("CREATE TEMP TABLE t AS SELECT a FROM src;")
    file_b = tmp_path / "file_b.sql"
    # File B redefines t locally, then uses its own definition
    file_b.write_text(
        "CREATE TEMP TABLE t AS SELECT x FROM other; INSERT INTO dst SELECT x FROM t;"
    )

    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(schema_resolver=resolver)
    aggregator = CrossFileAggregator()

    # Pass 1: register cross-file sources
    parsed_a = parser.parse_file(file_a, file_a.read_text())
    aggregator.register_pass1(parsed_a)
    parsed_b = parser.parse_file(file_b, file_b.read_text())
    aggregator.register_pass1(parsed_b)

    # Wire cross-file sources, then re-parse file_b (pass 2)
    resolver.register_cross_file_sources(aggregator.cross_file_sources)
    parsed_b_p2 = parser.parse_file(file_b, file_b.read_text())

    edges_result = _edges(parsed_b_p2)
    # Must resolve to OTHER, not SRC (intra-file t overrides cross-file t)
    assert any(e[0].upper() == "OTHER" and e[2] == "dst" for e in edges_result), (
        f"Intra-file CTAS should override cross-file: {edges_result}"
    )
    # SRC should NOT appear (that would mean cross-file t was used)
    assert not any(e[0].upper() == "SRC" and e[2] == "dst" for e in edges_result), (
        f"Cross-file t should be overridden, not used: {edges_result}"
    )
