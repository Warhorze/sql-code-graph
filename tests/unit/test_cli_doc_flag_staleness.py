"""Layer 1 — CLI flag and command staleness guard.

Scans a declared set of documentation files for ``sqlcg <command...> --flag``
invocations and asserts every command path and flag exists in the live Click
registry.  A red here means either:

  * a command or flag was renamed but the doc was not updated, or
  * a flag typo was introduced in the source (e.g. ``---metrics-path``).

Adding a new guide doc is a one-line change: add its path to
``_DOC_PATHS`` at the bottom of this file.

Guards the doc-tests-harness feature.  Note: ``---metrics-path``
(three dashes) was the first real bug this harness caught — it is now fixed
in ``src/sqlcg/cli/commands/gain.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
from typer.main import get_command

from sqlcg.cli.main import app

# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent  # repo root


def build_cli_registry() -> dict[str, set[str]]:
    """Return {command_path: set_of_valid_flags} for every leaf command.

    Command paths use space-separated tokens matching how they appear in docs:
    ``"index"``, ``"analyze pr-impact"``, ``"db init"``, etc.

    Flags include both primary opts (``--flag``) and secondary opts (``-f``).
    """
    cli = get_command(app)
    registry: dict[str, set[str]] = {}

    def _recurse(group: click.Group, path: tuple[str, ...]) -> None:
        for name, cmd in group.commands.items():
            cmd_path = path + (name,)
            if isinstance(cmd, click.Group):
                _recurse(cmd, cmd_path)
            else:
                flags: set[str] = set()
                for param in cmd.params:
                    flags.update(getattr(param, "opts", []))
                    flags.update(getattr(param, "secondary_opts", []))
                registry[" ".join(cmd_path)] = flags

    _recurse(cli, ())
    return registry


# ---------------------------------------------------------------------------
# Doc scanner
# ---------------------------------------------------------------------------

# Match a flag token: --word or -w (single char or multi-char)
_FLAG_RE = re.compile(r"--?[\w][\w-]*")


def _extract_invocations(line: str, registry: dict[str, set[str]]) -> list[tuple[str, list[str]]]:
    """Return [(command_path, flags_used)] for each sqlcg invocation in a line.

    Uses the registry to greedily match the longest known command path starting
    at each occurrence of ``sqlcg``.  Only invocations where the command path
    exists in the registry are returned — prose mentions like "sqlcg is built on"
    produce no match because "is" is not a top-level command.
    """
    results = []
    # Find all positions where 'sqlcg' appears preceded by whitespace or backtick
    for m in re.finditer(r"(?:^|[\s`])sqlcg\s+(.*)", line):
        rest = m.group(1)
        # Tokenise: split on whitespace, collect non-flag, non-placeholder tokens
        # as candidate command path until we hit a flag, placeholder, or path-like arg
        # Strip trailing backticks and punctuation from each token
        tokens = [t.rstrip("`'\".,;)") for t in rest.split()]
        tokens = [t for t in tokens if t]  # drop any empty tokens
        cmd_parts: list[str] = []
        flags: list[str] = []
        i = 0
        cmd_path_locked = False  # True once we stop extending the command path
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                # Flag token — collect it; command path is now locked
                if _FLAG_RE.match(tok):
                    flags.append(tok)
                    cmd_path_locked = True
            elif not cmd_path_locked and _looks_like_command_token(tok):
                # Try extending the command path
                candidate = " ".join(cmd_parts + [tok])
                if _is_valid_prefix(candidate, registry):
                    cmd_parts.append(tok)
                else:
                    # Can't extend further — lock the path; this token is an arg
                    cmd_path_locked = True
            else:
                # Argument, placeholder, or path token — skip it
                # (stop extending cmd path but keep scanning for flags)
                cmd_path_locked = True
            i += 1

        if cmd_parts:
            full_path = " ".join(cmd_parts)
            if full_path in registry:
                results.append((full_path, flags))

    return results


def _looks_like_command_token(tok: str) -> bool:
    """Return True if tok looks like a CLI command word (not a path/placeholder)."""
    if tok.startswith(".") or tok.startswith("/"):
        return False
    if tok.startswith("<") or tok.startswith("["):
        return False
    # Must be a plain word with optional hyphens (like 'pr-impact', 'install-hooks')
    return bool(re.match(r"^[a-zA-Z][\w-]*$", tok))


def _is_valid_prefix(candidate: str, registry: dict[str, set[str]]) -> bool:
    """Return True if candidate equals or is a prefix of some known command path."""
    if candidate in registry:
        return True
    # Is it a prefix (sub-group) of any known path?
    prefix = candidate + " "
    return any(path.startswith(prefix) for path in registry)


def _scan_doc(path: Path, registry: dict[str, set[str]]) -> list[tuple[str, list[str], int]]:
    """Return [(command_path, flags, lineno)] from a doc file."""
    if not path.exists():
        return []
    results = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for cmd_path, flags in _extract_invocations(line, registry):
            results.append((cmd_path, flags, lineno))
    return results


# ---------------------------------------------------------------------------
# Docs to scan — add new guide docs here (one path per entry)
# ---------------------------------------------------------------------------

_DOC_PATHS: list[Path] = [
    _ROOT / "README.md",
    _ROOT / "CLAUDE.md",
    _ROOT / "ARCHITECTURE_REVIEW.md",
    _ROOT / "docs" / "cli.md",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_registry_is_non_empty():
    """build_cli_registry() returns at least one command.

    Guards that the registry helper itself works — a failed import or
    empty app would produce a vacuously passing flag test.
    """
    registry = build_cli_registry()
    assert len(registry) > 0, "Registry is empty — CLI import or traversal broke"
    # Spot-check a few known commands
    assert "gain" in registry
    assert "index" in registry
    assert "analyze pr-impact" in registry


def test_cli_registry_gain_has_two_dash_metrics_path():
    """gain command exposes --metrics-path (two dashes, not three).

    This is the first concrete bug this harness exists to catch: gain.py
    previously declared _metrics_path without a typer.Option(), causing Typer
    to auto-generate ``---metrics-path`` (three dashes).  A reintroduction
    would re-break this test.
    """
    registry = build_cli_registry()
    gain_flags = registry["gain"]

    assert "--metrics-path" in gain_flags, (
        f"--metrics-path missing from gain flags: {sorted(gain_flags)}"
    )
    assert "---metrics-path" not in gain_flags, (
        "---metrics-path (three dashes) found — the typo was reintroduced"
    )


def test_doc_commands_exist_in_registry():
    """Every ``sqlcg <cmd>`` invocation in scanned docs maps to a known command.

    A rename without a doc update causes this to fail with a specific
    ``(file, line, command_path)`` triple that identifies exactly what drifted.
    """
    registry = build_cli_registry()
    failures: list[str] = []

    for doc_path in _DOC_PATHS:
        for cmd_path, _flags, lineno in _scan_doc(doc_path, registry):
            if cmd_path not in registry:
                failures.append(
                    f"{doc_path.relative_to(_ROOT)}:{lineno} unknown command '{cmd_path}'"
                )

    assert not failures, f"{len(failures)} unknown command(s) found in docs:\n" + "\n".join(
        f"  {f}" for f in failures
    )


def test_doc_flags_exist_in_registry():
    """Every ``--flag`` used in a doc invocation is a valid flag for that command.

    A flag rename without a doc update causes this to fail with a specific
    ``(file, line, command_path, flag)`` that identifies exactly what drifted.
    """
    registry = build_cli_registry()
    failures: list[str] = []

    for doc_path in _DOC_PATHS:
        for cmd_path, flags, lineno in _scan_doc(doc_path, registry):
            valid_flags = registry.get(cmd_path, set())
            for flag in flags:
                if flag not in valid_flags:
                    failures.append(
                        f"{doc_path.relative_to(_ROOT)}:{lineno} "
                        f"command '{cmd_path}': unknown flag '{flag}' "
                        f"(valid: {sorted(valid_flags)})"
                    )

    assert not failures, f"{len(failures)} unknown flag(s) found in docs:\n" + "\n".join(
        f"  {f}" for f in failures
    )


def test_invocation_extractor_on_representative_lines():
    """_extract_invocations recognises real CLI patterns and ignores prose.

    Asserts observable output from the extractor helper — cmd+flags tuples.
    """
    registry = build_cli_registry()

    positive_samples = [
        # (input_line, expected_cmd_path, expected_flags_subset)
        ("sqlcg index ./sql --dialect snowflake", "index", ["--dialect"]),
        ("sqlcg db init", "db init", []),
        ("`sqlcg gain`", "gain", []),
        ("sqlcg analyze pr-impact --base <ref>", "analyze pr-impact", ["--base"]),
        ("sqlcg git install-hooks", "git install-hooks", []),
        ("sqlcg install --scope project", "install", ["--scope"]),
        ("sqlcg catalog load columns.csv", "catalog load", []),
    ]

    for line, expected_cmd, expected_flags in positive_samples:
        found = _extract_invocations(line, registry)
        assert found, f"No invocation parsed from: {line!r}"
        cmd_path, flags = found[0]
        assert cmd_path == expected_cmd, (
            f"Input {line!r}: expected cmd '{expected_cmd}', got '{cmd_path}'"
        )
        for flag in expected_flags:
            assert flag in flags, f"Input {line!r}: expected flag '{flag}' in {flags}"

    # Prose mentions that must NOT produce a match
    non_invocations = [
        "sqlcg is built on sqlglot",
        "sqlcg expands wildcards automatically",
        "sqlcg cannot parse procedural blocks",
    ]
    for line in non_invocations:
        found = _extract_invocations(line, registry)
        assert not found, f"Prose line should produce no match but got {found!r}: {line!r}"
