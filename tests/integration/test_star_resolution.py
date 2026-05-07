"""Integration tests for star-projection graph resolution.

Sprint: sprint_star_resolution.md  Tickets: T-01, T-04, T-05, T-06

All tests are xfail until the full sprint implementation lands.
They use a real in-memory KuzuDB and cover the complete data path:
  parser -> indexer -> graph -> expansion Cypher -> COLUMN_LINEAGE edges.

REGRESSION GUARD: test_star_expansion_creates_edges and
test_no_ddl_means_no_expansion must NEVER be marked skip or xfail once
they first pass.
"""

from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def temp_db():
    """Fresh in-memory KuzuDB with schema initialised."""
    db = KuzuBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


@pytest.fixture
def star_repo(tmp_path):
    """Minimal two-file repo: DDL + star ETL."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.src (col1 INT, col2 STRING);\n"
        "CREATE TABLE BA.tgt (col1 INT, col2 STRING);\n"
    )
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO BA.tgt SELECT * FROM BA.src;\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# T-01 — DDL column definitions persisted to graph
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="HAS_COLUMN writes not yet implemented — T-01", strict=True)
def test_ddl_columns_persisted(temp_db, tmp_path):
    """Indexing CREATE TABLE must upsert SqlColumn nodes via HAS_COLUMN edges."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.src (col1 INT, col2 STRING);\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'BA.src'})-[:HAS_COLUMN]->(c:SqlColumn) "
        "RETURN c.col_name AS n ORDER BY n",
        {},
    )
    assert rows == [{"n": "col1"}, {"n": "col2"}], (
        f"Expected exactly [col1, col2] in order, got: {rows}"
    )


@pytest.mark.xfail(reason="HAS_COLUMN writes not yet implemented — T-01", strict=True)
def test_ddl_column_node_properties(temp_db, tmp_path):
    """Each SqlColumn node must have id, col_name, table_qualified set correctly."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.src (amount DECIMAL);\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (c:SqlColumn {id: 'BA.src.amount'}) "
        "RETURN c.id AS id, c.col_name AS n, c.table_qualified AS tq",
        {},
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "BA.src.amount"
    assert rows[0]["n"] == "amount"
    assert rows[0]["tq"] == "BA.src"


@pytest.mark.xfail(reason="HAS_COLUMN writes not yet implemented — T-01", strict=True)
def test_ddl_column_count_in_index_summary(temp_db, tmp_path):
    """index_repo summary must include columns_defined count after T-01."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.t (a INT, b INT, c INT);\n"
    )
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # The summary dict must expose how many columns were written
    assert "columns_defined" in result or any(
        "column" in k for k in result
    ), (
        f"index_repo summary must include a column count key. Got keys: {list(result.keys())}"
    )


# ---------------------------------------------------------------------------
# T-04 — STAR_SOURCE edges persisted to graph
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="STAR_SOURCE edge upserts not yet implemented — T-04", strict=True)
def test_star_source_edge_persisted(temp_db, star_repo):
    """INSERT INTO tgt SELECT * FROM src must produce one STAR_SOURCE edge."""
    Indexer().index_repo(star_repo, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (q:SqlQuery)-[s:STAR_SOURCE]->(t:SqlTable) "
        "RETURN t.qualified AS src, s.qualifier AS q, "
        "s.target_table AS tgt, s.confidence AS conf",
        {},
    )
    assert len(rows) == 1, f"Expected 1 STAR_SOURCE edge, got {len(rows)}: {rows}"
    assert rows[0]["src"] == "BA.src"
    assert rows[0]["q"] == "<unqualified>"
    assert rows[0]["tgt"] == "BA.tgt"
    assert abs(rows[0]["conf"] - 0.8) < 1e-6


@pytest.mark.xfail(reason="STAR_SOURCE edge upserts not yet implemented — T-04", strict=True)
def test_star_source_count_in_summary(temp_db, star_repo):
    """index_repo summary must include star_sources count."""
    result = Indexer().index_repo(star_repo, dialect=None, db=temp_db, use_git=False)

    assert "star_sources" in result or any(
        "star" in k for k in result
    ), (
        f"index_repo must return a star_sources count. Got: {list(result.keys())}"
    )


