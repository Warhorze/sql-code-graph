"""MCP server commands."""

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.server.selfheal import read_ondisk_version

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


@app.command("status")
def mcp_status() -> None:
    """Print server status JSON (connects to control socket).

    Returns JSON with fields: running, version, pid, db_path, indexed_sha,
    head_sha, stale_by_commits, connected_clients, uptime, writer_queue,
    stale_by_version when a server is live.

    `version` is the *running* server's reported sqlcg.__version__ (read live
    over the control socket — the build the editor is currently talking to).
    `stale_by_version` compares it against the *installed* sqlcg.__version__
    (the CLI process you just invoked): true when they differ, false when they
    match, null when the running version cannot be determined (degraded mode).
    When stale, a warning is printed telling you to restart the MCP server via
    your editor or run `sqlcg install` (which stops the stale server for you).
    This is distinct from `stale_by_commits` (graph-vs-repo freshness, not
    package-vs-binary drift).

    The status response is length-prefixed framed (v1.3.0, B3) so large
    writer_queue payloads are received in full — the client uses the
    recv-exactly makefile+readline+read(n) pattern, NOT a single recv(4096).

    When no server is found: {"running": false}.
    When the PID file exists with a live process but the socket is unavailable:
    {"running": true, "degraded": "socket unavailable", "stale_by_version": null, ...}
    (the running version cannot be read without a live socket).

    R3 (stale socket): if the socket file exists but the server is not
    responding (ConnectionRefusedError / FileNotFoundError), falls through
    to the PID-file probe — never hangs or errors on a dead socket.
    """
    import socket as _socket
    from datetime import datetime

    from sqlcg.server.control import is_pid_alive, read_pid, sock_path

    sp = sock_path()
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(sp))
            s.sendall(json.dumps({"op": "status"}).encode() + b"\n")
            # Framed recv-exactly (B3 / OD-4): read length line then exactly that many bytes.
            # This replaces the old s.recv(4096) which would truncate large writer_queue payloads.
            f = s.makefile("rb")
            length_line = f.readline()
            if length_line:
                try:
                    body_len = int(length_line.strip())
                    data = f.read(body_len)
                except (ValueError, OSError):
                    data = length_line  # fallback: treat first line as body
            else:
                data = b""

        status = json.loads(data.decode())

        # Drift detection (W1 / Step 1.4): compare the *running* server's
        # reported version (read over the socket, from the live process) against
        # the *on-disk* installed version (read fresh via read_ondisk_version()).
        # Using the on-disk version — not the CLI's baked-in __version__ — means
        # stale_by_version is correct even when the CLI process itself is freshly
        # reinstalled to a different version than the running server.
        running_version = status.get("version")
        ondisk_version = read_ondisk_version()
        # F1 guard: if ondisk_version is None the distribution is unreadable;
        # never treat that as a skew — same safety rule as detect_skew().
        stale = (
            running_version is not None
            and ondisk_version is not None
            and running_version != ondisk_version
        )
        status = {
            **status,
            "version": running_version,
            "ondisk_version": ondisk_version,
            "stale_by_version": stale,
        }

        # Pretty-print the base fields.
        console.print_json(json.dumps({k: v for k, v in status.items() if k != "writer_queue"}))

        if stale:
            console.print(
                f"[yellow]Warning:[/yellow] running MCP server is v{running_version}, "
                f"installed (on-disk) sqlcg is v{ondisk_version} — restart the MCP server via "
                "your editor (or run `sqlcg install`, which stops the stale server for you)."
            )

        # Render the writer_queue block separately for readability.
        wq = status.get("writer_queue")
        if wq:
            console.print("\n[bold]writer_queue[/bold]")
            active = wq.get("active")
            if active:
                console.print(f"  active: op={active.get('op')!r} root={active.get('root')!r}")
                prog = wq.get("active_progress", {})
                if prog.get("state") == "running":
                    files_done = prog.get("files_done", 0)
                    files_total = prog.get("files_total")
                    if files_total:
                        console.print(f"    progress: {files_done}/{files_total} files")
            else:
                console.print("  active: none")

            pending = wq.get("pending", [])
            console.print(f"  pending: {len(pending)}")

            total_coalesced = wq.get("coalesced_since_start", 0)
            by_reason = wq.get("coalesced_by_reason", {})
            if total_coalesced:
                from sqlcg.server.writer import (
                    COALESCE_COLLAPSED_INTO_PENDING_REINDEX,
                    COALESCE_REINDEX_DROPPED_INDEX_PENDING,
                    COALESCE_SUPERSEDED_BY_INDEX,
                )

                n_sup = by_reason.get(COALESCE_SUPERSEDED_BY_INDEX, 0)
                n_col = by_reason.get(COALESCE_COLLAPSED_INTO_PENDING_REINDEX, 0)
                n_drop = by_reason.get(COALESCE_REINDEX_DROPPED_INDEX_PENDING, 0)
                console.print(
                    f"  coalesced: {total_coalesced} "
                    f"(superseded_by_index={n_sup}, "
                    f"collapsed_into_pending_reindex={n_col}, "
                    f"reindex_dropped_index_pending={n_drop})"
                )
                last_at = wq.get("last_coalesce_at")
                last_reason = wq.get("last_coalesce_reason")
                if last_at and last_reason:
                    last_human = datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M:%S")
                    console.print(f"  last coalesce: {last_reason} at {last_human}")
            else:
                console.print("  coalesced: 0")

    except (FileNotFoundError, ConnectionRefusedError, OSError):
        # Socket unavailable — probe via PID file (R3: stale-socket fall-through)
        rec = read_pid()
        if rec and is_pid_alive(rec["pid"]):
            console.print_json(
                json.dumps(
                    {
                        "running": True,
                        "degraded": "socket unavailable",
                        "pid": rec["pid"],
                        "db_path": rec["db_path"],
                        # Cannot read the running version without a live socket —
                        # omit `version` and report drift as unknown (None → JSON
                        # null), never guess.
                        "stale_by_version": None,
                    }
                )
            )
        else:
            console.print_json(json.dumps({"running": False}))


