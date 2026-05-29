# Feature Plan: Living-Codebase Resync (F1)

Plan date: 2026-05-29
Author: architect-planner
Builds on: trust-layer ([`plan/trust_layer.md`](trust_layer.md)) and sprint-10 anchor tools, all on
branch `feat/sprint-10-anchor-tools`. Starting point: the F1 section of the DRAFT brief
[`plan/v1_living_codebase_and_skill.md`](v1_living_codebase_and_skill.md) (captures user intent +
grounded findings). F2 (bundled Claude skill) is a SEPARATE feature and is OUT of scope here.
Source authority: User feature request (the "files flooding in" problem); [`CLAUDE.md`](../CLAUDE.md)
performance invariants + small-/large-repo design rule + OOM/perf-budget notes;
[`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md).

---

## Summary

`sqlcg` indexes a repo once but the repo is a **living codebase** — branches get checked out,
pulls land, files are edited. Today a **branch change full-reindexes the entire corpus** (up to the
5-minute budget on 1,000–1,600 files) from *two* triggers: the `post-checkout` git hook and
`BranchMonitor._on_branch_change`. That is the "files flooding in" problem. The per-file watch path
is **already** incremental (2s debounce per file via `WatchJobManager`) and stays exactly as-is —
"parse on every save" is already handled correctly.

This feature replaces both full-reindex branch triggers with **one git-diff-based incremental resync
entry point** that reindexes only the changed files **plus the cross-file pass-2 affected dependency
closure** (the make-or-break correctness requirement, see Design §"Closure"). It extends the git
hooks to cover `post-merge` (pulls/merges) in addition to `post-checkout`, keeps the install
idempotent, and mirrors the uninstall sentinel-strip for the new hook.

---

## Scope

### In Scope

1. **`Indexer.resync_changed(root, old_sha, new_sha, db, dialect)`** — the single reusable
   incremental entry point. Computes the file delta from `git diff --name-status old new`, reindexes
   added/modified files, deletes removed files, and recomputes the cross-file pass-2 closure for the
   union of (dependents of changed files) ∪ (dependents of deleted files). Returns an observable
   summary dict (counts of files added/modified/deleted, closure files re-resolved).
2. **`sqlcg reindex` CLI command** with two modes:
   - `sqlcg reindex --from <sha> --to <sha> <root>` — explicit SHAs (what the hooks pass).
   - `sqlcg reindex <root>` (no SHAs) — standalone "catch up": reads the **last-indexed SHA** stored
     in graph metadata and diffs it against current `HEAD`. See Scope Decision (b).
3. **Persisted index metadata** (Scope Decision (b)): store the **last-indexed git SHA** on the
   existing `SchemaVersion`-style singleton so `sqlcg reindex` (no SHAs) can compute its own delta.
   This BUMPS `SCHEMA_VERSION` "3" → "4" (re-index is the migration path; no backward compat).
   `index_repo` writes it on every full index; `resync_changed` writes the new SHA on success.
4. **Rewire both branch triggers to `resync_changed`**:
   - `BranchMonitor` captures old + new SHA across a branch change and calls `resync_changed`
     instead of `index_repo`.
   - The `post-checkout` hook calls `sqlcg reindex --from "$1" --to "$2" <root>` instead of
     `sqlcg index <root>`.
5. **Extend git hooks**: add a **`post-merge`** hook (covers `git pull` / `git merge`) that calls
   `sqlcg reindex <root>` in standalone mode (post-merge receives no old/new SHA — only a squash
   flag — so it uses the stored-SHA delta path). Keep `install-hooks` idempotent (per-hook sentinel)
   and mirror the uninstall sentinel-strip for the new hook. **`post-commit` is OUT** — see Scope
   Decision (c).
6. **Tests** asserting observable output: correct delta set reindexed, dependents re-resolved
   (exact lineage after a simulated branch switch that changes an upstream CTAS body), deletes
   invalidate dependents, hooks contain the expected content, stored SHA round-trips.

### Non-Goals

- **The per-file debounced watch path is NOT changed.** `WatchJobManager` (2s debounce) and
  `SqlFileEventHandler` keep their current behaviour. The only watcher change is inside
  `BranchMonitor._on_branch_change` (swap `index_repo` → `resync_changed`).
- **No `post-commit` hook.** Local commits do not change tracked file *content* on disk relative to
  the working tree the user is already editing; the watch path already covers in-progress edits, and
  a commit hook would fire a resync on every commit (noisy, redundant). Decision (c).
- **No staleness *fact* tool/CLI in this PR.** "N files changed since last index" surfaced via MCP
  reusing the trust-layer `Judgement`/fact shape is a **fast-follow**, NOT this feature. Decision (d).
  The metadata this feature persists (last-indexed SHA) is the prerequisite that makes that
  fast-follow a small add later.
- **No change to the parser hot paths.** [`base.py`](../src/sqlcg/parsers/base.py) and the
  `_upsert_parsed_file` / `_extract_column_lineage` internals of
  [`indexer.py`](../src/sqlcg/indexer/indexer.py) are NOT touched. `resync_changed` is a NEW method
  that *composes* the existing pass-1/pass-2/upsert machinery on a subset of files; it must not
  reintroduce per-row work or O(N²) pass-2 expansion (see Constraints).
- **No new graph node/edge *labels*.** The only schema change is adding a scalar property to the
  metadata singleton (and bumping `SCHEMA_VERSION`). `File.sha` already exists in the DDL.

---

## Design

### The central correctness risk — the cross-file pass-2 affected closure

Per CLAUDE.md, **cross-file pass-2 (CTAS-body resolution) dominates lineage and is non-local**.
Grounding (verified):
- [`aggregator.py`](../src/sqlcg/lineage/aggregator.py) `register_pass1` harvests every CTAS body
  into `cross_file_sources` keyed by **lowercased bare table name**, and every defined table into
  `sources` keyed by `full_id`. Pass-2 of file *X* expands `exp.expand()` using the CTAS bodies of
  the *other* files that *X* references (`dependency_filter`).
- Therefore editing file *U* (which defines an upstream CTAS body) can change the resolved
  column-lineage of every file *D* that references *U* — even though *D* itself did not change.
- The existing single-file path ([`indexer.py`](../src/sqlcg/indexer/indexer.py) `reindex_file`)
  only re-resolves **VIEW** dependents via `STALE_VIEWS_QUERY` (`DECLARES`→VIEW). It does **not**
  re-resolve CTAS/table dependents. A naive incremental resync that only re-parses the touched files
  would inherit this gap and produce a **fast-but-WRONG** graph.

**The closure rule (Scope Decision (a)):**

A resync of changed-file set `C` and deleted-file set `Δ` must re-resolve, in pass-2:

```
reparse_set      = C                              (added + modified files: full pass-1 + pass-2)
closure_set      = dependents(C ∪ tables_defined_in(Δ))   (pass-2 re-resolve only)
delete_set       = Δ                              (delete_nodes_for_file, no reparse)
```

where `dependents(F)` = the set of files containing a query that `SELECTS_FROM` a table **defined in**
any file in `F`. This is **one BFS level by default (direct dependents)**, made transitive to a
bounded depth — see "Bounding" below.

Concretely the closure is computed by a single graph query per frontier level, reusing the existing
edge shape `(t:SqlTable)<-[:SELECTS_FROM]-(q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)`:

```cypher
-- DEPENDENT_FILES_OF_TABLES  (new named query)
-- Given a list of table qualified names, return the distinct files that consume them.
UNWIND $tables AS tbl
MATCH (t:SqlTable {qualified: tbl})<-[:SELECTS_FROM]-(q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)
RETURN DISTINCT f.path AS path
```

Why this is correct AND bounded:
- **Correct for modifications**: the tables defined in each changed file are looked up (graph read of
  the *pre-resync* state for deletes, post-reparse state for adds/mods), their consuming files are
  the affected set, and those files are re-run through pass-2 with the freshly-harvested
  `cross_file_sources`.
- **Correct for deletes**: a deleted file's tables are looked up **before** `delete_nodes_for_file`
  removes them, so its dependents are captured, then those dependents are re-resolved (they will now
  resolve the dangling reference to nothing — which is the correct new lineage).
- **Bounded**: transitive lineage *can* fan out, so the closure is computed by **iterating
  `DEPENDENT_FILES_OF_TABLES` to a fixed max depth (`max_closure_depth=3`, configurable)**, with a
  visited-set so each file is re-resolved at most once. The default depth of 3 covers the common
  staging→intermediate→mart→report ETL chain; depth is recorded here so it is not a magic number.
  If the frontier is still growing at the cap, the resync **falls back to a full `index_repo`** and
  logs the reason (observable in the summary dict as `fell_back_to_full: true`). This guarantees the
  graph is never silently wrong: either the bounded closure is provably complete (frontier emptied
  before the cap) or we did a full reindex.

This is the make-or-break of the feature and is tested directly (see Test Strategy
"closure correctness" scenarios) — not asserted by "no exception raised".

### `resync_changed` — algorithm

```
def resync_changed(root, old_sha, new_sha, db, dialect, ...) -> dict:
    1. delta = git_name_status_delta(root, old_sha, new_sha)   # added/modified/deleted .sql paths
       (filtered through load_ignore_spec + .sql suffix, same predicate as the watcher's _is_sql)
    2. if delta is None (git unavailable / shallow): full index_repo fallback (log + summary flag)
    3. # capture deleted files' tables BEFORE deleting (needed for dependent closure)
       deleted_tables = union over delete_set of GET_TABLES_DEFINED_IN_FILE
    4. for f in delete_set: db.delete_nodes_for_file(f)          # within a transaction
    5. reparse_set = added ∪ modified
       # PASS 1 over reparse_set + the existing on-disk corpus CTAS bodies:
       # to keep cross_file_sources complete WITHOUT re-reading the whole corpus, harvest
       # cross-file CTAS bodies for the closure from the GRAPH (already-parsed q.sql bodies are
       # NOT sufficient — see "Cross-file source seeding" below).
    6. closure_set = bounded_dependents(tables_defined_in(reparse_set) ∪ deleted_tables)
    7. resolve_set = reparse_set ∪ closure_set   (closure-only files: pass-2 re-resolve, not full reparse)
    8. run the existing two-pass machinery on resolve_set with batched upsert (batch_size default),
       deleting old nodes for each reparsed file first (delete_nodes_for_file is idempotent).
    9. self._expand_star_sources(db)             # once, after the batch (same as index_repo)
   10. write new_sha to metadata singleton
   11. return {added, modified, deleted, closure_resolved, fell_back_to_full, batch_size}
```

**Cross-file source seeding (the subtle part).** `index_repo` builds `cross_file_sources` by
harvesting CTAS bodies from the *in-memory* pass-1 results of the whole corpus. `resync_changed`
only parses a subset, so it must seed the aggregator's `cross_file_sources`/`sources` with the bodies
the closure files depend on. Two options, decided here:

- **Chosen: re-harvest CTAS bodies for the dependency frontier from disk by parsing the
  closure+reparse files' *referenced* upstream definers.** Specifically: after pass-1 of
  `reparse_set`, for any referenced table not satisfied in-memory, look up its defining file via
  `GET_TABLE_DEFINING_FILES`, and add those definer files to the pass-1 harvest set (pass-1 only —
  parse to harvest the CTAS body, do NOT re-upsert them unless they are in `reparse_set`). This keeps
  the harvest proportional to the *delta's dependency fan-in*, not the corpus. The `CrossFileAggregator`
  already exposes `register_pass1`; the new code path reuses it.
- Rejected: reconstructing CTAS bodies from the stored `q.sql` (truncated to 500 chars in
  `_upsert_parsed_file`, so it is lossy and cannot be re-parsed reliably). Recorded so a future
  contributor does not try to "optimize" by reading `q.sql`.

This seeding step is the reason the closure is computed against **defining files**, and it is bounded
by the same `max_closure_depth` walk.

### Persisted index metadata (Scope Decision (b))

`sqlcg reindex` with no SHAs needs a baseline. Design:

- Add a property to the metadata singleton. The cleanest, lowest-risk shape that respects "no new
  node labels" is to **add `indexed_sha STRING` to the existing `SchemaVersion` node** (it is already
  a singleton written in `kuzu_backend.init_schema`). DDL change in
  [`schema.cypher`](../src/sqlcg/core/schema.cypher):
  ```cypher
  CREATE NODE TABLE SchemaVersion (
      version STRING PRIMARY KEY,
      indexed_sha STRING
  );
  ```
- New backend methods on [`graph_db.py`](../src/sqlcg/core/graph_db.py) ABC +
  [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py): `set_indexed_sha(sha: str) -> None` and
  `get_indexed_sha() -> str | None`, written via `MERGE (v:SchemaVersion {version: $version}) SET
  v.indexed_sha = $sha` and read via `MATCH (v:SchemaVersion) RETURN v.indexed_sha`. Mirror the
  existing `get_schema_version` pattern (kuzu_backend.py line 353).
- **`SCHEMA_VERSION` bumps "3" → "4"** in [`schema.py`](../src/sqlcg/core/schema.py). The `watch`
  command already gates on a version mismatch (watch.py lines 35–41) and tells the user to reset +
  re-init — that is the documented migration path. No backward compat, no data migration.
- `index_repo` writes the current `HEAD` SHA via `set_indexed_sha` at the end of a successful run
  (it already knows `path`; capture `git rev-parse HEAD` once — `git` is already used by
  `walk_sql_files`). `resync_changed` writes `new_sha` on success.
- If `get_indexed_sha()` returns `None` (repo indexed before this feature, or never indexed),
  `sqlcg reindex` with no SHAs **falls back to a full `index_repo`** (and logs why). Correct and safe.

### API changes (CLI)

`sqlcg reindex` registered in [`main.py`](../src/sqlcg/cli/main.py) via
`app.command("reindex")(reindex.reindex_cmd)`. Signature mirrors `index_cmd` parameters that still
apply (`--dialect`/`-d` with `auto`, `--quiet`/`-q`, `--batch-size`), plus:
- `--from <sha>` (`old`, optional)
- `--to <sha>` (`new`, optional, defaults to `HEAD` when `--from` is given)
- positional `path` (root), same as `index`.

Behaviour:
- both `--from` and `--to` given → `resync_changed(path, from, to, ...)`.
- neither given → standalone: `old = db.get_indexed_sha()`, `new = HEAD`; if `old is None` →
  full `index_repo`.
- the command gates on `get_schema_version() == SCHEMA_VERSION` exactly like `watch` does, with the
  same reset-and-reinit message, so a v3 DB does not silently mis-resync.

### Git hooks

[`git.py`](../src/sqlcg/cli/commands/git.py) `install-hooks` is generalized to write **two** hooks,
each with its own sentinel, each idempotent and each with the existing "foreign hook present" warning
path preserved:

- `post-checkout` (sentinel `# sqlcg post-checkout hook`):
  ```sh
  #!/bin/sh
  # sqlcg post-checkout hook — incremental resync after branch switch
  # $3 == 1 means branch checkout (not file checkout); skip file checkouts
  [ "$3" = "1" ] || exit 0
  sqlcg reindex --from "$1" --to "$2" "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true
  ```
- `post-merge` (sentinel `# sqlcg post-merge hook`):
  ```sh
  #!/bin/sh
  # sqlcg post-merge hook — incremental resync after pull/merge
  # post-merge receives only $1 (squash flag), no old/new SHA; use stored-SHA delta
  sqlcg reindex "$(git rev-parse --show-toplevel)" --dialect auto --quiet || true
  ```

`uninstall.py` `_step3_remove_git_hook` is generalized to strip **both** sentinels (loop over a
`(filename, sentinel)` list), reusing the existing block-stripping logic. The current function name
and the existing post-checkout strip behaviour are preserved.

### Dependencies

- `git` CLI (already a runtime dependency — `walk_sql_files`, `BranchMonitor`).
- No new third-party package.
- Reuses: `CrossFileAggregator`, `Indexer._upsert_parsed_file`, `Indexer._expand_star_sources`,
  `delete_nodes_for_file`, `load_ignore_spec`, `get_parser`, `GET_TABLES_DEFINED_IN_FILE`,
  `GET_TABLE_DEFINING_FILES`, `NoiseFilter`-independent (resync does not noise-filter; it resyncs the
  literal file set).

---

## Implementation Steps

### Phase 1 — Metadata + schema bump (foundation)

**Step 1.1** — Add `indexed_sha STRING` to the `SchemaVersion` DDL and bump `SCHEMA_VERSION` to `"4"`.
- Files: [`schema.cypher`](../src/sqlcg/core/schema.cypher), [`schema.py`](../src/sqlcg/core/schema.py)
- Acceptance: a freshly `init_schema`'d DB has `get_schema_version() == "4"`; a v3 DB triggers the
  reset message in `watch`/`reindex`.

**Step 1.2** — Add `set_indexed_sha(sha)` + `get_indexed_sha()` to the `GraphBackend` ABC and the
KuzuBackend implementation (mirror `get_schema_version`).
- Files: [`graph_db.py`](../src/sqlcg/core/graph_db.py), [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
- Acceptance: `set_indexed_sha("abc")` then `get_indexed_sha() == "abc"` (round-trip integration
  test on in-memory KuzuDB).

**Step 1.3** — `index_repo` writes `HEAD` SHA via `set_indexed_sha` at the end of a successful run
(read `git rev-parse HEAD` in `cwd=path`; on git failure, skip silently — non-git repos still index).
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py)
- Acceptance: after `index_repo` on a git fixture, `get_indexed_sha()` equals the fixture's HEAD.

### Phase 2 — Closure query + git delta helper

**Step 2.1** — Add `DEPENDENT_FILES_OF_TABLES` named Cypher + export.
- Files: [`queries.cypher`](../src/sqlcg/core/queries.cypher), [`queries.py`](../src/sqlcg/core/queries.py)
- Acceptance: on a built 3-table chain (A→B→C across 3 files), querying with `[A]` returns B's file;
  with `[B]` returns C's file.

**Step 2.2** — Add a git delta helper `git_name_status_delta(root, old, new) -> Delta | None` (new
small module, e.g. [`src/sqlcg/indexer/git_delta.py`](../src/sqlcg/indexer/git_delta.py)). Runs
`git diff --name-status <old> <new>`, splits into added/modified/deleted/renamed (a rename = delete
old + add new), filters to non-ignored `.sql` files using `load_ignore_spec` + suffix check
(same predicate the watcher uses), returns absolute paths. Returns `None` on git failure / shallow
clone / unknown SHA so callers fall back to full index.
- Acceptance: unit test with a temp git repo: add/modify/delete/rename produce the correct four sets;
  a non-`.sql` change is excluded; an ignored path is excluded; a bad SHA returns `None`.

### Phase 3 — `resync_changed` (the core)

**Step 3.1** — Implement `Indexer.resync_changed(...)` per the algorithm above, including:
bounded closure walk (`max_closure_depth=3`), CTAS-body re-harvest of definer files for cross-file
seeding, capture-deleted-tables-before-delete, batched upsert, single `_expand_star_sources`,
`set_indexed_sha(new_sha)` on success, full-`index_repo` fallback (delta `None`, or frontier still
growing at depth cap) with a `fell_back_to_full` flag in the summary.
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py) (NEW method; existing methods untouched)
- Acceptance: see Test Strategy — closure correctness + delta-only scenarios.

**Step 3.2** — Verify `resync_changed` never calls `upsert_node`/`upsert_edge` per row — it composes
`_upsert_parsed_file` (already bulk Phase A→B→C) inside a batched transaction, identical to
`index_repo`'s `_flush_batch`. Refactor `_flush_batch` into a small reusable helper if needed so
both call sites share it (keeps the bulk invariant in one place).
- Acceptance: the bulk-upsert invariant test still passes; a new assertion confirms `resync_changed`
  on a 10-file delta issues O(files) bulk calls, not O(rows) per-row calls.

### Phase 4 — CLI `reindex` command

**Step 4.1** — Add [`src/sqlcg/cli/commands/reindex.py`](../src/sqlcg/cli/commands/reindex.py)
`reindex_cmd` with the signature in Design §"API changes", the schema-version gate (same message as
watch), the two modes (explicit SHAs vs stored-SHA delta vs full fallback when stored SHA is `None`),
and `--quiet` summary output.
- Files: new `reindex.py`, register in [`main.py`](../src/sqlcg/cli/main.py).
- Acceptance: `sqlcg reindex --from X --to Y <root>` reindexes only the delta (asserted via summary);
  `sqlcg reindex <root>` with a stored SHA reindexes the delta since that SHA; with no stored SHA
  does a full index.

### Phase 5 — Rewire branch triggers + extend hooks

**Step 5.1** — `BranchMonitor`: capture old SHA (`git rev-parse HEAD` *before* updating
`_current_branch`) and new SHA (after), and call `indexer.resync_changed(root, old, new, db, dialect)`
in `_on_branch_change` instead of `index_repo`. Keep the pause/cancel/drain coordination exactly as
today. `BranchMonitor.__init__` already has `indexer`, `db`, `watched_path`; it needs the `dialect`
threaded through (currently `WatchJobManager` holds it — pass it into `SqlFileEventHandler` →
`BranchMonitor`).
- Files: [`watcher.py`](../src/sqlcg/indexer/watcher.py), [`watch.py`](../src/sqlcg/cli/commands/watch.py)
  (thread `dialect` into the handler).
- Acceptance: simulated branch change in `test_branch_monitor` calls `resync_changed` (not
  `index_repo`) with the captured old/new SHAs; the existing pause/drain assertions still hold.

**Step 5.2** — Generalize `install-hooks` to write both `post-checkout` (updated to call
`sqlcg reindex --from "$1" --to "$2"`) and `post-merge`, each idempotent with its own sentinel,
preserving the foreign-hook warning path.
- Files: [`git.py`](../src/sqlcg/cli/commands/git.py)
- Acceptance: after `install-hooks`, both hook files exist, are `0o755`, contain their sentinel and
  the `sqlcg reindex` invocation; running twice does not duplicate the block.

**Step 5.3** — Generalize `uninstall` `_step3_remove_git_hook` to strip both sentinels.
- Files: [`uninstall.py`](../src/sqlcg/cli/commands/uninstall.py)
- Acceptance: after install then uninstall, neither hook file contains a sqlcg sentinel (deleted if
  the file becomes empty, otherwise the foreign content is preserved).

---

## Test Strategy

### Unit tests
- [`tests/unit/test_git_delta.py`](../tests/unit/test_git_delta.py) NEW — temp git repo; assert
  add/modify/delete/rename classification, `.sql`-only + ignore filtering, `None` on bad SHA.
- [`tests/unit/test_git_hooks.py`](../tests/unit/test_git_hooks.py) (extend) — both hooks written,
  idempotent, content contains `sqlcg reindex` + the SHA args for post-checkout; uninstall strips
  both; foreign-hook warning path preserved.
- [`tests/unit/test_branch_monitor.py`](../tests/unit/test_branch_monitor.py) (extend) — branch
  change invokes `resync_changed(old, new)` not `index_repo`; pause/cancel/drain unchanged.

### Integration tests (real in-memory KuzuDB, reuse `_index_fixture` helper)
In a new [`tests/integration/test_resync.py`](../tests/integration/test_resync.py):

- **closure correctness — modified upstream re-resolves dependents** (THE make-or-break test):
  index files U (`CREATE TABLE staging AS SELECT a, b FROM raw`) and D
  (`CREATE TABLE mart AS SELECT a FROM staging`). Capture `mart.a` upstream lineage. Then mutate U to
  `SELECT a AS renamed_a, b FROM raw` (so `staging` no longer has column `a`), commit, call
  `resync_changed(old, new)`. Assert: U is reparsed AND D is in the closure AND `mart`'s
  column-lineage is recomputed to reflect the new `staging` (observable: the old `staging.a → mart.a`
  edge is gone / changed), even though D's file was NOT in the git delta.
- **delete invalidates dependents**: index U + D as above; delete U, commit, `resync_changed`. Assert
  U's nodes are gone AND D was re-resolved (its dangling reference to `staging` resolves to nothing —
  observable: the `staging.* → mart.*` edges are removed).
