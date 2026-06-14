"""Integration tests for post-index join-column resolution (Bug #5, PR-5 option b).

Covers the complete data path:
  parser -> JOIN_COL_RESOLVE marker -> indexer -> catalog (information_schema)
  -> resolve_join_columns() SQL pass -> COLUMN_LINEAGE edges.

Each test asserts OBSERVABLE COLUMN_LINEAGE rows in DuckDB, not just "no
exception". Guards the PR-5 section of
[plan/sprints/bugfix_lineage_correctness_validation.md](../../plan/sprints/bugfix_lineage_correctness_validation.md).

The fixtures catalogue the second join table via the information_schema CSV
route (NOT parsed DDL) — the exact path that is unavailable at parse-time
qualify() and that the narrowed PR-A could not fix.
"""

import pytest

from sqlcg.cli.commands.catalog import apply_catalog_to_backend
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

HIGH_CONFIDENCE = 0.9
LOW_CONFIDENCE = 0.5


@pytest.fixture
def temp_db():
    """Fresh in-memory DuckDB with schema initialised."""
    db = DuckDBBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


def _col_lineage(db, transform=None):
    """Return list of (src_key, dst_key, transform, confidence) COLUMN_LINEAGE rows."""
    if transform is not None:
        return db._conn.execute(
            'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE" '
            "WHERE transform = ?",
            [transform],
        ).fetchall()
    return db._conn.execute(
        'SELECT src_key, dst_key, transform, confidence FROM "COLUMN_LINEAGE"'
    ).fetchall()


def _markers(db):
    return db._conn.execute('SELECT src_key, dst_key, bare_col FROM "JOIN_COL_RESOLVE"').fetchall()


def _write_catalog(path, rows):
    """Write an INFORMATION_SCHEMA.COLUMNS-style CSV. rows = [(schema, table, col)]."""
    lines = ["TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME"]
    lines += [f"{s},{t},{c}" for s, t, c in rows]
    path.write_text("\n".join(lines) + "\n")


def _index(repo, db):
    """Full index (no git, no timeout subprocess, single worker for determinism)."""
    return Indexer().index_repo(
        repo, dialect=None, db=db, use_git=False, timeout_per_file=0, n_workers=1
    )


# ---------------------------------------------------------------------------
# Dominant live case — second table catalogued ONLY via information_schema
# ---------------------------------------------------------------------------


@pytest.fixture
def dominant_repo(tmp_path):
    """orders parsed-DDL (amount); customers catalogued via information_schema (status)."""
    (tmp_path / "ddl.sql").write_text("CREATE TABLE sales.orders (amount INT, cid INT);\n")
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT amount, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    csv = tmp_path / "catalog.csv"
    # Note 4: CSV column casing DIFFERS from SQL (STATUS vs status) to guard the
    # case-insensitive owner match. The catalog loader lower-cases, and the
    # resolver normalises both sides, so the match must still succeed.
    _write_catalog(csv, [("sales", "customers", "STATUS"), ("sales", "customers", "id")])
    return tmp_path, csv


