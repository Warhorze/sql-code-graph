"""Coverage no-regression: non-table CREATE DDL is excluded from write-kind metrics.

Pins blast-radius item #7 of the create-kind mislabel bugfix
([plan doc](plan/sprints/bugfix_create_kind_mislabel.md), AC4).

``_WRITE_KINDS = ("INSERT", "MERGE", "CREATE_TABLE", "UPDATE")`` drives
``total_write_queries`` and ``zero_edge_write_queries``. Before the fix a
``CREATE SEQUENCE`` was stored as ``CREATE_TABLE`` and so inflated both counters
(it carries no COLUMN_LINEAGE, so it counted as a "zero-edge write" — a false
unresolved-write gap). After the fix it classifies as ``OTHER`` and drops out of
the write population. This is an honest-accounting correction, not a regression.

Observable oracle: ``collect_coverage`` routed to the in-memory graph returns the
write counts that exclude the sequence; an independent kind census on the same
graph confirms the sequence is stored as ``OTHER``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.cli.coverage import collect_coverage
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def indexed_db(tmp_path):
    """Index a repo with one resolvable INSERT, one CREATE TABLE, one CREATE SEQUENCE.

    Expected write population (kind IN _WRITE_KINDS):
      * ba.src — CREATE TABLE (zero COLUMN_LINEAGE → a zero-edge write)
      * ba.dst — INSERT … SELECT (has COLUMN_LINEAGE)
    The CREATE SEQUENCE classifies as OTHER → NOT in the write population.
    """
    db = DuckDBBackend(":memory:")
    db.init_schema()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.sql").write_text("CREATE TABLE ba.src (a INT);\n")
    (repo / "ins.sql").write_text("INSERT INTO ba.dst SELECT a FROM ba.src;\n")
    (repo / "seq.sql").write_text("CREATE SEQUENCE ba.s START 1;\n")
    Indexer().index_repo(repo, dialect="snowflake", db=db, use_git=False)
    yield db
    db.close()


def _collect(db: DuckDBBackend):
    def _routed(query: str, params: dict):
        return db.run_read(query, params)

    with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed):
        return collect_coverage()


def test_sequence_excluded_from_total_write_queries(indexed_db):
    """total_write_queries counts only INSERT + CREATE TABLE, not the CREATE SEQUENCE."""
    stats = _collect(indexed_db)
    assert stats is not None
    assert stats.total_write_queries == 2, (
        "total_write_queries must count only the INSERT and the CREATE TABLE "
        f"(the CREATE SEQUENCE is OTHER, excluded); got {stats.total_write_queries}. "
        "A value of 3 means the sequence was mislabeled CREATE_TABLE again."
    )


def test_sequence_does_not_inflate_zero_edge_write_queries(indexed_db):
    """zero_edge_write_queries reflects only the genuine zero-edge CREATE TABLE.

    The CREATE SEQUENCE has zero COLUMN_LINEAGE; before the fix it would have
    counted here as a false unresolved-write. Expected = 1 (the CREATE TABLE
    'ba.src' DDL with no SELECT body), not 2.
    """
    stats = _collect(indexed_db)
    assert stats is not None
    assert stats.zero_edge_write_queries == 1, (
        "zero_edge_write_queries must be 1 (the CREATE TABLE DDL only); "
        f"got {stats.zero_edge_write_queries}. A value of 2 means the sequence "
        "was counted as a zero-edge write (the inflation this fix removes)."
    )


def test_sequence_stored_as_other_kind(indexed_db):
    """Independent oracle: the sequence statement is stored as kind=OTHER."""
    rows = indexed_db.run_read('SELECT kind FROM "SqlQuery" WHERE id LIKE ?', {"id": "%seq.sql:0"})
    assert rows and rows[0]["kind"] == "OTHER", f"sequence must be stored as OTHER; got {rows}"
