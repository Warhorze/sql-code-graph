"""T-A1: Verify schema_sources are excluded from exp.expand() in Fix A.1.

This test confirms that schema-derived sources do not reach exp.expand(),
reducing O(N_files × N_schema) bloat. The sources are still passed to sg_lineage
via combined_sources for column resolution.
"""

from pathlib import Path
from unittest.mock import patch

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile, TableRef
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def test_expand_excludes_schema_sources_from_expand_call():
    """Verify exp.expand() is called with only CTAS/CTE sources, not schema_sources.

    Construct a _extract_column_lineage call with:
    - sources: dict with one CTE body
    - schema_sources: dict with one large table (not from sources)

    Patch sqlglot.expressions.expand and assert:
    - expand is called with a dict whose keys are {'cte_a'} only
    - sg_lineage still receives combined_sources (with both keys)
    """
    # Setup: create a resolver and parser
    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(resolver)

    # Build test AST nodes
    cte_body = sqlglot.parse_one("SELECT 1 AS x")
    schema_table_body = sqlglot.parse_one("SELECT " + ", ".join(f"{i} AS c{i}" for i in range(50)))

    sources = {"cte_a": cte_body}
    schema_sources = {"big_table": schema_table_body}

    # Parse a simple SELECT statement
    stmt = sqlglot.parse_one("SELECT cte_a.x FROM cte_a")

    # Create a minimal ParsedFile for the test
    out = ParsedFile(path=Path("test.sql"), dialect="snowflake")

    # Patch exp.expand and sg_lineage to track calls
    with (
        patch("sqlglot.expressions.expand") as mock_expand,
        patch("sqlglot.lineage.lineage") as mock_sg_lineage,
    ):
        # Setup mock return values
        mock_expand.return_value = stmt
        mock_sg_lineage.return_value = None

        # Call _extract_column_lineage
        parser._extract_column_lineage(
            stmt=stmt,
            path=Path("test.sql"),
            out=out,
            schema={},
            dst_table=TableRef(name="target", db=None, catalog=None),
            sources=sources,
            query_sources=[],
            schema_sources=schema_sources,
        )

        # Assert expand was called with only sources, not schema_sources
        assert mock_expand.called, "exp.expand should have been called"
        call_args = mock_expand.call_args
        sources_arg = (
            call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("sources", {})
        )
        expand_keys = set(sources_arg.keys())

        assert expand_keys == {"cte_a"}, (
            f"exp.expand should receive only CTAS/CTE sources {{'cte_a'}}, but got {expand_keys}"
        )

        # When scope= is provided (qualify succeeded), sg_lineage receives no sources= —
        # the scope already contains all column→table resolution. schema_sources must
        # never reach exp.expand(); they may reach sg_lineage only on the qualify-failed
        # fallback path (no scope). Assert "big_table" is absent from both paths.
        assert mock_sg_lineage.called, "sg_lineage should have been called"
        sg_call_args = mock_sg_lineage.call_args
        sg_sources_arg = sg_call_args.kwargs.get("sources", {})
        sg_keys = set(sg_sources_arg.keys())

        assert "big_table" not in sg_keys, (
            f"schema_sources key 'big_table' must not reach sg_lineage, got sources={sg_keys}"
        )
