"""Integration tests for the sprint-10 anchor tools (find_definition,
get_change_scope, get_backfill_order, diff_impact, scope_change).

Each test builds a real in-memory KuzuDB graph by indexing tiny SQL fixtures,
wires the module-level tools backend to it, and asserts on observable tool
output (specific names, counts, labels) — never just "no exception raised".
"""

import sqlcg.server.tools as tools
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def _index_fixture(tmp_path, files: dict[str, str], monkeypatch) -> KuzuBackend:
    """Write the given {filename: sql} fixtures, index them into a fresh
    in-memory graph, and wire it as the tools backend.

    chdir into tmp_path so NoiseFilter / presentation config resolve from a
    clean root (default patterns, no stray repo .sqlcg.toml).
    """
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)

    backend = KuzuBackend(":memory:")
    backend.init_schema()
    # _assert_indexed() requires a Repo node; the Indexer alone does not create one.
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend


# --------------------------------------------------------------------------
# PR-03 — find_definition
# --------------------------------------------------------------------------


def test_find_definition_single_authoritative(tmp_path, monkeypatch):
    """Scenario A — a single, non-backup definition is authoritative."""
    _index_fixture(
        tmp_path,
        {"dim_date.sql": "CREATE TABLE ba.dim_date (id INT);"},
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date")

    assert len(result.definitions) == 1, f"expected 1 definition, got {result.definitions}"
    assert result.definitions[0].is_authoritative is True
    assert result.definitions[0].is_backup is False
    assert result.duplicate_ddl is False
    assert result.definitions[0].file_path.endswith("dim_date.sql")


def test_find_definition_backup_flagged(tmp_path, monkeypatch):
    """Scenario B — a backup-named table is flagged is_backup, not hidden."""
    _index_fixture(
        tmp_path,
        {
            "dim_date.sql": "CREATE TABLE ba.dim_date (id INT);",
            "dim_date_bck.sql": "CREATE TABLE ba.dim_date_bck (id INT);",
        },
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date_bck")

    assert len(result.definitions) == 1
    assert result.definitions[0].is_backup is True, (
        "ba.dim_date_bck must match the default *_bck backup pattern"
    )
    assert result.definitions[0].is_authoritative is False
    assert any(p.endswith("dim_date_bck.sql") for p in result.noise_excluded)


def test_find_definition_duplicate_ddl(tmp_path, monkeypatch):
    """Scenario C — same table defined in two files is flagged duplicate_ddl."""
    _index_fixture(
        tmp_path,
        {
            "a.sql": "CREATE TABLE ba.dim_date (id INT);",
            "b.sql": "CREATE TABLE ba.dim_date (id INT, extra INT);",
        },
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date")

    assert result.duplicate_ddl is True
    assert len(result.definitions) == 2, f"expected 2 definitions, got {result.definitions}"


def test_find_definition_not_indexed(tmp_path, monkeypatch):
    """Scenario D — unknown table returns empty definitions and a hint."""
    _index_fixture(
        tmp_path,
        {"dim_date.sql": "CREATE TABLE ba.dim_date (id INT);"},
        monkeypatch,
    )

    result = tools.find_definition("ba.nonexistent_table")

    assert result.definitions == []
    assert result.hint is not None and len(result.hint) > 0


# --------------------------------------------------------------------------
# PR-04 — get_change_scope
# --------------------------------------------------------------------------


def test_change_scope_terminal_is_safe(tmp_path, monkeypatch):
    """Scenario A — a terminal view with no consumers is 'safe'."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source_table (id INT);",
            "terminal.sql": "CREATE VIEW ba.terminal_view AS SELECT id FROM ba.source_table;",
        },
        monkeypatch,
    )

    result = tools.get_change_scope("ba.terminal_view")

    assert result.risk_label == "safe", f"expected safe, got {result.risk_label}"
    assert len(result.defining_files) == 1
    assert len(result.upstream_tables) >= 1
    assert "ba.source_table" in result.upstream_tables


def test_change_scope_source_with_downstream(tmp_path, monkeypatch):
    """Scenario B — a source table with a downstream chain has a non-safe risk."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "etl.sql": "CREATE TABLE ba.etl AS SELECT id FROM ba.source;",
            "mart.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.etl;",
        },
        monkeypatch,
    )

    result = tools.get_change_scope("ba.source")

    assert result.risk_label in ("low", "medium", "high")
    assert len(result.affected_tables) >= 1
    assert any("etl" in t for t in result.affected_tables), (
        f"expected an etl table in affected_tables, got {result.affected_tables}"
    )


def test_change_scope_backup_excluded(tmp_path, monkeypatch):
    """Scenario C — backup tables are excluded from affected and reported."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "bck.sql": "CREATE TABLE ba.source_bck AS SELECT id FROM ba.source;",
            "mart.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.source_bck;",
        },
        monkeypatch,
    )

    result = tools.get_change_scope("ba.source")

    assert all("_bck" not in t for t in result.affected_tables), (
        f"backup tables must not appear in affected_tables: {result.affected_tables}"
    )
    assert any("source_bck" in t for t in result.noise_excluded), (
        f"source_bck must be reported in noise_excluded: {result.noise_excluded}"
    )


def test_risk_label_thresholds():
    """Scenario D — risk label thresholds (pure function, no graph)."""
    assert tools._risk_label(0) == "safe"
    assert tools._risk_label(3) == "low"
    assert tools._risk_label(15) == "medium"
    assert tools._risk_label(25) == "high"


# --------------------------------------------------------------------------
# PR-05 — get_backfill_order + diff_impact
# --------------------------------------------------------------------------


def test_backfill_order_topological(tmp_path, monkeypatch):
    """Scenario A — staged is rebuilt before mart."""
    _index_fixture(
        tmp_path,
        {
            "raw.sql": "CREATE TABLE ba.raw (id INT);",
            "staged.sql": "CREATE TABLE ba.staged AS SELECT id FROM ba.raw;",
            "mart.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.staged;",
        },
        monkeypatch,
    )

    result = tools.get_backfill_order("ba.raw")

    assert "ba.staged" in result.backfill_order
    assert "ba.mart" in result.backfill_order
    assert result.backfill_order.index("ba.staged") < result.backfill_order.index("ba.mart"), (
        f"staged must precede mart in rebuild order: {result.backfill_order}"
    )


def test_backfill_order_cycle_degrades(tmp_path, monkeypatch):
    """Scenario B — a dependency cycle degrades gracefully with a 'cycle' hint."""
    # b derives its column from a (unambiguous lineage a.id -> b.id), but selects
    # from both a and c at the table level, while c selects from b — so b and c
    # form a SELECTS_FROM cycle that the topological sort must handle gracefully.
    _index_fixture(
        tmp_path,
        {
            "a.sql": "CREATE TABLE ba.a (id INT);",
            "b.sql": (
                "CREATE VIEW ba.b AS "
                "SELECT a.id AS id FROM ba.a AS a JOIN ba.c AS c ON a.id = c.id;"
            ),
            "c.sql": "CREATE VIEW ba.c AS SELECT id FROM ba.b;",
        },
        monkeypatch,
    )

    result = tools.get_backfill_order("ba.a")

    assert len(result.backfill_order) >= 1, "order must be non-empty even with a cycle"
    assert result.hint is not None and "cycle" in result.hint.lower()


def test_diff_impact_file_to_blast_radius(tmp_path, monkeypatch):
    """Scenario C — changed file path resolves to a downstream blast radius."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source_table (id INT);",
            "etl.sql": "CREATE TABLE ba.etl_table AS SELECT id FROM ba.source_table;",
        },
        monkeypatch,
    )

    result = tools.diff_impact([str(tmp_path / "source.sql")])

    assert "ba.source_table" in result.changed_tables
    assert "ba.etl_table" in result.affected_tables


