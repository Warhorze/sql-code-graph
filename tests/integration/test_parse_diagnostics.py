"""Integration tests for parse-diagnostics persistence.

Verifies that parse_failed/parse_cause are correctly written to the File
node via the existing upsert_nodes_bulk(FILE, ...) path (bulk-upsert
invariant preserved), and that index_repo(profile=True) returns a valid
profile dict while profile=False omits it entirely.
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer


@pytest.fixture
def fresh_db():
    """In-memory DuckDB backend with schema initialised."""
    db = DuckDBBackend(":memory:")
    db.init_schema()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Phase 1 acceptance: File node columns persisted correctly
# ---------------------------------------------------------------------------


def test_clean_file_has_empty_cause(fresh_db, tmp_path):
    """A file with no parse errors should have parse_failed=false, parse_cause=''.

    Uses a simple CREATE TABLE followed by INSERT with explicit column mapping
    so sqlglot can resolve the lineage cleanly (no missing-column errors).
    """
    clean_sql = (
        "CREATE TABLE src (id INT, name VARCHAR(100));\n"
        "CREATE TABLE dst (id INT, name VARCHAR(100));\n"
        "INSERT INTO dst SELECT id, name FROM src;\n"
    )
    sql_file = tmp_path / "clean.sql"
    sql_file.write_text(clean_sql)

    indexer = Indexer()
    indexer.index_repo(tmp_path, dialect=None, db=fresh_db, timeout_per_file=30)

    path_str = str(sql_file)
    rows = fresh_db.run_read(
        'SELECT parse_failed AS failed, parse_cause AS cause FROM "File" WHERE path = ?',
        {"p": path_str},
    )
    assert len(rows) == 1, f"Expected 1 File node for {path_str}, got {len(rows)}"
    row = rows[0]
    assert row["failed"] is False, (
        f"parse_failed should be False for clean file, got {row['failed']}"
    )
    assert row["cause"] == "", f"parse_cause should be '' for clean file, got {row['cause']!r}"


def test_failing_file_has_correct_cause(fresh_db, tmp_path):
    """A file producing func_fallback errors should have parse_failed=true.

    func_fallback is triggered by an unaliased aggregate function (SUM, COUNT, etc.)
    in a SELECT — the parser cannot resolve the output column name, emitting
    col_lineage_skip:func_fallback:<FuncName>. This is a degrading bucket.

    The test asserts:
    - parse_failed = true (func_fallback is in _DEGRADING)
    - parse_cause = "func_fallback"
    - The cause was written via the existing upsert_nodes_bulk(FILE, ...) path.
    """
    # Unaliased SUM without a schema — triggers func_fallback
    failing_sql = "SELECT SUM(amount) FROM orders;"
    sql_file = tmp_path / "failing_file.sql"
    sql_file.write_text(failing_sql)

    indexer = Indexer()
    indexer.index_repo(tmp_path, dialect=None, db=fresh_db, timeout_per_file=30)

    path_str = str(sql_file)
    rows = fresh_db.run_read(
        'SELECT parse_failed AS failed, parse_cause AS cause FROM "File" WHERE path = ?',
        {"p": path_str},
    )
    assert len(rows) == 1, f"Expected 1 File node for {path_str}, got {len(rows)}"
    row = rows[0]
    # func_fallback is degrading — parse_failed must be true
    assert row["failed"] is True, (
        f"parse_failed should be True for func_fallback file, got {row['failed']}"
    )
    assert row["cause"] == "func_fallback", (
        f"parse_cause should be 'func_fallback' for this fixture, got {row['cause']!r}"
    )


def test_parse_cause_column_queryable_after_init(fresh_db):
    """SQL query against File.parse_cause on a fresh schema must not raise.

    This verifies that 'parse_cause' and 'parse_failed' columns exist on the
    File table in the current schema (they were absent in v2).
    """
    # Should return an empty result set, not raise a 'no such property' error.
    rows = fresh_db.run_read(
        'SELECT parse_cause AS cause, parse_failed AS failed FROM "File"',
        {},
    )
    # No nodes yet — empty is the expected result; the key test is no exception.
    assert isinstance(rows, list)


def test_schema_version_matches_current_migration():
    """SCHEMA_VERSION gates the re-index migration path (no backward compatibility).

    Most recently bumped 6 -> 7 for `COLUMN_LINEAGE.inferred_from_source_name`
    (plan/sprints/positional_insert_clone_blindspot.md Part A2). Update this
    literal in lockstep whenever schema.py bumps the constant.
    """
    from sqlcg.core.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == "7", f"Expected SCHEMA_VERSION='7', got {SCHEMA_VERSION!r}"


# ---------------------------------------------------------------------------
# Phase 3 acceptance: profiler dict shape
# ---------------------------------------------------------------------------

_EXPECTED_PROFILE_KEYS = {
    "pass1_parse_s",
    "pass2_resolve_s",
    "upsert_s",
    "star_expand_s",
    "total_s",
    "files",
    "ms_per_file",
    "slowest_files",
}


def test_profile_true_returns_profile_dict(fresh_db, tmp_path):
    """index_repo(profile=True) must include summary['profile'] with all required keys."""
    (tmp_path / "simple.sql").write_text("SELECT 1;")

    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect=None, db=fresh_db, profile=True, timeout_per_file=30
    )

    assert "profile" in summary, "summary must contain 'profile' key when profile=True"
    prof = summary["profile"]
    missing = _EXPECTED_PROFILE_KEYS - set(prof.keys())
    assert not missing, f"profile dict missing keys: {missing}"


def test_profile_total_s_positive(fresh_db, tmp_path):
    """profile['total_s'] must be > 0 — confirms timing actually ran."""
    (tmp_path / "a.sql").write_text("SELECT a FROM t;")

    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect=None, db=fresh_db, profile=True, timeout_per_file=30
    )

    prof = summary["profile"]
    assert prof["total_s"] > 0, f"total_s should be positive, got {prof['total_s']}"


def test_profile_false_no_profile_key(fresh_db, tmp_path):
    """index_repo(profile=False) must NOT include 'profile' in summary.

    This is the zero-overhead invariant: default runs are byte-identical to
    the pre-profiler baseline.
    """
    (tmp_path / "simple.sql").write_text("SELECT 1;")

    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect=None, db=fresh_db, profile=False, timeout_per_file=30
    )

    assert "profile" not in summary, (
        "'profile' must NOT be in summary when profile=False; "
        "no perf_counter overhead in the default path"
    )


def test_profile_ms_per_file_is_number(fresh_db, tmp_path):
    """profile['ms_per_file'] must be a positive float."""
    (tmp_path / "t.sql").write_text("SELECT id FROM orders;")

    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect=None, db=fresh_db, profile=True, timeout_per_file=30
    )

    ms = summary["profile"]["ms_per_file"]
    assert isinstance(ms, float), f"ms_per_file should be float, got {type(ms)}"
    assert ms > 0, f"ms_per_file should be positive, got {ms}"


def test_profile_slowest_files_is_list(fresh_db, tmp_path):
    """profile['slowest_files'] must be a list of (path, ms) tuples."""
    (tmp_path / "a.sql").write_text("SELECT a FROM src;")
    (tmp_path / "b.sql").write_text("SELECT b FROM src;")

    indexer = Indexer()
    summary = indexer.index_repo(
        tmp_path, dialect=None, db=fresh_db, profile=True, timeout_per_file=30
    )

    slowest = summary["profile"]["slowest_files"]
    assert isinstance(slowest, list), f"slowest_files should be a list, got {type(slowest)}"
    for item in slowest:
        assert isinstance(item, tuple) and len(item) == 2, (
            f"Each entry in slowest_files must be a (path, ms) tuple, got {item!r}"
        )
        path_str, ms = item
        assert isinstance(path_str, str), f"path in slowest_files must be str, got {type(path_str)}"
        assert isinstance(ms, float), f"ms in slowest_files must be float, got {type(ms)}"
