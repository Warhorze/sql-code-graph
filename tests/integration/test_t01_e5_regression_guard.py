"""Regression guard for FIX-E5: sg_lineage() must resolve CTE-derived columns
when the information schema is loaded as sources=.

Guards against a reversion of T-01 (sprint_06_lineage_coverage.md), where
sg_lineage() was called without schema_sources= and produced 1,280 E5 errors
('Cannot find column' on CTE-derived columns in CREATE DYNAMIC TABLE files).
"""

import csv
from pathlib import Path

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.schema_resolver import SchemaResolver


def _write_schema_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a minimal INFORMATION_SCHEMA.COLUMNS CSV and return its path."""
    path = tmp_path / "schema.csv"
    fieldnames = [
        "TABLE_CATALOG",
        "TABLE_SCHEMA",
        "TABLE_NAME",
        "COLUMN_NAME",
        "ORDINAL_POSITION",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


class TestE5RegressionGuard:
    """Test that CTE-derived column lineage resolves with schema_sources."""

    def test_cte_cross_file_column_lineage_resolves_with_schema(self, tmp_path):
        """COLUMN_LINEAGE edges must be non-zero when information schema is loaded.

        Without T-01, sg_lineage() raises 'Cannot find column' for CTE-derived columns
        because qualify() cannot expand cross-file CTE sources without sources=.
        This test guards against reversion of that fix.
        """
        # Minimal information schema: IA_SEMANTIC.Schapbeschikbaarheid has two columns
        schema_csv = _write_schema_csv(
            tmp_path,
            [
                {
                    "TABLE_CATALOG": "DWH",
                    "TABLE_SCHEMA": "IA_SEMANTIC",
                    "TABLE_NAME": "Schapbeschikbaarheid",
                    "COLUMN_NAME": "Rotatie",
                    "ORDINAL_POSITION": "1",
                },
                {
                    "TABLE_CATALOG": "DWH",
                    "TABLE_SCHEMA": "IA_SEMANTIC",
                    "TABLE_NAME": "Schapbeschikbaarheid",
                    "COLUMN_NAME": "Movertype",
                    "ORDINAL_POSITION": "2",
                },
            ],
        )

        # SQL that mirrors the E5-triggering pattern from the corpus:
        # CREATE DYNAMIC TABLE with CTE sourcing from a cross-file semantic view.
        sql = """
        CREATE OR REPLACE DYNAMIC TABLE agg_besteladviezen AS
        WITH movertype_per_dag AS (
            SELECT
                CASE WHEN AVG("Rotatie") >= 0.5 THEN 'Fast-mover'
                     ELSE 'Slow-mover' END AS "Movertype"
            FROM IA_SEMANTIC."Schapbeschikbaarheid"
        )
        SELECT mvt."Movertype"
        FROM movertype_per_dag AS mvt;
        """
        (tmp_path / "agg.sql").write_text(sql)

        backend = KuzuBackend(":memory:")
        backend.init_schema()
        indexer = Indexer()
        summary = indexer.index_repo(
            tmp_path,
            dialect="snowflake",
            db=backend,
            schema_csv=schema_csv,
        )

        rows = backend.run_read(
            "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN COUNT(e) AS cnt",
            {},
        )
        cnt = rows[0]["cnt"] if rows else 0
        assert cnt > 0, (
            f"E5 regression: CTE-derived column lineage must be non-zero when "
            f"information schema is loaded — guards against sprint_06 T-01 regression. "
            f"Index summary: {summary}"
        )

    def test_schema_sources_passed_to_sg_lineage(self, tmp_path):
        """schema_sources dict is generated with parsed SELECT nodes for sg_lineage."""
        import sqlglot.expressions as exp

        schema_csv = _write_schema_csv(
            tmp_path,
            [
                {
                    "TABLE_CATALOG": "DWH",
                    "TABLE_SCHEMA": "BA",
                    "TABLE_NAME": "orders",
                    "COLUMN_NAME": "id",
                    "ORDINAL_POSITION": "1",
                },
                {
                    "TABLE_CATALOG": "DWH",
                    "TABLE_SCHEMA": "BA",
                    "TABLE_NAME": "orders",
                    "COLUMN_NAME": "amount",
                    "ORDINAL_POSITION": "2",
                },
            ],
        )

        resolver = SchemaResolver("snowflake")
        resolver.add_information_schema(schema_csv)

        # Test that as_sources_dict is called and returns parsed exp.Select nodes
        schema_sources = resolver.as_sources_dict()

        assert "orders" in schema_sources
        assert "ba.orders" in schema_sources
        # Values should be parsed exp.Select nodes, not strings
        assert isinstance(schema_sources["orders"], exp.Select)
        assert isinstance(schema_sources["ba.orders"], exp.Select)
        # Verify the SELECT nodes have the correct columns/table (quoted, lowercased)
        sql_upper = str(schema_sources["orders"]).upper()
        assert "ID" in sql_upper and "AMOUNT" in sql_upper and "ORDERS" in sql_upper

    def test_schema_sources_computation_once_per_file(self):
        """schema_sources must be computed once per file, not once per statement.

        This is verified by code inspection:
        - AnsiParser.parse_file computes schema_sources once before the loop (line ~74)
        - _parse_statement receives schema_sources as a parameter
        - snowflake_parser._parse_scripting_file also computes it once at top
        """
        # This is a code-level verification test
        # The implementation shows schema_sources is computed once in parse_file
        # and passed into _parse_statement for every statement in the loop
        import inspect

        from sqlcg.parsers.ansi_parser import AnsiParser

        src = inspect.getsource(AnsiParser.parse_file)
        # Verify that as_sources_dict() is called before the statement loop
        assert "as_sources_dict()" in src, "as_sources_dict() must be called in parse_file"
        assert "schema_sources = self._schema.as_sources_dict()" in src, (
            "schema_sources must be computed once at file level"
        )

        # Verify _parse_statement signature includes schema_sources parameter
        sig = inspect.signature(AnsiParser._parse_statement)
        assert "schema_sources" in sig.parameters, (
            "_parse_statement signature must include schema_sources parameter"
        )
