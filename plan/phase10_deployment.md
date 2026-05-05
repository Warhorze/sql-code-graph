# Feature Plan: Phase 10 — Deployment & PyPI Publishing

## Summary

Package `sql-code-graph` for distribution on PyPI so that `pip install sql-code-graph`
(or `uvx sql-code-graph`) followed by `sqlcg install` registers the MCP server in
Claude Code's `~/.claude/settings.json` with zero manual JSON editing.

---

## Blocking Questions

None. All required decisions are resolved:

- PyPI package name: `sql-code-graph` (already in pyproject.toml, use it as-is)
- Author identity: `Warhorze` / `rademakerwesley@gmail.com`
- Install target: `~/.claude/settings.json` under `mcpServers` key
- Command detection: prefer `uvx` when available, fall back to `sqlcg`
- Build backend: `hatchling` (already configured)
- Publish mechanism: PyPI OIDC trusted publishing via `pypa/gh-action-pypi-publish`

**One operational pre-condition (not a code blocker):**
> Before the publish workflow can push to PyPI, the PyPI project must have the GitHub
> repository configured as a trusted publisher in the PyPI UI. See Step 5 (Release
> Process) for the exact setup sequence.

---

## Scope

### In Scope

- Fix `pyproject.toml` author and add `[project.urls]`
- New `sqlcg install` command that writes `~/.claude/settings.json`
- Fix `sqlcg mcp setup --write` to target `~/.claude/settings.json` (merge, not overwrite)
- GitHub Actions publish workflow triggered by `v*` tags
- `__version__` in `src/sqlcg/__init__.py` already exists — keep it in sync with
  `pyproject.toml` via commitizen (already configured with `version_provider = "pep621"`)
- Document exact release sequence

### Non-Goals

- Homebrew / conda / winget distribution
- Windows PATH detection beyond `shutil.which`
- Auto-upgrading an already-installed MCP server
- Setting up a new PyPI account (account already exists under `Warhorze`)
- Modifying any existing MCP tool behaviour

---

## Design

### API Changes

**New top-level command:**

```
sqlcg install
```

Flags:
- `--dry-run` (default: False) — print what would be written without modifying the file
- No other flags needed

**Changed command:**

```
sqlcg mcp setup [--print | --write]
```

`--write` now merges into `~/.claude/settings.json` under `mcpServers`
instead of overwriting `~/.claude/mcp.json`.

### Data Models

No new data models. The install command produces a JSON fragment:

```json
{
  "mcpServers": {
    "sql-code-graph": {
      "command": "uvx",
      "args": ["sql-code-graph", "mcp", "start"]
    }
  }
}
```

or, when `uvx` is not on PATH:

```json
{
  "mcpServers": {
    "sql-code-graph": {
      "command": "sqlcg",
      "args": ["mcp", "start"]
    }
  }
}
```

### Settings File Merge Strategy

Read `~/.claude/settings.json` → parse as JSON (tolerate missing file by starting from
`{}`) → deep-merge the `mcpServers` key → write back atomically (write to a `.tmp`
sibling, then `os.replace`). This prevents data loss if the process is interrupted
mid-write.

### Dependencies

No new runtime dependencies. All imports used in the install command are stdlib:
`json`, `os`, `pathlib`, `shutil`, `sys`.

---

## Implementation Steps

Steps are ordered lowest-risk-first. Each step is independently commit-able.

---

### Phase 10.1 — pyproject.toml metadata fixes

**Step 10.1.1**: Replace the author placeholder and add `[project.urls]`

Files affected:
- `pyproject.toml`

Exact changes:

```toml
# Replace:
authors = [{name = "Developer", email = "dev@example.com"}]

# With:
authors = [{name = "Warhorze", email = "rademakerwesley@gmail.com"}]
```

Add immediately after the `license` line (or after `authors`):

```toml
[project.urls]
Homepage = "https://github.com/Warhorze/sql-code-graph"
Repository = "https://github.com/Warhorze/sql-code-graph"
Issues = "https://github.com/Warhorze/sql-code-graph/issues"
Changelog = "https://github.com/Warhorze/sql-code-graph/blob/master/CHANGELOG.md"
```

Acceptance:
- `uv build` completes without warnings about missing metadata
- `python -c "import importlib.metadata; m=importlib.metadata.metadata('sql-code-graph'); print(m['Author-email'])"` prints `rademakerwesley@gmail.com`

---

### Phase 10.2 — Fix `sqlcg mcp setup --write`

