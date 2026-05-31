# Sprint Plan: sprint_12_v1.1.0 — Live Graph Freshness & Daemon Reindex

**Plan date**: 2026-05-31
**Author**: sprint-planner
**Source authority**: [`plan/v1.1.0_live_graph_freshness.md`](plan/v1.1.0_live_graph_freshness.md) (architect-planner, 2026-05-31)
**Issues**: [#28](https://github.com/Warhorze/sql-code-graph/issues/28) (silent stale graph),
[#29](https://github.com/Warhorze/sql-code-graph/issues/29) (MCP lifecycle + reindex through server),
[#30](https://github.com/Warhorze/sql-code-graph/issues/30) (freshness / staleness signal)
**Policy**: No TODO in any happy path. Every new method needs a grep-confirmed call site before PR opens.
Tests assert observable output. Paths derive from `get_db_path()` / `KuzuConfig`, never hardcoded.

---

## Summary

The MCP server holds the KuzuDB single-writer lock for its entire lifetime. Any second
process that calls `index`/`reindex`/`db reset` fails with "Database is locked". The git
hooks use `|| true`, so this failure is swallowed and the graph goes stale silently — the
worst failure mode for a correctness tool.

This sprint ships the full mitigation in four PRs:

- **PR-A** (low risk): expose the existing persisted SHA as a human-readable freshness line
  in `db info` and the MCP `db_info` tool. Immediately answers #30 and makes staleness *detectable*.
- **PR-B** (low/med, independent): `index --include-working-tree` for users who want to
  index uncommitted edits. Tracks a working-tree marker in the stored SHA so `db info` can report it.
- **PR-C** (med/high): Unix-socket control channel + `.pid` file + `mcp status`/`stop`/`restart`.
  The infrastructure that PR-D depends on.
- **PR-D** (high): reindex op on the running server (`anyio.to_thread.run_sync`, serialised lock)
  + `reindex --notify` fallback + git-hook stderr cue. Closes #28.

---

## Scope

### In Scope

- `src/sqlcg/core/freshness.py` — shared, pure, testable freshness helper
- Freshness block in `sqlcg db info` (CLI) and MCP `db_info` tool (`DbInfoResult`)
- `sqlcg index --include-working-tree` + dirty marker in stored SHA
- `src/sqlcg/server/control.py` — control-file paths + `.pid` writer/reader
- Unix-socket control-socket task on the existing `anyio` event loop; `status`/`stop`/`reindex` ops
- `sqlcg mcp status` / `stop` / `restart` CLI commands
- `sqlcg reindex --notify` + fallback to direct-write path
- Git-hook update to use `--notify` + non-silent stderr cue on failure

### Non-Goals

- Windows IPC (deferred to v1.2)
- Per-file SQL-only dirty tracking (v1.1 reports whole-tree dirty)
- Socket auth token (v1.2)
- True `restart` that re-parents an editor-spawned stdio process (v1.2)
- Reader/writer lock lease inside KuzuDB (rejected — no safe API)
- Freshness on every tool result by default (gated/off in v1.1)

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| `KuzuBackend.set_indexed_sha` / `get_indexed_sha` already exist | **Confirmed.** [`kuzu_backend.py`](src/sqlcg/core/kuzu_backend.py) lines 368 / 385. Called from [`indexer.py`](src/sqlcg/indexer/indexer.py) lines 362, 519, 765 and [`reindex.py`](src/sqlcg/cli/commands/reindex.py) line 108. |
| `db info` CLI does not call `get_indexed_sha()` | **Confirmed.** [`db.py`](src/sqlcg/cli/commands/db.py) lines 73–148: prints schema version + node counts only; no SHA / freshness line. |
| MCP `db_info` tool and `DbInfoResult` already exist | **Confirmed.** [`tools.py`](src/sqlcg/server/tools.py) line 1321; [`models.py`](src/sqlcg/server/models.py) line 178. Neither carries `indexed_sha`, `head_sha`, `stale_by_commits`, or `dirty`. |
| `core/freshness.py` does not exist | **Confirmed.** `ls src/sqlcg/core/` shows no `freshness.py`. No grep hits for `compute_freshness`, `Freshness`, `stale_by_commits`, or `head_sha` in `src/`. |
| Duplicate private `_get_head` helpers in `reindex.py` and `watcher.py` | **Confirmed.** `_get_head` at [`reindex.py`](src/sqlcg/cli/commands/reindex.py) line 153; `_get_current_head_sha` at [`watcher.py`](src/sqlcg/indexer/watcher.py) line 186. Both duplicate `git rev-parse HEAD`. The new shared helper consolidates them. |
| No socket / `.pid` / `notify` / control-channel code anywhere in `src/` | **Confirmed.** `grep` for `notify|pidfile|\.pid|\.sock|unix_listener|control` in `src/` returns zero hits (excluding the unrelated `config.py` prose comment). `mcp.py` has only `setup`, `start`, `best-practices`. |
| `resync_changed` signature verified | **Confirmed.** [`indexer.py`](src/sqlcg/indexer/indexer.py) line 428: `resync_changed(self, root, old_sha, new_sha, db, dialect, *, batch_size, timeout_per_file, max_closure_depth)`. Called from [`reindex.py`](src/sqlcg/cli/commands/reindex.py) lines 97 and 127. |
| `include-working-tree` flag does not exist | **Confirmed.** No hits for `include.working.tree` or `working_tree` in `src/`. |
| `Repo` node path is accessible via `r.path` | **Confirmed.** `queries.cypher` `LIST_DIALECTS_AND_REPOS` query: `MATCH (r:Repo) … RETURN r.path AS path`. |
| `server.main` holds the backend for the process lifetime; no second thread today | **Confirmed.** [`server.py`](src/sqlcg/server/server.py) lines 95–100: `init_backend()` before `anyio.run`; `shutdown_backend()` in `finally`. Single-threaded `anyio` loop with no existing task spawning. |
| `KuzuConfig.db_path` default is `Path.home() / ".sqlcg" / "graph.db"` | **Confirmed.** [`config.py`](src/sqlcg/core/config.py) line 17. `get_db_path()` at line 65 calls `KuzuConfig.from_env().db_path`. Socket path MUST be `get_db_path().with_suffix(".sock")`; pid path MUST be `get_db_path().with_suffix(".pid")`. |
| Git hooks use `|| true` (silent failure on lock) | **Confirmed.** [`git.py`](src/sqlcg/cli/commands/git.py) lines 29 / 40–41. No `--notify`, no stderr cue. |
| `Neo4jBackend.get_indexed_sha` raises `NotImplementedError` | **Confirmed.** [`neo4j_backend.py`](src/sqlcg/core/neo4j_backend.py) lines 194–199. Freshness code must guard this. |

---

## Ticket Table

| ID | Title | Files | Effort | Priority | Depends on | Blocks |
|----|-------|-------|--------|----------|------------|--------|
| PR-A | Freshness helper + `db info` + `db_info` tool | `core/freshness.py` (new), `cli/commands/db.py`, `server/models.py`, `server/tools.py` | S | HIGH | INDEPENDENT | PR-C |
| PR-B | `index --include-working-tree` | `cli/commands/index.py` | XS | MED | INDEPENDENT | NONE |
| PR-C | Unix-socket control channel + `mcp status`/`stop`/`restart` | `server/control.py` (new), `server/server.py`, `cli/commands/mcp.py` | L | HIGH | PR-A | PR-D |
| PR-D | Reindex op on server + `reindex --notify` + git-hook cue | `server/control.py`, `server/server.py`, `cli/commands/reindex.py`, `cli/commands/git.py` | L | HIGH | PR-C | NONE |

---

## Recommended Implementation Order

### Why this order

**PR-A ships first** because it is pure reporting on already-persisted data: no new state, no
concurrency, no IPC. It answers #30 on its own and makes #28's staleness detectable without
any architectural risk. It also supplies the `Freshness` dataclass and `compute_freshness`
helper that PR-C exposes via the `status` op, so PR-C's `status` response can include
`head_sha`/`stale_by_commits` without duplicating git-calling logic.

**PR-B is independent** and can land any time after PR-A. It touches only
[`index.py`](src/sqlcg/cli/commands/index.py) and the stored SHA sentinel, with no concurrency
concern. It is lower priority because PR-A already shows the dirty state; PR-B only lets
users *index* the dirty tree. It is placed after PR-A so the dirty marker format is already
defined by PR-A's `Freshness` design when PR-B writes `<head>+dirty` into the stored SHA.

**PR-C comes second** (or in parallel with PR-B on a two-developer team). It is the
prerequisite for PR-D: without the socket, `--notify` has nowhere to send the reindex op.
PR-C's `status` op references `Freshness` from PR-A — if PR-A is not merged, PR-C must
stub that field, which is wasteful. PR-C ships after PR-A is green.

**PR-D is last** because it wires the reindex op into the socket (needs PR-C) and modifies
the `reindex` CLI (needs the socket to be provably working e2e before adding the notify
path). PR-D is the highest-risk change (thread handoff, serialising lock, fallback logic)
and must have the smallest possible diff surface when it lands — which is achieved by
merging PR-A and PR-C first.

**Risk ordering** justifies keeping PR-B and PR-C separate even though both could
theoretically land in one sprint week. PR-C changes `server.py` (the MCP entry point);
a bug there kills all tool calls. Keeping it as a standalone PR means the reviewer's
entire attention is on the socket plumbing, not split with the working-tree flag.

### Single-developer sequence

1. PR-A — freshness helper + CLI + MCP tool
2. PR-B — `--include-working-tree` (small, independent; best done while PR-A is in review
   to avoid blocking)
3. PR-C — control socket + lifecycle
4. PR-D — reindex op + `--notify` + git-hook update

### Two-developer sequence

- **Track A**: PR-A → PR-C → PR-D (critical path, one developer)
- **Track B**: PR-B (independent, second developer; starts any time after PR-A merges)

---

## Ticket Specifications

---

### PR-A — Freshness helper + `db info` CLI + `db_info` MCP tool

**Source**: `plan/v1.1.0_live_graph_freshness.md` Phase 1 (LOW RISK / HIGH REWARD)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: PR-C (supplies `Freshness` type used by `status` op)

**Root cause**: `KuzuBackend.get_indexed_sha()` persists the indexed commit SHA but nothing
ever reads it for display. [`db.py`](src/sqlcg/cli/commands/db.py) line 73 never calls
`get_indexed_sha()`. [`tools.py`](src/sqlcg/server/tools.py) line 1383 constructs
`DbInfoResult` with no SHA or freshness fields. Two private helper functions
(`_get_head` in `reindex.py` line 153; `_get_current_head_sha` in `watcher.py` line 186)
duplicate `git rev-parse HEAD` without being shareable. The gap is a missing shared module
that computes the indexed-vs-HEAD delta and a missing wiring call at both display sites.

**What to do**:

1. Create `src/sqlcg/core/freshness.py` with:

   ```python
   from __future__ import annotations
   import subprocess
   from dataclasses import dataclass
   from pathlib import Path


   @dataclass(frozen=True)
   class Freshness:
       indexed_sha: str | None
       head_sha: str | None
       stale_by_commits: int | None  # commits HEAD is ahead of indexed_sha
       dirty: bool                   # working tree has uncommitted changes
       branch: str | None            # current branch name for human cue


   def _git(root: Path, *args: str) -> str | None:
       """Run a git command; return stdout stripped, or None on any failure."""
       try:
           r = subprocess.run(
               ["git", *args], cwd=str(root),
               capture_output=True, text=True,
           )
           return r.stdout.strip() if r.returncode == 0 else None
       except Exception:
           return None


   def compute_freshness(root: Path, indexed_sha: str | None) -> Freshness:
       """Compute freshness relative to the git repo at root.

       Returns a Freshness with all-None/False fields when root is not a git repo
       or git is unavailable — never raises.
       """
       head = _git(root, "rev-parse", "HEAD")
       branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
       dirty = bool(_git(root, "status", "--porcelain"))

       stale: int | None = None
       if head and indexed_sha and indexed_sha == head:
           stale = 0
       elif head and indexed_sha:
           raw = _git(root, "rev-list", "--count", f"{indexed_sha}..{head}")
           if raw is not None:
               try:
                   stale = int(raw)
               except ValueError:
                   stale = None
           # If indexed_sha is unknown (rebased/shallow), raw is None → stale stays None

       return Freshness(
           indexed_sha=indexed_sha,
           head_sha=head,
           stale_by_commits=stale,
           dirty=dirty,
           branch=branch,
       )


   def render_freshness_line(f: Freshness) -> str:
       """Return a one-line human summary, e.g.:
       'indexed at abc1234 (2 commits behind HEAD, working tree dirty)'
       """
       if f.indexed_sha is None:
           return "freshness: not available (graph was never indexed from a git repo)"

       parts: list[str] = []
       if f.stale_by_commits is None:
           parts.append("commit distance unknown (SHA not in history)")
       elif f.stale_by_commits == 0:
           parts.append("up to date")
       else:
           parts.append(f"{f.stale_by_commits} commit(s) behind HEAD")
       if f.dirty:
           parts.append("working tree dirty")

       summary = ", ".join(parts)
       sha8 = f.indexed_sha[:8]
       return f"indexed at {sha8} ({summary})"
   ```

2. Wire into `sqlcg db info` ([`db.py`](src/sqlcg/cli/commands/db.py), after the schema
   version print):

   ```python
   # Add import at top of db.py
   from sqlcg.core.freshness import compute_freshness, render_freshness_line

   # Inside db_info(), after printing schema version, before node counts loop:
   indexed_sha = backend.get_indexed_sha()
   repo_rows = backend.run_read("MATCH (r:Repo) RETURN r.path AS path LIMIT 1", {})
   if repo_rows and indexed_sha is not None:
       repo_root = Path(repo_rows[0]["path"])
       f = compute_freshness(repo_root, indexed_sha)
       console.print(render_freshness_line(f))
   ```
   When the DB is empty (`repo_rows` is empty) or `indexed_sha` is None, print nothing —
   the existing empty-DB warning already covers that case.

3. Extend [`DbInfoResult`](src/sqlcg/server/models.py) with optional freshness fields:

   ```python
   indexed_sha: str | None = Field(None, description="Git SHA of the last index run")
   head_sha: str | None = Field(None, description="Current HEAD SHA of the indexed repo")
   stale_by_commits: int | None = Field(None, description="Commits HEAD is ahead of indexed_sha")
   dirty: bool = Field(False, description="True if working tree has uncommitted changes")
   ```

4. Wire freshness into the MCP `db_info` tool ([`tools.py`](src/sqlcg/server/tools.py),
   inside the `db_info()` function, after `schema_version` is obtained):

   ```python
   from sqlcg.core.freshness import compute_freshness

   # Compute freshness from stored SHA + first Repo node
   _freshness_kwargs: dict = {}
   try:
       _indexed_sha = db.get_indexed_sha()
       _repo_rows = db.run_read("MATCH (r:Repo) RETURN r.path AS path LIMIT 1", {})
       if _repo_rows and _indexed_sha is not None:
           _root = Path(_repo_rows[0]["path"])
           _f = compute_freshness(_root, _indexed_sha)
           _freshness_kwargs = {
               "indexed_sha": _f.indexed_sha,
               "head_sha": _f.head_sha,
               "stale_by_commits": _f.stale_by_commits,
               "dirty": _f.dirty,
           }
       elif _indexed_sha is not None:
           _freshness_kwargs = {"indexed_sha": _indexed_sha}
   except NotImplementedError:
       # Neo4j backend raises NotImplementedError for get_indexed_sha — report null
       pass

   return DbInfoResult(
       schema_version=schema_version,
       node_counts=node_counts,
       column_lineage_edges=column_lineage_edges,
       parse_quality=parse_quality,
       warnings=warnings,
       **_freshness_kwargs,
   )
   ```

   The `NotImplementedError` catch is the only guard needed; `compute_freshness` itself
   never raises.

**Wiring verification**:

Before opening the PR, run:

- `grep -n "get_indexed_sha\|compute_freshness\|render_freshness_line" src/sqlcg/cli/commands/db.py`
  must show at least one hit for each name — confirms the CLI wiring is present.
- `grep -n "compute_freshness\|get_indexed_sha\|indexed_sha\|stale_by_commits" src/sqlcg/server/tools.py`
  must show hits for all four names inside `db_info` — confirms MCP wiring.
- `grep -n "indexed_sha\|head_sha\|stale_by_commits\|dirty" src/sqlcg/server/models.py`
  must show the four fields on `DbInfoResult`.
- `grep -rn "TODO" src/sqlcg/core/freshness.py` must return zero results.
- Confirm `_git` is called via `compute_freshness` (never from the display layer directly):
  `grep -n "_git\b" src/sqlcg/cli/commands/db.py src/sqlcg/server/tools.py` must return
  zero results.

**Files affected**:
- `src/sqlcg/core/freshness.py` — new module (Freshness dataclass, compute_freshness, render_freshness_line, _git)
- `src/sqlcg/cli/commands/db.py` — call get_indexed_sha + compute_freshness + render_freshness_line in db_info()
- `src/sqlcg/server/models.py` — add four optional fields to DbInfoResult
- `src/sqlcg/server/tools.py` — wire compute_freshness into db_info(); handle NotImplementedError for Neo4j

**Tests to add**:

- **Scenario A — stale detection**: build a temp git repo with 2 commits; call `set_indexed_sha(sha_of_commit_1)` on an in-memory KuzuDB; call `compute_freshness(root, sha_of_commit_1)`; assert `stale_by_commits == 1` and `dirty is False`.
- **Scenario B — dirty detection**: extend scenario A; touch a tracked file without committing; call `compute_freshness`; assert `dirty is True`.
- **Scenario C — unknown SHA**: call `compute_freshness(root, "deadbeef" * 5)` (non-existent SHA); assert `stale_by_commits is None` (not 0).
- **Scenario D — non-git directory**: call `compute_freshness(Path("/tmp"), None)`; assert returns `Freshness(indexed_sha=None, head_sha=None, stale_by_commits=None, dirty=False, branch=None)` with no exception.
- **Scenario E — db_info MCP integration**: real in-memory Kuzu; index a fixture; advance HEAD one commit; call `db_info()`; assert `result.stale_by_commits == 1`.
- **Scenario F — Neo4j guard**: mock backend where `get_indexed_sha()` raises `NotImplementedError`; call `db_info()`; assert `result.indexed_sha is None` and no exception propagates.
- **Scenario G — db info CLI empty DB**: CLI `db info` on an empty DB must not crash; assert the freshness line is absent from output.

**Acceptance criteria**:
- `[ ]` `sqlcg db info` on an indexed-then-advanced repo prints `indexed at <sha8> (N commit(s) behind HEAD)` — N is verifiable by running `git rev-list --count <sha>..HEAD`
- `[ ]` `sqlcg db info` on an empty DB prints no freshness line and does not crash
- `[ ]` `db_info()` MCP tool returns `stale_by_commits >= 1` after indexing and advancing HEAD
- `[ ]` `db_info()` MCP tool returns `indexed_sha: null` and no exception for Neo4j backend
- `[ ]` `grep -n "compute_freshness" src/sqlcg/cli/commands/db.py` — at least one hit
- `[ ]` `grep -n "stale_by_commits" src/sqlcg/server/models.py` — hit on DbInfoResult
- `[ ]` `grep -rn "TODO" src/sqlcg/core/freshness.py` — zero results

---

### PR-B — `index --include-working-tree`

**Source**: `plan/v1.1.0_live_graph_freshness.md` Phase 2 (LOW–MEDIUM / OPTIONAL)
**Effort**: XS
**Depends on**: INDEPENDENT (soft-depends on PR-A for the dirty-marker format)
**Blocks**: NONE

**Root cause**: The `index_cmd` function in [`index.py`](src/sqlcg/cli/commands/index.py)
already walks the working tree for SQL files, but the stored `indexed_sha` is always the
clean HEAD SHA (written by [`indexer.py`](src/sqlcg/indexer/indexer.py) line 362 from
`git rev-parse HEAD`). There is no flag to record that the index included uncommitted edits,
so `db info` cannot distinguish "indexed HEAD" from "indexed HEAD + dirty working tree".

**What to do**:

1. Add flag to `index_cmd` in [`index.py`](src/sqlcg/cli/commands/index.py):

   ```python
   include_working_tree: bool = typer.Option(
       False,
       "--include-working-tree",
       help="Index the working tree including uncommitted changes. "
            "Marks freshness as 'indexed with working-tree changes'.",
   )
   ```

2. After indexing completes (when the backend has written the HEAD SHA), if
   `include_working_tree` is True and the working tree is dirty, overwrite the stored SHA
   with a dirty sentinel:

   ```python
   if include_working_tree:
       from sqlcg.core.freshness import _git
       from sqlcg.core.config import get_db_path
       dirty_out = _git(path, "status", "--porcelain")
       if dirty_out:  # non-empty means dirty
           head = _git(path, "rev-parse", "HEAD") or "unknown"
           with get_backend() as _b2:
               _b2.set_indexed_sha(f"{head}+dirty")
   ```
   Note: the backend context manager in `index_cmd` is already closed at this point;
   open a new one for the sentinel write. The `+dirty` suffix is intentionally not a valid
   SHA so `git rev-list --count` returns `None` (treated as "unknown commit distance"),
   and `render_freshness_line` will print "commit distance unknown (SHA not in history)"
   alongside the dirty signal — which is accurate.

3. No changes to the indexer's walk logic — the existing tree walk already reads from disk,
   so uncommitted edits are inherently included. The flag only controls whether the sentinel
   SHA is written; without the flag, `set_indexed_sha` writes the clean HEAD as today.

**Wiring verification**:

- `grep -n "include.working.tree\|include_working_tree" src/sqlcg/cli/commands/index.py`
  must show the flag definition and the conditional sentinel write.
- `grep -n "\+dirty\|dirty.*sentinel" src/sqlcg/cli/commands/index.py` must show the
  sentinel write site.
- `grep -rn "TODO" src/sqlcg/cli/commands/index.py` — zero new TODOs introduced by this PR.

**Files affected**:
- `src/sqlcg/cli/commands/index.py` — add `--include-working-tree` flag + dirty sentinel write

**Tests to add**:

- **Scenario A — sentinel written**: temp git repo with uncommitted edit; run `index --include-working-tree`; assert `backend.get_indexed_sha()` ends with `+dirty`.
- **Scenario B — clean tree ignores flag**: temp git repo, clean working tree; run `index --include-working-tree`; assert `backend.get_indexed_sha()` is the plain HEAD SHA (no `+dirty`).
- **Scenario C — flag absent**: temp git repo with uncommitted edit; run plain `index` (no flag); assert `backend.get_indexed_sha()` is the plain HEAD SHA (unchanged behaviour).

**Acceptance criteria**:
- `[ ]` With an uncommitted SQL edit, `sqlcg index --include-working-tree` followed by `db info` shows a freshness line containing "commit distance unknown" and "working tree dirty"
- `[ ]` Without the flag, behaviour is identical to pre-PR-B (stored SHA is the clean HEAD)
- `[ ]` `grep -n "include_working_tree" src/sqlcg/cli/commands/index.py` — at least two hits (definition + use)
- `[ ]` Existing `index` integration tests still pass

---

### PR-C — Unix-socket control channel + `mcp status`/`stop`/`restart`

**Source**: `plan/v1.1.0_live_graph_freshness.md` Phase 3 (MEDIUM–HIGH RISK / HIGH REWARD)
**Effort**: L
**Depends on**: PR-A (for `Freshness` type in `status` response)
**Blocks**: PR-D

**Root cause**: The MCP server ([`server.py`](src/sqlcg/server/server.py)) has no
inter-process control surface. `mcp.py` has only `setup`, `start`, `best-practices`. There
is no `.pid` file, no Unix socket, no `anyio` control task, and no `mcp status`/`stop`
commands. This makes it impossible to discover whether a server is running on a given DB,
to stop it gracefully, or to ask it to apply a reindex delta (PR-D's requirement).

**What to do**:

1. Create `src/sqlcg/server/control.py` with path helpers and `.pid` file management.
   All paths derive from `get_db_path()` so two servers on two DBs do not collide:

   ```python
   from __future__ import annotations
   import json
   import os
   import time
   from pathlib import Path
   from sqlcg.core.config import get_db_path


   def sock_path(db_path: Path | None = None) -> Path:
       p = db_path or get_db_path()
       return p.with_suffix(".sock")


   def pid_path(db_path: Path | None = None) -> Path:
       p = db_path or get_db_path()
       return p.with_suffix(".pid")


   def write_pid(db_path: Path | None = None) -> None:
       """Write a JSON PID record: {pid, db_path, started_at}."""
       pp = pid_path(db_path)
       pp.write_text(json.dumps({
           "pid": os.getpid(),
           "db_path": str(db_path or get_db_path()),
           "started_at": time.time(),
       }))
       pp.chmod(0o600)


   def read_pid(db_path: Path | None = None) -> dict | None:
       """Return the PID record dict, or None if the file is missing/corrupt."""
       pp = pid_path(db_path)
       try:
           return json.loads(pp.read_text())
       except Exception:
           return None


   def cleanup_control_files(db_path: Path | None = None) -> None:
       """Remove .sock and .pid files silently (used in server finally)."""
       for p in (sock_path(db_path), pid_path(db_path)):
           try:
               p.unlink()
           except FileNotFoundError:
               pass


   def is_pid_alive(pid: int) -> bool:
       """Return True if a process with pid exists (signal 0 check)."""
       try:
           os.kill(pid, 0)
           return True
       except (ProcessLookupError, PermissionError):
           return False
   ```

2. Add the control-socket async task to [`server.py`](src/sqlcg/server/server.py). The task
   is spawned inside the same `anyio` task group as the stdio MCP loop. The `reindex` op
   is a stub in this PR (returns `{"error": "not implemented"}`) — it is fully wired in PR-D.
   The full reindex op body is added in PR-D; the stub ensures PR-C ships no TODO in the
   happy path of `status`/`stop`:

   ```python
   import anyio
   from sqlcg.server.control import sock_path, write_pid, cleanup_control_files

   async def _control_socket_task(
       db_path,
       backend_ref,         # callable that returns the current _backend
       reindex_lock,        # anyio.Lock — serialises reindex ops; PR-D populates this
       start_time: float,
   ) -> None:
       """Accept control connections on <db>.sock and dispatch ops."""
       import json, time
       sp = sock_path(db_path)
       listener = await anyio.create_unix_listener(str(sp))
       sp.chmod(0o600)
       async with listener:
           async for stream in listener:
               async with stream:
                   try:
                       raw = await stream.receive(4096)
                       req = json.loads(raw)
                       op = req.get("op")
                       if op == "status":
                           from sqlcg.core.freshness import compute_freshness
                           db = backend_ref()
                           indexed_sha = db.get_indexed_sha() if db else None
                           # repo root from Repo node
                           head_sha = None
                           stale = None
                           if db and indexed_sha:
                               rows = db.run_read(
                                   "MATCH (r:Repo) RETURN r.path AS path LIMIT 1", {}
                               )
                               if rows:
                                   from pathlib import Path as _Path
                                   f = compute_freshness(_Path(rows[0]["path"]), indexed_sha)
                                   head_sha = f.head_sha
                                   stale = f.stale_by_commits
                           resp = {
                               "running": True,
                               "pid": os.getpid(),
                               "db_path": str(db_path or get_db_path()),
                               "indexed_sha": indexed_sha,
                               "head_sha": head_sha,
                               "stale_by_commits": stale,
                               "connected_clients": 1,   # stdio = 1 by transport
                               "uptime": time.time() - start_time,
                           }
                       elif op == "stop":
                           resp = {"ok": True}
                           await stream.send(json.dumps(resp).encode() + b"\n")
                           # Signal the server to exit cleanly
                           import signal
                           os.kill(os.getpid(), signal.SIGTERM)
                           return
                       elif op == "reindex":
                           # Implemented in PR-D; stub returns error here so no TODO exists
                           resp = {"error": "reindex op not yet wired — upgrade to PR-D"}
                       else:
                           resp = {"error": f"unknown op: {op}"}
                       await stream.send(json.dumps(resp).encode() + b"\n")
                   except Exception as exc:
                       try:
                           await stream.send(
                               json.dumps({"error": str(exc)}).encode() + b"\n"
                           )
                       except Exception:
                           pass
   ```

   In `server.main`, wrap the existing `anyio.run` call to spawn the control task alongside
   the stdio loop:

   ```python
   import os, time
   from sqlcg.server.control import write_pid, cleanup_control_files

   def main(db_path: str | None = None) -> None:
       ...
       _configure_mcp_logging()
       load_dotenv()
       import sqlcg.server.tools
       sqlcg.server.tools.init_backend(db_path)
       _start_time = time.time()
       _db_path_obj = Path(db_path) if db_path else get_db_path()
       write_pid(_db_path_obj)
       try:
           anyio.run(_run_with_control, _db_path_obj, _start_time)
       finally:
           sqlcg.server.tools.shutdown_backend()
           cleanup_control_files(_db_path_obj)
   ```

   The `_run_with_control` coroutine opens a `TaskGroup` and spawns both the stdio loop and
   the control socket task. Add SIGTERM handling so `stop` causes a clean exit.

   Platform guard: wrap the socket listener creation in
   `if sys.platform != "win32": ...` so the import is safe on Windows. On Windows, skip
   `write_pid`/`cleanup_control_files` for the socket; still write `.pid`.

3. Add `mcp status`, `mcp stop`, `mcp restart` commands to [`mcp.py`](src/sqlcg/cli/commands/mcp.py):

   ```python
   @app.command("status")
   def mcp_status() -> None:
       """Print server status JSON (connects to control socket)."""
       import json, socket as _socket
       from sqlcg.server.control import sock_path, pid_path, read_pid, is_pid_alive
       sp = sock_path()
       try:
           with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
               s.settimeout(2)
               s.connect(str(sp))
               s.sendall(json.dumps({"op": "status"}).encode() + b"\n")
               data = s.recv(4096)
           console.print_json(data.decode())
       except (FileNotFoundError, ConnectionRefusedError, OSError):
           # Socket unavailable — try PID file
           rec = read_pid()
           if rec and is_pid_alive(rec["pid"]):
               console.print_json(json.dumps({
                   "running": True, "degraded": "socket unavailable",
                   "pid": rec["pid"], "db_path": rec["db_path"],
               }))
           else:
               console.print_json(json.dumps({"running": False}))

   @app.command("stop")
   def mcp_stop() -> None:
       """Stop the running MCP server gracefully."""
       import json, socket as _socket, time
       from sqlcg.server.control import sock_path, pid_path, read_pid, is_pid_alive
       sp = sock_path()
       try:
           with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
               s.settimeout(2)
               s.connect(str(sp))
               s.sendall(json.dumps({"op": "stop"}).encode() + b"\n")
               s.recv(128)
           # Wait up to 5 s for socket to disappear
           for _ in range(10):
               if not sp.exists():
                   break
               time.sleep(0.5)
           console.print("[green]Server stopped.[/green]")
       except (FileNotFoundError, ConnectionRefusedError, OSError):
           # Fall back to SIGTERM on PID file
           rec = read_pid()
           if rec and is_pid_alive(rec["pid"]):
               import signal, os
               os.kill(rec["pid"], signal.SIGTERM)
               console.print(f"[yellow]Socket unavailable — sent SIGTERM to PID {rec['pid']}[/yellow]")
           else:
               console.print("[yellow]No server found to stop.[/yellow]")

   @app.command("restart")
   def mcp_restart() -> None:
       """Stop the server. The client (editor) must respawn.

       v1.1 cannot re-parent an editor-spawned stdio process.
       After stopping, the editor extension must restart the MCP server.
       """
       mcp_stop()
       console.print(
           "[yellow]Server stopped. Please restart via your editor's MCP configuration.[/yellow]"
       )
       console.print(
           "[dim]True auto-restart (re-parenting stdio) is deferred to v1.2.[/dim]"
       )
   ```

**Wiring verification**:

- `grep -n "write_pid\|cleanup_control_files" src/sqlcg/server/server.py` must show both in `main`.
- `grep -n "_control_socket_task\|anyio.create_unix_listener" src/sqlcg/server/server.py` must hit.
- `grep -n "mcp_status\|mcp_stop\|mcp_restart" src/sqlcg/cli/commands/mcp.py` must show all three.
- `grep -n "sock_path\|pid_path" src/sqlcg/server/control.py` — both must call `get_db_path()` (never hardcode `~/.sqlcg`).
- `grep -rn "TODO" src/sqlcg/server/control.py src/sqlcg/server/server.py src/sqlcg/cli/commands/mcp.py` — zero results.
- `grep -n "win32\|platform" src/sqlcg/server/server.py` — platform guard must be present for socket creation.
- `grep -n "reindex" src/sqlcg/server/server.py` — the stub must return an error dict, not a TODO comment.

**Files affected**:
- `src/sqlcg/server/control.py` — new module (paths, pid read/write, cleanup, is_pid_alive)
- `src/sqlcg/server/server.py` — write_pid, launch control task alongside stdio loop, SIGTERM handler, cleanup in finally
- `src/sqlcg/cli/commands/mcp.py` — mcp_status, mcp_stop, mcp_restart commands

**Tests to add**:

- **Scenario A — path derivation**: `SQLCG_DB_PATH=/tmp/test.db` set in env; call `sock_path()` and `pid_path()`; assert results are `/tmp/test.sock` and `/tmp/test.pid` (derived from `get_db_path()`, never hardcoded).
- **Scenario B — mcp status no server**: no socket, no pid file; `mcp_status()` prints `{"running": false}`.
- **Scenario C — mcp status degraded**: pid file present with live PID but no socket; `mcp_status()` prints JSON with `"running": true, "degraded": "socket unavailable"`.
- **Scenario D — e2e status**: start a real `sqlcg mcp start` subprocess on a temp file DB; send `{"op": "status"}` to the socket; assert response contains `running`, `pid`, `db_path`, `uptime` as non-null fields.
- **Scenario E — e2e stop**: start server; call `mcp stop`; assert the socket file and pid file are removed within 5 s and the process exits 0.
- **Scenario F — stale socket cleanup**: create a stale `.sock` file with no process behind it; call `mcp_status()`; assert it falls through to `{"running": false}` (not a crash).

**Acceptance criteria**:
- `[ ]` `sqlcg mcp status` returns JSON with `running, pid, db_path, indexed_sha, head_sha, stale_by_commits, connected_clients, uptime` when a server is live
- `[ ]` `sqlcg mcp stop` causes the socket and pid file to disappear and the server process to exit 0
- `[ ]` `sqlcg mcp restart` prints the v1.1 client-respawn caveat (no TODO in the body)
- `[ ]` With no server: `mcp status` returns `{"running": false}`; `mcp stop` prints "No server found to stop" without crashing
- `[ ]` Stale socket (server crashed): `mcp status` falls through cleanly
- `[ ]` `grep -n "sock_path\|pid_path" src/sqlcg/server/control.py` — both derive from `get_db_path()`
- `[ ]` `grep -n "win32" src/sqlcg/server/server.py` — platform guard present
- `[ ]` `grep -rn "TODO" src/sqlcg/server/control.py` — zero results

---

### PR-D — Reindex op on server + `reindex --notify` + git-hook stderr cue

**Source**: `plan/v1.1.0_live_graph_freshness.md` Phase 4 (HIGH RISK / HIGH REWARD — closes #28)
**Effort**: L
**Depends on**: PR-C (socket + lock infrastructure)
**Blocks**: NONE

**Root cause**: When `sqlcg mcp start` is running, the git hooks call
`sqlcg reindex … || true` — this opens the DB from a second process, hits the single-writer
lock, fails silently. The fix is for the reindex to route through the already-running server
(which holds the lock) via the control socket. The server must then run `resync_changed` off
the event-loop thread (R1) behind a serialising lock (R2). The hook failure must no longer
be silent (R3 mitigation).

**What to do**:

1. Add the real `reindex` op handler in [`server.py`](src/sqlcg/server/server.py)'s
   `_control_socket_task`. Replace the stub from PR-C with the real implementation.
   The `reindex_lock` is an `anyio.Lock` created once in `_run_with_control` and passed in:

   ```python
   elif op == "reindex":
       root = req.get("root")
       from_sha = req.get("from")
       to_sha = req.get("to")
       dialect = req.get("dialect")
       if not root or not from_sha or not to_sha:
           resp = {"error": "reindex op requires root, from, to"}
       else:
           async with reindex_lock:
               import anyio
               from sqlcg.indexer.indexer import Indexer
               from pathlib import Path as _Path

               db = backend_ref()
               indexer = Indexer()

               def _do_reindex() -> dict:
                   return indexer.resync_changed(
                       _Path(root), from_sha, to_sha, db, dialect
                   )

               summary = await anyio.to_thread.run_sync(_do_reindex)
               # Refresh cached freshness after reindex
               # (the backend now has the new indexed_sha)
           resp = {"ok": True, "summary": summary}
   ```

   Critical constraints verified:
   - `resync_changed` is called unchanged (not reimplemented) — the perf invariants
     in CLAUDE.md (bulk upsert, qualify-once, etc.) are automatically inherited.
   - `anyio.to_thread.run_sync` keeps it off the event-loop thread (R1).
   - `reindex_lock` serialises concurrent notify calls (R2).
   - The `reindex_lock` is created once: `reindex_lock = anyio.Lock()` in `_run_with_control`.

2. Add `--notify` flag to `reindex` command in [`reindex.py`](src/sqlcg/cli/commands/reindex.py):

   ```python
   notify: bool = typer.Option(
       False,
       "--notify",
       help="If a server is live on this DB, route the reindex through the server "
            "(avoids lock contention). Falls back to direct write if no server is found.",
   )
   ```

   At the start of the command body, before opening the backend, attempt socket routing:

   ```python
   if notify:
       import json, socket as _socket
       from sqlcg.server.control import sock_path
       sp = sock_path()
       try:
           with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
               s.settimeout(30)  # reindex can take time
               s.connect(str(sp))
               payload = {
                   "op": "reindex",
                   "root": str(path),
                   "from": from_sha,
                   "to": to_sha or _get_head(path),
                   "dialect": dialect,
               }
               s.sendall(json.dumps(payload).encode() + b"\n")
               data = s.recv(65536)
           result = json.loads(data)
           if "error" in result:
               console.print(f"[red]Server reindex error: {result['error']}[/red]", err=True)
               raise typer.Exit(1)
           if not quiet:
               s = result.get("summary", {})
               console.print(
                   f"[green]Resynced via server[/green] "
                   f"+{s.get('added',0)} added, "
                   f"~{s.get('modified',0)} modified, "
                   f"-{s.get('deleted',0)} deleted"
               )
           raise typer.Exit(0)
       except (FileNotFoundError, ConnectionRefusedError, OSError):
           # No live server — fall through to direct-write path unchanged
           pass
       except typer.Exit:
           raise
       except Exception as exc:
           console.print(f"[red]--notify routing failed: {exc}[/red]", err=True)
           raise typer.Exit(1)
   # ... existing direct-write path continues unchanged
   ```

   The `from_sha` resolution: `reindex.py` already has `from_sha` as an optional argument
   and defaults to `backend.get_indexed_sha()` in standalone mode (line 108). Pass that
   same value into the socket payload (resolve before the `if notify:` block).

3. Update the git hooks in [`git.py`](src/sqlcg/cli/commands/git.py) to pass `--notify`
   and emit a non-silent stderr cue on failure. **Both changes land in the same PR** (as
   required by the feature plan — do not ship a no-op `--notify` before the socket exists):

   ```python
   _HOOKS: list[_HookSpec] = [
       _HookSpec(
           filename="post-checkout",
           sentinel="# sqlcg post-checkout hook",
           script=(
               "#!/bin/sh\n"
               "# sqlcg post-checkout hook — incremental resync after branch switch\n"
               '[ "$3" = "1" ] || exit 0\n'
               'sqlcg reindex --from "$1" --to "$2"'
               ' "$(git rev-parse --show-toplevel)" --dialect auto --quiet --notify'
               ' || echo "sqlcg: graph not updated (server busy/locked)'
               " — run 'sqlcg mcp status'\" >&2\n"
           ),
       ),
       _HookSpec(
           filename="post-merge",
           sentinel="# sqlcg post-merge hook",
           script="""\
   #!/bin/sh
   # sqlcg post-merge hook — incremental resync after pull/merge
   sqlcg reindex "$(git rev-parse --show-toplevel)" --dialect auto --quiet --notify \\
     || echo "sqlcg: graph not updated (server busy/locked) — run 'sqlcg mcp status'" >&2
   """,
       ),
   ]
   ```

   Note the hook sentinels are unchanged so `install-hooks` remains idempotent for users
   who already have the PR-C-era hooks. The sentinel is a comment string that does not
   include the command arguments.

**Wiring verification**:

- `grep -n "anyio.to_thread.run_sync\|resync_changed\|reindex_lock" src/sqlcg/server/server.py`
  must show all three in the `reindex` op branch — confirms R1 (thread offload) and R2
  (lock) are present and the real `resync_changed` is called (not a reimplementation).
- `grep -n "notify" src/sqlcg/cli/commands/reindex.py` must show the flag definition
  and the socket-routing block.
- `grep -n "notify\|mcp status" src/sqlcg/cli/commands/git.py` must show both in the
  hook scripts.
- `grep -rn "TODO" src/sqlcg/server/server.py src/sqlcg/cli/commands/reindex.py src/sqlcg/cli/commands/git.py`
  — zero results.
- `grep -n "resync_changed" src/sqlcg/server/server.py` — exactly one call site (in the
  `_do_reindex` closure); must not be a reimplementation.
- Confirm R3 (stale socket): `grep -n "ConnectionRefusedError\|FileNotFoundError" src/sqlcg/cli/commands/reindex.py`
  — both must appear in the `--notify` fallback block.

**Files affected**:
- `src/sqlcg/server/server.py` — replace reindex stub with real op: `anyio.to_thread.run_sync`, `reindex_lock`, call `resync_changed`; create `reindex_lock = anyio.Lock()` in `_run_with_control`
- `src/sqlcg/cli/commands/reindex.py` — add `--notify` flag + socket-routing block + fallback
- `src/sqlcg/cli/commands/git.py` — update hook scripts with `--notify` + stderr cue

**Tests to add**:

- **Scenario A — reindex op calls resync_changed (invariant guard)**: start server with
  a real temp Kuzu DB; mock `Indexer.resync_changed` with a spy; send `{"op": "reindex", ...}`
  to the socket; assert the spy was called exactly once (not a reimplemented walk, bulk-upsert
  invariant inherited). Assert response has `"ok": true`.
- **Scenario B — no lock contention**: server running on temp file DB; call
  `sqlcg reindex --notify`; assert graph reflects the new commit and no "Database is locked"
  error is emitted.
- **Scenario C — fallback on no server**: no server running; call `sqlcg reindex --notify`;
  assert it falls through to the direct-write path and the graph is updated (no crash).
- **Scenario D — fallback on stale socket**: create a stale `.sock` file (no process);
  call `sqlcg reindex --notify`; assert `ConnectionRefusedError` is caught and the
  direct-write path runs successfully.
- **Scenario E — server error surfaces on stderr**: server running but sends
  `{"error": "deliberate"}` for the reindex op; assert `sqlcg reindex --notify` exits
  non-zero and prints the error to stderr (not swallowed).
- **Scenario F — hook e2e**: simulate `post-checkout` with server live; assert the graph
  is updated and exit code is 0. Simulate server down + DB lockable: assert direct write
  succeeds. Simulate server returning error: assert stderr cue is printed and exit is 0
  (hook is non-fatal to checkout).
- **Scenario G — concurrent notify serialised**: send two concurrent `reindex` ops to the
  socket; assert both complete successfully and no KuzuDB error occurs (lock serialises them).

**Acceptance criteria**:
- `[ ]` With a server live, `sqlcg reindex --notify` updates the graph without a "Database is locked" error, while the server keeps serving tool calls
- `[ ]` With no server, `sqlcg reindex --notify` falls through to the direct-write path; all existing reindex tests pass
- `[ ]` `post-checkout`/`post-merge` hooks emit a one-line stderr cue when the graph cannot be updated; checkout remains non-fatal
- `[ ]` `grep -n "anyio.to_thread.run_sync" src/sqlcg/server/server.py` — hit in the reindex op branch (R1 confirmed)
- `[ ]` `grep -n "reindex_lock" src/sqlcg/server/server.py` — lock creation and `async with` both present (R2 confirmed)
- `[ ]` `grep -n "resync_changed" src/sqlcg/server/server.py` — exactly one call (not a reimplementation)
- `[ ]` `grep -rn "TODO" src/sqlcg/server/server.py src/sqlcg/cli/commands/reindex.py` — zero results
- `[ ]` Scenario A (spy test) passes — `resync_changed` is called once per notify, inheriting the bulk-upsert invariant

---

## Test Strategy

### The single most important regression guard

This test must be present in `tests/integration/test_reindex_via_server.py` before PR-D
merges. It would have caught the "silent stale graph" regression described in #28:

```python
def test_reindex_op_calls_resync_changed_not_reimplemented(tmp_path, monkeypatch):
    """Guard: the server-side reindex op must delegate to Indexer.resync_changed.

    If this test fails, the reindex op was reimplemented instead of delegated,
    which breaks the bulk-upsert invariant (CLAUDE.md perf table) and risks
    reimplementing the walk incorrectly.

    Guards against: v1.1.0 reindex-via-server regression.
    """
    from unittest.mock import MagicMock, patch
    import json, socket as _socket

    # Start the server on a real temp DB
    # ... (setup omitted for brevity; use the e2e server fixture)

    call_log = []
    original = Indexer.resync_changed
    def spy(self, *a, **kw):
        call_log.append((a, kw))
        return original(self, *a, **kw)

    with patch.object(Indexer, "resync_changed", spy):
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.connect(str(sock_path()))
            s.sendall(json.dumps({
                "op": "reindex", "root": str(tmp_path),
                "from": "abc123", "to": "def456", "dialect": "ansi",
            }).encode() + b"\n")
            resp = json.loads(s.recv(4096))

    assert len(call_log) == 1, (
        "resync_changed must be called exactly once — "
        "guards against reimplemented walk breaking bulk-upsert invariant (v1.1.0 regression)"
    )
    assert "ok" in resp, f"Expected ok response, got: {resp}"
```

This test asserts on an observable call-count (1, not 0, not 2) and the response shape.
It does not use `xfail` — it will fail if PR-D has not merged yet; that is correct.

---

## Wiring Checklist

| Question | PR-A | PR-B | PR-C | PR-D |
|----------|------|------|------|------|
| What calls this? | `db_info()` CLI and MCP tool call `compute_freshness`; `render_freshness_line` called from `db.py` | `index_cmd()` calls `_git()` via `compute_freshness` to detect dirty; writes `set_indexed_sha(head+dirty)` | `server.main()` calls `write_pid` and `cleanup_control_files`; `_run_with_control` spawns `_control_socket_task` | `_control_socket_task` calls `anyio.to_thread.run_sync(_do_reindex)`; `_do_reindex` calls `Indexer.resync_changed` |
| Where is the callback/parameter passed? | `compute_freshness(root, indexed_sha)` — `root` from `MATCH (r:Repo) RETURN r.path`; `indexed_sha` from `backend.get_indexed_sha()` | `include_working_tree` flag passed from CLI argument; dirty sentinel write uses `backend.set_indexed_sha` | `reindex_lock` created in `_run_with_control`, passed into `_control_socket_task`; `db_path_obj` derived from `get_db_path()` | `reindex_lock` from `_run_with_control`; `backend_ref` is `lambda: _backend`; `notify` flag in reindex CLI triggers socket routing |
| What constant/path does this align with? | No new constants. Calls `get_db_path()` for MCP `db_info` via backend singleton | `+dirty` sentinel is string appended to HEAD SHA; format defined here; PR-B must not hardcode `~/.sqlcg` | `sock_path()` and `pid_path()` both call `get_db_path()` from `KuzuConfig.from_env()` — never hardcoded | `sock_path()` from `server/control.py` — same `get_db_path()` chain; reindex CLI resolves socket path via `sock_path()` import |
| Does any TODO remain in the happy path? | No — `compute_freshness` and `render_freshness_line` are fully implemented | No — the sentinel write is a complete implementation | No — `status` and `stop` are fully implemented; the reindex stub returns an error dict (not a TODO comment) | No — the real `resync_changed` is called; no stub, no TODO |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg db info` on a repo indexed at commit A, with HEAD at commit B (2 commits ahead) prints `indexed at <sha8> (2 commit(s) behind HEAD)` — N is verifiable by running `git rev-list --count <sha>..HEAD`
- `[ ]` `sqlcg db info` on an empty database prints no freshness line and does not crash
- `[ ]` MCP `db_info` result JSON carries non-null `indexed_sha`, `head_sha`, `stale_by_commits` after indexing a git repo and advancing HEAD; Neo4j backend returns `indexed_sha: null` without raising
- `[ ]` `sqlcg index --include-working-tree` on a repo with an uncommitted SQL edit causes `db info` to show "commit distance unknown" and "working tree dirty"
- `[ ]` `sqlcg mcp status` returns JSON with `running: true, pid, db_path, indexed_sha, head_sha, stale_by_commits, connected_clients, uptime` when a server is running on that DB
- `[ ]` `sqlcg mcp stop` causes `<db>.sock` and `<db>.pid` to be removed and the server process to exit 0 within 5 seconds
- `[ ]` `sqlcg mcp restart` prints the client-respawn guidance without pretending to restart the editor-spawned process
- `[ ]` With a server live, `sqlcg reindex --notify` (and the updated `post-checkout` hook) updates the graph without emitting "Database is locked" while the server continues to serve tool calls
- `[ ]` With no server, `sqlcg reindex --notify` falls through to the direct-write path — all existing reindex tests pass unchanged
- `[ ]` `post-checkout` and `post-merge` hooks print a one-line stderr cue when the graph could not be updated; the git checkout/merge itself is not blocked
- `[ ]` All control-file paths derive from `get_db_path()` / `SQLCG_DB_PATH` — verified by the Scenario A unit test in PR-C
- `[ ]` No `# TODO` in any success branch of any new module — verified by `grep -rn "TODO" src/sqlcg/core/freshness.py src/sqlcg/server/control.py src/sqlcg/server/server.py src/sqlcg/cli/commands/reindex.py`

---

## Risks and Mitigations

| ID | Risk | Sev | Mitigation | Ticket |
|----|------|-----|------------|--------|
| R1 | `resync_changed` blocks the event loop | HIGH | `anyio.to_thread.run_sync` in the reindex op; test Scenario A asserts the call happens in a thread | PR-D |
| R2 | Two concurrent notifies corrupt the single Kuzu connection | HIGH | `reindex_lock = anyio.Lock()` created once in `_run_with_control`; Scenario G tests concurrency | PR-D |
| R3 | Stale `.sock`/`.pid` after crash causes false "server alive" | MED | `mcp status` and `--notify` both catch `ConnectionRefusedError`/`FileNotFoundError` and fall through; Scenario D tests this | PR-C, PR-D |
| R4 | `connected_clients` not truly knowable over stdio | LOW | Reported as `1` (stdio transport = 1 client by design); documented as approximate; not an acceptance gate | PR-C |
| R5 | `restart` cannot re-parent editor stdio process | MED | `mcp restart` = `stop` + client-respawn guidance; no pretend restart; documented clearly; no TODO in body | PR-C |
| R6 | Socket security — any local user could send `stop`/`reindex` | MED | Socket and pid file set to mode `0o600`; owner-only; documented; no auth token in v1.1 | PR-C |
| R7 | Windows has no Unix domain sockets | MED | Platform guard (`if sys.platform != "win32"`) around socket creation in `server.py`; import is safe on Windows; deferred to v1.2 | PR-C |
| R8 | `_run_with_control` task group teardown order | MED | Control socket task must be cancelled before `shutdown_backend()` runs (to avoid serving ops on a closed connection). Ensure `anyio.move_on_after` or task-group cancel scope wraps the socket task, and `finally` in `main` calls `shutdown_backend()` after `anyio.run` returns | PR-C |
| R9 | Hook sentinel unchanged but script changed — `install-hooks` idempotency | LOW | Idempotency check uses sentinel comment string only; the sentinel does not include command arguments, so updating the script body does not break the idempotency guard for users who re-run `install-hooks` | PR-D |

---

### Deviations

#### Deviation 1: Stop mechanism uses `os._exit(0)` instead of cancel-scope + stdin-close
- **Reason**: The MCP `stdio_server` (mcp SDK) uses `anyio.to_thread.run_sync(readline, abandon_on_cancel=False)` for stdin reading. When the server is launched as a subprocess with a PIPE stdin, closing the read-end of the pipe from within the subprocess does NOT unblock the `readline()` thread — the parent process holds the write end open, so no EOF is delivered. `anyio.run()` then blocks indefinitely waiting for the thread to finish (because `abandon_on_cancel=False`). Using a cancel-scope cancellation cannot interrupt this blocked thread.
- **Change**: `_stop_watcher` now calls `shutdown_backend()`, `cleanup_control_files()`, then `os._exit(0)` instead of closing stdin and cancelling the scope. R8 is satisfied in a slightly different way: the control socket task is still running when `shutdown_backend()` is called in `_stop_watcher`, but `cleanup_control_files()` removes the `.sock` file before `os._exit(0)`, so no new connections can arrive after cleanup begins. The `main()` finally block is bypassed by `os._exit`.
- **Impact**: The exit path for `mcp stop` is `os._exit(0)` (hard exit, no finally blocks other than `_stop_watcher`'s explicit cleanup). Normal EOF-on-stdin exit still goes through `main()`'s finally block. This is the standard pattern for daemon stop-on-request. Tests assert exit code 0, socket file removal, and PID file removal — all pass.
- **Date**: 2026-05-31

---

### PR-E — post-merge hook routes through the server (Cluster A live-verification fix)

**Source**: live verification of Cluster A (#28) against the real `../dwh` repo, 2026-05-31
**Effort**: XS (hook-text-only change in the hook generator)
**Depends on**: PR-D (the `--notify` server-routing path it relies on)
**Blocks**: NONE
**Branch**: `fix/v1.0.2-bugfix`

#### Finding (verified live)

`sqlcg reindex` is *always* incremental. With `--from/--to` it diffs those SHAs; without
them it reads the last-indexed SHA from the DB and diffs against HEAD; only with no stored
SHA does it do a full index ([`reindex.py`](src/sqlcg/cli/commands/reindex.py) docstring +
mode block L151-197).

`--notify` only routes through the running server when `--from` is supplied. In standalone
mode (`--notify`, no `--from`) it cannot read the stored SHA without opening the locked DB,
so it deliberately raises
`OSError("--notify without --from requires direct DB access; falling through")`
([`reindex.py`](src/sqlcg/cli/commands/reindex.py) L88-95) and falls through to the direct
write → hits the single-writer lock → the hook prints the stderr cue but **the graph is not
updated**.

Net effect verified live: **post-checkout** works (git hands it `$1`/`$2` → routes through
server → `Resynced via server`, exit 0). **post-merge** calls `reindex … --notify` with
**no `--from/--to`** ([`git.py`](src/sqlcg/cli/commands/git.py) L42), so after a `git pull`
while the MCP server is live the graph is not updated (visible warning, not silent — but
still not applied). The server op itself already requires both SHAs
([`server.py`](src/sqlcg/server/server.py) L178: `if not root or not from_sha or not to_sha`).

#### Root cause

The post-merge hook does not pass the SHAs that git already exposes in its environment.
post-checkout routes correctly *only because git gives it `$1`/`$2`*. post-merge gets no
positional SHAs, but git **does** set `ORIG_HEAD` to the pre-merge HEAD, and the post-merge
hook runs with `HEAD` already at the merge result. The implementation already anticipates
this — the standalone-notify branch comment says "the caller should pass --from explicitly."

#### The fix (hook-text only — no server/control/reindex-logic change)

Change the generated **post-merge** script in `_HOOKS`
([`git.py`](src/sqlcg/cli/commands/git.py) L35-45) so it passes the SHAs explicitly from
git's environment and routes through the server exactly like post-checkout, with a guard
for the case where `ORIG_HEAD` is unset.

**Exact recommended hook text** (post-merge `script`):

```sh
#!/bin/sh
# sqlcg post-merge hook — incremental resync after pull/merge
# git sets ORIG_HEAD to the pre-merge HEAD; pass it as --from so --notify can route
# through a running server (same path as post-checkout). If ORIG_HEAD is unset (e.g.
# first-ever merge / gc'd), fall back to the standalone stored-SHA delta (direct write).
PREV=$(git rev-parse --verify --quiet ORIG_HEAD)
TOP=$(git rev-parse --show-toplevel)
if [ -n "$PREV" ]; then
  sqlcg reindex --from "$PREV" --to HEAD "$TOP" --dialect auto --quiet --notify \
    || echo "sqlcg: graph not updated (server busy/locked) -- run 'sqlcg mcp status'" >&2
else
  sqlcg reindex "$TOP" --dialect auto --quiet --notify \
    || echo "sqlcg: graph not updated (server busy/locked) -- run 'sqlcg mcp status'" >&2
fi
```

Notes on the text:
- `git rev-parse --verify --quiet ORIG_HEAD` prints the SHA on success and **nothing**
  (empty, exit 1) when ORIG_HEAD is unset — the `|| true` semantics of `$(...)` in
  `PREV=$(...)` mean an unset ORIG_HEAD yields an empty `PREV`, not a hook abort. The
  `[ -n "$PREV" ]` test then selects the fallback branch.
- `--to HEAD` (not `--to "$2"`) because post-merge has no `$2`; HEAD is already the merge
  result when the hook fires.
- The else branch is the **current** post-merge behaviour verbatim (standalone `--notify`
  → falls through to stored-SHA direct write). It is the documented fallback, not new code.
- The sentinel comment `# sqlcg post-merge hook` is **unchanged** → `install-hooks`
  idempotency (R9) is preserved; users who re-run `install-hooks` with the old hook present
  get the skip-silently path, and the script body is updated only on a fresh install or
  when the user manually appends it.

#### Verdicts on the reasoning items

1. **ORIG_HEAD correctness/availability — RESOLVED, guard required.**
   Verified empirically (temp clones, this machine, git 2.x): ORIG_HEAD is set to the
   pre-merge HEAD for every case the hook fires on —
   fast-forward pull (`ORIG_HEAD`=pre-pull HEAD), true divergent merge (`ORIG_HEAD`=pre-merge
   local HEAD), `pull --rebase` (`ORIG_HEAD`=pre-rebase tip; `ORIG_HEAD..HEAD` correctly spans
   the rebased range as "files that differ now"), and squash merge (`$1==1`: ORIG_HEAD set at
   merge time, persists through the squash commit). It is **unset on a fresh clone that has
   never merged** (`git rev-parse ORIG_HEAD` → fatal). **Verdict: a guard is mandatory** —
   without it the hook would send an empty `--from` to the server, which the server op rejects
   (`"reindex op requires root, from, to"`), surfacing as a server error exit 1 + the stderr
   cue and no update. The `--verify --quiet` + `[ -n "$PREV" ]` guard routes the unset case
   to the standalone stored-SHA fallback (today's behaviour) instead.

2. **Delta-boundary correctness (stale-before-pull) — ACCEPTABLE, documented, no change.**
   `ORIG_HEAD..HEAD` assumes the graph was current at `ORIG_HEAD`. If the stored indexed SHA
   is *older* than `ORIG_HEAD`, this delta misses `stored_sha..ORIG_HEAD`. **Verdict: accept
   it.** This is the **identical** boundary post-checkout already has (it diffs `$1..$2`,
   ignoring stored SHA), so PR-E does not introduce a new correctness class — it makes
   post-merge consistent with post-checkout. Closing the gap would require the server op to
   read the stored SHA and compute `min(stored, ORIG_HEAD)..HEAD`, which is a server-logic
   change (out of scope for a hook-text fix) and is exactly the "freshness is detectable"
   safety net PR-A already ships: `sqlcg db info` / `mcp status` report `stale_by_commits`, so
   a drifted graph is observable and a manual `sqlcg reindex` (standalone stored-SHA path)
   recovers it. Recommendation: do **not** expand scope; document the boundary in the hook
   comment is optional, the plan record here suffices.

3. **No-server case — CORRECT, acceptable.**
   With `--from ORIG_HEAD` and no live server, `--notify` catches
   `ConnectionRefusedError`/`FileNotFoundError` and falls through to the **direct** path,
   which now takes the explicit-SHA branch and diffs `ORIG_HEAD..HEAD` instead of
   `stored_sha..HEAD`. **Verdict: correct for a non-server pull** — same boundary as item 2,
   and strictly better than today (today's no-`--from` direct path uses stored-SHA, which is
   *wider* and can re-walk more, but is not more correct given the graph was current at the
   prior indexed state). The explicit-SHA path is the same code the post-checkout hook already
   exercises in no-server mode and is covered by existing PR-D Scenario C/D fallback tests. No
   guard needed beyond item 1's ORIG_HEAD check.

4. **Touch the standalone `--notify` codepath? — NO.**
   The standalone `--notify` (no `--from`) → `OSError` → fall-through is **working as
   designed**: it cannot open the locked DB to read the stored SHA, so direct-write fallback
   is the only safe behaviour. PR-E makes the hook *avoid* that branch by supplying `--from`,
   which is the smaller and correct change. **Verdict: hook-generation change only**
   (`git.py` `_HOOKS` post-merge `script`). No change to `reindex.py`, `server.py`,
   `control.py`, or any reindex logic. This is the minimal diff that fixes the finding.

#### Files affected

- [`src/sqlcg/cli/commands/git.py`](src/sqlcg/cli/commands/git.py) — replace the post-merge
  `_HookSpec.script` string only. Sentinel unchanged. No other file changes.

#### Wiring verification (run before opening PR)

- `grep -n "ORIG_HEAD" src/sqlcg/cli/commands/git.py` — must hit (post-merge passes it).
- `grep -n "rev-parse --verify --quiet ORIG_HEAD" src/sqlcg/cli/commands/git.py` — guard present.
- `grep -n "# sqlcg post-merge hook" src/sqlcg/cli/commands/git.py` — sentinel string
  unchanged (R9 idempotency).
- `grep -rn "TODO" src/sqlcg/cli/commands/git.py` — zero results.
- Confirm no edit to `reindex.py`/`server.py`/`control.py`:
  `git diff --stat` for this PR must show only `git.py` (and the test file).

#### Tests to add

Add to the existing hook test module (the one that already asserts post-checkout/post-merge
content — locate via `grep -rln "post-merge\|install-hooks\|_HOOKS" tests/`).

- **Scenario A — observable hook content (unit)**: run `install-hooks` into a temp git repo;
  read `.git/hooks/post-merge`; assert it contains `--from "$PREV"`, `--to HEAD`, `--notify`,
  the `git rev-parse --verify --quiet ORIG_HEAD` guard, and the `[ -n "$PREV" ]` branch.
  Assert the sentinel `# sqlcg post-merge hook` is present (idempotency).
- **Scenario B — idempotency unchanged (unit)**: run `install-hooks` twice; assert the
  post-merge file is written once and the second run is the silent-skip path (sentinel match).
- **Scenario C — ORIG_HEAD guard selects fallback (behavioural, shell)**: in a temp repo with
  ORIG_HEAD **unset**, execute the generated post-merge script with `sqlcg` stubbed by a shim
  on PATH that records its argv; assert the shim was invoked **without** `--from` (fallback
  branch taken, no broken empty-`--from` payload).
- **Scenario D — ORIG_HEAD present routes with SHAs (behavioural, shell)**: temp repo with
  ORIG_HEAD set to a real SHA; execute the script with the `sqlcg` shim; assert the shim was
  invoked with `--from <ORIG_HEAD_sha> --to HEAD --notify`.
- **Scenario E — live re-verification (e2e, the gate for this fix)**: build a temp origin +
  clone, `install-hooks` in the clone, `index` it, start a real `sqlcg mcp start` on the
  clone's DB; advance origin by one commit; run `git pull` in the clone (triggers post-merge);
  assert (a) the graph now reflects the pulled commit via the server (`mcp status` /
  `db info` `stale_by_commits == 0`), and (b) **no "Database is locked" error** appeared on
  the pull's stderr. This is the exact live-verification failure PR-E fixes.

#### Acceptance criteria

- `[ ]` `git pull` (simulated in a temp clone) while the MCP server is live updates the graph
  **via the server** with no lock error — Scenario E asserts `stale_by_commits == 0` post-pull
  and no "Database is locked" on stderr.
- `[ ]` Generated post-merge hook contains `git rev-parse --verify --quiet ORIG_HEAD`,
  `--from "$PREV"`, `--to HEAD`, `--notify`, and the `[ -n "$PREV" ]` fallback branch
  (Scenario A).
- `[ ]` With ORIG_HEAD unset, the hook invokes `sqlcg reindex` **without** `--from` (standalone
  fallback) and does not error (Scenario C).
- `[ ]` `install-hooks` remains idempotent — sentinel `# sqlcg post-merge hook` unchanged;
  second run skips silently (Scenario B, R9).
- `[ ]` `git diff --stat` for the PR shows only `git.py` + the test file — no reindex/server
  logic change (item 4).
- `[ ]` `grep -rn "TODO" src/sqlcg/cli/commands/git.py` — zero results; no TODO in the happy path.
