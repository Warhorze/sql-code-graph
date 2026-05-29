"""Experiment: profile 100 random SQL files to diagnose per-file slowdown.

Measures:
  - Per-file wall time (is it growing over time?)
  - as_sources_dict() cost isolation
  - mapping_schema() cost isolation

Run:
  python scratch_profile_100.py

Parses directly with the full schema loaded (no subprocess) so timings reflect
real production conditions. Ctrl+C to abort if a file hangs (shouldn't after
fixes A/B/C).

Do NOT commit.
"""

from __future__ import annotations

import gc
import os
import random
import sys
import threading
import time
from pathlib import Path
from statistics import mean, median

# ── Config ────────────────────────────────────────────────────────────────────
DWH_ROOT = Path("/home/ignwrad/Projects/dwh")
SCHEMA_CSV = Path("/home/ignwrad/Projects/sql-code-graph/columns.csv")
N_FILES = 1000
SEED = 42


def _count_fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return -1


def _count_semaphores() -> int:
    try:
        return sum(1 for f in Path("/dev/shm").iterdir() if f.name.startswith("sem."))
    except Exception:
        return -1


def main() -> None:
    # ── Patch timing hooks ────────────────────────────────────────────────────
    import sqlcg.lineage.schema_resolver as _sr_mod

    _asd_calls: list[float] = []
    _asd_lock = threading.Lock()
    _orig_asd = _sr_mod.SchemaResolver.as_sources_dict

    def _timed_asd(self):
        t0 = time.perf_counter()
        result = _orig_asd(self)
        dt = time.perf_counter() - t0
        with _asd_lock:
            _asd_calls.append(dt)
        return result

    _sr_mod.SchemaResolver.as_sources_dict = _timed_asd  # type: ignore

    _ms_calls: list[float] = []
    _ms_lock = threading.Lock()
    _orig_ms = _sr_mod.SchemaResolver.mapping_schema

    def _timed_ms(self):
        t0 = time.perf_counter()
        result = _orig_ms(self)
        dt = time.perf_counter() - t0
        with _ms_lock:
            _ms_calls.append(dt)
        return result

    _sr_mod.SchemaResolver.mapping_schema = _timed_ms  # type: ignore

    # ── Setup ─────────────────────────────────────────────────────────────────
    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.snowflake_parser import SnowflakeParser

    print(f"[setup] loading schema CSV ({SCHEMA_CSV})...", flush=True)
    t0 = time.perf_counter()
    resolver = SchemaResolver(dialect="snowflake")
    n_tables = resolver.add_information_schema(SCHEMA_CSV)
    schema_load_time = time.perf_counter() - t0
    print(f"[setup] {n_tables} tables loaded in {schema_load_time:.2f}s", flush=True)

    # Cold cost of as_sources_dict
    _asd_calls.clear()
    t0 = time.perf_counter()
    asd = resolver.as_sources_dict()
    asd_cold = time.perf_counter() - t0
    n_asd_entries = len(asd)
    del asd
    print(f"[setup] as_sources_dict(): {n_asd_entries} entries in {asd_cold:.3f}s", flush=True)
    _asd_calls.clear()

    # Cold cost of mapping_schema
    _ms_calls.clear()
    t0 = time.perf_counter()
    ms = resolver.mapping_schema()
    ms_cold = time.perf_counter() - t0
    print(f"[setup] mapping_schema(): {ms_cold:.3f}s", flush=True)
    _ms_calls.clear()
    del ms

    all_sql = list(DWH_ROOT.rglob("*.sql"))
    print(f"[setup] found {len(all_sql)} SQL files total", flush=True)
    rng = random.Random(SEED)
    sample = rng.sample(all_sql, min(N_FILES, len(all_sql)))
    print(f"[setup] sampled {len(sample)} files (seed={SEED})\n", flush=True)

    parser = SnowflakeParser(resolver)

    # ── Parse loop ────────────────────────────────────────────────────────────
    file_times: list[float] = []
    fd_deltas: list[int] = []
    sem_deltas: list[int] = []
    asd_per_file: list[float] = []
    ms_per_file: list[float] = []
    n_statements: list[int] = []
    n_edges: list[int] = []

    print(
        f"{'idx':>4}  {'time_s':>7}  {'asd_s':>6}  {'ms_s':>6}"
        f"  {'stmts':>5}  {'edges':>6}  {'fds':>5}  {'sems':>5}  file"
    )
    print("-" * 110)

    total_t0 = time.perf_counter()

    for i, path in enumerate(sample):
        try:
            sql = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[{i:3d}] read error: {e}")
            continue

        _asd_calls.clear()
        _ms_calls.clear()
        fds_before = _count_fds()
        sems_before = _count_semaphores()

        t0 = time.perf_counter()
        try:
            result = parser.parse_file(path, sql)
        except Exception as exc:
            print(f"[{i:3d}] EXCEPTION: {exc}")
            file_times.append(0.0)
            continue

        elapsed = time.perf_counter() - t0

        fds_after = _count_fds()
        sems_after = _count_semaphores()
        fd_delta = fds_after - fds_before
        sem_delta = sems_after - sems_before

        asd_total = sum(_asd_calls)
        ms_total = sum(_ms_calls)
        n_stmt = len(result.statements)
        n_edge = sum(len(s.column_lineage) for s in result.statements)

        file_times.append(elapsed)
        fd_deltas.append(fd_delta)
        sem_deltas.append(sem_delta)
        asd_per_file.append(asd_total)
        ms_per_file.append(ms_total)
        n_statements.append(n_stmt)
        n_edges.append(n_edge)

        flag = "  <<SLOW" if elapsed > 5 else ""
        print(
            f"{i:4d}  {elapsed:7.3f}  {asd_total:6.3f}  {ms_total:6.3f}"
            f"  {n_stmt:5d}  {n_edge:6d}  {fd_delta:+5d}  {sem_delta:+5d}  {path.name[:40]}{flag}"
        )
        sys.stdout.flush()

        if (i + 1) % 10 == 0:
            gc.collect()
            total_fds = _count_fds()
            total_sems = _count_semaphores()
            elapsed_total = time.perf_counter() - total_t0
            recent = file_times[max(0, len(file_times) - 10) :]
            print(
                f"  ── checkpoint {i + 1:3d}: total_fds={total_fds}, total_sems={total_sems}, "
                f"last-10 avg={mean(recent):.3f}s, total={elapsed_total:.1f}s"
            )
            sys.stdout.flush()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    if file_times:
        good = [t for t in file_times if t > 0]
        print(f"Files parsed:      {len(file_times)}")
        print(f"Total wall time:   {sum(file_times):.1f}s")
        print(f"Per-file mean:     {mean(good):.3f}s")
        print(f"Per-file median:   {median(good):.3f}s")
        print(f"Per-file max:      {max(good):.3f}s  (idx={file_times.index(max(good))})")
        print(f"Per-file min:      {min(good):.4f}s")
        print(f"Total edges:       {sum(n_edges)}")
        print()
        if len(good) >= 40:
            first20 = mean(file_times[:20])
            last20 = mean(file_times[-20:])
            mid20 = mean(file_times[40:60]) if len(file_times) >= 60 else mean(file_times[20:40])
            ratio = last20 / first20 if first20 > 0 else float("inf")
            print(f"Trend first-20 avg: {first20:.3f}s")
            print(f"Trend mid-20  avg:  {mid20:.3f}s")
            print(f"Trend last-20 avg:  {last20:.3f}s")
            print(f"Slowdown ratio:     {ratio:.2f}x  (>1.5 = concerning)")
        print()
        tot = sum(file_times)
        asd_tot = sum(asd_per_file)
        ms_tot = sum(ms_per_file)
        print(
            f"as_sources_dict() total: {asd_tot:.2f}s,  avg/file: {mean(asd_per_file):.3f}s,  cold: {asd_cold:.3f}s"
        )
        print(
            f"mapping_schema()  total: {ms_tot:.2f}s,  avg/file: {mean(ms_per_file):.3f}s,  cold: {ms_cold:.3f}s"
        )
        print(
            f"FD delta total:   {sum(fd_deltas):+d}  (files with leak: {sum(1 for d in fd_deltas if d > 0)})"
        )
        print(f"Sem delta total:  {sum(sem_deltas):+d}")
        print()
        if tot > 0:
            print(f"as_sources_dict() % of wall time: {asd_tot / tot * 100:.1f}%")
            print(f"mapping_schema()  % of wall time: {ms_tot / tot * 100:.1f}%")
            other_pct = (1 - (asd_tot + ms_tot) / tot) * 100
            print(f"Other (parse/expand/lineage) %:   {other_pct:.1f}%")


if __name__ == "__main__":
    main()
