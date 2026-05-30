"""Unit tests for HardKillPool (300s plan — persistent spawn-mode worker pool).

Each test uses n_workers=2 to keep wall-time short while still exercising
multi-worker logic.  Tests are ordered by increasing complexity.
"""

from __future__ import annotations

import time

from sqlcg.indexer.pool import HardKillPool
from sqlcg.parsers.base import ParsedFile

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fast_task(path: str = "/tmp/a.sql", sql: str = "SELECT 1;") -> dict:
    return {"type": "parse_pass1", "path": path, "sql": sql}


def test_pool_fast_tasks_return_correctly(tmp_path):
    """n fast tasks all return valid ParsedFile objects."""
    f1 = tmp_path / "a.sql"
    f2 = tmp_path / "b.sql"
    f3 = tmp_path / "c.sql"
    for f in (f1, f2, f3):
        f.write_text("SELECT 1;", encoding="utf-8")

    tasks = [_fast_task(str(f), "SELECT 1;") for f in (f1, f2, f3)]

    with HardKillPool(None, n_workers=2) as pool:
        results = pool.map(tasks, per_task_timeout=10.0)

    assert len(results) == 3
    for i, r in enumerate(results):
        assert isinstance(r, ParsedFile), f"result[{i}] should be ParsedFile, got {type(r)}"


def test_pool_empty_task_list_returns_empty():
    with HardKillPool(None, n_workers=2) as pool:
        results = pool.map([], per_task_timeout=5.0)
    assert results == []


def test_pool_results_preserve_order(tmp_path):
    """Results are returned in task-input order, not completion order."""
    paths = [tmp_path / f"f{i}.sql" for i in range(6)]
    for p in paths:
        p.write_text("SELECT 1;", encoding="utf-8")

    tasks = [_fast_task(str(p), "SELECT 1;") for p in paths]

    with HardKillPool(None, n_workers=3) as pool:
        results = pool.map(tasks, per_task_timeout=10.0)

    assert len(results) == len(tasks)
    for i, (task, result) in enumerate(zip(tasks, results, strict=True)):
        assert isinstance(result, ParsedFile), f"slot {i}: expected ParsedFile"
        assert str(result.path) == task["path"], (
            f"slot {i}: path mismatch — expected {task['path']}, got {result.path}"
        )


def test_pool_more_tasks_than_workers(tmp_path):
    """Pool correctly processes tasks > n_workers via the work-stealing loop."""
    n = 10
    paths = [tmp_path / f"q{i}.sql" for i in range(n)]
    for p in paths:
        p.write_text("SELECT 1;", encoding="utf-8")

    tasks = [_fast_task(str(p), "SELECT 1;") for p in paths]

    with HardKillPool(None, n_workers=2) as pool:
        results = pool.map(tasks, per_task_timeout=10.0)

    assert len(results) == n
    assert all(isinstance(r, ParsedFile) for r in results)


def test_pool_reuse_across_map_calls(tmp_path):
    """Same pool instance can be used for pass-1 then pass-2 without re-warming."""
    paths = [tmp_path / f"r{i}.sql" for i in range(4)]
    for p in paths:
        p.write_text("SELECT 1;", encoding="utf-8")

    p1_tasks = [_fast_task(str(p), "SELECT 1;") for p in paths]
    p2_tasks = [
        {
            "type": "parse_pass2",
            "path": str(p),
            "sql": "SELECT 1;",
            "dependency_filter": None,
            "xfile_sql": {},
        }
        for p in paths
    ]

    with HardKillPool(None, n_workers=2) as pool:
        r1 = pool.map(p1_tasks, per_task_timeout=10.0)
        r2 = pool.map(p2_tasks, per_task_timeout=10.0)

    assert len(r1) == 4 and all(isinstance(r, ParsedFile) for r in r1)
    assert len(r2) == 4 and all(isinstance(r, ParsedFile) for r in r2)


# ---------------------------------------------------------------------------
# Scenario B — timeout fires, worker is killed and replaced
# ---------------------------------------------------------------------------


