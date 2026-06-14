"""Unit tests for the viz per-table facet loader (tags / jobs).

Guards the many-to-many glob facet mechanism of the graph-viz feature
([plan doc](plan/sprints/feature_graph_viz.md), Step 2.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlcg.viz.tags import FacetMap, load_facet, resolve_labels


def _csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "facet.csv"
    p.write_text(body, encoding="utf-8")
    return p


def test_glob_pattern_maps_only_matching_qualified_names(tmp_path: Path) -> None:
    """A glob row tags exactly the matching node ids, not others."""
    facet = load_facet(_csv(tmp_path, "pattern,label,color\nba.*_bck,backup,#6e7681\n"), "tag")
    assert resolve_labels("ba.orders_bck", facet) == ["backup"]
    assert resolve_labels("ba.orders", facet) == []
    assert resolve_labels("da.fact_bck", facet) == []


def test_one_table_matched_by_two_patterns_gets_both_tags(tmp_path: Path) -> None:
    """Many-to-many: a node matched by two patterns collects both labels, deduped+sorted."""
    facet = load_facet(
        _csv(
            tmp_path,
            "pattern,label\nba.*_bck,backup\nba.orders_*,deprecated\n",
        ),
        "tag",
    )
    assert resolve_labels("ba.orders_bck", facet) == ["backup", "deprecated"]


def test_one_tag_spanning_many_patterns_covers_all_matches(tmp_path: Path) -> None:
    """Many-to-many other direction: a single label over several patterns covers all."""
    facet = load_facet(
        _csv(
            tmp_path,
            "pattern,label\nba.*,core\nda.*,core\n",
        ),
        "tag",
    )
    assert resolve_labels("ba.x", facet) == ["core"]
    assert resolve_labels("da.y", facet) == ["core"]
    assert resolve_labels("ia.z", facet) == []


def test_matching_is_case_insensitive(tmp_path: Path) -> None:
    """Pattern matching lowercases both sides (parity with noise_match)."""
    facet = load_facet(_csv(tmp_path, "pattern,label\nBA.Orders_*,backup\n"), "tag")
    assert resolve_labels("ba.orders_bck", facet) == ["backup"]


def test_color_conflict_keeps_first_and_warns(tmp_path: Path, capsys) -> None:
    """A label with two colors keeps the first and prints a warning."""
    facet = load_facet(
        _csv(
            tmp_path,
            "pattern,label,color\nba.a,backup,#111111\nba.b,backup,#222222\n",
        ),
        "tag",
    )
    assert facet.colors["backup"] == "#111111"
    assert "conflicting" in capsys.readouterr().err


def test_uncolored_label_gets_stable_auto_color(tmp_path: Path) -> None:
    """A label with no color is assigned a deterministic palette color."""
    facet = load_facet(_csv(tmp_path, "pattern,label\nba.*,facts\n"), "tag")
    assert facet.colors["facts"].startswith("#")
    # Stable across reloads.
    facet2 = load_facet(_csv(tmp_path, "pattern,label\nba.*,facts\n"), "tag")
    assert facet.colors["facts"] == facet2.colors["facts"]


def test_empty_mapping_yields_empty_facet(tmp_path: Path) -> None:
    """Header-only CSV (or no --tags) → empty facet, no raise (graceful)."""
    facet = load_facet(_csv(tmp_path, "pattern,label,color\n"), "tag")
    assert facet.labels == []
    assert resolve_labels("ba.x", facet) == []
    # An unloaded facet (the no-flag default) is also empty.
    assert resolve_labels("ba.x", FacetMap(name="job")) == []


def test_labels_preserve_csv_order_deduped(tmp_path: Path) -> None:
    """FacetMap.labels are in first-seen CSV order (legend ordering is deterministic)."""
    facet = load_facet(
        _csv(
            tmp_path,
            "pattern,label\nba.*,zeta\nda.*,alpha\nia.*,zeta\n",
        ),
        "tag",
    )
    assert facet.labels == ["zeta", "alpha"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
