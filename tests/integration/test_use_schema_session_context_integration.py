"""Integration tests for USE SCHEMA session context + LIKE catalog inheritance (PR 2).

Tests verify end-to-end: index a scripting fixture file, assert graph keys are
schema-qualified.

Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2 / Phase 3.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCRIPTING_SQL = """\
CREATE PROCEDURE msspr_ia_outbound_doorbelasting()
BEGIN
  USE SCHEMA ia_outbound;
  INSERT INTO doorbelasting (col1, col2)
  SELECT a, b FROM src_table;
END;
"""

_DDL_SQL = """\
CREATE TABLE ia_outbound.doorbelasting (
    col1 VARCHAR,
    col2 VARCHAR
);
"""


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Scripting path end-to-end
# ---------------------------------------------------------------------------


class TestScriptingUseSchemaEndToEnd:
    """Index a scripting-mode file and confirm graph keys carry schema prefix.

    Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2 Step 3.1.
    """

    def test_scripting_insert_target_keyed_with_schema(self, db, tmp_path):
        """SqlQuery.target_table is schema-qualified after indexing a scripting file.

        The scripting file contains USE SCHEMA ia_outbound followed by a bare
        INSERT INTO doorbelasting.  After indexing the SqlQuery.target_table must be
        'ia_outbound.doorbelasting', not 'doorbelasting'.
        """
        (tmp_path / "ddl.sql").write_text(_DDL_SQL)
        (tmp_path / "proc.sql").write_text(_SCRIPTING_SQL)

        Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

        rows = db.run_read(
            """SELECT target_table FROM "SqlQuery"
               WHERE target_table LIKE '%doorbelasting%'""",
            {},
        )
        assert rows, "Expected at least one SqlQuery row for doorbelasting"
        for row in rows:
            target = row["target_table"]
            assert "ia_outbound" in target, (
                f"Expected schema-qualified target 'ia_outbound.doorbelasting', "
                f"got: {target!r}. USE SCHEMA in scripting path not applied."
            )

    def test_scripting_column_lineage_dst_keyed_with_schema(self, db, tmp_path):
        """COLUMN_LINEAGE dst_key for a scripting INSERT is schema-qualified.

        This is the direct F2 acceptance criterion for the doorbelasting pattern.
        Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2 Step 3.1.
        """
        (tmp_path / "ddl.sql").write_text(_DDL_SQL)
        (tmp_path / "proc.sql").write_text(_SCRIPTING_SQL)

        Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

        bare_dst = db.run_read(
            """SELECT dst_key FROM "COLUMN_LINEAGE"
               WHERE dst_key LIKE 'doorbelasting.%'
               AND dst_key NOT LIKE '%.%.%'""",
            {},
        )
        assert bare_dst == [], (
            f"Found unqualified COLUMN_LINEAGE dst_keys for doorbelasting: {bare_dst}. "
            "USE SCHEMA in scripting path must qualify the target."
        )

        qualified_dst = db.run_read(
            """SELECT dst_key FROM "COLUMN_LINEAGE"
               WHERE dst_key LIKE 'ia_outbound.doorbelasting.%'""",
            {},
        )
        assert qualified_dst, (
            "No COLUMN_LINEAGE edges with 'ia_outbound.doorbelasting.*' dst_key found. "
            "USE SCHEMA scripting-path qualification produced no edges."
        )

    def test_bare_insert_without_use_schema_unqualified(self, db, tmp_path):
        """Without USE SCHEMA the INSERT target remains unqualified (control test).

        Ensures we are not accidentally qualifying everything — only files that
        contain USE SCHEMA get qualified targets.
        """
        no_use_sql = """\
CREATE PROCEDURE no_schema_proc()
BEGIN
  INSERT INTO some_table (id) SELECT id FROM other_table;
END;
"""
        (tmp_path / "proc.sql").write_text(no_use_sql)

        Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

        rows = db.run_read(
            """SELECT target_table FROM "SqlQuery"
               WHERE target_table LIKE '%some_table%'""",
            {},
        )
        if rows:
            for row in rows:
                target = row["target_table"]
                # Without USE SCHEMA the target should stay bare (no schema prefix added)
                assert "." not in target or target == "some_table", (
                    f"Expected bare 'some_table' without USE SCHEMA, got: {target!r}"
                )
