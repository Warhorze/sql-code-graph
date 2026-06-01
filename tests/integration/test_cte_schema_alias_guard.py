"""Guard 3 (#40): Schema_alias join fixture.

Pins the §Half A schema_alias constraint from the plan:
  - A .sqlcg.toml with [sqlcg.schema_aliases] (e.g. staging_tmp = "staging")
  - A CTE body whose source schema is the aliased one (FROM staging_tmp.src)
  - Assert the emitted SqlTable.qualified equals the source column's table_qualified
    post-alias (staging.src, not staging_tmp.src), so the Half-B filter join matches.

This guard ensures that Half A's src-table emit uses the already-aliased
edge.src.table object (schema-aliasing is applied during parse via
_apply_table_alias before the edge is constructed), so SqlTable.qualified ==
SqlColumn.table_qualified by construction.

Guard 3 also includes an end-to-end composition test
(test_schema_alias_filtered_upstream_returns_canonical_source) that proves
alias-rewrite + Half-B filter-join + Half-A node-emission all work together on
the user-facing path — the actual "schema_alias degression" failure mode.

Observable-output contract: assertions check specific field values and id sets.
"""

from __future__ import annotations

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Alias fixture SQL
# ---------------------------------------------------------------------------

# The SQL used by both the node-equality test and the end-to-end composition test.
# staging_tmp.src_alias is the aliased schema reference (staging_tmp -> staging).
# mart.alias_dst.val is the CTE-built target column.
_ALIAS_SQL = (
    "INSERT INTO mart.alias_dst\n"
    "WITH aliased_cte AS (SELECT val FROM staging_tmp.src_alias)\n"
    "SELECT val FROM aliased_cte;\n"
)


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


