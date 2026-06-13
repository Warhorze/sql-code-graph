"""Integration test: CREATE VIEW lands in the graph as SqlTable.kind='view'.

End-to-end (parser → indexer → DuckDB) guard that a view is distinguishable
from a table in the persisted graph. Asserts observable graph output (the
kind column), not just parser internals.
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def temp_db():
    db = DuckDBBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


def _kind_of(db: DuckDBBackend, name: str) -> str | None:
    rows = db.run_read('SELECT kind FROM "SqlTable" WHERE name = ?', {"name": name})
    return rows[0]["kind"] if rows else None


def test_create_view_persists_kind_view(temp_db, tmp_path):
    """A repo with a CREATE VIEW and a CREATE TABLE persists distinct kinds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.sql").write_text("CREATE TABLE ba.src (a INT, b INT);\n")
    (repo / "v.sql").write_text("CREATE VIEW ba.v AS SELECT a, b FROM ba.src;\n")
    (repo / "t.sql").write_text("CREATE TABLE ba.t AS SELECT a FROM ba.src;\n")

    Indexer().index_repo(repo, dialect=None, db=temp_db, timeout_per_file=30)

    assert _kind_of(temp_db, "v") == "view", "CREATE VIEW should persist kind='view'"
    assert _kind_of(temp_db, "t") == "table", "CREATE TABLE AS should stay kind='table'"
    assert _kind_of(temp_db, "src") == "table", "plain CREATE TABLE should stay kind='table'"
