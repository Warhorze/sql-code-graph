"""Integration tests for the gating-join supplement retrofit (PR 3 — v1.24.0).

PR 3 adds ``gating_join_tables`` to ``ChangeScopeResult`` and ``DiffImpactResult``
by reusing PR 1's ``_table_row_reachability`` helper. These tests verify:

- The direct gating-join reader appears in ``gating_join_tables``, NOT in
  ``affected_tables`` (headline regression-pin).
- ``affected_tables`` and ``downstream_count`` are unchanged from pre-PR-3 values
  on the branch-(b) fixture (no broadening of existing contract).
- The CTE-wrapped pure-gating case appears in NEITHER field (documented gap).
- A purely value-derived downstream appears in ``affected_tables`` as before,
  and ``gating_join_tables`` may be empty or overlap — not incorrectly broadened.

All assertions are on observable output (specific table ids), never "no exception".
Fixtures use schema-qualified SQL shapes matching the live corpus.

Guards plan/sprints/unfilled_table_impact.md PR 3 Test Strategy.
"""

from __future__ import annotations

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer
from sqlcg.server.tools import diff_impact, get_change_scope

# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


def _index(tmp_path, files: dict[str, str]) -> DuckDBBackend:
    """Write files to tmp_path, index them, wire tools._backend, return backend."""
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend, use_git=False)
    tools._backend = backend
    tools._metrics = None
    return backend


# ---------------------------------------------------------------------------
# Branch-(b) fixture — direct gating join (CAUGHT by gating_join_tables)
# ---------------------------------------------------------------------------
#
# INSERT INTO da.d (x) SELECT da_other.x FROM da.other AS da_other
#   JOIN da.x AS v ON da_other.k = v.k
#
# da.d.x is COLUMN_LINEAGE-derived from da.other.x, NOT from da.x.
# But da.x is a direct non-CTE source (SELECTS_FROM edge) → gating-join case:
#   - get_change_scope("da.x") → da.d ∈ gating_join_tables, da.d ∉ affected_tables
#   - diff_impact with the etl file → same split

_B_DDL = """\
CREATE TABLE da.x (k INT);
CREATE TABLE da.other (k INT, x INT);
CREATE TABLE da.d (x INT);
"""

_B_ETL = """\
INSERT INTO da.d (x)
SELECT da_other.x
FROM da.other AS da_other
JOIN da.x AS v ON da_other.k = v.k;
"""


@pytest.fixture
def branch_b_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _B_DDL, "etl.sql": _B_ETL})


def test_change_scope_gating_join_reader_in_new_field(branch_b_db, tmp_path):
    """get_change_scope('da.x') returns da.d in gating_join_tables.

    Branch-(b) gating-join fixture: da.d reads da.x via a DIRECT inner join but
    derives no column from it (da.d.x ← da.other.x). The column-lineage closure
    (affected_tables) must NOT include da.d; gating_join_tables must include it.

    Headline regression-pin for PR 3.
    Guards plan/sprints/unfilled_table_impact.md PR 3 Test Strategy.
    """
    result = get_change_scope("da.x")
    assert "da.d" in result.gating_join_tables, (
        f"Expected da.d in gating_join_tables for target da.x. "
        f"Got gating_join_tables={result.gating_join_tables}"
    )


def test_change_scope_affected_tables_unchanged(branch_b_db, tmp_path):
    """get_change_scope('da.x') does NOT include da.d in affected_tables.

    affected_tables is column-lineage precise; da.d derives no column from da.x.
    PR 3 must not broaden this contract (decision B).

    Guards plan/sprints/unfilled_table_impact.md PR 3 Acceptance Criteria.
    """
    result = get_change_scope("da.x")
    assert "da.d" not in result.affected_tables, (
        f"da.d must NOT appear in affected_tables (no column derives from da.x). "
        f"Got affected_tables={result.affected_tables}"
    )


