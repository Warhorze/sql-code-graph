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
    from sqlcg.core.duckdb_backend import DuckDBBackend

    return DuckDBBackend(db_path)


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
            'SELECT DISTINCT src_key AS sid FROM "COLUMN_LINEAGE" WHERE dst_key = ?',
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
        'SELECT DISTINCT src_key AS sid FROM "COLUMN_LINEAGE" WHERE dst_key = ?',
        {"cid": col_id},
    )
    return {r["sid"] for r in rows if "." in _table_of(r["sid"])}


# Default backup-table patterns — mirror the PR-02 NoiseFilter defaults so the
# harness applies the same hygiene as the shipped tools without needing config.
_DEFAULT_BACKUP_PATTERNS = ["*_bck", "*_bck_us", "*_bck_[0-9]*", "*_backup", "*_backup_[0-9]*"]


def _table_is_noise(table_qualified: str, patterns: list[str]) -> bool:
    """True when the table-name part matches a backup glob (same rule as NoiseFilter)."""
    name = table_qualified.rsplit(".", 1)[-1] if "." in table_qualified else table_qualified
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _columns_of(db, table_qualified: str) -> list[str]:
    """All column ids belonging to a table via HAS_COLUMN."""
    rows = db.run_read(
        'SELECT dst_key AS cid FROM "HAS_COLUMN" WHERE src_key = ?',
        {"tq": table_qualified},
    )
    return [r["cid"] for r in rows]


def _table_blast_radius(
    db, table_qualified: str, patterns: list[str] | None = None, max_nodes: int = 50000
) -> set[str]:
    """BFS downstream over COLUMN_LINEAGE from all columns of `table_qualified`,
    roll up to table_qualified, drop the table itself and any backup-pattern
    noise. Returns the set of affected downstream tables."""
    patterns = patterns if patterns is not None else _DEFAULT_BACKUP_PATTERNS
    seen: set[str] = set()
    frontier: deque[str] = deque(_columns_of(db, table_qualified))
    affected: set[str] = set()
    while frontier and len(seen) < max_nodes:
        cid = frontier.popleft()
        if cid in seen:
            continue
        seen.add(cid)
        rows = db.run_read(
            'SELECT DISTINCT dst_key AS did FROM "COLUMN_LINEAGE" WHERE src_key = ?',
            {"cid": cid},
        )
        for r in rows:
            did = r["did"]
            if did not in seen:
                frontier.append(did)
            tq = _table_of(did)
            if tq != table_qualified and not _table_is_noise(tq, patterns):
                affected.add(tq)
    return affected


def _upstream_tables(
    db, table_qualified: str, patterns: list[str] | None = None, max_nodes: int = 50000
) -> set[str]:
    """BFS upstream over COLUMN_LINEAGE from all columns of `table_qualified`,
    roll up to table_qualified, drop the table itself and backup noise."""
    patterns = patterns if patterns is not None else _DEFAULT_BACKUP_PATTERNS
    seen: set[str] = set()
    frontier: deque[str] = deque(_columns_of(db, table_qualified))
    upstreams: set[str] = set()
    while frontier and len(seen) < max_nodes:
        cid = frontier.popleft()
        if cid in seen:
            continue
        seen.add(cid)
        rows = db.run_read(
            'SELECT DISTINCT src_key AS sid FROM "COLUMN_LINEAGE" WHERE dst_key = ?',
            {"cid": cid},
        )
        for r in rows:
            sid = r["sid"]
            if sid not in seen:
                frontier.append(sid)
            tq = _table_of(sid)
            if tq != table_qualified and not _table_is_noise(tq, patterns):
                upstreams.add(tq)
    return upstreams


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


def _downstream_count(db, table_qualified: str) -> int:
    """Return the exact downstream dependent table count (integer fact).

    Delegates to _table_blast_radius which already does BFS + noise filtering.
    The count equals len(affected tables) — the same number the trust layer's
    downstream_count field carries.
    """
    return len(_table_blast_radius(db, table_qualified))


def _is_dead_code(db, table_qualified: str) -> bool:
    """Return True when table_qualified has no within-corpus SELECTS_FROM consumers.

    One read — mirrors the ANALYZE_UNUSED_TABLES predicate.
    """
    rows = db.run_read(
        'SELECT count(*) AS n FROM "SELECTS_FROM" WHERE dst_key = ?',
        {"tq": table_qualified},
    )
    n = rows[0]["n"] if rows else 0
    return n == 0


