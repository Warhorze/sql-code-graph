# Feature Plan: MCP Server Self-Healing on Reinstall (issue #76)

**Status: APPROVED** (plan-reviewer 2026-06-11, APPROVE-WITH-AMENDMENTS — all A1–A6 /
W1–W4 amendments incorporated; see Review Trail). Ready for developer.

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

**Decision (resolved by plan-reviewer, A1):** invalidate the metadata finder cache, then
scan `im.distributions()`.

```python
import importlib.metadata as im

def read_ondisk_version() -> str | None:
    """Version of the sql-code-graph distribution on disk RIGHT NOW.

    A long-lived process caches distribution metadata so the on-disk version
    can be observed stale. We invalidate the finder cache first, then re-scan,
    so a reinstall under the running interpreter is observed regardless of the
    .dist-info mtime. Returns None if the distribution cannot be located
    (treated as "no skew" — never re-exec on an unreadable version; see F1).
    """
    try:
        # Clears the @lru_cache on FastPath.__new__ so the path list is re-read
        # from disk even when the .dist-info was replaced with the SAME mtime
        # (coarse-grained FS, same-second CI write). Without this, the mtime-keyed
        # FastPath.lookup cache could return the stale child list.
        im.MetadataPathFinder.invalidate_caches()
        for dist in im.distributions():
            if (dist.metadata["Name"] or "").lower() == "sql-code-graph":
                return dist.version
    except Exception:
        return None
    return None
```

- **Correct mechanism (verified on Python 3.12.3):** `im.distributions()` →
  `Distribution.discover()` → `MetadataPathFinder._search_paths()` maps `FastPath` over
  each `sys.path` entry. `FastPath.__new__` is `@functools.lru_cache`'d per path string,
  and `FastPath.lookup` is a `@method_cache` keyed on the directory `mtime`. When
  `uv tool install --force` rewrites the `.dist-info` in place the directory mtime
  changes, so `lookup` re-keys and the child scan runs fresh — a reinstall IS observed
  WITHOUT any explicit cache bust in the common case. (The earlier "constructs fresh
  PathDistribution objects" rationale was wrong; the real lever is the mtime-keyed
  lookup cache.)
- **Why `invalidate_caches()` is still required:** if the `.dist-info` is replaced with
  the SAME mtime (coarse-grained filesystem or a same-second write in CI/tests), the
  mtime-keyed lookup would not re-key. `im.MetadataPathFinder.invalidate_caches()` clears
  the `FastPath.__new__` lru_cache, forcing a fresh path-list read regardless of mtime.
  This is the defensive call the developer MUST place before the scan.
- We compare against `from sqlcg import __version__` (the constant baked into the running
  module at import — it does NOT change when disk changes; that asymmetry is the whole
  skew signal).
- **Edge:** if `uv tool install --force` swaps the *entire* venv directory (new path,
  old path unlinked), `sys.path` still points at the old (now-unlinked) entry until
  re-exec. The console-script shim re-resolves the venv on the *next* spawn, so the
  re-exec'd process gets the new code — this is why we re-exec the **console-script
  path**, not `sys.executable -m` against the stale `sys.path`. See D4.

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

**Resolved by plan-reviewer (A2).** `mcp._mcp_server.call_tool` is a **decorator
factory** (it registers a handler), NOT a runtime callable — wrapping it does nothing at
dispatch time. The single runtime dispatch point is the registered handler in
`mcp._mcp_server.request_handlers[types.CallToolRequest]`: FastMCP installs exactly one
handler there (via `call_tool(...)(self.call_tool)` in `FastMCP.__init__`), and every
`@mcp.tool()` invocation flows through it.

**Wiring (the only approved pattern):** in `server.py` `main()`, AFTER
`import sqlcg.server.tools` (which triggers tool registration):

```python
import mcp.types as types
_orig_handler = mcp._mcp_server.request_handlers[types.CallToolRequest]

async def _self_heal_then_dispatch(req):
    maybe_self_heal()           # throttled; sets the event on skew
    return await _orig_handler(req)

mcp._mcp_server.request_handlers[types.CallToolRequest] = _self_heal_then_dispatch
```

