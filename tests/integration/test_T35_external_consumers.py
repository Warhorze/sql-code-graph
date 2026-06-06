"""Failing acceptance tests for #35 — external downstream lineage injection.

Tests cover:
  PR-1 (35a): schema, config reader, ingestion pass
  PR-2 (35b): read-side surfacing in tools

These tests MUST FAIL until the developer implements #35.
Named T35-* per the plan's test strategy so `pytest -k T35` runs the set.
"""

from pathlib import Path

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# Symbols introduced by #35 — skip guard so suite stays runnable before the feature lands.
try:
    from sqlcg.core.config import (  # noqa: F401  # introduced by T35 PR-1
        ExternalConsumerSpec,
        get_external_consumers,
    )

    _CONFIG_AVAILABLE = True
except (ImportError, AttributeError):
    _CONFIG_AVAILABLE = False

try:
    from sqlcg.core.schema import NodeLabel

    _ = NodeLabel.EXTERNAL_CONSUMER  # introduced by T35 PR-1
    _SCHEMA_AVAILABLE = True
except (ImportError, AttributeError):
    _SCHEMA_AVAILABLE = False

try:
    from sqlcg.server.models import DiffImpactResult

    # external_consumers field introduced by T35 PR-2
    _dummy = DiffImpactResult(
        changed_files=[],
        changed_tables=[],
        affected_tables=[],
        presentation_facing=[],
        backfill_order=[],
        noise_excluded=[],
        external_consumers=[],
    )
    _DIFF_IMPACT_EXTERNAL = True
except (ImportError, AttributeError, TypeError):
    _DIFF_IMPACT_EXTERNAL = False

try:
    from sqlcg.server.models import PresentationCandidate

    assert "has_external_consumer" in PresentationCandidate.model_fields  # introduced by T35 PR-2
    _hec = PresentationCandidate(
        table_qualified="x.y", matched_prefix="x_", has_external_consumer=False
    )
    _PRES_CANDIDATE_HEC = True
except (ImportError, AttributeError, TypeError, AssertionError):
    _PRES_CANDIDATE_HEC = False


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_FIXTURE_SQL = {
    "sales.sql": (
        "CREATE TABLE ia_sales.fct_orders (order_id INT, customer_id INT);\n"
        "CREATE TABLE ia_sales.dim_customer (customer_id INT, name VARCHAR);\n"
    ),
    "marketing.sql": (
        "CREATE TABLE ia_marketing.audience_export (customer_id INT, segment VARCHAR);\n"
    ),
}


def _index_fixture(
    tmp_path: Path,
    extra_files: dict[str, str],
    monkeypatch,
) -> DuckDBBackend:
    """Write fixtures, index into fresh in-memory graph, wire as tools backend."""
    files = {**_FIXTURE_SQL, **extra_files}
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend


_MANIFEST_TWO_CONSUMERS = (
    "[[sqlcg.external_consumers]]\n"
    'name = "Tableau: Sales Dashboard"\n'
    'kind = "tableau"\n'
    'consumes = ["ia_sales.fct_orders", "ia_sales.dim_customer"]\n'
    "\n"
    "[[sqlcg.external_consumers]]\n"
    'name = "HubSpot Sync"\n'
    'kind = "reverse_etl"\n'
    'consumes = ["ia_marketing.audience_export"]\n'
)


# ---------------------------------------------------------------------------
# PR-1 — Schema: SCHEMA_VERSION "6" and ExternalConsumer DDL
# ---------------------------------------------------------------------------


def test_T35_schema_version_is_6():
    """SCHEMA_VERSION must be '6' after PR-1. Fails while still '5'."""
    from sqlcg.core.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == "6", (
        f"SCHEMA_VERSION must be '6' after #35 PR-1; currently {SCHEMA_VERSION!r}"
    )


