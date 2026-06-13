# MCP Server Self-Heal — Live Verification (issue #76 / v1.20.0)

**Date:** 2026-06-13
**Verifier:** automated live test in an isolated environment (no shared tool install,
no DWH graph touched).
**Status of item before this run:** "live-verify pending" (per MEMORY: lineage-identity
sprint state — MCP self-heal #76 shipped v1.20.0, user live-verify pending).

## VERDICT: self-heal WORKS LIVE.

A running MCP server, after its installed build changed on disk underneath it,
re-exec'd itself **in place (same OS PID) on the next tool call** and served the
new build with **no manual restart**.

---

## What triggers the re-exec (from the code)

The trigger is a **version-skew between the on-disk distribution and the running
process's baked-in `__version__`**:

- [`detect_skew()`](../../src/sqlcg/server/selfheal.py) compares
  `read_ondisk_version()` (the `sql-code-graph` dist version read **fresh** from disk —
  `im.MetadataPathFinder.invalidate_caches()` then `im.distributions()`) against the
  in-process constant `sqlcg.__version__`. Skew = the two strings differ
  (a readable on-disk `None` is treated as "no skew", F1).
- [`maybe_self_heal()`](../../src/sqlcg/server/selfheal.py) is the throttled trigger.
  It is called at **MCP tool entry** — `_self_heal_then_dispatch` wraps the single
  `CallToolRequest` handler in [`server.py:763`](../../src/sqlcg/server/server.py) —
  and at control-socket `status`. It scans disk at most once per
  `SQLCG_SKEW_CHECK_INTERVAL_S` (default 30 s; overridable for tests). On skew it logs
  a WARNING and sets the `_self_heal_event` anyio.Event.
- [`_self_heal_watcher()`](../../src/sqlcg/server/server.py) awaits that event, sets
  `shutdown_requested`, acquires `backend_lock` (drains in-flight work), closes the
  DuckDB backend, removes control files, then calls
  [`_reexec()`](../../src/sqlcg/server/server.py).
- `_reexec()` calls **`os.execv(_resolved_argv0, [...])`** — replacing the process image
  in place, so the **PID is preserved**. Guards: a generation cap
  (`SQLCG_SELF_HEAL_GENERATION >= 3` refuses, to stop re-exec storms — and the counter
  is bumped to `gen+1` immediately before `execv`), and an argv0-executability check
  (must be launched via the real `sqlcg` console-script, not `python -m`).

So a valid A→B trigger requires the **installed dist version on disk** to change to a
different string than the version the process imported at startup.

## Isolation (confirmed)

- **Own venv:** `/tmp/selfheal-test/venv` (`uv venv`, Python 3.12). Builds installed via
  `uv pip install` into that venv only. The shared `~/.local/bin/sqlcg` was never used
  or modified.
- **Own graph:** `/tmp/selfheal-test/db/graph.db`, built by indexing a throwaway
  2-table fixture (`/tmp/selfheal-test/sqlfix/model.sql`: `raw_orders` →
  `order_summary` CTAS). `SQLCG_DB_PATH` pointed at this path the whole time.
  `~/.sqlcg/graph.db` and `/home/ignwrad/Projects/dwh` were never touched.

## The exact A→B change

- **Build A** = repo HEAD, version **1.25.6** (`uv build` → wheel → installed into the
  isolated venv).
- **Build B** = HEAD source snapshot with `pyproject.toml` `version` and
  `src/sqlcg/__init__.py` `__version__` both bumped to the sentinel **1.25.7**, then
  `uv build` → wheel. While the server (build A) was running and held open, the wheel
  was `uv pip install --reinstall`'d into the **same** isolated venv, so the on-disk
  `sql-code-graph` dist became 1.25.7 — exactly the string `detect_skew()` keys on.
- `SQLCG_SKEW_CHECK_INTERVAL_S=0.0` was set so the skew check fired on the very next
  tool call instead of waiting up to 30 s. (This only changes the throttle window, not
  the trigger condition.)

## Evidence

Server started via the installed console-script: `sqlcg mcp start` (stdio JSON-RPC,
newline-delimited framing). Single long-lived process; stdin held open by the driver.

| | Before swap | After swap |
|---|---|---|
| **OS PID** | 1638150 | **1638150** (unchanged — `os.execv`) |
| **serverInfo.version** (initialize handshake) | `1.25.6` | **`1.25.7`** |
| **trace_column_lineage** result | correct (`order_summary.total_amount` ← `raw_orders.amount`) | correct (same) |

Captured JSON-RPC / log evidence (confirmatory run, 2026-06-13 11:14):

```
[driver] server launched OS-PID=1638150
[driver] HANDSHAKE-A serverInfo={"name": "SQL Code Graph", "version": "1.25.6"}
BUILD_A_PID=1638150
CALL1_RESULT={... "column": "order_summary.total_amount",
              "lineage": [{"name": "amount", "table": "raw_orders", ...}] ...}
[driver] build B installed on disk; issuing CALL2 to SAME running server
[driver] proc.poll() before CALL2 = None (None=alive)
[driver] CALL2 exception (expected if execv dropped the frame): TimeoutError('recv timeout')
[driver] proc.poll() after CALL2 = None OS-PID=1638150
AFTER_PID=1638150
[driver] HANDSHAKE-B serverInfo={"name": "SQL Code Graph", "version": "1.25.7"}
CALL3_RESULT={... "column": "order_summary.total_amount",
              "lineage": [{"name": "amount", "table": "raw_orders", ...}] ...}
```

Server stderr at the moment of the second tool call:

```
[sqlcg.server.selfheal] WARNING: version skew detected: running=1.25.6 ondisk=1.25.7 — scheduling re-exec
[sqlcg.server.server]   WARNING: self-heal watcher triggered — beginning drain-safe quiescence
```

**Note on CALL2:** the second tool call (id=3) timed out rather than returning. This is
the *expected* behaviour of an in-place `execv`: the request frame is consumed by the
old image, which then replaces itself before producing a response, so the in-flight
call is lost. The watcher relays a terminal frame to any `wait=true` control-socket
clients (A6), but a stdio tool call already past the dispatcher is simply dropped — the
client must re-issue (CALL3 did, and was answered by build B). This is a benign,
documented consequence of `execv`-based self-heal, not a failure.

### Independent corroboration (earlier run, PID 1636187)

In a first run the same PID 1636187 stayed alive after the swap; two independent
process-level checks confirmed the re-exec without relying on the JSON-RPC stream:

- `/proc/1636187/environ` contained **`SQLCG_SELF_HEAL_GENERATION=1`** — this var is set
  by `_reexec()` only on the line immediately before `os.execv`. A process the driver
  launched fresh starts at generation 0; reading `1` proves `_reexec()` ran and exec'd.
- `sqlcg mcp status` over the control socket reported `"pid": 1636187`,
  `"version": "1.25.7"`, `"ondisk_version": "1.25.7"`, `"stale_by_version": false` —
  same PID, now serving build B.

## Conclusion

The v1.20.0 `_reexec()` self-heal mechanism functions live: on a real on-disk build
change, a running MCP server detects the version skew at the next tool-call entry,
drains, closes the backend, and `execv`'s itself in place (same PID) onto the new build,
which then answers tool calls correctly — no manual restart. The only client-visible
cost is that the single tool call that triggers the heal is dropped and must be
re-issued.
