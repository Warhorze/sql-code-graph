"""Unit tests for the cte_source_gap_writes coverage metric (PR-1).

The metric counts write-kind SqlQuery rows that have ≥1 COLUMN_LINEAGE edge
but zero SELECTS_FROM source rows — CTE-wrapped writes whose column path
resolved but whose table path emitted nothing.

Plan: plan/sprints/issue-38-selects-from-island-lever.md §PR-1 Step 1.4
"""

from __future__ import annotations

import duckdb

from sqlcg.cli.coverage import _Q_CTE_SOURCE_GAP_WRITES

# ---------------------------------------------------------------------------
# Fixtures — synthetic in-memory DuckDB graph
# ---------------------------------------------------------------------------


def _build_synthetic_db() -> duckdb.DuckDBPyConnection:
    """Build a minimal in-memory DuckDB graph with three SqlQuery rows.

    Row 1 — CTE-wrapped write (INSERT kind):
        has COLUMN_LINEAGE but NO SELECTS_FROM row → counted by the metric.
    Row 2 — direct write (INSERT kind):
        has COLUMN_LINEAGE AND a SELECTS_FROM row → NOT counted.
    Row 3 — SELECT-kind CTAS with inferred target_table:
        has COLUMN_LINEAGE but NO SELECTS_FROM — must be EXCLUDED because
        kind='SELECT' is outside _WRITE_KINDS (Amendment A2).
    """
    con = duckdb.connect(":memory:")

    con.execute("""
        CREATE TABLE "SqlQuery" (
            id VARCHAR PRIMARY KEY,
            kind VARCHAR,
            target_table VARCHAR,
            file_path VARCHAR,
            parse_failed BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE "COLUMN_LINEAGE" (
            src_key VARCHAR,
            dst_key VARCHAR,
            query_id VARCHAR,
            inferred_from_source_name BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE "SELECTS_FROM" (
            src_key VARCHAR,
            dst_key VARCHAR
        )
    """)

    # Row 1: CTE-wrapped INSERT — COLUMN_LINEAGE present (real edge), SELECTS_FROM absent
    con.execute(
        'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
        ("q1", "INSERT", "ba.target_a", "etl/file_a.sql", False),
    )
    con.execute(
        'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
        ("ba.target_a.col1", "ba.source_a.col1", "q1", False),
    )
    # No SELECTS_FROM row for q1 — this is the gap the metric targets

    # Row 2: direct INSERT — COLUMN_LINEAGE present AND SELECTS_FROM present
    con.execute(
        'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
        ("q2", "INSERT", "ba.target_b", "etl/file_b.sql", False),
    )
    con.execute(
        'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
        ("ba.target_b.col1", "ba.source_b.col1", "q2", False),
    )
    con.execute(
        'INSERT INTO "SELECTS_FROM" VALUES (?, ?)',
        ("q2", "ba.source_b"),
    )

    # Row 3: SELECT-kind CTAS with inferred target_table — must be excluded
    # (Amendment A2: kind not in _WRITE_KINDS → not counted even if SELECTS_FROM absent)
    con.execute(
        'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
        ("q3", "SELECT", "da.ctas_target", "etl/file_c.sql", False),
    )
    con.execute(
        'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
        ("da.ctas_target.col1", "da.source_c.col1", "q3", False),
    )
    # No SELECTS_FROM row for q3 either

    return con