- **delta-only — untouched files not reparsed**: index 3 unrelated files; modify one; assert the
  summary reports `modified == 1`, `closure_resolved` excludes the two unrelated files, and the two
  unrelated files' nodes are byte-identical (e.g. their query node count unchanged).
- **bounded closure fallback**: construct a chain deeper than `max_closure_depth`; assert the resync
  sets `fell_back_to_full == True` and the resulting graph equals a full `index_repo` of the new tree
  (correctness preserved under fallback).
- **stored SHA round-trip + standalone reindex**: `index_repo` writes HEAD; `get_indexed_sha()`
  returns it; commit a change; `sqlcg reindex <root>` (no SHAs) reindexes exactly the delta and
  updates the stored SHA.
- **no stored SHA → full index**: fresh DB (or v-bumped), `get_indexed_sha() is None` →
  `sqlcg reindex <root>` does a full index (assert all files present).

### Regression / invariant
- The bulk-upsert invariant test ([`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py))
  and the perf scaling guard ([`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py))
  must stay green — `resync_changed` shares the batched `_flush_batch` path and must not add per-row
  upserts or per-column qualify. A targeted assertion confirms `resync_changed` on an N-file delta
  scales with the delta, not the corpus.

---

## Acceptance Criteria

- [ ] `resync_changed(root, old, new, db, dialect)` exists, is called from BOTH `BranchMonitor._on_branch_change`
      and `reindex_cmd` (grep-confirmed call sites), and returns a summary dict with
      `added/modified/deleted/closure_resolved/fell_back_to_full/batch_size`.
