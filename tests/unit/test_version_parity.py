"""Single-source-of-truth version parity test (version-parity-and-restart, Phase 6).

Asserts that every runtime surface reports `sqlcg.__version__` — never a
hardcoded or independently-derived string — and that `__version__` matches
the installed distribution metadata. A red here means a surface drifted from
`sqlcg.__version__` (the single source of truth) or `pyproject.toml`/
`__init__.py`/the distribution metadata fell out of lockstep.

Surfaces covered:
    1. `sqlcg.__version__ == importlib.metadata.version("sql-code-graph")`
    2. CLI `--version` (Typer CliRunner, observable stdout)
    3. MCP protocol `serverInfo.version` — pinned via `mcp._mcp_server.version`
       (the documented private-attr write; FastMCP has no `version` kwarg)
    4. `db_info().sqlcg_version` (focused unit, mocked backend — mirrors
       tests/unit/test_tools_warnings.py)
    5. Control-socket `status.version` — see TestMcpStatusVersionDrift in
       tests/unit/test_mcp_control.py (homed there per the plan; not duplicated
       here to avoid maintaining two fake-socket harnesses).
"""

import importlib.metadata
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg import __version__
from sqlcg.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. __version__ matches installed distribution metadata
# ---------------------------------------------------------------------------


def test_version_matches_distribution_metadata():
    """sqlcg.__version__ equals importlib.metadata.version("sql-code-graph").

    Catches a future pyproject.toml/__init__.py drift even if commitizen's
    version_files sync is bypassed by hand-editing one file.
    """
    assert __version__ == importlib.metadata.version("sql-code-graph")


# ---------------------------------------------------------------------------
# 2. CLI --version (eager root callback)
# ---------------------------------------------------------------------------


def test_cli_version_flag_prints_version_and_exits_zero():
    """`sqlcg --version` prints `sqlcg version <__version__>` and exits 0."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert __version__ in result.output
    assert f"sqlcg version {__version__}" in result.output


def test_cli_version_subcommand_matches_flag():
    """The `version` subcommand and the `--version` flag print the same string."""
    flag_result = runner.invoke(app, ["--version"])
    subcommand_result = runner.invoke(app, ["version"])

    assert flag_result.output.strip() == subcommand_result.output.strip()
    assert f"sqlcg version {__version__}" in subcommand_result.output


def test_cli_no_args_still_shows_help_not_error():
    """`sqlcg` with no args still shows help (not "Missing command")."""
    result = runner.invoke(app, [])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert "Missing command" not in result.output
    assert "Usage:" in result.output


# ---------------------------------------------------------------------------
# 3. MCP protocol serverInfo.version — private-attr pin
# ---------------------------------------------------------------------------


def test_mcp_server_version_attr_matches_dunder_version():
    """mcp._mcp_server.version == sqlcg.__version__ (was None before this feature).

    FastMCP.__init__ does not accept a `version` kwarg; the underlying
    mcp.server.lowlevel.Server does. server.py sets it post-construction —
    this pins that the private-attr write survives, so an upstream FastMCP
    change that breaks it fails this test loudly instead of silently shipping
    serverInfo.version == None again.
    """
    from sqlcg.server.server import mcp

    assert mcp._mcp_server.version == __version__
    assert mcp._mcp_server.version is not None


# ---------------------------------------------------------------------------
# 4. db_info().sqlcg_version
# ---------------------------------------------------------------------------


def _mock_backend_for_db_info() -> MagicMock:
    """A minimal mocked backend producing a healthy db_info() result.

    Mirrors the pattern in tests/unit/test_tools_warnings.py.
    """
    mock_backend = MagicMock()
    mock_backend.get_schema_version.return_value = "1"
    mock_backend.get_indexed_sha.return_value = None

    def run_read_side_effect(query, params):
        if "COLUMN_LINEAGE" in query:
            return [{"count": 5}]
        elif "parsing_mode" in query:
            return [{"mode": "sqlglot", "cnt": 10}]
        elif "SqlColumn" in query:
            return [{"count": 50}]
        elif "SqlQuery" in query:
            return [{"count": 10}]
        elif "SqlFile" in query:
            return [{"count": 5}]
        elif "SqlTable" in query:
            return [{"count": 20}]
        elif "Repo" in query:
            return [{"count": 1}]
        return [{"count": 0}]

    mock_backend.run_read.side_effect = run_read_side_effect
    return mock_backend


def test_db_info_sqlcg_version_matches_dunder_version():
    """db_info().sqlcg_version == sqlcg.__version__; schema_version stays distinct.

    sqlcg_version (package build) and schema_version (graph schema) are
    different axes — this asserts both are present and not conflated.
    """
    with patch("sqlcg.server.tools._get_backend") as mock_get_backend_func:
        mock_get_backend_func.return_value = _mock_backend_for_db_info()

        from sqlcg.server.tools import db_info

        result = db_info()

    assert result.sqlcg_version == __version__
    assert result.schema_version == "1"
    assert result.sqlcg_version != result.schema_version
