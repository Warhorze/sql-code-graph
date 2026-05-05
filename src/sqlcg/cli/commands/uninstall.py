"""Uninstall sqlcg from Claude Code and clean up local resources."""

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

console = Console()

_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_SERVER_KEY = "sql-code-graph"


def uninstall_cmd(  # noqa: B008
    keep_db: bool = typer.Option(False, "--keep-db", help="Skip database deletion"),  # noqa: B008
    force: bool = typer.Option(  # noqa: B008
        False, "--force", help="Delete database without prompting; also delete metrics store"
    ),
    repo: Path = typer.Option(  # noqa: B008
        None, "--repo", help="Repository path for git hook removal (default: current directory)"
    ),
) -> None:
    """Uninstall sqlcg from Claude Code and optionally clean up resources.

    Step 1: Remove MCP registration from ~/.claude/settings.json
    Step 2: Optionally delete the KùzuDB graph database
    Step 3: Remove git hook sentinel block from .git/hooks/post-checkout
    """
    # Step 1: Remove MCP entry from settings.json
    _step1_remove_mcp_entry()

    # Step 2: Offer to delete the KùzuDB (unless --keep-db flag is set)
    if not keep_db:
        _step2_delete_database(force)
    else:
        db_path = _get_db_path()
        if db_path:
            console.print(f"[dim]Keeping database at {db_path}[/dim]")

    # Step 3: Remove git hook sentinel block
    repo_path = repo if repo else Path.cwd()
    _step3_remove_git_hook(repo_path)


def _step1_remove_mcp_entry() -> None:
    """Remove the 'sql-code-graph' entry from ~/.claude/settings.json."""
    settings_path = _SETTINGS_PATH

    if not settings_path.exists():
        # Create an empty settings if it doesn't exist
        settings = {}
    else:
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            console.print(f"[yellow]Warning:[/yellow] {settings_path} contains invalid JSON")
            settings = {}

    mcp_servers: dict = settings.get("mcpServers", {})

    if _SERVER_KEY not in mcp_servers:
        console.print("[yellow]MCP entry not found — already removed[/yellow]")
        return

    # Remove the entry
    del mcp_servers[_SERVER_KEY]
    settings["mcpServers"] = mcp_servers

    # Write back via .tmp + os.replace pattern
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)

    console.print("[green]Removed MCP registration from ~/.claude/settings.json[/green]")


def _step2_delete_database(force: bool) -> None:
    """Offer to delete the KùzuDB graph database."""
    db_path = _get_db_path()

    if not db_path:
        console.print("[dim]No database configured[/dim]")
        return

    db_path_obj = Path(db_path)

    # Check if it's a kuzu backend (not Neo4j)
    # If db_path is a directory or ends with standard kuzu patterns, it's likely kuzu
    # For now, we'll assume anything in .sqlcg/kuzu is kuzu
    if not _is_kuzu_backend(db_path):
        console.print("[dim]Database is not KùzuDB — skipping deletion[/dim]")
        return

    if not db_path_obj.exists():
        console.print(f"[dim]Database not found at {db_path}[/dim]")
        return

    # Prompt or force delete
    if force:
        should_delete = True
    else:
        should_delete = typer.confirm(
            f"This will delete the graph database at {db_path}. Continue?",
            default=False,
        )

    if not should_delete:
        console.print("[dim]Keeping database[/dim]")
        return

    # Delete the database directory
    try:
        shutil.rmtree(db_path_obj, ignore_errors=True)
        console.print(f"[green]Deleted graph database at {db_path}[/green]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to delete database: {e}")

    # If --force, also delete the metrics store
    if force:
        metrics_path = Path.home() / ".sqlcg" / "metrics.db"
        if metrics_path.exists():
            try:
                metrics_path.unlink()
                console.print("[green]Deleted metrics store[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning:[/yellow] Failed to delete metrics store: {e}")


def _step3_remove_git_hook(repo_path: Path) -> None:
    """Remove the git hook sentinel block from .git/hooks/post-checkout."""
    hook_file = repo_path / ".git" / "hooks" / "post-checkout"

    if not hook_file.exists():
        console.print(f"[yellow]No git hook found in {repo_path}[/yellow]")
        return

    # Read the file
    content = hook_file.read_text()

    # Strip the sentinel block: from "# sqlcg post-checkout hook" to the end of the block
    # The block ends when we encounter a line that doesn't start with whitespace/# or is empty
    # followed by non-empty content
    lines = content.split("\n")
    filtered_lines = []
    skip_mode = False

    for i, line in enumerate(lines):
        if "# sqlcg post-checkout hook" in line:
            skip_mode = True
            continue

        if skip_mode:
            # Skip all lines that are part of the hook block
            # The block extends from the sentinel comment until we hit an empty line
            # followed by non-hook content, or until the end of file
            if line.strip() == "":
                # Check if there's content after this blank line that's not the hook
                remaining = "\n".join(lines[i + 1 :]).strip()
                if remaining:
                    # There's content after this blank line, so end the skip mode
                    skip_mode = False
                    filtered_lines.append("")  # Preserve the blank line separator
                # else: blank line is at end of file, just skip it
            # else: continue skipping
            continue

        filtered_lines.append(line)

    # Reconstruct the content
    if filtered_lines:
        new_content = "\n".join(filtered_lines).strip() + "\n"
    else:
        new_content = ""

    if not new_content.strip():
        # File became empty, delete it
        try:
            hook_file.unlink()
            console.print(
                f"[green]Removed git hook from {repo_path}/.git/hooks/post-checkout[/green]"
            )
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to delete hook file: {e}")
    else:
        # Write back the filtered content
        try:
            hook_file.write_text(new_content)
            console.print(
                f"[green]Removed git hook from {repo_path}/.git/hooks/post-checkout[/green]"
            )
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to update hook file: {e}")


def _get_db_path() -> str | None:
    """Get the configured database path from environment or default."""
    db_path = os.getenv("SQLCG_DB_PATH")
    if db_path:
        return db_path

    # Default path for kuzu
    default_path = str(Path.home() / ".sqlcg" / "kuzu.db")
    return default_path if Path(default_path).exists() else None


def _is_kuzu_backend(db_path: str) -> bool:
    """Check if the database is a KùzuDB backend (not Neo4j)."""
    backend = os.getenv("SQLCG_BACKEND", "kuzu").lower()
    return backend in ("kuzu", "")  # Default to kuzu if unset
