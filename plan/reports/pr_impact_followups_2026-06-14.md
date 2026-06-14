# pr-impact follow-ups surfaced during PR-F (#128) acceptance

**Date:** 2026-06-14
**Context:** shepherd review of PR-F (#128, self-healing reindex, branch `feat/pr-f-self-healing-reindex`).
PR-F itself ships correct + tested (self-heal `_reindex_to_sha` + the `get_indexed_sha` fix below).
These two items are out of PR-F scope and left for the user to triage.

## RESOLVED in PR-F — `get_indexed_sha` multi-version stale read

`get_indexed_sha()` was `SELECT indexed_sha FROM "SchemaVersion" LIMIT 1` (no `WHERE version`).
`set_indexed_sha` writes `INSERT OR REPLACE … (version, indexed_sha) VALUES (SCHEMA_VERSION, sha)`,
so a DB migrated across versions keeps one row per version (the live DWH had rows for `1`, `8`, `9`).
`LIMIT 1` therefore returned an arbitrary/stale row, defeating `_reindex_to_sha`'s post-heal SHA
check (and mis-reporting the indexed SHA to every other caller) on any migrated DB. **Fixed in PR-F**
(`WHERE version = SCHEMA_VERSION`) + regression test `test_get_indexed_sha_ignores_stale_version_rows`.

## OPEN 1 (likely a real bug) — forward-resync crash on delete-only HEAD deltas

**Symptom.** A live `sqlcg analyze pr-impact --base <ref>` run crashed with a **DuckDB internal
assertion inside `upsert_nodes_bulk(NodeLabel.QUERY, …)`** during the step-4b forward resync
(restore-to-HEAD), while processing newly-added **delete-only** SQL files in the HEAD delta.
(`indexer.py:1108` → `_upsert_file_batch` → `_flush_row_batch` → `indexer.py:278`.)

**Ruled out as a PR-C regression.** PR-C added a `qualify_failed` column to the QUERY node. I
checked: the *only* QUERY-node row builder is `indexer.py:1353` (`_build_file_row_set`), which PR-C
correctly updated to emit `qualify_failed`; the resync path flushes through that same builder, so
the rows are not missing the column. The `qualify_failed: 0` at `indexer.py:712` is an error-bucket
counter, not a node row. So this is **not** a column-list mismatch from PR-C.

**Caveats / unknowns.**
- Surfaced on the **3.8 GB swap-thrashing** dev box — a "DuckDB internal assertion" during a large
  bulk upsert *may* be memory/environment-induced rather than a logic bug. **Needs repro on a real
  box** before deep diagnosis.
- Not confirmed present on pre-PR-C master (repro is a full DWH resync; expensive here).
- Independent of PR-F's new code (the self-heal was correctly skipped — graph already at base).

**Suggested next step.** Repro on a normal-memory machine: index the DWH, advance HEAD by a
delete-only delta, run `pr-impact`. If it still asserts, bisect QUERY-row values on delete-only files
(NULL/dtype on a NOT-NULL column, duplicate id, or transaction-state from nested resync).

## OPEN 2 (documented limitation) — large backward self-heal stamps HEAD, not base

`_reindex_to_sha` reindexes backward via `Indexer.resync_changed(old, new=base_sha)`. The incremental
delta path stamps `base_sha` correctly (proven by `test_backward_incremental_heal_stamps_target_sha`).
But when the backward delta is large enough to trip `resync_changed`'s **closure-depth fallback**
(or git-delta-None fallback), the fallback does a **full index of the working tree** — which is still
checked out at HEAD — and stamps **HEAD**, not `base_sha`. `_reindex_to_sha`'s post-check then detects
the mismatch and returns `False` → user gets a clear "reindex manually" hint (safe: no wrong analysis).

**Full fix (follow-up):** a checkout-based or index-at-SHA path so a large backward heal can actually
materialize `base_sha`'s tree state, rather than falling back to a HEAD-stamped full index.

Related: [[project_pr_impact_followup]], [[project_data_loss_impact_sprint]].
