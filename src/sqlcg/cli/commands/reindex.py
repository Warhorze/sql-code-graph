"""Reindex command — incremental resync from a git delta.

Two modes:
  sqlcg reindex --from <sha> --to <sha> <root>
      Explicit SHAs: call resync_changed(root, from, to, ...).
  sqlcg reindex <root>
      Standalone "catch up": read last-indexed SHA from graph metadata,
      diff against current HEAD. If no stored SHA, do a full index_repo.

Gates on schema version like the watch command (same reset+reinit message).
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from sqlcg.core.graph_db import GraphBackend

console = Console()

# Client-side socket timeout for the --notify control-socket path.
# A real DWH server-side resync_changed measured ~89 s (41 changed files + closure);
# 300 s covers that with headroom while keeping the wait bounded on a wedged server.
# This is a CLI transport bound, NOT a DbConfig/indexer constant.
_NOTIFY_SOCKET_TIMEOUT_S = 300

# Set by the parent (foreground hook) process on the re-spawned detached child so it
# does not detach again — the child runs the direct synchronous path to completion
# inside its own session (#77). Absence means "spawn a detached child if needed".
_HOOK_DETACHED_ENV = "SQLCG_HOOK_DETACHED"

# Bounded retry/backoff for the detached child hitting "Database is locked" because
# the *previous* hook resync (e.g. from a rapid second checkout) is still finishing.
# 5 attempts with 1/2/4/8/16s sleeps cover ~31s — comfortably above the ~89s p50 but
# this targets the short window where two checkouts overlap, not a wedged process;
# a still-locked DB after this exhausts visibly logs failure (#77 requirement 4/5)
# rather than silently leaving the graph stale.
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BACKOFF_BASE_S = 1


def reindex_cmd(  # noqa: B008
    path: Path = typer.Argument(..., help="Repository root directory to resync"),  # noqa: B008
    from_sha: str | None = typer.Option(  # noqa: B008
        None, "--from", help="Base git SHA (previously-indexed state)"
    ),
    to_sha: str | None = typer.Option(  # noqa: B008
        None, "--to", help="Target git SHA (defaults to HEAD when --from is given)"
    ),
    dialect: str | None = typer.Option(  # noqa: B008
        "auto", "--dialect", "-d", help="SQL dialect ('auto' reads from .sqlcg.toml, default)"
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
    from sqlcg.core.config import config_file_present, get_backend, get_db_path, get_dialect
    from sqlcg.core.schema import SCHEMA_VERSION
    from sqlcg.indexer.indexer import Indexer

    # Resolve to absolute path so ignore-spec and git delta receive an absolute root
    path = path.resolve()

    # Resolve dialect before routing so the WriterRequest always carries a concrete
    # dialect (never the literal sentinel "auto").  Bug A: the route call was before
    # this resolution, causing the server to receive "auto" and fail with
    # "Unknown dialect 'auto'" on every server-routed reindex.
    if dialect == "auto":
        dialect = get_dialect(path)

    # Step 3.3 — route manual reindex through the socket when a server is live.
    # The --notify flag is kept for backward compatibility but no longer required;
    # manual reindex (no --notify) now also probes the socket by default.
    # W3: from=null is sent when from_sha is None — the server resolves the stored
    # SHA at drain start (no more "requires direct DB access" refusal).
    _is_hook_path = notify  # hook path: fire-and-forget; manual path: wait by default
    _routed = _try_route_reindex_via_server(
        path=path,
        from_sha=from_sha,
        to_sha=to_sha,
        dialect=dialect,
        wait=not _is_hook_path,
        quiet=quiet,
    )
    if _routed:
        return

    # #77 — no server is live and this is the hook path (--notify): the direct
    # synchronous resync below would run in the foreground of `git checkout`/
    # `git merge` and block it for the full resync time. Detach into a background
    # process that runs the same invocation to completion (with the lock-retry
    # below) and return to git immediately. The respawn guard env var prevents
    # the detached child from detaching again.
    if _is_hook_path and os.environ.get(_HOOK_DETACHED_ENV) != "1":
        _spawn_detached_hook_resync(path)
        return

    if not quiet and not config_file_present(path):
        console.print(
            f"[yellow]No .sqlcg.toml found at {path}/.sqlcg.toml — "
            "using defaults (snowflake dialect, no aliases/prefixes). "
            "Create .sqlcg.toml in the index directory to customise.[/yellow]"
        )

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    backend = _open_backend_with_lock_retry() if _is_hook_path else get_backend()
    with backend:
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


def _hook_resync_log_path() -> Path:
    """Return the log file path for detached hook resyncs.

    Derived from ``get_db_path()``'s parent directory (CLAUDE.md: never hardcode
    a path independently of the config module) — same directory as the DB file,
    e.g. ``~/.sqlcg/hook-resync.log``.
    """
    from sqlcg.core.config import get_db_path

    return get_db_path().parent / "hook-resync.log"


def _respawn_argv() -> list[str]:
    """Build the argv to re-exec for the detached hook resync (#77).

    ``sys.argv[0]`` is normally the resolved ``sqlcg`` console-script path
    the hook invoked directly — re-exec it as-is. When ``sys.argv[0]`` is a
    non-executable module entry point (``python -m sqlcg`` -> ``__main__.py``,
    used in tests/dev), re-exec via ``sys.executable -m sqlcg`` instead.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name == "__main__.py" or not os.access(argv0, os.X_OK):
        return [sys.executable, "-m", "sqlcg", *sys.argv[1:]]
    return [sys.argv[0], *sys.argv[1:]]


def _spawn_detached_hook_resync(path: Path) -> None:
    """Re-spawn this reindex invocation as a detached background process (#77).

    Re-execs ``sys.argv`` (the same ``sqlcg reindex --notify ...`` command the
    hook invoked) in a new session, with stdin from ``/dev/null`` and
    stdout/stderr appended to the hook-resync log file. The respawn guard env
    var (``SQLCG_HOOK_DETACHED=1``) is set on the child so it runs the direct
    synchronous path to completion instead of detaching again.

    The parent (this process, running in the foreground of the git hook)
    returns immediately after printing a single status line — git checkout/
    merge is not blocked.

    ``sys.argv[0]`` is normally the resolved ``sqlcg`` console-script path
    the hook invoked (see ``_resolve_sqlcg_bin`` in git.py). When invoked via
    ``python -m sqlcg`` (e.g. in tests/dev), ``sys.argv[0]`` is the
    non-executable ``__main__.py`` — re-exec via ``sys.executable -m sqlcg``
    instead so Popen does not hit ``PermissionError``.
    """
    log_path = _hook_resync_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    argv = _respawn_argv()
    env = dict(os.environ)
    env[_HOOK_DETACHED_ENV] = "1"

    with open(log_path, "ab") as log_file:
        subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            cwd=str(path),
            start_new_session=True,
        )

    console.print(f"sqlcg: resyncing graph in background (log: {log_path})")


