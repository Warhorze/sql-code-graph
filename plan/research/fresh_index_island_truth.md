# Fresh-index island truth — DWH at current master (v1.28.4)

Date: 2026-06-14 · Analyst pass over the live Intergamma DIY-store DWH.
DESCRIPTIVE + viz-regeneration. The DWH repo and warehouse were not modified.
All counts below are **measured now** against a fresh `~/.sqlcg/graph.db` unless tagged *(inferred)*.

---

## Step 1 — fresh full index

The stale `~/.sqlcg/graph.db` was schema **v8**; this build requires **v9** — `index` refused
to run against it (`Database schema is v8; this build requires v9`). That alone proves the store
was stale. Stale DB backed up to `~/.sqlcg/graph.db.stale-cd1b43cc.bak`, then `db reset && db init`
(schema v9) and a clean full index.

- Command: `uv run sqlcg index /home/ignwrad/Projects/dwh --dialect snowflake --profile`
- **Wall-time: 2:50 (170 s)** — within the eyeballed ~3-min budget on this 3.8 GB box.
- **Profile (CPU work, the budget that matters):** pass1 parse 13.12 s · pass2 resolve 9.67 s ·
  upsert 55.86 s · star-expand 4.87 s · **total 83.52 s** · 62.6 ms/file (1335 files). The wall/CPU
  gap is I/O thrash, not a regression.
- Index summary: 1335 files — 2122 tables, 41129 edges; 956 column-lineage, 323 table-only,
  55 DDL-only, 1 timeout (`etl/sql/dim/wtdh_artikel.sql`), 1 failed; 189 degraded.
- Indexed sha: `fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6`.

### Freshness proof — SELECTS_FROM

