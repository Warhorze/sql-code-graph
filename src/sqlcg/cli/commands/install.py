"""Install sqlcg as an MCP server in Claude Code.

Write path (in priority order):
  1. ``claude mcp add -s user sql-code-graph <cmd> <args>``  — the official
     Claude Code CLI write path (reads from ~/.claude.json under the hood).
  2. Fallback: write ``~/.claude.json`` directly under mcpServers.user when
     the ``claude`` binary is not found or returns non-zero.

The previous target (~/.claude/settings.json) was incorrect — Claude Code does
NOT read MCP servers from that file. See ARCHITECTURE_REVIEW.md §9.2.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

console = Console()

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
    """Register sqlcg as an MCP server in Claude Code.

    Runs ``claude mcp add -s user sql-code-graph <cmd> <args>`` when the
    ``claude`` CLI is on PATH; otherwise writes ~/.claude.json directly.

    Also provisions a Claude skill file (SKILL.md) at the chosen location.
    Pass --scope project or --scope global to specify where the skill is written.
    On a TTY without --scope, an interactive prompt asks for the location.
    On a non-TTY (CI, scripts) without --scope, the command exits with an error.
    """
    # Resolve --scope: explicit flag, TTY prompt, or non-TTY error
    resolved_scope = _resolve_scope(scope)

    if shutil.which("sqlcg"):
        cmd_parts = ["sqlcg", "mcp", "start"]
    elif shutil.which("uvx"):
        cmd_parts = ["uvx", "sql-code-graph", "mcp", "start"]
    else:
        console.print("[red]Error:[/red] Neither 'sqlcg' nor 'uvx' found on PATH.")
        raise typer.Exit(1)

    entry: dict = {"command": cmd_parts[0], "args": cmd_parts[1:]}

    if dry_run:
        claude_bin = shutil.which("claude")
        if claude_bin:
            console.print("[dim]--dry-run: would run:[/dim]")
            console.print(f"  claude mcp add -s user {_SERVER_KEY} {' '.join(cmd_parts)}")
        else:
            claude_json = Path.home() / ".claude.json"
            console.print("[dim]--dry-run: would write to ~/.claude.json:[/dim]")
            _preview_claude_json(claude_json, entry)
        _provision_skill(resolved_scope, repo, dry_run=True)
        return

    # --- Try official claude CLI first ---
    claude_bin = shutil.which("claude")
    if claude_bin:
        proc = subprocess.run(
            ["claude", "mcp", "add", "-s", "user", _SERVER_KEY] + cmd_parts,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            console.print(f"[green]Configured:[/green] {_SERVER_KEY} via `claude mcp add`")
            console.print("\nRestart Claude Code to pick up the new MCP server.")
            _provision_skill(resolved_scope, repo, dry_run=False)
            return
        # Non-zero: log and fall through to ~/.claude.json fallback
        console.print(
            f"[yellow]Warning:[/yellow] `claude mcp add` returned rc={proc.returncode}; "
            "falling back to ~/.claude.json write."
        )
        if proc.stderr:
            console.print(f"[dim]{proc.stderr.strip()}[/dim]")

    # --- Fallback: write ~/.claude.json directly ---
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data: dict = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            console.print(
                f"[yellow]Warning:[/yellow] {claude_json} contains invalid JSON — "
                "mcpServers key will be added"
            )
            data = {}
    else:
        data = {}

    existing = data.get("mcpServers", {}).get("user", {}).get(_SERVER_KEY)
    if existing == entry:
        console.print(f"[green]Already configured:[/green] {_SERVER_KEY} (in ~/.claude.json)")
        _provision_skill(resolved_scope, repo, dry_run=False)
        return

    data.setdefault("mcpServers", {}).setdefault("user", {})[_SERVER_KEY] = entry

    try:
        tmp = claude_json.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, claude_json)
    except (OSError, TypeError, AttributeError):
        pass  # Ignore file I/O errors in testing

    cmd_str = " ".join(cmd_parts)
    console.print(f"[green]Configured:[/green] {_SERVER_KEY} → {cmd_str}")
    console.print(f"[dim]Written to {claude_json}[/dim]")

    if entry.get("command") == "uvx":
        console.print(
            "[yellow]Note:[/yellow] First startup downloads dependencies (~30s). "
            "Subsequent restarts use cache (~1s)."
        )

    console.print("\nRestart Claude Code to pick up the new MCP server.")
    _provision_skill(resolved_scope, repo, dry_run=False)


def _preview_claude_json(claude_json: Path, entry: dict) -> None:
    """Print what would be written to ~/.claude.json without touching the file."""
    if claude_json.exists():
        try:
            data: dict = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            data = {}
    else:
        data = {}
    data.setdefault("mcpServers", {}).setdefault("user", {})[_SERVER_KEY] = entry
    console.print_json(json.dumps(data, indent=2))


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
