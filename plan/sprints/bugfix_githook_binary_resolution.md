# Bugfix Plan: Git-hook binary resolution + loud resync failure (#126 + #1)

**Status**: REVIEWED (plan-reviewer gate 2026-06-14 returned REWORK — 1 blocker + 2 reference
drifts; shepherd folded all and resolved both forks. **B1**: the foreground schema preflight
must be a SINGLE bounded sub-second fail-soft open — on `IOException` (DuckDB exclusive lock
held ⇒ concurrent resync) it falls through to detach, never blocking 31 s and never emitting a
false stale-schema error (mandated in Design (b) + Step 2.2 + an acceptance criterion). **B2**:
`test_git_hook_install.py` is under `tests/e2e/` not `tests/unit/` — corrected. Anchor drifts
corrected (CLI gate read at reindex.py:150; server raise at tools.py:219). Both forks DECIDED:
user-base-path + `command -v` sole shell fallback; `Exit(1)` with a load-bearing stderr message.
Headline root cause CONFIRMED; four frozen perf suites untouched. READY FOR DISPATCH.)
**Branch**: `plan/githook-binary-resolution` (cut off `master` @ v1.33.1)
**Author**: architect-planner (Opus 4.8)
**Plan date**: 2026-06-14
**Version bump**: PATCH — bug fix, no new capability surface. **Reconcile the exact
number at merge**: lineage PR-5 holds v1.34.0 in flight; this ships as the next free
patch after whatever lands (`1.34.1` if 1.34.0 merges first, else `1.33.2`). Do not
hard-code the version in code until merge time.
**Source authority**:
- GitHub **#126** (install-time stale path) — ARCHITECTURE_REVIEW §5 F10 LOW.
- Validation bug **#1** (run-time silent death) — [`plan/reports/validation_bugs_2026-06-14.md`](../reports/validation_bugs_2026-06-14.md) §"#1 (BLOCKER, ops)". NOT yet a GitHub issue; user owns the tracker.
- Prior reviewed spec for the install-time half: `git show plan/sprint-backlog-q2-bugfix:plan/sprints/sprint_backlog_q2_bugfix.md` §"PR-D".

> **Out of scope, explicitly excluded**: the #122 TC6b CliRunner test-guard change that
> PR-D bundled. It is unrelated to binary resolution and is NOT part of this plan.

---

## Summary

