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
    Step 4: Remove sqlcg skill directory from ~/.claude/skills/sqlcg/ and
            <repo>/.claude/skills/sqlcg/
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

    # Step 4: Remove sqlcg skill directory
    _step4_remove_skill(repo_path)


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


def _strip_sentinel_block(content: str, sentinel: str) -> str:
    """Strip the block introduced by sentinel from hook file content.

    The block starts at the sentinel line and extends until an empty line that is followed
    by non-empty content (end-of-block), or until the end of file.

    Returns the stripped content (may be empty if the sentinel was the only content).
    """
    lines = content.split("\n")
    filtered_lines = []
    skip_mode = False

    for i, line in enumerate(lines):
        if sentinel in line:
            skip_mode = True
            continue

        if skip_mode:
            if line.strip() == "":
                remaining = "\n".join(lines[i + 1 :]).strip()
                if remaining:
                    skip_mode = False
                    filtered_lines.append("")  # Preserve blank-line separator
                # else: trailing blank line — skip
            # else: continue skipping body lines
            continue

        filtered_lines.append(line)

    if filtered_lines:
        return "\n".join(filtered_lines).strip() + "\n"
    return ""


def _remove_single_hook(repo_path: Path, filename: str, sentinel: str) -> None:
    """Strip the sqlcg sentinel block from one git hook file.

    If the file becomes empty after stripping, delete it.
    If the file does not exist, emit a notice and return.
    """
    hook_file = repo_path / ".git" / "hooks" / filename

    if not hook_file.exists():
        console.print(f"[yellow]No {filename} hook found in {repo_path}[/yellow]")
        return

    content = hook_file.read_text()

    if sentinel not in content:
        # Nothing to strip
        return

    new_content = _strip_sentinel_block(content, sentinel)

    if not new_content.strip():
        try:
            hook_file.unlink()
            console.print(f"[green]Removed git hook from {repo_path}/.git/hooks/{filename}[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to delete hook file: {e}")
    else:
        try:
            hook_file.write_text(new_content)
            console.print(f"[green]Removed git hook from {repo_path}/.git/hooks/{filename}[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to update hook file: {e}")


# (filename, sentinel) pairs for all sqlcg-managed hooks
_HOOK_SENTINELS: list[tuple[str, str]] = [
    ("post-checkout", "# sqlcg post-checkout hook"),
    ("post-merge", "# sqlcg post-merge hook"),
]


def _step3_remove_git_hook(repo_path: Path) -> None:
    """Remove sqlcg sentinel blocks from all managed git hook files.

    Strips both post-checkout and post-merge hooks. Deletes a hook file if it
    becomes empty after stripping; preserves foreign content otherwise.
    """
    for filename, sentinel in _HOOK_SENTINELS:
        _remove_single_hook(repo_path, filename, sentinel)


def _get_db_path() -> str | None:
    """Get the configured database path from environment or default."""
    from sqlcg.core.config import KuzuConfig

    db_path = str(KuzuConfig.from_env().db_path)
    return db_path if Path(db_path).exists() else None


def _is_kuzu_backend(db_path: str) -> bool:
    """Check if the database is a KùzuDB backend (not Neo4j)."""
    backend = os.getenv("SQLCG_BACKEND", "kuzu").lower()
    return backend in ("kuzu", "")  # Default to kuzu if unset


# Candidate skill directory locations to remove (global first, then project-relative)
# Each entry is a callable(repo_path) -> Path resolving to the sqlcg skill dir.
_SKILL_DIR_TARGETS = [
    lambda repo_path: Path.home() / ".claude" / "skills" / "sqlcg",
    lambda repo_path: repo_path / ".claude" / "skills" / "sqlcg",
]


def _step4_remove_skill(repo_path: Path) -> None:
    """Remove the sqlcg-owned skill directory at all candidate locations.

    Iterates over the global (~/.claude/skills/sqlcg/) and project-relative
    (<repo>/.claude/skills/sqlcg/) directories. For each:
    - If the directory exists, removes it with shutil.rmtree (ignoring errors)
      and prints a green "Removed" notice.
    - If the directory does not exist, prints a yellow "not found" notice.

    Only the sqlcg/ subdirectory is ever removed — the parent skills/ dir and
    any sibling skill directories are left untouched.
    """
    any_found = False
    for target_fn in _SKILL_DIR_TARGETS:
        skill_dir = target_fn(repo_path)
        if skill_dir.exists():
            any_found = True
            shutil.rmtree(skill_dir, ignore_errors=True)
            console.print(f"[green]Removed skill directory:[/green] {skill_dir}")
        else:
            console.print(f"[yellow]Skill directory not found:[/yellow] {skill_dir}")

    if not any_found:
        console.print("[yellow]No skill directories found — nothing to remove.[/yellow]")
