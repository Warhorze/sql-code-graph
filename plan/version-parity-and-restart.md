# Feature Plan: Version Parity + Auto-Restart of Stale MCP on Reinstall

## Summary
Make the package version, CLI `--version`, and the running MCP server's reported
version a single source of truth that is always identical, and make `sqlcg install`
stop a stale running MCP server so the editor respawns a fresh process. This closes
the "we debugged against the wrong (stale) MCP server" failure class.

## Motivation / Background
After a package upgrade the editor kept the old stdio MCP process alive, so debugging
happened against stale code. There was also no way to ask the *running* MCP server its
version. This is the follow-up to issue #29 (closed — lifecycle commands) and the
documented backlog items:
- ARCHITECTURE_REVIEW.md "minor ui changes": *add version flag to cli `--version`*.
- ARCHITECTURE_REVIEW.md §11.3 (HIGH): `sqlcg install` leaves stale entries / does not
  refresh the running server.
- ARCHITECTURE_REVIEW.md §17.4/§17.5: `restart` is `stop` + editor-respawn guidance; a
  true re-parent needs a launcher (out of scope here). We rely on the editor's respawn,
  which is exactly the mechanism this plan leans on.

## Scope

### In Scope
- An eager `--version` callback on the root [`sqlcg`](src/sqlcg/cli/main.py) Typer app
  (the `version` subcommand stays; both share one helper — no duplicated logic).
- Set `mcp._mcp_server.version = __version__` in [`server.py`](src/sqlcg/server/server.py)
  so the MCP `initialize` handshake reports `serverInfo.version`.
- Add a `version` field to the control-socket `status` response and to
  [`mcp status`](src/sqlcg/cli/commands/mcp.py) output, plus a `stale_by_version`
  boolean comparing running version to installed `__version__`.
- Add a `sqlcg_version` field to the `db_info` MCP tool result so an agent can
  self-check which build it is talking to (the protocol `serverInfo.version` is not
  reliably visible to the agent persona).
- Make `sqlcg install` stop a running MCP server (best-effort, via the existing control
  socket) on **every** non-dry-run success path — including "already configured" — so the
  editor respawns a fresh process on the new version. The stop fires exactly once, at a
  single shared tail all success branches converge on (see Step 5.1).
- A test asserting CLI `--version`, MCP `serverInfo.version`, control-socket `status.version`,
  and `db_info.sqlcg_version` all equal `sqlcg.__version__`, and that `__version__`
  equals `importlib.metadata.version("sql-code-graph")`.
- Version bump (additive surface → minor: `1.4.3` → `1.5.0`), `uv lock`.

### Non-Goals
- A true re-parenting launcher / true graceful `restart` (deferred per §17.5 — the
  editor owns the stdio process lifecycle; we cannot re-spawn it ourselves).
- Post-install hooks for `uv tool install` / `pip install` (unreliable across installers
  — explicitly rejected as the trigger; see Design Decision 1).
- Changing the graph **schema** version concept (`get_schema_version` / `SCHEMA_VERSION`
  in [`tools.py`](src/sqlcg/server/tools.py)). That is a different axis (graph schema, not
  package version) and must not be conflated.
- Touching any perf-hot path in [`base.py`](src/sqlcg/parsers/base.py) or
  [`indexer.py`](src/sqlcg/indexer/indexer.py).
- Auto-fixing the §11.3 uvx/sqlcg priority inversion (separate finding; out of scope here).

## Design

### Single source of truth
Every surface reads `from sqlcg import __version__` (defined at
[`src/sqlcg/__init__.py:3`](src/sqlcg/__init__.py), kept in lockstep with
`pyproject.toml` by commitizen `version_files`). No surface hardcodes a version string.

Two version axes exist and must stay distinct:
- **Package version** — `sqlcg.__version__` (this feature). The "are we on the right build?" axis.
- **Graph schema version** — `get_schema_version()` / `SCHEMA_VERSION` (unchanged). The
  "does the DB match the code's schema?" axis.