- [ ] On a modified upstream CTAS, a **dependent file not in the git delta** has its column-lineage
      recomputed (closure correctness integration test asserts the exact edge change).
- [ ] On a deleted file, its dependents are re-resolved and the dangling lineage edges are removed
      (integration test asserts the removed edges).
- [ ] Untouched files are NOT reparsed (summary `modified` count + unchanged node counts assert this).
- [ ] Closure walk is bounded at `max_closure_depth=3`; exceeding it sets `fell_back_to_full == True`
      and the resulting graph equals a full `index_repo` (integration test).
- [ ] `SCHEMA_VERSION == "4"`; `SchemaVersion` DDL has `indexed_sha`; `set_indexed_sha`/`get_indexed_sha`
      round-trip; `index_repo` and `resync_changed` both write the SHA on success.
- [ ] `sqlcg reindex --from X --to Y <root>` resyncs the delta; `sqlcg reindex <root>` uses the stored
      SHA; with no stored SHA it does a full index. The command gates on schema version like `watch`.
- [ ] `install-hooks` writes BOTH `post-checkout` (calling `sqlcg reindex --from "$1" --to "$2"`) and
      `post-merge` (calling `sqlcg reindex <root>`), each idempotent, each `0o755`, foreign-hook
      warning preserved.
- [ ] `uninstall` strips BOTH hook sentinels.
- [ ] The per-file watch path (`WatchJobManager`, `SqlFileEventHandler` on_modified/created/moved/deleted)
      is unchanged — `git diff` shows no behavioural change to `jobs.py` or the file-event handlers.
