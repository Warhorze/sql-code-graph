# Feature Plan: F2 — Bundle a Claude Skill in the `sqlcg` Install

Plan date: 2026-05-30
Author: architect-planner (hardened from [`plan/v1_living_codebase_and_skill.md`](v1_living_codebase_and_skill.md) § "Feature 2 — Bundle a Claude Skill in the Install")
Branch: `feat/sprint-10-anchor-tools`
Source authority: User feature request (road to v1, F2); v1 draft Feature 2; [`CLAUDE.md`](../CLAUDE.md) invariants; trust-layer `Judgement` model (commits `943a1da`/`95670ab`).

---

## Summary

Make `sqlcg install` provision a **Claude skill** — a `SKILL.md` file with YAML
frontmatter — that teaches Claude *when and how* to call each `sqlcg` MCP tool for
best lineage results, and that encodes the trust-layer **fact vs. heuristic** boundary.
`sqlcg uninstall` removes it. The user chooses the install location (project-level
`.claude/skills/` vs. global `~/.claude/skills/`).

---

## Grounding-Verified (real symbols / line numbers — read before implementing)

| Fact | Source | Symbol / line |
|------|--------|---------------|
| Install registers MCP server in `~/.claude/settings.json`; has `--dry-run`; uses `.tmp`+`os.replace` atomic write; idempotent (compares existing entry) | [`install.py`](../src/sqlcg/cli/commands/install.py) | `install_cmd` L17; `_SETTINGS_PATH` L13; atomic write L69-72; idempotent check L44-47 |
| Uninstall runs 3 steps; Step 3 strips git-hook sentinel blocks via `(filename, sentinel)` table | [`uninstall.py`](../src/sqlcg/cli/commands/uninstall.py) | `uninstall_cmd` L17; `_step3_remove_git_hook` L208; `_HOOK_SENTINELS` L202; `_strip_sentinel_block` L133 |
| Idempotent install idiom: per-item spec table, "sentinel already present → skip silently; foreign file → warn, do not clobber" | [`git.py`](../src/sqlcg/cli/commands/git.py) | `_HookSpec` namedtuple; `_install_single_hook` (sentinel check + skip + foreign-file warn); `_HOOKS` table |
| MCP tools registered via `@mcp.tool()` — tool name = function name (FastMCP default; `_timed_tool` preserves it with `functools.wraps`) | [`tools.py`](../src/sqlcg/server/tools.py) | `@mcp.tool()` L388; `_timed_tool` L362 (`functools.wraps` L370); `mcp = FastMCP(...)` in [`server.py`](../src/sqlcg/server/server.py) L32 |
| `Judgement` model: `assertion_type ∈ {"fact","heuristic"}`; fact MUST NOT carry confidence/reason; heuristic MUST | [`models.py`](../src/sqlcg/server/models.py) | `Judgement` L8; validator `_check_invariants` L38-46 |
| `analyze_unused` → `UnusedTablesResult`; each `UnusedCandidate.dead_code` is a **heuristic** (`confidence=0.5`, may be consumed externally) | [`models.py`](../src/sqlcg/server/models.py) | `UnusedCandidate` L364; `dead_code: Judgement` L372 |
| `get_hub_ranking` → `HubRankingResult` — **all fields are facts; no Judgement field** | [`models.py`](../src/sqlcg/server/models.py) | `HubRankingResult` L406 ("no Judgement field is present" L410) |
| `get_change_scope`/`scope_change` carry a `risk: Judgement` (heuristic) but their counts/lists are facts | [`models.py`](../src/sqlcg/server/models.py) | `ChangeScopeResult.risk` L259; `ScopeChangeResult.risk` L350 |
| CLI wiring: single commands registered on `app` in `main.py`; `install`/`uninstall` already wired | [`main.py`](../src/sqlcg/cli/main.py) | L46-47 |
| Path/constant owner is `KuzuConfig`; default db `~/.sqlcg/graph.db` | [`config.py`](../src/sqlcg/core/config.py) | `KuzuConfig.db_path` L17; `from_env` L24 |
| Package version single source | [`__init__.py`](../src/sqlcg/__init__.py) | `__version__ = "0.3.1"` L3 |
| Wheel packaging: `packages = ["src/sqlcg"]` (hatchling) | [`pyproject.toml`](../pyproject.toml) | `[tool.hatch.build.targets.wheel]` L89 |

### Confirmed full MCP tool registry (16 tools — grep-confirmed, do not invent names)

