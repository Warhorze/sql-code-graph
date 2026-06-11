"""Integration tests for per-file CTE/derived key namespacing (PR 3).

Indexes two fixture files each defining a CTE named 'final' with one shared
column name into an in-memory DuckDB graph and verifies:
- The two CTE nodes get disjoint graph keys (F3 fix).
- trace_column_lineage from file A's target returns no node whose file is file B.
- The CTE-collision counter reads 0 for the fixture graph.

Guards plan/sprints/sprint_lineage_identity_and_session_context.md §PR 3 / Phase 4.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.cli.coverage import collect_coverage
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


def _routed(backend: DuckDBBackend):
    """Return a run_read_routed side-effect that routes queries to the given backend."""

    def _fn(query: str, params: dict):
        return backend.run_read(query, params)

    return _fn


# ---------------------------------------------------------------------------
# Fixture SQL — two files each defining a CTE named "final" with "revenue" col
# ---------------------------------------------------------------------------

# File A: source_a.revenue → final.revenue → target_a.revenue
_FILE_A_SQL = """\
CREATE TABLE ba.source_a (revenue NUMERIC);
INSERT INTO ba.target_a (revenue)
WITH final AS (SELECT revenue FROM ba.source_a)
SELECT revenue FROM final;
"""

# File B: source_b.revenue → final.revenue → target_b.revenue
_FILE_B_SQL = """\
CREATE TABLE ba.source_b (revenue NUMERIC);
INSERT INTO ba.target_b (revenue)
WITH final AS (SELECT revenue FROM ba.source_b)
SELECT revenue FROM final;
"""


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def indexed_db(db, tmp_path):
    """Index both CTE-fixture files into the in-memory DuckDB backend."""
    (tmp_path / "file_a.sql").write_text(_FILE_A_SQL)
    (tmp_path / "file_b.sql").write_text(_FILE_B_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db


class TestCteKeyNamespacingDisjointness:
    """Same-named CTEs in different files produce disjoint graph keys after PR 3.

    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """

    def test_two_cte_final_nodes_have_disjoint_qualified_keys(self, indexed_db):
        """After indexing, there are exactly two SqlTable nodes with 'final' in qualified,
        and they have different qualified keys (one per file).

        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        rows = indexed_db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte' AND qualified LIKE '%final%'",
            {},
        )
        qualified_keys = [r["qualified"] for r in rows]
        assert len(qualified_keys) == 2, (
            f"Expected exactly 2 CTE 'final' nodes (one per file), got: {qualified_keys}"
        )
        assert qualified_keys[0] != qualified_keys[1], (
            f"Two CTE 'final' nodes have the same key — namespacing not applied: {qualified_keys}"
        )
        # Both keys must contain "::" (the namespace delimiter)
        assert all("::" in k for k in qualified_keys), (
            f"CTE keys missing '::' namespace delimiter: {qualified_keys}"
        )

    def test_cte_collision_counter_is_zero_for_namespaced_fixture(self, indexed_db, tmp_path):
        """The CTE-collision counter (PR 4) reads 0 for the fixture graph.

        After namespacing, no two CTE dst keys receive edges from more than one file.
        Guards sprint_lineage_identity_and_session_context.md §PR 3 (acceptance criterion).
        """
        with patch(
            "sqlcg.cli.coverage.run_read_routed",
            side_effect=_routed(indexed_db),
        ):
            stats = collect_coverage()
        assert stats is not None, "collect_coverage returned None"
        assert stats.cte_key_collisions == 0, (
            f"Expected 0 CTE key collisions after namespacing, got {stats.cte_key_collisions}. "
            "A collision means two files write to the same CTE graph node."
        )

    def test_file_a_cte_key_is_repo_relative(self, indexed_db, tmp_path):
        """After PR 3 (sprint_postmortem_fixes §PR 3), the CTE 'final' node from file_a.sql
        uses a repo-relative posix key ('file_a.sql::final'), not an absolute path.

        Updated from the original absolute-path assertion to verify the new portable
        key format.  Two different absolute roots produce the same relative key.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.1.
        """
        all_cte_rows = indexed_db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte'",
            {},
        )
        # Repo-relative key: file is at the root of tmp_path, so it is just the filename.
        expected_key = "file_a.sql::final"
        matching = [r for r in all_cte_rows if r["qualified"] == expected_key]
        assert len(matching) == 1, (
            f"Expected 1 CTE node with qualified='{expected_key}' (repo-relative), "
            f"got: {all_cte_rows}. "
            f"Absolute path prefix must NOT appear in CTE keys after sprint_postmortem_fixes §PR 3."
        )
        # Verify no absolute path leaked into the key.
        for row in all_cte_rows:
            assert not row["qualified"].startswith("/"), (
                f"CTE key contains absolute path: {row['qualified']!r}. "
                f"Keys must be repo-relative after sprint_postmortem_fixes §PR 3."
            )

    def test_file_b_cte_key_is_repo_relative(self, indexed_db, tmp_path):
        """After PR 3 (sprint_postmortem_fixes §PR 3), the CTE 'final' node from file_b.sql
        uses a repo-relative posix key ('file_b.sql::final'), not an absolute path.

        Guards sprint_postmortem_fixes.md §PR 3 Step 3.1.
        """
        all_cte_rows = indexed_db.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE kind = 'cte'",
            {},
        )
        expected_key = "file_b.sql::final"
        matching = [r for r in all_cte_rows if r["qualified"] == expected_key]
        assert len(matching) == 1, (
            f"Expected 1 CTE node with qualified='{expected_key}' (repo-relative), "
            f"got: {all_cte_rows}. "
            f"Absolute path prefix must NOT appear in CTE keys after sprint_postmortem_fixes §PR 3."
        )


