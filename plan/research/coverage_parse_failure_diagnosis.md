# Coverage Parse-Failure Diagnosis — E5 & E8 on the DWH

**Date:** 2026-06-13
**Mode:** READ-ONLY investigation. No source fixed, nothing committed, DWH repo untouched.
**Graph:** `/home/ignwrad/.sqlcg/graph.db` (1,337 files; 410 `parse_failed=True`).

## TL;DR

| Code | Meaning | Cluster | Files | Fixability verdict |
|------|---------|---------|-------|--------------------|
| **E5** | `sg_lineage()` raised `Cannot find column '<NAME>'` for a plain identifier | `ddl/changelogs/IA-SEMANTIC` + `IA-TABLEAU` (Liquibase views) | 217 | **ONE fixable pattern** — quoted-identifier case mismatch in view col-list rename. 1 PR could recover ~211 files. |
| **E8** | `sg_lineage()` returned a root but `_lineage_node_to_edges` produced **no edges** (`col_lineage_skip:dynamic_source`) | `etl/sql/fact` + `dim` + `int` (ETL) | 184 | **One dominant pattern (temp-table chains) + a long tail.** Harder; not a clean 1-PR win. |

`parse_failed=True` here means **per-column lineage degradation**, NOT a whole-file parse failure (sqlglot parses these files fine). E4 (`parse_failure`, the real "file won't parse") is essentially absent on this corpus.

---

## 1. What E5 and E8 actually mean (from source, not guessed)

The buckets are defined in [`error_classify.py`](../../src/sqlcg/indexer/error_classify.py) `_classify_error()` (the live classifier) and tabulated in [`parsing_errors_experiment.md`](parsing_errors_experiment.md) §"Error Category Taxonomy". `parse_cause` on the `File` node is the *dominant* bucket over a file's error list (`dominant_cause()`), and `parse_failed` is `True` whenever that dominant bucket is in `_DEGRADING` (E1/E2/E3/E5/E8/timeout/worker_error/func_fallback/qualify_failed).

