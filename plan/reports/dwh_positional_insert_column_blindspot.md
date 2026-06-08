# Field Report: Column-Lineage Blind Spot on CLONE/Positional-INSERT Fact Tables

Report date: 2026-06-08
Author: live DWH analysis session (Claude Code)
Source authority: Live investigation against the indexed `dwh` repo (Snowflake dialect)
Trigger: Diagnosing user-reported missing rows in `IA_SEMANTIC."Voorraad bouwmarkt per dag"`

---

## Summary

While tracing the lineage of the semantic view `Voorraad bouwmarkt per dag`, the MCP
column-lineage closure (`get_upstream_dependencies`) returned **empty** at the fact table
`BA.WTFS_VOORRAAD_DAGSTAND`, which initially looked like "no lineage extracted." It is not.
Column lineage *does* exist (48 edges from the fact INSERT alone, plus more from sibling
ETL variants). The closure dead-ended because of a structural blind spot in how sqlcg
catalogs columns for tables that are **created by `CLONE` and loaded by positional
`INSERT ... SELECT` with no column aliases**.

This pattern is not an edge case in this corpus — it is the *standard* fact-table build
pattern in the DWH (see `CLAUDE.md` → "DDL Table Rebuild Pattern" and "DDL Column Backfill
Pattern"). So the blind spot affects a large class of the most important tables in the
graph, not one table.

---

## What was observed

Target view definition (`ddl/changelogs/IA-SEMANTIC/VOORRAAD_BOUWMARKT_PER_DAG.sql`):

```sql
SELECT
    ...
    MA_VRIJE_VRD AS "Voorraad vrij",
    ...
FROM BA.WTFS_VOORRAAD_DAGSTAND
```

Tool calls and results:

| Call | Result |
|---|---|
| `trace_column_lineage("ia_semantic.Voorraad bouwmarkt per dag.Voorraad vrij")` | 1 hop: `ba.wtfs_voorraad_dagstand.ma_vrije_vrd`, confidence 1.0 |
| `get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` | **empty** (depth 0) |
| `find_table_usages("ba.wtfs_voorraad_dagstand")` | **empty** (despite the view selecting from it) |
| `find_definition("ba.wtfs_voorraad_dagstand")` | no `DEFINED_IN`; only producer files |

Direct graph queries (DuckDB backend) resolved the contradiction:

```sql
-- Lineage edges DO exist for the fact:
SELECT query_id, COUNT(*) FROM COLUMN_LINEAGE
WHERE query_id LIKE '%wtfs_voorraad_dagstand%' GROUP BY query_id;
--   etl/.../wtfs_voorraad_dagstand.sql:6        -> 48 edges
--   etl/.../wtfs_voorraad_dagstand_webshop.sql  -> 82 edges
--   etl/.../wtfs_voorraad_dagstand_igdc.sql     -> 59 edges   ... etc.

-- But the fact has NO column catalog at all:
SELECT COUNT(*) FROM SqlColumn WHERE table_name = 'ba.wtfs_voorraad_dagstand';
--   0
SELECT dst_key FROM HAS_COLUMN WHERE dst_key LIKE 'ba.wtfs_voorraad_dagstand.%vrije_vrd%';
--   (none)

-- The edges that DO exist are keyed on the SOURCE column name, not the target:
SELECT dst_key, src_key FROM COLUMN_LINEAGE
WHERE dst_key LIKE 'ba.wtfs_voorraad_dagstand.%';
--   dst = ...mh_vrije_vrd   src = ...tmp_..._enriched.mh_vrije_vrd
--   (no dst = ...ma_vrije_vrd node exists)
```

---

## Root cause

Two conditions hold simultaneously for this table, and together they hide the column:

1. **No `CREATE TABLE (...column list...)` in the indexed repo.** The fact is born via
   `CREATE TABLE IF NOT EXISTS ... CLONE ...` and refilled with `TRUNCATE` +
   positional `INSERT INTO ... SELECT`. There is no authoritative column-list DDL for
   sqlcg to parse, so `SqlColumn`/`HAS_COLUMN` for this table is empty.

2. **Positional `INSERT` with no `AS` aliases.** The load projects `vrd.mh_vrije_vrd`
   (the *historic* `MH_` source measure) into the fact, and the DDL column order maps it
   to the *actual* `MA_VRIJE_VRD` column. The rename is real and fully materialized in
   the database (the view reads `MA_VRIJE_VRD` and compiles in prod; the webshop sibling
   ETL reads `vrd.ma_vrije_vrd` off the fact and writes `som_vrije_vrd AS ma_vrije_vrd`).

Because sqlcg had no target column catalog (condition 1), it fell back to naming the
lineage node after the **source expression** (condition 2). The graph therefore contains
a phantom `ba.wtfs_voorraad_dagstand.mh_vrije_vrd` node and **no** `ma_vrije_vrd` node —
the column that actually exists in the database.

Net effect: a genuine, DB-materialized `MH_ → MA_` rename is invisible to the graph, and
the upstream/downstream closure silently splits at that boundary.

---

## Why this matters for analysis

- **`get_upstream_dependencies` returning empty is ambiguous.** It can mean "no lineage"
  OR "lineage exists under a different, source-derived key." For any table built by the
  CLONE/positional-INSERT pattern, treat an empty closure as *unproven*, not *negative*.

- **The blind spot is systemic in this corpus.** The DWH's documented fact-build patterns
  (rebuild via CLONE + explicit `INSERT`; column backfill via INSERT-from-backup) both
  satisfy condition 1 and usually condition 2. The `WTF*` fact layer — the analytically
  most valuable tables — is the most affected.

- **Cross-layer prefix renames (`MH_`↔`MA_`, `SH_`↔`SN_`/`SA_`) are the common casualty.**
  These prefixes encode SCD/measure semantics (historic vs actual). When the rename is
  positional and unaliased, the graph keeps the source-side prefix, which is not only a
  broken edge but a *semantically mislabeled* node.

- **For this specific investigation**, hand-reading the ETL was more reliable than the
  column closure. The real chain is:
  `ia_semantic."Voorraad bouwmarkt per dag"."Voorraad vrij"`
  ← `BA.WTFS_VOORRAAD_DAGSTAND.MA_VRIJE_VRD` (DB) / `mh_vrije_vrd` (graph node)
  ← `tmp_..._enriched` ← `tmp_..._base` (both `SELECT vrd.*`)
  ← `BA.WTFV_VOORRAAD_DAGSTAND.MH_VRIJE_VRD`.
  Note the `SELECT vrd.*` temp CTEs are a *second*, independent gap (star-expansion isn't
  materialized as per-column edges; tracked via `STAR_SOURCE` instead).

---

## Suggested follow-ups (for sqlcg)

1. **Disambiguate empty closures.** Have `get_upstream_dependencies`/`trace_column_lineage`
   distinguish "table has no column catalog" from "column has no upstream edges," and
   surface a hint when the table was only seen via positional INSERT/CLONE.

2. **Resolve positional INSERT against target DDL when available**, and when not available,
   flag the produced column nodes as `inferred_from_source_name=true` so consumers know the
   key may not match the real column.

3. **Catalog columns from `INSERT` target lists / CLONE sources**, not only from
   `CREATE TABLE (...)`, so CLONE-built facts get a column catalog.

4. **Consider star-source resolution** for `SELECT alias.*` temp tables so chains through
   intermediate `*_base`/`*_enriched` CTEs don't break.

---

---

## v1.6.0 follow-up — reproduction on 2026-06-08

Installed from local `../sql-code-graph` repo (built as 1.6.0) into the project venv and global
`uv tool install`. DB schema bumped v6 → v7; re-indexed the same `dwh` corpus.

### Status per follow-up

**#1 — Disambiguate empty closures**: **Done.**
`get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` now returns a `hint`:

> *"No column catalog for 'ba.wtfs_voorraad_dagstand' (0 catalogued columns). This table was
> likely created via CREATE TABLE … CLONE and/or loaded by a positional INSERT … SELECT with
> no column list, so sqlcg has no authoritative target column names. Lineage edges may still
> exist keyed on a SOURCE-derived column name — check raw COLUMN_LINEAGE for this table.
> Empty here is UNPROVEN, not negative."*

**#2 — Resolve positional INSERT against target DDL**: **Still open.**
`COLUMN_LINEAGE` edges into the fact are still keyed on the source-side name:

```
dst=ba.wtfs_voorraad_dagstand.mh_vrije_vrd  ←  src=tmp_..._enriched.mh_vrije_vrd
```

No `ma_vrije_vrd` dst-edge exists. The `hint` in #1 tells the user to look here, but the
underlying mis-keying is not fixed.

**#3 — Catalog columns from CLONE / INSERT targets**: **Partially done.**
`SqlColumn` now has 63 rows for `ba.wtfs_voorraad_dagstand` (was 0). The indexer logs
*"CLONE inheritance: 1326 column(s) inherited across 34 CLONE target(s)"*.
However `HAS_COLUMN` edges for the table are still empty (`[]`), so the catalog rows are not
wired into the lineage traversal; `get_upstream_dependencies` still reads 0 catalogued columns
and dead-ends.

**#4 — Star-source resolution for `SELECT alias.*` CTEs**: **Broken, not missing.**
`STAR_SOURCE` has 468 edges and star expansion ran (671 expansion edges in `COLUMN_LINEAGE`).
But the expansion produces wrong mappings through the `SELECT vrd.*` CTEs. For example:

```
ba.wtfv_voorraad_dagstand.mh_vrije_vrd  →  ba.tmp_wtfs_voorraad_dagstand_base.ma_voorraad_totaal
```

`mh_vrije_vrd` is being assigned to `ma_voorraad_totaal` — a positional column-order mismatch.
The path `tmp_..._base → tmp_..._enriched.mh_vrije_vrd` that the report identified as a gap is
still missing entirely; no lineage edge lands on `tmp_..._enriched.mh_vrije_vrd`.

Root cause is the same as #2: star expansion needs a target column catalog to map positions
correctly, and for CLONE-built tables that catalog is absent. So star-expansion runs but
produces incorrect positional assignments rather than no edges at all — a silent wrong-answer
failure rather than an obvious gap.

### Net verdict

The full chain `ia_semantic."Voorraad vrij"` ← `BA.WTFS_VOORRAAD_DAGSTAND.MA_VRIJE_VRD` ←
`tmp_..._enriched` ← `tmp_..._base` ← `BA.WTFV_VOORRAAD_DAGSTAND.MH_VRIJE_VRD` remains
broken in the graph. Fix #2 (positional INSERT resolution with target DDL) is the load-bearing
repair; #3 (wiring HAS_COLUMN from CLONE catalog) and #4 (star expansion using that catalog for
correct positional mapping) both depend on it landing first.

---

## Reproduction

```text
# 1. View hop is captured:
trace_column_lineage  ia_semantic.Voorraad bouwmarkt per dag.Voorraad vrij
# 2. Closure dead-ends at the fact:
get_upstream_dependencies  ba.wtfs_voorraad_dagstand.ma_vrije_vrd      -> empty
# 3. But edges exist (keyed on source name) and catalog is empty:
execute_sql  SELECT COUNT(*) FROM SqlColumn WHERE table_name='ba.wtfs_voorraad_dagstand'   -> 0
execute_sql  SELECT dst_key,src_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'ba.wtfs_voorraad_dagstand.%'
```
