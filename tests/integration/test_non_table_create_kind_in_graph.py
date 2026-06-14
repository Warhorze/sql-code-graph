"""Integration test: non-table CREATE DDL is stored as SqlQuery.kind='OTHER'.

End-to-end (parser → indexer → DuckDB) guard for the create-kind mislabel
bugfix ([plan doc](plan/sprints/bugfix_create_kind_mislabel.md)).

Scope (RESCOPED, kind-only — plan §Scope):
  * AC1/AC2 — a fixture with CREATE SEQUENCE / STAGE plus real CREATE TABLE /
    CTAS controls stores ``SqlQuery.kind = OTHER`` for the non-table DDL and
    ``CREATE_TABLE`` for the genuine tables.
  * AC3 — the ``analyze impact`` query (analyze.py:317 shape) renders the
    sequence's producing statement as ``kind=OTHER`` not ``CREATE_TABLE``.

Explicitly OUT OF SCOPE (plan Follow-up §, blast-radius row #1b): the sequence
name persists as a ``SqlTable`` source node via the kind-independent
``referenced_tables`` → ``indexer.py`` source loop. This fix corrects only the
stored kind; it does NOT remove that phantom node. No assertion here claims the
node is absent.
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


def test_sequence_produces_selects_from_edge(indexed, db):
    """The sequence statement carries a SELECTS_FROM edge to its name node.

    This is the live shape behind the bug — a non-table CREATE that has a
    SELECTS_FROM edge yet was mislabeled CREATE_TABLE. Asserting the edge exists
    proves the reclassified statement is exactly the kind that leaked into
    ``analyze impact`` (NOT a statement with no edges).
    """
    rows = db.run_read(
        'SELECT q.id AS id, q.kind AS kind FROM "SqlQuery" q '
        'JOIN "SELECTS_FROM" sf ON sf.src_key = q.id '
        "WHERE sf.dst_key = ?",
        {"dst": "ba.s"},
    )
    assert len(rows) == 1, f"expected one SELECTS_FROM producer for ba.s, got {rows}"
    assert rows[0]["kind"] == "OTHER", (
        f"the sequence statement with a SELECTS_FROM edge must be kind=OTHER, "
        f"got {rows[0]['kind']!r}"
    )


def test_analyze_impact_renders_sequence_as_other(indexed, db, monkeypatch):
    """AC3: ``analyze impact ba.s`` renders the producing statement kind=OTHER.

    Runs the exact analyze.py:317 query shape against the indexed graph and the
    CLI render path; asserts the kind column shows OTHER, never CREATE_TABLE.
    No assertion is made about the ba.s SqlTable node's existence (out of scope).
    """
    # Direct query (analyze.py:317 shape) — observable kind on the producer row.
    results = db.run_read(
        "SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target"
        ' FROM "SqlTable" t'
        ' JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified'
        ' JOIN "SqlQuery" q ON q.id = sf.src_key'
        " WHERE t.qualified = ? LIMIT 100",
        {"t": "ba.s"},
    )
    kinds = {r["kind"] for r in results}
    assert kinds == {"OTHER"}, (
        f"analyze impact on ba.s must render only kind=OTHER for the sequence producer; got {kinds}"
    )

    # CLI render path: kind=OTHER appears, CREATE_TABLE does not.
    from typer.testing import CliRunner

    from sqlcg.cli.commands.analyze import app

    def _route_to_db(query, params):
        return db.run_read(query, params)

    monkeypatch.setattr("sqlcg.cli.commands.analyze.run_read_routed", _route_to_db)
    result = CliRunner().invoke(app, ["impact", "ba.s", "--raw"])
    assert result.exit_code == 0, result.output
    assert "OTHER" in result.output, (
        f"analyze impact output must contain kind=OTHER for the sequence.\n{result.output}"
    )
    assert "CREATE_TABLE" not in result.output, (
        f"analyze impact must NOT render CREATE_TABLE for the sequence.\n{result.output}"
    )
