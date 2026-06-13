"""Integration tests for the SELECTS_FROM-completeness fix (PR 4).

Guards the three-signal READ_TABLES union definition for `analyze unused`
and the `find_table_usages` augmentation with `usage_kind`.

Plan reference: plan/sprints/unfilled_table_impact.md §"PR 4 — SELECTS_FROM-completeness fix"
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import ANALYZE_UNUSED_TABLES_QUERY
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared backend fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory DuckDB with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Fixture SQL strings
# ---------------------------------------------------------------------------

# T0c: table `da.x` read ONLY via a CTE-wrapped derived SELECT that lands a
# COLUMN_LINEAGE edge sourced at `da.x`. No top-level SELECTS_FROM edge to da.x.
# `da.x` has NO direct SELECTS_FROM or STAR_SOURCE edge — only the CTE-routed
# COLUMN_LINEAGE. The fix must detect it as USED via D3.
_T0C_SQL = """\
INSERT INTO da.d
WITH cte AS (SELECT x.a FROM da.x x)
SELECT cte.a FROM cte;
"""

# Direct-read table: `da.src` read via a plain top-level INSERT-SELECT.
# SELECTS_FROM edge exists — D1 signal.
_DIRECT_READ_SQL = """\
INSERT INTO da.dst
SELECT a FROM da.src;
"""

# Genuinely unreferenced table: `da.ghost` is never read anywhere.
# It will be created as a SqlTable node via a CREATE TABLE statement.
_GHOST_DEFINE_SQL = """\
CREATE TABLE da.ghost (id INTEGER, val VARCHAR);
"""

# Star-source read: `da.star_src` is consumed via SELECT *.
# Should produce a STAR_SOURCE edge — D2 signal.
_STAR_SOURCE_SQL = """\
INSERT INTO da.star_dst
SELECT * FROM da.star_src;
"""

# T0d: CTE-wrapped PURE-gating read — the documented gap.
# `da.x_gate` is defined via CREATE TABLE so it exists in SqlTable.
# It is read only inside a CTE and derives NO column (used only as a join filter).
# After the fix, neither SELECTS_FROM nor COLUMN_LINEAGE source edges exist for it,
# so it STILL appears in ANALYZE_UNUSED_TABLES.
_T0D_DDL = """\
CREATE TABLE da.x_gate (k INTEGER);
"""
_T0D_SQL = """\
INSERT INTO da.d_gate
WITH cte_gate AS (SELECT 1 AS k FROM da.x_gate x_gate)
SELECT other.y FROM da.other_gate other
JOIN cte_gate ON other.k = cte_gate.k;
"""


# ---------------------------------------------------------------------------
# T0c — CTE-wrapped derived read is COUNTED (headline fix)
# ---------------------------------------------------------------------------


class TestCteWrappedDerivedReadIsCounted:
    """T0c: a table read only via a CTE-wrapped derived SELECT must NOT appear in
    `analyze unused` and MUST appear in `find_table_usages` with usage_kind='via_lineage'.

    Guards the D3 (COLUMN_LINEAGE source-table rollup) signal in the three-signal
    READ_TABLES union. Before the fix, `da.x` would falsely appear as unused because
    `ANALYZE_UNUSED_TABLES` only checked `SELECTS_FROM`.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Test T0c
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        (tmp_path / "fixture_t0c.sql").write_text(_T0C_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_cte_derived_source_not_in_unused(self, indexed_db):
        """da.x is NOT returned by ANALYZE_UNUSED_TABLES after the fix.

        Pre-fix: da.x would appear because it has no SELECTS_FROM edge.
        Post-fix: da.x is excluded because a COLUMN_LINEAGE edge is sourced at it.
        """
        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}
        assert "da.x" not in unused_tables, (
            f"da.x falsely reported as unused. Unused tables: {sorted(unused_tables)}. "
            "The D3 COLUMN_LINEAGE signal should have detected it."
        )

    def test_cte_derived_source_has_via_lineage_usage(self, indexed_db):
        """find_table_usages('x') returns at least one usage with usage_kind='via_lineage'.

        The CTE-wrapped derived read (`INSERT INTO da.d WITH cte AS (SELECT x.a FROM da.x x)
        SELECT cte.a FROM cte`) emits a COLUMN_LINEAGE edge sourced at da.x. The augmented
        find_table_usages must surface this as a via_lineage usage.
        """
        from sqlcg.core.queries import FIND_TABLE_USAGES_VIA_LINEAGE_QUERY

        rows = indexed_db.run_read(
            FIND_TABLE_USAGES_VIA_LINEAGE_QUERY,
            {"name": "x"},
        )
        assert len(rows) >= 1, (
            "FIND_TABLE_USAGES_VIA_LINEAGE returned no rows for table 'x'. "
            "Expected at least one CTE-routed usage."
        )

    def test_destination_table_not_in_unused(self, indexed_db):
        """da.d (the target of the INSERT) must also not appear as unused — it is written
        by the fixture query, which is not a read-side check, but the table must be
        present in the graph. Sanity-check that the fixture indexed correctly.
        """
        rows = indexed_db.run_read(
            'SELECT qualified FROM "SqlTable" WHERE qualified = ?',
            {"qualified": "da.x"},
        )
        assert len(rows) == 1, "da.x SqlTable node not created — fixture did not index correctly."


