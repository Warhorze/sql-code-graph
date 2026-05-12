"""E21: Alias forward reference (currently working).

sqlglot resolves alias chains correctly, tracing forward references back
to their original sources. This test documents the current correct behavior.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e21_alias_forward_ref(parser):
    """Alias chain a → b → output resolves correctly to src.a."""
    sql = Path(__file__).with_name("e21_alias_forward_ref.sql").read_text()
    result = parse(parser, sql, "e21_alias_forward_ref.sql")

    all_edges = edges(result)

    assert any(e[3].upper() == "C" for e in all_edges), (
        f"Expected edge to output column C: {all_edges}"
    )

    assert any(e[1].upper() == "A" and e[3].upper() == "C" for e in all_edges), (
        f"Expected A → C edge (alias chain resolved): {all_edges}"
    )


def test_e21_three_level_chain(parser):
    """Three-hop alias chain a → b → c → d resolves end-to-end."""
    sql = Path(__file__).with_name("e21_three_level_chain.sql").read_text()
    result = parse(parser, sql, "e21_three_level_chain.sql")

    all_edges = edges(result)

    assert any(e[1].upper() == "A" and e[3].upper() == "D" for e in all_edges), (
        f"Expected A → D edge (three-hop chain resolved): {all_edges}"
    )