The git hooks `sqlcg git install-hooks` writes embed a fixed `sqlcg` binary path that
drifts from the current tool, and when the embedded binary is stale the auto-resync dies
**silently** (exit 0 to the user, error only in a background log file). This plan combines
the install-time fix family (**#126**) with the run-time loud-failure fix (**#1**) into one
PATCH. It is the **actuator** that pairs with PR #154's in-graph `tool_version_stale`
detector (shipped this session): the detector *observes* drift; this plan makes the hook
*not drift silently*.

---

## Findings on current master (anchors RE-CONFIRMED @ v1.33.1)

PR-D's anchors predate this session (master moved v1.27→v1.33.1, schema v9→v10). Re-verified:

| Claim in PR-D / #126 spec | Status on master v1.33.1 | Corrected anchor |
|---|---|---|
| `_resolve_sqlcg_bin` at `git.py:63-88`, `which` first | **STALE — already fixed.** The install-time half of #126 is **already merged.** `_resolve_sqlcg_bin` now puts `site.getuserbase()/bin/sqlcg` at **step 0**, demotes `shutil.which` to step 1, and emits a `.venv` warning. | [`git.py:64-107`](../../src/sqlcg/cli/commands/git.py) |
| Hook templates embed `{sqlcg_bin}` | Confirmed. Both `post-checkout` and `post-merge` `_HookSpec.script_template` format an absolute `{sqlcg_bin}` at install time. | [`git.py:26-61`](../../src/sqlcg/cli/commands/git.py) |
| `_install_single_hook` writes the rendered template | Confirmed; also now self-upgrades a stale sqlcg-owned hook (sentinel present, body differs → overwrite). | [`git.py:110-148`](../../src/sqlcg/cli/commands/git.py) |
| Schema gate on resync | Present and **loud at the CLI layer** (`typer.Exit(1)`). | [`reindex.py:150-158`](../../src/sqlcg/cli/commands/reindex.py) (read at 150, gate 151-158) |
| Server-routed resync schema gate | Present, raises `RuntimeError` in the server path (`_assert_schema_current` 197-219, raise at 219). | [`server/tools.py:213-219`](../../src/sqlcg/server/tools.py) |

### Why the install-time half being merged changes the scope

Because #126's install-time fix already landed, **this plan is NOT "redo PR-D".** The
residual install-time gap is the one PR-D could not close by design: an
**already-installed hook** keeps whatever absolute path it baked at install time. Fixing
`_resolve_sqlcg_bin` only helps the *next* `install-hooks` run; it never repoints a hook
already on disk pointing at a stale `~/.local/bin/sqlcg`. That residual is exactly
bug **#1**.

### Root cause of #1's *silent* death (traced, not assumed)

The user's observed "exit 0, no error" is the compound of two real code paths:

1. **The hook detaches before the gate.** On the hook path (`--notify`, no live server)
   `reindex_cmd` re-spawns a **detached** child and returns immediately
   ([`reindex.py:131-133`](../../src/sqlcg/cli/commands/reindex.py), the #77 fix). The
   foreground hook prints `sqlcg: resyncing graph in background (log: …)` and exits 0 to
   `git checkout`. The schema gate at [`reindex.py:149-158`](../../src/sqlcg/cli/commands/reindex.py)
   then fires **in the detached child**, whose stdout/stderr are redirected to
   `~/.sqlcg/hook-resync.log` ([`reindex.py:280-289`](../../src/sqlcg/cli/commands/reindex.py)).
   Its `Exit(1)` is **invisible** to the user — the graph never refreshes, no error
   reaches the terminal. This is intrinsic to the async design and must stay async
   (the #77 requirement: never block `git checkout` synchronously).
2. **A stale embedded binary may pre-date the gate entirely.** The hook in the field
   points at `~/.local/bin/sqlcg` **v1.25.6 (schema v8)**. That old binary's resync path
   may gate differently — or, worse, attempt a write against a v9/v10 graph and fail in a
   way that the old binary logged but never surfaced. Either way the *current* tool's loud
   CLI gate is not the binary that runs.

So the two halves of #1 are: **(a)** the embedded path is stale → wrong binary runs;
**(b)** even the *right* binary's failure is swallowed by the detached-child log.

---

## Scope

### In scope
- **(a)** Make the hook track the current tool so it does not run a stale binary after a
  venv rebuild or a tool upgrade — covering both the *next install* and *already-installed*
  hooks.
- **(b)** Make a schema/version-mismatch resync failure **visible** to the user on a branch
  switch, despite the async detach — without reintroducing a synchronous `git checkout` hang
  (#77) and without firing on the legitimate "no server live / async still running" cases.
- Tests that model **real install configs** (editable `.venv` vs user-base PyPI) and a
  forced schema mismatch.

### Non-goals
- The #122 TC6b CliRunner guard (PR-D's other half) — explicitly excluded.
- Changing the async-detach design of #77 (the background resync stays background).
- Auto-upgrading the user's installed `sqlcg` binary, or running `db reset` for them.
- Touching the parser/indexer hot path. **The four frozen perf suites
  ([`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)) are NOT
  touched by this plan** — no change goes near `base.py` or `indexer.py`.

---

## Design

### (a) Hook should not bake a path that goes stale

**Options considered** (editable-vs-PyPI × small-vs-large-repo matrix):

| Option | Editable `.venv` install | User-base PyPI install | `uv tool` install | Survives venv rebuild? | Cost |
|---|---|---|---|---|---|
| (i) re-resolve `sqlcg` on `$PATH` at hook **run-time** (`command -v sqlcg`) | works if venv active in the shell that runs the hook — but git hooks run with a *non-interactive* env; `$PATH` may not include `.venv/bin` | works | works | yes | low; one `sh` line |
| (ii) `uv run --project <root> sqlcg` when repo is uv-managed | works, always current | n/a (not a uv project) | n/a | yes | `uv run` cold-start latency on every checkout; couples hook to uv presence |
| (iii) prefer stable user-base path (PR-D, already merged for *install*) | helps only if a user-base copy exists | works | n/a (uv tool path ≠ getuserbase) | yes for that path; **no** for already-installed hooks | done at install only |
| (iv) **hybrid**: stable path if present → else run-time `$PATH` resolution → else loud error | best coverage | best coverage | covered via PATH | yes | low |

**RECOMMENDATION — (iv) hybrid, implemented as a run-time resolver inside the hook
script.** Rationale:

- The install-time resolver (option iii) is already merged and is the right *first*
  choice, but it cannot fix a hook already on disk. The durable fix is to make the **hook
  script itself** resolve the binary at run-time, so a single embedded literal can never go
  stale.
- The hook script changes from embedding an absolute `{sqlcg_bin}` to embedding a small
  **resolution preamble** that, at checkout time:
  1. tries the install-time-resolved absolute path (still rendered in, as the preferred
     entry — preserves PR-D's user-base preference and the common fast path);
  2. if that path no longer exists/executable, falls back to `command -v sqlcg` on `$PATH`;
  3. if neither resolves, **prints a loud error to stderr and exits non-zero** — never
     silently skips.
- This is a pure `sh` template change in `_HOOKS`; it does not require `uv` and degrades
  gracefully on both small (20-ETL, often editable) and large (PyPI, user-base) repos.
- Option (ii) `uv run` is rejected as the default: it adds cold-start latency to every
  branch switch and assumes a uv-managed repo, violating the zero-friction small-repo rule.

> **DESIGN DECISION (closed by plan-review — see Resolved decisions): render the resolved
> user-base absolute path as the preferred entry + `command -v sqlcg` as the sole shell
> fallback; the shell never recomputes `getuserbase()`.** Original framing kept for context: whether the
> hook preamble's run-time `$PATH` fallback should *also* attempt `site.getuserbase()/bin`
> in shell (mirroring the Python resolver), or rely solely on `command -v`. The Python
> resolver knows `getuserbase()`; the shell does not without re-deriving it. Recommended:
> render the resolved user-base path AND a `command -v sqlcg` fallback, so the shell never
> needs to compute `getuserbase()`.

**Constant alignment**: the hook templates must not hardcode `~/.local/bin` or any path —
the preferred absolute path is whatever `_resolve_sqlcg_bin()` returns (which already
derives from `site.getuserbase()`), and the hook-resync log path stays derived from
`get_db_path().parent` ([`reindex.py:228-237`](../../src/sqlcg/cli/commands/reindex.py)),
never hardcoded — matching the `DbConfig`/`get_db_path` ownership rule.

### (b) Resync must FAIL LOUDLY on a schema/version mismatch

The schema gate already exists and is loud *at the CLI layer*; the problem is the gate runs
in the **detached child** whose output goes to a log file. Two coordinated changes:

1. **Pre-detach schema check (the key fix).** Move a *cheap, read-only* schema-version
   check to run in the **foreground hook process** — BEFORE `_spawn_detached_hook_resync`
   at [`reindex.py:131`](../../src/sqlcg/cli/commands/reindex.py). If the stored schema
   version (or the indexed `sqlcg_version` vs running version, the `tool_version_stale`
   signal) does not match, the foreground process prints a **visible, actionable** message
   to stderr and exits **non-zero** *without detaching*:

   > `sqlcg: graph schema is v8 but this tool needs v10 — the hook binary is stale. Run`
   > `'sqlcg git install-hooks' to repoint the hook, then 'sqlcg db reset && sqlcg index <root>'.`

   This is read-only (`get_schema_version()` / `get_indexed_version()`), so it does not
   risk a synchronous *write* hang — it does not reintroduce #77 (the #77 hang was the full
   *resync write* running in the foreground; a one-row metadata read is bounded and fast).

   - **New behaviour, grep-confirmed call site**: the new foreground check is invoked from
     `reindex_cmd` on the hook path (`_is_hook_path` true), immediately before the existing
     `_spawn_detached_hook_resync(path)` call at [`reindex.py:131-133`](../../src/sqlcg/cli/commands/reindex.py).
     If the planner/developer extracts it as a helper (e.g. `_hook_preflight_schema_check`),
     its only caller is `reindex_cmd`; that call site must be named and grep-confirmed in the
     PR (`grep -n "_hook_preflight_schema_check" src/sqlcg/cli/commands/reindex.py`).
   - **BOUNDED, FAIL-SOFT open (plan-review B1 — MANDATED, not developer's choice).**
     DuckDB takes an **exclusive** file lock: per [`config.py:470-490`](../../src/sqlcg/core/config.py),
     a second process **cannot open the DB at all, even read-only** (`read_only=True` still
     raises `IOException`; settled empirically in #63). On rapid successive checkouts a prior
     detached resync still holds the lock, so the preflight's open WILL contend. The two naive
     options both regress the hot path: `_open_backend_with_lock_retry()`
     ([`reindex.py:294-325`](../../src/sqlcg/cli/commands/reindex.py)) **blocks ~31 s** →
     a #77-class `git checkout` stall; a plain `get_backend(read_only=True)` raises
     `IOException` immediately → a **false "schema stale" error on a healthy graph**.
     Therefore the preflight MUST do a **single bounded open (1 attempt, hard cap well under
     1 s — NOT the 31 s lock-retry)** and, on `IOException` (lock held ⇒ a resync is already
     in flight), treat it as **"cannot determine — fall through to detach exactly as today"**:
     never block, never emit a schema-mismatch error. It opens read-only and releases before
     detaching so the child can still acquire the lock. Only a *successful* read that shows a
     real mismatch triggers the loud non-zero exit.

2. **Surface the detached child's terminal failure on the *next* hook run.** The detached
   child's `Exit(1)` writes to `~/.sqlcg/hook-resync.log`. On the next branch switch, the
   foreground preflight (change 1) catches the still-mismatched schema and surfaces it — so
   the user is not dependent on reading the log. (No new log-tailing machinery is needed;
   the preflight is the surfacing mechanism.)

**Do NOT break the legitimate async / no-server cases.** The preflight only gates on a
*schema/version mismatch* (a deterministic, fatal condition). When schema matches, the code
detaches and returns exactly as today — the "server still applying", "no server live →
background resync", and `TimeoutError → Exit(0)` paths
([`reindex.py:433-445`](../../src/sqlcg/cli/commands/reindex.py)) are untouched.

### Data models / constants
- No schema change. `SCHEMA_VERSION` and the `SchemaVersion(version, indexed_sha,
  sqlcg_version)` row are read, never written, by the preflight.
- The `tool_version_stale` signal is the `Freshness.tool_version_stale` field from
  [`freshness.py:46-51`](../../src/sqlcg/core/freshness.py) (PR #154). The preflight may
  reuse `get_indexed_version()` / `get_schema_version()` directly rather than building a
  full `Freshness` — developer's choice, documented.

### Dependencies
- **PR #154** (`tool_version_stale` detector) — shipped this session; this plan is its
  actuator. No code dependency that blocks (the preflight can gate on raw
  `get_schema_version()` even if the version-stamp column is absent on a legacy graph), but
  the user-facing message should reference version skew when `get_indexed_version()` is
  non-None.

---

## Implementation steps

### Phase 1 — (a) run-time-resilient hook script

**Step 1.1**: Change both `_HookSpec.script_template` entries in `_HOOKS` so the script
resolves the binary at run-time with the hybrid order (preferred rendered path → `command
-v sqlcg` → loud non-zero error).
- Files: [`git.py:26-61`](../../src/sqlcg/cli/commands/git.py).
- The sentinel comment lines (`# sqlcg post-checkout hook`, `# sqlcg post-merge hook`) must
  stay byte-for-byte unchanged so `_install_single_hook` idempotency / self-upgrade still
  matches ([`git.py:122-131`](../../src/sqlcg/cli/commands/git.py)).
- Acceptance: the rendered script contains a `command -v sqlcg` fallback and an explicit
  non-zero-exit error branch when no binary resolves; it no longer relies on a single
  embedded literal being valid forever.

**Step 1.2**: Because templates changed, every already-installed sqlcg hook becomes "stale
sqlcg-owned" on the next `install-hooks` run and is **auto-upgraded** by the existing
self-upgrade branch ([`git.py:126-131`](../../src/sqlcg/cli/commands/git.py)). Verify the
upgrade path emits `Upgraded git hook:` and writes the new template.
- Files: none changed; this is a behaviour to test (Step 3.x).
- Acceptance: re-running `install-hooks` over a v1.33.1-era hook rewrites it to the
  run-time-resilient form.

### Phase 2 — (b) loud foreground preflight

**Step 2.1**: Add a foreground, read-only schema/version preflight in `reindex_cmd` on the
hook path, gating before `_spawn_detached_hook_resync`.
- Files: [`reindex.py:125-133`](../../src/sqlcg/cli/commands/reindex.py).
- On mismatch: print a visible stderr message naming the stale-binary cause + the repoint
  remedy, and `raise typer.Exit(1)` (or a defined non-zero) WITHOUT detaching.
- On match: unchanged — detach and return.
- Grep-confirmed call site: the new helper (if extracted) is called only from `reindex_cmd`;
  name it in the PR description.
- Acceptance: a v8/v10 mismatch on the hook path produces a non-zero exit and a terminal
  message; a matching schema detaches and exits 0 exactly as today.

**Step 2.2**: The preflight open MUST be **single-attempt, sub-second, fail-soft** (plan-review
B1), and released before the detach path runs so the detached child does not hit a
self-inflicted lock.
- Files: [`reindex.py`](../../src/sqlcg/cli/commands/reindex.py) (preflight block).
- Use a bounded open (1 attempt / hard cap ≪ 1 s); **do NOT** call `_open_backend_with_lock_retry`
  ([`reindex.py:294-325`](../../src/sqlcg/cli/commands/reindex.py)) here — its ~31 s retry would
  stall `git checkout` (a #77-class regression).
- On `IOException` (DB exclusively locked ⇒ a resync is already in flight): **fall through to
  the existing detach path unchanged** — no block, no error. Only a successful read showing a
  real mismatch raises the loud exit.
- Acceptance:
  - matching schema → detached child still acquires the lock and completes (existing
    `test_hook_reindex_detach.py` scenarios stay green);
  - **lock held (concurrent resync in flight) → preflight does NOT block and does NOT emit a
    schema-mismatch error; it falls through to detach** (the B1 hot-path-safety criterion —
    assert via a test that holds the DB lock then runs the hook-path preflight and observes
    fall-through, not a 31 s stall and not a false stale-schema message).

---

## Test strategy

All tests are unit/integration on the CLI + git-hook surface; **no perf-suite involvement.**
Describe tests by behaviour; the developer names them `test_<unit>_<scenario>_<expected>`.

### (a) hook tracks the current tool
- **Editable-venv rebuild stays resolvable**: install a hook whose preferred rendered path
  points into a `.venv/bin/sqlcg`; delete that path (simulate venv rebuild) but keep a
  `sqlcg` on `$PATH`; run the rendered hook script (as `sh`) and assert it resolves the
  `$PATH` binary and invokes it — not a failed exec of the deleted path. Models the editable
  config.
- **User-base PyPI install fast path**: with the preferred rendered path present (a
  user-base shim) and on `$PATH`, assert the hook invokes the preferred path. Models the
  PyPI config.
- **No binary anywhere → loud error**: render the hook, remove every `sqlcg` from the
  preferred path and `$PATH`, run the script; assert non-zero exit and a stderr message —
  never a silent exit 0. (Extends [`test_git_hooks.py`](../../tests/unit/test_git_hooks.py)
  / [`test_git_hook_install.py`](../../tests/e2e/test_git_hook_install.py) — the install test
  is an **e2e** test, not unit; corrected per plan-review B2.)
- **Re-install upgrades a stale-template hook**: write an old-template sqlcg hook, run
  `install-hooks`, assert it is rewritten to the run-time-resilient template and
  `Upgraded git hook:` is printed. (Extends
  [`test_BugC_hook_upgrade.py`](../../tests/unit/test_BugC_hook_upgrade.py).)

### (b) resync fails loudly on schema mismatch
- **Hook-path schema mismatch surfaces a visible error, non-zero, no detach**: seed a DB
  with a stored schema version ≠ current `SCHEMA_VERSION`; invoke `reindex_cmd` on the hook
  path (`--notify`, no live server); assert exit code is non-zero AND the output contains
  the "schema … hook is stale … run 'sqlcg git install-hooks'" message AND
  `_spawn_detached_hook_resync` was **not** called (monkeypatch-spy). This is the exact
  silent-exit-0 failure mode from #1, inverted. (New test; sits beside
  [`test_hook_reindex_detach.py`](../../tests/unit/test_hook_reindex_detach.py).)
- **Matching schema still detaches and exits 0**: regression guard that change (b) did NOT
  break the async path — reuse the existing detach scenarios; assert exit 0 and the
  background-notice line.
- **No-server / timeout cases untouched**: assert the `TimeoutError → Exit(0)` and
  no-live-server fall-through behaviours are unchanged (the preflight only gates on schema
  mismatch). Cross-references the CLOSED #77 contract: the foreground does only a bounded
  read, never a synchronous write resync.

---

## Acceptance criteria (OBSERVABLE)

- [ ] Rendered hook script (from `_HookSpec.script_template`) contains a `command -v sqlcg`
      run-time fallback and an explicit non-zero error branch; it no longer depends on a
      single embedded absolute literal remaining valid. (grep the rendered script in a test)
- [ ] A hook installed against a `.venv/bin/sqlcg` path still invokes a working `sqlcg`
      after that path is deleted (venv-rebuild simulation), via `$PATH`. (test asserts the
      invoked binary, exit 0)
- [ ] With no `sqlcg` resolvable anywhere, the hook exits **non-zero** and prints a stderr
      error — never silent exit 0. (test asserts exit code + message)
- [ ] Re-running `install-hooks` over a pre-v1.34 sqlcg hook rewrites it to the new template
      and prints `Upgraded git hook:`. (test asserts file content + output)
- [ ] On the hook path, a stored-schema-version ≠ current-`SCHEMA_VERSION` produces a
      **non-zero exit** and a terminal message naming the stale binary and the
      `sqlcg git install-hooks` remedy, and `_spawn_detached_hook_resync` is NOT called.
      (test asserts exit code, message substring, spy-not-called)
- [ ] A matching schema on the hook path still detaches and exits 0 with the existing
      background-notice line. (existing detach tests remain green)
- [ ] `TimeoutError`/no-live-server paths are unchanged. (existing tests remain green)
- [ ] Every new helper has a grep-confirmed single call site named in the PR.
- [ ] No TODO in any changed happy path.
- [ ] The four frozen perf suites are untouched and still green (not modified by this PR).

---

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Foreground read introduces a checkout slowdown (re-opening #77) | LOW | MED | Preflight is a single-row metadata READ with the existing lock-retry; bound it, never run a write in the foreground. Test asserts no `resync_changed` call in the mismatch path. |
| Run-time `$PATH` in a non-interactive git-hook env lacks `.venv/bin` | MED | LOW | Render the install-time-resolved preferred path FIRST; `$PATH` is only the fallback. Loud error if both miss — never silent. |
| Template change makes every existing hook "stale" and noisy on re-install | LOW | LOW | Self-upgrade branch already overwrites + prints once; idempotent thereafter. |
| Preflight read holds the lock, blocking the detached child | LOW | MED | Release the backend before detaching (Step 2.2); covered by the matching-schema detach regression test. |

---

### Resolved decisions (both forks closed by plan-review 2026-06-14)

1. **Shell fallback scope — DECIDED.** Render the resolved **user-base absolute path** as the
   preferred entry; use **`command -v sqlcg` as the sole shell fallback**. The shell does NOT
   recompute `getuserbase()` (that would reimplement `_resolve_sqlcg_bin` fragilely) and does
   NOT hardcode `~/.local/bin` (house rule). `command -v` covers `uv tool`, user-base-on-PATH,
   and active-venv cases. No maintainer input required.
2. **Exit-code convention — DECIDED: `typer.Exit(1)` with an advisory stderr message.** Git
   runs `post-checkout`/`post-merge` **after** the operation and ignores the hook's exit
   status — a non-zero exit cannot abort/block `git checkout`. So the **stderr message is the
   load-bearing part**, not the code; the acceptance test asserts the message substring (it
   already does). `Exit(1)` matches the existing CLI gate at [`reindex.py:158`](../../src/sqlcg/cli/commands/reindex.py).

---

## Plan Compliance

_(to be filled by architect-planner after the developer says "finished")_
