"""Column coverage diagnostic for an indexed sqlcg DuckDB graph.

Measures, for every catalogued table:
  - how many columns are in SqlColumn (ddl / clone_inherited)
  - how many COLUMN_LINEAGE edges land on / leave from each column
  - which tables have edges but NO catalog (the core blindspot)
  - for zero-catalog tables, what SQL pattern produced them

Usage:
    uv run python scripts/column_coverage_check.py \
        --db  path/to/sqlcg.duckdb  \
        --sql path/to/sql/repo       \
        [--out coverage_report.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Path to the indexed sqlcg DuckDB file")
    p.add_argument("--sql", required=True, help="Root of the SQL source repo (for pattern scan)")
    p.add_argument("--out", default=None, help="Optional path to write JSON results")
    p.add_argument("--min-cols", type=int, default=3, help="Below this, flag as PARTIAL (default 3)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

Q_TABLE_COVERAGE = """
SELECT
    t.table_name,
    COUNT(c.col_name) FILTER (WHERE c.source = 'ddl')             AS ddl_cols,
    COUNT(c.col_name) FILTER (WHERE c.source = 'clone_inherited') AS inherited_cols,
    COUNT(c.col_name)                                              AS total_cols
FROM SqlTable t
LEFT JOIN SqlColumn c ON c.table_name = t.table_name
GROUP BY t.table_name
ORDER BY total_cols ASC, t.table_name
"""

Q_COLUMN_LINEAGE = """
SELECT
    c.table_name,
    c.col_name,
    c.source,
    COUNT(DISTINCT lu.src_key)            AS upstream_count,
    COUNT(DISTINCT ld.dst_key)            AS downstream_count,
    BOOL_OR(lu.inferred_from_source_name) AS has_inferred_upstream
FROM SqlColumn c
LEFT JOIN COLUMN_LINEAGE lu ON lu.dst_key = c.table_name || '.' || c.col_name
LEFT JOIN COLUMN_LINEAGE ld ON ld.src_key = c.table_name || '.' || c.col_name
GROUP BY c.table_name, c.col_name, c.source
ORDER BY c.table_name, c.col_name
"""

# Tables that have COLUMN_LINEAGE edges but NO SqlColumn rows — the blindspot.
Q_EDGES_NO_CATALOG = """
SELECT
    CASE
        WHEN array_length(string_split(dst_key, '.')) >= 3
            THEN string_split(dst_key, '.')[1] || '.' || string_split(dst_key, '.')[2]
        WHEN array_length(string_split(dst_key, '.')) = 2
            THEN string_split(dst_key, '.')[1]
        ELSE dst_key
    END AS table_name,
    COUNT(*) AS edge_count,
    COUNT(*) FILTER (WHERE inferred_from_source_name = TRUE) AS inferred_edge_count
