"""Integration tests for the key-normalisation choke point (PR 1).

Mirrors the wtfa_loonkosten pattern from the live DWH: a USE SCHEMA + INSERT with
an explicit column list where stmt.this is exp.Schema.  After indexing with
schema_aliases={"ba_tmp": "ba"}, zero keys should contain 'ba_tmp.' in any graph
table.

Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1 / Step 2.2.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Two-statement fixture mirroring the loonkosten pattern:
#   stmt 0 — DDL in ba (base schema) that defines the table columns
#   stmt 1 — column-list INSERT into BA_TMP schema (aliased to BA)
# ---------------------------------------------------------------------------

_FIXTURE_DDL = """\
CREATE TABLE ba.wtfa_loonkosten (
    loon_id   INT,
    loon_naam VARCHAR
);
"""

_FIXTURE_INSERT = """\
INSERT INTO ba_tmp.wtfa_loonkosten (loon_id, loon_naam)
SELECT loon_id, loon_naam
FROM da.staging_loonkosten;
"""


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


class TestKeyNormalisationChokepointIntegration:
    """schema_aliases={"ba_tmp": "ba"} collapses all ba_tmp.* keys to ba.* in the graph."""

    def test_zero_ba_tmp_keys_in_sqltable(self, db, tmp_path):
        """No SqlTable.qualified contains 'ba_tmp.' after indexing with the alias map.

        Fixture: two SQL files, one DDL (ba.wtfa_loonkosten) and one INSERT from
        ba_tmp schema.  The alias map folds ba_tmp → ba so the INSERT target key
        must be 'ba.wtfa_loonkosten', not 'ba_tmp.wtfa_loonkosten'.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        ba_tmp_tables = db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE 'ba_tmp.%'",
            {},
        )
        assert ba_tmp_tables == [], (
            f"Found SqlTable rows with 'ba_tmp.' after normalisation: {ba_tmp_tables}. "
            "normalize_keys must fold ba_tmp → ba before graph keys are rendered."
        )

    def test_zero_ba_tmp_keys_in_sqlcolumn(self, db, tmp_path):
        """No SqlColumn.table_qualified contains 'ba_tmp.' after normalisation.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        ba_tmp_cols = db.run_read(
            "SELECT table_qualified FROM \"SqlColumn\" WHERE table_qualified LIKE 'ba_tmp.%'",
            {},
        )
        assert ba_tmp_cols == [], (
            f"Found SqlColumn rows with 'ba_tmp.' after normalisation: {ba_tmp_cols}"
        )

    def test_zero_ba_tmp_keys_in_column_lineage(self, db, tmp_path):
        """No COLUMN_LINEAGE src_key or dst_key contains 'ba_tmp.' after normalisation.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        ba_tmp_edges_src = db.run_read(
            "SELECT src_key FROM \"COLUMN_LINEAGE\" WHERE src_key LIKE 'ba_tmp.%'",
            {},
        )
        ba_tmp_edges_dst = db.run_read(
            "SELECT dst_key FROM \"COLUMN_LINEAGE\" WHERE dst_key LIKE 'ba_tmp.%'",
            {},
        )
        assert ba_tmp_edges_src == [], (
            f"COLUMN_LINEAGE src_key contains 'ba_tmp.': {ba_tmp_edges_src}"
        )
        assert ba_tmp_edges_dst == [], (
            f"COLUMN_LINEAGE dst_key contains 'ba_tmp.': {ba_tmp_edges_dst}"
        )

    def test_zero_ba_tmp_keys_in_sqlquery_target(self, db, tmp_path):
        """No SqlQuery.target_table contains 'ba_tmp.' after normalisation.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        ba_tmp_queries = db.run_read(
            "SELECT target_table FROM \"SqlQuery\" WHERE target_table LIKE 'ba_tmp.%'",
            {},
        )
        assert ba_tmp_queries == [], f"SqlQuery.target_table contains 'ba_tmp.': {ba_tmp_queries}"

    def test_zero_ba_tmp_keys_in_has_column(self, db, tmp_path):
        """No HAS_COLUMN src_key or dst_key contains 'ba_tmp.' after normalisation.

        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        ba_tmp_hc_src = db.run_read(
            "SELECT src_key FROM \"HAS_COLUMN\" WHERE src_key LIKE 'ba_tmp.%'",
            {},
        )
        ba_tmp_hc_dst = db.run_read(
            "SELECT dst_key FROM \"HAS_COLUMN\" WHERE dst_key LIKE 'ba_tmp.%'",
            {},
        )
        assert ba_tmp_hc_src == [], f"HAS_COLUMN src_key contains 'ba_tmp.': {ba_tmp_hc_src}"
        assert ba_tmp_hc_dst == [], f"HAS_COLUMN dst_key contains 'ba_tmp.': {ba_tmp_hc_dst}"

    def test_aliased_target_is_canonical_key(self, db, tmp_path):
        """After normalisation the INSERT target is stored under the canonical schema key.

        Asserts non-empty output: 'ba.wtfa_loonkosten' MUST exist in SqlTable.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        canonical_tables = db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE qualified = 'ba.wtfa_loonkosten'",
            {},
        )
        assert canonical_tables, (
            "SqlTable 'ba.wtfa_loonkosten' not found after alias normalisation. "
            "The INSERT target with ba_tmp schema should be folded to the canonical ba key."
        )

    def test_no_leading_dot_keys_in_column_lineage(self, db, tmp_path):
        """No COLUMN_LINEAGE key has a leading dot after normalisation.

        Leading-dot keys ('.ean_code', '.metadata$filename') arise from stage reads
        where TableRef.full_id=''.  normalize_keys must eliminate them.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 1.
        """
        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
        (tmp_path / "ddl.sql").write_text(_FIXTURE_DDL)
        (tmp_path / "insert.sql").write_text(_FIXTURE_INSERT)

        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        leading_dot_src = db.run_read(
            "SELECT src_key FROM \"COLUMN_LINEAGE\" WHERE src_key LIKE '.%'",
            {},
        )
        leading_dot_dst = db.run_read(
            "SELECT dst_key FROM \"COLUMN_LINEAGE\" WHERE dst_key LIKE '.%'",
            {},
        )
        assert leading_dot_src == [], f"Leading-dot src_key found: {leading_dot_src}"
        assert leading_dot_dst == [], f"Leading-dot dst_key found: {leading_dot_dst}"
