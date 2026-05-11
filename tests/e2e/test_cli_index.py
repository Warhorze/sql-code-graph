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


def test_index_schema_from_info_schema_exits_nonzero_on_missing_csv(tmp_path, monkeypatch):
    """--schema-from-info-schema exits non-zero when the CSV file does not exist."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(
        app,
        ["index", str(fixtures_path), "--schema-from-info-schema", str(tmp_path / "missing.csv")],
    )
    assert result.exit_code != 0


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
    # Check that the table name appears in the output
    assert "orders" in result.output.lower()


def test_mcp_setup_prints_valid_json(monkeypatch):
    """Test that mcp setup prints valid JSON."""
    result = runner.invoke(app, ["mcp", "setup"])
    assert result.exit_code == 0

    # Try to parse the output as JSON
    import json

    json.loads(result.output)  # Should not raise


def test_db_reset_repo_isolates(tmp_path, monkeypatch):
    """Test that db reset --repo isolates one repo and preserves others."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Init database
    runner.invoke(app, ["db", "init"])

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    # Index the same fixtures twice (simulating two repos)
    # Note: in practice, you'd index two different repos, but we'll use the same fixtures
    # with different paths to simulate two repos
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()

    # Copy fixtures to both paths
    import shutil

    for sql_file in fixtures_path.glob("*.sql"):
        shutil.copy(sql_file, repo1 / sql_file.name)
        shutil.copy(sql_file, repo2 / sql_file.name)

    # Index both repos
    result1 = runner.invoke(app, ["index", str(repo1)])
    assert result1.exit_code == 0
    result2 = runner.invoke(app, ["index", str(repo2)])
    assert result2.exit_code == 0

    # Check that both repos are indexed
    info_before = runner.invoke(app, ["db", "info"])
    assert "Repo:" in info_before.output or "Repo" in info_before.output

    # Reset one repo
    reset_result = runner.invoke(app, ["db", "reset", "--repo", str(repo1)])
    assert reset_result.exit_code == 0
    assert "Reset repo" in reset_result.output

    # Verify the other repo's nodes still exist by finding tables
    # (The second repo should still have indexed tables)
    find_result = runner.invoke(app, ["find", "table", "orders"])
    assert find_result.exit_code == 0