def _hub_rank(db, table_qualified: str, k: int = 1000) -> int | None:
    """Return the 1-based hub rank of table_qualified, or None if not in top-k.

    Runs the same HUB_RANKING Cypher with a large k (default 1000) so that a
    table with genuine dependents that ranks below top-10 does not produce a
    false-negative anchor failure.  Applies the same noise-filter defaults as
    the shipped tool.
    """
    from sqlcg.core.queries import HUB_RANKING_QUERY
    from sqlcg.server.noise_filter import NoiseFilter

    noise_filter = NoiseFilter(
        patterns=_DEFAULT_BACKUP_PATTERNS,
        schema_aliases={},
    )
    rows = db.run_read(HUB_RANKING_QUERY, {"k": k})
    rank = 1
    for row in rows:
        tq = row["table_qualified"]
        if noise_filter.is_noise(tq):
            continue
        if tq == table_qualified:
            return rank
        rank += 1
    return None


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
                scope="column",
                backup_noise=backup_noise,
                n_expected=len(expected),
            )
            results.append(r)

            # Table-level blast-radius scoring (V-GOLDEN). Gated on its own
            # table_status key so a seed anchor can carry hand-verified table
            # truth while its column-level sources are still draft.
            target_table = entry.get("target_table")
            table_status = entry.get("table_status", "draft")
            if target_table and entry.get("expected_downstream_tables") is not None:
                exp_dn = {
                    t for t in entry["expected_downstream_tables"] if not _table_is_noise(t, [])
                }
                act_dn = _table_blast_radius(db, target_table)
                rr = _score(exp_dn, act_dn)
                rr.update(
                    target=target_table,
                    bucket=entry.get("bucket", ""),
                    status=table_status,
                    scope="downstream_tables",
                    backup_noise=0,
                    n_expected=len(exp_dn),
                )
                results.append(rr)
            if target_table and entry.get("expected_upstream_tables") is not None:
                exp_up = {
                    t for t in entry["expected_upstream_tables"] if not _table_is_noise(t, [])
                }
                act_up = _upstream_tables(db, target_table)
                rr = _score(exp_up, act_up)
                rr.update(
                    target=target_table,
                    bucket=entry.get("bucket", ""),
                    status=table_status,
                    scope="upstream_tables",
                    backup_noise=0,
                    n_expected=len(exp_up),
                )
                results.append(rr)

            # Answer-anchor scoring (trust layer). Three independently-gated keys,
            # each scored as a binary pass/fail with recall=1.0 for a match.
            # Gated on table_status: curated (same gate as blast-radius anchors).
            if target_table:
                if "expected_downstream_count" in entry:
                    expected_count = entry["expected_downstream_count"]
                    actual_count = _downstream_count(db, target_table)
                    match = actual_count == expected_count
                    rr = {
                        "target": target_table,
                        "bucket": entry.get("bucket", ""),
                        "status": table_status,
                        "scope": "downstream_count",
                        "recall": 1.0 if match else 0.0,
                        "precision": 1.0 if match else 0.0,
                        "f1": 1.0 if match else 0.0,
                        "missing": [] if match else [str(expected_count)],
                        "spurious": [],
                        "backup_noise": 0,
                        "n_expected": 1,
                        "tp": 1 if match else 0,
                        "fp": 0,
                        "fn": 0 if match else 1,
                    }
                    results.append(rr)

                if "expected_dead_code" in entry:
                    expected_dc = entry["expected_dead_code"]
                    actual_dc = _is_dead_code(db, target_table)
                    match = actual_dc == expected_dc
                    rr = {
                        "target": target_table,
                        "bucket": entry.get("bucket", ""),
                        "status": table_status,
                        "scope": "dead_code",
                        "recall": 1.0 if match else 0.0,
                        "precision": 1.0 if match else 0.0,
                        "f1": 1.0 if match else 0.0,
                        "missing": [] if match else [str(expected_dc)],
                        "spurious": [],
                        "backup_noise": 0,
                        "n_expected": 1,
                        "tp": 1 if match else 0,
                        "fp": 0,
                        "fn": 0 if match else 1,
                    }
                    results.append(rr)

                if "expected_top_hub_rank" in entry:
                    expected_rank = entry["expected_top_hub_rank"]
                    actual_rank = _hub_rank(db, target_table)
                    match = actual_rank is not None and actual_rank <= expected_rank
                    rr = {
                        "target": target_table,
                        "bucket": entry.get("bucket", ""),
                        "status": table_status,
                        "scope": "hub_rank",
                        "recall": 1.0 if match else 0.0,
                        "precision": 1.0 if match else 0.0,
                        "f1": 1.0 if match else 0.0,
                        "missing": [] if match else [str(expected_rank)],
                        "spurious": [],
                        "backup_noise": 0,
                        "n_expected": 1,
                        "tp": 1 if match else 0,
                        "fp": 0,
                        "fn": 0 if match else 1,
                    }
                    results.append(rr)
    finally:
        db.close()
    return results


