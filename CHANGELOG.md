## v1.25.0 (2026-06-13)

> **Re-index recommended (no schema change).** `SCHEMA_VERSION` unchanged.
> View classification (`kind='view'`) and the `gating_join_tables` supplement only
> take effect on a fresh `sqlcg index <path>`.

### Feat

- **view classification**: `CREATE VIEW` targets now stamped `kind='view'` so they
  persist as `SqlTable.kind='view'` instead of being indistinguishable from tables;
  live-verified on a ~1,400-file DWH (582 views classified)
- **empty-impact** (`sqlcg analyze empty-impact` / `get_empty_propagation` MCP tool):
  downstream blast radius when named table(s) are empty — two views: value-derivation
  (primary, column-lineage refinement) and row-reachability (supplement, direct
  gating-join reads)
- **pr-impact** (`sqlcg analyze pr-impact` / `get_pr_impact` MCP tool): detect
  producers a PR dropped + their blast radius; code-regression detection (not runtime
  monitoring); rename-handling via column-set AND consumer Jaccard ≥ 0.6
- **gating-join supplement** (`get_change_scope` / `diff_impact`): `gating_join_tables`
  field added — tables that row-empty via direct gating-join reads supplementing the
  column-lineage blast radius; caveat: depends on join type (not recorded on edges)
- **3-signal unused** (`analyze_unused` / `sqlcg analyze unused`): redefines "used"
  as the union of (D1) direct `SELECTS_FROM`, (D2) `STAR_SOURCE`, and (D3)
  column-lineage CTE-derived read; `find_table_usages` augmented with `via_lineage`
  entries for the same D3 signal

### Fix

- **pr-impact BUG-A**: CLI path was not initialising the backend before calling
  `_compute_pr_impact`, causing `RuntimeError: Backend not initialized`
- **pr-impact BUG-B**: blast-radius engine was zeroing the result when the resync
  produced no new edges; now computed on the pre-resync graph (Option i)

## v1.1.3 (2026-06-01)

> **Re-index recommended (no schema change).** `SCHEMA_VERSION` stays `6`, so no
> forced migration — but the lineage fixes below only take effect on a fresh
> `sqlcg index <path>`, which adds the previously-missing CTE-body source nodes and
> union-CTE edges. Folds in the unreleased v1.1.1/v1.1.2 work — there is no separate
> 1.1.1 or 1.1.2 release.

### Fix

- **#39**: emit a `SqlTable` node for source tables referenced only inside CTE bodies,
  so `find table` and the graph-completeness invariant see CTE-body-only sources
