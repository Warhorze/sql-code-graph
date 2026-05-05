"""Install command for sqlcg (e.g., git hooks, shell completions)."""

import subprocess
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="Install sqlcg tools and hooks")
console = Console()


@app.command("hooks")
def install_hooks() -> None:
    """Install or verify git hooks for the current repository.

    Creates a post-checkout hook that automatically runs `sqlcg index --dialect auto --quiet`
    to keep the code graph in sync when switching branches.
    """
    try:
        # Check if we're in a git repository
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            console.print("[red]Error:[/red] Not in a git repository")
            raise typer.Exit(1)

        git_dir = Path(result.stdout.strip())
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_path = hooks_dir / "post-checkout"
        hook_sentinel = "# sqlcg post-checkout hook"

        # Check if hook already contains sqlcg line
        if hook_path.exists():
            content = hook_path.read_text()
            if hook_sentinel in content:
                console.print("[green]✓[/green] sqlcg post-checkout hook already installed")
                return

            # Append to existing hook
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n{hook_sentinel}\nsqlcg index --dialect auto --quiet\n"
        else:
            # Create new hook
            content = (
                "#!/bin/bash\n"
                f"{hook_sentinel}\n"
                "sqlcg index --dialect auto --quiet\n"
            )

        hook_path.write_text(content)
        hook_path.chmod(0o755)
        console.print("[green]✓[/green] sqlcg post-checkout hook installed")

    except Exception as e:
        console.print(f"[red]Error installing hooks:[/red] {e}")
        raise typer.Exit(1) from e
