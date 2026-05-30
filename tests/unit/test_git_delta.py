"""Unit tests for git_name_status_delta.

Uses a real temporary git repo to exercise add/modify/delete/rename
classification and the ignore/suffix filter.
"""

import subprocess
from pathlib import Path

import pytest

from sqlcg.indexer.git_delta import git_name_status_delta


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with an initial commit."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    # Initial commit so repo has history
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _commit(repo: Path, msg: str = "change") -> str:
    """Stage all changes and commit; return the new HEAD SHA."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _get_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_added_file_appears_in_delta(git_repo):
    """A new .sql file is classified as added."""
    old_sha = _get_head(git_repo)
    (git_repo / "new.sql").write_text("SELECT 1")
    new_sha = _commit(git_repo, "add sql")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert (git_repo / "new.sql").resolve() in delta.added
    assert not delta.modified
    assert not delta.deleted


def test_modified_file_appears_in_delta(git_repo):
    """A modified .sql file is classified as modified."""
    (git_repo / "existing.sql").write_text("SELECT 1")
    old_sha = _commit(git_repo, "add sql")
    (git_repo / "existing.sql").write_text("SELECT 2")
    new_sha = _commit(git_repo, "modify sql")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert (git_repo / "existing.sql").resolve() in delta.modified
    assert not delta.added
    assert not delta.deleted


def test_deleted_file_appears_in_delta(git_repo):
    """A deleted .sql file is classified as deleted."""
    (git_repo / "gone.sql").write_text("SELECT 1")
    old_sha = _commit(git_repo, "add sql")
    (git_repo / "gone.sql").unlink()
    new_sha = _commit(git_repo, "delete sql")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert (git_repo / "gone.sql").resolve() in delta.deleted
    assert not delta.added
    assert not delta.modified


def test_renamed_file_produces_delete_and_add(git_repo):
    """A renamed .sql file is a delete of old + add of new."""
    (git_repo / "old.sql").write_text("SELECT 1")
    old_sha = _commit(git_repo, "add sql")
    (git_repo / "old.sql").rename(git_repo / "new.sql")
    new_sha = _commit(git_repo, "rename sql")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert (git_repo / "old.sql").resolve() in delta.deleted
    assert (git_repo / "new.sql").resolve() in delta.added
    assert not delta.modified


def test_non_sql_change_is_excluded(git_repo):
    """Changes to non-.sql files are not included in the delta."""
    old_sha = _get_head(git_repo)
    (git_repo / "data.csv").write_text("a,b\n1,2\n")
    new_sha = _commit(git_repo, "add csv")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert not delta.added
    assert not delta.modified
    assert not delta.deleted


def test_ignored_sql_file_is_excluded(git_repo):
    """SQL files matching the ignore spec are excluded."""
    # Create a .sqlcgignore that excludes ignored/
    (git_repo / ".sqlcgignore").write_text("ignored/\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add ignore"], cwd=git_repo, check=True, capture_output=True
    )
    old_sha = _get_head(git_repo)

    (git_repo / "ignored").mkdir(exist_ok=True)
    (git_repo / "ignored" / "skip.sql").write_text("SELECT 1")
    new_sha = _commit(git_repo, "add ignored sql")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    # The ignored file must NOT appear in any set
    assert not delta.added, f"Ignored file appeared in added: {delta.added}"
    assert not delta.modified
    assert not delta.deleted


def test_bad_sha_returns_none(git_repo):
    """An unknown SHA returns None so callers fall back to full index."""
    delta = git_name_status_delta(git_repo, "deadbeefdeadbeef", "cafebabecafebabe")
    assert delta is None


def test_multiple_changes_classified_correctly(git_repo):
    """Add + modify + delete in one commit produce the correct three sets."""
    (git_repo / "keep.sql").write_text("SELECT 1")
    (git_repo / "delete_me.sql").write_text("SELECT 2")
    old_sha = _commit(git_repo, "setup")

    (git_repo / "new.sql").write_text("SELECT 3")  # added
    (git_repo / "keep.sql").write_text("SELECT 99")  # modified
    (git_repo / "delete_me.sql").unlink()  # deleted
    new_sha = _commit(git_repo, "multi change")

    delta = git_name_status_delta(git_repo, old_sha, new_sha)
    assert delta is not None
    assert (git_repo / "new.sql").resolve() in delta.added
    assert (git_repo / "keep.sql").resolve() in delta.modified
    assert (git_repo / "delete_me.sql").resolve() in delta.deleted
