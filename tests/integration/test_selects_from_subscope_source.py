"""SELECTS_FROM source-row guard for #38 PR-3 subscope descent fix.

Before PR-3 the ``_real_tables()`` method descended CTE child scopes (PR-2) but did
not recurse into derived-table subscopes or the ``Subquery`` wrapper of a CTAS.  This
meant that ``INSERT … SELECT FROM (SELECT … FROM real)`` and
``CREATE TABLE t AS (SELECT … FROM real_a JOIN real_b)`` emitted zero ``SELECTS_FROM``
source rows.

PR-3 extends ``_real_tables`` with a third pass: ``traverse_scope(scope.expression.parent)``
walks the already-built scope tree once per statement and collects tables from every
subscope, filtered against the union of all subscopes' CTE-alias keys.  No
``build_scope``, ``qualify``, or ``expand`` is called in this pass.

SELECTS_FROM edge semantics: ``src_key`` = SqlQuery.id (the write query),
``dst_key`` = SqlTable.qualified (the SOURCE table the query reads from).
So ``dst_key='sub.src'`` means the write query selects FROM sub.src.

This file asserts the RAW ``(src_key, dst_key)`` ``SELECTS_FROM`` rows on the indexed
graph for each PR-3 shape.  It is separate from ``test_selects_from_cte_body_source.py``
(PR-2 shapes) and ``test_issue38_cte_insert_regression.py`` (column-level, different
charter).

Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# SQL fixtures — one group per shape
# Note: SELECTS_FROM dst_key = source table (where the query reads FROM).
# ---------------------------------------------------------------------------

# Shape 1: INSERT … SELECT FROM (SELECT … FROM real) d
# Expected: SELECTS_FROM row with dst_key='sub.src'
_DDL_SUBQUERY_INSERT = """\
CREATE TABLE sub.src (x NUMBER);
CREATE TABLE sub.dst (x NUMBER);
"""

_SUBQUERY_INSERT = """\
INSERT INTO sub.dst (x)
SELECT x FROM (SELECT x FROM sub.src) d;
"""

# Shape 2: CREATE TABLE t AS (SELECT … FROM real_a JOIN real_b) — CTAS-as-Subquery
# Expected: SELECTS_FROM rows with dst_key in {'cs.src_a', 'cs.src_b'}
_DDL_CTAS_SUBQUERY = """\
CREATE TABLE cs.src_a (x NUMBER);
CREATE TABLE cs.src_b (x NUMBER);
CREATE TABLE cs.dst (x NUMBER);
"""

_CTAS_SUBQUERY = """\
CREATE OR REPLACE TABLE cs.dst AS (
    SELECT a.x FROM cs.src_a a JOIN cs.src_b b ON a.x = b.x
);
"""

# Shape 3: comment-prefixed INSERT with subscope source
# Expected: SELECTS_FROM row with dst_key='ci.src'
_DDL_COMMENT_INSERT = """\
CREATE TABLE ci.src (x NUMBER);
CREATE TABLE ci.dst (x NUMBER);
"""

_COMMENT_INSERT = """\
/* populate ci.dst from subquery */
INSERT INTO ci.dst (x)
SELECT x FROM (SELECT x FROM ci.src) d;
"""

# Shape 4: schema-alias remap on subscope table (ba_tmp→ba configured)
# INSERT INTO ba.target SELECT FROM (SELECT FROM ba_tmp.real) d
# Expected: SELECTS_FROM dst_key = 'ba.real', NOT 'ba_tmp.real'
_DDL_ALIAS_SUBSCOPE = """\
CREATE TABLE ba.real (v NUMBER);
CREATE TABLE ba.target (v NUMBER);
"""

_ALIAS_SUBSCOPE_INSERT = """\
INSERT INTO ba.target (v)
SELECT v FROM (SELECT v FROM ba_tmp.real) d;
"""

# Shape 5: multi-CTE control — inner CTE alias (cte1) must NOT appear as SELECTS_FROM dst_key
# Expected: SELECTS_FROM dst_key='mc.src'; 'cte1'/'cte2' must NOT appear
_DDL_MULTI_CTE_CTRL = """\
CREATE TABLE mc.src (x NUMBER);
CREATE TABLE mc.dst (x NUMBER);
"""

_MULTI_CTE_CTRL = """\
INSERT INTO mc.dst (x)
WITH cte1 AS (SELECT x FROM mc.src),
     cte2 AS (SELECT x FROM cte1)
