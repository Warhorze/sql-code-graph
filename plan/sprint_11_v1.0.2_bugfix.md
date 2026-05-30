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
- #27d (per-file timeout failure bucket): requires new graph node kind and schema migration → deferred
- #27e (duplicate rows in `analyze upstream`): upstream KuzuDB path deduplication — low urgency → deferred
- Full default_schema config for unqualified DDL/DML (#26 option 1): design impact is sprint-sized; PR-06 ships the safer post-index unification fallback at query time

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|----------------|
| #22 — stdio frames on stderr | Confirmed. `server.py:25` sets `sys.stdout = sys.stderr` at module-import time (line 29, before `mcp.run()`). `mcp.server.stdio.stdio_server()` captures `sys.stdout.buffer` at call time, so frames go to stderr fd 2. `sys.stdout` is replaced before the buffer is saved. |
| #23 — install writes settings.json | Confirmed. `install.py:14` hardcodes `_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"`. `claude mcp list` on this machine returns no sqlcg server despite `settings.json` containing the entry. `claude mcp add -s user` is the correct write path. |
| #24 — reindex relative path crash | Confirmed. `git_delta.py:78` calls `load_ignore_spec(root)` with the un-resolved `root` from the typer `Path` argument. `is_ignored` at `ignore.py:36` calls `path.relative_to(root)` where `path` is an absolute path produced by `(root / rel_path).resolve()` and `root` is still `.`. `ValueError` is raised. `reindex.py` never calls `path.resolve()`. |
| #12 — find table case-sensitive | Confirmed. `find.py:21` passes `{"name": name}` with no `.lower()`. Graph keys are lowercase (C2). MCP `find_table_usages` at `tools.py:593` does `.lower()`; the CLI command does not. |
| #25 — INSERT positional mapping | Confirmed. `base.py:922–961` INSERT aliasing block iterates over `col_expressions` and skips columns that `col_expr.alias` is truthy. If the SELECT alias (e.g. `x`) differs from the INSERT column list name (e.g. `c1`), the alias path short-circuits and the positional mapping is never applied. The main column loop at line 692 maps by SELECT alias, not INSERT target column position. Edge attributed to wrong column name. |
| #26 — unqualified INSERT orphan node | Confirmed. `TableRef.__post_init__` lowercases `name` and sets `db=None` when no schema is present. `full_id` for `INSERT INTO FACT_T` produces `"fact_t"` (no schema). DDL-defined node is `"mart.fact_t"`. No unification logic exists; `analyze upstream mart.fact_t.col` and `analyze upstream fact_t.col` return disjoint results. |
| #13 — index parse warnings to stdout | Confirmed. `index.py:72–74` sets `logging.CRITICAL` by default (suppresses all warnings) and `logging.DEBUG` only with `--debug`. Warnings from the indexer go through Python `logging`, which is suppressed at `CRITICAL` level normally. However the progress bar uses `redirect_stderr=True` which captures stderr. Current state: warnings are suppressed, not noisy. Bug #13 may be less severe in v1.0.1+ than reported. Assessment: the real remaining gap is that timed-out files appear in the summary count but not in `analyze failures` (which only queries `f.parse_failed=true` nodes, not timeout nodes). |
| #27a — `*_bck_*` glob missing | Confirmed. `config.py:139–145` default patterns are `["*_bck", "*_bck_us", "*_bck_[0-9]*", "*_backup", "*_backup_[0-9]*"]`. Pattern `*_bck_*` is missing, so `foo_bck_us39553` is not matched by any default. The `ignore_table_regexes` facility exists but defaults to empty. |
| #27b — CLI analyze/find does not apply NoiseFilter | Confirmed. `analyze.py` and `find.py` issue raw Cypher with no noise filtering. `NoiseFilter.from_config()` is only called in `server/tools.py`. |

---

## Ticket Table

| ID | Title | Files | Effort | Priority | Depends on | Blocks |
|----|-------|-------|--------|----------|------------|--------|
| PR-01 | Fix stdio MCP transport (fd 1 vs fd 2) | `server/server.py`, `tests/unit/test_server.py`, `tests/unit/test_mcp_stdio_smoke.py` (new) | S | P0 | INDEPENDENT | PR-02 (reveals) |
| PR-02 | Fix install to write via `claude mcp add` | `cli/commands/install.py`, `cli/commands/mcp.py`, `tests/unit/test_install.py` | S | P0 | INDEPENDENT | NONE |
| PR-03 | Fix reindex relative path crash | `cli/commands/reindex.py`, `indexer/git_delta.py`, `utils/ignore.py`, `tests/unit/test_git_delta.py` | XS | P1 | INDEPENDENT | NONE |
| PR-04 | Fix `find table` case-sensitivity | `cli/commands/find.py`, `tests/unit/test_cli.py` | XS | P1 | INDEPENDENT | NONE |
| PR-05 | Fix INSERT…SELECT positional column mapping | `parsers/base.py`, `tests/unit/test_base_parser.py`, `tests/unit/test_perf_scaling_guard.py` (verify) | M | P1 | INDEPENDENT | NONE |
| PR-06 | Fix unqualified INSERT orphan node (query-time fallback) | `cli/commands/analyze.py`, `server/tools.py`, `tests/unit/test_firstuser_findings.py` | S | P2 | INDEPENDENT | NONE |
| PR-07 | Index warnings: route to log file, add `--verbose` | `cli/commands/index.py`, `core/config.py`, `indexer/indexer.py`, `tests/unit/test_index_cmd.py` | S | P2 | INDEPENDENT | NONE |
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

**Root cause**: `server.py:25` replaces `sys.stdout` with `sys.stderr` at module-import time (line 29 calls `_configure_mcp_logging()` at module scope). `mcp.server.stdio.stdio_server()` captures `sys.stdout.buffer` at call time (inside `FastMCP.run_stdio_async`, which runs only when `mcp.run()` is invoked). Since `sys.stdout` has already been replaced by the time `stdio_server()` accesses `sys.stdout.buffer`, it gets `sys.stderr.buffer` — JSON-RPC frames are written to fd 2 (stderr). The client reads from fd 1 (stdout) and sees nothing; the handshake times out.

The comment in the file says "MCP uses stdout for JSON-RPC messages" and claims to protect it by redirecting stdout — but the protection is inverted: it destroys the stream the protocol needs.

**What to do**:

1. In `server.py`, save the real stdout binary stream **before** any reassignment:
   ```python
   import os
   import sys

   # Capture the real fd 1 binary stream before any reassignment.
   # stdio_server() will receive this as its explicit stdout argument so that
   # JSON-RPC frames go to fd 1 regardless of what sys.stdout points to later.
   _real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)
   ```

2. Replace `_configure_mcp_logging` to redirect logging only (not protocol):
   ```python
   def _configure_mcp_logging() -> None:
       """Route all Python logging and print() output to stderr.

       sys.stdout is replaced with sys.stderr so that any stray print() call
       does not pollute fd 1 (reserved for MCP JSON-RPC frames).
       The real fd 1 binary stream is captured in _real_stdout_buffer before
       this replacement and passed explicitly to stdio_server().
       """
       sys.stdout = sys.stderr
   ```

3. In `main()`, pass `_real_stdout_buffer` explicitly to `mcp.run()`. The `FastMCP.run()` method accepts no `stdout` argument directly, but `FastMCP.run_stdio_async` calls `stdio_server()` with no arguments. Patch by subclassing or by calling `run_stdio_async` directly with an explicit `stdout` argument:

   ```python
   import io
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
       load_dotenv()
       import sqlcg.server.tools
       sqlcg.server.tools.init_backend(db_path)
       try:
           anyio.run(_run_stdio_async_with_real_stdout)
       finally:
           sqlcg.server.tools.shutdown_backend()
   ```

   This bypasses `mcp.run()` (which calls `run_stdio_async` with no stdout override) and directly drives the server loop with the captured fd 1.

4. Remove the module-level `_configure_mcp_logging()` call and instead call it at the start of `main()`, **after** `_real_stdout_buffer` has been captured. The invariant is: capture fd 1 first, redirect `sys.stdout` second.

5. Configure the `logging` module to route to stderr explicitly:
   ```python
   import logging
   logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
   ```

**Wiring verification**:

- `grep -n "_real_stdout_buffer\|os.dup" src/sqlcg/server/server.py` must show the `os.dup(1)` capture line appearing **before** any `sys.stdout =` reassignment.
- `grep -n "sys.stdout = sys.stderr\|_configure_mcp_logging" src/sqlcg/server/server.py` — must NOT appear at module scope; must appear inside `main()` after `_real_stdout_buffer` is set.
- `grep -n "stdio_server" src/sqlcg/server/server.py` must show explicit `stdout=` argument passed to `stdio_server()`.
- After the fix, the existing `test_server.py::test_configure_mcp_logging_redirects_stdout` test must be updated (it currently asserts `sys.stdout is sys.stderr` which is correct, but the call order changes).

**Files affected**:
- [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py) — capture fd 1, reorder, pass explicit stdout to stdio_server
- [`tests/unit/test_server.py`](../tests/unit/test_server.py) — update existing test; add smoke test
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

- **Scenario B — `_configure_mcp_logging` does not destroy fd 1**: call `_configure_mcp_logging()`, then assert `os.write(1, b"")` succeeds (fd 1 is still writable). This is a unit test in `test_server.py`.

**Acceptance criteria**:
- `[ ]` `sqlcg mcp start` sends JSON-RPC frames on stdout (fd 1) — confirmed by `test_mcp_stdio_smoke.py::test_mcp_start_handshake_on_stdout` passing
- `[ ]` `grep -n "os.dup(1)\|_real_stdout_buffer" src/sqlcg/server/server.py` shows capture before any `sys.stdout =` reassignment
- `[ ]` No TODO in `main()` or `_run_stdio_async_with_real_stdout`
- `[ ]` Existing `test_server.py::test_configure_mcp_logging_redirects_stdout` still passes (updated to match new call order)

---

### PR-02 — Fix `install` to write via `claude mcp add`

**Source**: GitHub issue #23 (HIGH)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `install.py:14` sets `_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"`. Claude Code reads MCP servers from the `claude mcp add` registry (backed by `~/.claude.json` `mcpServers.user` scope, not `settings.json`). Confirmed on this machine: `~/.claude/settings.json` has the entry; `claude mcp list` does not show it. The `claude mcp add -s user <name> <command> [args...]` CLI is the correct write path.

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

**Wiring verification**:

- `grep -n "settings.json\|_SETTINGS_PATH" src/sqlcg/cli/commands/install.py` must return zero results after the fix.
- `grep -n "settings.json" src/sqlcg/cli/commands/mcp.py` must return zero results after the fix.
- `grep -n "claude mcp add\|subprocess.*claude\|shutil.which.*claude" src/sqlcg/cli/commands/install.py` must show the `claude mcp add` shelling out.
- `grep -n ".claude.json\|claude_json" src/sqlcg/cli/commands/install.py` must show the fallback write path.

**Files affected**:
- [`src/sqlcg/cli/commands/install.py`](../src/sqlcg/cli/commands/install.py) — replace `settings.json` path with `claude mcp add` + `~/.claude.json` fallback
- [`src/sqlcg/cli/commands/mcp.py`](../src/sqlcg/cli/commands/mcp.py) — remove `settings.json` write from `mcp_setup --write`

**Tests to add**:

Update `tests/unit/test_install.py`:

- **Scenario A — `claude` available, `claude mcp add` succeeds**: mock `shutil.which("claude")` to return a path and `subprocess.run` to return `returncode=0`; invoke `install_cmd(scope="project", dry_run=False)`; assert `subprocess.run` was called with args containing `["claude", "mcp", "add", "-s", "user", "sql-code-graph", ...]`; assert no write to `settings.json`.
- **Scenario B — `claude` not available, falls back to `~/.claude.json`**: mock `shutil.which("claude")` to return `None`; invoke `install_cmd(scope="project", dry_run=False)`; assert the mock for `~/.claude.json` was written with `mcpServers.user.sql-code-graph` present; assert no write to `settings.json`.
- **Scenario C — `--dry-run` prints command, writes nothing**: mock both; assert no file writes and stdout contains the `claude mcp add` command string.

The assertion "no write to `settings.json`" means `Path.home() / ".claude" / "settings.json"` is not opened for writing — verify via `unittest.mock.patch("pathlib.Path.open")` or by checking calls to `os.replace`.

**Acceptance criteria**:
- `[ ]` `sqlcg install --scope project` runs `claude mcp add -s user sql-code-graph sqlcg mcp start` when `claude` is on PATH
- `[ ]` After `sqlcg install --scope project`, `claude mcp list` shows `sql-code-graph`
- `[ ]` `grep -n "settings.json" src/sqlcg/cli/commands/install.py` returns zero results
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

1. In the INSERT aliasing block (lines 922–961), change the alias-skip guard. Instead of skipping when `col_expr.alias` is truthy, **rename** the target: use `insert_cols[idx]` as both the destination column name and the alias for `sg_lineage`. The existing `body_no_with + aliased` pattern handles this correctly — extend it to also apply when the expression already has an alias, replacing the alias with the positional name:

   ```python
   # Before (line 939):
   if col_expr.alias:
       continue  # already handled by the main col loop

   # After:
   # Positional mapping always wins when an INSERT column list is present.
   # If the SELECT alias matches the INSERT column name, this is a no-op.
   # If they differ, we emit the edge under the INSERT column name (positional).
   insert_col = insert_cols[idx]
   if not insert_col:
       continue
   # Replace (or add) alias with the INSERT target column name
   aliased = exp.Alias(this=col_expr.copy(), alias=insert_col)
   body_no_with.set("expressions", [aliased])
   patched_sql = body_no_with.sql(dialect=self.DIALECT)
   try:
       root = sg_lineage(insert_col, patched_sql, dialect=self.DIALECT)
       ...
   ```

2. **Suppress the main-loop alias attribution for INSERT columns covered by the positional block**: when the INSERT aliasing block handles a column, that column should NOT also be emitted by the main loop (which would create a duplicate edge with the wrong destination name). Track which SELECT item indices were handled by the positional block and skip them in the main loop.

   Implementation: collect `positionally_handled_aliases: set[str]` before the main loop. After the INSERT aliasing block runs, populate it with the aliases of handled columns. In the main loop, skip any `col_expr` whose alias is in `positionally_handled_aliases`. This requires restructuring — the INSERT block currently runs after the main loop. The cleanest fix is to run the INSERT column-list scan **before** the main column loop and populate a skip set:

   ```python
   # Compute INSERT positional skip set BEFORE the main column loop
   positional_col_names: dict[int, str] = {}   # idx → insert_col_name
   if isinstance(stmt, exp.Insert) and isinstance(stmt.this, exp.Schema):
       insert_cols_list = [c.name for c in stmt.this.expressions]
       for idx, col_expr in enumerate(col_expressions):
           if idx < len(insert_cols_list) and insert_cols_list[idx]:
               positional_col_names[idx] = insert_cols_list[idx]

   # In the main loop, skip positions covered by positional mapping:
   for loop_idx, col_expr in enumerate(col_expressions):
       if loop_idx in positional_col_names:
           continue  # positional INSERT block handles this column
       ...
   ```

3. The INSERT aliasing block (now run after the main loop, or refactored to run before) emits edges under `insert_cols[idx]` for all positions in `positional_col_names`, using the existing `body_no_with + patched_sql` one-body-copy pattern.

4. **Perf invariant check (mandatory)**: after the change, verify all four invariants are still intact:
   - `body_no_with = body.copy()` still happens **once** before the INSERT column loop (not inside it).
   - `copy=False` and `trim_selects=False` are still passed to `sg_lineage` when `body_scope` is not None.
   - `body_scope` is still built **once** per statement before the column loop.
   - The pure-literal skip `if not list(col_expr.find_all(exp.Column)): continue` is still present.

5. Emit an optional diagnostic log when a SELECT alias does not match its positional INSERT target column name:
   ```python
   if col_expr.alias and col_expr.alias != insert_col:
       self._log.debug(
           "INSERT positional override: SELECT alias %r → INSERT col %r at position %d",
           col_expr.alias, insert_col, idx,
       )
   ```

**Wiring verification**:

- `grep -n "positional_col_names\|positionally_handled" src/sqlcg/parsers/base.py` must show the skip-set construction above the main column loop.
- `grep -n "body_no_with = body.copy()" src/sqlcg/parsers/base.py` must show exactly **one** occurrence (not inside a loop).
- `grep -n "copy=False" src/sqlcg/parsers/base.py` must still show the `copy=False` kwarg in the `sg_kwargs` construction.
- `grep -n "body_scope = build_scope" src/sqlcg/parsers/base.py` must show exactly **two** occurrences (primary + schema-free retry), both inside the `if scope is None:` block.
- `rtk uv run pytest tests/unit/test_perf_scaling_guard.py -x` must pass.
- `rtk uv run pytest tests/unit/test_T09_01_qualify_once.py -x` must pass.
- `rtk uv run pytest tests/unit/test_bulk_upsert_invariant.py -x` must pass.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — refactor INSERT aliasing block; add positional skip set

**Tests to add**:

Add to `tests/unit/test_base_parser.py`:

- **Scenario A — alias matches positional (no-op case)**: parse `INSERT INTO t (col1, col2) SELECT a AS col1, b AS col2 FROM src`; assert edges contain `(src.a → t.col1)` and `(src.b → t.col2)`.
- **Scenario B — alias diverges from positional (the bug case)**: parse `INSERT INTO t (c1, c2, c3) SELECT a AS x, b AS y, c AS z FROM src`; assert edges contain `(src.a → t.c1)`, `(src.b → t.c2)`, `(src.c → t.c3)` and do NOT contain `(src.a → t.x)`, `(src.b → t.y)`, `(src.c → t.z)`.
- **Scenario C — perf invariant: body.copy() called once per INSERT, not per column**: use `unittest.mock.patch.object(exp.Select, "copy")` to count calls during parsing of a 5-column INSERT; assert `copy` was called exactly **once** (for `body_no_with = body.copy()`).

**Acceptance criteria**:
- `[ ]` `INSERT INTO t (c1, c2) SELECT a AS x, b AS y FROM src` produces edges `src.a → t.c1` and `src.b → t.c2` (positional, not alias-based)
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py` all green after the change
- `[ ]` `grep -n "body_no_with = body.copy()" src/sqlcg/parsers/base.py` shows exactly one occurrence outside any loop
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

1. In `analyze.py`, after the `upstream` query returns empty, run a bare-name fallback query:
   ```python
   # After running the normal upstream query:
   if not results:
       # Bare-name fallback: try without schema prefix
       bare = ref.rsplit(".", 1)[-1]  # e.g. "fact_t.col" from "mart.fact_t.col"
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
               "The INSERT target may be unqualified — re-index with an explicit schema or "
               "use 'sqlcg find table' to find the stored qualified name."
           )
           results = fallback_results
   ```

2. Apply the same bare-name fallback in `analyze downstream` and in `tools.py` `trace_column_lineage` and `find_column_definition`.

3. The bare-name match is approximate (multiple tables with the same name in different schemas could match). The hint message must name this limitation.

4. Do NOT change the indexer, parser, or graph schema. This is query-time only.

**Wiring verification**:

- `grep -n "bare_name\|bare =\|rsplit.*1.*-1" src/sqlcg/cli/commands/analyze.py` must show the fallback in both `upstream` and `downstream`.
- `grep -n "bare_name\|rsplit" src/sqlcg/server/tools.py` must show the fallback in `trace_column_lineage`.
- `grep -n "TODO" src/sqlcg/cli/commands/analyze.py` must return zero results.

**Files affected**:
- [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) — add bare-name fallback to `upstream` and `downstream`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — add bare-name fallback to `trace_column_lineage` and `find_column_definition`

**Tests to add**:

Add to `tests/integration/test_indexer_to_graph.py` or a new `tests/unit/test_unqualified_fallback.py`:

- **Scenario A — unqualified INSERT target, qualified analyze**: index SQL containing `INSERT INTO FACT_T SELECT amount FROM mart.orders` (no schema on target); verify that `analyze upstream fact_t.amount` (bare name) returns `mart.orders.amount` in the result set and that `analyze upstream mart.fact_t.amount` (qualified) returns results (after fallback) with a hint message.
- **Scenario B — schema-qualified path not broken by fallback**: index SQL containing `INSERT INTO mart.fact_t SELECT amount FROM mart.orders`; verify that `analyze upstream mart.fact_t.amount` returns results WITHOUT triggering the bare-name fallback (no hint message printed).

**Acceptance criteria**:
- `[ ]` `sqlcg analyze upstream fact_t.amount` returns edges when the INSERT target was stored bare, with a printed hint message
- `[ ]` `sqlcg analyze upstream mart.fact_t.amount` returns edges when the target was qualified, without the hint
- `[ ]` No indexer or schema migration required for this fix
- `[ ]` No TODO in the bare-name fallback block

---

### PR-07 — Index parse warnings: route to log file, add `--verbose`

**Source**: GitHub issue #13 (observability)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause investigation**: The reported symptom (hundreds of WARNING lines to stdout) differs from the current v1.0.1 behaviour. `index.py:72–74` sets `logging.CRITICAL` by default (all warnings suppressed) and `logging.DEBUG` only with `--debug`. The progress bar uses `redirect_stderr=True`, which captures stderr. Current state: warnings are NOT shown by default.

The remaining real gap (confirmed by reading the code): the `analyze failures` command at `analyze.py:71` queries `WHERE f.parse_failed = true` — this covers parse failures with recorded E-codes. But timed-out files are recorded in the `summary["error_summary"]["timeout"]` counter and printed in the CLI summary, but are NOT written to `File` nodes with `parse_failed=true`. They fall into a different bucket that is not queryable via `analyze failures`.

**What to do**:

1. **Route sqlcg logger output to a log file** when the log file path is configured via `KuzuConfig`:

   Add `log_path` to `KuzuConfig`:
   ```python
   log_path: Path = Field(default_factory=lambda: Path.home() / ".sqlcg" / "index.log")
   ```

   In `index.py`, configure a `FileHandler` to route `logging.WARNING+` output from the `sqlcg` logger to `KuzuConfig.from_env().log_path`:
   ```python
   log_path = KuzuConfig.from_env().log_path
   log_path.parent.mkdir(parents=True, exist_ok=True)
   file_handler = logging.FileHandler(log_path)
   file_handler.setLevel(logging.WARNING)
   logging.getLogger("sqlcg").addHandler(file_handler)
   logging.getLogger("sqlcg").setLevel(logging.WARNING)
   ```

2. **Add `--verbose` flag** to `index_cmd` that routes warnings to stderr instead of the log file (existing `--debug` flag for DEBUG-level is too noisy; `--verbose` is WARNING+):
   ```python
   verbose: bool = typer.Option(False, "--verbose", "-v", help="Print parse warnings to stderr")
   ```
   When `--verbose` is set, add a `StreamHandler(sys.stderr)` instead of the `FileHandler`.

3. **Record timed-out files in the graph** so `analyze failures` can query them. In `indexer.py`, when a file times out, upsert a `File` node with `parse_failed=true` and `parse_cause="timeout"`. This makes the `analyze failures --cause timeout` query work. This is the substantive fix — the log routing is secondary.

4. Print a summary line: `"Parse warnings written to ~/.sqlcg/index.log — use --verbose to show here."` only when warnings > 0 and `--verbose` is not set.

**Wiring verification**:

- `grep -n "log_path\|FileHandler\|index.log" src/sqlcg/cli/commands/index.py` must show the `FileHandler` setup.
- `grep -n "log_path" src/sqlcg/core/config.py` must show `log_path` field in `KuzuConfig`.
- `grep -n "hardcoded.*log\|index\.log" src/sqlcg/core/config.py` — log path must come from `KuzuConfig`, not be hardcoded in `index.py`.
- `grep -n "timeout.*parse_failed\|parse_cause.*timeout" src/sqlcg/indexer/indexer.py` must show the timeout node upsert.

**Files affected**:
- [`src/sqlcg/core/config.py`](../src/sqlcg/core/config.py) — add `log_path` to `KuzuConfig`
- [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py) — add `FileHandler`, add `--verbose` flag
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — upsert timeout files with `parse_failed=true, parse_cause="timeout"`

**Tests to add**:

- **Scenario A — timed-out file appears in `analyze failures --cause timeout`**: in an integration test, index a fixture file that always times out (stub the timeout); assert that `analyze failures --cause timeout` returns a non-empty result containing that file's path.
- **Scenario B — log file is written to `KuzuConfig.log_path`, not a hardcoded path**: call `index_cmd` with a patched `KuzuConfig.from_env().log_path` pointing to a tmp dir; assert the log file appears at the configured path, not at `~/.sqlcg/index.log`.

**Acceptance criteria**:
- `[ ]` After `sqlcg index <path>`, timed-out files appear in `sqlcg analyze failures --cause timeout`
- `[ ]` Parse warning logs are written to `KuzuConfig.from_env().log_path` (default `~/.sqlcg/index.log`), not to stdout or stderr
- `[ ]` `grep -n "hardcoded\|index\.log" src/sqlcg/cli/commands/index.py` returns zero hardcoded paths — path comes from `KuzuConfig`
- `[ ]` No TODO in the `FileHandler` setup path

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

In `analyze.py`, apply `NoiseFilter.from_config()` to the `results` list before printing for `upstream` and `downstream`. Add a `--raw` flag to opt out:

```python
raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results")

