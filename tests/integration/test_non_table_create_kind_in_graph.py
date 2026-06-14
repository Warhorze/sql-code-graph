"""Integration test: non-table CREATE DDL is stored as SqlQuery.kind='OTHER'.

End-to-end (parser → indexer → DuckDB) guard for the create-kind mislabel
bugfix ([plan doc](plan/sprints/bugfix_create_kind_mislabel.md)).

Scope (RESCOPED, kind-only — plan §Scope):
  * AC1/AC2 — a fixture with CREATE SEQUENCE / STAGE plus real CREATE TABLE /
    CTAS controls stores ``SqlQuery.kind = OTHER`` for the non-table DDL and
    ``CREATE_TABLE`` for the genuine tables.
  * AC3 — the ``analyze impact`` query (analyze.py:317 shape) renders the
    sequence's producing statement as ``kind=OTHER`` not ``CREATE_TABLE``.

Originally OUT OF SCOPE for #159 (plan Follow-up §, blast-radius row #1b): the
sequence name persisted as a ``SqlTable`` source node via the kind-independent
``referenced_tables`` → ``indexer.py`` source loop. **That phantom node has since
been removed by the PR-B source-gate (#161)**
([plan doc](plan/sprints/sprint_snowflake_lineage_patterns.md)). The two tests
below that previously asserted the phantom SELECTS_FROM edge / ``ba.s`` node
*exists* have been flipped to assert its *absence* — the corrected post-#161
graph shape. The kind-only assertions (AC1/AC2) are unchanged.
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def db():
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def _kind_of_query(db: DuckDBBackend, id_suffix: str) -> str | None:
    """Return SqlQuery.kind for the statement whose id ends with ``id_suffix``."""
    rows = db.run_read(
        'SELECT id, kind FROM "SqlQuery" WHERE id LIKE ?',
        {"id": f"%{id_suffix}"},
    )
    return rows[0]["kind"] if rows else None


@pytest.fixture
def indexed(db, tmp_path):
    """Index a repo with non-table CREATEs (SEQUENCE/STAGE) + table controls.

    On the Snowflake path a ``CREATE SEQUENCE ba.s`` parses with target=None and
    its name ``ba.s`` enters ``sources`` → a ``SELECTS_FROM`` edge to ``ba.s`` and
    a phantom ``SqlTable`` source node (out of scope, see module docstring).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seq.sql").write_text("CREATE SEQUENCE ba.s START 1;\n")
    (repo / "stg.sql").write_text("CREATE STAGE ba.stg;\n")
    (repo / "tbl.sql").write_text("CREATE TABLE ba.t (a INT);\n")
    (repo / "ctas.sql").write_text("CREATE TABLE ba.t2 AS SELECT a FROM ba.t;\n")
    Indexer().index_repo(repo, dialect="snowflake", db=db, use_git=False)
    return repo


def test_create_sequence_stored_as_other(indexed, db):
    """AC1: CREATE SEQUENCE is stored as SqlQuery.kind='OTHER' (not CREATE_TABLE)."""
    assert _kind_of_query(db, "seq.sql:0") == "OTHER", (
        "CREATE SEQUENCE must store kind=OTHER after the catch-all flip; "
        "before the fix it was mislabeled CREATE_TABLE."
    )


def test_create_stage_stored_as_other(indexed, db):
    """AC1: CREATE STAGE is stored as SqlQuery.kind='OTHER'."""
    assert _kind_of_query(db, "stg.sql:0") == "OTHER", (
        "CREATE STAGE must store kind=OTHER, not CREATE_TABLE."
    )


def test_real_create_table_still_create_table(indexed, db):
    """AC2: a genuine CREATE TABLE control still stores kind='CREATE_TABLE'."""
    assert _kind_of_query(db, "tbl.sql:0") == "CREATE_TABLE", (
        "Plain CREATE TABLE must remain CREATE_TABLE (no regression on real tables)."
    )


def test_ctas_still_create_table(indexed, db):
    """AC2: CTAS (CREATE TABLE AS SELECT) control still stores kind='CREATE_TABLE'."""
    assert _kind_of_query(db, "ctas.sql:0") == "CREATE_TABLE", (
        "CTAS has Create.kind=='TABLE' and must remain CREATE_TABLE."
    )


def test_sequence_produces_no_phantom_selects_from_edge(indexed, db):
    """Post-#161: the sequence statement carries NO phantom SELECTS_FROM edge.

    Before PR-B (#161) the sequence's own name ``ba.s`` was scooped into
    ``sources`` and emitted as a ``SELECTS_FROM`` edge from the producing
    statement to a phantom ``ba.s`` SqlTable node. The source-gate suppresses
    this, so no SELECTS_FROM edge points at ``ba.s`` any longer. (Flipped from the
    pre-#161 assertion that the edge existed — see module docstring.)
    """
    rows = db.run_read(
        'SELECT q.id AS id, q.kind AS kind FROM "SqlQuery" q '
        'JOIN "SELECTS_FROM" sf ON sf.src_key = q.id '
        "WHERE sf.dst_key = ?",
        {"dst": "ba.s"},
    )
    assert rows == [], f"phantom SELECTS_FROM edge to ba.s should be gone, got {rows}"


def test_sequence_name_is_not_a_sqltable_node(indexed, db):
    """Post-#161: the sequence object name is not emitted as a SqlTable node.

    The phantom ``ba.s`` SqlTable node previously created by the kind-independent
    source loop is removed by the PR-B source-gate (#161).
    """
    rows = db.run_read('SELECT qualified FROM "SqlTable" WHERE qualified = ?', {"q": "ba.s"})
    assert rows == [], f"phantom SqlTable node ba.s should be gone, got {rows}"


def test_analyze_impact_sequence_node_absent(indexed, db, monkeypatch):
    """Post-#161: ``analyze impact ba.s`` finds no phantom producer.

    The phantom ``ba.s`` node and its SELECTS_FROM edge are removed, so the
    analyze.py:317 query shape returns no rows for ``ba.s`` and the CLI never
    renders a CREATE_TABLE (or any) producer for it. (Flipped from the pre-#161
    assertion that the producer rendered as OTHER — see module docstring.)
    """
    # Direct query (analyze.py:317 shape) — no producer row for the phantom node.
    results = db.run_read(
        "SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target"
        ' FROM "SqlTable" t'
        ' JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified'
        ' JOIN "SqlQuery" q ON q.id = sf.src_key'
        " WHERE t.qualified = ? LIMIT 100",
        {"t": "ba.s"},
    )
    assert results == [], f"no producer should resolve via the removed phantom ba.s; got {results}"

    # CLI render path: CREATE_TABLE must not be rendered for the sequence.
    from typer.testing import CliRunner

    from sqlcg.cli.commands.analyze import app

    def _route_to_db(query, params):
        return db.run_read(query, params)

    monkeypatch.setattr("sqlcg.cli.commands.analyze.run_read_routed", _route_to_db)
    result = CliRunner().invoke(app, ["impact", "ba.s", "--raw"])
    assert result.exit_code == 0, result.output
    assert "CREATE_TABLE" not in result.output, (
        f"analyze impact must NOT render CREATE_TABLE for the sequence.\n{result.output}"
    )
