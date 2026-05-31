"""Index command for scanning and indexing SQL files."""

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from sqlcg.core.config import KuzuConfig, get_backend, get_db_path, get_dialect
from sqlcg.indexer.indexer import Indexer

console = Console()


def index_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Directory to index"),  # noqa: B008
    dialect: str | None = typer.Option(  # noqa: B008
        None, "--dialect", "-d", help="SQL dialect (or 'auto' to read from .sqlcg.toml)"
    ),
    dbt_manifest: Path | None = typer.Option(  # noqa: B008
        None, "--dbt-manifest", help="Path to dbt manifest"
    ),
    timeout_per_file: int = typer.Option(  # noqa: B008
        5, "--timeout-per-file", help="Timeout per file in seconds"
    ),
    buffer_pool_size: int = typer.Option(  # noqa: B008
        0,
        "--buffer-pool-size",
        help="KuzuDB buffer pool size in MB (0 = default). "
        "Set to 256-512 on memory-constrained machines.",
    ),
    batch_size: int = typer.Option(  # noqa: B008
        50,
        "--batch-size",
        help=(
            "Files per KuzuDB transaction in the upsert pass. "
            "Default 50 balances commit-overhead reduction (vs. legacy per-file commits) "
            "against per-batch memory cost. Lower values are safer for memory-constrained "
            "machines; higher values give marginal speedup at the cost of larger working sets. "
            "Set to 1 to reproduce legacy per-file commit behaviour."
        ),
    ),
    no_ddl: bool = typer.Option(  # noqa: B008
        False, "--no-ddl", help="Skip table-node upserts for DDL-only files"
    ),
    quiet: bool = typer.Option(  # noqa: B008
        False, "--quiet", "-q", help="Suppress summary console output"
    ),
    verbose: bool = typer.Option(  # noqa: B008
        False, "--verbose", "-v", help="Print parse warnings to stderr instead of log file"
    ),
    debug: bool = typer.Option(  # noqa: B008
        False, "--debug", help="Show detailed log output during indexing"
    ),
    profile: bool = typer.Option(  # noqa: B008
        False, "--profile/--no-profile", help="Emit per-stage timing after indexing"
    ),
    include_working_tree: bool = typer.Option(  # noqa: B008
        False,
        "--include-working-tree",
        help=(
            "Index the working tree including uncommitted changes. "
            "Marks freshness as 'indexed with working-tree changes'."
        ),
    ),
) -> None:
    """Index SQL files in a directory.

    Schema aliases (staging schema → canonical schema) can be configured in
    .sqlcg.toml under sqlcg.schema_aliases, e.g. da_tmp = "da".
    """

    import logging
    import sys

    level = logging.DEBUG if debug else logging.CRITICAL
    logging.getLogger("sqlcg").setLevel(level)
    logging.getLogger("sqlglot").setLevel(level)

    # Route parse warnings to stderr (--verbose) or to the configured log file.
    sqlcg_log = logging.getLogger("sqlcg")

    class _CountingHandler(logging.Handler):
        """Counts WARNING+ records emitted during indexing."""

        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.count = 0

        def emit(self, record: logging.LogRecord) -> None:
            self.count += 1

    _counter = _CountingHandler()
    sqlcg_log.addHandler(_counter)

    if verbose:
        _warn_handler: logging.Handler = logging.StreamHandler(sys.stderr)
        _warn_handler.setLevel(logging.WARNING)
        sqlcg_log.addHandler(_warn_handler)
        _warn_log_path = None
    else:
        _warn_log_path = KuzuConfig.from_env().log_path
        _warn_log_path.parent.mkdir(parents=True, exist_ok=True)
        _warn_handler = logging.FileHandler(_warn_log_path)
        _warn_handler.setLevel(logging.WARNING)
        sqlcg_log.addHandler(_warn_handler)

    # Set buffer pool size via env var if specified
    if buffer_pool_size > 0:
        os.environ["SQLCG_BUFFER_POOL_MB"] = str(buffer_pool_size)

    # Resolve dialect: 'auto' reads from .sqlcg.toml, otherwise use provided value
    if dialect == "auto":
        dialect = get_dialect(path)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _run_index(
            path=path,
            dialect=dialect,
            dbt_manifest=dbt_manifest,
            timeout_per_file=timeout_per_file,
            no_ddl=no_ddl,
            quiet=quiet,
            batch_size=batch_size,
            profile=profile,
        )
    except KeyboardInterrupt:
        # The backend context manager (inside _run_index) has already closed the
        # KuzuDB connection and released the lock by the time we get here.
        console.print("\n[yellow]Interrupted — no partial graph written. Re-run to index.[/yellow]")
        raise typer.Exit(130) from None
    finally:
        sqlcg_log.removeHandler(_warn_handler)
        sqlcg_log.removeHandler(_counter)
        _warn_handler.close()

    # --include-working-tree: if the working tree is dirty, overwrite the stored SHA
    # with a "<head>+dirty" sentinel so 'db info' can distinguish clean-HEAD index
    # from working-tree-inclusive index.  The backend was closed inside _run_index,
    # so we open a fresh context here for the single sentinel write.
    if include_working_tree:
        from sqlcg.core.freshness import _git

        dirty_out = _git(path, "status", "--porcelain")
        if dirty_out:  # non-empty string → working tree is dirty
            head = _git(path, "rev-parse", "HEAD") or "unknown"
            with get_backend() as _b2:
                _b2.set_indexed_sha(f"{head}+dirty")

    if not verbose and not quiet and _counter.count > 0 and _warn_log_path is not None:
        console.print(
            f"[yellow]Parse warnings written to {_warn_log_path} "
            "— use --verbose to show here.[/yellow]"
        )