def _format_report(results: list[dict]) -> str:
    column_results = [r for r in results if r.get("scope", "column") == "column"]
    table_results = [r for r in results if r.get("scope", "column") != "column"]

    lines = [
        f"\n{'=' * 100}",
        f"GOLDEN LINEAGE REPORT  ({len(column_results)} cols, {len(table_results)} blast-radius)",
        f"{'=' * 100}",
        f"{'st':<4}{'P':>5}{'R':>6}{'F1':>6}{'exp':>5}{'noise':>7}  target",
    ]
    for r in sorted(column_results, key=lambda x: (x["status"] != "curated", x["f1"])):
        st = "CUR" if r["status"] == "curated" else "drf"
        lines.append(
            f"{st:<4}{r['precision']:>5.2f}{r['recall']:>6.2f}{r['f1']:>6.2f}"
            f"{r['n_expected']:>5}{r['backup_noise']:>7}  {r['target']}"
        )

    if table_results:
        lines += [f"{'-' * 100}", "BLAST RADIUS (table-level)"]
        for r in sorted(table_results, key=lambda x: (x["status"] != "curated", x["f1"])):
            st = "CUR" if r["status"] == "curated" else "drf"
            lines.append(
                f"{st:<4}{r['precision']:>5.2f}{r['recall']:>6.2f}{r['f1']:>6.2f}"
                f"{r['n_expected']:>5}{'':>7}  [{r['scope']}] {r['target']}"
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
        lines.append("(no curated entries yet — report only, nothing enforced)")
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
                f"  [{r.get('scope', 'column')}] {r['target']}: "
                f"P={r['precision']:.2f} R={r['recall']:.2f} "
                f"missing={r['missing']} spurious={r['spurious']}"
            )
    assert not failures, (
        f"{len(failures)} curated entr(y/ies) below quality floor "
        f"(P>={PRECISION_FLOOR}, R>={RECALL_FLOOR}):\n" + "\n".join(failures)
    )


# --------------------------------------------------------------------------
# PR-07 unit scenarios — exercise the table-level helpers on a built graph.
# These do NOT need the gitignored golden file or SQLCG_DB_PATH.
# --------------------------------------------------------------------------


def _mk_chain(backend, tables: list[str]) -> None:
    """Build a single-column COLUMN_LINEAGE chain across the given tables."""
    backend.init_schema()
    for t in tables:
        name = t.rsplit(".", 1)[-1]
        backend.upsert_node(
            "SqlTable",
            t,
            {
                "qualified": t,
                "catalog": "",
                "db": "",
                "name": name,
                "kind": "TABLE",
                "defined_in_file": "",
            },
        )
        cid = f"{t}.col"
        backend.upsert_node(
            "SqlColumn",
            cid,
            {
                "id": cid,
                "catalog": "",
                "db": "",
                "table_name": name,
                "col_name": "col",
                "table_qualified": t,
            },
        )
        backend.upsert_edge("SqlTable", t, "SqlColumn", cid, "HAS_COLUMN", {"source": ""})
    for a, b in zip(tables, tables[1:], strict=False):
        backend.upsert_edge(
            "SqlColumn",
            f"{a}.col",
            "SqlColumn",
            f"{b}.col",
            "COLUMN_LINEAGE",
            {"transform": "SELECT", "confidence": 1.0, "query_id": "q"},
        )


