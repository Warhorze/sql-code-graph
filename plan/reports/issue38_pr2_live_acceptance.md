# Issue #38 PR-2 + PR-1 — Adversarial Live-DWH Acceptance

**Date:** 2026-06-13
**Reviewer:** adversarial acceptance pass (try-to-break)
**Under test:** v1.26.1 (`_real_tables()` CTE-body descent) + v1.26.0 (`cte_source_gap_writes` metric)
**Corpus:** live DWH, 1335 files / 2122 tables, A/B DBs `/tmp/ab_master.duckdb` (pre-PR-2) vs `/tmp/ab_pr2.duckdb` (with PR-2)

## Verdict

PR-2 and PR-1 are **CORRECT and live-verified**. No false edges, no missed remaps, no
dupes, no dropped edges, no parse regressions; the metric and `gain` agree; the new edges
are end-to-end visible via `sqlcg analyze impact`. **One finding** worth a follow-up
ticket: the team's documented residual breakdown is wrong, and there is a real **second
silent gap class** in the residual 110 that PR-2 does not (and should not) address but
that the recursive-CTE "known gap" framing obscures.

## What checked out (evidence)

| Dig-point | Result | Evidence |
|---|---|---|
| Metric re-derivation (7) | PASS | hand-SQL `cte_source_gap_writes`: master **169**, pr2 **110**; `sqlcg gain` reports the same 169 / 110 |
| Net edge delta | +473 (not +438) | `SELECTS_FROM` distinct: master 4871 → pr2 5344. Headline "+438" is stale/understated; direction correct |
| No dropped edges (8) | PASS | `master_SF − pr2_SF = 0`. Zero real edges removed; NET = +473 is pure addition |
| New-edge audit (1) | PASS | sampled 25 of 473 new (src→dst), read each producing `SqlQuery.sql` by eye. Every dst is a real table the CTE body genuinely reads; 0 CTE aliases leaked (`cte`,`T1`,`affected_weeks`,`datums`… all absent from dst set); 0 phantoms (all 167 dst are real `SqlTable` nodes); 0 self-loops (src==dst); 0 self-source (dst==own target_table) |
| Alias remap (2) | PASS | of all 473 new edges, **0** land on a `*_tmp` schema. INSERT targets like `BA_TMP.X` correctly remapped: edge lands on `ba.wtda_datum`, not `ba_tmp.…` |
| Dedup (3) | PASS | `COUNT(*) == COUNT(DISTINCT (src_key,dst_key))` in both DBs; dupes = 0 |
| Parse regression (8b) | PASS | stored `parse_failed` = **20 in both** DBs; 0 new, 0 fixed. PR-2's scope descent broke no parse. (The A/B "qualify_failed 7→9" is an index-log counter, not the stored field — see Finding 2) |
| MCP/CLI consumer surface (6) | PASS | `sqlcg analyze impact ba.wtfa_inkoop`: on master the CTE-wrapped INSERT `wtfe_kpi_inkoop_artikel_voorraadlocatie` is **absent**; on pr2 it appears as an `INSERT` consumer. New edge is genuinely consumable, not inert |
| Health (gain) | flat | strict 84% (45618/54338), scoped 98% (36373/36968), catalogued 88% (5786/6574). HAS_COLUMN sources: information_schema 110249, ddl 19968, usage 6877, star_expansion 4799, clone_inherited 290 |

A representative correct new edge: query `etl/sql/fact/wtfe_energie.sql:4`
(`INSERT INTO BA_TMP.WTFE_ENERGIE WITH VERBRUIK_PER_EAN_DAG_INTERVAL AS (… FROM DA.RTENG_ENGIEBE …)`)
now emits `SELECTS_FROM → da.rteng_engiebe` (canonical, remapped). Absent in master.

## Finding 1 — residual breakdown is mislabeled; a second silent gap class exists

**Symptom.** The documented prior breakdown of the residual gap is *0 recursive / 12
non-recursive WITH / 98 plain INSERT…SELECT*. The first two are right; the "98 plain" is
wrong and hides a real, distinct gap class.

**Reproduction.**
```
# residual population (pr2):
SELECT q.id,q.kind,q.target_table,q.sql FROM "SqlQuery" q
WHERE q.kind IN ('INSERT','MERGE','CREATE_TABLE','UPDATE') AND q.target_table <> ''
  AND EXISTS (SELECT 1 FROM "COLUMN_LINEAGE" cl WHERE cl.query_id=q.id)
  AND NOT EXISTS (SELECT 1 FROM "SELECTS_FROM" sf WHERE sf.src_key=q.id);
-- 110 rows
```
Bucketed: **0 recursive**, **12 non-recursive WITH**, and the remaining 98 split into
**~50 plain INSERT…SELECT *with* a FROM clause** and **~48 with no real source FROM**
(literal/sequence inserts, e.g. `htdyn_salesline_delete.sql:1` = `SELECT NULL AS …`).
(Exact sub-split is regex-sensitive; the load-bearing fact is the existence of the
with-FROM bucket, confirmed below by parse-truth.)

