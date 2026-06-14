# Issue #38 PR-3 — Adversarial Live-DWH Acceptance

**Date:** 2026-06-13
**Reviewer:** shepherd adversarial acceptance pass (try-to-break)
**Under test:** PR-3 v1.28.0 (`_real_tables()` Step-3 subscope descent via `traverse_scope`)
**Corpus:** live DWH A/B DBs — `/tmp/ab_pr2.duckdb` (master @ v1.26.1, post-PR-2) vs
`/tmp/ab_pr3.duckdb` (with PR-3). 1335 files.

## Verdict

PR-3 is **CORRECT and live-verified**. The headline `cte_source_gap_writes` **110 → 1** is
**genuine source recovery**, not metric-gaming by false edges. Every adversarial probe came
back clean. Mergeable.

## What checked out (evidence)

| Dig-point | Result | Evidence |
|---|---|---|
| Metric re-derivation | PASS | hand-SQL `cte_source_gap_writes`: pr2 **110**, pr3 **1** (matches dev report) |
| Net edge delta | +755 | `SELECTS_FROM`: pr2 5344 → pr3 6094 (= +750 distinct; +755 raw new (src,dst) pairs) |
| Dedup | PASS | `COUNT(*) == COUNT(DISTINCT (src_key,dst_key))` in pr3 (6094==6094); dupes = 0 |
| **False-edge audit (disk truth)** | **PASS** | all **109** writes that moved out of the gap had every new edge-dst table-name **present in its on-disk source file** (266 dst checks, **0 absent**) |
| Phantom dst | PASS | 0 of 755 new dsts are non-`SqlTable` nodes |
| CTE-alias leak | PASS | 0 of 755 new dsts are unqualified (every dst is schema-qualified) |
| Alias remap (`*_tmp`) | PASS | 0 of 755 new dsts land on a `*_tmp` schema |
| Self-source / self-loop | PASS | 0 new edges have `dst == target_table`; the 1 residual is a true self-reload, correctly dropped |
| Hot-path discipline | PASS | `traverse_scope` imported at module level (patchable); called once per statement in `_real_tables`; perf-guard counter added & asserted flat at N/2N |
| Cross-statement bleed | PASS | statements are parsed/scoped individually; `scope.expression.parent` is statement-local — verified no edge sourced a table from a sibling statement |

## Finding — the "~48 sourceless literal inserts" floor is FALSIFIED

The PR-2 acceptance report, the post-PR-2 research doc, GitHub #116, and PR-A's new
`coverage.py` comment all assert that ~48 of the residual writes are **sourceless literal
inserts** (`INSERT … SELECT NULL AS x`) that "can never gain a `SELECTS_FROM` row by
construction." **This is wrong.** Those writes are not sourceless — their real source
tables live in a `FROM`/`WHERE … IN (SELECT … FROM real)` subquery that the stored
`SqlQuery.sql` (**truncated at 500 chars**) cuts off, making them *look* like pure-NULL
projections.

Canonical example — `etl/sql/int/htdyn_salesline_delete.sql:1`:
```sql
INSERT INTO da_tmp.htdyn_salesline
SELECT NULL AS definitiongroup, …, inventtransid, …, partition
  FROM da.htdyn_salesline
 WHERE … AND inventtransid IN (SELECT inventorylotid FROM da.rtdyn_voided_salesline WHERE …)
```
- Stored (truncated) SQL ends mid-projection at char 500, before the `FROM` — looks sourceless.
- The self-reload `da.htdyn_salesline` (FROM) is correctly dropped as a self-loop after
  `da_tmp → da` remap.
- PR-3's subscope descent correctly recovers `da.rtdyn_voided_salesline` from the `IN`
  subquery → one true `SELECTS_FROM` edge. **Correct, not fabricated.**

This is why traverse_scope took the gap to 1 instead of the projected ~48 floor: the floor
did not exist. The lone residual is a *pure* self-reload (`INSERT INTO ba_tmp.X SELECT … FROM
BA.X` with no other source), where the only source equals the target after remap → correctly
no edge.

## Follow-ups (documentation only — no code defect)

1. **Correct the floor narrative.** PR-A's `coverage.py` floor comment, the
   `cte_source_gap_writes` field docstring, and the `[[project_issue38_residual_scope]]`
   memory still cite a "~48 permanent literal-insert floor." After PR-3 the residual is 1.
   PR-A's `inferred_from_source_name = FALSE` filter is still a reasonable guard (excludes
   writes whose *only* column lineage is inferred) and does not conflict with PR-3 — but its
   "~48 floor removed" rationale is overstated and should be reworded to "excludes
   inference-only writes" without the floor magnitude claim.
2. **Stored-SQL truncation (500 chars) is an audit hazard.** Any future false-edge check that
   greps `SqlQuery.sql` for a table/`FROM` keyword will produce false positives. Use the
   on-disk file as truth, or raise/remove the truncation for write queries.

## Not a defect

The 500-char `SqlQuery.sql` truncation initially read as a false-edge smoking gun (a
`SELECT NULL` insert with a `SELECTS_FROM` edge). Disk verification cleared it. Recording so
the next reviewer does not re-raise it.