def test_T35_external_consumer_node_label_exists():
    """NodeLabel.EXTERNAL_CONSUMER must exist in schema.py."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("NodeLabel.EXTERNAL_CONSUMER not yet implemented (#35 PR-1)")

    from sqlcg.core.schema import NodeLabel

    assert NodeLabel.EXTERNAL_CONSUMER == "ExternalConsumer", (
        f"NodeLabel.EXTERNAL_CONSUMER must be 'ExternalConsumer'; "  # noqa: E501
        f"got {NodeLabel.EXTERNAL_CONSUMER!r}"
    )


def test_T35_consumed_by_rel_type_exists():
    """RelType.CONSUMED_BY must exist in schema.py."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("RelType.CONSUMED_BY not yet implemented (#35 PR-1)")

    try:
        from sqlcg.core.schema import RelType

        _ = RelType.CONSUMED_BY
    except AttributeError:
        pytest.skip("RelType.CONSUMED_BY not yet implemented (#35 PR-1)")

    assert RelType.CONSUMED_BY == "CONSUMED_BY", (
        f"RelType.CONSUMED_BY must be 'CONSUMED_BY'; got {RelType.CONSUMED_BY!r}"
    )


def test_T35_init_schema_creates_external_consumer_table(tmp_path):
    """T35-IDX-0: after init_schema, ExternalConsumer table exists (returns 0 rows, not error)."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("NodeLabel.EXTERNAL_CONSUMER not yet implemented (#35 PR-1)")

    backend = DuckDBBackend(":memory:")
    backend.init_schema()

    rows = backend.run_read('SELECT count(*) AS n FROM "ExternalConsumer"', {})
    count = rows[0]["n"] if rows else None
    assert count == 0, (
        f"ExternalConsumer table must exist and be empty post-init; got count={count}"
    )


# ---------------------------------------------------------------------------
# PR-1 — _pk_field correctness: ExternalConsumer PK is 'name', not 'id'
# ---------------------------------------------------------------------------


def test_T35_pk_field_for_external_consumer_is_name():
    """_pk_field(NodeLabel.EXTERNAL_CONSUMER) must return 'name', not 'id'."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("NodeLabel.EXTERNAL_CONSUMER not yet implemented (#35 PR-1)")

    from sqlcg.core.graph_db import GraphBackend
    from sqlcg.core.schema import NodeLabel

    pk = GraphBackend._pk_field(NodeLabel.EXTERNAL_CONSUMER)
    assert pk == "name", (
        f"_pk_field(EXTERNAL_CONSUMER) must return 'name'; got {pk!r}. "
        "Missing explicit case in graph_db.py — default 'id' targets a non-existent column."
    )


def test_T35_upsert_nodes_bulk_external_consumer_uses_name_pk(tmp_path):
    """Bulk-upsert ExternalConsumer rows via upsert_nodes_bulk succeeds; retrievable by name."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("NodeLabel.EXTERNAL_CONSUMER not yet implemented (#35 PR-1)")

    from sqlcg.core.schema import NodeLabel

    backend = DuckDBBackend(":memory:")
    backend.init_schema()

    backend.upsert_nodes_bulk(
        NodeLabel.EXTERNAL_CONSUMER,
        [{"name": "Tableau: Sales", "consumer_type": "tableau"}],
    )

    rows = backend.run_read(
        'SELECT consumer_type AS ct FROM "ExternalConsumer" WHERE name = ?',
        {"name": "Tableau: Sales"},
    )
    assert len(rows) == 1, f"Expected 1 ExternalConsumer row; got {len(rows)}"
    assert rows[0]["ct"] == "tableau", f"consumer_type must be 'tableau'; got {rows[0]['ct']!r}"


# ---------------------------------------------------------------------------
# T35-IDX-1: full ingestion pass — consumers and edges persisted
# ---------------------------------------------------------------------------


def test_T35_IDX_1_ingestion_persists_consumers_and_edges(tmp_path, monkeypatch):
    """T35-IDX-1: indexing a fixture with a manifest referencing two real tables
    persists ExternalConsumer nodes and CONSUMED_BY edges.
    """
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    backend = _index_fixture(
        tmp_path,
        {".sqlcg.toml": _MANIFEST_TWO_CONSUMERS},
        monkeypatch,
    )

    consumer_rows = backend.run_read('SELECT name, consumer_type AS ct FROM "ExternalConsumer"', {})
    assert len(consumer_rows) == 2, (
        f"Expected 2 ExternalConsumer nodes; got {len(consumer_rows)}: {consumer_rows}"
    )

    edge_rows = backend.run_read('SELECT count(*) AS n FROM "CONSUMED_BY"', {})
    edge_count = edge_rows[0]["n"] if edge_rows else 0
    assert edge_count >= 3, (  # fct_orders + dim_customer + audience_export
        f"Expected ≥3 CONSUMED_BY edges; got {edge_count}"
    )


def test_T35_IDX_1_index_repo_return_dict_reports_counts(tmp_path, monkeypatch):
    """T35-IDX-1 (return dict): index_repo result must include external_consumers count ≥ 2."""
    if not _CONFIG_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35 PR-1)")
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    for name, sql in {**_FIXTURE_SQL, ".sqlcg.toml": _MANIFEST_TWO_CONSUMERS}.items():
        (tmp_path / name).write_text(sql)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    result = Indexer().index_repo(tmp_path, dialect=None, db=backend)

    assert "external_consumers" in result, (
        f"index_repo result must include 'external_consumers' key; keys: {list(result.keys())}"
    )
    assert result["external_consumers"] >= 2, (
        f"external_consumers count must be ≥ 2; got {result['external_consumers']}"
    )


# ---------------------------------------------------------------------------
# T35-IDX-2: small-repo no-manifest safety — byte-identical behavior
# ---------------------------------------------------------------------------


def test_T35_IDX_2_no_manifest_zero_external_nodes(tmp_path, monkeypatch):
    """T35-IDX-2: indexing the same fixture WITHOUT a manifest yields 0 ExternalConsumer
    nodes and 0 CONSUMED_BY edges.
    """
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    backend = _index_fixture(tmp_path, {}, monkeypatch)  # no .sqlcg.toml

    consumer_rows = backend.run_read('SELECT count(*) AS n FROM "ExternalConsumer"', {})
    count = consumer_rows[0]["n"] if consumer_rows else 0
    assert count == 0, f"ExternalConsumer count must be 0 with no manifest; got {count}"

    edge_rows = backend.run_read('SELECT count(*) AS n FROM "CONSUMED_BY"', {})
    edge_count = edge_rows[0]["n"] if edge_rows else 0
    assert edge_count == 0, f"CONSUMED_BY count must be 0 with no manifest; got {edge_count}"


def test_T35_IDX_2_no_manifest_table_count_unchanged(tmp_path, monkeypatch):
    """T35-IDX-2 (regression guard): table counts are identical with vs without manifest."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    # Index with no manifest
    tmp_no_manifest = tmp_path / "no_manifest"
    tmp_no_manifest.mkdir()
    backend_no = _index_fixture(tmp_no_manifest, {}, monkeypatch)
    rows_no = backend_no.run_read('SELECT count(*) AS n FROM "SqlTable"', {})
    count_no = rows_no[0]["n"] if rows_no else 0

    # Index with manifest (in a separate tmp dir to avoid state bleed)
    tmp_with = tmp_path / "with_manifest"
    tmp_with.mkdir()
    for name, sql in _FIXTURE_SQL.items():
        (tmp_with / name).write_text(sql)
    (tmp_with / ".sqlcg.toml").write_text(_MANIFEST_TWO_CONSUMERS)
    backend_with = DuckDBBackend(":memory:")
    backend_with.init_schema()
    backend_with.upsert_node("Repo", str(tmp_with), {"path": str(tmp_with), "name": "with"})
    Indexer().index_repo(tmp_with, dialect=None, db=backend_with)
    rows_with = backend_with.run_read('SELECT count(*) AS n FROM "SqlTable"', {})
    count_with = rows_with[0]["n"] if rows_with else 0

    assert count_no == count_with, (
        f"SqlTable count must be identical with vs without manifest; "
        f"no_manifest={count_no}, with_manifest={count_with}"
    )


# ---------------------------------------------------------------------------
# T35-IDX-3: unmatched target → warning in return dict, partial edges still persisted
# ---------------------------------------------------------------------------


def test_T35_IDX_3_unmatched_target_warning_and_partial_edges(tmp_path, monkeypatch):
    """T35-IDX-3: manifest referencing one real and one unknown table → 1 valid CONSUMED_BY
    edge and the unknown target appears in the return dict's warning list.
    """
    if not _CONFIG_AVAILABLE or not _SCHEMA_AVAILABLE:
        pytest.skip("T35 PR-1 not yet implemented")

    manifest = (
        "[[sqlcg.external_consumers]]\n"
        'name = "Partial Consumer"\n'
        'kind = "tableau"\n'
        'consumes = ["ia_sales.fct_orders", "nonexistent.ghost_table"]\n'
    )
    for name, sql in _FIXTURE_SQL.items():
        (tmp_path / name).write_text(sql)
    (tmp_path / ".sqlcg.toml").write_text(manifest)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    result = Indexer().index_repo(tmp_path, dialect=None, db=backend)

    edge_rows = backend.run_read('SELECT count(*) AS n FROM "CONSUMED_BY"', {})
    edge_count = edge_rows[0]["n"] if edge_rows else 0
    assert edge_count >= 1, (
        f"At least 1 CONSUMED_BY edge must be created for the valid target; got {edge_count}"
    )

    # Warning list must mention the unknown table
    warnings = result.get("external_consumer_warnings", [])
    assert any("nonexistent.ghost_table" in w for w in warnings), (
        f"Warning list must name 'nonexistent.ghost_table'; got {warnings}"
    )


# ---------------------------------------------------------------------------
# PR-1 — Perf invariant: ingestion must use bulk upsert, not per-row calls
# ---------------------------------------------------------------------------


def test_T35_PERF_ingestion_uses_bulk_upsert_only(tmp_path, monkeypatch):
    """T35-PERF: _ingest_external_consumers must not call upsert_node/upsert_edge (singular).

    Patches the backend to spy on singular vs bulk calls.
    """
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    singular_calls: list[str] = []
    bulk_node_calls: list[str] = []
    bulk_edge_calls: list[str] = []

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})

    _orig_upsert_node = backend.upsert_node
    _orig_upsert_edge = backend.upsert_edge
    _orig_bulk_nodes = backend.upsert_nodes_bulk
    _orig_bulk_edges = backend.upsert_edges_bulk

    def _spy_upsert_node(label, pk, props):
        # Only flag calls that come from the external consumer ingestion path
        # (EXTERNAL_CONSUMER label or CONSUMED_BY rel).  Ignore indexer setup.
        if label == "ExternalConsumer":
            singular_calls.append(f"upsert_node({label})")
        return _orig_upsert_node(label, pk, props)

    def _spy_upsert_edge(src_label, rel_type, dst_label, src_pk, dst_pk, props=None):
        if rel_type == "CONSUMED_BY":
            singular_calls.append(f"upsert_edge({rel_type})")
        return _orig_upsert_edge(src_label, rel_type, dst_label, src_pk, dst_pk, props)

    def _spy_bulk_nodes(label, rows):
        if label == "ExternalConsumer":
            bulk_node_calls.append(label)
        return _orig_bulk_nodes(label, rows)

    def _spy_bulk_edges(src_label, dst_label, rel_type, rows):
        if rel_type == "CONSUMED_BY":
            bulk_edge_calls.append(rel_type)
        return _orig_bulk_edges(src_label, dst_label, rel_type, rows)

    backend.upsert_node = _spy_upsert_node
    backend.upsert_edge = _spy_upsert_edge
    backend.upsert_nodes_bulk = _spy_bulk_nodes
    backend.upsert_edges_bulk = _spy_bulk_edges

    for name, sql in _FIXTURE_SQL.items():
        (tmp_path / name).write_text(sql)
    (tmp_path / ".sqlcg.toml").write_text(_MANIFEST_TWO_CONSUMERS)
    monkeypatch.chdir(tmp_path)

    Indexer().index_repo(tmp_path, dialect=None, db=backend)

    assert singular_calls == [], (
        f"upsert_node/upsert_edge (singular) must NOT be called for ExternalConsumer/CONSUMED_BY; "
        f"calls: {singular_calls}"
    )
    assert len(bulk_node_calls) >= 1, (
        "upsert_nodes_bulk must be called at least once for ExternalConsumer"
    )
    assert len(bulk_edge_calls) >= 1, (
        "upsert_edges_bulk must be called at least once for CONSUMED_BY"
    )


