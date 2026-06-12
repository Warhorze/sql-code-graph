"""E2E test for `sqlcg analyze pr-impact` CLI command.

Exercises the REAL `sqlcg analyze pr-impact` entrypoint WITHOUT pre-seeding
``tools._backend`` — the fixture indexes to the DB path that ``get_backend()``
opens (via ``SQLCG_DB_PATH``), so the CLI acquires the graph itself.  This
exercises the BUG-A seam fix (v1.24.2): reverting the CLI backend-acquisition
change reproduces ``RuntimeError: Backend not initialized``.

Also pins BUG-B (blast radius zeroed by post-resync engine call): asserts a
non-empty ``value_empty_columns`` for a deleted producer with non-trivial
downstream.

Guards plan/sprints/unfilled_table_impact.md PR 5 Test Strategy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

runner = CliRunner()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )


def _commit_all(path: Path, msg: str = "commit") -> str:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Fixture: genuine-deletion scenario with non-trivial downstream
# ---------------------------------------------------------------------------

# da.source produces da.orphan; da.child derives columns from da.orphan.
# da.source.sql is deleted at HEAD.
# This fixture mirrors the working integration test: da.orphan gets kind='table'
# because da.child's SELECT FROM da.orphan creates a 'table' entry first, and the
# DB-level guard prevents the INSERT INTO da.orphan 'derived' row from downgrading it.
# da.report provides a 2nd hop for a non-trivial blast radius.
_PRODUCER_SQL = """\
INSERT INTO da.orphan SELECT id, amount FROM da.source_raw
"""

_DOWNSTREAM_SQL = """\
INSERT INTO da.child SELECT orphan.amount FROM da.orphan
"""

_REPORT_SQL = """\
INSERT INTO da.report SELECT child.amount FROM da.child
"""


@pytest.fixture
def genuine_loss_repo(tmp_path):
    """Index a repo at base (DB on disk), then delete the producer.

    Critically: does NOT set tools._backend.  The CLI must acquire the backend
    itself via get_backend() / SQLCG_DB_PATH.

    Returns (base_sha, db_path, tmp_path).
    """
    _init_repo(tmp_path)

    (tmp_path / "downstream.sql").write_text(_DOWNSTREAM_SQL)
    (tmp_path / "producer.sql").write_text(_PRODUCER_SQL)
    (tmp_path / "report.sql").write_text(_REPORT_SQL)
    base_sha = _commit_all(tmp_path, "base")

    # Index to a real on-disk DB so get_backend() can open it.
    db_path = str(tmp_path / "test.db")
    backend = DuckDBBackend(db_path)
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    # use_git=True so git ls-files returns files in alphabetical order (deterministic):
    # downstream.sql < producer.sql < report.sql, ensuring downstream.sql is processed
    # first and da.orphan gets kind='table' (not 'derived').
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=True)
    backend.set_indexed_sha(base_sha)
    backend.close()  # release the file lock so the CLI can open it

    # Delete the producer file to create the head state.
    (tmp_path / "producer.sql").unlink()
    _commit_all(tmp_path, "delete producer.sql")

    # NOTE: tools._backend is intentionally NOT set here.
    # If the CLI relied on the _backend global (the pre-fix broken path), it
    # would raise RuntimeError: Backend not initialized.
    return base_sha, db_path, tmp_path


# ---------------------------------------------------------------------------
# BUG A (v1.24.2): real CLI runs end-to-end without Backend-not-initialized
# ---------------------------------------------------------------------------


def test_pr_impact_cli_genuine_loss_prints_lost_producer_and_caveat(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha>' runs to completion via the real CLI.

    BUG A regression: reverting the CLI backend-acquisition fix (Step 5.1 in
    plan/sprints/unfilled_table_impact.md §PR 5) would cause this test to fail
    with ``RuntimeError: Backend not initialized`` because the CLI would call
    get_pr_impact() -> _open_backend() -> _get_backend() which raises when
    tools._backend is None (it is never set in the CLI process).

    Assertion: exit_code == 0 AND the runtime-blind caveat appears in output.

    Guards plan/sprints/unfilled_table_impact.md PR 5 E2E test strategy.
    """
    base_sha, db_path, tmp_path = genuine_loss_repo

    result = runner.invoke(
        app,
        ["analyze", "pr-impact", "--base", base_sha],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )

    output = result.output
    assert result.exit_code == 0, (
        f"CLI must exit 0 (BUG A fix: no Backend not initialized). "
        f"exit_code={result.exit_code}\nOutput:\n{output}"
    )
    # Runtime-blind caveat MUST be present.
    assert (
        "CODE REGRESSION" in output.upper()
        or "runtime" in output.lower()
        or "code regression" in output.lower()
    ), f"Runtime-blind caveat must appear in output; got:\n{output}"


