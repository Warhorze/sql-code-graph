"""End-to-end CLI tests for parse-diagnostics features.

Tests:
- `sqlcg analyze failures` lists failing files with cause; --cause filters.
- `sqlcg index --profile` prints timing table; without the flag it does not.
"""

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


@pytest.fixture
def db_with_failures(tmp_path, monkeypatch):
    """Index a corpus with one E5 file and one clean file; return tmp_path."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Init db
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, f"db init failed: {result.output}"

    # Write fixtures
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # Clean file: DDL + DML with fully resolved columns (no parse errors)
    (corpus / "clean.sql").write_text(
        "CREATE TABLE src (id INT, amount DECIMAL);\n"
        "CREATE TABLE dst (id INT, amount DECIMAL);\n"
        "INSERT INTO dst SELECT id, amount FROM src;\n"
    )
    # Failing file: unaliased aggregate triggers func_fallback (a degrading bucket)
    (corpus / "failing.sql").write_text("SELECT SUM(amount) FROM orders;")

    result = runner.invoke(app, ["index", str(corpus)])
    assert result.exit_code == 0, f"index failed: {result.output}"

    return tmp_path


def test_analyze_failures_lists_failing_file(db_with_failures, monkeypatch, tmp_path):
    """analyze failures should print at least one row with func_fallback cause.

    The path column may be truncated by Rich's table renderer when the terminal
    is narrow (Typer test runner has a fixed width), so we assert on the cause
    column (never truncated) and verify the path column header is present.
    """
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    result = runner.invoke(app, ["analyze", "failures"])
    assert result.exit_code == 0, f"analyze failures failed: {result.output}"

    output = result.output
    # The cause column is narrow and is never truncated
    assert "func_fallback" in output, (
        f"Expected 'func_fallback' cause in analyze failures output, got:\n{output}"
    )
    # The path column header must be present (even if cell values are truncated)
    assert "path" in output, (
        f"Expected 'path' column header in analyze failures output, got:\n{output}"
    )


def test_analyze_failures_clean_corpus_no_results(tmp_path, monkeypatch):
    """analyze failures on a clean corpus should print 'No results'."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "clean.db"))

    runner.invoke(app, ["db", "init"])

    corpus = tmp_path / "clean_corpus"
    corpus.mkdir()
    (corpus / "a.sql").write_text("CREATE TABLE orders (id INT, amount DECIMAL);")

    runner.invoke(app, ["index", str(corpus)])

    result = runner.invoke(app, ["analyze", "failures"])
    assert result.exit_code == 0, f"analyze failures failed: {result.output}"
    assert "No results" in result.output, (
        f"Expected 'No results' for clean corpus, got:\n{result.output}"
    )


def test_analyze_failures_cause_filter(db_with_failures, monkeypatch, tmp_path):
    """--cause func_fallback shows a row; --cause timeout returns nothing."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "test.db"))

    # Filter for func_fallback — should show the failing file's cause
    result_ff = runner.invoke(app, ["analyze", "failures", "--cause", "func_fallback"])
    assert result_ff.exit_code == 0, (
        f"analyze failures --cause func_fallback failed: {result_ff.output}"
    )
    assert "func_fallback" in result_ff.output, (
        f"Expected 'func_fallback' in --cause func_fallback output, got:\n{result_ff.output}"
    )

    # Filter for timeout — no timeouts in this corpus
    result_timeout = runner.invoke(app, ["analyze", "failures", "--cause", "timeout"])
    assert result_timeout.exit_code == 0, (
        f"analyze failures --cause timeout failed: {result_timeout.output}"
    )
    assert "No results" in result_timeout.output, (
        f"Expected 'No results' for --cause timeout, got:\n{result_timeout.output}"
    )


def test_index_profile_flag_prints_timing(tmp_path, monkeypatch):
    """sqlcg index --profile should print per-stage timing and ms/file."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "profile.db"))

    runner.invoke(app, ["db", "init"])

    corpus = tmp_path / "prof_corpus"
    corpus.mkdir()
    (corpus / "a.sql").write_text("SELECT id FROM orders;")

    result = runner.invoke(app, ["index", str(corpus), "--profile"])
    assert result.exit_code == 0, f"index --profile failed: {result.output}"

    output = result.output
    # Profile block header
    assert "Profile" in output, f"Expected 'Profile' section header in output, got:\n{output}"
    # Per-stage keys
    assert "pass1" in output, f"Expected 'pass1' timing line, got:\n{output}"
    assert "ms/file" in output, f"Expected 'ms/file' figure, got:\n{output}"


def test_index_without_profile_no_timing_table(tmp_path, monkeypatch):
    """sqlcg index without --profile should NOT print any timing table."""
    monkeypatch.setenv("SQLCG_DB_PATH", str(tmp_path / "noprofile.db"))

    runner.invoke(app, ["db", "init"])

    corpus = tmp_path / "noprof_corpus"
    corpus.mkdir()
    (corpus / "a.sql").write_text("SELECT id FROM orders;")

    result = runner.invoke(app, ["index", str(corpus)])
    assert result.exit_code == 0, f"index failed: {result.output}"

    output = result.output
    assert "pass1" not in output, f"Timing table must not appear without --profile, got:\n{output}"
    assert "ms/file" not in output, f"'ms/file' must not appear without --profile, got:\n{output}"
