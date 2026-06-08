"""Failing acceptance tests for hygiene batch — config-path footgun warning + find column noise.

Tests cover:
  - config_file_present() helper (Phase 1, Step 1.1)
  - index_cmd soft warning when .sqlcg.toml is absent (Phase 1, Step 1.2)
  - --quiet suppresses the warning (Phase 1, Step 1.2)
  - index_cmd prints NO warning when .sqlcg.toml is present (Phase 1, Step 1.2)
  - find column NoiseFilter parity — noise column dropped by default (Phase 2, Step 2.1)
  - find column --raw keeps noise column (Phase 2, Step 2.1)
  - analyze failures --help lists all _CAUSE_PRIORITY buckets (Phase 3, Step 3.1)

All tests MUST FAIL (or skip due to missing symbol) before the developer lands the code.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

# --- Phase 1: config_file_present helper ---

try:
    from sqlcg.core.config import config_file_present  # introduced by Phase 1 Step 1.1
except ImportError:
    config_file_present = None  # type: ignore[assignment]


def test_hygiene_config_file_present_false_for_empty_dir() -> None:
    """config_file_present returns False when .sqlcg.toml is absent."""
    if config_file_present is None:
        pytest.skip("config_file_present not yet implemented (Phase 1 Step 1.1)")
    with tempfile.TemporaryDirectory() as tmpdir:
        result = config_file_present(Path(tmpdir))
        assert result is False, f"Expected False for empty dir, got {result!r}"


def test_hygiene_config_file_present_true_when_toml_exists() -> None:
    """config_file_present returns True when .sqlcg.toml is present."""
    if config_file_present is None:
        pytest.skip("config_file_present not yet implemented (Phase 1 Step 1.1)")
    with tempfile.TemporaryDirectory() as tmpdir:
        toml = Path(tmpdir) / ".sqlcg.toml"
        toml.write_text('[sqlcg]\ndialect = "snowflake"\n')
        result = config_file_present(Path(tmpdir))
        assert result is True, f"Expected True after writing .sqlcg.toml, got {result!r}"


# --- Phase 1: index_cmd warning ---

from sqlcg.cli.commands.index import index_cmd  # noqa: E402  (already in codebase)


def _make_mock_indexer_summary() -> dict:
    return {
        "files_parsed": 0,
        "tables_found": 0,
        "lineage_edges_created": 1,
        "parse_errors": 0,
        "quality": {"full": 0, "table_only": 0, "scripting_fallback": 0, "failed": 0},
        "error_summary": {},
    }


def _run_index_cmd(tmp_path: Path, *, quiet: bool = False) -> list[str]:
    """Invoke index_cmd with full mocks; return list of console.print() call strings."""
    printed: list[str] = []

    def _capture(*args: object, **kwargs: object) -> None:
        if args:
            printed.append(str(args[0]))

    with (
        patch("sqlcg.cli.commands.index.console") as mock_console,
        patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend,
        patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class,
        patch("sqlcg.cli.commands.index.get_db_path") as mock_db_path,
        patch("sqlcg.cli.commands.index.get_dialect", return_value=None),
        # Force the direct-write path: a live sqlcg server on the host machine
        # would otherwise route this through the socket and bypass every mock
        # below, making the test depend on host runtime state.
        patch("sqlcg.cli.commands.index._try_route_index_via_server", return_value=False),
    ):
        mock_console.print = _capture
        mock_backend = MagicMock()
        mock_backend.get_schema_version.return_value = "7"
        mock_get_backend.return_value.__enter__.return_value = mock_backend
        mock_indexer = MagicMock()
        mock_indexer_class.return_value = mock_indexer
        mock_indexer.index_repo.return_value = _make_mock_indexer_summary()
        mock_db_path.return_value = tmp_path / ".sqlcg" / "graph.db"

        index_cmd(
            tmp_path,
            dialect=None,
            dbt_manifest=None,
            timeout_per_file=5,
            batch_size=50,
            no_ddl=False,
            quiet=quiet,
            verbose=False,
            debug=False,
            profile=False,
            include_working_tree=False,
        )
    return printed


def test_hygiene_index_warns_when_no_config() -> None:
    """index_cmd prints a soft warning naming the searched path when .sqlcg.toml is absent.

    This test will FAIL before Phase 1 Step 1.2 is implemented because
    config_file_present is not yet imported or called from index.py.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # No .sqlcg.toml in tmp_path
        printed = _run_index_cmd(tmp_path, quiet=False)
        combined = " ".join(printed)
        assert ".sqlcg.toml" in combined, (
            f"Expected warning naming '.sqlcg.toml' in output when config is absent; "
            f"got: {printed!r}"
        )
        # Must also name the actual searched path
        assert str(tmp_path) in combined, (
            f"Expected warning to name the searched path '{tmp_path}'; got: {printed!r}"
        )