def stop_server() -> bool:
    """Stop a running MCP server gracefully (best-effort). Returns True if stopped.

    Sends a ``stop`` op via the control socket; waits up to 5 s for the
    socket file to disappear (confirming clean exit).  Falls back to SIGTERM
    on the PID-file PID if the socket is unavailable.

    R3 (stale socket): ``ConnectionRefusedError`` / ``FileNotFoundError`` are
    caught — never hangs on a dead socket; a socket error here must not abort
    the caller (``mcp_stop`` and ``install_cmd`` both rely on this).

    Returns:
        True if a running server was found and stopped (via socket or SIGTERM
        fallback); False if no running server was found.
    """
    import socket as _socket
    import time

    from sqlcg.server.control import is_pid_alive, read_pid, sock_path

    sp = sock_path()
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(sp))
            s.sendall(json.dumps({"op": "stop"}).encode() + b"\n")
            s.recv(128)
        # Wait up to 5 s for the socket file to disappear (confirms clean exit)
        for _ in range(10):
            if not sp.exists():
                break
            time.sleep(0.5)
        console.print("[green]Server stopped.[/green]")
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        # Socket unavailable — fall back to SIGTERM via PID file
        import signal

        rec = read_pid()
        if rec and is_pid_alive(rec["pid"]):
            os.kill(rec["pid"], signal.SIGTERM)
            console.print(f"[yellow]Socket unavailable — sent SIGTERM to PID {rec['pid']}[/yellow]")
            return True
        return False


@app.command("stop")
def mcp_stop() -> None:
    """Stop the running MCP server gracefully.

    Delegates to ``stop_server()`` (shared with ``sqlcg install``) and prints
    a final message based on whether a server was found.
    """
    stopped = stop_server()
    if not stopped:
        console.print("[yellow]No server found to stop.[/yellow]")


@app.command("restart")
def mcp_restart() -> None:
    """Stop the server. Use only when the process is wedged.

    As of v1.20.0 the server self-heals automatically: on the next MCP tool
    call after a reinstall it detects the version skew and re-execs itself
    in place (same PID, same stdio pipe) — no editor reconnect needed.

    Manual restart is only needed when the process is unresponsive or you
    want to force an immediate pick-up of the new build without waiting for
    the next tool call.  In Claude Code run /mcp → reconnect sql-code-graph
    (or restart the session) after stopping.
    """
    mcp_stop()
    console.print(
        "[yellow]Server stopped.[/yellow] In Claude Code run [bold]/mcp[/bold] → reconnect "
        "[italic]sql-code-graph[/italic] (or restart the session) to pick up the new build."
    )
