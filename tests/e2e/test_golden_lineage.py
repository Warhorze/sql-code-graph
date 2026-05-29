"""Golden lineage-quality harness — validate edge quality against curated truth.

WHAT THIS IS
------------
A set of target columns, each annotated with its KNOWN-CORRECT upstream physical
source columns (curated by a human). This harness scores the engine's actual
lineage against that ground truth: precision / recall / F1 per column and in
aggregate. It turns "this lineage looks plausible" into a number that moves when
edge quality regresses or improves.

The ground-truth data lives in tests/e2e/golden_lineage.yaml (GITIGNORED — it
names private DWH columns). This harness code is generic and committable: it
SKIPS cleanly when the golden file or the pre-built graph is absent.

HOW IT RUNS
-----------
It validates an EXISTING graph (built via `sqlcg index`), it does NOT re-index —
scoring is seconds, not the ~3 min an index takes. Point it at a graph with:

    SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run pytest tests/e2e/test_golden_lineage.py -s

Or run it as a standalone report (no pytest):

    SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python tests/e2e/test_golden_lineage.py

ENFORCEMENT
-----------
Only columns marked `status: curated` are asserted (a human certified their
expected_sources). `status: draft` columns are reported but never fail the build,
so the file is useful immediately and tightens as you curate. Backup-snapshot
sources matching `ignore_source_patterns` are excluded from BOTH sides of the
comparison but counted as `backup_noise` so pollution stays visible.
"""

from __future__ import annotations

import fnmatch
import os
from collections import deque
from pathlib import Path

import pytest

GOLDEN_FILE = Path(__file__).parent / "golden_lineage.yaml"
RECALL_FLOOR = float(os.getenv("SQLCG_GOLDEN_RECALL_FLOOR", "0.8"))
PRECISION_FLOOR = float(os.getenv("SQLCG_GOLDEN_PRECISION_FLOOR", "0.8"))


def _table_of(col_id: str) -> str:
    """`schema.table.col` / `table.col` -> the table-qualified part (drop the column)."""
    return col_id.rsplit(".", 1)[0]


def _is_ignored(col_id: str, patterns: list[str]) -> bool:
    table = _table_of(col_id)
    return any(fnmatch.fnmatch(table, p) or fnmatch.fnmatch(table.lower(), p) for p in patterns)


def _load_golden() -> dict:
    if not GOLDEN_FILE.exists():
        pytest.skip(
            f"golden file absent: {GOLDEN_FILE} (run scratch_bootstrap_golden.py to seed it)"
        )
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed — `uv add --dev pyyaml`")
    data = yaml.safe_load(GOLDEN_FILE.read_text())
    if not data or not data.get("columns"):
        pytest.skip("golden file has no columns")
    return data


def _open_db():
    db_path = os.getenv("SQLCG_DB_PATH")
    if not db_path or not Path(db_path).exists():
        pytest.skip(
            "no pre-built graph: set SQLCG_DB_PATH to a graph built with `sqlcg index` "
            "(this harness scores an existing graph; it does not index)"
        )
    from sqlcg.core.kuzu_backend import KuzuBackend

    return KuzuBackend(db_path)


def _reachable_physical_leaves(db, col_id: str, max_nodes: int = 5000) -> set[str]:
    """BFS upstream over COLUMN_LINEAGE; return ultimate physical source columns
    (schema-qualified nodes with no further upstream edge)."""
    seen: set[str] = set()
    leaves: set[str] = set()
    frontier: deque[str] = deque([col_id])
    while frontier and len(seen) < max_nodes:
        cid = frontier.popleft()
        if cid in seen:
            continue
        seen.add(cid)
        rows = db.run_read(
            "MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->(d:SqlColumn) "
            "WHERE d.id = $cid RETURN DISTINCT s.id AS sid",
            {"cid": cid},
        )
        if not rows:
            if cid != col_id and "." in _table_of(cid):
                leaves.add(cid)
            continue
        for r in rows:
            frontier.append(r["sid"])
    return leaves