- [ ] No edit to `base.py`; `_upsert_parsed_file`/`_extract_column_lineage` internals unchanged;
      bulk-upsert invariant + perf scaling guard stay green.
- [ ] No `# TODO` in the happy path of `resync_changed`, the closure walk, the CTAS re-harvest, the
      `reindex` command, or the hooks.
- [ ] Every new method/command has a grep-confirmed call site; path/SHA fallbacks match `KuzuConfig`
      (db path) and the existing git usage; tests assert observable output, not "no exception raised".

---

## Scope Decisions (explicit, per the brief)

**(a) Cross-file pass-2 affected closure — computed and bounded; deletes handled.**
Closure = files containing a query that `SELECTS_FROM` a table **defined in** any changed-or-deleted
file, walked transitively via the new `DEPENDENT_FILES_OF_TABLES` query to a fixed
`max_closure_depth=3` (covers the typical staging→intermediate→mart→report chain). Each closure file
is pass-2 **re-resolved** (not full-reparsed). Cross-file CTAS bodies the closure depends on are
re-harvested from disk by parsing the upstream **definer** files (pass-1 only), because the stored
`q.sql` is truncated to 500 chars and is lossy. **Deletes**: a deleted file's defined tables are
looked up via `GET_TABLES_DEFINED_IN_FILE` **before** `delete_nodes_for_file`, so its dependents are
captured and re-resolved (they correctly lose the dangling lineage). If the frontier is still growing
at the depth cap, the resync **falls back to a full `index_repo`** (flagged `fell_back_to_full`) so
the graph is never silently wrong.

