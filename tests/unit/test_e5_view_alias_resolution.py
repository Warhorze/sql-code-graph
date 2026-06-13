"""Unit tests for E5 fix: quoted case-sensitive single-token view-alias resolution.

Before PR-2, qualify() uppercased single-token quoted view output aliases
(e.g. "Afdelingsnummer" → AFDELINGSNUMMER in the scope), so
sg_lineage("Afdelingsnummer", body, ...) raised
"Cannot find column 'AFDELINGSNUMMER'" → E5 error.
Space-containing aliases were unaffected because spaces prevent normalization.

Fix: pass a quoted exp.Column expression (quoted=True) instead of a plain
string to sg_lineage for aliased column expressions. normalize_identifiers()
preserves case for quoted identifiers, so the scope lookup succeeds.

Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
"""

from __future__ import annotations

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser

# Real T&T Liquibase view shape: quoted schema.view, col-list with COMMENT,
# BOTH a single-token quoted alias ("Afdelingsnummer") and a space-containing
# quoted alias ("Afdeling koppelcode") in the same view.
_E5_VIEW_SQL = """\
CREATE OR REPLACE VIEW IA_SEMANTIC."Afdeling" (
    "Afdeling koppelcode" COMMENT 'Afdeling koppelcode',
    "Afdelingsnummer"     COMMENT 'Afdelingsnummer'
) AS SELECT
    S1_AFDELING AS "Afdeling koppelcode",
    DN_AFDELING AS "Afdelingsnummer"
FROM BA.WTDA_AFDELING;
"""


def _make_parser() -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"))


class TestE5SingleTokenQuotedAliasResolution:
    """E5 fix: sg_lineage resolves single-token quoted view output aliases."""

    def test_single_token_quoted_alias_emits_lineage_edge(self, tmp_path):
        """Single-token quoted alias "Afdelingsnummer" now emits a COLUMN_LINEAGE edge.

        Before PR-2: sg_lineage raised "Cannot find column 'AFDELINGSNUMMER'"
        because qualify() uppercased the bareword-looking alias and the case-
        sensitive scope lookup failed. After PR-2: a quoted exp.Column arg
        preserves case through normalize_identifiers(), so the lookup succeeds.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        stmts = result.statements
        assert stmts, "no statements parsed"

        # Collect all dst column names across all emitted edges
        all_dst_names = [edge.dst.name for stmt in stmts for edge in stmt.column_lineage]

        # The single-token alias must now resolve ("afdelingsnummer" after C2 lowercase)
        assert any(name == "afdelingsnummer" for name in all_dst_names), (
            f"Expected 'afdelingsnummer' in dst names. Got: {all_dst_names}. "
            f"Errors: {result.errors}"
        )

    def test_single_token_alias_emits_no_cannot_find_column_error(self, tmp_path):
        """Single-token quoted alias "Afdelingsnummer" produces zero E5 errors.

        The E5 error bucket is identified by the pattern
        col_lineage:<col>:Cannot find column '<NAME>' in error strings.
        After PR-2 this must be absent for single-token aliases.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        e5_errors = [
            e for e in result.errors if "Cannot find column" in e and "Afdelingsnummer" in e
        ]
        assert not e5_errors, f"Expected zero E5 errors for 'Afdelingsnummer', got: {e5_errors}"

    def test_space_containing_alias_still_resolves(self, tmp_path):
        """Space-containing alias "Afdeling koppelcode" still emits a lineage edge.

        Regression guard: the E5 fix must not break the already-working case.
        Space-containing aliases normalize correctly even before PR-2 because
        spaces prevent normalize_identifiers() from uppercasing them.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (non-regression).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        stmts = result.statements
        assert stmts, "no statements parsed"

        all_dst_names = [edge.dst.name for stmt in stmts for edge in stmt.column_lineage]

        # The space-containing alias must still resolve ("afdeling koppelcode" lowercase)
        assert any(name == "afdeling koppelcode" for name in all_dst_names), (
            f"Expected 'afdeling koppelcode' in dst names. Got: {all_dst_names}. "
            f"Errors: {result.errors}"
        )

    def test_both_aliases_emit_edges_and_zero_errors(self, tmp_path):
        """Both the single-token AND space-containing aliases emit edges with zero errors.

        Combines the positive assertion (both edges present) with the absence
        of E5 errors. This is the consolidated acceptance test for the full
        E5 fixture shape matching the live T&T Liquibase view pattern.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1 + non-regression).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        stmts = result.statements
        assert stmts, "no statements parsed"

        all_dst_names = {edge.dst.name for stmt in stmts for edge in stmt.column_lineage}

        # Both aliases must be present
        assert "afdelingsnummer" in all_dst_names, (
            f"Single-token alias 'afdelingsnummer' missing. Got: {all_dst_names}"
        )
        assert "afdeling koppelcode" in all_dst_names, (
            f"Space-containing alias 'afdeling koppelcode' missing. Got: {all_dst_names}"
        )

        # Zero E5 errors
        e5_errors = [e for e in result.errors if "Cannot find column" in e]
        assert not e5_errors, f"Expected zero 'Cannot find column' errors. Got: {e5_errors}"

    def test_source_column_preserved_in_edge(self, tmp_path):
        """The source column name is correctly traced from the underlying expression.

        For DN_AFDELING AS "Afdelingsnummer", the src column must be 'dn_afdeling'
        (from BA.WTDA_AFDELING), not a garbage value. This verifies that option-1
        (resolve on the source expression) correctly traces through the SELECT body.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        stmts = result.statements
        assert stmts, "no statements parsed"

        # Find the edge for "Afdelingsnummer"
        afdelingsnummer_edges = [
            edge
            for stmt in stmts
            for edge in stmt.column_lineage
            if edge.dst.name == "afdelingsnummer"
        ]

        assert afdelingsnummer_edges, (
            f"No edge found for dst='afdelingsnummer'. All edges: "
            f"{[(e.src.name, e.dst.name) for stmt in stmts for e in stmt.column_lineage]}"
        )

        src_names = {e.src.name for e in afdelingsnummer_edges}
        assert "dn_afdeling" in src_names, (
            f"Expected src='dn_afdeling' for 'afdelingsnummer', got: {src_names}"
        )

    def test_src_table_is_wtda_afdeling(self, tmp_path):
        """Source table for both aliases is 'wtda_afdeling' (from BA.WTDA_AFDELING).

        Verifies that the edge traces to the correct leaf source table, not to
        a garbage/intermediate node.

        Guards plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
        """
        parser = _make_parser()
        result = parser.parse_file(tmp_path / "afdeling.sql", _E5_VIEW_SQL)

        stmts = result.statements
        assert stmts, "no statements parsed"

        all_edges = [edge for stmt in stmts for edge in stmt.column_lineage]
        assert all_edges, f"No edges emitted. Errors: {result.errors}"

        src_tables = {e.src.table.name for e in all_edges}
        assert "wtda_afdeling" in src_tables, (
            f"Expected src table 'wtda_afdeling', got: {src_tables}"
        )
