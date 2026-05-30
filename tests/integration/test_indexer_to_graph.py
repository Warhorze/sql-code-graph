"""Integration tests for Indexer and graph persistence."""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.utils.ignore import load_ignore_spec


@pytest.fixture
def temp_db():
    """Create a temporary in-memory KuzuDB."""
    db = KuzuBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


def test_index_synthetic_fixtures(temp_db):
    """Test full index of synthetic fixtures produces expected nodes."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    indexer = Indexer()
    result = indexer.index_repo(
        fixtures_path,
        dialect=None,
        db=temp_db,
        timeout_per_file=30,
    )

    # Verify result dict has expected keys
    assert "files_parsed" in result
    assert "parse_errors" in result
    assert "tables_found" in result
    assert "lineage_edges_created" in result

    # Should have parsed some files
    assert result["files_parsed"] > 0
    # Should have found some tables
    assert result["tables_found"] > 0


def test_reindex_file_rollback_on_error(temp_db):
    """Test that reindex_file rolls back on injected error."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    indexer = Indexer()

    # Initial index
    indexer.index_repo(fixtures_path, dialect=None, db=temp_db, timeout_per_file=30)

    # Get the file path of one of the fixtures
    sql_files = list(fixtures_path.glob("*.sql"))
    if not sql_files:
        pytest.skip("No SQL files found in fixtures")

    file_path = str(sql_files[0])

    # Get node count before re-index
    result = temp_db.run_read("MATCH (n) RETURN COUNT(*) as count", {})
    count_before = result[0]["count"]

    # Simulate reindex with rollback (delete the file to trigger error on re-read)
    original_path = Path(file_path)
    original_content = original_path.read_text()
    original_path.unlink()

    try:
        # This should fail during re-index but rollback
        indexer.reindex_file(file_path, temp_db, dialect=None)
    except Exception:
        pass  # Expected to fail

    # Restore the file for comparison
    original_path.write_text(original_content)

    # Node count should be unchanged (rollback)
    result = temp_db.run_read("MATCH (n) RETURN COUNT(*) as count", {})
    count_after = result[0]["count"]

    # Count should be the same (transaction rolled back)
    assert count_before == count_after


def test_walker_yields_only_sql_files(temp_db):
    """Test that walker yields only .sql files and respects ignore patterns."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    spec = load_ignore_spec(fixtures_path)

    from sqlcg.indexer.walker import walk_sql_files

    files = list(walk_sql_files(fixtures_path, spec))

    # All should be .sql files
    assert all(f.suffix == ".sql" for f in files)
    # Should find at least the 3 synthetic files
    assert len(files) >= 3


def test_sigint_during_index_aborts_without_partial_write(temp_db):
    """SIGINT during indexing must re-raise and write NO partial graph.

    A partial pass-1-only result is an incomplete graph (no cross-file
    resolution, no star expansion); flushing it would leave a misleading
    half-index that "keeps going" after the user expects the process to die.
    The contract is: kill workers, abort, write nothing — re-run to index.

    SIGINT lands in the main process while it blocks in HardKillPool.map
    (the worker subprocesses ignore SIGINT), so we patch map to raise
    KeyboardInterrupt — the same point the real interrupt surfaces.
    """
    from unittest.mock import patch

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    indexer = Indexer()

    with patch(
        "sqlcg.indexer.indexer.HardKillPool.map",
        side_effect=KeyboardInterrupt("User interrupted"),
    ):
        with pytest.raises(KeyboardInterrupt):
            indexer.index_repo(fixtures_path, dialect=None, db=temp_db, timeout_per_file=30)

    # No indexed content should have been written: no partial flush on interrupt.
    # (init_schema seeds a schema-version metadata node, so assert on File nodes,
    # which only the upsert pass — skipped on interrupt — creates.)
    result = temp_db.run_read("MATCH (f:File) RETURN COUNT(*) as count", {})
    assert result[0]["count"] == 0
