"""Per-table label facets (tags / jobs) for ``sqlcg viz``.

Plan: plan/sprints/feature_graph_viz.md §"Tags CSV format" + Decision A.

A *facet* is a named, many-to-many mapping from table id (``SqlTable.qualified``,
the node id) to a set of labels, supplied via a BYO CSV. ``tag`` and ``job`` are
both facets driven by this one generic mechanism — the CLI loads ``--tags`` as
the ``tag`` facet (legend swatches: color + filter) and ``--jobs`` as the
``job`` facet (the job dropdown: filter). No graph/external job source.

CSV SHAPE (chosen — documented here and in the CLI ``--help``): one row per
``pattern,label[,color]`` mapping; header required::

    pattern,label,color
    ba.*_bck,backup,#6e7681
    ba.*_bck,deprecated,#d29922
    da.fact_*,facts,
    ia_tableau.ba_wtda_webshop_order,reporting,#58a6ff

Two files of identical shape (``--tags`` and ``--jobs``) rather than a single
``facet`` column, matching the prompt's CLI signature; the facet name is implied
by the flag and passed to :func:`load_facet`.

Matching rules (parity with :mod:`sqlcg.lineage.noise_match` / the
``ignore_table_patterns`` lever):

* ``pattern`` is matched against the node id with :func:`fnmatch.fnmatch`
  (glob: ``*``, ``?``, ``[seq]`` — NOT regex, NOT SQL LIKE). A literal qualified
  name (no glob metachars) is an exact match.
* Matching is **case-insensitive** (both sides lowercased).
* **Many-to-many both directions**: one pattern may carry many labels (a table
  gets several); one label may span many patterns (a label covers many tables).
* Optional ``color`` column: first-wins on conflict (with a printed warning);
  uncolored labels get a stable auto-assigned palette color by sort order.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

# Stable auto-color palette for labels with no explicit color. Same hues as the
# schema palette in the renderer so the two legends look consistent.
_AUTO_PALETTE = [
    "#58a6ff",
    "#3fb950",
    "#f0883e",
    "#d2a8ff",
    "#ff7b72",
    "#ffa657",
    "#79c0ff",
    "#d29922",
    "#a5d6ff",
    "#56d364",
]


@dataclass
class FacetMap:
    """Compiled facet: ordered ``(pattern, label)`` rules + a label->color map.

    ``rules`` preserves CSV order so legend ordering and "first-checked-tag wins"
    coloring are deterministic. ``colors`` maps every label to a hex color (an
    auto color when none was given).
    """

    name: str
    rules: list[tuple[str, str]] = field(default_factory=list)
    colors: dict[str, str] = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        """Labels in first-seen (CSV) order, deduped."""
        seen: dict[str, None] = {}
        for _, label in self.rules:
            seen.setdefault(label, None)
        return list(seen)


def load_facet(path: Path, name: str) -> FacetMap:
    """Parse a facet CSV into a :class:`FacetMap`.

    Args:
        path: CSV file (``pattern,label[,color]``; header required).
        name: Facet name (e.g. ``"tag"`` or ``"job"``).

    Returns:
        A compiled :class:`FacetMap`. An empty mapping (header only, or rows with
        blank pattern/label) yields an empty facet — graceful, never a raise.
    """
    facet = FacetMap(name=name)
    explicit_colors: dict[str, str] = {}

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return facet

    # Skip the header row unconditionally (header required by contract).
    for row in rows[1:]:
        if len(row) < 2:
            continue
        pattern = row[0].strip()
        label = row[1].strip()
        if not pattern or not label:
            continue
        facet.rules.append((pattern.lower(), label))
        color = row[2].strip() if len(row) >= 3 else ""
        if color:
            if label in explicit_colors and explicit_colors[label] != color:
                print(
                    f"warning: facet '{name}' label '{label}' has conflicting "
                    f"colors {explicit_colors[label]!r} and {color!r}; "
                    f"keeping the first ({explicit_colors[label]!r}).",
                    file=sys.stderr,
                )
            else:
                explicit_colors[label] = color

    # Assign colors: explicit first-wins, then stable auto-palette by sort order
    # for the remainder (deterministic across runs).
    facet.colors.update(explicit_colors)
    uncolored = sorted(label for label in facet.labels if label not in facet.colors)
    for i, label in enumerate(uncolored):
        facet.colors[label] = _AUTO_PALETTE[i % len(_AUTO_PALETTE)]

    return facet


def resolve_labels(node_id: str, facet: FacetMap) -> list[str]:
    """Return the sorted, deduped labels a node carries under ``facet``.

    Applies every compiled rule via case-insensitive :func:`fnmatch`. A node
    matched by several patterns collects all their labels (m2m).

    Args:
        node_id: The table id (``SqlTable.qualified``).
        facet: A compiled :class:`FacetMap`.

    Returns:
        Sorted unique label list (``[]`` when nothing matches).
    """
    nid = node_id.lower()
    matched = {label for pattern, label in facet.rules if fnmatch(nid, pattern)}
    return sorted(matched)