class TestCteSourceGapWritesMetric:
    """The cte_source_gap_writes query counts only write-kind rows with the gap.

    Plan: plan/sprints/issue-38-selects-from-island-lever.md §PR-1 Step 1.4
    """

    def test_cte_source_gap_writes_counts_gap_row_excludes_direct_and_select_kind(self):
        """Query returns exactly 1 on the synthetic graph.

        The graph has:
        - q1 (INSERT, gap): counted → contributes 1
        - q2 (INSERT, no gap — SELECTS_FROM present): not counted
        - q3 (SELECT kind, gap): excluded by kind discriminator (Amendment A2)

        Asserts the integer value 1, not merely "no exception".
        """
        con = _build_synthetic_db()
        try:
            result = con.execute(_Q_CTE_SOURCE_GAP_WRITES).fetchone()
        finally:
            con.close()

        assert result is not None, "Query returned no rows"
        count = result[0]
        assert count == 1, (
            f"Expected cte_source_gap_writes=1 on synthetic graph "
            f"(q1=gap-write, q2=direct-no-gap, q3=SELECT-kind-excluded), got {count}"
        )

    def test_cte_source_gap_writes_zero_on_all_direct_graph(self):
        """Query returns 0 when every write-kind row has a SELECTS_FROM source.

        Guards the "inverse complement of zero_edge_write_queries" claim —
        when there is no gap the metric reports 0, not a spurious positive.
        """
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE "SqlQuery" (
                id VARCHAR PRIMARY KEY,
                kind VARCHAR,
                target_table VARCHAR,
                file_path VARCHAR,
                parse_failed BOOLEAN
            )
        """)
        con.execute("""
            CREATE TABLE "COLUMN_LINEAGE" (
                src_key VARCHAR,
                dst_key VARCHAR,
                query_id VARCHAR,
                inferred_from_source_name BOOLEAN
            )
        """)
        con.execute("""
            CREATE TABLE "SELECTS_FROM" (
                src_key VARCHAR,
                dst_key VARCHAR
            )
        """)
        # One INSERT that has both COLUMN_LINEAGE and SELECTS_FROM — no gap
        con.execute(
            'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
            ("q1", "INSERT", "ba.target", "etl/f.sql", False),
        )
        con.execute(
            'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
            ("ba.target.col1", "ba.src.col1", "q1", False),
        )
        con.execute('INSERT INTO "SELECTS_FROM" VALUES (?, ?)', ("q1", "ba.src"))

        try:
            result = con.execute(_Q_CTE_SOURCE_GAP_WRITES).fetchone()
        finally:
            con.close()

        assert result is not None
        count = result[0]
        assert count == 0, f"Expected cte_source_gap_writes=0 on all-direct graph, got {count}"

    def test_cte_source_gap_writes_select_kind_excluded_by_discriminator(self):
        """SELECT-kind rows are excluded even when they have target_table and no SELECTS_FROM.

        This is the Amendment A2 discriminator: only kind IN _WRITE_KINDS counts.
        """
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE "SqlQuery" (
                id VARCHAR PRIMARY KEY,
                kind VARCHAR,
                target_table VARCHAR,
                file_path VARCHAR,
                parse_failed BOOLEAN
            )
        """)
        con.execute("""
            CREATE TABLE "COLUMN_LINEAGE" (
                src_key VARCHAR,
                dst_key VARCHAR,
                query_id VARCHAR,
                inferred_from_source_name BOOLEAN
            )
        """)
        con.execute('CREATE TABLE "SELECTS_FROM" (src_key VARCHAR, dst_key VARCHAR)')

        # A SELECT-kind row with a non-empty target_table and COLUMN_LINEAGE but no SELECTS_FROM
        con.execute(
            'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
            ("q_select", "SELECT", "da.inferred_target", "etl/ctas.sql", False),
        )
        con.execute(
            'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
            ("da.inferred_target.col1", "da.src.col1", "q_select", False),
        )

        try:
            result = con.execute(_Q_CTE_SOURCE_GAP_WRITES).fetchone()
        finally:
            con.close()

        assert result is not None
        count = result[0]
        assert count == 0, (
            f"SELECT-kind row must be excluded by kind discriminator, got count={count}"
        )

    def test_cte_source_gap_excludes_writes_with_only_inferred_edges(self):
        """Writes whose COLUMN_LINEAGE edges are all inferred_from_source_name=TRUE are excluded.

        No-FROM literal inserts (INSERT … SELECT NULL AS x) produce only phantom
        (inferred=TRUE) edges.  They are not addressable CTE gaps and must not be
        counted even though they have COLUMN_LINEAGE but no SELECTS_FROM.

        Guards #116 floor-filter
        ([plan doc](plan/sprints/sprint_backlog_q2_bugfix.md)).
        """
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE "SqlQuery" (
                id VARCHAR PRIMARY KEY,
                kind VARCHAR,
                target_table VARCHAR,
                file_path VARCHAR,
                parse_failed BOOLEAN
            )
        """)
        con.execute("""
            CREATE TABLE "COLUMN_LINEAGE" (
                src_key VARCHAR,
                dst_key VARCHAR,
                query_id VARCHAR,
                inferred_from_source_name BOOLEAN
            )
        """)
        con.execute('CREATE TABLE "SELECTS_FROM" (src_key VARCHAR, dst_key VARCHAR)')

        # INSERT with only inferred edges (no-FROM literal insert floor)
        con.execute(
            'INSERT INTO "SqlQuery" VALUES (?, ?, ?, ?, ?)',
            ("q_literal", "INSERT", "t", "etl/literal.sql", False),
        )
        con.execute(
            'INSERT INTO "COLUMN_LINEAGE" VALUES (?, ?, ?, ?)',
            ("t.x", "t.x", "q_literal", True),  # inferred=TRUE
        )
        # No SELECTS_FROM row

        try:
            result = con.execute(_Q_CTE_SOURCE_GAP_WRITES).fetchone()
        finally:
            con.close()

        assert result is not None
        count = result[0]
        assert count == 0, (
            f"Expected 0: all-inferred write must not be counted as CTE gap, got {count}"
        )
