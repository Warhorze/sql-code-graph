"""Git integration commands for sqlcg."""

from pathlib import Path

import typer
from rich.console import Console

console = Console()

app = typer.Typer(name="git", help="Git integration commands")


@app.command("install-hooks")
def install_hooks(
    repo: Path | None = typer.Option(  # noqa: B008
        None, "--repo", "-r", help="Path to git repository (default: current directory)"
    ),
) -> None:
    """Install git hooks for sqlcg integration.

    Writes a post-checkout hook that triggers graph resync after branch switches.
    Idempotent: running multiple times produces one hook entry.
    """
    if repo is None:
        repo = Path.cwd()

    git_dir = repo / ".git"
    hooks_dir = git_dir / "hooks"

    if not git_dir.exists():
        console.print("[red]Error: not a git repository[/red]")
        raise typer.Exit(1)

    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_path = hooks_dir / "post-checkout"
    hook_sentinel = "# sqlcg post-checkout hook"

    # Hook script content
    hook_script = """#!/bin/sh
# sqlcg post-checkout hook — resync graph after branch switch
# $3 == 1 means branch checkout (not file checkout); skip file checkouts
[ "$3" = "1" ] || exit 0
sqlcg index "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true
"""

    # Check if hook already exists
    if hook_path.exists():
        existing_content = hook_path.read_text()
        if hook_sentinel in existing_content:
            # Already installed, idempotent: skip silently
            return
        else:
            # Existing hook without sqlcg sentinel
            console.print(
                "[yellow]Warning: existing post-checkout hook found that was not created "
                "by sqlcg.[/yellow]"
            )
            console.print(
                "[yellow]To integrate sqlcg, manually append the following to "
                ".git/hooks/post-checkout:[/yellow]"
            )
            console.print("")
            console.print("[cyan]" + hook_script.rstrip() + "[/cyan]")
            return

    # Write hook script
    hook_path.write_text(hook_script)

    # Make it executable
    hook_path.chmod(0o755)

    console.print("[green]Installed git hook:[/green] .git/hooks/post-checkout")
