"""End-to-end regression guard for column lineage — guards against the v0.3.0 regression
where column_lineage was always 0 due to two wiring gaps (ARCHITECTURE_REVIEW §11.2)."""

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


def test_column_lineage_edges_nonzero_after_index(tmp_path):
    """Guard against the v0.3.0 regression: column lineage was always 0.

    In v0.3.0, _extract_column_lineage was never called (ansi_parser.py hardcoded [])
    and the sg_lineage → LineageEdge conversion was a TODO. Both are now fixed.
    This test must NEVER be marked xfail — a failure here means the fix was reverted.
    """
    sql = "CREATE VIEW revenue AS SELECT amount, customer_id FROM orders;"
    (tmp_path / "views.sql").write_text(sql)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect=None, db=backend)

    rows = backend.run_read(
        'SELECT COUNT(*) AS cnt FROM "COLUMN_LINEAGE"',
        {},
    )
    assert rows[0]["cnt"] > 0, (
        "Column lineage edges must be non-zero after indexing a CREATE VIEW AS SELECT. "
        f"Index summary: {summary}"
    )
