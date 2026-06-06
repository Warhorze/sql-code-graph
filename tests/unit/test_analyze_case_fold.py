"""Unit tests for analyze CLI command — case-fold fix (#50).

Guards against the regression where ``analyze upstream`` / ``analyze downstream``
passed the raw user-supplied anchor straight into the graph query.  Graph keys are
stored lowercase at index time (C2 normalization), so an UPPERCASE anchor returned
"No results" while the identical lowercase anchor resolved.

The fix mirrors ``find.py`` line 19 / line 41: lowercase the ``ref`` argument
at the top of each command handler, before the first ``run_read_routed`` call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()

# Fake upstream rows returned by the graph (lowercase ids, as stored).
_UPSTREAM_ROWS = [
    {
        "id": "ba.src_orders.order_id",
        "table_qualified": "ba.src_orders",
        "file": "etl/load.sql",
        "line": 10,
    },
]

# Fake downstream rows.
_DOWNSTREAM_ROWS = [
    {
        "id": "ba.fact_orders.order_id",
        "table_qualified": "ba.fact_orders",
        "file": "etl/transform.sql",
        "line": 5,
    },
]


def _make_nf_mock() -> MagicMock:
    """Return a NoiseFilter mock that passes all rows through unchanged."""
    nf_instance = MagicMock()
    nf_instance.is_noise.return_value = False
    return nf_instance


class TestAnalyzeUpstreamCaseFold:
    """Upstream command lowercases the anchor before the graph query."""

    def test_uppercase_anchor_lowercased_in_query(self) -> None:
        """UPPERCASE anchor is lowercased before run_read_routed (#50).

        Observable assertion: run_read_routed receives the lowercased ref AND
        returns a non-empty result set that is printed (not "No results").
        """
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            return_value=_UPSTREAM_ROWS,
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result = runner.invoke(app, ["analyze", "upstream", "BA.SRC_ORDERS.ORDER_ID"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        assert "No results" not in result.output, (
            "UPPERCASE anchor should resolve — got 'No results' instead"
        )
        # The first run_read_routed call must carry the lowercased ref.
        first_call_params = mock_rrr.call_args_list[0][0][1]  # positional arg 1
        assert first_call_params.get("ref") == "ba.src_orders.order_id", (
            f"analyze upstream must lowercase the ref; got {first_call_params!r}"
        )

    def test_lowercase_and_uppercase_return_same_result(self) -> None:
        """Lowercase and UPPERCASE anchors produce the same non-empty result set."""
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            return_value=_UPSTREAM_ROWS,
        ):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result_lower = runner.invoke(app, ["analyze", "upstream", "ba.src_orders.order_id"])

        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            return_value=_UPSTREAM_ROWS,
        ):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result_upper = runner.invoke(app, ["analyze", "upstream", "BA.SRC_ORDERS.ORDER_ID"])

        assert result_lower.exit_code == 0
        assert result_upper.exit_code == 0
        # Both should produce the same column-id in output (non-empty).
        assert "ba.src_orders.order_id" in result_lower.output
        assert "ba.src_orders.order_id" in result_upper.output

    def test_mixed_case_anchor_lowercased(self) -> None:
        """Mixed-case anchor (Ba.Src_Orders.Order_Id) is fully lowercased."""
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            return_value=_UPSTREAM_ROWS,
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                runner.invoke(app, ["analyze", "upstream", "Ba.Src_Orders.Order_Id"])

        first_call_params = mock_rrr.call_args_list[0][0][1]
        assert first_call_params.get("ref") == "ba.src_orders.order_id", (
            f"Mixed-case ref must be fully lowercased; got {first_call_params!r}"
        )

    def test_bare_ref_fallback_lowercased(self) -> None:
        """_bare_ref fallback path also passes a lowercased bare ref.

        Simulates the case where the qualified lookup returns nothing but the
        bare (schema-stripped) lookup succeeds.  The bare ref must be lowercase.
        """
        # First call (qualified) → empty; second call (bare) → result.
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            side_effect=[[], _UPSTREAM_ROWS],
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result = runner.invoke(app, ["analyze", "upstream", "BA.SRC_ORDERS.ORDER_ID"])

        assert result.exit_code == 0
        assert mock_rrr.call_count == 2, "Expected two run_read_routed calls (qualified + bare)"
        bare_call_params = mock_rrr.call_args_list[1][0][1]
        assert bare_call_params.get("bare") == "src_orders.order_id", (
            f"Bare-ref fallback must be lowercase; got {bare_call_params!r}"
        )


class TestAnalyzeDownstreamCaseFold:
    """Downstream command lowercases the anchor before the graph query."""

    def test_uppercase_anchor_lowercased_in_query(self) -> None:
        """UPPERCASE anchor is lowercased before run_read_routed (#50).

        downstream() also calls run_read_routed for external consumers after the
        main result; we return [] for those follow-up calls via side_effect.
        """
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            # First call: main downstream rows.  Subsequent calls (external consumers): [].
            side_effect=[_DOWNSTREAM_ROWS, [], []],
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result = runner.invoke(app, ["analyze", "downstream", "BA.FACT_ORDERS.ORDER_ID"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        assert "No results" not in result.output, (
            "UPPERCASE anchor should resolve — got 'No results' instead"
        )
        first_call_params = mock_rrr.call_args_list[0][0][1]
        assert first_call_params.get("ref") == "ba.fact_orders.order_id", (
            f"analyze downstream must lowercase the ref; got {first_call_params!r}"
        )

    def test_lowercase_and_uppercase_return_same_result(self) -> None:
        """Lowercase and UPPERCASE anchors produce the same non-empty result set."""
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            side_effect=[_DOWNSTREAM_ROWS, [], []],
        ):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result_lower = runner.invoke(
                    app, ["analyze", "downstream", "ba.fact_orders.order_id"]
                )

        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            side_effect=[_DOWNSTREAM_ROWS, [], []],
        ):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result_upper = runner.invoke(
                    app, ["analyze", "downstream", "BA.FACT_ORDERS.ORDER_ID"]
                )

        assert result_lower.exit_code == 0
        assert result_upper.exit_code == 0
        assert "ba.fact_orders.order_id" in result_lower.output
        assert "ba.fact_orders.order_id" in result_upper.output

    def test_mixed_case_anchor_lowercased(self) -> None:
        """Mixed-case anchor is fully lowercased for downstream."""
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            side_effect=[_DOWNSTREAM_ROWS, [], []],
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                runner.invoke(app, ["analyze", "downstream", "Ba.Fact_Orders.Order_Id"])

        first_call_params = mock_rrr.call_args_list[0][0][1]
        assert first_call_params.get("ref") == "ba.fact_orders.order_id", (
            f"Mixed-case ref must be fully lowercased; got {first_call_params!r}"
        )

    def test_bare_ref_fallback_lowercased(self) -> None:
        """_bare_ref fallback path also passes a lowercased bare ref (downstream)."""
        with patch(
            "sqlcg.cli.commands.analyze.run_read_routed",
            # qualified → empty, bare → downstream rows, then external consumer calls → [].
            side_effect=[[], _DOWNSTREAM_ROWS, [], []],
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                mock_nf.return_value = _make_nf_mock()
                result = runner.invoke(app, ["analyze", "downstream", "BA.FACT_ORDERS.ORDER_ID"])

        assert result.exit_code == 0
        # First call: qualified lookup (empty). Second call: bare lookup (rows returned).
        # Subsequent calls: external consumer queries for terminal tables.
        assert mock_rrr.call_count >= 2, (
            "Expected at least 2 run_read_routed calls (qualified + bare); "
            f"got {mock_rrr.call_count}"
        )
        bare_call_params = mock_rrr.call_args_list[1][0][1]
        assert bare_call_params.get("bare") == "fact_orders.order_id", (
            f"Bare-ref fallback must be lowercase; got {bare_call_params!r}"
        )
