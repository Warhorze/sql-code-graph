"""Main CLI entry point for sqlcg."""

import typer

app = typer.Typer()


@app.command()
def version() -> None:
    """Show version."""
    from sqlcg import __version__
    typer.echo(f"sqlcg version {__version__}")


def main() -> None:
    """SQL Code Graph - SQL lineage and dependency analysis tool."""
    app()


if __name__ == "__main__":
    main()
