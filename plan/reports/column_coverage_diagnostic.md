# Column Coverage Diagnostic — Systematic Approach

Report date: 2026-06-08
Purpose: Replace per-symptom whack-a-mole with a measurement-first diagnosis.
Instead of chasing individual tool hints, we enumerate every DDL-defined table,
measure how many of its columns are catalogued in the graph, and for each column
how many lineage edges exist — then group the zero-coverage tables by SQL pattern
to find which parser paths are systematically missing.

---

## The Script: `scripts/column_coverage_check.py`

Single self-contained script. Reads an indexed DuckDB graph, joins DDL facts against
the graph, and prints a structured coverage report.

### Inputs

```
uv run python scripts/column_coverage_check.py \
    --db  <path/to/sqlcg.duckdb>   \   # indexed graph DB
    --sql <path/to/sql/repo>        \   # the original SQL source repo (for pattern scan)
    [--min-columns 1]               \   # skip trivial views (default: 1)
    [--out coverage_report.json]        # optional JSON dump
```

### Output sections

**Section 1 — Per-table column coverage**

For every table that appears in `SqlTable`:
- `ddl_cols`: number of columns in `SqlColumn` with `source='ddl'`
- `inherited_cols`: number with `source='clone_inherited'`
- `total_cols`: `ddl_cols + inherited_cols`
- `flag`: `ZERO_COLS` when `total_cols == 0`, `PARTIAL` when > 0 but < expected,
  `OK` otherwise

Flag `ZERO_COLS` is the primary signal — it means the parser never catalogued this
table at all. `PARTIAL` needs a threshold; for now we flag when `total_cols < 3`
(an arbitrary floor — adjust after first run).

**Section 2 — Per-column lineage coverage**

For every column in `SqlColumn` (any `source`):
- `upstream_count`: `COLUMN_LINEAGE` rows where `dst_key = col.full_key`
- `downstream_count`: `COLUMN_LINEAGE` rows where `src_key = col.full_key`
- `inferred`: whether at least one edge landing on this column has
  `inferred_from_source_name=true` (the phantom-name signal)
- `flag`: `DEAD_END` when both counts are 0 and the table is not a known source
  (no upstream AND no downstream = this column is isolated in the graph)

**Section 3 — Zero-coverage table pattern scan**

For each `ZERO_COLS` table, search the source SQL repo for the table name and
classify the defining SQL pattern:
- `CLONE`: `CREATE TABLE … CLONE …` (no column list, no body)
- `CTAS`: `CREATE TABLE … AS SELECT …` (body, no explicit column list)
- `PLAIN_DDL`: `CREATE TABLE … (col1 …)` (should never be ZERO_COLS — bug)
- `INSERT_ONLY`: table appears only in `INSERT INTO` statements, never in `CREATE`
- `EXTERNAL/TEMP`: `CREATE EXTERNAL TABLE`, `CREATE TEMP TABLE`, etc.
- `NOT_FOUND`: the table name appears in the graph but not in the source repo
  (cross-database reference, or stale graph)

This section is the diagnostic payoff: group `ZERO_COLS` tables by pattern, count
each group. The largest group(s) name the parser path(s) to fix.

**Section 4 — Summary**

```
Tables total:              N
  ZERO_COLS:               n  (n%)
  PARTIAL:                 n  (n%)
  OK:                      n  (n%)

ZERO_COLS by pattern:
  CLONE:                   n
  CTAS:                    n
  INSERT_ONLY:             n
  PLAIN_DDL (bug!):        n
  EXTERNAL/TEMP:           n
  NOT_FOUND:               n

Columns total:             N
  with upstream lineage:   n  (n%)
  with downstream lineage: n  (n%)
  DEAD_END (both 0):       n  (n%)

Inferred-name edges total: n  (phantom column nodes)
```

---

## Implementation sketch

