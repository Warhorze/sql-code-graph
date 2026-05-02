"""MCP server commands."""

import json
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="MCP server commands")
console = Console()


@app.command("setup")
def mcp_setup(print_only: bool = typer.Option(True, "--print/--write")) -> None:
    """Print or write MCP server config JSON."""
    config = {
        "mcpServers": {
            "sql-code-graph": {
                "command": "sqlcg",
                "args": ["mcp", "start"],
            }
        }
    }
    config_json = json.dumps(config, indent=2)

    if print_only:
        # Print to stdout for manual use
        console.print_json(config_json)
    else:
        # Write to ~/.claude/mcp.json
        config_path = Path.home() / ".claude" / "mcp.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_json + "\n")
        console.print(f"[green]Configuration written to[/green] {config_path}")


@app.command("start")
def mcp_start() -> None:
    """Start the MCP server."""
    from sqlcg.server.server import main as server_main

    server_main()
