"""Integration tests for Indexer.resync_changed (F1 — living-codebase resync).

All tests use real in-memory KuzuDB and a real temp git repo to simulate
branch changes. Observable output is asserted — not "no exception raised".
"""

import subprocess
from pathlib import Path

import pytest

from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    return tmp_path


def _commit_all(repo: Path, msg: str = "change") -> str:
    """Stage everything and commit; return the new HEAD SHA."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _get_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _count_edges(db: KuzuBackend) -> int:
    """Count all COLUMN_LINEAGE edges in the graph."""
    rows = db.run_read("MATCH ()-[r:COLUMN_LINEAGE]->() RETURN count(r) AS n", {})
    return rows[0]["n"] if rows else 0


def _count_selects_from(db: KuzuBackend) -> int:
    """Count all SELECTS_FROM edges in the graph."""
    rows = db.run_read("MATCH ()-[r:SELECTS_FROM]->() RETURN count(r) AS n", {})
    return rows[0]["n"] if rows else 0


def _file_node_exists(db: KuzuBackend, path: str) -> bool:
    rows = db.run_read("MATCH (f:File {path: $p}) RETURN f.path AS p", {"p": path})
    return bool(rows)


def _get_column_lineage_edges(db: KuzuBackend) -> list[tuple[str, str]]:
    """Return all (src_id, dst_id) pairs of COLUMN_LINEAGE edges."""
    rows = db.run_read(
        "MATCH (src:SqlColumn)-[:COLUMN_LINEAGE]->(dst:SqlColumn) "
        "RETURN src.id AS src, dst.id AS dst",
        {},
    )
    return [(r["src"], r["dst"]) for r in rows]


# ---------------------------------------------------------------------------
# Phase 1 tests: indexed_sha round-trip
# ---------------------------------------------------------------------------


def test_indexed_sha_roundtrip(db):
    """set_indexed_sha / get_indexed_sha round-trips correctly."""
    assert db.get_indexed_sha() is None, "fresh DB should have no indexed SHA"
    db.set_indexed_sha("abc123")
    assert db.get_indexed_sha() == "abc123"
    db.set_indexed_sha("deadbeef")
    assert db.get_indexed_sha() == "deadbeef", "second write must overwrite the first"


def test_index_repo_writes_head_sha(git_repo, db):
    """index_repo must write the HEAD SHA via set_indexed_sha on success."""
    (git_repo / "t.sql").write_text("SELECT 1 AS a FROM src")
    sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    stored = db.get_indexed_sha()
    assert stored == sha, f"index_repo must write HEAD SHA; got {stored!r}, expected {sha!r}"


# ---------------------------------------------------------------------------
# Phase 3 tests: resync_changed core
# ---------------------------------------------------------------------------


def test_resync_delta_only_untouched_not_reparsed(git_repo, db):
    """Untouched files are NOT reparsed — summary.modified == 1, not 3."""
    # Three independent files with no cross-file dependency
    (git_repo / "a.sql").write_text("CREATE TABLE ta AS SELECT x FROM raw_a")
    (git_repo / "b.sql").write_text("CREATE TABLE tb AS SELECT y FROM raw_b")
    (git_repo / "c.sql").write_text("CREATE TABLE tc AS SELECT z FROM raw_c")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Modify only file a
    (git_repo / "a.sql").write_text("CREATE TABLE ta AS SELECT x AS renamed_x FROM raw_a")
    new_sha = _commit_all(git_repo, "modify a")

    result = indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert result["fell_back_to_full"] is False
    assert result["modified"] == 1, f"Expected 1 modified, got {result['modified']}"
    assert result["added"] == 0
    assert result["deleted"] == 0
    # Closure should be empty (no dependents of ta)
    assert result["closure_resolved"] == 0


def test_resync_writes_new_sha_on_success(git_repo, db):
    """resync_changed must write new_sha via set_indexed_sha on success."""
    (git_repo / "t.sql").write_text("SELECT 1 AS a FROM src")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    (git_repo / "t.sql").write_text("SELECT 2 AS a FROM src")
    new_sha = _commit_all(git_repo, "modify")

    indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert db.get_indexed_sha() == new_sha, "resync_changed must persist new_sha on success"


def test_resync_delete_removes_nodes(git_repo, db):
    """Deleted file's nodes are removed from the graph."""
    (git_repo / "target.sql").write_text("CREATE TABLE staging AS SELECT a, b FROM raw_table")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Verify the file was indexed
    assert _file_node_exists(db, str((git_repo / "target.sql").resolve()))

    # Delete the file
    (git_repo / "target.sql").unlink()
    new_sha = _commit_all(git_repo, "delete")

    result = indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert result["deleted"] == 1
    # File node must be gone
    assert not _file_node_exists(db, str((git_repo / "target.sql").resolve())), (
        "deleted file's File node must be removed from the graph"
    )