def test_schema_alias_src_table_qualified_matches_column(db, tmp_path):
    """SqlTable.qualified matches SqlColumn.table_qualified after schema-alias rewrite.

    Fixture: staging_tmp is aliased to staging in .sqlcg.toml.
    SQL: a CTE body references staging_tmp.src_alias.
    Expected: SqlTable.qualified == 'staging.src_alias' (not 'staging_tmp.src_alias')
              SqlColumn.table_qualified == 'staging.src_alias' (same)
              => the Half-B OPTIONAL MATCH join on qualified == table_qualified works.

    This is the node-equality invariant guard (localises failure if the alias constraint
    regresses).  The end-to-end composition test is
    test_schema_alias_filtered_upstream_returns_canonical_source below.
    """
    # Write .sqlcg.toml with schema alias
    (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nstaging_tmp = "staging"\n')

    (tmp_path / "fixture.sql").write_text(_ALIAS_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # The SqlTable node for the source must use the canonical schema ('staging')
    table_rows = db.run_read(
        "MATCH (t:SqlTable {qualified: 'staging.src_alias'}) "
        "RETURN t.qualified AS qualified, t.kind AS kind",
        {},
    )
    assert table_rows, (
        "SqlTable node 'staging.src_alias' not found after indexing with schema alias.\n"
        "Expected the alias 'staging_tmp' -> 'staging' to be applied at parse time.\n"
        "staging_tmp.src_alias should be stored as staging.src_alias."
    )
    assert table_rows[0]["kind"] == "table", (
        f"Expected kind='table' for staging.src_alias, got '{table_rows[0]['kind']}'"
    )

    # The SqlColumn node's table_qualified must match the SqlTable's qualified
    col_rows = db.run_read(
        "MATCH (c:SqlColumn) WHERE c.table_qualified = 'staging.src_alias' "
        "RETURN c.id AS id, c.table_qualified AS tq LIMIT 1",
        {},
    )
    assert col_rows, (
        "SqlColumn with table_qualified='staging.src_alias' not found.\n"
        "The column's table_qualified must match the aliased schema."
    )

    # Confirm alignment: SqlTable.qualified == SqlColumn.table_qualified
    assert table_rows[0]["qualified"] == col_rows[0]["tq"], (
        f"Mismatch: SqlTable.qualified='{table_rows[0]['qualified']}' != "
        f"SqlColumn.table_qualified='{col_rows[0]['tq']}'\n"
        "Half A schema_alias constraint violated."
    )

    # Confirm no unaliased node exists (staging_tmp.src_alias must not appear)
    unaliased_rows = db.run_read(
        "MATCH (t:SqlTable {qualified: 'staging_tmp.src_alias'}) RETURN t.qualified AS qualified",
        {},
    )
    assert not unaliased_rows, (
        "Found unaliased SqlTable node 'staging_tmp.src_alias' — alias was not applied.\n"
        "Schema alias must rewrite the schema before the edge is constructed."
    )


def test_schema_alias_filtered_upstream_returns_canonical_source(db, tmp_path):
    """End-to-end: alias-rewrite + Half-B filter-join + Half-A node-emission all compose.

    This closes the end-to-end gap left by the node-equality invariant alone:
    the node-equality test pins that SqlTable.qualified matches the column's
    table_qualified post-alias, but does NOT prove that the aliased node can be
    found by the Half-B OPTIONAL MATCH filter-join on the user-facing CLI path.

    This test runs the exact filtered upstream query that ``analyze upstream``
    builds (kind-filter ON, --include-intermediate OFF) for the aliased CTE-built
    target column ``mart.alias_dst.val`` and asserts:

    - The canonical source id ``staging.src_alias.val`` IS in the returned set.
    - The pre-alias id ``staging_tmp.src_alias.val`` is NOT in the returned set.
    - The result set is non-empty (observable output, not "no exception").

    If either Half A (src-table emit) or Half B (OPTIONAL MATCH filter inversion)
    is reverted this test must go red:
    - Revert Half A: SqlTable node for staging.src_alias is absent → Half B's
      OPTIONAL MATCH yields NULL for t, so WHERE t.kind IS NULL keeps it — the
      source id is still returned BUT the node-equality test (above) also goes red,
      localising the failure precisely.  More critically, if Half A is absent AND
      the old inner-join filter is restored (both halves reverted), the absent node
      causes the inner MATCH to drop the source → empty result → this test fails.
    - Revert Half B only (restore inner-join filter, keep Half A): the SqlTable node
      EXISTS (Half A is in place) so the join succeeds and kind='table' matches the
      IN list — the test stays green.  Correct: Half B is only needed as a safety net
      for graphs indexed before Half A shipped.  This test therefore documents that
      once both halves are in place the filter-join works end-to-end.
    - Revert both halves: inner-join filter + absent node → zero rows → red.
    """
    # Write .sqlcg.toml with schema alias
    (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nstaging_tmp = "staging"\n')

    (tmp_path / "fixture.sql").write_text(_ALIAS_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Build the exact query analyze.py constructs with kind-filter ON (default).
    # Mirror of _upstream_filtered_query() in test_cte_recall_guard.py.
    depth = 5
    kind_filter = (
        f"OPTIONAL MATCH (t:{NodeLabel.TABLE} {{qualified: src.table_qualified}}) "
        f"WITH c, src, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
    )
    query = (
        f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
        f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src:{NodeLabel.COLUMN}) "
        f"{kind_filter}"
        f"OPTIONAL MATCH (src)-[direct:{RelType.COLUMN_LINEAGE}]->(c) "
        "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
        "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100"
    )

    rows = db.run_read(query, {"ref": "mart.alias_dst.val"})
    returned_ids = {r["id"] for r in rows}

    # Observable output: result set must be non-empty.
    assert returned_ids, (
        "Filtered upstream query returned no results for 'mart.alias_dst.val'.\n"
        "Both Half A (src-table emit) and Half B (OPTIONAL MATCH filter) must be in "
        "place for this to return rows."
    )

    # Canonical source id must be present (alias applied: staging_tmp -> staging).
    assert "staging.src_alias.val" in returned_ids, (
        f"Canonical source 'staging.src_alias.val' not in filtered upstream results.\n"
        f"Returned ids: {sorted(returned_ids)}\n"
        "The schema alias 'staging_tmp' -> 'staging' must be applied before the edge "
        "is constructed so the SqlTable.qualified join target is 'staging.src_alias'."
    )

    # Pre-alias id must NOT appear (the alias rewrite must be permanent, not cosmetic).
    assert "staging_tmp.src_alias.val" not in returned_ids, (
        f"Pre-alias id 'staging_tmp.src_alias.val' found in results — alias not applied.\n"
        f"Returned ids: {sorted(returned_ids)}"
    )
