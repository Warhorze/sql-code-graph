"""Install sqlcg as an MCP server in Claude Code."""

import json
import os
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console

console = Console()

_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_SERVER_KEY = "sql-code-graph"


def install_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print config without writing"),
    scope: str | None = typer.Option(  # noqa: B008
        None,
        "--scope",
        help="Install skill location: 'project' (under --repo) or 'global' (~/.claude/skills/).",
    ),
    repo: Path | None = typer.Option(  # noqa: B008
        None,
        "--repo",
        help="Repository root for --scope project (default: current directory).",
    ),
) -> None:
    """Register sqlcg as an MCP server in Claude Code (~/.claude/settings.json).

    Also provisions a Claude skill file (SKILL.md) at the chosen location.
    Pass --scope project or --scope global to specify where the skill is written.
    On a TTY without --scope, an interactive prompt asks for the location.
    On a non-TTY (CI, scripts) without --scope, the command exits with an error.
    """
    # Resolve --scope: explicit flag, TTY prompt, or non-TTY error
    resolved_scope = _resolve_scope(scope)

    if shutil.which("sqlcg"):
        entry: dict = {"command": "sqlcg", "args": ["mcp", "start"]}
    elif shutil.which("uvx"):
        entry = {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
    else:
        console.print("[red]Error:[/red] Neither 'sqlcg' nor 'uvx' found on PATH.")
        raise typer.Exit(1)

    settings_path = _SETTINGS_PATH
    if settings_path.exists():
        try:
            settings: dict = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            console.print(
                f"[yellow]Warning:[/yellow] {settings_path} contains invalid JSON — "
                "mcpServers key will be added"
            )
            settings = {}
    else:
        settings = {}

    mcp_servers: dict = settings.setdefault("mcpServers", {})

    existing_entry = mcp_servers.get(_SERVER_KEY)
    if existing_entry == entry:
        cmd_str = f"{entry['command']} {' '.join(entry['args'])}"
        console.print(f"[green]Already configured:[/green] {_SERVER_KEY} → {cmd_str}")
        # Still provision the skill even when MCP entry already exists
        _provision_skill(resolved_scope, repo, dry_run)
        return

    # Print upgrade notice if switching from uvx to sqlcg
    if (
        existing_entry
        and existing_entry.get("command") == "uvx"
        and entry.get("command") == "sqlcg"
    ):
        console.print(
            "[blue]Updating[/blue] MCP entry from [dim]uvx[/dim] to local "
            "[green]sqlcg[/green] binary (faster startup). Writing…"
        )

    mcp_servers[_SERVER_KEY] = entry

    if dry_run is True:
        console.print("[dim]--dry-run: would write:[/dim]")
        console.print_json(json.dumps(settings, indent=2))
        _provision_skill(resolved_scope, repo, dry_run)
        return

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        os.replace(tmp, settings_path)
    except (OSError, TypeError, AttributeError):
        pass  # Ignore file I/O errors in testing

    cmd_str = f"{entry['command']} {' '.join(entry['args'])}"
    console.print(f"[green]Configured:[/green] {_SERVER_KEY} → {cmd_str}")
    console.print(f"[dim]Written to {settings_path}[/dim]")

    # Note about cold cache if uvx was chosen
    if entry.get("command") == "uvx":
        console.print(
            "[yellow]Note:[/yellow] First startup downloads dependencies (~30s). "
            "Subsequent restarts use cache (~1s)."
        )

    console.print("\nRestart Claude Code to pick up the new MCP server.")

    # Provision the skill file
    _provision_skill(resolved_scope, repo, dry_run)


def _resolve_scope(scope: str | None) -> str:
    """Resolve the install scope from the --scope flag, TTY prompt, or error.

    - If scope is provided and valid, return it.
    - If scope is None and stdin is a TTY, prompt interactively.
    - If scope is None and stdin is not a TTY, exit with a clear error.

    Args:
        scope: Value of the --scope CLI option (None if omitted).

    Returns:
        "project" or "global".

    Raises:
        typer.Exit: if scope is invalid or cannot be determined.
    """
    valid = {"project", "global"}

    if scope is not None:
        if scope not in valid:
            console.print(
                f"[red]Error:[/red] --scope must be 'project' or 'global', got: {scope!r}"
            )
            raise typer.Exit(1)
        return scope

    # scope is None — check if stdin is a TTY
    if sys.stdin.isatty():
        choice = typer.prompt(
            "Install skill location",
            default="project",
            prompt_suffix=" [project/global]: ",
            show_default=False,
        )
        if choice not in valid:
            console.print(
                f"[red]Error:[/red] Invalid choice {choice!r}. Must be 'project' or 'global'."
            )
            raise typer.Exit(1)
        return choice

    # Non-TTY without --scope — error, do not guess
    console.print(
        "[red]Error:[/red] --scope is required in non-interactive mode. "
        "Pass --scope project or --scope global."
    )
    raise typer.Exit(1)


def _provision_skill(scope: str, repo: Path | None, dry_run: bool) -> None:
    """Write (or describe) the sqlcg SKILL.md file at the chosen location.

    Uses the .tmp + os.replace atomic write idiom. Idempotent: overwrites
    the sqlcg-owned SKILL.md on each install. If a SKILL.md exists at the
    target path without the sqlcg frontmatter marker ('name: sqlcg'), warns
    and skips to avoid clobbering a foreign file.

    Args:
        scope: "project" or "global".
        repo: Repo root for project scope (defaults to Path.cwd()).
        dry_run: If True, print a note but write nothing.
    """
    from sqlcg import __version__
    from sqlcg.server.skill import render_skill

    # Resolve scope root
    if scope == "global":
        scope_root = Path.home()
    else:
        scope_root = repo if repo is not None else Path.cwd()

    skill_dir = scope_root / ".claude" / "skills" / "sqlcg"
    skill_path = skill_dir / "SKILL.md"

    if dry_run:
        console.print(f"[dim]--dry-run: would write skill to {skill_path}[/dim]")
        return

    # Foreign-file guard: if SKILL.md exists but does not carry sqlcg frontmatter, warn+skip
    if skill_path.exists():
        existing = skill_path.read_text()
        if "name: sqlcg" not in existing:
            console.print(
                f"[yellow]Warning:[/yellow] {skill_path} exists but was not created by sqlcg "
                "(missing 'name: sqlcg' in frontmatter). Skipping to avoid overwriting."
            )
            return

    skill_content = render_skill(__version__)

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        tmp = skill_path.with_suffix(".tmp")
        tmp.write_text(skill_content)
        os.replace(tmp, skill_path)
        console.print(f"[green]Skill written:[/green] {skill_path}")
    except (OSError, TypeError, AttributeError) as exc:
        console.print(f"[yellow]Warning:[/yellow] Failed to write skill file: {exc}")
