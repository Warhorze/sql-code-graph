"""Render the self-contained ``sqlcg viz`` HTML.

Plan: plan/sprints/feature_graph_viz.md §"Self-contained bake mechanics".

The emitted file is **self-contained** (the load-bearing acceptance): the
force-graph library is inlined from a vendored asset, and ``DATA`` / ``TAGS`` /
``JOBS`` / ``SCHEMA_CONFIG`` are JSON-baked into the template. There is no
external ``src``/``href``/``fetch``/CDN reference, so the user opens it on
Windows via ``file://``.
"""

from __future__ import annotations

import json
from importlib.resources import files

# Default output filename. The hand-made artifact was ``table_graph.html``;
# keeping the name means the user's existing open-by-double-click workflow is
# unchanged. The CLI default references this constant — never hardcode twice.
DEFAULT_VIZ_OUT = "table_graph.html"

# Schema color palette + neutral "other" hue (baked into SCHEMA_CONFIG so the
# client never invents colors). Matches the curated palette of the prior artifact.
_SCHEMA_PALETTE = [
    "#58a6ff",
    "#3fb950",
    "#f0883e",
    "#d2a8ff",
    "#ff7b72",
    "#ffa657",
    "#79c0ff",
]
_SCHEMA_OTHER_COLOR = "#6e7681"

_LIB_PLACEHOLDER = "/*__FORCE_GRAPH_LIB__*/"
_DATA_PLACEHOLDER = "/*__DATA__*/"
_TAGS_PLACEHOLDER = "/*__TAGS__*/"
_JOBS_PLACEHOLDER = "/*__JOBS__*/"
_SCHEMA_CONFIG_PLACEHOLDER = "/*__SCHEMA_CONFIG__*/"


def _read_asset(name: str) -> str:
    return files("sqlcg.viz.assets").joinpath(name).read_text(encoding="utf-8")


def render_html(
    data: dict,
    tags: dict,
    jobs: dict,
    schemas: list[str],
) -> str:
    """Bake ``data``/``tags``/``jobs``/``schema-config`` into the template.

    Args:
        data: ``{nodes, links, adj}`` from :func:`sqlcg.viz.data.build_viz_data`.
        tags: ``{"labels": [...], "colors": {label: hex}}`` for the tag facet.
        jobs: ``{"labels": [...], "colors": {label: hex}}`` for the job facet.
        schemas: Configured schema list (``[]`` → client derives from the data).

    Returns:
        The full self-contained HTML document as a string.
    """
    lib = _read_asset("force-graph.min.js")
    template = _read_asset("template.html")

    schema_config = {
        "schemas": schemas,
        "palette": _SCHEMA_PALETTE,
        "other": _SCHEMA_OTHER_COLOR,
    }

    # json.dumps with ensure_ascii keeps the bake ASCII-safe; the JSON literal is
    # valid JS and slots into `const X = /*__X__*/;`.
    html = template.replace(_LIB_PLACEHOLDER, lib)
    html = html.replace(_DATA_PLACEHOLDER, json.dumps(data))
    html = html.replace(_TAGS_PLACEHOLDER, json.dumps(tags))
    html = html.replace(_JOBS_PLACEHOLDER, json.dumps(jobs))
    html = html.replace(_SCHEMA_CONFIG_PLACEHOLDER, json.dumps(schema_config))
    return html
