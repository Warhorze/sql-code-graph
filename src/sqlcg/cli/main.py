"""Main CLI entry point for sqlcg."""

import typer
from dotenv import load_dotenv

from sqlcg.cli.commands import analyze, db, find, gain, index, install, mcp, report, watch

app = typer.Typer(name="sqlcg", help="SQL code graph analyzer")

# Register subcommand groups
app.add_typer(db.app, name="db")
app.add_typer(find.app, name="find")
app.add_typer(analyze.app, name="analyze")
app.add_typer(mcp.app, name="mcp")
app.add_typer(install.app, name="install")

# Register single commands
app.command("index")(index.index_cmd)
app.command("watch")(watch.watch_cmd)
app.command("gain")(gain.gain_cmd)
app.command("report")(report.report_cmd)


@app.command()
def version() -> None:
    """Show version."""
    from sqlcg import __version__

    typer.echo(f"sqlcg version {__version__}")


def main() -> None:
    """SQL Code Graph - SQL lineage and dependency analysis tool."""
    load_dotenv()
    app()


if __name__ == "__main__":
    main()
