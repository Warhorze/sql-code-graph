"""Failing acceptance tests for plan/sprints/fix_firstuser_findings.md.

Each test is named after the step it exercises and must FAIL (or skip) until the
developer implements that step. Tests that accidentally pass before implementation
are not valid acceptance tests.

Steps covered:
  Step 1.1  — TableRef lowercases catalog, db, name in __post_init__
  Step 1.2  — ColumnRef lowercases name in __post_init__
  Step 1.4  — find_table_usages lowercases table_name input
  Step 1.4b — _parse_column_ref lowercases col_ref input (BQ-1 fix)
  Step 3.1  — BFS traversal deduplicates emitted nodes by node_id
  Step 4.1  — empty-lineage hint contains schema-prefix example
  Step 4.2  — downstream empty hint contains "terminal output"
  Step 4.3  — trace_column_lineage docstring contains schema-qualified example
  Step 5.1  — duplicate-DDL collision logged at DEBUG, not WARNING
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Step 1.1 — TableRef case normalization
# ---------------------------------------------------------------------------


def test_S1_1_tableref_lowercases_db_and_name():
    """TableRef.__post_init__ must lowercase db and name so full_id is lowercase."""
    from sqlcg.parsers.base import TableRef

    ref_upper = TableRef(db="BA", name="WTFE_KPI")
    ref_lower = TableRef(db="ba", name="wtfe_kpi")

    # After step 1.1 both must produce the same lowercase full_id
    assert ref_upper.full_id == "ba.wtfe_kpi", f"Expected 'ba.wtfe_kpi', got '{ref_upper.full_id}'"
    assert ref_upper.full_id == ref_lower.full_id, (
        f"Mixed-case and lowercase TableRef must produce identical full_id. "
        f"Upper: '{ref_upper.full_id}', lower: '{ref_lower.full_id}'"
    )


def test_S1_1_tableref_lowercases_catalog():
    """TableRef.__post_init__ must lowercase the catalog component."""
    from sqlcg.parsers.base import TableRef

    ref = TableRef(catalog="MY_WAREHOUSE", db="BA", name="T")
    assert ref.full_id == "my_warehouse.ba.t", f"Expected 'my_warehouse.ba.t', got '{ref.full_id}'"


def test_S1_1_tableref_alias_not_lowercased():
    """TableRef alias must NOT be lowercased — it is not an identity field."""
    from sqlcg.parsers.base import TableRef

    ref = TableRef(db="ba", name="t", alias="SRC")
    # alias should be preserved as-is
    assert ref.alias == "SRC", f"alias must not be lowercased, expected 'SRC', got '{ref.alias}'"


# ---------------------------------------------------------------------------
# Step 1.2 — ColumnRef case normalization
# ---------------------------------------------------------------------------


def test_S1_2_columnref_lowercases_name():
    """ColumnRef.__post_init__ must lowercase the column name."""
    from sqlcg.parsers.base import ColumnRef, TableRef

    col = ColumnRef(TableRef(db="BA", name="T"), "MA_ROTATIE")
    assert col.full_id == "ba.t.ma_rotatie", f"Expected 'ba.t.ma_rotatie', got '{col.full_id}'"


def test_S1_2_columnref_full_id_equal_for_any_case():
    """ColumnRef.full_id must be identical regardless of input case."""
    from sqlcg.parsers.base import ColumnRef, TableRef

    col_upper = ColumnRef(TableRef(db="BA", name="T"), "COL")
    col_lower = ColumnRef(TableRef(db="ba", name="t"), "col")

    assert col_upper.full_id == col_lower.full_id, (
        f"full_id mismatch: '{col_upper.full_id}' vs '{col_lower.full_id}'"
    )


# ---------------------------------------------------------------------------
# Step 1.4 — find_table_usages lowercases input
# ---------------------------------------------------------------------------


def test_S1_4_find_table_usages_lowercases_input():
    """find_table_usages must pass table_name.lower() to the Cypher query."""
    from sqlcg.server.tools import find_table_usages

    captured_params = {}

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        captured_params.update(params)
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        find_table_usages("WTFE_KPI_ROTATIE_WEBSHOP")

    name_param = captured_params.get("name", "NOT_FOUND")
    assert name_param == "wtfe_kpi_rotatie_webshop", (
        f"Expected name param to be lowercased 'wtfe_kpi_rotatie_webshop', got '{name_param}'"
    )


# ---------------------------------------------------------------------------
# Step 1.4b — _parse_column_ref lowercases input (BQ-1 fix)
# ---------------------------------------------------------------------------


def test_S1_4b_parse_column_ref_lowercases_input():
    """_parse_column_ref must lowercase col_ref before splitting.

    This is BQ-1: without this fix, trace_column_lineage('BA.T.COL') constructs
    col_id='BA.T.COL' which cannot match a graph that stores 'ba.t.col'.
    """
    from sqlcg.server.tools import trace_column_lineage

    captured_id_params = []

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        if "id" in params:
            captured_id_params.append(params["id"])
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        trace_column_lineage("BA.WTFE_KPI.MA_ROTATIE")

    assert captured_id_params, "No id param was captured — BFS loop never ran a query"
    first_id = captured_id_params[0]
    assert first_id == "ba.wtfe_kpi.ma_rotatie", (
        f"Expected id param to be lowercased 'ba.wtfe_kpi.ma_rotatie', got '{first_id}'"
    )


def test_S1_4b_downstream_lowercases_input():
    """get_downstream_dependencies must also lowercase the col_ref input."""
    from sqlcg.server.tools import get_downstream_dependencies

    captured_id_params = []

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        if "id" in params:
            captured_id_params.append(params["id"])
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        get_downstream_dependencies("BA.T.COL")

    assert captured_id_params, "No id param captured"
    assert captured_id_params[0] == "ba.t.col", (
        f"Expected 'ba.t.col', got '{captured_id_params[0]}'"
    )


# ---------------------------------------------------------------------------
# Step 3.1 — BFS dedup emitted nodes
# ---------------------------------------------------------------------------


def test_S3_1_trace_column_lineage_deduplicates_nodes():
    """trace_column_lineage must not emit the same node_id twice.

    Scenario: the root column has TWO upstream sources that both point to the same
    upstream node. Without an emitted-set guard, that upstream node appears twice in
    lineage.
    """
    from sqlcg.server.tools import trace_column_lineage

    # The BFS: root col_id -> two rows pointing to same src node
    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        current = params.get("id", "")
        if current in ("ba.tgt.col", "ba.tgt.col"):
            # Two rows with the SAME upstream id — simulates the duplicate scenario
            return [
                {"id": "ba.src.col", "col_name": "col"},
                {"id": "ba.src.col", "col_name": "col"},
            ]
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = trace_column_lineage("ba.tgt.col")

    # After dedup, only one node with id 'ba.src.col' should appear
    names = [n.name for n in result.lineage]
    assert len(names) == 1, f"Expected 1 deduplicated node, got {len(names)}: {names}"


# ---------------------------------------------------------------------------
# Steps 2.1 + 2.3 (F3) — owning table surfaced on each traversal node
# ---------------------------------------------------------------------------


def test_S2_trace_lineage_node_carries_table():
    """trace_column_lineage nodes must surface table_qualified as `table`."""
    from sqlcg.server.tools import trace_column_lineage

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        if params.get("id") == "ba.tgt.col":
            return [
                {"id": "ba.src.col", "col_name": "col", "table_qualified": "ba.src"},
            ]
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = trace_column_lineage("ba.tgt.col")

    assert result.lineage, "Expected at least one lineage node"
    node = result.lineage[0]
    assert node.name == "col", f"name must be the bare column, got {node.name!r}"
    assert node.table == "ba.src", f"node.table must be 'ba.src', got {node.table!r}"


def test_S2_upstream_node_carries_table():
    """get_upstream_dependencies nodes must surface table_qualified as `table`."""
    from sqlcg.server.tools import get_upstream_dependencies

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        if params.get("id") == "ba.tgt.col":
            return [{"id": "ba.src.col", "col_name": "col", "table_qualified": "ba.src"}]
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = get_upstream_dependencies("ba.tgt.col")

    assert result.nodes, "Expected at least one dependency node"
    assert result.nodes[0].table == "ba.src", (
        f"node.table must be 'ba.src', got {result.nodes[0].table!r}"
    )


def test_S2_downstream_node_carries_table():
    """get_downstream_dependencies nodes must surface table_qualified as `table`."""
    from sqlcg.server.tools import get_downstream_dependencies

    def run_read_side_effect(query, params):
        if "count" in query.lower() or "Repo" in query:
            return [{"n": 1}]
        if params.get("id") == "ba.tgt.col":
            return [{"id": "ba.dst.col", "col_name": "col", "table_qualified": "ba.dst"}]
        return []

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = run_read_side_effect
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = get_downstream_dependencies("ba.tgt.col")

    assert result.nodes, "Expected at least one dependency node"
    assert result.nodes[0].table == "ba.dst", (
        f"node.table must be 'ba.dst', got {result.nodes[0].table!r}"
    )


# ---------------------------------------------------------------------------
# Step 4.1 — Hint contains schema-prefix example
# ---------------------------------------------------------------------------


def test_S4_1_trace_lineage_empty_hint_contains_schema_prefix():
    """Empty-lineage hint must contain the schema-qualified example."""
    from sqlcg.server.tools import trace_column_lineage

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = [
            [{"n": 1}],  # _assert_indexed
            [],  # primary BFS query — no lineage
            [],  # PR-06 bare-name fallback BFS (3-part ref) — also empty
            # A1 _empty_closure_hint probe (GET_COLUMNS_FOR_TABLE) — table IS
            # catalogued, so the GENERIC "no lineage found" hint fires (the one
            # this test pins); the no-catalog hint is covered by the dedicated
            # A1 acceptance test (test_clone_positional_insert_blindspot.py).
            [{"col_id": "ba.t.col", "col_name": "col"}],
        ]
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = trace_column_lineage("ba.t.col")

    assert result.hint is not None
    assert "ba.table_name.column_name" in result.hint, (
        f"Empty-lineage hint must contain 'ba.table_name.column_name'. Got: {result.hint!r}"
    )


def test_S4_1_upstream_empty_hint_contains_schema_prefix():
    """get_upstream_dependencies empty hint must contain schema-qualified example."""
    from sqlcg.server.tools import get_upstream_dependencies

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = [
            [{"n": 1}],
            [],
            # A1 probe — table IS catalogued, so the GENERIC hint fires (the one
            # this test pins); the no-catalog hint has its own dedicated test.
            [{"col_id": "ba.t.col", "col_name": "col"}],
        ]
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = get_upstream_dependencies("ba.t.col")

    assert result.hint is not None
    assert "ba.table_name.column_name" in result.hint, (
        f"Upstream hint must contain 'ba.table_name.column_name'. Got: {result.hint!r}"
    )


# ---------------------------------------------------------------------------
# Step 4.2 — Downstream hint distinguishes terminal from lookup failure
# ---------------------------------------------------------------------------


def test_S4_2_downstream_empty_hint_contains_terminal_output():
    """get_downstream_dependencies empty hint must say 'terminal output'."""
    from sqlcg.server.tools import get_downstream_dependencies

    with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = [
            [{"n": 1}],  # _assert_indexed
            [],  # downstream BFS query — no column nodes
            [],  # batch consumer query — no external consumers
            [{"col_id": "ba.t.col", "col_name": "col"}],  # A1 probe — table IS catalogued
        ]
        mock_backend_ctx.return_value.__enter__.return_value = mock_db

        result = get_downstream_dependencies("ba.t.col")

    assert result.hint is not None
    assert "terminal" in result.hint.lower(), (
        f"Downstream hint must mention 'terminal'. Got: {result.hint!r}"
    )


def test_S4_2_downstream_hint_differs_from_upstream_hint():
    """Downstream and upstream empty hints must be distinct strings."""
    from sqlcg.server.tools import get_downstream_dependencies, get_upstream_dependencies

    def make_mock(side_effects):
        with patch("sqlcg.server.tools._open_backend") as mock_backend_ctx:
            mock_db = MagicMock()
            mock_db.run_read.side_effect = side_effects
            mock_backend_ctx.return_value.__enter__.return_value = mock_db
            return mock_backend_ctx

    # Both probes return a non-empty catalog so each tool's GENERIC empty hint
    # fires (the distinction this test pins is generic-upstream vs
    # generic-downstream/"terminal output", not catalog-vs-no-catalog — that's
    # covered separately by the dedicated A1 acceptance test).
    _catalog_probe = [{"col_id": "ba.t.col", "col_name": "col"}]

    with patch("sqlcg.server.tools._open_backend") as m:
        mock_db = MagicMock()
        mock_db.run_read.side_effect = [[{"n": 1}], [], _catalog_probe]
        m.return_value.__enter__.return_value = mock_db
        upstream = get_upstream_dependencies("ba.t.col")

    with patch("sqlcg.server.tools._open_backend") as m:
        mock_db = MagicMock()
        # indexed, bfs, batch-consumer, A1 catalog-probe (terminal-output branch)
        mock_db.run_read.side_effect = [[{"n": 1}], [], [], _catalog_probe]
        m.return_value.__enter__.return_value = mock_db
        downstream = get_downstream_dependencies("ba.t.col")

    assert upstream.hint != downstream.hint, (
        "Upstream and downstream empty hints must be different strings to distinguish "
        "terminal output from lookup failure."
    )


# ---------------------------------------------------------------------------
# Step 4.3 — Docstring contains schema-qualified example
# ---------------------------------------------------------------------------


def test_S4_3_trace_column_lineage_docstring_has_schema_example():
    """trace_column_lineage docstring must document the schema-qualified input format."""
    from sqlcg.server import tools

    doc = tools.trace_column_lineage.__doc__ or ""
    assert "ba.table_name.column_name" in doc or "schema" in doc.lower(), (
        f"trace_column_lineage docstring must contain schema-qualified example. Got: {doc!r}"
    )
    # Stricter: must have the literal example per acceptance criterion
    assert "ba.table_name.column_name" in doc, (
        f"Docstring must contain literal 'ba.table_name.column_name'. Got: {doc!r}"
    )


# ---------------------------------------------------------------------------
# Step 5.1 — Duplicate-DDL warning level lowered to DEBUG
# ---------------------------------------------------------------------------


def test_S5_1_duplicate_ddl_not_logged_at_warning(tmp_path, caplog):
    """Duplicate DDL collision must not emit a WARNING log entry.

    The structured error string 'duplicate_ddl:' must still be recorded in
    parsed.errors so the signal remains queryable.

    Strategy: call _upsert_parsed_file directly with a mocked db and a
    pre-populated defined_table_registry so the duplicate branch fires without
    needing a real KuzuDB instance.
    """
    from pathlib import Path

    try:
        from sqlcg.indexer.indexer import Indexer
        from sqlcg.parsers.base import ParsedFile, TableRef
    except ImportError:
        pytest.skip("Required symbols not importable")

    # Construct a minimal ParsedFile that defines table 'ba.orders'
    parsed2 = ParsedFile(path=Path(str(tmp_path / "f2.sql")), dialect=None)
    tbl = TableRef(db="ba", name="orders")
    parsed2.defined_tables.append(tbl)

    # pre-populated registry says f1.sql already owns 'ba.orders'
    registry = {"ba.orders": str(tmp_path / "f1.sql")}

    mock_db = MagicMock()
    mock_db.upsert_nodes_bulk.return_value = None
    mock_db.upsert_edges_bulk.return_value = None

    indexer = Indexer()

    with caplog.at_level(logging.WARNING, logger="sqlcg.indexer.indexer"):
        try:
            indexer._upsert_parsed_file(parsed2, mock_db, defined_table_registry=registry)
        except Exception:
            pass  # We only care about the log, not upsert success

    # After step 5.1: no WARNING should be emitted for the duplicate_ddl event
    dup_warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and (
            "already defined" in r.getMessage().lower()
            or "duplicate_ddl" in r.getMessage()
            or "add columns" in r.getMessage()
        )
    ]
    assert not dup_warnings, (
        f"Expected no WARNING for duplicate DDL collision (should be DEBUG). "
        f"Found: {[r.getMessage() for r in dup_warnings]}"
    )

    # The structured error entry must still be present in parsed2.errors
    dup_errors = [e for e in parsed2.errors if "duplicate_ddl" in e]
    assert dup_errors, f"Expected 'duplicate_ddl:' in parsed2.errors. Got errors: {parsed2.errors}"
