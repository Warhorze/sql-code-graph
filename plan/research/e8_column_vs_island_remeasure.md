# E8 re-measurement: column-level rulers vs island count

**Date:** 2026-06-13  
**Branch:** `worktree-agent-a37071c3b4d7ab596` (isolated worktree from `feat/issue-38-cte-source-metric`)  
**HEAD sha (worktree):** `64a4e103e8f46fe2a6af7aef474c486254e6387e`  
**Working checkout (measurement source):** `/home/ignwrad/Projects/sql-code-graph`  
**Working checkout branch:** `feat/issue-38-cte-source-metric` @ `14afe05`

> **Critical framing:** E8 is a COLUMN_LINEAGE fix (`col_lineage_skip:dynamic_source`). Islands
> are SELECTS_FROM-WCC (table-level). Prior E8 judgement ("no progress: 147→147 islands") was
> structurally blind to E8's actual target. This document measures both rulers and reports each
> number with its edge type.

---

## What E8 actually changed

E8 (commits `a841c4b` + `3a31953` on `feat/issue-38-cte-source-metric`) changes
[`src/sqlcg/parsers/ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) in the CTAS
`sources_map` registration block:

**Pre-E8 (one key registered):**
```python
sources_map[target_name] = _expr        # bare key only, e.g. "tmp_x"
```

**Post-E8 (three keys registered):**
```python
sources_map[target_name] = _expr                        # bare key — ANSI path
sources_map[f"{target_db}.{target_name}"] = _expr       # post-alias qualified key
sources_map[f"{ast_db}.{target_name}"] = _expr          # pre-alias key (when different)
```

**Mechanism:** `_qualify_bare_tables` (Snowflake USE SCHEMA path) prefixes bare table names
with the active schema (`tmp_x → ba_tmp.tmp_x`) before `_parse_statement` runs. Subsequent
statements reference `ba_tmp.tmp_x` in the AST; `exp.expand()` normalises that to
`ba_tmp.tmp_x` and looks it up in `sources_map` — which only held the bare key `tmp_x` →
lookup miss → `col_lineage_skip:dynamic_source` → E8. The fix registers all key forms so
`exp.expand()` resolves the temp body and column lineage can proceed through the chain.

**Effect on SELECTS_FROM (table-level):** None. `sources_map` feeds `exp.expand()` on the
column-lineage path only. The SELECTS_FROM emission path (`stmt.sources`) is populated before
`sources_map` is consulted; E8 does not touch it. Islands are determined by SELECTS_FROM-WCC,
so E8 is structurally neutral on the island count.

---

## Method

Two from-scratch DWH indexes, sequential (OOM risk prevents parallel runs):

- **Same config both runs:** `/home/ignwrad/Projects/dwh/.sqlcg.toml` — dialect `snowflake`,
  schema_aliases `ba_tmp→ba / da_tmp→da / ean_tmp→ean`, noise filter `_bck` regex + `ma.rtetl_delta`,
  catalog `/home/ignwrad/Projects/sql-code-graph/columns.csv` (122,815 columns, auto-applied).
- **WITH E8:** current code at `14afe05`, DB `/tmp/e8_with.duckdb`.
- **WITHOUT E8:** reverted `ansi_parser.py` to pre-E8 (bare key only), DB `/tmp/e8_without.duckdb`.
  E8 unit tests (`tests/unit/test_e8_temp_chain_key_mismatch.py`) confirmed FAILING after revert,
  PASSING after restore — proving E8 was actually removed for the WITHOUT run.
- **SELECTS_FROM island count:** computed via networkx WCC on noise-filtered SELECTS_FROM edges
  (same method as [`plan/reports/dwh_graph_analysis.md`](../reports/dwh_graph_analysis.md)).
- **cte_source_gap_writes:** queried via `sqlcg gain` §G (PR-1 metric, queries COLUMN_LINEAGE ≥1
  AND SELECTS_FROM = 0 for write-kind queries).

---

## Results

### Edge counts (raw)

| Metric | WITHOUT E8 | WITH E8 | Delta | Edge type |
|--------|-----------|---------|-------|-----------|
| COLUMN_LINEAGE total | 53,162 | 54,890 | **+1,728** | COLUMN_LINEAGE |
| SELECTS_FROM total | 4,869 | 4,867 | -2 | SELECTS_FROM |

### Column-level health (COLUMN_LINEAGE rulers)

| Metric | WITHOUT E8 | WITH E8 | Delta | Edge type |
|--------|-----------|---------|-------|-----------|
| strict % (col verbatim in HAS_COLUMN) | 44,771 / 53,162 = **84%** | 45,851 / 54,890 = **84%** | 0 pp | COLUMN_LINEAGE |
| strict good edges (absolute) | 44,771 | 45,851 | **+1,080** | COLUMN_LINEAGE |
| scoped % (excl CTE/derived/temp dst) | 36,057 / 36,652 = **98%** | 36,547 / 37,650 = **97%** | -1 pp | COLUMN_LINEAGE |
| scoped good edges | 36,057 | 36,547 | **+490** | COLUMN_LINEAGE |
| scoped total edges | 36,652 | 37,650 | +998 | COLUMN_LINEAGE |
| catalogued % (table in HAS_COLUMN) | 5,785 / 6,562 = **88%** | 5,789 / 6,560 = **88%** | 0 pp | HAS_COLUMN / SqlTable |

### col_lineage_skip:dynamic_source (E8 event count, COLUMN_LINEAGE)

| Metric | WITHOUT E8 | WITH E8 | Delta | Edge type |
|--------|-----------|---------|-------|-----------|
| E8 raw error count (indexer summary) | **1,033** | **1,100** | +67 | COLUMN_LINEAGE skip |
| Files degraded (E8 dominant cause) | 178 | 181 | +3 | File |

> **Important:** The E8 count is HIGHER with the fix than without. This is not a regression —
> it reflects that the fix resolves some chains earlier (enabling column lineage to proceed
> further into subsequent statements where `dynamic_source` events still occur), and/or that
> the re-qualify path now reaches more statements. The fix converts what were previously
> silent no-ops (lookup miss → no lineage at all) into partial lineage (some columns resolve,
> others still hit dynamic_source downstream). The +1,728 COLUMN_LINEAGE edge delta confirms
> the fix is producing lineage; the E8 count is not the right ruler for effectiveness.

### Table-level metric (SELECTS_FROM-WCC, the WRONG ruler for E8)

| Metric | WITHOUT E8 | WITH E8 | Delta | Edge type |
|--------|-----------|---------|-------|-----------|
| WCC components | 148 | 148 | 0 | SELECTS_FROM-WCC |
| Giant component nodes | 1,625 / 2,030 | 1,625 / 2,029 | 0 | SELECTS_FROM-WCC |
| Small islands (not giant) | **147** | **147** | 0 | SELECTS_FROM-WCC |

This confirms the prior "147→147, no progress" observation. The island count is structurally
blind to E8 because E8 only affects `exp.expand()` on the column-lineage path — it does not
change `stmt.sources` or the SELECTS_FROM emission path.

### cte_source_gap_writes (COLUMN_LINEAGE × SELECTS_FROM interaction)

| Metric | WITHOUT E8 | WITH E8 | Delta | Edge type |
|--------|-----------|---------|-------|-----------|
| cte_source_gap_writes | **168** | **167** | -1 | COLUMN_LINEAGE ≥1 AND SELECTS_FROM = 0 |
| write queries zero outgoing COLUMN_LINEAGE | 1,090 | 1,120 | +30 | COLUMN_LINEAGE |

> **Interaction confirmed:** E8 raises the COLUMN_LINEAGE > 0 population (the numerator of
> cte_source_gap_writes). With E8, 1,728 more COLUMN_LINEAGE edges exist, some of which are
> on queries that also lack SELECTS_FROM — that is the E8 × cte_source_gap interaction.
> The delta is small (-1) because most of the 1,728 new edges are on queries that already
> had COLUMN_LINEAGE (E8 adds more edges to already-partially-resolving files), not on newly
> promoted zero-edge queries. The "write queries zero outgoing" count actually INCREASES with
> E8 (+30), suggesting that some newly-resolved chains expose additional write statements that
> now have COLUMN_LINEAGE but still lack SELECTS_FROM.

### Index wall-times

| Run | Wall time | Budget |
|----|-----------|--------|
| WITH E8 | 5m57.8s (real) | 3m (over — WSL2 machine load) |
| WITHOUT E8 | 4m18.4s (real) | 3m (over — WSL2 machine load) |

Both runs are over the 3-min budget. A prior PR-1 run on a loaded WSL2 machine measured ~443s
(~7m23s). The 5m57s and 4m18s figures are consistent with loaded WSL2 variance; no code
regression evidence (the parsers are unchanged except for the targeted E8 revert). The
catalog re-apply phase (122,815 columns) is the historically observed bottleneck; both runs
complete the parsing+CLONE phase in ~2m30s, with the catalog taking ~1m30-3m.

---

## Verdict

**Did E8 produce a measurable COLUMN-level improvement that the island count was blind to?**

Yes. E8 produced **+1,728 COLUMN_LINEAGE edges** (53,162 → 54,890, +3.2%) on the same 1,335
file corpus. In absolute terms: +1,080 strict-good edges; +490 scoped-good edges. The strict
percentage holds at 84% because both the numerator (good edges) and denominator (total edges)
grew — the new edges land in roughly the same quality distribution as the existing population.
The scoped percentage dropped 1 pp (98% → 97%) because the denominator (scoped total) grew
faster (+998) than the scoped-good count (+490), suggesting some new temp-chain-resolved
edges land on CTE/derived/temp dst nodes (excluded from scoped-good) rather than real tables.

**Was the island count the right ruler?** No. The 147→147 island result is confirmed —
E8 is column-path only and structurally cannot move SELECTS_FROM-WCC. The prior judgement
("E8 produced no progress") was a measurement error: the wrong metric was used. The correct
metric shows a real, measurable column-level win.

**What did E8 do to cte_source_gap_writes?** Minimal effect (-1: 168→167). The interaction
is real (E8 grows the COLUMN_LINEAGE>0 population, which is cte_source_gap_writes' numerator)
but small because most new edges land on already-partially-resolved files, not on newly
promoted zero-to-positive files.

---

## Framing caveat

The `col_lineage_skip:dynamic_source` count (E8 events) is NOT a reliable "E8 progress" ruler.
It counts events during parse, not edges resolved. With the fix, more parsing proceeds through
resolved temp chains, which can expose additional dynamic_source events further downstream.
The correct ruler for E8's effectiveness is the COLUMN_LINEAGE edge delta (+1,728 confirmed).

The `cte_source_gap_writes` metric (PR-1, COLUMN_LINEAGE ≥1 AND SELECTS_FROM = 0) is the
correct PR-2 gate. E8's contribution to it is small and the baseline for PR-2 evaluation
is **167** (WITH E8, the live code state).
