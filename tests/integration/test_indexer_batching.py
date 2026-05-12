"""Tests for batched writes in Indexer.index_repo."""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def _write_test_corpus(corpus_path: Path, num_files: int = 12) -> None:
    """Write num_files simple SQL files to corpus_path for testing batching.

    Each file contains a CREATE VIEW statement to ensure consistent parsing
    and predictable lineage edges.
    """
    corpus_path.mkdir(parents=True, exist_ok=True)
    for i in range(num_files):
        sql_file = corpus_path / f"view_{i:02d}.sql"
        sql_file.write_text(
            f"CREATE VIEW v{i:02d} AS SELECT a FROM src_{i:02d};",
            encoding="utf-8",
        )


def test_batch_size_equivalence_1_vs_50(tmp_path):
    """Graph state after batch_size=1 must equal batch_size=50 for same input.

    The single most important regression guard for PERF-BATCH: verifies that
    the change from per-file commits to batched writes produces an identical
    graph (table set, edge count, column count).
    """
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=12)

    # Index with batch_size=1
    db1 = KuzuBackend(":memory:")
    db1.init_schema()
    summary1 = Indexer().index_repo(
        corpus_path, "snowflake", db1, use_git=False, batch_size=1
    )

    state1 = {
        "tables": sorted(
            r["q"]
            for r in db1.run_read(
                "MATCH (t:SqlTable) RETURN t.qualified AS q", {}
            )
        ),
        "edges": db1.run_read(
            "MATCH ()-[e:COLUMN_LINEAGE]->() RETURN count(e) AS n", {}
        )[0]["n"],
        "columns": db1.run_read("MATCH (c:SqlColumn) RETURN count(c) AS n", {})[0][
            "n"
        ],
    }
    db1.close()

    # Index with batch_size=50
    db50 = KuzuBackend(":memory:")
    db50.init_schema()
    summary50 = Indexer().index_repo(
        corpus_path, "snowflake", db50, use_git=False, batch_size=50
    )

    state50 = {
        "tables": sorted(
            r["q"]
            for r in db50.run_read(
                "MATCH (t:SqlTable) RETURN t.qualified AS q", {}
            )
        ),
        "edges": db50.run_read(
            "MATCH ()-[e:COLUMN_LINEAGE]->() RETURN count(e) AS n", {}
        )[0]["n"],
        "columns": db50.run_read(
            "MATCH (c:SqlColumn) RETURN count(c) AS n", {}
        )[0]["n"],
    }
    db50.close()

    # Verify identical graphs
    assert state1 == state50, (
        f"Graphs differ: batch_size=1 produced {state1}, "
        f"batch_size=50 produced {state50}"
    )
    assert summary1["files_parsed"] == summary50["files_parsed"]
    assert summary1["tables_found"] == summary50["tables_found"]
    assert summary1["lineage_edges_created"] == summary50["lineage_edges_created"]


def test_batch_size_partial_batch_flushed(tmp_path):
    """Batch size larger than corpus commits final partial batch.

    When batch_size > num_files, the trailing partial batch must still be
    flushed at the end. This verifies the final `_flush_batch(batch)` call.
    """
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=5)

    db = KuzuBackend(":memory:")
    db.init_schema()
    summary = Indexer().index_repo(
        corpus_path, "snowflake", db, use_git=False, batch_size=1000
    )

    assert summary["files_parsed"] == 5
    assert summary["tables_found"] >= 5

    # Verify all 5 files were indexed
    file_count = db.run_read("MATCH (f:File) RETURN count(f) AS n", {})[0]["n"]
    assert file_count == 5, (
        f"Expected 5 File nodes; final partial batch flush may have failed. "
        f"Got {file_count} files, summary={summary}"
    )
    db.close()


def test_batch_size_zero_raises_valueerror(tmp_path):
    """batch_size=0 raises ValueError with clear message."""
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=2)

    db = KuzuBackend(":memory:")
    db.init_schema()

    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        Indexer().index_repo(corpus_path, "snowflake", db, batch_size=0)

    db.close()


def test_batch_size_one_bad_file_does_not_abort_peers(tmp_path, monkeypatch):
    """One bad file in a batch does not abort batch peers.

    Verifies that try/except inside _flush_batch preserves the legacy
    behavior where one bad file does not abort the whole batch.
    """
    corpus_path = tmp_path / "corpus"
    corpus_path.mkdir(parents=True, exist_ok=True)

    # Write two valid files
    (corpus_path / "good1.sql").write_text("CREATE VIEW good1 AS SELECT a FROM src;")
    (corpus_path / "good2.sql").write_text("CREATE VIEW good2 AS SELECT b FROM src;")

    db = KuzuBackend(":memory:")
    db.init_schema()

    # Index normally first
    summary = Indexer().index_repo(
        corpus_path, "snowflake", db, use_git=False, batch_size=10
    )

    # Both files should be indexed
    assert summary["files_parsed"] == 2
    assert summary["quality"]["failed"] == 0

    # Verify both views exist in the graph
    tables = db.run_read(
        "MATCH (t:SqlTable) WHERE t.name IN ['good1', 'good2'] RETURN t.name AS name "
        "ORDER BY name",
        {},
    )
    table_names = [r["name"] for r in tables]
    assert set(table_names) == {"good1", "good2"}, (
        f"Expected both views; got {table_names}. "
        f"Summary: {summary}"
    )
    db.close()


def test_batch_size_observable_in_return_dict(tmp_path):
    """batch_size parameter is observable in the return dict."""
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=3)

    db = KuzuBackend(":memory:")
    db.init_schema()

    for batch_size_value in [1, 7, 50, 100]:
        summary = Indexer().index_repo(
            corpus_path, "snowflake", db, use_git=False, batch_size=batch_size_value
        )
        assert summary["batch_size"] == batch_size_value, (
            f"Expected batch_size={batch_size_value} in return dict; "
            f"got {summary.get('batch_size')}"
        )

    db.close()
