"""Integration tests for star-projection graph resolution.

All tests use a real in-memory KuzuDB and cover the complete data path:
  parser -> indexer -> graph -> expansion Cypher -> COLUMN_LINEAGE edges.

REGRESSION GUARD: test_star_expansion_creates_edges and
test_no_ddl_means_no_expansion must NEVER be marked skip or xfail once
they first pass.
"""

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
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")
    return tmp_path


# ---------------------------------------------------------------------------
# DDL column definitions persisted to graph
# ---------------------------------------------------------------------------


def test_ddl_columns_persisted(temp_db, tmp_path):
    """Indexing CREATE TABLE must upsert SqlColumn nodes via HAS_COLUMN edges."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (col1 INT, col2 STRING);\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'BA.src'})-[:HAS_COLUMN]->(c:SqlColumn) "
        "RETURN c.col_name AS n ORDER BY n",
        {},
    )
    assert rows == [{"n": "col1"}, {"n": "col2"}], (
        f"Expected exactly [col1, col2] in order, got: {rows}"
    )


def test_ddl_column_node_properties(temp_db, tmp_path):
    """Each SqlColumn node must have id, col_name, table_qualified set correctly."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (amount DECIMAL);\n")
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


def test_ddl_column_count_in_index_summary(temp_db, tmp_path):
    """index_repo summary must include columns_defined count."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.t (a INT, b INT, c INT);\n")
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # The summary dict must expose how many columns were written
    assert "columns_defined" in result or any("column" in k for k in result), (
        f"index_repo summary must include a column count key. Got keys: {list(result.keys())}"
    )


# ---------------------------------------------------------------------------
# STAR_SOURCE edges persisted to graph
# ---------------------------------------------------------------------------


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


def test_star_source_count_in_summary(temp_db, star_repo):
    """index_repo summary must include star_sources count."""
    result = Indexer().index_repo(star_repo, dialect=None, db=temp_db, use_git=False)

    assert "star_sources" in result or any("star" in k for k in result), (
        f"index_repo must return a star_sources count. Got: {list(result.keys())}"
    )


def test_alias_star_source_edge_has_qualifier(temp_db, tmp_path):
    """SELECT base.* FROM src AS base must produce a STAR_SOURCE edge with qualifier='base'."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    (tmp_path / "etl.sql").write_text("CREATE TABLE BA.tgt AS SELECT base.* FROM BA.src AS base;\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN s.qualifier AS q",
        {},
    )
    assert len(rows) == 1
    assert rows[0]["q"] == "base", f"Expected qualifier='base', got {rows[0]['q']!r}"


# ---------------------------------------------------------------------------
# Expansion Cypher creates COLUMN_LINEAGE edges
# ---------------------------------------------------------------------------


def test_star_expansion_creates_edges(temp_db, tmp_path):
    """
    REGRESSION GUARD — do not mark xfail or skip once this first passes.

    Full pipeline: DDL columns + star ETL -> expansion -> COLUMN_LINEAGE edges.
    """
    (tmp_path / "ddl_src.sql").write_text("CREATE TABLE BA.src (col1 INT, col2 STRING);\n")
    (tmp_path / "ddl_tgt.sql").write_text("CREATE TABLE BA.tgt (col1 INT, col2 STRING);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")
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


def test_star_expansion_idempotent(temp_db, tmp_path):
    """Running _expand_star_sources twice must not create duplicate edges."""
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE BA.src (a INT);\nCREATE TABLE BA.tgt (a INT);\n"
    )
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    indexer = Indexer()
    second_run = indexer._expand_star_sources(temp_db)

    rows = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
        {},
    )
    assert rows[0]["n"] == 1, (
        f"Idempotent expansion must not multiply edges. Second run got {second_run}, "
        f"final count is {rows[0]['n']}, expected 1"
    )