- **E5** = [`error_classify.py:124-151`](../../src/sqlcg/indexer/error_classify.py#L124): an error string `col_lineage:<col>:Cannot find column '<NAME>'` where `<NAME>` is a **plain identifier** (no parens → not E2, not literally `NULL` → not E1). Doc name `lineage_other`: `sg_lineage()` raised while resolving one projected column. Emitted at [`base.py:1465`](../../src/sqlcg/parsers/base.py#L1465) (`col_lineage:{col_name}:{exc}`), paired with `col_lineage_skip:unknown_sentinel:{col_name}` at [`base.py:1469`](../../src/sqlcg/parsers/base.py#L1469).
- **E8** = [`error_classify.py:121-122`](../../src/sqlcg/indexer/error_classify.py#L121): the skip marker `col_lineage_skip:dynamic_source:<col>`. Doc name `no_edges_from_root`: `sg_lineage()` *succeeded* (returned a root) but `_lineage_node_to_edges()` returned an empty list — the root's leaf sources don't resolve to a known table. Emitted at [`base.py:1452`](../../src/sqlcg/parsers/base.py#L1452).

Key consequence: **both are per-column, not per-file.** A file can emit a handful of E5/E8 column errors and *still* produce its table-level edges and most of its other columns. So `parse_failed=True` overstates damage at the file level and is why these files are not isolated satellites (see §4).

---

## 2. E5 — definition, fixability, snippets

### Cluster (measured)
Of 217 E5 files: **120 `ddl/changelogs/IA-SEMANTIC`, 91 `ddl/changelogs/IA-TABLEAU`, 1 `IA-TABLEAU-OBT`, 1 `ddl/semtex_views.sql`** (= 213 Liquibase view files), plus **4 `etl/sql/int/htdyn_*_delete.sql`** (a different minor case). So E5 is **~98% one subsystem.**

### Root cause (reproduced directly with `SnowflakeParser`, no graph write)
The IA-SEMANTIC / IA-TABLEAU files are T&T-generated Liquibase views of the shape:

```sql
CREATE OR REPLACE VIEW IA_SEMANTIC."Afdeling" (
	"Afdeling koppelcode" COMMENT '...',
	"Afdelingsnummer"     COMMENT '...',
	...
)
AS
	SELECT
		S1_AFDELING AS "Afdeling koppelcode",
		DN_AFDELING AS "Afdelingsnummer",
		...
	FROM BA.WTDA_AFDELING;
```

Reproduced error on `IA-SEMANTIC/AFDELING.sql`:
```
col_lineage:Afdelingsnummer:Cannot find column 'AFDELINGSNUMMER' in query.
```
The view declares output column `"Afdelingsnummer"` (quoted, case-preserved Dutch). The lineage extractor resolves each view output column against the SELECT body, but `sg_lineage` **uppercases the bareword** to `AFDELINGSNUMMER` and then cannot match the case-sensitive quoted alias `"Afdelingsnummer"` → raises → E5.

Confirming detail: in `AFDELING.sql` only `Afdelingsnummer` failed; `"Afdeling koppelcode"`, `"Afdeling code"`, `"Afdeling naam"` all resolved. The failing names are **single-token** identifiers (look bareword-like → get uppercased); names **with spaces** stay quoted and match. Same one mechanism across the cluster — `IA-TABLEAU/ARTIKEL.sql` (a 100+-column view) reproduced **39** such single-token failures (`Artikelnummer`, `Toeleverancier`, `Seizoensartikel`, …).

The 4 `htdyn_*_delete.sql` files are unrelated: `col_lineage:partition:Cannot find column 'PARTITION'` — `PARTITION` used as a column where it's a reserved/window keyword. Tiny tail, ignore for the lever.

### Verdict: **ONE fixable pattern (1-PR win).**
A single fix to how view output-column names are looked up against the SELECT body (preserve quoting / case-fold consistently / match the projection alias by position rather than re-resolving the renamed output name) would recover **~211 files** and the per-column edges for every single-token view column across IA-SEMANTIC + IA-TABLEAU. (Recovery count = *inferred* from the cluster size; the mechanism is *measured*/reproduced.)

---

## 3. E8 — definition, fixability, snippets

### Cluster (measured)
Of 184 E8 files: **79 `etl/sql/fact`, 33 `etl/sql/dim`, 19 `etl/sql/int`, 16 `etl/sql/da`, 13 `etl/sql/interface`, 12 `etl/pdi/template`, 9 `ddl/changelogs/BA-TABLES`, 2 validation.** = ETL transform scripts, not DDL.

### Root cause (reproduced + skip_counts evidence)
The dominant E8 mechanism is the **CREATE TEMP TABLE chain**. Example `etl/sql/fact/wtfa_inkoop.sql` (reproduced: `col_lineage_skip:dynamic_source:TA_ROWID`):

```sql
truncate table WTFA_INKOOP;
create temp table tmp_delta as select SA_INKOOP_ORDER from BA.WTFE_INKOOP_ORDER ...;
create temp table tmp_inkooporder as select ... from BA.WTFE_INKOOP_ORDER ... ;
-- ... final INSERT INTO WTFA_INKOOP SELECT ... FROM tmp_inkooporder ...
```
The final write's lineage root resolves to a **temp table** (`tmp_*`) that has no DDL/`SqlTable` node, so `_lineage_node_to_edges` finds no leaf → empty edge list → `dynamic_source` → E8.

But E8 is **not monolithic**. `skip_counts` on E8 files mixes `dynamic_source` with `star`, `merge_branch`, etc.:
- `WTFE_KORTING.sql`: `{"dynamic_source": 29, "star": 9, "merge_branch": 1}`
- `WTDH_ARTIKEL.sql`: `{"star": 12, "dynamic_source": 1}`
- `WTFS_VOORRAAD_ZONDAGSTAND.sql`: `{"dynamic_source": 4, "star": 2}`

So `dynamic_source` (temp-table / unresolved-source) is the most common driver, but `SELECT *` star-expansion gaps and merge branches ride alongside in the same files.

### Verdict: **One dominant pattern + a long tail (NOT a clean 1-PR win).**
Resolving temp-table chains (treat `CREATE TEMP TABLE x AS SELECT …` as an intermediate node and chain through it, like the existing CTE handling at [`base.py:1471-1478`](../../src/sqlcg/parsers/base.py#L1471)) would knock out the largest E8 slice — but the residual `star`/`merge_branch` columns in the same files mean many files won't flip fully clean from one change. Higher effort, partial-per-file recovery. (Dominance of `dynamic_source` is *measured* via skip_counts; per-file flip rate is *inferred*.)

---

## 4. Do E5/E8 cause the satellites/orphans?

Queried the graph (tables defined in E5/E8 files vs. their incoming edges):

| | E5 | E8 |
|---|---|---|
| Tables defined in cluster files | 224 | 593 |
| With incoming `COLUMN_LINEAGE` | **224 (100%)** | 504 (85%) |
| Appear as `SELECTS_FROM` dst | 0 | 457 |
| **Isolated (no incoming col-lineage)** | **0** | **89** |

**Finding (measured): E5 does NOT create satellites.** Every E5 view table still has incoming column lineage — the failure is partial (the single-token renamed columns), not total. Fixing E5 improves **column-level completeness inside already-connected views**, it does not reconnect islands.

**E8 is the satellite driver:** ~**89 tables** defined in E8 files have *no* incoming column lineage — these are the temp-table-chain write targets whose source never resolved. Against the reported ~147 small islands / zero-edge tables, E8 plausibly accounts for **~60% (≈89/147)** of them. (89 is *measured*; the 147 denominator is from the task brief, so the 60% ratio is *inferred*.) The remaining islands are attributable to other causes (not E5/E8 files) and were not enumerated here.

---

## 5. The misleading index-summary metric (file:line)

The summary line ("`Indexed N files — … 1 timed out, 1 failed`") is built in [`index.py:417-444`](../../src/sqlcg/cli/commands/index.py#L417):

- `n_timeout = err.get("timeout", 0)` ([index.py:425](../../src/sqlcg/cli/commands/index.py#L425)) → "**N timed out**" = `error_summary["timeout"]`, the count of `timeout:` **error strings**, populated at [`indexer.py:715-719`](../../src/sqlcg/indexer/indexer.py#L715). On the DWH that's **1**.
- `n_failed = quality.get("failed", 0)` ([index.py:424](../../src/sqlcg/cli/commands/index.py#L424)) → "**N failed**" = `nonlocal_counts["quality"]["failed"]`, which is incremented **only when `_build_file_rows` throws an exception** at [`indexer.py:1634-1641`](../../src/sqlcg/indexer/indexer.py#L1641). That's a row-*construction* crash — a totally different event from a lineage degradation. On the DWH that's **1**.

The other quality buckets (`full` / `table_only` / `scripting_fallback`) come from `parsed.parse_quality` ([indexer.py:1255](../../src/sqlcg/indexer/indexer.py#L1255), [indexer.py:1648](../../src/sqlcg/indexer/indexer.py#L1648)) — a per-file enum that is independent of E-code degradation.

**What it counts wrong:** *nothing* in the summary reads `File.parse_failed` / `parse_cause`. A file with 39 E5 column errors still has `parse_quality = full` or `table_only` (it emitted its table edges, no row-build crash), so it lands in the green/yellow buckets and is invisible to "failed". The summary reports **2** problem files while **410** are flagged `parse_failed=True`.

**Fix shape (not applied):** surface the `error_summary` degrading-bucket total — `error_summary` is already in the result dict at [`indexer.py:740`](../../src/sqlcg/indexer/indexer.py#L740) — e.g. print `sum(error_summary[c] for c in degrading)` or the count of `parse_failed=True` files, alongside (not replacing) the existing quality line.

---

## 6. Recommendation — what moves coverage most

**Fix E5 first.** It is the clean, high-yield, low-risk lever:
1. **One root cause** (view-output-column case/quoting mismatch in the lineage lookup), **reproduced**.
2. **~211 files** in two homogeneous Liquibase directories; the rest of E5 is a 4-file unrelated tail.
3. Low blast radius — it only changes how renamed view columns resolve; the table edges already exist, so there's little risk of regressing connected lineage.
4. Expected recovery: **~211 files flip off `parse_failed`** and the single-token renamed columns in every IA-SEMANTIC/IA-TABLEAU view gain proper column lineage.

**E8 second**, scoped to the `dynamic_source` / temp-table-chain sub-pattern: model `CREATE TEMP TABLE … AS SELECT` as an intermediate lineage node (mirror the existing CTE-chaining at [`base.py:1471`](../../src/sqlcg/parsers/base.py#L1471)). This is the only change that reconnects the **~89 isolated island tables**, but it's higher effort and many E8 files won't fully clear (residual `star`/`merge_branch`). Treat as a separate PR after E5.

**Also fix the summary metric** (cheap, §5): print the real `parse_failed` / degrading total so "410 degraded" stops hiding behind "1 failed". Do this regardless — it's a one-line reporting change with no parser risk.

**Do NOT chase the timeout angle** — only 1 file (`etl/sql/dim/wtdh_artikel.sql`) times out. The brief's premise (E5+E8 are the lever, not timeouts) is confirmed.
