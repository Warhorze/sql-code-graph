"""Unit tests for load_schema command helpers.

Sprint: sprint_star_resolution.md  Ticket: T-08

All tests are xfail until load_schema_cmd is implemented.
"""

import pytest

# ---------------------------------------------------------------------------
# T-08 — _make_qualified helper
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="load_schema module not yet created — T-08", strict=True)
def test_qualified_name_lowercased_two_part():
    """_make_qualified must lowercase and join schema.table when include_catalog=False."""
    try:
        from sqlcg.cli.commands.load_schema import _make_qualified
    except ImportError:
        pytest.skip("load_schema module not yet created — T-08")

    result = _make_qualified("MYDB", "BA", "SRC_TABLE", include_catalog=False)
    assert result == "ba.src_table", (
        f"Expected 'ba.src_table', got {result!r}. "
        "_make_qualified must lowercase all parts and exclude catalog by default."
    )


@pytest.mark.xfail(reason="load_schema module not yet created — T-08", strict=True)
def test_qualified_name_lowercased_three_part():
    """_make_qualified must include catalog when include_catalog=True."""
    try:
        from sqlcg.cli.commands.load_schema import _make_qualified
    except ImportError:
        pytest.skip("load_schema module not yet created — T-08")

    result = _make_qualified("MYDB", "BA", "SRC_TABLE", include_catalog=True)
    assert result == "mydb.ba.src_table", (
        f"Expected 'mydb.ba.src_table', got {result!r}. "
        "_make_qualified(include_catalog=True) must prefix with lowercased catalog."
    )


@pytest.mark.xfail(reason="load_schema module not yet created — T-08", strict=True)
def test_qualified_name_empty_catalog_excluded():
    """Empty catalog must be omitted even when include_catalog=True."""
    try:
        from sqlcg.cli.commands.load_schema import _make_qualified
    except ImportError:
        pytest.skip("load_schema module not yet created — T-08")

    result = _make_qualified("", "BA", "SRC", include_catalog=True)
    assert result == "ba.src", (
        f"Empty catalog must be omitted. Got {result!r}"
    )


@pytest.mark.xfail(reason="load_schema module not yet created — T-08", strict=True)
def test_load_schema_cmd_registered_in_app():
    """load-schema must appear in the CLI help output after T-08."""
    try:
        from typer.testing import CliRunner

        from sqlcg.cli.main import app
    except ImportError:
        pytest.skip("CLI main not importable")

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert "load-schema" in result.output, (
        "'load-schema' command must be registered in main app. "
        "Add app.command('load-schema')(load_schema_cmd) in cli/main.py."
    )
