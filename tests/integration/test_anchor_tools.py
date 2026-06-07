"""Integration tests for the sprint-10 anchor tools (find_definition,
get_change_scope, get_backfill_order, diff_impact, scope_change).

Each test builds a real in-memory KuzuDB graph by indexing tiny SQL fixtures,
wires the module-level tools backend to it, and asserts on observable tool
output (specific names, counts, labels) — never just "no exception raised".
"""

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


def _index_fixture(tmp_path, files: dict[str, str], monkeypatch) -> DuckDBBackend:
    """Write the given {filename: sql} fixtures, index them into a fresh
    in-memory graph, and wire it as the tools backend.

    chdir into tmp_path so NoiseFilter / presentation config resolve from a
    clean root (default patterns, no stray repo .sqlcg.toml).
    """
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)

    backend = DuckDBBackend(":memory:")
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

    assert result.risk.label == "safe", f"expected safe, got {result.risk.label}"
    assert result.risk.assertion_type == "heuristic"
    assert result.downstream_count == len(result.affected_tables)
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

    assert result.risk.label in ("low", "medium", "high")
    assert result.risk.assertion_type == "heuristic"
    assert result.downstream_count == len(result.affected_tables)
    assert str(result.downstream_count) in result.risk.reason, (
        f"downstream_count {result.downstream_count} must appear in reason: {result.risk.reason}"
    )
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


def test_change_scope_risk_is_fact_grounded(tmp_path, monkeypatch):
    """Scenario E — risk Judgement carries a fact-grounded reason citing the count."""
    _index_fixture(
        tmp_path,
        {
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "etl.sql": "CREATE TABLE ba.etl AS SELECT id FROM ba.source;",
        },
        monkeypatch,
    )

    result = tools.get_change_scope("ba.source")

    assert result.risk.assertion_type == "heuristic"
    assert result.risk.label in ("safe", "low", "medium", "high")
    assert result.risk.confidence == 0.6
    assert result.risk.reason is not None
    assert str(result.downstream_count) in result.risk.reason, (
        f"downstream_count {result.downstream_count} must appear in reason: {result.risk.reason}"
    )
    assert result.downstream_count == len(result.affected_tables)


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
    """Scenario B — a dependency cycle degrades gracefully with a 'cycle' hint.

    Issue #38 (Option A): Kahn adjacency is now derived from the COLUMN_LINEAGE
    closure, not SELECTS_FROM — a table-reference cycle that carries no genuine
    *column*-level cycle (e.g. b's column derives unambiguously from a while b
    merely also references c at the table level) is no longer mistaken for a
    cycle; that was an artifact of the old SELECTS_FROM-based adjacency and is
    now correctly resolved into a causal a -> b -> c chain (see
    test_backfill_order_topological_via_column_lineage_chain). To keep the
    cycle-degradation contract covered, this fixture constructs a genuine
    *column*-level cycle reachable from the target: b.id derives from a.id
    (so it is in a's downstream closure) AND from c.id, while c.id derives
    from b.id — mutual recursion between b and c — which COLUMN_LINEAGE
    adjacency must still detect and degrade gracefully.
    """
    _index_fixture(
        tmp_path,
        {
            "ddl.sql": (
                "CREATE TABLE ba.a (id INT);CREATE TABLE ba.b (id INT);CREATE TABLE ba.c (id INT);"
            ),
            "b1.sql": "INSERT INTO ba.b (id) SELECT a.id AS id FROM ba.a AS a;",
            "b2.sql": "INSERT INTO ba.b (id) SELECT c.id AS id FROM ba.c AS c;",
            "c.sql": "INSERT INTO ba.c (id) SELECT b.id AS id FROM ba.b AS b;",
        },
        monkeypatch,
    )

    result = tools.get_backfill_order("ba.a")

    assert len(result.backfill_order) >= 1, "order must be non-empty even with a cycle"
    assert result.hint is not None and "cycle" in result.hint.lower()