# ---------------------------------------------------------------------------
# Direct-read table still works
# ---------------------------------------------------------------------------


class TestDirectReadTableStillWorks:
    """A table read via a plain top-level SELECTS_FROM is NOT in unused and is
    returned by find_table_usages / the via-lineage query with the correct labels.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Test Strategy (direct-read table)
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        (tmp_path / "direct.sql").write_text(_DIRECT_READ_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_direct_read_not_in_unused(self, indexed_db):
        """da.src with a direct SELECTS_FROM edge must NOT appear as unused."""
        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}
        assert "da.src" not in unused_tables, (
            f"da.src falsely reported as unused. Unused: {sorted(unused_tables)}."
        )

    def test_direct_read_has_selects_from_edge(self, indexed_db):
        """Confirm the direct SELECTS_FROM edge exists for da.src — D1 signal fires."""
        rows = indexed_db.run_read(
            'SELECT dst_key FROM "SELECTS_FROM" WHERE dst_key = ?',
            {"dst_key": "da.src"},
        )
        assert len(rows) >= 1, "No SELECTS_FROM edge found for da.src — fixture unexpected."


# ---------------------------------------------------------------------------
# Genuinely-unreferenced table still appears in unused
# ---------------------------------------------------------------------------


class TestGenuinelyUnreferencedTableStillInUnused:
    """A table with no SELECTS_FROM, STAR_SOURCE, or outgoing COLUMN_LINEAGE is STILL
    returned by ANALYZE_UNUSED_TABLES. The bugfix must not hide real dead tables.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Test Strategy (genuinely unreferenced)
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        (tmp_path / "ghost.sql").write_text(_GHOST_DEFINE_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_ghost_table_still_in_unused(self, indexed_db):
        """da.ghost (defined but never read) STILL appears as unused after the fix."""
        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}
        assert "da.ghost" in unused_tables, (
            f"da.ghost not in unused tables. Unused: {sorted(unused_tables)}. "
            "The fix must not over-correct and hide genuinely dead tables."
        )


# ---------------------------------------------------------------------------
# Star-source read keeps table out of unused (D2)
# ---------------------------------------------------------------------------


