"""Unit tests for CLI."""

import subprocess
import sys


def test_cli_help():
    """Test that the CLI help works."""
    result = subprocess.run(
        [sys.executable, "-m", "sqlcg", "--help"],
        capture_output=True,
        text=True,
    )
    # Help should exit with code 0
    assert result.returncode == 0
    assert "--help" in result.stdout or "--help" in result.stderr


def test_import_cli():
    """Test that we can import the CLI without errors."""
    from sqlcg.cli.main import app
    assert app is not None


def test_import_sqlcg():
    """Test that we can import sqlcg without writing to stdout."""
    import io
    import sys

    old_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()
        import sqlcg
        output = sys.stdout.getvalue()
        assert output == "", f"Expected no stdout output, got: {output}"
        assert hasattr(sqlcg, "__version__")
    finally:
        sys.stdout = old_stdout
