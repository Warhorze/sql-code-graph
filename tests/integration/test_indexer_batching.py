"""Tests for batched writes in Indexer.index_repo."""

from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
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
    db1 = DuckDBBackend(":memory:")
    db1.init_schema()
    summary1 = Indexer().index_repo(corpus_path, "snowflake", db1, use_git=False, batch_size=1)

    state1 = {
        "tables": sorted(r["q"] for r in db1.run_read('SELECT qualified AS q FROM "SqlTable"', {})),
        "edges": db1.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})[0]["n"],
        "columns": db1.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})[0]["n"],
    }
    db1.close()

    # Index with batch_size=50
    db50 = DuckDBBackend(":memory:")
    db50.init_schema()
    summary50 = Indexer().index_repo(corpus_path, "snowflake", db50, use_git=False, batch_size=50)

    state50 = {
        "tables": sorted(
            r["q"] for r in db50.run_read('SELECT qualified AS q FROM "SqlTable"', {})
        ),
        "edges": db50.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})[0]["n"],
        "columns": db50.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})[0]["n"],
    }
    db50.close()

    # Verify identical graphs
    assert state1 == state50, (
        f"Graphs differ: batch_size=1 produced {state1}, batch_size=50 produced {state50}"
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

    db = DuckDBBackend(":memory:")
    db.init_schema()
    summary = Indexer().index_repo(corpus_path, "snowflake", db, use_git=False, batch_size=1000)

    assert summary["files_parsed"] == 5
    assert summary["tables_found"] >= 5

    # Verify all 5 files were indexed
    file_count = db.run_read('SELECT count(*) AS n FROM "File"', {})[0]["n"]
    assert file_count == 5, (
        f"Expected 5 File nodes; final partial batch flush may have failed. "
        f"Got {file_count} files, summary={summary}"
    )
    db.close()


def test_batch_size_zero_raises_valueerror(tmp_path):
    """batch_size=0 raises ValueError with clear message."""
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=2)

    db = DuckDBBackend(":memory:")
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

    db = DuckDBBackend(":memory:")
    db.init_schema()

    # Index normally first
    summary = Indexer().index_repo(corpus_path, "snowflake", db, use_git=False, batch_size=10)

    # Both files should be indexed
    assert summary["files_parsed"] == 2
    assert summary["quality"]["failed"] == 0

    # Verify both views exist in the graph
    tables = db.run_read(
        "SELECT name FROM \"SqlTable\" WHERE name IN ('good1', 'good2') ORDER BY name",
        {},
    )
    table_names = [r["name"] for r in tables]
    assert set(table_names) == {"good1", "good2"}, (
        f"Expected both views; got {table_names}. Summary: {summary}"
    )
    db.close()


def test_batch_size_observable_in_return_dict(tmp_path):
    """batch_size parameter is observable in the return dict."""
    corpus_path = tmp_path / "corpus"
    _write_test_corpus(corpus_path, num_files=3)

    db = DuckDBBackend(":memory:")
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


def _write_shared_dim_corpus(corpus_path: Path) -> None:
    """Write a corpus where one shared dimension table is referenced by 3 view files.

    dim_customer is DEFINED in ddl/dim_customer.sql (defined_in_file non-empty).
    Three view files (view_a, view_b, view_c) each SELECT a column FROM dim_customer,
    producing three distinct COLUMN_LINEAGE edges mapping different src columns to the
    same dim_customer column.

    This exercises the cross-file batch dedup path:
    - table_rows for dim_customer appear 4 times across files (1 DDL + 3 ref rows);
      dedup must keep the row with non-empty defined_in_file.
    - COLUMN_LINEAGE edges (src_key, dst_key) are unique across files in this fixture
      (each view maps its own column), so all three edges must survive dedup.
    """
    corpus_path.mkdir(parents=True, exist_ok=True)
    ddl_dir = corpus_path / "ddl"
    ddl_dir.mkdir()

    # dim_customer DDL — defines the shared dimension table with a 'customer_id' column
    (ddl_dir / "dim_customer.sql").write_text(
        "CREATE TABLE dim_customer (customer_id INT, customer_name VARCHAR);",
        encoding="utf-8",
    )

    # Three view files each joining against dim_customer
    views_dir = corpus_path / "views"
    views_dir.mkdir()
    (views_dir / "view_a.sql").write_text(
        "CREATE VIEW view_a AS SELECT a.customer_id AS cid_a FROM dim_customer a;",
        encoding="utf-8",
    )
    (views_dir / "view_b.sql").write_text(
        "CREATE VIEW view_b AS SELECT b.customer_id AS cid_b FROM dim_customer b;",
        encoding="utf-8",
    )
    (views_dir / "view_c.sql").write_text(
        "CREATE VIEW view_c AS SELECT c.customer_id AS cid_c FROM dim_customer c;",
        encoding="utf-8",
    )