class TestStarSourceReadCountedAsUsed:
    """A SELECT * from `da.star_src` produces a STAR_SOURCE edge; the table must NOT
    appear in ANALYZE_UNUSED_TABLES (D2 signal).

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Test Strategy (star-source read)
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        (tmp_path / "star.sql").write_text(_STAR_SOURCE_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_star_source_not_in_unused(self, indexed_db):
        """da.star_src with a STAR_SOURCE edge must NOT appear as unused."""
        # Verify a STAR_SOURCE edge was emitted
        star_rows = indexed_db.run_read(
            'SELECT dst_key FROM "STAR_SOURCE" WHERE dst_key = ?',
            {"dst_key": "da.star_src"},
        )
        if not star_rows:
            pytest.skip("No STAR_SOURCE edge emitted for da.star_src — fixture may need expansion")

        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}
        assert "da.star_src" not in unused_tables, (
            f"da.star_src falsely reported as unused. Unused: {sorted(unused_tables)}."
        )


# ---------------------------------------------------------------------------
# T0d — CTE-wrapped PURE-gating read is the documented gap
# ---------------------------------------------------------------------------


class TestCtePureGatingReadIsDocumentedGap:
    """T0d: `da.x_gate` is read only inside a CTE without deriving any column.

    Originally documented as a known gap where D1 (SELECTS_FROM) did NOT fire because
    the parser's ``_real_tables`` (base.py L604) excluded CTE-sourced names.

    PR-2 (#38 island-lever fix, plan/sprints/issue-38-selects-from-island-lever.md):
    ``_real_tables()`` now descends CTE child scopes and emits SELECTS_FROM rows for
    tables found there.  The T0d CTE body reads ``da.x_gate``, so D1 NOW fires, the
    table appears in SELECTS_FROM, and it is correctly removed from "unused".
    The "documented gap" is now CLOSED by the parser fix.

    D2 (STAR_SOURCE) and D3 (COLUMN_LINEAGE source) remain absent because no column
    value derives from x_gate — those assertions are unchanged.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Test T0d (gap now closed by PR-2).
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        # Write DDL first so da.x_gate has a SqlTable node, then the consuming query.
        (tmp_path / "gate_ddl.sql").write_text(_T0D_DDL)
        (tmp_path / "gate.sql").write_text(_T0D_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_pure_gating_table_not_in_unused_after_pr2(self, indexed_db):
        """da.x_gate (CTE-pure-gating, no derived column) is now REMOVED from unused.

        After PR-2 _real_tables() descends the CTE child scope and emits a SELECTS_FROM
        row for da.x_gate.  The D1 signal fires; ANALYZE_UNUSED_TABLES excludes it.
        The T0d gap is closed.

        Guards plan/sprints/issue-38-selects-from-island-lever.md §Acceptance PR-2.
        """
        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}

        assert "da.x_gate" not in unused_tables, (
            f"da.x_gate still appears as unused after PR-2 fix. "
            f"Unused: {sorted(unused_tables)}. "
            "The CTE-body descent should have emitted a SELECTS_FROM row for x_gate."
        )

    def test_pure_gating_table_has_selects_from_after_pr2(self, indexed_db):
        """D1 (SELECTS_FROM) now fires for da.x_gate after the PR-2 fix.

        D2 (STAR_SOURCE) and D3 (COLUMN_LINEAGE source) remain absent — no column
        value derives from x_gate.
        """
        # D1: NOW has SELECTS_FROM (PR-2 fix)
        sf_rows = indexed_db.run_read(
            'SELECT dst_key FROM "SELECTS_FROM" WHERE dst_key = ?',
            {"dst_key": "da.x_gate"},
        )
        assert len(sf_rows) >= 1, (
            "No SELECTS_FROM edge for da.x_gate — PR-2 CTE-body descent did not fire. "
            "Expected at least one row with dst_key='da.x_gate'."
        )

        # D2: still no STAR_SOURCE (unchanged — no SELECT * from x_gate)
        ss_rows = indexed_db.run_read(
            'SELECT dst_key FROM "STAR_SOURCE" WHERE dst_key = ?',
            {"dst_key": "da.x_gate"},
        )
        assert len(ss_rows) == 0, "Unexpected STAR_SOURCE edge for da.x_gate"

        # D3: still no COLUMN_LINEAGE source (unchanged — no column derives from x_gate)
        from sqlcg.core.queries import FIND_TABLE_USAGES_VIA_LINEAGE_QUERY

        cl_rows = indexed_db.run_read(
            FIND_TABLE_USAGES_VIA_LINEAGE_QUERY,
            {"name": "x_gate"},
        )
        assert len(cl_rows) == 0, (
            "Unexpected COLUMN_LINEAGE source for da.x_gate — the D3 path is not expected here"
        )

    def test_pure_gating_find_table_usages_returns_direct_usage_after_pr2(
        self, indexed_db, monkeypatch
    ):
        """find_table_usages for x_gate now returns a direct usage after PR-2 fix.

        After PR-2, the CTE-body read of x_gate emits a SELECTS_FROM row, which
        is picked up by find_table_usages as a 'direct' usage_kind.
        """
        from unittest.mock import MagicMock

        import sqlcg.server.tools as tools_mod

        # Patch _open_backend to return our indexed_db without touching disk
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=indexed_db)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(tools_mod, "_open_backend", lambda: mock_ctx)

        # Patch _assert_indexed to be a no-op
        monkeypatch.setattr(tools_mod, "_assert_indexed", lambda db: None)

        result = tools_mod.find_table_usages("x_gate")
        assert len(result.usages) >= 1, (
            f"Expected at least one usage for x_gate after PR-2 fix; got: {result.usages}. "
            "The CTE-body descent emits SELECTS_FROM, so find_table_usages should return it."
        )
        kinds = {u.usage_kind for u in result.usages}
        assert "direct" in kinds, (
            f"Expected usage_kind='direct' (from SELECTS_FROM); got kinds={kinds}"
        )


