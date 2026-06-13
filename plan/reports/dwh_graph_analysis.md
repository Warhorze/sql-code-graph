# DWH Lineage Graph Analysis (descriptive, read-only)

Date: 2026-06-13 · Analyst: graph-analytics pass over the real Intergamma DIY-store DWH.
This is a DESCRIPTIVE analysis. Nothing in the warehouse or the DWH repo was modified.

---

## Reproduction (exact — a later acceptance agent reuses this indexed graph)

```bash
# 1. Install the built wheel as a CLI tool
uv tool install --force /home/ignwrad/Projects/sql-code-graph/dist/sql_code_graph-1.25.0-py3-none-any.whl
sqlcg version          # -> 1.25.0
which sqlcg            # -> /home/ignwrad/.local/bin/sqlcg

# 2. Clean from-scratch index from inside the DWH repo
cd /home/ignwrad/Projects/dwh
sqlcg db init          # -> Database initialised at /home/ignwrad/.sqlcg/graph.db (schema v8)
sqlcg index . --dialect snowflake
```

- **DuckDB graph file:** `/home/ignwrad/.sqlcg/graph.db` (schema v8, the global default — NOT a per-repo `.sqlcg/` dir; `/home/ignwrad/Projects/dwh/.sqlcg/` holds only `config.json`)
- **Index wall-time:** **2:44 (164 s)** — within the documented 210-256 s budget; not a regression.
- **Config used** (`/home/ignwrad/Projects/dwh/.sqlcg.toml`): dialect `snowflake`; schema_aliases `ba_tmp→ba, da_tmp→da, ean_tmp→ean`; noise filter regex `_bck` + ignored `ma.rtetl_delta`; catalog `columns.csv` (auto-applied, 122,815 columns).
- **Indexed sha:** `fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6`
- Index summary: `1335 files — 2122 tables, 40340 edges; 956 with column lineage, 323 table-only, 55 DDL-only, 1 timed out (etl/sql/dim/wtdh_artikel.sql), 1 failed`.

### `sqlcg gain` health table (verbatim)

```
G. Coverage
  Tables with catalog: 5785 / 6571 (88%)
  Edge health (strict, column-level): 43807 / 52514 (83%)
  Edge health (table-level, legacy): 44344 / 52514 (84%)
  Edge health (scoped, excl. CTE/derived/temp): 35092 / 35688 (98%)
  Phantom edges: 12603 / 52514 (24%)
    confirmed: 12002  contradicted: 413 (0.8% of all edges)  unverified: 188
  Blindspot tables: 637
    15 table(s) cover 80% of bad-edge volume
    Top blindspot tables (by bad-edge count):
      ba.wtfa_kpi_datum_klant: 70 / ba.wtfe_kpi_datum_klant: 60
      ba.wtfs_voorraad_dagstand: 52 / ba.wtfs_voorraad_zondagstand: 52
      da.rtgmd_postnl_customer_usage: 43 / da.rtgmd_postnl_athome_3s: 33
      wtfe_kpi_voorraad_artikel_voorraadlocatie: 32 / da.rtgmd_postnl_facturen: 30
      da.hthyb_pages: 23 / da.rtgmd_dhb_extra_kosten: 19
  Corpus: 1335 files, db_path=/home/ignwrad/.sqlcg/graph.db
  Write queries with zero outgoing lineage: 1090 / 2349
  Rescuable unqualified edges: 86
```

### How the table-level graph was built (evidence contract)

Table-level edges are **not** stored as `INSERTS_INTO`/`DECLARES` rows (those rel tables are empty: count=0). The write target lives in `SqlQuery.target_table`. A table-level edge `source → target` is therefore:

```sql
SELECT DISTINCT sf.dst_key AS src, q.target_table AS tgt
FROM SELECTS_FROM sf JOIN SqlQuery q ON q.id = sf.src_key
WHERE q.target_table <> '' AND q.target_table <> sf.dst_key;
```

`STAR_SOURCE`-derived edges (381) are a strict subset of the `SELECTS_FROM` set (union unchanged at 3,106). After applying the `.sqlcg.toml` noise filter (`_bck` regex, `ma.rtetl_delta`), the analysis graph is:

