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

# Client-side socket timeout for the --notify control-socket path.
# A real DWH server-side resync_changed measured ~89 s (41 changed files + closure);
# 300 s covers that with headroom while keeping the wait bounded on a wedged server.
# This is a CLI transport bound, NOT a KuzuConfig/indexer constant.
_NOTIFY_SOCKET_TIMEOUT_S = 300


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
        10,
        "--timeout-per-file",
        help="Per-file parse timeout in seconds",
    ),
    notify: bool = typer.Option(  # noqa: B008
        False,
        "--notify",
        help=(
            "If a server is live on this DB, route the reindex through the server "
            "(avoids lock contention). Falls back to direct write if no server is found."
        ),
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
    import json
    import socket as _socket

    from sqlcg.core.config import config_file_present, get_backend, get_db_path, get_dialect
    from sqlcg.core.schema import SCHEMA_VERSION
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.server.control import sock_path

    # Resolve to absolute path so ignore-spec and git delta receive an absolute root
    path = path.resolve()

    # --notify: if a server is live, route reindex through the socket (R3 fallback)
    if notify:
        sp = sock_path()
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(_NOTIFY_SOCKET_TIMEOUT_S)
                s.connect(str(sp))
                # Resolve SHAs before sending — standalone mode reads from DB via socket
                effective_from = from_sha
                if effective_from is None:
                    # Standalone mode: we cannot read stored SHA here without opening the
                    # DB (which would conflict with the running server).  If no --from is
                    # given with --notify, we send from="stored" as a sentinel and fall
                    # back to direct write; the caller should pass --from explicitly.
                    raise OSError(  # noqa: TRY301
                        "--notify without --from requires direct DB access; falling through"
                    )
                # Resolve symbolic refs (HEAD, branch names) to concrete 40-char SHAs
                # before sending — prevents literal "HEAD" from being stored in the graph.
                effective_from = _resolve_ref(path, effective_from)
                effective_to = _resolve_ref(path, to_sha) if to_sha else _get_head(path)
                payload = {
                    "op": "reindex",
                    "root": str(path),
                    "from": effective_from,
                    "to": effective_to,
                    "dialect": dialect,
                }
                s.sendall(json.dumps(payload).encode() + b"\n")
                data = s.recv(65536)
            result = json.loads(data)
            if "error" in result:
                console.print(f"[red]Server reindex error: {result['error']}[/red]")
                raise typer.Exit(1)
            if not quiet:
                srv_summary = result.get("summary", {})
                console.print(
                    f"[green]Resynced via server[/green] "
                    f"+{srv_summary.get('added', 0)} added, "
                    f"~{srv_summary.get('modified', 0)} modified, "
                    f"-{srv_summary.get('deleted', 0)} deleted"
                )
            raise typer.Exit(0)
        except TimeoutError:
            # Bug 1 fix: server is alive and working (accepted the connection, holds the
            # lock, will finish and persist).  Do NOT fall through to the direct-write
            # path — that would hit the held lock and produce a false "Database is locked"
            # error.  Exit 0 so the git hook stays non-fatal; the server will complete.
            # (socket.timeout is an alias of TimeoutError, a subclass of OSError — this
            # clause must be listed before the broad OSError clause below.)
            import sys

            print(
                f"Server is still applying the reindex (timed out waiting after "
                f"{_NOTIFY_SOCKET_TIMEOUT_S}s); the graph will update when it finishes "
                f"— check 'sqlcg mcp status'.",
                file=sys.stderr,
            )
            raise typer.Exit(0) from None
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            # R3: no live server (stale socket, socket absent, fallback condition) —
            # fall through to the existing direct-write path unchanged.
            # NOTE: socket.timeout / TimeoutError is an OSError subclass, so the
            # dedicated timeout clause above must be listed first (already is).
            pass
        except typer.Exit:
            raise
        except Exception as exc:
            console.print(f"[red]--notify routing failed: {exc}[/red]")
            raise typer.Exit(1) from exc

    # Resolve dialect
    if dialect == "auto":
        dialect = get_dialect(path)

    if not quiet and not config_file_present(path):
        console.print(
            f"[yellow]No .sqlcg.toml found at {path}/.sqlcg.toml — "
            "using defaults (snowflake dialect, no aliases/prefixes). "
            "Create .sqlcg.toml in the index directory to customise.[/yellow]"
        )

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
            # Explicit-SHA mode — resolve symbolic refs to concrete SHAs before storing
            effective_from = _resolve_ref(path, from_sha)
            effective_to = _resolve_ref(path, to_sha) if to_sha else _get_head(path)
            if not quiet:
                console.print(
                    f"Resyncing [cyan]{path}[/cyan] "
                    f"[dim]{effective_from[:8]}..{effective_to[:8]}[/dim]"
                )
            summary = indexer.resync_changed(
                path,
                effective_from,
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


def _resolve_ref(root: Path, ref: str) -> str:
    """Resolve a git ref (HEAD, branch, tag, or concrete SHA) to a 40-char SHA.

    A concrete SHA resolves to itself (idempotent), so callers may pass either a
    symbolic ref or a SHA without branching.

    Raises typer.Exit(1) if git is unavailable or the ref cannot be resolved.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(
                f"[red]Could not resolve ref '{ref}' in {root}: {result.stderr.strip()}[/red]"
            )
            raise typer.Exit(1)
        return result.stdout.strip()
    except FileNotFoundError:
        console.print("[red]git is not available — cannot resolve ref[/red]")
        raise typer.Exit(1) from None


def _get_head(root: Path) -> str:
    """Return the current HEAD SHA for the git repo at root.

    Delegates to _resolve_ref so there is one git-rev-parse code path.
    Raises typer.Exit(1) if git is unavailable or root is not a git repo.
    """
    return _resolve_ref(root, "HEAD")
