"""Unit tests for tools warnings (T-02)."""

from unittest.mock import MagicMock, patch

from sqlcg.server.tools import list_dialects_and_repos


def test_list_dialects_and_repos_warns_on_zero_sql_columns():
    """Test that list_dialects_and_repos warns when SqlColumn count is 0 (T-02)."""
    with patch("sqlcg.server.tools._get_backend") as mock_get_backend_func:
        mock_backend = MagicMock()
        mock_get_backend_func.return_value = mock_backend

        # Mock run_read to handle:
        # 1. _assert_indexed check (Repo count > 0)
        # 2. LIST_DIALECTS_AND_REPOS_QUERY (repo data)
        # 3. SqlColumn count check (returns 0 to trigger warning)
        def run_read_side_effect(query, params):
            if "SqlColumn" in query:
                # Return 0 SqlColumn count to trigger warning
                return [{"count": 0}]
            elif "Repo" in query and "MATCH (r:Repo)" in query and "BELONGS_TO" not in query:
                # _assert_indexed check
                return [{"n": 1}]  # At least one repo exists
            else:
                # Return repo data for LIST_DIALECTS_AND_REPOS_QUERY
                return [{"path": "/test/sql", "name": "test_repo", "dialects": ["snowflake"]}]

        mock_backend.run_read.side_effect = run_read_side_effect

        result = list_dialects_and_repos()

        # Verify warnings are non-empty
        assert result.warnings, "Expected warnings to be non-empty"
        # Verify first warning mentions SqlColumn
        assert "SqlColumn" in result.warnings[0],             f"Expected first warning to mention 'SqlColumn', got: {result.warnings[0]}"
