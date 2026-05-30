"""Unit tests for find CLI command — case-sensitivity fix (#12).

Guards against the regression where find_table("MY_TABLE") failed to match
graph nodes whose qualified name is stored lowercase ("ba.my_table") due to
C2 normalization at index time.
"""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg.cli.main import app

runner = CliRunner()


def _make_backend(results: list[dict]) -> MagicMock:
    """Return a mock backend context manager that produces the given results."""
    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(return_value=results)
    return backend


class TestFindTableCaseInsensitive:
    """Scenario A — uppercase input is normalized to lowercase before the graph query."""

    def test_uppercase_input_lowercased_in_query(self) -> None:
        """Uppercase table name is lowercased before the CONTAINS predicate.

        The graph stores Table.qualified in lowercase (C2 normalization). When
        the user passes MY_TABLE, the query must receive 'my_table' so the
        CONTAINS match succeeds.
        """
        backend = _make_backend([{"qualified": "ba.my_table", "kind": "TABLE"}])

        with patch("sqlcg.cli.commands.find.get_backend", return_value=backend):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                nf_instance = MagicMock()
                nf_instance.filter_nodes.return_value = (["ba.my_table"], [])
                mock_nf.return_value = nf_instance

                result = runner.invoke(app, ["find", "table", "MY_TABLE"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        # The backend was called with the lowercased name
        call_args = backend.run_read.call_args
        assert call_args is not None
        params = call_args[0][1]  # second positional arg is the params dict
        assert params["name"] == "my_table", (
            f"find_table must lowercase the name before the query; got {params['name']!r}"
        )

    def test_mixed_case_input_lowercased(self) -> None:
        """Mixed-case input is fully lowercased before the graph query."""
        backend = _make_backend([])

        with patch("sqlcg.cli.commands.find.get_backend", return_value=backend):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                nf_instance = MagicMock()
                nf_instance.filter_nodes.return_value = ([], [])
                mock_nf.return_value = nf_instance

                runner.invoke(app, ["find", "table", "Wtfs_VOORRAAD_Week"])

        call_args = backend.run_read.call_args
        assert call_args is not None
        params = call_args[0][1]
        assert params["name"] == "wtfs_voorraad_week", (
            f"Mixed-case input must be fully lowercased; got {params['name']!r}"
        )


class TestFindColumnCaseInsensitive:
    """Scenario B — find_column lowercases the ref argument."""

    def test_uppercase_column_ref_lowercased(self) -> None:
        """BA.ORDERS.AMOUNT is lowercased to ba.orders.amount before query."""
        backend = _make_backend([{"id": "ba.orders.amount"}])

        with patch("sqlcg.cli.commands.find.get_backend", return_value=backend):
            result = runner.invoke(app, ["find", "column", "BA.ORDERS.AMOUNT"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        call_args = backend.run_read.call_args
        assert call_args is not None
        params = call_args[0][1]
        assert params["ref"] == "ba.orders.amount", (
            f"find_column must lowercase the ref; got {params['ref']!r}"
        )