def test_hygiene_index_no_warning_when_config_present() -> None:
    """index_cmd prints NO config-path warning when .sqlcg.toml exists at index path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg]\ndialect = "snowflake"\n')
        printed = _run_index_cmd(tmp_path, quiet=False)
        # Check that none of the output lines contain the specific "no config" warning
        config_lines = [
            line
            for line in printed
            if ".sqlcg.toml" in line
            and (
                "not found" in line.lower()
                or "no config" in line.lower()
                or "defaults" in line.lower()
            )
        ]
        assert config_lines == [], (
            f"Expected NO config warning when .sqlcg.toml is present; "
            f"warning lines found: {config_lines!r}"
        )


def test_hygiene_index_quiet_suppresses_config_warning() -> None:
    """--quiet suppresses the config-path warning even when .sqlcg.toml is absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # No .sqlcg.toml — warning would fire, but --quiet must suppress it
        printed = _run_index_cmd(tmp_path, quiet=True)
        config_warning_lines = [line for line in printed if ".sqlcg.toml" in line]
        assert config_warning_lines == [], (
            f"--quiet must suppress config-path warning; got: {config_warning_lines!r}"
        )


# --- Phase 2: find column NoiseFilter parity ---

from sqlcg.cli.main import app as cli_app  # noqa: E402

_runner = CliRunner()


def _make_backend(results: list[dict]) -> MagicMock:
    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(return_value=results)
    return backend


def test_hygiene_find_column_filters_noise_by_default() -> None:
    """find column drops noise-table columns by default (without --raw).

    This test will FAIL before Phase 2 Step 2.1 is implemented because
    find_column has no --raw flag and no NoiseFilter call yet.
    """
    # ba.foo_bck is a noise table (matches *_bck glob)
    noise_col = {"id": "ba.foo_bck.order_id"}

    nf_instance = MagicMock()
    nf_instance.is_noise.return_value = True  # foo_bck → noise

    with patch("sqlcg.cli.commands.find.run_read_routed", return_value=[noise_col]):
        with patch("sqlcg.server.noise_filter.NoiseFilter.from_config", return_value=nf_instance):
            result = _runner.invoke(cli_app, ["find", "column", "foo_bck.order_id"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    # The noise column must NOT appear in output when filtering is on (default)
    assert "ba.foo_bck.order_id" not in result.output, (
        f"Noise column should be filtered by default; got output: {result.output!r}"
    )


def test_hygiene_find_column_raw_keeps_noise() -> None:
    """find column --raw retains noise-table columns (disables NoiseFilter).

    This test will FAIL before Phase 2 Step 2.1 is implemented because
    find_column has no --raw flag yet.
    """
    noise_col = {"id": "ba.foo_bck.order_id"}

    with patch("sqlcg.cli.commands.find.run_read_routed", return_value=[noise_col]):
        result = _runner.invoke(cli_app, ["find", "column", "--raw", "foo_bck.order_id"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert "ba.foo_bck.order_id" in result.output, (
        f"--raw must keep noise columns; got output: {result.output!r}"
    )


# --- Phase 3: analyze failures --help discoverability ---


def test_hygiene_failures_help_lists_all_cause_buckets() -> None:
    """analyze failures --help enumerates all _CAUSE_PRIORITY bucket names.

    This test will FAIL before Phase 3 Step 3.1 is implemented because
    the --help text currently shows only "(e.g. E5, timeout)".
    """
    result = _runner.invoke(cli_app, ["analyze", "failures", "--help"])
    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"

    expected_buckets = [
        "timeout",
        "E8",
        "E3",
        "E2",
        "E5",
        "E1",
        "qualify_failed",
        "func_fallback",
        "pure_ddl_skip",
    ]
    missing = [b for b in expected_buckets if b not in result.output]
    assert missing == [], (
        f"--help output is missing bucket names {missing!r}. Full help output:\n{result.output}"
    )