This wraps every tool exactly once. The developer MUST show grep output confirming
`request_handlers[types.CallToolRequest]` is the sole registered dispatch path and that
the wrap is installed once. Do NOT use the `_get_backend()` fallback (it misses tools
that never touch the backend and is fragile).

`maybe_self_heal()` is **synchronous and non-blocking** in the common (no-skew) path. On
skew it must hand off to the async re-exec sequence (D5) because draining requires
acquiring the anyio `backend_lock`. Implementation: the detector sets a module-level
`_self_heal_requested = anyio.Event`; the existing `_stop_watcher`-style task observes it
(see D5). The tool-entry hook never blocks the event loop itself.

### D4 — The `os.execv` invocation

```python
import os, sys

# Resolved ONCE at server startup in main(), BEFORE any chdir / arg mutation.
# Module-level so _reexec uses the absolute launch path, not a relative sys.argv[0].
_resolved_argv0: str | None = None  # set in main(): os.path.abspath(sys.argv[0])

def _reexec() -> None:
    # A4 loop guard: refuse after 3 generations (corrupt/partial install).
    gen = int(os.environ.get("SQLCG_SELF_HEAL_GENERATION", "0"))
    if gen >= 3:
        logger.error(
            "self-heal re-exec refused: SQLCG_SELF_HEAL_GENERATION=%d — "
            "the new build still reports a skew; restart the MCP server manually",
            gen,
        )
        return
    # A5: use the absolute launch path captured at startup; verify it is executable.
    if not (_resolved_argv0 and os.path.isfile(_resolved_argv0) and os.access(_resolved_argv0, os.X_OK)):
        logger.error("self-heal re-exec refused: argv0 %r is not an executable file", _resolved_argv0)
        return  # caller (F2 path) re-opens the backend and keeps serving
    os.environ["SQLCG_SELF_HEAL_GENERATION"] = str(gen + 1)
    os.execv(_resolved_argv0, [_resolved_argv0, *sys.argv[1:]])
```

- **argv[0] resolution (A5):** `main()` stores
  `_resolved_argv0 = os.path.abspath(sys.argv[0])` at startup (the console-script shim the
  editor launched, e.g. `/home/…/.local/bin/sqlcg`, which re-resolves the active
  distribution on spawn — exactly what picks up a force-reinstalled venv). We use the
  absolute path, **not** `sys.argv[0]` at exec time (which could be relative after a
  chdir) and **not** `sys.executable -m sqlcg.server`. The argv tail is passed unchanged
  so the new process starts in the identical server mode. If `_resolved_argv0` is not an
  executable file at exec time, `_reexec` refuses and the F2 path keeps serving.
- **stdio (A3 — corrected):** `os.execv` replaces the process image but inherited fds keep
  their `close_on_exec` flag. The **real fd 1** (the MCP JSON-RPC pipe), fd 0 (stdin) and
  fd 2 (stderr) are `CLOEXEC=False` (verified) and survive the exec untouched, so the
  client's pipe never breaks. The process-local `_real_stdout_buffer = os.fdopen(os.dup(1), …)`
  IS `CLOEXEC=True` on Python 3.12/Linux (`os.dup` returns a CLOEXEC fd) and is therefore
  **closed by the exec** — that is fine and intended, because the re-exec'd image re-runs
  module scope and re-creates `_real_stdout_buffer` from the still-open fd 1.
  **Guard:** the developer must confirm fds 0/1/2 are `CLOEXEC=False` at exec time (e.g.
  via `os.get_inheritable(1)` returning True). Do NOT "confirm no CLOEXEC on the dup
  buffer" — that buffer is CLOEXEC=True by design and discarded by exec.
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
5. Relay terminal frames to pending waiters (A6, below), then
   `cleanup_control_files(db_path)` — remove `.sock` and `.pid` (the new process rewrites
   `.pid` with the *same* PID and re-binds the socket; see D6).