Of the ~50 plain-with-FROM, **~29 read at least one real *persistent* table** in
FROM/JOIN (canonicalised tmp→base, membership-checked against `SqlTable`). Concrete,
parse-verified example — `etl/sql/int/hthyb_ean_pegholes_delete.sql:1`:
```
INSERT INTO da_tmp.HTHYB_EAN_PEGHOLES
SELECT EAN_CODE, … FROM DA.HTHYB_EAN_PEGHOLES AS aa WHERE …
```
- COLUMN_LINEAGE for this query resolves the source as `da.hthyb_ean_pegholes`
  (`inferred_from_source_name = True`).
- SELECTS_FROM for this query is **empty**.

**Observed vs expected.** Column path independently sees `da.hthyb_ean_pegholes`; table
path emits nothing. The two lineage planes disagree — exactly the inconsistency dig-point
5 hunts for. Expected: a `SELECTS_FROM → da.hthyb_ean_pegholes` row (it is a real read,
distinct from the target after tmp→base canonicalisation).

**Pre-existing, NOT a PR-2 regression.** All ~50 plain-with-FROM residuals are *also*
zero-SELECTS_FROM in `/tmp/ab_master.duckdb` (checked: 50/50). PR-2 correctly left them
alone — they are not CTE-body cases. This is a latent gap PR-2's metric now surfaces, not
one it caused.

**Root-cause hypothesis.** This is NOT the `_real_tables` CTE path
([`base.py:604`](../../src/sqlcg/parsers/base.py)) — these have no CTE. The miss is in
the non-CTE table-extraction / SELECTS_FROM emission path for plain INSERT…SELECT whose
FROM table equals the *target* table modulo the `*_tmp` alias (delete/reload patterns:
`INSERT INTO da_tmp.X SELECT … FROM DA.X`). Likely the table is being treated as the
write target (and therefore not emitted as a source), or a same-name source/target
self-reference is being suppressed *before* tmp→base canonicalisation makes them distinct
ids. Needs a focused look at where SELECTS_FROM source rows are filtered against the
INSERT target in [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) around the
`_real_tables` call site (L435) and the target-suppression logic.

**Blast radius.** ~29 write queries across the corpus lose a real table-level source
edge; these are the delete/reload "…_delete.sql" and initial-load templates. Lineage
traces *into* those targets dead-end at the table level (column level is intact). Not
introduced by PR-2; size-bounded by the residual 110.

**Why it matters for #38.** The residual 110 will NOT trend to 0 by fixing recursive
CTEs (there are zero). The dominant remaining classes are (a) plain self-reload
INSERT…SELECT (~29, real edge lost) and (b) sourceless literal inserts (~48, correctly
no source — arguably should be *excluded* from the metric numerator since they have no
FROM at all). The next #38 lever is the plain-INSERT-SELECT self-reload path, not a
recursive-CTE ticket.

## Finding 2 — "qualify_failed 7→9" is not visible in the graph

The A/B headline reports qualify_failed rising 7→9. The **stored** `SqlQuery.parse_failed`
is 20 in both DBs with zero delta (no file newly failing, none fixed). So either the 7→9
is an index-time log counter not persisted to the graph, or it refers to a sub-statement
qualify retry that does not flip `parse_failed`. Not a correctness problem for PR-2 (no
parse regression is observable in the graph), but the "7→9" claim cannot be corroborated
from the DBs and should be re-sourced before it's cited as evidence.

## Recommended follow-ups

1. **New ticket — plain self-reload INSERT…SELECT loses SELECTS_FROM** (~29 queries):
   emit the table-level source for `INSERT INTO s_tmp.X SELECT … FROM s.X` where the
   source canonicalises to a *different* id than the target. Quantify with the metric.
2. **Consider excluding sourceless writes from `cte_source_gap_writes`**: the ~48 no-FROM
   literal inserts have *no* table source by construction; counting them inflates a metric
   whose name implies a missing-but-recoverable source. Or rename to reflect "writes with
   column lineage but no table source" (a superset of the CTE gap).
3. **Correct the documented residual breakdown** in
   [`issue-38-selects-from-island-lever.md`](../sprints/issue-38-selects-from-island-lever.md):
   it is 0 recursive / 12 WITH / ~50 plain-with-FROM / ~48 no-FROM, not 0/12/98.
4. **Re-source the qualify_failed 7→9 figure** or drop it from the A/B headline.

## Not a defect

PR-2 itself is clean: every new edge is a true CTE-body read, every remap is correct, no
dupes, no drops, no parse damage, and the new lineage is visible end-to-end through the
consumer-facing `analyze impact` command. Ship-quality.
