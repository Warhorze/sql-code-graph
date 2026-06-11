"""Lineage-coverage metrics shared by `sqlcg db info` and `sqlcg gain`.

Read-only: queries against the existing DuckDB graph, routed through
run_read_routed (server-aware). No schema change, no metrics collection,
nothing in the indexing path.

Plan: plan/sprints/graph_health_catalog_and_metrics.md §3 (PR 1)
Supersedes the four-query baseline from plan/sprints/gain_coverage_metrics.md —
the original six fields are kept verbatim (legacy table-level numbers); all
new fields are additive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlcg.core.config import get_db_path
from sqlcg.server.read_client import run_read_routed

# DuckDB expression that strips the trailing ".<column>" from a COLUMN_LINEAGE
# key, yielding the destination table's full_id — which equals SqlTable.qualified
# and HAS_COLUMN.src_key. Verified against base.py: ColumnRef.full_id ==
# f"{table.full_id}.{name}", joined by ".". No regex needed (DuckDB).
_DST_TABLE = "left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))"

# Write-kind query kinds for the recall line. CREATE_TABLE covers both plain
# DDL and CTAS — QueryKind (base.py) has no separate CREATE_TABLE_AS value.
_WRITE_KINDS = ("INSERT", "MERGE", "CREATE_TABLE", "UPDATE")

# ---------------------------------------------------------------------------
# Query 1 (legacy): catalog coverage — distinct tables in HAS_COLUMN vs total SqlTable rows.
# ---------------------------------------------------------------------------
_Q_CATALOG_COVERAGE = """
SELECT COUNT(DISTINCT src_key) AS catalogued_tables,
       (SELECT COUNT(*) FROM "SqlTable") AS total_tables
FROM "HAS_COLUMN"
"""

# ---------------------------------------------------------------------------
# Query 2 (legacy): table-level edge health — edges whose stripped dst key has
# ANY catalogued HAS_COLUMN entry for that table (column not checked).
# ---------------------------------------------------------------------------
_Q_EDGE_HEALTH = f"""
SELECT SUM(CASE WHEN EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc
           WHERE hc.src_key = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
       ) THEN 1 ELSE 0 END) AS good_edges,
       COUNT(*) AS total_edges
FROM "COLUMN_LINEAGE" cl
"""

# ---------------------------------------------------------------------------
# Query 2-strict (new primary): edge good iff cl.dst_key exists verbatim in
# HAS_COLUMN.dst_key (index-backed by idx_HAS_COLUMN_dst). Column-level, not
# table-level.
# ---------------------------------------------------------------------------
_Q_EDGE_HEALTH_STRICT = """
SELECT SUM(CASE WHEN EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.dst_key = cl.dst_key
       ) THEN 1 ELSE 0 END) AS good_edges_strict,
       COUNT(*) AS total_edges
FROM "COLUMN_LINEAGE" cl
"""

# ---------------------------------------------------------------------------
# Query 2-scoped: same strict check, but excludes edges whose stripped dst
# table is a CTE/derived intermediate (correct-by-design, not a coverage gap).
# ---------------------------------------------------------------------------
_Q_EDGE_HEALTH_SCOPED = f"""
SELECT
    SUM(CASE WHEN EXISTS (
            SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.dst_key = cl.dst_key
        ) THEN 1 ELSE 0 END) AS good_edges_scoped,
    COUNT(*) AS total_edges_scoped
FROM "COLUMN_LINEAGE" cl
WHERE NOT EXISTS (
    SELECT 1 FROM "SqlTable" t
    WHERE t.qualified = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
      AND t.kind IN ('cte', 'derived')
)
"""

# ---------------------------------------------------------------------------
# Query 3 (legacy): phantom rate — edges flagged inferred_from_source_name.
# ---------------------------------------------------------------------------
_Q_PHANTOM_RATE = """
SELECT SUM(CASE WHEN inferred_from_source_name THEN 1 ELSE 0 END) AS phantom,
       COUNT(*) AS total
