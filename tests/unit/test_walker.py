"""Unit tests for SQL file walker."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.indexer.walker import walk_sql_files
from sqlcg.utils.ignore import load_ignore_spec


@pytest.fixture
def temp_sql_tree():
    """Create a temporary directory tree with SQL files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        (tmpdir / "queries.sql").write_text("SELECT 1;")
        (tmpdir / "subdir").mkdir()
        (tmpdir / "subdir" / "nested.sql").write_text("SELECT 2;")
        (tmpdir / "subdir" / "deep").mkdir()
        (tmpdir / "subdir" / "deep" / "query.sql").write_text("SELECT 3;")
        (tmpdir / "config.yaml").write_text("key: value")
        (tmpdir / "README.md").write_text("# Test")

        yield tmpdir


# ---------------------------------------------------------------------------
# rglob fallback (use_git=False)
# ---------------------------------------------------------------------------

def test_walk_sql_files_yields_only_sql(temp_sql_tree):
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec, use_git=False))
    assert len(files) == 3
    assert all(f.suffix == ".sql" for f in files)
    assert {f.name for f in files} == {"queries.sql", "nested.sql", "query.sql"}


def test_walk_sql_files_respects_ignore_patterns(temp_sql_tree):
    (temp_sql_tree / ".sqlcgignore").write_text("subdir/\n*.bak\n")
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec, use_git=False))
    assert len(files) == 1
    assert files[0].name == "queries.sql"


def test_walk_sql_files_traverses_subdirectories(temp_sql_tree):
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec, use_git=False))
    assert len(files) == 3
    paths = {str(f.relative_to(temp_sql_tree)) for f in files}
    assert paths == {"queries.sql", "subdir/nested.sql", "subdir/deep/query.sql"}


def test_walk_sql_files_empty_ignore_patterns(temp_sql_tree):
    spec = load_ignore_spec(temp_sql_tree)
    files = list(walk_sql_files(temp_sql_tree, spec, use_git=False))
    assert len(files) == 3


# ---------------------------------------------------------------------------
# git mode (use_git=True, default)
# ---------------------------------------------------------------------------

def test_git_mode_returns_only_tracked_files(temp_sql_tree):
    """Only files returned by git ls-files are indexed."""
    tracked = ["queries.sql", "subdir/nested.sql"]

    def fake_run(cmd, cwd, capture_output, text, check):
        assert cmd == ["git", "ls-files", "--cached"]
        assert cwd == temp_sql_tree
        import subprocess as sp
        r = sp.CompletedProcess(cmd, 0)
        r.stdout = "\n".join(tracked) + "\n"
        r.stderr = ""
        return r

    spec = load_ignore_spec(temp_sql_tree)
    with patch("sqlcg.indexer.walker.subprocess.run", side_effect=fake_run):
        files = list(walk_sql_files(temp_sql_tree, spec, use_git=True))

    assert len(files) == 2
    names = {f.name for f in files}
    assert names == {"queries.sql", "nested.sql"}


def test_git_mode_falls_back_to_rglob_when_git_unavailable(temp_sql_tree):
    """Falls back to rglob when git is not installed."""
    spec = load_ignore_spec(temp_sql_tree)
    with patch(
        "sqlcg.indexer.walker.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        files = list(walk_sql_files(temp_sql_tree, spec, use_git=True))

    assert len(files) == 3


def test_git_mode_falls_back_when_not_a_git_repo(temp_sql_tree):
    """Falls back to rglob when the directory is not a git repo."""
    spec = load_ignore_spec(temp_sql_tree)
    with patch(
        "sqlcg.indexer.walker.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, "git"),
    ):
        files = list(walk_sql_files(temp_sql_tree, spec, use_git=True))

    assert len(files) == 3


def test_git_mode_is_default(temp_sql_tree):
    """use_git=True is the default — no explicit argument needed."""
    tracked = ["queries.sql"]

    def fake_run(cmd, cwd, capture_output, text, check):
        assert cmd == ["git", "ls-files", "--cached"]
        assert cwd == temp_sql_tree
        import subprocess as sp
        r = sp.CompletedProcess(cmd, 0)
        r.stdout = "queries.sql\n"
        r.stderr = ""
        return r

    spec = load_ignore_spec(temp_sql_tree)
    with patch("sqlcg.indexer.walker.subprocess.run", side_effect=fake_run):
        files = list(walk_sql_files(temp_sql_tree, spec))  # no use_git arg

    assert len(files) == 1
    assert files[0].name == "queries.sql"
