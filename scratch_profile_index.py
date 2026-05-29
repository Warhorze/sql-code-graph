"""Profile indexer on 60 ETL files to find the slow phase."""
import time
from pathlib import Path
import tempfile, shutil

from sqlcg.indexer.indexer import Indexer
from sqlcg.core.kuzu_backend import KuzuBackend

sql_dir = Path("/home/ignwrad/Projects/dwh/etl/sql")
# Use every 10th file to get a representative spread across all directories
all_files = sorted(sql_dir.rglob("*.sql"))
files = all_files[::10]  # ~60 files spread evenly across the corpus

with tempfile.TemporaryDirectory() as tmpdir:
    for f in files:
        shutil.copy(f, tmpdir)

    db = KuzuBackend(str(Path(tmpdir) / "test.db"))
    db.init_schema()
    indexer = Indexer()

    t0 = time.perf_counter()
    result = indexer.index_repo(Path(tmpdir), dialect="snowflake", db=db)
    elapsed = time.perf_counter() - t0

n = result['files_parsed']
print(f"\n{n} files in {elapsed:.1f}s = {elapsed/n*1000:.0f}ms/file")
print(f"Extrapolated to 604 files: {elapsed/n*604:.0f}s ({elapsed/n*604/60:.1f}min)")
print(f"Quality: {result['quality']}")
print(f"Tables: {result['tables_found']}, Edges: {result['lineage_edges_created']}, Errors: {result['parse_errors']}")