def test_no_ddl_means_no_expansion(temp_db, tmp_path):
    """
    REGRESSION GUARD — do not mark xfail or skip once this first passes.

    When source table has no HAS_COLUMN children, expansion must produce 0 edges
    but the STAR_SOURCE edge must still be present as a breadcrumb.
    """
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.unknown_src;\n")
    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    assert result.get("star_edges_expanded") == 0, "No DDL means no expansion should occur"

    # STAR_SOURCE breadcrumb must still be persisted
    rows = temp_db.run_read(
        "MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n",
        {},
    )
    assert rows[0]["n"] >= 1, "STAR_SOURCE edge must remain even when expansion produces nothing"

    # Zero STAR_EXPANSION edges
    rows2 = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
        {},
    )
    assert rows2[0]["n"] == 0


def test_alias_star_expansion(temp_db, tmp_path):
    """SELECT base.* FROM src AS base must produce STAR_EXPANSION edges via alias resolution."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT, b INT);\n")
    (tmp_path / "etl.sql").write_text("CREATE TABLE BA.tgt AS SELECT base.* FROM BA.src AS base;\n")
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
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
        {},
    )
    assert rows2[0]["n"] == 2


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
# reindex_file cleans up stale STAR_SOURCE edges
# ---------------------------------------------------------------------------


def test_reindex_clears_star_source_when_star_removed(temp_db, tmp_path):
    """After removing SELECT * from an ETL file, reindex must leave zero STAR_SOURCE edges."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Confirm edge exists
    rows = temp_db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    assert rows[0]["n"] >= 1

    # Rewrite ETL without star
    etl.write_text("INSERT INTO BA.tgt SELECT a FROM BA.src;\n")
    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows2 = temp_db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    assert rows2[0]["n"] == 0, "STAR_SOURCE edge must be gone after reindex removes the SELECT *"


def test_reindex_re_expands_star_sources(temp_db, tmp_path):
    """After reindex of an ETL file, STAR_EXPANSION edges must be re-created."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT, b INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Re-index with unchanged ETL — edges must survive (MERGE idempotency)
    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows = temp_db.run_read(
        "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
        {},
    )
    assert rows[0]["n"] == 2, "After reindex, star expansion edges must be re-created (not lost)"


def test_reindex_does_not_multiply_star_source_edges(temp_db, tmp_path):
    """reindex_file must not duplicate STAR_SOURCE edges (MERGE idempotency via DETACH DELETE)."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    etl = tmp_path / "etl.sql"
    etl.write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows_before = temp_db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    count_before = rows_before[0]["n"]

    Indexer().reindex_file(str(etl), temp_db, dialect=None)

    rows_after = temp_db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    count_after = rows_after[0]["n"]

    assert count_after == count_before, (
        f"STAR_SOURCE count changed from {count_before} to {count_after} after reindex. "
        "DETACH DELETE + re-upsert must be net-zero."
    )


# ---------------------------------------------------------------------------
# expansion query method contract
# ---------------------------------------------------------------------------


def test_expand_star_sources_method_exists():
    """Indexer must expose _expand_star_sources(db) -> int."""
    indexer = Indexer()
    assert hasattr(indexer, "_expand_star_sources"), (
        "_expand_star_sources must be defined on Indexer"
    )


def test_index_repo_returns_star_edges_expanded(temp_db, tmp_path):
    """index_repo must include 'star_edges_expanded' in its return dict."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (a INT);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    result = Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)
    assert "star_edges_expanded" in result, (
        f"index_repo summary must have 'star_edges_expanded'. Got keys: {list(result.keys())}"
    )


# ---------------------------------------------------------------------------
# Duplicate DDL detection
# ---------------------------------------------------------------------------


def test_duplicate_ddl_warns(temp_db, tmp_path, caplog):
    """Two files both defining CREATE TABLE BA.src must trigger a structured warning."""
    import logging

    (tmp_path / "ddl_v1.sql").write_text("CREATE TABLE BA.src (col1 INT);\n")
    (tmp_path / "ddl_v2.sql").write_text("CREATE TABLE BA.src (col1 INT, col2 STRING);\n")

    with caplog.at_level(logging.WARNING, logger="sqlcg.indexer.indexer"):
        Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # A WARNING containing both file references must have been emitted
    dup_warnings = [
        r.message for r in caplog.records if r.levelno == logging.WARNING and "BA.src" in r.message
    ]
    assert len(dup_warnings) >= 1, (
        "Expected a logger.warning about duplicate DDL for BA.src. "
        "Add the duplicate-DDL guard to _upsert_parsed_file."
    )
    # The warning must mention both files so the user can locate the conflict
    warn_text = " ".join(dup_warnings)
    assert "ddl_v1.sql" in warn_text or "ddl_v2.sql" in warn_text, (
        "Warning must reference at least one of the conflicting file paths"
    )


def test_duplicate_ddl_error_recorded_in_parsed_errors(temp_db, tmp_path):
    """duplicate_ddl:<table> must appear in the indexer's error channel after duplicate DDL."""
    # We need to reach into parsed.errors; easiest is to patch the indexer
    # and capture what _upsert_parsed_file receives.
    from unittest.mock import patch

    captured_errors: list[list[str]] = []
    original_upsert = Indexer._upsert_parsed_file

    def recording_upsert(self, parsed, db, gold_tables=frozenset(), defined_table_registry=None):
        result = original_upsert(
            self,
            parsed,
            db,
            gold_tables=gold_tables,
            defined_table_registry=defined_table_registry,
        )
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


