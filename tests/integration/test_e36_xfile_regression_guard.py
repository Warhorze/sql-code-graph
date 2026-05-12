"""Integration regression guard for E36 cross-file temp table resolution."""

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def test_e36_xfile_regression_guard(tmp_path):
    """E36 cross-file: end-to-end via Indexer.index_repo produces COLUMN_LINEAGE edges.

    This is the single most important regression guard for T-07-01.
    It ensures that cross-file temp table propagation does not silently regress
    in the future.

    Index a two-file directory:
    - file_a.sql creates a TEMP TABLE t
    - file_b.sql INSERTs from t
    Query the database and assert at least one COLUMN_LINEAGE edge exists
    where the destination is in file_b.
    """
    # Create the test fixture files
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()

    file_a = sql_dir / "file_a.sql"
    file_a.write_text("CREATE TEMP TABLE t AS SELECT a FROM src;")

    file_b = sql_dir / "file_b.sql"
    file_b.write_text("INSERT INTO dst SELECT a FROM t;")

    # Index the directory
    db = KuzuBackend(":memory:")
    db.init_schema()
    indexer = Indexer()
    result = indexer.index_repo(sql_dir, "snowflake", db)

    # Verify files were parsed
    assert result["files_parsed"] == 2, f"Expected 2 files parsed, got {result}"

    # Query the database for COLUMN_LINEAGE edges into the dst table
    query = """
    MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->(d:SqlColumn)
    WHERE d.table_qualified CONTAINS 'dst'
    RETURN count(*) as cnt
    """
    rows = db.run_read(query, {})
    edge_count = rows[0]["cnt"] if rows else 0

    assert edge_count >= 1, (
        f"Expected at least one COLUMN_LINEAGE edge into dst table; "
        f"got {edge_count}. Query result: {rows}"
    )
