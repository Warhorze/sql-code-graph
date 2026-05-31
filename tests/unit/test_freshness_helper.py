"""Unit tests for core/freshness.py — compute_freshness and render_freshness_line.

All tests use a real temp git repo so git calls produce deterministic output.
No KuzuDB dependency — freshness.py is deliberately backend-free.

Scenarios from sprint_12_v1.1.0.md PR-A:
    A — stale detection (N commits behind HEAD)
    B — dirty detection (uncommitted working-tree changes)
    C — unknown SHA (SHA not in git history)
    D — non-git directory / missing indexed_sha
    G — db info CLI on empty DB (no crash, no freshness line)
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.core.freshness import Freshness, _git, compute_freshness, render_freshness_line

# ---------------------------------------------------------------------------
# Git repo helpers (shared across tests)
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    """Initialise a git repo with user identity configured."""
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
    """Stage everything and commit; return the new HEAD SHA."""
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path):
    """A minimal git repo with one initial commit."""
    _init_repo(tmp_path)
    (tmp_path / "init.sql").write_text("-- init\n")
    _commit_all(tmp_path, "initial commit")
    return tmp_path


# ---------------------------------------------------------------------------
# Scenario A — stale detection
# ---------------------------------------------------------------------------


class TestStaleDetection:
    """Scenario A: indexed at commit 1, HEAD is commit 2 → stale_by_commits == 1."""

    def test_one_commit_behind(self, git_repo: Path):
        """stale_by_commits is 1 when HEAD is one commit ahead of indexed_sha."""
        sha_a = _head_sha(git_repo)

        # Advance HEAD by one commit
        (git_repo / "step.sql").write_text("SELECT 1;\n")
        _commit_all(git_repo, "step 1")

        f = compute_freshness(git_repo, sha_a)

        assert f.stale_by_commits == 1, (
            f"Expected stale_by_commits=1 (one commit ahead), got {f.stale_by_commits}"
        )
        assert f.dirty is False
        assert f.indexed_sha == sha_a
        assert f.head_sha is not None
        assert f.head_sha != sha_a

    def test_two_commits_behind(self, git_repo: Path):
        """stale_by_commits is 2 when HEAD is two commits ahead of indexed_sha."""
        sha_a = _head_sha(git_repo)

        (git_repo / "step1.sql").write_text("SELECT 1;\n")
        _commit_all(git_repo, "step 1")
        (git_repo / "step2.sql").write_text("SELECT 2;\n")
        _commit_all(git_repo, "step 2")

        f = compute_freshness(git_repo, sha_a)

        assert f.stale_by_commits == 2

    def test_up_to_date(self, git_repo: Path):
        """stale_by_commits is 0 when indexed_sha == HEAD."""
        sha = _head_sha(git_repo)

        f = compute_freshness(git_repo, sha)

        assert f.stale_by_commits == 0
        assert f.dirty is False
        assert f.indexed_sha == sha
        assert f.head_sha == sha

    def test_render_stale_line(self, git_repo: Path):
        """render_freshness_line shows N commit(s) behind HEAD."""
        sha_a = _head_sha(git_repo)

        (git_repo / "next.sql").write_text("SELECT 42;\n")
        _commit_all(git_repo, "advance")

        f = compute_freshness(git_repo, sha_a)
        line = render_freshness_line(f)

        assert sha_a[:8] in line, f"SHA prefix not in line: {line!r}"
        assert "commit(s) behind HEAD" in line or "behind HEAD" in line, (
            f"Expected stale message in: {line!r}"
        )

    def test_render_up_to_date_line(self, git_repo: Path):
        """render_freshness_line shows 'up to date' when graph is current."""
        sha = _head_sha(git_repo)
        f = compute_freshness(git_repo, sha)
        line = render_freshness_line(f)

        assert "up to date" in line, f"Expected 'up to date' in: {line!r}"
        assert sha[:8] in line


# ---------------------------------------------------------------------------
# Scenario B — dirty detection
# ---------------------------------------------------------------------------


class TestDirtyDetection:
    """Scenario B: working tree has uncommitted changes → dirty is True."""

    def test_dirty_is_true_on_uncommitted_change(self, git_repo: Path):
        """dirty is True when a tracked file has been modified without committing."""
        sha = _head_sha(git_repo)

        # Modify a tracked file without committing
        (git_repo / "init.sql").write_text("-- modified\n")

        f = compute_freshness(git_repo, sha)

        assert f.dirty is True, "Expected dirty=True after uncommitted modification"
        assert f.stale_by_commits == 0  # SHA is up to date, only WD is dirty

    def test_dirty_false_after_commit(self, git_repo: Path):
        """dirty is False once changes are committed."""
        (git_repo / "new.sql").write_text("SELECT 1;\n")
        sha = _commit_all(git_repo, "clean commit")

        f = compute_freshness(git_repo, sha)

        assert f.dirty is False

    def test_render_dirty_line(self, git_repo: Path):
        """render_freshness_line includes 'working tree dirty' when dirty is True."""
        sha = _head_sha(git_repo)
        (git_repo / "init.sql").write_text("-- dirty\n")

        f = compute_freshness(git_repo, sha)
        line = render_freshness_line(f)

        assert "working tree dirty" in line, f"Expected dirty in: {line!r}"


# ---------------------------------------------------------------------------
# Scenario C — unknown SHA
# ---------------------------------------------------------------------------


class TestUnknownSha:
    """Scenario C: indexed_sha is not in the git history → stale_by_commits is None."""

    def test_unknown_sha_returns_none_distance(self, git_repo: Path):
        """stale_by_commits is None when the indexed SHA does not exist in history."""
        fake_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        f = compute_freshness(git_repo, fake_sha)

        assert f.stale_by_commits is None, (
            f"Expected None for unknown SHA, got {f.stale_by_commits}"
        )
        assert f.indexed_sha == fake_sha
        assert f.head_sha is not None

    def test_render_unknown_sha_line(self, git_repo: Path):
        """render_freshness_line shows 'commit distance unknown' for unknown SHA."""
        fake_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        f = compute_freshness(git_repo, fake_sha)
        line = render_freshness_line(f)

        assert "commit distance unknown" in line, f"Expected 'commit distance unknown' in: {line!r}"


# ---------------------------------------------------------------------------
# Scenario D — non-git directory / missing indexed_sha
# ---------------------------------------------------------------------------


class TestNonGitDirectory:
    """Scenario D: root is not a git repo or indexed_sha is None → all-None/False."""

    def test_non_git_dir_returns_all_none(self, tmp_path: Path):
        """compute_freshness on a non-git dir returns Freshness with all-None fields."""
        f = compute_freshness(tmp_path, None)

        assert f.indexed_sha is None
        assert f.head_sha is None
        assert f.stale_by_commits is None
        assert f.dirty is False
        assert f.branch is None

    def test_non_git_dir_does_not_raise(self, tmp_path: Path):
        """compute_freshness never raises, even on a non-git directory."""
        # Must not raise any exception
        result = compute_freshness(tmp_path, None)
        assert isinstance(result, Freshness)

    def test_none_indexed_sha_with_git_repo_has_none_distance(self, git_repo: Path):
        """When indexed_sha is None, stale_by_commits is also None (no base to measure from)."""
        f = compute_freshness(git_repo, None)

        assert f.stale_by_commits is None
        # head_sha is available (the repo is real)
        assert f.head_sha is not None
        # indexed_sha is passed through as None
        assert f.indexed_sha is None

    def test_render_no_sha_line(self, tmp_path: Path):
        """render_freshness_line returns 'not available' when indexed_sha is None."""
        f = compute_freshness(tmp_path, None)
        line = render_freshness_line(f)

        assert "not available" in line, f"Expected 'not available' in: {line!r}"


# ---------------------------------------------------------------------------
# Scenario G — db info CLI does not crash on empty DB
# ---------------------------------------------------------------------------


class TestCliEmptyDb:
    """Scenario G: 'db info' on an empty DB must not crash and must not print freshness."""

    def test_empty_db_no_freshness_line_no_crash(self, tmp_path: Path):
        """CLI 'db info' on empty DB exits 0 and does not print a freshness line."""
        from typer.testing import CliRunner

        from sqlcg.cli.commands.db import app

        runner = CliRunner()

        with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.__enter__.return_value = mock_backend
            mock_backend.__exit__.return_value = None
            mock_backend.get_schema_version.return_value = "5"
            # Empty DB: no indexed SHA, no Repo nodes
            mock_backend.get_indexed_sha.return_value = None

            def run_read_side_effect(query: str, _params: dict):
                if "STAR_SOURCE" in query or "STAR_EXPANSION" in query:
                    return [{"n": 0}]
                if "path" in query and "Repo" in query:
                    return []  # no repo rows
                if "Repo" in query:
                    return [{"count": 0}]
                if "parsing_mode" in query:
                    return []
                return [{"count": 0}]

            mock_backend.run_read.side_effect = run_read_side_effect
            mock_get_backend.return_value = mock_backend

            result = runner.invoke(app, ["info"])

        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
        assert "indexed at" not in result.output, "No freshness line should appear for an empty DB"
        assert "Database is empty" in result.output


# ---------------------------------------------------------------------------
# _git helper
# ---------------------------------------------------------------------------


class TestGitHelper:
    """Unit tests for the private _git() helper."""

    def test_returns_none_for_nonexistent_command(self, tmp_path: Path):
        """_git returns None when the git command fails."""
        result = _git(tmp_path, "nonexistent-git-subcommand-xyz")
        assert result is None

    def test_returns_none_for_non_git_directory(self, tmp_path: Path):
        """_git returns None in a directory that has no .git."""
        result = _git(tmp_path, "rev-parse", "HEAD")
        assert result is None

    def test_returns_head_sha_in_git_repo(self, git_repo: Path):
        """_git returns the HEAD SHA in a valid git repo."""
        result = _git(git_repo, "rev-parse", "HEAD")
        assert result is not None
        assert len(result) == 40  # full SHA