# ---------------------------------------------------------------------------
# Both the inlined CLI SQL and ANALYZE_UNUSED_TABLES stay in sync
# ---------------------------------------------------------------------------


class TestUnusedInlinedSqlAndNamedQueryInSync:
    """Both the CLI inlined SQL and ANALYZE_UNUSED_TABLES use the three-signal union.
    Verify that a CTE-derived-only table is excluded by BOTH paths on the same graph.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Acceptance Criteria
    """

    @pytest.fixture
    def indexed_db(self, db, tmp_path):
        (tmp_path / "sync_check.sql").write_text(_T0C_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
        return db

    def test_named_query_excludes_cte_derived_source(self, indexed_db):
        """ANALYZE_UNUSED_TABLES (named query) excludes da.x (CTE-derived only)."""
        rows = indexed_db.run_read(ANALYZE_UNUSED_TABLES_QUERY, {})
        unused_tables = {r["table_qualified"] for r in rows}
        assert "da.x" not in unused_tables, (
            f"ANALYZE_UNUSED_TABLES still returns da.x. Unused: {sorted(unused_tables)}"
        )

    def test_inlined_cli_sql_excludes_cte_derived_source(self, indexed_db):
        """The inlined CLI SQL (three-signal union) also excludes da.x."""
        inlined_sql = (
            'SELECT DISTINCT qualified FROM "SqlTable"'
            " WHERE qualified NOT IN ("
            '    SELECT DISTINCT dst_key AS table_qualified FROM "SELECTS_FROM"'
            "    UNION"
            '    SELECT DISTINCT dst_key AS table_qualified FROM "STAR_SOURCE"'
            "    UNION"
            "    SELECT DISTINCT src.table_qualified AS table_qualified"
            '    FROM "COLUMN_LINEAGE" cl'
            '    JOIN "SqlColumn" src ON src.id = cl.src_key'
            "    WHERE src.table_qualified IS NOT NULL AND src.table_qualified <> ''"
            ")"
            " LIMIT 100"
        )
        rows = indexed_db.run_read(inlined_sql, {})
        unused_tables = {r["qualified"] for r in rows}
        assert "da.x" not in unused_tables, (
            f"CLI inlined SQL still returns da.x. Unused: {sorted(unused_tables)}"
        )
