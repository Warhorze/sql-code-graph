"""Unit tests for submit_feedback with FN label (T-13)."""

from unittest.mock import patch

from sqlcg.server.tools import submit_feedback


def test_fn_label_accepted():
    """Test that FN label is accepted and recorded (T-13)."""
    with patch("sqlcg.server.tools._metrics"):
        # Test that FN is not rejected
        try:
            result = submit_feedback(
                tool_name="trace_column_lineage",
                query="orders.amount",
                label="FN",
                note="Expected lineage but got empty"
            )
            assert result["status"] in ("recorded", "skipped")
        except ValueError:
            raise AssertionError("FN label should be accepted")


def test_tp_and_fp_still_valid():
    """Test that TP and FP labels still work."""
    with patch("sqlcg.server.tools._metrics"):
        # Test TP
        try:
            result = submit_feedback(
                tool_name="find_table_usages",
                query="orders",
                label="TP"
            )
            assert result["status"] in ("recorded", "skipped")
        except ValueError:
            raise AssertionError("TP label should be accepted")

        # Test FP
        try:
            result = submit_feedback(
                tool_name="find_table_usages",
                query="orders",
                label="FP"
            )
            assert result["status"] in ("recorded", "skipped")
        except ValueError:
            raise AssertionError("FP label should be accepted")


def test_invalid_label_raises_error_mentioning_fn():
    """Test that invalid label raises ValueError mentioning FN."""
    with patch("sqlcg.server.tools._metrics"):
        try:
            submit_feedback(
                tool_name="trace_column_lineage",
                query="orders.amount",
                label="XX"
            )
            raise AssertionError("Should raise ValueError for invalid label")
        except ValueError as e:
            error_msg = str(e)
            assert "FN" in error_msg, f"Error message should mention FN, got: {error_msg}"
            assert "TP" in error_msg
            assert "FP" in error_msg
