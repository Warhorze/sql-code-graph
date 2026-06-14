# Fix Plan: Backward Self-Heal — Index at SHA via Detached Worktree

**Plan date:** 2026-06-14
**Author:** architect-planner
**Status:** NEEDS-REVIEW
**Target version:** 1.28.4 (fix-only → **patch** per CLAUDE.md SemVer rule; master is at 1.28.3)

GitHub issue: [#128](https://github.com/Warhorze/sql-code-graph/issues/128) — "pr-impact requires graph indexed at base-ref and is not self-healing"
Tracked as **OPEN-2** in [`plan/reports/pr_impact_followups_2026-06-14.md`](../reports/pr_impact_followups_2026-06-14.md).

---

## Re-validation against CURRENT master (supersedes the stale "REWORK ONLY" review)

An earlier plan-review judged this draft "REWORK ONLY", but that review ran against a
**stale pre-PR-F local branch**. Re-validated against current `master`
(`ecbe60c`, `__version__ = "1.28.3"`), the rework premise is **largely void**:

- `_reindex_to_sha` **EXISTS** at [`tools.py:2709`](../../src/sqlcg/server/tools.py) and is
  called from `_compute_pr_impact` at [`tools.py:2843`](../../src/sqlcg/server/tools.py).
  Its contract is fully in place: it returns `False` on any exception or post-heal SHA
  mismatch ([`tools.py:2742`, `2752`](../../src/sqlcg/server/tools.py)). **No change to this
  function is needed** — the fix lands entirely below it, in `resync_changed`.
- `get_indexed_sha` is now **SCHEMA_VERSION-keyed** (PR-F, commit `1adbb72`), so the
  post-heal check `actual_sha == target_sha` at [`tools.py:2752`](../../src/sqlcg/server/tools.py)
  is reliable on migrated DBs. The draft no longer needs to defend against the
  multi-version-stale-read; that is RESOLVED upstream.
- The stamp mechanism the worktree approach relies on is confirmed at
  [`indexer.py:684-686`](../../src/sqlcg/indexer/indexer.py): `git rev-parse HEAD` is run with
  `cwd=str(path)`, where `path` is `index_repo`'s first argument. Stamping is therefore a
  pure function of the directory passed to `index_repo` — exactly the property the worktree
  path exploits (see Design §SHA stamping).

**What the draft still got wrong (corrected in this revision):**
- Master is `1.28.3`, not `1.28.1`; this is a **fix**, so target is **1.28.4 (patch)**, not 1.29.0/minor.
- `test_backward_incremental_heal_stamps_target_sha` lives in
  [`tests/integration/test_pr_impact_integration.py:466`](../../tests/integration/test_pr_impact_integration.py),
  NOT in `test_pr_impact_unit.py`.
- Step 2.1's original wording perpetuated the **B4 anti-pattern** (faking the stamp). Fixed below.

---

## Summary

`pr-impact` self-heals backward via [`_reindex_to_sha`](../../src/sqlcg/server/tools.py) →
`Indexer.resync_changed(old=HEAD_sha, new=base_sha)` ([`tools.py:2741`](../../src/sqlcg/server/tools.py)).
The **incremental path** (delta within `max_closure_depth=3`) works correctly and stamps
`base_sha` (proven by `test_backward_incremental_heal_stamps_target_sha`).

The **large-backward-delta fallback** is broken. `resync_changed` has two fallback branches
that both call bare `self.index_repo(root, …)`:
- git-delta-None fallback: [`indexer.py:858-860`](../../src/sqlcg/indexer/indexer.py).
- closure-depth-exceeded fallback: [`indexer.py:967-969`](../../src/sqlcg/indexer/indexer.py).

`root` is the live working tree (checked out at HEAD). For a **backward** reindex
(`new_sha = base_sha < HEAD`), `index_repo` walks HEAD, and its end-of-run stamp at
[`indexer.py:684-693`](../../src/sqlcg/indexer/indexer.py) writes HEAD, not `base_sha`.
`_reindex_to_sha`'s post-check at [`tools.py:2752`](../../src/sqlcg/server/tools.py) detects
the mismatch and returns `False`, so the user gets the "reindex manually" hint at
[`tools.py:2851`](../../src/sqlcg/server/tools.py) (SAFE — fails closed, no wrong analysis), but
the heal is unavailable.

This plan adds a **detached git worktree** path so both fallback cases materialize
`base_sha`'s tree state and full-index against it, leaving the user's working tree untouched.

---

## Resolved design decisions (a) (b) (c)

### (a) — Index-at-SHA mechanism: `git worktree add --detach` is CHOSEN

**Decision:** materialize `base_sha` via `git worktree add --detach <tmp> <base_sha>`, run
`index_repo(<tmp>, …)` against it, then `git worktree remove --force <tmp>`.

**cwd/stamp interaction (with file:line):** `index_repo`'s end-of-run stamp runs
`git rev-parse HEAD` with `cwd=str(path)` at [`indexer.py:684-686`](../../src/sqlcg/indexer/indexer.py),
and writes the result via `db.set_indexed_sha(_head_sha)` at
[`indexer.py:693`](../../src/sqlcg/indexer/indexer.py). When `path` is the detached worktree
checked out at `base_sha`, `git rev-parse HEAD` returns `base_sha` (a detached worktree's HEAD
is the named commit). Therefore the existing stamp writes `base_sha` **with zero change to
`index_repo` and no new `set_indexed_sha` call**. This auto-correction is the whole point of
the worktree approach — the stamp follows the cwd.

This decision is final; alternatives (`git archive | tar`, blob reads, `git checkout` in place)
are rejected per the table in Design §Chosen materialization approach.

### (b) — B4 fix: Step-2.1 unit test must NOT fake `set_indexed_sha`

**Problem identified:** the existing PR-F unit tests
([`test_pr_impact_unit.py:323`, `362`, `392`](../../tests/unit/test_pr_impact_unit.py)) drive the
post-heal SHA via `mock_db.get_indexed_sha.side_effect = [...]` while fully mocking `Indexer`.
They never exercise `resync_changed`, the fallback dispatch, the worktree, or the real stamp.
The original draft's Step 2.1 repeated this by monkeypatching `_index_repo_at_sha` to "call
`db.set_indexed_sha(target_sha)`" — a fake that would pass even if the real stamp were broken.

**Decision:** the correctness of "fallback stamps base_sha" is proven ONLY by an **integration
test against a real DuckDB backend and a real git worktree** (Step 2.2), where the stamp comes
from the genuine `index_repo` → `git rev-parse HEAD` → `set_indexed_sha` chain. The unit test in
Step 2.1 is **demoted** to asserting only the **dispatch decision** (backward → `_index_repo_at_sha`,
forward → `index_repo`) and the **`False`-on-failure contract**; it must NOT assert anything about
the stamped SHA, and must NOT inject a `set_indexed_sha` side-effect that fakes the stamp.

### (c) — `use_git` inside the worktree: decided `use_git=True` (default) UP FRONT

**Decision:** call `index_repo(tmp_dir, dialect, db)` with the **default `use_git=True`**
([`indexer.py:326`](../../src/sqlcg/indexer/indexer.py)).

**Why:** a detached worktree has a working `.git` gitdir-pointer file; `git ls-files` inside it
([`indexer.py:388`](../../src/sqlcg/indexer/indexer.py) via `walk_sql_files`) enumerates the tracked
files of the worktree's HEAD — i.e. exactly the files committed at `base_sha`. This is the correct
file set for a base-SHA index and applies the same `.gitignore`/tracked-file filtering as a normal
run, keeping the backward-heal index identical to what a `sqlcg index` at `base_sha` would produce.
`walk_sql_files` already falls back to `rglob` if `git ls-files` fails, so there is no hard failure
mode. (The former BQ1 is resolved: `use_git=True`, with the built-in rglob fallback as the safety
net — no separate developer investigation gates the work.)

---

## Non-Goals

- **No change to the incremental path.** The small-delta happy path through `resync_changed`
  ([`indexer.py:863-1000+`](../../src/sqlcg/indexer/indexer.py)) is correct and must not be slowed.
- **No change to `index_repo`'s hot-path logic** or any CLAUDE.md performance invariant.
- **No change to the forward resync path** (graph at base → HEAD) inside `_compute_pr_impact`.
- **No change to `_reindex_to_sha`** ([`tools.py:2709`](../../src/sqlcg/server/tools.py)) — its
  `True`/`False` contract absorbs the fix transparently.
- **No change to `get_indexed_sha`/`set_indexed_sha`** (already SCHEMA_VERSION-keyed from PR-F).
- **No Windows worktree path.** `git worktree` is available on all supported platforms
  (Linux, macOS, WSL2). A Windows-specific fallback, if ever needed, is a separate fix.
- **No guard against the installed post-checkout hook racing the heal.** That race is documented
  at [`tools.py:2799-2805`](../../src/sqlcg/server/tools.py) and is out of scope.

---

## Design

### Root cause
Both fallback branches call bare `self.index_repo(root, …)`
([`indexer.py:858-860`](../../src/sqlcg/indexer/indexer.py) and
[`indexer.py:967-969`](../../src/sqlcg/indexer/indexer.py)). `root` is the live working tree at
HEAD; for a backward reindex it indexes and stamps HEAD, so the post-heal check fails.

### Chosen materialization approach: `git worktree add --detach`

```
git worktree add --detach <tmp_dir> <base_sha>
index_repo(<tmp_dir>, dialect, db, use_git=True, …)   # stamp auto-corrects to base_sha
git worktree remove --force <tmp_dir>
```

| Approach | Verdict | Reason |
|----------|---------|--------|
| `git worktree add --detach` | **Chosen** | Real filesystem tree `index_repo`/`walk_sql_files` walk with zero core changes; isolated from the user's tree; one-call cleanup; stamp auto-corrects via cwd. |
| `git archive \| tar -x` | Considered | Equivalent result but adds a shell pipeline + temp tar and depends on `.gitattributes export-ignore` semantics the project hasn't tested. |
| `git cat-file blob SHA:path` | Considered | Avoids a disk copy but requires reimplementing `walk_sql_files`. Too much new surface. |
| In-place `git checkout base_sha` | Rejected | Mutates the user's working tree — violates the core safety requirement; a crash leaves detached HEAD. |

### SHA stamping (the correctness keystone)
See decision (a) above. The stamp at [`indexer.py:684-693`](../../src/sqlcg/indexer/indexer.py)
uses `cwd=str(path)`; passing the worktree as `path` makes it write `base_sha`. No new stamp call.

### Catalog (columns.csv) interaction
`index_repo` calls `_reapply_catalog_if_configured(db, path)` at
[`indexer.py:700`](../../src/sqlcg/indexer/indexer.py). With `path` = worktree root, it reads the
`.sqlcg.toml`/`columns.csv` committed **at base_sha** — which is exactly the correct config for a
base-SHA index. If absent at base_sha, catalog reapplication silently skips. No special handling.

### Uncommitted changes
The detached worktree contains only committed state at `base_sha`; the user's uncommitted
working-tree edits are invisible to it. Correct: `pr-impact` analyses committed SHAs only.

### Where the new code lives
- New private method `Indexer._index_repo_at_sha` in
  [`indexer.py`](../../src/sqlcg/indexer/indexer.py), wrapping the worktree create/index/cleanup.
- New helper `_get_current_head(root) -> str | None` in
  [`git_delta.py`](../../src/sqlcg/indexer/git_delta.py) — chosen over `freshness._git`
  ([`freshness.py:46`](../../src/sqlcg/core/freshness.py)) to avoid a core-layer import:
  `indexer.py` **already** imports `git_delta` at [`indexer.py:837`](../../src/sqlcg/indexer/indexer.py).
- Both fallback branches in `resync_changed` get a backward-vs-forward dispatch.

### Detecting backward vs. forward fallback
Before each fallback `index_repo` call, resolve current HEAD via `_get_current_head(root)`:
- If HEAD is known and `new_sha != HEAD` → **backward** → `_index_repo_at_sha(root, new_sha, …)`.
- Else (forward, where the tree IS at `new_sha`; or HEAD unknown) → `index_repo(root, …)` unchanged.

This keeps the **forward fallback path completely unaffected** (acceptance gate below).

### Cleanup safety
`try/finally` around the worktree; `git worktree remove --force` (force needed for a detached
worktree git treats as modified), then a belt-and-suspenders `shutil.rmtree(..., ignore_errors=True)`
for git versions that exit 0 yet leave the dir. Temp dir created via `tempfile.mkdtemp(prefix="sqlcg_worktree_")`.

### Performance
- Incremental path: **unchanged** — no new subprocess, no worktree.
- Backward fallback: adds one `git worktree add` + checkout + `index_repo` + `git worktree remove`.
  Cost is O(corpus) — identical to the manual full reindex that was previously the only option.
  No happy-path regression; the four perf-invariant suites are untouched (see gates).

---

## Implementation Steps

### Phase 1 — helpers + dispatch

**Step 1.1 — Add `_get_current_head` to [`git_delta.py`](../../src/sqlcg/indexer/git_delta.py).**
```python
def _get_current_head(root: Path) -> str | None:
    """Return `git rev-parse HEAD` for *root*, or None on any failure."""
```
Mirror `freshness._git` ([`freshness.py:46-60`](../../src/sqlcg/core/freshness.py)) exactly
(`subprocess.run([...], cwd=str(root), capture_output=True, text=True)`; return stripped stdout on
returncode 0 else None; never raise). `git_delta.py` already imports `subprocess`
([`git_delta.py:11`](../../src/sqlcg/indexer/git_delta.py)).
Call site confirmed in Step 1.3.

**Step 1.2 — Add `_index_repo_at_sha` to `Indexer` in [`indexer.py`](../../src/sqlcg/indexer/indexer.py).**
```python
def _index_repo_at_sha(
    self, root: Path, target_sha: str, db: GraphBackend, dialect: str | None,
    *, batch_size: int = 50, timeout_per_file: int = 10,
) -> None:
    """Full index of *target_sha*'s tree via a detached git worktree.

    Stamp is written by index_repo's existing `git rev-parse HEAD` (cwd=worktree)
    at indexer.py:684-693, which returns target_sha for a detached worktree.
    The worktree is always removed (try/finally + shutil.rmtree backstop).
    """
```
- `git worktree add --detach <tmp> <target_sha>` (cwd=root); on `CalledProcessError`
  (unknown SHA, shallow clone, git missing) log ERROR and **re-raise** so the caller's
  `_reindex_to_sha` returns `False` and the existing manual-reindex hint fires.
- `self.index_repo(tmp_dir, dialect, db, batch_size=batch_size, timeout_per_file=timeout_per_file)`
  — `use_git=True` (default; decision (c)).
- **No** `db.set_indexed_sha` call here — the stamp is `index_repo`'s job.
- `finally`: `git worktree remove --force <tmp>` (capture_output, swallow + log on failure),
  then `shutil.rmtree(tmp_dir, ignore_errors=True)` if it still exists.

**Step 1.3 — Wire dispatch into BOTH fallback branches of `resync_changed`.**
Replace the bare `self.index_repo(root, …)` at
[`indexer.py:858-860`](../../src/sqlcg/indexer/indexer.py) (git-delta-None) and at
[`indexer.py:967-969`](../../src/sqlcg/indexer/indexer.py) (closure-depth) with:
```python
from sqlcg.indexer.git_delta import _get_current_head  # already-imported module
_head = _get_current_head(root)
if _head is not None and new_sha != _head:
    self._index_repo_at_sha(root, new_sha, db, dialect,
                            batch_size=batch_size, timeout_per_file=timeout_per_file)
else:
    self.index_repo(root, dialect, db,
                    batch_size=batch_size, timeout_per_file=timeout_per_file)
```
This is the grep-confirmed call site for both `_index_repo_at_sha` and `_get_current_head`.

### Phase 2 — Tests

**Step 2.1 — Unit: dispatch + failure contract** in
[`tests/unit/test_pr_impact_unit.py`](../../tests/unit/test_pr_impact_unit.py)
(co-located with the existing `_reindex_to_sha` tests). **Per decision (b): do NOT fake the stamp.**
- `test_resync_fallback_dispatches_to_index_at_sha_when_backward`: on a real `Indexer`, patch
  `_get_current_head` → a HEAD ≠ `new_sha`, force the git-delta-None fallback (patch
  `git_name_status_delta` → `None`), patch `Indexer._index_repo_at_sha` and `Indexer.index_repo`
  as spies. Assert `_index_repo_at_sha` called once, `index_repo` (the bare root call) NOT called.
  Assert NOTHING about the stamped SHA.
- `test_resync_fallback_dispatches_to_index_repo_when_forward`: same but `_get_current_head` ==
  `new_sha`; assert `index_repo` called, `_index_repo_at_sha` NOT called.
- `test_reindex_to_sha_returns_false_when_index_at_sha_raises`: patch `Indexer._index_repo_at_sha`
  to raise `subprocess.CalledProcessError`; assert `_reindex_to_sha` returns `False` (no propagation).

**Step 2.2 — Integration: real worktree stamps base_sha (the B4-correct proof)** in
[`tests/integration/test_resync.py`](../../tests/integration/test_resync.py):
- `test_large_backward_heal_via_worktree_stamps_base_sha`:
  Build a real git repo with a CTAS chain longer than `max_closure_depth=3` (A→B→C→D→E).
  Commit 1 = A only; index it. Commit 2 adds B,C,D,E; index at commit 2 (graph at HEAD).
  Call `_reindex_to_sha(db, root, commit_1_sha)`. The backward delta deletes 4 files whose
  closure fans out past depth 3, tripping the closure-depth fallback. Assert (real backend,
  real worktree, real stamp):
  - `_reindex_to_sha` returns `True`.
  - `db.get_indexed_sha() == commit_1_sha`.
  - Graph reflects commit 1: B,C,D,E nodes absent; A present.
  - User's working tree still at commit 2 (`_get_current_head(root) == commit_2_sha`).
- `test_large_backward_heal_worktree_cleaned_up_on_failure`:
  Patch `Indexer.index_repo` to raise `RuntimeError("injected")` after the worktree exists.
  Snapshot `tempfile.gettempdir()` for `sqlcg_worktree_*` before/after; assert no leak and that
  `_reindex_to_sha` returns `False`.

**Step 2.3 — Unit: `_get_current_head`** in
[`tests/unit/test_git_delta.py`](../../tests/unit/test_git_delta.py):
- `test_get_current_head_returns_sha_in_git_repo`: temp git repo, one commit → 40-char hex SHA.
- `test_get_current_head_returns_none_in_non_git_dir`: bare temp dir → `None`.

### Phase 3 — Version bump (patch)
**Step 3.1** — `1.28.3` → `1.28.4`:
- [`pyproject.toml`](../../pyproject.toml) line 7 `version = "1.28.4"`
- [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) line 3 `__version__ = "1.28.4"`
- `uv lock`
- Acceptance: `test_version_parity.py` green.

---

## Files Touched

| File | Change |
|------|--------|
| [`src/sqlcg/indexer/indexer.py`](../../src/sqlcg/indexer/indexer.py) | Add `_index_repo_at_sha`; dispatch at the two fallback branches (`:858-860`, `:967-969`) |
| [`src/sqlcg/indexer/git_delta.py`](../../src/sqlcg/indexer/git_delta.py) | Add `_get_current_head` |
| [`tests/unit/test_pr_impact_unit.py`](../../tests/unit/test_pr_impact_unit.py) | Dispatch + failure-contract unit tests (no faked stamp) |
| [`tests/unit/test_git_delta.py`](../../tests/unit/test_git_delta.py) | `_get_current_head` unit tests |
| [`tests/integration/test_resync.py`](../../tests/integration/test_resync.py) | Real-worktree stamp + cleanup integration tests |
| [`pyproject.toml`](../../pyproject.toml) | Version → `1.28.4` |
| [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) | `__version__ = "1.28.4"` |

**NOT touched:** `src/sqlcg/server/tools.py` (`_reindex_to_sha` unchanged), `src/sqlcg/parsers/**`,
`src/sqlcg/core/**`, all four perf-invariant test files.

---

## Acceptance Gates (verbatim)

1. **Integration gate (backward heal):** a large backward self-heal now stamps `base_sha`
   (not HEAD) and `_reindex_to_sha` returns `True`. Specifically,
   `test_large_backward_heal_via_worktree_stamps_base_sha` asserts, against a real DuckDB backend
   and a real git worktree, that after a closure-depth-fallback backward heal:
   `_reindex_to_sha(db, root, commit_1_sha) is True` and `db.get_indexed_sha() == commit_1_sha`.
   The base_sha must be proven by the **real** `index_repo` stamp path — NOT a faked
   `set_indexed_sha` (B4).
2. **Perf-invariant gate:** the four perf-invariant suites pass **UNMODIFIED** —
   [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
   [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
   [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
   [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py).
3. **Forward-path gate:** the forward (non-self-heal) path is unaffected — the forward fallback
   still calls `index_repo(root, …)` directly (asserted by
   `test_resync_fallback_dispatches_to_index_repo_when_forward`), and the incremental path is
   unchanged (existing `test_backward_incremental_heal_stamps_target_sha` at
   [`test_pr_impact_integration.py:466`](../../tests/integration/test_pr_impact_integration.py)
   still passes).
4. **Cleanup gate:** the temp worktree is always cleaned up, including on failure —
   `test_large_backward_heal_worktree_cleaned_up_on_failure` asserts no `sqlcg_worktree_*` dir
   remains in `tempfile.gettempdir()` after an injected `index_repo` failure, and that
   `_reindex_to_sha` returns `False`.
5. **Helper gate:** `_get_current_head` returns a 40-char hex SHA in a git repo and `None` in a
   non-git directory (Step 2.3).
6. **Call-site gate:** every new function has a grep-confirmed call site (`_index_repo_at_sha`
   and `_get_current_head` both called from the dispatch in Step 1.3).
7. **Version gate:** `test_version_parity.py` green at `1.28.4`.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `git worktree add` slow on large repos | Medium | Cost == the manual full reindex that was the only prior option. No incremental-path regression. |
| `git worktree add` fails on shallow clone (missing base_sha) | Medium | `CalledProcessError` re-raised → `_reindex_to_sha` returns `False` → existing manual-reindex hint. Same as pre-fix. |
| Temp worktree leaked on crash | Low | `try/finally` + `shutil.rmtree(ignore_errors=True)` backstop. Cleanup gate asserts no leak on injected failure. |
| `index_repo` stamps wrong SHA in worktree | Low | Detached-worktree HEAD is always the named commit; `_reindex_to_sha`'s post-check at [`tools.py:2752`](../../src/sqlcg/server/tools.py) is the final safety net (returns `False` on mismatch). |
| Cross-device `tempfile.gettempdir()` vs repo (WSL2 `/tmp`) | Low | git worktrees do not require same-filesystem placement. If it ever fails, `CalledProcessError` is caught → safe `False`. |
| Concurrent heals create two worktrees | Very low | Unique `mkdtemp` per call; DB writes already guarded by the server `backend_lock`; CLI is single-process. |

---

## Version Bump
`1.28.3` → `1.28.4` (patch: fix-only; the backward-fallback previously returned `False`
immediately, so no existing behaviour regresses — the heal simply now succeeds where it failed closed).

---

## Review Trail
*(To be filled in by plan-reviewer.)*