6. `_reexec()`. (If `_reexec` returns without exec'ing — A4 generation cap or A5 bad
   argv0 — fall through to the F2 recovery path: re-open the backend and resume. See
   W2 for the lock-release ordering.)

**Mid-drain new request arrival:** `shutdown_requested.set()` (step 2) makes the drain
loop's `while not shutdown_requested.is_set()` exit after the active drain; new enqueues
sit in `_pending` and are simply dropped by the exec — acceptable, because the new
process is the *same* server the client talks to, and a `wait=false` enqueue is
fire-and-forget.

**Waiter relay under the queue lock (A6):** a `wait=true` client would otherwise lose its
stream. After step 2 (`shutdown_requested.set()`), the watcher MUST drain `_pending`
waiters safely by acquiring `writer_queue._lock` (the same lock `enqueue`/`pop_next` use)
and, for each `req` in `writer_queue._pending`, `await ch.send({"ok": false, "done": true,
"error": "server self-healing — re-issue"})` for every `ch` in `req._waiters` (mirroring
the coalesce-notify pattern inside `WriterQueue.enqueue`, which sends under `self._lock`).
Direct unsynchronised access to `_pending`/`_waiters` is racy and forbidden. The active
request's waiter is handled by the drain loop's own terminal frame when it commits in
step 3, so it is not double-notified here.

**W3 — throttle vs drain are independent.** `maybe_self_heal()` runs at MCP tool entry on
the event loop (the throttle is on the *disk check*, not on the drain), never inside
`drain_loop` (which runs off-thread via `to_thread.run_sync`). The re-exec does not race
the drain: `shutdown_requested.set()` (step 2) + `async with backend_lock` (step 3)
guarantee any active drain has committed before `shutdown_backend`/exec. The 30 s throttle
and the drain ordering do not interact.

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

### D7 — Fallback scope

**Plan-reviewer resolution (BQ3): PR 2 ships in 1.20.0** after the A1–A6/W1–W2 amendments
are applied — none requires a design change. PR 1 remains the fallback if PR 2 is later
rejected: correct the `install.py` message + `mcp restart` docstring, document the
reinstall flow, and ship the skew detector (which `mcp status` consumes). The detector
ships regardless — low-risk and useful standalone.

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

