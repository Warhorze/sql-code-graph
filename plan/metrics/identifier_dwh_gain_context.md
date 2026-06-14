# PR-A IDENTIFIER($var) — gain before/after context

Plan: [`sprint_snowflake_lineage_patterns.md`](../sprints/sprint_snowflake_lineage_patterns.md) §PR-A.

## Runs

- BEFORE: clean master `86a500f` (**v1.34.3**, the canonical current-master baseline)
  → [`identifier_dwh_before_v1.34.3.json`](identifier_dwh_before_v1.34.3.json)
- AFTER: `fix/identifier-var-indirection` (**v1.35.0**, PR-A)
  → [`identifier_dwh_after_v1.35.0.json`](identifier_dwh_after_v1.35.0.json)
- Corpus: `/home/ignwrad/Projects/dwh` (1,451 `.sql` files), fresh from-scratch index
  into an isolated `SQLCG_DB_PATH` (never `~/.sqlcg/graph.db`).
- BEFORE index wall-clock: 2:52 (under the 3-min budget), peak RSS ~1.2 GB.

## Headline deltas (graph-derived coverage block)

| metric | BEFORE | AFTER | delta |
|---|---|---|---|
| resolvable_write_col_edges | 36,936 | 36,936 | +0 |
| zero_edge_write_queries | 1,034 | 1,034 | +0 |
| total_write_queries | 2,323 | 2,323 | 0 |
| good_edges | 52,667 | 52,657 | -10 |
| good_edges_strict | 48,140 | 48,140 | +0 |
| good_edges_scoped | 37,460 | 37,460 | +0 |
| total_edges | 60,815 | 60,805 | -10 |
| edge_health_strict_pct | 79.16 | 79.17 | +0.01 |

## Why the headline metrics did not move on THIS corpus

PR-A resolves only **pure string-literal** `SET v='da.x'` / `v := 'da.x'` bind variables
(plan §Step A.1: concatenation / `CONCAT` / non-literal RHS is explicitly out of scope and
falls through to the empty-id drop, no regression).

Of the **136** DWH files using `identifier($…)`, **zero** use a pure-literal SET RHS:

- 96 use `SET full_tablename = 'DL_' || split_part(current_database(),'_',2) || '.SCHEMA.TBL'`
  (a `CURRENT_DATABASE()`-dependent **concatenation**, parses to `exp.DPipe`, not `exp.Literal`).
- 40 use `SET v = (SELECT 'DL_' || … )` (a **subquery** RHS).

Both forms depend on the runtime database name and are not statically resolvable — they are the
documented out-of-scope case. The 142-file / 175-site estimate in the research doc counted
IDENTIFIER occurrences; it did not pre-classify the SET RHS form. On this corpus the resolvable
(static-literal) subset is empty, so the metric movement is 0 by construction, not by failure.

## The -10 good_edges delta is pre-existing indexer non-determinism, NOT a PR-A regression

A control re-index of clean master (v1.34.3) vs the original master run produced:

```
master-run-1 COLUMN_LINEAGE = 60,815
master-run-2 COLUMN_LINEAGE = 60,813   (net -2)
symmetric diff master-vs-master = 1,104 edges
```

The BEFORE-vs-AFTER symmetric diff (1,232 edges, net -10) is the same order of magnitude and
touches 26 complex multi-CTE fact/interface files **none of which contain IDENTIFIER**
(e.g. `wtfe_artikel_inteken_lijst.sql`, `wtfe_kassahandeling.sql`). The PR-A code path is never
entered for those files. The ±10 net is well inside the master-vs-master noise band; no
PR-A-attributable coverage regression was observed (`good_edges_strict`/`good_edges_scoped` both +0).

## Verdict for the gain gate

- No coverage-axis regression attributable to PR-A (strict/scoped both flat; net good_edges
  delta inside the measured ±10 master-vs-master non-determinism floor).
- No positive headline movement on THIS corpus because its IDENTIFIER sites are all dynamic
  (`CURRENT_DATABASE()` concat/subquery), which PR-A correctly leaves for the empty-id drop.
- PR-A's resolution is proven on static-literal sites by the unit + integration suites
  (`tests/unit/test_identifier_var_indirection.py`,
  `tests/integration/snowflake/test_identifier_var_indirection_integration.py`) asserting real
  COLUMN_LINEAGE / SELECTS_FROM edges. A corpus with static-literal SET binds would gain;
  this one has none.