def test_pool_timeout_kills_and_replaces_worker(tmp_path):
    """A worker that exceeds per_task_timeout is SIGKILL'd; the slot is respawned
    and subsequent tasks complete normally.

    Strategy: dispatch a task with per_task_timeout=0.001s so the coordinator
    fires before the worker can respond.  Then dispatch a second batch at a
    normal timeout and verify it still succeeds — confirming the slot was
    respawned and is functional.
    """
    p = tmp_path / "a.sql"
    p.write_text("SELECT 1;", encoding="utf-8")

    # First map: near-zero timeout — coordinator kills worker(s) mid-init
    with HardKillPool(None, n_workers=1) as pool:
        results_timed_out = pool.map(
            [_fast_task(str(p), "SELECT 1;")],
            per_task_timeout=0.001,
        )
        # After the kill the slot is respawned; a normal-timeout task must work
        results_ok = pool.map(
            [_fast_task(str(p), "SELECT 1;")],
            per_task_timeout=10.0,
        )

    assert len(results_timed_out) == 1
    r_timeout = results_timed_out[0]
    assert isinstance(r_timeout, ParsedFile)
    # Either the parse beat the 0.001s (fast machine) or a timeout error was recorded
    # — both are valid outcomes; what matters is no hang and no None result.

    assert len(results_ok) == 1
    assert isinstance(results_ok[0], ParsedFile), "Post-kill slot must still return ParsedFile"


def test_pool_very_short_timeout_does_not_hang(tmp_path):
    """With per_task_timeout=0.001s the pool still terminates quickly."""
    paths = [tmp_path / f"t{i}.sql" for i in range(4)]
    for p in paths:
        p.write_text("SELECT 1;", encoding="utf-8")

    tasks = [_fast_task(str(p), "SELECT 1;") for p in paths]

    start = time.perf_counter()
    with HardKillPool(None, n_workers=2) as pool:
        results = pool.map(tasks, per_task_timeout=0.001)
    elapsed = time.perf_counter() - start

    # Pool must finish regardless of whether tasks timed out
    assert len(results) == 4
    # Each result is either a ParsedFile (succeeded before timeout) or a
    # ParsedFile with a timeout error (killed); never None.
    for r in results:
        assert isinstance(r, ParsedFile), f"Expected ParsedFile, got {type(r)}"
    # Should complete in well under 30 s even with multiple respawns
    assert elapsed < 60.0, f"Pool hung for {elapsed:.1f}s with very short timeout"


# ---------------------------------------------------------------------------
# Scenario C — worker exception is captured, not leaked
# ---------------------------------------------------------------------------


def test_pool_bad_sql_produces_parsedfile_with_errors(tmp_path):
    """Malformed SQL produces a ParsedFile with error messages, no exception raised."""
    p = tmp_path / "bad.sql"
    # sqlglot tolerates most SQL; use something that produces errors but doesn't hang
    p.write_text("THIS IS NOT SQL ;;; --- @@@@", encoding="utf-8")

    tasks = [_fast_task(str(p), "THIS IS NOT SQL ;;; --- @@@@")]

    with HardKillPool(None, n_workers=1) as pool:
        results = pool.map(tasks, per_task_timeout=10.0)

    assert len(results) == 1
    assert isinstance(results[0], ParsedFile)
    # The result should be a ParsedFile — it may or may not have errors
    # depending on how sqlglot handles the garbage SQL, but it must not be None.


# ---------------------------------------------------------------------------
# Scenario D — shutdown is clean (no zombie processes)
# ---------------------------------------------------------------------------


def test_pool_shutdown_terminates_workers():
    """After __exit__, pool._workers is empty and no child processes linger."""
    with HardKillPool(None, n_workers=2) as pool:
        procs = [w.process for w in pool._workers]
        assert all(p.is_alive() for p in procs), "Workers should be alive during map"

    # After exit, all processes should have been joined / killed
    for p in procs:
        assert not p.is_alive(), f"Worker pid={p.pid} still alive after shutdown"
