"""Integration tests for the degraded_files metric in index_repo summary.

Verifies that index_repo returns the correct degraded_files count for fixtures
that trigger parse degradation.

Note on E5 fixtures: the quoted-alias views were the original E5 fixture class,
but PR-2 (fix/e5-view-alias) fixed E5 — those views now parse cleanly with zero
errors.  This test was updated in PR-2 to use a still-degrading fixture class
(qualify_failed, which reliably triggers E-code degradation) while retaining the
distinct-file counting invariant.

Note on E8 fixture: E8 (dynamic_source) only reproduces with the Snowflake
scripting path (_qualify_bare_tables key-mismatch), which is PR-3's concern.

Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1 / Integration test.
"""

from sqlcg.core.duckdb_backend import DuckDBBackend  # noqa: E402
from sqlcg.indexer.indexer import Indexer  # noqa: E402

# ---------------------------------------------------------------------------
# E5-shape fixture (now CLEAN after PR-2): quoted single-token view alias
# ---------------------------------------------------------------------------
# After PR-2 this view parses cleanly — both aliases resolve. Retained here to
# document that the E5 fixture no longer triggers degradation and as a regression
# guard for the PR-2 fix (see also tests/unit/test_e5_view_alias_resolution.py).
#
_E5_SQL_AFDELING = """\
CREATE OR REPLACE VIEW IA_SEMANTIC."Afdeling" (
    "Afdeling koppelcode" COMMENT 'multi-word stays quoted — resolves fine',
    "Afdelingsnummer"     COMMENT 'single-token — now fixed by PR-2'
) AS SELECT
    S1_AFDELING  AS "Afdeling koppelcode",
    DN_AFDELING  AS "Afdelingsnummer"
FROM BA.WTDA_AFDELING;
"""

# ---------------------------------------------------------------------------
# Degrading fixture 1: func_fallback degradation (in _DEGRADING set)
# ---------------------------------------------------------------------------
# Unaliased function expressions (UPPER, COALESCE, etc.) hit the func_fallback
# path in base.py, which is in _DEGRADING.  When ALL columns in a statement are
# func_fallback, the file's dominant cause is func_fallback → degraded_files++.
# This is a stable, cross-version trigger that survives E5/E8 fixes.
#
_DEGRADING_SQL_1 = """\
INSERT INTO BA.TARGET
SELECT
    UPPER(col_a),
    COALESCE(col_b, 0)
FROM BA.SRC_TABLE;
"""

# ---------------------------------------------------------------------------
# Degrading fixture 2: same mechanism, distinct file — tests distinct counting
# ---------------------------------------------------------------------------
# Three func_fallback columns → dominant cause = func_fallback → 1 degraded file.
# Tests that degraded_files counts DISTINCT FILES, not error strings.
#
_DEGRADING_SQL_2 = """\
INSERT INTO BA.OTHER_TARGET
SELECT
    UPPER(x),
    LOWER(y),
    TRIM(z)
FROM BA.SRC_TABLE;
"""


def test_degraded_files_count_matches_two_e5_fixture_files(tmp_path):
    """index_repo reports degraded_files == 2 for two degrading files.

    The fixture contains exactly two files with func_fallback errors (unaliased
    function expressions trigger the func_fallback skip, which is in _DEGRADING):
      - degrading_1.sql: 2 func_fallback column skips → 1 degraded file
      - degrading_2.sql: 3 func_fallback column skips → 1 degraded file

    degraded_files counts DISTINCT FILES, not error strings:
    degrading_2.sql with 3 errors still contributes only 1 to degraded_files.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1 Integration test.
    """
    (tmp_path / "degrading_1.sql").write_text(_DEGRADING_SQL_1)
    (tmp_path / "degrading_2.sql").write_text(_DEGRADING_SQL_2)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect="snowflake", db=backend)

    # Observable output: the new key must be present
    assert "degraded_files" in summary, (
        "index_repo summary must contain 'degraded_files' key (Step 1.1)"
    )
    n_degraded = summary["degraded_files"]

    # The fixture has exactly 2 degraded files (not 5 — the multi-error file counts once)
    assert n_degraded == 2, (
        f"Expected degraded_files == 2 (2 degrading files, regardless of per-file error count); "
        f"got {n_degraded}.  error_summary={summary.get('error_summary')}"
    )


def test_e5_view_fixture_now_clean_after_pr2_fix(tmp_path):
    """E5-shape view fixture produces zero errors after PR-2 fix.

    Before PR-2, the quoted single-token alias 'Afdelingsnummer' triggered E5.
    After PR-2, both aliases resolve cleanly → degraded_files == 0.

    This test pins the regression guard: the PR-2 fix must not be reverted.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-2 (Step 2.1).
    """
    (tmp_path / "e5_afdeling.sql").write_text(_E5_SQL_AFDELING)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect="snowflake", db=backend)

    assert "degraded_files" in summary, "index_repo summary must contain 'degraded_files' key"
    n_degraded = summary.get("degraded_files", -1)
    assert n_degraded == 0, (
        f"E5 view fixture should be clean after PR-2 fix, got degraded_files={n_degraded}. "
        f"error_summary={summary.get('error_summary')}"
    )


def test_degraded_files_zero_for_clean_fixture(tmp_path):
    """index_repo reports degraded_files == 0 for a clean file with no E-code errors.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1 Integration test.
    """
    (tmp_path / "clean.sql").write_text(
        "CREATE TABLE BA.ORDERS (id INTEGER, amount DECIMAL);\n"
        "CREATE VIEW BA.V_ORDERS AS SELECT id, amount FROM BA.ORDERS;\n"
    )

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect="snowflake", db=backend)

    assert summary.get("degraded_files", -1) == 0, (
        f"Expected degraded_files == 0 for a clean file; "
        f"got {summary.get('degraded_files')}.  "
        f"error_summary={summary.get('error_summary')}"
    )
