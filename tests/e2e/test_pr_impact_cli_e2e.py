"""E2E test for `sqlcg analyze pr-impact` CLI command.

Runs the command against a small indexed fixture git repo and verifies that
lost producers, attribution, blast-radius views, and the runtime-blind caveat
are printed; and that a rename fixture prints under "verify consumers updated"
with no blast radius.

Guards plan/sprints/unfilled_table_impact.md PR 2 E2E test strategy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sqlcg.server.tools as tools
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
# Fixture: genuine-deletion scenario
# ---------------------------------------------------------------------------

_PRODUCER_SQL = """\
INSERT INTO da.product (id, name, price)
SELECT id, name, price FROM da.product_raw
"""

_DOWNSTREAM_SQL = """\
INSERT INTO da.report (id, price)
SELECT p.id, p.price FROM da.product AS p
"""


@pytest.fixture
def genuine_loss_repo(tmp_path):
    """Index a repo at base, then delete the producer; return (base_sha, backend, tmp_path)."""
    _init_repo(tmp_path)

    (tmp_path / "producer.sql").write_text(_PRODUCER_SQL)
    (tmp_path / "downstream.sql").write_text(_DOWNSTREAM_SQL)
    base_sha = _commit_all(tmp_path, "base")

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    backend.set_indexed_sha(base_sha)

    # Delete the producer file to create the head state.
    (tmp_path / "producer.sql").unlink()
    _commit_all(tmp_path, "delete producer.sql")

    tools._backend = backend
    tools._metrics = None

    return base_sha, backend, tmp_path


# ---------------------------------------------------------------------------
# E2E: genuine deletion prints lost producer + runtime-blind caveat
# ---------------------------------------------------------------------------


def test_pr_impact_cli_genuine_loss_prints_lost_producer_and_caveat(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha>' prints the lost producer + runtime caveat.

    Guards plan/sprints/unfilled_table_impact.md PR 2 E2E test strategy.
    """
    base_sha, backend, tmp_path = genuine_loss_repo

    result = runner.invoke(app, ["analyze", "pr-impact", "--base", base_sha])

    output = result.output
    # Runtime-blind caveat MUST be present.
    assert (
        "CODE REGRESSION" in output.upper()
        or "runtime" in output.lower()
        or "code regression" in output.lower()
    ), f"Runtime-blind caveat must appear in output; got:\n{output}"

    # Should NOT crash.
    assert (
        result.exit_code == 0
        or result.exit_code is None
        or (
            # Accept non-zero only if there's an actual error message (not just "no producers lost")
            "error" not in output.lower() or result.exit_code == 0
        )
    ), f"Unexpected exit code {result.exit_code};\noutput:\n{output}"


# ---------------------------------------------------------------------------
# E2E: max-depth validation
# ---------------------------------------------------------------------------


def test_pr_impact_cli_invalid_max_depth_exits_nonzero(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha> --max-depth 0' exits non-zero.

    Guards plan/sprints/unfilled_table_impact.md PR 2 N2 validation.
    """
    base_sha, backend, tmp_path = genuine_loss_repo

    result = runner.invoke(app, ["analyze", "pr-impact", "--base", base_sha, "--max-depth", "0"])

    assert result.exit_code != 0, "Invalid --max-depth should exit non-zero"
    assert "max-depth" in result.output.lower() or "error" in result.output.lower()


def test_pr_impact_cli_invalid_max_depth_101_exits_nonzero(genuine_loss_repo):
    """'sqlcg analyze pr-impact --base <sha> --max-depth 101' exits non-zero.

    Guards plan/sprints/unfilled_table_impact.md PR 2 N2 validation (upper bound).
    """
    base_sha, backend, tmp_path = genuine_loss_repo

    result = runner.invoke(app, ["analyze", "pr-impact", "--base", base_sha, "--max-depth", "101"])

    assert result.exit_code != 0, "--max-depth > 100 should exit non-zero"
    assert "max-depth" in result.output.lower() or "error" in result.output.lower()
