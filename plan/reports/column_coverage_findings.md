# Column Coverage Findings — Systematic Diagnostic

Report date: 2026-06-08
Source: `scripts/column_coverage_check.py` + manual pattern drill-down against the indexed DWH graph
(`~/.sqlcg/graph.db` — snapshot copied to `/tmp/sqlcg_diag.duckdb` for read-only analysis).

---

## Top-line numbers

| Metric | Count | % |
|--------|-------|---|
| SqlTable total | 2,944 | — |
| Tables with ≥3 SqlColumn rows (OK) | 528 | 18% |
| Tables with ZERO SqlColumn rows | 2,416 | 82% |
| COLUMN_LINEAGE edges total | 51,434 | — |
| Edges with `inferred_from_source_name=true` (phantom) | 13,412 | 26% |
| Tables with edges but NO HAS_COLUMN (blindspot) | 1,681 | — |

82% of catalogued tables have no column catalog. 26% of all lineage edges are phantom
(destination node named from source expression, not authoritative target column). These
are not random gaps — they cluster into four distinct SQL patterns described below.

---

## Pattern 1 — CTE/derived node destination leak

**15,102 edges (29% of all edges) land on CTE or derived-query nodes, not real tables.**

- `kind='cte'`: 405 CTE tables, 6,944 edges
- `kind='derived'`: 237 derived-query tables, 8,158 edges

### What the SQL looks like

```sql
-- INSERT target is ba.wtfe_kpi_gemiddelde_voorraad_artikel_voorraadlocatie
-- CTEs defined inline AFTER the column list

INSERT INTO wtfe_kpi_gemiddelde_voorraad_artikel_voorraadlocatie
(  dn_datum, sn_artikel_s1, ... )
WITH cte_datum AS (SELECT ...),
     cte_gemiddelde_voorraad_periode_bm AS (SELECT ...),
     cte_insert AS (
       SELECT ... FROM cte_gemiddelde_voorraad_periode_bm
       UNION
       SELECT ... FROM cte_datum
     )
SELECT dn_datum, sn_artikel_s1, ...
FROM cte_insert
WHERE 1 = 1;
```

File: `etl/sql/fact/wtfe_kpi_gemiddelde_voorraad_artikel_voorraadlocatie.sql`

The lineage destination for all 698 edges in this statement is `cte_insert.col_name`
instead of `ba.wtfe_kpi_gemiddelde_voorraad_artikel_voorraadlocatie.col_name`. The
parser sees `FROM cte_insert` and routes the final hop to the CTE name.

This pattern is widespread: `cte_insert` (698 edges), `final` (322 edges),
`totaal` (235 edges), `base` (112 edges), `cte` (192 edges), etc.

### Root cause

When a CTE is the last SELECT source feeding an `INSERT INTO`, the parser resolves the
destination as the CTE node rather than the actual INSERT target. The real INSERT target
is identified only once (the `INSERT INTO t (col_list)` header), but the SELECT body's
FROM resolves to the CTE name and that becomes the lineage destination node.

### Fix target

Parser must carry the INSERT target through the CTE scope: when an INSERT's SELECT body
`FROM`s a CTE, the destination of that CTE's columns is the INSERT target, not the CTE.
Equivalent to how `base.py`'s `#25 block` resolves column-list INSERTs — the same
destination propagation needs to happen when the final SELECT sources a CTE.

---

## Pattern 2 — Positional INSERT with no column catalog (phantom edges)

**13,412 edges (26%) are `inferred_from_source_name=true` across 304 tables.**

This is the original blindspot: `INSERT INTO t SELECT src.x, src.y FROM src` with no
column list and no DDL column catalog for `t`. The destination node is named from the
SOURCE expression (`src.x`) instead of the real target column. The CLONE fix (Part C)
partially addressed this — 34 CLONE tables now have inherited catalogs (1,326
`clone_inherited` HAS_COLUMN rows). 60 CLONE tables still have no catalog because their
CLONE source is outside the indexed repo (cross-database CLONE — expected degrade path).

The remaining 13,412 phantom edges come from tables whose DDL catalog was never parsed
at all: CTAS tables (no explicit column list), temp tables, and tables whose defining
DDL is in a file the indexer didn't see.

### Fix target

Covered by the existing branch (Parts A2/B/C) for the CLONE + DDL-catalog path.
The residual 13,412 are driven by P3 and P4 below — fixing those reduces this count.

---

## Pattern 3 — Quoted view names with spaces: schema prefix stripped from SqlColumn

**6,045 edges, 232 tables — all 232 tables have ZERO HAS_COLUMN entries.**

### What the SQL looks like

```sql
-- DDL file: ddl/changelogs/IA-SEMANTIC/ODS_WORKAROUND_ARTIKELDIMENSIE_CONCEPT.sql
create or replace view IA_SEMANTIC."ODS Workaround ARTIKEL CONCEPT" as
select
    art.S1_artikel,
    pc.CODE as "DN_ARTIKEL_NUMMER",
    ...
```

### The mismatch

| Location | Value |
|----------|-------|
| `SqlTable.qualified` | `ia_semantic.ods workaround artikel concept` |
| `SqlColumn.table_name` | `ods workaround artikel concept` ← **schema dropped** |
| `HAS_COLUMN.src_key` | `ia_semantic.ods workaround artikel concept` |
| Expected `HAS_COLUMN.dst_key` | `ia_semantic.ods workaround artikel concept.dn_artikel_nummer` |
| Actual `SqlColumn.id` | `ods workaround artikel concept.dn_artikel_nummer` ← **mismatch** |

