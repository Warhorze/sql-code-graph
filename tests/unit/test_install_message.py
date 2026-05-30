"""Unit tests for install command uvx cold-cache message (T-14)."""

from unittest.mock import patch

from sqlcg.cli.commands.install import install_cmd


def test_uvx_available_shows_cold_cache_message():
    """Test that uvx-only (no sqlcg) triggers cold cache startup time message (T-14)."""

    def which_impl(cmd: str):
        # Only uvx is available, sqlcg is not
        if cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    with patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl):
        with patch("sqlcg.cli.commands.install._provision_skill"):
            with patch("sqlcg.cli.commands.install.os.replace"):
                with patch("sqlcg.cli.commands.install.console") as mock_console:
                    install_cmd(scope="global", dry_run=False)

                    calls = str(mock_console.print.call_args_list)

                    # The command should be uvx in the settings
                    assert "uvx" in calls, f"Output should mention uvx command; calls: {calls}"

                    # The cold-cache note must be printed when command is uvx
                    assert any(
                        "cold" in str(call).lower()
                        or "download" in str(call).lower()
                        or "first startup" in str(call).lower()
                        for call in mock_console.print.call_args_list
                    ), (
                        f"Cold-cache / first-startup message must appear for uvx command; "
                        f"calls: {calls}"
                    )


def test_uvx_not_available_no_cold_cache_message():
    """Test that when uvx not available, sqlcg command is used without cold cache warning (T-14)."""

    def which_impl(cmd: str):
        # Only sqlcg is available, uvx is not
        if cmd == "sqlcg":
            return "/usr/local/bin/sqlcg"
        return None

    with patch("sqlcg.cli.commands.install.shutil.which", side_effect=which_impl):
        with patch("sqlcg.cli.commands.install._provision_skill"):
            with patch("sqlcg.cli.commands.install.os.replace"):
                with patch("sqlcg.cli.commands.install.console") as mock_console:
                    install_cmd(scope="global", dry_run=False)

                    calls = str(mock_console.print.call_args_list)

                    # Verify that sqlcg command is used
                    assert "sqlcg" in calls, f"Output should mention sqlcg command; calls: {calls}"

                    # Should NOT have cold cache message if using sqlcg directly
                    has_cold_cache = any(
                        "cold" in str(call).lower()
                        or "first startup" in str(call).lower()
                        or "download" in str(call).lower()
                        for call in mock_console.print.call_args_list
                    )
                    assert not has_cold_cache, (
                        f"Cold-cache message must NOT appear when command is sqlcg; calls: {calls}"
                    )