def test_backfill_order_topological_via_column_lineage_chain(tmp_path, monkeypatch):
    """Issue #38 (Option A) — a table-reference cycle with no genuine column-level
    cycle is resolved into the correct causal chain, not the cycle fallback.

    b's column derives unambiguously from a (a.id -> b.id); b also references c
    at the table level (a SELECTS_FROM-level "cycle" the old adjacency saw), but
    there is no column-level cycle: c.id derives from b.id only. COLUMN_LINEAGE
    adjacency correctly resolves this to the causal chain a -> b -> c — no cycle
    hint, deterministic producer-before-consumer order.
    """
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

    assert result.hint is None or "cycle" not in result.hint.lower(), (
        f"no genuine column-level cycle exists; must not degrade: {result.hint}"
    )
    assert "ba.b" in result.backfill_order
    assert "ba.c" in result.backfill_order
    assert result.backfill_order.index("ba.b") < result.backfill_order.index("ba.c"), (
        f"b must precede c in rebuild order: {result.backfill_order}"
    )


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


def test_diff_impact_producer_file_blast_radius(tmp_path, monkeypatch):
    """ETL producer files (INSERT...SELECT) populate a table without a DEFINED_IN
    edge — that edge is DDL-only. diff_impact must still resolve the populated
    table from QUERY_DEFINED_IN -> SqlQuery.target_table, not just DEFINED_IN, or
    the blast radius is silently empty for the tool's primary CI use case.

    Three SEPARATE files: ddl.sql defines ba.dim (DDL only, DEFINED_IN edge),
    source.sql defines ba.source (DDL only), producer.sql POPULATES ba.dim via
    INSERT...SELECT (QUERY_DEFINED_IN + target_table, no DEFINED_IN), and
    consumer.sql is downstream of ba.dim.
    """
    _index_fixture(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE ba.dim (id INT);",
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "producer.sql": "INSERT INTO ba.dim SELECT id FROM ba.source;",
            "consumer.sql": "CREATE TABLE ba.mart AS SELECT id FROM ba.dim;",
        },
        monkeypatch,
    )

    result = tools.diff_impact([str(tmp_path / "producer.sql")])

    assert "ba.dim" in result.changed_tables, (
        f"producer file must resolve its populated table via QUERY_DEFINED_IN: "
        f"{result.changed_tables}"
    )
    assert "ba.mart" in result.affected_tables, (
        f"downstream consumer of the populated table must be in the blast radius: "
        f"{result.affected_tables}"
    )
    assert result.hint is None, (
        f"a real producer file must not fall through to the 'no tables defined' hint: {result.hint}"
    )


def test_get_definition_and_change_scope_surface_producer_files(tmp_path, monkeypatch):
    """Output-side half of #58 (reverse of the diff_impact producer-file fix above):
    given a TABLE, get_definition / get_change_scope must surface the ETL producer
    file (INSERT...SELECT, via QUERY_DEFINED_IN -> SqlQuery.target_table), not just
    the DDL file (via DEFINED_IN) — the producer file is the more useful answer to
    "where do I change this table's logic?".

    Separate ddl.sql (DEFINED_IN only) and producer.sql (QUERY_DEFINED_IN +
    target_table, no DEFINED_IN) — mirrors the input-side fixture pattern.
    """
    _index_fixture(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE ba.dim (id INT);",
            "source.sql": "CREATE TABLE ba.source (id INT);",
            "producer.sql": "INSERT INTO ba.dim SELECT id FROM ba.source;",
        },
        monkeypatch,
    )

    ddl_path = str(tmp_path / "ddl.sql")
    producer_path = str(tmp_path / "producer.sql")

    scope = tools.get_change_scope("ba.dim")
    assert ddl_path in scope.defining_files, (
        f"DDL file must remain in defining_files: {scope.defining_files}"
    )
    assert producer_path in scope.defining_files, (
        f"ETL producer file must be unioned into defining_files via QUERY_DEFINED_IN: "
        f"{scope.defining_files}"
    )

    definition = tools.find_definition("ba.dim")
    assert producer_path in definition.producer_files, (
        f"get_definition must surface the producer file: {definition.producer_files}"
    )
    assert any(d.file_path == ddl_path for d in definition.definitions), (
        f"DDL definition must remain present: {definition.definitions}"
    )


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
    assert result.risk.label in ("low", "medium", "high")
    assert result.risk.assertion_type == "heuristic"
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
    assert result.risk.label == "safe"
    assert result.risk.assertion_type == "heuristic"
    assert result.hint is not None and len(result.hint) > 0


# --------------------------------------------------------------------------
# Trust Layer — analyze_unused
# --------------------------------------------------------------------------