### Version surfaces (the four readouts)
| Surface | How version is exposed | Reads from |
|---------|------------------------|-----------|
| CLI flag | `sqlcg --version` eager callback | `sqlcg.__version__` |
| CLI subcommand (existing) | `sqlcg version` | `sqlcg.__version__` (shared helper) |
| MCP protocol | `serverInfo.version` in `initialize` handshake | `mcp._mcp_server.version` set to `sqlcg.__version__` |
| Control socket | `status` JSON `version` field + `mcp status` output | `sqlcg.__version__` in the server process |
| MCP tool (agent-visible) | `db_info().sqlcg_version` | `sqlcg.__version__` |

### Drift detection
`mcp status` compares the **running server's** reported `version` (from the live process,
read over the socket) against the **installed** `sqlcg.__version__` (the version of the
CLI process you just invoked). If they differ, it emits `stale_by_version: true` and a
human-readable warning telling the user to restart the MCP server in their editor (or run
`sqlcg install`, which now stops the stale server for them).

### `mcp._mcp_server.version` — deliberate private access
`FastMCP.__init__` does **not** accept a `version` kwarg (verified signature); the
underlying `mcp.server.lowlevel.Server.__init__(self, name, version=None, ...)` does.
We set it post-construction:
```python
mcp = FastMCP("SQL Code Graph")
mcp._mcp_server.version = __version__  # deliberate: FastMCP has no version kwarg
```
This is a documented, deliberate private-attribute write. A unit test pins it (so an
upstream API change that breaks it fails loudly rather than silently shipping
`serverInfo.version = None` again).

### Data Models
- [`DbInfoResult`](src/sqlcg/server/models.py) gains:
  ```python
  sqlcg_version: str = Field(..., description="Installed sqlcg package version (sqlcg.__version__)")
  ```
- Control-socket `status` response dict gains `"version": __version__`.
- `mcp status` output dict gains `version` (running) and `stale_by_version` (computed).

### Dependencies
No new runtime dependencies. `importlib.metadata` is stdlib.

## Implementation Steps

### Phase 1: Single source of truth helper
**Step 1.1**: Add a tiny shared version-string helper.
- Decision: keep it trivial — both the CLI flag and the `version` subcommand call one
  function so the format string is defined once.
- File: [`src/sqlcg/cli/main.py`](src/sqlcg/cli/main.py) — add a module-level
  `def _version_string() -> str: from sqlcg import __version__; return f"sqlcg version {__version__}"`.
- Acceptance: `version` subcommand echoes `_version_string()`; no behaviour change to it.

**Step 1.2**: Add an eager `--version` callback on the root app.
- File: [`src/sqlcg/cli/main.py`](src/sqlcg/cli/main.py).
- This is the **first `@app.callback()` on the root app** — confirmed none exists today
  (main.py has only `@app.command()` registrations and the `version` subcommand). Use the
  full eager-option-on-a-root-callback pattern below.
- The callback **must** use `invoke_without_command=True`. Without it, bare `sqlcg --version`
  can error "Missing command" before the eager option's callback gets a chance to run —
  the eager callback fires during parameter processing, but Typer still wants a command
  unless the root callback opts out of requiring one.
- Exact pattern (state this verbatim in the implementation):
  ```python
  def _version_callback(value: bool) -> None:
      if value:
          typer.echo(_version_string())
          raise typer.Exit()

  @app.callback(invoke_without_command=True)
  def _root(
      version: bool = typer.Option(
          False, "--version", help="Show version and exit.",
          is_eager=True, callback=_version_callback,
      ),
  ) -> None:
      """SQL code graph analyzer."""
      # No body needed: the eager --version callback exits before we get here.
      # When no subcommand is given and --version is absent, Typer shows help
      # because no_args_is_help is the app default for a group with subcommands.
  ```
  Callback function signature is explicit above: `_root(version: bool = typer.Option(...))`
  returning `None`, and `_version_callback(value: bool) -> None`.
- Must not break existing subcommand dispatch: the eager callback returns immediately when
  `--version` is not passed, so `_root` runs as a no-op and Typer dispatches the subcommand.