**(b) Standalone `sqlcg reindex` needs persisted metadata — YES.**
Add `indexed_sha STRING` to the singleton `SchemaVersion` node (no new node label), with
`set_indexed_sha`/`get_indexed_sha` backend methods. `index_repo` and `resync_changed` write the
current/new HEAD SHA on success. `sqlcg reindex` with no `--from`/`--to` diffs the stored SHA against
`HEAD`; if the stored SHA is `None` it does a full index. This **bumps `SCHEMA_VERSION` "3" → "4"**;
re-index is the migration path (no backward compat). Per-file *content* hashes are NOT needed —
`git diff` of two SHAs is the authoritative, cheaper delta (content hashes would only matter for
detecting working-tree edits, which the watch path already handles). `File.sha` already exists in the
DDL but is unused; this feature does not start populating it (out of scope; git is the source of truth
for the delta).

**(c) `post-commit` hook — OUT.**
Only `post-checkout` + `post-merge` are in scope. A commit does not change which files exist relative
to the working tree the user is editing, the watch path already covers in-progress edits, and a
commit-triggered resync would fire on every commit (noisy and redundant). Recorded so it is a
deliberate exclusion, not an oversight.

**(d) Staleness *fact* — FAST-FOLLOW, not this feature.**
Surfacing "N files changed since last index" via MCP/CLI reusing the trust-layer `Judgement`/fact
shape is deferred. This feature delivers the **prerequisite** (the persisted `indexed_sha`), so the
fast-follow becomes a small read-only tool: `count(git_name_status_delta(get_indexed_sha(), HEAD))`
surfaced as a cold fact. Keeping it out keeps this PR appropriately sized for one feature.