def test_analyze_unused_fact_heuristic_separation(tmp_path, monkeypatch):
    """Scenario A — used table absent, orphan table present as heuristic candidate."""
    _index_fixture(
        tmp_path,
        {
            "producer.sql": (
                "CREATE TABLE ba.used (id INT);\nCREATE VIEW ba.consumer AS SELECT id FROM ba.used;"
            ),
            "orphan.sql": "CREATE TABLE ba.orphan (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    candidate_names = [c.table_qualified for c in result.candidates]
    assert "ba.orphan" in candidate_names, (
        f"ba.orphan must be a dead-code candidate; got {candidate_names}"
    )
    assert "ba.used" not in candidate_names, (
        f"ba.used must not be a candidate (it is consumed); got {candidate_names}"
    )

    orphan = next(c for c in result.candidates if c.table_qualified == "ba.orphan")
    assert orphan.within_corpus_references == 0
    assert orphan.dead_code.assertion_type == "heuristic"
    assert orphan.dead_code.confidence == 0.5
    assert orphan.dead_code.reason is not None and len(orphan.dead_code.reason) > 0

    assert result.total_tables_scanned >= 2


def test_analyze_unused_backup_excluded(tmp_path, monkeypatch):
    """Scenario B — backup tables are not reported as dead-code candidates."""
    _index_fixture(
        tmp_path,
        {
            "orphan.sql": "CREATE TABLE ba.orphan (id INT);",
            "bck.sql": "CREATE TABLE ba.orphan_bck (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    candidate_names = [c.table_qualified for c in result.candidates]
    assert all("_bck" not in n for n in candidate_names), (
        f"backup tables must not appear in candidates: {candidate_names}"
    )
    assert "ba.orphan" in candidate_names


# --------------------------------------------------------------------------
# Trust Layer — get_hub_ranking
# --------------------------------------------------------------------------


def test_hub_ranking_exact_fact(tmp_path, monkeypatch):
    """Scenario A — hub table with 3 consumers ranks first with exact count."""
    _index_fixture(
        tmp_path,
        {
            "hub.sql": "CREATE TABLE ba.hub (id INT);",
            "c1.sql": "CREATE TABLE ba.c1 AS SELECT id FROM ba.hub;",
            "c2.sql": "CREATE TABLE ba.c2 AS SELECT id FROM ba.hub;",
            "c3.sql": "CREATE TABLE ba.c3 AS SELECT id FROM ba.hub;",
            "lonely.sql": "CREATE TABLE ba.lonely (id INT);",
            "lc1.sql": "CREATE TABLE ba.lc1 AS SELECT id FROM ba.lonely;",
        },
        monkeypatch,
    )

    result = tools.get_hub_ranking(k=10)

    assert len(result.top) >= 1, "ranking must be non-empty"
    top_entry = result.top[0]
    assert top_entry.table_qualified == "ba.hub", (
        f"ba.hub (3 consumers) must rank first; got {top_entry.table_qualified}"
    )
    assert top_entry.downstream_dependents == 3, (
        f"expected 3 distinct consumers for ba.hub; got {top_entry.downstream_dependents}"
    )
    assert top_entry.rank == 1

    # HubEntry must carry no Judgement/confidence/reason attribute.
    assert not hasattr(top_entry, "dead_code")
    assert not hasattr(top_entry, "confidence")
    assert not hasattr(top_entry, "reason")
    assert not hasattr(top_entry, "assertion_type")


def test_hub_ranking_k_cap_and_backup_excluded(tmp_path, monkeypatch):
    """Scenario B — k caps results; backup tables never appear."""
    _index_fixture(
        tmp_path,
        {
            "hub.sql": "CREATE TABLE ba.hub (id INT);",
            "c1.sql": "CREATE TABLE ba.c1 AS SELECT id FROM ba.hub;",
            "hub_bck.sql": "CREATE TABLE ba.hub_bck (id INT);",
            "bc1.sql": "CREATE TABLE ba.bc1 AS SELECT id FROM ba.hub_bck;",
        },
        monkeypatch,
    )

    result = tools.get_hub_ranking(k=1)

    assert len(result.top) <= 1
    assert all("_bck" not in e.table_qualified for e in result.top), (
        f"backup tables must not appear in hub ranking: {[e.table_qualified for e in result.top]}"
    )
