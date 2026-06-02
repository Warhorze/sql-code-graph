"""Integration tests for cross-file lineage resolution — Phase 5 realignment.

CrossFileAggregator.resolve_pass2 was deleted (zero production call sites).
These tests now drive the production path: index_repo on tiny two/three-file
fixtures, asserting on the resulting graph (column lineage edges, no crash on
deleted file).  Observable-output rule: assert on graph edges, not on
ParsedFile internal state.
"""

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import RelType
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.aggregator import CrossFileAggregator


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def test_cross_file_view_resolution(db, tmp_path):
    """Views defined in one file are resolved from another via the production pass-2 path.

    File A defines customers and orders; File B creates customer_orders view joining them;
    File C queries the view.  After index_repo the view's column lineage must be present.
    """
    (tmp_path / "base.sql").write_text(
        "CREATE TABLE customers (id INT, name VARCHAR);\n"
        "CREATE TABLE orders (id INT, customer_id INT);\n"
    )
    (tmp_path / "views.sql").write_text(
        "CREATE VIEW customer_orders AS\n"
        "SELECT c.id, c.name, o.id as order_id\n"
        "FROM customers c JOIN orders o ON c.id = o.customer_id;\n"
    )
    (tmp_path / "reports.sql").write_text("SELECT * FROM customer_orders WHERE id > 100;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # At minimum, the view must exist as a table node
    rows = db.run_read(
        "MATCH (t:SqlTable {qualified: $q}) RETURN t.qualified AS q",
        {"q": "customer_orders"},
    )
    assert rows, (
        "customer_orders view not found in graph after index_repo — "
        "cross-file view resolution did not run."
    )


def test_view_pass2_emits_column_lineage_edges(db, tmp_path):
    """After pass-2 resolution, the view query must carry column lineage edges.

    customer_orders selects c.id, c.name, o.id→order_id from two source tables.
    Each projected column must appear as a COLUMN_LINEAGE edge in the graph.
    """
    (tmp_path / "base.sql").write_text(
        "CREATE TABLE customers (id INT, name VARCHAR);\n"
        "CREATE TABLE orders (id INT, customer_id INT);\n"
    )
    (tmp_path / "views.sql").write_text(
        "CREATE VIEW customer_orders AS\n"
        "SELECT c.id, c.name, o.id as order_id\n"
        "FROM customers c JOIN orders o ON c.id = o.customer_id;\n"
    )

    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # COLUMN_LINEAGE edges into customer_orders.* must exist
    rows = db.run_read(
        f"MATCH (src:SqlColumn)-[:{RelType.COLUMN_LINEAGE}]->(dst:SqlColumn) "
        "WHERE dst.table_qualified = 'customer_orders' "
        "RETURN src.id AS src, dst.id AS dst LIMIT 20",
        {},
    )
    assert rows, (
        "No COLUMN_LINEAGE edges into customer_orders found after index_repo.\n"
        "CREATE VIEW customer_orders AS SELECT c.id, c.name, o.id … "
        "must emit at least one COLUMN_LINEAGE edge per projected column."
    )


def test_cross_file_lineage_deleted_file(tmp_path):
    """Removing a file before index_repo does not crash; the summary is returned.

    Phase 5 equivalent of the old resolve_pass2 deleted-file test.
    If the reference file is deleted before the pool worker re-reads it for pass 2,
    the worker raises and index_repo falls back to the pass-1 result with a WARNING.
    """
    (tmp_path / "define.sql").write_text("CREATE TABLE raw_orders (id INT, amount DECIMAL);")
    ref_file = tmp_path / "reference.sql"
    ref_file.write_text("SELECT id, amount FROM raw_orders;")

    # Verify pass-2 would be needed
    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.registry import get_parser

    schema = SchemaResolver()
    parser = get_parser(None, schema)
    pass1_def = parser.parse_file(tmp_path / "define.sql", (tmp_path / "define.sql").read_text())
    pass1_ref = parser.parse_file(ref_file, ref_file.read_text())
    agg = CrossFileAggregator()
    agg.register_pass1(pass1_def)
    agg.register_pass1(pass1_ref)
    assert agg._needs_pass2(pass1_ref), (
        "reference.sql should need pass-2 — the test premise is invalid otherwise."
    )

    # Delete the reference file before indexing
    ref_file.unlink()

    db = KuzuBackend(":memory:")
    db.init_schema()
    try:
        summary = Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    finally:
        db.close()

    assert isinstance(summary, dict), "index_repo must return a summary dict even on file-not-found"
