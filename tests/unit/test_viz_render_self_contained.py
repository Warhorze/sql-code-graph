"""Unit tests for the viz HTML renderer self-containment + bake.

Guards the load-bearing self-containment acceptance of the graph-viz feature
([plan doc](plan/sprints/feature_graph_viz.md), Step 3.1). The emitted file must
inline the force-graph lib + DATA/TAGS/JOBS/SCHEMA_CONFIG with no external
resource-loading reference, so it opens on Windows via file://.
"""

from __future__ import annotations

import json
import re

import pytest

from sqlcg.viz.render import DEFAULT_VIZ_OUT, render_html

# Tokens that would LOAD an external resource. W3C namespace URIs inside the
# minified lib (xmlns="http://www.w3.org/...") are identifiers, never fetched,
# so a bare "http" ban is wrong — these are the real loading constructs.
_EXTERNAL_LOAD_TOKENS = (
    'src="http',
    "src='http",
    'src="//',
    'href="http',
    'href="//',
    "fetch(",
    "<link",
    "import(",
    'from "http',
    "cdnjs",
    "unpkg",
    "jsdelivr",
)


def _sample() -> str:
    data = {
        "nodes": [
            {
                "id": "ba.x",
                "schema": "ba",
                "kind": "table",
                "deg": 1,
                "cat": True,
                "jobs": ["ej_daily"],
                "tags": ["backup"],
            }
        ],
        "links": [{"source": "ba.x", "target": "da.y", "n": 2}],
        "adj": {"ba.x": ["da.y"], "da.y": ["ba.x"]},
    }
    tags = {"labels": ["backup"], "colors": {"backup": "#6e7681"}}
    jobs = {"labels": ["ej_daily"], "colors": {"ej_daily": "#58a6ff"}}
    return render_html(data, tags, jobs, ["ba", "da"])


def test_default_out_constant_value() -> None:
    """DEFAULT_VIZ_OUT preserves the historical artifact filename."""
    assert DEFAULT_VIZ_OUT == "table_graph.html"


def test_no_external_resource_loading_tokens() -> None:
    """The emitted HTML loads no external resource (CDN/src/href/fetch)."""
    html = _sample()
    for tok in _EXTERNAL_LOAD_TOKENS:
        assert tok not in html, f"external-load token present: {tok!r}"


def test_lib_and_data_are_inlined() -> None:
    """force-graph lib + the DATA/TAGS/JOBS/SCHEMA_CONFIG consts are baked in."""
    html = _sample()
    assert "ForceGraph" in html  # lib sentinel
    assert "force-graph 1.49.5" in html  # vendored version comment
    assert "const DATA = " in html
    assert "const TAGS = " in html
    assert "const JOBS = " in html
    assert "const SCHEMA_CONFIG = " in html


def test_baked_data_round_trips() -> None:
    """The baked DATA JSON parses back and carries the node fields + adj."""
    html = _sample()
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    assert m is not None
    data = json.loads(m.group(1))
    node = data["nodes"][0]
    assert {"id", "schema", "kind", "deg", "cat", "jobs", "tags"} <= set(node)
    assert data["adj"] == {"ba.x": ["da.y"], "da.y": ["ba.x"]}
    assert data["links"][0]["n"] == 2


def test_schema_config_carries_configured_schemas() -> None:
    """SCHEMA_CONFIG bakes the configured schema list + a palette."""
    html = _sample()
    m = re.search(r"const SCHEMA_CONFIG = (\{.*?\});", html, re.S)
    assert m is not None
    cfg = json.loads(m.group(1))
    assert cfg["schemas"] == ["ba", "da"]
    assert isinstance(cfg["palette"], list) and cfg["palette"]
    assert cfg["other"].startswith("#")


def test_kind_defaults_table_view_temp_on_cte_derived_off() -> None:
    """The template wires table/view/temp ON and cte/derived OFF by default."""
    html = _sample()
    assert "KIND_DEFAULT_ON = new Set(['table', 'view', 'temp'])" in html
    assert "KNOWN_KINDS = ['table', 'view', 'temp', 'cte', 'derived']" in html
    # external is never a kind toggle.
    assert "'external'" not in html


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
