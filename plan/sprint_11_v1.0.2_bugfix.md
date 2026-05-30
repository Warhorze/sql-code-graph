# Sprint Plan: v1.0.2 Bugfix Sprint

Plan date: 2026-05-30
Author: sprint-planner
Source authority: GitHub issues #12, #13, #22, #23, #24, #25, #26, #27 (partial); [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md); CLAUDE.md invariant table; source tree cross-check performed 2026-05-30.
Policy: no backward compatibility (re-index is the migration path); no TODO in happy paths; every new method has a grep-confirmed call site before PR opens; tests must assert observable output, not "no exception raised"; path/constant fallbacks must match `KuzuConfig`.

---

## Summary

This sprint fixes the eight open bugs that block reliable in-editor usage of sqlcg v1.0.x. The two highest-severity bugs (#22 stdio transport broken, #23 install writes wrong config file) together prevent any stdio MCP client from connecting at all. The remaining bugs address correctness gaps (INSERT positional mapping, case-sensitive find, unqualified INSERT target nodes), a crash on relative-path reindex, and observability/UX issues. Six of the eight issues are confirmed present in the source tree; two (#25 INSERT positional, #26 unqualified schema) require behavioural corrections to the lineage engine. Issues #5 and #15 are already closed and are not included.

---

## Scope

### In Scope
- PR-01: Fix stdio MCP transport — frames to fd 1, logs to fd 2 (#22)
- PR-02: Fix `install` / `mcp setup` to write to `~/.claude.json` via `claude mcp add` (#23)
- PR-03: Fix `reindex .` crash on relative path in ignore matcher (#24)
- PR-04: Fix `find table` case-sensitivity (#12)
- PR-05: Fix INSERT…SELECT positional column mapping (#25)
- PR-06: Fix unqualified INSERT target creating orphan nodes (#26)
- PR-07: Fix index parse warnings noise (#13) — partial scope defined below
- PR-08: #27 bundle — in-scope sub-items only (27a noise glob, 27b CLI NoiseFilter)

### Non-Goals (deferred to v1.1+)
- #27c (CTE/subquery pseudo-node tagging): medium scope, requires schema change → deferred
- #27d (per-file timeout failure bucket) — **partially folded into PR-07**: the message-format mismatch fix makes pool-path timeouts visible to `analyze failures --cause timeout` without a new graph node kind or schema migration. What remains deferred: a separate graph node type for timeouts with structured metadata (separate from `File.parse_failed`).
- #27e (duplicate rows in `analyze upstream`): upstream KuzuDB path deduplication — low urgency → deferred
- Full default_schema config for unqualified DDL/DML (#26 option 1): design impact is sprint-sized; PR-06 ships the safer post-index unification fallback at query time

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|----------------|
| #22 — stdio frames on stderr | Confirmed. `server.py:29` calls `_configure_mcp_logging()` at module scope, which sets `sys.stdout = sys.stderr`. `FastMCP("SQL Code Graph")` is constructed at line 32, also at module scope, AFTER the redirect. `mcp.server.stdio.stdio_server()` captures `sys.stdout.buffer` at call time (inside `mcp.run()`), so it gets `sys.stderr.buffer`. JSON-RPC frames go to fd 2. The fix: capture `os.dup(1)` as `_real_stdout_buffer` at module top BEFORE `_configure_mcp_logging()` is called AND before `FastMCP(...)` is constructed; pass it to `stdio_server(stdout=...)`. The `mcp` SDK is already pinned at `>=1.27.0,<2.0` in `pyproject.toml`. |
| #23 — install writes settings.json | Confirmed. `install.py:14` hardcodes `_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"`. `claude mcp list` on this machine returns no sqlcg server despite `settings.json` containing the entry. `claude mcp add -s user` is the correct write path. `ARCHITECTURE_REVIEW.md § 9.2` documents the now-wrong path and must be updated by PR-02. |
| #24 — reindex relative path crash | Confirmed. `git_delta.py:78` calls `load_ignore_spec(root)` with the un-resolved `root` from the typer `Path` argument. `is_ignored` at `ignore.py:36` calls `path.relative_to(root)` where `path` is an absolute path produced by `(root / rel_path).resolve()` and `root` is still `.`. `ValueError` is raised. `reindex.py` never calls `path.resolve()`. `.sqlcgignore` is read from `root.resolve() / ".sqlcgignore"` once `root` is resolved. |
| #12 — find table case-sensitive | Confirmed. `find.py:21` passes `{"name": name}` with no `.lower()`. Graph keys are lowercase (C2). MCP `find_table_usages` at `tools.py:593` does `.lower()`; the CLI command does not. |
| #25 — INSERT positional mapping | Confirmed. `base.py:922–961` INSERT aliasing block iterates over `col_expressions` and skips columns where `col_expr.alias` is truthy (line 939: `if col_expr.alias: continue`). The aliased-expression case is fully unhandled for positional override. The `body_no_with = body.copy()` + strip-WITH happens ONCE before the loop at line 934 (CLAUDE.md invariant intact); only the single projection is swapped per column. The positional block must run BEFORE the main column loop so the skip-set is populated before the main loop consumes it. |
| #26 — unqualified INSERT orphan node | Confirmed. `TableRef.__post_init__` lowercases `name` and sets `db=None` when no schema is present. `full_id` for `INSERT INTO FACT_T` produces `"fact_t"`. DDL-defined node is `"mart.fact_t"`. The fallback derivation for 3-part refs (e.g. `"mart.fact_t.amount"`) must use `".".join(ref.split(".")[1:])` → `"fact_t.amount"`, not `ref.rsplit(".", 1)[-1]` → `"amount"` (which would catastrophically match every column named `amount`). For 2-part refs (`"fact_t.amount"`) no schema to strip; ref is already the bare form. |
| #13 / #27d — timeout observability | Confirmed. `pool.py:492` emits `f"timeout file={Path(path).name}"` (no colon, no time value) and `f"skipped:poison file={Path(path).name}"`. `error_classify.py:97` only matches `msg.startswith("timeout:")` — pool-path timeouts classify as `"other"` → `dominant_cause` returns `("", False)` → `parse_failed=False` → invisible to `analyze failures --cause timeout`. Meanwhile `_index_single_file` (line 842) emits `f"timeout:{timeout}s file=..."` which IS caught. The fix is NOT a second upsert — `_upsert_parsed_file` already calls `dominant_cause(parsed.errors)` at line 899 and writes `parse_failed`/`parse_cause`. The fix is: make `_timeout_file` emit `f"timeout:{timeout}s file=..."` matching `_index_single_file`, AND extend `_classify_error` to match `"skipped:poison"`. |
| #27a — `*_bck_*` glob missing | Confirmed. `config.py:139–145` default patterns are `["*_bck", "*_bck_us", "*_bck_[0-9]*", "*_backup", "*_backup_[0-9]*"]`. Pattern `*_bck_*` is missing, so `foo_bck_us39553` is not matched by any default. The `ignore_table_regexes` facility exists but defaults to empty. |
| #27b — CLI analyze/find does not apply NoiseFilter | Confirmed. `analyze.py` and `find.py` issue raw Cypher with no noise filtering. `NoiseFilter.from_config()` is only called in `server/tools.py`. `NoiseFilter.from_config()` takes an optional `repo_root: Path` and falls back to `Path.cwd()` when `None` — this is the resolution for `analyze.py` which has no repo-path context. |

---

## Ticket Table

| ID | Title | Files | Effort | Priority | Depends on | Blocks |
|----|-------|-------|--------|----------|------------|--------|
| PR-01 | Fix stdio MCP transport (fd 1 vs fd 2) | `server/server.py`, `tests/unit/test_server.py`, `tests/unit/test_mcp_stdio_smoke.py` (new) | S | P0 | INDEPENDENT | PR-02 (reveals) |
| PR-02 | Fix install to write via `claude mcp add` | `cli/commands/install.py`, `cli/commands/mcp.py`, `tests/unit/test_install.py`, `ARCHITECTURE_REVIEW.md` | S | P0 | INDEPENDENT | NONE |
| PR-03 | Fix reindex relative path crash | `cli/commands/reindex.py`, `indexer/git_delta.py`, `utils/ignore.py`, `tests/unit/test_git_delta.py` | XS | P1 | INDEPENDENT | NONE |
| PR-04 | Fix `find table` case-sensitivity | `cli/commands/find.py`, `tests/unit/test_cli.py` | XS | P1 | INDEPENDENT | NONE |
| PR-05 | Fix INSERT…SELECT positional column mapping | `parsers/base.py`, `tests/unit/test_base_parser.py`, `tests/unit/test_perf_scaling_guard.py` (verify) | M | P1 | INDEPENDENT | NONE |
| PR-06 | Fix unqualified INSERT orphan node (query-time fallback) | `cli/commands/analyze.py`, `server/tools.py`, `tests/unit/test_firstuser_findings.py` | S | P2 | INDEPENDENT | NONE |
| PR-07 | Fix timeout observability (pool path) + route parse warnings to log file | `indexer/pool.py`, `indexer/error_classify.py`, `core/config.py`, `cli/commands/index.py`, `tests/unit/test_error_classify.py` | S | P2 | INDEPENDENT | NONE |
| PR-08 | #27a noise glob + #27b CLI NoiseFilter | `core/config.py`, `cli/commands/analyze.py`, `cli/commands/find.py`, `tests/unit/test_noise_filter.py`, `tests/unit/test_firstuser_findings.py` | S | P2 | INDEPENDENT | NONE |

---

## Recommended Implementation Order

### Why this order

**PR-03 first (XS, zero risk):** Fixes a crash on a typer argument. One-line fix in `reindex.py`. Unblocks testing of the reindex path. Ships alone to keep the crash fix reviewable atomically.

**PR-04 second (XS, zero risk):** One `.lower()` call in `find.py`. Cannot conflict with any other PR. Ships alone.

**PR-01 third (S, P0):** The most critical bug for usability, but it is safe to ship after the two zero-risk XS fixes are merged so the PR surface is clean. The fix touches only `server/server.py` and adds a new smoke test. No dependency on other PRs; order here is only risk management (ship low-risk first so the diff surface is clean if PR-01 introduces a problem).

**PR-02 fourth (S, P0):** `install.py` rewrite. Depends on nothing. Should ship before PR-06 and PR-08 because install is the user's entry path; fixing it unblocks any user trying to set up the server fresh.

**PR-05 fifth (M, P1):** Touches the parser hot path (`base.py`). The highest-risk change in the sprint because it modifies the INSERT aliasing block protected by perf-guard tests. Ships after all XS/S low-risk tickets are merged so the review can focus entirely on the lineage correctness + perf invariant check.

**PR-08 sixth (S, P2):** Adds `*_bck_*` default glob and wires `NoiseFilter` into CLI commands. Touches `config.py` (same module as PR-07) but different functions — can be reviewed independently. Ships before PR-07 to reduce the `config.py` delta surface in PR-07.

**PR-06 seventh (S, P2):** Adds query-time bare-name fallback to `analyze` commands and the MCP tools. Touches `analyze.py` and `tools.py`. No dependency on other PRs.

**PR-07 last (S, P2):** Log routing to `~/.sqlcg/index.log`. Touches `index.py` and `config.py`. Ships last because it is lowest urgency and touches `config.py` which PR-08 also touches; sequential order avoids merge conflicts.

### Single-developer sequence

1. PR-03 — reindex path crash (XS)
2. PR-04 — find table case (XS)
3. PR-01 — stdio transport (S, P0)
4. PR-02 — install config path (S, P0)
5. PR-05 — INSERT positional mapping (M, P1)
6. PR-08 — noise glob + CLI NoiseFilter (S, P2)
7. PR-06 — unqualified INSERT fallback (S, P2)
8. PR-07 — index log routing (S, P2)

### Two-developer sequence

**Prerequisites for split** (must be merged first, one dev): PR-03, PR-04 (both XS).

After prerequisites merge:
- Track A: PR-01 → PR-05 → PR-06 (protocol correctness + lineage correctness)
- Track B: PR-02 → PR-08 → PR-07 (install + observability + UX)

Track A and Track B have no shared files after prerequisites merge, except: PR-05 modifies `base.py` which PR-06 does not touch; PR-06 modifies `analyze.py`/`tools.py` which PR-05 does not touch. Clean split.

---

## Ticket Specifications

---

### PR-03 — Fix `reindex .` crash on relative path in ignore matcher

**Source**: GitHub issue #24 (MEDIUM)
**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `reindex.py` accepts a typer `Path` argument named `path` and passes it directly to `indexer.resync_changed(path, ...)`. Inside `resync_changed`, `git_delta.git_name_status_delta(root, ...)` is called with the un-resolved `root`. At `git_delta.py:78`, `load_ignore_spec(root)` creates the spec. At `git_delta.py:94–103`, git diff output produces relative paths that are `.resolve()`d to absolute, then passed to `is_ignored(abs_path, root, spec)`. At `ignore.py:36`, `path.relative_to(root)` is called with `root=Path(".")` and `path` being an absolute path — Python raises `ValueError` because an absolute path cannot be made relative to a non-matching relative path.

**What to do**:

1. In `reindex.py`, resolve `path` to absolute at the top of `reindex_cmd`, before any other use:
   ```python
   # Before:
   path: Path = typer.Argument(...)
   # First use of path is passed to get_dialect(path), get_head(path), etc.

   # After (first line of reindex_cmd body):
   path = path.resolve()
   ```

2. As a defensive belt-and-suspenders fix, also resolve `root` at the top of `git_name_status_delta`:
   ```python
   # After:
   def git_name_status_delta(root: Path, old_sha: str, new_sha: str) -> Delta | None:
       root = root.resolve()   # guard against caller passing a relative path
       ...
   ```

3. Do the same in `load_ignore_spec` defensively:
   ```python
   def load_ignore_spec(root: Path) -> pathspec.PathSpec:
       root = Path(root).resolve()
       ...
   ```
   This ensures `is_ignored` never receives a relative `root` regardless of call site.

**Wiring verification**:

- `grep -n "resolve" src/sqlcg/cli/commands/reindex.py` must show `path = path.resolve()` in `reindex_cmd` body.
- `grep -n "resolve" src/sqlcg/indexer/git_delta.py` must show `root = root.resolve()` at top of `git_name_status_delta`.
- `grep -n "resolve" src/sqlcg/utils/ignore.py` must show `root = Path(root).resolve()` at top of `load_ignore_spec`.
- `grep -n "relative_to" src/sqlcg/utils/ignore.py` must still show the `path.relative_to(root)` call at line 36 — we are not removing it, we are ensuring `root` is always absolute so the call cannot fail.

**Files affected**:
- [`src/sqlcg/cli/commands/reindex.py`](../src/sqlcg/cli/commands/reindex.py) — resolve `path` at top of `reindex_cmd`
- [`src/sqlcg/indexer/git_delta.py`](../src/sqlcg/indexer/git_delta.py) — resolve `root` at top of `git_name_status_delta`
- [`src/sqlcg/utils/ignore.py`](../src/sqlcg/utils/ignore.py) — resolve `root` at top of `load_ignore_spec`

**Tests to add**:

- **Scenario A — relative root does not crash `is_ignored`**: call `is_ignored(Path("/absolute/path/foo.sql"), Path("."), spec)` where spec is a no-match spec; assert result is `False` without `ValueError`.
- **Scenario B — relative root does not crash `load_ignore_spec`**: call `load_ignore_spec(Path("."))` in a tmp dir with no `.sqlcgignore`; assert a `PathSpec` object is returned.
- **Scenario C — `git_name_status_delta` called with `.`**: in a test that stubs `subprocess.run` to return a synthetic diff, pass `root=Path(".")`, assert a `Delta` is returned (not `None`, no `ValueError`).

Place these in `tests/unit/test_git_delta.py` alongside existing tests.

**Acceptance criteria**:
- `[ ]` `sqlcg reindex .` from the repo root does not raise `ValueError` (git delta parsing completes without exception)
- `[ ]` `grep -n "resolve" src/sqlcg/utils/ignore.py` shows `root = Path(root).resolve()` in `load_ignore_spec`
- `[ ]` `.sqlcgignore` is read from `root.resolve() / ".sqlcgignore"` — confirmed by `grep -n "ignore_file = root" src/sqlcg/utils/ignore.py` showing `root / ".sqlcgignore"` after `root` has been resolved at the top of `load_ignore_spec`
- `[ ]` No TODO in happy path of `is_ignored` or `load_ignore_spec`

---

### PR-04 — Fix `find table` case-sensitivity

**Source**: GitHub issue #12 (bug)
**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `find.py:21` issues `WHERE t.qualified CONTAINS $name` with `name` set to the raw user input string. Graph `Table.qualified` values are lowercased at index time (C2 normalization in `TableRef.__post_init__`). When the user types `WTFS_VOORRAAD_WEEK` or any mixed-case variant, the `CONTAINS` predicate fails to match the stored lowercase key. The MCP tool `find_table_usages` already applies `.lower()` at `tools.py:593` — the CLI command is missing the same normalization.

**What to do**:

In `find.py`, normalize the `name` argument to lowercase before the query:

```python
# find_table (line 19):
def find_table(name: str = ...) -> None:
    name = name.lower()   # add this line
    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE}) WHERE t.qualified CONTAINS $name ...",
            {"name": name},
        )
```

Apply the same normalization to `find_column` (`ref` argument) and `find_pattern` (no change — pattern is user-arbitrary substring, keep as-is). For `find_column`, `ref` is a table.column reference — since both parts are lowercased at index time, apply `.lower()`:

```python
def find_column(ref: str = ...) -> None:
    ref = ref.lower()   # add this line
```

**Wiring verification**:

- `grep -n "lower()" src/sqlcg/cli/commands/find.py` must show `.lower()` in both `find_table` and `find_column` before the `backend.run_read` call.
- `grep -n "lower()" src/sqlcg/server/tools.py | grep find_table` must still show `table_name.lower()` at `tools.py:593` — confirm both paths normalize consistently.

**Files affected**:
- [`src/sqlcg/cli/commands/find.py`](../src/sqlcg/cli/commands/find.py) — add `.lower()` to `find_table` and `find_column`

**Tests to add**:

- **Scenario A — uppercase input matches lowercase-stored table**: index a file containing `CREATE TABLE ba.MY_TABLE AS SELECT 1`, then call `find_table("MY_TABLE")` and assert `qualified == "ba.my_table"` in the result list.
- **Scenario B — mixed-case column ref matches**: index a file with `SELECT AMOUNT FROM ba.orders`, call `find_column("BA.ORDERS.AMOUNT")`, assert result is non-empty.

These are integration tests; place in `tests/integration/test_indexer_to_graph.py` or a new `tests/unit/test_find_cmd.py`.

**Acceptance criteria**:
- `[ ]` `sqlcg find table WTFS_VOORRAAD_WEEK` returns the same result as `sqlcg find table wtfs_voorraad_week` on an indexed graph
- `[ ]` `grep -n "lower()" src/sqlcg/cli/commands/find.py` shows two `.lower()` calls (one per command)
- `[ ]` No TODO in happy path

---

### PR-01 — Fix stdio MCP transport: frames to fd 1, logs to fd 2

**Source**: GitHub issue #22 (CRITICAL)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `server.py` calls `_configure_mcp_logging()` at module scope (line 29), which sets `sys.stdout = sys.stderr`. `FastMCP("SQL Code Graph")` is then constructed at module scope (line 32), also AFTER the redirect. `mcp.server.stdio.stdio_server()` captures `sys.stdout.buffer` at call time (inside `FastMCP.run_stdio_async`, which runs only when `mcp.run()` is invoked). Since `sys.stdout` has already been replaced, `stdio_server()` gets `sys.stderr.buffer` — JSON-RPC frames are written to fd 2 (stderr). The client reads from fd 1 (stdout) and sees nothing; the handshake times out.

**Invariant**: `_real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)` must be the FIRST statement at module top — before both `_configure_mcp_logging()` and `FastMCP("SQL Code Graph")`. `FastMCP(...)` remains at module scope (tools.py imports and registers against it); the capture must simply precede it.

The `mcp` SDK is already pinned at `>=1.27.0,<2.0` in `pyproject.toml` — no version pin change needed for this PR. The fix touches `mcp._mcp_server` (a private attribute); if the SDK is ever upgraded outside the `<2.0` bound, verify `_mcp_server` still exists.

**What to do**:

1. At the very top of `server.py`, before any other statement, capture fd 1:
   ```python
   import os
   import sys

   # Capture the real fd 1 binary stream FIRST — before _configure_mcp_logging()
   # (which replaces sys.stdout) AND before FastMCP("SQL Code Graph") construction
   # (which may emit output). stdio_server() receives this explicitly so JSON-RPC
   # frames go to fd 1 regardless of what sys.stdout points to afterward.
   _real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)
   ```

2. `_configure_mcp_logging()` redirects `sys.stdout` to `sys.stderr` (unchanged logic). Move its call inside `main()`, AFTER `_real_stdout_buffer` has been captured at module top. The function body is correct; only its call site moves:
   ```python
   def _configure_mcp_logging() -> None:
       """Route all Python logging and print() output to stderr.

       sys.stdout is replaced with sys.stderr so that any stray print() call
       does not pollute fd 1 (reserved for MCP JSON-RPC frames).
       The real fd 1 binary stream is captured in _real_stdout_buffer at module
       top before this replacement and passed explicitly to stdio_server().
       """
       sys.stdout = sys.stderr
   ```

3. `FastMCP("SQL Code Graph")` remains at module scope (no change) so that `tools.py` can import and register tools via `@mcp.tool()`. This is correct because `_real_stdout_buffer` is captured before it.

4. In `main()`, call `_configure_mcp_logging()` first (before any logging or print), then drive the server loop with the captured fd 1. Replace the `mcp.run()` call with a direct `_run_stdio_async_with_real_stdout` coroutine that passes `_real_stdout_buffer` to `stdio_server()`:

   ```python
   from io import TextIOWrapper

   async def _run_stdio_async_with_real_stdout() -> None:
       from mcp.server.stdio import stdio_server
       stdout_text = TextIOWrapper(_real_stdout_buffer, encoding="utf-8", line_buffering=False)
       async with stdio_server(stdout=anyio.wrap_file(stdout_text)) as (read_stream, write_stream):
           await mcp._mcp_server.run(
               read_stream,
               write_stream,
               mcp._mcp_server.create_initialization_options(),
           )

   def main(db_path: str | None = None) -> None:
       import anyio
       _configure_mcp_logging()   # must be first — before load_dotenv or any print
       load_dotenv()
       import sqlcg.server.tools
       sqlcg.server.tools.init_backend(db_path)
       try:
           anyio.run(_run_stdio_async_with_real_stdout)
       finally:
           sqlcg.server.tools.shutdown_backend()
   ```

   This bypasses `mcp.run()` (which calls `run_stdio_async` with no stdout override) and directly drives the server loop with the captured fd 1.

5. Remove the module-level `_configure_mcp_logging()` call (line 29 in the current file). The only call is now inside `main()`.

6. Configure the `logging` module to route to stderr explicitly:
   ```python
   import logging
   logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
   ```

**Wiring verification**:

- `grep -n "_real_stdout_buffer\|os.dup" src/sqlcg/server/server.py` must show the `os.dup(1)` capture line as the **first** substantive statement in the module (before any `def`, `class`, or `FastMCP` call).
- `grep -n "FastMCP\|_configure_mcp_logging\(\)" src/sqlcg/server/server.py` — `FastMCP(...)` must appear AFTER `_real_stdout_buffer` assignment; the standalone `_configure_mcp_logging()` call must NOT appear at module scope.
- `grep -n "stdio_server" src/sqlcg/server/server.py` must show explicit `stdout=` argument passed to `stdio_server()`.
- `grep -n "mcp\.run\(\)" src/sqlcg/server/server.py` must return zero results (replaced by `_run_stdio_async_with_real_stdout`).
- After the fix, the existing `test_server.py::test_configure_mcp_logging_redirects_stdout` test must be updated (it asserts `sys.stdout is sys.stderr` after the call — assertion remains valid, but call site changes to inside `main()`).

**Files affected**:
- [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py) — capture fd 1 at module top before FastMCP; move `_configure_mcp_logging()` call into `main()`; pass explicit stdout to `stdio_server()`
- [`tests/unit/test_server.py`](../tests/unit/test_server.py) — update existing test to match new call order; add Scenario B unit test
- [`tests/unit/test_mcp_stdio_smoke.py`](../tests/unit/test_mcp_stdio_smoke.py) — new smoke test (see below)

**Tests to add**:

Place the smoke test in a new file `tests/unit/test_mcp_stdio_smoke.py`.

- **Scenario A — mcp start stdio handshake over subprocess**: spawn `uv run sqlcg mcp start` as a subprocess; send a valid `initialize` JSON-RPC request to its stdin; read from its stdout; assert the response contains `"result"` and `"capabilities"`, and that `tools` is present in the initialization response. Close with `notifications/cancelled`. Assert the subprocess stdout is non-empty and stderr does not contain a JSON-RPC frame.

  ```python
  import json, subprocess, sys

  INIT_REQUEST = json.dumps({
      "jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {
          "protocolVersion": "2024-11-05",
          "capabilities": {},
          "clientInfo": {"name": "smoke-test", "version": "0"},
      }
  }) + "\n"

  def test_mcp_start_handshake_on_stdout():
      proc = subprocess.Popen(
          ["uv", "run", "sqlcg", "mcp", "start"],
          stdin=subprocess.PIPE,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
      )
      try:
          proc.stdin.write(INIT_REQUEST.encode())
          proc.stdin.flush()
          line = proc.stdout.readline()
          response = json.loads(line)
          assert "result" in response, (
              f"MCP initialize must return a result on stdout — "
              f"stdio transport regression. Got: {line!r}"
          )
          assert "capabilities" in response["result"], (
              "MCP initialize result must contain capabilities. "
              f"Got: {response['result']}"
          )
      finally:
          proc.kill()
          proc.wait()
  ```

- **Scenario B — `_configure_mcp_logging` does not destroy fd 1**: call `_configure_mcp_logging()`, then assert `os.write(1, b"")` succeeds (fd 1 is still open and writable). This is a unit test in `test_server.py`. Note: `_configure_mcp_logging()` now moves its call site to `main()`, but the function itself is still importable for testing.

**Acceptance criteria**:
- `[ ]` `sqlcg mcp start` sends JSON-RPC frames on stdout (fd 1) — confirmed by `test_mcp_stdio_smoke.py::test_mcp_start_handshake_on_stdout` passing
- `[ ]` `grep -n "os.dup(1)\|_real_stdout_buffer" src/sqlcg/server/server.py` shows the capture as the first substantive statement, before `FastMCP(...)` and any `_configure_mcp_logging()` call
- `[ ]` `grep -n "_configure_mcp_logging()" src/sqlcg/server/server.py` shows exactly one call, inside `main()`, not at module scope
- `[ ]` No TODO in `main()` or `_run_stdio_async_with_real_stdout`
- `[ ]` Existing `test_server.py::test_configure_mcp_logging_redirects_stdout` still passes (updated to match new call order)

---

### PR-02 — Fix `install` to write via `claude mcp add`

**Source**: GitHub issue #23 (HIGH)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `install.py:14` sets `_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"`. Claude Code reads MCP servers from the `claude mcp add` registry (backed by `~/.claude.json` `mcpServers.user` scope, not `settings.json`). Confirmed on this machine: `~/.claude/settings.json` has the entry; `claude mcp list` does not show it. The `claude mcp add -s user <name> <command> [args...]` CLI is the correct write path.

`ARCHITECTURE_REVIEW.md § 9.2` ("Settings file target") documents the now-wrong path `~/.claude/settings.json` and must be updated in this PR so the review does not go stale.

**What to do**:

1. In `install.py`, attempt `claude mcp add -s user sql-code-graph <command> <args...>` first (using `shutil.which("claude")`). On success (returncode 0), print a confirmation and return.

2. If `claude` is not on PATH or returns non-zero, fall back to writing `~/.claude.json` directly:
   ```python
   claude_json = Path.home() / ".claude.json"
   data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
   data.setdefault("mcpServers", {}).setdefault("user", {})[_SERVER_KEY] = entry
   tmp = claude_json.with_suffix(".tmp")
   tmp.write_text(json.dumps(data, indent=2) + "\n")
   os.replace(tmp, claude_json)
   ```

3. Remove all writes to `~/.claude/settings.json` from both `install.py` and `mcp.py`. The `mcp setup --write` command in `mcp.py` currently writes `settings.json`; change it to write `~/.claude.json` using the same path from step 2, or print-only (the `--print` default is already correct).

4. Update the CLI help text in `install.py` docstring and `mcp.py:mcp_setup` docstring to describe the new behaviour.

5. The `--dry-run` path must print what would be executed (the `claude mcp add` command or the JSON write), not actually write anything.

6. Update `ARCHITECTURE_REVIEW.md § 9.2` to replace the "Settings file target" paragraph — change it to document the `claude mcp add -s user` write path and `~/.claude.json` fallback as the correct mechanism.

**Wiring verification**:

- `grep -n "settings.json\|_SETTINGS_PATH" src/sqlcg/cli/commands/install.py` must return zero results after the fix.
- `grep -n "settings.json" src/sqlcg/cli/commands/mcp.py` must return zero results after the fix.
- `grep -n "claude mcp add\|subprocess.*claude\|shutil.which.*claude" src/sqlcg/cli/commands/install.py` must show the `claude mcp add` shelling out.
- `grep -n ".claude.json\|claude_json" src/sqlcg/cli/commands/install.py` must show the fallback write path.
- `grep -n "settings.json" ARCHITECTURE_REVIEW.md` must return zero results in § 9.2 after the update.

**Files affected**:
- [`src/sqlcg/cli/commands/install.py`](../src/sqlcg/cli/commands/install.py) — replace `settings.json` path with `claude mcp add` + `~/.claude.json` fallback; remove `_SETTINGS_PATH`
- [`src/sqlcg/cli/commands/mcp.py`](../src/sqlcg/cli/commands/mcp.py) — remove `settings.json` write from `mcp_setup --write`
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — update § 9.2 to document `claude mcp add` path

**Tests to add**:

Update `tests/unit/test_install.py` — the existing tests patch `_SETTINGS_PATH` and assert on `~/.claude/settings.json`; these tests must be deleted or replaced, not left alongside new tests, because `_SETTINGS_PATH` is removed by this PR.

- **Delete** all existing test scenarios that reference `_SETTINGS_PATH` or assert writes to `~/.claude/settings.json`.
- **Scenario A — `claude` available, `claude mcp add` succeeds**: mock `shutil.which("claude")` to return a path and `subprocess.run` to return `returncode=0`; invoke `install_cmd(scope="project", dry_run=False)`; assert `subprocess.run` was called with args containing `["claude", "mcp", "add", "-s", "user", "sql-code-graph", ...]`; assert `os.replace` was NOT called (no file write).
- **Scenario B — `claude` not available, falls back to `~/.claude.json`**: mock `shutil.which("claude")` to return `None`; invoke `install_cmd(scope="project", dry_run=False)`; assert `~/.claude.json` (via a mock) was written with `mcpServers.user.sql-code-graph` present; assert `~/.claude/settings.json` was never opened for writing.
- **Scenario C — `--dry-run` prints command, writes nothing**: mock both; assert no file writes and stdout contains the `claude mcp add` command string.

The assertion "no write to `settings.json`" means `Path.home() / ".claude" / "settings.json"` is not opened for writing — verify via `unittest.mock.patch("pathlib.Path.open")` or by checking calls to `os.replace`.

**Acceptance criteria**:
- `[ ]` `sqlcg install --scope project` runs `claude mcp add -s user sql-code-graph sqlcg mcp start` when `claude` is on PATH
- `[ ]` After `sqlcg install --scope project`, `claude mcp list` shows `sql-code-graph`
- `[ ]` `grep -n "settings.json" src/sqlcg/cli/commands/install.py` returns zero results
- `[ ]` `grep -n "settings.json" ARCHITECTURE_REVIEW.md` returns zero results in § 9.2 (section updated to document `claude mcp add` path)
- `[ ]` All pre-existing `test_install.py` tests that reference `_SETTINGS_PATH` or `settings.json` are deleted or replaced
- `[ ]` No TODO in the happy path of `install_cmd`

---

### PR-05 — Fix INSERT…SELECT positional column mapping

**Source**: GitHub issue #25 (MEDIUM)
**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: In `base.py`, the INSERT aliasing block at line 922 handles `INSERT INTO t (c1, c2) SELECT a AS x, b AS y ...` by iterating over `col_expressions` and skipping any item where `col_expr.alias` is truthy (line 939: `if col_expr.alias: continue`). This means when the SELECT item has an alias, the positional INSERT column mapping is entirely skipped. The column ends up attributed to the alias name (`x`, `y`) via the main column loop above (line 692), not to the INSERT target column names (`c1`, `c2`). This creates edges pointing to non-existent columns in the target table when aliases differ from column names.

The correct behaviour: when an INSERT has an explicit column list, the target column name at position `idx` is authoritative. The SELECT alias is cosmetic for the SELECT but meaningless to the INSERT target. The positional mapping must override alias attribution.

**What to do**:

1. **Move the positional INSERT skip-set computation BEFORE the main column loop** (W2 fix — definitive ordering). The INSERT column-list block must run before the main loop so that its skip-set is populated before the main loop consumes `col_expressions`. There is no hedge here: positional INSERT block runs first; main loop skips indices already handled:

   ```python
   # Compute INSERT positional skip set BEFORE the main column loop.
   # (Must run first so main loop can skip these indices.)
   positional_col_names: dict[int, str] = {}   # idx → insert_col_name
   if isinstance(stmt, exp.Insert) and isinstance(stmt.this, exp.Schema):
       insert_cols_list = [c.name for c in stmt.this.expressions]
       # body_no_with: built ONCE here, before any loop. Only the single
       # projection is swapped per column — CLAUDE.md invariant.
       body_no_with = body.copy()
       body_no_with.set("with_", None)
       for idx, col_expr in enumerate(col_expressions):
           if idx >= len(insert_cols_list):
               break
           insert_col = insert_cols_list[idx]
           if not insert_col:
               continue
           positional_col_names[idx] = insert_col
           # Positional mapping always wins — replace (or add) the alias with
           # the INSERT target column name, regardless of SELECT alias.
           aliased = exp.Alias(this=col_expr.copy(), alias=insert_col)
           body_no_with.set("expressions", [aliased])
           patched_sql = body_no_with.sql(dialect=self.DIALECT)
           if col_expr.alias and col_expr.alias != insert_col:
               self._log.debug(
                   "INSERT positional override: SELECT alias %r → INSERT col %r at position %d",
                   col_expr.alias, insert_col, idx,
               )
           try:
               root = sg_lineage(insert_col, patched_sql, dialect=self.DIALECT)
               if root:
                   new_edges = self._lineage_node_to_edges(
                       root, dst_col_name=insert_col, dst_table=dst_table,
                       path=path, out=out,
                   )
                   edges.extend(new_edges)
           except Exception:
               pass

   # Main column loop — skips positions already handled by positional INSERT block:
   for loop_idx, col_expr in enumerate(col_expressions):
       if loop_idx in positional_col_names:
           continue  # positional INSERT block already emitted this column
       ...
   ```

2. **CLAUDE.md invariant preservation**: the `body_no_with = body.copy()` + strip-WITH happens ONCE before the inner loop (inside the `if isinstance(stmt, exp.Insert) and isinstance(stmt.this, exp.Schema):` block, not per column). Only the single projection (`body_no_with.set("expressions", [aliased])`) is swapped per column. The existing INSERT block at lines 922–961 ALREADY does this correctly — the refactor preserves this pattern while moving the block before the main loop. Do NOT introduce a second `body.copy()` call inside any loop.

3. **Remove the old INSERT aliasing block** (lines 922–961) now that the logic is moved before the main loop. The existing block had `if col_expr.alias: continue` — that guard is now replaced by the positional override in step 1.

4. **Perf invariant check (mandatory)**: after the change, verify all four invariants are still intact:
   - `body_no_with = body.copy()` still happens **once** before the INSERT column loop (not inside it).
   - `copy=False` and `trim_selects=False` are still passed to `sg_lineage` when `body_scope` is not None.
   - `body_scope` is still built **once** per statement before the column loop.
   - The pure-literal skip `if not list(col_expr.find_all(exp.Column)): continue` is still present in the main loop.

**Wiring verification**:

- `grep -n "positional_col_names\|body_no_with = body.copy()" src/sqlcg/parsers/base.py` — both must appear ABOVE the main `for loop_idx, col_expr in enumerate(col_expressions):` loop.
- `grep -n "body_no_with = body.copy()" src/sqlcg/parsers/base.py` must show exactly **one** occurrence (not inside a column-level loop).
- `grep -n "copy=False" src/sqlcg/parsers/base.py` must still show the `copy=False` kwarg in the `sg_kwargs` construction.
- `grep -n "body_scope = build_scope" src/sqlcg/parsers/base.py` must show exactly **two** occurrences (primary + schema-free retry), both inside the `if scope is None:` block.
- `grep -n "if col_expr.alias:.*continue" src/sqlcg/parsers/base.py` must return zero results (the alias-skip guard is removed; positional override replaces it).
- `rtk uv run pytest tests/unit/test_perf_scaling_guard.py -x` must pass.
- `rtk uv run pytest tests/unit/test_T09_01_qualify_once.py -x` must pass.
- `rtk uv run pytest tests/unit/test_bulk_upsert_invariant.py -x` must pass.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — move INSERT aliasing block before main column loop; replace alias-skip guard with positional override; populate skip-set for main loop

**Tests to add**:

Add to `tests/unit/test_base_parser.py`:

- **Scenario A — alias matches positional (no-op case)**: parse `INSERT INTO t (col1, col2) SELECT a AS col1, b AS col2 FROM src`; assert edges contain `(src.a → t.col1)` and `(src.b → t.col2)`.
- **Scenario B — alias diverges from positional (the bug case)**: parse `INSERT INTO t (c1, c2, c3) SELECT a AS x, b AS y, c AS z FROM src`; assert edges contain `(src.a → t.c1)`, `(src.b → t.c2)`, `(src.c → t.c3)` and do NOT contain `(src.a → t.x)`, `(src.b → t.y)`, `(src.c → t.z)`.
- **Scenario C — perf invariant: body.copy() called once per INSERT, not per column**: patch `exp.Select.copy` via `unittest.mock.patch.object(exp.Select, "copy", wraps=exp.Select.copy)` and count only the full-body copy call (not all `copy()` calls). Parse a 5-column INSERT; assert `exp.Select.copy` was called exactly **once** (for `body_no_with = body.copy()`, not once per column). This directly pins the CLAUDE.md invariant against regression.

**Acceptance criteria**:
- `[ ]` `INSERT INTO t (c1, c2) SELECT a AS x, b AS y FROM src` produces edges `src.a → t.c1` and `src.b → t.c2` (positional, not alias-based)
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py` all green after the change
- `[ ]` `grep -n "body_no_with = body.copy()" src/sqlcg/parsers/base.py` shows exactly one occurrence outside any loop
- `[ ]` Positional INSERT block appears BEFORE the main `for loop_idx, col_expr` loop in the source — confirmed by line-number order in grep output
- `[ ]` `grep -n "if col_expr.alias:.*continue" src/sqlcg/parsers/base.py` returns zero results
- `[ ]` No TODO in the INSERT aliasing block

---

### PR-06 — Fix unqualified INSERT target creating orphan nodes (query-time fallback)

**Source**: GitHub issue #26 (MEDIUM)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `TableRef.__post_init__` stores `db=None` when no schema is present. `full_id` for `INSERT INTO FACT_T` yields `"fact_t"`. The DDL-defined node has `full_id = "mart.fact_t"`. These are distinct graph nodes with no edge between them. `analyze upstream mart.fact_t.col` returns empty; `analyze upstream fact_t.col` has the real lineage. No unification logic exists at index time or query time.

**Design choice for 1.0.2**: post-index unification at query time is the safest fix that avoids schema migration and preserves the two-scale design rule. It does not require a new graph node kind or a `default_schema` config. The approach: when `analyze upstream <qualified>` returns zero results, query the graph for nodes whose bare `name` (last `.`-component of `qualified`) matches, and return those results with a schema-resolution hint. This is a fallback, not a rewrite of the indexer.

Full `default_schema` indexer support (option 1 from the issue) is deferred to v1.1 because it requires touching the parser hot path and the `get_schema_aliases` config surface.

**What to do**:

1. Derive the bare-name fallback correctly. The ref argument is a `table.column` or `schema.table.column` string. The bare form strips only the schema prefix, keeping `table.column`:

   - For a **3-part ref** (`"mart.fact_t.amount"`): `".".join(ref.split(".")[1:])` → `"fact_t.amount"`. This strips one component (the schema) and keeps the rest intact.
   - For a **2-part ref** (`"fact_t.amount"`): the ref already has no schema prefix; `len(ref.split(".")) < 3` so no stripping is needed — use the ref as-is.
   - Do NOT use `ref.rsplit(".", 1)[-1]` — for `"mart.fact_t.amount"` that yields `"amount"` (just the column name), which would catastrophically match every column named `amount` in every table.

   ```python
   def _bare_ref(ref: str) -> str:
       """Strip schema prefix from a ref string, keeping table.column."""
       parts = ref.split(".")
       if len(parts) >= 3:
           return ".".join(parts[1:])  # drop schema, keep table.column
       return ref  # already bare (no schema prefix)
   ```

2. In `analyze.py`, after the `upstream` query returns empty, run a bare-name fallback query only when the ref has 3+ components (otherwise the ref is already bare and the primary query already failed — no fallback to try):
   ```python
   # After running the normal upstream query:
   if not results and len(ref.split(".")) >= 3:
       bare = _bare_ref(ref)  # e.g. "fact_t.amount" from "mart.fact_t.amount"
       fallback_results = backend.run_read(
           f"MATCH p=(c:{NodeLabel.COLUMN} {{id: $bare}})"
           f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src) "
           "RETURN src.id AS id LIMIT 100",
           {"bare": bare},
       )
       if fallback_results:
           console.print(
               f"[yellow]Hint:[/yellow] No results for '{ref}'. "
               f"Found {len(fallback_results)} edge(s) under bare name '{bare}'. "
               "The INSERT target may have been indexed without a schema prefix. "
               "Multiple tables with the same unqualified name in different schemas "
               "would all match — re-index with an explicit schema for precise results."
           )
           results = fallback_results
   ```
   This fallback only fires when the primary query returns zero edges AND the ref has a schema component to strip — it does not mask a genuine empty result for already-bare refs.

3. Apply the same `_bare_ref` fallback in `analyze downstream` and in `tools.py` `trace_column_lineage` and `find_column_definition`.

4. Do NOT change the indexer, parser, or graph schema. This is query-time only.

**Wiring verification**:

- `grep -n "_bare_ref\|bare_ref\|split.*\.\|join.*parts" src/sqlcg/cli/commands/analyze.py` must show the correct derivation using `".".join(parts[1:])` (not `rsplit`).
- `grep -n "_bare_ref\|rsplit" src/sqlcg/cli/commands/analyze.py` — must show zero `rsplit` calls in the fallback path.
- `grep -n "_bare_ref" src/sqlcg/server/tools.py` must show the fallback in `trace_column_lineage`.
- `grep -n "TODO" src/sqlcg/cli/commands/analyze.py` must return zero results.

**Files affected**:
- [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) — add `_bare_ref()` helper; add bare-name fallback to `upstream` and `downstream` guarded by `len >= 3`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — add bare-name fallback to `trace_column_lineage` and `find_column_definition`

**Tests to add**:

Add to `tests/integration/test_indexer_to_graph.py` or a new `tests/unit/test_unqualified_fallback.py`:

- **Scenario A — 3-part ref fallback strips schema correctly**: call `_bare_ref("mart.fact_t.amount")`; assert result is `"fact_t.amount"` (not `"amount"`).
- **Scenario B — 2-part ref returns unchanged**: call `_bare_ref("fact_t.amount")`; assert result is `"fact_t.amount"` (no change).
- **Scenario C — unqualified INSERT target, qualified analyze**: index SQL containing `INSERT INTO FACT_T SELECT amount FROM mart.orders` (no schema on target); invoke `analyze upstream mart.fact_t.amount`; assert edges containing `mart.orders.amount` are returned and a hint message is printed.
- **Scenario D — schema-qualified path not broken by fallback**: index SQL containing `INSERT INTO mart.fact_t SELECT amount FROM mart.orders`; invoke `analyze upstream mart.fact_t.amount`; assert edges are returned WITHOUT triggering the bare-name fallback (no hint message printed, primary query succeeds).

**Acceptance criteria**:
- `[ ]` `_bare_ref("mart.fact_t.amount")` returns `"fact_t.amount"` — confirmed by Scenario A test
- `[ ]` `_bare_ref("fact_t.amount")` returns `"fact_t.amount"` unchanged — confirmed by Scenario B test
- `[ ]` `sqlcg analyze upstream mart.fact_t.amount` returns edges (with hint) when the INSERT target was stored unqualified as `fact_t`
- `[ ]` `sqlcg analyze upstream mart.fact_t.amount` returns edges WITHOUT hint when the target was properly qualified — fallback does not fire
- `[ ]` No indexer or schema migration required for this fix
- `[ ]` No TODO in the bare-name fallback block

---

### PR-07 — Fix timeout observability + route parse warnings to log file

**Source**: GitHub issues #13 (observability) and #27d (timeout visibility) — combined because both share the same root cause and the same fix in `error_classify.py`.
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause** (B1 — confirmed against source):

There are two code paths that can time out a file:

1. **`_index_single_file` path** (`indexer.py:842`): emits `f"timeout:{timeout}s file={path.name} size=...B dialect=..."`. `error_classify.py:97` matches `msg.startswith("timeout:")` → bucket `"timeout"` → `parse_failed=True`. This path IS visible to `analyze failures --cause timeout`.

2. **`pool.py:_timeout_file` path** (`pool.py:492`): emits `f"timeout file={Path(path).name}"` (no colon after `"timeout"`, no time value). `error_classify.py:97` does NOT match because `"timeout file=..."` does not `startswith("timeout:")`. Result: `dominant_cause` returns `("", False)` → `parse_failed=False` → the timed-out file is invisible to `analyze failures --cause timeout`.

Additionally, `pool.py:_timeout_file` emits `f"skipped:poison file={Path(path).name}"` for poison-retry files. `_classify_error` has no case for `"skipped:poison"` — these also fall through to `"other"`.

The fix is in the message format and the classifier — NOT a new second-upsert approach. `_upsert_parsed_file` already calls `dominant_cause(parsed.errors)` at line 899 and writes `parse_failed`/`parse_cause` correctly from whatever errors the `ParsedFile` contains. The gap is that pool-path errors emit in a format the classifier cannot read.

**What to do**:

**Fix 1 — `pool.py:_timeout_file`**: Change the emitted message to match the `_index_single_file` format so `_classify_error` can read it:

```python
def _timeout_file(
    path: str,
    dialect: str | None,
    timeout_s: float,   # add this parameter
    poison: bool = False,
) -> ParsedFile:
    pf = ParsedFile(path=Path(path), dialect=dialect)
    if poison:
        pf.errors.append(f"skipped:poison file={Path(path).name}")
    else:
        pf.errors.append(
            f"timeout:{timeout_s:.0f}s file={Path(path).name}"
        )
    return pf
```

The call site in `_run_map_loop` (line 378) must pass `timeout_s=per_task_timeout`:
```python
results[tidx] = _timeout_file(path, self._dialect, timeout_s=per_task_timeout)
```

**Fix 2 — `error_classify.py:_classify_error`**: Extend to also handle `"skipped:poison"` messages from `_timeout_file`:

```python
# Existing (line 97):
if msg.startswith("timeout:"):
    return "timeout"

# Add poison handling:
if msg.startswith("skipped:poison"):
    return "timeout"   # poison = repeated timeouts; treat as timeout bucket
```

This ensures pool-path timeouts and poison-retry files both land in the `"timeout"` bucket, producing `parse_failed=True` and appearing in `analyze failures --cause timeout`.

**Fix 3 — add `log_path` to `KuzuConfig`**: Route `sqlcg` logger warnings to a file:

Add to `KuzuConfig`:
```python
log_path: Path = Field(default_factory=lambda: Path.home() / ".sqlcg" / "index.log")
```

Add to `KuzuConfig.from_env()` (following the existing `SQLCG_DB_PATH`/`SQLCG_BUFFER_POOL_MB` pattern):
```python
env_log = os.getenv("SQLCG_LOG_PATH")
return cls(
    db_path=Path(env_path) if env_path else Path.home() / ".sqlcg" / "graph.db",
    buffer_pool_size_mb=int(env_buf) if env_buf else 0,
    log_path=Path(env_log) if env_log else Path.home() / ".sqlcg" / "index.log",
)
```

The `SQLCG_LOG_PATH` env var must be wired in `from_env()` — without it the field is dead (cannot be overridden without changing code).

In `index.py`, configure a `FileHandler`:
```python
log_path = KuzuConfig.from_env().log_path
log_path.parent.mkdir(parents=True, exist_ok=True)
file_handler = logging.FileHandler(log_path)
file_handler.setLevel(logging.WARNING)
logging.getLogger("sqlcg").addHandler(file_handler)
```

**Fix 4 — add `--verbose` flag** to `index_cmd`:
```python
verbose: bool = typer.Option(False, "--verbose", "-v", help="Print parse warnings to stderr")
```
When `--verbose` is set, add a `StreamHandler(sys.stderr)` instead of the `FileHandler`.

Print a summary line when warnings > 0 and `--verbose` not set:
`"Parse warnings written to {log_path} — use --verbose to show here."`

**Wiring verification**:

- `grep -n "timeout_s\|per_task_timeout" src/sqlcg/indexer/pool.py` must show `_timeout_file` receiving `timeout_s=per_task_timeout` at the call site.
- `grep -n "startswith.*timeout\|skipped:poison" src/sqlcg/indexer/error_classify.py` must show both `timeout:` and `skipped:poison` handled.
- `grep -n "log_path\|SQLCG_LOG_PATH" src/sqlcg/core/config.py` must show `log_path` field in `KuzuConfig` AND `env_log = os.getenv("SQLCG_LOG_PATH")` in `from_env()`.
- `grep -n "log_path\|FileHandler" src/sqlcg/cli/commands/index.py` must show `KuzuConfig.from_env().log_path` used (not a hardcoded path).
- `grep -n "index\.log" src/sqlcg/cli/commands/index.py` must return zero results — no hardcoded path in `index.py`.

**Files affected**:
- [`src/sqlcg/indexer/pool.py`](../src/sqlcg/indexer/pool.py) — fix `_timeout_file` to emit `timeout:{N}s file=...` format; add `timeout_s` parameter; update call site
- [`src/sqlcg/indexer/error_classify.py`](../src/sqlcg/indexer/error_classify.py) — add `skipped:poison` → `"timeout"` classification
- [`src/sqlcg/core/config.py`](../src/sqlcg/core/config.py) — add `log_path` field to `KuzuConfig`; add `SQLCG_LOG_PATH` env override to `from_env()`
- [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py) — add `FileHandler` to `KuzuConfig.from_env().log_path`; add `--verbose` flag

**Tests to add**:

- **Scenario A — pool-path timeout appears in `analyze failures --cause timeout`**: in a unit test, construct a `ParsedFile` whose `errors` list contains `"timeout:5s file=foo.sql"` (the new pool format); pass through `dominant_cause`; assert `parse_failed=True` and `parse_cause="timeout"`. Then assert the same for `"skipped:poison file=foo.sql"`.
- **Scenario B — old format `"timeout file=foo.sql"` no longer emitted**: after the fix, `_timeout_file(path, dialect, timeout_s=5.0)` must produce `errors[0] == "timeout:5s file=foo.sql"` (not the old format). Assert this in a unit test.
- **Scenario C — log file is written to `KuzuConfig.log_path`, not a hardcoded path**: patch `KuzuConfig.from_env()` to return a config with `log_path` pointing to a tmp file; run `index_cmd`; assert the log file appears at the configured path.
- **Scenario D — `SQLCG_LOG_PATH` env var overrides default**: set `SQLCG_LOG_PATH=/tmp/custom.log` in env; call `KuzuConfig.from_env()`; assert `config.log_path == Path("/tmp/custom.log")`.

**Acceptance criteria**:
- `[ ]` After `sqlcg index <path>` that includes a file that times out via the pool path, `sqlcg analyze failures --cause timeout` returns a non-empty result containing that file
- `[ ]` `grep -n "skipped:poison" src/sqlcg/indexer/error_classify.py` shows the `"timeout"` bucket classification
- `[ ]` `grep -n "SQLCG_LOG_PATH" src/sqlcg/core/config.py` shows the env override in `from_env()`
- `[ ]` `grep -n "index\.log" src/sqlcg/cli/commands/index.py` returns zero results — path comes from `KuzuConfig`
- `[ ]` No TODO in the classifier or `FileHandler` setup path

---

### PR-08 — #27a noise glob + #27b CLI NoiseFilter

**Source**: GitHub issue #27 sub-items (a) and (b) (LOW, bundle)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**:
- **27a**: `config.py:139–145` default patterns miss `*_bck_*`. Pattern `*_bck_us39553` has more than a suffix `_bck_<digits>` component — it needs `*_bck_*` to catch the intermediate word. The existing `ignore_table_regexes` facility (empty by default) can handle this, but the common case should work out of the box.
- **27b**: `analyze.py` and `find.py` issue raw Cypher with no noise filtering. `NoiseFilter.from_config()` is used in `server/tools.py` for all MCP tools but not in the CLI commands. Users who filter noise in the MCP session see different answers than users who query via `sqlcg analyze upstream`.

**What to do**:

**27a:**

Add `"*_bck_*"` to the default patterns in `get_noise_filter_patterns`:
```python
default_patterns = [
    "*_bck",
    "*_bck_*",        # new: catches foo_bck_us39553, bar_bck_archive
    "*_bck_us",
    "*_bck_[0-9]*",
    "*_backup",
    "*_backup_[0-9]*",
]
```

**27b:**

`NoiseFilter.from_config(repo_root)` accepts an optional `repo_root: Path` and falls back to `Path.cwd()` when `None`. `analyze.py` has no repo-path context (unlike `tools.py`, which could derive it from the db path). The decision for `analyze.py`: pass `repo_root=None` to use `Path.cwd()` as the `.sqlcg.toml` search root. This is correct for the common case where the user runs `sqlcg analyze` from the repo root. If users keep `.sqlcg.toml` in a non-CWD location, the `SQLCG_DB_PATH` env var is the right mechanism, not a special path-derivation in this command. Add this decision to the acceptance criteria.

In `analyze.py`, apply `NoiseFilter.from_config()` to the `results` list before printing for `upstream` and `downstream`. Add a `--raw` flag to opt out:

```python
raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results")

# After the query:
if not raw:
    from sqlcg.server.noise_filter import NoiseFilter
    nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
    ids = [r["id"] for r in results]
    kept, excluded = nf.filter_nodes(ids)
    results = [r for r in results if r["id"] in set(kept)]
```

Apply the same pattern to `find_table` in `find.py` with a `--raw` flag.

Do not apply noise filtering to `find_column` or `find_pattern` — those are structural lookups where the user is explicitly targeting a node by name; noise filtering would hide what they asked for.

**Wiring verification**:

- `grep -n "NoiseFilter\|from_config" src/sqlcg/cli/commands/analyze.py` must show `NoiseFilter.from_config()` in both `upstream` and `downstream`.
- `grep -n "NoiseFilter\|from_config" src/sqlcg/cli/commands/find.py` must show `NoiseFilter.from_config()` in `find_table`.
- `grep -n "\*_bck_\*" src/sqlcg/core/config.py` must show the new default pattern in `get_noise_filter_patterns`.
- `grep -n "TODO" src/sqlcg/cli/commands/analyze.py` must return zero results after the change.

**Files affected**:
- [`src/sqlcg/core/config.py`](../src/sqlcg/core/config.py) — add `"*_bck_*"` to default patterns
- [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) — add `NoiseFilter.from_config()` to `upstream` and `downstream`; add `--raw` flag
- [`src/sqlcg/cli/commands/find.py`](../src/sqlcg/cli/commands/find.py) — add `NoiseFilter.from_config()` to `find_table`; add `--raw` flag

**Tests to add**:

Update `tests/unit/test_noise_filter.py`:

- **Scenario A — `*_bck_*` default catches mid-suffix variant**: call `NoiseFilter.from_config()` with default patterns (no `.sqlcg.toml`); assert `is_noise("ba.foo_bck_us39553")` returns `True`.
- **Scenario B — `*_bck_*` does not over-match non-backup names**: assert `is_noise("ba.bck_tracker")` returns `False` (name starts with `bck_`, not ends with `_bck`). Note: `*_bck_*` is anchored as an fnmatch glob on the table-name part — `fnmatch("bck_tracker", "*_bck_*")` returns `False` because there is no `_bck_` in the middle. Verify this.
- **Scenario C — CLI `analyze upstream` respects NoiseFilter**: use an in-memory backend; index a SQL file that produces a lineage edge from a backup table `ba.src_bck_us99` to `ba.fact`; call the CLI command; assert `ba.src_bck_us99` is not in the output; call with `--raw`; assert it IS in the output.

**Acceptance criteria**:
- `[ ]` `sqlcg analyze upstream <col>` excludes backup-table nodes by default; `--raw` restores them
- `[ ]` `NoiseFilter.is_noise("ba.foo_bck_us39553")` returns `True` with default config
- `[ ]` `grep -n "NoiseFilter" src/sqlcg/cli/commands/analyze.py` shows two calls (upstream + downstream)
- `[ ]` `NoiseFilter.from_config()` in `analyze.py` is called with `repo_root=None` (falls back to `Path.cwd()`); confirmed by grep showing no explicit `repo_root` argument at the call site
- `[ ]` No TODO in the noise-filter application path in `analyze.py`

---

## Test Strategy

### The single most important regression guard

This guard would have caught the #22 stdio transport bug before shipping v1.0.0/v1.0.1:

```python
# tests/unit/test_mcp_stdio_smoke.py

import json
import subprocess

INIT_REQUEST = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "0"},
    },
}) + "\n"


def test_mcp_start_handshake_on_stdout():
    """MCP initialize must produce a JSON-RPC result on stdout.

    Guards against the v1.0.0/v1.0.1 regression where sys.stdout was replaced
    with sys.stderr before stdio_server() captured sys.stdout.buffer, causing
    all JSON-RPC frames to go to stderr and Claude Code to time out the handshake.
    """
    proc = subprocess.Popen(
        ["uv", "run", "sqlcg", "mcp", "start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(INIT_REQUEST.encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, (
            "sqlcg mcp start produced no output on stdout — "
            "stdio transport regression: JSON-RPC frames are going to stderr "
            "instead of stdout. Guards against v1.0.0 regression."
        )
        response = json.loads(line)
        assert "result" in response, (
            f"MCP initialize must return a result on stdout — "
            f"stdio transport regression. Got: {line!r}"
        )
        assert "capabilities" in response.get("result", {}), (
            "MCP initialize result must contain capabilities field. "
            f"Response was: {response}"
        )
    finally:
        proc.kill()
        proc.wait()
```

This test is **not** marked `xfail`. It passes after PR-01 ships. Before PR-01 ships, it fails with an assertion error on `assert line` (stdout is empty). The failure message explicitly names the regression.

---

## Wiring Checklist

| Question | PR-01 | PR-02 | PR-03 | PR-04 | PR-05 | PR-06 | PR-07 | PR-08 |
|----------|-------|-------|-------|-------|-------|-------|-------|-------|
| What calls this? | `mcp_start()` in `mcp.py` calls `server_main()` which is `main()` in `server.py` | `install_cmd()` called by Typer CLI entry point | `reindex_cmd()` calls `indexer.resync_changed(path, ...)` which calls `git_name_status_delta(root, ...)` | `find_table()` called by `sqlcg find table <name>` | `_extract_column_lineage()` called from `parse_file()` in every parser subclass | `upstream()` / `downstream()` in `analyze.py`; `trace_column_lineage()` in `tools.py` | `index_cmd()` → `_run_index()` → `Indexer.index_repo()` via pool; `_upsert_parsed_file` reads errors already set by pool/`_index_single_file` | `upstream()` / `downstream()` in `analyze.py`; `find_table()` in `find.py` |
| Where is the callback/parameter passed? | `mcp.run()` replaced by `anyio.run(_run_stdio_async_with_real_stdout)`; `_real_stdout_buffer` captured at module top BEFORE `_configure_mcp_logging()` call AND before `FastMCP(...)` construction | `claude mcp add -s user <name> <cmd> <args>` via `subprocess.run`; fallback writes `~/.claude.json`; `ARCHITECTURE_REVIEW.md § 9.2` updated to document new path | `path = path.resolve()` at top of `reindex_cmd`; `root = root.resolve()` at top of `git_name_status_delta`; defensive resolve in `load_ignore_spec`; `.sqlcgignore` read from resolved `root / ".sqlcgignore"` | `name = name.lower()` added before `backend.run_read` in `find_table` | `positional_col_names` dict and `body_no_with = body.copy()` computed BEFORE main column loop; positional block removes the `if col_expr.alias: continue` guard; skip set gates the main loop | `_bare_ref(ref)` uses `".".join(ref.split(".")[1:])` for 3-part refs; fallback fires only when primary returns empty AND `len(ref.split(".")) >= 3` | `_timeout_file` emits `"timeout:{N}s file=..."` format; `_classify_error` handles `"skipped:poison"` as `"timeout"`; `log_path` in `KuzuConfig.from_env()` via `SQLCG_LOG_PATH` | `NoiseFilter.from_config(repo_root=None)` called in `analyze.py` (falls back to `Path.cwd()` for `.sqlcg.toml` lookup); `--raw` opts out |
| What constant/path does this align with? | fd 1 captured via `os.dup(1)` — no config constant; mcp SDK pinned `>=1.27.0,<2.0` in `pyproject.toml` | `~/.claude.json` (not `settings.json`); `claude mcp add -s user` scope | `Path.resolve()` — standard library, no config constant | No constant — `.lower()` normalization matches C2 TableRef normalization in `base.py` | No config constant — INSERT column list from the AST `stmt.this.expressions`; `body_no_with = body.copy()` once per statement (CLAUDE.md invariant) | `NodeLabel.COLUMN` and `RelType.COLUMN_LINEAGE` from `core/schema.py`; `_bare_ref` strips schema prefix keeping `table.col` form | `KuzuConfig.from_env().log_path` via `SQLCG_LOG_PATH` env var — never `Path.home() / ".sqlcg" / "index.log"` hardcoded in `index.py` | `NoiseFilter.from_config()` — `repo_root=None` → `Path.cwd()` fallback (documented in `noise_filter.py:153`) |
| Does any TODO remain in the happy path? | No — `_real_stdout_buffer` capture and `_run_stdio_async_with_real_stdout` must be complete | No — `claude mcp add` call and `~/.claude.json` fallback must both be complete; `ARCHITECTURE_REVIEW.md § 9.2` updated in same PR | No — three `resolve()` calls, no deferred work | No — one `.lower()` call | No — positional block moved before main loop; `if col_expr.alias: continue` guard removed; no deferred edges | No — `_bare_ref` helper and fallback are the complete fix for 1.0.2; `default_schema` indexer support goes in `ARCHITECTURE_REVIEW.md` not in code | No — `_timeout_file` format fixed, classifier updated, `FileHandler` setup complete | No — `NoiseFilter.from_config()` call and `--raw` flag must be complete |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg mcp start` produces a valid JSON-RPC response on stdout when sent an `initialize` request — confirmed by `test_mcp_stdio_smoke.py::test_mcp_start_handshake_on_stdout` passing
- `[ ]` After `sqlcg install --scope project` on a machine where `claude` is on PATH, `claude mcp list` shows `sql-code-graph`; `ARCHITECTURE_REVIEW.md § 9.2` no longer references `settings.json`
- `[ ]` `sqlcg reindex .` from a git-tracked directory does not raise `ValueError` and completes the delta computation; `.sqlcgignore` is read from the resolved root
- `[ ]` `sqlcg find table WTFS_VOORRAAD_WEEK` returns the same result as `sqlcg find table wtfs_voorraad_week` on an indexed graph
- `[ ]` Parsing `INSERT INTO t (c1, c2) SELECT a AS x, b AS y FROM src` produces edges `src.a → t.c1` and `src.b → t.c2` (positional attribution, not alias-based)
- `[ ]` `sqlcg analyze upstream mart.fact_t.amount` returns results (with hint) when the INSERT target was stored unqualified as `fact_t`; `_bare_ref("mart.fact_t.amount")` returns `"fact_t.amount"` (not `"amount"`)
- `[ ]` After `sqlcg index <path>` that includes a file timed out via the pool path, `sqlcg analyze failures --cause timeout` returns a non-empty result for that file
- `[ ]` `sqlcg analyze upstream <col>` excludes backup tables matching `*_bck_*` by default; `--raw` restores them; `NoiseFilter.from_config()` in `analyze.py` uses `Path.cwd()` as `.sqlcg.toml` root
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, and `test_bulk_upsert_invariant.py` all pass after PR-05 merges

---

## Risks and Mitigations

| Risk | Ticket | Mitigation |
|------|--------|------------|
| `_run_stdio_async_with_real_stdout` accesses FastMCP internals (`mcp._mcp_server`) that may change with SDK upgrades | PR-01 | mcp SDK is already pinned `>=1.27.0,<2.0` in `pyproject.toml`; smoke test error message names the SDK version; if SDK is upgraded past `<2.0`, verify `_mcp_server` attribute exists |
| `_real_stdout_buffer` capture order: if another module-level import emits to stdout before `server.py` is loaded, fd 1 may already be dirty | PR-01 | `_real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)` is the first statement in `server.py` before any imports — this is the correct position; document in the file comment |
| `claude mcp add` CLI interface may differ across Claude Code versions | PR-02 | Shell out with `subprocess.run(check=False)` and always fall back to `~/.claude.json` write on non-zero exit; log the stderr from the failed `claude mcp add` call |
| INSERT positional block change may introduce duplicate edges if both the main loop and the positional block fire for the same column | PR-05 | The `positional_col_names` skip set in the main loop prevents double-emission; Scenario A (alias matches positional) guards against duplicates; `grep -n "if col_expr.alias:.*continue" base.py` must return zero results after the fix |
| Bare-name fallback in PR-06 may return wrong results if two tables have the same unqualified name in different schemas | PR-06 | Fallback only fires when `len(ref.split(".")) >= 3` AND primary returns empty; hint message explicitly names the multi-schema ambiguity; users with schema collisions should re-index with explicit schema |
| `_bare_ref` using `rsplit` would yield only the column name for 3-part refs — catastrophic false-positive | PR-06 | `_bare_ref` uses `".".join(parts[1:])` not `rsplit(".", 1)[-1]`; Scenario A test pins this derivation |
| Pool-path `_timeout_file` signature change (adding `timeout_s` param) must update all call sites | PR-07 | There is one call site: `_run_map_loop` line 378; grep confirms; add `timeout_s=per_task_timeout` there |
| `*_bck_*` glob may over-match tables with `_bck_` in the middle of a non-backup name (e.g. `abck_tracker`) | PR-08 | `fnmatch("abck_tracker", "*_bck_*")` returns `False` — `_bck_` must have a preceding `_` char; verify this in the test suite |
