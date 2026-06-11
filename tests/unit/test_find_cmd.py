"""Unit tests for find CLI command — case-sensitivity fix (#12).

Guards against the regression where find_table("MY_TABLE") failed to match
graph nodes whose qualified name is stored lowercase ("ba.my_table") due to
C2 normalization at index time.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sqlcg.cli.main import app

# resolved_repo_root() is imported into find.py and issues a "SELECT path FROM
# Repo" query against the DB to feed NoiseFilter.from_config.  With the autouse
# DB-isolation fixture the DB is empty/schema-less, so the query raises
# CatalogException.  Patch the name at its use site.
_PATCH_RESOLVED_ROOT = patch(
    "sqlcg.cli.commands.find.resolved_repo_root",
    return_value=Path("/tmp/fake-repo"),
)

runner = CliRunner()


class TestFindTableCaseInsensitive:
    """Scenario A — uppercase input is normalized to lowercase before the graph query."""

    def test_uppercase_input_lowercased_in_query(self) -> None:
        """Uppercase table name is lowercased before the CONTAINS predicate.

        The graph stores Table.qualified in lowercase (C2 normalization). When
        the user passes MY_TABLE, the query must receive 'my_table' so the
        CONTAINS match succeeds.
        """
        with (
            _PATCH_RESOLVED_ROOT,
            patch(
                "sqlcg.cli.commands.find.run_read_routed",
                return_value=[{"qualified": "ba.my_table", "kind": "TABLE"}],
            ) as mock_rrr,
        ):
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                nf_instance = MagicMock()
                nf_instance.filter_nodes.return_value = (["ba.my_table"], [])
                mock_nf.return_value = nf_instance

                result = runner.invoke(app, ["find", "table", "MY_TABLE"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        # run_read_routed was called with the lowercased name
        assert mock_rrr.call_count == 1
        _, params = mock_rrr.call_args[0]  # (cypher, params)
        assert params["name"] == "my_table", (
            f"find_table must lowercase the name before the query; got {params['name']!r}"
        )

    def test_mixed_case_input_lowercased(self) -> None:
        """Mixed-case input is fully lowercased before the graph query."""
        with patch(
            "sqlcg.cli.commands.find.run_read_routed",
            return_value=[],
        ) as mock_rrr:
            with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
                nf_instance = MagicMock()
                nf_instance.filter_nodes.return_value = ([], [])
                mock_nf.return_value = nf_instance

                runner.invoke(app, ["find", "table", "Wtfs_VOORRAAD_Week"])

        assert mock_rrr.call_count == 1
        _, params = mock_rrr.call_args[0]
        assert params["name"] == "wtfs_voorraad_week", (
            f"Mixed-case input must be fully lowercased; got {params['name']!r}"
        )


class TestFindColumnCaseInsensitive:
    """Scenario B — find_column lowercases the ref argument."""

    def test_uppercase_column_ref_lowercased(self) -> None:
        """BA.ORDERS.AMOUNT is lowercased to ba.orders.amount before query."""
        with (
            _PATCH_RESOLVED_ROOT,
            patch(
                "sqlcg.cli.commands.find.run_read_routed",
                return_value=[{"id": "ba.orders.amount"}],
            ) as mock_rrr,
        ):
            result = runner.invoke(app, ["find", "column", "BA.ORDERS.AMOUNT"])

        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        assert mock_rrr.call_count == 1
        _, params = mock_rrr.call_args[0]
        assert params["ref"] == "ba.orders.amount", (
            f"find_column must lowercase the ref; got {params['ref']!r}"
        )