- Files affected: `main.py`.
- Acceptance (each is an observable check):
  - (a) `sqlcg --version` prints `sqlcg version 1.5.0` and exits 0.
  - (b) `sqlcg` with no args still shows the help text (not an error).
  - (c) `sqlcg index --help` (and other subcommands, e.g. `sqlcg mcp status`) still dispatch.
  - (d) `sqlcg --help` still renders the root help correctly after the callback is added.
  - (e) `sqlcg version` (the subcommand) still works and echoes `_version_string()`.

### Phase 2: MCP protocol serverInfo.version
**Step 2.1**: Set `mcp._mcp_server.version` at module scope.
- File: [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py) — immediately after
  `mcp = FastMCP("SQL Code Graph")` (line 36). Import `__version__` at module top.
- Add an inline comment documenting why the private attr is used (no `version` kwarg on FastMCP).
- This must not disturb the fd-1 capture ordering invariant documented at the top of the
  module (the assignment is a pure attribute write, no I/O — safe after line 36).
- Acceptance: `mcp._mcp_server.version == sqlcg.__version__` (was `None`); the
  `create_initialization_options()` handshake reports `serverInfo.version`.

### Phase 3: Control-socket status version + drift
**Step 3.1**: Add `version` to the `status` response.
- File: [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py) — in the `op == "status"`
  branch (the `resp` dict around line 220), add `"version": __version__`.
- Acceptance: a `{"op":"status"}` request returns a `version` field equal to the running
  server process's `__version__`.

**Step 3.2**: Surface running version + drift in `mcp status`.
- File: [`src/sqlcg/cli/commands/mcp.py`](src/sqlcg/cli/commands/mcp.py) — in `mcp_status`,
  after parsing the framed `status` payload:
  - read `running_version = status.get("version")`.
  - compute `from sqlcg import __version__; stale = running_version is not None and running_version != __version__`.
  - include both in the printed JSON (`version`, `stale_by_version`).
  - if `stale`, print a yellow warning: running vX vs installed vY → restart via editor or run `sqlcg install`.
- The degraded (PID-only) branch cannot read a version → omit `version`, set
  `stale_by_version` to Python `None` (unknown). Do not guess.
- **Typing note**: the `mcp status` output is a plain `dict` serialized by
  `console.print_json` / `json.dumps`, NOT a Pydantic model. So the degraded branch simply
  puts Python `None` in the dict, which serializes to JSON `null`. There is no
  `Optional[bool]` Pydantic field to add and no model change for this — it is a dict value.
- Acceptance: when running and installed versions differ, `mcp status` JSON shows
  `stale_by_version: true` and the warning text; when equal, `stale_by_version: false`
  and no warning.

### Phase 4: Agent-visible version via db_info
**Step 4.1**: Add `sqlcg_version` to `DbInfoResult`.
- File: [`src/sqlcg/server/models.py`](src/sqlcg/server/models.py) — add the field (see Data Models).
- Required field (`...`) — no default — so it cannot silently be omitted.

**Step 4.2**: Populate it in the `db_info` tool.
- File: [`src/sqlcg/server/tools.py`](src/sqlcg/server/tools.py) `db_info` (line 1662) —
  `from sqlcg import __version__` and pass `sqlcg_version=__version__` into the
  `DbInfoResult(...)` return. Keep `schema_version` exactly as-is (do not conflate).
- Update the `db_info` docstring with one line explaining `sqlcg_version` is the package
  build (distinct from `schema_version`).
- Acceptance: `db_info()` result carries `sqlcg_version == sqlcg.__version__`; existing
  `schema_version` field unchanged.

### Phase 5: install stops the stale server
**Step 5.1**: Make `install_cmd` stop a running MCP server on every non-dry-run success path.

