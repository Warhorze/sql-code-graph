# First-User E2E Test Report — `ma_rotatie_totaal` lineage
Date: 2026-05-28  
Tester: Claude (acting as first-time user)  
sqlcg version: 0.3.1  
Repo tested: `/home/ignwrad/Projects/dwh` (all SQL files, no information_schema CSV)

---

## Setup summary

| Step | Result |
|------|--------|
| sqlcg binary | 0.3.1 at `/home/ignwrad/.local/bin/sqlcg` |
| `.sqlcg/` before init | Did not exist — clean slate |
| DB init | OK — `~/.sqlcg/graph.db` (schema v2) |
| Index path | `.` (repo root — covers `ddl/`, `etl/sql/`, `etl/pdi/`) |
| Dialect | `snowflake` |
| Total SQL files found | 1,453 |
| Files indexed | **1,340** (113 skipped: pure DDL, auth scripts, etc.) |
| Parse failures | **2** |
| Files with column lineage | 953 |
| Table-only lineage | 318 |
| DDL-only (skipped) | 67 |
| COLUMN_LINEAGE edges | 171,628 (49,146 raw + star expansion) |
| Indexing wall time | ~7 minutes |

---

## Bug found and fixed during test

**`_timed_tool` decorator stripped function names** (`src/sqlcg/server/tools.py`).

The inner `wrapper` function in `_timed_tool` lacked `@functools.wraps(func)`. As a result, every decorated function's `__name__` was `"wrapper"`, and the MCP framework registered all 5 core tools under that name — each clobbering the previous. Only 3 tools were visible to MCP clients (`index_repo`, `wrapper`, `submit_feedback`); `trace_column_lineage`, `find_table_usages`, `get_downstream_dependencies`, `get_upstream_dependencies`, and `search_sql_pattern` were silently lost.

**Fix applied:** added `@functools.wraps(func)` and `import functools` to `tools.py`. Reinstalled from source. All 9 tools now register correctly.

---

## Lineage results for `ma_rotatie_totaal`

The column exists in 10+ tables. Queried two representative persistent tables.

### Upstream — `ba.wtfa_kpi_rotatie_bouwmarkt.ma_rotatie_totaal` (depth 5)

```json
{
  "column": "ba.wtfa_kpi_rotatie_bouwmarkt.ma_rotatie_totaal",
  "lineage": [
    {"name": "\"ma_aantal\"",    "kind": "column", "file": null, "confidence": null},
    {"name": "\"ma_aantal\"",    "kind": "column", "file": null, "confidence": null},
    {"name": "\"sn_artikel_s1\"","kind": "column", "file": null, "confidence": null},
    {"name": "\"sn_artikel_s1\"","kind": "column", "file": null, "confidence": null}
  ],
  "hint": null
}
```

### Upstream — `ba_tmp.wtfe_kpi_rotatie_voorraadlocatie.ma_rotatie_totaal` (depth 5)

```json
{
  "column": "ba_tmp.wtfe_kpi_rotatie_voorraadlocatie.ma_rotatie_totaal",
  "lineage": [
    {"name": "\"MA_ROTATIE_TOTAAL\"", "kind": "column", "file": null, "confidence": null}
  ],
  "hint": null
}
```

### Downstream — both tables

```json
{"nodes": [], "hint": "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0 ..."}
```

Empty for both. Plausible for a terminal fact table, but indistinguishable from a lookup failure (see findings below).

---

## Findings

### F1 — Schema prefix required but not documented
**Severity: High — breaks first-time use**

The tool signature says `"table.column"` or `"catalog.db.table.column"`. The stored column IDs are `schema.table.column` (e.g. `ba.wtfa_kpi_rotatie_bouwmarkt.ma_rotatie_totaal`). Passing just `table.column` silently returns empty lineage with a hint that points at `db info` — not at the missing schema prefix.

A first-time user has no way to know the schema prefix without running a raw Cypher query against SqlColumn nodes. The tool description or the empty-result hint should say: *"Try including the Snowflake schema, e.g. `ba.table_name.column_name`."*

### F2 — Duplicate nodes in upstream result
**Severity: Medium**

`ma_aantal` and `sn_artikel_s1` each appear twice in the result for `wtfa_kpi_rotatie_bouwmarkt`. The BFS `visited` guard prevents re-queuing but not duplicate emission when the same `node_id` arrives on two separate BFS frontier steps before it has been added to `visited`. Dedup should be applied to `lineage` before returning.

### F3 — No source table in lineage nodes
**Severity: High — result is not actionable** — ✅ **FIXED 2026-05-29**

Upstream returns bare column names (`ma_aantal`, `sn_artikel_s1`) with no table and no file. A data analyst cannot act on a column name without knowing which table it belongs to. The `LineageNode` model has a `file` field that is always `null`. At minimum, the node's `id` (which encodes `schema.table.column`) should be surfaced, or `table_name` should be a separate field.

**Fix applied:** `LineageNode` and `DependencyNode` now carry a `table: str | None` field populated from `SqlColumn.table_qualified`. The two dependency queries (`GET_UPSTREAM_DEPENDENCIES`, `GET_DOWNSTREAM_DEPENDENCIES`) now `RETURN ... table_qualified`, and all three traversal tool builders set `table=row["table_qualified"]`. Each node now surfaces `name` (bare column) **and** `table` (`schema.table`). Covered by `test_S2_*` in `tests/unit/test_firstuser_findings.py`.

### F4 — Empty-downstream hint is ambiguous
**Severity: Medium**

The empty-downstream hint text is identical whether (a) the column has no consumers in the indexed SQL, or (b) the lookup failed. These are very different situations. Hint should distinguish: *"No downstream consumers found — this column may be a terminal output. If you expected consumers, check that the consuming files were indexed."*

### F5 — Indexing time over 5-minute budget
**Severity: Low (borderline)** — duplicate-query-node lead ⛔ **not reproduced 2026-05-29**

Measurement (airbnb fixture, 10 files): `MATCH (q:SqlQuery) RETURN q.id, count(*)` →
10 distinct ids, max per-id count = 1, zero ids with `n > 1`. The `query_id = f"{path}:{i}"`
key is unique per statement and `query_rows` is deduped at index time, so there is no
node duplication to fix. The perf budget itself (throughput rewrite) remains out of scope
per the plan; closed as "not reproduced — no code change."

Original note follows.

Indexing 1,340 files took ~7 minutes. The CLAUDE.md budget is 5 minutes for 1,600 files on a laptop. This run was 1,340 files in 7 minutes (~0.31 s/file), putting 1,600 files at ~8 minutes — nearly 2× over budget. Measured on WSL2; native Linux would be faster, but worth tracking.

### F6 — Warnings about `tmp_*` table name collisions
**Severity: Info**

Dozens of warnings like `Table tmp_base already defined in file A — file B will add columns to the union`. This is expected for CTEs named generically across files, but the volume (hundreds) is noisy. These are not errors but may inflate the column union and affect star expansion accuracy.

---

## Would a data analyst understand the output?

**No, not without guidance.**

- The schema-prefix requirement is non-obvious and the error mode is silent.
- The result returns column names with no table context — unusable for navigation.
- Duplicate nodes look like a bug.
- The downstream empty-result hint is indistinguishable from a hard failure.

With fixes to F1 (hint improvement), F2 (dedup), and F3 (include table in node), the output would be meaningfully usable.
