"""Git integration commands for sqlcg."""

import shutil
import site
import sys
from pathlib import Path
from typing import NamedTuple

import typer
from rich.console import Console

console = Console()

app = typer.Typer(name="git", help="Git integration commands")


class _HookSpec(NamedTuple):
    filename: str
    sentinel: str
    script_template: str


# Hook script templates — use {sqlcg_bin} as the placeholder for the resolved binary.
# The sentinel comments (e.g. "# sqlcg post-checkout hook") must stay byte-for-byte
# unchanged so R9 idempotency is preserved: _install_single_hook matches them verbatim.
#
# Run-time binary resolution (#126 + #1): the hook no longer trusts the single embedded
# absolute literal to stay valid forever (a venv rebuild or tool upgrade makes the
# install-time path stale and the resync would then run a wrong/missing binary). Instead
# the shared _RESOLVE_PREAMBLE resolves the binary at *run time* with a hybrid order:
#   1. the preferred path rendered in at install time ({sqlcg_bin}, normally the stable
#      user-base binary from _resolve_sqlcg_bin) — the fast path, used if still executable;
#   2. `command -v sqlcg` on $PATH — covers uv-tool, user-base-on-PATH, and active-venv;
#   3. neither resolves → print a loud stderr error and exit non-zero (never silent).
# The shell never recomputes getuserbase() — the preferred path is rendered in by Python.
# It exports SQLCG_BIN for the hook body to invoke.
_RESOLVE_PREAMBLE = (
    'if [ -x "{sqlcg_bin}" ]; then\n'
    '  SQLCG_BIN="{sqlcg_bin}"\n'
    "elif command -v sqlcg >/dev/null 2>&1; then\n"
    '  SQLCG_BIN="$(command -v sqlcg)"\n'
    "else\n"
    "  echo \"sqlcg: cannot find the sqlcg binary (tried '{sqlcg_bin}' and \\$PATH)"
    " -- run 'sqlcg git install-hooks' to repoint this hook\" >&2\n"
    "  exit 1\n"
    "fi\n"
)

_HOOKS: list[_HookSpec] = [
    _HookSpec(
        filename="post-checkout",
        sentinel="# sqlcg post-checkout hook",
        script_template=(
            "#!/bin/sh\n"
            "# sqlcg post-checkout hook — incremental resync after branch switch\n"
            "# $3 == 1 means branch checkout (not file checkout); skip file checkouts\n"
            '[ "$3" = "1" ] || exit 0\n'
            + _RESOLVE_PREAMBLE
            + '"$SQLCG_BIN" reindex --from "$1" --to "$2"'
            ' "$(git rev-parse --show-toplevel)" --dialect auto --quiet --notify'
            ' || echo "sqlcg: graph not updated (reindex failed)'
            " -- run 'sqlcg mcp status'\" >&2\n"
        ),
    ),
    _HookSpec(
        filename="post-merge",
        sentinel="# sqlcg post-merge hook",
        script_template=(
            "#!/bin/sh\n"
            "# sqlcg post-merge hook — incremental resync after pull/merge\n"
            "# git sets ORIG_HEAD to the pre-merge HEAD; pass it as --from so --notify\n"
            "# can route through a running server (same path as post-checkout). If\n"
            "# ORIG_HEAD is unset (e.g. first-ever merge / gc'd), fall back to the\n"
            "# standalone stored-SHA delta (direct write).\n"
            + _RESOLVE_PREAMBLE
            + "PREV=$(git rev-parse --verify --quiet ORIG_HEAD)\n"
            "TOP=$(git rev-parse --show-toplevel)\n"
            'if [ -n "$PREV" ]; then\n'
            '  "$SQLCG_BIN" reindex --from "$PREV" --to HEAD "$TOP" --dialect auto'
            " --quiet --notify \\\n"
            '    || echo "sqlcg: graph not updated (reindex failed)'
            " -- run 'sqlcg mcp status'\" >&2\n"
            "else\n"
            '  "$SQLCG_BIN" reindex "$TOP" --dialect auto --quiet --notify \\\n'
            '    || echo "sqlcg: graph not updated (reindex failed)'
            " -- run 'sqlcg mcp status'\" >&2\n"
            "fi\n"
        ),
    ),
]


