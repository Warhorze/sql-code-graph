"""Unit tests for execute_cypher ratio in gain command (T-13)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.cli.commands.gain import gain_cmd


class TestGainRatio:
    """Test execute_cypher ratio in gain command."""

    def test_high_ratio_triggers_warning(self):
        """Test that ratio > 0.3 triggers yellow warning (T-13)."""
        with patch("sqlcg.metrics.store.MetricsStore") as MockMetricsStore:
            mock_metrics = MagicMock()
            MockMetricsStore.return_value = mock_metrics

            # 15 execute_cypher + 5 other = 75% (high)
            def execute_query_side_effect(query, params=None):
                if "execute_cypher" in query and "WHERE" in query:
                    return [[15]]
                elif (
                    "COUNT(*) as count FROM tool_calls" in query
                    and "WHERE timestamp" not in query
                ):
                    return [[20]]
                elif "index_runs" in query:
                    return []
                elif "feedback" in query:
                    return [[0, 0]]
                elif "GROUP BY tool_name" in query:
                    return [["trace_column_lineage", 5]]
                return []

            mock_metrics.execute_query.side_effect = execute_query_side_effect
            mock_metrics.init_schema.return_value = None

            with patch("sqlcg.cli.commands.gain.Path") as MockPath:
                mock_path = MagicMock(spec=Path)
                mock_path.exists.return_value = True
                MockPath.return_value = mock_path

                with patch("sqlcg.cli.commands.gain.console"):
                    gain_cmd(_metrics_path=mock_path)
                    assert mock_metrics.execute_query.called

    def test_low_ratio_no_warning(self):
        """Test that ratio <= 0.3 shows no warning (T-13)."""
        with patch("sqlcg.metrics.store.MetricsStore") as MockMetricsStore:
            mock_metrics = MagicMock()
            MockMetricsStore.return_value = mock_metrics

            # 2 execute_cypher + 18 other = 10% (low)
            def execute_query_side_effect(query, params=None):
                if "execute_cypher" in query and "WHERE" in query:
                    return [[2]]
                elif (
                    "COUNT(*) as count FROM tool_calls" in query
                    and "WHERE timestamp" not in query
                ):
                    return [[20]]
                elif "index_runs" in query:
                    return []
                elif "feedback" in query:
                    return [[0, 0]]
                elif "GROUP BY tool_name" in query:
                    return [["trace_column_lineage", 18]]
                return []

            mock_metrics.execute_query.side_effect = execute_query_side_effect
            mock_metrics.init_schema.return_value = None

            with patch("sqlcg.cli.commands.gain.Path") as MockPath:
                mock_path = MagicMock(spec=Path)
                mock_path.exists.return_value = True
                MockPath.return_value = mock_path

                with patch("sqlcg.cli.commands.gain.console"):
                    gain_cmd(_metrics_path=mock_path)
                    assert mock_metrics.execute_query.called

    def test_zero_calls_no_division_error(self):
        """Test that zero total calls doesn't cause division error (T-13)."""
        with patch("sqlcg.metrics.store.MetricsStore") as MockMetricsStore:
            mock_metrics = MagicMock()
            MockMetricsStore.return_value = mock_metrics

            def execute_query_side_effect(query, params=None):
                if "execute_cypher" in query and "WHERE" in query:
                    return [[0]]
                elif (
                    "COUNT(*) as count FROM tool_calls" in query
                    and "WHERE timestamp" not in query
                ):
                    return [[0]]
                elif "index_runs" in query:
                    return []
                elif "feedback" in query:
                    return [[0, 0]]
                elif "GROUP BY tool_name" in query:
                    return []
                return []

            mock_metrics.execute_query.side_effect = execute_query_side_effect
            mock_metrics.init_schema.return_value = None

            with patch("sqlcg.cli.commands.gain.Path") as MockPath:
                mock_path = MagicMock(spec=Path)
                mock_path.exists.return_value = True
                MockPath.return_value = mock_path

                try:
                    gain_cmd(_metrics_path=mock_path)
                except ZeroDivisionError:
                    raise AssertionError(
                        "Should not raise ZeroDivisionError with zero calls"
                    ) from None
