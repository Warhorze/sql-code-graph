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


def _write_schema_csv(tmp_path: Path, rows: list[dict]) -> str:
    """Write a minimal INFORMATION_SCHEMA.COLUMNS CSV and return its path."""
    path = tmp_path / "schema.csv"
    fieldnames = ["TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_cte_cross_file_column_lineage_resolves_with_schema(tmp_path: Path) -> None:
    """COLUMN_LINEAGE edges must be non-zero after indexing a CREATE DYNAMIC TABLE
    whose CTE sources are resolved via the information schema.

    Without T-01, sg_lineage() raises 'Cannot find column' for CTE-derived columns
    because qualify() cannot expand cross-file CTE sources without sources=.
    This test guards against reversion of that fix.

    Failure message: 'E5 regression: CTE-derived column lineage must be non-zero
    when information schema is loaded — guards against sprint_06 T-01 regression.'
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
            CASE WHEN AVG("Rotatie") >= 0.5 THEN 'Fast-mover' ELSE 'Slow-mover' END AS "Movertype"
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
        schema_csv=Path(schema_csv),
    )

    rows = backend.run_read(
        "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN COUNT(e) AS cnt",
        {},
    )
    cnt = rows[0]["cnt"] if rows else 0
    assert cnt > 0, (
        f"E5 regression: CTE-derived column lineage must be non-zero when information "
        f"schema is loaded — guards against sprint_06 T-01 regression. "
        f"Index summary: {summary}"
    )
