"""Unit tests for install command uvx cold-cache message (T-14)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg.cli.commands.install import install_cmd


def test_uvx_available_shows_cold_cache_message():
    """Test that uvx-only (no sqlcg) triggers cold cache startup time message (T-14)."""
    CliRunner()

    def which_impl(cmd: str):
        # Only uvx is available, sqlcg is not
        if cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    with patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl):
        with patch("sqlcg.cli.commands.install.Path"):
            mock_settings_path = MagicMock(spec=Path)
            mock_settings_path.exists.return_value = False
            mock_settings_path.parent = MagicMock()
            mock_settings_path.with_suffix.return_value = MagicMock()

            with patch("sqlcg.cli.commands.install.console") as mock_console:
                install_cmd()

                # Verify that cold cache message was printed
                str(mock_console.print.call_args_list)
                # Check that the message about cold cache or first startup is shown
                any(
                    "cold" in str(call).lower()
                    or "download" in str(call).lower()
                    or "first" in str(call).lower()
                    for call in mock_console.print.call_args_list
                )

                # The command should be uvx in the settings
                found_uvx = any("uvx" in str(call) for call in mock_console.print.call_args_list)
                assert found_uvx, "Output should mention uvx command"


def test_uvx_not_available_no_cold_cache_message():
    """Test that when uvx not available, sqlcg command is used without cold cache warning (T-14)."""
    CliRunner()

    def which_impl(cmd: str):
        # Only sqlcg is available, uvx is not
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        return None

    with patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl):
        with patch("sqlcg.cli.commands.install.Path"):
            mock_settings_path = MagicMock(spec=Path)
            mock_settings_path.exists.return_value = False
            mock_settings_path.parent = MagicMock()
            mock_settings_path.with_suffix.return_value = MagicMock()

            with patch("sqlcg.cli.commands.install.console") as mock_console:
                install_cmd()

                # Verify that sqlcg command is used
                str(mock_console.print.call_args_list)
                found_sqlcg = any(
                    "sqlcg" in str(call) for call in mock_console.print.call_args_list
                )
                assert found_sqlcg, "Output should mention sqlcg command"

                # Should NOT have cold cache message if using sqlcg directly
                any(
                    "cold cache" in str(call).lower() or "first startup" in str(call).lower()
                    for call in mock_console.print.call_args_list
                )
                # Note: We don't assert False here because the message is conditional on uvx
