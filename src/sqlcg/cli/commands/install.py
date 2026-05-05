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
    if shutil.which("uvx"):
        entry: dict = {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
    else:
        entry = {"command": "sqlcg", "args": ["mcp", "start"]}

    settings_path = _SETTINGS_PATH
    if settings_path.exists():
        try:
            settings: dict = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            console.print(
                f"[yellow]Warning:[/yellow] {settings_path} contains invalid JSON — "
                "mcpServers key will be added"
            )
            settings = {}
    else:
        settings = {}

    mcp_servers: dict = settings.setdefault("mcpServers", {})

    if mcp_servers.get(_SERVER_KEY) == entry:
        console.print(f"[green]Already configured:[/green] {_SERVER_KEY} → {settings_path}")
        return

    mcp_servers[_SERVER_KEY] = entry

    if dry_run:
        console.print("[dim]--dry-run: would write:[/dim]")
        console.print_json(json.dumps(settings, indent=2))
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)

    cmd_str = f"{entry['command']} {' '.join(entry['args'])}"
    console.print(f"[green]Configured:[/green] {_SERVER_KEY} → {cmd_str}")
    console.print(f"[dim]Written to {settings_path}[/dim]")
    console.print("\nRestart Claude Code to pick up the new MCP server.")