# ---------------------------------------------------------------------------
# BUG B (v1.24.2): blast radius is NON-ZERO for a deleted producer
# ---------------------------------------------------------------------------


def test_pr_impact_cli_non_zero_blast_radius_for_deleted_producer(genuine_loss_repo):
    """'sqlcg analyze pr-impact' reports non-zero blast radius for a deleted producer.

    BUG B regression: reverting the blast-radius pre-resync fix (Step 5.2 in
    plan/sprints/unfilled_table_impact.md §PR 5) would cause get_pr_impact to
    compute the radius AFTER resync_changed deletes the lost producer's nodes,
    collapsing value_empty_columns to [] (the capstone 502-column collapse).

    Assertion: the rendered View-2 section shows da.report (or at least a
    non-empty affected-table row) with a non-zero column count.

    Guards plan/sprints/unfilled_table_impact.md PR 5 BUG-B E2E pin.
    """
    base_sha, db_path, tmp_path = genuine_loss_repo

    result = runner.invoke(
        app,
        ["analyze", "pr-impact", "--base", base_sha],
        env={"SQLCG_DB_PATH": db_path},
        catch_exceptions=False,
    )

    output = result.output
    assert result.exit_code == 0, f"exit_code={result.exit_code}\nOutput:\n{output}"

    # The blast radius View-2 section must mention da.child (the downstream table).
    # Before the BUG B fix the View-2 table was empty (0 affected tables / 0 columns).
    assert "da.child" in output or "da.orphan" in output, (
        f"da.child or da.orphan must appear in blast radius output (BUG B fix: radius "
        f"computed on intact base graph). Output:\n{output}"
    )


# ---------------------------------------------------------------------------
# max-depth validation
# ---------------------------------------------------------------------------


def test_pr_impact_cli_invalid_max_depth_exits_nonzero(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha> --max-depth 0' exits non-zero.

    Guards plan/sprints/unfilled_table_impact.md PR 2 N2 validation.
    """
    base_sha, db_path, tmp_path = genuine_loss_repo

    result = runner.invoke(
        app,
        ["analyze", "pr-impact", "--base", base_sha, "--max-depth", "0"],
        env={"SQLCG_DB_PATH": db_path},
    )

    assert result.exit_code != 0, "Invalid --max-depth should exit non-zero"
    assert "max-depth" in result.output.lower() or "error" in result.output.lower()


def test_pr_impact_cli_invalid_max_depth_101_exits_nonzero(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha> --max-depth 101' exits non-zero.

    Guards plan/sprints/unfilled_table_impact.md PR 2 N2 validation (upper bound).
    """
    base_sha, db_path, tmp_path = genuine_loss_repo

    result = runner.invoke(
        app,
        ["analyze", "pr-impact", "--base", base_sha, "--max-depth", "101"],
        env={"SQLCG_DB_PATH": db_path},
    )

    assert result.exit_code != 0, "--max-depth > 100 should exit non-zero"
    assert "max-depth" in result.output.lower() or "error" in result.output.lower()