**5.1a — extract `stop_server()` in `mcp.py` (no install-local wrapper).**
- File: [`src/sqlcg/cli/commands/mcp.py`](src/sqlcg/cli/commands/mcp.py).
- Extract the socket-stop body of [`mcp_stop`](src/sqlcg/cli/commands/mcp.py) into a
  reusable module-level function `stop_server() -> bool` in `mcp.py`. It performs the
  existing connect → send `{"op":"stop"}` → `recv(128)` → wait-for-socket-gone → SIGTERM
  fallback logic and returns `True` if a server was stopped, `False` if none was found.
- `mcp_stop` then calls `stop_server()` and prints its existing messages based on the bool.
- `install_cmd` imports `stop_server` from `mcp.py` and **calls it directly** — there is
  **no** `_stop_running_server()` install-local wrapper. (The previous draft's
  `_stop_running_server()` wrapper is dead and is removed from this plan.)
- Grep-confirmed call sites for the new method: `stop_server` is called from `mcp_stop`
  **and** from `install_cmd` — two real call sites, satisfying the "every new method has a
  grep-confirmed call site" rule.

**5.1b — single funnel point in `install_cmd` (`install.py`).**
- File: [`src/sqlcg/cli/commands/install.py`](src/sqlcg/cli/commands/install.py).
- `install_cmd` today has **FOUR** return/exit shapes; three are non-dry-run success paths
  that must trigger the stop, and dry-run must NOT:
  1. line 88 — `claude mcp add` succeeds → currently `_provision_skill` + `return`.
  2. line 115 — "Already configured" (`existing == entry`) → currently `_provision_skill` +
     `return`. **This is exactly the reinstall case**: the config didn't change but the
     binary on disk did, so the stale running server still must be stopped. Do NOT skip it.
  3. line ~137 — the fallback `~/.claude.json` write path → falls through to
     `_provision_skill` at the function tail.
  4. dry-run (line 64–74) → `_provision_skill(dry_run=True)` + `return`; must NOT stop,
     prints "would stop running MCP server" instead.
- **Restructure so all three non-dry-run branches converge on one tail** rather than
  returning early. Concretely: replace the early `return`s at lines 88 and 115 so that,
  after each branch prints its own "Configured" / "Already configured" line, control falls
  through to a single shared tail that runs **once**:
  ```python
  # single non-dry-run tail (reached by all three success branches):
  stopped = stop_server()
  if stopped:
      console.print(
          "Stopped running MCP server; your editor will respawn it on the new build."
      )
  else:
      console.print("No running MCP server to refresh.")
  _provision_skill(resolved_scope, repo, dry_run=False)
  ```
  Use a structure such as: set the per-branch "Configured" message, then `break`/fall into
  the shared tail (e.g. wrap the three branches so they assign and then jump past to the
  tail, or invert the early-returns into `if/elif/else` so the tail is unconditionally
  reached on the non-dry-run path). The dry-run block keeps its own early `return` and
  prints "would stop running MCP server" before returning.
- `stop_server()` is called **exactly once** on the non-dry-run path — do NOT duplicate
  the call in all three branches, and do NOT miss the "Already configured" branch.
- The stop is best-effort: `stop_server()` returning `False` (no server) is a no-op with
  the "No running MCP server to refresh." message; a socket error inside `stop_server()` is
  already swallowed by its own except clause and must not abort install.

**Message (resolves the `(vX)` inconsistency):** the install stop line **drops the `(vX)`
version suffix**. `stop_server() -> bool` returns only a bool, not the stopped server's
version, and querying `status` first just to render `(vX)` would add an extra socket
round-trip for no real benefit. So the message is exactly:
"Stopped running MCP server; your editor will respawn it on the new build."

- Acceptance: with a running server, `sqlcg install` (non-dry-run) sends a `stop` op and
  the server exits, for **all three** success branches including "Already configured";
  `stop_server()` is invoked exactly once per install; with no server, install completes
  with the no-op message; `--dry-run` never stops anything and prints "would stop running
  MCP server".

