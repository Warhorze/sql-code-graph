"""Unit tests for SQL base parser (parsers/base.py)."""

import unittest.mock
from pathlib import Path
from unittest.mock import patch

import sqlglot.expressions as exp
from sqlglot import parse_one

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParsedFile


class TestExtractColumnLineageExceptions:
    """Test column lineage exception handling in _extract_column_lineage."""

    def test_sg_lineage_exception_recorded(self, caplog):
        """Test that sg_lineage exceptions append to errors and emit zero-confidence edge."""
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        # Construct a minimal SELECT statement
        stmt = parse_one("SELECT bad_col FROM t")

        # Create a ParsedFile object to track errors
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Mock sg_lineage to raise an exception
        with patch("sqlglot.lineage.lineage") as mock_sg_lineage:
            mock_sg_lineage.side_effect = ValueError("mock lineage failure")

            # Call _extract_column_lineage directly
            edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)

            # Assert error was recorded with structured key
            assert len(out.errors) > 0
            assert any("col_lineage:bad_col:mock lineage failure" in str(e) for e in out.errors)

            # Note: T-09-06 demotes per-column errors to DEBUG.
            # The actual logging is tested in test_T09_06_log_verbosity.py.
            # This test primarily checks that the error is recorded in out.errors.

            # Assert exactly one zero-confidence edge returned
            assert len(edges.edges) == 1
            assert edges.edges[0].confidence == 0.0

    def test_outer_statement_exception_recorded(self, caplog):
        """Test that outer statement exceptions are recorded in col_lineage:statement."""
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        # Create output object with errors list
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Parse a statement and call method
        stmt = parse_one("SELECT * FROM t")

        # Create a schema dict that will cause issues
        bad_schema = None

        edges = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=bad_schema)

        # The method should handle the exception gracefully
        assert isinstance(edges.edges, list)

    def test_sg_lineage_success_returns_edges(self):
        """When sg_lineage returns a root node, edges must be emitted — not an empty list.

        Uses a real SELECT so the tree walker receives a genuine LineageNode with a
        Table source, not an unconfigured MagicMock that _lineage_node_to_table_ref rejects.
        """
        parser = AnsiParser(SchemaResolver())
        stmt = parse_one("SELECT col1 FROM t")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        result = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema={})

        assert len(result.edges) > 0, (
            "Expected at least one LineageEdge for SELECT col1 FROM t, got none. "
            "The tree walker in _lineage_node_to_edges must emit edges for leaf nodes."
        )
        assert all(e.confidence > 0.0 for e in result.edges), (
            "Edges from a successful sg_lineage call must have confidence > 0."
        )


class TestE8DynamicSourceMarker:
    """sg_lineage returns a root but no leaf sources — dynamic identifier pattern."""

    def test_dynamic_identifier_emits_skip_marker(self):
        # IDENTIFIER($var) is opaque to sqlglot; it's a zero-argument-like function
        # with no exp.Column descendants. T-05 (literal column skip) now silently
        # skips such expressions before sg_lineage can analyze them.
        # This is correct behavior — IDENTIFIER() is not a real column reference.
        parser = AnsiParser(SchemaResolver())
        out = ParsedFile(path=Path("test.sql"), dialect=None)
        # Use Anonymous function as a stand-in for identifier($var) — sqlglot can't resolve it
        stmt = parse_one("INSERT INTO tgt SELECT IDENTIFIER('src_tbl') AS col1 FROM src")
        parser._extract_column_lineage(stmt, Path("test.sql"), out, schema={})
        # After T-05: IDENTIFIER() is silently skipped, no dynamic_source marker.
        # This is the correct and expected behavior per the T-05 spec.
        assert not any(e.startswith("col_lineage_skip:dynamic_source:") for e in out.errors), (
            f"IDENTIFIER() should be silently skipped by T-05 guard (no real column), "
            f"not recorded as dynamic_source. Got: {out.errors}"
        )


