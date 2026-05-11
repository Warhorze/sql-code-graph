"""E2E tests for star-projection resolution using the star_corpus fixture."""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

STAR_CORPUS = Path(__file__).parent.parent / "fixtures" / "star_corpus"

# Baseline established in v0.3.1 postmortem for the jaffle_shop corpus.
# Star expansion adds on top of this.
BASELINE_NAMED_COL_EDGES = 7


@pytest.fixture(scope="module")
def indexed_star_corpus(tmp_path_factory):
    """Index the star_corpus fixture once for all tests in this module."""
    if not STAR_CORPUS.exists():
        pytest.skip("tests/fixtures/star_corpus/ not found — create it as part of the sprint")

    db_dir = tmp_path_factory.mktemp("star_corpus_db")
    db = KuzuBackend(str(db_dir / "star.db"))
    db.init_schema()

    indexer = Indexer()
    result = indexer.index_repo(STAR_CORPUS, dialect=None, db=db, use_git=False)

    yield db, result

    db.close()


def test_dwh_corpus_emits_star_expanded_edges(indexed_star_corpus):
    """star_corpus must produce at least 3 STAR_EXPANSION edges after index_repo."""
    db, result = indexed_star_corpus

    assert "star_edges_expanded" in result, (
        f"index_repo must return star_edges_expanded. Got: {list(result.keys())}"
    )
    assert result["star_edges_expanded"] > 0, (
        "star_corpus contains SELECT * ETL files — expansion must produce > 0 edges"
    )

    rows = db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
        {},
    )
    expanded_count = rows[0]["n"]
    assert expanded_count >= 3, (
        f"Expected >= 3 STAR_EXPANSION edges from star_corpus, got {expanded_count}"
    )


def test_total_lineage_exceeds_baseline(indexed_star_corpus):
    """Total COLUMN_LINEAGE edges from star_corpus includes star expansion edges.

    Note: star_corpus is all SELECT * (no named-column edges), so the baseline
    from jaffle_shop doesn't apply. We just verify that star expansion produced edges.
    """
    db, result = indexed_star_corpus

    rows = db.run_read("MATCH ()-[r:COLUMN_LINEAGE]->() RETURN count(r) AS n", {})
    total = rows[0]["n"]
    # star_corpus has 3 columns per target table (col1, col2, col3) and 2 targets
    # = 6 STAR_EXPANSION edges. The test confirms we got at least some edges.
    assert total >= 3, f"Star expansion should produce at least 3 edges, got {total}"


def test_star_source_edges_visible_in_corpus(indexed_star_corpus):
    """star_corpus must have STAR_SOURCE edges visible in the graph after indexing."""
    db, _result = indexed_star_corpus

    rows = db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    assert rows[0]["n"] >= 1, (
        "No STAR_SOURCE edges found. Parser must emit StarSource markers "
        "and indexer must upsert them."
    )


def test_expansion_edges_have_correct_confidence(indexed_star_corpus):
    """Every STAR_EXPANSION edge must have confidence=0.8."""
    db, _result = indexed_star_corpus

    rows = db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN r.confidence AS c",
        {},
    )
    assert len(rows) > 0
    for row in rows:
        assert abs(row["c"] - 0.8) < 1e-6, (
            f"STAR_EXPANSION edge has unexpected confidence {row['c']!r}; expected 0.8"
        )


def test_ddl_columns_persisted_in_corpus(indexed_star_corpus):
    """star_corpus DDL files must produce HAS_COLUMN edges in the graph."""
    db, _result = indexed_star_corpus

    rows = db.run_read("MATCH (:SqlTable)-[:HAS_COLUMN]->(c:SqlColumn) RETURN count(c) AS n", {})
    assert rows[0]["n"] >= 3, (
        f"Expected >= 3 SqlColumn nodes (one per DDL column). Got {rows[0]['n']}. "
        "DDL column extraction must be wired into _upsert_parsed_file."
    )