```python
# scripts/column_coverage_check.py

import argparse, json, re, sys
from pathlib import Path
from collections import defaultdict

# --- arg parse ---
# --db, --sql, --min-columns, --out

# --- connect ---
# import duckdb; con = duckdb.connect(db_path, read_only=True)

# --- Section 1: per-table column coverage ---
# SELECT t.table_name, COUNT(c.col_name) FILTER (WHERE c.source='ddl') AS ddl_cols,
#        COUNT(c.col_name) FILTER (WHERE c.source='clone_inherited') AS inherited_cols
# FROM SqlTable t
# LEFT JOIN SqlColumn c ON c.table_name = t.table_name
# GROUP BY t.table_name

# --- Section 2: per-column lineage counts ---
# SELECT c.table_name, c.col_name,
#        COUNT(DISTINCT l_up.query_id) FILTER (WHERE l_up.dst_key = c.table_name||'.'||c.col_name) AS upstream,
#        COUNT(DISTINCT l_dn.query_id) FILTER (WHERE l_dn.src_key = c.table_name||'.'||c.col_name) AS downstream,
#        BOOL_OR(l_up.inferred_from_source_name) AS inferred
# FROM SqlColumn c
# LEFT JOIN COLUMN_LINEAGE l_up ON l_up.dst_key = c.table_name||'.'||c.col_name
# LEFT JOIN COLUMN_LINEAGE l_dn ON l_dn.src_key = c.table_name||'.'||c.col_name
# GROUP BY c.table_name, c.col_name

# --- Section 3: pattern scan for ZERO_COLS tables ---
# For each table_name in ZERO_COLS:
#   grep sql_repo for CREATE ... <table_name>
#   classify by regex: CLONE / CTAS / PLAIN_DDL / INSERT_ONLY / EXTERNAL / NOT_FOUND

# --- Section 4: summary + optional JSON dump ---
```

---

## Queries (exact DuckDB SQL, ready to paste)

### S1 — table coverage
```sql
SELECT
    t.table_name,
    COUNT(c.col_name) FILTER (WHERE c.source = 'ddl')             AS ddl_cols,
    COUNT(c.col_name) FILTER (WHERE c.source = 'clone_inherited') AS inherited_cols,
    COUNT(c.col_name)                                              AS total_cols,
    CASE
        WHEN COUNT(c.col_name) = 0 THEN 'ZERO_COLS'
        WHEN COUNT(c.col_name) < 3 THEN 'PARTIAL'
        ELSE 'OK'
    END AS flag
FROM SqlTable t
LEFT JOIN SqlColumn c ON c.table_name = t.table_name
GROUP BY t.table_name
ORDER BY flag DESC, total_cols ASC;
```

### S2 — column lineage coverage
```sql
SELECT
    c.table_name,
    c.col_name,
    c.source,
    COUNT(DISTINCT lu.src_key)                        AS upstream_count,
    COUNT(DISTINCT ld.dst_key)                        AS downstream_count,
    BOOL_OR(lu.inferred_from_source_name)             AS has_inferred_upstream,
    CASE
        WHEN COUNT(DISTINCT lu.src_key) = 0
         AND COUNT(DISTINCT ld.dst_key) = 0 THEN 'DEAD_END'
        ELSE 'OK'
    END AS flag
FROM SqlColumn c
LEFT JOIN COLUMN_LINEAGE lu ON lu.dst_key = c.table_name || '.' || c.col_name
LEFT JOIN COLUMN_LINEAGE ld ON ld.src_key = c.table_name || '.' || c.col_name
GROUP BY c.table_name, c.col_name, c.source
ORDER BY flag DESC, c.table_name, c.col_name;
```

### S3 — inferred (phantom) edge count
```sql
SELECT COUNT(*) AS inferred_edges
FROM COLUMN_LINEAGE
WHERE inferred_from_source_name = TRUE;
```

### S4 — tables that have COLUMN_LINEAGE edges but NO SqlColumn rows (the exact blindspot)
```sql
SELECT DISTINCT
    split_part(dst_key, '.', 1) || '.' || split_part(dst_key, '.', 2) AS table_name,
    COUNT(*) AS edge_count
FROM COLUMN_LINEAGE
WHERE NOT EXISTS (
    SELECT 1 FROM SqlColumn c
    WHERE c.table_name = split_part(dst_key, '.', 1) || '.' || split_part(dst_key, '.', 2)
)
GROUP BY 1
ORDER BY edge_count DESC;
```

S4 is the critical one: these are tables with lineage data but no catalog — the
closures that silently dead-end.

---

## What to do with the output

1. Run S4 first — these are the confirmed blindspots where edges exist but catalog is absent.
2. For each table in S4, classify the SQL pattern (Section 3). The dominant pattern is
   the parser fix target.
3. Run S1 to see ZERO_COLS tables that also have no lineage edges (pure dead tables vs
   cataloguing failures).
4. Run S2 to identify DEAD_END columns — columns that are catalogued but isolated.
5. Fix the highest-count pattern group, re-index, re-run the script, compare deltas.

---

## Files to create

- `scripts/column_coverage_check.py` — the script itself
- This report (`plan/reports/column_coverage_diagnostic.md`) — the plan

The script should be runnable against the real DWH DB without touching the DWH repo
(read-only connection, no commits to `/home/ignwrad/Projects/dwh`).
