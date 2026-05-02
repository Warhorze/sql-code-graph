"""Unit tests for WatchJobManager."""

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