FROM "COLUMN_LINEAGE"
"""

# ---------------------------------------------------------------------------
# Query 3-split: three-way phantom verdict for inferred_from_source_name=True edges.
#   confirmed    — dst column exists verbatim in HAS_COLUMN (the guess was right)
#   contradicted — dst table catalogued but the exact dst column is absent
#                   (the guess is provably wrong — the worst class of edge)
#   unverified   — dst table itself is not catalogued at all
# ---------------------------------------------------------------------------
_Q_PHANTOM_SPLIT = f"""
SELECT
    SUM(CASE WHEN EXISTS (
            SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.dst_key = cl.dst_key
        ) THEN 1 ELSE 0 END) AS phantom_confirmed,
    SUM(CASE WHEN NOT EXISTS (
            SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.dst_key = cl.dst_key
        ) AND EXISTS (
            SELECT 1 FROM "HAS_COLUMN" hc
            WHERE hc.src_key = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
        ) THEN 1 ELSE 0 END) AS phantom_contradicted,
    SUM(CASE WHEN NOT EXISTS (
            SELECT 1 FROM "HAS_COLUMN" hc
            WHERE hc.src_key = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
        ) THEN 1 ELSE 0 END) AS phantom_unverified
FROM "COLUMN_LINEAGE" cl
WHERE cl.inferred_from_source_name
"""

# ---------------------------------------------------------------------------
# Query 4 (legacy): blindspot tables — distinct dst tables with edges but no
# HAS_COLUMN entry for that table.
# ---------------------------------------------------------------------------
_Q_BLINDSPOT = f"""
SELECT COUNT(DISTINCT {_DST_TABLE.replace("dst_key", "cl.dst_key")}) AS blindspot
FROM "COLUMN_LINEAGE" cl
WHERE NOT EXISTS (
    SELECT 1 FROM "HAS_COLUMN" hc
    WHERE hc.src_key = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
)
"""

# ---------------------------------------------------------------------------
# Query 4-weighted: top-10 blindspot tables by bad-edge count (strict
# definition — edge bad iff cl.dst_key has no exact HAS_COLUMN row), plus
# the number of tables covering 80% of total bad-edge volume.
#
# Excludes CTE/derived dst tables (kind IN ('cte', 'derived')) — same set as
# _Q_EDGE_HEALTH_SCOPED — so the blindspot ranking surfaces real catalog gaps
# instead of structural CTE/derived nodes (Fix 4a). The original finding said
# kind='cte' only; widened to ('cte','derived') for parity with the scoped KPI.
# ---------------------------------------------------------------------------
_Q_BLINDSPOT_WEIGHTED = f"""
WITH bad_edges AS (
    SELECT {_DST_TABLE.replace("dst_key", "cl.dst_key")} AS dst_table
    FROM "COLUMN_LINEAGE" cl
    WHERE NOT EXISTS (
        SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.dst_key = cl.dst_key
    )
    AND NOT EXISTS (
        SELECT 1 FROM "SqlTable" t
        WHERE t.qualified = {_DST_TABLE.replace("dst_key", "cl.dst_key")}
          AND t.kind IN ('cte', 'derived')
    )
),
per_table AS (
    SELECT dst_table, COUNT(*) AS bad_count
    FROM bad_edges
    GROUP BY dst_table
),
ranked AS (
    SELECT dst_table, bad_count,
           SUM(bad_count) OVER (ORDER BY bad_count DESC, dst_table
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total,
           SUM(bad_count) OVER () AS grand_total,
           ROW_NUMBER() OVER (ORDER BY bad_count DESC, dst_table) AS rn
    FROM per_table
)
SELECT dst_table, bad_count, rn,
       running_total, grand_total
FROM ranked
ORDER BY bad_count DESC, dst_table
"""

# ---------------------------------------------------------------------------
# Section G corpus fingerprint — files indexed + indexed_sha (read from
# SchemaVersion). DB path and index timestamp are filled in from get_db_path()
# and os.stat() in collect_coverage(), never hardcoded SQL.
# ---------------------------------------------------------------------------
_Q_FINGERPRINT = """
SELECT
    (SELECT COUNT(*) FROM "File") AS files_indexed,
    (SELECT indexed_sha FROM "SchemaVersion" LIMIT 1) AS indexed_sha
"""

# ---------------------------------------------------------------------------
# Recall line (a): degraded-parse rate, overall + by top-level directory of
# file_path relative to the indexed root (Repo.path). parse_failed means the
# scope-build/column-lineage path degraded to a table-level fallback — NOT
# that the statement is unparsed (snowflake_parser.py / ansi_parser.py).
# ---------------------------------------------------------------------------
_Q_DEGRADED_PARSE_OVERALL = """
SELECT SUM(CASE WHEN parse_failed THEN 1 ELSE 0 END) AS degraded,
       COUNT(*) AS total
FROM "SqlQuery"
"""

_Q_DEGRADED_PARSE_BY_DIR = """
WITH root AS (
    SELECT path FROM "Repo" LIMIT 1
)
SELECT
    CASE
        WHEN instr(substr(q.file_path, len(root.path) + 2), '/') > 0
            THEN split_part(substr(q.file_path, len(root.path) + 2), '/', 1)
        ELSE '(root)'
    END AS top_dir,
    SUM(CASE WHEN q.parse_failed THEN 1 ELSE 0 END) AS degraded,
    COUNT(*) AS total
FROM "SqlQuery" q, root
WHERE q.file_path LIKE root.path || '%'
GROUP BY 1
ORDER BY total DESC
"""

# ---------------------------------------------------------------------------
# Recall line (b): write-kind queries that parsed but emitted zero outgoing
# COLUMN_LINEAGE edges.
# ---------------------------------------------------------------------------
_WRITE_KINDS_SQL = ", ".join(f"'{k}'" for k in _WRITE_KINDS)
_Q_ZERO_EDGE_WRITES = f"""
SELECT
    SUM(CASE WHEN NOT EXISTS (
            SELECT 1 FROM "COLUMN_LINEAGE" cl WHERE cl.query_id = q.id
        ) THEN 1 ELSE 0 END) AS zero_edge_writes,
    COUNT(*) AS total_write_queries
FROM "SqlQuery" q
WHERE kind IN ({_WRITE_KINDS_SQL})
"""


@dataclass
class BlindspotTable:
    """A single table in the edge-weighted blindspot ranking."""

    table: str
    bad_edges: int


@dataclass
class CoverageStats:
    """Lineage-coverage health metrics from the DuckDB graph.

    The original six fields (catalogued_tables..blindspot_tables) are the
    legacy table-level metrics — kept verbatim, never repurposed. All
    subsequent fields are additive (PR 1, plan/sprints/graph_health_catalog_and_metrics.md).

    Percent properties guard divide-by-zero and return 0.0 on an empty graph.
    """

    # --- legacy fields (gain_coverage_metrics, unchanged meaning) ---
    catalogued_tables: int
    total_tables: int
    good_edges: int
    total_edges: int
    phantom_edges: int
    blindspot_tables: int

    # --- strict column-level health (new primary) ---
    good_edges_strict: int = 0

    # --- scoped (excludes CTE/derived dst) ---
    good_edges_scoped: int = 0
    total_edges_scoped: int = 0

    # --- phantom three-way split ---
    phantom_confirmed: int = 0
    phantom_contradicted: int = 0
    phantom_unverified: int = 0

    # --- edge-weighted blindspot ---
    top_blindspot_tables: list[BlindspotTable] = field(default_factory=list)
    blindspot_tables_for_80pct: int = 0

    # --- corpus fingerprint ---
    files_indexed: int = 0
    indexed_sha: str | None = None
    db_path: str = ""
    index_timestamp: float | None = None

    # --- recall ---
    degraded_parse_total: int = 0
    degraded_parse_queries: int = 0
    degraded_parse_by_dir: dict[str, tuple[int, int]] = field(default_factory=dict)
    zero_edge_write_queries: int = 0
    total_write_queries: int = 0

    @property
    def catalog_pct(self) -> float:
        """Percentage of SqlTable rows that appear in HAS_COLUMN (0.0–100.0)."""
        if self.total_tables == 0:
            return 0.0
        return self.catalogued_tables / self.total_tables * 100

    @property
    def edge_health_pct(self) -> float:
        """Legacy table-level edge health: dst table has ANY catalogued column."""
        if self.total_edges == 0:
            return 0.0
        return self.good_edges / self.total_edges * 100

    @property
    def edge_health_strict_pct(self) -> float:
        """Strict column-level edge health: dst column exists verbatim in HAS_COLUMN."""
        if self.total_edges == 0:
            return 0.0
        return self.good_edges_strict / self.total_edges * 100

    @property
    def edge_health_scoped_pct(self) -> float:
        """Strict edge health excluding CTE/derived dst intermediates."""
        if self.total_edges_scoped == 0:
            return 0.0
        return self.good_edges_scoped / self.total_edges_scoped * 100

    @property
    def phantom_pct(self) -> float:
        """Percentage of COLUMN_LINEAGE edges flagged inferred_from_source_name."""
        if self.total_edges == 0:
            return 0.0
        return self.phantom_edges / self.total_edges * 100

    @property
    def phantom_contradicted_pct(self) -> float:
        """Contradicted phantom edges as a percentage of ALL edges (headline trust number)."""
        if self.total_edges == 0:
            return 0.0
        return self.phantom_contradicted / self.total_edges * 100

    @property
    def degraded_parse_pct(self) -> float:
        """Percentage of SqlQuery rows with parse_failed=true (degraded extraction)."""
        if self.degraded_parse_total == 0:
            return 0.0
        return self.degraded_parse_queries / self.degraded_parse_total * 100


def collect_coverage() -> CoverageStats | None:
    """Run the coverage queries via run_read_routed.

    Returns a populated CoverageStats on success, or None on any failure
    (graph unavailable, server busy, empty graph). Callers degrade gracefully
    — same contract as gain.py Section F.

    Plan: plan/sprints/graph_health_catalog_and_metrics.md §3 (PR 1)
    """
    try:
        q1 = run_read_routed(_Q_CATALOG_COVERAGE, {})
        q2 = run_read_routed(_Q_EDGE_HEALTH, {})
        q2_strict = run_read_routed(_Q_EDGE_HEALTH_STRICT, {})
        q2_scoped = run_read_routed(_Q_EDGE_HEALTH_SCOPED, {})
        q3 = run_read_routed(_Q_PHANTOM_RATE, {})
        q3_split = run_read_routed(_Q_PHANTOM_SPLIT, {})
        q4 = run_read_routed(_Q_BLINDSPOT, {})
        q4_weighted = run_read_routed(_Q_BLINDSPOT_WEIGHTED, {})
        q_fp = run_read_routed(_Q_FINGERPRINT, {})
        q_degraded = run_read_routed(_Q_DEGRADED_PARSE_OVERALL, {})
        q_degraded_dir = run_read_routed(_Q_DEGRADED_PARSE_BY_DIR, {})
        q_zero_edge = run_read_routed(_Q_ZERO_EDGE_WRITES, {})

        row1 = q1[0] if q1 else {}
        row2 = q2[0] if q2 else {}
        row2_strict = q2_strict[0] if q2_strict else {}
        row2_scoped = q2_scoped[0] if q2_scoped else {}
        row3 = q3[0] if q3 else {}
        row3_split = q3_split[0] if q3_split else {}
        row4 = q4[0] if q4 else {}
        row_fp = q_fp[0] if q_fp else {}
        row_degraded = q_degraded[0] if q_degraded else {}
        row_zero_edge = q_zero_edge[0] if q_zero_edge else {}

        top_blindspot = [
            BlindspotTable(table=str(r["dst_table"]), bad_edges=int(r["bad_count"]))
            for r in q4_weighted[:10]
        ]

        blindspot_80pct = 0
        if q4_weighted:
            grand_total = int(q4_weighted[0].get("grand_total") or 0)
            threshold = grand_total * 0.8
            for r in q4_weighted:
                blindspot_80pct = int(r["rn"])
                if float(r["running_total"]) >= threshold:
                    break

        degraded_by_dir: dict[str, tuple[int, int]] = {
            str(r["top_dir"]): (int(r["degraded"] or 0), int(r["total"] or 0))
            for r in q_degraded_dir
        }

        db_path = get_db_path()
        index_timestamp: float | None = None
        try:
            index_timestamp = os.stat(db_path).st_mtime
        except OSError:
            index_timestamp = None

        return CoverageStats(
            catalogued_tables=int(row1.get("catalogued_tables") or 0),
            total_tables=int(row1.get("total_tables") or 0),
            good_edges=int(row2.get("good_edges") or 0),
            total_edges=int(row2.get("total_edges") or 0),
            phantom_edges=int(row3.get("phantom") or 0),
            blindspot_tables=int(row4.get("blindspot") or 0),
            good_edges_strict=int(row2_strict.get("good_edges_strict") or 0),
            good_edges_scoped=int(row2_scoped.get("good_edges_scoped") or 0),
            total_edges_scoped=int(row2_scoped.get("total_edges_scoped") or 0),
            phantom_confirmed=int(row3_split.get("phantom_confirmed") or 0),
            phantom_contradicted=int(row3_split.get("phantom_contradicted") or 0),
            phantom_unverified=int(row3_split.get("phantom_unverified") or 0),
            top_blindspot_tables=top_blindspot,
            blindspot_tables_for_80pct=blindspot_80pct,
            files_indexed=int(row_fp.get("files_indexed") or 0),
            indexed_sha=row_fp.get("indexed_sha"),
            db_path=str(db_path),
            index_timestamp=index_timestamp,
            degraded_parse_total=int(row_degraded.get("total") or 0),
            degraded_parse_queries=int(row_degraded.get("degraded") or 0),
            degraded_parse_by_dir=degraded_by_dir,
            zero_edge_write_queries=int(row_zero_edge.get("zero_edge_writes") or 0),
            total_write_queries=int(row_zero_edge.get("total_write_queries") or 0),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Colour helpers (single source of truth for both command surfaces)
# ---------------------------------------------------------------------------


def edge_health_colour(pct: float) -> str:
    """Return a Rich colour name for an edge-health percentage.

    Thresholds: red < 30%, yellow [30%, 50%), green >= 50%.
    """
    if pct < 30.0:
        return "red"
    if pct < 50.0:
        return "yellow"
    return "green"


def phantom_colour(pct: float) -> str:
    """Return a Rich colour name for a phantom-rate percentage.

    Thresholds: green <= 10%, yellow (10%, 20%], red > 20%.
    """
    if pct > 20.0:
        return "red"
    if pct > 10.0:
        return "yellow"
    return "green"


def blindspot_colour(count: int) -> str:
    """Return a Rich colour name (or plain sentinel) for a blindspot table count.

    Threshold: yellow when > 500, plain (empty string) at/below 500.
    No red tier specified.
    """
    if count > 500:
        return "yellow"
    return ""


def contradicted_colour(pct: float) -> str:
    """Return a Rich colour name for the contradicted-phantom percentage.

    Threshold (plan §3.3): red when contradicted edges exceed 0.5% of total
    edges — these are the worst edges in the graph (catalog-disproved
    positional guesses counted as good by the legacy table-level metric).
    """
    if pct > 0.5:
        return "red"
    return "green"


# ---------------------------------------------------------------------------
# Rendering — single source of truth for db.py and gain.py Section G.
# ---------------------------------------------------------------------------


def render_coverage_lines(coverage: CoverageStats, indent: str = "  ") -> list[str]:
    """Return Rich-markup console lines for `db info` and `gain` Section G.

    Plan: plan/sprints/graph_health_catalog_and_metrics.md §3 (PR 1) — both
    render sites share this to avoid drift between db.py and gain.py.
    """
    lines: list[str] = []

    lines.append(
        f"{indent}Tables with catalog: {coverage.catalogued_tables} / {coverage.total_tables}"
        f" ({coverage.catalog_pct:.0f}%)"
    )

    # Strict column-level health — new primary metric.
    strict_colour = edge_health_colour(coverage.edge_health_strict_pct)
    lines.append(
        f"{indent}[{strict_colour}]Edge health (strict, column-level):"
        f" {coverage.good_edges_strict} / {coverage.total_edges}"
        f" ({coverage.edge_health_strict_pct:.0f}%)[/{strict_colour}]"
    )

    # Legacy table-level health — kept, relabelled.
    legacy_colour = edge_health_colour(coverage.edge_health_pct)
    lines.append(
        f"{indent}[{legacy_colour}]Edge health (table-level, legacy):"
        f" {coverage.good_edges} / {coverage.total_edges}"
        f" ({coverage.edge_health_pct:.0f}%)[/{legacy_colour}]"
    )

    # Scoped (excludes CTE/derived dst intermediates).
    scoped_colour = edge_health_colour(coverage.edge_health_scoped_pct)
    lines.append(
        f"{indent}[{scoped_colour}]Edge health (scoped, excl. CTE/derived):"
        f" {coverage.good_edges_scoped} / {coverage.total_edges_scoped}"
        f" ({coverage.edge_health_scoped_pct:.0f}%)[/{scoped_colour}]"
    )

    # Phantom — legacy total + three-way split.
    ph_colour = phantom_colour(coverage.phantom_pct)
    lines.append(
        f"{indent}[{ph_colour}]Phantom edges: {coverage.phantom_edges}"
        f" / {coverage.total_edges} ({coverage.phantom_pct:.0f}%)[/{ph_colour}]"
    )
    contra_colour = contradicted_colour(coverage.phantom_contradicted_pct)
    lines.append(
        f"{indent}  confirmed: {coverage.phantom_confirmed}"
        f"  [{contra_colour}]contradicted: {coverage.phantom_contradicted}"
        f" ({coverage.phantom_contradicted_pct:.1f}% of all edges)[/{contra_colour}]"
        f"  unverified: {coverage.phantom_unverified}"
    )

    # Blindspot — raw count + edge-weighted top-10 + 80% concentration.
    bs_colour = blindspot_colour(coverage.blindspot_tables)
    if bs_colour:
        lines.append(
            f"{indent}[{bs_colour}]Blindspot tables: {coverage.blindspot_tables}[/{bs_colour}]"
        )
    else:
        lines.append(f"{indent}Blindspot tables: {coverage.blindspot_tables}")
    if coverage.top_blindspot_tables:
        lines.append(
            f"{indent}  {coverage.blindspot_tables_for_80pct} table(s) cover 80% of bad-edge volume"
        )
        lines.append(f"{indent}  Top blindspot tables (by bad-edge count):")
        for bt in coverage.top_blindspot_tables[:10]:
            lines.append(f"{indent}    {bt.table}: {bt.bad_edges}")

    # Corpus fingerprint.
    ts = (
        datetime.fromtimestamp(coverage.index_timestamp, tz=UTC).isoformat()
        if coverage.index_timestamp is not None
        else "unknown"
    )
    lines.append(
        f"{indent}Corpus: {coverage.files_indexed} files, indexed_sha="
        f"{coverage.indexed_sha or 'unknown'}, db_path={coverage.db_path}, "
        f"index_timestamp={ts}"
    )

    # Recall lines.
    lines.append(
        f"{indent}Degraded-parse queries: {coverage.degraded_parse_queries}"
        f" / {coverage.degraded_parse_total} ({coverage.degraded_parse_pct:.0f}%)"
    )
    for top_dir, (degraded, total) in sorted(
        coverage.degraded_parse_by_dir.items(), key=lambda kv: kv[1][1], reverse=True
    ):
        pct = (degraded / total * 100) if total else 0.0
        lines.append(f"{indent}  {top_dir}/: {degraded} / {total} ({pct:.0f}%)")
    lines.append(
        f"{indent}Write queries with zero outgoing lineage: "
        f"{coverage.zero_edge_write_queries} / {coverage.total_write_queries}"
    )

    return lines


def coverage_to_json(coverage: CoverageStats) -> dict:
    """Return the `--json` coverage dict for `sqlcg gain`.

    The legacy six keys are returned verbatim and unchanged in meaning; all
    other keys are additive (PR 1, plan/sprints/graph_health_catalog_and_metrics.md §3).
    """
    return {
        # legacy (unchanged meaning)
        "catalogued_tables": coverage.catalogued_tables,
        "total_tables": coverage.total_tables,
        "good_edges": coverage.good_edges,
        "total_edges": coverage.total_edges,
        "phantom_edges": coverage.phantom_edges,
        "blindspot_tables": coverage.blindspot_tables,
        # strict column-level health (new primary)
        "good_edges_strict": coverage.good_edges_strict,
        "edge_health_strict_pct": round(coverage.edge_health_strict_pct, 2),
        # scoped (excludes CTE/derived dst)
        "good_edges_scoped": coverage.good_edges_scoped,
        "total_edges_scoped": coverage.total_edges_scoped,
        "edge_health_scoped_pct": round(coverage.edge_health_scoped_pct, 2),
        # phantom three-way split
        "phantom_confirmed": coverage.phantom_confirmed,
        "phantom_contradicted": coverage.phantom_contradicted,
        "phantom_unverified": coverage.phantom_unverified,
        # edge-weighted blindspot
        "top_blindspot_tables": [
            {"table": bt.table, "bad_edges": bt.bad_edges} for bt in coverage.top_blindspot_tables
        ],
        "blindspot_tables_for_80pct": coverage.blindspot_tables_for_80pct,
        # corpus fingerprint
        "files_indexed": coverage.files_indexed,
        "indexed_sha": coverage.indexed_sha,
        "db_path": coverage.db_path,
        "index_timestamp": coverage.index_timestamp,
        # recall
        "degraded_parse_total": coverage.degraded_parse_total,
        "degraded_parse_queries": coverage.degraded_parse_queries,
        "degraded_parse_by_dir": {
            k: {"degraded": v[0], "total": v[1]} for k, v in coverage.degraded_parse_by_dir.items()
        },
        "zero_edge_write_queries": coverage.zero_edge_write_queries,
        "total_write_queries": coverage.total_write_queries,
    }
