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


def test_index_with_batch_size_flag(tmp_path, monkeypatch):
    """Test that --batch-size flag is accepted and works."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Init database
    runner.invoke(app, ["db", "init"])

    fixtures_path = Path(__file__).parent.parent / "fixtures" / "synthetic"
    if not fixtures_path.exists():
        pytest.skip("Synthetic fixtures not found")

    # Test with default batch size
    result_default = runner.invoke(app, ["index", str(fixtures_path)])
    assert result_default.exit_code == 0
    assert "Indexed" in result_default.output

    # Reset and test with explicit batch size
    runner.invoke(app, ["db", "reset"])
    result_custom = runner.invoke(app, ["index", str(fixtures_path), "--batch-size", "7"])
    assert result_custom.exit_code == 0
    assert "Indexed" in result_custom.output


def test_v1_1_1_F2_index_relative_path_git_walk_no_crash(tmp_path, monkeypatch):
    """Regression test: `sqlcg index <relative-path>` must not raise ValueError.

    Bug: walker.py:_git_sql_files joins root / f where root is the relative input.
    ignore.py:is_ignored then does path.relative_to(root.resolve()) — path is
    relative, root is absolute → ValueError.

    This test MUST FAIL before F2 (walk_sql_files root.resolve() fix) is implemented.
    The test covers the git-walk branch by init-ing a git repo + `git add`.
    """
    import subprocess

    # Create a parent dir and a sub-repo
    parent = tmp_path / "parent"
    repo = parent / "repo"
    repo.mkdir(parents=True)

    (repo / "dim_customer.sql").write_text("CREATE TABLE dim_customer (id INT)")
    # Init git and stage the file so git ls-files returns it
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=False)
    subprocess.run(
        ["git", "add", "dim_customer.sql"], cwd=str(repo), capture_output=True, check=False
    )

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))
    runner.invoke(app, ["db", "init"])

    # Run `sqlcg index ./repo` from the parent directory (relative path)
    monkeypatch.chdir(parent)
    result = runner.invoke(app, ["index", "./repo"])

    assert result.exit_code == 0, (
        f"sqlcg index <relative-path> crashed with exit_code={result.exit_code}.\n"
        f"Output: {result.output}\n"
        "Expected exit_code=0 after F2 fix (resolve root once in walk_sql_files)."
    )
    assert "ValueError" not in (result.output or ""), (
        "ValueError appeared in output — the relative-path crash in ignore.py is not fixed."
    )
    # The indexed file's table should be queryable
    find_result = runner.invoke(app, ["find", "table", "dim_customer"])
    assert "dim_customer" in find_result.output.lower(), (
        "dim_customer table not found after indexing via relative path"
        " — index may have silently skipped the file."
    )


def test_v1_1_1_F2_index_relative_path_rglob_fallback_no_crash(tmp_path, monkeypatch):
    """Regression test: `sqlcg index <relative-path>` with non-git dir (rglob fallback).

    Sibling of the git-walk branch test above — confirms the rglob path is also safe
    (it was already fixed in #24 but we keep it here to prevent regression).
    """
    parent = tmp_path / "parent"
    non_git_repo = parent / "data"
    non_git_repo.mkdir(parents=True)

    (non_git_repo / "fact_orders.sql").write_text("CREATE TABLE fact_orders (id INT)")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))
    runner.invoke(app, ["db", "init"])

    monkeypatch.chdir(parent)
    result = runner.invoke(app, ["index", "./data"])

    assert result.exit_code == 0, (
        f"sqlcg index <relative-path> (rglob branch) crashed: exit={result.exit_code}\n"
        f"Output: {result.output}"
    )
    assert "ValueError" not in (result.output or ""), "ValueError appeared in rglob-branch output."
