# Feature Plan: F2 — Bundle a Claude Skill in the `sqlcg` Install

Plan date: 2026-05-31
Author: architect-planner
Branch: `feat/cluster-b-provenance`
Source authority: User F2 request (last v1.1.0 feature); [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §16.2 (locked decision); the now-shipped trust layer (#31/#32/#33), presentation segregation (#34), egress injection (#35a/#35b); [`CLAUDE.md`](../CLAUDE.md) invariants.
Supersedes/consolidates: [`plan/bundle_claude_skill.md`](bundle_claude_skill.md) — the original F2 draft (renamed from the lost `v1_living_codebase_and_skill.md`). That draft predates the trust-layer ship and the install-path reconciliation; this plan is the code-vs-plan-verified successor.

---

## Summary

`sqlcg install` provisions a **Claude skill** (`SKILL.md`, YAML frontmatter) that teaches
an LLM agent the `sqlcg` MCP tool surface and encodes the **fact/heuristic boundary**.
The skill body is generated from the live FastMCP registry (tool table) spliced into
curated prose (`_BOUNDARY` + `_WORKFLOWS`), so it cannot drift from the actual tool set.
The same body is surfaced via `sqlcg mcp best-practices`. `sqlcg uninstall` removes the
skill. Install location is a **user choice** via `--scope {project|global}`.

---

## Investigation Findings (verified against the real tree on this branch)

### Finding 1 — F2 is already implemented on this branch

F2 is **built, wired, and tested**, and recorded as a locked decision in
[`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §16.2. This plan is therefore a
**code-vs-plan verification + one-gap-closing** plan, not a greenfield build.

| Component | Location | Status |
|-----------|----------|--------|
| Skill renderer (pure, no I/O) | [`server/skill.py`](../src/sqlcg/server/skill.py) | shipped — `render_skill(version)`, `render_body()`, `list_registered_tools()`, `_tool_is_heuristic()`, `TOOL_RETURN_MODELS` (16 tools), `_generate_tool_table()` |
| Install provisioning | [`cli/commands/install.py`](../src/sqlcg/cli/commands/install.py) | shipped — `--scope`, `--repo`, `_resolve_scope()`, `_provision_skill()` |
| Uninstall teardown | [`cli/commands/uninstall.py`](../src/sqlcg/cli/commands/uninstall.py) | shipped — `_step4_remove_skill()` + `_SKILL_DIR_TARGETS` |
| Best-practices CLI | [`cli/commands/mcp.py`](../src/sqlcg/cli/commands/mcp.py) | shipped — `mcp best-practices` → `render_body()` |
| Tests | `tests/unit/test_F2_skill_render.py`, `test_F2_install_skill.py`, `test_F2_uninstall_skill.py`, `test_mcp_best_practices.py`; `tests/e2e/test_F2_skill_install_e2e.py` | shipped — 22 passed, 1 skipped (gate test, satisfied) |

### Finding 2 — The install surface F2 hooks into

`sqlcg` ships as a console script (`[project.scripts] sqlcg = "sqlcg.cli.main:main"`,
[`pyproject.toml`](../pyproject.toml)) installed via `uv`/`pip`/`uvx`. There is no separate
binary installer; the package itself is the install unit. The **MCP-registration** install
is [`install_cmd`](../src/sqlcg/cli/commands/install.py) (`sqlcg install`), which registers
the server via `claude mcp add -s user` (preferred) or a direct `~/.claude.json` write
(fallback). F2 hooks into **this existing command** — it adds skill provisioning as a side
effect of `sqlcg install`, not a new top-level command. No packaging change is needed:
`skill.py`'s prose constants live inside `src/sqlcg`, which already ships under
`[tool.hatch.build.targets.wheel] packages = ["src/sqlcg"]`.

> **Path-reconciliation note**: the *original* F2 draft and `uninstall.py` Step 1 reference
> `~/.claude/settings.json` for MCP registration. The hygiene/config batch reconciled the
> **install** write path to `claude mcp add` / `~/.claude.json` ([`install.py`](../src/sqlcg/cli/commands/install.py)
> docstring L9-11). This is an MCP-registration concern, **orthogonal to the skill file
> location**, which is `<scope-root>/.claude/skills/sqlcg/SKILL.md` in both code paths.
> The skill location is unaffected by the settings.json↔claude.json reconciliation.

### Finding 3 — What "bundle a Claude skill" concretely means here

It means **option (a) + (c)**: ship a **SKILL.md-format file** for Claude Code's skill
system (`.claude/skills/sqlcg/SKILL.md`, YAML frontmatter `name`/`description`/`version`),
**generated at install time** from the in-process MCP registry + curated prose. It does
**not** extend the MCP server's served instructions as a separate runtime string — the
*single source* of the boundary doc is `render_body()`, reused by both the bundled file and
the `mcp best-practices` CLI so they cannot diverge. The drift-guard test pins the rendered
file's tool table to the live `@mcp.tool()` registry.

### Finding 4 — Install location: locked decision is NOT lost

Memory recorded the location as a "user choice (locked decision)" whose draft was missing.
**It is not missing** — it is recorded in [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md)
§16.2 "Locked decisions": *install location is a user choice (`--scope` flag + interactive
TTY prompt; errors rather than guessing on non-TTY stdin)*. The shipped
[`_resolve_scope`](../src/sqlcg/cli/commands/install.py) implements exactly this. **See the
"Install-Location Decision" section below for the explicit options + recommendation the user
asked me to surface, and confirmation that the as-shipped behaviour matches the locked intent.**

### Finding 5 — Trust-layer semantics: mostly encoded, one prose gap (#31)

The user wants the skill to encode the shipped trust semantics so the LLM interprets results
correctly. Verified against [`server/models.py`](../src/sqlcg/server/models.py) and the
shipped `_BOUNDARY`/`_WORKFLOWS`:

| Trust fact | In the model? | In the skill prose? |
|------------|---------------|---------------------|
| `confidence=1.0` + `reason=None` = FACT edge; `confidence<1.0` + non-null `reason` = INFERRED (#32) | `LineageNode.confidence`/`.reason` (models.py L58/L63) | **Yes** — `_BOUNDARY` (skill.py L46-49) |
| `presentation_facing` terminals are not dead code (#34) | `UnusedCandidate`/`PresentationCandidate` | **Yes** — `_WORKFLOWS` "Dead code" bullet (skill.py L57) |
| `external_consumer` / `has_external_consumer` (#33/#35) — do NOT delete egress tables | `DependencyNode(kind="external_consumer")`, `external_consumers` on diff/scope | **Yes** — `_WORKFLOWS` bullets (skill.py L57-59) |
| **`file`/`line`/`expression` provenance on a lineage edge (#31)** | `LineageNode.file`/`.line`/`.expression` (models.py L57/L59/L60) | **GAP — not taught.** The LLM is never told these fields exist or that it should cite them when reporting a lineage edge. |
| **`table_kind` ∈ {table, cte, derived, external} (#33)** | `LineageNode.table_kind` (models.py L67) | **GAP — not taught.** `table_kind` is mentioned nowhere in `_BOUNDARY`/`_WORKFLOWS`; the LLM cannot distinguish a real source table from a CTE/derived/external when summarizing lineage. |

This is the **one substantive change** F2 needs to fully satisfy the user's intent. See
Phase A below. Everything else is verification-only.

---

## Install-Location Decision (the single item to confirm before release)

The user asked me to surface the options with a recommendation. The decision is **already
locked** in the architecture review and **already shipped**; this restates it for explicit
sign-off.

| Option | Where SKILL.md lands | Friction for 20-ETL user | Verdict |
|--------|----------------------|--------------------------|---------|
| **A. Hardcode project-level** `<repo>/.claude/skills/sqlcg/` | repo-scoped, committable | low, but writes into the user's repo without asking | rejected — violates "tool does not decide" |
| **B. Hardcode global** `~/.claude/skills/sqlcg/` | one global skill | low | rejected — same reason; also surprises team repos |
| **C. `--scope {project\|global}` flag + TTY prompt default `project`; non-TTY without flag errors** | user picks | **lowest correct friction** — interactive user is prompted (one keypress, defaults to `project`); scripted user must be explicit so CI never silently writes a team-shared file | **CHOSEN & SHIPPED** |

**Recommendation (matches as-shipped):** Option C. It preserves the locked "user choice"
intent as a *runtime* decision, defaults to `project` on a TTY (zero-config for the 20-ETL
user who just runs `sqlcg install` and accepts the prompt default), and refuses to guess in
non-interactive mode (so a CI install cannot clobber an unintended location).

**>>> CONFIRM BEFORE RELEASE:** the TTY-prompt **default of `project`** (vs `global`). This
is the only F2 item that is a judgement call rather than a verified fact. The shipped code
defaults the prompt to `project` ([`_resolve_scope`](../src/sqlcg/cli/commands/install.py)
L182). If the user prefers a `global` default, that is a one-line change to the `default=`
argument. Everything else in F2 is buildable/shipped as-is.

---

## Scope

### In Scope
- One prose change to [`server/skill.py`](../src/sqlcg/server/skill.py): teach the LLM the
  `file`/`line`/`expression` provenance fields (#31) and the `table_kind` distinction (#33)
  on a traced lineage edge, in `_BOUNDARY`/`_WORKFLOWS` (Phase A).
- Verification of the as-shipped renderer, install, uninstall, best-practices, and tests
  against this plan (Phase B — no code change, documentation of pass/fail).
- Confirmation that F2 has **no SCHEMA_VERSION implication** (Phase B).

### Non-Goals
- No change to the MCP tool set, tool behaviour, the `Judgement` model, or any return model.
- No change to the MCP-registration write path (`claude mcp add` / `~/.claude.json`).
- No packaging change (no `force-include`; relies on existing wheel `packages`).
- No new CLI command (F2 extends `install`/`uninstall`/`mcp`, all already wired).
- No change to the `--scope` mechanism or its default (pending the one confirmation above).

---

## Design (as shipped — no structural change)

- **`render_body()`** = `_BOUNDARY` + generated tool table + `_WORKFLOWS`. Single source for
  both the bundled `SKILL.md` body and `sqlcg mcp best-practices`.
- **`render_skill(version)`** = YAML frontmatter (`name: sqlcg`, `description`,
  `version: <__version__>`) + `render_body()`. Single entry point called by
  `_provision_skill` ([`install.py`](../src/sqlcg/cli/commands/install.py) L241).
- **Fact/heuristic tag** derived from the return model via `_tool_is_heuristic` (inspects for
  a `Judgement`-typed field, recursing one level into `list[BaseModel]`/`Union`). Not a
  hand-maintained list.
- **Drift guard**: `list_registered_tools()` reads the live FastMCP registry; the unit test
  asserts skill↔registry parity (no missing/extra tool, exactly 16).
- **Skill file path**: `<scope-root>/.claude/skills/sqlcg/SKILL.md`; `scope-root` is
  `Path.home()` (global) or `--repo`/`Path.cwd()` (project). sqlcg owns only the `…/sqlcg/`
  subdir; foreign sibling skills are never touched.

### Constants / fallbacks (CLAUDE.md compliance)
- Version is sourced **only** from `sqlcg.__version__`; no hardcoded version.
- The skill location derives from `Path.home()`/repo path only; it does **not** touch or
  duplicate any `KuzuConfig` db-path constant. No `KuzuConfig` fallback is in play.

---

## Implementation Steps

### Phase A — Close the #31/#33 provenance prose gap (the only code change)

**Step A.1**: Extend `_BOUNDARY` in [`server/skill.py`](../src/sqlcg/server/skill.py) so the
`trace_column_lineage` description names the provenance fields and the `table_kind`
distinction. The added prose must state, in the skill's own words:
- Every `LineageNode` carries `file`/`line`/`expression` provenance — when reporting a
  lineage edge, **cite the file and line** and (optionally) the producing SQL `expression`
  rather than asserting an unsourced edge.
- `table_kind ∈ {table, cte, derived, external}` distinguishes a real source table from a
  CTE / derived subquery / external source — the LLM must not present a `cte`/`derived`
  intermediate as the ultimate source table.

**Step A.2**: Add a one-line provenance reminder to the `_WORKFLOWS` "Trace lineage" bullet
(skill.py L56) so the workflow text and the boundary agree.

- Files affected: `src/sqlcg/server/skill.py` (prose constants only — no logic change).
- Constraint: `skill.py` is exempt from E501 ([`pyproject.toml`](../pyproject.toml)
  `[tool.ruff.lint.per-file-ignores]`), so long markdown lines are acceptable.
- No happy-path TODO; no new method (so no new call site needed).

### Phase B — Code-vs-plan verification (no code change; record results)

**Step B.1**: Confirm every F2 method has a grep-confirmed call site (already verified — see
Wiring Checklist).

**Step B.2**: Confirm F2 has **no SCHEMA_VERSION implication**. Verified: `SCHEMA_VERSION = "6"`
([`core/schema.py`](../src/sqlcg/core/schema.py) L6); F2 touches only `server/skill.py`,
`cli/commands/install.py`, `cli/commands/uninstall.py`, `cli/commands/mcp.py` — none import or
mutate `SCHEMA_VERSION`, and no graph node/edge is added. F2 is tooling/docs only. Re-index is
not required to adopt F2.

**Step B.3**: Run the F2 test suite + perf invariants; record observable-output pass.

---

## Wiring Checklist (grep-confirmed on this branch)

| Symbol | Defined | Called from | Status |
|--------|---------|-------------|--------|
| `render_skill` | skill.py L239 | install.py L241 (`_provision_skill`) | wired |
| `render_body` | skill.py L229 | mcp.py L71-73 (`mcp best-practices`); skill.py L260 (`render_skill`) | wired |
| `_provision_skill` | install.py L202 | install.py L73, L87, L114, L137 (all `install_cmd` exit paths incl. dry-run) | wired |
| `_resolve_scope` | install.py L153 | install.py L52 (`install_cmd`) | wired |
| `_step4_remove_skill` | uninstall.py L245 | uninstall.py L50 (`uninstall_cmd`) | wired |
| `list_registered_tools` | skill.py L178 | skill.py L215 (`_generate_tool_table`); drift-guard test | wired |
| `install` / `uninstall` / `mcp` commands | — | registered on Typer `app` in `cli/main.py` | wired |

No new method is introduced by Phase A (prose-only), so the checklist is unchanged by this plan.

---

## Test Strategy

- **Unit** (existing, must stay green): `test_F2_skill_render.py` (frontmatter, fact/heuristic
  sections, drift guard, 16-tool parity), `test_F2_install_skill.py` (global write, frontmatter,
  all-16-tools, idempotent, dry-run writes nothing, project `--repo`, non-TTY-without-scope
  errors), `test_F2_uninstall_skill.py` (skill dir removed, foreign sibling preserved,
  clean-machine "not found"), `test_mcp_best_practices.py`.
- **E2E** (existing): `test_F2_skill_install_e2e.py` — real CLI install→on-disk SKILL.md→uninstall.
- **Phase A regression**: extend `test_F2_skill_render.py` with two assertions on observable
  output — `render_skill(...)` contains the substrings `file`/`line`/`expression` (provenance)
  and `table_kind` (or the explicit kinds `cte`/`derived`/`external`). Assert on rendered text,
  not "no exception".
- **Perf invariants** (must stay green, unrelated but mandatory per CLAUDE.md):
  `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`.

### Commands (per CLAUDE.md)
- `rtk uv run pytest tests/unit/test_F2_skill_render.py tests/unit/test_F2_install_skill.py tests/unit/test_F2_uninstall_skill.py tests/unit/test_mcp_best_practices.py tests/e2e/test_F2_skill_install_e2e.py -x`
- `rtk uv run pyright` and `rtk uv run ruff check src tests`

---

## Acceptance Criteria

- [ ] `sqlcg install --scope global` writes `~/.claude/skills/sqlcg/SKILL.md` with YAML
      frontmatter (`name: sqlcg`, `version: <__version__>`) and a body listing all 16 MCP tools. *(shipped)*
- [ ] `--scope project --repo <path>` writes under `<path>/.claude/skills/sqlcg/`. *(shipped)*
- [ ] Omitting `--scope` prompts interactively on a TTY (default `project`); errors non-zero on
      non-TTY with a message naming `--scope`. *(shipped)*
- [ ] Re-installing produces exactly one skill file (idempotent); `install --dry-run` writes none. *(shipped)*
- [ ] `sqlcg uninstall` removes `…/skills/sqlcg/` at global + project locations, preserving any
      foreign sibling skill; clean-machine uninstall prints "not found" and exits 0. *(shipped)*
- [ ] `sqlcg mcp best-practices` prints the same boundary body as the bundled skill. *(shipped)*
- [ ] Drift-guard unit test passes: skill↔live-registry parity, exactly 16 tools. *(shipped)*
- [ ] **The skill prose teaches `file`/`line`/`expression` provenance (#31) and the `table_kind`
      distinction (#33) for `trace_column_lineage`, asserted on rendered output.** *(Phase A — TO DO)*
- [ ] The skill states the fact/heuristic boundary: lineage/dependency tools + `get_hub_ranking`
      = facts; `analyze_unused.dead_code` + `*scope*.risk` = heuristics carrying `confidence`/`reason`;
      the LLM must NOT suggest deleting `presentation_facing` or `external_consumer` tables. *(shipped)*
- [ ] No `# TODO` in any happy path; every F2 method has a grep-confirmed call site; `pyright`/`ruff` clean. *(verified)*
- [ ] F2 introduces **no SCHEMA_VERSION change** (stays "6"); no re-index required. *(verified)*

---

## Risks and Mitigations

- **Skill prose drifts from tool set** → table generated from live registry + drift-guard test (mitigated, shipped).
- **Clobbering a user's hand-written skill** → install warns+skips on a foreign `SKILL.md`
  (missing `name: sqlcg` marker); uninstall rmtree's only `…/skills/sqlcg/` (mitigated, shipped).
- **Silent wrong-location write in CI** → non-TTY without `--scope` errors instead of guessing (mitigated, shipped).
- **FastMCP private-attr brittleness** (`_tool_manager._tools`) → falls back to `TOOL_RETURN_MODELS`
  keys, which the drift-guard cross-checks against the registry (mitigated, shipped).
- **Phase A prose contradicting the `_BOUNDARY` authority** → Phase A edits *that same
  authority* (`_BOUNDARY`/`_WORKFLOWS` in skill.py), so there is one source; the
  `mcp best-practices` output and the bundled file both render from it (no divergence risk).

---

## Blocking Questions

None for build. One **release-gate confirmation** (non-blocking for implementation):

1. **TTY-prompt default for `--scope`** — shipped default is `project`. Confirm `project`
   (recommended, repo-scoped, zero-config for the 20-ETL user) vs `global`. One-line change if
   changed. *Surfaced for the sleeping user per instruction; everything else is buildable now.*

## Escalations

- F2 is recorded in [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §16.2 with the locked
  install-location decision — no architect-reviewer escalation needed. After Phase A lands, the
  reviewer may optionally note that the skill now also teaches #31 provenance + #33 `table_kind`,
  but this is consistent with (not a change to) the §16.2 decision.