def _resolve_sqlcg_bin() -> str:
    """Resolve the absolute path of the installing sqlcg binary.

    Resolution order:
    0. Path(site.getuserbase()) / "bin" / "sqlcg" — stable user-installed PyPI binary.
       Preferred over the venv binary because it survives venv rebuilds.
    1. shutil.which("sqlcg") — the binary on the installer's $PATH (demoted fallback).
       Emits a warning if the resolved path is inside a virtualenv (.venv directory),
       as the hook may go stale if the venv is rebuilt.
    2. sys.argv[0] resolved via Path(...).resolve() if it ends in "sqlcg" and is executable.
    3. Bare "sqlcg" fallback — prints a warning so the user knows.

    Returns the resolved path string (absolute when resolvable, bare "sqlcg" otherwise).
    """
    # 0. Prefer the stable user-installed binary (survives venv rebuilds)
    user_bin = Path(site.getuserbase()) / "bin" / "sqlcg"
    if user_bin.is_file() and user_bin.stat().st_mode & 0o111:
        return str(user_bin)

    # 1. Try $PATH — demoted fallback (may resolve inside .venv on editable installs)
    which_result = shutil.which("sqlcg")
    if which_result:
        # Warn when the resolved binary is inside a virtualenv: the hook embeds this
        # path and will break if the venv is deleted and rebuilt.
        if "/.venv/" in which_result or "\\.venv\\" in which_result:
            console.print(
                "[yellow]Warning: resolved sqlcg binary is inside a virtualenv "
                f"('.venv'): {which_result}\n"
                "The installed git hook may go stale if the venv is rebuilt. "
                "Consider `uv tool install sqlcg` for a stable path.[/yellow]"
            )
        return which_result

    # 2. Try sys.argv[0] for python -m / editable-install invocations
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.name == "sqlcg" and argv0.is_file() and argv0.stat().st_mode & 0o111:
        return str(argv0)

    # 3. Bare fallback — still functional but relies on $PATH at hook-run time
    console.print(
        "[yellow]Warning: could not resolve the sqlcg binary path; the generated hooks "
        "will use bare 'sqlcg' and rely on $PATH at hook-run time.[/yellow]"
    )
    return "sqlcg"


def _install_single_hook(hooks_dir: Path, spec: _HookSpec, sqlcg_bin: str) -> None:
    """Install one git hook idempotently.

    If the hook file already contains the sentinel, it is already installed — skip silently.
    If the hook file exists without the sentinel, warn and print the script for manual append.
    Otherwise, write the hook file and set 0o755.
    """
    hook_path = hooks_dir / spec.filename
    script = spec.script_template.format(sqlcg_bin=sqlcg_bin)

    if hook_path.exists():
        existing_content = hook_path.read_text()
        if spec.sentinel in existing_content:
            if existing_content == script:
                # Byte-identical current template — true idempotency, silent skip.
                return
            # Sentinel present but content differs: sqlcg-owned but stale hook.
            # Overwrite with the current rendered template and report the upgrade.
            hook_path.write_text(script)
            hook_path.chmod(0o755)
            console.print(f"[green]Upgraded git hook:[/green] .git/hooks/{spec.filename}")
            return
        else:
            # Foreign hook without sqlcg sentinel
            console.print(
                f"[yellow]Warning: existing {spec.filename} hook found that was not created "
                "by sqlcg.[/yellow]"
            )
            console.print(
                f"[yellow]To integrate sqlcg, manually append the following to "
                f".git/hooks/{spec.filename}:[/yellow]"
            )
            console.print("")
            console.print("[cyan]" + script.rstrip() + "[/cyan]")
            return

    hook_path.write_text(script)
    hook_path.chmod(0o755)
    console.print(f"[green]Installed git hook:[/green] .git/hooks/{spec.filename}")


@app.command("install-hooks")
def install_hooks(
    repo: Path | None = typer.Option(  # noqa: B008
        None, "--repo", "-r", help="Path to git repository (default: current directory)"
    ),
) -> None:
    """Install git hooks for sqlcg integration.

    Writes a post-checkout hook that triggers incremental resync after branch switches
    and a post-merge hook that triggers resync after pulls/merges.
    Idempotent: running multiple times produces one hook entry per hook.
    The hooks embed the absolute path of the installing sqlcg binary so version skew
    between the installed binary and the hook command is avoided.
    """
    if repo is None:
        repo = Path.cwd()

    git_dir = repo / ".git"
    hooks_dir = git_dir / "hooks"

    if not git_dir.exists():
        console.print("[red]Error: not a git repository[/red]")
        raise typer.Exit(1)

    hooks_dir.mkdir(parents=True, exist_ok=True)

    sqlcg_bin = _resolve_sqlcg_bin()

    for spec in _HOOKS:
        _install_single_hook(hooks_dir, spec, sqlcg_bin)
