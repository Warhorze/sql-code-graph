"""Acceptance tests for the bulk-upsert API (T-01) on DuckDBBackend.

Each test function is named after the sprint-08 ticket and scenario so
`pytest -k T-01` works. These assert the observable graph state and the
backend API contracts (empty no-op, heterogeneous-row rejection, edge-key
requirement, transaction participation).

Note: the original Kuzu-era ``test_T01_call_count_shape`` (which asserted on
Cypher ``UNWIND``/``MERGE`` keyword counts) was removed in the DuckDB
migration — the call-count-shape regression it guarded is now pinned for the
live backend by ``test_upsert_batch_invariant.py`` (flat N-vs-2N execute()
count) and ``test_bulk_upsert_invariant.py`` (bulk, never per-node).
"""

from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.indexer import Indexer


def _write_test_corpus(path: Path, n: int = 12) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (path / f"view_{i:02d}.sql").write_text(
            f"CREATE VIEW v{i:02d} AS SELECT a FROM src_{i:02d};",
            encoding="utf-8",
        )


@pytest.fixture
def backend():
    b = DuckDBBackend(":memory:")
    b.init_schema()
    yield b
    b.close()


# ---------------------------------------------------------------------------
# Scenario A — bulk path produces a non-empty, correct graph
# ---------------------------------------------------------------------------


def test_T01_bulk_path_equivalence_vs_baseline(tmp_path):
    """The bulk path IS the only path inside _upsert_parsed_file; indexing a
    12-file synthetic corpus must produce the expected nodes and edges (not an
    empty graph from a silently-failed bulk call)."""
    corpus = tmp_path / "corpus"
    _write_test_corpus(corpus, n=12)

    db_bulk = DuckDBBackend(":memory:")
    db_bulk.init_schema()
    summary = Indexer().index_repo(corpus, "snowflake", db_bulk, use_git=False)

    # The bulk methods must exist on the backend.
    assert hasattr(db_bulk, "upsert_nodes_bulk")
    assert hasattr(db_bulk, "upsert_edges_bulk")

    # 12 CREATE VIEW v AS SELECT a FROM src statements produce:
    #   - 24 SqlTable nodes (v00..v11 + src_00..src_11)
    #   - 12 COLUMN_LINEAGE edges (a -> v.a for each view)
    tables = db_bulk.run_read('SELECT count(*) AS n FROM "SqlTable"', {})[0]["n"]
    edges = db_bulk.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})[0]["n"]
    assert tables >= 24, f"Expected >= 24 SqlTable nodes, got {tables}"
    assert edges >= 12, f"Expected >= 12 COLUMN_LINEAGE edges, got {edges}"

    assert summary["tables_found"] >= 12, (
        f"tables_found={summary['tables_found']} — bulk upsert may have silently failed"
    )
    db_bulk.close()


# ---------------------------------------------------------------------------
# Scenario B — upsert_nodes_bulk with [] is a no-op
# ---------------------------------------------------------------------------


def test_T01_upsert_nodes_bulk_empty_is_noop(backend):
    """upsert_nodes_bulk([]) must be a no-op — no exception, no state change."""
    before = backend.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})[0]["n"]
    backend.upsert_nodes_bulk(NodeLabel.COLUMN, [])
    after = backend.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})[0]["n"]
    assert before == after, "upsert_nodes_bulk([]) changed node count unexpectedly"


# ---------------------------------------------------------------------------
# Scenario C — upsert_nodes_bulk rejects heterogeneous rows
# ---------------------------------------------------------------------------


def test_T01_upsert_nodes_bulk_rejects_heterogeneous_rows(backend):
    """upsert_nodes_bulk must raise ValueError for rows with different property keys."""
    with pytest.raises(ValueError, match="property keys"):
        backend.upsert_nodes_bulk(
            NodeLabel.COLUMN,
            [{"id": "x.a"}, {"id": "y.b", "col_name": "b"}],
        )


# ---------------------------------------------------------------------------
# Scenario D — upsert_edges_bulk requires src_key / dst_key
# ---------------------------------------------------------------------------


def test_T01_upsert_edges_bulk_requires_src_dst_key(backend):
    """upsert_edges_bulk must raise ValueError when dst_key is missing."""
    with pytest.raises(ValueError, match="dst_key"):
        backend.upsert_edges_bulk(
            NodeLabel.COLUMN,
            NodeLabel.COLUMN,
            RelType.COLUMN_LINEAGE,
            [{"src_key": "x.a"}],  # dst_key missing
        )


# ---------------------------------------------------------------------------
# Scenario G — bulk writes participate in the active transaction (rollback)
# ---------------------------------------------------------------------------


def test_T01_bulk_writes_participate_in_transaction(backend):
    """Bulk upserts inside a rolled-back transaction must not persist."""
    before = backend.run_read('SELECT count(*) AS n FROM "SqlTable"', {})[0]["n"]

    try:
        with backend.transaction():
            backend.upsert_nodes_bulk(
                NodeLabel.TABLE,
                [
                    {
                        "qualified": "rollback_test.t",
                        "name": "t",
                        "catalog": "",
                        "db": "rollback_test",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    }
                ],
            )
            raise RuntimeError("intentional rollback")
    except RuntimeError:
        pass  # expected

    after = backend.run_read('SELECT count(*) AS n FROM "SqlTable"', {})[0]["n"]
    assert after == before, (
        f"Bulk upsert survived rollback: before={before}, after={after}. "
        "Bulk writes must participate in the active transaction."
    )
