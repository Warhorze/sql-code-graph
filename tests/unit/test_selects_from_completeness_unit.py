"""Unit tests for the SELECTS_FROM-completeness fix (PR 4).

Tests TableUsage.usage_kind model field and the unused command help/caveat string.

Plan reference: plan/sprints/unfilled_table_impact.md §"PR 4 — SELECTS_FROM-completeness fix"
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# TableUsage.usage_kind model tests
# ---------------------------------------------------------------------------


class TestTableUsageKindField:
    """TableUsage.usage_kind defaults to 'direct' and accepts 'via_lineage'.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Step 4.3
    """

    def test_table_usage_defaults_to_direct(self):
        """TableUsage(query_file=...) defaults usage_kind to 'direct' (additive, backward-safe)."""
        from sqlcg.server.models import TableUsage

        usage = TableUsage(query_file="/path/to/file.sql")
        assert usage.usage_kind == "direct"

    def test_table_usage_accepts_via_lineage(self):
        """TableUsage(usage_kind='via_lineage') round-trips correctly."""
        from sqlcg.server.models import TableUsage

        usage = TableUsage(query_file="/path/to/file.sql", usage_kind="via_lineage")
        assert usage.usage_kind == "via_lineage"

    def test_table_usage_kind_does_not_shadow_kind_field(self):
        """usage_kind is distinct from kind (SQL statement kind like INSERT/SELECT)."""
        from sqlcg.server.models import TableUsage

        usage = TableUsage(
            query_file="/path/to/file.sql",
            kind="INSERT",
            usage_kind="via_lineage",
        )
        assert usage.kind == "INSERT"
        assert usage.usage_kind == "via_lineage"


# ---------------------------------------------------------------------------
# unused command help caveat string test
# ---------------------------------------------------------------------------


class TestUnusedCommandHelpContainsGapCaveat:
    """The `unused` CLI command docstring/help contains the CTE pure-gating known gap caveat.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Step 4.4 Acceptance
    """

    def test_unused_help_mentions_pure_gating_gap(self):
        """The unused command function docstring mentions the CTE pure-gating known gap."""
        from sqlcg.cli.commands.analyze import unused

        doc = unused.__doc__ or ""
        # The caveat must mention both "pure-gating" and "NOT detected" (or "known gap")
        assert "pure-gating" in doc or "pure gating" in doc, (
            f"unused command docstring does not mention 'pure-gating'.\nDocstring: {doc}"
        )
        assert "NOT detected" in doc or "known gap" in doc, (
            f"unused command docstring does not contain the known-gap caveat.\nDocstring: {doc}"
        )

    def test_unused_help_mentions_column_lineage(self):
        """The unused command docstring mentions COLUMN_LINEAGE (D3 signal) to explain coverage."""
        from sqlcg.cli.commands.analyze import unused

        doc = unused.__doc__ or ""
        assert "COLUMN_LINEAGE" in doc or "column-lineage" in doc.lower(), (
            f"unused command docstring does not mention COLUMN_LINEAGE.\nDocstring: {doc}"
        )


# ---------------------------------------------------------------------------
# find_table_usages empty hint test
# ---------------------------------------------------------------------------


class TestFindTableUsagesEmptyHintMentionsGap:
    """find_table_usages empty hint no longer misattributes CTE routing as 'external'.

    The fix must mention CTE pure-gating as a known gap rather than only saying
    'not referenced or consumed externally'.

    Plan: plan/sprints/unfilled_table_impact.md §PR 4 Step 4.3 (fix empty hint)
    """

    def test_empty_hint_mentions_known_gap(self):
        """The empty hint text returned by find_table_usages mentions the known gap."""
        from unittest.mock import MagicMock

        import sqlcg.server.tools as tools_mod

        empty_db = MagicMock()
        empty_db.run_read.return_value = []

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=empty_db)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        original_open = tools_mod._open_backend
        original_assert = tools_mod._assert_indexed
        tools_mod._open_backend = lambda: mock_ctx
        tools_mod._assert_indexed = lambda db: None

        try:
            result = tools_mod.find_table_usages("da.ghost")
        finally:
            tools_mod._open_backend = original_open
            tools_mod._assert_indexed = original_assert

        assert result.hint is not None
        assert "pure-gating" in result.hint or "pure gating" in result.hint, (
            f"Empty hint does not mention pure-gating known gap.\nHint: {result.hint}"
        )
        assert "known gap" in result.hint or "NOT detected" in result.hint, (
            f"Empty hint does not document the known gap.\nHint: {result.hint}"
        )

    def test_empty_hint_no_longer_only_blames_external(self):
        """The empty hint does not solely blame 'external consumption' without mentioning
        the CTE routing cause — users should understand why a table can appear unused.
        """
        from unittest.mock import MagicMock

        import sqlcg.server.tools as tools_mod

        empty_db = MagicMock()
        empty_db.run_read.return_value = []

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=empty_db)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        original_open = tools_mod._open_backend
        original_assert = tools_mod._assert_indexed
        tools_mod._open_backend = lambda: mock_ctx
        tools_mod._assert_indexed = lambda db: None

        try:
            result = tools_mod.find_table_usages("da.ghost")
        finally:
            tools_mod._open_backend = original_open
            tools_mod._assert_indexed = original_assert

        hint = result.hint or ""
        # The hint should explain CTE routing, not just external consumption.
        # It should mention COLUMN_LINEAGE or CTE as a cause.
        assert "CTE" in hint or "lineage" in hint.lower(), (
            f"Hint does not explain CTE-routing cause.\nHint: {hint}"
        )
