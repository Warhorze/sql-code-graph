"""Integration tests for freshness fields in the MCP db_info tool.

Uses a real in-memory DuckDB backend + a real temp git repo.  The backend singleton
(``_backend`` in tools.py) is patched to inject the real backend so db_info()
exercises the full code path including the new freshness fields.

Scenarios:
    E — db_info MCP returns stale_by_commits == 1 after HEAD advances
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.server.models import DbInfoResult
from sqlcg.server.tools import db_info

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )


def _commit_all(path: Path, msg: str = "commit") -> str:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _head_sha(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _setup_kuzu_with_repo(tmp_path: Path, git_root: Path, indexed_sha: str) -> DuckDBBackend:
    """Create an in-memory DuckDB with schema init, a Repo node, and indexed_sha set."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()

    # Insert a Repo node so the freshness query finds a root path
    backend.upsert_node("Repo", str(git_root), {"path": str(git_root), "name": "test_repo"})
    backend.set_indexed_sha(indexed_sha)
    return backend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path):
    """A minimal git repo with one initial commit."""
    _init_repo(tmp_path)
    (tmp_path / "init.sql").write_text("-- init\n")
    sha = _commit_all(tmp_path, "initial commit")
    return tmp_path, sha


# ---------------------------------------------------------------------------
# Scenario E — db_info returns stale_by_commits >= 1 after advancing HEAD
# ---------------------------------------------------------------------------


class TestDbInfoFreshnessIntegration:
    """Scenario E: real Kuzu DB + real git repo → freshness fields populated."""

    def test_stale_by_one_commit(self, tmp_path: Path):
        """db_info returns stale_by_commits == 1 after indexing then advancing HEAD."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        _init_repo(git_root)
        (git_root / "init.sql").write_text("-- init\n")
        sha_a = _commit_all(git_root, "commit A")

        # Advance HEAD by one commit AFTER recording indexed_sha
        (git_root / "next.sql").write_text("SELECT 1;\n")
        _commit_all(git_root, "commit B")

        backend = _setup_kuzu_with_repo(tmp_path, git_root, sha_a)
        try:
            with patch("sqlcg.server.tools._get_backend", return_value=backend):
                result = db_info()
        finally:
            backend.close()

        assert isinstance(result, DbInfoResult)
        assert result.stale_by_commits == 1, (
            f"Expected stale_by_commits=1, got {result.stale_by_commits}"
        )
        assert result.indexed_sha == sha_a
        assert result.head_sha is not None
        assert result.head_sha != sha_a

    def test_up_to_date_graph(self, tmp_path: Path):
        """db_info returns stale_by_commits == 0 when indexed_sha == HEAD."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        _init_repo(git_root)
        (git_root / "init.sql").write_text("-- init\n")
        sha = _commit_all(git_root, "commit A")

        backend = _setup_kuzu_with_repo(tmp_path, git_root, sha)
        try:
            with patch("sqlcg.server.tools._get_backend", return_value=backend):
                result = db_info()
        finally:
            backend.close()

        assert result.stale_by_commits == 0
        assert result.indexed_sha == sha
        assert result.head_sha == sha

    def test_dirty_working_tree(self, tmp_path: Path):
        """db_info returns dirty == True when working tree has uncommitted changes."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        _init_repo(git_root)
        (git_root / "init.sql").write_text("-- init\n")
        sha = _commit_all(git_root, "commit A")

        # Make the working tree dirty (uncommitted change)
        (git_root / "init.sql").write_text("-- modified\n")

        backend = _setup_kuzu_with_repo(tmp_path, git_root, sha)
        try:
            with patch("sqlcg.server.tools._get_backend", return_value=backend):
                result = db_info()
        finally:
            backend.close()

        assert result.dirty is True, "Expected dirty=True after uncommitted change"

    def test_no_sha_no_freshness_fields(self, tmp_path: Path):
        """db_info returns indexed_sha=None when the graph was never indexed."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        _init_repo(git_root)
        (git_root / "init.sql").write_text("-- init\n")
        _commit_all(git_root, "commit A")

        # Backend with NO indexed_sha set
        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            with patch("sqlcg.server.tools._get_backend", return_value=backend):
                result = db_info()
        finally:
            backend.close()

        assert result.indexed_sha is None
        assert result.stale_by_commits is None
