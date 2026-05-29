"""Compare schema_csv vs no-schema on IA-SEMANTIC and IA-DATAPRODUCTS folders.

These are the dynamic-table layers most likely to benefit from an information
schema CSV (wide SELECT * on production tables, cross-schema references).

Do NOT commit.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

DWH_ROOT = Path("/home/ignwrad/Projects/dwh/ddl/changelogs")
SCHEMA_CSV = Path("/home/ignwrad/Projects/sql-code-graph/columns.csv")

FOLDERS = {
    "IA-SEMANTIC": DWH_ROOT / "IA-SEMANTIC",
    "IA-DATAPRODUCTS": DWH_ROOT / "IA-DATAPRODUCTS",
}

EDGE_Q = "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN count(e) AS n"
STAR_Q = "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) WHERE e.via_star = true RETURN count(e) AS n"


def run(corpus: Path, schema_csv: Path | None, label: str) -> dict:
    from sqlcg.core.kuzu_backend import KuzuBackend
    from sqlcg.indexer.indexer import Indexer

    with tempfile.TemporaryDirectory() as tmp:
        db = KuzuBackend(str(Path(tmp) / "t.db"))
        db.init_schema()
        t0 = time.perf_counter()
        with patch(
            "sqlcg.cli.commands.load_schema._load_schema_into_graph",
            return_value=(0, 0),
        ):
            s = Indexer().index_repo(
                path=corpus,
                dialect="snowflake",
                db=db,
                timeout_per_file=10,
                use_git=False,
                batch_size=50,
                schema_csv=schema_csv,
                n_workers=2,
            )
        elapsed = time.perf_counter() - t0
        edges = db.run_read(EDGE_Q, {})[0]["n"]
        # sample a few edges
        sample = db.run_read(
            "MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->(d:SqlColumn) "
            "RETURN s.table_name AS s_t, s.col_name AS s_c, "
            "d.table_name AS d_t, d.col_name AS d_c LIMIT 5",
            {},
        )
        db.close()

    return {
        "label": label,
        "files": s.get("files_parsed", 0),
        "edges": edges,
        "star_expanded": s.get("star_edges_expanded", 0),
        "columns_defined": s.get("columns_defined", 0),
        "parse_errors": s.get("parse_errors", 0),
        "qualify_failed": s.get("error_summary", {}).get("qualify_failed", 0),
        "elapsed": round(elapsed, 2),
        "sample": sample,
    }


def print_result(r: dict) -> None:
    print(f"\n  {r['label']}")
    print(f"    files={r['files']}  edges={r['edges']}  star_expanded={r['star_expanded']}")
    print(f"    columns_defined={r['columns_defined']}  parse_errors={r['parse_errors']}  qualify_failed={r['qualify_failed']}  ({r['elapsed']}s)")
    if r["sample"]:
        print("    sample edges:")
        for row in r["sample"]:
            print(f"      {row['s_t']}.{row['s_c']}  ->  {row['d_t']}.{row['d_c']}")


if __name__ == "__main__":
    for name, folder in FOLDERS.items():
        if not folder.exists():
            print(f"SKIP: {folder} not found")
            continue

        files = list(folder.rglob("*.sql"))
        print(f"\n{'='*60}")
        print(f"  {name}  ({len(files)} files)")
        print(f"{'='*60}")

        r_with = run(folder, SCHEMA_CSV, "with schema_csv")
        r_without = run(folder, None, "without schema_csv")

        print_result(r_with)
        print_result(r_without)

        delta = r_with["edges"] - r_without["edges"]
        print(f"\n  delta edges: {delta:+d}  ({'schema adds' if delta > 0 else 'schema removes' if delta < 0 else 'no difference'})")