@pytest.mark.xfail(reason="STAR_SOURCE edge upserts not yet implemented — T-04", strict=True)
def test_alias_star_source_edge_has_qualifier(temp_db, tmp_path):
    """SELECT base.* FROM src AS base must produce a STAR_SOURCE edge with qualifier='base'."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    (tmp_path / "etl.sql").write_text(
        "CREATE TABLE BA.tgt AS SELECT base.* FROM BA.src AS base;\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN s.qualifier AS q",
        {},
    )
    assert len(rows) == 1
    assert rows[0]["q"] == "base", (
        f"Expected qualifier='base', got {rows[0]['q']!r}"
    )


# ---------------------------------------------------------------------------
# T-05 — Expansion Cypher creates COLUMN_LINEAGE edges
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="Expansion Cypher not yet implemented — T-05", strict=True)
def test_star_expansion_creates_edges(temp_db, tmp_path):
    """
    REGRESSION GUARD — do not mark xfail or skip once this first passes.

    Full pipeline: DDL columns + star ETL -> expansion -> COLUMN_LINEAGE edges.
    """
    (tmp_path / "ddl_src.sql").write_text(
        "CREATE TABLE BA.src (col1 INT, col2 STRING);\n"
    )
    (tmp_path / "ddl_tgt.sql").write_text(
        "CREATE TABLE BA.tgt (col1 INT, col2 STRING);\n"
    )
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO BA.tgt SELECT * FROM BA.src;\n"
    )
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    assert result.get("star_edges_expanded") == 2, (
        f"Expected star_edges_expanded=2, got {result.get('star_edges_expanded')}"
    )

    rows = temp_db.run_read(
        "MATCH (s:SqlColumn)-[r:COLUMN_LINEAGE]->(d:SqlColumn) "
        "WHERE r.transform = 'STAR_EXPANSION' "
        "RETURN s.id AS src, d.id AS dst, r.confidence AS c "
        "ORDER BY src",
        {},
    )
    assert rows == [
        {"src": "BA.src.col1", "dst": "BA.tgt.col1", "c": pytest.approx(0.8)},
        {"src": "BA.src.col2", "dst": "BA.tgt.col2", "c": pytest.approx(0.8)},
    ], f"Unexpected expansion edges: {rows}"


@pytest.mark.xfail(reason="Expansion Cypher not yet implemented — T-05", strict=True)
def test_star_expansion_idempotent(temp_db, tmp_path):
    """Running _expand_star_sources twice must not create duplicate edges."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    indexer = Indexer()
    second_run = indexer._expand_star_sources(temp_db)
    assert second_run == 0, (
        f"Second expansion run must produce 0 new edges (MERGE idempotency). Got {second_run}"
    )

    rows = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() "
        "RETURN count(r) AS n",
        {},
    )
    assert rows[0]["n"] == 1, (
        "Idempotent expansion must not multiply edges"
    )


@pytest.mark.xfail(reason="Expansion Cypher not yet implemented — T-05", strict=True)
def test_no_ddl_means_no_expansion(temp_db, tmp_path):
    """
    REGRESSION GUARD — do not mark xfail or skip once this first passes.

    When source table has no HAS_COLUMN children, expansion must produce 0 edges
    but the STAR_SOURCE edge must still be present as a breadcrumb.
    """
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO BA.tgt SELECT * FROM BA.unknown_src;\n"
    )
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    assert result.get("star_edges_expanded") == 0, (
        "No DDL means no expansion should occur"
    )

    # STAR_SOURCE breadcrumb must still be persisted
    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n",
        {},
    )
    assert rows[0]["n"] >= 1, (
        "STAR_SOURCE edge must remain even when expansion produces nothing"
    )

    # Zero STAR_EXPANSION edges
    rows2 = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() "
        "RETURN count(r) AS n",
        {},
    )
    assert rows2[0]["n"] == 0


