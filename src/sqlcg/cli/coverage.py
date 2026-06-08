"""Lineage-coverage metrics shared by `sqlcg db info` and `sqlcg gain`.

Read-only: four queries against the existing DuckDB graph, routed through
run_read_routed (server-aware). No schema change, no metrics collection.

Plan: plan/sprints/gain_coverage_metrics.md
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlcg.server.read_client import run_read_routed

# DuckDB expression that strips the trailing ".<column>" from a COLUMN_LINEAGE
# key, yielding the destination table's full_id — which equals SqlTable.qualified
# and HAS_COLUMN.src_key. Verified against base.py: ColumnRef.full_id ==
# f"{table.full_id}.{name}", joined by ".". No regex needed (DuckDB).
_DST_TABLE = "left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))"

# Query 1: catalog coverage — how many distinct tables appear in HAS_COLUMN vs. total SqlTable rows.
_Q_CATALOG_COVERAGE = """
SELECT COUNT(DISTINCT src_key) AS catalogued_tables,
       (SELECT COUNT(*) FROM "SqlTable") AS total_tables
FROM "HAS_COLUMN"
"""

# Query 2: edge health — edges whose stripped dst key has a catalogued HAS_COLUMN entry.
_Q_EDGE_HEALTH = """
SELECT SUM(CASE WHEN EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc
           WHERE hc.src_key = left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
       ) THEN 1 ELSE 0 END) AS good_edges,
       COUNT(*) AS total_edges
FROM "COLUMN_LINEAGE" cl
"""

# Query 3: phantom rate — edges flagged inferred_from_source_name.
_Q_PHANTOM_RATE = """
SELECT SUM(CASE WHEN inferred_from_source_name THEN 1 ELSE 0 END) AS phantom,
       COUNT(*) AS total
FROM "COLUMN_LINEAGE"
"""

# Query 4: blindspot tables — distinct dst tables with edges but no HAS_COLUMN entry.
_Q_BLINDSPOT = """
SELECT COUNT(DISTINCT left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))) AS blindspot
FROM "COLUMN_LINEAGE" cl
WHERE NOT EXISTS (
    SELECT 1 FROM "HAS_COLUMN" hc
    WHERE hc.src_key = left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
)
"""


@dataclass
class CoverageStats:
    """Lineage-coverage health metrics from the DuckDB graph.

    All six fields are raw integers read directly from the four SQL queries.
    Percent properties guard divide-by-zero and return 0.0 on an empty graph.
    """

    catalogued_tables: int
    total_tables: int
    good_edges: int
    total_edges: int
    phantom_edges: int
    blindspot_tables: int

    @property
    def catalog_pct(self) -> float:
        """Percentage of SqlTable rows that appear in HAS_COLUMN (0.0–100.0)."""
        if self.total_tables == 0:
            return 0.0
        return self.catalogued_tables / self.total_tables * 100

    @property
    def edge_health_pct(self) -> float:
        """Percentage of COLUMN_LINEAGE edges whose dst lands on a catalogued table."""
        if self.total_edges == 0:
            return 0.0
        return self.good_edges / self.total_edges * 100

    @property
    def phantom_pct(self) -> float:
        """Percentage of COLUMN_LINEAGE edges flagged inferred_from_source_name."""
        if self.total_edges == 0:
            return 0.0
        return self.phantom_edges / self.total_edges * 100


def collect_coverage() -> CoverageStats | None:
    """Run the four coverage queries via run_read_routed.

    Returns a populated CoverageStats on success, or None on any failure
    (graph unavailable, server busy, empty graph). Callers degrade gracefully
    — same contract as gain.py Section F.

    Plan: plan/sprints/gain_coverage_metrics.md §"Shared coverage module"
    """
    try:
        q1 = run_read_routed(_Q_CATALOG_COVERAGE, {})
        q2 = run_read_routed(_Q_EDGE_HEALTH, {})
        q3 = run_read_routed(_Q_PHANTOM_RATE, {})
        q4 = run_read_routed(_Q_BLINDSPOT, {})

        row1 = q1[0] if q1 else {}
        row2 = q2[0] if q2 else {}
        row3 = q3[0] if q3 else {}
        row4 = q4[0] if q4 else {}

        return CoverageStats(
            catalogued_tables=int(row1.get("catalogued_tables") or 0),
            total_tables=int(row1.get("total_tables") or 0),
            good_edges=int(row2.get("good_edges") or 0),
            total_edges=int(row2.get("total_edges") or 0),
            phantom_edges=int(row3.get("phantom") or 0),
            blindspot_tables=int(row4.get("blindspot") or 0),
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
