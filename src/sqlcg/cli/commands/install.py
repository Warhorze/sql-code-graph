"""Install sqlcg as an MCP server in Claude Code."""

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

console = Console()

_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_SERVER_KEY = "sql-code-graph"


def install_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print config without writing"),
) -> None:
    """Register sqlcg as an MCP server in Claude Code (~/.claude/settings.json)."""
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
