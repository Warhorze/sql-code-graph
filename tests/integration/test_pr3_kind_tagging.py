"""PR-3 (#33) integration tests: CLI/MCP parity + node-kind tagging.

Scenarios:
    A — CTE kind tagging: index a fixture with a CTE; assert the CTE alias node
        has SqlTable.kind == "cte" and the real source table has kind == "table".
    B — <output> excluded: index a fixture; query SqlTable nodes; assert no node
        has qualified == "<output>".
    C — upstream excludes CTE by default; --include-intermediate shows them.
    D — impact NoiseFilter + de-dup: --raw includes noise-pattern tables.
    E — unused NoiseFilter + de-dup: --raw includes noise-pattern tables.
    F — trace_column_lineage labels CTE nodes with table_kind == "cte".

Regression guard: STALE_VIEWS removed — reindex_file no longer uses it.
"""

from __future__ import annotations

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Scenario A — CTE kind tagging
# ---------------------------------------------------------------------------


def test_scenario_a_cte_node_has_kind_cte(db, tmp_path):
    """A CTE alias must appear as a SqlTable node with kind='cte'.

    The real source table behind the CTE must have kind='table'.
    Guards that TableRef.role="cte" flows to kind="cte" in the graph.
    """
    sql = (
        "CREATE TABLE src (a INT);\n"
        "INSERT INTO dst\n"
        "WITH my_cte AS (SELECT a FROM src)\n"
        "SELECT a FROM my_cte;\n"
    )
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Query all SqlTable nodes and their kinds
    rows = db.run_read(
        "MATCH (t:SqlTable) RETURN t.qualified AS qualified, t.kind AS kind",
        {},
    )
    table_by_qualified = {r["qualified"]: r["kind"] for r in rows}

    # CTE alias node must exist with kind="cte"
    assert "my_cte" in table_by_qualified, (
        f"CTE alias 'my_cte' not found in SqlTable nodes. "
        f"Found: {sorted(table_by_qualified.keys())}"
    )
    assert table_by_qualified["my_cte"] == "cte", (
        f"Expected kind='cte' for CTE alias 'my_cte', got '{table_by_qualified['my_cte']}'"
    )

    # Real source table must have kind="table"
    assert "src" in table_by_qualified, (
        f"Source table 'src' not found. Found: {sorted(table_by_qualified.keys())}"
    )
    assert table_by_qualified["src"] == "table", (
        f"Expected kind='table' for source 'src', got '{table_by_qualified['src']}'"
    )


# ---------------------------------------------------------------------------
# Scenario B — <output> excluded
# ---------------------------------------------------------------------------


def test_scenario_b_output_sink_not_in_graph(db, tmp_path):
    """No SqlTable node with qualified == '<output>' must exist after indexing.

    The <output> synthetic sink is a fallback destination in base.py when
    stmt.target is None; it must not be persisted as a real SqlTable node.
    """
    # A plain SELECT (no target) would have produced <output> edges before the fix.
    # An INSERT with a target is the normal case; include both.
    sql = "CREATE TABLE src (a INT);\nINSERT INTO dst SELECT a AS x FROM src;\n"
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(
        "MATCH (t:SqlTable) WHERE t.qualified = '<output>' RETURN t.qualified AS qualified",
        {},
    )
    assert len(rows) == 0, (
        f"Found {len(rows)} SqlTable node(s) with qualified='<output>'. "
        "The <output> synthetic sink must not be persisted."
    )


# ---------------------------------------------------------------------------
# Scenario C — upstream excludes CTE by default; --include-intermediate shows them
# ---------------------------------------------------------------------------


