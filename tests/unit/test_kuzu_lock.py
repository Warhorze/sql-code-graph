"""Unit tests for KuzuDB lock error handling.

These tests are Kuzu-specific: they test `find_lock_holder` and the lock-error
re-raise logic that lives in kuzu_backend.py, which is scheduled for deletion
in Phase 5 of the DuckDB migration (plan v4).  DuckDB uses a different locking
model (single-writer MVCC; cross-process access is handled by the socket proxy)
so these tests have no DuckDB equivalent.
"""

import pytest

pytest.skip(
    reason="Kuzu-only: tests kuzu_backend.py lock-error handling; "
    "kuzu_backend.py is deleted in Phase 5 of DuckDB migration (plan v4)",
    allow_module_level=True,
)

# Unreachable at runtime (module-level skip above); kept for static analysis
# until the file is deleted in Phase 5.
from unittest.mock import patch  # noqa: E402

from sqlcg.core.kuzu_backend import KuzuBackend, find_lock_holder  # noqa: E402


class TestKuzuLockError:
    """Unit tests for lock error re-raising with PID hint."""

    def test_lock_error_re_raised_with_hint(self):
        """Guard: lock error is caught and re-raised with PID hint."""
        # Patch kuzu.database.Database to raise lock error
        with patch("kuzu.database.Database") as mock_db:
            mock_db.side_effect = RuntimeError(
                "IO exception: Could not set lock on file: /tmp/test.db"
            )

            # Patch _find_lock_holder to return a PID
            with patch("sqlcg.core.kuzu_backend.find_lock_holder") as mock_find:
                mock_find.return_value = "PID 12345"

                # Verify RuntimeError is raised with helpful message
                with pytest.raises(RuntimeError) as exc_info:
                    KuzuBackend("/tmp/test.db")

                assert "Database is locked" in str(exc_info.value), (
                    f"Error message must mention 'Database is locked'. Got: {exc_info.value}"
                )
                assert "12345" in str(exc_info.value), (
                    f"Error message must include PID. Got: {exc_info.value}"
                )
                assert "kill" in str(exc_info.value), (
                    f"Error message must suggest kill command. Got: {exc_info.value}"
                )

    def test_non_lock_error_propagates_unchanged(self):
        """Guard: non-lock RuntimeError is propagated unchanged."""
        # Patch kuzu.database.Database to raise a non-lock error
        with patch("kuzu.database.Database") as mock_db:
            original_error = RuntimeError("Some other error")
            mock_db.side_effect = original_error

            # Verify error is raised unchanged
            with pytest.raises(RuntimeError) as exc_info:
                KuzuBackend("/tmp/test.db")

            assert str(exc_info.value) == "Some other error", (
                f"Non-lock errors must propagate unchanged. Got: {exc_info.value}"
            )

    def test_find_lock_holder_when_lsof_unavailable(self):
        """Guard: _find_lock_holder returns graceful fallback without lsof."""
        # Patch shutil.which to indicate lsof is not available
        with patch("shutil.which", return_value=None):
            result = find_lock_holder("/tmp/test.db")

            assert "unknown" in result.lower(), (
                f"Fallback message should indicate lsof unavailable. Got: {result}"
            )


class TestKuzuLockErrorMessages:
    """Test that various lock error formats are recognized."""

    @pytest.mark.parametrize(
        "error_message",
        [
            "IO exception: Could not set lock on file: /tmp/test.db",
            "Could not set lock on file",
            "Error: lock",
            "LOCK: file is locked",
        ],
    )
    def test_lock_error_variants_recognized(self, error_message: str):
        """Guard: various lock error message formats are recognized."""
        with patch("kuzu.database.Database") as mock_db:
            mock_db.side_effect = RuntimeError(error_message)

            with patch(
                "sqlcg.core.kuzu_backend.find_lock_holder",
                return_value="PID unknown",
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    KuzuBackend("/tmp/test.db")

                assert "Database is locked" in str(exc_info.value), (
                    f"Lock error not recognized: {error_message}"
                )
