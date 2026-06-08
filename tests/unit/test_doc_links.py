"""Guard: every local markdown link in *maintained* docs points at a real file.

Catches the most common rot — a renamed/moved/deleted file leaving a dead link
behind. This is the enforcement mechanism for the project's markdown-link
convention (CLAUDE.md): links must use real paths so breakage is *visible*, and
this test makes that breakage *fail CI* rather than rely on a human spotting it.

Scope is deliberately limited to docs we keep current: top-level `*.md`
(README, CLAUDE, ARCHITECTURE_REVIEW) and `docs/`. The `plan/` tree and any
`*_ARCHIVE.md` are *frozen historical records* — their links described the repo
as it was when written, so policing them would force false-history edits (and
many targets are since-deleted files). They are intentionally excluded.

Matches inline markdown links `[text](target)` whose target is a local path
(not http(s)://, mailto:, or a pure #anchor). The `#fragment` is stripped
before checking file existence. It cannot catch a link that resolves to the
*wrong* file — only one that resolves to *nothing*.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Inline markdown links: [text](target). Skips images is unnecessary — an
# image with a dead local path is just as broken and worth flagging.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Directories excluded from the scan. `.claude` is gitignored (agent worktrees,
# progress files); `plan` is a frozen historical archive (see module docstring).
_SKIP_DIRS = {
    ".git",
    ".claude",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "plan",
}


def _iter_markdown_files() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.md")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
        # Frozen verbatim snapshots — their links reflect a past repo state.
        and not p.name.endswith("_ARCHIVE.md")
    ]


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "tel:", "ftp://"))


def _broken_links() -> list[str]:
    """Return human-readable descriptions of every broken local link found."""
    problems: list[str] = []
    for md in _iter_markdown_files():
        text = md.read_text(encoding="utf-8")
        rel_md = md.relative_to(REPO_ROOT)
        for match in _LINK_RE.finditer(text):
            target = match.group(1).strip()
            # Strip an optional title:  [x](path "title")
            target = target.split(" ", 1)[0]
            if not target or _is_external(target):
                continue
            # Drop the #anchor fragment — we validate the file, not the anchor.
            path_part = target.split("#", 1)[0]
            if not path_part:
                # Pure in-document anchor (e.g. [x](#section)); not a file link.
                continue
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                problems.append(f"{rel_md}: [{path_part}] -> missing")
    return problems


def test_no_broken_local_markdown_links() -> None:
    problems = _broken_links()
    assert not problems, "Broken local markdown links found:\n" + "\n".join(sorted(problems))
