"""The sqlcg viz command — generate a self-contained graph-explorer HTML.

Plan: plan/sprints/feature_graph_viz.md §"CLI signature".

Queries the live graph (via the server-aware ``run_read_routed`` seam),
attaches per-table label facets (tags/jobs) from BYO CSVs, and writes ONE
self-contained ``.html`` with the force-graph library inlined — openable on
Windows via ``file://``. Touches no parser/indexer/lineage code.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from sqlcg.core.config import get_viz_schemas
from sqlcg.utils.logging import getLogger
from sqlcg.viz.data import build_viz_data
from sqlcg.viz.render import DEFAULT_VIZ_OUT, render_html
from sqlcg.viz.tags import FacetMap, load_facet, resolve_labels

logger = getLogger(__name__)
console = Console()


def _facet_payload(facet: FacetMap) -> dict:
    """Shape a FacetMap for baking: ``{labels, colors}``."""
    return {"labels": facet.labels, "colors": facet.colors}


def viz_cmd(
    tags: Path | None = typer.Option(  # noqa: B008
        None,
        "--tags",
        help=(
            "BYO tags CSV (pattern,label[,color]; header required). The 'tag' "
            "facet drives the legend swatches (color + filter). Patterns are "
            "fnmatch globs (*, ?, [seq]) matched case-insensitively against the "
            "qualified table name. Many-to-many. Omit to hide the tag legend."
        ),
    ),
    jobs: Path | None = typer.Option(  # noqa: B008
        None,
        "--jobs",
        help=(
            "BYO jobs CSV, same shape as --tags. The 'job' facet drives the job "
            "dropdown (filter only). Omit to leave the dropdown empty."
        ),
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None,
        "--out",
        help=f"Output HTML path (default: {DEFAULT_VIZ_OUT} in the current directory).",
    ),
    config_dir: Path = typer.Option(  # noqa: B008
        Path("."),
        "--config-dir",
        help="Directory searched for .sqlcg.toml ([sqlcg.viz] schemas).",
    ),
) -> None:
    """Generate a self-contained graph-explorer HTML from the live graph.

    Reads the graph via the server-aware routed-read path (never a direct DB
    open). The emitted file inlines the force-graph library plus the baked node/
    edge/adjacency data and the tag/job facets — no external resources — so it
    opens by double-click on Windows.

    The client-side filter composes schema ∩ kind ∩ tag (multi-select per facet,
    union within a facet, intersection across) plus a job dropdown; an edge
    renders only when both endpoints are visible. Node kinds table/view/temp are
    ON by default, cte/derived OFF (switchable).
    """
    out_path = out if out is not None else Path(DEFAULT_VIZ_OUT)

    schemas = get_viz_schemas(config_dir)

    data = build_viz_data()

    tag_facet = load_facet(tags, "tag") if tags is not None else FacetMap(name="tag")
    job_facet = load_facet(jobs, "job") if jobs is not None else FacetMap(name="job")

    # Attach resolved facet labels to every node (m2m glob resolution).
    for node in data["nodes"]:
        node["tags"] = resolve_labels(node["id"], tag_facet)
        node["jobs"] = resolve_labels(node["id"], job_facet)

    html = render_html(
        data,
        _facet_payload(tag_facet),
        _facet_payload(job_facet),
        schemas,
    )
    out_path.write_text(html, encoding="utf-8")

    schema_count = len(schemas) if schemas else len({n["schema"] for n in data["nodes"]})
    console.print(
        f"Wrote {out_path} — {len(data['nodes'])} nodes, "
        f"{len(data['links'])} edges, {len(tag_facet.labels)} tags, "
        f"{len(job_facet.labels)} jobs, {schema_count} schemas."
    )
