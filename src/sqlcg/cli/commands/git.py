"""Git integration commands for sqlcg."""

from pathlib import Path
from typing import NamedTuple

import typer
from rich.console import Console

console = Console()

app = typer.Typer(name="git", help="Git integration commands")


class _HookSpec(NamedTuple):
    filename: str
    sentinel: str
    script: str


_HOOKS: list[_HookSpec] = [
    _HookSpec(
        filename="post-checkout",
        sentinel="# sqlcg post-checkout hook",
        script=(
            "#!/bin/sh\n"
            "# sqlcg post-checkout hook — incremental resync after branch switch\n"
            "# $3 == 1 means branch checkout (not file checkout); skip file checkouts\n"
            '[ "$3" = "1" ] || exit 0\n'
            'sqlcg reindex --from "$1" --to "$2"'
            ' "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true\n'
        ),
    ),
    _HookSpec(
        filename="post-merge",
        sentinel="# sqlcg post-merge hook",
        script="""\
#!/bin/sh
# sqlcg post-merge hook — incremental resync after pull/merge
# post-merge receives only $1 (squash flag), no old/new SHA; use stored-SHA delta
sqlcg reindex "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true
""",
    ),
]


def _install_single_hook(hooks_dir: Path, spec: _HookSpec) -> None:
    """Install one git hook idempotently.

    If the hook file already contains the sentinel, it is already installed — skip silently.
    If the hook file exists without the sentinel, warn and print the script for manual append.
    Otherwise, write the hook file and set 0o755.
    """
    hook_path = hooks_dir / spec.filename

    if hook_path.exists():
        existing_content = hook_path.read_text()
        if spec.sentinel in existing_content:
            # Already installed — idempotent, skip silently
            return
        else:
            # Foreign hook without sqlcg sentinel
            console.print(
                f"[yellow]Warning: existing {spec.filename} hook found that was not created "
                "by sqlcg.[/yellow]"
            )
            console.print(
                f"[yellow]To integrate sqlcg, manually append the following to "
                f".git/hooks/{spec.filename}:[/yellow]"
            )
            console.print("")
            console.print("[cyan]" + spec.script.rstrip() + "[/cyan]")
            return

    hook_path.write_text(spec.script)
    hook_path.chmod(0o755)
    console.print(f"[green]Installed git hook:[/green] .git/hooks/{spec.filename}")


@app.command("install-hooks")
def install_hooks(
    repo: Path | None = typer.Option(  # noqa: B008
        None, "--repo", "-r", help="Path to git repository (default: current directory)"
    ),
) -> None:
    """Install git hooks for sqlcg integration.

    Writes a post-checkout hook that triggers incremental resync after branch switches
    and a post-merge hook that triggers resync after pulls/merges.
    Idempotent: running multiple times produces one hook entry per hook.
    """
    if repo is None:
        repo = Path.cwd()

    git_dir = repo / ".git"
    hooks_dir = git_dir / "hooks"

    if not git_dir.exists():
        console.print("[red]Error: not a git repository[/red]")
        raise typer.Exit(1)

    hooks_dir.mkdir(parents=True, exist_ok=True)

    for spec in _HOOKS:
        _install_single_hook(hooks_dir, spec)
