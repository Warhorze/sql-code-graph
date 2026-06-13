"""Empirical fixture test for third residual dual-write class.

Replicates wtfi_acp_reop_promo.sql pattern:
  - use schema BA_TMP (alias ba_tmp → ba)
  - create or replace temp table TMP_PROMO as ... (bare name, no schema prefix)
  - from TMP_PROMO (bare name reference in subsequent INSERT)

Plus a second file that also creates tmp_promo to force pass-2 cross-file machinery.

This test is used to confirm the root cause and verify the fix.
Guards plan/sprints/temp_table_namespacing.md §Deviation 3.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# Exact pattern from wtfi_acp_reop_promo.sql:
# - USE SCHEMA BA_TMP (ba_tmp aliased to ba)
# - CREATE OR REPLACE TEMP TABLE TMP_PROMO AS ... (bare name)
# - INSERT INTO ... SELECT ... FROM TMP_PROMO (bare name)
_BARE_TEMP_FILE_SQL = """\
use schema BA_TMP;
truncate table BA_TMP.WTFI_ACP_REOP_PROMO;
create or replace temp table TMP_PROMO as
select 1 as DN_ARTIKELNR, 2 as MA_AANTAL_PROMO
from BA.WTFE_VERKOOPINFO v;
insert into BA_TMP.WTFI_ACP_REOP_PROMO
select DN_ARTIKELNR, MA_AANTAL_PROMO
from TMP_PROMO;
"""

# Second file that also defines tmp_promo (different schema, forces cross-file)
_CROSS_FILE_TMP_PROMO_SQL = """\
use schema DA_TMP;
create or replace temporary table tmp_promo as
select PROMOTION_CODE, 1 as MA_VALUE
from DA.HTHYB_PROMOTIES;
insert into DA.TTINT_PROMOTIE_RESULT
select PROMOTION_CODE from tmp_promo;
"""

# Schema alias config
_CONFIG_TOML = '[sqlcg.schema_aliases]\nba_tmp = "ba"\nda_tmp = "da"\n'


@pytest.fixture
def third_residual_db(tmp_path):
    """Index the bare-name-temp fixture with cross-file machinery forced."""
    (tmp_path / "etl" / "sql" / "interface").mkdir(parents=True)
    (tmp_path / "etl" / "sql" / "int").mkdir(parents=True)
    (tmp_path / "etl" / "sql" / "interface" / "wtfi_acp_reop_promo.sql").write_text(
        _BARE_TEMP_FILE_SQL
    )
    (tmp_path / "etl" / "sql" / "int" / "ttint_promotie_verkoop_prijs.sql").write_text(
        _CROSS_FILE_TMP_PROMO_SQL
    )
    (tmp_path / ".sqlcg.toml").write_text(_CONFIG_TOML)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)
    yield backend
    backend.close()


class TestThirdResidualBareNameTemp:
    """USE SCHEMA + bare CREATE TEMP TABLE name + cross-file tmp machinery must not
    produce a bare-key (non-namespaced) SqlTable node.

    This is the third residual dual-write class from temp_table_namespacing.md.
    Files like wtfi_acp_reop_promo.sql use a bare CREATE TEMP TABLE name (no
    schema prefix) within a USE SCHEMA BA_TMP block. The schema alias (ba_tmp→ba)
    causes the qualification + alias path. After PR #92, the Step 2.2
    _lineage_node_to_edges fix handled the alias-mismatch for schema-aliased leaf
    nodes — but this third class involves an additional mechanism.
    """

    def test_no_bare_tmp_promo_node(self, third_residual_db):
        """After indexing, the bare 'ba.tmp_promo' (kind='table') node must NOT exist.

        Pre-fix: both 'ba.tmp_promo' (kind='table') AND
        '<rel>::ba.tmp_promo' (kind='temp') existed — the dual-write.
        Post-fix: only the namespaced kind='temp' node.

        Guards temp_table_namespacing.md §Deviation 3 (third residual class).
        """
        rows = third_residual_db.run_read(
            'SELECT qualified, kind, defined_in_file FROM "SqlTable"'
            " WHERE qualified = 'ba.tmp_promo'",
            {},
        )
        assert rows == [], (
            f"Bare 'ba.tmp_promo' kind='table' node must NOT exist after indexing with "
            f"schema alias ba_tmp→ba and bare CREATE TEMP TABLE name. Got: {rows}. "
            "This is the third residual dual-write class from temp_table_namespacing.md."
        )

    def test_namespaced_temp_node_exists(self, third_residual_db):
        """After indexing, the namespaced kind='temp' node must exist."""
        rows = third_residual_db.run_read(
            "SELECT qualified, kind FROM \"SqlTable\" WHERE name = 'tmp_promo' AND kind = 'temp'",
            {},
        )
        assert len(rows) == 2, (
            f"Expected 2 namespaced kind='temp' tmp_promo nodes (one per defining file), "
            f"got {len(rows)}: {rows}"
        )
        for r in rows:
            assert "::" in r["qualified"], f"Node must be namespaced: {r['qualified']!r}"

    def test_no_bare_selects_from_tmp_promo(self, third_residual_db):
        """SELECTS_FROM edges must use namespaced key only for tmp_promo.

        Pre-fix: both bare 'ba.tmp_promo' AND namespaced SELECTS_FROM edges coexisted.
        Post-fix: only namespaced edges.
        """
        rows = third_residual_db.run_read(
            """SELECT src_key, dst_key FROM "SELECTS_FROM"
               WHERE dst_key LIKE '%tmp_promo%'
               ORDER BY src_key""",
            {},
        )
        bare_rows = [r for r in rows if "::" not in r["dst_key"]]
        assert bare_rows == [], (
            f"Bare 'ba.tmp_promo' SELECTS_FROM edges must NOT exist. "
            f"Found: {bare_rows}. All SELECTS_FROM touching tmp_promo: {rows}"
        )

    def test_column_lineage_uses_namespaced_key(self, third_residual_db):
        """COLUMN_LINEAGE edges must use the namespaced key for tmp_promo endpoints."""
        rows = third_residual_db.run_read(
            """SELECT src_key, dst_key FROM COLUMN_LINEAGE
               WHERE src_key LIKE '%tmp_promo%' OR dst_key LIKE '%tmp_promo%'
               ORDER BY src_key""",
            {},
        )
        bare_rows = [
            r
            for r in rows
            if ("::" not in r["src_key"] and "tmp_promo" in r["src_key"])
            or ("::" not in r["dst_key"] and "tmp_promo" in r["dst_key"])
        ]
        assert bare_rows == [], (
            f"COLUMN_LINEAGE edges must not reference bare 'ba.tmp_promo'. "
            f"Found bare edges: {bare_rows}. All tmp_promo edges: {rows}"
        )
