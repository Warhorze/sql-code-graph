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
