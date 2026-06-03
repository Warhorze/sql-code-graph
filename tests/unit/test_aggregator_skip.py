"""Tests for T-02 (pass-2 skip predicate) — Phase 5 realignment.

CrossFileAggregator.resolve_pass2 was deleted (Phase 5 — zero production call sites).
These tests now drive the production path: _needs_pass2 predicate (lives on aggregator,
still used by index_repo) and the observable end-to-end effect via index_repo on a tiny
two-file fixture.

The identity semantics of the old resolve_pass2 (returns the exact same ParsedFile when
no re-parse needed) no longer apply; the pass2_skipped count in the index_repo summary
is the observable equivalent.
"""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.parsers.base import ParsedFile, QueryNode, TableRef

# Gracefully skip if _needs_pass2 already exists (shouldn't before T-02).
try:
    from sqlcg.lineage.aggregator import CrossFileAggregator as _CA

    _has_predicate = hasattr(_CA(), "_needs_pass2")
except Exception:
    _has_predicate = False


def _make_parsed_file(
    path: Path,
    defined_tables: list[TableRef],
    statement_sources: list[list[TableRef]],
) -> ParsedFile:
    """Build a minimal ParsedFile for predicate testing."""
    pf = ParsedFile(path=path, dialect="snowflake")
    pf.defined_tables = defined_tables
    for sources in statement_sources:
        stmt = QueryNode(file=path, statement_index=0, sql="", kind="SELECT")
        stmt.sources = sources
        pf.statements.append(stmt)
    return pf


# ---------------------------------------------------------------------------
# Scenario A — _needs_pass2 returns False for a file with no cross-file sources
# ---------------------------------------------------------------------------


def test_T02_no_cross_file_sources_skips_pass2(tmp_path):
    """File referencing only its own tables must be skipped in pass 2."""
    if not _has_predicate:
        pytest.fail("CrossFileAggregator._needs_pass2 not found — T-02 not yet implemented")

    sql_a = tmp_path / "a.sql"
    sql_a.write_text("CREATE TABLE db.s.a (x INT);", encoding="utf-8")
    sql_b = tmp_path / "b.sql"
    sql_b.write_text("CREATE TABLE db.s.b (y INT);", encoding="utf-8")

    tbl_a = TableRef(catalog="db", db="s", name="a")
    tbl_b = TableRef(catalog="db", db="s", name="b")

    # f_a.sql references only itself
    f_a = _make_parsed_file(sql_a, defined_tables=[tbl_a], statement_sources=[[tbl_a]])
    # f_b.sql defines b and references only itself
    f_b = _make_parsed_file(sql_b, defined_tables=[tbl_b], statement_sources=[[tbl_b]])

    aggregator = CrossFileAggregator()
    aggregator.register_pass1(f_a)
    aggregator.register_pass1(f_b)

    # f_a references no cross-file tables — predicate must return False
    assert not aggregator._needs_pass2(f_a), (
        "_needs_pass2 returned True for f_a which references only its own tables — "
        "the skip predicate should return False."
    )


# ---------------------------------------------------------------------------
# Scenario B — _needs_pass2 returns True for a file with cross-file source
# ---------------------------------------------------------------------------


def test_T02_cross_file_source_triggers_pass2(tmp_path):
    """File referencing a table defined in another file must have _needs_pass2=True."""
    if not _has_predicate:
        pytest.fail("CrossFileAggregator._needs_pass2 not found — T-02 not yet implemented")

    sql_a = tmp_path / "a.sql"
    sql_a.write_text("SELECT y FROM db.s.b;", encoding="utf-8")
    sql_b = tmp_path / "b.sql"
    sql_b.write_text("CREATE TABLE db.s.b (y INT);", encoding="utf-8")

    tbl_b = TableRef(catalog="db", db="s", name="b")

    # f_a references b which is defined in f_b (cross-file)
    f_a = _make_parsed_file(sql_a, defined_tables=[], statement_sources=[[tbl_b]])
    f_b = _make_parsed_file(sql_b, defined_tables=[tbl_b], statement_sources=[])

    aggregator = CrossFileAggregator()
    aggregator.register_pass1(f_a)
    aggregator.register_pass1(f_b)

    assert aggregator._needs_pass2(f_a), (
        "_needs_pass2 returned False for f_a which references b defined in f_b — "
        "cross-file dependency must trigger pass-2."
    )


# ---------------------------------------------------------------------------
# Scenario E — pass2_skipped is observable in index_repo summary
# ---------------------------------------------------------------------------


def test_T02_pass2_skipped_in_summary(tmp_path):
    """index_repo summary must include pass2_skipped with a positive count.

    For a corpus where at least one file has no cross-file dependencies,
    pass2_skipped must be >= 1 after T-02 lands.
    """
    # Two isolated files — neither references the other.
    (tmp_path / "a.sql").write_text("CREATE VIEW a AS SELECT 1 AS x;", encoding="utf-8")
    (tmp_path / "b.sql").write_text("CREATE VIEW b AS SELECT 2 AS y;", encoding="utf-8")

    db = KuzuBackend(":memory:")
    db.init_schema()
    summary = Indexer().index_repo(tmp_path, "snowflake", db, use_git=False)
    db.close()

    assert "pass2_skipped" in summary, (
        "pass2_skipped key not found in index_repo summary — T-02 not yet implemented"
    )
    assert summary["pass2_skipped"] >= 1, (
        f"pass2_skipped={summary['pass2_skipped']} — expected >= 1 for a corpus "
        "where files have no cross-file dependencies"
    )
