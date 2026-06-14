"""Integration tests for SELECT-* temp-promote column lineage (Bug #4 / #2).

All tests use a real in-memory DuckDB and the full path
``parser -> indexer -> graph -> star expansion -> COLUMN_LINEAGE edges``,
asserting the observable edges (never "no exception raised").

Root cause guarded here: a temp built as ``CREATE TEMP t AS SELECT * FROM src``
has no own HAS_COLUMN rows, so the promote out of it (``INSERT INTO persisted
SELECT * FROM t``) could expand to nothing when ``src`` is catalogued only via
information_schema — because the star expansion ran BEFORE the catalog was
applied. PR-4 re-runs the expansion after the catalog re-apply.

Guards the temp-promote star recall plan
([plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §PR-4 / §Bug #2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def db():
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def _lineage_pairs(db: DuckDBBackend) -> set[tuple[str, str]]:
    """All (src_key, dst_key) COLUMN_LINEAGE pairs currently in the graph."""
    rows = db.run_read('SELECT src_key, dst_key FROM "COLUMN_LINEAGE"', {})
    return {(r["src_key"], r["dst_key"]) for r in rows}


def _write_catalog_toml(tmp_path: Path, csv_path: Path) -> None:
    """Configure .sqlcg.toml so index_repo re-applies *csv_path* as the catalog."""
    (tmp_path / ".sqlcg.toml").write_text(
        f'[sqlcg.catalog]\npath = "{csv_path.as_posix()}"\n', encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Bug #4: parsed-DDL source — already worked before PR-4; pinned as a guard
# ---------------------------------------------------------------------------


def test_star_promote_chain_from_parsed_ddl_source(db, tmp_path):
    """SELECT-* temp promote chains full lineage when the source DDL is parsed.

    raw_src.{a,b,c} -> stg_tmp.{a,b,c} -> persisted.{a,b,c}. This case passed
    before PR-4 (DDL columns exist at the first star pass); pinned so a refactor
    cannot regress the two-level chain.

    Guards [plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §PR-4.
    """
    (tmp_path / "etl.sql").write_text(
        "CREATE TABLE raw_src (a INT, b INT, c INT);\n"
        "CREATE TEMPORARY TABLE stg_TMP AS SELECT * FROM raw_src;\n"
        "INSERT INTO persisted SELECT * FROM stg_TMP;\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    pairs = _lineage_pairs(db)
    expected = {
        ("raw_src.a", "etl.sql::stg_tmp.a"),
        ("raw_src.b", "etl.sql::stg_tmp.b"),
        ("raw_src.c", "etl.sql::stg_tmp.c"),
        ("etl.sql::stg_tmp.a", "persisted.a"),
        ("etl.sql::stg_tmp.b", "persisted.b"),
        ("etl.sql::stg_tmp.c", "persisted.c"),
    }
    assert expected <= pairs, f"Missing chain edges. Have: {sorted(pairs)}"


# ---------------------------------------------------------------------------
# Bug #4: information_schema-catalogued source ONLY — the PR-4 fix
# ---------------------------------------------------------------------------


def test_star_promote_chain_from_information_schema_source(db, tmp_path):
    """Same full chain when the source is catalogued ONLY via information_schema.

    No parsed DDL for ba.raw_src — its columns arrive with the catalog re-apply,
    which runs AFTER the first star pass. PR-4 re-runs expansion post-catalog so
    the two-level chain ba.raw_src -> stg_tmp -> ba.persisted is produced. This is
    the live-DWH-representative case (sources catalogued, not parsed from DDL).

    Guards [plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §PR-4.
    """
    (tmp_path / "etl.sql").write_text(
        "CREATE TEMPORARY TABLE stg_TMP AS SELECT * FROM ba.raw_src;\n"
        "INSERT INTO ba.persisted SELECT * FROM stg_TMP;\n"
    )
    csv_path = tmp_path / "cols.csv"
    csv_path.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\nba,raw_src,a\nba,raw_src,b\nba,raw_src,c\n",
        encoding="utf-8",
    )
    _write_catalog_toml(tmp_path, csv_path)

    result = Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    assert result.get("catalog_reapplied") is True, f"catalog not re-applied: {result}"

    pairs = _lineage_pairs(db)
    expected = {
        ("ba.raw_src.a", "etl.sql::stg_tmp.a"),
        ("ba.raw_src.b", "etl.sql::stg_tmp.b"),
        ("ba.raw_src.c", "etl.sql::stg_tmp.c"),
        ("etl.sql::stg_tmp.a", "ba.persisted.a"),
        ("etl.sql::stg_tmp.b", "ba.persisted.b"),
        ("etl.sql::stg_tmp.c", "ba.persisted.c"),
    }
    assert expected <= pairs, f"information_schema chain missing. Have: {sorted(pairs)}"


# ---------------------------------------------------------------------------
# Bug #4: honest-empty degrade — source has NO columns anywhere
# ---------------------------------------------------------------------------


def test_star_promote_no_columns_anywhere_emits_no_phantom_edge(db, tmp_path):
    """When the source has NO columns in DDL or catalog, the promote is empty.

    No fabricated/phantom column edge into the temp or the persisted target — an
    honest empty result, never a wrong edge (Non-Goal: uncatalogued sources).

    Guards [plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §PR-4.
    """
    (tmp_path / "etl.sql").write_text(
        "CREATE TEMPORARY TABLE stg_TMP AS SELECT * FROM ba.raw_src;\n"
        "INSERT INTO ba.persisted SELECT * FROM stg_TMP;\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    pairs = _lineage_pairs(db)
    fabricated = {(s, d) for (s, d) in pairs if "stg_tmp" in d or d.startswith("ba.persisted")}
    assert fabricated == set(), f"Expected no fabricated edges, got: {sorted(fabricated)}"


# ---------------------------------------------------------------------------
# Bug #4: promote into a persisted table whose own DDL exists prefers the
# temp's real source columns over the persisted DDL ordinal guess.
# ---------------------------------------------------------------------------


def test_star_promote_prefers_temp_source_columns_over_persisted_ddl(db, tmp_path):
    """Promote into a DDL-defined persisted table uses the temp's real columns.

    persisted has its own DDL (a, b, c) and the promote is SELECT * from a temp
    fed by raw_src(a, b, c). The resulting edges must carry the temp's real source
    columns (raw_src.col -> stg_tmp.col -> persisted.col), NOT a positional guess.

    Guards [plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §PR-4.
    """
    (tmp_path / "etl.sql").write_text(
        "CREATE TABLE raw_src (a INT, b INT, c INT);\n"
        "CREATE TABLE persisted (a INT, b INT, c INT);\n"
        "CREATE TEMPORARY TABLE stg_TMP AS SELECT * FROM raw_src;\n"
        "INSERT INTO persisted SELECT * FROM stg_TMP;\n"
    )
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    pairs = _lineage_pairs(db)
    for col in ("a", "b", "c"):
        assert (f"etl.sql::stg_tmp.{col}", f"persisted.{col}") in pairs, (
            f"persisted.{col} not sourced from real temp column. Have: {sorted(pairs)}"
        )
        assert (f"raw_src.{col}", f"etl.sql::stg_tmp.{col}") in pairs, (
            f"stg_tmp.{col} not sourced from raw_src.{col}. Have: {sorted(pairs)}"
        )


# ---------------------------------------------------------------------------
# Bug #2 compound acceptance: 14-col staging temp -> payment fact, no phantom
# ---------------------------------------------------------------------------


def test_payment_fact_promote_resolves_all_columns_no_backup_phantom(db, tmp_path):
    """14-col staging temp promotes 14/14 to payment_fact with ZERO backup edge.

    Models the live failure: a 14-column staging temp (pay_stg_TMP) fed by the
    real source (payments, catalogued via information_schema), a same-bare-named
    backup table with DDL (payment_fact_backup), and the promote
    INSERT INTO payment_fact SELECT * FROM pay_stg_TMP. Asserts:
      - all 14 pay_stg_TMP columns have HAS_COLUMN;
      - 14 payment_fact.<col> edges trace to payments.<col> via the temp;
      - ZERO COLUMN_LINEAGE edge attributes any payment_fact column to the backup.

    The backup table carries DDL so a phantom edge CAN appear if the fix is
    incomplete (no green-on-toy-fixture). The real source is catalogued via
    information_schema (PR-5's resolution route), so the live config is exercised.

    Discharges Bug #2 (= PR-5 binding + PR-4 temp-column population). Guards
    [plan doc](plan/sprints/bugfix_lineage_correctness_validation.md) §Bug #2.
    """
    cols = [f"c{i:02d}" for i in range(14)]
    col_list = ", ".join(f"{c} INT" for c in cols)

    (tmp_path / "etl.sql").write_text(
        # Backup table with its own DDL (the phantom trap).
        f"CREATE TABLE pay.payment_fact_backup ({col_list});\n"
        # Staging temp built as SELECT * from the catalogued real source.
        "CREATE TEMPORARY TABLE pay_stg_TMP AS SELECT * FROM pay.payments;\n"
        # The promote into the persisted fact table.
        "INSERT INTO pay.payment_fact SELECT * FROM pay_stg_TMP;\n"
    )
    # payments is catalogued ONLY via information_schema (no parsed DDL).
    csv_path = tmp_path / "cols.csv"
    lines = ["TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME"]
    lines += [f"pay,payments,{c}" for c in cols]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_catalog_toml(tmp_path, csv_path)

    result = Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    assert result.get("catalog_reapplied") is True, f"catalog not re-applied: {result}"

    # (a) all 14 temp columns exist.
    temp_cols = db.run_read(
        'SELECT dst_key FROM "HAS_COLUMN" WHERE src_key = ?',
        {"src_key": "etl.sql::pay_stg_tmp"},
    )
    temp_col_names = {r["dst_key"].rsplit(".", 1)[1] for r in temp_cols}
    assert temp_col_names == set(cols), (
        f"Expected 14 temp columns {sorted(cols)}, got {sorted(temp_col_names)}"
    )

    pairs = _lineage_pairs(db)

    # (b) all 14 payment_fact columns trace to payments via the temp.
    for c in cols:
        assert (f"etl.sql::pay_stg_tmp.{c}", f"pay.payment_fact.{c}") in pairs, (
            f"payment_fact.{c} not sourced from the temp. Have: {sorted(pairs)}"
        )
        assert (f"pay.payments.{c}", f"etl.sql::pay_stg_tmp.{c}") in pairs, (
            f"temp col {c} not sourced from payments.{c}. Have: {sorted(pairs)}"
        )

    # (c) ZERO edge attributes any payment_fact column to the backup table.
    phantom = {
        (s, d)
        for (s, d) in pairs
        if "payment_fact_backup" in s and d.startswith("pay.payment_fact.")
    }
    assert phantom == set(), f"Phantom backup edge(s) present: {sorted(phantom)}"