### Phase 6: Single-source-of-truth test
**Step 6.1**: Add a parity test.
- File: `tests/unit/test_version_parity.py`.
- Assert:
  1. `sqlcg.__version__ == importlib.metadata.version("sql-code-graph")`.
  2. CLI `--version` output (via Typer `CliRunner`) contains `sqlcg.__version__`.
  3. `mcp._mcp_server.version == sqlcg.__version__` (import
     [`server.py`](src/sqlcg/server/server.py); pins the private-attr set survives upstream changes).
  4. `db_info().sqlcg_version == sqlcg.__version__` (integration-level, with a backend) —
     OR a focused unit asserting `DbInfoResult` carries the field and the tool passes
     `__version__` (use whichever matches existing `db_info` test infra; prefer the
     integration path if one already exists).
  5. Control-socket `status` includes `version == sqlcg.__version__`. Home this assertion
     in [`tests/unit/test_mcp_control.py`](tests/unit/test_mcp_control.py) (the existing
     control-socket test module). The assertion must inspect **observable output**, NOT
     source-grep the `resp` dict construction. Only these two forms are acceptable:
     (a) drive the real control socket end-to-end and parse the framed `status` response,
     asserting the parsed `version` equals `sqlcg.__version__`; or
     (b) call the `status` handler directly and inspect the returned dict's `version` value.
     Do not assert by reading the server source for the literal `"version": __version__`.
- Acceptance: all five assertions pass; the test fails if any surface diverges or
  hardcodes a version.

### Phase 7: Version bump + lock
**Step 7.1**: Bump version to `1.5.0`.
- [`pyproject.toml`](pyproject.toml) `version = "1.5.0"`.
- [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py) `__version__ = "1.5.0"`.
- Run `uv lock`.
- Acceptance: `uv run sqlcg --version` prints `sqlcg version 1.5.0`; the parity test passes
  against `1.5.0`; `git diff` shows the lockfile's own version entry refreshed.
- NOTE: the agent does NOT tag or close issues — the user tags after merge (per CLAUDE.md
  "Releasing" + project memory "never close; verify before tag").

## Design Decisions (resolved)

### Decision 1 — Restart-on-reinstall trigger: `sqlcg install` stops the running server (option a), plus a drift *warning* in `mcp status` (option b as detection, not auto-stop)
**Recommendation: a + b (detection only).**
- uv-tool/pip installs do not reliably run post-install hooks, so an installer hook is
  rejected. The trigger that the user already performs after an upgrade is `sqlcg install`
  (it is in the QUICK START). Making `install` stop the stale server (Phase 5) is the
  reliable, explicit trigger: stop → editor respawns the freshly-installed build.
- `mcp status` (Phase 3) is a *detector*, not an auto-killer. Auto-stopping a server from a
  read-only `status` call would be a surprising side effect. It instead reports
  `stale_by_version` and tells the user the fix. This respects "no TODO in happy path"
  (the stop path is fully implemented in install; status just reports).
- We rely on the editor respawning the stdio process — consistent with §17.4/§17.5 (we
  cannot re-parent it ourselves; the editor owns it). This is honest, not faked.

### Decision 2 — `--version` for the MCP: implement all three readouts
**Recommendation: implement the protocol field AND the operator/agent readouts.**
- (i) Protocol `serverInfo.version` (Phase 2) is the canonical answer to "what version is
  the running server?" but is not reliably visible to the agent persona or to a human at
  the CLI.
- (ii) `mcp status` `version` field (Phase 3) is the human/operator readout and the place
  drift is flagged.
- (iii) `db_info().sqlcg_version` (Phase 4) is the agent-callable readout so an agent can
  self-check it is on the right build before trusting results.
All three are needed to actually let someone confirm which version is running.

### Decision 3 — Single source of truth + parity test
**Recommendation: every surface reads `from sqlcg import __version__`; one test pins parity.**
- No surface hardcodes a version. Phase 6 asserts the four runtime surfaces agree with
  `__version__` and that `__version__` equals the installed distribution metadata
  (`importlib.metadata.version("sql-code-graph")`) — verified resolvable in this env
  (returns `1.4.3` today). This catches a future `pyproject.toml`/`__init__.py` drift even
  if commitizen is bypassed.

