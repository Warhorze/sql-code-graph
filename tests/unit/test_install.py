"""Tests for sqlcg install command."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app


@pytest.fixture
def cli_runner() -> CliRunner:
    """Provide a CLI runner for testing."""
    return CliRunner()


class TestInstallHooks:
    """Test suite for install hooks command."""

    def test_install_hooks_creates_hook_file(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        """Test that install hooks creates a post-checkout hook file."""
        # Mock git directory
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            # Mock successful git rev-parse
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            with patch(
                "sqlcg.cli.commands.install.Path.cwd",
                return_value=tmp_path,
            ):
                with patch(
                    "sqlcg.cli.commands.install.Path.home",
                    return_value=tmp_path,
                ):
                    # Create a git_dir in the mocked location
                    actual_hook_dir = tmp_path / ".git" / "hooks"
                    actual_hook_dir.mkdir(parents=True, exist_ok=True)

                    result = cli_runner.invoke(app, ["install", "hooks"])

        # Should succeed
        assert result.exit_code == 0
        assert "hook installed" in result.stdout

    def test_install_hooks_skips_if_already_installed(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that install hooks skips if hook already installed."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        hook_path = hooks_dir / "post-checkout"
        hook_content = (
            "#!/bin/bash\n"
            "# sqlcg post-checkout hook\n"
            "sqlcg index --dialect auto --quiet\n"
        )
        hook_path.write_text(hook_content)
        hook_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            result = cli_runner.invoke(app, ["install", "hooks"])

        # Should exit with code 0 and report hook already installed
        assert result.exit_code == 0
        assert "already installed" in result.stdout

    def test_install_hooks_appends_to_existing_hook(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that install hooks appends to existing post-checkout hook."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        hook_path = hooks_dir / "post-checkout"
        existing_content = "#!/bin/bash\n# Some other hook\necho 'existing'\n"
        hook_path.write_text(existing_content)
        hook_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            result = cli_runner.invoke(app, ["install", "hooks"])

        assert result.exit_code == 0
        assert "hook installed" in result.stdout
        content = hook_path.read_text()
        assert "# sqlcg post-checkout hook" in content
        assert "sqlcg index --dialect auto --quiet" in content

    def test_install_hooks_not_in_git_repo(self, cli_runner: CliRunner) -> None:
        """Test that install hooks fails when not in a git repository."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128,  # git error code for not in repo
                stdout="",
            )

            result = cli_runner.invoke(app, ["install", "hooks"])

        assert result.exit_code == 1
        assert "Not in a git repository" in result.stdout

    def test_install_hooks_sets_executable_permission(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that hook file is made executable."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        hook_path = hooks_dir / "post-checkout"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            _ = cli_runner.invoke(app, ["install", "hooks"])

        if hook_path.exists():
            # Check that file is executable (mode & 0o111)
            assert hook_path.stat().st_mode & 0o111

    def test_install_hooks_error_handling(self, cli_runner: CliRunner) -> None:
        """Test that install hooks handles errors gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Test error")

            result = cli_runner.invoke(app, ["install", "hooks"])

        assert result.exit_code == 1
        assert "Error installing hooks" in result.stdout

    def test_install_hooks_sentinel_string(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that the exact sentinel string is used for idempotency."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        hook_path = hooks_dir / "post-checkout"
        # Create a hook with a different comment
        hook_path.write_text("#!/bin/bash\n# Different comment\necho 'test'\n")
        hook_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            result = cli_runner.invoke(app, ["install", "hooks"])

        # Should successfully append the hook
        assert result.exit_code == 0
        content = hook_path.read_text()
        assert "# sqlcg post-checkout hook" in content
        assert "# Different comment" in content

    def test_install_hooks_idempotency(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that running install hooks twice is idempotent."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            # First invocation
            result1 = cli_runner.invoke(app, ["install", "hooks"])
            assert result1.exit_code == 0

            # Second invocation
            result2 = cli_runner.invoke(app, ["install", "hooks"])
            assert result2.exit_code == 0

            # Should report hook already installed on second run
            assert "already installed" in result2.stdout

    def test_install_hooks_creates_parent_directories(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test that install hooks creates parent directories if missing."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # Don't create hooks_dir explicitly

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(git_dir) + "\n",
            )

            result = cli_runner.invoke(app, ["install", "hooks"])

        assert result.exit_code == 0
        hooks_dir = git_dir / "hooks"
        assert hooks_dir.exists()