def _open_backend_with_lock_retry() -> "GraphBackend":
    """Open the DuckDB backend, retrying with backoff if it is locked.

    The detached hook-resync child can race a previous hook resync that is
    still finishing (e.g. two rapid checkouts). ``get_backend()`` raises
    ``duckdb.IOException`` synchronously when the file is locked by another
    process. Retry a bounded number of times with exponential backoff
    (``_LOCK_RETRY_ATTEMPTS`` attempts, ``_LOCK_RETRY_BACKOFF_BASE_S * 2**n``
    seconds between attempts — ~31 s total).

    If the lock is still held after all retries, log the failure clearly to
    the hook-resync log (so a stale graph is never silent — #77 requirement 5)
    and exit non-zero.
    """
    import duckdb

    from sqlcg.core.config import get_backend

    last_exc: duckdb.IOException | None = None
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            return get_backend()
        except duckdb.IOException as exc:
            last_exc = exc
            if attempt < _LOCK_RETRY_ATTEMPTS - 1:
                time.sleep(_LOCK_RETRY_BACKOFF_BASE_S * (2**attempt))

    console.print(
        f"[red]sqlcg: hook resync failed — database still locked after "
        f"{_LOCK_RETRY_ATTEMPTS} attempts: {last_exc}[/red]"
    )
    raise typer.Exit(1)


