"""Unit tests for transform-kind classification on lineage edges.

Guards PR 4 of the graph-health sprint
([plan doc](plan/sprints/graph_health_catalog_and_metrics.md §6)).

Each test asserts observable output: the exact ``transform`` value on emitted
``LineageEdge`` objects, not merely "no exception raised".
"""

from pathlib import Path

from sqlglot import parse_one

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import SqlParser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _edges(parser_result) -> list:
    """Collect all LineageEdge objects from all statements in a ParsedFile."""
    out = []
    for stmt in parser_result.statements:
        out.extend(stmt.column_lineage)
    return out


def _parse(sql: str):
    """Parse SQL and return all lineage edges from the first (or only) statement."""
    parser = AnsiParser(SchemaResolver(dialect=None))
    result = parser.parse_file(Path("test.sql"), sql)
    return _edges(result)


# ---------------------------------------------------------------------------
# _classify_transform unit tests (static method, AST-only)
# ---------------------------------------------------------------------------


class TestClassifyTransform:
    """Unit tests for SqlParser._classify_transform — no parsing, pure AST."""

    def test_bare_column_no_alias_returns_pass_through(self):
        """A bare column reference with no alias is PASS_THROUGH.

        Guards the AST-only path for the simplest SELECT projection.
        """
        col_expr = parse_one("SELECT a FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "PASS_THROUGH"

    def test_column_aliased_same_name_returns_pass_through(self):
        """A column aliased to the same name (case-insensitive) is PASS_THROUGH.

        ``SELECT a AS a`` and ``SELECT a AS A`` are both pass-throughs.
        """
        same = parse_one("SELECT a AS a FROM t").expressions[0]
        assert SqlParser._classify_transform(same) == "PASS_THROUGH"

        upper = parse_one("SELECT a AS A FROM t").expressions[0]
        assert SqlParser._classify_transform(upper) == "PASS_THROUGH"

    def test_column_aliased_different_name_returns_rename(self):
        """A bare column aliased to a different name is RENAME.

        The value flows unchanged but the output column has a new name.
        """
        col_expr = parse_one("SELECT a AS b FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "RENAME"

    def test_aggregation_sum_returns_aggregation(self):
        """SUM(x) projection is AGGREGATION.

        Guards the bounded find(exp.AggFunc) on the single projection expression.
        """
        col_expr = parse_one("SELECT SUM(x) AS total FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "AGGREGATION"

    def test_aggregation_count_returns_aggregation(self):
        """COUNT(*) projection is AGGREGATION."""
        col_expr = parse_one("SELECT COUNT(*) AS cnt FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "AGGREGATION"

    def test_aggregation_max_returns_aggregation(self):
        """MAX(x) projection is AGGREGATION."""
        col_expr = parse_one("SELECT MAX(x) AS m FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "AGGREGATION"

    def test_arithmetic_expression_returns_expression(self):
        """An arithmetic expression (x * y) is EXPRESSION.

        No AggFunc, so it falls through to the default EXPRESSION bucket.
        """
        col_expr = parse_one("SELECT x * y AS total FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "EXPRESSION"

    def test_function_call_no_aggfunc_returns_expression(self):
        """A scalar function (UPPER, COALESCE, …) with no AggFunc is EXPRESSION."""
        col_expr = parse_one("SELECT UPPER(name) AS upper_name FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "EXPRESSION"

    def test_cast_expression_returns_expression(self):
        """CAST(x AS INTEGER) is EXPRESSION — it transforms type, not aggregation."""
        col_expr = parse_one("SELECT CAST(x AS INTEGER) AS x_int FROM t").expressions[0]
        assert SqlParser._classify_transform(col_expr) == "EXPRESSION"

    def test_case_when_returns_expression(self):
        """CASE WHEN … END is EXPRESSION — no AggFunc."""
        col_expr = parse_one(
            "SELECT CASE WHEN a > 0 THEN 'pos' ELSE 'neg' END AS sign FROM t"
        ).expressions[0]
        assert SqlParser._classify_transform(col_expr) == "EXPRESSION"


# ---------------------------------------------------------------------------
# Integration: parse_file emits correct transform values on edges
# ---------------------------------------------------------------------------


class TestTransformOnSelectEdges:
    """parse_file emits the four distinct transform values on SELECT projections.

    Observable output: exact ``transform`` string on each ``LineageEdge``.
    Guards PR 4 (plan/sprints/graph_health_catalog_and_metrics.md §6).
    """

    def test_four_transform_kinds_in_single_select(self):
        """A single SELECT with all four projection shapes emits the four distinct transforms.

        Fixture:
          a               → PASS_THROUGH (bare column, no alias)
          b AS c          → RENAME       (bare column, alias differs)
          SUM(d) AS total → AGGREGATION
          x * y AS amt    → EXPRESSION   (arithmetic)
        """
        sql = "SELECT a, b AS c, SUM(d) AS total, x * y AS amt FROM src"
        edges = _parse(sql)
        transforms = {e.dst.name: e.transform for e in edges if e.confidence == 1.0}

        assert transforms.get("a") == "PASS_THROUGH", (
            f"expected PASS_THROUGH for 'a', got {transforms.get('a')!r}"
        )
        assert transforms.get("c") == "RENAME", (
            f"expected RENAME for 'c', got {transforms.get('c')!r}"
        )
        assert transforms.get("total") == "AGGREGATION", (
            f"expected AGGREGATION for 'total', got {transforms.get('total')!r}"
        )
        assert transforms.get("amt") == "EXPRESSION", (
            f"expected EXPRESSION for 'amt', got {transforms.get('amt')!r}"
        )

    def test_pass_through_same_alias_name(self):
        """``SELECT a AS a`` produces PASS_THROUGH (same-name alias)."""
        edges = _parse("SELECT a AS a FROM src")
        assert any(e.transform == "PASS_THROUGH" for e in edges if e.confidence == 1.0)

    def test_rename_different_name(self):
        """``SELECT amount AS amt`` produces RENAME (different name) on the RENAME edge."""
        edges = _parse("SELECT amount AS amt FROM src")
        rename_edges = [e for e in edges if e.transform == "RENAME" and e.confidence == 1.0]
        assert len(rename_edges) >= 1
        assert rename_edges[0].dst.name == "amt"

    def test_aggregation_produces_aggregation_transform(self):
        """SUM projection is AGGREGATION; assert transform is set correctly."""
        edges = _parse("SELECT SUM(revenue) AS total_revenue FROM sales")
        agg_edges = [e for e in edges if e.transform == "AGGREGATION" and e.confidence == 1.0]
        assert len(agg_edges) >= 1


class TestTransformOnInsertEdges:
    """INSERT-with-column-list shapes also receive classification.

    Guards the wiring of _classify_transform in the INSERT positional block (#25).
    The INSERT positional block classifies the *original SELECT expression* (the col_expr
    before alias override by the INSERT column list), so:
    - ``INSERT INTO t (a) SELECT a FROM src`` → PASS_THROUGH (bare column, no alias)
    - ``INSERT INTO t (total) SELECT SUM(x) FROM src`` → AGGREGATION
    - ``INSERT INTO t (amt) SELECT x * y FROM src`` → EXPRESSION
    """

    def test_insert_with_column_list_pass_through(self):
        """INSERT INTO t (a) SELECT a FROM src emits PASS_THROUGH."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "INSERT INTO t (a) SELECT a FROM src",
        )
        edges = _edges(result)
        pt_edges = [e for e in edges if e.transform == "PASS_THROUGH"]
        assert len(pt_edges) >= 1, (
            f"Expected PASS_THROUGH edge; got transforms={[e.transform for e in edges]}"
        )

    def test_insert_with_column_list_aggregation(self):
        """INSERT INTO t (total) SELECT SUM(x) FROM src emits AGGREGATION."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "INSERT INTO t (total) SELECT SUM(x) FROM src",
        )
        edges = _edges(result)
        agg_edges = [e for e in edges if e.transform == "AGGREGATION"]
        assert len(agg_edges) >= 1, (
            f"Expected AGGREGATION edge; got transforms={[e.transform for e in edges]}"
        )

    def test_insert_with_column_list_expression(self):
        """INSERT INTO t (amt) SELECT x * y FROM src emits EXPRESSION."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "INSERT INTO t (amt) SELECT x * y FROM src",
        )
        edges = _edges(result)
        expr_edges = [e for e in edges if e.transform == "EXPRESSION"]
        assert len(expr_edges) >= 1, (
            f"Expected EXPRESSION edge; got transforms={[e.transform for e in edges]}"
        )


class TestTransformOnCtasEdges:
    """CTAS shapes (CREATE TABLE AS SELECT) receive classification."""

    def test_ctas_pass_through(self):
        """CTAS with bare column projection emits PASS_THROUGH."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "CREATE TABLE tgt AS SELECT a FROM src",
        )
        edges = _edges(result)
        pt_edges = [e for e in edges if e.transform == "PASS_THROUGH"]
        assert len(pt_edges) >= 1, (
            f"Expected PASS_THROUGH edge; got transforms={[e.transform for e in edges]}"
        )

    def test_ctas_aggregation(self):
        """CTAS with SUM projection emits AGGREGATION."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "CREATE TABLE tgt AS SELECT SUM(x) AS total FROM src",
        )
        edges = _edges(result)
        agg_edges = [e for e in edges if e.transform == "AGGREGATION"]
        assert len(agg_edges) >= 1, (
            f"Expected AGGREGATION edge; got transforms={[e.transform for e in edges]}"
        )

    def test_ctas_expression(self):
        """CTAS with arithmetic expression emits EXPRESSION."""
        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(
            Path("test.sql"),
            "CREATE TABLE tgt AS SELECT x + y AS sum_xy FROM src",
        )
        edges = _edges(result)
        expr_edges = [e for e in edges if e.transform == "EXPRESSION"]
        assert len(expr_edges) >= 1, (
            f"Expected EXPRESSION edge; got transforms={[e.transform for e in edges]}"
        )
