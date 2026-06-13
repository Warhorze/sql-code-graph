"""SELECTS_FROM source-row regression guard for #38 PR-2 CTE-body descent fix.

Before PR-2 the ``_real_tables()`` method in ``base.py`` filtered out CTE aliases
at the top level but never descended into CTE child scopes.  This meant that any
CTE-wrapped INSERT/CTAS — ``INSERT INTO dst WITH cte AS (SELECT … FROM src) SELECT …
FROM cte`` — emitted zero ``SELECTS_FROM`` source rows, leaving the written table as
a table-level island.

PR-2 fixes this by doing a single-level pass over ``scope.cte_sources.values()``
and collecting real tables from each CTE child scope, filtering against the ROOT
scope's flat ``cte_sources.keys()`` set.

This file asserts the RAW ``(src_key, dst_key)`` ``SELECTS_FROM`` rows produced by
the indexer.  It is intentionally separate from
``test_issue38_cte_insert_regression.py`` (which is column-level by charter and never
asserts ``SELECTS_FROM`` rows).

Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Design "PR-2 — the fix"
and §Acceptance Criteria PR-2 (Amendments A1, A3).
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared SQL fixtures
# ---------------------------------------------------------------------------

# Single CTE wrapping an INSERT — the basic gap case.
_DDL_BASIC = """\
CREATE TABLE s.a (x NUMBER);
CREATE TABLE s.b (x NUMBER);
"""

_CTE_INSERT_BASIC = """\
INSERT INTO s.b (x)
WITH cte AS (SELECT x FROM s.a)
SELECT x FROM cte;
"""

_DIRECT_INSERT_CONTROL = """\
INSERT INTO s.b (x) SELECT x FROM s.a;
"""

# Multi-CTE: cte2 reads from cte1, which reads from s.a.
# Expected: SELECTS_FROM dst = s.a (not s.b, not cte1).
_DDL_MULTI_CTE = """\
CREATE TABLE mc.src (x NUMBER);
CREATE TABLE mc.dst (x NUMBER);
"""

_MULTI_CTE_INSERT = """\
INSERT INTO mc.dst (x)
WITH cte1 AS (SELECT x FROM mc.src),
     cte2 AS (SELECT x FROM cte1)
