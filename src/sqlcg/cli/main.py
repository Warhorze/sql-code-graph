"""Main CLI entry point for sqlcg."""

import typer
from dotenv import load_dotenv

from sqlcg.cli.commands import (
    analyze,
    catalog,
    db,
    find,
    gain,
    git,
    index,
    install,
    mcp,
    reindex,
    report,
    uninstall,
    watch,
)

help_text = """SQL code graph analyzer.

QUICK START:
  1. sqlcg db init
  2. sqlcg index <path> --dialect snowflake
  3. sqlcg git install-hooks
  4. sqlcg install --scope project   # also provisions a Claude skill (SKILL.md)

USING THE MCP TOOLS:
  Read `sqlcg mcp best-practices` first — it explains the fact/heuristic
  boundary so heuristic output (dead-code, risk) is never reported as fact.
  See `sqlcg mcp --help` for all MCP commands.

Note: Binary is `sqlcg`; PyPI package is `sql-code-graph`.
"""


def _version_string() -> str:
    from sqlcg import __version__

    return f"sqlcg version {__version__}"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(_version_string())
        raise typer.Exit()


app = typer.Typer(name="sqlcg", help=help_text)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    """SQL code graph analyzer."""
    # The eager --version callback exits before we get here when --version is
    # passed. Otherwise: if no subcommand was given, show help (matching the
    # pre-callback behaviour) instead of silently exiting 0.
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


# Register subcommand groups
app.add_typer(db.app, name="db")
app.add_typer(find.app, name="find")
app.add_typer(analyze.app, name="analyze")
app.add_typer(catalog.app, name="catalog")
app.add_typer(mcp.app, name="mcp")
app.add_typer(git.app, name="git")

# Register single commands
app.command("index")(index.index_cmd)
app.command("reindex")(reindex.reindex_cmd)
app.command("watch")(watch.watch_cmd)
app.command("gain")(gain.gain_cmd)
app.command("report")(report.report_cmd)
app.command("install")(install.install_cmd)
app.command("uninstall")(uninstall.uninstall_cmd)


@app.command()
def version() -> None:
    """Show version."""
    typer.echo(_version_string())


def main() -> None:
    """SQL Code Graph - SQL lineage and dependency analysis tool."""
    load_dotenv()
    app()


if __name__ == "__main__":
    main()