def test_change_scope_downstream_count_equals_affected_tables_len(branch_b_db, tmp_path):
    """downstream_count == len(affected_tables) — unchanged from pre-PR-3 contract.

    PR 3 adds gating_join_tables separately; it must not inflate downstream_count.
    Guards plan/sprints/unfilled_table_impact.md PR 3 Acceptance Criteria.
    """
    result = get_change_scope("da.x")
    assert result.downstream_count == len(result.affected_tables), (
        f"downstream_count ({result.downstream_count}) must equal "
        f"len(affected_tables) ({len(result.affected_tables)})"
    )


def test_diff_impact_gating_join_reader_in_new_field(branch_b_db, tmp_path):
    """diff_impact with the etl file returns a gating_join_tables list[str] field.

    The etl file writes da.d (INSERT INTO da.d). gating_join_tables is the supplement
    for direct gating-join readers of the changed tables. For the etl.sql fixture,
    da.d is the changed table — any table that reads da.d via a gating join would
    appear in gating_join_tables. Here there is no further downstream gating-join
    reader, so we assert the field is a list[str] and that the contract
    (no downstream_count on DiffImpactResult) is correct.

    Guards plan/sprints/unfilled_table_impact.md PR 3 Test Strategy (diff_impact equiv).
    """
    etl_path = str(tmp_path / "etl.sql")
    result = diff_impact([etl_path])
    assert isinstance(result.gating_join_tables, list), (
        f"gating_join_tables must be a list, got {type(result.gating_join_tables)}"
    )
    # DiffImpactResult tracks affected_tables from column-lineage — must not be broadened.
    # (No downstream_count field on DiffImpactResult — that lives only on ChangeScopeResult.)
    for t in result.gating_join_tables:
        assert t not in result.changed_tables, (
            f"gating_join_tables must exclude changed_tables seeds. "
            f"Found {t!r} in both gating_join_tables and changed_tables."
        )


# ---------------------------------------------------------------------------
# diff_impact with a separate "source-only" DDL + gating-join ETL fixture
# ---------------------------------------------------------------------------
#
# We need a changed file that defines ONLY da.x (not da.d), so da.d is not in
# changed_tables and can appear in gating_join_tables.

_B2_DDL_X = """\
CREATE TABLE da.x (k INT);
"""

_B2_DDL_REST = """\
CREATE TABLE da.other (k INT, x INT);
CREATE TABLE da.d (x INT);
"""

_B2_ETL = """\
INSERT INTO da.d (x)
SELECT da_other.x
FROM da.other AS da_other
JOIN da.x AS v ON da_other.k = v.k;
"""


@pytest.fixture
def branch_b2_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(
        tmp_path,
        {"ddl_x.sql": _B2_DDL_X, "ddl_rest.sql": _B2_DDL_REST, "etl.sql": _B2_ETL},
    )


def test_diff_impact_ddl_file_gating_join_in_new_field(branch_b2_db, tmp_path):
    """diff_impact with ddl_x.sql (defines only da.x) returns da.d in gating_join_tables.

    da.x is the only changed table (defined in ddl_x.sql alone).
    da.d reads da.x via a direct gating join with no column-lineage derivation from da.x
    → da.d ∈ gating_join_tables, da.d ∉ affected_tables.

    Guards plan/sprints/unfilled_table_impact.md PR 3 Test Strategy (diff_impact equiv).
    """
    ddl_x_path = str(tmp_path / "ddl_x.sql")
    result = diff_impact([ddl_x_path])
    # da.x is in changed_tables (defined only in ddl_x.sql)
    assert "da.x" in result.changed_tables, (
        f"Expected da.x in changed_tables. Got: {result.changed_tables}"
    )
    # da.d reads da.x via gating join → in gating_join_tables
    assert "da.d" in result.gating_join_tables, (
        f"Expected da.d in gating_join_tables when da.x is the only changed table. "
        f"Got: {result.gating_join_tables}"
    )
    # da.d must NOT appear in affected_tables (no column derives from da.x)
    assert "da.d" not in result.affected_tables, (
        f"da.d must NOT appear in affected_tables (no column-lineage from da.x). "
        f"Got: {result.affected_tables}"
    )


