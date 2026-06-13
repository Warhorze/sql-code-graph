"""Layer 2 — Markdown relative link existence guard.

Globs every relative markdown link ``](path)`` in the project's key docs and
asserts each linked path resolves on disk.  http(s):// and anchor-only ``#…``
links are ignored.

A red here means a file was renamed or moved but a doc still links the old
path.  Adding a new guide doc to the scanned set is a one-line change in
``_DOC_PATHS`` at the bottom of this file.

Guards the doc-tests-harness plan.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent  # repo root

# Matches ](path) but not ](http...) and not ](#anchor)
# Also handles ](path#anchor) — the path portion before # is what we check
_LINK_RE = re.compile(
    r"""
    \]          # closing bracket of link text
    \(          # opening paren
    (           # capture group: the href
        (?!http[s]?://)  # not an absolute URL
        (?!\#)           # not an anchor-only link
        [^)]+            # everything up to closing paren
    )
    \)
    """,
    re.VERBOSE,
)


def _scan_links(doc_path: Path) -> list[tuple[str, int]]:
    """Return [(raw_href, lineno)] for every relative link in doc_path."""
    if not doc_path.exists():
        return []
    results = []
    for lineno, line in enumerate(doc_path.read_text(encoding="utf-8").splitlines(), 1):
        for m in _LINK_RE.finditer(line):
            href = m.group(1).strip()
            # Strip trailing anchor fragment from path
            path_part = href.split("#")[0].strip()
            if path_part:  # not a bare anchor after stripping
                results.append((path_part, lineno))
    return results


def _resolve_link(href: str, doc_dir: Path) -> Path:
    """Resolve a relative link href against the directory containing the doc."""
    return (doc_dir / href).resolve()


# ---------------------------------------------------------------------------
# Docs to scan — add new guide docs here (one path per entry)
# ---------------------------------------------------------------------------

_DOC_PATHS: list[Path] = [
    _ROOT / "README.md",
    _ROOT / "CLAUDE.md",
    _ROOT / "ARCHITECTURE_REVIEW.md",
    _ROOT / "docs" / "cli.md",
    _ROOT / "docs" / "getting-started.md",
    _ROOT / "docs" / "releasing-pypi.md",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_key_doc_files_exist():
    """Every path in _DOC_PATHS itself exists on disk.

    Catches a misspelled or moved doc before the link-existence check runs.
    """
    missing = [p for p in _DOC_PATHS if not p.exists()]
    assert not missing, f"{len(missing)} declared doc path(s) do not exist:\n" + "\n".join(
        f"  {p}" for p in missing
    )


def test_relative_markdown_links_resolve():
    """Every relative markdown link in the scanned docs points to an existing path.

    Absolute URLs (http/https) and anchor-only links (#section) are skipped.
    A failure means a file was renamed/moved without updating the doc link.
    """
    failures: list[str] = []

    for doc_path in _DOC_PATHS:
        if not doc_path.exists():
            continue
        doc_dir = doc_path.parent
        for href, lineno in _scan_links(doc_path):
            resolved = _resolve_link(href, doc_dir)
            if not resolved.exists():
                failures.append(
                    f"{doc_path.relative_to(_ROOT)}:{lineno} broken link '{href}' → {resolved}"
                )

    assert not failures, f"{len(failures)} broken relative link(s) found:\n" + "\n".join(
        f"  {f}" for f in failures
    )


def test_link_scanner_extracts_links_and_skips_urls():
    """_scan_links finds relative links and ignores http(s) and anchor-only hrefs.

    Asserts observable output — actual link paths extracted — not just "no exception".
    """
    # Write a synthetic markdown snippet to a temp file
    import tempfile

    content = """\
See [base.py](src/sqlcg/parsers/base.py) for details.
Also [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md).
External: [sqlglot](https://github.com/tobymao/sqlglot).
Section: [Quick start](#quick-start).
With anchor: [plan](plan/WORKFLOW.md#section).
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp_path = Path(f.name)

    try:
        links = _scan_links(tmp_path)
        hrefs = [href for href, _ in links]
    finally:
        tmp_path.unlink(missing_ok=True)

    # Must find these relative links
    assert "src/sqlcg/parsers/base.py" in hrefs, f"Expected base.py link, got {hrefs}"
    assert "ARCHITECTURE_REVIEW.md" in hrefs, f"Expected ARCHITECTURE_REVIEW.md, got {hrefs}"
    # With-anchor path part only
    assert "plan/WORKFLOW.md" in hrefs, f"Expected plan/WORKFLOW.md (anchor stripped), got {hrefs}"
    # Must NOT include http URL or bare anchor
    assert not any(h.startswith("http") for h in hrefs), (
        f"http links should be filtered out, got {hrefs}"
    )
    assert "#quick-start" not in hrefs, "Bare anchors should be filtered out"
    assert "" not in hrefs, "Empty string should never appear in hrefs"
