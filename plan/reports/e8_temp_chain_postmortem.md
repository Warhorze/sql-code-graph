# Postmortem — E8 temp-chain fix (PR-3) under-delivered vs projection

Date: 2026-06-13. Status: **PR #111 parked (open, unmerged)**. Sprint
`plan/sprints/coverage_parse_failures.md` shipped PR-1 (metric, v1.25.6) + PR-2 (E5, v1.25.7);
**PR-3 (E8) was NOT merged** — this records why.

## What E8 was supposed to do
Diagnosis (`plan/research/coverage_parse_failure_diagnosis.md`) projected E8 as the **island driver**:
"89 of its 593 defined tables have zero incoming column lineage (temp-chain targets), ≈60% of the
~147 islands." Plan acceptance: reconnect ~89 islands; E8 degraded-file count 178 → its
`dynamic_source` floor.

## What it actually did (measured live, post-E8 graph vs frozen pre-E8 `/tmp/graph_before_e8.db`)

| graph | metric | before | after | Δ |
|---|---|---|---|---|
| **Table-level** (`SELECTS_FROM → target_table`; the T3 "147 islands" baseline & what a viz shows) | small islands | 147 | 147 | **0** |
| **Column-level** (`COLUMN_LINEAGE`, collapsed to tables; what E8 actually touches) | small islands | 100 | 89 | **−11** |
| Column-level | giant component | 2473 | 2504 | +31 |
| Column-level | `COLUMN_LINEAGE` rows | 53,571 | 55,193 | **+1,622** |
| `gain` | strict resolved | 44,865 | 45,962 | +1,097 |
| `gain` | strict % | 84% | **83%** | −1pp |
| index summary | E8 degraded files | 178 | 182 | +4 |
| Column-level | table-nodes | 2,783 | 2,737 | −46 |
| Column-level | tables w/ incoming | 2,307 | 2,282 | −25 |

**Net:** ~11 column-level islands reconnected (not ~89), +1,097 resolved edges, giant +31 — a real
but ~8× smaller win than projected, and the headline strict% ratio *dipped*.

## Root cause of the over-projection — a methodology conflation in the diagnosis
The "147 islands" baseline was computed on the **`SELECTS_FROM` table-level** graph. The "~89 islands"
estimate was derived from **`COLUMN_LINEAGE` incoming-edge** counts. **These are two different graphs.**
The diagnosis cross-applied a column-level "89 tables with zero incoming column lineage" as "≈60% of
the 147 [table-level] islands" — an apples-to-oranges ratio. E8 operates on `COLUMN_LINEAGE` (temp-body
inlining), so it cannot move the `SELECTS_FROM` table-level component structure at all (temp tables were
always `SELECTS_FROM` nodes). On the graph it *does* affect (column-level), the real reconnection was −11.

## Secondary finding — the strict% dip is denominator growth, not lost resolution
E8's temp-body inlining **surfaces more candidate column edges** (`COLUMN_LINEAGE` +1,622) than it
resolves (+1,097). Lineage *completeness* improves (more true edges present); the resolved *ratio* dips
84%→83% because the newly-surfaced edges aren't all resolved. For a "coverage sprint" this is an awkward
optic even though absolute resolved lineage went up.

## Open question (unverified — must clear before any future merge)
The column graph shows **−46 table-nodes** and **−25 tables-with-incoming**. Likely benign temp-node
consolidation from inlining, but it *could* hide a few tables that lost their only edge. Not investigated
before parking. Any revival of PR #111 must confirm no table lost lineage.

## Decision
PR #111 parked unmerged. The code is correct (3 review concerns answered: qualified-key deviation
justified for the Snowflake `USE SCHEMA` path, v1.21.1 footgun guard proven intact, perf counter added
not weakened; gates green 979/14-perf/21-namespacing). It was **not worth merging**: ~11-island gain +
1pp strict regression do not justify the hot-path 3-key `sources_map` registration complexity.

## Recommendations
1. **Fix the diagnosis methodology debt:** any island/orphan estimate must state its edge type
   (`SELECTS_FROM` vs `COLUMN_LINEAGE`) and never cross-apply a column-level count to a table-level
   baseline. This conflation also produced the earlier `ia_*` "zero-edge export mirror" over-attribution
   (see [[project_dwh_graph_analysis_findings]] / `ARCHITECTURE_REVIEW.md §6.4`).
2. **If E8 is revived:** define success as column-level reconnection + verify zero edge loss (the −25),
   and weigh +1,097 edges / 11 islands against the parser complexity explicitly.
3. **The table-level island lever is elsewhere** — the 147 `SELECTS_FROM` islands are NOT E8 temp-chains;
   a separate investigation is needed to find what actually disconnects them.
4. The PR-1 per-cause metric and E5 (real column-coverage win, +1,058 edges, shipped 1.25.7) stand —
   the sprint was not a loss, only PR-3 was descoped.