def test_diff_impact_presentation_configured(tmp_path, monkeypatch):
    """Scenario D (configured) — presentation_facing reflects configured prefixes."""
    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "view.sql": "CREATE VIEW ia_analytics.ba_view AS SELECT id FROM ba.source;",
        },
        monkeypatch,
    )

    result = tools.diff_impact([str(tmp_path / "source.sql")])

    assert "ia_analytics.ba_view" in result.presentation_facing, (
        f"configured ia_ prefix must flag the view: {result.presentation_facing}"
    )


def test_diff_impact_presentation_off_by_default(tmp_path, monkeypatch):
    """Scenario D (unconfigured) — presentation_facing is empty with no config."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "view.sql": "CREATE VIEW ia_analytics.ba_view AS SELECT id FROM ba.source;",
        },
        monkeypatch,
    )

    result = tools.diff_impact([str(tmp_path / "source.sql")])

    assert result.presentation_facing == [], (
        "presentation_facing must be empty when no prefix is configured (no hardcoded ia_)"
    )


def test_backfill_order_excludes_noise(tmp_path, monkeypatch):
    """Scenario E — backup tables excluded from backfill order, reported as noise."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "bck.sql": "CREATE TABLE ba.source_bck AS SELECT id FROM ba.source;",
            "mart.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.source_bck;",
        },
        monkeypatch,
    )

    result = tools.get_backfill_order("ba.source")

    assert "ba.source_bck" not in result.backfill_order
    assert any("source_bck" in t for t in result.noise_excluded)


# --------------------------------------------------------------------------
# PR-06 — scope_change (synthesis)
# --------------------------------------------------------------------------


def test_scope_change_full_synthesis(tmp_path, monkeypatch):
    """Scenario A — one call assembles definition, scope, and backfill order."""
    _index_fixture(
        tmp_path,
        {
            "raw.sql": "CREATE TABLE ba.raw (id INT);",
            "staged.sql": "CREATE TABLE ba.staged AS SELECT id FROM ba.raw;",
            "mart.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.staged;",
        },
        monkeypatch,
    )

    result = tools.scope_change("ba.raw")

    assert len(result.authoritative_files) >= 1
    assert result.authoritative_files[0].endswith("raw.sql")
    assert "ba.staged" in result.downstream_blast_radius
    assert result.risk_label in ("low", "medium", "high")
    assert len(result.backfill_order) >= 1
    assert result.truncated is False


def test_scope_change_noise_excluded(tmp_path, monkeypatch):
    """Scenario B — backups are excluded from the blast radius and reported."""
    _index_fixture(
        tmp_path,
        {
            "raw.sql": "CREATE TABLE ba.raw (id INT);",
            "staged.sql": "CREATE TABLE ba.staged AS SELECT id FROM ba.raw;",
            "bck.sql": "CREATE TABLE ba.staged_bck AS SELECT id FROM ba.raw;",
        },
        monkeypatch,
    )

    result = tools.scope_change("ba.raw")

    assert all("_bck" not in t for t in result.downstream_blast_radius)
    assert len(result.noise_excluded) >= 1


def test_scope_change_undefined_table(tmp_path, monkeypatch):
    """Scenario C — an undefined target returns empty sets, safe risk, a hint."""
    _index_fixture(
        tmp_path,
        {"raw.sql": "CREATE TABLE ba.raw (id INT);"},
        monkeypatch,
    )

    result = tools.scope_change("ba.does_not_exist")

    assert result.authoritative_files == []
    assert result.downstream_blast_radius == []
    assert result.risk_label == "safe"
    assert result.hint is not None and len(result.hint) > 0
