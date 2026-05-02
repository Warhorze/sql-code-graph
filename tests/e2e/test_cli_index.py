"""End-to-end tests for CLI commands."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


def test_db_init_idempotent(tmp_path, monkeypatch):
    """Test that db init is idempotent."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # First init
    result1 = runner.invoke(app, ["db", "init"])
    assert result1.exit_code == 0
    assert "Database initialised" in result1.output

    # Second init (should not error)
    result2 = runner.invoke(app, ["db", "init"])
    assert result2.exit_code == 0
    assert "Database initialised" in result2.output


def test_index_synthetic_fixtures(tmp_path, monkeypatch):
    """Test indexing synthetic fixtures."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Init database
    runner.invoke(app, ["db", "init"])

    # Index fixtures
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    result = runner.invoke(app, ["index", str(fixtures_path)])
    assert result.exit_code == 0
    assert "Indexed" in result.output


def test_index_schema_from_info_schema_exits_nonzero(tmp_path, monkeypatch):
    """Test that --schema-from-info-schema exits with error."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    result = runner.invoke(
        app,
        ["index", str(fixtures_path), "--schema-from-info-schema", "x.csv"],
    )
    assert result.exit_code != 0
    assert "not yet implemented" in result.output.lower()


def test_find_table_after_index(tmp_path, monkeypatch):
    """Test finding tables after indexing."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Init and index
    runner.invoke(app, ["db", "init"])

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    runner.invoke(app, ["index", str(fixtures_path)])

    # Find a table that should exist in the fixtures
    # The synthetic fixtures include tables like "orders", "customers"
    result = runner.invoke(app, ["find", "table", "orders"])
    assert result.exit_code == 0


def test_mcp_setup_prints_valid_json(monkeypatch):
    """Test that mcp setup prints valid JSON."""
    result = runner.invoke(app, ["mcp", "setup"])
    assert result.exit_code == 0

    # Try to parse the output as JSON
    import json

    json.loads(result.output)  # Should not raise