`SqlTable` is keyed correctly with the full schema-qualified name. `SqlColumn.table_name`
drops the schema prefix for views whose names contain spaces. So `HAS_COLUMN` is never
wired because the `src_key` (`ia_semantic.ods workaround artikel concept`) never matches
any `SqlColumn.table_name` (`ods workaround artikel concept`).

Confirmed for 8 sample tables:

| SqlTable.qualified | SqlColumn.table_name | Cols |
|--------------------|---------------------|------|
| `ia_semantic.ods workaround artikel concept` | `ods workaround artikel concept` | 319 |
| `ia_semantic.artikel tijdsgebonden` | `artikel tijdsgebonden` | 198 |
| `ia_tableau.artikel tijdsgebonden` | `artikel tijdsgebonden` | 198 |
| `ia_semantic.ods workaround artikel` | `ods workaround artikel` | 137 |
| `ia_semantic.kpi supply chain dag ...` | `kpi supply chain dag ...` | 115 |

### Fix target

When a CREATE VIEW/TABLE name is a quoted identifier containing spaces, the parser must
preserve the full schema-qualified name in `SqlColumn.table_name` (and therefore in the
`SqlColumn.id`) so the `HAS_COLUMN` join finds the column rows. This is a normalization
inconsistency in `_extract_defined_columns` / the column-catalog emission path.

---

## Pattern 4 — CTAS tables: no column catalog from `CREATE TABLE … AS SELECT`

**241 CTAS tables confirmed by pattern scan; all have ZERO SqlColumn rows.**

### What the SQL looks like

```sql
-- Direct CTAS
CREATE OR REPLACE TABLE da.htdyn_wmswarehouselocation AS
SELECT locationid, name, inventlocationid FROM da.ttsrc;

-- CTAS with CTE body
CREATE OR REPLACE TEMP TABLE ba_tmp.tmp_wtfs_voorraad_dagstand_igdc_base AS
WITH datum AS (SELECT dn_datum, s1_datum FROM ba.wtda_datum)
SELECT d.dn_datum, v.sn_artikel FROM datum d JOIN ba.wtfv src v ON ...;
```

### Root cause

`_extract_defined_columns` only harvests columns from the `CREATE TABLE (col1 type, col2 type)` 
explicit column list. A CTAS has no such list — the columns come from the SELECT body.
The parser currently emits 0 `defined_columns` for CTAS statements, so `SqlColumn` and
`HAS_COLUMN` are empty.

### Fix target

For `CREATE TABLE t AS SELECT ...`, derive the output column names from the SELECT's
projection list (the aliases / column names of the outermost SELECT). This is statically
available from the parsed AST — no execution needed. CTAS bodies that use `SELECT *`
would still fall through to P1/STAR resolution.

---

## Pattern 5 — USE SCHEMA + unqualified INSERT targets

Embedded in the BARE_UNQUALIFIED group (956 tables). Files that begin with
`use schema BA_TMP;` or `use schema BA;` then reference INSERT targets without schema
prefix. The parser ignores `USE SCHEMA` so the target resolves as a bare name.

Example from `etl/sql/fact/wtfv_prijsvoorstel.sql`:
```sql
use schema BA_TMP;
truncate table WTFV_PRIJSVOORSTEL;       -- resolves to BA_TMP.WTFV_PRIJSVOORSTEL
...
INSERT INTO WTFV_PRIJSVOORSTEL ...       -- should be BA.WTFV_PRIJSVOORSTEL
```
Edges land on `wtfv_prijsvoorstel` (bare) instead of `ba.wtfv_prijsvoorstel`.
This is lower priority than P1–P4 and cannot be safely fixed without resolving
the schema context; catalogued separately.

---

## Pattern 6 — Table aliases (to verify)

Source alias resolution is **working correctly** (0 edges with unresolved src tables).
Every edge's `src_key` resolves to a real `SqlTable.qualified`. The user's concern
about aliases should be verified with a dedicated test to confirm aliases on INSERT
targets (not just SELECT sources) are also handled.

---

## Summary: patterns ranked by edge impact

| # | Pattern | Tables | Edges | Fixable? |
|---|---------|--------|-------|----------|
| P1 | CTE destination leak | 642 | **15,102** | Yes — parser INSERT-target propagation |
| P2 | Positional INSERT phantom | 304 | **13,412** | Partially (CLONE done; CTAS/DDL remain) |
| P3 | Space-name schema strip | 232 | **6,045** | Yes — normalize SqlColumn.table_name |
| P4 | CTAS no column catalog | 241 | unknown | Yes — derive from SELECT body |
| P5 | CLONE cross-DB (degrade) | 60 | — | No — expected, A1/A2 hint is correct |
| P6 | USE SCHEMA bare names | ~N/A | ~N/A | Deferred — needs schema context |

Fix P1, P3, P4 → expect to recover **~35,559 edges** from phantom/dead-end status and
**~1,115 tables** with catalog. P2 residual should collapse once P3/P4 supply catalogs
the phantom edges currently lack.

---

## Next step: integration tests

Write one failing integration test per pattern (P1–P4) that:
1. Uses a minimal self-contained SQL fixture (no DWH files)
2. Indexes through `Indexer().index_repo()`
3. Asserts the expected `COLUMN_LINEAGE` destinations and `HAS_COLUMN` rows
4. Fails today (confirming the gap), passes after the fix

See `tests/integration/test_column_coverage_patterns.py` (to be created).
