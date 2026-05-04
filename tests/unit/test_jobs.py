"""Unit tests for WatchJobManager."""

import time
from unittest.mock import MagicMock

import pytest

from sqlcg.core.jobs import WatchJobManager


@pytest.fixture
def mock_indexer():
    """Create a mock Indexer."""
    return MagicMock()


@pytest.fixture
def mock_db():
    """Create a mock GraphBackend."""
    return MagicMock()


def test_schedule_creates_timer(mock_indexer, mock_db):
    """Test that schedule creates a timer."""
    timer_count = [0]

    def counting_timer(delay, callback, args=None):
        """Timer that just counts creation."""
        timer_count[0] += 1

        class MockTimer:
            def start(self):
                pass

            def cancel(self):
                pass

        return MockTimer()

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, _timer_factory=counting_timer)
    manager.schedule("test.sql")

    # Should have created 1 timer
    assert timer_count[0] == 1


def test_schedule_cancels_previous_timer(mock_indexer, mock_db):
    """Test that rescheduling cancels the previous timer."""
    cancel_count = [0]

    def counting_timer(delay, callback, args=None):
        """Timer that counts cancellations."""

        class MockTimer:
            def start(self):
                pass

            def cancel(self):
                cancel_count[0] += 1

        return MockTimer()

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, _timer_factory=counting_timer)

    # Schedule the same file twice
    manager.schedule("test.sql")
    manager.schedule("test.sql")

    # The first timer should have been cancelled
    assert cancel_count[0] == 1


def test_different_files_get_different_timers(mock_indexer, mock_db):
    """Test that different files create separate timers."""
    paths_seen = []

    def path_tracking_timer(delay, callback, args=None):
        """Timer that tracks which paths are passed."""
        if args:
            paths_seen.append(args[0])

        class MockTimer:
            def start(self):
                pass

            def cancel(self):
                pass

        return MockTimer()

    manager = WatchJobManager(
        mock_indexer, mock_db, dialect=None, _timer_factory=path_tracking_timer
    )

    manager.schedule("file1.sql")
    manager.schedule("file2.sql")

    # Should have timers for both files
    assert len(manager._timers) == 2
    assert "file1.sql" in manager._timers
    assert "file2.sql" in manager._timers


def test_timers_dict_tracks_files(mock_indexer, mock_db):
    """Test that _timers dict correctly tracks scheduled files."""

    def dummy_timer(delay, callback, args=None):
        class MockTimer:
            def start(self):
                pass

            def cancel(self):
                pass

        return MockTimer()

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, _timer_factory=dummy_timer)

    manager.schedule("file1.sql")
    manager.schedule("file2.sql")

    # Should have entries for both files
    assert "file1.sql" in manager._timers
    assert "file2.sql" in manager._timers

    # Cancel one file
    list(manager._timers.values())[0].cancel()

    # Should still have both entries until they're removed by _run_job
    assert len(manager._timers) == 2


def test_cancel_all_clears_timers(mock_indexer, mock_db):
    """Test that cancel_all removes all timers."""

    def dummy_timer(delay, callback, args=None):
        class MockTimer:
            def start(self):
                pass

            def cancel(self):
                pass

        return MockTimer()

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, _timer_factory=dummy_timer)

    manager.schedule("file1.sql")
    manager.schedule("file2.sql")

    assert len(manager._timers) == 2

    manager.cancel_all()

    assert len(manager._timers) == 0


def test_rapid_saves_result_in_one_reindex(mock_indexer, mock_db):
    """Test that rapid saves to same file result in exactly one reindex call."""
    # Use real threading.Timer with very short delay (10ms)
    # This tests the real debouncing behavior

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, debounce_seconds=0.01)

    # Schedule the same file twice rapidly
    manager.schedule("test.sql")
    manager.schedule("test.sql")

    # Wait for timers to fire (should be ~10ms + some buffer)
    time.sleep(0.05)

    # Should have called reindex_file exactly once
    # (second schedule cancelled first timer, only second fires)
    assert mock_indexer.reindex_file.call_count == 1


def test_different_files_both_reindexed(mock_indexer, mock_db):
    """Test that scheduling different files results in two reindex calls."""
    # Use real threading.Timer with very short delay
    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, debounce_seconds=0.01)

    # Schedule two different files
    manager.schedule("file_a.sql")
    manager.schedule("file_b.sql")

    # Wait for timers to fire
    time.sleep(0.05)

    # Both timers should fire, so reindex_file should be called twice
    assert mock_indexer.reindex_file.call_count == 2

    # Verify the calls were made with correct file paths
    calls = mock_indexer.reindex_file.call_args_list
    called_files = {str(call[0][0]) for call in calls}
    assert called_files == {"file_a.sql", "file_b.sql"}


def test_run_job_exception_cleans_up_timer(mock_indexer, mock_db):
    """Test that exception in _run_job cleans up the timer entry."""
    # Make reindex_file raise an exception
    mock_indexer.reindex_file.side_effect = RuntimeError("Test error")

    manager = WatchJobManager(mock_indexer, mock_db, dialect=None, debounce_seconds=0.01)

    # Schedule a file
    manager.schedule("test.sql")

    # Timer should be in _timers before it fires
    assert "test.sql" in manager._timers

    # Wait for timer to fire (will call _run_job which raises and then cleans up)
    time.sleep(0.05)

    # Timer should have been cleaned up from _timers dict despite the exception
    assert len(manager._timers) == 0