def _try_route_reindex_via_server(
    *,
    path: Path,
    from_sha: str | None,
    to_sha: str | None,
    dialect: str | None,
    wait: bool,
    quiet: bool,
) -> bool:
    """Probe for a live server and route the reindex through the socket if found.

    W3: ``from`` may be ``None`` — the server resolves the stored indexed SHA
    at drain start.  Symbolic refs are resolved to concrete SHAs before sending
    (prevents literal "HEAD" being stored in the graph).

    Returns True if the reindex was handled via the server (caller should return).
    Returns False if no server is live (caller should fall through to direct path).
    """
    import json
    import socket as _socket

    from sqlcg.server.control import sock_path

    sp = sock_path()
    if not sp.exists():
        return False

    # Resolve symbolic SHAs if provided (the hook path already resolves them).
    effective_from = _resolve_ref(path, from_sha) if from_sha is not None else None
    effective_to = _resolve_ref(path, to_sha) if to_sha is not None else None

    payload = {
        "op": "reindex",
        "root": str(path),
        "from": effective_from,  # None → server resolves at drain start (W3)
        "to": effective_to,
        "dialect": dialect,
        "wait": wait,
        "requested_by": "hook" if not wait else "cli",
    }
    payload_bytes = json.dumps(payload).encode()
    frame = f"{len(payload_bytes)}\n".encode() + payload_bytes

    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(_NOTIFY_SOCKET_TIMEOUT_S)
            s.connect(str(sp))
            s.sendall(frame)

            f = s.makefile("rb")
            if not wait:
                # Fire-and-forget: read one framed acknowledgement.
                length_line = f.readline()
                if length_line:
                    try:
                        body_len = int(length_line.strip())
                        resp_bytes = f.read(body_len)
                        result = json.loads(resp_bytes)
                        if "error" in result:
                            console.print(f"[red]Server reindex error: {result['error']}[/red]")
                            raise typer.Exit(1)
                        if not quiet:
                            pos = result.get("position", "?")
                            console.print(
                                f"[green]Reindex queued via server[/green] (position {pos})"
                            )
                    except (ValueError, json.JSONDecodeError):
                        pass
                return True

            # wait=True: stream framed frames until done:true.
            while True:
                length_line = f.readline()
                if not length_line:
                    break
                try:
                    body_len = int(length_line.strip())
                except ValueError:
                    break
                frame_bytes = f.read(body_len)
                frame_resp = json.loads(frame_bytes)

                if frame_resp.get("done"):
                    if not frame_resp.get("ok"):
                        err = frame_resp.get("error", "unknown error")
                        console.print(f"[red]Server reindex error: {err}[/red]")
                        raise typer.Exit(1)
                    srv_summary = frame_resp.get("summary", {})
                    if not quiet:
                        if srv_summary.get("fell_back_to_full"):
                            console.print(
                                "[yellow]Closure exceeded depth cap — fell back to full index "
                                "(via server).[/yellow]"
                            )
                        else:
                            console.print(
                                f"[green]Resynced via server[/green] "
                                f"+{srv_summary.get('added', 0)} added, "
                                f"~{srv_summary.get('modified', 0)} modified, "
                                f"-{srv_summary.get('deleted', 0)} deleted"
                            )
                    break

        return True

    except TimeoutError:
        import sys

        print(
            f"Server is still applying the reindex (timed out waiting after "
            f"{_NOTIFY_SOCKET_TIMEOUT_S}s); the graph will update when it finishes "
            "— check 'sqlcg mcp status'.",
            file=sys.stderr,
        )
        raise typer.Exit(0) from None
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        # No live server — fall through to direct path.
        return False
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]Socket routing failed: {exc}[/red]")
        raise typer.Exit(1) from exc


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