> **plan-reviewer correction (B1)**: `db_info` is NOT decorated with `@mcp.tool()` in
> [`tools.py`](../src/sqlcg/server/tools.py) (L1263 has only `@_timed_tool("db_info")`
> with no `@mcp.tool()` above it). The FastMCP runtime confirms 16 tools via
> `mcp._tool_manager._tools`. All plan references corrected from 17 to 16;
> `db_info` removed from the registry list.

Lineage / dependency **facts**: `trace_column_lineage`, `find_table_usages`,
`find_definition`, `get_downstream_dependencies`, `get_upstream_dependencies`,
`get_backfill_order`, `diff_impact`, `search_sql_pattern`, `list_dialects_and_repos`,
`get_hub_ranking`.

**Fact + embedded heuristic** (`risk: Judgement`): `get_change_scope`, `scope_change`.

**Heuristic-bearing** (`dead_code: Judgement`, confidence=0.5): `analyze_unused`.

Operational: `index_repo`, `execute_cypher`, `submit_feedback`.

---

## Open-Decision Resolutions (defaults chosen with rationale — not left open)

### D1 — Skill content: **generated scaffold + curated prose** (chosen)
- **Rejected — fully generated from registry**: drift-proof but produces robotic,
  low-signal prose; cannot express the cross-tool *workflow* ("scope a change → read
  authoritative files → backfill order") that makes the skill valuable.
- **Rejected — fully hand-written**: highest quality but violates the CLAUDE.md
  anti-drift rule ("keep the skill in sync with the actual tool set… so it can't
  drift"). A tool rename would silently rot the skill.
- **Chosen — hybrid**: a generator reads the live registry (the FastMCP tool
  set) and emits a **mechanically-derived tool table** (name + one-line purpose +
  `fact`/`heuristic` label sourced from the return-model's `Judgement` fields), then
  splices it between curated prose sections (frontmatter, philosophy, the
  fact/heuristic contract, recommended workflows). **Drift guard**: a unit test
  asserts every `@mcp.tool()` name appears in the generated skill and the skill names
  no tool that is not registered. The curated prose is a constant; the table is
  generated at install time from the imported `mcp` instance.
- **Single source of truth for the fact/heuristic label**: the return-type model's
  fields, not a hand-maintained list. `analyze_unused` and the two `*scope*` tools are
  labelled heuristic-bearing because their models contain a `Judgement`; `get_hub_ranking`
  is labelled pure-fact because its model has none. The generator inspects a small
  explicit `TOOL_RETURN_MODELS` map (tool name → model class) and checks for a
  `Judgement`-typed field — see Step 2.1. This keeps the boundary derived from the
  actual model surface, matching the trust-layer contract.

### D2 — Surfacing the location choice: **`--scope` flag, with interactive prompt as fallback** (chosen)
- The user's locked decision is "location is a user choice — the tool does not decide."
- **Chosen**: `sqlcg install --scope {project|global}`. When `--scope` is omitted **and**
  stdin is a TTY, prompt interactively (`typer.prompt`/`typer.confirm`) for project vs.
  global. When `--scope` is omitted and stdin is **not** a TTY (CI, scripted install),
  **fail with a clear error** instructing the user to pass `--scope` — never silently
  pick a default location, because picking wrong writes a team-shared file the user did
  not intend. This honours "the tool does not decide" in both interactive and scripted modes.
- `project` → `<repo>/.claude/skills/sqlcg/SKILL.md` (repo defaults to `Path.cwd()`,
  overridable via the new `--repo` option added in Step 3.2).
- `global` → `~/.claude/skills/sqlcg/SKILL.md`.

### D3 — Shipping & versioning: **generated at install time; stamped with `__version__`** (chosen)
- **Rejected — ship a static `SKILL.md` data file inside the wheel and copy it**:
  would require `force-include` packaging config and a separate file to keep in sync;
  re-introduces drift between the packaged file and the live tool set.
- **Chosen**: the skill is **generated at install time** from the in-process tool
  registry + curated prose constants (both already inside `src/sqlcg`, so they ship with
  the wheel automatically under `packages = ["src/sqlcg"]` — no packaging change needed).
  The generated `SKILL.md` frontmatter carries `version: <sqlcg.__version__>` (single
  source, [`__init__.py`](../src/sqlcg/__init__.py) L3). Re-install regenerates and
  overwrites the sqlcg-owned skill file, so the skill tracks the installed version with
  zero manual upkeep. No backward-compat shim (CLAUDE.md): regeneration is the migration.

---

## Scope

### In Scope
- A skill-rendering module that produces `SKILL.md` text (frontmatter + curated prose +
  generated tool table + fact/heuristic contract) from the live MCP registry.
- `sqlcg install` provisions the skill at a user-chosen location (`--scope` + TTY prompt).
- `sqlcg uninstall` removes the sqlcg-owned skill directory (mirroring the Step-3 pattern).
- Idempotent re-install (overwrite sqlcg-owned file; never clobber a foreign `skills/`).
- Drift-guard unit test pinning skill ↔ registry parity.

### Non-Goals
- No change to the MCP tool set, tool behaviour, or the `Judgement` model.
- No change to F1 git-hook install/uninstall (only mirror its *idiom*).
- No `--dry-run` rendering of the skill beyond what `install --dry-run` already prints
  (a one-line note that the skill would be written; full body printing is optional, not required).
- No multi-skill or per-repo skill customization; one canonical sqlcg skill.
- No auto-regeneration on tool changes outside of install (re-install is the path).

---

## Design

### New module: `src/sqlcg/server/skill.py`
Owns skill rendering. Pure functions, no I/O, so it is unit-testable without touching disk.

```
# constants (curated prose — the single source for hand-written sections)
SKILL_NAME = "sqlcg"
_FRONTMATTER_DESCRIPTION = "..."        # what the skill is for
_PHILOSOPHY = "..."                     # facts vs heuristics contract prose
_WORKFLOWS = "..."                      # recommended cross-tool workflows

# explicit tool→return-model map (single source for the fact/heuristic label)
TOOL_RETURN_MODELS: dict[str, type[BaseModel]] = {
    "trace_column_lineage": LineageResult,
    ... all 17 ...
}

def _tool_is_heuristic(model: type[BaseModel]) -> bool:
    """True iff any field of `model` is (or contains) a Judgement — derived, not hand-listed."""

def list_registered_tools() -> list[str]:
    """Tool names from the live FastMCP registry (import `mcp` from server.server)."""

def render_skill(version: str) -> str:
    """Return the full SKILL.md text: frontmatter(version) + prose + generated table."""
```

`render_skill` is the **single entry point** the installer calls — grep-confirmed call
site requirement satisfied by Step 3.1 invoking it.

### Skill file layout (written by install)
```
<scope-root>/.claude/skills/sqlcg/SKILL.md
```
- `scope-root` = `<repo>` (project) or `Path.home()` (global).
- The directory `…/skills/sqlcg/` is **sqlcg-owned**: uninstall removes this directory,
  never the parent `skills/` (which may hold foreign skills).

### SKILL.md content contract (what `render_skill` MUST emit)
- **Frontmatter** (YAML): `name: sqlcg`, `description: <…>`, `version: <__version__>`.
- **Fact/heuristic contract section** — states verbatim: lineage/dependency tools and
  `get_hub_ranking` return **facts**; `analyze_unused`'s `dead_code` and the `risk` on
  `get_change_scope`/`scope_change` are **heuristics** carrying `confidence` + `reason`
  and MUST NOT be reported to the user as fact.
- **Tool table** (generated): every registered tool, its one-line purpose, and a
  `fact` / `heuristic` tag from `_tool_is_heuristic`.
- **Workflow section** (curated): e.g. "Before changing a table: call `scope_change` →
  read `authoritative_files` → follow `backfill_order`; treat `risk` as a heuristic."

### Constants / fallbacks
- No new path constant duplicates `KuzuConfig`. The skill location is derived from
  `Path.home()` / the repo path only; it does not touch the db path. (No `KuzuConfig`
  fallback is in play here — documented so the developer does not invent one.)
- Version string sourced **only** from `sqlcg.__version__`.

---

## Implementation Steps

### Phase 1 — Skill renderer (pure, no I/O)

**Step 1.1**: Create [`src/sqlcg/server/skill.py`](../src/sqlcg/server/skill.py) with the
curated-prose constants, `SKILL_NAME`, and `render_skill(version)` returning the full
frontmatter + prose body (tool table stubbed empty for now, filled in 2.1).
- Files affected: `src/sqlcg/server/skill.py` (new).
- Acceptance: `render_skill("0.3.1")` returns a string that **starts with** `---\n`,
  contains `name: sqlcg`, `version: 0.3.1`, and a literal "facts" and "heuristics"
  section header. Asserted in unit test (observable string content, not "no raise").

### Phase 2 — Tool table generated from the live registry

**Step 2.1**: Add `TOOL_RETURN_MODELS` (all 16 tools → their return model), `list_registered_tools()`
(reads names from the imported `mcp` FastMCP instance), `_tool_is_heuristic(model)`
(inspects model fields for a `Judgement`-typed field), and splice the generated table
into `render_skill`.
- Files affected: `src/sqlcg/server/skill.py`.
- Acceptance (drift guard): a unit test asserts
  (a) every name in `list_registered_tools()` appears in `render_skill(...)` output;
  (b) the skill references no tool name absent from the registry;
  (c) `analyze_unused`, `get_change_scope`, `scope_change` are tagged `heuristic`;
  `get_hub_ranking`, `trace_column_lineage`, `find_definition` are tagged `fact`.

### Phase 3 — Install provisions the skill

**Step 3.1**: Extend [`install.py`](../src/sqlcg/cli/commands/install.py): add `--scope`
option (`project`/`global`), TTY-prompt fallback, non-TTY-without-scope error; a helper
`_provision_skill(scope, repo, dry_run)` that resolves the target dir, calls
`render_skill(sqlcg.__version__)`, and writes `…/skills/sqlcg/SKILL.md` via the existing
`.tmp`+`os.replace` atomic idiom. Call `_provision_skill` from `install_cmd` after MCP
registration. Idempotent: overwrite the sqlcg-owned file; if a non-sqlcg `SKILL.md`
exists at that exact path (no sqlcg frontmatter marker), warn and skip — mirror
[`git.py`](../src/sqlcg/cli/commands/git.py) `_install_single_hook` foreign-file behaviour.
- Files affected: `src/sqlcg/cli/commands/install.py`.
- Acceptance: with `fake_home` (monkeypatched, per [`test_install.py`](../tests/unit/test_install.py) L31)
  `install --scope global` writes `~/.claude/skills/sqlcg/SKILL.md`; the file exists,
  starts with `---`, and contains all 17 tool names. Re-running `install --scope global`
  leaves exactly one file with identical content (idempotent). `install --dry-run --scope global`
  writes **nothing** and prints a note that the skill would be written.

**Step 3.2**: Add a `--repo` option to `install_cmd` so `project` scope resolves against
the right repo root (default `Path.cwd()`), consistent with how `uninstall --repo`
resolves the repo. **Note (plan-reviewer W1)**: `install.py` currently has NO `--repo`
parameter — it must be added as a new `typer.Option`. The prior wording "any existing
repo/path option" was incorrect; there is no such option on `install_cmd` today.
- Files affected: `src/sqlcg/cli/commands/install.py`.
- Acceptance: `install --scope project --repo <tmp>` writes `<tmp>/.claude/skills/sqlcg/SKILL.md`.

### Phase 4 — Uninstall removes the skill

**Step 4.1**: Add `_step4_remove_skill(repo_path)` to [`uninstall.py`](../src/sqlcg/cli/commands/uninstall.py),
called from `uninstall_cmd` after Step 3. It removes the **sqlcg-owned** skill directory
at **both** candidate locations checked in order — `~/.claude/skills/sqlcg/` and
`<repo>/.claude/skills/sqlcg/` — using `shutil.rmtree(..., ignore_errors=True)` only on
the `…/skills/sqlcg/` directory (never the parent). Emit the same green "Removed …" /
yellow "not found" notices as Step 3. Mirror the `_HOOK_SENTINELS`-style "iterate over a
small list of targets" idiom.
- Files affected: `src/sqlcg/cli/commands/uninstall.py`.
- Acceptance: after `install --scope global` then `uninstall`, `~/.claude/skills/sqlcg/`
  does not exist, but a sibling `~/.claude/skills/other/SKILL.md` written by the test
  beforehand is untouched (foreign-content preservation). `uninstall` on a clean machine
  prints the "not found" notice and exits 0.

### Phase 5 — Docs & quick-start

**Step 5.1**: Update the `main.py` `help_text` QUICK START to mention the skill is
provisioned by `install`, and add a short note in the install command help about `--scope`.
- Files affected: [`main.py`](../src/sqlcg/cli/main.py), `install.py` docstring/help.
- Acceptance: `sqlcg install --help` output contains `--scope` and the words `project` and `global`.

---

## Test Strategy

### Unit tests (`tests/unit/`)
- `test_skill_render.py`:
  - frontmatter present (`---`, `name: sqlcg`, `version:`).
  - fact/heuristic contract section present with both words.
  - **drift guard** (the key test): every `@mcp.tool()` registered name appears in the
    rendered skill, and the skill names no unregistered tool. This is the anti-drift
    invariant from CLAUDE.md, asserted on observable output.
  - heuristic-tag correctness for `analyze_unused`/`*scope*`; fact-tag for `get_hub_ranking`.
- Extend `test_install.py`:
  - `--scope global` writes the file; content includes all tool names; idempotent re-install.
  - `--scope project --repo <tmp>` writes under the repo.
  - `--dry-run` writes nothing.
  - missing `--scope` with non-TTY stdin → non-zero exit with a "pass --scope" message.
- Extend `test_uninstall.py`:
  - skill dir removed after install→uninstall; foreign sibling skill preserved;
    clean-machine uninstall prints "not found" and exits 0.

### E2E tests (`tests/e2e/`)
- `test_skill_install_e2e.py`: invoke the real CLI via `CliRunner` end-to-end with a
  monkeypatched home/tmp repo: `install --scope project` then assert the on-disk
  `SKILL.md` parses as YAML-frontmatter + body and lists the live tool set; then
  `uninstall` and assert the file is gone. (No graph backend needed — pure filesystem +
  registry, so this is fast and deterministic.)

### Commands to run (per CLAUDE.md)
- `rtk uv run pytest tests/unit/test_skill_render.py tests/unit/test_install.py tests/unit/test_uninstall.py -x`
- `rtk uv run pytest tests/e2e/test_skill_install_e2e.py -x`
- `rtk uv run pyright` and `rtk uv run ruff check src tests`

---

## Acceptance Criteria
- [ ] `sqlcg install --scope global` writes `~/.claude/skills/sqlcg/SKILL.md` with YAML
      frontmatter (`name: sqlcg`, `version: <__version__>`) and a body listing all 17 MCP tools.
- [ ] The skill states the fact/heuristic boundary: lineage/dependency tools +
      `get_hub_ranking` = facts; `analyze_unused.dead_code` and `*scope*.risk` = heuristics
      (with confidence/reason) that MUST NOT be reported as fact.
- [ ] `--scope project --repo <path>` writes under `<path>/.claude/skills/sqlcg/`.
- [ ] Omitting `--scope` prompts interactively on a TTY; errors (non-zero) on non-TTY.
- [ ] Re-installing produces exactly one skill file with identical content (idempotent).
- [ ] `install --dry-run` writes no skill file.
- [ ] `sqlcg uninstall` removes `…/skills/sqlcg/` at both global and project locations,
      preserving any foreign sibling skill; clean-machine uninstall prints "not found", exits 0.
- [ ] Drift-guard unit test passes: skill ↔ live registry parity (no missing/extra tool).
- [ ] No `# TODO` in any happy path; `render_skill` and `_provision_skill` and
      `_step4_remove_skill` each have a grep-confirmed call site; `pyright`/`ruff` clean.

## Risks and Mitigations
- **Drift between skill prose and tool set** → the table is generated from the live
  registry and pinned by the drift-guard test; only the table can express tool names.
- **Clobbering a user's hand-written skill** → sqlcg owns only `…/skills/sqlcg/`; install
  warns and skips on a foreign `SKILL.md`; uninstall rmtree's only the sqlcg subdir.
- **Silent wrong-location write in CI** → non-TTY without `--scope` errors instead of guessing.
- **FastMCP name extraction brittleness** → `list_registered_tools()` reads from the
  imported `mcp` instance via `mcp._tool_manager._tools` (a private dict attr confirmed
  present in FastMCP's `ToolManager`; verified at implementation time). If the FastMCP
  accessor API changes, fall back to the `TOOL_RETURN_MODELS` keys (the drift-guard test
  cross-checks these against `@mcp.tool()` grep, so the fallback stays correct even if
  the private attr is renamed in a FastMCP upgrade).

---

## Escalations to the user
- **F2 is not yet in [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md).** It is fully
  specified in the v1 draft and explicitly requested, so this plan proceeds, but the
  architect-reviewer should record F2 (and its locked "install location is a user choice"
  decision) in the architecture review before merge, per the project flow.

### Blocking Questions
- None. All three open questions (D1/D2/D3) are resolved with grounded defaults above; the
  user may override any of them, but planning is not blocked.

### Plan-Reviewer Corrections (applied — not blocking)

| ID | Severity | Issue | Resolution |
|----|----------|-------|------------|
| B1 | Blocker (resolved) | Plan claimed 17 tools; `db_info` is not `@mcp.tool()` decorated. Actual registry: 16 tools. | All "17" references corrected to 16; `db_info` removed from registry list and TOOL_RETURN_MODELS count. |
| W1 | Warning (resolved) | Step 3.2 said "any existing repo/path option" — `install.py` has NO `--repo` option today. | Step 3.2 reworded to "Add a `--repo` option" (new parameter, not wire-through). |
| N1 | Note | Line reference "idempotent check L45-48" was off by one. | Corrected to L44-47. |
| N2 | Note | FastMCP private attr `_tool_manager._tools` not mentioned in risk entry. | Risk entry updated to name the attr and explain the fallback mechanism. |