# After the query:
if not raw:
    from sqlcg.server.noise_filter import NoiseFilter
    nf = NoiseFilter.from_config()
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
| What calls this? | `mcp_start()` in `mcp.py` calls `server_main()` which is `main()` in `server.py` | `install_cmd()` called by Typer CLI entry point | `reindex_cmd()` calls `indexer.resync_changed(path, ...)` which calls `git_name_status_delta(root, ...)` | `find_table()` called by `sqlcg find table <name>` | `_extract_column_lineage()` called from `parse_file()` in every parser subclass | `upstream()` / `downstream()` in `analyze.py`; `trace_column_lineage()` in `tools.py` | `index_cmd()` → `_run_index()` → `Indexer.index_repo()` | `upstream()` / `downstream()` in `analyze.py`; `find_table()` in `find.py` |
| Where is the callback/parameter passed? | `mcp.run()` replaced by `anyio.run(_run_stdio_async_with_real_stdout)`; `_real_stdout_buffer` captured at module level before `sys.stdout` reassignment | `claude mcp add -s user <name> <cmd> <args>` via `subprocess.run`; fallback writes `~/.claude.json` | `path = path.resolve()` at top of `reindex_cmd`; `root = root.resolve()` at top of `git_name_status_delta`; defensive resolve in `load_ignore_spec` | `name = name.lower()` added before `backend.run_read` in `find_table` | `positional_col_names` dict computed before main loop; INSERT aliasing block extended to cover aliased expressions; skip set gates the main loop | Bare-name fallback runs after empty results from primary query; hint message identifies the schema-qualification gap | `FileHandler` to `KuzuConfig.from_env().log_path` added in `index_cmd`; timeout nodes upserted in `indexer.py` | `NoiseFilter.from_config()` called on raw result list in `upstream`, `downstream`, `find_table`; `--raw` opts out |
| What constant/path does this align with? | fd 1 captured via `os.dup(1)` — no config constant, it is the OS stdout fd | `~/.claude.json` (not `settings.json`); `claude mcp add -s user` scope | `Path.resolve()` — standard library, no config constant | No constant — `.lower()` normalization matches C2 TableRef normalization in `base.py` | No config constant — INSERT column list from the AST `stmt.this.expressions` | `NodeLabel.COLUMN` and `RelType.COLUMN_LINEAGE` from `core/schema.py` | `KuzuConfig.from_env().log_path` — must use this, never `Path.home() / ".sqlcg" / "index.log"` hardcoded | `NoiseFilter.from_config()` — reads from `KuzuConfig.from_env().db_path` parent dir / cwd |
| Does any TODO remain in the happy path? | No — `_real_stdout_buffer` capture and `_run_stdio_async_with_real_stdout` must be complete | No — `claude mcp add` call and `~/.claude.json` fallback must both be complete | No — three `resolve()` calls, no deferred work | No — one `.lower()` call | No — positional skip set and extended aliasing block must be complete; no deferred edges | No — bare-name fallback is the complete fix for 1.0.2; a TODO for `default_schema` indexer support would go in `ARCHITECTURE_REVIEW.md`, not in the happy path | No — `FileHandler` setup and timeout node upsert must be complete | No — `NoiseFilter.from_config()` call and `--raw` flag must be complete |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg mcp start` produces a valid JSON-RPC response on stdout when sent an `initialize` request — confirmed by `test_mcp_stdio_smoke.py::test_mcp_start_handshake_on_stdout` passing
- `[ ]` After `sqlcg install --scope project` on a machine where `claude` is on PATH, `claude mcp list` shows `sql-code-graph`
- `[ ]` `sqlcg reindex .` from a git-tracked directory does not raise `ValueError` and completes the delta computation
- `[ ]` `sqlcg find table WTFS_VOORRAAD_WEEK` returns the same result as `sqlcg find table wtfs_voorraad_week` on an indexed graph
- `[ ]` Parsing `INSERT INTO t (c1, c2) SELECT a AS x, b AS y FROM src` produces edges `src.a → t.c1` and `src.b → t.c2` (positional attribution, not alias-based)
- `[ ]` `sqlcg analyze upstream mart.fact_t.amount` returns results (with hint) when the INSERT target was stored unqualified as `fact_t`
- `[ ]` After `sqlcg index <path>` that includes a file that times out, `sqlcg analyze failures --cause timeout` returns a non-empty result
- `[ ]` `sqlcg analyze upstream <col>` excludes backup tables matching `*_bck_*` by default; `--raw` restores them
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, and `test_bulk_upsert_invariant.py` all pass after PR-05 merges

---

## Risks and Mitigations

| Risk | Ticket | Mitigation |
|------|--------|------------|
| `_run_stdio_async_with_real_stdout` accesses FastMCP internals (`mcp._mcp_server`) that may change with SDK upgrades | PR-01 | Pin `mcp` SDK version in `pyproject.toml`; add SDK version to the smoke test's error message so upgrade failures are diagnosable |
| `claude mcp add` CLI interface may differ across Claude Code versions | PR-02 | Shell out with `subprocess.run(check=False)` and always fall back to `~/.claude.json` write on non-zero exit; log the stderr from the failed `claude mcp add` call |
| INSERT positional block change may introduce duplicate edges if both the main loop and the positional block fire for the same column | PR-05 | The `positional_col_names` skip set in the main loop prevents double-emission; add Scenario A (alias matches positional) as a regression guard to detect duplicates |
| Bare-name fallback in PR-06 may return wrong results if two tables have the same unqualified name in different schemas | PR-06 | The hint message explicitly warns about this; the fallback only fires on empty primary results; users with schema collisions should re-index with an explicit schema alias in `.sqlcg.toml` |
| Adding `log_path` to `KuzuConfig` changes the serialized config surface — env var `SQLCG_LOG_PATH` may be expected by some users | PR-07 | Add `SQLCG_LOG_PATH` as an env-var override consistent with `SQLCG_DB_PATH` pattern |
| `*_bck_*` glob may over-match tables with `_bck_` in the middle of a non-backup name (e.g. `abck_tracker`) | PR-08 | `fnmatch("abck_tracker", "*_bck_*")` returns `False` — `_bck_` must have a preceding `_` char; verify this in the test suite |
