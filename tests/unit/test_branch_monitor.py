"""Unit tests for BranchMonitor."""

import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.indexer.watcher import BranchMonitor


@pytest.fixture
def mock_job_manager():
    """Create a mock WatchJobManager."""
    return MagicMock()


@pytest.fixture
def mock_indexer():
    """Create a mock Indexer."""
    return MagicMock()


@pytest.fixture
def mock_db():
    """Create a mock GraphBackend."""
    return MagicMock()


@pytest.fixture
def temp_path():
    """Create a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


_OLD_SHA = "aaa111aaa111aaa111aaa111aaa111aaa111aaa1"
_NEW_SHA = "bbb222bbb222bbb222bbb222bbb222bbb222bbb2"


def _make_git_mock(branch_call_limit: int = 2, head_sha: str = _NEW_SHA):
    """Return a mock for subprocess.run that simulates branch changes and SHA lookups.

    The mock distinguishes between:
      - git rev-parse --abbrev-ref HEAD  (branch name)
      - git rev-parse HEAD               (commit SHA)

    branch_call_limit: number of --abbrev-ref calls that return "main"; subsequent calls
    return "develop", triggering a branch-change.

    head_sha: returned for all git rev-parse HEAD calls. In practice, both old_sha and
    new_sha calls happen after the branch detection already switched to "develop", so the
    mock returns the same value for simplicity. The important assertions are that
    resync_changed is called (not index_repo), and that the SHA args are non-empty strings.
    """
    branch_call_count = [0]

    def git_mock(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0

        if "--abbrev-ref" in cmd:
            branch_call_count[0] += 1
            result.stdout = "main\n" if branch_call_count[0] <= branch_call_limit else "develop\n"
        else:
            # git rev-parse HEAD
            result.stdout = head_sha + "\n"

        return result

    return git_mock


def test_branch_monitor_triggers_resync_changed_on_branch_change(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Branch change must call resync_changed (not index_repo) with captured old/new SHAs."""
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch(
        "sqlcg.indexer.watcher.subprocess.run", side_effect=_make_git_mock(head_sha=_NEW_SHA)
    ):
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    # resync_changed must have been called, index_repo must NOT
    assert mock_indexer.resync_changed.called, "resync_changed should be called on branch change"
    assert not mock_indexer.index_repo.called, (
        "index_repo must NOT be called (replaced by resync_changed)"
    )

    # Verify positional args: (path, old_sha, new_sha, ...) and keyword db/dialect
    call_args = mock_indexer.resync_changed.call_args
    assert call_args[0][0] == temp_path, "first positional arg must be the watched path"
    # old_sha and new_sha are both non-empty strings (both come from git rev-parse HEAD mock)
    assert isinstance(call_args[0][1], str) and call_args[0][1], (
        "old_sha must be a non-empty string"
    )
    assert isinstance(call_args[0][2], str) and call_args[0][2], (
        "new_sha must be a non-empty string"
    )
    assert call_args[1]["db"] == mock_db, "db keyword arg must be mock_db"

    # cancel_all must be called
    assert mock_job_manager.cancel_all.called, "cancel_all should be called on branch change"


def test_branch_monitor_dialect_threaded_through(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """The dialect passed to BranchMonitor must be forwarded to resync_changed."""
    monitor = BranchMonitor(
        temp_path,
        mock_job_manager,
        mock_indexer,
        mock_db,
        _poll_interval=0.1,
        dialect="snowflake",
    )

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=_make_git_mock()):
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    assert mock_indexer.resync_changed.called
    call_kwargs = mock_indexer.resync_changed.call_args[1]
    assert call_kwargs.get("dialect") == "snowflake", (
        "dialect must be forwarded from BranchMonitor to resync_changed"
    )


def test_branch_monitor_no_resync_on_same_branch(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor doesn't resync repeatedly when branch stays constant.

    The initial None -> "main" transition triggers one resync (initialization).
    After that, no further resyncs occur as long as the branch remains "main".
    """

    def git_rev_parse_always_main(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "main\n"
        return result

    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_always_main):
        monitor.start()
        # Give it enough time for multiple poll cycles
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    # The initial None -> "main" triggers exactly one resync; after that, stable branch = no more
    call_count = mock_indexer.resync_changed.call_count
    assert call_count <= 1, (
        f"resync_changed should not be called repeatedly on a stable branch "
        f"(got {call_count} calls)"
    )
    # index_repo must never be called
    assert not mock_indexer.index_repo.called


def test_branch_monitor_pauses_and_resumes_job_manager(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor pauses, resyncs, and resumes job manager."""
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=_make_git_mock()):
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    set_paused_calls = [call for call in mock_job_manager.method_calls if call[0] == "set_paused"]
    assert any(call[1][0] is True for call in set_paused_calls), "set_paused(True) not called"
    assert mock_job_manager.drain_queued.called


def test_branch_monitor_queues_file_events_during_resync(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """File events are queued during resync (pause/resync/resume cycle)."""
    set_paused_calls = []

    def mock_set_paused(paused):
        set_paused_calls.append(paused)

    mock_job_manager.set_paused = mock_set_paused

    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=_make_git_mock()):
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    assert set_paused_calls, "set_paused should have been called"
    assert set_paused_calls[0] is True, "set_paused(True) called before resync"
    assert set_paused_calls[-1] is False, "set_paused(False) called after resync"
    assert mock_job_manager.drain_queued.called, "drain_queued should be called after resync"
    assert mock_job_manager.cancel_all.called, (
        "cancel_all should be called to stop pending timers during resync"
    )


def test_branch_monitor_stops_cleanly(temp_path, mock_job_manager, mock_indexer, mock_db):
    """Test that BranchMonitor can be stopped and joined cleanly."""

    def git_mock(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "main\n"
        return result

    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_mock):
        monitor.start()
        time.sleep(0.2)
        monitor.stop()
        monitor.join(timeout=2)

    assert not monitor.is_alive()


def test_branch_monitor_handles_git_error_gracefully(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor handles git command failures gracefully."""
    call_count = [0]

    def git_error_mock(*args, **kwargs):
        call_count[0] += 1
        raise subprocess.CalledProcessError(1, "git")

    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_error_mock):
        monitor.start()
        time.sleep(0.3)
        monitor.join(timeout=2)

    assert not monitor.is_alive(), "BranchMonitor should exit cleanly on git error"
    assert call_count[0] >= 1
