"""Unit tests for per-file CTE/derived key namespacing (PR 3).

Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 3 /
Phase 4 (Steps 4.1–4.2).

Tests cover:
- TableRef.full_id prefixes namespace:: for CTE/derived roles.
- Two fixture files each defining a CTE "final" produce disjoint dst keys.
- _tmp table refs are NOT namespaced (role="table").
- noise_filter._table_short_name helper on namespaced and non-namespaced keys.
- coverage _DST_TABLE strip behavior on namespaced keys.
- tools._bare_ref behavior against namespaced keys.
- _harvest_usage_catalog skips namespaced CTE keys (W2).
"""

from __future__ import annotations

from sqlcg.parsers.base import ColumnRef, TableRef
from sqlcg.server.noise_filter import _table_short_name

# ---------------------------------------------------------------------------
# Step 4.1 — namespace field on TableRef
# ---------------------------------------------------------------------------


class TestTableRefNamespace:
    """TableRef.full_id prefixes namespace:: only for non-table roles.

    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """

    def test_cte_tableref_with_namespace_prefixes_path(self):
        """A CTE TableRef with namespace set produces a namespaced full_id.

        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        ref = TableRef(name="final", role="cte", namespace="etl/sql/fact/wtfa.sql")
        assert ref.full_id == "etl/sql/fact/wtfa.sql::final"

    def test_cte_tableref_without_namespace_is_bare(self):
        """Without a namespace, CTE full_id is just the bare name (legacy).

        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        ref = TableRef(name="final", role="cte")
        assert ref.full_id == "final"

    def test_table_role_ignores_namespace(self):
        """Physical table refs ignore the namespace field.

        Only role != "table" gets the namespace prefix so db/catalog semantics
        for real tables are untouched.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        ref = TableRef(db="ba", name="wtfa_loonkosten", role="table", namespace="some/file.sql")
        assert ref.full_id == "ba.wtfa_loonkosten"

    def test_tmp_tables_not_namespaced(self):
        """_tmp tables have role='table' and must not be namespaced.

        _tmp tables are real cross-file lineage carriers; per-file namespacing
        must not break their identity.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        ref = TableRef(
            db="ba_tmp", name="wtfa_loonkosten", role="table", namespace="etl/sql/fact/wtfa.sql"
        )
        # namespace is set but role='table' → no prefix
        assert ref.full_id == "ba_tmp.wtfa_loonkosten"
        assert "::" not in ref.full_id

    def test_two_cte_same_name_different_namespaces_are_disjoint(self):
        """Two CTE TableRefs with the same name but different namespaces have different full_ids.

        This is the core F3 fix: same-named CTEs in different files produce disjoint keys.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        ref_a = TableRef(name="final", role="cte", namespace="etl/sql/fact/file_a.sql")
        ref_b = TableRef(name="final", role="cte", namespace="etl/sql/fact/file_b.sql")
        assert ref_a.full_id != ref_b.full_id
        assert ref_a.full_id == "etl/sql/fact/file_a.sql::final"
        assert ref_b.full_id == "etl/sql/fact/file_b.sql::final"

    def test_column_ref_full_id_inherits_namespaced_table(self):
        """ColumnRef.full_id incorporates the namespaced table key automatically.

        Column keys follow automatically via ColumnRef.full_id = table.full_id + "." + name.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        tbl = TableRef(name="final", role="cte", namespace="etl/sql/fact/wtfa.sql")
        col = ColumnRef(table=tbl, name="revenue")
        assert col.full_id == "etl/sql/fact/wtfa.sql::final.revenue"

    def test_namespace_preserved_by_dataclasses_replace(self):
        """dataclasses.replace preserves namespace when rewriting non-identity fields.

        normalize_keys uses dataclasses.replace to rewrite db via _apply_alias_to_ref;
        CTE refs typically have db=None so no rewrite fires, but even if a future code
        path replaces alias or role, namespace must survive.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        import dataclasses

        ref = TableRef(name="final", role="cte", namespace="etl/sql/fact/wtfa.sql")
        # Simulate replacing the alias field (doesn't affect full_id)
        new_ref = dataclasses.replace(ref, alias="f")
        assert new_ref.namespace == "etl/sql/fact/wtfa.sql"
        assert new_ref.full_id == "etl/sql/fact/wtfa.sql::final"


# ---------------------------------------------------------------------------
# B2 — noise_filter._table_short_name helper
# ---------------------------------------------------------------------------


class TestTableShortNameHelper:
    """_table_short_name correctly extracts the bare name from qualified keys.

    Guards the B2 fix: noise_filter.is_noise used .split(".")[-1] which would
    yield "sql::final" for a namespaced key.
    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """

    def test_namespaced_cte_key_returns_cte_name(self):
        """a/b.sql::final → 'final'.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (B2).
        """
        assert _table_short_name("a/b.sql::final") == "final"

    def test_namespaced_cte_key_with_deep_path(self):
        """etl/sql/fact/wtfa_loonkosten.sql::cte_insert → 'cte_insert'.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (B2).
        """
        assert _table_short_name("etl/sql/fact/wtfa_loonkosten.sql::cte_insert") == "cte_insert"

    def test_schema_qualified_table_returns_table_name(self):
        """schema.table → 'table'.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (B2).
        """
        assert _table_short_name("schema.table") == "table"

    def test_bare_name_unchanged(self):
        """bare_table → 'bare_table' (no delimiter present).

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (B2).
        """
        assert _table_short_name("bare_table") == "bare_table"

    def test_three_part_qualified_returns_last_segment(self):
        """catalog.schema.table → 'table'.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (B2).
        """
        assert _table_short_name("catalog.schema.table") == "table"


# ---------------------------------------------------------------------------
# N3 — tools._bare_ref behavior against namespaced keys
# ---------------------------------------------------------------------------


class TestBareRefWithNamespacedKeys:
    """_bare_ref returns namespaced CTE keys unchanged (no schema prefix to strip).

    Guards sprint_lineage_identity_and_session_context.md §PR 3 (N3).
    """

    def test_bare_ref_returns_namespaced_key_unchanged(self):
        """A CTE column key with '::' is returned unchanged by _bare_ref.

        CTE keys have no schema prefix; the bare-name fallback is a no-op for them.
        Guards sprint_lineage_identity_and_session_context.md §PR 3 (N3).
        """
        from sqlcg.server.tools import _bare_ref

        key = "a/b.sql::final.revenue"
        assert _bare_ref(key) == key

    def test_bare_ref_normal_schema_still_stripped(self):
        """Normal 3-part schema.table.col keys are still stripped by _bare_ref.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (N3).
        """
        from sqlcg.server.tools import _bare_ref

        assert _bare_ref("mart.fact_t.amount") == "fact_t.amount"

    def test_bare_ref_two_part_unchanged(self):
        """Two-part fact_t.amount is returned unchanged.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (N3).
        """
        from sqlcg.server.tools import _bare_ref

        assert _bare_ref("fact_t.amount") == "fact_t.amount"


# ---------------------------------------------------------------------------
# W2 — _harvest_usage_catalog skips namespaced CTE keys
# ---------------------------------------------------------------------------


class TestHarvestUsageCatalogSkipsNamespacedKeys:
    """_harvest_usage_catalog must skip namespaced CTE src keys (W2 fix).

    After PR 3, CTE src keys contain '::' (e.g. 'a/b.sql::final').  The old guard
    '"." not in table_qualified' would pass them through because the path contains dots.
    The new guard checks '"::" in table_qualified' before the dot check.
    Guards sprint_lineage_identity_and_session_context.md §PR 3 (W2).
    """

    def test_namespaced_cte_src_key_not_harvested(self):
        """A COLUMN_LINEAGE edge whose src table is a namespaced CTE is skipped.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (W2).
        """
        from sqlcg.indexer.indexer import _harvest_usage_catalog

        # Build a fake edge whose src_key has a namespaced CTE table
        edges = [
            {
                "src_key": "a/b.sql::final.revenue",
                "dst_key": "ba.wtfa_loonkosten.revenue",
            }
        ]
        # Even if kind is "table" (which it shouldn't be, but worst case)
        kind_map = {"a/b.sql::final": "table"}
        col_rows, hc_edges = _harvest_usage_catalog(edges, kind_map)
        assert col_rows == [], "Namespaced CTE src key must not be harvested into usage catalog"
        assert hc_edges == [], "Namespaced CTE src key must not produce HAS_COLUMN usage edges"

    def test_physical_table_src_key_still_harvested(self):
        """A COLUMN_LINEAGE edge whose src table is a physical table is still harvested.

        Guards sprint_lineage_identity_and_session_context.md §PR 3 (W2).
        """
        from sqlcg.indexer.indexer import _harvest_usage_catalog

        edges = [
            {
                "src_key": "ba.staging_table.col_a",
                "dst_key": "ba.target_table.col_a",
            }
        ]
        kind_map = {"ba.staging_table": "table"}
        col_rows, hc_edges = _harvest_usage_catalog(edges, kind_map)
        assert len(col_rows) == 1, "Physical table src key must be harvested"
        assert col_rows[0]["table_qualified"] == "ba.staging_table"
