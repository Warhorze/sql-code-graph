"""Profile only the <<SLOW files from the last full run.

Run:
  uv run python scratch_profile_slow.py

Do NOT commit.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from statistics import mean, median

DWH_ROOT = Path("/home/ignwrad/Projects/dwh")
SCHEMA_CSV = Path("/home/ignwrad/Projects/sql-code-graph/columns.csv")
TIMEOUT_S = 10       # per-file wall-clock limit for triage pass; set to None to disable
CPROFILE_WORST = True  # after triage, cProfile the slowest non-timeout file

# Extracted from profile.log — all 28 <<SLOW files
SLOW_NAMES = [
    "wtfe_artikel_inteken_lijst.sql",
    "wtfe_loyalty.sql",
    "initial_load_wtfa_kpi_datum_klant_us45932.sql",
    "wtfe_verkoopinfo_2.sql",
    "MSSPR_IA_OUTBOUND_DOORBELASTING.sql",
    "wtdh_bouwmarkt.sql",
    "wtfe_kpi_supply_chain_week_segment_voorraadlocatie.sql",
    "semtex_views.sql",
    "wtfi_axi_ail.sql",
    "wtfa_kpi_datum_klant_us49133.sql",
    "wtfi_bus_feebepaling.sql",
    "wtfe_kassahandeling.sql",
    "rtint_gmdf_prognose.sql",
    "wtdh_artikel.sql",
    "dj_da_kdb.sql",
    "WTFE_KORTING.sql",
    "wtfa_loonkosten.sql",
    "wtfv_prijsvoorstel.sql",
    "initial_loads_us47062.sql",
    "WTDH_ARTIKEL.sql",
    "wtfe_kpi_supply_chain_artikel_voorraadlocatie.sql",
    "initial_load_wtfe_kpi_datum_klant_us45715.sql",
    "ttint_voorraad_dagstand.sql",
    "wtfi_pushorder.sql",
    "wtfa_loonkosten_functie.sql",
    "wtfi_promotie_afzet.sql",
    "wtfs_voorraad_dagstand_eenmalig.sql",
    "f-32038-herstelactie-ontdubbelen-inkoop-orders.sql",
]


def _count_fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return -1


def main() -> None:
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

    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.parsers.snowflake_parser import SnowflakeParser

    print(f"[setup] loading schema CSV ({SCHEMA_CSV})...", flush=True)
    t0 = time.perf_counter()
    resolver = SchemaResolver(dialect="snowflake")
    n_tables = resolver.add_information_schema(SCHEMA_CSV)
    print(f"[setup] {n_tables} tables loaded in {time.perf_counter() - t0:.2f}s", flush=True)

    # Resolve slow file paths
    slow_paths: list[Path] = []
    name_to_paths: dict[str, list[Path]] = {}
    for p in DWH_ROOT.rglob("*.sql"):
        name_to_paths.setdefault(p.name, []).append(p)
    for name in SLOW_NAMES:
        candidates = name_to_paths.get(name, [])
        if not candidates:
            # Case-insensitive fallback
            candidates = [p for p in DWH_ROOT.rglob("*.sql") if p.name.lower() == name.lower()]
        if candidates:
            slow_paths.append(candidates[0])
        else:
            print(f"  [warn] not found: {name}", flush=True)

    print(f"[setup] found {len(slow_paths)}/{len(SLOW_NAMES)} slow files\n", flush=True)

    parser = SnowflakeParser(resolver)

    file_times: list[float] = []
    n_edges_list: list[int] = []
    asd_per_file: list[float] = []
    ms_per_file: list[float] = []

    print(
        f"{'idx':>4}  {'time_s':>7}  {'asd_s':>6}  {'ms_s':>6}"
        f"  {'stmts':>5}  {'edges':>6}  file"
    )
    print("-" * 90)

    for i, path in enumerate(slow_paths):
        try:
            sql = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[{i:3d}] read error: {e}")
            continue

        _asd_calls.clear()
        _ms_calls.clear()

        t0 = time.perf_counter()
        _result: list = []
        _exc: list = []

        def _run():
            try:
                _result.append(parser.parse_file(path, sql))
            except Exception as e:
                _exc.append(e)

        if TIMEOUT_S is not None:
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=TIMEOUT_S)
            if t.is_alive():
                elapsed = time.perf_counter() - t0
                print(f"{i:4d}  {'TIMEOUT':>7}  {'':>6}  {'':>6}  {'':>5}  {'':>6}  {path.name[:45]}  <<TIMEOUT>{elapsed:.0f}s")
                sys.stdout.flush()
                file_times.append(elapsed)
                continue
        else:
            _run()

        if _exc:
            print(f"[{i:3d}] EXCEPTION ({path.name}): {_exc[0]}")
            file_times.append(0.0)
            continue

        result = _result[0]
        elapsed = time.perf_counter() - t0
        asd_total = sum(_asd_calls)
        ms_total = sum(_ms_calls)
        n_stmt = len(result.statements)
        n_edge = sum(len(s.column_lineage) for s in result.statements)

        file_times.append(elapsed)
        n_edges_list.append(n_edge)
        asd_per_file.append(asd_total)
        ms_per_file.append(ms_total)

        flag = "  <<STILL_SLOW" if elapsed > 5 else ""
        print(
            f"{i:4d}  {elapsed:7.3f}  {asd_total:6.3f}  {ms_total:6.3f}"
            f"  {n_stmt:5d}  {n_edge:6d}  {path.name[:45]}{flag}"
        )
        sys.stdout.flush()

    print("\n" + "=" * 80)
    print("SUMMARY (slow files only)")
    print("=" * 80)
    good = [t for t in file_times if t > 0]
    if good:
        print(f"Files:         {len(good)}")
        print(f"Total time:    {sum(good):.1f}s")
        print(f"Mean:          {mean(good):.3f}s")
        print(f"Median:        {median(good):.3f}s")
        print(f"Max:           {max(good):.3f}s  ({slow_paths[file_times.index(max(good))].name})")
        print(f"Total edges:   {sum(n_edges_list)}")
        still_slow = sum(1 for t in good if t > 5)
        timeouts = sum(1 for t in file_times if t >= (TIMEOUT_S or 0) and t > 5)
        print(f"Still >5s:     {still_slow}/{len(good)}  (timeouts: {timeouts})")

    # --- cProfile drill-down on the worst completed file ---
    if not CPROFILE_WORST:
        return
    completed = [(t, p) for t, p in zip(file_times, slow_paths) if 0 < t < (TIMEOUT_S or float("inf"))]
    if not completed:
        print("\n[cprofile] no completed files to drill into")
        return
    worst_time, worst_path = max(completed)
    print(f"\n[cprofile] drilling into worst completed file: {worst_path.name} ({worst_time:.2f}s)")
    import cProfile, pstats, io as _io
    sql = worst_path.read_text(encoding="utf-8", errors="replace")
    pr = cProfile.Profile()
    pr.enable()
    try:
        parser.parse_file(worst_path, sql)
    finally:
        pr.disable()
    sio = _io.StringIO()
    ps = pstats.Stats(pr, stream=sio).sort_stats("cumulative")
    ps.print_stats(30)
    print(sio.getvalue())


if __name__ == "__main__":
    main()
