"""Unit tests for db_info health warnings (T-02)."""

from unittest.mock import MagicMock, patch

from sqlcg.server.tools import db_info


def test_db_info_warns_on_zero_sql_columns():
    """Test that db_info warns when SqlColumn count is 0 (T-02)."""
    with patch("sqlcg.server.tools._get_backend") as mock_get_backend_func:
        mock_backend = MagicMock()
        mock_backend.get_schema_version.return_value = "1"
        mock_get_backend_func.return_value = mock_backend

        def run_read_side_effect(query, params):
            if "COLUMN_LINEAGE" in query:
                return [{"count": 0}]
            elif "parsing_mode" in query:
                return [{"mode": "sqlglot", "cnt": 10}]
            elif "SqlColumn" in query:
                return [{"count": 0}]
            elif "SqlQuery" in query:
                return [{"count": 10}]
            elif "SqlFile" in query:
                return [{"count": 5}]
            elif "SqlTable" in query:
                return [{"count": 20}]
            elif "Repo" in query:
                return [{"count": 1}]
            return [{"count": 0}]

        mock_backend.run_read.side_effect = run_read_side_effect

        result = db_info()

        assert result.warnings, "Expected warnings to be non-empty"
        assert "SqlColumn" in result.warnings[0], (
            f"Expected first warning to mention 'SqlColumn', got: {result.warnings[0]}"
        )