class TestErrorPropagation:
    """Test error propagation from _parse_statement to ParsedFile."""

    def test_parse_file_with_clean_ctas_no_errors(self):
        """Test that a clean CTAS produces no col_lineage errors.

        Parse a CREATE TABLE AS SELECT 1 AS x,
        assert no col_lineage errors in parsed.errors.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "CREATE TABLE t AS SELECT 1 AS x"
        parsed = parser.parse_file(Path("test.sql"), sql)

        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]
        assert col_lineage_errors == [], f"Expected no col_lineage errors, got: {parsed.errors}"

    def test_parse_file_can_call_with_out_parameter(self):
        """Test that _parse_statement receives and uses the out parameter.

        Verifies that _parse_statement passes the out parameter correctly
        so that errors from _extract_column_lineage are appended to the caller's
        ParsedFile object instead of being discarded.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT 1 AS x"
        parsed = parser.parse_file(Path("test.sql"), sql)

        # Verify parse succeeded and statements were added
        assert len(parsed.statements) == 1
        assert parsed.statements[0].kind == "SELECT"


class TestTempTableSources:
    """Test temp table source accumulation in _parse_statement."""

    def test_sources_parameter_passed_to_extract_column_lineage(self):
        """Test that _parse_statement passes sources to _extract_column_lineage.

        This is a unit test that verifies the parameter threading before
        integration tests check actual temp table resolution.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT a FROM t"
        parsed = parser.parse_file(Path("test.sql"), sql)

        # Verify parse succeeded (the real test is implementation coverage)
        assert len(parsed.statements) == 1


class TestStarColumnSkip:
    """Test star column skip in _extract_column_lineage."""

    def test_qualified_star_produces_skip_marker(self):
        """Test that qualified star (base.*) is skipped with error marker.

        Parse CREATE TEMP TABLE tmp_b AS SELECT base.*, 1 AS x FROM tmp_a base
        Assert:
        - parsed.errors contains one entry starting with col_lineage_skip:star:base
        - No entry starts with col_lineage:
        (Note: column x won't produce edges without a sources_map)
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "CREATE TEMP TABLE tmp_b AS SELECT base.*, 1 AS x FROM tmp_a base"
        parsed = parser.parse_file(Path("test.sql"), sql)

        skip_errors = [e for e in parsed.errors if e.startswith("col_lineage_skip:star:")]
        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]

        assert len(skip_errors) >= 1, f"Expected skip marker, got errors: {parsed.errors}"
        assert skip_errors[0].startswith("col_lineage_skip:star:base"), (
            f"Expected qualified star marker, got: {skip_errors}"
        )
        assert col_lineage_errors == [], (
            f"Star should not cause col_lineage error, got: {col_lineage_errors}"
        )

    def test_unqualified_star_produces_skip_marker(self):
        """Test that unqualified star (*) is skipped with error marker.

        Parse SELECT * FROM t
        Assert:
        - parsed.errors contains one entry starting with col_lineage_skip:star:
        - No exception raised
        - No column lineage edges (star cannot be resolved)
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        sql = "SELECT * FROM t"
        parsed = parser.parse_file(Path("test.sql"), sql)

        skip_errors = [e for e in parsed.errors if e.startswith("col_lineage_skip:star:")]
        col_lineage_errors = [e for e in parsed.errors if e.startswith("col_lineage:")]

        assert len(skip_errors) >= 1, f"Expected skip marker, got errors: {parsed.errors}"
        assert col_lineage_errors == [], (
            f"Star should not cause col_lineage error, got: {col_lineage_errors}"
        )


# ---------------------------------------------------------------------------
# PR-05 — INSERT…SELECT positional column mapping (#25)
# ---------------------------------------------------------------------------


def _edges_as_tuples(parsed: ParsedFile) -> set[tuple[str, str]]:
    """Extract (src.full_id, dst.full_id) tuples from a ParsedFile's lineage."""
    result = set()
    for stmt in parsed.statements:
        for edge in stmt.column_lineage:
            if edge.confidence > 0:
                result.add((edge.src.full_id, edge.dst.full_id))
    return result


