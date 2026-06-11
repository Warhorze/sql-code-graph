# Feature Plan: MCP Server Self-Healing on Reinstall (issue #76)

**Status: DRAFT** — plan-reviewer gates next. No implementation until reviewed.

**Target version: 1.20.0** (new runtime surface — additive, nothing breaks → minor per CLAUDE.md SemVer rule. master is at 1.19.1).

GitHub issue: [#76](https://github.com/Warhorze/sql-code-graph/issues/76) — "Reinstalling sqlcg leaves a stale MCP server; Claude Code needs a manual /mcp reconnect".

---

## Summary

A long-running stdio MCP server keeps executing the *old* build after the package is
reinstalled out-of-band (`uv tool install --force`, `uv sync`), and even after
`sqlcg install` stops it the editor does not transparently respawn it. Make the server
**self-heal**: on a cheap, off-hot-path check it compares its own baked-in
`sqlcg.__version__` against the version of the distribution currently on disk; on a
skew it finishes the in-flight request, quiesces the writer + closes the DuckDB
connection, then `os.execv`'s the freshly-installed binary **in place (same PID)** so
the MCP client's stdio pipe never breaks and no `/mcp` reconnect is needed.

---

## Scope

### In Scope
- A **version-skew detector**: read the on-disk distribution version freshly (bypassing
  the long-lived process's cached `importlib.metadata`), compare to baked-in
  `__version__`, return a boolean + the two versions for logging.
- A **self-heal trigger** wired off any parser/indexer hot path — invoked on the
  control-socket `status` op and on MCP tool entry via a single shared guard, gated by a
  monotonic-clock throttle so it never runs more than once per N seconds.
- A **graceful re-exec sequence**: set shutdown_requested → acquire `backend_lock`
  (waits for any active drain to commit) → `shutdown_backend()` (closes DuckDB, releases
  the exclusive lock — issue #63) → remove control files → `os.execv` preserving fds 0/1/2
  and env.
- Correct the overpromising `install.py` message and document the reinstall flow.
- Replace the `mcp restart` "deferred to v1.2" docstring with the shipped behaviour.

### Non-Goals
- **No changes to `src/sqlcg/parsers/**` or `src/sqlcg/indexer/**`.** This feature must
  not touch any performance invariant in CLAUDE.md. The detector runs only on
  control-socket ops and MCP tool entry, never inside parse/qualify/upsert loops.
- No new MCP tool surface; no schema change; no protocol-framing change.
- No attempt to migrate a stale *graph* — re-exec picks up the new code only; the DB
  schema gate (`_assert_schema_current`) already handles a schema-version mismatch on
  the re-exec'd process's `init_backend`.
- No Windows re-exec path (the control socket / drain stack is already
  `sys.platform != "win32"` only in `server.py`). On Windows the detector logs a skew
  and no-ops; the fallback message path still applies.
- No periodic background timer task — the throttle is checked *lazily* on activity, so an
  idle server with no tool calls simply re-execs on the next call after a reinstall.

---

## Design

### D1 — Reading the on-disk distribution version (cache pitfall)

A process that has already imported `sqlcg` caches `importlib.metadata` results: a plain
`importlib.metadata.version("sql-code-graph")` may return the *old* version from the
in-memory `PathDistribution` cache, defeating the whole feature. We must force a fresh
read from disk on every check.

**Decision:** read via a freshly constructed `MetadataPathFinder` scan rather than the
cached top-level helper.

```python
import importlib.metadata as im

def read_ondisk_version() -> str | None:
    """Version of the sql-code-graph distribution on disk RIGHT NOW.

    A long-lived process caches distribution metadata; this re-scans sys.path
    so a reinstall under the running interpreter is observed. Returns None if
    the distribution cannot be located (treated as "no skew" — never re-exec
    on an unreadable version; see failure mode F1).
    """
    try:
        # importlib.metadata.distributions() re-walks the finders on each call;
        # it is NOT memoised the way the module-level version() helper is.
        for dist in im.distributions():
            if (dist.metadata["Name"] or "").lower() == "sql-code-graph":
                return dist.version
    except Exception:
        return None
    return None
```

- `im.distributions()` re-walks `sys.path` / the meta-path finders each call (verified:
  it constructs fresh `PathDistribution` objects), so it observes a reinstall that
  replaced the `.dist-info` directory under the same `sys.path` entry.
- We compare against `from sqlcg import __version__` (the constant baked into the running
  module at import — it does NOT change when disk changes; that asymmetry is the whole
  skew signal).
- **Edge:** if `uv tool install --force` swaps the *entire* venv directory (new path,
  old path unlinked), `sys.path` still points at the old (now-unlinked) entry until
  re-exec. The console-script shim re-resolves the venv on the *next* spawn, so the
  re-exec'd process gets the new code — this is why we re-exec the **console-script
  path**, not `sys.executable -m` against the stale `sys.path`. See D4.

> The reviewer must confirm `im.distributions()` is not memoised in the target Python
> (3.12). If it is, fall back to clearing `im.MetadataPathFinder.invalidate_caches()` +
> `importlib.invalidate_caches()` before the scan. This is a **blocking item for the
> reviewer** — pin the exact mechanism before the developer codes it.

### D2 — Check cadence (off every hot path)

- **Where:** a single function `maybe_self_heal()` is called at exactly two activity
  points: (a) the top of the control-socket `status` handler, and (b) a shared guard at
  MCP tool entry (see D3 for the wiring point). Never inside the drain body, never inside
  `index_repo`'s parse loop.
- **Throttle:** module-level `_last_skew_check: float` using `time.monotonic()`. If
  `monotonic() - _last_skew_check < _SKEW_CHECK_INTERVAL_S` the call returns immediately
  without touching disk. **`_SKEW_CHECK_INTERVAL_S = 30.0`** (constant, documented).
  Rationale: a `distributions()` scan is ~1–5 ms; bounding it to once / 30 s makes it
  invisible even under rapid tool-call bursts, while still re-execing within 30 s of a
  reinstall on the next activity.
- The throttle is the **first** statement so a hot caller pays only a monotonic subtract.

### D3 — MCP tool-entry wiring point (grep-confirmed call site required)

The cleanest single hook is FastMCP's tool dispatch. Two candidate sites — the developer
picks whichever is grep-confirmed to wrap **every** `@mcp.tool()` invocation exactly once:

1. **Preferred:** wrap `mcp._mcp_server.call_tool` (the low-level handler FastMCP routes
   every tool through) with a thin async pre-check that calls `maybe_self_heal()` then
   delegates. Register this in `server.py` `main()` after tools import, before
   `anyio.run`.
2. **Fallback (if call_tool is not a single interception point):** add one
   `maybe_self_heal()` line at the top of `_get_backend()` in `tools.py` — every tool
   that touches the graph calls it. Downside: tools that don't hit the backend skip the
   check; acceptable because the `status` op also checks.

> **Reviewer decision required:** confirm via grep which interception point wraps all
> tool calls once. Do not ship a hook that double-fires or misses tools. The developer
> must show the grep output for the chosen site in the PR.

`maybe_self_heal()` is **synchronous and non-blocking** in the common (no-skew) path. On
skew it must hand off to the async re-exec sequence (D5) because draining requires
acquiring the anyio `backend_lock`. Implementation: the detector sets a module-level
`_self_heal_requested = anyio.Event`; the existing `_stop_watcher`-style task observes it
(see D5). The tool-entry hook never blocks the event loop itself.

### D4 — The `os.execv` invocation

```python
import os, sys

def _reexec() -> None:
    # argv[0]: prefer the console-script path the editor actually launched, so
    # the re-exec'd process re-resolves the (possibly relocated) venv. sys.argv[0]
    # is that path for an installed `sqlcg` entry point.
    script = sys.argv[0]
    os.execv(script, [script, *sys.argv[1:]])
```

- **argv[0] resolution:** use `sys.argv[0]` (the console-script path the editor
  configured, e.g. the `sqlcg` shim), **not** `sys.executable -m sqlcg.server`. The shim
  re-resolves the active distribution at spawn, which is exactly what picks up a
  force-reinstalled venv. We pass the original argv tail unchanged so the new process
  starts in the identical server mode (`sqlcg mcp serve` or whatever the editor launched).
- **stdio:** `os.execv` replaces the process image but **keeps all open file descriptors**
  by default (no `close_on_exec` is set on fds 0/1/2). The MCP JSON-RPC pipe on fd 1 (and
  stdin fd 0, stderr fd 2) survives the exec untouched, so the client's pipe never breaks.
  **Guard:** the developer must confirm no code sets `O_CLOEXEC` on the duped fd in
  `server.py` (`_real_stdout_buffer = os.fdopen(os.dup(1), ...)` — `os.dup` does NOT set
  CLOEXEC, and that buffer is process-local, discarded by the exec). The *real* fds 0/1/2
  are inherited by the new image.
- **env:** `os.execv` (no `e`) inherits the current environment, which is correct —
  `SQLCG_DB_PATH` etc. carry over. We deliberately do **not** use `execve` with a curated
  env; the inherited env is what the editor set.

### D5 — Drain-safe quiescence + DuckDB lock release (issue #63)

Re-exec must happen only after the writer is quiesced and the DuckDB exclusive lock is
released, or the new process cannot open the DB (issue #63 context).

Reuse the **existing** shutdown machinery in `server.py` (`_stop_watcher`) rather than
inventing a parallel path. Add a `_self_heal_event = anyio.Event()` alongside the
existing `stop_event`, and a watcher task `_self_heal_watcher` that mirrors
`_stop_watcher`'s ordering but ends in `_reexec()` instead of `os._exit(0)`:

1. `_self_heal_event.wait()` (set by `maybe_self_heal()` on detected skew).
2. `shutdown_requested.set()` — the drain loop finishes its current drain and starts no new one.
3. `async with backend_lock:` — blocks until any **active** drain has committed its
   transaction (same guarantee `_stop_watcher` relies on).
4. `_tools.shutdown_backend()` under the lock — **closes the DuckDB connection, releasing
   the exclusive lock** so the re-exec'd process's `init_backend` can open it.
5. `cleanup_control_files(db_path)` — remove `.sock` and `.pid` (the new process rewrites
   `.pid` with the *same* PID and re-binds the socket; see D6).
6. `_reexec()`.

**Mid-drain new request arrival:** `shutdown_requested.set()` (step 2) makes the drain
loop's `while not shutdown_requested.is_set()` exit after the active drain; new enqueues
sit in `_pending` and are simply dropped by the exec — acceptable, because the new
process is the *same* server the client talks to, and a `wait=false` enqueue is
fire-and-forget. A `wait=true` client would lose its stream; **mitigation:** the watcher
relays a terminal `{"ok": false, "done": true, "error": "server self-healing — re-issue"}`
frame to any pending `_waiters` before exec (reuse the coalesce-notify pattern already in
`WriterQueue.enqueue`). The reviewer should confirm this is reachable for `wait=true`
index/reindex.

### D6 — Control socket + pidfile across exec (same PID)

- `os.execv` preserves the **PID**. The pidfile records `{pid, db_path, started_at}`; the
  new image rewrites it via the existing `write_pid()` in `main()`. Because the PID is
  unchanged, any concurrent `stop_server()` SIGTERM-by-pid fallback still targets the
  right process. **We remove the pidfile in step 5 and let `main()` recreate it** rather
  than leaving a stale `started_at` — observable: `started_at` resets, which correctly
  reflects the new image's start.
- The **socket** is an `AF_UNIX` listener bound to a path; the listener fd does NOT
  survive cleanly as a *listening* socket across exec for our purposes (the new
  `_control_socket_task` calls `create_unix_listener` again). We therefore unlink the
  socket file in step 5 (`cleanup_control_files`) so the new process's
  `create_unix_listener(str(sp))` does not fail with `EADDRINUSE` on a stale path. There
  is a sub-millisecond window where `mcp status` would see no socket → it already falls
  through to the PID probe (R3), which finds the same live PID. Acceptable, documented.

### D7 — Fallback scope (if execv proves too risky in review)

If the reviewer judges re-exec too risky for 1.20.0, the **minimum** shipped scope is
PR 1 alone (below): correct the `install.py` message and the `mcp restart` docstring,
and document the reinstall flow. This still closes the "overpromising message" half of
issue #76. The skew **detector** (D1) ships regardless because `mcp status` already
consumes a version-skew signal — it is low-risk and useful standalone.

---

## PR Split

**Two PRs.** Justification: the message/docstring fix + the pure detector are low-risk,
fully unit-testable, and independently valuable (they improve `mcp status` and `sqlcg
install` guidance even if re-exec is deferred). The `os.execv` re-exec machinery is the
high-risk, e2e-gated part and benefits from isolated review. Splitting lets the fallback
scope (D7) ship as PR 1 if PR 2 is rejected.

- **PR 1 — message fix + version-skew detector (no behaviour change to the running loop).**
  - `install.py` message correction; `mcp restart` docstring rewrite; reinstall-flow docs.
  - `read_ondisk_version()` + a pure `detect_skew()` returning `(skew: bool, running, ondisk)`.
  - Wires the detector into `mcp status` output (replaces the existing CLI-side
    `__version__` compare with the on-disk read so `stale_by_version` is meaningful for an
    out-of-band reinstall). Version bump to **1.20.0** lands here.
- **PR 2 — self-heal re-exec.**
  - `maybe_self_heal()` throttle + event signalling.
  - `_self_heal_watcher` task + `_reexec()`.
  - Tool-entry hook (D3).
  - subprocess e2e (T-E2E below).

---

## Implementation Steps

### Phase 1 (PR 1): message + detector

**Step 1.1** — Correct the `install.py` tail message.
- Files: [`src/sqlcg/cli/commands/install.py`](src/sqlcg/cli/commands/install.py) ~145–149.
- Replace "your editor will respawn it on the new build" with honest guidance:
  "Stopped the running MCP server. In Claude Code run `/mcp` → reconnect
  sql-code-graph (or restart the session) to pick up the new build." Keep the
  no-server branch.
- Acceptance: `test_install.py` asserts the new string; no test asserts the old
  overpromise.

**Step 1.2** — Rewrite the `mcp restart` docstring.
- Files: [`src/sqlcg/cli/commands/mcp.py`](src/sqlcg/cli/commands/mcp.py) 277–291.
- Remove "deferred to v1.2"; describe the shipped self-heal behaviour (server now
  re-execs itself on next activity after a reinstall; manual restart only needed if the
  process is wedged).
- Acceptance: no source string contains "deferred to v1.2" (grep guard in a test).

**Step 1.3** — `read_ondisk_version()` + `detect_skew()`.
- Files: new module [`src/sqlcg/server/selfheal.py`](src/sqlcg/server/selfheal.py).
- `read_ondisk_version() -> str | None` per D1; `detect_skew() -> tuple[bool, str, str | None]`
  comparing `__version__` to the on-disk read. Returns `(False, ...)` when on-disk is None.
- Acceptance: unit tests (T1–T3 below).

**Step 1.4** — Use the on-disk read in `mcp status`.
- Files: [`src/sqlcg/cli/commands/mcp.py`](src/sqlcg/cli/commands/mcp.py) ~133–140.
- The CLI-side compare currently uses the CLI process's `__version__`; for an out-of-band
  reinstall the CLI process IS the new version, so that compare already works. Keep it,
  but route through `detect_skew()` for a single source of truth on the comparison logic.
- Acceptance: `TestMcpStatusVersionDrift` in `test_mcp_control.py` still passes; assert
  `stale_by_version` reflects a simulated skew.

**Step 1.5** — Version bump to 1.20.0.
- Files: [`pyproject.toml`](pyproject.toml), [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py), `uv lock`.
- Acceptance: `test_version_parity.py` green.

### Phase 2 (PR 2): self-heal re-exec

**Step 2.1** — `maybe_self_heal()` throttle + signal.
- Files: [`src/sqlcg/server/selfheal.py`](src/sqlcg/server/selfheal.py).
- Module-level `_last_skew_check`, `_SKEW_CHECK_INTERVAL_S = 30.0`, and a setter for the
  `anyio.Event` injected by `server.py`. On detected skew: log loudly at WARNING
  (`running=…, ondisk=…, re-execing`) and set the event. On no skew or unreadable on-disk:
  return immediately.
- Acceptance: T4 (throttle), T5 (skew sets event), T6 (unreadable → no event).

**Step 2.2** — `_reexec()` + `_self_heal_watcher`.
- Files: [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py).
- Add `_self_heal_event`, register it with `selfheal`, spawn `_self_heal_watcher` in
  `_run_with_control`'s task group (next to `_stop_watcher`). Watcher follows D5 ordering,
  relays terminal frames to `_waiters`, ends in `_reexec()`.
- **Failure handling (F2):** wrap `_reexec()` in try/except. If `os.execv` raises
  (`OSError`), log at ERROR (`self-heal re-exec FAILED, continuing on old build vX: <err>`),
  **re-open the backend** (`init_backend(db_path)`) so the server keeps serving, clear
  `shutdown_requested`, and resume the drain loop. The server MUST keep working on the old
  build rather than dying. This is the explicit "keep serving on the old build, log
  loudly" requirement.
- Acceptance: T7 (watcher ordering: drain finishes before backend close), T-E2E.

**Step 2.3** — Tool-entry hook.
- Files: [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py) main()/wiring, or
  [`src/sqlcg/server/tools.py`](src/sqlcg/server/tools.py) `_get_backend()` per D3.
- Grep-confirm the single interception point; show grep in the PR.
- Acceptance: T-E2E (a tool call after a swap triggers re-exec).

---

## Failure Modes

| # | Mode | Handling |
|---|------|----------|
| F1 | On-disk version unreadable (`distributions()` returns None) | `detect_skew` returns `(False, …)`; never re-exec on an unknown version. |
| F2 | `os.execv` raises | Catch `OSError`; log ERROR loudly; re-open backend, clear `shutdown_requested`, keep serving old build. Server never dies on a failed re-exec. |
| F3 | New request arrives mid-drain | `shutdown_requested` stops new drains; pending `wait=true` waiters get a terminal "self-healing — re-issue" frame; `wait=false` enqueues are dropped (fire-and-forget, re-issuable). |
| F4 | DuckDB exclusive lock held across exec (issue #63) | Step D5.4 closes the connection under `backend_lock` BEFORE exec; the new image opens the DB cleanly. Covered by T-E2E asserting the re-exec'd server answers a query. |
| F5 | Stale socket window during exec | `cleanup_control_files` unlinks `.sock`; new process re-binds. `mcp status` falls through to PID probe (R3) and finds the same live PID. |
| F6 | WSL2 specifics | No WSL2-specific syscall is used; `os.execv` + `AF_UNIX` behave identically to native Linux. The only WSL2 note: file-watch/inode timing on the `.dist-info` swap — the 30 s throttle + lazy check absorb any filesystem latency. No special handling needed; documented so the reviewer doesn't expect a WSL2 branch. |
| F7 | Windows | Self-heal path is `sys.platform != "win32"`-gated (matches existing control-socket gating). On Windows the detector still powers `mcp status`; the message/doc fallback applies. |

---

## Test Strategy

> Tests assert **observable output** (CLAUDE.md). Describe by behaviour; the developer
> names `test_<unit>_<scenario>_<expected>` and links this plan in the docstring.

### Unit (PR 1)
- **T1** `read_ondisk_version` returns the installed distribution's version string
  (assert equals `importlib.metadata.version("sql-code-graph")` in the clean test env).
- **T2** `read_ondisk_version` returns None when the distribution is absent (monkeypatch
  `im.distributions` to yield nothing) — asserts the None branch, not an exception.
- **T3** `detect_skew` returns `(True, running, ondisk)` when on-disk differs from
  `__version__` (monkeypatch `read_ondisk_version` to a different string) and
  `(False, …)` when equal or None.
- **T-install** `sqlcg install` non-dry-run prints the corrected reconnect message
  (CliRunner stdout assertion).
- **T-grep** no source file under `src/` contains the literal "deferred to v1.2".

### Unit (PR 2)
- **T4** `maybe_self_heal` called twice within the throttle window touches disk once
  (monkeypatch a counting `read_ondisk_version`; assert call count == 1; advance a fake
  monotonic clock and assert the second window scans again).
- **T5** on skew, `maybe_self_heal` sets the injected event and logs at WARNING with both
  versions (assert event.is_set() and caplog content).
- **T6** on unreadable on-disk version, the event is NOT set.
- **T7** drain-ordering: with an active drain holding `backend_lock`, the self-heal
  watcher does not call `shutdown_backend` until the lock is released (use a fake backend
  recording call order; assert `clear_all_tables`/commit precedes `close`). Mirrors the
  `_stop_watcher` test harness if one exists.

### E2E (PR 2) — subprocess, the hard part
- **T-E2E** `test_selfheal_e2e.py` (under `tests/e2e/`):
  1. Build/install sqlcg at version A into a throwaway venv (or monkeypatch the on-disk
     version marker the detector reads — see note).
  2. Spawn the server as a subprocess on a tmp DB; send `initialize` + a real tool call
     over stdio; assert it answers and `serverInfo.version == A`.
  3. **Swap the installed version marker to B** (the realistic lever: replace the
     `.dist-info`'s `METADATA` Version line in the test venv so `distributions()` reports
     B without re-importing).
  4. Send another tool call (or `status`); assert: (a) the response **still arrives** on
     the same stdio pipe — the client never reconnected — and (b) the server now reports
     `version == B` (re-exec'd in place, same PID).
  5. Assert the DB is openable by the re-exec'd process (proves F4 — lock released).
- **Note on the swap mechanism:** building a second real wheel is heavy; the reviewer
  should confirm whether the e2e edits the `.dist-info` METADATA Version (cheap, exercises
  the real `distributions()` read path) or stands up two wheels. Editing METADATA is the
  recommended cheap path because `read_ondisk_version` reads exactly that field.
- This e2e is **subprocess-based** because re-exec cannot be exercised in-process (it
  replaces the test interpreter). The unit tests cover the detector + ordering; the e2e
  covers the actual exec + stdio survival + lock release.

---

## Acceptance Criteria (observable)

- [ ] After an out-of-band reinstall (version marker swapped A→B) and one subsequent MCP
      tool call, the server answers on the **same stdio pipe** and `serverInfo.version`
      reports B — no `/mcp` reconnect performed (T-E2E).
- [ ] The re-exec'd process opens the DuckDB file successfully (no "could not set lock"
      error) — proving the connection was closed before exec (T-E2E, F4).
- [ ] When `os.execv` fails, the server logs an ERROR naming both versions and continues
      answering tool calls on the old build (T-F2 / observable log + a follow-up tool call
      succeeds).
- [ ] `maybe_self_heal` scans disk at most once per `_SKEW_CHECK_INTERVAL_S` (T4).
- [ ] `sqlcg install` prints the corrected reconnect message; no string promises
      automatic respawn (T-install).
- [ ] No source string contains "deferred to v1.2" (T-grep).
- [ ] `sqlcg mcp status` reports `stale_by_version: true` after a marker swap and false
      when versions match (T3 + control test).
- [ ] `test_version_parity.py` green at 1.20.0.
- [ ] No file under `src/sqlcg/parsers/` or `src/sqlcg/indexer/` is modified (diff check).
- [ ] Every new function has a grep-confirmed call site shown in the PR (`maybe_self_heal`,
      `_reexec`, `read_ondisk_version`, `detect_skew`, `_self_heal_watcher`).

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `importlib.metadata` caching returns stale on-disk version → never re-execs | D1 uses `distributions()` (re-walks finders); reviewer pins the exact non-memoised mechanism before coding. **Blocking item.** |
| `os.execv` on a relocated venv re-resolves to old code | Re-exec the console-script path (`sys.argv[0]`), not `sys.executable -m`; the shim re-resolves the active venv. |
| Re-exec mid-drain corrupts the graph | Drain commits under `backend_lock` before `shutdown_backend`; DuckDB transaction is atomic — a not-yet-started drain is simply dropped. |
| CLOEXEC on a server fd breaks the client pipe after exec | Developer confirms no `O_CLOEXEC` is set on fds 0/1/2; `os.dup` buffer is process-local and discarded by exec. |
| Throttle masks a needed re-exec on a fully idle server | Acceptable: an idle server has no client traffic to serve stale; the next tool call re-execs within 30 s. Documented non-goal (no background timer). |
| Scope too large for one PR | Split into PR 1 (message + detector) and PR 2 (re-exec); PR 1 is the fallback scope. |

---

## User Verification Queue

Per project convention the agent never tags releases; the user verifies on the live DWH
and tags after merge. **Pending tags not yet pushed (carry forward, do not lose):**
`v1.15.0`, `v1.16.0`, `v1.17.0`, `v1.18.0`, `v1.19.0`, `v1.19.1`, and — after this ships —
`v1.20.0`. Confirm the exact pending set against `git tag -l` before tagging; the
memory note "Graph-health sprint state" cleared v1.6.0–v1.14.2 but v1.15.0–v1.19.1 remain
unverified/untagged.

---

## Blocking Questions (resolve before plan-reviewer sign-off)

1. **(D1)** Confirm `importlib.metadata.distributions()` is non-memoised on Python 3.12 in
   this environment so a reinstall under the running interpreter is observed without an
   explicit `invalidate_caches()`. If memoised, pin the cache-busting call.
2. **(D3)** Confirm via grep the single tool-dispatch interception point that wraps every
   `@mcp.tool()` call exactly once (FastMCP `_mcp_server.call_tool` vs `_get_backend()`).
3. **(D7)** Decide whether PR 2 (re-exec) is in-scope for 1.20.0 or deferred, leaving PR 1
   as the shipped scope.