**Step 10.2.1**: Rewrite the `--write` path in `src/sqlcg/cli/commands/mcp.py`

Current behaviour: writes `{"mcpServers": {...}}` to `~/.claude/mcp.json` (wrong path,
wrong merge strategy).

New behaviour:

1. Detect command: `shutil.which("uvx")` — if found, use `uvx`; else use `sqlcg`.
2. Build the server entry dict (see Design section above).
3. Read `~/.claude/settings.json` if it exists, parse as JSON; else start with `{}`.
4. Merge: `existing.setdefault("mcpServers", {})[\"sql-code-graph\"] = server_entry`
5. Write back atomically via a `.tmp` sibling + `os.replace`.
6. Print a confirmation message with the resolved path.

Files affected:
- `src/sqlcg/cli/commands/mcp.py`

Acceptance:
- `sqlcg mcp setup --write` creates `~/.claude/settings.json` if absent
- Running it twice does not duplicate the key
- Existing keys in `settings.json` are preserved
- `~/.claude/mcp.json` is NOT created or modified

---

### Phase 10.3 — New `sqlcg install` command

**Step 10.3.1**: Create `src/sqlcg/cli/commands/install.py`

```python
"""sqlcg install — register the MCP server in Claude Code."""

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

console = Console()

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
SERVER_KEY = "sql-code-graph"


def _build_server_entry() -> dict:
    if shutil.which("uvx"):
        return {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
    return {"command": "sqlcg", "args": ["mcp", "start"]}


def _detect_claude_code() -> bool:
    """Return True if Claude Code appears to be installed."""
    return SETTINGS_PATH.exists() or SETTINGS_PATH.parent.exists()


def _merge_settings(settings: dict, entry: dict) -> dict:
    servers = settings.setdefault("mcpServers", {})
    servers[SERVER_KEY] = entry
    return settings


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing"),
) -> None:
    """Register sql-code-graph as an MCP server in Claude Code."""
    if not _detect_claude_code():
        console.print(
            "[yellow]Warning:[/yellow] ~/.claude/ directory not found. "
            "Claude Code may not be installed. Proceeding anyway."
        )

    entry = _build_server_entry()

    # Read existing settings
    existing: dict = {}
    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            console.print(
                f"[yellow]Warning:[/yellow] {SETTINGS_PATH} contains invalid JSON. "
                "A fresh mcpServers key will be added."
            )

    # Idempotency check
    if existing.get("mcpServers", {}).get(SERVER_KEY) == entry:
        console.print(f"[green]Already configured:[/green] {SERVER_KEY} in {SETTINGS_PATH}")
        raise typer.Exit(0)

    updated = _merge_settings(existing, entry)

    if dry_run:
        console.print("[bold]Dry run — would write:[/bold]")
        console.print_json(json.dumps(updated, indent=2))
        raise typer.Exit(0)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(SETTINGS_PATH, updated)

    cmd_display = (
        f"uvx {' '.join(entry['args'])}"
        if entry["command"] == "uvx"
        else f"sqlcg {' '.join(entry['args'])}"
    )
    console.print(
        f"[green]Registered[/green] {SERVER_KEY} in {SETTINGS_PATH}\n"
        f"  Command: {cmd_display}\n"
        f"  Restart Claude Code to activate."
    )
```

**Step 10.3.2**: Register `install_cmd` in `src/sqlcg/cli/main.py`

Add import:
```python
from sqlcg.cli.commands import analyze, db, find, gain, git, index, install, mcp, report, watch
```

Add registration after the existing `app.command("report")(report.report_cmd)` line:
```python
app.command("install")(install.install_cmd)
```

Acceptance:
- `sqlcg install --help` prints the command description
- `sqlcg install --dry-run` prints the JSON that would be written, exits 0, does not create file
- `sqlcg install` on a machine without `~/.claude/` creates the directory and file
- Running `sqlcg install` twice: second run prints "Already configured" and exits 0
- Pre-existing keys in `settings.json` are preserved after `sqlcg install`
- If `uvx` is on PATH, the entry uses `"command": "uvx"` with `"args": ["sql-code-graph", "mcp", "start"]`
- If `uvx` is not on PATH, the entry uses `"command": "sqlcg"` with `"args": ["mcp", "start"]`

---

### Phase 10.4 — Unit tests

**Step 10.4.1**: Create `tests/unit/test_install.py`

Test cases (all use `tmp_path` fixture; never touch real `~/.claude/`):