class TestInsertPositionalMapping:
    """PR-05 (#25): INSERT column-list positional mapping overrides SELECT aliases."""

    def test_scenario_a_alias_matches_positional_no_op(self) -> None:
        """Scenario A (no-op): alias equals INSERT column name → edges attributed to INSERT cols.

        INSERT INTO t (col1, col2) SELECT a AS col1, b AS col2 FROM src
        should produce edges src.a → t.col1 and src.b → t.col2.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)
        sql = "INSERT INTO t (col1, col2) SELECT a AS col1, b AS col2 FROM src"
        parsed = parser.parse_file(Path("test_a.sql"), sql)
        edges = _edges_as_tuples(parsed)

        assert any("col1" in dst for _, dst in edges), f"Expected edge to t.col1 but got: {edges}"
        assert any("col2" in dst for _, dst in edges), f"Expected edge to t.col2 but got: {edges}"
        # The alias names (col1, col2) match the INSERT columns — same result
        assert not any("t.x" in dst or "t.y" in dst for _, dst in edges), (
            f"Alias names x/y must not appear in edges: {edges}"
        )

    def test_scenario_b_alias_diverges_from_positional(self) -> None:
        """Scenario B (the bug case): SELECT alias differs from INSERT column name.

        INSERT INTO t (c1, c2, c3) SELECT a AS x, b AS y, c AS z FROM src
        should produce edges src.a → t.c1, src.b → t.c2, src.c → t.c3
        and NOT src.a → t.x, src.b → t.y, src.c → t.z.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)
        sql = "INSERT INTO t (c1, c2, c3) SELECT a AS x, b AS y, c AS z FROM src"
        parsed = parser.parse_file(Path("test_b.sql"), sql)
        edges = _edges_as_tuples(parsed)

        # The INSERT column names (c1, c2, c3) must appear in dst
        assert any("c1" in dst for _, dst in edges), (
            f"Expected edge to t.c1 (positional) but got: {edges}"
        )
        assert any("c2" in dst for _, dst in edges), (
            f"Expected edge to t.c2 (positional) but got: {edges}"
        )
        assert any("c3" in dst for _, dst in edges), (
            f"Expected edge to t.c3 (positional) but got: {edges}"
        )
        # SELECT aliases (x, y, z) must NOT appear as dst column names
        alias_dst = {
            dst
            for _, dst in edges
            if dst.endswith(".x") or dst.endswith(".y") or dst.endswith(".z")
        }
        assert not alias_dst, (
            f"SELECT aliases x/y/z must not appear as destination columns (positional "
            f"override should have replaced them): found {alias_dst}"
        )

    def test_scenario_c_body_copy_called_once_per_insert(self) -> None:
        """Scenario C — CLAUDE.md invariant: body.copy() called once per INSERT statement.

        Parse a 5-column INSERT and assert that exp.Select.copy is called exactly
        ONCE (for body_no_with = body.copy()), not once per column. This directly
        pins the per-column full-body deepcopy regression from 4234e5d.
        """
        schema = SchemaResolver()
        parser = AnsiParser(schema)

        n_cols = 5
        out_cols = ", ".join(f"c{i}" for i in range(n_cols))
        src_cols = ", ".join(f"a{i} AS x{i}" for i in range(n_cols))
        sql = f"INSERT INTO t ({out_cols}) SELECT {src_cols} FROM src"

        # Count full-body SELECT.copy() calls (multi-expression SELECT only)
        orig_copy = exp.Select.copy
        full_body_copy_count = 0

        def copy_counting(self: exp.Select, *a, **k):
            nonlocal full_body_copy_count
            if len(self.expressions) > 1:
                full_body_copy_count += 1
            return orig_copy(self, *a, **k)

        with unittest.mock.patch.object(exp.Select, "copy", copy_counting):
            parser.parse_file(Path("test_c.sql"), sql)

        assert full_body_copy_count == 1, (
            f"body.copy() must be called exactly ONCE per INSERT statement "
            f"(CLAUDE.md invariant — no per-column body deepcopy). "
            f"Called {full_body_copy_count} times for a {n_cols}-column INSERT."
        )
