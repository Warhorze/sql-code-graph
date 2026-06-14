"""IDENTIFIER($var) resolution produces real graph SELECTS_FROM / COLUMN_LINEAGE edges.

Indexes a Snowflake file whose source/target tables are reached via
``FROM IDENTIFIER($v)`` / ``INSERT INTO IDENTIFIER($v)`` after a ``SET v='…'``
binding.  Before PR-A these resolved to empty-id tables and produced no real edge;
after PR-A the literal-bound table is recovered and a SELECTS_FROM + COLUMN_LINEAGE
edge lands in the graph.

Guards the PR-A IDENTIFIER($var) indirection plan
([plan doc](../../../plan/sprints/sprint_snowflake_lineage_patterns.md)).
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

_DDL = """\
CREATE TABLE da.stibat_x (id NUMBER);
CREATE TABLE da.sink (id NUMBER);
CREATE TABLE raw.src (a NUMBER);
CREATE TABLE da.result (a NUMBER);
"""

# SET-bound FROM IDENTIFIER (source indirection) + SET-bound INSERT INTO IDENTIFIER
# (target indirection).
_IDENTIFIER_DML = """\
SET TABLE_STIBAT='da.stibat_x';
INSERT INTO da.sink SELECT sti.id AS id FROM identifier($TABLE_STIBAT) sti;

SET tgt='da.result';
INSERT INTO IDENTIFIER($tgt) SELECT s.a AS a FROM raw.src s;
"""


@pytest.fixture()
def indexed_db(tmp_path):
    db = DuckDBBackend(":memory:")
    db.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL)
    (tmp_path / "etl.sql").write_text(_IDENTIFIER_DML)
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db)
    yield db
    db.close()


def test_from_identifier_resolves_selects_from_edge(indexed_db):
    """SELECTS_FROM edge from the INSERT query to the SET-resolved source da.stibat_x."""
    rows = indexed_db.run_read(
        'SELECT t.qualified AS q FROM "SELECTS_FROM" e '
        'JOIN "SqlTable" t ON e.dst_key = t.qualified '
        "WHERE t.qualified LIKE '%stibat_x%'",
        {},
    )
    assert any("stibat_x" in r["q"] for r in rows), (
        f"expected a SELECTS_FROM edge into da.stibat_x, got {rows}"
    )


def test_from_identifier_resolves_column_lineage_edge(indexed_db):
    """COLUMN_LINEAGE edge da.stibat_x.id -> da.sink.id (the IDENTIFIER source resolved)."""
    rows = indexed_db.run_read(
        'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" '
        "WHERE src_key LIKE '%stibat_x%' AND dst_key LIKE '%sink%'",
        {},
    )
    assert len(rows) >= 1, (
        f"expected COLUMN_LINEAGE da.stibat_x.id -> da.sink.id, got none "
        f"({indexed_db.run_read('SELECT src_key, dst_key FROM "COLUMN_LINEAGE"', {})})"
    )


def test_insert_into_identifier_resolves_target_node(indexed_db):
    """INSERT INTO IDENTIFIER($tgt) target resolves to da.result with a real source edge."""
    rows = indexed_db.run_read(
        'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" '
        "WHERE src_key LIKE '%raw.src%' AND dst_key LIKE '%result%'",
        {},
    )
    assert len(rows) >= 1, (
        f"expected COLUMN_LINEAGE raw.src.a -> da.result.a, got none "
        f"({indexed_db.run_read('SELECT src_key, dst_key FROM "COLUMN_LINEAGE"', {})})"
    )


def test_no_empty_id_table_node_for_resolved_identifier(indexed_db):
    """No empty-id phantom SqlTable node leaks for the resolved IDENTIFIER tables."""
    rows = indexed_db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE qualified = '' OR qualified IS NULL",
        {},
    )
    assert rows == [], f"an empty-id SqlTable node leaked: {rows}"
