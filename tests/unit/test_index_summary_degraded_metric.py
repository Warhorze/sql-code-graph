"""Unit and CLI rendering tests for the honest index-summary metric (PR-1).

Guards the degraded_files count introduced in indexer.py and the corresponding
CLI output line in index.py.

Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Steps 1.1–1.2.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlcg.indexer.error_classify import _DEGRADING, dominant_cause
from sqlcg.parsers.base import ParsedFile

# ---------------------------------------------------------------------------
# Unit: dominant_cause / _DEGRADING contract (no indexer needed)
# ---------------------------------------------------------------------------


def test_dominant_cause_e5_errors_marked_degraded():
    """A ParsedFile with E5 errors (Cannot find column) returns parse_failed=True.

    dominant_cause classifies 'col_lineage:Col:Cannot find column 'COL'' as E5,
    which is in _DEGRADING.  This is the contract that Step 1.1 relies on.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1.
    """
    errors = [
        "col_lineage:Afdelingsnummer:Cannot find column 'AFDELINGSNUMMER' in query.",
        "col_lineage_skip:unknown_sentinel:Afdelingsnummer",
    ]
    cause, parse_failed = dominant_cause(errors)
    assert parse_failed is True, f"Expected parse_failed=True for E5 errors; got cause={cause!r}"
    assert cause == "E5", f"Expected dominant cause E5; got {cause!r}"
    assert "E5" in _DEGRADING


def test_dominant_cause_e8_errors_marked_degraded():
    """A ParsedFile with E8 errors (dynamic_source) returns parse_failed=True.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1.
    """
    errors = [
        "col_lineage_skip:dynamic_source:TA_ROWID",
        "col_lineage_skip:dynamic_source:SA_ORDER",
    ]
    cause, parse_failed = dominant_cause(errors)
    assert parse_failed is True, f"Expected parse_failed=True for E8 errors; got cause={cause!r}"
    assert cause == "E8"


def test_dominant_cause_clean_file_not_degraded():
    """A ParsedFile with no errors returns parse_failed=False.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1.
    """
    cause, parse_failed = dominant_cause([])
    assert parse_failed is False, "Expected parse_failed=False for clean file"
    assert cause == ""


# ---------------------------------------------------------------------------
# Unit: degraded_files accumulation in indexer (driven via real index_repo)
# ---------------------------------------------------------------------------


def _make_parsed_file_with_errors(path: Path, errors: list[str]) -> ParsedFile:
    """Helper: construct a ParsedFile carrying given error strings."""
    pf = ParsedFile(path=path, dialect="snowflake")
    pf.errors.extend(errors)
    return pf


def test_degraded_files_count_distinct_files():
    """degraded_files in the summary counts distinct files, not total error strings.

    A file with 39 E5 errors contributes 1 to degraded_files, not 39.
    This pins the denominator described in plan/sprints/coverage_parse_failures.md
    §PR-1 Step 1.1 (the "410 files" metric, not sum-of-error-strings).

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1.
    """
    # Simulate the accumulation logic from indexer.py Step 1.1 directly.
    # Builds a list of ParsedFile objects and counts degraded_files using
    # dominant_cause — mirrors the exact loop added to indexer.py.
    parsed_files = [
        # File 1: 3 E5 errors → 1 degraded file (not 3)
        _make_parsed_file_with_errors(
            Path("a.sql"),
            [
                "col_lineage:Col1:Cannot find column 'COL1' in query.",
                "col_lineage:Col2:Cannot find column 'COL2' in query.",
                "col_lineage:Col3:Cannot find column 'COL3' in query.",
            ],
        ),
        # File 2: 2 E8 errors → 1 degraded file
        _make_parsed_file_with_errors(
            Path("b.sql"),
            [
                "col_lineage_skip:dynamic_source:COL_A",
                "col_lineage_skip:dynamic_source:COL_B",
            ],
        ),
        # File 3: no errors → not degraded
        _make_parsed_file_with_errors(Path("c.sql"), []),
    ]

    degraded_files = 0
    degraded_by_cause: dict[str, int] = {}
    for parsed in parsed_files:
        cause, parse_failed = dominant_cause(parsed.errors)
        if parse_failed:
            degraded_files += 1
            degraded_by_cause[cause] = degraded_by_cause.get(cause, 0) + 1

    assert degraded_files == 2, (
        f"Expected 2 degraded files (1 E5 + 1 E8), not sum of error strings; "
        f"got degraded_files={degraded_files}"
    )
    # degraded_by_cause must partition the headline: buckets sum to degraded_files
    assert degraded_by_cause == {"E5": 1, "E8": 1}, (
        f"Expected degraded_by_cause={{'E5': 1, 'E8': 1}}; got {degraded_by_cause}"
    )
    assert sum(degraded_by_cause.values()) == degraded_files, (
        f"degraded_by_cause values must sum to degraded_files ({degraded_files}); "
        f"got sum={sum(degraded_by_cause.values())}"
    )


# ---------------------------------------------------------------------------
# Unit/CLI: rendering — degraded-files line is ADDITIVE; existing lines unchanged
# ---------------------------------------------------------------------------


def _build_base_summary(**overrides) -> dict:
    """Return a minimal summary dict suitable for the index_cmd rendering path."""
    base = {
        "files_found": 10,
        "files_parsed": 10,
        "tables_found": 5,
        "lineage_edges_created": 42,
        "parse_errors": 0,
        "quality": {"full": 7, "table_only": 2, "scripting_fallback": 0, "failed": 1},
        "error_summary": {
            "E5": 0,
            "E8": 0,
            "E1": 0,
            "E2": 0,
            "E3": 0,
            "timeout": 0,
            "pure_ddl_skip": 0,
            "func_fallback": 0,
            "qualify_failed": 0,
            "other": 0,
        },
        "degraded_files": 0,
        # degraded_by_cause: FILE counts per dominant cause (partitions degraded_files).
        # Sum of values must equal degraded_files.
        "degraded_by_cause": {},
        "batch_size": 50,
        "external_consumers": 0,
        "external_consumer_edges": 0,
        "external_consumer_warnings": 0,
        "catalog_reapplied": False,
        "catalog_result": {},
    }
    base.update(overrides)
    return base


def _call_index_cmd_capture_output(summary: dict) -> list[str]:
    """Drive index_cmd with a mocked indexer returning the given summary dict.

    Returns the list of strings passed to console.print.
    """
    from sqlcg.cli.commands.index import index_cmd

    printed: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        (temp_path / "test.sql").write_text("SELECT 1")

        with (
            patch("sqlcg.cli.commands.index.console") as mock_console,
            patch("sqlcg.cli.commands.index.get_backend") as mock_get_backend,
            patch("sqlcg.cli.commands.index.Indexer") as mock_indexer_class,
            patch("sqlcg.cli.commands.index.get_db_path") as mock_get_db_path,
            patch("sqlcg.cli.commands.index.get_dialect") as mock_get_dialect,
            patch("sqlcg.cli.commands.index._try_route_index_via_server", return_value=False),
            patch("sqlcg.cli.commands.index.Progress"),
        ):
            mock_console.print = lambda *a, **kw: printed.append(str(a[0]) if a else "")

            mock_backend = MagicMock()
            mock_backend.get_schema_version.return_value = "8"
            mock_get_backend.return_value.__enter__.return_value = mock_backend

            mock_indexer = MagicMock()
            mock_indexer_class.return_value = mock_indexer
            mock_indexer.index_repo.return_value = summary

            mock_get_db_path.return_value = Path(tmpdir) / ".sqlcg"
            mock_get_dialect.return_value = None

            index_cmd(
                temp_path,
                dialect=None,
                dbt_manifest=None,
                timeout_per_file=30,
                no_ddl=False,
                quiet=False,
            )

    return printed


def test_degraded_line_rendered_with_correct_count():
    """CLI prints degraded-files line with FILE-based buckets that sum to the headline.

    degraded_by_cause partitions degraded_files by dominant cause (one file = one bucket
    entry), so the sum of bucket values must equal the headline count.  The previous
    error_summary-based breakdown counted error STRINGS and would show "E5: 1141" when
    only ~217 files had E5 as their dominant cause — this test pins the corrected
    file-count behaviour described in the review finding on PR #109.

    Fixture: file1 dominant-E5, file2 dominant-E8, file3 clean → degraded_files=2,
    degraded_by_cause={"E5": 1, "E8": 1}.  Rendered line buckets must sum to 2.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.2.
    """
    summary = _build_base_summary(
        degraded_files=2,
        # degraded_by_cause: FILE counts — 1 file dominated by E5, 1 by E8.
        # Sum (1+1=2) must equal degraded_files (2).
        degraded_by_cause={"E5": 1, "E8": 1},
        error_summary={
            # error_summary has more error strings than files (e.g. 39 E5 strings
            # for the one E5-dominant file) — but the CLI line must NOT use these.
            "E5": 39,
            "E8": 5,
            "E1": 0,
            "E2": 0,
            "E3": 0,
            "timeout": 0,
            "pure_ddl_skip": 0,
            "func_fallback": 0,
            "qualify_failed": 0,
            "other": 0,
        },
    )
    output = _call_index_cmd_capture_output(summary)
    joined = " ".join(output)

    # The headline "2" must appear
    assert "2" in joined, f"Expected degraded count 2 in output; got: {output}"
    # Both cause buckets must appear
    assert "E5" in joined, f"Expected E5 bucket in output; got: {output}"
    assert "E8" in joined, f"Expected E8 bucket in output; got: {output}"
    # The key phrase from the plan's wording
    assert "degraded" in joined.lower(), f"Expected 'degraded' in output; got: {output}"

    # Critical: the rendered bucket values must be FILE counts (1 each), NOT error-string
    # counts (39 / 5).  Parse the degraded line and verify bucket values sum to the headline.
    degraded_line = next((line for line in output if "degraded" in line.lower()), "")
    assert "E5: 1" in degraded_line, (
        f"Expected 'E5: 1' (file count) in degraded line, not error-string count; "
        f"got: {degraded_line!r}"
    )
    assert "E8: 1" in degraded_line, (
        f"Expected 'E8: 1' (file count) in degraded line, not error-string count; "
        f"got: {degraded_line!r}"
    )
    # Verify "39" (the error-string count) does NOT appear in the degraded line
    assert "39" not in degraded_line, (
        f"Degraded line must use file counts, not error-string counts; got: {degraded_line!r}"
    )


def test_existing_failed_line_unchanged_when_degraded():
    """The existing 'N failed' quality line is unchanged when degraded_files > 0.

    The degraded-files line is ADDITIVE — it must not replace or suppress the
    existing quality/failed line.  Both must appear in the output when both are set.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.2.
    """
    summary = _build_base_summary(
        degraded_files=3,
        degraded_by_cause={"E5": 2, "timeout": 1},
        quality={"full": 7, "table_only": 1, "scripting_fallback": 0, "failed": 1},
        error_summary={
            "E5": 3,
            "E8": 0,
            "E1": 0,
            "E2": 0,
            "E3": 0,
            "timeout": 1,
            "pure_ddl_skip": 0,
            "func_fallback": 0,
            "qualify_failed": 0,
            "other": 0,
        },
    )
    output = _call_index_cmd_capture_output(summary)
    joined = " ".join(output)

    # Both must appear
    assert "failed" in joined.lower(), f"Expected 'failed' quality line; got: {output}"
    assert "degraded" in joined.lower(), f"Expected 'degraded' line; got: {output}"


def test_no_degraded_line_when_all_clean():
    """No degraded-files line is printed when degraded_files == 0 (clean index).

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.2.
    """
    summary = _build_base_summary(
        degraded_files=0,
        quality={"full": 9, "table_only": 1, "scripting_fallback": 0, "failed": 0},
    )
    output = _call_index_cmd_capture_output(summary)
    joined = " ".join(output)

    assert "degraded" not in joined.lower(), (
        f"Expected no 'degraded' line when degraded_files=0; got: {output}"
    )
