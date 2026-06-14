"""Build the baked node/link/adjacency dataset for ``sqlcg viz``.

Plan: plan/sprints/feature_graph_viz.md §"DATA query + node/edge/adjacency schema"

All reads go through the server-aware :func:`run_read_routed` seam (the same
path ``gain``/``coverage`` use) — never a direct ``get_backend`` open — so the
generator does not contend for the DuckDB single-file exclusive lock (#63).

EDGE MODEL (plan-review B1 — the load-bearing correctness point). The graph's
three lineage relations are NOT table->table:

* ``SELECTS_FROM`` / ``STAR_SOURCE`` are ``SqlQuery -> SqlTable``. The SqlTable
  end (``dst_key``) is the **source** table; the **target** table is the query's
  write target, resolved from ``SqlQuery.target_table``. A read-only SELECT with
  no resolvable target contributes no link (skipped).
* ``COLUMN_LINEAGE`` is ``SqlColumn -> SqlColumn``. Both endpoint keys
  ``<table>.<col>`` are collapsed to their table by stripping the trailing
  ``.<col>`` with the established ``_DST_TABLE`` pattern from
  :mod:`sqlcg.cli.coverage`.

Self-loops are dropped; duplicate ``(source, target)`` pairs are collapsed with
a NEW ``n`` count (the hand-made artifact has no ``n``; this is additive).
"""

from __future__ import annotations

from collections import defaultdict

# Reuse the verified DuckDB expression that strips the trailing ".<column>"
# from a COLUMN_LINEAGE key, yielding the table's full_id (== SqlTable.qualified).
# Importing keeps a single source of truth — no second hand-written copy.
from sqlcg.cli.coverage import _DST_TABLE
from sqlcg.server.read_client import run_read_routed

# ---------------------------------------------------------------------------
# Node query: every SqlTable, with a catalogued flag (EXISTS in HAS_COLUMN keyed
# on src_key = qualified — the same join coverage.py uses).
# ---------------------------------------------------------------------------
_Q_NODES = """
SELECT t.qualified AS qualified,
       t.db        AS db,
       t.kind      AS kind,
       EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc WHERE hc.src_key = t.qualified
       ) AS catalogued
FROM "SqlTable" t
"""

# ---------------------------------------------------------------------------
# Query-anchored source->target links (SELECTS_FROM + STAR_SOURCE).
# dst_key = the source SqlTable.qualified; q.target_table = the write target.
# Read-only SELECTs (target_table NULL/empty) drop out via the WHERE clause.
# ---------------------------------------------------------------------------
_Q_QUERY_LINKS = """
SELECT sf.dst_key      AS source_table,
       q.target_table  AS target_table
FROM "SELECTS_FROM" sf
JOIN "SqlQuery" q ON q.id = sf.src_key
WHERE q.target_table IS NOT NULL AND q.target_table <> ''
UNION ALL
SELECT ss.dst_key      AS source_table,
       q.target_table  AS target_table
FROM "STAR_SOURCE" ss
JOIN "SqlQuery" q ON q.id = ss.src_key
WHERE q.target_table IS NOT NULL AND q.target_table <> ''
"""

# ---------------------------------------------------------------------------
# Column-lineage links collapsed to table level (both endpoints stripped).
# ---------------------------------------------------------------------------
_Q_COLUMN_LINKS = f"""
SELECT {_DST_TABLE.replace("dst_key", "cl.src_key")} AS source_table,
       {_DST_TABLE.replace("dst_key", "cl.dst_key")} AS target_table
FROM "COLUMN_LINEAGE" cl
"""


def build_viz_data() -> dict:
    """Build the baked ``{nodes, links, adj}`` dataset from the live graph.

    Returns:
        A dict with three keys:

        * ``nodes`` — list of ``{id, schema, kind, deg, cat, jobs, tags}``.
          ``id`` is ``SqlTable.qualified``; ``schema`` is ``SqlTable.db``;
          ``jobs``/``tags`` start empty (facets are attached later by the CLI).
        * ``links`` — list of ``{source, target, n}`` table->table edges with
          self-loops dropped and duplicates collapsed (``n`` = collapsed count).
        * ``adj`` — undirected ``{id: [neighbor, ...]}`` adjacency map over the
          collapsed link set (baked for the future neighborhood feature).
    """
    node_rows = run_read_routed(_Q_NODES, {})

    nodes: list[dict] = []
    known_ids: set[str] = set()
    for r in node_rows:
        qualified = r["qualified"]
        known_ids.add(qualified)
        nodes.append(
            {
                "id": qualified,
                "schema": r["db"],
                "kind": r["kind"],
                "deg": 0,  # filled in after edges are collapsed
                "cat": bool(r["catalogued"]),
                "jobs": [],
                "tags": [],
            }
        )

    # Collapse all link sources into a single (source, target) -> count map.
    link_counts: dict[tuple[str, str], int] = defaultdict(int)
    for query in (_Q_QUERY_LINKS, _Q_COLUMN_LINKS):
        for r in run_read_routed(query, {}):
            src = r["source_table"]
            dst = r["target_table"]
            if not src or not dst:
                continue
            if src == dst:  # self-loop dropped
                continue
            link_counts[(src, dst)] += 1

    links: list[dict] = [
        {"source": src, "target": dst, "n": n} for (src, dst), n in link_counts.items()
    ]

    # Undirected adjacency + per-node degree (distinct-neighbor count) over the
    # collapsed link set. Only nodes that exist as SqlTable rows are kept as
    # adjacency keys so the bake never references a phantom node id.
    adj_sets: dict[str, set[str]] = defaultdict(set)
    for src, dst in link_counts:
        adj_sets[src].add(dst)
        adj_sets[dst].add(src)

    deg_by_id = {nid: len(neighbors) for nid, neighbors in adj_sets.items()}
    for n in nodes:
        n["deg"] = deg_by_id.get(n["id"], 0)

    adj = {nid: sorted(neighbors) for nid, neighbors in adj_sets.items() if nid in known_ids}

    return {"nodes": nodes, "links": links, "adj": adj}
