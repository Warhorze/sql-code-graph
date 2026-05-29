"""Developer smoke test: profile 10 random SQL files after perf fixes.

Exits 1 immediately if any file is slow (> 2 s) or raises an exception.
Expected total runtime: < 60 s on a fixed build.

Run from the project root:
    python scratch_profile_10.py

Do NOT commit.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from statistics import mean

DWH_ROOT = Path("/home/ignwrad/Projects/dwh")
SCHEMA_CSV = Path("/home/ignwrad/Projects/sql-code-graph/columns.csv")
N_FILES = 10
SEED = 42
PER_FILE_BUDGET = 2.0  # seconds


def main() -> None:
    if not DWH_ROOT.exists():
        print(f"SKIP: DWH corpus not found at {DWH_ROOT}")
        sys.exit(0)
    if not SCHEMA_CSV.exists():
        print(f"SKIP: schema CSV not found at {SCHEMA_CSV}")
        sys.exit(0)

    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.snowflake_parser import SnowflakeParser

    print(f"[setup] loading schema CSV ({SCHEMA_CSV})...", flush=True)
    t0 = time.perf_counter()
    resolver = SchemaResolver(dialect="snowflake")
    n_tables = resolver.add_information_schema(SCHEMA_CSV)
    print(f"[setup] {n_tables} tables loaded in {time.perf_counter() - t0:.2f}s", flush=True)

    all_sql = list(DWH_ROOT.rglob("*.sql"))
    if not all_sql:
        print(f"FAIL: no .sql files found under {DWH_ROOT}")
        sys.exit(1)

    sample = random.Random(SEED).sample(all_sql, min(N_FILES, len(all_sql)))
    print(f"[setup] sampled {len(sample)} files (seed={SEED})\n", flush=True)

    parser = SnowflakeParser(resolver)

    file_times: list[float] = []
    total_edges = 0

    print(f"{'idx':>4}  {'time_s':>7}  {'stmts':>5}  {'edges':>6}  file")
    print("-" * 70)

    for i, path in enumerate(sample):
        try:
            sql = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"FAIL: could not read {path}: {e}")
            sys.exit(1)

        t0 = time.perf_counter()
        try:
            result = parser.parse_file(path, sql)
        except Exception as e:
            print(f"FAIL: exception parsing {path.name}: {e}")
            sys.exit(1)
        elapsed = time.perf_counter() - t0

        n_stmt = len(result.statements)
        n_edge = sum(len(s.column_lineage) for s in result.statements)
        total_edges += n_edge
        file_times.append(elapsed)

        flag = "  <<SLOW — FAIL" if elapsed > PER_FILE_BUDGET else ""
        print(f"{i:4d}  {elapsed:7.3f}  {n_stmt:5d}  {n_edge:6d}  {path.name[:40]}{flag}")
        sys.stdout.flush()

        if elapsed > PER_FILE_BUDGET:
            print(f"\nFAIL: {path.name} took {elapsed:.2f}s (budget {PER_FILE_BUDGET}s).")
            print("Fix A / Fix B may not be active. Check dependency_filter wiring and as_sources_dict cache.")
            sys.exit(1)

    print("\n" + "=" * 70)
    print("PASS")
    print("=" * 70)
    print(f"Files:           {len(file_times)}")
    print(f"Total wall time: {sum(file_times):.2f}s")
    print(f"Per-file mean:   {mean(file_times):.3f}s")
    print(f"Per-file max:    {max(file_times):.3f}s")
    print(f"Total edges:     {total_edges}")


if __name__ == "__main__":
    main()