def _run_index(
    *,
    path: Path,
    dialect: str | None,
    dbt_manifest: Path | None,
    timeout_per_file: int,
    no_ddl: bool,
    quiet: bool,
    batch_size: int,
    profile: bool,
) -> None:
    with get_backend() as backend:
        backend.init_schema()

        # Check schema version — must match current build
        from sqlcg.core.schema import SCHEMA_VERSION, NodeLabel

        stored = backend.get_schema_version()
        if stored != SCHEMA_VERSION:
            console.print(
                f"[red]Database schema is v{stored}; this build requires v{SCHEMA_VERSION}. "
                "Run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-index.[/red]"
            )
            raise typer.Exit(1)

        abs_path = str(path.resolve())
        backend.upsert_node(
            NodeLabel.REPO,
            abs_path,
            {
                "path": abs_path,
                "name": path.name,
            },
        )

        # Index the repository
        indexer = Indexer()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
            redirect_stderr=True,
        ) as progress:
            task = progress.add_task("Parsing", total=None)

            def _progress_callback(n: int, total_n: int) -> None:
                progress.update(task, completed=n, total=total_n)

            summary = indexer.index_repo(
                path,
                dialect,
                backend,
                dbt_manifest,
                timeout_per_file,
                progress_callback=_progress_callback,
                no_ddl=no_ddl,
                batch_size=batch_size,
                profile=profile,
            )

        # Connect files to repo
        from sqlcg.core.schema import RelType

        files_query = "MATCH (f:File) WHERE f.path STARTS WITH $repo_prefix RETURN f.path AS path"
        file_rows = backend.run_read(files_query, {"repo_prefix": abs_path})
        for row in file_rows:
            backend.upsert_edge(
                NodeLabel.FILE,
                row["path"],
                NodeLabel.REPO,
                abs_path,
                RelType.BELONGS_TO,
                {},
            )

        # Print summary unless --quiet is specified
        if not quiet:
            quality = summary.get("quality", {})
            err = summary.get("error_summary", {})
            n_full = quality.get("full", 0)
            n_partial = quality.get("table_only", 0)
            n_ddl = quality.get("scripting_fallback", 0)
            n_failed = quality.get("failed", 0)
            n_timeout = err.get("timeout", 0)

            quality_parts = []
            if n_full:
                quality_parts.append(f"[green]{n_full} with column lineage[/green]")
            if n_partial:
                quality_parts.append(f"[yellow]{n_partial} table-only[/yellow]")
            if n_ddl:
                quality_parts.append(f"{n_ddl} DDL-only")
            if n_timeout:
                quality_parts.append(f"[red]{n_timeout} timed out[/red]")
            if n_failed:
                quality_parts.append(f"[red]{n_failed} failed[/red]")

            console.print(
                f"[green]Indexed[/green] {summary['files_parsed']} files — "
                f"{summary['tables_found']} tables, {summary['lineage_edges_created']} edges"
            )
            if quality_parts:
                console.print("  " + " · ".join(quality_parts))
            if summary.get("lineage_edges_created", 0) == 0:
                console.print(
                    "[yellow]Warning: 0 lineage edges extracted — column lineage "
                    "unavailable.[/yellow]"
                )

        if prof := summary.get("profile"):
            console.print("\n[bold]Profile[/bold]")
            console.print(
                f"  pass1 parse:    {prof['pass1_parse_s']:.2f}s\n"
                f"  pass2 resolve:  {prof['pass2_resolve_s']:.2f}s\n"
                f"  upsert:         {prof['upsert_s']:.2f}s\n"
                f"  star expand:    {prof['star_expand_s']:.2f}s\n"
                f"  total:          {prof['total_s']:.2f}s\n"
                f"  ms/file:        {prof['ms_per_file']:.1f}ms  ({prof['files']} files)"
            )
            if prof["slowest_files"]:
                console.print("  [bold]Slowest files[/bold]")
                for file_path, ms in prof["slowest_files"]:
                    console.print(f"    {ms:8.1f}ms  {file_path}")
