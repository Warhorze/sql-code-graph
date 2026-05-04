"""Unit tests for SQL file walker."""

import tempfile
from pathlib import Path

import pytest

from sqlcg.indexer.walker import walk_sql_files
from sqlcg.utils.ignore import load_ignore_spec


@pytest.fixture
def temp_sql_tree():
    """Create a temporary directory tree with SQL files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create some SQL files
        (tmpdir / "queries.sql").write_text("SELECT 1;")
        (tmpdir / "subdir").mkdir()
        (tmpdir / "subdir" / "nested.sql").write_text("SELECT 2;")
        (tmpdir / "subdir" / "deep").mkdir()
        (tmpdir / "subdir" / "deep" / "query.sql").write_text("SELECT 3;")

        # Create non-SQL files
        (tmpdir / "config.yaml").write_text("key: value")
        (tmpdir / "README.md").write_text("# Test")

        yield tmpdir


def test_walk_sql_files_yields_only_sql(temp_sql_tree):
    """Test that walk_sql_files only yields .sql files."""
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec))

    # Should find 3 SQL files
    assert len(files) == 3
    assert all(f.suffix == ".sql" for f in files)

    file_names = {f.name for f in files}
    assert file_names == {"queries.sql", "nested.sql", "query.sql"}


def test_walk_sql_files_respects_ignore_patterns(temp_sql_tree):
    """Test that walk_sql_files respects .sqlcgignore patterns."""
    # Create .sqlcgignore
    (temp_sql_tree / ".sqlcgignore").write_text("subdir/\n*.bak\n")

    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec))

    # Should only find the top-level SQL file
    assert len(files) == 1
    assert files[0].name == "queries.sql"


def test_walk_sql_files_traverses_subdirectories(temp_sql_tree):
    """Test that walk_sql_files traverses subdirectories."""
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec))

    # Should find all SQL files in subdirectories
    assert len(files) == 3
    paths = {str(f.relative_to(temp_sql_tree)) for f in files}
    assert paths == {"queries.sql", "subdir/nested.sql", "subdir/deep/query.sql"}


def test_walk_sql_files_empty_ignore_patterns(temp_sql_tree):
    """Test with no .sqlcgignore file (no patterns)."""
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec))

    # Should find all 3 SQL files
    assert len(files) == 3