**Step 1.4** — Use the on-disk read in `mcp status` (W1 — corrected).
- Files: [`src/sqlcg/cli/commands/mcp.py`](src/sqlcg/cli/commands/mcp.py) ~133–140.
- **W1 clarification:** `mcp status` compares the *running server's* reported version
  (read over the socket) against the *installed package* version. These are two different
  comparisons from `detect_skew()` (which compares the on-disk version against the calling
  process's baked-in `__version__`). Do NOT route the CLI status through `detect_skew()`.
  Instead, the CLI should call `read_ondisk_version()` directly and compare it to
  `running_version` (from the socket): `stale = running_version is not None and
  running_version != read_ondisk_version()`. This makes `stale_by_version` correct even
  when the CLI process itself is the freshly reinstalled build. `read_ondisk_version()` is
  the single source of truth for "what is installed on disk now".
- Acceptance: `TestMcpStatusVersionDrift` in `test_mcp_control.py` still passes; assert
  `stale_by_version` is `true` when the socket reports an older version than on disk and
  `false` when they match (simulate via monkeypatched `read_ondisk_version`).

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
- In `main()`, set the module-level `_resolved_argv0 = os.path.abspath(sys.argv[0])`
  **before** `anyio.run` (A5 — captured once at startup, never re-read at exec time).
- `_reexec()` per D4: A4 generation guard (`SQLCG_SELF_HEAL_GENERATION >= 3` → log ERROR,
  return without exec), A5 argv0 executability check, then bump the generation env var and
  `os.execv(_resolved_argv0, [_resolved_argv0, *sys.argv[1:]])`.
- Add `_self_heal_event`, register it with `selfheal`, spawn `_self_heal_watcher` in
  `_run_with_control`'s task group (next to `_stop_watcher`). Watcher follows D5 ordering.
- **Waiter relay (A6):** after `shutdown_requested.set()` the watcher acquires
  `writer_queue._lock` and sends the terminal "self-healing — re-issue" frame to every
  `ch` in each pending `req._waiters`. Never touch `_pending`/`_waiters` without the lock.
- **Failure handling (F2 + W2):** wrap the re-exec in try/except, AND handle the case where
  `_reexec()` returns without exec'ing (A4 cap / A5 bad argv0). In both cases the server
  must keep serving the old build. **W2 — lock-release ordering:** the F2 recovery
  (`init_backend(db_path)` + clear `shutdown_requested` + resume drain) MUST run AFTER the
  `async with backend_lock:` block has exited — the drain loop re-acquires `backend_lock`,
  so re-opening the backend while still holding it would deadlock. Structure: do
  `shutdown_backend()` and the waiter relay inside the lock; exit the lock; call `_reexec()`;
  if it returns / raises, re-open the backend and clear `shutdown_requested` OUTSIDE the
  lock. Log ERROR (`self-heal re-exec FAILED, continuing on old build vX: <err>`).
- Acceptance: T7 (watcher ordering: drain finishes before backend close), T-F2/F8
  (re-exec refusal keeps serving without deadlock), T-E2E.

**Step 2.3** — Tool-entry hook (A2).
- Files: [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py) `main()`.
- Per D3: after `import sqlcg.server.tools`, wrap
  `mcp._mcp_server.request_handlers[types.CallToolRequest]` with an async shim that calls
  `maybe_self_heal()` then delegates to the saved original. Do NOT wrap
  `mcp._mcp_server.call_tool` (a decorator factory) and do NOT use the `_get_backend()`
  fallback.
- Grep-confirm `request_handlers[types.CallToolRequest]` is the sole registered dispatch
  path and the wrap installs once; show grep in the PR.
- Acceptance: T-E2E (a tool call after a swap triggers re-exec).

---

## Failure Modes

| # | Mode | Handling |
|---|------|----------|
| F1 | On-disk version unreadable (`distributions()` returns None) | `detect_skew` returns `(False, …)`; never re-exec on an unknown version. |
| F2 | `os.execv` raises (or `_reexec` returns without exec'ing) | Catch `OSError`; log ERROR loudly; **after exiting `backend_lock` (W2)** re-open backend, clear `shutdown_requested`, keep serving old build. Server never dies on a failed re-exec; never deadlocks on the lock. |
| F3 | New request arrives mid-drain | `shutdown_requested` stops new drains; pending `wait=true` waiters get a terminal "self-healing — re-issue" frame; `wait=false` enqueues are dropped (fire-and-forget, re-issuable). |
| F4 | DuckDB exclusive lock held across exec (issue #63) | Step D5.4 closes the connection under `backend_lock` BEFORE exec; the new image opens the DB cleanly. Covered by T-E2E asserting the re-exec'd server answers a query. |
| F5 | Stale socket window during exec | `cleanup_control_files` unlinks `.sock`; new process re-binds. `mcp status` falls through to PID probe (R3) and finds the same live PID. |
| F6 | WSL2 specifics | No WSL2-specific syscall is used; `os.execv` + `AF_UNIX` behave identically to native Linux. The only WSL2 note: file-watch/inode timing on the `.dist-info` swap — the 30 s throttle + lazy check absorb any filesystem latency. No special handling needed; documented so the reviewer doesn't expect a WSL2 branch. |
| F7 | Windows | Self-heal path is `sys.platform != "win32"`-gated (matches existing control-socket gating). On Windows the detector still powers `mcp status`; the message/doc fallback applies. |
| F8 | Re-exec loop (corrupt/partial install: new build still reports a skew → re-exec storm) | `SQLCG_SELF_HEAL_GENERATION` env counter incremented before each exec; `_reexec` refuses at `>= 3`, logs ERROR with a manual-restart instruction, and falls through to F2 (keep serving). Covered by T-F8. |

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
- **T-F8** `_reexec()` with `SQLCG_SELF_HEAL_GENERATION=3` in env logs an ERROR and does
  NOT call `os.execv` (monkeypatch `os.execv` to a recording stub; assert it was never
  called and the generation guard message is logged).
- **T-F2-argv0** `_reexec()` with `_resolved_argv0` pointing at a non-executable path logs
  ERROR and does not call `os.execv` (A5 guard).

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
     `version == B` (re-exec'd in place).
  5. **PID preservation (W4):** read the pidfile before the swap and after the re-exec;
     assert they contain the **same** PID (proves in-place `os.execv`, not a respawn).
  6. Assert the DB is openable by the re-exec'd process (proves F4 — lock released).
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
- [ ] The re-exec'd process has the **same PID** as the pre-swap server (T-E2E, W4).
- [ ] When `os.execv` fails or `_reexec` refuses, the server logs an ERROR and continues
      answering tool calls on the old build with no deadlock (T-F2 / observable log + a
      follow-up tool call succeeds).
- [ ] `_reexec` refuses to loop more than 3 generations (`SQLCG_SELF_HEAL_GENERATION`),
      logging a manual-restart instruction (T-F8).
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
| `importlib.metadata` caching returns stale on-disk version → never re-execs | RESOLVED (A1): `im.MetadataPathFinder.invalidate_caches()` before the `distributions()` scan clears the mtime-keyed `FastPath` cache, so a same-mtime `.dist-info` replacement is still observed. |
| `os.execv` on a relocated venv re-resolves to old code | Re-exec the **absolute** console-script path captured at startup (`_resolved_argv0`, A5), not `sys.executable -m`; the shim re-resolves the active venv. |
| Re-exec mid-drain corrupts the graph | Drain commits under `backend_lock` before `shutdown_backend`; DuckDB transaction is atomic — a not-yet-started drain is simply dropped. |
| CLOEXEC on a server fd breaks the client pipe after exec | RESOLVED (A3): fds 0/1/2 are CLOEXEC=False (verified) and survive exec; the `os.dup(1)` buffer is CLOEXEC=True by design and discarded by exec, then re-created at module scope. Developer asserts `os.get_inheritable(1) is True`. |
| Re-exec storm on a corrupt install | RESOLVED (A4): `SQLCG_SELF_HEAL_GENERATION` env counter; `_reexec` refuses at `>= 3` (F8). |
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

## Blocking Questions — RESOLVED by plan-reviewer (2026-06-11)

1. **(BQ1 / D1)** RESOLVED. `im.distributions()` re-reads disk via the mtime-keyed
   `FastPath.lookup` cache, so an in-place `.dist-info` rewrite IS observed; the
   defensive `im.MetadataPathFinder.invalidate_caches()` call (A1) handles same-mtime
   replacements. Mechanism pinned in D1.
2. **(BQ2 / D3)** RESOLVED. The single dispatch point is
   `mcp._mcp_server.request_handlers[types.CallToolRequest]` (NOT `call_tool`, a decorator
   factory). Wrap-the-handler pattern pinned in D3 / Step 2.3 (A2).
3. **(BQ3 / D7)** RESOLVED. PR 2 (re-exec) ships in 1.20.0 after the A1–A6/W1–W2
   amendments (all incorporated above); none requires a design change. PR 1 remains the
   fallback if PR 2 is rejected.

## Review Trail

- **2026-06-11 — plan-reviewer: APPROVE-WITH-AMENDMENTS.** Amendments A1 (invalidate_caches
  + corrected mechanism), A2 (request_handlers interception), A3 (CLOEXEC correction),
  A4 (re-exec generation guard / F8), A5 (absolute argv0 captured at startup), A6 (waiter
  relay under `writer_queue._lock`), and warnings W1 (mcp status uses `read_ondisk_version`
  directly), W2 (F2 backend re-open outside `backend_lock`), W4 (PID-preservation e2e
  assertion) all incorporated. Notes N1–N3 required no change. Plan is ready for developer.
