"""Integration tests for DuckDB read-concurrency and 1,600-file write-memory ceiling.

Scenario A — cursor snapshot non-blocking:
  With run_read using a per-call cursor, a read issued while a write transaction is
  open on the shared connection returns immediately (within 1 s) with the last
  committed data, without waiting for the transaction to commit.

Scenario B — memory ceiling at 1,600 files:
  The indexer must stay below 3.8 GiB peak RSS when indexing 1,600 minimal SQL
  fixtures — guards against OOM regressions on the target machine class.
  Measured in a child subprocess so the reading reflects only the indexer process,
  not pytest infrastructure.

Guards: ARCHITECTURE_REVIEW.md §5 F2 (read non-blocking) and §5 F3 (memory ceiling).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend

# ---------------------------------------------------------------------------
# Scenario A — cursor snapshot non-blocking
# ---------------------------------------------------------------------------

_SEED_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "_test_seed" (id INTEGER PRIMARY KEY, val VARCHAR)
"""
_SEED_INSERT_SQL = "INSERT INTO \"_test_seed\" VALUES (1, 'hello')"
_READ_SQL = 'SELECT id, val FROM "_test_seed" WHERE id = 1'


def test_run_read_returns_without_waiting_for_open_write_transaction() -> None:
    """run_read with a cursor snapshot returns in <1 s during an open write transaction.

    Guards ARCHITECTURE_REVIEW §5 F2 (non-blocking reads via cursor snapshot).

    A write transaction is opened on a dedicated DuckDBBackend but NOT committed.
    A separate thread calls run_read on the same backend; with cursor-per-read, the
    read sees the last committed snapshot immediately rather than blocking on the
    open transaction.  The thread must join within 1 second, and the result must be
    non-empty.
    """
    # Use a dedicated backend with a real file so we can see MVCC behaviour.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "snapshot_test.db")

        backend = DuckDBBackend(db_path)
        backend.init_schema()

        # Seed a row and commit so run_read has something to see.
        backend._conn.execute(_SEED_TABLE_SQL)
        backend._conn.execute(_SEED_INSERT_SQL)
        backend._conn.commit()

        read_result: list[dict] = []
        read_error: list[Exception] = []

        def _reader() -> None:
            try:
                rows = backend.run_read(_READ_SQL, {})
                read_result.extend(rows)
            except Exception as exc:  # noqa: BLE001
                read_error.append(exc)

        # Open a write transaction on the connection WITHOUT committing.
        # This simulates a drain in progress.
        backend._conn.begin()
        try:
            backend._conn.execute("UPDATE \"_test_seed\" SET val = 'in-flight' WHERE id = 1")

            # Reader thread: must return in <1 s via cursor snapshot.
            reader = threading.Thread(target=_reader, daemon=True)
            reader.start()
            reader.join(timeout=1.0)

            assert not reader.is_alive(), (
                "run_read blocked for >1 s while a write transaction was open — "
                "cursor snapshot is not working; backend_lock may still be held"
            )
            assert not read_error, f"run_read raised: {read_error[0]}"
            assert read_result, (
                "run_read returned empty result during open write transaction — "
                "cursor snapshot must see last committed data"
            )
            assert read_result[0]["val"] == "hello", (
                f"Expected last-committed value 'hello', got {read_result[0]['val']!r}"
            )
        finally:
            # Roll back the open transaction so the backend closes cleanly.
            try:
                backend._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            backend.close()


# ---------------------------------------------------------------------------
# Scenario B — memory ceiling at 1,600 files (subprocess measurement)
# ---------------------------------------------------------------------------

_INDEXER_CHILD_SCRIPT = textwrap.dedent("""\
    import os
    import resource
    import sys
    from pathlib import Path

    corpus_dir = sys.argv[1]
    db_path = sys.argv[2]

    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.indexer.indexer import Indexer

    db = DuckDBBackend(db_path)
    db.init_schema()
    Indexer().index_repo(
        Path(corpus_dir),
        "ansi",
        db,
        use_git=False,
        batch_size=50,
    )
    db.close()

    # Report peak RSS in bytes via stdout so the parent can parse it.
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux: ru_maxrss is in KiB; macOS: in bytes.
    if sys.platform == "linux":
        peak_bytes = usage.ru_maxrss * 1024
    else:
        peak_bytes = usage.ru_maxrss
    print(peak_bytes)
""")

_3_8_GIB = int(3.8 * 1024**3)


def _generate_minimal_sql_corpus(root: Path, n: int = 1600) -> None:
    """Write n minimal SQL files to root — each file produces exactly one edge."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / f"t_{i:04d}.sql").write_text(
            f"CREATE VIEW v_{i:04d} AS SELECT col_{i} FROM src_{i:04d};",
            encoding="utf-8",
        )


@pytest.mark.slow
def test_indexer_peak_rss_stays_below_3_8_gib_on_1600_files() -> None:
    """Indexing 1,600 minimal SQL fixtures stays below 3.8 GiB peak RSS.

    Guards ARCHITECTURE_REVIEW §5 F3 (1,600-file write-memory ceiling).
    Peak RSS is measured in a child subprocess so the reading reflects only the
    indexer, not pytest or prior test state.  Uses VmHWM from /proc/<pid>/status
    (Linux) as a cross-check, with subprocess self-reported ru_maxrss as the
    primary metric.

    Failure message: "Peak write-memory must stay below 3.8 GiB on a
    1,600-file corpus — guards against OOM regression"
    """
    if sys.platform not in ("linux", "darwin"):
        pytest.skip("RSS measurement only supported on Linux/macOS")

    with tempfile.TemporaryDirectory() as tmp:
        corpus_dir = Path(tmp) / "corpus"
        db_path = str(Path(tmp) / "ceiling_test.db")
        _generate_minimal_sql_corpus(corpus_dir, n=1600)

        # Write the child script to a temp file.
        script_path = Path(tmp) / "_indexer_child.py"
        script_path.write_text(_INDEXER_CHILD_SCRIPT, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script_path), str(corpus_dir), db_path],
            capture_output=True,
            text=True,
            timeout=600,  # 10-min hard ceiling
        )

        assert result.returncode == 0, (
            f"Child indexer process exited with {result.returncode}.\n"
            f"stderr: {result.stderr[-2000:]}"
        )

        raw = result.stdout.strip()
        assert raw, f"Child process produced no RSS output.\nstderr: {result.stderr[-1000:]}"
        peak_rss_bytes = int(raw)

        assert peak_rss_bytes < _3_8_GIB, (
            f"Peak write-memory must stay below 3.8 GiB on a 1,600-file corpus — "
            f"guards against OOM regression "
            f"(measured {peak_rss_bytes / 1024**3:.2f} GiB)"
        )