def test_table_blast_radius_nonempty():
    """Scenario A — blast radius reaches the downstream tables."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(":memory:")
    _mk_chain(backend, ["ba.source", "ba.etl", "ba.mart"])

    result = _table_blast_radius(backend, "ba.source")

    assert len(result) >= 1
    assert "ba.etl" in result
    assert "ba.mart" in result


def test_table_blast_radius_excludes_noise():
    """Scenario B — backup tables are excluded from the blast radius."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(":memory:")
    _mk_chain(backend, ["ba.source", "ba.source_bck", "ba.mart"])

    result = _table_blast_radius(backend, "ba.source")

    assert "ba.source_bck" not in result, f"backup must be filtered: {result}"
    assert "ba.mart" in result


def test_evaluate_handles_missing_downstream_key(tmp_path, monkeypatch):
    """Scenario C — evaluate() returns cleanly when an entry has no
    expected_downstream_tables key (the new code path must not error)."""
    import sys

    from sqlcg.core.duckdb_backend import DuckDBBackend

    db_path = str(tmp_path / "graph.db")
    backend = DuckDBBackend(db_path)
    _mk_chain(backend, ["ba.source", "ba.etl"])
    backend.close()

    golden = tmp_path / "golden_lineage.yaml"
    golden.write_text(
        "mode: reachable_leaves\n"
        "columns:\n"
        "  - target: ba.etl.col\n"
        "    expected_sources: [ba.source.col]\n"
        "    status: draft\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "GOLDEN_FILE", golden)
    monkeypatch.setenv("SQLCG_DB_PATH", db_path)

    results = evaluate()

    assert isinstance(results, list)
    assert len(results) >= 1
    # No table-level result was produced (no expected_downstream_tables key).
    assert all(r.get("scope", "column") == "column" for r in results)


# --------------------------------------------------------------------------
# Trust-layer unit scenarios — no golden file / no SQLCG_DB_PATH needed.
# Exercise the three answer-anchor helpers on built in-memory graphs.
# --------------------------------------------------------------------------


def _mk_selects_from_chain(backend, tables: list[str]) -> None:
    """Build a COLUMN_LINEAGE + SELECTS_FROM chain across the given tables.

    Each table after the first has a query that selects from the previous table,
    creating both COLUMN_LINEAGE (for _table_blast_radius) and SELECTS_FROM
    (for _is_dead_code / _hub_rank) edges.
    """
    backend.init_schema()
    for t in tables:
        name = t.rsplit(".", 1)[-1]
        backend.upsert_node(
            "SqlTable",
            t,
            {
                "qualified": t,
                "catalog": "",
                "db": "",
                "name": name,
                "kind": "TABLE",
                "defined_in_file": "",
            },
        )
        cid = f"{t}.col"
        backend.upsert_node(
            "SqlColumn",
            cid,
            {
                "id": cid,
                "catalog": "",
                "db": "",
                "table_name": name,
                "col_name": "col",
                "table_qualified": t,
            },
        )
        backend.upsert_edge("SqlTable", t, "SqlColumn", cid, "HAS_COLUMN", {"source": ""})
    for i, (a, b) in enumerate(zip(tables, tables[1:], strict=False)):
        # COLUMN_LINEAGE for downstream BFS
        backend.upsert_edge(
            "SqlColumn",
            f"{a}.col",
            "SqlColumn",
            f"{b}.col",
            "COLUMN_LINEAGE",
            {"transform": "SELECT", "confidence": 1.0, "query_id": f"q{i}"},
        )
        # SqlQuery + SELECTS_FROM for dead-code / hub-rank predicates
        qid = f"q{i}_id"
        backend.upsert_node(
            "SqlQuery",
            qid,
            {
                "id": qid,
                "kind": "INSERT",
                "sql": f"INSERT INTO {b} SELECT col FROM {a}",
                "target_table": b,
                "parsing_mode": "sqlglot",
            },
        )
        backend.upsert_edge("SqlQuery", qid, "SqlTable", a, "SELECTS_FROM", {})


def test_downstream_count_exact():
    """_downstream_count returns the exact integer for a 3-table chain."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(":memory:")
    _mk_selects_from_chain(backend, ["ba.src", "ba.etl", "ba.mart"])

    count = _downstream_count(backend, "ba.src")

    assert count == 2, f"expected 2 downstream tables (etl, mart); got {count}"


def test_is_dead_code_true_and_false():
    """_is_dead_code returns True for no-consumer table, False for consumed table."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(":memory:")
    _mk_selects_from_chain(backend, ["ba.src", "ba.etl"])

    # ba.src has no SELECTS_FROM incoming (nothing selects from it in this fixture);
    # actually with our chain: a query selects FROM ba.src to produce ba.etl.
    # So ba.etl is a consumer of ba.src — ba.src is not dead code.
    # But ba.etl has no consumer — it IS dead code.
    assert _is_dead_code(backend, "ba.etl") is True, "ba.etl has no consumers — dead code"
    assert _is_dead_code(backend, "ba.src") is False, "ba.src is consumed by the etl query"


def test_hub_rank_most_referenced_first():
    """_hub_rank returns rank 1 for the most-referenced table in a fan-in fixture."""
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(":memory:")
    # ba.hub is consumed by c1, c2, c3; ba.lonely is consumed by c1 only
    backend.init_schema()

    def _add_table(t: str) -> None:
        name = t.rsplit(".", 1)[-1]
        backend.upsert_node(
            "SqlTable",
            t,
            {
                "qualified": t,
                "catalog": "",
                "db": "",
                "name": name,
                "kind": "TABLE",
                "defined_in_file": "",
            },
        )

    for t in ["ba.hub", "ba.lonely", "ba.c1", "ba.c2", "ba.c3"]:
        _add_table(t)

    # Add queries: c1, c2, c3 each select from hub; c1 also selects from lonely
    for i, consumer in enumerate(["ba.c1", "ba.c2", "ba.c3"]):
        qid = f"qhub{i}"
        backend.upsert_node(
            "SqlQuery",
            qid,
            {
                "id": qid,
                "kind": "INSERT",
                "sql": "",
                "target_table": consumer,
                "parsing_mode": "sqlglot",
            },
        )
        backend.upsert_edge("SqlQuery", qid, "SqlTable", "ba.hub", "SELECTS_FROM", {})

    qlonely = "qlonely"
    backend.upsert_node(
        "SqlQuery",
        qlonely,
        {
            "id": qlonely,
            "kind": "INSERT",
            "sql": "",
            "target_table": "ba.c1",
            "parsing_mode": "sqlglot",
        },
    )
    backend.upsert_edge("SqlQuery", qlonely, "SqlTable", "ba.lonely", "SELECTS_FROM", {})

    hub_rank = _hub_rank(backend, "ba.hub", k=1000)
    lonely_rank = _hub_rank(backend, "ba.lonely", k=1000)

    assert hub_rank == 1, f"ba.hub (3 consumers) must rank first; got rank={hub_rank}"
    assert lonely_rank is not None and lonely_rank > 1, (
        f"ba.lonely (1 consumer) must rank below hub; got rank={lonely_rank}"
    )


def test_evaluate_handles_missing_answer_keys(tmp_path, monkeypatch):
    """evaluate() adds no non-column scope rows when all three answer-anchor keys
    are absent from an entry (backward compat for existing golden files)."""
    import sys

    from sqlcg.core.duckdb_backend import DuckDBBackend

    db_path = str(tmp_path / "graph.db")
    backend = DuckDBBackend(db_path)
    _mk_chain(backend, ["ba.source", "ba.etl"])
    backend.close()

    golden = tmp_path / "golden_lineage.yaml"
    golden.write_text(
        "mode: reachable_leaves\n"
        "columns:\n"
        "  - target: ba.etl.col\n"
        "    expected_sources: [ba.source.col]\n"
        "    status: draft\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "GOLDEN_FILE", golden)
    monkeypatch.setenv("SQLCG_DB_PATH", db_path)

    results = evaluate()

    assert isinstance(results, list)
    assert len(results) >= 1
    # No downstream_count / dead_code / hub_rank rows (keys absent from entry).
    non_column = [r for r in results if r.get("scope", "column") != "column"]
    assert non_column == [], f"expected no answer-anchor rows; got {non_column}"


if __name__ == "__main__":
    print(_format_report(evaluate()))
