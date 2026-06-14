"""PR-2b (#27a) — index-time backup table-NAME exclusion (Option B).

Acceptance tests for the destructive, opt-in ``[sqlcg.index_filter] enabled``
filter wired into the indexer's Phase-C flush and both post-flush emitters
(clone inheritance + catalog re-apply).  Each test asserts observable graph
state (node/edge presence and counts), never merely "no exception".

The same name excluded at index time is what ``NoiseFilter`` would hide at
query time — both delegate to the core-resident ``TableNameMatcher``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.noise_match import TableNameMatcher
from sqlcg.indexer.indexer import Indexer
from sqlcg.server.noise_filter import NoiseFilter

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    db = DuckDBBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


def _write_corpus(root: Path, files: dict[str, str], toml: str | None = None) -> None:
    for name, sql in files.items():
        (root / name).write_text(sql, encoding="utf-8")
    if toml is not None:
        (root / ".sqlcg.toml").write_text(toml, encoding="utf-8")


def _index(root: Path, db: DuckDBBackend) -> dict:
    return Indexer().index_repo(
        root, dialect="snowflake", db=db, timeout_per_file=30, use_git=False
    )


def _tables(db: DuckDBBackend) -> set[str]:
    return {r["qualified"] for r in db.run_read('SELECT qualified FROM "SqlTable"', {})}


def _node_edge_counts(db: DuckDBBackend) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tbl in ("File", "SqlTable", "SqlColumn", "SqlQuery"):
        counts[tbl] = db.run_read(f'SELECT COUNT(*) AS n FROM "{tbl}"', {})[0]["n"]
    for rel in ("HAS_COLUMN", "DEFINED_IN", "SELECTS_FROM", "COLUMN_LINEAGE"):
        counts[rel] = db.run_read(f'SELECT COUNT(*) AS n FROM "{rel}"', {})[0]["n"]
    return counts


def _has_column_sources(db: DuckDBBackend, table_q: str) -> list[str]:
    rows = db.run_read(
        'SELECT source FROM "HAS_COLUMN" WHERE src_key = ?',
        {"s": table_q},
    )
    return [r["source"] for r in rows]


# A small corpus with a legitimately-named DDL file that declares a backup clone
# INLINE (the case the file-level ignore cannot reach), plus legit tables.
_CORPUS = {
    "dim_date.sql": (
        "CREATE TABLE ba.dim_date (id INT, d DATE);\n"
        "CREATE TABLE ba.dim_date_bck_20240716 CLONE ba.dim_date;\n"
    ),
    "mart.sql": "CREATE TABLE ba.mart_sales AS SELECT id FROM ba.dim_date;\n",
    "legit.sql": (
        "CREATE TABLE wtda.wtda_artikel_eenmalig (id INT);\n"
        "CREATE TABLE ba.fct_orders_20240101 (id INT);\n"
    ),
}

_TOML_ENABLED = "[sqlcg.index_filter]\nenabled = true\n"
_TOML_DISABLED_WITH_PATTERNS = (
    "[sqlcg.index_filter]\nenabled = false\n\n"
    '[sqlcg.noise_filter]\nignore_table_patterns = ["*_bck", "*_bck_*", "*_bck_[0-9]*"]\n'
)


# ---------------------------------------------------------------------------
# Default-OFF guarded no-op
# ---------------------------------------------------------------------------


def test_default_off_with_patterns_is_byte_identical_noop(tmp_path, temp_db):
    """enabled=false WITH patterns defined → graph identical to no-filter.

    Proves the GUARD (not mere absence of config) is the no-op: the .sqlcg.toml
    DOES define backup patterns, but enabled=false leaves the backup clone in.
    """
    no_filter = tmp_path / "nofilter"
    no_filter.mkdir()
    _write_corpus(no_filter, _CORPUS, toml=None)
    db2 = DuckDBBackend(":memory:")
    db2.init_schema()
    try:
        _index(no_filter, db2)
        baseline = _node_edge_counts(db2)
        baseline_tables = _tables(db2)
    finally:
        db2.close()

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    _write_corpus(guarded, _CORPUS, toml=_TOML_DISABLED_WITH_PATTERNS)
    _index(guarded, temp_db)

    assert _node_edge_counts(temp_db) == baseline
    assert _tables(temp_db) == baseline_tables
    # The backup clone is present (not excluded) in the default-off graph.
    assert "ba.dim_date_bck_20240716" in baseline_tables


# ---------------------------------------------------------------------------
# Flag ON — backup table excluded; legit tables untouched; zero orphans
# ---------------------------------------------------------------------------


def test_flag_on_excludes_backup_table(tmp_path, temp_db):
    _write_corpus(tmp_path, _CORPUS, toml=_TOML_ENABLED)
    _index(tmp_path, temp_db)

    tables = _tables(temp_db)
    assert "ba.dim_date_bck_20240716" not in tables, "backup clone must be excluded at index time"
    assert "ba.dim_date" in tables, "legit source table must remain"


def test_flag_on_legit_tables_untouched(tmp_path, temp_db):
    """A legit _eenmalig prod table and a legit dated mart are NOT excluded."""
    _write_corpus(tmp_path, _CORPUS, toml=_TOML_ENABLED)
    _index(tmp_path, temp_db)

    tables = _tables(temp_db)
    assert "wtda.wtda_artikel_eenmalig" in tables, "bare _eenmalig is not a default pattern"
    assert "ba.fct_orders_20240101" in tables, "legit dated mart must not be excluded"


def test_flag_on_clone_target_no_inherited_columns(tmp_path, temp_db):
    """Clone-target parity: the excluded *_bck* CLONE target gets NO
    HAS_COLUMN(source='clone_inherited') columns (post-flush emitter 1)."""
    _write_corpus(tmp_path, _CORPUS, toml=_TOML_ENABLED)
    _index(tmp_path, temp_db)

    sources = _has_column_sources(temp_db, "ba.dim_date_bck_20240716")
    assert sources == [], f"excluded clone target must have no HAS_COLUMN rows, got {sources}"


def test_flag_off_clone_target_does_inherit_columns(tmp_path, temp_db):
    """Control: with the flag OFF the clone target DOES inherit columns —
    proves the clone-inheritance path actually fires for this fixture."""
    _write_corpus(tmp_path, _CORPUS, toml=None)
    _index(tmp_path, temp_db)

    sources = _has_column_sources(temp_db, "ba.dim_date_bck_20240716")
    assert "clone_inherited" in sources, (
        "control: clone target must inherit columns when the filter is off"
    )


def test_flag_on_zero_orphans(tmp_path, temp_db):
    """No COLUMN / HAS_COLUMN / DEFINED_IN / SELECTS_FROM / COLUMN_LINEAGE
    references an excluded table id, after BOTH post-flush emitters ran."""
    _write_corpus(tmp_path, _CORPUS, toml=_TOML_ENABLED)
    _index(tmp_path, temp_db)

    excluded = "ba.dim_date_bck_20240716"
    prefix = excluded + "."

    # SqlColumn nodes owned by the excluded table
    cols = db_count(temp_db, '"SqlColumn"', "table_qualified = ?", excluded)
    assert cols == 0
    # HAS_COLUMN / DEFINED_IN keyed on the excluded table as src
    assert db_count(temp_db, '"HAS_COLUMN"', "src_key = ?", excluded) == 0
    assert db_count(temp_db, '"DEFINED_IN"', "src_key = ?", excluded) == 0
    # SELECTS_FROM with the excluded table as dst
    assert db_count(temp_db, '"SELECTS_FROM"', "dst_key = ?", excluded) == 0
    # COLUMN_LINEAGE referencing any column of the excluded table (src or dst)
    cl = temp_db.run_read(
        'SELECT COUNT(*) AS n FROM "COLUMN_LINEAGE" WHERE src_key LIKE ? OR dst_key LIKE ?',
        {"a": prefix + "%", "b": prefix + "%"},
    )[0]["n"]
    assert cl == 0


def db_count(db: DuckDBBackend, tbl: str, where: str, val: str) -> int:
    return db.run_read(f"SELECT COUNT(*) AS n FROM {tbl} WHERE {where}", {"v": val})[0]["n"]


# ---------------------------------------------------------------------------
# Catalog re-apply parity (post-flush emitter 2)
# ---------------------------------------------------------------------------


def test_flag_on_catalog_reapply_still_excludes_backup(tmp_path, temp_db):
    """Catalog parity: flag ON + a catalog CSV that contains a *_bck* row →
    that table is STILL absent after the catalog re-apply."""
    catalog = tmp_path / "cols.csv"
    catalog.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\n"
        "ba,dim_date,id\n"
        "ba,dim_date_bck_20240716,id\n",  # info_schema lists the physical backup clone
        encoding="utf-8",
    )
    toml = f'[sqlcg.index_filter]\nenabled = true\n\n[sqlcg.catalog]\npath = "{catalog}"\n'
    _write_corpus(tmp_path, _CORPUS, toml=toml)
    _index(tmp_path, temp_db)

    tables = _tables(temp_db)
    assert "ba.dim_date_bck_20240716" not in tables, (
        "catalog re-apply must honour the index-time exclusion"
    )
    # No HAS_COLUMN was re-created for the excluded backup table by the catalog.
    assert _has_column_sources(temp_db, "ba.dim_date_bck_20240716") == []
    # The legit catalog table row was still processed (its DDL column wins by
    # precedence, so the surviving source is 'ddl' — the catalog did not skip it).
    assert "ddl" in _has_column_sources(temp_db, "ba.dim_date")


def test_catalog_reapply_recreates_backup_without_flag(tmp_path, temp_db):
    """Control: WITHOUT the flag, the catalog re-apply DOES re-create the
    backup table — proves the catalog path is the gap the filter must close."""
    catalog = tmp_path / "cols.csv"
    catalog.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\nba,only_bck_20240716,id\n",
        encoding="utf-8",
    )
    toml = f'[sqlcg.catalog]\npath = "{catalog}"\n'
    _write_corpus(tmp_path, {"x.sql": "CREATE TABLE ba.real (id INT);\n"}, toml=toml)
    _index(tmp_path, temp_db)
    assert "ba.only_bck_20240716" in _tables(temp_db)


# ---------------------------------------------------------------------------
# Index-time / query-time parity
# ---------------------------------------------------------------------------


def test_index_and_query_exclusion_parity(tmp_path, temp_db):
    """The same name excluded at index time is what NoiseFilter hides at query
    time — both delegate to the shared core TableNameMatcher."""
    _write_corpus(tmp_path, _CORPUS, toml=_TOML_ENABLED)
    _index(tmp_path, temp_db)

    excluded = "ba.dim_date_bck_20240716"
    assert excluded not in _tables(temp_db)

    matcher = TableNameMatcher.from_config(tmp_path)
    nf = NoiseFilter.from_config(tmp_path)
    assert matcher.matches(excluded) is True
    assert nf.is_noise(excluded) is True
    # And the legit tables are not noise under either.
    assert matcher.matches("ba.dim_date") is False
    assert nf.is_noise("ba.dim_date") is False
