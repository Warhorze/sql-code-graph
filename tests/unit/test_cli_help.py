"""Unit tests for CLI help strings and user-facing messages (T-01)."""

from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.server.exceptions import NotIndexedError


class TestQuickStartInHelp:
    """Test QUICK START block in CLI help."""

    def test_help_contains_quick_start_block(self):
        """Test that sqlcg --help contains the QUICK START block with all three steps."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "QUICK START" in result.output
        assert "sqlcg db init" in result.output
        assert "sqlcg index" in result.output
        assert "sqlcg git install-hooks" in result.output

    def test_help_quick_start_steps_in_order(self):
        """Test that the three steps appear in the correct order."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])

        output = result.output

        # Find indices of each step
        idx_1 = output.index("1.")
        idx_2 = output.index("2.")
        idx_3 = output.index("3.")

        # Verify they're in order
        assert idx_1 < idx_2 < idx_3, "Steps should be in numeric order"

    def test_help_contains_binary_package_note(self):
        """Test that help text includes the binary/package name note."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "Binary is `sqlcg`" in result.output
        assert "PyPI package is `sql-code-graph`" in result.output


class TestNotIndexedErrorMessage:
    """Test NotIndexedError message."""

    def test_not_indexed_error_contains_both_commands(self):
        """Test that NotIndexedError message names both db init and index commands."""
        exc = NotIndexedError(
            "No repos indexed. Run 'sqlcg db init' then 'sqlcg index <path>' first."
        )
        msg = str(exc)

        assert "sqlcg db init" in msg
        assert "sqlcg index <path>" in msg
