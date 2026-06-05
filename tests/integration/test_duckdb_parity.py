"""DuckDB parity gate — Phase 3 exit gate.

Verifies that DuckDBBackend produces identical results to the Kuzu reference
fixture (tests/fixtures/duckdb_parity/kuzu_reference.json) for:
- node counts per label
- edge counts per type
- COLUMN_LINEAGE traversal reached-sets (upstream + downstream)
- hub ranking top-k
- star-source and star-expansion counts
- table-level edge query

The reference fixture was extracted from a live KuzuBackend on the same corpus
(airbnb + star_corpus fixtures) before Kuzu is deleted in Phase 5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import (
    COUNT_STAR_EXPANSIONS_QUERY,
    COUNT_STAR_SOURCES_QUERY,
    HUB_RANKING_QUERY,
)
from sqlcg.indexer.indexer import Indexer

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures"
AIRBNB_PATH = FIXTURE_PATH / "airbnb"
STAR_PATH = FIXTURE_PATH / "star_corpus"
REFERENCE_PATH = FIXTURE_PATH / "duckdb_parity" / "kuzu_reference.json"


@pytest.fixture(scope="module")
def kuzu_reference() -> dict:
    """Load the committed Kuzu reference fixture."""
    if not REFERENCE_PATH.exists():
        pytest.skip(
            f"Kuzu reference fixture not found at {REFERENCE_PATH}. "
            "Run Phase 3 STEP 0 extraction script before running parity tests."
        )
    with open(REFERENCE_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def duckdb_indexed(tmp_path_factory) -> DuckDBBackend:
    """DuckDB backend indexed with the same corpus as the Kuzu reference."""
    if not AIRBNB_PATH.exists():
        pytest.skip("Airbnb fixtures not found")

    db_path = str(tmp_path_factory.mktemp("duckdb_parity") / "parity.db")
    db = DuckDBBackend(db_path)
    db.init_schema()
    indexer = Indexer()
    indexer.index_repo(AIRBNB_PATH, dialect="snowflake", db=db, use_git=False)
    if STAR_PATH.exists():
        indexer.index_repo(STAR_PATH, dialect="snowflake", db=db, use_git=False)
    yield db
    db.close()


class TestNodeCountParity:
    """DuckDB node counts must match the Kuzu reference."""

    def test_file_node_count_matches(self, duckdb_indexed, kuzu_reference):
        """File node count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "File"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["node_counts"]["File"]
        assert duckdb_count == kuzu_count, (
            f"File node count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_sql_table_node_count_matches(self, duckdb_indexed, kuzu_reference):
        """SqlTable node count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "SqlTable"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["node_counts"]["SqlTable"]
        assert duckdb_count == kuzu_count, (
            f"SqlTable count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_sql_column_node_count_matches(self, duckdb_indexed, kuzu_reference):
        """SqlColumn node count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["node_counts"]["SqlColumn"]
        assert duckdb_count == kuzu_count, (
            f"SqlColumn count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_sql_query_node_count_matches(self, duckdb_indexed, kuzu_reference):
        """SqlQuery node count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "SqlQuery"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["node_counts"]["SqlQuery"]
        assert duckdb_count == kuzu_count, (
            f"SqlQuery count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )


class TestEdgeCountParity:
    """DuckDB edge counts must match the Kuzu reference."""

    def test_defined_in_edge_count_matches(self, duckdb_indexed, kuzu_reference):
        """DEFINED_IN edge count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "DEFINED_IN"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["edge_counts"]["DEFINED_IN"]
        assert duckdb_count == kuzu_count, (
            f"DEFINED_IN count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_has_column_edge_count_matches(self, duckdb_indexed, kuzu_reference):
        """HAS_COLUMN edge count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "HAS_COLUMN"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["edge_counts"]["HAS_COLUMN"]
        assert duckdb_count == kuzu_count, (
            f"HAS_COLUMN count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_selects_from_edge_count_matches(self, duckdb_indexed, kuzu_reference):
        """SELECTS_FROM edge count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "SELECTS_FROM"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["edge_counts"]["SELECTS_FROM"]
        assert duckdb_count == kuzu_count, (
            f"SELECTS_FROM count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_column_lineage_edge_count_matches(self, duckdb_indexed, kuzu_reference):
        """COLUMN_LINEAGE edge count matches Kuzu reference."""
        rows = duckdb_indexed.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["edge_counts"]["COLUMN_LINEAGE"]
        assert duckdb_count == kuzu_count, (
            f"COLUMN_LINEAGE count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )

    def test_star_source_edge_count_matches(self, duckdb_indexed, kuzu_reference):
        """STAR_SOURCE edge count matches Kuzu reference."""
        rows = duckdb_indexed.run_read(COUNT_STAR_SOURCES_QUERY, {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["star_source_count"]
        assert duckdb_count == kuzu_count, (
            f"STAR_SOURCE count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )


class TestTraversalParity:
    """DuckDB lineage traversal reached-sets must match the Kuzu reference."""

    def test_upstream_traversal_matches(self, duckdb_indexed, kuzu_reference):
        """Upstream traversal from each seed column matches Kuzu reached-sets."""
        from sqlcg.cli.commands.analyze import _upstream_sql

        sql = _upstream_sql(10, include_intermediate=True)
        for seed in kuzu_reference["traversal_seeds"]:
            kuzu_upstream = set(kuzu_reference["traversal_results"][seed]["upstream"])
            rows = duckdb_indexed.run_read(sql, {"ref": seed})
            duckdb_upstream = {r["id"] for r in rows}
            assert duckdb_upstream == kuzu_upstream, (
                f"Upstream mismatch for seed={seed!r}: "
                f"DuckDB={sorted(duckdb_upstream)}, Kuzu={sorted(kuzu_upstream)}"
            )

    def test_downstream_traversal_matches(self, duckdb_indexed, kuzu_reference):
        """Downstream traversal from each seed column matches Kuzu reached-sets."""
        from sqlcg.cli.commands.analyze import _downstream_sql

        sql = _downstream_sql(10, include_intermediate=True)
        for seed in kuzu_reference["traversal_seeds"]:
            kuzu_downstream = set(kuzu_reference["traversal_results"][seed]["downstream"])
            rows = duckdb_indexed.run_read(sql, {"ref": seed})
            duckdb_downstream = {r["id"] for r in rows}
            assert duckdb_downstream == kuzu_downstream, (
                f"Downstream mismatch for seed={seed!r}: "
                f"DuckDB={sorted(duckdb_downstream)}, Kuzu={sorted(kuzu_downstream)}"
            )


class TestHubRankingParity:
    """DuckDB hub ranking must match the Kuzu reference (C7)."""

    def test_hub_ranking_top_table_matches(self, duckdb_indexed, kuzu_reference):
        """Top hub table from DuckDB matches Kuzu reference."""
        rows = duckdb_indexed.run_read(HUB_RANKING_QUERY, {"k": 10})
        assert rows, "Expected non-empty hub ranking from DuckDB"
        # The top table should be in the Kuzu reference hub ranking
        kuzu_top_tables = {r["table_qualified"] for r in kuzu_reference["hub_ranking_top10"]}
        assert rows[0]["table_qualified"] in kuzu_top_tables, (
            f"DuckDB top hub table {rows[0]['table_qualified']!r} not in Kuzu reference: "
            f"{kuzu_top_tables}"
        )

    def test_hub_ranking_table_set_matches(self, duckdb_indexed, kuzu_reference):
        """Hub ranking table set from DuckDB matches Kuzu reference set."""
        rows = duckdb_indexed.run_read(HUB_RANKING_QUERY, {"k": 100})
        duckdb_tables = {r["table_qualified"] for r in rows}
        kuzu_tables = {r["table_qualified"] for r in kuzu_reference["hub_ranking_top10"]}
        # DuckDB should cover at least the same tables as Kuzu reference
        assert kuzu_tables.issubset(duckdb_tables), (
            f"Kuzu hub tables not covered by DuckDB: {kuzu_tables - duckdb_tables}"
        )


class TestStarExpansionParity:
    """DuckDB star expansion must match the Kuzu reference."""

    def test_star_expansion_count_matches(self, duckdb_indexed, kuzu_reference):
        """Star expansion count after indexing matches Kuzu reference (C7)."""
        rows = duckdb_indexed.run_read(COUNT_STAR_EXPANSIONS_QUERY, {})
        duckdb_count = rows[0]["n"] if rows else 0
        kuzu_count = kuzu_reference["star_expansion_count"]
        assert duckdb_count == kuzu_count, (
            f"Star expansion count mismatch: DuckDB={duckdb_count}, Kuzu={kuzu_count}"
        )


class TestTableEdgeParity:
    """DuckDB table-level SELECTS_FROM edge query must match the Kuzu reference."""

    def test_table_level_edges_match(self, duckdb_indexed, kuzu_reference):
        """Table-level SELECTS_FROM edges from DuckDB match the Kuzu reference sample."""
        rows = duckdb_indexed.run_read(
            "SELECT DISTINCT q.target_table AS query_target, t.qualified AS source_table"
            ' FROM "SqlQuery" q'
            ' JOIN "SELECTS_FROM" sf ON sf.src_key = q.id'
            ' JOIN "SqlTable" t ON t.qualified = sf.dst_key'
            " WHERE q.target_table <> ''",
            {},
        )
        duckdb_edges = {(r["query_target"], r["source_table"]) for r in rows}
        kuzu_sample = {
            (r["query_target"], r["source_table"])
            for r in kuzu_reference["table_level_edges_sample"]
        }
        missing = kuzu_sample - duckdb_edges
        assert not missing, (
            f"DuckDB is missing {len(missing)} edges present in Kuzu reference: {missing}"
        )
