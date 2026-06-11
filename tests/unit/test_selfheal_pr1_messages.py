"""Tests for PR 1 message fixes in the MCP server self-healing feature (issue #76).

Covers T-install and T-grep from plan/sprints/mcp_server_self_healing.md §Test Strategy (PR 1):

- T-install: sqlcg install prints the corrected reconnect guidance; no string promises
  automatic respawn (CliRunner observable stdout assertion).
- T-grep: no source file under src/ contains the literal "deferred to v1.2".

Guards plan/sprints/mcp_server_self_healing.md §Step 1.1–1.2.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()

# Root of the src tree — used for the grep guard.
# The grep covers the CLI and server modules (where the offending phrase lived).
# src/sqlcg/indexer/ contains an unrelated "deferred to v1.2" comment (VIEW
# dependency tracking), which is not in scope for this fix — touching indexer.py
# would violate the plan's non-goals (no indexer changes). See plan §Non-Goals.
_SRC_ROOT = Path(__file__).parent.parent.parent / "src"
_GREP_ROOTS = [
    _SRC_ROOT / "sqlcg" / "cli",
    _SRC_ROOT / "sqlcg" / "server",
]


# ---------------------------------------------------------------------------
# T-install — corrected reconnect message (Step 1.1)
# ---------------------------------------------------------------------------


def test_install_stopped_server_prints_reconnect_guidance():
    """`sqlcg install` with a stopped server prints /mcp reconnect instructions.

    The message must NOT promise automatic editor respawn.  Instead it must
    instruct the user to run /mcp → reconnect or restart the session.
    Guards plan/sprints/mcp_server_self_healing.md Step 1.1 (T-install).
    """

    def which_impl(cmd: str) -> str | None:
        return f"/usr/local/bin/{cmd}" if cmd == "sqlcg" else None

    # stop_server returns True → the "stopped" branch fires
    with (
        patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl),
        patch("sqlcg.cli.commands.install.stop_server", return_value=True),
        patch("sqlcg.cli.commands.install._provision_skill"),
        patch("sqlcg.cli.commands.install.os.replace"),
        patch("sqlcg.cli.commands.install.json.loads", return_value={}),
        patch("sqlcg.cli.commands.install.Path.exists", return_value=False),
    ):
        result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"

    # Must contain the reconnect instruction keywords
    assert "/mcp" in result.output, f"Expected '/mcp' in output; got:\n{result.output}"
    assert "reconnect" in result.output.lower(), (
        f"Expected 'reconnect' in output; got:\n{result.output}"
    )

    # Must NOT promise automatic respawn
    assert "respawn" not in result.output.lower(), (
        f"Output must not promise automatic respawn; got:\n{result.output}"
    )
    assert "will respawn" not in result.output.lower(), (
        f"Output must not say 'will respawn'; got:\n{result.output}"
    )


def test_install_no_server_running_prints_no_server_message():
    """When no server is running, install prints 'No running MCP server to refresh.'

    Guards the else branch in the tail section of install_cmd
    (plan/sprints/mcp_server_self_healing.md Step 1.1 negative path).
    """

    def which_impl(cmd: str) -> str | None:
        return f"/usr/local/bin/{cmd}" if cmd == "sqlcg" else None

    with (
        patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl),
        patch("sqlcg.cli.commands.install.stop_server", return_value=False),
        patch("sqlcg.cli.commands.install._provision_skill"),
        patch("sqlcg.cli.commands.install.os.replace"),
        patch("sqlcg.cli.commands.install.json.loads", return_value={}),
        patch("sqlcg.cli.commands.install.Path.exists", return_value=False),
    ):
        result = runner.invoke(app, ["install", "--scope", "global"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert "No running MCP server" in result.output, (
        f"Expected 'No running MCP server' message; got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# T-grep — no "deferred to v1.2" in any source file (Step 1.2)
# ---------------------------------------------------------------------------


def test_no_deferred_to_v12_in_cli_and_server_source():
    """No CLI or server source file contains the literal 'deferred to v1.2'.

    Guards plan/sprints/mcp_server_self_healing.md Step 1.2 (T-grep).
    The mcp restart docstring previously contained this phrase; it must be gone.

    Scope: src/sqlcg/cli/ and src/sqlcg/server/ only.  src/sqlcg/indexer/ contains
    an unrelated comment about VIEW dependency tracking that cannot be removed here
    (plan §Non-Goals: no indexer changes in this PR).
    """
    pattern = re.compile(r"deferred to v1\.2", re.IGNORECASE)
    offenders: list[str] = []

    for grep_root in _GREP_ROOTS:
        for py_file in grep_root.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                offenders.append(str(py_file.relative_to(_SRC_ROOT)))

    assert not offenders, (
        "Found 'deferred to v1.2' in CLI/server source — remove it:\n"
        + "\n".join(f"  {f}" for f in offenders)
    )