SELECT x FROM cte2;
"""

# Shape 6: CTE body with its own nested derived table (§1c shape)
# WITH cte AS (SELECT x FROM (SELECT x FROM cd.real) inner_d) INSERT INTO cd.dst …
# PR-2 reads cte_scope.tables which is empty; PR-3 descends into the nested derived table.
# Expected: SELECTS_FROM dst_key='cd.real'
_DDL_CTE_DERIVED = """\
CREATE TABLE cd.real (x NUMBER);
CREATE TABLE cd.dst (x NUMBER);
"""

_CTE_DERIVED_INSERT = """\
INSERT INTO cd.dst (x)
WITH cte AS (SELECT x FROM (SELECT x FROM cd.real) inner_d)
SELECT x FROM cte;
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def subquery_insert_db(tmp_path):
    """Index INSERT with derived-table source (sub.src in a subquery)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_SUBQUERY_INSERT)
    (tmp_path / "insert.sql").write_text(_SUBQUERY_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def ctas_subquery_db(tmp_path):
    """Index CTAS wrapped in a Subquery AST node (cs.src_a JOIN cs.src_b)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_CTAS_SUBQUERY)
    (tmp_path / "ctas.sql").write_text(_CTAS_SUBQUERY)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def comment_insert_db(tmp_path):
    """Index block-comment-prefixed INSERT with subscope source."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_COMMENT_INSERT)
    (tmp_path / "insert.sql").write_text(_COMMENT_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def alias_subscope_db(tmp_path):
    """Index INSERT with ba_tmp→ba aliased subscope source table."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / ".sqlcg.toml").write_text('[sqlcg]\n[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
    (tmp_path / "ddl.sql").write_text(_DDL_ALIAS_SUBSCOPE)
    (tmp_path / "insert.sql").write_text(_ALIAS_SUBSCOPE_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def multi_cte_ctrl_db(tmp_path):
    """Index multi-CTE INSERT (PR-2 control — must not regress after PR-3 is added)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_MULTI_CTE_CTRL)
    (tmp_path / "insert.sql").write_text(_MULTI_CTE_CTRL)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def cte_derived_db(tmp_path):
    """Index CTE body with nested derived table (§1c shape: cd.real inside derived)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_CTE_DERIVED)
    (tmp_path / "insert.sql").write_text(_CTE_DERIVED_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sf_dst_keys(backend: DuckDBBackend) -> set[str]:
    """Return all SELECTS_FROM dst_key values (= source table qualified names)."""
    rows = backend.run_read(
        'SELECT dst_key FROM "SELECTS_FROM"',
        {},
    )
    return {r["dst_key"] for r in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubqueryInsertEmitsSelectsFrom:
    """INSERT … SELECT FROM (subquery) emits SELECTS_FROM to the real source table.

    SELECTS_FROM dst_key = source table the query reads FROM (sub.src here).
    Guards #38 PR-3 subscope descent in _real_tables.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
    """

    def test_subquery_source_emits_dst_key_sub_src(self, subquery_insert_db: DuckDBBackend) -> None:
        """dst_key='sub.src' appears in SELECTS_FROM after indexing the derived-table INSERT."""
        dst_keys = _sf_dst_keys(subquery_insert_db)
        assert "sub.src" in dst_keys, (
            f"Expected SELECTS_FROM dst_key='sub.src'; got {dst_keys}. "
            "The subscope descent in _real_tables did not recover sub.src from the derived table."
        )

    def test_subquery_source_has_table_node(self, subquery_insert_db: DuckDBBackend) -> None:
        """sub.src exists as a SqlTable node (observable output, not just edge presence)."""
        rows = subquery_insert_db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE qualified = 'sub.src'",
            {},
        )
        assert rows, "sub.src node not found in SqlTable"

    def test_derived_alias_not_emitted_as_dst_key(self, subquery_insert_db: DuckDBBackend) -> None:
        """The derived-table alias 'd' must NOT appear as a SELECTS_FROM dst_key."""
        dst_keys = _sf_dst_keys(subquery_insert_db)
        alias_matches = {k for k in dst_keys if k == "d" or k.endswith(".d")}
        assert not alias_matches, f"Derived-table alias 'd' appears as dst_key: {alias_matches}"


class TestCtasSubqueryEmitsSelectsFrom:
    """CREATE TABLE t AS (SELECT … FROM real_a JOIN real_b) emits SELECTS_FROM to both sources.

    The CTAS wraps the SELECT in a Subquery AST node; root scope.tables is empty.
    SELECTS_FROM dst_key = cs.src_a and cs.src_b (the source tables read by the CTAS).
    Guards #38 PR-3 subscope descent.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
    """

    def test_ctas_subquery_emits_dst_key_cs_src_a(self, ctas_subquery_db: DuckDBBackend) -> None:
        """dst_key='cs.src_a' appears in SELECTS_FROM after CTAS-as-Subquery indexing."""
        dst_keys = _sf_dst_keys(ctas_subquery_db)
        assert "cs.src_a" in dst_keys, (
            f"Expected SELECTS_FROM dst_key='cs.src_a'; got {dst_keys}. "
            "CTAS-as-Subquery subscope was not descended."
        )

    def test_ctas_subquery_emits_dst_key_cs_src_b(self, ctas_subquery_db: DuckDBBackend) -> None:
        """dst_key='cs.src_b' appears in SELECTS_FROM after CTAS-as-Subquery indexing."""
        dst_keys = _sf_dst_keys(ctas_subquery_db)
        assert "cs.src_b" in dst_keys, (
            f"Expected SELECTS_FROM dst_key='cs.src_b'; got {dst_keys}. "
            "CTAS-as-Subquery subscope was not descended."
        )

    def test_ctas_both_source_table_nodes_present(self, ctas_subquery_db: DuckDBBackend) -> None:
        """Both cs.src_a and cs.src_b exist as SqlTable nodes."""
        for tbl in ("cs.src_a", "cs.src_b"):
            rows = ctas_subquery_db.run_read(
                'SELECT qualified FROM "SqlTable" WHERE qualified = ?',
                {"q": tbl},
            )
            assert rows, f"{tbl} node not found in SqlTable"


class TestCommentPrefixedInsertEmitsSelectsFrom:
    """A block-comment-prefixed INSERT with a subscope source still emits SELECTS_FROM.

    Guards that leading block comments do not prevent scope analysis.
    SELECTS_FROM dst_key = ci.src (the source table).
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
    """

    def test_comment_insert_emits_dst_key_ci_src(self, comment_insert_db: DuckDBBackend) -> None:
        """dst_key='ci.src' appears even when INSERT is preceded by a block comment."""
        dst_keys = _sf_dst_keys(comment_insert_db)
        assert "ci.src" in dst_keys, (
            f"Expected SELECTS_FROM dst_key='ci.src'; got {dst_keys}. "
            "Comment-prefixed INSERT did not recover ci.src from the subscope."
        )


class TestSchemaAliasRemapOnSubscopeTable:
    """ba_tmp→ba alias remap applies to tables found in subscopes.

    INSERT INTO ba.target SELECT FROM (SELECT FROM ba_tmp.real) d with ba_tmp→ba alias
    configured must emit SELECTS_FROM dst_key = 'ba.real', NOT 'ba_tmp.real'.
    Guards #38 PR-3 alias remap on subscope tables.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
    """

    def test_subscope_source_alias_remapped_to_ba_real(
        self, alias_subscope_db: DuckDBBackend
    ) -> None:
        """dst_key='ba.real' (aliased form) appears, not 'ba_tmp.real'."""
        dst_keys = _sf_dst_keys(alias_subscope_db)
        assert "ba.real" in dst_keys, (
            f"Expected SELECTS_FROM dst_key='ba.real' (aliased); got {dst_keys}. "
            "ba_tmp→ba alias remap did not apply to the subscope table."
        )
        assert "ba_tmp.real" not in dst_keys, (
            f"dst_key='ba_tmp.real' (un-remapped) found in {dst_keys}. "
            "Alias remap must apply before emission."
        )


class TestMultiCteCtrlNoRegressionAfterPr3:
    """Multi-CTE control: inner CTE alias must NOT appear as SELECTS_FROM dst_key.

    This shape was covered by PR-2; asserting it still works after PR-3 is added.
    Guards that the union-of-all-subscopes CTE filter does not over-filter real tables.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §3.2
    """

    def test_cte1_alias_not_emitted_as_dst_key(self, multi_cte_ctrl_db: DuckDBBackend) -> None:
        """CTE aliases cte1/cte2 must not appear as SELECTS_FROM dst_key values."""
        dst_keys = _sf_dst_keys(multi_cte_ctrl_db)
        for alias in ("cte1", "mc.cte1", "cte2", "mc.cte2"):
            assert alias not in dst_keys, f"CTE alias '{alias}' appeared as dst_key in {dst_keys}"

    def test_real_source_mc_src_emitted_as_dst_key(self, multi_cte_ctrl_db: DuckDBBackend) -> None:
        """dst_key='mc.src' (real table behind the CTE chain) appears in SELECTS_FROM."""
        dst_keys = _sf_dst_keys(multi_cte_ctrl_db)
        assert "mc.src" in dst_keys, (
            f"Expected mc.src in SELECTS_FROM dst_keys; got {dst_keys}. "
            "PR-2 CTE body descent regressed after PR-3."
        )


class TestCteDerivedBodySourceEmitsSelectsFrom:
    """CTE body with its own nested derived table: real source recovered via subscope descent.

    Shape: WITH cte AS (SELECT x FROM (SELECT x FROM cd.real) inner_d) INSERT INTO cd.dst …
    PR-2 reads only the CTE child scope's .tables (which is empty when the CTE body
    itself uses a derived table); PR-3's traverse_scope pass descends into the nested scope.
    SELECTS_FROM dst_key = cd.real.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §1c, §3.2
    """

    def test_nested_derived_in_cte_body_emits_dst_key_cd_real(
        self, cte_derived_db: DuckDBBackend
    ) -> None:
        """dst_key='cd.real' appears in SELECTS_FROM (nested derived table inside CTE body)."""
        dst_keys = _sf_dst_keys(cte_derived_db)
        assert "cd.real" in dst_keys, (
            f"cd.real not found in SELECTS_FROM dst_keys {dst_keys}. "
            "Nested derived table inside CTE body was not recovered by PR-3 subscope descent."
        )


class TestNoDuplicateSelectsFromRows:
    """A table reachable via both the top-level and a subscope must not produce duplicate rows.

    Guards the de-dup logic in _real_tables across all three passes.
    Governing plan: plan/sprints/issue-38-residual-source-extraction.md §2 (de-dup)
    """

    def test_no_duplicate_src_dst_pairs(self, subquery_insert_db: DuckDBBackend) -> None:
        """No (src_key, dst_key) pair appears more than once in SELECTS_FROM."""
        dup_rows = subquery_insert_db.run_read(
            """
            SELECT src_key, dst_key, COUNT(*) AS cnt
            FROM "SELECTS_FROM"
            GROUP BY src_key, dst_key
            HAVING COUNT(*) > 1
            """,
            {},
        )
        assert not dup_rows, (
            f"Duplicate SELECTS_FROM (src_key, dst_key) pairs found: {dup_rows}. "
            "De-dup logic in _real_tables is broken."
        )
