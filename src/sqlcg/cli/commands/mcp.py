"""MCP server commands."""

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="MCP server commands")
console = Console()

_SERVER_KEY = "sql-code-graph"


def _server_entry() -> dict:
    if shutil.which("uvx"):
        return {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
    return {"command": "sqlcg", "args": ["mcp", "start"]}


@app.command("setup")
def mcp_setup(print_only: bool = typer.Option(True, "--print/--write")) -> None:
    """Print or write MCP server config JSON.

    --print (default): print the JSON snippet for manual insertion.
    --write: write to ~/.claude.json under mcpServers.user (the correct path
             for Claude Code — not settings.json, which Claude Code does not read
             for MCP servers).
    """
    entry = _server_entry()
    if print_only:
        console.print_json(json.dumps({"mcpServers": {_SERVER_KEY: entry}}, indent=2))
        return

    # Write to ~/.claude.json (correct path for Claude Code MCP servers)
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data: dict = json.loads(claude_json.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    data.setdefault("mcpServers", {}).setdefault("user", {})[_SERVER_KEY] = entry

    tmp = claude_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, claude_json)
    console.print(f"[green]Configuration written to[/green] {claude_json}")
    console.print("Note: Binary is `sqlcg`; PyPI package is `sql-code-graph`.")


@app.command("start")
def mcp_start() -> None:
    """Start the MCP server."""
    from sqlcg.server.server import main as server_main

    server_main()


@app.command("best-practices")
def mcp_best_practices() -> None:
    """Print MCP tool best-practices (the fact/heuristic boundary).

    Same guidance as the bundled Claude skill — useful for humans or agents
    that have not installed the skill.
    """
    from sqlcg.server.skill import render_body

    typer.echo(render_body())