def test_duplicate_ddl_still_writes_union_columns(temp_db, tmp_path):
    """Despite the warning, both DDL files' columns must reach the graph (union semantics)."""
    (tmp_path / "a.sql").write_text("CREATE TABLE BA.src (col1 INT);\n")
    (tmp_path / "b.sql").write_text("CREATE TABLE BA.src (col2 STRING);\n")

    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    # Exactly one SqlTable node for BA.src
    tables = temp_db.run_read("MATCH (t:SqlTable {qualified: 'BA.src'}) RETURN count(t) AS n", {})
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


# ---------------------------------------------------------------------------
# load-schema writes authoritative HAS_COLUMN edges
# ---------------------------------------------------------------------------


def _get_load_fn():
    """Return a testable load-schema callable, or skip if load_schema is not yet implemented."""
    try:
        from sqlcg.cli.commands.load_schema import load_schema_cmd_test

        return load_schema_cmd_test
    except ImportError:
        pass
    try:
        from sqlcg.cli.commands.load_schema import _load_schema_into_graph

        def _wrap(csv_path, include_catalog=False, db=None):
            _load_schema_into_graph(csv_path, include_catalog, db)

        return _wrap
    except ImportError:
        pytest.skip("load_schema module not yet implemented")


def _csv(path, rows):
    header = "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
    path.write_text(header + "".join(rows), encoding="utf-8")


def test_load_schema_writes_has_column(temp_db, tmp_path):
    """load_schema must write HAS_COLUMN edges tagged source='information_schema'."""
    load = _get_load_fn()
    csv_file = tmp_path / "cols.csv"
    _csv(
        csv_file,
        [
            "mydb,BA,orders,id,1,INT\n",
            "mydb,BA,orders,amount,2,DECIMAL\n",
            "mydb,BA,customers,id,1,INT\n",
            "mydb,BA,customers,name,2,STRING\n",
        ],
    )
    load(csv_path=csv_file, include_catalog=False, db=temp_db)

    rows = temp_db.run_read("MATCH ()-[r:HAS_COLUMN]->() RETURN r.source AS src", {})
    assert len(rows) == 4, f"Expected 4 HAS_COLUMN edges, got {len(rows)}"
    assert all(r["src"] == "information_schema" for r in rows)


def test_gold_schema_suppresses_ddl_columns(temp_db, tmp_path):
    """DDL indexing must not overwrite information_schema HAS_COLUMN edges."""
    load = _get_load_fn()
    csv_file = tmp_path / "cols.csv"
    _csv(csv_file, ["mydb,BA,src,col1,1,INT\n", "mydb,BA,src,col2,2,STRING\n"])
    load(csv_path=csv_file, include_catalog=False, db=temp_db)

    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (col1 INT, col2 STRING, col3 DATE);\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'ba.src'})-[r:HAS_COLUMN]->() RETURN r.source AS src", {}
    )
    assert len(rows) == 2, f"Gold guard must block col3. Got {len(rows)} edges"
    assert all(r["src"] == "information_schema" for r in rows)


