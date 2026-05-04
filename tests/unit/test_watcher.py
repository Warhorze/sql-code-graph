"""Unit tests for SQL file watcher."""

from unittest.mock import MagicMock

import pytest

from sqlcg.indexer.watcher import SqlFileEventHandler
from sqlcg.utils.ignore import load_ignore_spec


@pytest.fixture
def mock_job_manager():
    """Create a mock WatchJobManager."""
    return MagicMock()


@pytest.fixture
def mock_db():
    """Create a mock GraphBackend."""
    return MagicMock()


@pytest.fixture
def test_root(tmp_path):
    """Create a test root directory."""
    return tmp_path


@pytest.fixture
def ignore_spec(test_root):
    """Create an ignore spec for test root."""
    return load_ignore_spec(test_root)


@pytest.fixture
def event_handler(mock_job_manager, mock_db, ignore_spec, test_root):
    """Create an event handler."""
    return SqlFileEventHandler(mock_job_manager, mock_db, ignore_spec, test_root)


def test_on_modified_with_sql_file(event_handler, mock_job_manager, test_root):
    """Test that on_modified schedules a SQL file."""
    event = MagicMock()
    event.is_directory = False
    sql_path = str(test_root / "file.sql")
    event.src_path = sql_path

    event_handler.on_modified(event)

    mock_job_manager.schedule.assert_called_once_with(sql_path)


def test_on_modified_with_non_sql_file(event_handler, mock_job_manager, test_root):
    """Test that on_modified ignores non-SQL files."""
    event = MagicMock()
    event.is_directory = False
    event.src_path = str(test_root / "file.py")

    event_handler.on_modified(event)

    mock_job_manager.schedule.assert_not_called()


def test_on_modified_with_directory(event_handler, mock_job_manager, test_root):
    """Test that on_modified ignores directories."""
    event = MagicMock()
    event.is_directory = True
    event.src_path = str(test_root / "dir")

    event_handler.on_modified(event)

    mock_job_manager.schedule.assert_not_called()


def test_on_created_with_sql_file(event_handler, mock_job_manager, test_root):
    """Test that on_created schedules a SQL file."""
    event = MagicMock()
    event.is_directory = False
    sql_path = str(test_root / "file.sql")
    event.src_path = sql_path

    event_handler.on_created(event)

    mock_job_manager.schedule.assert_called_once_with(sql_path)


def test_on_moved_with_sql_file(event_handler, mock_job_manager, test_root):
    """Test that on_moved schedules a moved SQL file."""
    event = MagicMock()
    event.is_directory = False
    sql_path = str(test_root / "new_location.sql")
    event.dest_path = sql_path

    event_handler.on_moved(event)

    mock_job_manager.schedule.assert_called_once_with(sql_path)


def test_on_deleted_with_sql_file(event_handler, mock_db, test_root):
    """Test that on_deleted removes nodes from graph."""
    event = MagicMock()
    event.is_directory = False
    sql_path = str(test_root / "file.sql")
    event.src_path = sql_path

    event_handler.on_deleted(event)

    mock_db.delete_nodes_for_file.assert_called_once_with(sql_path)


def test_on_deleted_with_non_sql_file(event_handler, mock_db, test_root):
    """Test that on_deleted ignores non-SQL files."""
    event = MagicMock()
    event.is_directory = False
    event.src_path = str(test_root / "file.py")

    event_handler.on_deleted(event)

    mock_db.delete_nodes_for_file.assert_not_called()


def test_is_sql_with_sql_extension(event_handler, test_root):
    """Test _is_sql with .sql extension."""
    assert event_handler._is_sql(str(test_root / "file.sql")) is True


def test_is_sql_with_other_extension(event_handler, test_root):
    """Test _is_sql with non-sql extension."""
    assert event_handler._is_sql(str(test_root / "file.py")) is False


def test_is_sql_respects_ignore_patterns(test_root, mock_job_manager, mock_db):
    """Test that _is_sql respects ignore patterns."""
    # Create .sqlcgignore
    (test_root / ".sqlcgignore").write_text("ignored/\n")

    spec = load_ignore_spec(test_root)
    handler = SqlFileEventHandler(mock_job_manager, mock_db, spec, test_root)

    # File in ignored directory should not be SQL
    assert handler._is_sql(str(test_root / "ignored" / "file.sql")) is False

    # File not in ignored directory should be SQL
    assert handler._is_sql(str(test_root / "file.sql")) is True