| | SELECTS_FROM | SqlQuery | STAR_SOURCE |
|---|---|---|---|
| STALE (cd1b43cc, pre-#38 PR-2/PR-3) | **4871** | 5183 | — |
| FRESH (v1.28.4) | **6094** | 5186 | 505 |

6094 matches the expected ~6094 → index is fresh, post-#38.

### `gain` health (fresh)

- Tables with catalog: 5794 / 6577 (88%)
- Edge health — strict (column) 46156/54736 (84%) · table-level (legacy) 46675/54736 (85%) ·
  **scoped (excl CTE/derived/temp) 36593/54736 ... 36593/37147 (99%)**
- Phantom edges 12435/54736 (23%) — confirmed 11895, contradicted 352 (0.6%), unverified 188
- Blindspot tables: 626
- **Catalog load confirmed present** (a missing load would zero this): `columns.csv` re-applied,
  122,815 columns. HAS_COLUMN source breakdown: information_schema 110261 · ddl 19950 ·
  usage 6909 · star_expansion 5244 · clone_inherited 290.

---

## Q1 — IA-DATAPRODUCTS: NOT a fresh-index bug, a deliberate `.sqlcgignore` exclusion (HEADLINE)

Measured against the fresh DB:

| Metric | Value |
|---|---|
| `SqlQuery` rows where `file_path LIKE '%IA-DATAPRODUCTS%'` | **0** |
| `File` rows for that dir | **0** |
| of which parse_failed | n/a (never indexed) |
| SELECTS_FROM edges originating there | **0** |
| `ia_dataproducts.*` nodes present (from catalog only) | 124, all kind=`table`, all `defined_in_file=''` |

The two sample data-products are present as **catalog-only orphan nodes** with **no producing
query, no inbound source edge, zero degree**:
- `ia_dataproducts.agg_conversie_week_bouwmarkt_vorig_jaar` — producing query 0, inbound edges 0
- `ia_dataproducts.agg_transactie_datum_artikel_bouwmarkt_vorig_jaar` — producing query 0, inbound edges 0

**Root cause (pinpointed, file:line):** the DWH repo ships
`/home/ignwrad/Projects/dwh/.sqlcgignore` whose sole pattern is:

```
ddl/changelogs/IA-DATAPRODUCTS/
```

The indexer's file walk honours it:
- discovery entry: `src/sqlcg/indexer/indexer.py:469` → `walk_sql_files(path, spec, use_git)`
- per-file filter: `src/sqlcg/indexer/walker.py:50` `if path.exists() and not is_ignored(path, root, spec)`
- spec load: `src/sqlcg/utils/ignore.py:8-23`; match: `src/sqlcg/utils/ignore.py:26-38` (gitwildmatch)

All 113 git-tracked `IA-DATAPRODUCTS/*.sql` match the directory pattern and are dropped before
parsing. This is **drop-at-discovery, not target-resolution / statement-persistence**, and it is
**by design**, not staleness. The `.sqlcgignore` comment justifies it: COMBI/AGG dynamic tables
have 100+ columns × 10+ FULL OUTER JOINs → O(columns×joins) sqlglot cost, "excluded until
subprocess isolation is implemented (ARCHITECTURE_REVIEW.md § 14.5)."

**Caveat on that justification:** the comment claims "all source tables are absent from schema
(E5), so zero lineage edges are produced anyway." That is only partly true. A representative file
(`AGG_CONVERSIE_WEEK_BOUWMARKT_VORIG_JAAR.sql`) reads `FROM IA_SEMANTIC."Conversie"` /
`IA_SEMANTIC."Datum"` — **`ia_semantic` IS fully indexed (187 files)**, so un-ignoring would yield
at least a table-level `ia_semantic.* → ia_dataproducts.*` edge (the quoted-identifier columns may
still E5, but the table-level FROM is resolvable).

**VERDICT:** the dynamic-table data-products are **genuinely orphaned on a fresh index because
their DDL is never indexed** (`.sqlcgignore`), not because the indexer fails to resolve them. This
is a *deliberate-exclusion / coverage* gap, accurately filable as: "IA-DATAPRODUCTS dynamic tables
excluded by `.sqlcgignore` pending subprocess isolation (#14.5); their `ia_semantic` sources are
in-corpus, so they would connect if the perf concern were solved."

---

## Q2 — True island count (fresh vs stale)

Islands computed as small weakly-connected components over the query-mediated table-level
projection (`SELECTS_FROM.dst_key → SqlQuery.target_table`), with the `.sqlcg.toml` noise filter
(`_bck` regex + `ma.rtetl_delta`) — the methodology of `plan/reports/dwh_graph_analysis.md`.

| | WCC count | giant | small islands |
|---|---|---|---|
| STALE report (cd1b43cc) | 148 | 1625 | **147** |
| FRESH (v1.28.4), filtered | 44 | **2015** | **43** |
| FRESH, unfiltered | 44 | 2122 | 43 |

Fresh size hist (filtered, excl giant): `{2:22, 3:15, 4:3, 6:1, 7:1, 8:1}`. The giant grew
1625→2015 and small islands collapsed 147→43 — the #38 PR-2/PR-3 connectivity work.

### %artikel% / %bouwmarkt% REAL tables (kind table/view)

154 such real tables in the graph: **149 in the giant component, only 5 islanded.** The 5 form a
single chain around one missing producer:

| islanded table | kind | defining file | classification |
|---|---|---|---|
| `ba.wtfa_artikel_formule_dagomzet` | table | (none in graph) | **REAL EXTRACTION GAP** (see below) |
| `ia_analytics.ba_wtfa_artikel_formule_dagomzet` | view | IA-ANALYTICS/FULL_SELECT_BA_VIEWS.sql | downstream of the gap |
| `ia_semantic.artikel formule dagomzet` | view | IA-SEMANTIC/ARTIKEL_FORMULE_DAGOMZET.sql | downstream of the gap |
| `ia_tableau.artikel formule dagomzet` | view | IA-TABLEAU/ARTIKEL_FORMULE_DAGOMZET.sql | downstream of the gap |
| `analytics_workbench.test_informatic.voorraad bouwmarkt per dag` | view | ddl/semtex_views.sql | unrelated scratch/test view |

**The lever — `ba.wtfa_artikel_formule_dagomzet` is a real extraction gap (not staleness, not
backup noise).** Its file
`/home/ignwrad/Projects/dwh/ddl/changelogs/BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql`
IS indexed (`File` row exists, parse_failed=False) **but yields 0 SqlQuery rows and 0 edges**,
because the `CREATE OR REPLACE DYNAMIC TABLE ... AS WITH ... FROM ba.wtfe_verkoopinfo ...` lives
inside a Snowflake-scripting dynamic-SQL **string literal**:

```sql
DECLARE ddl_statement STRING;
BEGIN
  ddl_statement := 'CREATE OR REPLACE DYNAMIC TABLE BA.WTFA_ARTIKEL_FORMULE_DAGOMZET ( ... )
                    ... AS WITH datum AS (SELECT ... from ba.wtda_datum ...)
                    , artikel_verkoop_bouwmarkt AS (SELECT ... FROM ba.wtfe_verkoopinfo JOIN datum d ...)
                    SELECT * FROM artikel_verkoop_bouwmarkt ORDER BY 1';
  EXECUTE IMMEDIATE (:ddl_statement);
END;
```

file: `.../BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql` — the embedded CREATE/SELECT spans
roughly lines 13–46 inside the quoted `ddl_statement`. The parser sees a string, not SQL, so the
real sources `ba.wtfe_verkoopinfo` (the #6 sales hub) and `ba.wtda_datum` (date dim) produce no
edge → this table and its 3 IA downstream views float as an island. **SQL pattern to handle:**
`ddl_statement := '<DDL>'; EXECUTE IMMEDIATE` (dynamic-SQL / EXECUTE IMMEDIATE blindspot).
Resolving it would pull a 4-node island into the giant and connect a sales-derived dynamic table.

---

## Q3 — Backup/snapshot noise (issue #27a)

Across the **unfiltered** projection there are **123 `_eenmalig_` / `_bck` nodes** (the `_bck`
ones are exactly what the `.sqlcg.toml` `ignore_table_regexes=["_bck"]` filter removes). Of those,
**only 4 are actually in islands**:
- `ba.wtfe_net_promoter_score_web_bck46617`
- `da.rtint_netail_concurrentie_prijs_statistieken_bck_us23212`
- `da.rtnps_web_score_bck46617`
- `da.ttprp_abcde_labels_bck_us32363`

After applying the `_bck` noise filter, **0 island tables match backup/snapshot/timestamp
patterns.** Conclusion for #27a: backup/snapshot clones are NOT a meaningful inflator of the
*lineage* island count — the filter already neutralises them. They DO inflate the *eyeballed viz*
(see Step 3) only as part of the deg-0 singleton mass, where the viz currently keeps `_bck` nodes.

---

## Step 3 — regenerated viz (`table_graph.html`)

No in-repo generator exists for the `DATA` block (the `viz_color_by_schema` sprint explicitly notes
"the DATA generation step, which we do not have"). The block was faithfully reproduced from the
fresh DB, matching the existing node schema (`id, kind, schema, deg, cat, jobs`) and link schema
(`source, target`): nodes = all `table`/`view` SqlTable rows plus `ref-only` link endpoints with no
definition; `schema`=`db`; `deg`=undirected link degree; `cat`=has catalog columns; `jobs`=defining
file basenames; links = the query-mediated table-level projection (unfiltered, matching the old
viz which kept `_bck`). Only the DATA literal on line 56 was replaced; all force-graph JS and the
schema-color logic on disk were preserved. No uncommitted color edits were applied (none were
present on disk). `node --check` passes on the regenerated inline script.

### Before/after the maintainer will SEE in the viz

| | nodes | links | giant comp | singleton islands (deg-0) | small multi-node islands |
|---|---|---|---|---|---|
| OLD (stale) | 5349 | 1779 | 1121 | 3883 | 126 |
| NEW (fresh) | 6020 | 4098 | **2122** | 3772 | **43** |

- **Links +130% (1779→4098)**, **giant component +89% (1121→2122)**, **small multi-node islands
  −66% (126→43).** The "many islands" impression is dominated by ~3,700 deg-0 catalog singletons
  (un-lineaged tables + the 124 `ia_dataproducts` catalog orphans), which the viz still renders.
  The artikel/bouwmarkt clustering specifically tightened: only the one EXECUTE-IMMEDIATE gap
  cluster (4 nodes) remains islanded among %artikel%/%bouwmarkt% reals.

---

## What is measured vs inferred

- Measured now: every count in Q1/Q2/Q3, the SELECTS_FROM/gain numbers, the island deltas, the
  `.sqlcgignore` content and the EXECUTE-IMMEDIATE file body.
- Inferred: that un-ignoring IA-DATAPRODUCTS would add `ia_semantic→ia_dataproducts` edges (based
  on reading one representative file; column-level may still E5 on quoted identifiers).

## Filable issues (accurate)

1. **IA-DATAPRODUCTS coverage gap (deliberate):** 113 dynamic-table files excluded by
   `dwh/.sqlcgignore` → 124 orphan catalog nodes, 0 edges. Sources (`ia_semantic`) ARE indexed;
   the exclusion is a perf workaround (O(cols×joins), ARCHITECTURE_REVIEW §14.5). Revisit once
   subprocess isolation lands.
2. **EXECUTE IMMEDIATE / dynamic-SQL blindspot:** `ddl_statement := '<CREATE...AS SELECT...>';
   EXECUTE IMMEDIATE` blocks parse clean at the outer level but extract 0 inner queries/edges.
   Confirmed on `BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql`; islands a sales-derived
   dynamic table + 3 IA views.