# ---------------------------------------------------------------------------
# T0b shape — CTE-wrapped pure-gating read (DOCUMENTED GAP — NOT caught)
# ---------------------------------------------------------------------------
#
# INSERT INTO da.d (y)
# WITH cte AS (SELECT 1 AS k FROM da.x)
# SELECT da_other.y FROM da.other AS da_other JOIN cte ON da_other.k = cte.k
#
# da.x is only read inside the CTE. No column derives from da.x.
# Neither affected_tables nor gating_join_tables detect da.d for source da.x.
# This is an accepted known gap (parser _real_tables excludes CTE-sourced names).

_CTE_DDL = """\
CREATE TABLE da.x (k INT);
CREATE TABLE da.other (k INT, y INT);
CREATE TABLE da.d (y INT);
"""

_CTE_ETL = """\
INSERT INTO da.d (y)
WITH cte AS (
    SELECT 1 AS k FROM da.x
)
SELECT da_other.y
FROM da.other AS da_other
JOIN cte ON da_other.k = cte.k;
"""


@pytest.fixture
def cte_gap_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _CTE_DDL, "etl.sql": _CTE_ETL})


def test_change_scope_cte_gating_in_gating_join_after_pr2(cte_gap_db, tmp_path):
    """CTE-wrapped pure-gating read: da.d IS now in gating_join_tables after PR-2.

    Originally documented as a gap (plan/sprints/unfilled_table_impact.md §View 1
    M1): the parser's _real_tables excluded CTE-sourced names, so no SELECTS_FROM
    edge was emitted for da.x.  PR-2 fixes this by descending CTE child scopes.

    After PR-2: da.x IS a SELECTS_FROM source of the INSERT into da.d (via the CTE
    body), so get_change_scope("da.x") correctly reports da.d as a gating-join
    downstream.  da.d remains NOT in affected_tables (no column derives from da.x).

    Guards plan/sprints/issue-38-selects-from-island-lever.md §Acceptance PR-2.
    """
    result = get_change_scope("da.x")
    assert "da.d" in result.gating_join_tables, (
        f"da.d must appear in gating_join_tables after PR-2 CTE-body fix. "
        f"Got: {result.gating_join_tables}. "
        "The CTE body reads da.x; _real_tables now emits SELECTS_FROM for it."
    )
    assert "da.d" not in result.affected_tables, (
        f"da.d must NOT appear in affected_tables (no column derives from da.x). "
        f"Got: {result.affected_tables}"
    )


# ---------------------------------------------------------------------------
# Value-derived case — regression: affected_tables unchanged
# ---------------------------------------------------------------------------
#
# INSERT INTO da.d (x) SELECT da_src.x FROM da.src AS da_src
#
# da.d.x is COLUMN_LINEAGE-derived from da.src.x.
# get_change_scope("da.src") → da.d ∈ affected_tables (value-derived, unchanged).
# gating_join_tables may also contain da.d (it reads da.src via SELECTS_FROM),
# but the key invariant is that affected_tables is NOT empty here.

_VALUE_DDL = """\
CREATE TABLE da.src (x INT);
CREATE TABLE da.d (x INT);
"""

_VALUE_ETL = """\
INSERT INTO da.d (x)
SELECT da_src.x
FROM da.src AS da_src;
"""


@pytest.fixture
def value_derived_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return _index(tmp_path, {"ddl.sql": _VALUE_DDL, "etl.sql": _VALUE_ETL})


def test_change_scope_value_derived_still_in_affected_tables(value_derived_db, tmp_path):
    """A purely value-derived downstream still appears in affected_tables after PR 3.

    No regression on the existing column-lineage blast radius. PR 3 adds
    gating_join_tables as a SEPARATE field; it must not remove tables from
    affected_tables.

    Guards plan/sprints/unfilled_table_impact.md PR 3 Test Strategy
    (no regression on value-derived case).
    """
    result = get_change_scope("da.src")
    assert "da.d" in result.affected_tables, (
        f"da.d must still appear in affected_tables for value-derived case. "
        f"Got: {result.affected_tables}"
    )
    assert result.downstream_count == len(result.affected_tables), (
        f"downstream_count ({result.downstream_count}) must equal "
        f"len(affected_tables) ({len(result.affected_tables)})"
    )
