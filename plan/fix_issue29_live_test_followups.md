# Feature Plan: #29 Live-Test Follow-up Fixes (fold into PR #51 / v1.3.0)

## Summary
Three correctness bugs (plus one cosmetic message fix) surfaced during live-DWH
testing of the open PR #51 branch `feat/fix-issue-29-single-writer-queue`
(v1.3.0 — single-writer queue + RO→RW escalation). These fold into the **unreleased**
PR #51; the version stays **1.3.0** (no separate bump — see Version note below).

Source of findings: GitHub issue [#29](https://github.com/Warhorze/sql-code-graph/issues/29)
live-test comment. Bug B is already documented as an architecture follow-up in
`ARCHITECTURE_REVIEW.md` §20.2 ("Deviation 1 — MCP `index_repo` tool inline escalation").

## Version note (RESOLVED — stays 1.3.0)
These are bug fixes to code that has **not yet shipped** (PR #51 has no reviews and is
not merged). Per CLAUDE.md SemVer, the version stays **1.3.0** — there is no released
1.3.0 to patch against, so no `1.3.1`. Do **not** bump `pyproject.toml` / `__init__.py`
/ `uv.lock`. Decision confirmed (see Resolved Decisions §D1): fold into PR #51, version
unchanged.

## Scope

### In Scope
- **Bug A** — resolve `--dialect auto` (and any repo-config-derived client-side value)
  **before** building the socket WriterRequest in `reindex.py` and `index.py`.
- **Bug B** — store the path `init_backend(db_path)` received in a module-level variable
  and use it for all escalation/de-escalation in `tools.py`; audit every `get_db_path()`
  call in `tools.py` / `writer.py` / `server.py` for the same default-path leak.
- **Bug C** — `install-hooks` upgrades an sqlcg-owned hook in place when the rendered
  content differs from the current template; prints `Upgraded git hook: …`. True
  idempotency (identical rendered content) stays a silent skip. Foreign-hook warning
  path unchanged.
- **Minor D** — reword the hook fallback message from "server busy/locked" to a
  failure-agnostic "reindex failed".
- Regression tests for A, B, C, D that assert **observable output** (payload contents,
  default-path untouched, stdout message, hook file content).

### Non-Goals
- **The kuzu 0.11.3 `KU_UNREACHABLE` crash** (csr_node_group.cpp:411 on a second
  consecutive reindex). It reproduces on the direct no-server path on a fresh DB →
  upstream/pre-existing, tracked separately in the issue comment. **No kuzu upgrade,
  no DB-suspect guard in this plan.**
- The **deeper §20.2 escalation-architecture fixes** (escalation outside `backend_lock`,
  RW-lock leak on the `index_repo` failure path, the dead `_set_backend_lock` /
  `_backend_lock` stub). Bug B as scoped by the maintainer is *narrow*: "use the init
  path, audit `get_db_path`". §20.2 demands more. See "Design — Bug B" for the boundary
  and the explicit decision; the full §20.2 remediation is **out of scope here** and
  remains a tracked v1.3.x follow-up (resolved decision D3).
- No version bump (see Version note).
- **Performance invariants are untouched.** None of these fixes go near
  [`base.py`](src/sqlcg/parsers/base.py) or [`indexer.py`](src/sqlcg/indexer/indexer.py)
  hot paths. The diff is confined to two CLI commands, `tools.py`, `git.py`, and tests.
  The developer must confirm `git diff --stat` shows zero changes to `base.py` and to
  `indexer.py`'s `_extract_column_lineage` / `_upsert_parsed_file` / `_build_file_rows`
  / `_flush_row_batch`.

## Design

### Bug A — `--dialect auto` leaks through the routed reindex/index

**Root cause (verified in code):**
- [`reindex.py`](src/sqlcg/cli/commands/reindex.py) calls
  `_try_route_reindex_via_server(... dialect=dialect ...)` at **lines 86-93**, which sends
  `payload["dialect"] = dialect` (line 227). The `if dialect == "auto": dialect = get_dialect(path)`
  resolution is at **lines 98-99 — AFTER** the routing call. So when a server is live the
  WriterRequest carries the literal `"auto"`.
- [`index.py`](src/sqlcg/cli/commands/index.py) has the **same ordering bug**:
  `_try_route_index_via_server(... dialect=dialect ...)` at **lines 113-118**
  (payload at line 230) runs **before** the `if dialect == "auto"` resolution at
  **lines 155-156**. The routed full index *appeared* correct in live testing only
  because the synthetic DDL happened to parse acceptably; the code order is still wrong
  and must be fixed for parity.

**Server-side confirmation (no fix needed there):** the drain loop
([`writer.py`](src/sqlcg/server/writer.py) lines 528-605) passes `req.dialect`
verbatim to `index_repo` / `resync_changed`. There is no server-side `auto` resolution
— the server cannot resolve `auto` because `get_dialect(path)` reads the repo's
`.sqlcg.toml` and that is a client-side concern. The fix **must** be client-side.
`"Unknown dialect 'auto'"` is raised downstream by sqlglot's dialect lookup once the
literal `"auto"` reaches the parser.

**Fix:** in **both** `reindex.py` and `index.py`, move the
`if dialect == "auto": dialect = get_dialect(path)` resolution **above** the
`_try_route_*_via_server(...)` call so the routing payload always carries a concrete
dialect (or an explicit `None`/named dialect the user passed). After the move:
- `reindex.py`: resolve dialect right after `path = path.resolve()` (line 78), before
  line 86. `get_dialect` is already imported in the function-local import at line 73.
- `index.py`: resolve dialect right after `path = path.resolve()` (line 110), before
  line 113. `get_dialect` is already imported at module level (line 19).
- The duplicate resolution further down (reindex.py 98-99, index.py 155-156) is then
  **removed** (it becomes dead — the value is already resolved). Confirm via grep that
  exactly one `if dialect == "auto"` remains per file, and it is before the route call.

**Boundary note:** "auto" is the only repo-config-derived value carried in the payload
today (`root`, `from`, `to`, `wait` are not config-derived). No other client-side
`.sqlcg.toml` lookup needs hoisting. Document this so the developer does not over-reach.

### Bug B — RW escalation ignores the path `init_backend()` received

**Root cause (verified in code):**
- [`tools.py`](src/sqlcg/server/tools.py) `init_backend(db_path)` (line 128) opens the
  backend on `path = db_path or str(get_db_path())` (line 151) but stores **only**
  `_backend` and `_serving_ro` (lines 168-169). The `path` is **not** retained.
- The MCP tool `index_repo` (line 547) recomputes `db_path_str = str(get_db_path())`
  (**line 579**) and passes that to `_get_or_escalate_rw` (line 581) and
  `_de_escalate_to_ro_from_tool` (line 614). So escalation opens whatever
  `get_db_path()` returns — i.e. the default `~/.sqlcg/graph.db` — **not** the DB
  `init_backend` actually opened.

**Live impact (reproduced):** `tests/e2e/test_mcp_tools.py` does
`init_backend(str(tmp_path / "test.db"))` (line 30) then `index_repo(...)` (line 33).
The escalation opened the user's real `~/.sqlcg/graph.db`, wrote
`tests/fixtures/synthetic` into it during a pytest run, and held the live DB lock —
blocking a real server start.

**Fix:**
1. Add a module-level variable `_init_db_path: str | None = None` next to `_backend`
   / `_serving_ro` (tools.py lines 101-114).
2. In `init_backend`, set `_init_db_path = path` (the resolved `db_path or
   str(get_db_path())`) under the existing `global` declaration (line 150 — add
   `_init_db_path` to it).
3. In `shutdown_backend` (line 181), reset `_init_db_path = None` alongside the other
   globals (add to the `global` at line 187).
4. Add a private accessor `_escalation_db_path() -> str` that returns `_init_db_path`
   when set, else `str(get_db_path())` (preserves the no-`init_backend` direct-call
   path used by some unit tests). **This is a new function — it MUST have a grep-confirmed
   call site before the PR opens** (the `index_repo` tool is its call site; see step).
5. In `index_repo` (line 579), replace `db_path_str = str(get_db_path())` with
   `db_path_str = _escalation_db_path()`. The downstream `_get_or_escalate_rw(db_path_str, …)`
   (581) and `_de_escalate_to_ro_from_tool(db_path_str)` (614) then use the init path.

**Audit of every `get_db_path()` call (verified list):**
| File:line | Context | Verdict |
|---|---|---|
| `tools.py:14` | import | n/a |
| `tools.py:151` | `init_backend` fallback when `db_path is None` | **correct** — this is the source of truth; capture it into `_init_db_path` |
| `tools.py:579` | `index_repo` escalation | **LEAK — fix** (step 5) |
| `writer.py` `escalate_to_rw` / `de_escalate_to_ro` | take `db_path: str` as a parameter; the drain passes the `drain_loop(db_path=…)` argument (writer.py 510-511, 628-629) | **correct** — drain path threads the real db_path; do not change |
| `server.py:140,221` | status reply `db_path` display | display-only; reflects `db_path or _get_db_path()` from the server CLI arg — **correct** |
| `server.py:509,525` | `_run_with_control` resolves `_db_path_obj = Path(db_path) if db_path else get_db_path()` and threads it into `drain_loop` | **correct** — the drain receives the server's actual db_path |

So the **only** leak is `tools.py:579` (the MCP `index_repo` tool). The drain/CLI-op
path is already correct because `db_path` is threaded as a parameter, not re-fetched.

**§20.2 boundary (explicit):** this fix corrects the *path* used by the existing
`index_repo` tool escalation. It does **not** move escalation under `backend_lock`, does
**not** add a `finally`-based de-escalation, and does **not** wire/remove the dead
`_set_backend_lock` / `_backend_lock` stub. Those are the §20.2 remediation, scoped out
here (resolved decision D3). The Bug B fix makes the tool target the correct DB; it does not claim
to make the tool concurrency-safe.

### Bug C — `install-hooks` silently refuses to upgrade old sqlcg hooks

**Root cause (verified in code):** [`git.py`](src/sqlcg/cli/commands/git.py)
`_install_single_hook` (line 91): when the file exists and `spec.sentinel in existing_content`
(line 103), it returns silently (line 105). The sentinel string (e.g.
`# sqlcg post-checkout hook`) is **identical across versions**, so a v1.2.x hook (no
`--notify`, blocks `git checkout` ~2 min against a live server) survives a v1.3.0
`install-hooks` with exit 0 and no output.

**Fix:** the sentinel proves sqlcg ownership. When the sentinel is present:
- Render the current template: `script = spec.script_template.format(sqlcg_bin=sqlcg_bin)`
  (this already happens at line 99). The rendered script embeds the resolved binary path,
  so **the comparison MUST be against the fully rendered `script`**, not the raw template.
- If `existing_content == script` → identical → **silent skip** (true idempotency, R9).
- If `existing_content != script` → sqlcg-owned but stale → **overwrite** (`write_text` +
  `chmod(0o755)`) and print `[green]Upgraded git hook:[/green] .git/hooks/<filename>`.

When the sentinel is **absent** (foreign hook) → the existing warning + manual-append
path (lines 106-118) is **unchanged**.

**Comparison subtlety:** `existing_content` is the raw file text; `script` is the
rendered template. A hook installed by a different sqlcg version differs in the command
line (e.g. missing `--notify`, or the old "server busy/locked" message — see Minor D),
so the `!=` will fire and trigger the upgrade. A hook installed by the *current* binary
from the *same* resolved path is byte-identical → silent skip. Note the binary path is
embedded, so two installs from different binary locations will (correctly) re-render and
upgrade.

### Minor D — misleading hook fallback message

**Root cause (verified in code):** both hook templates in `_HOOKS`
([`git.py`](src/sqlcg/cli/commands/git.py) lines 36-37 post-checkout, lines 53 & 56
post-merge) print `sqlcg: graph not updated (server busy/locked) -- run 'sqlcg mcp status'`
on **any** reindex non-zero exit, including a KuzuDB storage crash (KU_UNREACHABLE), which
has nothing to do with the server being busy.

**Fix:** reword all three occurrences to a failure-agnostic message:
`sqlcg: graph not updated (reindex failed) -- run 'sqlcg mcp status'`.

**Interaction with Bug C:** changing the template text changes the **rendered** content,
so every previously-installed hook (with the old message) now differs from the new
template → Bug C's upgrade path will (correctly) overwrite it on the next `install-hooks`.
This is the intended behaviour and is exactly why C and D ship together. The
`test_git_hooks_notify.py` content guards
([`tests/unit/test_git_hooks_notify.py`](tests/unit/test_git_hooks_notify.py)) assert the
old "server busy/locked" string — they MUST be updated to the new wording (see Test
Strategy; not a regression, a deliberate text change).

### Docs impact
`docs/cli.md` is auto-generated by
[`scripts/generate_cli_docs.sh`](scripts/generate_cli_docs.sh) from **command docstrings
and option metadata only**. Verified: it does **not** embed `install-hooks` stdout
messages nor the hook template text (the `install-hooks` section at docs/cli.md:635-653
is the docstring + the `--repo` option). Bug C changes only **stdout** (a new
`console.print`), and Minor D changes only the **hook template string** — neither touches
a docstring or an option. **No `docs/cli.md` regeneration is required.** The developer
must confirm by diffing: `bash scripts/generate_cli_docs.sh` then `git diff docs/cli.md`
must show **no change**. (If it does, a docstring was inadvertently touched — investigate.)

## Implementation Steps

### Phase 1: Bug A — client-side dialect resolution before routing
**Step 1.1**: In `reindex.py`, move `if dialect == "auto": dialect = get_dialect(path)`
to immediately after `path = path.resolve()` (after line 78, before the route call at
line 86). Remove the now-dead resolution at lines 98-99.
- Files: [`reindex.py`](src/sqlcg/cli/commands/reindex.py)
- Acceptance: `_try_route_reindex_via_server` receives a resolved dialect; grep shows
  exactly one `if dialect == "auto"` in the file, positioned before line ~86.

**Step 1.2**: In `index.py`, move `if dialect == "auto": dialect = get_dialect(path)`
to immediately after `path = path.resolve()` (after line 110, before the route call at
line 113). Remove the now-dead resolution at lines 155-156.
- Files: [`index.py`](src/sqlcg/cli/commands/index.py)
- Acceptance: `_try_route_index_via_server` receives a resolved dialect; grep shows
  exactly one `if dialect == "auto"` in the file, positioned before line ~113.

### Phase 2: Bug B — escalation uses the init path
**Step 2.1**: Add `_init_db_path: str | None = None` module global (tools.py ~line 114);
set it in `init_backend` (add to `global` at 150; assign after computing `path` at 151);
reset to `None` in `shutdown_backend` (add to `global` at 187).
- Files: [`tools.py`](src/sqlcg/server/tools.py)
- Acceptance: `init_backend(p)` then reading the module global returns `p`;
  `shutdown_backend()` clears it.

**Step 2.2**: Add `_escalation_db_path() -> str` returning `_init_db_path or str(get_db_path())`.
Replace `db_path_str = str(get_db_path())` at line 579 with `db_path_str = _escalation_db_path()`.
- Files: [`tools.py`](src/sqlcg/server/tools.py)
- Acceptance: grep shows `_escalation_db_path` defined once and called at exactly one
  site (`index_repo`); no behavioural change when `init_backend` was never called
  (falls back to `get_db_path()`).

### Phase 3: Bug C + Minor D — hook upgrade + reworded message
**Step 3.1 (Minor D)**: Reword the fallback message in all three template occurrences
to `sqlcg: graph not updated (reindex failed) -- run 'sqlcg mcp status'`.
- Files: [`git.py`](src/sqlcg/cli/commands/git.py) (lines 36-37, 53, 56)
- Acceptance: no occurrence of `server busy/locked` remains in `git.py`.

**Step 3.2 (Bug C)**: In `_install_single_hook`, replace the silent-skip branch (lines
103-105) with: if sentinel present and `existing_content == script` → silent return; if
sentinel present and content differs → `write_text(script)` + `chmod(0o755)` + print
`Upgraded git hook: .git/hooks/<filename>`. Foreign-hook branch unchanged.
- Files: [`git.py`](src/sqlcg/cli/commands/git.py)
- Acceptance: identical rendered content → no output, file unchanged; sentinel-present
  stale content → file overwritten with current `script`, "Upgraded git hook:" printed;
  no-sentinel file → unchanged warning path.

### Phase 4: Tests + verification
**Step 4.1**: Bug A regression — assert the WriterRequest never carries `"auto"`.
**Step 4.2**: Bug B regression — `init_backend(tmp)` + escalating `index_repo` must not
touch the default path.
**Step 4.3**: Bug C/D tests — upgrade overwrites stale hook + prints; idempotent skip;
foreign warning unchanged; new message present.
**Step 4.4**: In `test_git_hooks_notify.py`, add a new negative assertion that
  `"server busy/locked"` does NOT appear in any hook template. No existing guard
  asserts the old wording (current tests cover `--notify`, `>&2`, and `|| true`
  absence only), so nothing needs updating — only the new negative assertion is added.
  Also add a positive assertion that all templates contain `"(reindex failed)"`.
**Step 4.5**: Run perf invariant guards + full suite + ruff + pyright; confirm
`git diff --stat` excludes `base.py` and indexer hot paths; confirm
`generate_cli_docs.sh` produces no `docs/cli.md` diff.

## Test Strategy

### Unit tests
- **Bug A** (`tests/integration/`, new file `test_dialect_auto_resolution.py` — these
  tests require a live Unix-socket thread to capture the on-wire payload and belong in
  `tests/integration/`, not `tests/unit/`): call
  `reindex_cmd(..., dialect="auto")` and `index_cmd(..., dialect="auto")` with a live
  fake socket server (reuse the in-process accept pattern from
  [`tests/integration/test_reindex_via_server.py`](tests/integration/test_reindex_via_server.py)
  lines 114-255 — a thread that accepts, reads the framed payload, decodes JSON). Assert
  `json.loads(payload)["dialect"] != "auto"` and equals `get_dialect(repo_root)` (use a
  fixture repo with a `.sqlcg.toml` declaring e.g. `dialect = "postgres"` so the assertion
  is non-vacuous — `assert payload["dialect"] == "postgres"`). Add the symmetric index
  case. **Observable assertion: the exact dialect string on the wire.**
- **Bug B** (`tests/unit/` or `tests/e2e/test_mcp_tools.py`): `init_backend(str(tmp_path /
  "test.db"))`, then call `index_repo(fixture)`. Assert the default path is never touched.
  Two complementary observable checks (use both):
  1. `monkeypatch` `sqlcg.server.tools.get_db_path` to a sentinel that **raises** if called
     during escalation (proves `_escalation_db_path` used `_init_db_path`, not
     `get_db_path()`). Note `init_backend` itself calls `get_db_path` only on the
     `db_path is None` branch — since we pass a path, it must not be called at all in this
     test → the raising sentinel is safe across the whole call.
  2. Record the default `~/.sqlcg/graph.db` mtime (or its non-existence) before and after;
     assert unchanged / still-absent. Guards the live-observed symptom directly.
  **Teardown requirement**: call `shutdown_backend()` after `index_repo` returns (e.g.
  in a `finally` block or pytest fixture teardown) so the `_init_db_path` module global
  is reset between tests. Without this, a later `init_backend(None)` call in a subsequent
  test inherits the stale path and produces a misleading pass.
- **Bug C** (`tests/unit/test_git_hooks.py` — existing, extend it): three cases —
  (a) pre-write a stale sqlcg hook (sentinel + old "server busy/locked" text), run
  `install-hooks`, assert file now equals the current rendered template AND stdout contains
  `Upgraded git hook:`; (b) write the current rendered hook, run again, assert no output and
  byte-identical file (idempotency); (c) write a foreign hook (no sentinel), assert the
  warning path fires and the file is unchanged. **Observable: file content + captured stdout.**
- **Minor D** (`tests/unit/test_git_hooks_notify.py` — existing): add a new
  negative assertion that no hook template contains `"server busy/locked"`. No
  existing guard asserts the old wording (current tests only check `--notify`, `>&2`,
  and absence of `|| true`), so nothing needs updating — only two assertions are added:
  one asserting all templates contain `"(reindex failed)"` and one asserting none
  contain `"server busy/locked"`.

### Integration / e2e
- The existing [`tests/integration/test_reindex_via_server.py`](tests/integration/test_reindex_via_server.py)
  and `tests/e2e/test_mcp_tools.py` must stay green; `test_mcp_tools.py` is the file that
  surfaced Bug B live, so its green state after the fix is itself a guard.

### Invariant guards (must stay green, untouched)
[`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py),
[`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py).

## Acceptance Criteria
- [ ] **A**: With a live server, `sqlcg reindex --dialect auto <repo>` sends a WriterRequest
      whose `dialect` is the resolved dialect (never `"auto"`); same for `sqlcg index
      --dialect auto`. Regression test asserts the on-wire payload.
- [ ] **A**: Each of `reindex.py` / `index.py` has exactly one `if dialect == "auto"`
      block, positioned before the socket-route call.
- [ ] **B**: `init_backend(tmp)` followed by an escalating `index_repo` never opens,
      writes, or locks the default `~/.sqlcg/graph.db`. Regression test proves it via a
      raising-sentinel monkeypatch AND an mtime/absence check.
- [ ] **B**: `_escalation_db_path` has exactly one grep-confirmed call site.
- [ ] **B**: every `get_db_path()` call in `tools.py`/`writer.py`/`server.py` is either
      the captured init source (tools.py:151), a display value, or threaded as a parameter
      — the audit table holds.
- [ ] **C**: `install-hooks` over a stale sqlcg-owned hook overwrites it and prints
      `Upgraded git hook: .git/hooks/<name>`; over an identical hook it is silent and the
      file is byte-unchanged; over a foreign (no-sentinel) hook it prints the unchanged
      warning and leaves the file untouched.
- [ ] **C**: the upgrade comparison is against the fully **rendered** template (binary
      path embedded), not the raw template.
- [ ] **D**: no hook template prints "server busy/locked"; all three print
      `(reindex failed)`. `test_git_hooks_notify.py` updated accordingly.
- [ ] Version stays 1.3.0 (no bump to `pyproject.toml` / `__init__.py` / `uv.lock`).
- [ ] `git diff --stat` shows no change to `base.py` or to `indexer.py`'s hot-path methods.
- [ ] `bash scripts/generate_cli_docs.sh` yields no `docs/cli.md` diff.
- [ ] Full suite + ruff + pyright clean; the four invariant guards green.

## Risks and Mitigations
| Risk | Mitigation |
|---|---|
| Bug B fix mistaken for full §20.2 remediation | Plan explicitly scopes Bug B to "use init path + audit"; §20.2 lock/finally/stub work is out of scope (resolved decision D3). Acceptance criteria do not claim concurrency safety. |
| Bug C upgrade compares raw template, not rendered → false "identical" or false "upgrade" | Step 3.2 + acceptance criterion mandate comparing the rendered `script`; binary path is embedded. |
| Minor D text change unexpectedly fails existing hook-content tests | Step 4.4 updates `test_git_hooks_notify.py` deliberately; this is an intended text change, not a regression. |
| Bug A: removing the second `auto` resolution leaves a stale path on a code path that bypasses routing | The route call returns early when handled; the direct path now relies on the hoisted resolution. Both reindex.py and index.py keep exactly one resolution that runs unconditionally before routing — verified the route call does not consume/clear `dialect`. |
| Hoisting `get_dialect` in `reindex.py` uses the function-local import | `get_dialect` is already in the line-73 import tuple; the hoisted call sits after that import. Confirm the import precedes the new call site. |

## Rollout / Rollback
- All four fixes fold into PR #51 (no separate PR, no separate version). Per MEMORY
  "Separate PRs, don't fold": this PR has **no reviews yet**, so folding follow-up fixes
  for the same unreleased feature into it is appropriate (the rule targets folding new
  work into an *already-reviewed* PR).
- Rollback: revert the four commits; the v1.3.0 single-writer feature is unaffected
  (these are additive corrections).

### Resolved Decisions (2026-06-04 — shepherd, user-approved)
All three pre-implementation questions are resolved. None blocks implementation.

- **D1 — Version stays 1.3.0, no bump.** These fixes fold into the unreleased v1.3.0
  PR #51 (no reviews yet). Do not touch `pyproject.toml` / `__init__.py` / `uv.lock`.
  See Version note above.
- **D2 — Bug C overwrite prints a line (not silent).** On the sentinel-present /
  content-differs upgrade path, print `Upgraded git hook: .git/hooks/<name>`. Silent
  upgrades of executable hooks are worse than one line of output. True idempotency
  (identical rendered content) remains a silent skip.
- **D3 — §20.2 escalation-architecture remediation stays OUT of scope.** This plan
  covers bugs A-D only. The deeper `ARCHITECTURE_REVIEW.md` §20.2 work — (a) escalation
  outside `backend_lock`, (b) RW-lock leak on the `index_repo` failure path (no
  `finally`), (c) the dead `_set_backend_lock` / `_backend_lock` stub — remains a tracked
  separate v1.3.x follow-up. The §20.2 cross-reference is retained in Non-Goals and
  Design — Bug B so the linkage is not lost.