@pytest.mark.xfail(reason="Expansion Cypher not yet implemented — T-05", strict=True)
def test_alias_star_expansion(temp_db, tmp_path):
    """SELECT base.* FROM src AS base must produce STAR_EXPANSION edges via alias resolution."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.src (a INT, b INT);\n"
    )
    (tmp_path / "etl.sql").write_text(
        "CREATE TABLE BA.tgt AS SELECT base.* FROM BA.src AS base;\n"
    )
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    assert result.get("star_edges_expanded") == 2

    # Qualifier must be stored correctly
    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN s.qualifier AS q",
        {},
    )
    assert rows[0]["q"] == "base"

    # Two expansion edges created
    rows2 = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() "
        "RETURN count(r) AS n",
        {},
    )
    assert rows2[0]["n"] == 2


@pytest.mark.xfail(reason="Expansion Cypher not yet implemented — T-05", strict=True)
def test_expansion_edge_transform_and_query_id(temp_db, tmp_path):
    """COLUMN_LINEAGE edges from expansion must carry transform='STAR_EXPANSION' and query_id."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (x INT);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() "
        "RETURN r.transform AS t, r.confidence AS c, r.query_id AS qid",
        {},
    )
    assert len(rows) >= 1
    assert rows[0]["t"] == "STAR_EXPANSION"
    assert abs(rows[0]["c"] - 0.8) < 1e-6
    assert rows[0]["qid"] is not None and rows[0]["qid"] != ""


# ---------------------------------------------------------------------------
# T-06 — reindex_file cleans up stale STAR_SOURCE edges
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="reindex_file expansion not yet implemented — T-06", strict=True)
def test_reindex_clears_star_source_when_star_removed(temp_db, tmp_path):
    """After removing SELECT * from an ETL file, reindex must leave zero STAR_SOURCE edges."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Confirm edge exists
    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {}
    )
    assert rows[0]["n"] >= 1

    # Rewrite ETL without star
    etl.write_text("INSERT INTO BA.tgt SELECT a FROM BA.src;\n")
    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows2 = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {}
    )
    assert rows2[0]["n"] == 0, (
        "STAR_SOURCE edge must be gone after reindex removes the SELECT *"
    )


@pytest.mark.xfail(reason="reindex_file expansion not yet implemented — T-06", strict=True)
def test_reindex_re_expands_star_sources(temp_db, tmp_path):
    """After reindex of an ETL file, STAR_EXPANSION edges must be re-created."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT, b INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Re-index with unchanged ETL — edges must survive (MERGE idempotency)
    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() "
        "RETURN count(r) AS n",
        {},
    )
    assert rows[0]["n"] == 2, (
        "After reindex, star expansion edges must be re-created (not lost)"
    )


@pytest.mark.xfail(reason="reindex_file expansion not yet implemented — T-06", strict=True)
def test_reindex_does_not_multiply_star_source_edges(temp_db, tmp_path):
    """reindex_file must not duplicate STAR_SOURCE edges (MERGE idempotency via DETACH DELETE)."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows_before = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {}
    )
    count_before = rows_before[0]["n"]

    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows_after = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {}
    )
    count_after = rows_after[0]["n"]

    assert count_after == count_before, (
        f"STAR_SOURCE count changed from {count_before} to {count_after} after reindex. "
        "DETACH DELETE + re-upsert must be net-zero."
    )


# ---------------------------------------------------------------------------
# T-05 — expansion query method contract
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="_expand_star_sources method not yet implemented — T-05", strict=True)
def test_expand_star_sources_method_exists():
    """Indexer must expose _expand_star_sources(db) -> int."""
    indexer = Indexer()
    assert hasattr(indexer, "_expand_star_sources"), (
        "_expand_star_sources must be defined on Indexer"
    )


@pytest.mark.xfail(reason="star_edges_expanded key not yet in index_repo summary — T-05", strict=True)
def test_index_repo_returns_star_edges_expanded(temp_db, tmp_path):
    """index_repo must include 'star_edges_expanded' in its return dict."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)
    assert "star_edges_expanded" in result, (
        f"index_repo summary must have 'star_edges_expanded'. Got keys: {list(result.keys())}"
    )


