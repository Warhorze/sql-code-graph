"""Unit tests for the PR-2b (#27a) [sqlcg.index_filter] config + core matcher."""

from __future__ import annotations

from pathlib import Path

from sqlcg.core.config import get_index_filter_enabled
from sqlcg.core.noise_match import TableNameMatcher, table_short_name


def _toml(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".sqlcg.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_index_filter_defaults_off_when_no_config(tmp_path):
    assert get_index_filter_enabled(tmp_path) is False


def test_index_filter_defaults_off_when_block_absent(tmp_path):
    root = _toml(tmp_path, '[sqlcg.noise_filter]\nignore_table_patterns = ["*_bck"]\n')
    # Even with noise_filter patterns defined, the destructive switch is OFF.
    assert get_index_filter_enabled(root) is False


def test_index_filter_reads_true(tmp_path):
    root = _toml(tmp_path, "[sqlcg.index_filter]\nenabled = true\n")
    assert get_index_filter_enabled(root) is True


def test_index_filter_reads_false(tmp_path):
    root = _toml(tmp_path, "[sqlcg.index_filter]\nenabled = false\n")
    assert get_index_filter_enabled(root) is False


def test_index_filter_non_bool_value_ignored(tmp_path):
    root = _toml(tmp_path, '[sqlcg.index_filter]\nenabled = "yes"\n')
    assert get_index_filter_enabled(root) is False


def test_matcher_defaults_match_backup_globs():
    m = TableNameMatcher.from_config(Path.cwd())
    assert m.matches("ba.foo_bck") is True
    assert m.matches("ba.foo_bck_20240716") is True
    assert m.matches("ba.foo") is False
    # bare _eenmalig is deliberately NOT a default table pattern (legit prod table)
    assert m.matches("wtda.wtda_artikel_eenmalig") is False


def test_matcher_callable_shorthand():
    m = TableNameMatcher(patterns=["*_bck"])
    assert m("a.x_bck") is True
    assert m("a.x") is False


def test_matcher_explicit_layers():
    m = TableNameMatcher(
        patterns=["*_tmp"],
        ignored_tables=["ma.rtetl_delta"],
        ignore_regexes=["_bck"],
    )
    assert m.matches("MA.RTETL_DELTA") is True  # exact, case-insensitive
    assert m.matches("a.foo_tmp") is True  # glob
    assert m.matches("a.bar_bck_archive") is True  # unanchored regex
    assert m.matches("a.clean") is False


def test_matcher_invalid_regex_is_skipped_not_raised():
    m = TableNameMatcher(ignore_regexes=["[unclosed"])
    assert m.matches("a.anything") is False


def test_table_short_name_handles_cte_namespace():
    assert table_short_name("path/file.sql::final") == "final"
    assert table_short_name("schema.table") == "table"
    assert table_short_name("bare") == "bare"
