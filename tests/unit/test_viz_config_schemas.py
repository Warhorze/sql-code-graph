"""Unit tests for the [sqlcg.viz] schemas config reader.

Guards the viz config layer of the graph-viz feature
([plan doc](plan/sprints/feature_graph_viz.md), Step 1.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlcg.core.config import get_viz_schemas


def _write_toml(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".sqlcg.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_get_viz_schemas_returns_configured_list(tmp_path: Path) -> None:
    """A [sqlcg.viz] schemas list is read back verbatim in order."""
    root = _write_toml(
        tmp_path,
        '[sqlcg.viz]\nschemas = ["ba", "da", "ia_analytics"]\n',
    )
    assert get_viz_schemas(root) == ["ba", "da", "ia_analytics"]


def test_get_viz_schemas_absent_block_returns_empty(tmp_path: Path) -> None:
    """No [sqlcg.viz] block → [] so the generator falls back to data-derived."""
    root = _write_toml(tmp_path, '[sqlcg]\ndialect = "snowflake"\n')
    assert get_viz_schemas(root) == []


def test_get_viz_schemas_no_config_file_returns_empty(tmp_path: Path) -> None:
    """No .sqlcg.toml at all → [] (zero-config small-repo path)."""
    assert get_viz_schemas(tmp_path) == []


def test_get_viz_schemas_non_list_value_does_not_raise(tmp_path: Path) -> None:
    """A non-list schemas value is ignored without raising → []."""
    root = _write_toml(tmp_path, '[sqlcg.viz]\nschemas = "ba"\n')
    assert get_viz_schemas(root) == []


def test_get_viz_schemas_drops_non_string_entries(tmp_path: Path) -> None:
    """Non-string entries inside the list are filtered out."""
    root = _write_toml(tmp_path, '[sqlcg.viz]\nschemas = ["ba", 7, "da"]\n')
    assert get_viz_schemas(root) == ["ba", "da"]


def test_get_viz_schemas_malformed_toml_does_not_raise(tmp_path: Path) -> None:
    """Malformed TOML is swallowed by the except guard → []."""
    root = _write_toml(tmp_path, "[sqlcg.viz\nschemas = broken")
    assert get_viz_schemas(root) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
