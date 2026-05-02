"""MCP server commands."""

import json

import typer
from rich.console import Console

app = typer.Typer(help="MCP server commands")
console = Console()


@app.command("setup")
def mcp_setup(print_only: bool = typer.Option(True, "--print/--write")) -> None:
    """Print MCP server config JSON."""
    config = {
        "mcpServers": {
            "sql-code-graph": {
                "command": "sqlcg",
                "args": ["mcp", "start"],
            }
        }
    }
    console.print_json(json.dumps(config))


@app.command("start")
def mcp_start() -> None:
    """Start the MCP server."""
    from sqlcg.server.server import main as server_main

    server_main()