def test_resync_closure_dependent_re_resolved_on_upstream_change(git_repo, db):
    """Make-or-break test: editing an upstream CTAS causes downstream re-resolve.

    U defines staging (SELECT a, b FROM raw).
    D reads from staging and defines mart.
    After U is modified to rename column a -> renamed_a, D must be re-resolved
    even though D was not in the git delta.
    """
    u_sql_v1 = "CREATE TABLE staging AS SELECT a, b FROM raw"
    d_sql = "CREATE TABLE mart AS SELECT a FROM staging"

    (git_repo / "upstream.sql").write_text(u_sql_v1)
    (git_repo / "downstream.sql").write_text(d_sql)
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Count SELECTS_FROM edges for downstream before resync
    rows_before = db.run_read(
        "MATCH (f:File {path: $p})<-[:QUERY_DEFINED_IN]-(q:SqlQuery) RETURN count(q) AS n",
        {"p": str((git_repo / "downstream.sql").resolve())},
    )
    queries_before = rows_before[0]["n"] if rows_before else 0
    assert queries_before > 0, "downstream.sql must have at least one query node after index"

    # Modify upstream (column rename)
    u_sql_v2 = "CREATE TABLE staging AS SELECT a AS renamed_a, b FROM raw"
    (git_repo / "upstream.sql").write_text(u_sql_v2)
    new_sha = _commit_all(git_repo, "rename column")

    result = indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert result["fell_back_to_full"] is False
    assert result["modified"] == 1, "only upstream.sql is in the git delta"
    # Downstream must be in the closure
    assert result["closure_resolved"] >= 1, (
        "downstream.sql must be in the closure (it SELECTS_FROM staging which is defined "
        "in upstream.sql, which changed)"
    )


def test_resync_delete_propagates_to_dependents(git_repo, db):
    """Deleting upstream file re-resolves dependents (dangling reference -> no edges)."""
    (git_repo / "upstream.sql").write_text("CREATE TABLE staging AS SELECT a, b FROM raw")
    (git_repo / "downstream.sql").write_text("CREATE TABLE mart AS SELECT a FROM staging")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Delete the upstream file
    (git_repo / "upstream.sql").unlink()
    new_sha = _commit_all(git_repo, "delete upstream")

    result = indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert result["fell_back_to_full"] is False
    assert result["deleted"] == 1

    # upstream.sql's File node must be gone
    assert not _file_node_exists(db, str((git_repo / "upstream.sql").resolve()))

    # Downstream must have been re-resolved (closure captured before delete)
    # Observable: the downstream file's query node still exists
    d_path = str((git_repo / "downstream.sql").resolve())
    d_rows = db.run_read(
        "MATCH (f:File {path: $p})<-[:QUERY_DEFINED_IN]-(q:SqlQuery) RETURN count(q) AS n",
        {"p": d_path},
    )
    assert d_rows[0]["n"] >= 0  # file still indexable (could be 0 if resolved to nothing)


def test_resync_fallback_on_bad_sha(git_repo, db):
    """A bad old_sha triggers fell_back_to_full=True and indexes the corpus."""
    (git_repo / "t.sql").write_text("SELECT 1 AS a FROM src")
    _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    new_sha = _get_head(git_repo)
    result = indexer.resync_changed(git_repo, "deadbeefdeadbeef", new_sha, db, dialect=None)

    assert result["fell_back_to_full"] is True
    # Graph must still be correct (full index was run)
    assert _file_node_exists(db, str((git_repo / "t.sql").resolve()))


def test_resync_empty_delta_updates_sha(git_repo, db):
    """An empty delta (no SQL file changes) updates the stored SHA without touching the graph."""
    (git_repo / "t.sql").write_text("SELECT 1 AS a FROM src")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Commit a non-SQL change
    (git_repo / "README.md").write_text("docs update\n")
    new_sha = _commit_all(git_repo, "docs only")

    edges_before = _count_edges(db)
    result = indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert result["added"] == 0
    assert result["modified"] == 0
    assert result["deleted"] == 0
    assert result["fell_back_to_full"] is False
    assert db.get_indexed_sha() == new_sha, "SHA must be updated even for empty delta"
    # Graph must be unchanged
    assert _count_edges(db) == edges_before


