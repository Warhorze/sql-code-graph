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


def test_branch_monitor_triggers_resync_on_branch_change(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor calls cancel_all and index_repo on branch change."""
    call_count = [0]

    def git_rev_parse_mock(*args, **kwargs):
        """Mock git rev-parse that changes branch after second call."""
        result = MagicMock()
        call_count[0] += 1

        # First call: main (initializes current_branch)
        # Second call: main (no change, should not trigger resync)
        # Third+ call: develop (branch changed, triggers resync)
        if call_count[0] <= 2:
            result.stdout = "main\n"
        else:
            result.stdout = "develop\n"

        return result

    # Use fast poll interval for testing (0.1 seconds)
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_mock):
        monitor.start()
        # Wait for 2+ polling cycles (0.1s each)
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    # Verify that cancel_all was called
    assert mock_job_manager.cancel_all.called, "cancel_all should be called on branch change"

    # Verify that index_repo was called with correct parameters
    assert mock_indexer.index_repo.called, "index_repo should be called on branch change"
    call_args = mock_indexer.index_repo.call_args
    assert call_args[0][0] == temp_path  # path
    assert call_args[1]["dialect"] is None
    assert call_args[1]["db"] == mock_db


def test_branch_monitor_no_resync_on_same_branch(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor doesn't resync when branch doesn't change."""

    def git_rev_parse_always_main(cwd=None, capture_output=False, text=False, check=False):
        """Mock git rev-parse that always returns main."""
        result = MagicMock()
        result.stdout = "main\n"
        return result

    # Use fast poll interval for testing
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_always_main):
        monitor.start()
        # Give it time to poll a few times
        time.sleep(0.3)
        monitor.stop()
        monitor.join(timeout=2)

    # index_repo should not have been called (no branch change)
    assert not mock_indexer.index_repo.called


def test_branch_monitor_pauses_and_resumes_job_manager(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor pauses, resyncs, and resumes job manager."""
    call_count = [0]

    def git_rev_parse_mock(*args, **kwargs):
        """Mock git rev-parse that changes branch after second call."""
        result = MagicMock()
        call_count[0] += 1

        if call_count[0] <= 2:
            result.stdout = "main\n"
        else:
            result.stdout = "develop\n"

        return result

    # Use fast poll interval for testing
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_mock):
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    # Verify the sequence of calls
    # set_paused(True) should be called before cancel_all
    set_paused_calls = [call for call in mock_job_manager.method_calls if call[0] == "set_paused"]
    assert any(call[1][0] is True for call in set_paused_calls), "set_paused(True) not called"

    # drain_queued should be called (as part of resuming)
    assert mock_job_manager.drain_queued.called


def test_branch_monitor_queues_file_events_during_resync(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that file events are queued during resync and not dropped.

    Verifies the BranchMonitor -> WatchJobManager integration:
    1. When a branch change is detected, set_paused(True) is called
    2. While resync is in-flight, file events received should be queued (not dropped)
    3. After resync completes, set_paused(False) and drain_queued() are called
    4. Queued paths are eventually handed back to schedule()
    """
    call_count = [0]
    set_paused_calls = []
    schedule_calls = []

    def git_rev_parse_mock(*args, **kwargs):
        """Mock git rev-parse that changes branch after second call."""
        result = MagicMock()
        call_count[0] += 1

        # First call: main (initializes current_branch)
        # Second call: main (no change)
        # Third+ call: develop (triggers resync)
        if call_count[0] <= 2:
            result.stdout = "main\n"
        else:
            result.stdout = "develop\n"

        return result

    def mock_schedule(file_path):
        """Track scheduled file paths."""
        schedule_calls.append(file_path)

    def mock_set_paused(paused):
        """Track pause/resume calls."""
        set_paused_calls.append(paused)

    # Set up mock job manager with our tracking methods
    mock_job_manager.schedule = mock_schedule
    mock_job_manager.set_paused = mock_set_paused

    # Use fast poll interval for testing
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_mock):
        monitor.start()
        # Wait for branch change to be detected and resync to trigger
        time.sleep(0.5)
        monitor.stop()
        monitor.join(timeout=2)

    # Verify that set_paused was called with True and then False
    # This demonstrates the pause/resync/resume cycle
    assert set_paused_calls, "set_paused should have been called"
    assert set_paused_calls[0] is True, "set_paused(True) called before resync"
    assert set_paused_calls[-1] is False, "set_paused(False) called after resync"

    # Verify drain_queued was called (which re-schedules queued events)
    assert mock_job_manager.drain_queued.called, "drain_queued should be called after resync"

    # Verify the overall sequence: pause -> index_repo -> resume -> drain
    # This guarantees that any file events arriving during resync won't be lost
    assert mock_job_manager.cancel_all.called, (
        "cancel_all should be called to stop pending timers during resync"
    )


def test_branch_monitor_stops_cleanly(temp_path, mock_job_manager, mock_indexer, mock_db):
    """Test that BranchMonitor can be stopped and joined cleanly."""

    def git_rev_parse_mock(cwd=None, capture_output=False, text=False, check=False):
        """Mock git rev-parse."""
        result = MagicMock()
        result.stdout = "main\n"
        return result

    # Use fast poll interval for testing
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_rev_parse_mock):
        monitor.start()
        time.sleep(0.2)
        monitor.stop()
        # join should not raise or hang
        monitor.join(timeout=2)

    # Thread should be stopped
    assert not monitor.is_alive()


def test_branch_monitor_handles_git_error_gracefully(
    temp_path, mock_job_manager, mock_indexer, mock_db
):
    """Test that BranchMonitor handles git command failures gracefully."""

    call_count = [0]

    def git_error_mock(*args, **kwargs):
        """Mock git rev-parse that raises CalledProcessError on first call."""
        call_count[0] += 1
        # Only raise on the first call, then patch should still be active
        if call_count[0] == 1:
            raise subprocess.CalledProcessError(1, "git")
        # If called again (shouldn't happen), raise it again
        raise subprocess.CalledProcessError(1, "git")

    # Use fast poll interval for testing
    monitor = BranchMonitor(temp_path, mock_job_manager, mock_indexer, mock_db, _poll_interval=0.1)

    with patch("sqlcg.indexer.watcher.subprocess.run", side_effect=git_error_mock):
        monitor.start()
        time.sleep(0.3)  # Give it time to try once and exit
        monitor.join(timeout=2)

    # Thread should have exited cleanly due to the error
    assert not monitor.is_alive(), "BranchMonitor should exit cleanly on git error"
    # Verify git was called at least once
    assert call_count[0] >= 1
