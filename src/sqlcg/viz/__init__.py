"""Self-contained graph-visualization generator for ``sqlcg viz``.

Plan: plan/sprints/feature_graph_viz.md

A pure read + render feature: queries the live DuckDB graph (via the
server-aware ``run_read_routed`` seam), resolves query-anchored edges to
table->table links, attaches per-table label facets (tags/jobs) from a BYO
CSV, and emits ONE self-contained HTML file with the force-graph library
inlined. No parser/indexer/lineage code is touched.
"""