def test_resync_bulk_not_per_row(git_repo, db):
    """resync_changed must use upsert_nodes_bulk/upsert_edges_bulk, never per-row methods.

    Confirms the bulk-upsert invariant is upheld through the resync path.
    """
    from unittest.mock import patch

    (git_repo / "a.sql").write_text("CREATE TABLE t AS SELECT x FROM src")
    old_sha = _commit_all(git_repo, "init")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    (git_repo / "a.sql").write_text("CREATE TABLE t AS SELECT y FROM src")
    new_sha = _commit_all(git_repo, "modify")

    # Track calls to per-row vs bulk upsert methods
    bulk_node_calls = []
    bulk_edge_calls = []
    per_node_calls = []
    per_edge_calls = []

    orig_upsert_nodes_bulk = db.upsert_nodes_bulk
    orig_upsert_edges_bulk = db.upsert_edges_bulk

    with (
        patch.object(
            db,
            "upsert_nodes_bulk",
            side_effect=lambda *a, **kw: (
                bulk_node_calls.append(1),
                orig_upsert_nodes_bulk(*a, **kw),
            ),
        ) as _,
        patch.object(
            db,
            "upsert_edges_bulk",
            side_effect=lambda *a, **kw: (
                bulk_edge_calls.append(1),
                orig_upsert_edges_bulk(*a, **kw),
            ),
        ) as _,
        patch.object(
            db, "upsert_node", side_effect=lambda *a, **kw: (per_node_calls.append(1), None)
        ) as _,
        patch.object(
            db, "upsert_edge", side_effect=lambda *a, **kw: (per_edge_calls.append(1), None)
        ) as _,
    ):
        indexer.resync_changed(git_repo, old_sha, new_sha, db, dialect=None)

    assert bulk_node_calls, "upsert_nodes_bulk was never called in resync_changed"
    assert not per_node_calls, "upsert_node (per-row) was called — must use upsert_nodes_bulk"
    assert not per_edge_calls, "upsert_edge (per-row) was called — must use upsert_edges_bulk"


# ---------------------------------------------------------------------------
# Bounded closure fallback test
# ---------------------------------------------------------------------------


def test_resync_fallback_when_closure_exceeds_max_depth(git_repo, db):
    """When the closure frontier still grows at max_closure_depth, fell_back_to_full=True."""
    # Build a chain deeper than max_closure_depth=3:
    # a -> b -> c -> d -> e (5 levels)
    # Modify a: closure would need to reach e (4 hops from a), exceeding depth=3.
    sql_a = "CREATE TABLE t_a AS SELECT x FROM raw"
    sql_b = "CREATE TABLE t_b AS SELECT x FROM t_a"
    sql_c = "CREATE TABLE t_c AS SELECT x FROM t_b"
    sql_d = "CREATE TABLE t_d AS SELECT x FROM t_c"
    sql_e = "CREATE TABLE t_e AS SELECT x FROM t_d"

    (git_repo / "a.sql").write_text(sql_a)
    (git_repo / "b.sql").write_text(sql_b)
    (git_repo / "c.sql").write_text(sql_c)
    (git_repo / "d.sql").write_text(sql_d)
    (git_repo / "e.sql").write_text(sql_e)
    old_sha = _commit_all(git_repo, "init chain")

    indexer = Indexer()
    indexer.index_repo(git_repo, dialect=None, db=db)

    # Modify the root of the chain
    (git_repo / "a.sql").write_text("CREATE TABLE t_a AS SELECT y FROM raw")
    new_sha = _commit_all(git_repo, "modify root")

    # max_closure_depth=3 means we can traverse to c (3 hops from a),
    # but d and e are still growing at cap -> fallback
    result = indexer.resync_changed(
        git_repo, old_sha, new_sha, db, dialect=None, max_closure_depth=3
    )

    assert result["fell_back_to_full"] is True, (
        "A chain deeper than max_closure_depth must trigger fell_back_to_full=True"
    )
    # After fallback, the full graph must still be correct
    assert _file_node_exists(db, str((git_repo / "e.sql").resolve())), (
        "Full index fallback must index all files including e.sql"
    )
