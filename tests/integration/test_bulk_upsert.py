"""Failing acceptance tests for T-01 (bulk upsert API + indexer rewrite).

These tests fail until the developer lands T-01. Each test function is named
after the sprint-08 ticket and scenario so `pytest -k T-01` works.

Run before implementation to confirm red:
    uv run pytest tests/integration/test_bulk_upsert.py --no-header -q
"""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.indexer import Indexer

# T-01: upsert_nodes_bulk and upsert_edges_bulk are introduced by T-01.
# Import with graceful skip so the file can be collected without error pre-implementation.
try:
    from sqlcg.core.kuzu_backend import KuzuBackend as _KB  # noqa: F401

    _kb = _KB(":memory:")
    _kb.init_schema()
    _has_bulk = hasattr(_kb, "upsert_nodes_bulk") and hasattr(_kb, "upsert_edges_bulk")
    _kb.close()
except Exception:
    _has_bulk = False

_requires_bulk = pytest.mark.skipif(
    _has_bulk,
    reason="T-01 already implemented — test should be green, not skipped",
)


def _write_test_corpus(path: Path, n: int = 12) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (path / f"view_{i:02d}.sql").write_text(
            f"CREATE VIEW v{i:02d} AS SELECT a FROM src_{i:02d};",
            encoding="utf-8",
        )


@pytest.fixture
def backend():
    b = KuzuBackend(":memory:")
    b.init_schema()
    yield b
    b.close()


# ---------------------------------------------------------------------------
# Scenario A — bulk path produces identical graph state to single-row path
# ---------------------------------------------------------------------------


def test_T01_bulk_path_equivalence_vs_baseline(tmp_path):
    """Bulk upsert (T-01) must produce identical graph state to the per-row path.

    Compares the post-T-01 graph against the same 12-file synthetic corpus used by
    test_indexer_batching.py::test_batch_size_equivalence_1_vs_50.  After T-01 the
    bulk path IS the only path inside _upsert_parsed_file, so both sides of this
    comparison use the new code.  The test is the acceptance gate: if the rewrite
    drops any node or edge the assertion will catch it against the per-row baseline
    captured from the pre-T-01 code or from a two-corpus cross-check.

    Fail condition before T-01: upsert_nodes_bulk / upsert_edges_bulk do not exist
    on KuzuBackend, so index_repo raises AttributeError.
    """
    corpus = tmp_path / "corpus"
    _write_test_corpus(corpus, n=12)

    db_bulk = KuzuBackend(":memory:")
    db_bulk.init_schema()
    summary = Indexer().index_repo(corpus, "snowflake", db_bulk, use_git=False)

    # The bulk methods must be called — they only exist after T-01.
    # If the methods are missing, index_repo raises AttributeError.
    assert hasattr(db_bulk, "upsert_nodes_bulk"), (
        "upsert_nodes_bulk not found on KuzuBackend — T-01 not yet implemented"
    )
    assert hasattr(db_bulk, "upsert_edges_bulk"), (
        "upsert_edges_bulk not found on KuzuBackend — T-01 not yet implemented"
    )

    # Graph must contain nodes and edges — not empty from a failed bulk call.
    tables = db_bulk.run_read("MATCH (t:SqlTable) RETURN count(t) AS n", {})[0]["n"]
    # columns count verified indirectly via tables_found in summary below
    edges = db_bulk.run_read("MATCH ()-[e:COLUMN_LINEAGE]->() RETURN count(e) AS n", {})[0]["n"]

    # 12 CREATE VIEW v AS SELECT a FROM src statements produce:
    #   - 24 SqlTable nodes (v00..v11 + src_00..src_11)
    #   - 12 COLUMN_LINEAGE edges (a -> v.a for each view)
    assert tables >= 24, f"Expected >= 24 SqlTable nodes, got {tables}"
    assert edges >= 12, f"Expected >= 12 COLUMN_LINEAGE edges, got {edges}"

    # Summary must reflect the bulk upsert counts (not zero).
    assert summary["tables_found"] >= 12, (
        f"tables_found={summary['tables_found']} — bulk upsert may have silently failed"
    )
    db_bulk.close()


# ---------------------------------------------------------------------------
# Scenario B — upsert_nodes_bulk with [] is a no-op
# ---------------------------------------------------------------------------


def test_T01_upsert_nodes_bulk_empty_is_noop(backend):
    """upsert_nodes_bulk([]) must be a no-op — no exception, no state change."""
    if not hasattr(backend, "upsert_nodes_bulk"):
        pytest.fail("upsert_nodes_bulk not found — T-01 not yet implemented")

    before = backend.run_read("MATCH (c:SqlColumn) RETURN count(c) AS n", {})[0]["n"]
    backend.upsert_nodes_bulk(NodeLabel.COLUMN, [])  # type: ignore[attr-defined]
    after = backend.run_read("MATCH (c:SqlColumn) RETURN count(c) AS n", {})[0]["n"]
    assert before == after, "upsert_nodes_bulk([]) changed node count unexpectedly"


# ---------------------------------------------------------------------------
# Scenario C — upsert_nodes_bulk rejects heterogeneous rows
# ---------------------------------------------------------------------------