1. `test_build_server_entry_uvx_available` — mock `shutil.which` to return a path for `uvx`; assert `command == "uvx"` and `args == ["sql-code-graph", "mcp", "start"]`
2. `test_build_server_entry_no_uvx` — mock `shutil.which` to return `None`; assert `command == "sqlcg"` and `args == ["mcp", "start"]`
3. `test_install_creates_settings_file` — call `install_cmd` with a monkeypatched `SETTINGS_PATH` pointing at `tmp_path/settings.json`; assert file is created with correct `mcpServers` key
4. `test_install_merges_existing_settings` — write a pre-existing `settings.json` with a `theme` key; after install, assert both `theme` and `mcpServers` are present
5. `test_install_idempotent` — run install twice; assert file content is identical after both runs; assert second run prints "Already configured"
6. `test_install_dry_run_no_file_created` — run with `--dry-run`; assert the file is not created
7. `test_install_warns_on_missing_claude_dir` — point `SETTINGS_PATH` at a nonexistent parent; assert warning is printed but install proceeds
8. `test_install_handles_invalid_json` — write `{invalid` to the settings file; assert install does not raise, writes a valid file
9. `test_mcp_setup_write_merges_into_settings_json` — call `mcp_setup(print_only=False)` with monkeypatched `SETTINGS_PATH`; assert correct path written, existing keys preserved
10. `test_mcp_setup_write_does_not_create_mcp_json` — after `mcp_setup(print_only=False)`, assert `~/.claude/mcp.json` does not exist (use tmp_path so this is safe)

Minimum: 10 tests. All use `tmp_path`; none touch the real home directory.

---

### Phase 10.5 — GitHub Actions publish workflow

**Step 10.5.1**: Create `.github/workflows/publish.yml`

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*"

jobs:
  test:
    uses: ./.github/workflows/test.yml

  publish:
    needs: test
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write   # required for OIDC trusted publishing

    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Build wheel and sdist
        run: uv build

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        # No api-token needed — uses OIDC trusted publishing
```

Notes on workflow design:
- `needs: test` gates publishing behind the full test matrix; if any test job fails, the
  publish job is skipped.
- `environment: pypi` maps to a GitHub Actions environment (create one named `pypi` in
  the repo settings). The environment can be configured to require a manual approval gate
  before publishing.
- `id-token: write` permission is mandatory for the OIDC flow; it grants the workflow a
  short-lived token that PyPI verifies against the registered trusted publisher.
- `uv build` produces `dist/sql_code_graph-*.whl` and `dist/sql_code_graph-*.tar.gz`.

**Blocker (operational, not a code change):**

Before pushing the first `v*` tag, the PyPI trusted publisher must be configured:

1. Go to https://pypi.org/manage/account/publishing/ (logged in as `Warhorze`)
2. Add a new publisher:
   - PyPI project name: `sql-code-graph`
   - GitHub owner: `Warhorze`
   - GitHub repository: `sql-code-graph`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
3. Create the package on PyPI first by publishing once (the OIDC publisher registers
   against an existing project name; it cannot create a new project). For the first
   publish, use `uv publish --token <api-token>` locally, then switch to OIDC for all
   subsequent releases.

---

### Phase 10.6 — Release process documentation

The exact sequence for cutting a release:

**Step 10.6.1**: Bump version

- The `pyproject.toml` `[tool.commitizen]` config uses `version_provider = "pep621"`.
  Run: `uvx commitizen bump` — this updates `pyproject.toml` version, generates a
  CHANGELOG entry, and creates a git tag.
- Commitizen does NOT automatically update `src/sqlcg/__init__.py`. Add a
  `version_files` entry to `[tool.commitizen]` so both are bumped atomically:

```toml
[tool.commitizen]
name = "cz_conventional_commits"
version = "0.1.0"
version_provider = "pep621"
tag_format = "v$version"
update_changelog_on_bump = true
version_files = [
    "pyproject.toml:version",
    "src/sqlcg/__init__.py:__version__",
]
```

**Step 10.6.2**: Release sequence (run locally)

```bash
# 1. Ensure you are on master and it is clean
git checkout master && git pull origin master

# 2. Bump version (updates pyproject.toml + __init__.py + CHANGELOG, creates commit + tag)
uvx commitizen bump

# 3. Push commit and tag
git push origin master --tags

# 4. GitHub Actions detects the v* tag → runs test matrix → publishes to PyPI
```

**Step 10.6.3**: User installation after publish

```bash
# Option A — run without installing (recommended; uses cached wheel)
uvx sql-code-graph install