## Test Strategy
- **Unit**:
  - `tests/unit/test_version_parity.py` (Phase 6) — the five parity assertions.
  - CLI `--version` via Typer `CliRunner` — observable stdout asserted (not "no exception").
  - `mcp status` drift: mock the socket `status` payload with a mismatched `version`,
    assert `stale_by_version: true` + warning text in output; matching version → false + no warning.
  - `install` stop-on-reinstall: mock `stop_server()`, assert it is called **exactly once**
    on the non-dry-run path — including the "Already configured" branch — and NOT called on
    `--dry-run`; assert the no-op message when it returns False; assert the stop message has
    no `(vX)` suffix.
  - Private-attr guard: `mcp._mcp_server.version == __version__` (fails loudly if FastMCP
    upstream changes).
- **Integration** (real backend, in-memory): `db_info().sqlcg_version == __version__`
  alongside an existing `db_info` integration test if one exists; otherwise add a focused one.
- **Observable-output rule**: every test asserts a concrete value (version string, boolean,
  warning substring), never merely that a call did not raise.
- Run full gate before handoff: `uv run pytest`, `uv run pyright`, `uv run ruff check src tests`.

## Acceptance Criteria
- [ ] `sqlcg --version` prints `sqlcg version <__version__>` and exits 0; `sqlcg` with no
      args still shows help; `sqlcg --help` renders correctly after the callback is added;
      `sqlcg version` still works; all other subcommands (e.g. `sqlcg index --help`,
      `sqlcg mcp status`) dispatch unchanged.
- [ ] `mcp._mcp_server.version == sqlcg.__version__` (no longer `None`); the MCP
      `initialize` handshake reports `serverInfo.version`.
- [ ] Control-socket `status` response includes `version == <running __version__>`.
- [ ] `sqlcg mcp status` shows `version` and `stale_by_version`, and warns when the running
      server version differs from the installed `__version__`.
- [ ] `db_info().sqlcg_version == sqlcg.__version__`; `schema_version` unchanged and distinct.
- [ ] `sqlcg install` (non-dry-run) stops a running MCP server so the editor respawns it,
      on all three success branches including "Already configured"; `stop_server()` is
      invoked exactly once per install; no-op message when none running; `--dry-run` stops
      nothing. Stop message drops the `(vX)` suffix.
- [ ] `stop_server()` lives in `mcp.py` (no install-local wrapper) and has grep-confirmed
      call sites in both `mcp_stop` and `install_cmd`.
- [ ] Parity test asserts all four runtime surfaces equal `__version__` and
      `__version__ == importlib.metadata.version("sql-code-graph")`.
- [ ] Version bumped to `1.5.0` in `pyproject.toml` + `__init__.py`; `uv lock` run.
- [ ] No TODO in any happy path; no changes to `base.py`/`indexer.py`.

## Risks and Mitigations
| Risk | Mitigation |
|------|-----------|
| FastMCP upstream removes/renames `_mcp_server` (private attr) | Unit test pins `mcp._mcp_server.version == __version__`; a break fails the suite loudly instead of silently shipping `None`. |
| `install` stopping a server the user did not want stopped | Stop is best-effort + clearly messaged; install's whole purpose post-upgrade is to refresh the server; `--dry-run` never stops. |
| `importlib.metadata.version("sql-code-graph")` differs in an editable/uv dev tree | Verified it returns the correct value in this env. If a future dev layout breaks it, the parity test surfaces it immediately (intended). |
| Conflating package version with graph schema version | Distinct fields (`sqlcg_version` vs `schema_version`), distinct docstrings, called out in Non-Goals. |
| Editor does not auto-respawn after stop | Documented limitation (§17.4/§17.5); `mcp status` warning + install message tell the user to restart via the editor. |

## Rollout / Rollback
- Additive surface only; no schema change → no re-index required. Rollback = revert the PR.
- Ships as one feature PR that bumps `1.4.3 → 1.5.0` (SemVer minor: additive, nothing
  breaks). User tags `v1.5.0` on the master merge commit after merge (agent does not tag).