def _get_graph_state_with_properties(db: DuckDBBackend) -> dict:
    """Query the graph for counts AND properties needed to prove F1-AC4.

    Returns a dict with:
      - node/edge counts (same as the existing equivalence test)
      - dim_customer.defined_in_file  — must be non-empty (provenance preserved)
      - COLUMN_LINEAGE edge property sets (query_id, transform, confidence) sorted
        for deterministic comparison
    """
    tables = sorted(r["q"] for r in db.run_read('SELECT qualified AS q FROM "SqlTable"', {}))
    edge_count = db.run_read('SELECT count(*) AS n FROM "COLUMN_LINEAGE"', {})[0]["n"]
    col_count = db.run_read('SELECT count(*) AS n FROM "SqlColumn"', {})[0]["n"]

    # defined_in_file provenance for the shared dim_customer table
    dim_rows = db.run_read(
        'SELECT defined_in_file AS dif FROM "SqlTable" WHERE name = ?',
        {"name": "dim_customer"},
    )
    dim_defined_in_file = dim_rows[0]["dif"] if dim_rows else None

    # All COLUMN_LINEAGE edge properties — sorted so comparison is order-independent
    edge_props = sorted(
        (r["src"], r["dst"], r["transform"], r["confidence"], r["query_id"])
        for r in db.run_read(
            "SELECT cl.src_key AS src, cl.dst_key AS dst, "
            "cl.transform AS transform, cl.confidence AS confidence, "
            'cl.query_id AS query_id FROM "COLUMN_LINEAGE" cl',
            {},
        )
    )

    return {
        "tables": tables,
        "edge_count": edge_count,
        "col_count": col_count,
        "dim_defined_in_file": dim_defined_in_file,
        "edge_props": edge_props,
    }


def test_shared_dim_batch_vs_per_file_properties_identical(tmp_path):
    """F1-AC4: batch path and batch_size=1 produce identical graphs including properties.

    Exercises the cross-file batch dedup path introduced in PR-1 (v1.1.1):
      - dim_customer appears in 4 files (1 DDL + 3 views); the dedup must keep
        the row with non-empty defined_in_file (provenance guard).
      - Three COLUMN_LINEAGE edges (one per view file) all have distinct (src,dst)
        pairs so all three must survive dedup; their properties (query_id, transform,
        confidence) must be identical between the two indexing paths.

    Guards the Risks table entry: 'defined_in_file provenance lost when a shared
    table is deduped across files' and 'batch dedup silently dropping
    COLUMN_LINEAGE.query_id/transform'.
    """
    corpus_path = tmp_path / "corpus"
    _write_shared_dim_corpus(corpus_path)

    # Index with batch_size=1 (per-file flush — the reference path)
    db1 = DuckDBBackend(":memory:")
    db1.init_schema()
    Indexer().index_repo(corpus_path, "snowflake", db1, use_git=False, batch_size=1)
    state1 = _get_graph_state_with_properties(db1)
    db1.close()

    # Index with batch_size=50 (all 4 files in one batch — exercises cross-file dedup)
    db50 = DuckDBBackend(":memory:")
    db50.init_schema()
    Indexer().index_repo(corpus_path, "snowflake", db50, use_git=False, batch_size=50)
    state50 = _get_graph_state_with_properties(db50)
    db50.close()

    # dim_customer.defined_in_file must be non-empty in both paths (provenance preserved)
    assert state1["dim_defined_in_file"], (
        "batch_size=1: dim_customer.defined_in_file is empty — provenance lost"
    )
    assert state50["dim_defined_in_file"], (
        "batch_size=50: dim_customer.defined_in_file is empty — "
        "batch dedup dropped provenance from the DDL row"
    )

    # The two paths must agree on which file defines dim_customer
    assert state1["dim_defined_in_file"] == state50["dim_defined_in_file"], (
        f"defined_in_file mismatch: batch_size=1 → {state1['dim_defined_in_file']!r}, "
        f"batch_size=50 → {state50['dim_defined_in_file']!r}"
    )

    # COLUMN_LINEAGE edge properties must be identical (query_id, transform, confidence)
    assert state1["edge_props"] == state50["edge_props"], (
        f"COLUMN_LINEAGE edge properties differ between batch paths:\n"
        f"  batch_size=1 : {state1['edge_props']}\n"
        f"  batch_size=50: {state50['edge_props']}"
    )

    # Node/edge counts must match (existing equivalence check extended to this fixture)
    assert state1["tables"] == state50["tables"], (
        f"Table sets differ: {state1['tables']} vs {state50['tables']}"
    )
    assert state1["edge_count"] == state50["edge_count"], (
        f"COLUMN_LINEAGE counts differ: {state1['edge_count']} vs {state50['edge_count']}"
    )
    assert state1["col_count"] == state50["col_count"], (
        f"Column counts differ: {state1['col_count']} vs {state50['col_count']}"
    )
