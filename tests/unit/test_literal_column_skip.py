"""Failing acceptance tests for T-05 (E1 literal-column false-fire suppression).

These tests fail until the developer lands T-05. Named after the ticket so
`pytest -k T-05` works.
"""

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.registry import get_parser


def _parse(sql: str):
    """Parse a single SQL string with SnowflakeParser, return ParsedFile."""
    from pathlib import Path

    schema = SchemaResolver(dialect="snowflake")
    parser = get_parser("snowflake", schema)
    path = Path("test.sql")
    return parser.parse_file(path, sql)


def _col_lineage_errors(parsed):
    """Return all errors that match the E1 col_lineage pattern."""
    return [e for e in parsed.errors if "col_lineage" in e and "Cannot find column" in e]


# ---------------------------------------------------------------------------
# Scenario A — SELECT NULL produces zero E1 errors
# ---------------------------------------------------------------------------


def test_T05_select_null_no_e1_error():
    """INSERT … SELECT NULL must not emit 'Cannot find column NULL' errors.

    Before T-05, the expression NULL (exp.Null) reaches sg_lineage('NULL', ...)
    which raises 'Cannot find column NULL in query' — an E1 false fire.
    After T-05, the literal-skip guard fires and the expression is silently skipped.
    """
    parsed = _parse("INSERT INTO db.s.t (a) SELECT NULL;")
    e1_errors = _col_lineage_errors(parsed)
    assert e1_errors == [], (
        f"Expected zero E1 errors for SELECT NULL, got: {e1_errors}. "
        "T-05 guard 'if not list(col_expr.find_all(exp.Column)): continue' "
        "not yet implemented."
    )


# ---------------------------------------------------------------------------
# Scenario B — SELECT CURRENT_TIMESTAMP() is silently skipped
# ---------------------------------------------------------------------------


def test_T05_select_current_timestamp_no_e1_error():
    """INSERT … SELECT CURRENT_TIMESTAMP() must not emit E1 errors."""
    parsed = _parse("INSERT INTO db.s.t (a) SELECT CURRENT_TIMESTAMP();")
    e1_errors = _col_lineage_errors(parsed)
    assert e1_errors == [], (
        f"Expected zero E1 errors for SELECT CURRENT_TIMESTAMP(), got: {e1_errors}"
    )


# ---------------------------------------------------------------------------
# Scenario C — SELECT 1 + 2 is silently skipped
# ---------------------------------------------------------------------------


def test_T05_select_arithmetic_literal_no_e1_error():
    """INSERT … SELECT 1 + 2 must not emit E1 errors."""
    parsed = _parse("INSERT INTO db.s.t (a) SELECT 1 + 2;")
    e1_errors = _col_lineage_errors(parsed)
    assert e1_errors == [], f"Expected zero E1 errors for SELECT 1 + 2, got: {e1_errors}"


# ---------------------------------------------------------------------------
# Scenario D — Real column expression still resolves (negative regression test)
# ---------------------------------------------------------------------------


def test_T05_real_column_still_produces_lineage_edge():
    """Real column references must NOT be suppressed by the literal-skip guard.

    After T-05, 'if not list(col_expr.find_all(exp.Column)): continue' must only
    fire for expressions with ZERO exp.Column descendants. A real column reference
    (exp.Column) has exactly one such descendant, so the guard must not fire.
    """
    parsed = _parse("INSERT INTO db.s.t (a) SELECT src.col FROM db.s.src;")

    all_lineage_edges = [edge for stmt in parsed.statements for edge in stmt.column_lineage]
    assert len(all_lineage_edges) >= 1, (
        f"Expected >= 1 COLUMN_LINEAGE edge for 'SELECT src.col', got 0. "
        "The T-05 guard must not suppress real column references. "
        f"Errors: {parsed.errors}"
    )


# ---------------------------------------------------------------------------
# Scenario E — Mixed literal + real column: only real column produces edge
# ---------------------------------------------------------------------------


def test_T05_mixed_literal_and_real_column():
    """SELECT NULL, src.col must produce exactly 1 lineage edge (for src.col only)."""
    parsed = _parse("INSERT INTO db.s.t (a, b) SELECT NULL, src.col FROM db.s.src;")

    null_e1_errors = [e for e in parsed.errors if "NULL" in e and "Cannot find column" in e]
    assert null_e1_errors == [], (
        f"NULL literal still produces E1 error after T-05: {null_e1_errors}"
    )

    all_lineage_edges = [edge for stmt in parsed.statements for edge in stmt.column_lineage]
    assert len(all_lineage_edges) >= 1, (
        "SELECT NULL, src.col should produce >= 1 edge (for src.col). "
        "Got 0 — the guard may be too aggressive."
    )


# ---------------------------------------------------------------------------
# Scenario G — Function with column argument is NOT skipped
# ---------------------------------------------------------------------------


def test_T05_function_with_column_arg_not_skipped():
    """COALESCE(src.col, 0) must not be skipped — it contains a real exp.Column."""
    parsed = _parse("INSERT INTO db.s.t (a) SELECT COALESCE(src.col, 0) FROM db.s.src;")

    all_lineage_edges = [edge for stmt in parsed.statements for edge in stmt.column_lineage]
    assert len(all_lineage_edges) >= 1, (
        "COALESCE(src.col, 0) has a real Column descendant and must NOT be skipped. "
        "Expected >= 1 lineage edge, got 0. "
        f"Errors: {parsed.errors}"
    )