- **#38**: invert the `analyze upstream`/`downstream` kind-filter to `OPTIONAL MATCH …
  WHERE t.kind IS NULL OR t.kind IN ['table','external']`, so default-flag queries
  return real physical sources instead of dead-ending at the insert-CTE node
- **#38**: trace `SELECT *` across a `UNION ALL` of sibling CTEs (N≥3 branches) — derive
  the unioning CTE's columns from the deepest branch `SELECT`; the CTE hop is preserved
  (`cte_union.col ← branch_cte.col`, `CTE_PROJECTION`) and physical sources stay reachable
  via multi-hop traversal
- **#28**: open CLI read commands (`analyze`, `find`, `db info`, `gain`) with a read-only
  KùzuDB connection so concurrent readers coexist and reads don't take the exclusive write
  lock; accurate lock-semantics docs (a read still blocks while a read-write writer holds the
  process-level lock — documented as a known limitation)
- **#28**: git hooks route `reindex --notify` through the live server's control socket and
  print a visible stderr warning on failure instead of a silent `|| true`

### Test / hardening

- **#40**: replace the ineffective lineage regression guards with effective sentinels —
  reverting either half of the #38/#39 fix now turns a guard red; the #38 filter is asserted
  against the real production query path, not a mirrored string
- **#28**: cross-process lock tests pin reader/reader concurrency and the writer-blocks-reader
  limitation; hook `--notify` content and write-path fallthrough are regression-tested

## v1.1.0 (2026-06-01)

> **⚠️ Upgrade note — re-index required.** The graph schema advanced from
> `SCHEMA_VERSION` 4 to 6 (added `start_line` on `SqlQuery`, repurposed
> `SqlTable.kind`, added the `ExternalConsumer` node + `CONSUMED_BY` edge). There
> is no in-place migration — re-run `sqlcg index <path>` to rebuild the graph. If
> you run the MCP server, stop it (`sqlcg mcp stop`) and reconnect via your editor after re-indexing. (Note: as of v1.20.0 the server self-heals on the next tool call — no manual stop needed.)

### Feat

- **#28/#29/#30**: live-graph freshness & daemon reindex — Unix-socket control channel,
  `mcp status/stop/restart`, reindex through the running server, post-merge hook routing
  via `ORIG_HEAD`, `index --include-working-tree` + dirty sentinel, and a freshness signal
  (indexed SHA vs HEAD) surfaced in `db info` / the `db_info` MCP tool
- **#31**: source location — `file`/`line`/`expression` on lineage edges (`start_line` on `SqlQuery`)
- **#32**: meaningful confidence — `1.0` for plainly-parsed facts, with a `reason` on inferred edges
- **#33**: CLI/MCP output parity + node-kind tagging (`table` / `cte` / `derived` / `external`)
- **#34**: `analyze_unused` segregates presentation/terminal tables from dead code
- **#35**: external downstream lineage injection — `ExternalConsumer` node + `CONSUMED_BY` edge
- **hygiene**: config-path footgun warning, find/analyze noise-filter parity, cause discoverability,
  and MCP config-root reconciliation
- **skill**: bundled Claude skill teaches the new provenance + node-kind fields (F2)
- **indexer**: batch-scoped upsert — bulk writes once per batch instead of per file
  (~13,400 → ~270 backend round-trips on the 1,340-file DWH corpus; upsert 169s → 52s)

### Fix

- **#31**: preserve newline count in Snowflake preprocessing (multi-line `start_line` bug)
- **#28**: socket timeout fix, symbolic-ref `--to` resolution, hook binary path
- **index**: resolve walker root once so relative paths yield absolute paths (sibling of #24)
- **analyze_unused**: external-consumer warning clarifies the edge is created anyway;
  debug log on the no-Repo File-node fallback

### Perf

- **indexer**: raise default per-file parse timeout 5s→10s

## v1.0.2 (2026-05-30)

### Fix

- **parser**: restore COALESCE/func lineage + CTE-chain hops broken by eb19f29 (#25)
- **observability**: make pool-path timeouts visible + route warnings to log file (#13/#27d PR-07)
- **analyze**: query-time bare-name fallback for unqualified INSERT targets (#26 PR-06)
- **analyze**: wire NoiseFilter into CLI + add *_bck_* glob (#27a/b PR-08)
- **install**: restore uvx cold-cache message dropped in #23 rewrite
- **parser**: preserve skip markers for INSERT-column-list expressions (#25 regression)
- **parser**: INSERT positional mapping overrides SELECT alias attribution (#25)
- **install**: write via claude mcp add + ~/.claude.json fallback, remove settings.json (#23)
- **server**: capture fd 1 before _configure_mcp_logging to fix stdio transport (#22)
- **find**: lowercase name/ref before graph query to match C2 normalization (#12)
- **reindex**: resolve root to absolute before ignore matching (#24)

## v1.0.1 (2026-05-30)

### Fix

- **indexer**: abort immediately on Ctrl-C instead of flushing a partial graph

### Perf

- **parser**: recover 3 once-per-statement lineage regressions from 4234e5d

## v1.0.0 (2026-05-30)

### Feat

- **trust-layer**: `Judgement` discriminator (fact vs. heuristic) on risk/dead-code
  outputs; `analyze_unused` + `get_hub_ranking` tools; golden-harness answer-anchors
- **F1 living-codebase resync**: incremental branch-change resync via
  `Indexer.resync_changed` + cross-file pass-2 affected closure; standalone
  `sqlcg reindex`; `indexed_sha` metadata (schema v4); `post-merge` hook
- **F2 bundled skill**: `sqlcg install` provisions a `SKILL.md` encoding the
  fact/heuristic boundary; `sqlcg mcp best-practices` CLI command
- **index**: `--profile` flag emitting per-stage and per-file timing
- **analyze**: `failures` subcommand surfacing per-file parse-failure cause

### Refactor

- remove the INFORMATION_SCHEMA-CSV / `load-schema` path (zero measured edge delta)
- compact the generated `SKILL.md` (~40% smaller, fact/heuristic-sorted tool table)

## v0.2.1 (2026-05-05)

### Fix

- **benchmarks**: add bench_*.py to pytest python_files so bench_indexer.py is collected
- **tests**: use tracked variable in fake_run and regenerate cli docs
- **tests**: strip ANSI codes in test_cli_help assertion

## v0.2.0 (2026-05-05)

### Feat

- **walker**: default to git ls-files to avoid indexing untracked files
- **phase-9+10**: git-aware graph freshness + PyPI publishing
- **phase-8**: metrics and feedback layer

### Fix

- **tools**: move inline Cypher queries to queries.py and fix decorator order (C1, C2)
- **walker**: address code-review findings C2/W1/W2/W4/S1
