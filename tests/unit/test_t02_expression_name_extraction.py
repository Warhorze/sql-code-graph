"""Unit tests for T-02: best-effort expression name extraction (FIX-E2)."""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


class TestExpressionNameExtraction:
    """Test best-effort fallback for extracting names from unaliased expressions."""

    def test_unaliased_function_expression_attempts_lineage(self):
        """Unaliased function expression must attempt lineage, not skip silently.

        Before fix: ROUND(amount, 2) was skipped with no error marker
        After fix: should attempt lineage and record col_lineage_attempt:expr_fallback:Round
        """
        sql = "CREATE VIEW v AS SELECT ROUND(amount, 2) FROM orders;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        assert len(parsed.statements) > 0
        errors = parsed.errors
        # Must NOT contain the old "skip" marker
        assert not any("col_lineage_skip:expr_no_name" in e for e in errors), (
            "Old blanket skip marker must be removed"
        )
        # Must contain the new fallback marker (actual type depends on sqlglot's function class)
        assert any("col_lineage_attempt:expr_fallback:" in e for e in errors), (
            "Must record fallback attempt for non-Column expressions"
        )

    def test_star_expressions_still_skipped(self):
        """Star expressions (*) must still be skipped, not attempted."""
        sql = "CREATE VIEW v AS SELECT * FROM orders;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        assert len(parsed.statements) > 0
        errors = parsed.errors
        # Star must be skipped
        assert any("col_lineage_skip:star:" in e for e in errors), "Star must be skipped"
        # But no fallback attempt
        assert not any("col_lineage_attempt:expr_fallback" in e for e in errors), (
            "Star expressions must not attempt fallback"
        )

    def test_cast_expression_fallback_records_type(self):
        """CAST(col AS type) expression must attempt and record type."""
        sql = "CREATE VIEW v AS SELECT CAST(id AS VARCHAR) FROM orders;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        assert len(parsed.statements) > 0
        errors = parsed.errors
        # Must record some expression fallback (Casting or Cast depending on sqlglot version)
        assert any("col_lineage_attempt:expr_fallback:" in e for e in errors), (
            "Must record fallback for CAST expressions"
        )

    def test_aliased_expression_skips_fallback(self):
        """Aliased expressions must be used as-is, no fallback needed."""
        sql = "CREATE VIEW v AS SELECT ROUND(amount, 2) AS rounded_amount FROM orders;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        assert len(parsed.statements) > 0
        errors = parsed.errors
        # No fallback needed since alias is present
        assert not any("col_lineage_attempt:expr_fallback" in e for e in errors), (
            "Aliased expressions should not use fallback"
        )

    def test_column_reference_still_works(self):
        """Bare column references (Column nodes) must still work without fallback."""
        sql = "CREATE VIEW v AS SELECT amount FROM orders;"
        parser = AnsiParser(SchemaResolver())

        parsed = parser.parse_file(Path("test.sql"), sql)

        assert len(parsed.statements) > 0
        errors = parsed.errors
        # Should not attempt fallback for Column nodes
        assert not any("col_lineage_attempt:expr_fallback:Column" in e for e in errors), (
            "Column references should not trigger fallback"
        )