**2,030 nodes · 2,930 directed edges** (NetworkX `DiGraph`).

Queried directly against `/home/ignwrad/.sqlcg/graph.db` via `uv run --with duckdb --with networkx --with scipy --with python-louvain python`.

---

## Lens 1 — Centrality (VALIDATED against ground truth)

**Ground-truth check first.** The user's known hubs are "verkoop_info" and "artikel". The DWH names them with prefixes; matched nodes:

| Ground truth | Graph node | Degree rank | Total degree (in/out) |
|---|---|---|---|
| verkoop_info | `ba.wtfe_verkoopinfo` | **#6** | 57 (in 7 / out 50) |
| artikel | `ba.wtdh_artikel` | **#3** | 107 (in 1 / out 106) |

Both land in the degree top-6 as **fan-out hubs** (huge out-degree = consumed warehouse-wide). **Degree centrality is therefore trustworthy** — it independently reproduced both known hubs.

Caveat on PageRank: PageRank ranks them low (`wtdh_artikel` PR-rank #1422) because PR rewards *being a sink of many paths*, and these are *sources*. For "what breaks the most if this table is wrong", **out-degree / total-degree is the right metric here, not PageRank.** Reported accordingly.

### Top hubs by total degree (the trustworthy metric)

```
120 in=1   out=119  ba.wtdh_bouwmarkt          <- biggest fan-out: the store dimension
115 in=2   out=113  ba.wtda_formule            <- banner/formula dimension
107 in=1   out=106  ba.wtdh_artikel            <- GROUND TRUTH (article master)
 73 in=0   out=73   ba.wtda_datum              <- date dimension (pure source leaf)
 60 in=5   out=55   ba.wtdh_artikel_cm_segment
 57 in=7   out=50   ba.wtfe_verkoopinfo        <- GROUND TRUTH (sales fact)
 48 in=4   out=44   ba.wtdh_artikel_cm_subgroep
 45 in=1   out=44   ba.wtda_artikel
 37 in=3   out=34   ba.wtdh_artikel_cm_groep
 36 in=3   out=33   ba.wtda_week
```

**Plain language:** the warehouse is anchored on a handful of conformed dimensions — store (`wtdh_bouwmarkt`), banner (`wtda_formule`), article (`wtdh_artikel`), date/week — exactly as you'd expect of a retail star schema. Sales (`wtfe_verkoopinfo`) is the top *fact* hub.

### Surprising high-centrality tables (you would NOT have guessed these)

- **`da.ttint_verkooptransactie`** — PageRank **#4**, degree #13, **in=25/out=5**. A staging/integration table that is the single biggest *convergence point* (25 inbound). It is upstream of sales, promotions and transactions. **BUSINESS** finding: a quiet integration table is a top hub.
- **`ba.wtfe_korting`** (discounts) — **PageRank #1**, in=11/out=7. Discount logic is far more central than its name suggests.
- **`ba.wtfi_axi_ail`** and **`ba.wtfe_artikel_inteken_lijst`** (article-intake list) — PageRank #2 / #3. The intake-list pipeline is a dense hub, not a leaf report.

### Bridge tables = single points of failure (highest betweenness)

These sit on the most shortest-paths; if they fail, the warehouse **splits** into disconnected halves:

```
0.00372 in=7  out=50  ba.wtfe_verkoopinfo              <- #1 bridge AND a known hub
0.00340 in=25 out=5   da.ttint_verkooptransactie       <- transaction integration bridge
0.00337 ...           tmp_verkoopinfo_verrijkt (in verkoopinfo.sql)
0.00175 in=12 out=7   ba.wtfe_promotie_artikel_bouwmarkt  <- bridges promotions <-> sales <-> store
0.00125 ...           tmp_vrd_surrograat (ttint_voorraad_dagstand.sql)
0.00120 in=2  out=18  ba.wtfv_voorraad_dagstand        <- inventory daily-snapshot bridge
```

**Plain language — the three named single-points-of-failure:** `wtfe_verkoopinfo` (sales) and `ttint_verkooptransactie` (transaction integration) are the load-bearing bridges of the whole warehouse; `wtfe_promotie_artikel_bouwmarkt` is the bridge that ties promotions, articles and stores together; `wtfv_voorraad_dagstand` is the inventory bridge. An outage in any of these fragments lineage across subject areas.

---

## Lens 2 — Communities (Louvain)

**171 communities, modularity 0.763** (strong, well-separated structure). Communities map cleanly onto retail subject areas — hypothesis confirmed. Top 10 (BUSINESS):

| # | Size | Subject area (from members) | Anchor tables |
|---|---|---|---|
| 5 | 199 | **Sales / article / verkoopinfo** | `wtdh_artikel`, `wtdh_artikel_cm_segment`, `wtfe_verkoopinfo` |
| 4 | 147 | **Store + HR/workforce** (bouwmarkt + werknemer/employee/uren/contract) | `wtdh_bouwmarkt`, `wtdh_contract` |
| 14 | 147 | **Promotions / intake-list** (promotie/ail/prognose) | `wtda_promotie`, `wtfe_artikel_inteken_lijst`, `ttint_verkooptransactie` |
| 3 | 132 | **Discounts / POS** (korting/kassatransactie) | `wtfe_korting`, `wtfa_kassatransactie` |
| 23 | 131 | **Pricing / competitor** (formule/concurrent/prijsvoorstel) | `wtda_formule`, `wtda_artikel` |
| 13 | 125 | **Purchasing** (inkoop/order/factuur/leverancier) | `wtda_leverancier`, `wtfe_inkoop_*` |
| 33 | 118 | **IGDC channel / inventory** (igdc/voorraad/order) | `wtfe_verkoop_order_igdc` |
| 15 | 110 | **Inventory / planogram / presentation-variant** | `wtfv_presentatievariant`, `wtfv_voorraad_dagstand` |
| 20 | 94 | **Budget / forecast** (budget/segment) | `wtda_week`, `wtfe_budget_*` |
| 32 | 81 | **Webshop / loyalty / prediction** | `wtfv_webshop_orderregel`, `wtfe_loyalty` |

### Anomalies (the interesting part)

- **`ba.wtfe_loyalty` sits in the Webshop community (#32), not a customer/CRM community.** Name suggests loyalty/CRM; edges place it with webshop orders + predictions. CANDIDATE for mis-placement or a genuine "loyalty is computed inside webshop" coupling.
- **HR/workforce (werknemer, employee, uren, contract) is fused into the *store* community (#4)** rather than standing alone. Plain language: staffing data has no independent lineage island — it only exists as it hangs off the store dimension. Could be BUSINESS (HR is modelled per store) or a sign HR lineage is thin.
- **Cross-community bridge edges** concentrate on the betweenness tables from Lens 1: `wtfe_promotie_artikel_bouwmarkt` stitches Promotions(#14)↔Sales(#5)↔Store(#4); `ttint_verkooptransactie` stitches Promotions↔Sales. These cross-community edges are where a refactor or outage has the widest blast radius.

---

## Lens 3 — Components & orphans

```
weakly-connected components: 148
giant component: 1,625 / 2,030 nodes (80% of the connected graph)
component-size histogram: {2:86, 3:34, 4:18, 5:2, 6:3, 7:2, 8:1, 9:1, 1625:1}
```

**Plain language:** 80% of all tables that have any lineage edge live in one big connected mass. The remaining ~400 nodes form 147 small islands (mostly 2-4 tables).

### Small-component classification (2-4 node islands)

Cross-referenced each island member's `defined_in_file` against `File.parse_failed`:

- **29 islands = COVERAGE** — a member's defining file parse-failed, so the island is a parse gap, not a real isolate.
- **109 islands = BUSINESS candidate** — clean-parse defining files; genuinely small standalone pipelines.

### The bigger orphan story: tables with ZERO table-level lineage edges

```
SqlTable nodes (filtered): 6,447
... that appear in a table-level lineage edge: 2,030
... that NEVER appear in any table-level edge: 4,417
   of which: kind=table 3,812 | cte 530 (within-query artifacts) | temp 73 | view 1
   with a defining DDL file: 90  | reference-only (no DDL node): 4,327
   neither source nor target of any edge ("fully untouched"): 4,168
```

**Tagging the 4,417:**
- **530 CTE nodes — neither (within-query artifacts, expected; NOT orphans).** Discard.
- **90 with a defining DDL file:** **33 COVERAGE** (defining file parse-failed) + **57 BUSINESS** (clean-parse standalone DDL, e.g. `ia_outbound.doorbelasting`, `ia_outbound.nzc_inkoop`, `ma.macon_delta`, `ma.melog_datafusion_statistics` — genuine standalone/outbound tables).
- **~3,800 reference-only tables (no DDL captured), schema split `ba` 1,625 / `da` 737 / `ia_analytics` 711 / `ia_*` exports ~550:** **COVERAGE-dominant.** These are mostly tables referenced in SELECTs whose *write query never resolved a target* — this is the same population as the `gain` finding **"Write queries with zero outgoing lineage: 1,090 / 2,349"** and the **404/1,335 parse-failed files**. The `ia_analytics`/`ia_tableau`/`ia_businessobjects`/`ia_semantic` schemas are downstream **export mirrors / consumers**, not dead tables.

**Discipline note (do NOT let coverage masquerade as "dead tables"):** the headline is NOT "4,000 dead tables". It is "~4,000 tables lack a captured table-level lineage edge, and that population is dominated by parse-failure coverage gaps (404 failed files) + targetless write queries (1,090) + downstream export mirrors — not by genuinely standalone business tables." Only **~57 clean-parse standalone DDL tables** are confidently BUSINESS isolates.

---

## Lens 4 — Hub-hypothesis instrumentation (CANDIDATES only)

**Method (topology only):** for each table compute its set of upstream LEAF (base) tables; cluster by Jaccard similarity of those leaf sets; surface high-similarity groups (J ≥ 0.6) that share **no direct parent** = candidates for a conformed intermediate that doesn't yet exist.

```
leaf (base) tables: many; nodes with >=2 upstream leaves analysed
candidate clusters (J>=0.6, no shared direct parent): 63 clusters, 830 tables
physical-only (excl. ia_* export mirrors & ::temp) pairs: 374
```

**Two structural caveats, stated up front (anti-fabrication):**
1. The two largest clusters (251 and 116 tables) are **over-merged by transitive chaining** through generic shared leaves (`da.ttdiv_date`, `da.ttdiv_week`). They are NOT one duplicated-logic group — discard them; trust the small coherent clusters.
2. Many high-Jaccard groups are **export mirrors** (`ia_tableau.*`, `ia_semantic.*`, `ia_businessobjects.*`, `ia_analytics.*` copies of one physical table). Same leaves because they ARE the same table re-exported — not a refactor target.

### Ranked physical-to-physical CANDIDATES (topology + cheap column signal)

| Rank | Candidate group (short names) | J | Shared leaves | Shared cols | Read |
|---|---|---|---|---|---|
| 1 | `wtfv_voorraad_dagstand` · `ttint_voorraad_dagstand` · `wtfv_voorraad_dagstand_initial_load` | 1.0 | 33 | 33/33 | **Strongest.** Inventory daily-snapshot derived 3× from identical inputs with identical columns. |
| 2 | `wtfs_voorraad_dagstand` · `wtfs_voorraad_zondagstand` | 1.0 | 39 | **71** | Daily vs Sunday inventory snapshot, 71 shared columns. Also the #3/#4 blindspot tables in `gain`. |
| 3 | `wtfs_voorraad_dagstand_igdc` · `wtfv_voorraad_dagstand_igdc` | 1.0 | 20 | 32 | IGDC-channel inventory, fs/fv split from same leaves. |
| 4 | `wtfv_presentatievariant` · `wtfs_presentatievariant` | 1.0 | 8 | 20 | Presentation-variant computed twice (fv/fs prefix). |
| 5 | `wtfe_inkoop_order` · `wtfe_inkoop_ontvangst` · `wtfe_inkoop_pakbon` | ≥0.6 | — | 19 / 19 | Purchasing trio share 19 columns each; candidate conformed purchasing intermediate. |
| 6 | `wtfv_telopdracht` · `wtfs_telopdracht` | 1.0 | 6 | 8 | Count-order fv/fs split. |
| 7 | `wtfv_gevaarlijke_stoffen…voorraad` · `wtfs_…voorraad` | 1.0 | 36 | 19 | Hazardous-materials inventory, fv/fs split. |

**Recurring signature:** a `wtfv_` (fact-view) table and a `wtfs_` (fact-snapshot) table with **the same base tables and near-identical columns**. This is the user's hypothesis made visible — the same derivation appears to run on both sides of an fv/fs prefix split. This is the deliverable that justifies a tier-2 "refactor advisor" investigation.

### CANDIDATEs explicitly NOT to trust without tier-2 (false-positive proof)

`wtfi_manus_export <> wtfe_verkoopinfo_backup_us26608`: **J=1.0 (50 shared leaves) but only 5 shared columns of 84.** Same inputs, totally different output — proving topology alone cannot confirm duplicated *logic*. Every candidate above must clear tier-2.

### Confirming evidence a future tier-2 pass needs (per the brief)

For each candidate group, before any "merge these" recommendation:
- **(a) SQL expression comparison** — prove the *logic* (not just the inputs) is duplicated. Same upstream leaves ≠ same transform (see the `manus_export` false positive). Compare the actual SELECT projections / WHERE / join logic per `SqlQuery.sql`.
- **(b) Shared grain / PK** — a conformed hub is only legal if the candidates share a grain. **Not available in the graph today:** there is no PK/grain node, and `.sqlcg.toml` carries none.

**Cheap signal captured NOW:**
- **Column-name overlap IS available** (`SqlColumn.col_name` per `table_qualified`) and was computed above — it is the single best cheap discriminator (ranks 1-2 show 33-71 shared columns = strong; the false positive shows 5 = weak).
- **Catalog columns ARE loaded** (`columns.csv`, 122,815 rows, 88% of tables catalogued) — gives column *names and types* for a richer tier-2 comparison.
- **Missing:** PK/uniqueness/grain metadata, and any expression-level diff. Those are the gap a tier-2 pass must fill.

---

## Tier-2 hub-candidate backlog (ranked, for the refactor-advisor decision)

1. **Inventory daily-snapshot family** (`*voorraad_dagstand*` — ranks 1-3 above). Highest column overlap (33-71), spans plain/initial-load/igdc variants. Tier-2: diff the SQL of `wtfv_` vs `wtfs_` vs `ttint_` snapshot builders; confirm shared grain (article × store × day).
2. **fv/fs prefix-split pairs** (presentatievariant, telopdracht, gevaarlijke_stoffen). Systematic pattern — one tier-2 SQL diff may explain the whole class. If the fv/fs split is mechanical, a single shared intermediate could collapse many pairs.
3. **Purchasing trio** (`inkoop_order`/`inkoop_ontvangst`/`inkoop_pakbon`, 19 shared cols). Tier-2: are order/receipt/packing-slip re-deriving a common purchasing base?
4. **Export-mirror consolidation (lower priority, different fix):** the `ia_tableau`/`ia_semantic`/`ia_businessobjects`/`ia_analytics` quads are not logic duplication — they are publish targets. A "publish once, expose many" view layer is a separate, lower-risk lever.

**What this list does NOT claim:** it does not say "merge these". Every group is topology + column-name evidence only; the `manus_export` false positive proves why SQL-expression comparison (a) and grain (b) are mandatory before any prescriptive recommendation.

---

## Method limitations / honesty

- Table-level edges are reconstructed from `SELECTS_FROM` + `SqlQuery.target_table` (the rel tables `INSERTS_INTO`/`DECLARES`/`UPDATES`/`DELETES_FROM` are empty in this schema). Queries with no resolved target produce no edge — this *undercounts* lineage and is the main driver of the Lens-3 orphan population.
- Betweenness/PageRank computed on the 2,030-node filtered DiGraph; absolute float values are only meaningful as a ranking.
- Louvain run with `random_state=42` on the undirected projection; community ids are not stable across seeds but the subject-area groupings are.
- 404/1,335 files parse-failed and 1 timed out — coverage is incomplete; every "isolate"/"orphan" was tagged BUSINESS vs COVERAGE rather than asserted as dead.
