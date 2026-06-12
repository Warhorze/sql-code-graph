"""E2E test for `sqlcg analyze empty-impact` CLI command.

Runs the command against a small indexed fixture repo and verifies that
both views are printed with the required caveat label and that the
value full/partial marker is shown.

Guards plan/sprints/unfilled_table_impact.md PR 1 Test Strategy (E2E).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixture corpus — schema-qualified INSERT with direct join (gating case)
# and a value-derived column so both views have non-empty output.
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE da.src (k INT, a INT);
CREATE TABLE da.other (k INT, b INT);
CREATE TABLE da.dst (a INT, b INT);
"""

_ETL_SQL = """\
INSERT INTO da.dst (a, b)
SELECT da_src.a AS a, da_other.b AS b
FROM da.src AS da_src
JOIN da.other AS da_other ON da_src.k = da_other.k;
"""


@pytest.fixture
def indexed_dir(tmp_path):
    """Index the two-table fixture and return (db_path, tmp_path)."""
    (tmp_path / "ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "etl.sql").write_text(_ETL_SQL)

    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    backend.close()
    return db_path, tmp_path


def test_empty_impact_cli_prints_both_views(indexed_dir):
    """'sqlcg analyze empty-impact da.src' prints View 2 (PRIMARY) and View 1 (SUPPLEMENT).

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 3.1 acceptance +
    E2E test requirement.
    """
    db_path, tmp_path = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src"],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )
    output = result.output
    assert result.exit_code == 0, f"Non-zero exit code. Output:\n{output}"
    # View 2 label.
    assert "View 2" in output, f"Missing 'View 2' in output:\n{output}"
    # View 1 label.
    assert "View 1" in output, f"Missing 'View 1' in output:\n{output}"


def test_empty_impact_cli_row_caveat_label_present(indexed_dir):
    """The View 1 section carries the 'known gap' and 'depends on join type' caveats.

    Rich may wrap long lines; we check each word token present in the output rather
    than exact substring to avoid false failures due to terminal-width line-wrapping.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Acceptance Criteria (string assertions).
    """
    db_path, _ = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src"],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )
    # Strip whitespace collapses newlines from Rich line-wrapping.
    output_flat = " ".join(result.output.split())
    assert "known gap" in output_flat, f"Missing 'known gap' caveat in output:\n{result.output}"
    assert "depends on join type" in output_flat, (
        f"Missing 'depends on join type' caveat in output:\n{result.output}"
    )


def test_empty_impact_cli_value_full_partial_marker(indexed_dir):
    """The value table shows a 'full' or 'partial' marker for each table.

    Guards plan/sprints/unfilled_table_impact.md PR 1 Step 3.1 acceptance.
    """
    db_path, _ = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src"],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )
    output = result.output
    assert result.exit_code == 0
    # The value table renders 'full' or 'partial' in the third column.
    assert "full" in output or "partial" in output, (
        f"Expected 'full' or 'partial' marker in output:\n{output}"
    )


def test_empty_impact_cli_max_depth_zero_exits_nonzero(indexed_dir):
    """--max-depth 0 exits non-zero with the documented error message.

    Guards plan/sprints/unfilled_table_impact.md PR 1 N2.
    """
    db_path, _ = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src", "--max-depth", "0"],
        env={"SQLCG_DB_PATH": db_path},
    )
    assert result.exit_code != 0
    assert "--max-depth must be between 1 and 100" in result.output


def test_empty_impact_cli_max_depth_over_100_exits_nonzero(indexed_dir):
    """--max-depth 101 exits non-zero with the documented error message.

    Guards plan/sprints/unfilled_table_impact.md PR 1 N2.
    """
    db_path, _ = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src", "--max-depth", "101"],
        env={"SQLCG_DB_PATH": db_path},
    )
    assert result.exit_code != 0
    assert "--max-depth must be between 1 and 100" in result.output


def test_empty_impact_cli_max_depth_valid_exits_zero(indexed_dir):
    """--max-depth 5 (valid) exits 0 and shows output.

    Guards plan/sprints/unfilled_table_impact.md PR 1 N2.
    """
    db_path, _ = indexed_dir
    result = runner.invoke(
        app,
        ["analyze", "empty-impact", "da.src", "--max-depth", "5"],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"Non-zero exit. Output:\n{result.output}"