def test_partial_csv_leaves_ddl_intact(temp_db, tmp_path):
    """A CSV covering only one table must leave uncovered tables' DDL columns intact."""
    load = _get_load_fn()
    csv_file = tmp_path / "cols.csv"
    _csv(csv_file, ["mydb,BA,other,id,1,INT\n"])
    load(csv_path=csv_file, include_catalog=False, db=temp_db)

    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.src (col1 INT);\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'BA.src'})-[r:HAS_COLUMN]->() RETURN r.source AS src", {}
    )
    assert len(rows) == 1
    assert rows[0]["src"] == "ddl"


def test_load_schema_idempotent(temp_db, tmp_path):
    """Running load_schema twice on the same CSV must not create duplicate edges."""
    load = _get_load_fn()
    csv_file = tmp_path / "cols.csv"
    _csv(csv_file, ["mydb,BA,src,col1,1,INT\n", "mydb,BA,src,col2,2,STRING\n"])
    load(csv_path=csv_file, include_catalog=False, db=temp_db)
    load(csv_path=csv_file, include_catalog=False, db=temp_db)

    rows = temp_db.run_read("MATCH ()-[r:HAS_COLUMN]->() RETURN count(r) AS n", {})
    assert rows[0]["n"] == 2, f"Must not duplicate edges. Got {rows[0]['n']}"


def test_case_insensitive_gold_guard(temp_db, tmp_path):
    """Upper-case CSV names must match lower-case DDL via normalisation."""
    load = _get_load_fn()
    csv_file = tmp_path / "cols.csv"
    _csv(csv_file, ["MYDB,BA,SRC,col1,1,INT\n", "MYDB,BA,SRC,col2,2,STRING\n"])
    load(csv_path=csv_file, include_catalog=False, db=temp_db)

    (tmp_path / "ddl.sql").write_text("CREATE TABLE ba.src (col1 INT, col2 STRING, col3 DATE);\n")
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False)

    rows = temp_db.run_read(
        "MATCH (:SqlTable {qualified: 'ba.src'})-[r:HAS_COLUMN]->() RETURN r.source AS src", {}
    )
    assert len(rows) == 2, f"Case normalisation must block col3. Got {len(rows)} edges"


def test_index_with_manually_loaded_schema_csv(temp_db, tmp_path):
    """CLI-style workflow: load schema CSV first, then index repo."""
    try:
        from sqlcg.cli.commands.load_schema import _load_schema_into_graph
    except ImportError:
        pytest.skip("load_schema not yet implemented")

    sqlcg_dir = tmp_path / ".sqlcg"
    sqlcg_dir.mkdir()
    schema_csv = sqlcg_dir / "schema.csv"
    _csv(
        schema_csv,
        [
            "mydb,BA,src,col1,1,INT\n",
            "mydb,BA,src,col2,2,STRING\n",
        ],
    )
    # Need DDL for target table to enable full expansion
    (tmp_path / "ddl.sql").write_text("CREATE TABLE BA.tgt (col1 INT, col2 STRING);\n")
    (tmp_path / "etl.sql").write_text("INSERT INTO BA.tgt SELECT * FROM BA.src;\n")

    # Manually load the schema CSV (as the CLI does) — this is index.py's responsibility
    _load_schema_into_graph(schema_csv, include_catalog=False, db=temp_db)

    # Then index_repo is called without auto-discovery (schema_csv=None)
    Indexer().index_repo(tmp_path, dialect=None, db=temp_db, use_git=False, schema_csv=None)

    # Verify that the pre-loaded schema edges are present
    rows = temp_db.run_read(
        "MATCH ()-[r:HAS_COLUMN {source: 'information_schema'}]->() RETURN count(r) AS n", {}
    )
    assert rows[0]["n"] >= 2, "Pre-loaded schema.csv must have produced HAS_COLUMN edges"

    # Verify that STAR_SOURCE edges were still detected and (optionally) expanded
    star_sources = temp_db.run_read("MATCH ()-[s:STAR_SOURCE]->() RETURN count(s) AS n", {})
    assert star_sources[0]["n"] >= 1, "index_repo must detect STAR_SOURCE edges"
