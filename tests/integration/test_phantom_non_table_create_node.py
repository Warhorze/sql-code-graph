"""Non-table CREATE objects must not become SqlTable nodes / SELECTS_FROM edges.

Guards the phantom-source-node bugfix (#161)
([plan doc](plan/sprints/sprint_snowflake_lineage_patterns.md), PR-B).

#159 fixed the kind label; the phantom SOURCE node persisted. For a
`CREATE SEQUENCE ba.s` the object's own name was scooped into
`QueryNode.sources` and then leaked into the graph as a `SqlTable` node with a
`SELECTS_FROM` edge. The source-gate in `AnsiParser._parse_statement` suppresses
this. This integration test indexes a mixed fixture and asserts on the resulting
graph structure (observable output), confirming:

- the SEQUENCE / STAGE / FILE FORMAT names are NOT SqlTable nodes and have NO
  SELECTS_FROM edge;
- a real `INSERT INTO ba.t SELECT ... FROM ba.real` keeps its `ba.real -> ba.t`
  lineage (gate edge-neutral for real tables).
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# A non-table CREATE batch + a genuine INSERT...SELECT control, one file.
_FIXTURE_SQL = """\
CREATE SEQUENCE IF NOT EXISTS ba.wsdh_s1;
CREATE STAGE ba.msstg_ingest URL='s3://bucket/x';
CREATE FILE FORMAT ba.msfmt_parquet TYPE=PARQUET;
INSERT INTO ba.t SELECT a FROM ba.real;
"""

_PHANTOM_NAMES = ("ba.wsdh_s1", "ba.msstg_ingest", "ba.msfmt_parquet")


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def _table_nodes(db: DuckDBBackend) -> set[str]:
    rows = db.run_read('SELECT qualified FROM "SqlTable"', {})
    return {r["qualified"] for r in rows if r["qualified"]}


def _selects_from_pairs(db: DuckDBBackend) -> set[tuple[str, str]]:
    rows = db.run_read('SELECT src_key, dst_key FROM "SELECTS_FROM"', {})
    return {(r["src_key"], r["dst_key"]) for r in rows}


def test_non_table_create_objects_absent_real_lineage_present(db, tmp_path):
    """SEQUENCE/STAGE/FILE FORMAT names absent; real INSERT...SELECT intact (AC-B3/B4)."""
    (tmp_path / "fixture.sql").write_text(_FIXTURE_SQL)
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    nodes = _table_nodes(db)

    # AC-B3: no SqlTable node for any non-table CREATE object.
    for name in _PHANTOM_NAMES:
        assert name not in nodes, f"phantom SqlTable node leaked for {name}: {sorted(nodes)}"

    # AC-B3: no SELECTS_FROM edge touches a phantom name (as src or dst).
    pairs = _selects_from_pairs(db)
    for name in _PHANTOM_NAMES:
        assert not any(name in pair for pair in pairs), (
            f"phantom SELECTS_FROM edge touches {name}: {sorted(pairs)}"
        )

    # AC-B4 control: the real source/target survive. SELECTS_FROM is
    # file -> source_table, so the real source must appear as a SELECTS_FROM dst.
    assert "ba.real" in nodes, f"real source ba.real missing from nodes: {sorted(nodes)}"
    assert "ba.t" in nodes, f"real target ba.t missing from nodes: {sorted(nodes)}"
    assert any(dst == "ba.real" for _src, dst in pairs), (
        f"real SELECTS_FROM edge into ba.real missing: {sorted(pairs)}"
    )