---

## Risks and Mitigations

- **Risk: closure under-computes → fast-but-wrong graph.** Mitigation: closure is the make-or-break
  test (modified-upstream + deleted-upstream scenarios assert exact edge changes on dependents NOT in
  the delta); depth-cap fallback to full index guarantees no silent wrongness.
- **Risk: cross-file CTAS seeding is incomplete because only a subset is parsed.** Mitigation:
  re-harvest definer files (pass-1) for referenced-but-unsatisfied tables; the depth-cap fallback
  covers pathological fan-in.
- **Risk: SCHEMA_VERSION bump strands existing v3 DBs.** Mitigation: `watch`/`reindex` already gate on
  version and print the reset+reinit message; re-index is the documented migration path. Acceptable
  per the no-backward-compat rule.
- **Risk: re-introducing per-row upserts in the new path.** Mitigation: `resync_changed` reuses the
  shared batched `_flush_batch` helper; bulk-upsert invariant + perf scaling guard tests gate it.
- **Risk: BranchMonitor races (resync in flight at shutdown).** Mitigation: unchanged from today —
  `daemon=False` + `join(timeout=5)` already handles in-flight resync; `resync_changed` is a shorter
  operation than the full index it replaces, reducing the window.

## Rollout / rollback notes

- Rollout: ships on `feat/sprint-10-anchor-tools`. Users re-init the DB once (v3→v4) — the same
  reset+reinit flow already surfaced by `watch`. `git install-hooks` re-run installs the new
  `post-merge` hook and updates `post-checkout` (idempotent).
