"""Unit tests for dominant_cause() in error_classify.py.

Covers bucket counting, priority tie-break, degrading vs non-degrading flag,
and the empty / all-other base cases.
"""

from sqlcg.indexer.error_classify import _CAUSE_PRIORITY, _DEGRADING, dominant_cause

# ---------------------------------------------------------------------------
# Happy-path bucket selection
# ---------------------------------------------------------------------------


def test_dominant_cause_most_frequent_bucket():
    """E5 appears 3× and E8 appears once — E5 wins by frequency."""
    errors = [
        "col_lineage:ROTATIE:Cannot find column 'ROTATIE' in query.",
        "col_lineage:ANDERE:Cannot find column 'ANDERE' in query.",
        "col_lineage:DERDE:Cannot find column 'DERDE' in query.",
        "col_lineage_skip:dynamic_source:some_col",
    ]
    cause, failed = dominant_cause(errors)
    assert cause == "E5"
    assert failed is True


def test_dominant_cause_e8_e5_tie_priority():
    """E8 and E5 each appear once — E8 wins by priority (higher blast-radius)."""
    errors = [
        "col_lineage_skip:dynamic_source:some_col",
        "col_lineage:ROTATIE:Cannot find column 'ROTATIE' in query.",
    ]
    cause, failed = dominant_cause(errors)
    assert cause == "E8"
    assert failed is True


def test_dominant_cause_pure_ddl_skip_not_degrading():
    """pure_ddl_skip is non-degrading: parse_cause is set but parse_failed is False."""
    errors = ["col_lineage_skip:pure_ddl_file"]
    cause, failed = dominant_cause(errors)
    assert cause == "pure_ddl_skip"
    assert failed is False


def test_dominant_cause_empty_list():
    """Empty error list returns ('', False)."""
    cause, failed = dominant_cause([])
    assert cause == ""
    assert failed is False


def test_dominant_cause_all_other():
    """All messages classify as 'other' — returns ('', False)."""
    errors = ["some_unknown_marker", "another_unknown"]
    cause, failed = dominant_cause(errors)
    assert cause == ""
    assert failed is False


def test_dominant_cause_timeout():
    """Single timeout error — returns ('timeout', True)."""
    errors = ["timeout:5s file=big.sql size=10000B dialect=ansi"]
    cause, failed = dominant_cause(errors)
    assert cause == "timeout"
    assert failed is True


def test_dominant_cause_func_fallback():
    """func_fallback error — degrading, returns ('func_fallback', True)."""
    errors = ["col_lineage_skip:func_fallback:Anonymous"]
    cause, failed = dominant_cause(errors)
    assert cause == "func_fallback"
    assert failed is True


def test_dominant_cause_qualify_failed():
    """qualify_failed error — degrading."""
    errors = ["col_lineage_skip:qualify_failed:ValueError something"]
    cause, failed = dominant_cause(errors)
    assert cause == "qualify_failed"
    assert failed is True


# ---------------------------------------------------------------------------
# Multi-bucket tie-break tests (priority ordering)
# ---------------------------------------------------------------------------


def test_dominant_cause_timeout_beats_e8_on_tie():
    """timeout and E8 both appear once — timeout wins (highest priority)."""
    errors = [
        "timeout:30s file=slow.sql size=99999B dialect=snowflake",
        "col_lineage_skip:dynamic_source:col",
    ]
    cause, failed = dominant_cause(errors)
    assert cause == "timeout"
    assert failed is True


def test_dominant_cause_priority_list_is_complete():
    """Every bucket in _DEGRADING is represented in _CAUSE_PRIORITY."""
    for bucket in _DEGRADING:
        assert bucket in _CAUSE_PRIORITY, (
            f"Degrading bucket {bucket!r} is missing from _CAUSE_PRIORITY"
        )


def test_dominant_cause_pure_ddl_in_priority():
    """pure_ddl_skip is the last entry in _CAUSE_PRIORITY (lowest severity)."""
    assert _CAUSE_PRIORITY[-1] == "pure_ddl_skip"


# ---------------------------------------------------------------------------
# Mixed-signal files
# ---------------------------------------------------------------------------


def test_dominant_cause_noise_does_not_pollute():
    """'other' messages are ignored — the real bucket still dominates."""
    errors = [
        "some_noise_a",
        "some_noise_b",
        "some_noise_c",
        "col_lineage:X:Cannot find column 'X' in query.",
    ]
    cause, failed = dominant_cause(errors)
    assert cause == "E5"
    assert failed is True


def test_dominant_cause_e1_null():
    """E1 (NULL literal) is degrading."""
    errors = ["col_lineage:NULL:Cannot find column 'NULL' in query."]
    cause, failed = dominant_cause(errors)
    assert cause == "E1"
    assert failed is True


def test_dominant_cause_e2_function():
    """E2 (function expression column) is degrading."""
    errors = ["col_lineage:YEAR(...):Cannot find column 'YEAR(DATE_COLUMN)' in query."]
    cause, failed = dominant_cause(errors)
    assert cause == "E2"
    assert failed is True