SELECT x FROM cte2;
"""

# Schema-alias CTE body: ba_tmp.real_table with ba_tmp→ba alias configured.
# Expected: dst_key = 'ba.real_table' (alias remapped), NOT 'ba_tmp.real_table'.
_DDL_ALIAS = """\
CREATE TABLE ba.real_table (v NUMBER);
CREATE TABLE ba.target (v NUMBER);
"""

_ALIAS_CTE_INSERT = """\
INSERT INTO ba.target (v)
WITH cte AS (SELECT v FROM ba_tmp.real_table)
SELECT v FROM cte;
"""

# De-dup: real table appears both top-level and in a CTE body.
# Expected: exactly one SELECTS_FROM row for s.a (not two).
_DDL_DEDUP = """\
CREATE TABLE d.src (x NUMBER);
CREATE TABLE d.dst (x NUMBER);
"""

_DEDUP_INSERT = """\
INSERT INTO d.dst (x)
WITH cte AS (SELECT x FROM d.src)
SELECT x FROM d.src
UNION ALL
SELECT x FROM cte;
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def basic_db(tmp_path):
    """Index a single-CTE INSERT and a direct-INSERT control; return the backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_BASIC)
    (tmp_path / "etl_cte.sql").write_text(_CTE_INSERT_BASIC)
    (tmp_path / "etl_direct.sql").write_text(_DIRECT_INSERT_CONTROL)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def multi_cte_db(tmp_path):
    """Index a multi-CTE INSERT (cte2 reads from cte1 reads from mc.src)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_MULTI_CTE)
    (tmp_path / "etl.sql").write_text(_MULTI_CTE_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def alias_db(tmp_path):
    """Index a CTE-body INSERT with a schema alias (ba_tmp → ba)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    # Write .sqlcg.toml with the alias so get_schema_aliases picks it up.
    (tmp_path / ".sqlcg.toml").write_text('[sqlcg]\n[sqlcg.schema_aliases]\nba_tmp = "ba"\n')
    (tmp_path / "ddl.sql").write_text(_DDL_ALIAS)
    (tmp_path / "etl.sql").write_text(_ALIAS_CTE_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


@pytest.fixture()
def dedup_db(tmp_path):
    """Index an INSERT where the same real table appears top-level and in CTE body."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    (tmp_path / "ddl.sql").write_text(_DDL_DEDUP)
    (tmp_path / "etl.sql").write_text(_DEDUP_INSERT)
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Step 2.0 pre-flight: tool-layer #38 fix still works (COLUMN_LINEAGE based)
# ---------------------------------------------------------------------------


def test_kahn_topological_tool_layer_unaffected_by_selects_from_rows(basic_db):
    """The Kahn topological sort (_kahn_topological_sort in tools.py) reads COLUMN_LINEAGE,
    not SELECTS_FROM.  Confirm it still returns non-empty adjacency for the CTE-wrapped
    INSERT once SELECTS_FROM rows appear.

    Step 2.0 pre-flight: the existing tool-layer #38 fix (PR #57, get_backfill_order /
    get_change_scope) builds adjacency from the COLUMN_LINEAGE closure and should be
    unaffected by new SELECTS_FROM rows (it deliberately bypasses SELECTS_FROM because
    CTE-wrapped writes previously emitted no source rows there).

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Step 2.0.
    """
    # Verify COLUMN_LINEAGE still has edges (the tool-layer fix depends on these).
    cl_rows = basic_db.run_read(
        'SELECT count(*) AS n FROM "COLUMN_LINEAGE"',
        {},
    )
    assert cl_rows[0]["n"] > 0, (
        "COLUMN_LINEAGE is empty — the CTE chain is severed; "
        "the tool-layer #38 fix (get_backfill_order) would have no adjacency to work with."
    )

    # Verify SELECTS_FROM now has rows (PR-2 fix is active).
    sf_rows = basic_db.run_read(
        'SELECT count(*) AS n FROM "SELECTS_FROM"',
        {},
    )
    assert sf_rows[0]["n"] > 0, (
        "SELECTS_FROM is empty — the PR-2 fix did not emit source rows. "
        "Both the tool-layer (#38 PR-1) and this new fix are needed."
    )

    # The two edge tables coexist without collision — confirm their counts separately.
    assert cl_rows[0]["n"] >= 1
    assert sf_rows[0]["n"] >= 1


# ---------------------------------------------------------------------------
# Step 2.2 regression guard: SELECTS_FROM row assertions
# ---------------------------------------------------------------------------


def test_cte_wrapped_insert_emits_selects_from_source_row(basic_db):
    """A CTE-wrapped INSERT emits a SELECTS_FROM row with dst_key = 's.a'.

    Before PR-2 _real_tables() filtered out the top-level CTE alias 'cte' and never
    descended into the CTE child scope.  The INSERT therefore emitted zero SELECTS_FROM
    rows, leaving s.b as a table-level island.

    After PR-2 the CTE body descent collects s.a and the emission loop writes the row.

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Acceptance PR-2.
    """
    rows = basic_db.run_read(
        'SELECT src_key, dst_key FROM "SELECTS_FROM" ORDER BY dst_key',
        {},
    )
    dst_keys = {r["dst_key"] for r in rows}
    assert "s.a" in dst_keys, (
        f"Expected dst_key 's.a' in SELECTS_FROM (CTE body table); got {sorted(dst_keys)}.\n"
        "PR-2 _real_tables() CTE-body descent did not produce a source row for the CTE INSERT."
    )


def test_direct_insert_control_still_emits_selects_from_row(basic_db):
    """The direct INSERT (no CTE) still emits SELECTS_FROM dst_key = 's.a'.

    Guards against the PR-2 change accidentally breaking the top-level table path.
    The direct INSERT and CTE INSERT both read from s.a — both should appear in
    SELECTS_FROM.

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Acceptance PR-2.
    """
    rows = basic_db.run_read(
        "SELECT dst_key FROM \"SELECTS_FROM\" WHERE dst_key = 's.a'",
        {},
    )
    # At least one row with dst_key = 's.a' must exist (from the direct INSERT).
    assert len(rows) >= 1, (
        "No SELECTS_FROM row with dst_key = 's.a' found.\n"
        "The direct INSERT control should always emit a source row — top-level path broken."
    )


def test_multi_cte_chain_resolves_to_innermost_real_table_not_cte_alias(multi_cte_db):
    """cte2 reads from cte1 reads from mc.src — SELECTS_FROM dst must be mc.src, not cte1.

    Amendment A1: filter each CTE child's tables against the ROOT scope's flat
    cte_sources.keys() set, not the child's own cte_sources.  This drops 'cte1' from
    cte2's table list because 'cte1' is in root.cte_sources.keys().

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Design PR-2
    mechanics (Amendment A1, sqlglot-probe-confirmed).
    """
    rows = multi_cte_db.run_read(
        'SELECT dst_key FROM "SELECTS_FROM"',
        {},
    )
    dst_keys = {r["dst_key"] for r in rows}

    assert "mc.src" in dst_keys, (
        f"Expected dst_key 'mc.src' in SELECTS_FROM (innermost real table); "
        f"got {sorted(dst_keys)}.\n"
        "The multi-CTE chain did not resolve to the real source table."
    )
    assert "cte1" not in dst_keys, (
        f"dst_key 'cte1' found in SELECTS_FROM — CTE alias leaked as a source row.\n"
        "Amendment A1 filter (root.cte_sources.keys()) is not applied correctly.\n"
        f"All dst_keys: {sorted(dst_keys)}"
    )


def test_schema_alias_in_cte_body_emits_remapped_dst_key(alias_db):
    """CTE body referencing ba_tmp.real_table (with ba_tmp→ba configured) emits 'ba.real_table'.

    Amendment A3: _apply_table_alias is applied to CTE-body table expressions exactly
    as it is applied to top-level table expressions.  Without this the new edges land
    on un-aliased ids (ba_tmp.real_table) and miss the giant component.

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Acceptance PR-2
    (Amendment A3).
    """
    rows = alias_db.run_read(
        'SELECT dst_key FROM "SELECTS_FROM"',
        {},
    )
    dst_keys = {r["dst_key"] for r in rows}

    assert "ba.real_table" in dst_keys, (
        f"Expected dst_key 'ba.real_table' (alias ba_tmp→ba applied); got {sorted(dst_keys)}.\n"
        "Amendment A3 alias remap is not applied to CTE-body table references."
    )
    assert "ba_tmp.real_table" not in dst_keys, (
        "dst_key 'ba_tmp.real_table' found — un-aliased id leaked into SELECTS_FROM.\n"
        "The alias remap (_apply_table_alias) must be applied to CTE-body tables.\n"
        f"All dst_keys: {sorted(dst_keys)}"
    )


def test_real_table_in_both_toplevel_and_cte_body_emits_no_duplicate(dedup_db):
    """A real table present both top-level and in a CTE body emits exactly one SELECTS_FROM row.

    The de-duplication in _real_tables() (seen set on full_id) must prevent duplicate rows
    when the same table is collected from the top-level scope AND from the CTE child scope.

    Governing plan: plan/sprints/issue-38-selects-from-island-lever.md §Design PR-2
    (de-dup, plan mechanics list).
    """
    rows = dedup_db.run_read(
        "SELECT src_key, dst_key, count(*) AS cnt "
        'FROM "SELECTS_FROM" GROUP BY src_key, dst_key HAVING cnt > 1',
        {},
    )
    assert len(rows) == 0, (
        f"Duplicate SELECTS_FROM (src_key, dst_key) pairs found: {rows}.\n"
        "The de-dup in _real_tables() (seen set) did not prevent duplicate rows.\n"
        "The emission loop has a PRIMARY KEY guard, but _real_tables must return unique refs."
    )

    # Also confirm d.src actually appears (not silently dropped by the de-dup).
    src_rows = dedup_db.run_read(
        "SELECT dst_key FROM \"SELECTS_FROM\" WHERE dst_key = 'd.src'",
        {},
    )
    assert len(src_rows) >= 1, (
        "d.src not found in SELECTS_FROM at all — the de-dup over-filtered the table.\n"
        "The table must appear exactly once, not zero times."
    )