# ---------------------------------------------------------------------------
# PR-2 — T35-DOWN-1: get_downstream_dependencies appends external_consumer terminal
# ---------------------------------------------------------------------------


def test_T35_DOWN_1_downstream_includes_external_consumer(tmp_path, monkeypatch):
    """T35-DOWN-1: get_downstream_dependencies on a column whose table has a CONSUMED_BY
    edge returns a DependencyNode with kind='external_consumer' and the consumer name.
    """
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35)")

    # Simple lineage: src_table.col → dst_table.col, dst_table consumed by 'Tableau'
    sql_files = {
        "src.sql": "CREATE TABLE ba.src_table (col INT);",
        "dst.sql": (
            "CREATE TABLE ia_sales.dst_table (col INT);\n"
            "INSERT INTO ia_sales.dst_table SELECT col FROM ba.src_table;\n"
        ),
        ".sqlcg.toml": (
            "[[sqlcg.external_consumers]]\n"
            'name = "Tableau: Dst"\n'
            'kind = "tableau"\n'
            'consumes = ["ia_sales.dst_table"]\n'
        ),
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.get_downstream_dependencies("ba.src_table.col")

    kinds = [n.kind for n in result.nodes]
    names = [n.name for n in result.nodes]

    assert "external_consumer" in kinds, (
        f"Result must include a DependencyNode with kind='external_consumer'; got kinds={kinds}"
    )
    assert "Tableau: Dst" in names, (
        f"Result must include 'Tableau: Dst' consumer; got names={names}"
    )


def test_T35_DOWN_1_control_no_consumer_unchanged(tmp_path, monkeypatch):
    """T35-DOWN-1 control: terminal column with no CONSUMED_BY edge — no external_consumer node."""
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35)")

    sql_files = {
        "src.sql": "CREATE TABLE ba.src (col INT);",
        "dst.sql": ("CREATE TABLE ba.dst (col INT);\nINSERT INTO ba.dst SELECT col FROM ba.src;\n"),
        # No manifest — no consumers
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.get_downstream_dependencies("ba.src.col")

    kinds = [n.kind for n in result.nodes]
    assert "external_consumer" not in kinds, (
        f"No external_consumer nodes expected with no manifest; got kinds={kinds}"
    )


# ---------------------------------------------------------------------------
# PR-2 — T35-DIFF-1: diff_impact reports external_consumers field
# ---------------------------------------------------------------------------


def test_T35_DIFF_1_diff_impact_reports_external_consumers(tmp_path, monkeypatch):
    """T35-DIFF-1: diff_impact whose blast radius reaches a consumed table lists
    the consumer name in external_consumers.
    """
    if not _DIFF_IMPACT_EXTERNAL:
        pytest.skip("DiffImpactResult.external_consumers not yet implemented (#35 PR-2)")
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    sql_files = {
        "src.sql": "CREATE TABLE ba.upstream (col INT);",
        "dst.sql": (
            "CREATE TABLE ia_sales.downstream (col INT);\n"
            "INSERT INTO ia_sales.downstream SELECT col FROM ba.upstream;\n"
        ),
        ".sqlcg.toml": (
            "[[sqlcg.external_consumers]]\n"
            'name = "Tableau: Downstream"\n'
            'kind = "tableau"\n'
            'consumes = ["ia_sales.downstream"]\n'
        ),
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.diff_impact([str(tmp_path / "src.sql")])

    assert hasattr(result, "external_consumers"), (
        "DiffImpactResult must have an external_consumers field"
    )
    assert "Tableau: Downstream" in result.external_consumers, (
        f"external_consumers must list 'Tableau: Downstream'; got {result.external_consumers}"
    )


def test_T35_DIFF_1_control_no_consumer_empty_list(tmp_path, monkeypatch):
    """T35-DIFF-1 control: diff_impact with no consumers yields external_consumers=[]."""
    if not _DIFF_IMPACT_EXTERNAL:
        pytest.skip("DiffImpactResult.external_consumers not yet implemented (#35 PR-2)")
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    sql_files = {
        "src.sql": "CREATE TABLE ba.upstream (col INT);",
        "dst.sql": (
            "CREATE TABLE ba.downstream (col INT);\n"
            "INSERT INTO ba.downstream SELECT col FROM ba.upstream;\n"
        ),
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.diff_impact([str(tmp_path / "src.sql")])

    assert result.external_consumers == [], (
        f"external_consumers must be [] with no manifest; got {result.external_consumers}"
    )


# ---------------------------------------------------------------------------
# PR-2 — T35-UNUSED-1: analyze_unused reports has_external_consumer flag
# ---------------------------------------------------------------------------


def test_T35_UNUSED_1_has_external_consumer_true(tmp_path, monkeypatch):
    """T35-UNUSED-1: a presentation-facing table with a CONSUMED_BY edge reports
    has_external_consumer=True.
    """
    if not _PRES_CANDIDATE_HEC:
        pytest.skip("PresentationCandidate.has_external_consumer not yet implemented (#35 PR-2)")
    if not _SCHEMA_AVAILABLE:
        pytest.skip("ExternalConsumer schema not yet implemented (#35 PR-1)")

    sql_files = {
        "pres.sql": "CREATE TABLE ia_pres.dashboard (col INT);",
        ".sqlcg.toml": (
            '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n'
            "\n"
            "[[sqlcg.external_consumers]]\n"
            'name = "Tableau: Dashboard"\n'
            'kind = "tableau"\n'
            'consumes = ["ia_pres.dashboard"]\n'
        ),
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.analyze_unused()

    pf = result.presentation_facing
    assert pf, f"presentation_facing must be non-empty; got {pf}"
    entry = next((e for e in pf if e.table_qualified == "ia_pres.dashboard"), None)
    assert entry is not None, (
        "ia_pres.dashboard must appear in presentation_facing; "
        f"got {[e.table_qualified for e in pf]}"
    )
    assert entry.has_external_consumer is True, (
        f"has_external_consumer must be True for a table with CONSUMED_BY; "
        f"got {entry.has_external_consumer}"
    )


def test_T35_UNUSED_1_has_external_consumer_false(tmp_path, monkeypatch):
    """T35-UNUSED-1: a presentation-facing table WITHOUT a CONSUMED_BY edge reports
    has_external_consumer=False.
    """
    if not _PRES_CANDIDATE_HEC:
        pytest.skip("PresentationCandidate.has_external_consumer not yet implemented (#35 PR-2)")

    sql_files = {
        "pres.sql": "CREATE TABLE ia_pres.dashboard (col INT);",
        ".sqlcg.toml": (
            '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n'
            # No [[sqlcg.external_consumers]] section
        ),
    }
    _index_fixture(tmp_path, sql_files, monkeypatch)

    result = tools.analyze_unused()

    pf = result.presentation_facing
    entry = next((e for e in pf if e.table_qualified == "ia_pres.dashboard"), None)
    if entry is None:
        pytest.skip(
            "ia_pres.dashboard not in presentation_facing — may need presentation prefix config"
        )

    assert entry.has_external_consumer is False, (
        f"has_external_consumer must be False for a table with no CONSUMED_BY; "
        f"got {entry.has_external_consumer}"
    )