def test_resolve_join_columns_information_schema_owner_produces_high_confidence_edge(
    temp_db, dominant_repo
):
    """Dominant live case: second table (information_schema only) gets its edge at 0.9.

    Guards the PR-5 dominant-case acceptance criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    repo, csv = dominant_repo
    _index(repo, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()

    rows = {(s, d): (t, conf) for s, d, t, conf in _col_lineage(temp_db)}
    # Both edges present.
    assert ("sales.orders.amount", "sales.tgt.amount") in rows
    assert ("sales.customers.status", "sales.tgt.status") in rows
    # The information_schema-resolved one-owner edge carries HIGH confidence.
    _t, conf = rows[("sales.customers.status", "sales.tgt.status")]
    assert _t == "JOIN_COL_RESOLVED"
    assert conf == pytest.approx(HIGH_CONFIDENCE)


def test_resolve_join_columns_case_mismatched_catalog_still_matches(temp_db, dominant_repo):
    """Note 4: CSV column casing (STATUS) differs from SQL (status); owner match still fires.

    A naive case-sensitive equality join would drop this edge; the resolver
    normalises casing on both sides. Guards the case-insensitive acceptance
    criterion in [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    repo, csv = dominant_repo
    _index(repo, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()

    edges = _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")
    src_dst = {(s, d) for s, d, _t, _c in edges}
    assert ("sales.customers.status", "sales.tgt.status") in src_dst


def test_resolve_join_columns_no_misbind_survivor(temp_db, dominant_repo):
    """The suppressed sqlglot mis-bind (orders.status) must NOT survive.

    Guards the no-mis-bind-survivor acceptance criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    repo, csv = dominant_repo
    _index(repo, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()

    survivors = temp_db._conn.execute(
        'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE src_key = ?',
        ["sales.orders.status"],
    ).fetchall()
    assert survivors == []


def test_full_index_pipeline_resolves_join_columns_via_configured_catalog(temp_db, tmp_path):
    """The full index_repo pipeline resolves markers when the catalog is configured.

    Models the live config: [sqlcg.catalog] reapplies the information_schema CSV
    during indexing, then the resolver runs. Asserts the second-table edge appears
    end-to-end without a manual catalog/resolve call. Guards PR-5 pipeline wiring
    ([the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md)).
    """
    (tmp_path / "ddl.sql").write_text("CREATE TABLE sales.orders (amount INT, cid INT);\n")
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT amount, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    csv = tmp_path / "catalog.csv"
    _write_catalog(csv, [("sales", "customers", "status")])
    (tmp_path / ".sqlcg.toml").write_text(f'[sqlcg.catalog]\npath = "{csv}"\n')

    result = _index(tmp_path, temp_db)
    assert result.get("join_cols_resolved", 0) >= 1

    src_dst = {(s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")}
    assert ("sales.customers.status", "sales.tgt.status") in src_dst


# ---------------------------------------------------------------------------
# Both sources get edges (split across DDL + information_schema)
# ---------------------------------------------------------------------------


def test_resolve_join_columns_both_sources_get_their_columns(temp_db, tmp_path):
    """A 3-column projection splits across both join tables; each column resolves to its owner.

    orders catalogued via DDL, customers via information_schema. Guards the
    both-sources acceptance criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    (tmp_path / "ddl.sql").write_text("CREATE TABLE sales.orders (amount INT, qty INT, cid INT);\n")
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT amount, qty, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    csv = tmp_path / "catalog.csv"
    _write_catalog(csv, [("sales", "customers", "status"), ("sales", "customers", "id")])

    _index(tmp_path, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()

    src_dst = {(s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")}
    assert ("sales.orders.amount", "sales.tgt.amount") in src_dst
    assert ("sales.orders.qty", "sales.tgt.qty") in src_dst
    assert ("sales.customers.status", "sales.tgt.status") in src_dst


# ---------------------------------------------------------------------------
# Genuine ambiguity → over-attribute, never mis-pick
# ---------------------------------------------------------------------------


def test_resolve_join_columns_ambiguous_column_over_attributes_low_confidence(temp_db, tmp_path):
    """When bare_col exists on BOTH source tables, emit one LOW-confidence edge per owner.

    Never a single confident wrong edge. Guards the over-attribution acceptance
    criterion in [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE sales.orders (status INT, cid INT);\n"
        "CREATE TABLE sales.customers (status INT, id INT);\n"
    )
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    _index(tmp_path, temp_db)
    temp_db.resolve_join_columns()

    edges = _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")
    src_dst = {(s, d): conf for s, d, _t, conf in edges}
    assert ("sales.orders.status", "sales.tgt.status") in src_dst
    assert ("sales.customers.status", "sales.tgt.status") in src_dst
    for conf in src_dst.values():
        assert conf == pytest.approx(LOW_CONFIDENCE)


# ---------------------------------------------------------------------------
# Safe-identifier regression — qualified join is untouched
# ---------------------------------------------------------------------------


def test_qualified_join_emits_no_markers_and_keeps_existing_edges(temp_db, tmp_path):
    """An already-qualified join (o.amount, c.status) emits ZERO markers; edges unchanged.

    Guards the safe-identifier regression criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    (tmp_path / "ddl.sql").write_text(
        "CREATE TABLE sales.orders (amount INT, cid INT);\n"
        "CREATE TABLE sales.customers (status INT, id INT);\n"
    )
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT o.amount, c.status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    _index(tmp_path, temp_db)

    # Zero markers for the fully-qualified projection.
    assert _markers(temp_db) == []

    temp_db.resolve_join_columns()
    # No JOIN_COL_RESOLVED edges (nothing was deferred).
    assert _col_lineage(temp_db, transform="JOIN_COL_RESOLVED") == []
    # The normal sg_lineage edges still resolve each column to its qualified owner.
    src_dst = {(s, d) for s, d, _t, _c in _col_lineage(temp_db)}
    assert ("sales.orders.amount", "sales.tgt.amount") in src_dst
    assert ("sales.customers.status", "sales.tgt.status") in src_dst


# ---------------------------------------------------------------------------
# Degrade — neither source catalogued anywhere (XML-DDL gap)
# ---------------------------------------------------------------------------


def test_resolve_join_columns_uncatalogued_sources_emit_nothing(temp_db, tmp_path):
    """Neither source has columns anywhere → marker resolves to NO edge (honest empty).

    No fabricated first-table edge. Guards the degrade (XML-DDL gap) criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    # No DDL file, no catalog — the join's tables have zero HAS_COLUMN rows.
    (tmp_path / "etl.sql").write_text(
        "INSERT INTO sales.tgt SELECT amount, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    _index(tmp_path, temp_db)
    # Markers exist (the projections are bare and the join has >=2 sources).
    assert len(_markers(temp_db)) >= 1

    temp_db.resolve_join_columns()
    # But no owning columns exist anywhere → no resolved edges, no first-table guess.
    assert _col_lineage(temp_db, transform="JOIN_COL_RESOLVED") == []
    survivors = temp_db._conn.execute(
        'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE src_key = ?',
        ["sales.orders.status"],
    ).fetchall()
    assert survivors == []


# ---------------------------------------------------------------------------
# Incremental reindex regression guard (plan-review BLOCKER)
# ---------------------------------------------------------------------------


def test_reindex_file_preserves_resolved_join_edge(temp_db, dominant_repo):
    """reindex_file re-resolves markers against persisted information_schema rows.

    Full-index WITH the catalog loaded, assert the second-table edge exists; then
    reindex_file the join file (single-file incremental, NO catalog reapply) and
    assert the edge STILL exists. Without re-resolution the incremental path would
    silently drop it. Guards the incremental-reindex BLOCKER criterion in
    [the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md).
    """
    repo, csv = dominant_repo
    _index(repo, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()

    target = ("sales.customers.status", "sales.tgt.status")
    before = {(s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")}
    assert target in before

    # Single-file incremental reindex of the join file — no catalog reapply.
    Indexer().reindex_file(str(repo / "etl.sql"), temp_db, None)

    after = {(s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")}
    assert target in after, (
        "reindex_file dropped the resolved join-column edge — the incremental path "
        "must re-resolve markers against the persisted information_schema HAS_COLUMN rows"
    )


def _git(repo, *args):
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _head(repo):
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_resync_changed_preserves_resolved_join_edge(temp_db, tmp_path):
    """resync_changed re-resolves markers against persisted information_schema rows.

    A resync_changed variant of the incremental-reindex BLOCKER guard: drives the
    git-delta path on a two-commit repo and asserts the second-table edge survives
    the delta even though resync does not reapply the catalog
    ([the plan](../../plan/sprints/bugfix_lineage_correctness_validation.md)).
    """
    repo = tmp_path
    (repo / "ddl.sql").write_text("CREATE TABLE sales.orders (amount INT, cid INT);\n")
    etl = (
        "INSERT INTO sales.tgt SELECT amount, status "
        "FROM sales.orders o JOIN sales.customers c ON o.cid = c.id;\n"
    )
    (repo / "etl.sql").write_text(etl)
    csv = repo / "catalog.csv"
    _write_catalog(csv, [("sales", "customers", "status"), ("sales", "customers", "id")])

    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "ddl.sql", "etl.sql")
    _git(repo, "commit", "-m", "initial")
    old_sha = _head(repo)

    # Full index + catalog so information_schema HAS_COLUMN rows persist.
    _index(repo, temp_db)
    apply_catalog_to_backend(csv, temp_db)
    temp_db.resolve_join_columns()
    target = ("sales.customers.status", "sales.tgt.status")
    assert target in {
        (s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")
    }

    # Touch the join file and commit a new SHA, then resync the delta.
    (repo / "etl.sql").write_text(etl + "-- touched\n")
    _git(repo, "add", "etl.sql")
    _git(repo, "commit", "-m", "touch etl")
    new_sha = _head(repo)

    Indexer().resync_changed(repo, old_sha, new_sha, temp_db, None, timeout_per_file=0)

    after = {(s, d) for s, d, _t, _c in _col_lineage(temp_db, transform="JOIN_COL_RESOLVED")}
    assert target in after