- Rollback: revert the PR; v3 schema and the full-reindex hooks return. No persisted-data migration to
  undo beyond a one-time re-index (the migration path in both directions).

---

## Open Decisions (genuinely unresolved — flagged for the user / reviewer)

These do not block implementation (sensible defaults are in the plan) but the user owns the final call:

1. **`max_closure_depth` default = 3.** Chosen to cover the common
   staging→intermediate→mart→report ETL chain while keeping the closure bounded and the
   "still-growing → full reindex" fallback rare. If real DWH chains are routinely deeper, raising the
   default trades a wider incremental closure against more frequent full-index fallbacks. Confirm the
   typical max chain depth on the target corpus before locking the default. (Implementation makes it a
   parameter so this is a one-line change, not a redesign.)

2. **`post-merge` standalone-delta vs. capturing merge parents.** `post-merge` gets no old/new SHA
   (only the squash flag), so the plan uses the stored-`indexed_sha`→`HEAD` delta. An alternative is
   to derive the pre-merge SHA from `ORIG_HEAD` inside the hook and pass explicit `--from/--to`. I
   chose the stored-SHA path because it is robust even if the user ran other operations between index
   and merge, and it reuses the same standalone code path as `sqlcg reindex <root>`. Confirm this is
   acceptable, or opt into `ORIG_HEAD` for a tighter merge delta. Both are correct; the stored-SHA
   path is simpler and strictly-safe (it can only resync a superset, never a subset).