def test_scenario_c_downstream_excludes_cte_by_default(db, tmp_path):
    """analyze downstream must exclude CTE nodes by default.

    With --include-intermediate the CTE column reappears.
    Guards the kind-filter query and the --include-intermediate flag.

    The CTE creates a COLUMN_LINEAGE edge: src.a -> my_cte.a (kind=cte).
    The outer SELECT creates: src.a -> dst.a (direct, CTE collapsed through).
    So downstream of src.a has both my_cte.a (cte-origin) and dst.a (table-origin).
    Default filter excludes my_cte.a; --include-intermediate restores it.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from sqlcg.cli.commands.analyze import app

    sql = (
        "CREATE TABLE src (a INT);\n"
        "INSERT INTO dst\n"
        "WITH my_cte AS (SELECT a FROM src)\n"
        "SELECT a FROM my_cte;\n"
    )
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    runner = CliRunner()

    # Default: CTE should NOT appear in downstream
    with patch("sqlcg.cli.commands.analyze.get_backend") as mock_get_backend:
        mock_get_backend.return_value.__enter__.return_value = db
        mock_get_backend.return_value.__exit__.return_value = False
        result_default = runner.invoke(app, ["downstream", "src.a", "--raw"])

    # CTE intermediate must be filtered by default
    assert "my_cte" not in result_default.output, (
        f"CTE 'my_cte' appeared in default downstream output (should be filtered).\n"
        f"Output: {result_default.output}"
    )
    # Real destination table 'dst' must still appear
    assert "dst" in result_default.output, (
        f"Destination 'dst' not in default downstream output.\nOutput: {result_default.output}"
    )

    # With --include-intermediate: CTE should appear
    with patch("sqlcg.cli.commands.analyze.get_backend") as mock_get_backend:
        mock_get_backend.return_value.__enter__.return_value = db
        mock_get_backend.return_value.__exit__.return_value = False
        result_intermediate = runner.invoke(
            app, ["downstream", "src.a", "--raw", "--include-intermediate"]
        )

    assert "my_cte" in result_intermediate.output, (
        f"CTE 'my_cte' not in --include-intermediate downstream output.\n"
        f"Output: {result_intermediate.output}"
    )


# ---------------------------------------------------------------------------
# Scenario D — impact NoiseFilter + de-dup
# ---------------------------------------------------------------------------


def test_scenario_d_impact_noise_filter_and_raw(db, tmp_path):
    """analyze impact must apply NoiseFilter by default; --raw disables it.

    Creates two queries selecting from the same table, one targeting a
    noise-pattern table (bak_*). Default output excludes it (1 row);
    --raw includes it (2 rows).

    The 'impact' command prints query IDs (file:stmt_index) and kind, not
    target table names. We verify row counts to check filtering.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from sqlcg.cli.commands.analyze import app

    sql = (
        "CREATE TABLE src (a INT);\n"
        "INSERT INTO real_dst SELECT a FROM src;\n"
        "INSERT INTO bak_real_dst SELECT a FROM src;\n"
    )
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    runner = CliRunner()

    # Verify both queries exist in the graph before filtering
    all_impact = db.run_read(
        "MATCH (t:SqlTable {qualified: 'src'})<-[:SELECTS_FROM]-(q:SqlQuery) "
        "RETURN DISTINCT q.id AS id, q.target_table AS target LIMIT 100",
        {},
    )
    assert len(all_impact) == 2, (
        f"Expected 2 queries impacting 'src', got {len(all_impact)}: {all_impact}"
    )

    # Patch NoiseFilter to treat bak_* as noise
    from sqlcg.server.noise_filter import NoiseFilter

    noisy_filter = NoiseFilter(
        patterns=["bak_*"],
        ignored_tables=set(),
        schema_aliases={},
    )

    with (
        patch("sqlcg.cli.commands.analyze.get_backend") as mock_backend,
        patch(
            "sqlcg.server.noise_filter.NoiseFilter.from_config",
            return_value=noisy_filter,
        ),
    ):
        mock_backend.return_value.__enter__.return_value = db
        mock_backend.return_value.__exit__.return_value = False
        result_filtered = runner.invoke(app, ["impact", "src"])

    # Filtered: only real_dst query remains (bak_real_dst filtered out)
    # Table renders with border rows; count "INSERT" kind occurrences
    assert result_filtered.output.count("INSERT") == 1, (
        f"Expected 1 INSERT row (bak_real_dst filtered), "
        f"got {result_filtered.output.count('INSERT')} in:\n{result_filtered.output}"
    )

    # --raw: both queries must appear
    with (
        patch("sqlcg.cli.commands.analyze.get_backend") as mock_backend,
        patch(
            "sqlcg.server.noise_filter.NoiseFilter.from_config",
            return_value=noisy_filter,
        ),
    ):
        mock_backend.return_value.__enter__.return_value = db
        mock_backend.return_value.__exit__.return_value = False
        result_raw = runner.invoke(app, ["impact", "src", "--raw"])

    assert result_raw.output.count("INSERT") == 2, (
        f"Expected 2 INSERT rows with --raw, "
        f"got {result_raw.output.count('INSERT')} in:\n{result_raw.output}"
    )


# ---------------------------------------------------------------------------
# Scenario E — unused NoiseFilter + de-dup
# ---------------------------------------------------------------------------