class TestCteNamespacingTraceIsolation:
    """trace_column_lineage from file A's target returns no lineage from file B.

    Guards sprint_lineage_identity_and_session_context.md §PR 3 acceptance.
    """

    def test_column_lineage_from_target_a_does_not_contain_source_b(self, indexed_db):
        """Tracing ba.target_a.revenue upstream returns only file A's source (ba.source_a).

        Before namespacing, the collided 'final' node would cause the trace to
        also include ba.source_b (via file B's CTE path). After namespacing,
        the two CTE nodes are disjoint so cross-file pollution cannot happen.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY

        # BFS upstream from ba.target_a.revenue
        visited: set[str] = set()
        queue = ["ba.target_a.revenue"]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            rows = indexed_db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current})
            for row in rows:
                node_id = row["id"]
                if node_id not in visited:
                    queue.append(node_id)

        # ba.source_b.revenue must NOT appear in the trace from file A's target
        assert "ba.source_b.revenue" not in visited, (
            f"Cross-file CTE pollution: ba.source_b.revenue appeared in trace of "
            f"ba.target_a.revenue. Namespacing did not prevent the collision. "
            f"Visited nodes: {sorted(visited)}"
        )

    def test_column_lineage_from_target_a_does_contain_source_a(self, indexed_db):
        """Tracing ba.target_a.revenue upstream returns ba.source_a.revenue.

        Verifies the trace still works correctly after namespacing — the within-file
        CTE chain must remain intact.
        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY

        visited: set[str] = set()
        queue = ["ba.target_a.revenue"]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            rows = indexed_db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": current})
            for row in rows:
                node_id = row["id"]
                if node_id not in visited:
                    queue.append(node_id)

        # ba.source_a.revenue MUST appear (intra-file CTE chain intact)
        assert "ba.source_a.revenue" in visited, (
            f"Intra-file CTE chain broken: ba.source_a.revenue not in trace of "
            f"ba.target_a.revenue after namespacing. "
            f"Visited nodes: {sorted(visited)}"
        )


class TestCoverageDstTableStripOnNamespacedKey:
    """coverage._DST_TABLE strip correctly handles namespaced CTE column keys.

    _DST_TABLE = "left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))" strips
    after the LAST dot.  For "path/file.sql::final.col_name" this yields
    "path/file.sql::final" — the namespaced table qualified key.
    Guards sprint_lineage_identity_and_session_context.md §PR 3.
    """

    def test_dst_table_strip_on_namespaced_column_key(self, db):
        """DuckDB _DST_TABLE expression strips correctly on a namespaced column key.

        Guards sprint_lineage_identity_and_session_context.md §PR 3.
        """
        _DST_TABLE = "left(dst_key, len(dst_key) - instr(reverse(dst_key), '.'))"
        result = db.run_read(
            f"SELECT {_DST_TABLE} AS dst_table FROM (VALUES (?) ) AS t(dst_key)",
            {"dst_key": "a/b.sql::final.revenue"},
        )
        assert result, "Query returned no rows"
        assert result[0]["dst_table"] == "a/b.sql::final", (
            f"_DST_TABLE strip on namespaced key gave wrong result: {result[0]['dst_table']!r}. "
            "Expected 'a/b.sql::final'."
        )