# ---------------------------------------------------------------------------
# T-01 — Duplicate DDL detection
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="duplicate DDL guard not yet implemented — T-01", strict=True)
def test_duplicate_ddl_warns(temp_db, tmp_path, caplog):
    """Two files both defining CREATE TABLE BA.src must trigger a structured warning."""
    import logging

    (tmp_path / "ddl_v1.sql").write_text(
        "CREATE TABLE BA.src (col1 INT);\n"
    )
    (tmp_path / "ddl_v2.sql").write_text(
        "CREATE TABLE BA.src (col1 INT, col2 STRING);\n"
    )

    with caplog.at_level(logging.WARNING, logger="sqlcg.indexer.indexer"):
        Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # A WARNING containing both file references must have been emitted
    dup_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "BA.src" in r.message
    ]
    assert len(dup_warnings) >= 1, (
        "Expected a logger.warning about duplicate DDL for BA.src. "
        "Add the duplicate-DDL guard to _upsert_parsed_file as described in T-01."
    )
    # The warning must mention both files so the user can locate the conflict
    warn_text = " ".join(dup_warnings)
    assert "ddl_v1.sql" in warn_text or "ddl_v2.sql" in warn_text, (
        "Warning must reference at least one of the conflicting file paths"
    )


@pytest.mark.xfail(reason="duplicate DDL guard not yet implemented — T-01", strict=True)
def test_duplicate_ddl_error_recorded_in_parsed_errors(temp_db, tmp_path):
    """duplicate_ddl:<table> must appear in the indexer's error channel after duplicate DDL."""
    # We need to reach into parsed.errors; easiest is to patch the indexer
    # and capture what _upsert_parsed_file receives.
    from unittest.mock import patch

    captured_errors: list[list[str]] = []
    original_upsert = Indexer._upsert_parsed_file

    def recording_upsert(self, parsed, db):
        result = original_upsert(self, parsed, db)
        captured_errors.append(list(parsed.errors))
        return result

    (tmp_path / "a.sql").write_text("CREATE TABLE BA.t (x INT);\n")
    (tmp_path / "b.sql").write_text("CREATE TABLE BA.t (x INT, y INT);\n")

    with patch.object(Indexer, "_upsert_parsed_file", recording_upsert):
        Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    all_errors = [e for errors in captured_errors for e in errors]
    dup_errors = [e for e in all_errors if e.startswith("duplicate_ddl:")]
    assert len(dup_errors) >= 1, (
        f"Expected at least one duplicate_ddl: error entry. All errors: {all_errors}"
    )
    assert "BA.t" in dup_errors[0], (
        f"duplicate_ddl error must name the conflicting table. Got: {dup_errors[0]}"
    )


@pytest.mark.xfail(reason="duplicate DDL guard not yet implemented — T-01", strict=True)
def test_duplicate_ddl_still_writes_union_columns(temp_db, tmp_path):
    """Despite the warning, both DDL files' columns must reach the graph (union semantics)."""
    (tmp_path / "a.sql").write_text("CREATE TABLE BA.src (col1 INT);\n")
    (tmp_path / "b.sql").write_text("CREATE TABLE BA.src (col2 STRING);\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Exactly one SqlTable node for BA.src
    tables = temp_db.run_read(
        "MATCH (t:SqlTable {qualified: 'BA.src'}) RETURN count(t) AS n", {}
    )
    assert tables[0]["n"] == 1, "Duplicate DDL must not create duplicate SqlTable nodes"

    # Both columns must be present (union of both DDL files)
    cols = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'BA.src'})-[:HAS_COLUMN]->(c:SqlColumn) "
        "RETURN c.col_name AS n ORDER BY n",
        {},
    )
    col_names = [r["n"] for r in cols]
    assert "col1" in col_names, "col1 from first DDL file must be in the graph"
    assert "col2" in col_names, "col2 from second DDL file must be in the graph"
