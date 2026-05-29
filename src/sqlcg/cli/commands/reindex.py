"""Reindex command — incremental resync from a git delta.

Two modes:
  sqlcg reindex --from <sha> --to <sha> <root>
      Explicit SHAs: call resync_changed(root, from, to, ...).
  sqlcg reindex <root>
      Standalone "catch up": read last-indexed SHA from graph metadata,
      diff against current HEAD. If no stored SHA, do a full index_repo.

Gates on schema version like the watch command (same reset+reinit message).
"""

import subprocess
from pathlib import Path

import typer
from rich.console import Console

console = Console()


def reindex_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Repository root directory to resync"),  # noqa: B008
    from_sha: str | None = typer.Option(  # noqa: B008
        None, "--from", help="Base git SHA (previously-indexed state)"
    ),
    to_sha: str | None = typer.Option(  # noqa: B008
        None, "--to", help="Target git SHA (defaults to HEAD when --from is given)"
    ),
    dialect: str | None = typer.Option(  # noqa: B008
        None, "--dialect", "-d", help="SQL dialect (or 'auto' to read from .sqlcg.toml)"
    ),
    quiet: bool = typer.Option(  # noqa: B008
        False, "--quiet", "-q", help="Suppress summary output"
    ),
    batch_size: int = typer.Option(  # noqa: B008
        50,
        "--batch-size",
        help="Files per KuzuDB transaction (same default as index command)",
    ),
    timeout_per_file: int = typer.Option(  # noqa: B008
        5,
        "--timeout-per-file",
        help="Per-file parse timeout in seconds",
    ),
) -> None:
    """Incrementally resync the graph after a git branch change or pull.

    When --from and --to are given (e.g. from the post-checkout hook), only the
    files that changed between those two SHAs are re-parsed, plus the cross-file
    pass-2 closure (files that SELECT FROM tables defined in changed files).

    Without --from/--to, reads the last-indexed SHA from the database and diffs it
    against the current HEAD. If no stored SHA is found, falls back to a full index.

    Exits with an error if the database schema version does not match the current
    build — run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-init.
    """
    from sqlcg.core.config import get_backend, get_db_path, get_dialect
    from sqlcg.core.schema import SCHEMA_VERSION
    from sqlcg.indexer.indexer import Indexer

    # Resolve dialect
    if dialect == "auto":
        dialect = get_dialect(path)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_backend() as backend:
        backend.init_schema()

        # Schema-version gate (mirrors watch.py)
        stored_version = backend.get_schema_version()
        if stored_version != SCHEMA_VERSION:
            console.print(
                f"[red]Database schema is v{stored_version}; "
                f"this build requires v{SCHEMA_VERSION}. "
                "Run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' "
                "to re-initialize.[/red]"
            )
            raise typer.Exit(1)

        indexer = Indexer()

        # ---- Determine mode -------------------------------------------------------
        if from_sha is not None:
            # Explicit-SHA mode
            effective_to = to_sha or _get_head(path)
            if not quiet:
                console.print(
                    f"Resyncing [cyan]{path}[/cyan] [dim]{from_sha[:8]}..{effective_to[:8]}[/dim]"
                )
            summary = indexer.resync_changed(
                path,
                from_sha,
                effective_to,
                backend,
                dialect,
                batch_size=batch_size,
                timeout_per_file=timeout_per_file,
            )
        else:
            # Standalone mode: use stored SHA -> HEAD
            old = backend.get_indexed_sha()
            if old is None:
                if not quiet:
                    console.print(
                        "[yellow]No stored index SHA found — performing full index.[/yellow]"
                    )
                indexer.index_repo(
                    path,
                    dialect,
                    backend,
                    batch_size=batch_size,
                    timeout_per_file=timeout_per_file,
                )
                if not quiet:
                    console.print("[green]Full index complete.[/green]")
                return
            new = _get_head(path)
            if not quiet:
                console.print(f"Resyncing [cyan]{path}[/cyan] [dim]{old[:8]}..{new[:8]}[/dim]")
            summary = indexer.resync_changed(
                path,
                old,
                new,
                backend,
                dialect,
                batch_size=batch_size,
                timeout_per_file=timeout_per_file,
            )

        # ---- Print summary --------------------------------------------------------
        if not quiet:
            if summary.get("fell_back_to_full"):
                console.print(
                    "[yellow]Closure exceeded depth cap — fell back to full index.[/yellow]"
                )
            else:
                console.print(
                    f"[green]Resynced[/green] "
                    f"+{summary['added']} added, "
                    f"~{summary['modified']} modified, "
                    f"-{summary['deleted']} deleted, "
                    f"{summary['closure_resolved']} closure files re-resolved"
                )


def _get_head(root: Path) -> str:
    """Return the current HEAD SHA for the git repo at root.

    Raises typer.Exit(1) if git is unavailable or root is not a git repo.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(
                f"[red]Could not determine HEAD SHA in {root}: {result.stderr.strip()}[/red]"
            )
            raise typer.Exit(1)
        return result.stdout.strip()
    except FileNotFoundError:
        console.print("[red]git is not available — cannot determine HEAD SHA[/red]")
        raise typer.Exit(1) from None
