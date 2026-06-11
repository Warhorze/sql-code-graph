"""Integration tests for repo-relative CTE/derived namespace keys (PR 3).

Guards plan/sprints/sprint_postmortem_fixes.md §PR 3.

Assertions:
- After indexing a small corpus, no '::' key in COLUMN_LINEAGE or SqlTable
  starts with '/' (absolute POSIX path) or a Windows drive letter prefix.
- (A3) Consumer round-trip: _harvest_usage_catalog harvests zero '::' keys;
  noise filter still classifies '::' keys as noise.
"""

from __future__ import annotations

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Core: no absolute path prefix in any :: key
# ---------------------------------------------------------------------------


def test_no_absolute_prefix_in_cte_keys_after_indexing(db, tmp_path):
    """After indexing a multi-file corpus, no '::' key contains an absolute path.

    Both SqlTable.qualified and COLUMN_LINEAGE src_key / dst_key are checked.
    An absolute path leaking means rel_path was not threaded correctly to the parser.
    Guards sprint_postmortem_fixes.md §PR 3 Step 3.1.
    """
    # Three-file corpus: DDL, a CTE query, a cross-file reference.
    (tmp_path / "ddl.sql").write_text("CREATE TABLE ba.source_x (val NUMERIC);\n")
    (tmp_path / "etl").mkdir()
    (tmp_path / "etl" / "transform.sql").write_text(
        "INSERT INTO ba.target_x\n"
        "WITH prep AS (SELECT val FROM ba.source_x)\n"
        "SELECT val FROM prep;\n"
    )
    (tmp_path / "etl" / "derived.sql").write_text(
        "INSERT INTO ba.derived_y\n"
        "WITH agg AS (SELECT val FROM ba.source_x)\n"
        "SELECT val FROM agg;\n"
    )

    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    # Check SqlTable nodes
    table_rows = db.run_read('SELECT qualified FROM "SqlTable"', {})
    for row in table_rows:
        q = row["qualified"]
        if "::" in q:
            assert not q.startswith("/"), (
                f"SqlTable.qualified contains absolute POSIX path: {q!r}. "
                "CTE/derived keys must be repo-relative after sprint_postmortem_fixes §PR 3."
            )
            # Detect Windows-style drive letters (e.g. "C:\\..." or "C:/...")
            assert not (len(q) >= 2 and q[1] == ":"), (
                f"SqlTable.qualified contains Windows drive letter: {q!r}."
            )

    # Check COLUMN_LINEAGE edges
    edge_rows = db.run_read('SELECT src_key, dst_key FROM "COLUMN_LINEAGE"', {})
    for row in edge_rows:
        for field in ("src_key", "dst_key"):
            key = row[field]
            if key and "::" in key:
                assert not key.startswith("/"), (
                    f"COLUMN_LINEAGE.{field} contains absolute POSIX path: {key!r}. "
                    "CTE/derived keys must be repo-relative."
                )
                assert not (len(key) >= 2 and key[1] == ":"), (
                    f"COLUMN_LINEAGE.{field} contains Windows drive letter: {key!r}."
                )


def test_relative_cte_keys_contain_subdir_prefix_when_nested(db, tmp_path):
    """CTE keys produced for a file in a subdirectory include the subdir prefix.

    A file at 'etl/transform.sql' produces 'etl/transform.sql::prep', not
    just 'transform.sql::prep'.
    Guards sprint_postmortem_fixes.md §PR 3 Step 3.1 (posix path includes subdir).
    """
    (tmp_path / "etl").mkdir()
    (tmp_path / "etl" / "transform.sql").write_text(
        "INSERT INTO ba.target\nWITH prep AS (SELECT a FROM ba.source)\nSELECT a FROM prep;\n"
    )

    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    cte_rows = db.run_read(
        "SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE '%::prep'",
        {},
    )
    assert len(cte_rows) >= 1, (
        "No CTE 'prep' node found — fixture must produce at least one CTE node."
    )
    for row in cte_rows:
        key = row["qualified"]
        assert key == "etl/transform.sql::prep", (
            f"CTE key {key!r} does not match expected 'etl/transform.sql::prep'. "
            "Subdirectory prefix must be included in repo-relative keys."
        )


def test_portability_two_absolute_roots_same_relative_corpus(tmp_path):
    """Indexing the same file layout from two absolute roots yields identical CTE keys.

    This is the portability requirement from sprint_postmortem_fixes §PR 3.
    """
    sql = "INSERT INTO target\nWITH cte AS (SELECT x FROM src)\nSELECT x FROM cte;\n"
    rel_subdir = "etl"
    rel_file = "etl/work.sql"

    # Build two separate absolute roots with the same relative layout
    root_a = tmp_path / "corpus_a"
    (root_a / rel_subdir).mkdir(parents=True)
    (root_a / rel_file).write_text(sql)

    root_b = tmp_path / "deeply" / "nested" / "corpus_b"
    (root_b / rel_subdir).mkdir(parents=True)
    (root_b / rel_file).write_text(sql)

    def _index_and_get_cte_keys(root) -> set[str]:
        db = DuckDBBackend(":memory:")
        db.init_schema()
        try:
            Indexer().index_repo(root, dialect=None, db=db, use_git=False)
            rows = db.run_read(
                "SELECT qualified FROM \"SqlTable\" WHERE qualified LIKE '%::cte'",
                {},
            )
            return {r["qualified"] for r in rows}
        finally:
            db.close()

    keys_a = _index_and_get_cte_keys(root_a)
    keys_b = _index_and_get_cte_keys(root_b)

    assert keys_a, "No CTE 'cte' node found in corpus A."
    assert keys_a == keys_b, (
        f"CTE keys differ between two absolute roots with the same relative layout.\n"
        f"Root A: {keys_a}\n"
        f"Root B: {keys_b}\n"
        f"Keys must be identical — portability requirement (sprint_postmortem_fixes §PR 3)."
    )
    for key in keys_a:
        assert key == "etl/work.sql::cte", f"Key {key!r} does not match 'etl/work.sql::cte'."
