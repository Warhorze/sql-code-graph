"""Unit tests for configuration helpers."""

import tempfile
from pathlib import Path

import pytest

from sqlcg.core.config import get_dialect


@pytest.fixture
def temp_dir():
    """Create a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_get_dialect_returns_snowflake_by_default(temp_dir):
    """Test that get_dialect falls back to snowflake when no config file exists."""
    dialect = get_dialect(temp_dir)
    assert dialect == "snowflake"


def test_get_dialect_reads_from_sqlcg_toml(temp_dir):
    """Test that get_dialect reads dialect from .sqlcg.toml."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[sqlcg]\ndialect = "bigquery"\n')

    dialect = get_dialect(temp_dir)
    assert dialect == "bigquery"


def test_get_dialect_handles_postgres(temp_dir):
    """Test that get_dialect correctly reads postgres dialect."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[sqlcg]\ndialect = "postgres"\n')

    dialect = get_dialect(temp_dir)
    assert dialect == "postgres"


def test_get_dialect_ignores_invalid_toml(temp_dir):
    """Test that get_dialect falls back to snowflake on invalid TOML."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text("invalid toml syntax {")

    dialect = get_dialect(temp_dir)
    assert dialect == "snowflake"


def test_get_dialect_ignores_missing_sqlcg_section(temp_dir):
    """Test that get_dialect falls back to snowflake if [sqlcg] section missing."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[other]\nkey = "value"\n')

    dialect = get_dialect(temp_dir)
    assert dialect == "snowflake"


def test_get_dialect_ignores_missing_dialect_key(temp_dir):
    """Test that get_dialect falls back to snowflake if dialect key missing."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[sqlcg]\nother_key = "value"\n')

    dialect = get_dialect(temp_dir)
    assert dialect == "snowflake"


def test_get_dialect_with_path_object(temp_dir):
    """Test that get_dialect works with Path objects."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[sqlcg]\ndialect = "mysql"\n')

    # Pass as Path object
    dialect = get_dialect(temp_dir)
    assert dialect == "mysql"


def test_get_dialect_with_string_path(temp_dir):
    """Test that get_dialect works with string paths."""
    config_file = temp_dir / ".sqlcg.toml"
    config_file.write_text('[sqlcg]\ndialect = "tsql"\n')

    # Pass as string
    dialect = get_dialect(str(temp_dir))
    assert dialect == "tsql"