def _direct_sources(db, col_id: str) -> set[str]:
    """One-hop physical sources feeding this column directly."""
    rows = db.run_read(
        "MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->(d:SqlColumn) "
        "WHERE d.id = $cid RETURN DISTINCT s.id AS sid",
        {"cid": col_id},
    )
    return {r["sid"] for r in rows if "." in _table_of(r["sid"])}


def _score(expected: set[str], actual: set[str]) -> dict:
    tp = len(expected & actual)
    fp = len(actual - expected)
    fn = len(expected - actual)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if not expected else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "missing": sorted(expected - actual),
        "spurious": sorted(actual - expected),
    }


def evaluate() -> list[dict]:
    """Score every golden column against the graph. Returns per-column results."""
    golden = _load_golden()
    ignore = golden.get("ignore_source_patterns", [])
    mode = golden.get("mode", "reachable_leaves")
    db = _open_db()
    results = []
    try:
        for entry in golden["columns"]:
            target = entry["target"]
            expected = {
                s for s in (entry.get("expected_sources") or []) if not _is_ignored(s, ignore)
            }
            if mode == "direct_sources":
                actual_all = _direct_sources(db, target)
            else:
                actual_all = _reachable_physical_leaves(db, target)
            backup_noise = sum(1 for s in actual_all if _is_ignored(s, ignore))
            actual = {s for s in actual_all if not _is_ignored(s, ignore)}
            r = _score(expected, actual)
            r.update(
                target=target,
                bucket=entry.get("bucket", ""),
                status=entry.get("status", "draft"),
                backup_noise=backup_noise,
                n_expected=len(expected),
            )
            results.append(r)
    finally:
        db.close()
    return results


def _format_report(results: list[dict]) -> str:
    lines = [
        f"\n{'=' * 100}",
        f"GOLDEN LINEAGE REPORT  ({len(results)} columns)",
        f"{'=' * 100}",
        f"{'st':<4}{'P':>5}{'R':>6}{'F1':>6}{'exp':>5}{'noise':>7}  target",
    ]
    for r in sorted(results, key=lambda x: (x["status"] != "curated", x["f1"])):
        st = "CUR" if r["status"] == "curated" else "drf"
        lines.append(
            f"{st:<4}{r['precision']:>5.2f}{r['recall']:>6.2f}{r['f1']:>6.2f}"
            f"{r['n_expected']:>5}{r['backup_noise']:>7}  {r['target']}"
        )
    cur = [r for r in results if r["status"] == "curated"]
    if cur:
        mp = sum(r["precision"] for r in cur) / len(cur)
        mr = sum(r["recall"] for r in cur) / len(cur)
        mf = sum(r["f1"] for r in cur) / len(cur)
        lines += [
            f"{'-' * 100}",
            f"CURATED MACRO  precision={mp:.3f}  recall={mr:.3f}  f1={mf:.3f}  "
            f"(floors: P>={PRECISION_FLOOR} R>={RECALL_FLOOR})",
        ]
    else:
        lines.append("(no curated columns yet — report only, nothing enforced)")
    return "\n".join(lines)


def test_golden_lineage_quality(capsys):
    """Score the graph against the golden set; enforce floors only on curated columns."""
    results = evaluate()
    report = _format_report(results)
    with capsys.disabled():
        print(report)

    failures = []
    for r in results:
        if r["status"] != "curated":
            continue
        if r["recall"] < RECALL_FLOOR or r["precision"] < PRECISION_FLOOR:
            failures.append(
                f"  {r['target']}: P={r['precision']:.2f} R={r['recall']:.2f} "
                f"missing={r['missing']} spurious={r['spurious']}"
            )
    assert not failures, (
        f"{len(failures)} curated column(s) below quality floor "
        f"(P>={PRECISION_FLOOR}, R>={RECALL_FLOOR}):\n" + "\n".join(failures)
    )


if __name__ == "__main__":
    print(_format_report(evaluate()))