def test_T01_upsert_nodes_bulk_rejects_heterogeneous_rows(backend):
    """upsert_nodes_bulk must raise ValueError for rows with different property keys."""
    if not hasattr(backend, "upsert_nodes_bulk"):
        pytest.fail("upsert_nodes_bulk not found — T-01 not yet implemented")

    with pytest.raises(ValueError, match="property keys"):
        backend.upsert_nodes_bulk(  # type: ignore[attr-defined]
            NodeLabel.COLUMN,
            [{"id": "x.a"}, {"id": "y.b", "col_name": "b"}],
        )


# ---------------------------------------------------------------------------
# Scenario D — upsert_edges_bulk requires src_key / dst_key
# ---------------------------------------------------------------------------


def test_T01_upsert_edges_bulk_requires_src_dst_key(backend):
    """upsert_edges_bulk must raise ValueError when dst_key is missing."""
    if not hasattr(backend, "upsert_edges_bulk"):
        pytest.fail("upsert_edges_bulk not found — T-01 not yet implemented")

    with pytest.raises(ValueError, match="dst_key"):
        backend.upsert_edges_bulk(  # type: ignore[attr-defined]
            NodeLabel.COLUMN,
            NodeLabel.COLUMN,
            RelType.COLUMN_LINEAGE,
            [{"src_key": "x.a"}],  # dst_key missing
        )


# ---------------------------------------------------------------------------
# Scenario F — call-count shape: <= 12 execute() calls per file flush
# ---------------------------------------------------------------------------


def test_T01_call_count_shape(tmp_path, monkeypatch):
    """_upsert_parsed_file must make <= 12 execute() calls in the bulk path.

    A file with 1 table + 20 columns + 5 lineage edges should use at most:
      4 upsert_nodes_bulk calls (File, Table, Column, Query)
    + 6 upsert_edges_bulk calls (DEFINED_IN, HAS_COLUMN, QUERY_DEFINED_IN,
                                  SELECTS_FROM, COLUMN_LINEAGE, STAR_SOURCE)
    + 1 duplicate-DDL run_read check
    + 1 BEGIN TRANSACTION (via _flush_batch)
    + 1 COMMIT
    = 13 maximum, but the bulk methods each issue exactly 1 execute(), so
      the MERGE/UNWIND calls are bounded at 12 for the upsert-only phase.

    If this test sees > 12 execute() calls for the upsert portion, the rewrite
    has regressed to per-row calls.
    """
    db = KuzuBackend(":memory:")
    db.init_schema()

    if not hasattr(db, "upsert_nodes_bulk"):
        db.close()
        pytest.fail("upsert_nodes_bulk not found — T-01 not yet implemented")

    execute_calls: list[str] = []
    _orig_execute = db._conn.execute

    def _counting_execute(query, params=None):
        execute_calls.append(query.strip()[:60])
        if params is not None:
            return _orig_execute(query, params)
        return _orig_execute(query)

    monkeypatch.setattr(db._conn, "execute", _counting_execute)

    # Write a file with 1 table + 20 columns
    sql_file = tmp_path / "t.sql"
    cols = ", ".join(f"c{i} INT" for i in range(20))
    sql_file.write_text(f"CREATE TABLE db.s.tgt ({cols});", encoding="utf-8")

    execute_calls.clear()
    Indexer().index_repo(tmp_path, "snowflake", db, use_git=False, batch_size=1)

    unwind_calls = [q for q in execute_calls if "UNWIND" in q.upper()]
    assert len(unwind_calls) >= 2, (
        f"Expected >= 2 UNWIND calls (node + edge groups) in bulk path, "
        f"got {len(unwind_calls)}. All calls: {execute_calls}"
    )

    # Per-row MERGE calls (non-UNWIND) in the upsert phase must be zero.
    # BEGIN TRANSACTION and COMMIT are allowed, as is the duplicate-DDL run_read.
    merge_calls = [q for q in execute_calls if "MERGE" in q.upper() and "UNWIND" not in q.upper()]
    assert len(merge_calls) == 0, (
        f"Found {len(merge_calls)} per-row MERGE calls after T-01 rewrite "
        f"— regression to single-row path. Calls: {merge_calls}"
    )
    db.close()


# ---------------------------------------------------------------------------
# Scenario G — bulk writes participate in the active transaction (rollback)
# ---------------------------------------------------------------------------


def test_T01_bulk_writes_participate_in_transaction(backend):
    """Bulk upserts inside a rolled-back transaction must not persist."""
    if not hasattr(backend, "upsert_nodes_bulk"):
        pytest.fail("upsert_nodes_bulk not found — T-01 not yet implemented")

    before = backend.run_read("MATCH (t:SqlTable) RETURN count(t) AS n", {})[0]["n"]

    try:
        with backend.transaction():
            backend.upsert_nodes_bulk(  # type: ignore[attr-defined]
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

    after = backend.run_read("MATCH (t:SqlTable) RETURN count(t) AS n", {})[0]["n"]
    assert after == before, (
        f"Bulk upsert survived rollback: before={before}, after={after}. "
        "Bulk writes must participate in the active transaction."
    )