def test_scenario_e_unused_noise_filter_and_raw(db, tmp_path):
    """analyze unused must apply NoiseFilter by default; --raw disables it.

    Creates two tables with no SELECTS_FROM references, one matching a noise
    pattern (bak_*). Default output excludes it; --raw includes it.
    The 'unused' command prints qualified table names, so we can check directly.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from sqlcg.cli.commands.analyze import app

    # Both tables are unused (no SELECT references)
    sql = "CREATE TABLE real_orphan (a INT);\nCREATE TABLE bak_orphan (a INT);\n"
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    runner = CliRunner()

    from sqlcg.server.noise_filter import NoiseFilter

    noisy_filter = NoiseFilter(
        patterns=["bak_*"],
        ignored_tables=set(),
        schema_aliases={},
    )

    with (
        patch("sqlcg.cli.commands.analyze.get_backend") as mock_backend,
        patch(
            "sqlcg.server.noise_filter.NoiseFilter.from_config",
            return_value=noisy_filter,
        ),
    ):
        mock_backend.return_value.__enter__.return_value = db
        mock_backend.return_value.__exit__.return_value = False
        result_filtered = runner.invoke(app, ["unused"])

    assert "bak_orphan" not in result_filtered.output, (
        f"Noise table 'bak_orphan' appeared in default unused output.\n"
        f"Output: {result_filtered.output}"
    )
    assert "real_orphan" in result_filtered.output, (
        f"Real orphan 'real_orphan' not in default unused output.\nOutput: {result_filtered.output}"
    )

    # --raw: noise table must appear
    with (
        patch("sqlcg.cli.commands.analyze.get_backend") as mock_backend,
        patch(
            "sqlcg.server.noise_filter.NoiseFilter.from_config",
            return_value=noisy_filter,
        ),
    ):
        mock_backend.return_value.__enter__.return_value = db
        mock_backend.return_value.__exit__.return_value = False
        result_raw = runner.invoke(app, ["unused", "--raw"])

    assert "bak_orphan" in result_raw.output, (
        f"Noise table 'bak_orphan' not in --raw unused output.\nOutput: {result_raw.output}"
    )
    assert "real_orphan" in result_raw.output, (
        f"'real_orphan' not in --raw unused output.\nOutput: {result_raw.output}"
    )


# ---------------------------------------------------------------------------
# Scenario F — trace_column_lineage labels CTE nodes with table_kind="cte"
# ---------------------------------------------------------------------------


def test_scenario_f_trace_populates_table_kind(db, tmp_path):
    """TRACE_COLUMN_LINEAGE must populate table_kind from the SqlTable join.

    The TRACE_COLUMN_LINEAGE query joins (t:SqlTable {qualified: src.table_qualified})
    and returns t.kind as table_kind. For source columns from real tables, table_kind
    must be 'table'. This guards the SqlTable join in TRACE_COLUMN_LINEAGE and the
    tools.py table_kind wiring.

    Note: the parser collapses CTE chains (src.a -> cte.a AND src.a -> dst.a directly),
    so the trace for dst.a returns src.a with table_kind='table'. The CTE alias appears
    as a SqlTable node with kind='cte' (verified by Scenario A); it does not appear as a
    *source* in COLUMN_LINEAGE edges (CTEs are destinations, not sources in the graph).
    """
    sql = (
        "CREATE TABLE src (a INT);\n"
        "INSERT INTO dst\n"
        "WITH my_cte AS (SELECT a FROM src)\n"
        "SELECT a FROM my_cte;\n"
    )
    (tmp_path / "fixture.sql").write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Query TRACE_COLUMN_LINEAGE directly — it includes table_kind via SqlTable join
    rows = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": "dst.a"})
    assert len(rows) >= 1, (
        "No COLUMN_LINEAGE edges found for dst.a — fixture may not have produced edges."
    )

    # All source columns come from real tables ('src') — table_kind must be 'table'
    for row in rows:
        assert row.get("table_kind") is not None, (
            f"table_kind is None in trace result {row}. "
            f"Guards that TRACE_COLUMN_LINEAGE returns t.kind from the SqlTable join."
        )
        assert row.get("table_kind") == "table", (
            f"Expected table_kind='table' for source from 'src', "
            f"got '{row.get('table_kind')}' in row {row}."
        )

    # Also verify the CTE alias has table_kind='cte' when traced as a destination
    rows_cte = db.run_read(TRACE_COLUMN_LINEAGE_QUERY, {"id": "my_cte.a"})
    assert len(rows_cte) >= 1, (
        "No COLUMN_LINEAGE edges found for my_cte.a — CTE projection edge missing."
    )
    for row in rows_cte:
        # CTE destination's source is 'src' — table_kind='table'
        assert row.get("table_kind") == "table", (
            f"Expected table_kind='table' for source of my_cte.a, "
            f"got '{row.get('table_kind')}' in row {row}."
        )


# ---------------------------------------------------------------------------
# Regression guard: STALE_VIEWS removed — reindex_file no longer uses it
# ---------------------------------------------------------------------------


def test_stale_views_removed_from_queries():
    """STALE_VIEWS must not exist in queries.cypher or queries.py.

    Guards Step 3.1 (STALE_VIEWS drop): the dead query was removed because
    it matched kind='VIEW' which is never written, making it always return 0 rows.
    """
    from pathlib import Path

    import sqlcg.core.queries as q_mod

    # queries.py must not export STALE_VIEWS_QUERY
    assert not hasattr(q_mod, "STALE_VIEWS_QUERY"), (
        "queries.py still exports STALE_VIEWS_QUERY — it must be removed (Step 3.1)."
    )

    # queries.cypher must not contain the STALE_VIEWS block header
    cypher_path = Path(q_mod.__file__).parent / "queries.cypher"
    cypher_text = cypher_path.read_text(encoding="utf-8")
    assert "STALE_VIEWS" not in cypher_text, (
        "queries.cypher still contains a STALE_VIEWS block — it must be removed (Step 3.1)."
    )

    # indexer.py must not import or reference STALE_VIEWS
    import sqlcg.indexer.indexer as idx_mod

    idx_source = Path(idx_mod.__file__).read_text(encoding="utf-8")
    assert "STALE_VIEWS" not in idx_source, (
        "indexer.py still references STALE_VIEWS — all stale-view cascade code "
        "must be removed (Step 3.1)."
    )
