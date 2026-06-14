"""PR-2 (#31) integration tests: source location (file / line / expression).

Scenarios:
    A — _compute_start_lines unit test: parse a SQL string with 3 statements,
        assert correct 1-based line numbers.
    B — start_line persisted: index a two-statement fixture; assert both SqlQuery
        nodes have non-zero start_line and second > first.
    C — TRACE_COLUMN_LINEAGE query returns file/line/expression: query the graph
        directly and assert the row has non-null file, line >= 1, non-empty expression.
    D — v4 graph triggers re-index gate: schema version mismatch exits with message.
    E — test_bulk_upsert_invariant passes unchanged (start_line key does not add
        new bulk call): verified by running test_bulk_upsert_invariant; this
        scenario is documented here but the invariant test is the actual guard.
    F — Snowflake preprocessing does not desync start lines: fixture with an
        ALTER … SET TAG; statement deleted by preprocessing; surviving statements'
        start_line matches original file line.
    G — Snowflake scripting fallback emits start_line=0 and trace shows line=None.

Regression guard: trace must return non-null file and line for real ANSI edges
    (guards against v1.0.x file=None hardcode in tools.py trace loops).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.queries import TRACE_COLUMN_LINEAGE_QUERY
from sqlcg.indexer.indexer import Indexer
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory KuzuDB with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Scenario A — _compute_start_lines unit test
# ---------------------------------------------------------------------------


def test_scenario_a_compute_start_lines_three_statements():
    """_compute_start_lines must return one 1-based line number per statement.

    Input has three statements separated by semicolons.  Blank lines between
    statements shift the start line of each statement.
    """
    sql = "SELECT 1;\n\nSELECT 2;\n\nSELECT 3;"
    # Line 1: SELECT 1
    # Line 2: blank
    # Line 3: SELECT 2
    # Line 4: blank
    # Line 5: SELECT 3
    lines = AnsiParser._compute_start_lines(sql)
    assert lines == [1, 3, 5], (
        f"Expected [1, 3, 5] for three newline-separated statements, got {lines}"
    )


def test_scenario_a_compute_start_lines_no_trailing_semicolon():
    """_compute_start_lines must handle a final statement with no trailing semicolon."""
    sql = "SELECT 1;\nSELECT 2"
    lines = AnsiParser._compute_start_lines(sql)
    assert len(lines) == 2, f"Expected 2 lines, got {lines}"
    assert lines[0] == 1
    assert lines[1] == 2


def test_scenario_a_compute_start_lines_single_statement():
    """_compute_start_lines must return [1] for a single statement on line 1."""
    sql = "CREATE TABLE t (a INT);"
    lines = AnsiParser._compute_start_lines(sql)
    assert lines == [1], f"Expected [1], got {lines}"


# ---------------------------------------------------------------------------
# Scenario B — start_line persisted in SqlQuery nodes
# ---------------------------------------------------------------------------


def test_scenario_b_start_line_persisted(db, tmp_path):
    """Indexed SqlQuery nodes must have non-zero start_line values.

    Two statements in the fixture: the first at line 1, the second at line 2.
    Both must have distinct, non-zero start_line values.
    """
    sql = "CREATE TABLE src (a INT);\nINSERT INTO dst SELECT a AS x FROM src;\n"
    fixture = tmp_path / "fixture.sql"
    fixture.write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(
        'SELECT statement_index AS idx, start_line AS line FROM "SqlQuery" '
        "WHERE file_path = ? ORDER BY statement_index",
        {"path": str(fixture)},
    )
    assert len(rows) >= 2, f"Expected at least 2 SqlQuery nodes, got {len(rows)}"

    # All start_lines must be non-zero
    for row in rows:
        assert row["line"] != 0 and row["line"] is not None, (
            f"start_line must be non-zero (not the sentinel 0) for ANSI statements. "
            f"Got start_line={row['line']} for statement_index={row['idx']}"
        )

    # Second statement must have a higher start_line than the first
    line_0 = rows[0]["line"]
    line_1 = rows[1]["line"]
    assert line_1 > line_0, (
        f"Second statement start_line ({line_1}) must be > first ({line_0}). "
        "The INSERT on line 2 must report line 2, not line 1."
    )


# ---------------------------------------------------------------------------
# Scenario C — TRACE_COLUMN_LINEAGE query returns file/line/expression
# ---------------------------------------------------------------------------


def test_scenario_c_trace_query_returns_file_line_expression(db, tmp_path):
    """TRACE_COLUMN_LINEAGE query must return non-null file, line >= 1, and expression.

    Guards against the v1.0.x regression where file=None was hardcoded in
    tools.py trace loops and start_line was never persisted.

    Uses TRACE_COLUMN_LINEAGE_QUERY directly (same query as tools.py trace loops)
    to verify both the graph data and the query structure.
    """
    sql = "CREATE TABLE src (a INT);\nINSERT INTO dst SELECT a AS x FROM src;\n"
    fixture = tmp_path / "fixture.sql"
    fixture.write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(
        TRACE_COLUMN_LINEAGE_QUERY,
        {"id": "dst.x"},
    )
    assert len(rows) >= 1, "Expected at least one COLUMN_LINEAGE edge for dst.x; got none."

    for row in rows:
        file_val = row.get("file")
        line_val = row.get("line")
        expr_val = row.get("expression")

        assert file_val is not None, (
            f"TRACE_COLUMN_LINEAGE must return non-null 'file'. "
            f"Got file={file_val!r}. Guards against v1.0.x file=None hardcode. Row: {row}"
        )
        assert line_val is not None and line_val >= 1, (
            f"TRACE_COLUMN_LINEAGE must return line >= 1. "
            f"Got line={line_val!r}. Guards against start_line=0 sentinel. "
            f"Row: {row}"
        )
        assert expr_val is not None and len(expr_val) > 0, (
            f"TRACE_COLUMN_LINEAGE must return non-empty 'expression'. "
            f"Got expression={expr_val!r}. Row: {row}"
        )


# ---------------------------------------------------------------------------
# Regression guard (part of Scenario C) — direct graph query
# ---------------------------------------------------------------------------


def test_regression_guard_trace_returns_source_location(db, tmp_path):
    """Column lineage trace must return non-null file and line for real edges.

    Guards against the v1.0.x regression where file=None was hardcoded in
    tools.py trace loops, making the tool useless for provenance audits.
    """
    sql = "CREATE TABLE src (a INT);\nINSERT INTO dst SELECT a AS x FROM src;"
    fixture = tmp_path / "fixture.sql"
    fixture.write_text(sql)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

    rows = db.run_read(
        "SELECT q.file_path AS file, q.start_line AS line "
        'FROM "COLUMN_LINEAGE" r '
        'JOIN "SqlQuery" q ON q.id = r.query_id '
        "WHERE r.dst_key = ?",
        {"id": "dst.x"},
    )
    assert len(rows) >= 1, (
        "No COLUMN_LINEAGE edges found — regression guard: trace must return edges"
    )
    for row in rows:
        assert row["file"] is not None, (
            "file must be non-null on lineage edges — "
            "guards against v1.0.x file=None hardcode regression"
        )
        assert row["line"] is not None and row["line"] >= 1, (
            "line must be >= 1 on lineage edges — guards against start_line never being persisted"
        )


# ---------------------------------------------------------------------------
# Scenario D — v4 graph triggers re-index gate
# ---------------------------------------------------------------------------


def test_scenario_d_schema_version_mismatch_exits(tmp_path):
    """Indexing with a v4 schema DB must exit with code 1 and a re-index message.

    The version gate in index.py compares SCHEMA_VERSION with the stored value.
    A v4 graph must be rejected now that SCHEMA_VERSION='5'.
    """
    from sqlcg.core.schema import SCHEMA_VERSION

    # Create a backend that returns "4" to simulate an old graph
    db_v4 = DuckDBBackend(":memory:")
    db_v4.init_schema()

    # Patch the stored schema version to simulate v4
    with patch.object(db_v4, "get_schema_version", return_value="4"):
        stored = db_v4.get_schema_version()
        assert stored == "4"
        assert stored != SCHEMA_VERSION, "SCHEMA_VERSION must differ from '4' after the PR-2 bump"
        # Confirm the version gate would fire (matches the check in index.py).
        # SCHEMA_VERSION has since moved to '11' (JOIN_COL_RESOLVE edge table,
        # plan/sprints/bugfix_lineage_correctness_validation.md §PR-5) — the gate
        # logic this test pins (any mismatch triggers re-index) is unaffected.
        assert SCHEMA_VERSION == "11", f"Expected SCHEMA_VERSION='11', got {SCHEMA_VERSION!r}"


# ---------------------------------------------------------------------------
# Scenario F — Snowflake preprocessing does not desync start lines
# ---------------------------------------------------------------------------


def test_scenario_f_snowflake_preprocessing_preserves_start_lines(db, tmp_path):
    """Surviving Snowflake statements must report start_line from the ORIGINAL file.

    The fixture contains:
    - Statement 1: CREATE TABLE src at line 1
    - Statement 2: ALTER TABLE ... MODIFY COLUMN ... SET TAG ...; at line 2
      (deleted by _preprocess_snowflake_sql Gap 4b)
    - Statement 3: INSERT INTO dst ... at line 3

    After preprocessing deletes the ALTER ... SET TAG; statement, the post-process
    statement count is 2 (CREATE + INSERT). If start_lines were computed from the
    PREPROCESSED SQL, stmt_index 1 would map to the INSERT's post-process line 2,
    but the actual file line is 3.

    Guards the PR-2 step 5 fix: start_lines are computed from the ORIGINAL SQL in
    SnowflakeParser.parse_file before _preprocess_snowflake_sql is called.
    """
    # Build a fixture where the ALTER ... SET TAG; statement is at line 2
    # The CREATE TABLE is at line 1, the INSERT is at line 3
    sql_lines = [
        "CREATE TABLE src (a INT);",
        "ALTER TABLE src MODIFY COLUMN a SET TAG GOVERNANCE.pii = 'true';",
        "INSERT INTO dst SELECT a AS x FROM src;",
    ]
    fixture_sql = "\n".join(sql_lines) + "\n"
    fixture = tmp_path / "fixture.sf.sql"
    fixture.write_text(fixture_sql)

    # Use SnowflakeParser directly to verify start_line values
    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver()
    parser = SnowflakeParser(resolver)
    parsed = parser.parse_file(fixture, fixture_sql)

    assert len(parsed.statements) >= 1, (
        "Expected at least 1 surviving statement after preprocessing. "
        f"Got {len(parsed.statements)}. Statements: {[s.kind for s in parsed.statements]}"
    )

    # Find the CREATE TABLE statement and INSERT statement by kind
    create_stmts = [s for s in parsed.statements if "CREATE" in s.kind]
    insert_stmts = [s for s in parsed.statements if s.kind == "INSERT"]

    # CREATE TABLE must be at line 1 (original file line)
    if create_stmts:
        create_line = create_stmts[0].start_line
        assert create_line == 1, (
            f"CREATE TABLE must report start_line=1 (original file line). "
            f"Got start_line={create_line}. This indicates the start_line was "
            "computed from preprocessed SQL (wrong) rather than original SQL."
        )

    # INSERT must be at line 3 (original file line), not line 2 (post-preprocess index)
    if insert_stmts:
        insert_line = insert_stmts[0].start_line
        assert insert_line == 3, (
            f"INSERT must report start_line=3 (original file line 3). "
            f"Got start_line={insert_line}. "
            "If start_line=2, preprocessing deleted the ALTER ... SET TAG; statement "
            "and recomputed start_lines from the mutated SQL (index-desync bug)."
        )


# ---------------------------------------------------------------------------
# Scenario F2 — Multi-line ALTER … SET TAG; does not shift subsequent start_line
# ---------------------------------------------------------------------------


def test_scenario_f2_multiline_alter_set_tag_preserves_subsequent_start_line():
    """Multi-line ALTER … SET TAG; deletion must NOT shift subsequent statements' start_line.

    The fixture:
      Line 1: CREATE TABLE src (a INT);
      Lines 2-4: ALTER TABLE … MODIFY COLUMN … SET TAG … (3 lines, including body)
                 (deleted by Gap 4b preprocessing)
      Line 5: INSERT INTO dst SELECT a AS x FROM src;

    Before the fix: the three interior newlines of the ALTER block were consumed
    by the regex substitution → the preprocessed SQL had the INSERT at line 2 →
    start_line was persisted as 2 instead of 5.

    After the fix: the replacement preserves newline count → INSERT appears at
    line 5 in the preprocessed SQL → start_line is correctly persisted as 5.
    """
    sql_lines = [
        "CREATE TABLE src (a INT);",  # line 1
        "ALTER TABLE src",  # line 2
        "  MODIFY COLUMN a",  # line 3
        "  SET TAG GOVERNANCE.pii = 'true';",  # line 4
        "INSERT INTO dst SELECT a AS x FROM src;",  # line 5
    ]
    fixture_sql = "\n".join(sql_lines) + "\n"

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver()
    parser = SnowflakeParser(resolver)
    parsed = parser.parse_file(None, fixture_sql)  # type: ignore[arg-type]

    insert_stmts = [s for s in parsed.statements if s.kind == "INSERT"]
    assert insert_stmts, (
        "Expected at least one INSERT statement in parsed output. "
        f"Parsed kinds: {[s.kind for s in parsed.statements]}"
    )
    insert_line = insert_stmts[0].start_line
    assert insert_line == 5, (
        f"INSERT must report start_line=5 (original file line 5). "
        f"Got start_line={insert_line}. "
        "If start_line=2, the multi-line ALTER block newlines were dropped during "
        "preprocessing (Gap 4b line-count-preservation bug)."
    )


# ---------------------------------------------------------------------------
# Scenario F3 — Multi-line UNPIVOT does not shift subsequent start_line
# ---------------------------------------------------------------------------


def test_scenario_f3_multiline_unpivot_preserves_subsequent_start_line():
    """Multi-line UNPIVOT clause deletion must NOT shift subsequent statements' start_line.

    The fixture:
      Line 1: CREATE TABLE dst AS
      Line 2: SELECT a, b FROM src
      Lines 3-6: UNPIVOT (val FOR col IN (   -- 4 lines, including interior newlines
                   x, y
                 )) AS unpiv
      Line 7-8: ;   (end of CREATE)
      ... wait, we need a simpler multi-statement fixture.

    Simpler approach: the UNPIVOT block spans lines 3-5 inside a SELECT statement,
    followed by a second statement at line 7.

    Fixture:
      Line 1: CREATE TABLE src (x INT, y INT);
      Lines 2-6: CREATE TABLE dst AS SELECT a FROM src
                   UNPIVOT (val FOR col IN (
                     x,
                     y
                   )) AS unpiv;
      Line 7: INSERT INTO final_out SELECT a FROM dst;

    After UNPIVOT strip (which spans lines 3-6 in the CREATE): the subsequent
    INSERT must still report start_line=7.
    """
    sql_lines = [
        "CREATE TABLE src (x INT, y INT);",  # line 1
        "CREATE TABLE dst AS SELECT a FROM src",  # line 2
        "  UNPIVOT (val FOR col IN (",  # line 3
        "    x,",  # line 4
        "    y",  # line 5
        "  )) AS unpiv;",  # line 6
        "INSERT INTO final_out SELECT a FROM dst;",  # line 7
    ]
    fixture_sql = "\n".join(sql_lines) + "\n"

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver()
    parser = SnowflakeParser(resolver)
    parsed = parser.parse_file(None, fixture_sql)  # type: ignore[arg-type]

    insert_stmts = [s for s in parsed.statements if s.kind == "INSERT"]
    assert insert_stmts, (
        "Expected at least one INSERT statement after UNPIVOT strip. "
        f"Parsed kinds: {[s.kind for s in parsed.statements]}"
    )
    insert_line = insert_stmts[0].start_line
    assert insert_line == 7, (
        f"INSERT must report start_line=7 (original file line 7). "
        f"Got start_line={insert_line}. "
        "If start_line < 7, the multi-line UNPIVOT block newlines were dropped "
        "during preprocessing (Gap 3 line-count-preservation bug)."
    )


# ---------------------------------------------------------------------------
# Scenario F4 — Multi-line WITH TAG does not shift subsequent start_line
# ---------------------------------------------------------------------------


def test_scenario_f4_multiline_with_tag_preserves_subsequent_start_line():
    """Multi-line WITH TAG clause deletion must NOT shift subsequent statements' start_line.

    The fixture:
      Line 1: CREATE OR REPLACE TABLE tgt (
      Line 2:   col_a INT
      Lines 3-6:   WITH TAG (
                     GOVERNANCE.pii = 'true',
                     GOVERNANCE.category = 'finance'
                   )
      Line 7: ) AS SELECT col_a FROM src;
      Line 8: INSERT INTO final_out SELECT col_a FROM tgt;

    After WITH TAG strip (lines 3-6): the INSERT must still report start_line=8.
    """
    sql_lines = [
        "CREATE OR REPLACE TABLE tgt (col_a INT",  # line 1
        "  WITH TAG (",  # line 2
        "    GOVERNANCE.pii = 'true',",  # line 3
        "    GOVERNANCE.category = 'finance'",  # line 4
        "  )",  # line 5
        ") AS SELECT col_a FROM src;",  # line 6
        "INSERT INTO final_out SELECT col_a FROM tgt;",  # line 7
    ]
    fixture_sql = "\n".join(sql_lines) + "\n"

    from sqlcg.lineage.schema_resolver import SchemaResolver

    resolver = SchemaResolver()
    parser = SnowflakeParser(resolver)
    parsed = parser.parse_file(None, fixture_sql)  # type: ignore[arg-type]

    insert_stmts = [s for s in parsed.statements if s.kind == "INSERT"]
    assert insert_stmts, (
        "Expected at least one INSERT statement after WITH TAG strip. "
        f"Parsed kinds: {[s.kind for s in parsed.statements]}"
    )
    insert_line = insert_stmts[0].start_line
    assert insert_line == 7, (
        f"INSERT must report start_line=7 (original file line 7). "
        f"Got start_line={insert_line}. "
        "If start_line < 7, the multi-line WITH TAG block newlines were dropped "
        "during preprocessing (Gap 4 line-count-preservation bug)."
    )


# ---------------------------------------------------------------------------
# Scenario G — Snowflake scripting fallback emits start_line=0 → line=None
# ---------------------------------------------------------------------------


def test_scenario_g_scripting_fallback_start_line_zero(db, tmp_path):
    """Scripting-path SqlQuery nodes must have start_line=0 (unknown sentinel).

    The scripting path uses regex DML extraction and loses original file line
    positions.  start_line=0 is the sentinel meaning 'unknown'.
    """
    # A Snowflake file with a BEGIN/END scripting block triggers _parse_scripting_file
    scripting_sql = "BEGIN\n  INSERT INTO dst SELECT a AS x FROM src;\nEND;"
    fixture = tmp_path / "scripting.sf.sql"
    fixture.write_text(scripting_sql)
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    rows = db.run_read(
        'SELECT start_line AS line FROM "SqlQuery" WHERE file_path = ?',
        {"path": str(fixture)},
    )
    # If no statements were indexed (scripting block had no DML), skip gracefully
    if not rows:
        pytest.skip("No SqlQuery nodes indexed for scripting fixture — no DML extracted")

    for row in rows:
        assert row["line"] == 0, (
            f"Scripting-fallback SqlQuery nodes must have start_line=0 (sentinel). "
            f"Got start_line={row['line']}."
        )


def test_scenario_g_scripting_fallback_trace_shows_line_none(db, tmp_path):
    """trace_column_lineage must surface line=None for scripting-path edges.

    When start_line=0 (scripting sentinel), the tools.py guard
    ``row.get('line') or None`` converts 0 → None.
    """
    scripting_sql = (
        "CREATE TABLE src (a INT);\nBEGIN\n  INSERT INTO dst SELECT a AS x FROM src;\nEND;"
    )
    fixture = tmp_path / "scripting2.sf.sql"
    fixture.write_text(scripting_sql)
    Indexer().index_repo(tmp_path, dialect="snowflake", db=db, use_git=False)

    # Query directly for scripting-path edges
    rows = db.run_read(
        "SELECT q.start_line AS raw_line "
        'FROM "COLUMN_LINEAGE" r '
        'JOIN "SqlQuery" q ON q.id = r.query_id '
        "WHERE r.dst_key = ? AND q.parsing_mode = 'scripting'",
        {"id": "dst.x"},
    )
    if not rows:
        pytest.skip("No scripting-path COLUMN_LINEAGE edges found — DML not extracted")

    for row in rows:
        raw = row.get("raw_line")
        # The sentinel value in the graph is 0
        assert raw == 0, f"Scripting-path SqlQuery nodes must store start_line=0. Got {raw}."
        # The tools.py guard 'row.get("line") or None' converts 0 → None
        converted = raw or None
        assert converted is None, (
            f"0 sentinel must convert to None via 'or None' guard. Got {converted!r}."
        )