FROM COLUMN_LINEAGE
WHERE NOT EXISTS (
    SELECT 1 FROM SqlColumn c
    WHERE dst_key LIKE c.table_name || '.%'
)
GROUP BY 1
ORDER BY edge_count DESC
"""

Q_INFERRED_TOTAL = """
SELECT COUNT(*) AS inferred_edges FROM COLUMN_LINEAGE WHERE inferred_from_source_name = TRUE
"""


# ---------------------------------------------------------------------------
# Pattern classifier
# ---------------------------------------------------------------------------

_PAT_CLONE       = re.compile(r'\bCREATE\b.*?\bCLONE\b', re.I | re.S)
_PAT_CTAS        = re.compile(r'\bCREATE\b.*?\bAS\s+SELECT\b', re.I | re.S)
_PAT_PLAIN_DDL   = re.compile(r'\bCREATE\b[^(]*\(', re.I | re.S)
_PAT_EXTERNAL    = re.compile(r'\bCREATE\b.*?\b(EXTERNAL|TEMP|TEMPORARY)\b.*?\bTABLE\b', re.I | re.S)
_PAT_INSERT      = re.compile(r'\bINSERT\s+(?:OVERWRITE\s+)?INTO\b', re.I)


def classify_pattern(table_name: str, sql_root: Path) -> str:
    bare = table_name.split(".")[-1].lower()
    matches: list[Path] = []

    for f in sql_root.rglob("*.sql"):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if re.search(re.escape(bare), text, re.I):
            matches.append(f)

    if not matches:
        return "NOT_FOUND"

    for f in matches:
        text = f.read_text(errors="replace")
        # Only look at blocks that mention this table name near a CREATE
        if _PAT_EXTERNAL.search(text):
            return "EXTERNAL/TEMP"
        if _PAT_CLONE.search(text):
            return "CLONE"
        if _PAT_CTAS.search(text):
            return "CTAS"
        if _PAT_PLAIN_DDL.search(text):
            return "PLAIN_DDL"

    # Table name found in files but only in INSERT / SELECT context — never in CREATE
    return "INSERT_ONLY"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{100 * n // total}%"


def print_section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print('=' * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    try:
        import duckdb
    except ImportError:
        sys.exit("duckdb not available — run via: uv run python scripts/column_coverage_check.py ...")

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    sql_root = Path(args.sql).expanduser()
    if not sql_root.exists():
        sys.exit(f"SQL repo not found: {sql_root}")

    con = duckdb.connect(str(db_path), read_only=True)

    # -----------------------------------------------------------------------
    # Section 1 — table catalog coverage
    # -----------------------------------------------------------------------
    print_section("S1 — Table catalog coverage")
    table_rows = con.execute(Q_TABLE_COVERAGE).fetchall()
    table_cols = [d[0] for d in con.description]

    zero_cols_tables: list[str] = []
    partial_tables: list[str] = []
    ok_tables: list[str] = []

    table_coverage: dict[str, dict] = {}
    for row in table_rows:
        r = dict(zip(table_cols, row))
        total = r["total_cols"]
        if total == 0:
            flag = "ZERO_COLS"
            zero_cols_tables.append(r["table_name"])
        elif total < args.min_cols:
            flag = "PARTIAL"
            partial_tables.append(r["table_name"])
        else:
            flag = "OK"
            ok_tables.append(r["table_name"])
        r["flag"] = flag
        table_coverage[r["table_name"]] = r

    print(f"{'table_name':<55} {'ddl':>5} {'clone':>6} {'total':>6}  flag")
    print("-" * 80)
    for t, r in sorted(table_coverage.items(), key=lambda x: (x[1]["flag"], x[0])):
        print(f"{t:<55} {r['ddl_cols']:>5} {r['inherited_cols']:>6} {r['total_cols']:>6}  {r['flag']}")

    # -----------------------------------------------------------------------
    # Section 2 — column lineage coverage
    # -----------------------------------------------------------------------
    print_section("S2 — Column lineage coverage (DEAD_END = no up or downstream)")
    col_rows = con.execute(Q_COLUMN_LINEAGE).fetchall()
    col_cols = [d[0] for d in con.description]

    dead_end_cols: list[dict] = []
    inferred_cols: list[dict] = []
    col_coverage: list[dict] = []

    for row in col_rows:
        r = dict(zip(col_cols, row))
        r["flag"] = "DEAD_END" if r["upstream_count"] == 0 and r["downstream_count"] == 0 else "OK"
        if r["flag"] == "DEAD_END":
            dead_end_cols.append(r)
        if r["has_inferred_upstream"]:
            inferred_cols.append(r)
        col_coverage.append(r)

    print(f"{'table.col':<65} {'up':>4} {'dn':>4}  inferred  flag")
    print("-" * 90)
    for r in col_coverage:
        key = f"{r['table_name']}.{r['col_name']}"
        inferred_marker = "YES" if r["has_inferred_upstream"] else ""
        print(f"{key:<65} {r['upstream_count']:>4} {r['downstream_count']:>4}  {inferred_marker:<8}  {r['flag']}")

    # -----------------------------------------------------------------------
    # Section 3 — tables with edges but NO catalog
    # -----------------------------------------------------------------------
    print_section("S3 — Tables with COLUMN_LINEAGE edges but ZERO SqlColumn rows (blindspot)")
    blindspot_rows = con.execute(Q_EDGES_NO_CATALOG).fetchall()
    blindspot_cols = [d[0] for d in con.description]

    blindspots: list[dict] = []
    for row in blindspot_rows:
        r = dict(zip(blindspot_cols, row))
        blindspots.append(r)

    if not blindspots:
        print("  (none — all tables with lineage edges have at least one SqlColumn row)")
    else:
        print(f"{'table_name':<55} {'edges':>6}  {'inferred':>8}  pattern")
        print("-" * 85)
        pattern_counts: dict[str, int] = defaultdict(int)
        for r in blindspots:
            pat = classify_pattern(r["table_name"], sql_root)
            r["pattern"] = pat
            pattern_counts[pat] += 1
            print(f"{r['table_name']:<55} {r['edge_count']:>6}  {r['inferred_edge_count']:>8}  {pat}")

    # -----------------------------------------------------------------------
    # Section 4 — pattern scan for ZERO_COLS tables (even those with no edges)
    # -----------------------------------------------------------------------
    print_section("S4 — ZERO_COLS tables: SQL pattern classification")
    zero_pattern_counts: dict[str, int] = defaultdict(int)
    zero_pattern_detail: list[dict] = []

    for tname in zero_cols_tables:
        pat = classify_pattern(tname, sql_root)
        zero_pattern_counts[pat] += 1
        zero_pattern_detail.append({"table_name": tname, "pattern": pat})
        print(f"  {tname:<55}  {pat}")

    # -----------------------------------------------------------------------
    # Section 5 — inferred edge total
    # -----------------------------------------------------------------------
    inferred_total = con.execute(Q_INFERRED_TOTAL).fetchone()[0]

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    total_tables = len(table_coverage)
    total_cols   = len(col_coverage)
    cols_with_up = sum(1 for r in col_coverage if r["upstream_count"] > 0)
    cols_with_dn = sum(1 for r in col_coverage if r["downstream_count"] > 0)

    print_section("SUMMARY")
    print(f"Tables total:              {total_tables}")
    print(f"  ZERO_COLS:               {len(zero_cols_tables)}  ({pct(len(zero_cols_tables), total_tables)})")
    print(f"  PARTIAL (<{args.min_cols} cols):       {len(partial_tables)}  ({pct(len(partial_tables), total_tables)})")
    print(f"  OK:                      {len(ok_tables)}  ({pct(len(ok_tables), total_tables)})")
    print()
    if zero_cols_tables:
        print("ZERO_COLS by SQL pattern:")
        for pat, cnt in sorted(zero_pattern_counts.items(), key=lambda x: -x[1]):
            print(f"  {pat:<20} {cnt}")
    print()
    print(f"Columns total:             {total_cols}")
    print(f"  with upstream lineage:   {cols_with_up}  ({pct(cols_with_up, total_cols)})")
    print(f"  with downstream lineage: {cols_with_dn}  ({pct(cols_with_dn, total_cols)})")
    print(f"  DEAD_END (both=0):       {len(dead_end_cols)}  ({pct(len(dead_end_cols), total_cols)})")
    print()
    print(f"Blindspot tables (edges, no catalog): {len(blindspots)}")
    if blindspots:
        print("  by pattern:")
        for pat, cnt in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            print(f"    {pat:<20} {cnt}")
    print()
    print(f"Inferred-name edges total: {inferred_total}  (phantom column nodes)")

    con.close()

    # -----------------------------------------------------------------------
    # JSON dump
    # -----------------------------------------------------------------------
    if args.out:
        out_path = Path(args.out)
        report = {
            "table_coverage": list(table_coverage.values()),
            "column_coverage": col_coverage,
            "blindspot_tables": blindspots,
            "zero_cols_pattern_detail": zero_pattern_detail,
            "summary": {
                "total_tables": total_tables,
                "zero_cols": len(zero_cols_tables),
                "partial": len(partial_tables),
                "ok": len(ok_tables),
                "zero_cols_by_pattern": dict(zero_pattern_counts),
                "total_cols": total_cols,
                "cols_with_upstream": cols_with_up,
                "cols_with_downstream": cols_with_dn,
                "dead_end_cols": len(dead_end_cols),
                "blindspot_table_count": len(blindspots),
                "blindspot_by_pattern": dict(pattern_counts) if blindspots else {},
                "inferred_edges": inferred_total,
            },
        }
        out_path.write_text(json.dumps(report, indent=2))
        print(f"\nJSON report written to {out_path}")


if __name__ == "__main__":
    main()
