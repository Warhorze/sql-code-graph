# Postmortem: Dialect-Default & Query-Config Fix Batch (v1.13.1 → v1.14.2, 2026-06-11)

> Plan: [`v1.14.0_dialect_and_query_config_fixes.md`](../sprints/v1.14.0_dialect_and_query_config_fixes.md).
> This document records the plan's **Final Step** — the honest post-fix DWH baseline.
> It **supersedes** the "Final measured state" table in
> [`graph_health_sprint_postmortem.md`](graph_health_sprint_postmortem.md): that table
> was measured on the silent ANSI misparse that Fix 1 corrected, so it *understates*
> the graph. The historical table is left unedited per the plan; treat the numbers
> below as the canonical baseline going forward.

## What shipped

| PR | Fix | Version | Merge |
|----|-----|---------|-------|
| [#73](https://github.com/Warhorze/sql-code-graph/pull/73) | Fix 1 — `index`/`reindex`/`watch` default `--dialect` to `"auto"` | v1.13.1 | `1d48baf` |
| [#74](https://github.com/Warhorze/sql-code-graph/pull/74) | Fix 2 — `catalog load` folds schema aliases at load time | v1.13.2 | `f3a5d11` |
| [#75](https://github.com/Warhorze/sql-code-graph/pull/75) | Fix 3 — repo-anchored query-time config (`resolved_repo_root`) | v1.14.0 | `65d84c4` |
| [#78](https://github.com/Warhorze/sql-code-graph/pull/78) | Fix 4a+4b — blindspot ranking excludes `cte`/`derived`; all-noise-filtered notice | v1.14.1 | `2fef3d4` |
| [#79](https://github.com/Warhorze/sql-code-graph/pull/79) | Issue #77 — hook reindex detaches instead of blocking `git checkout` (outside the plan, shipped in the same closeout) | v1.14.2 | `ea8ce1b` |

The common thread: dialect, schema aliases, and noise-filter config now apply
**identically across every entry point** — index time, catalog load, and query time —
regardless of the invoking directory. Every coverage number recorded before v1.13.1
was an ANSI-parse artifact.

## The honest baseline (fresh DWH index, 2026-06-11, v1.14.2)

Measured per the plan's Final Step: `db reset` → bare `sqlcg index /home/ignwrad/Projects/dwh`
(**no `--dialect` flag** — proving Fix 1 end-to-end) → `sqlcg catalog load columns.csv` →
`sqlcg gain`. Full gain output archived at `/tmp/sqlcg_gain_v1142.txt` (regenerate via the
runbook in the graph-health postmortem; the bare-index command now suffices).

| Metric | ANSI default (pre-fix) | Plan reference (`--dialect snowflake`) | **This run (bare index, v1.14.2)** |
|--------|------------------------:|----------------------------------------:|-------------------------------------:|
| Strict edge health | 74% | 83% | **83% (38,508 / 46,493)** |
| Scoped edge health (excl. CTE/derived) | 84% | 97% | **97% (38,508 / 39,865)** |
| Contradicted edges | 688 | 242 | **242 (0.5% of all edges)** |
| Failed files | 146 | 1 | **1** (+1 timed out) |
| Files with column lineage | 824 | 902 | **902** |
| `analyze upstream ba.wtfa_kassatransactie.ma_bedrag_inc` | no results | results | **4 rows, noise-clean, from any cwd** |

The bare-index column matching the explicit-dialect column **is** the acceptance
result: the default path now produces what previously required the flag.

### Additional metrics

- **Corpus**: 1,379 files → 1,954 tables, 40,152 lineage edges; 902 files with column
  lineage, 413 table-only, 63 DDL-only.
- **Index wall time**: 84.6 s (prior baseline 210–256 s; well under the 5-minute
  budget — treat the delta as indicative, not a measured speedup claim).
- **Catalog load**: 144,311 CSV rows in 69.2 s, 0 malformed; 4,862 tables /
  122,815 columns; **71,179 rows folded through schema aliases**
  (`ba_tmp→ba`, `da_tmp→da`, `ean_tmp→ean`) — Fix 2 proven; no `*_tmp`
  phantom tables in the catalogued or blindspot output.
- **Catalogued tables**: 5,524 / 6,225 (89%).
- **Phantom edges**: 9,489 / 46,493 (20%) — confirmed 9,015, contradicted 242,
  unverified 232.
- **Blindspots**: 541 tables; 6 tables cover 80% of bad-edge volume. Top entries are
  real tables (`<unknown>` 656, `ba.tmp_base2` 158, `doorbelasting` 107, …) with **no
  CTE/derived structural names** (`cte_insert`, `final`, …) — Fix 4a proven.
- **Degraded-parse queries**: 92 / 4,863 (2%). Zero-outgoing-lineage write queries:
  1,034 / 2,169.

### Fix-by-fix proof points

1. **Fix 1**: bare `sqlcg index` reproduced the explicit `--dialect snowflake` numbers
   exactly (83/97/242/1/902).
2. **Fix 2**: 71,179 alias-folded catalog rows; zero `*_tmp` phantom-table artifacts.
3. **Fix 3**: the upstream trace above was run from `/tmp` (outside both repos) and
   still applied the dwh's `.sqlcg.toml` noise filter — no `_bck` clone tables.
4. **Fix 4a**: blindspot top-10 contains only real tables, consistent with the scoped
   KPI's `('cte','derived')` exclusion.

## Release hygiene restored

Tagging had lapsed after v1.5.1. Fourteen annotated tags were reconstructed from
master's version-bump history and pushed (v1.6.0 … v1.14.2, each on its master merge
commit). v1.8.0 was intentionally skipped — it only ever existed on an abandoned
branch, never on master.

## Residual gaps / follow-ups

- The structural blindspot levers are unchanged from the graph-health postmortem
  (Liquibase-XML DDL ingestion remains the big one — see
  [`graph_health_sprint_postmortem.md`](graph_health_sprint_postmortem.md) §Residual gaps).
- [#76](https://github.com/Warhorze/sql-code-graph/issues/76) — stale MCP server after
  reinstall; analysed and deliberately parked (staged fix documented in the issue).
- [#77](https://github.com/Warhorze/sql-code-graph/issues/77) — fixed in v1.14.2
  (PR #79); the issue stays open for user verification per house rules.
- Full-test-suite side effect: some benchmark/e2e test overwrites
  [`plan/measurements/sprint_08_*.json`](../measurements/) in place — should write to a
  temp path instead (observed twice during this batch's reviews).
- Next phase: deployment readiness (scope to be defined).