# Option B — install globally then register
pip install sql-code-graph
sqlcg install
```

Both options result in the `mcpServers` entry being written to `~/.claude/settings.json`.
The user then restarts Claude Code to activate the server.

---

## Test Strategy

### Unit tests

All in `tests/unit/test_install.py`:
- Cover both uvx-available and uvx-absent command selection
- Cover create, merge, idempotency, dry-run, invalid JSON, and missing directory scenarios
- Use `tmp_path` exclusively — never touch real `~/.claude/`
- Use `monkeypatch` to override `shutil.which` and `install.SETTINGS_PATH`

### Integration / smoke test

Not required for Phase 10. The acceptance criteria are fully exercisable in unit tests.
The end-to-end path (push tag → PyPI publish → `uvx install`) is verified manually on
the first release.

### CI gate

The publish workflow has `needs: test` which runs the existing `test.yml` matrix
(Python 3.12 and 3.13, unit + integration, pyright, ruff) before any publish step.
This ensures no broken release is ever published.

---

## Acceptance Criteria

- [ ] `pyproject.toml` `authors` field is `[{name = "Warhorze", email = "rademakerwesley@gmail.com"}]`
- [ ] `pyproject.toml` has a `[project.urls]` section with Homepage, Repository, Issues, Changelog
- [ ] `uv build` produces a wheel and sdist with no warnings
- [ ] `sqlcg install --help` works and describes the command
- [ ] `sqlcg install` writes `~/.claude/settings.json` with the correct `mcpServers` entry (using uvx when available)
- [ ] Running `sqlcg install` twice prints "Already configured" on the second invocation and exits 0
- [ ] `sqlcg install` preserves pre-existing keys in `settings.json`
- [ ] `sqlcg install --dry-run` prints the JSON without creating or modifying any file
- [ ] `sqlcg mcp setup --write` writes to `~/.claude/settings.json` (not `~/.claude/mcp.json`) and merges correctly
- [ ] `.github/workflows/publish.yml` exists and triggers on `v*` tags
- [ ] `publish.yml` has `needs: test` gate
- [ ] `publish.yml` uses OIDC trusted publishing (no hardcoded API token)
- [ ] `[tool.commitizen]` `version_files` includes `src/sqlcg/__init__.py:__version__`
- [ ] All 10+ unit tests in `test_install.py` pass
- [ ] All pre-existing tests continue to pass (no regressions)

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PyPI trusted publisher not configured before first tag push | HIGH | Publish fails silently | Document the PyPI UI setup as a blocking pre-condition in Step 10.5.1; perform the one-time local publish first |
| Package name `sql-code-graph` already taken on PyPI | LOW | First publish fails | Check https://pypi.org/project/sql-code-graph/ before tagging; the name appears to be available but confirm before Step 10.6.2 |
| `~/.claude/settings.json` has unexpected schema (e.g. Claude Desktop vs Claude Code) | MEDIUM | Key written in wrong location | The `mcpServers` key is the same for both Claude Desktop and Claude Code settings files; risk is low |
| Commitizen `version_files` regex mismatch for `__version__` | LOW | Version not updated in `__init__.py` | Test with `uvx commitizen bump --dry-run` before cutting first release |
| `uv build` includes unintended files in sdist | LOW | Package larger than expected | Add `[tool.hatch.build.targets.sdist] exclude` patterns if needed after first build inspection |
| `uvx` resolved at install time but not at runtime | LOW | Wrong command written if environment changes | The install command resolves `uvx` at the moment `sqlcg install` is run, which is the correct behaviour; documented in help text |

---

## Implementation Order Summary

Lowest risk first:

1. **Step 10.1.1** — `pyproject.toml` metadata (pure metadata, zero code risk)
2. **Step 10.1.2** — commitizen `version_files` (one-line toml addition)
3. **Step 10.2.1** — Fix `mcp setup --write` (isolated to one function, no new file)
4. **Step 10.3.1** — Create `install.py` (new file, no existing code changed)
5. **Step 10.3.2** — Register in `main.py` (one import line + one command registration)
6. **Step 10.4.1** — Write `test_install.py` (test-only, safe)
7. **Step 10.5.1** — Create `publish.yml` (CI only, no code change)
8. **Operational pre-condition** — Configure PyPI trusted publisher in PyPI UI (before pushing v* tag)
9. **Step 10.6** — Cut first release with `uvx commitizen bump && git push --tags`
