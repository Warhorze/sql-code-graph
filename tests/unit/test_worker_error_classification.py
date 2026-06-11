"""Unit tests for worker_error bucket in error_classify (PR 2, step 2.2).

A pool worker that raises an exception returns an ``_error_file`` ParsedFile
whose ``errors`` list contains ``"worker_error:<ExcType>:<msg>"`` (or
``"worker_error:send_failed"``).  Before this fix, ``_classify_error`` mapped
these messages to ``"other"``, so ``dominant_cause`` returned ``("", False)``
and the File row was stored as healthy with an empty cause.

After the fix: ``"worker_error"`` is its own degrading bucket, causing the
File row to be marked ``parse_failed=True`` with ``parse_cause="worker_error"``.

Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.2.
"""

from __future__ import annotations

from sqlcg.indexer.error_classify import _classify_error, dominant_cause

# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------


class TestClassifyErrorWorkerError:
    def test_classify_worker_error_basic(self):
        """'worker_error:send_failed' maps to the worker_error bucket."""
        assert _classify_error("worker_error:send_failed") == "worker_error"

    def test_classify_worker_error_exc_type(self):
        """'worker_error:AttributeError:...' maps to the worker_error bucket."""
        assert (
            _classify_error("worker_error:AttributeError:'str' object has no attribute 'args'")
            == "worker_error"
        )

    def test_classify_worker_error_runtime_error(self):
        """'worker_error:RuntimeError:pipe_error:...' maps to the worker_error bucket."""
        assert (
            _classify_error("worker_error:RuntimeError:pipe_error:[Errno 32] Broken pipe")
            == "worker_error"
        )

    def test_classify_unrelated_messages_unchanged(self):
        """Non-worker-error messages still classify as before."""
        assert _classify_error("timeout:30s") == "timeout"
        assert _classify_error("col_lineage_skip:pure_ddl_file") == "pure_ddl_skip"
        assert _classify_error("col_lineage_skip:qualify_failed:some.table") == "qualify_failed"
        assert _classify_error("something_completely_unknown") == "other"


# ---------------------------------------------------------------------------
# dominant_cause integration
# ---------------------------------------------------------------------------


class TestDominantCauseWorkerError:
    def test_worker_error_message_yields_parse_failed_true(self):
        """A single worker_error message produces parse_failed=True.

        Before the fix, dominant_cause returned ("", False) for this input.
        Observable via the File row's parse_failed column.

        Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.2.
        """
        errors = ["worker_error:AttributeError:'str' object has no attribute 'args'"]
        cause, failed = dominant_cause(errors)
        assert failed is True, f"Expected parse_failed=True for worker_error, got failed={failed!r}"
        assert cause == "worker_error", f"Expected parse_cause='worker_error', got {cause!r}"

    def test_worker_error_send_failed_yields_parse_failed_true(self):
        """'worker_error:send_failed' also yields parse_failed=True."""
        errors = ["worker_error:send_failed"]
        cause, failed = dominant_cause(errors)
        assert failed is True
        assert cause == "worker_error"

    def test_worker_error_dominates_other_bucket(self):
        """worker_error outranks 'other' class messages (higher priority)."""
        errors = ["something_unknown", "worker_error:RuntimeError:boom"]
        cause, failed = dominant_cause(errors)
        assert cause == "worker_error"
        assert failed is True

    def test_timeout_dominates_worker_error(self):
        """timeout has higher priority than worker_error (same count, ties broken by priority)."""
        errors = ["timeout:30s", "worker_error:RuntimeError:boom"]
        cause, failed = dominant_cause(errors)
        assert cause == "timeout"
        assert failed is True

    def test_empty_errors_unchanged(self):
        """Empty errors list still returns ('', False)."""
        cause, failed = dominant_cause([])
        assert cause == ""
        assert failed is False

    def test_worker_error_is_degrading(self):
        """worker_error maps to a degrading bucket (not a skip)."""
        from sqlcg.indexer.error_classify import _DEGRADING

        assert "worker_error" in _DEGRADING
