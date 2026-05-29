"""Full-pipeline e2e smoke test with information_schema CSV.

Copies N random SQL files to a temp dir and runs index_repo with schema_csv,
mirroring the real e2e test but at controlled scale. Start at N=10, work up.

Usage:
    python scratch_e2e_schema.py          # 10 files
    python scratch_e2e_schema.py 50       # 50 files
    python scratch_e2e_schema.py 200      # etc.

Do NOT commit.
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

DWH_ROOT = Path("/home/ignwrad/Projects/dwh")
SCHEMA_CSV = Path("/home/ignwrad/Projects/sql-code-graph/columns.csv")
SEED = 42
N_FILES = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def main() -> None:
    if not DWH_ROOT.exists():
        print(f"SKIP: DWH corpus not found at {DWH_ROOT}")
        sys.exit(0)
    if not SCHEMA_CSV.exists():
        print(f"SKIP: schema CSV not found at {SCHEMA_CSV}")
        sys.exit(0)

    all_sql = list(DWH_ROOT.rglob("*.sql"))
    if not all_sql:
        print(f"FAIL: no .sql files under {DWH_ROOT}")
        sys.exit(1)

    sample = random.Random(SEED).sample(all_sql, min(N_FILES, len(all_sql)))
    print(f"[setup] sampled {len(sample)} / {len(all_sql)} files  (seed={SEED})")
    print(f"[setup] schema CSV: {SCHEMA_CSV}  ({SCHEMA_CSV.stat().st_size // 1024} KB)")

    with tempfile.TemporaryDirectory(prefix="sqlcg_e2e_schema_") as tmp:
        corpus = Path(tmp) / "corpus"
        corpus.mkdir()
        for src in sample:
            # Preserve filename, flatten dir structure (no collisions expected at N<=200)
            dst = corpus / src.name
            if dst.exists():
                dst = corpus / f"{src.parent.name}__{src.name}"
            shutil.copy2(src, dst)

        print(f"[setup] copied {len(sample)} files to {corpus}\n")

        from sqlcg.core.kuzu_backend import KuzuBackend
        from sqlcg.indexer.indexer import Indexer

        db_path = Path(tmp) / "test.db"
        db = KuzuBackend(str(db_path))
        db.init_schema()

        # Skip _load_schema_into_graph (row-by-row upserts, >2min for 11MB CSV).
        # Workers receive schema_csv directly for SchemaResolver — lineage resolution
        # still uses schema; gold-table graph nodes are just absent in this smoke test.
        print(f"[step 1] index_repo({N_FILES} files, n_workers=2, with schema_csv)...", flush=True)
        t0 = time.perf_counter()
        indexer = Indexer()

        # Monkey-patch _load_schema_into_graph out so index_repo doesn't call it
        import sqlcg.indexer.indexer as _idx_mod
        from sqlcg.cli.commands import load_schema as _ls_mod
        _orig = _ls_mod._load_schema_into_graph
        _ls_mod._load_schema_into_graph = lambda *a, **kw: (0, 0)
        try:
            summary = indexer.index_repo(
                path=corpus,
                dialect="snowflake",
                db=db,
                timeout_per_file=5,
                use_git=False,
                batch_size=50,
                schema_csv=SCHEMA_CSV,
                n_workers=2,
            )
        finally:
            _ls_mod._load_schema_into_graph = _orig

        elapsed = time.perf_counter() - t0
        print(f"[step 1] done in {elapsed:.2f}s", flush=True)

        print("=" * 60)
        print(f"  N={len(sample)} files  +  schema CSV")
        print("=" * 60)
        print(f"  Wall time:          {elapsed:.2f}s")
        print(f"  files_parsed:       {summary.get('files_parsed', '?')}")
        print(f"  parse_errors:       {summary.get('parse_errors', '?')}")
        print(f"  tables_found:       {summary.get('tables_found', '?')}")
        print(f"  columns_defined:    {summary.get('columns_defined', '?')}")
        print(f"  lineage_edges:      {summary.get('lineage_edges_created', '?')}")
        print(f"  star_edges_expanded:{summary.get('star_edges_expanded', '?')}")
        print(f"  pass2_skipped:      {summary.get('pass2_skipped', '?')}")

        # Quick sanity query — any column lineage at all?
        rows = db.run_read(
            "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN count(e) AS n",
            {},
        )
        edge_count = rows[0]["n"] if rows else 0
        print(f"\n  Graph edge count (query): {edge_count}")

        # Show a sample edge if any exist
        if edge_count > 0:
            sample_rows = db.run_read(
                "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) "
                "RETURN s.table_name AS s_t, s.col_name AS s_c, "
                "d.table_name AS d_t, d.col_name AS d_c LIMIT 3",
                {},
            )
            print("\n  Sample edges:")
            for r in sample_rows:
                print(f"    {r['s_t']}.{r['s_c']}  ->  {r['d_t']}.{r['d_c']}")

        db.close()

    if elapsed > 0 and summary.get("files_parsed", 0) > 0:
        extrap_1600 = elapsed * (1600 / summary["files_parsed"])
        print(f"\n  Extrapolated @ 1600 files: {extrap_1600:.0f}s  (budget 300s)")

    print()


if __name__ == "__main__":
    main()
