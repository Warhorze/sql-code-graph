"""Integration tests for the degraded_files metric in index_repo summary.

Verifies that index_repo returns the correct degraded_files count for fixtures
that trigger parse degradation (E5-class errors).

Note on E8 fixture: E8 (dynamic_source) only reproduces with the Snowflake
scripting path (_qualify_bare_tables key-mismatch), which is PR-3's concern.
The normal Snowflake parser correctly resolves temp chains intra-file.  Two
E5-shape views are used here to pin the distinct-file counting (degraded_files=2)
without requiring a scripting-path fixture.

Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1 / Integration test.
"""

from sqlcg.core.duckdb_backend import DuckDBBackend  # noqa: E402
from sqlcg.indexer.indexer import Indexer  # noqa: E402

# ---------------------------------------------------------------------------
# E5-shape fixture 1: T&T-generated view with quoted single-token output column
# ---------------------------------------------------------------------------
# qualify() uppercases the single-token alias "Afdelingsnummer" → AFDELINGSNUMMER;
# sg_lineage raises "Cannot find column 'AFDELINGSNUMMER'" → E5.
# "Afdeling koppelcode" (with space) stays quoted and resolves fine.
#
_E5_SQL_AFDELING = """\
CREATE OR REPLACE VIEW IA_SEMANTIC."Afdeling" (
    "Afdeling koppelcode" COMMENT 'multi-word stays quoted — resolves fine',
    "Afdelingsnummer"     COMMENT 'single-token gets uppercased by qualify → E5'
) AS SELECT
    S1_AFDELING  AS "Afdeling koppelcode",
    DN_AFDELING  AS "Afdelingsnummer"
FROM BA.WTDA_AFDELING;
"""

# ---------------------------------------------------------------------------
# E5-shape fixture 2: second T&T-generated view — same mechanism, different table
# ---------------------------------------------------------------------------
# Both "Artikelnummer" and "Artikelnaam" are single-token → both uppercased → E5
# for each column.  This file contributes 1 degraded file (not 2), testing
# that the distinct-file counting is correct.
#
_E5_SQL_ARTIKEL = """\
CREATE OR REPLACE VIEW IA_SEMANTIC."Artikel" (
    "Artikelnummer" COMMENT 'single-token → E5',
    "Artikelnaam"   COMMENT 'single-token → E5'
) AS SELECT
    DA_ART_NR    AS "Artikelnummer",
    DA_ART_NAAM  AS "Artikelnaam"
FROM BA.WTDA_ARTIKEL;
"""


def test_degraded_files_count_matches_two_e5_fixture_files(tmp_path):
    """index_repo reports degraded_files == 2 for two E5-shape view files.

    The fixture contains exactly two files that trigger parse degradation:
      - e5_afdeling.sql:  1 E5 column error (Afdelingsnummer) → 1 degraded file
      - e5_artikel.sql:   2 E5 column errors (Artikelnummer + Artikelnaam) → 1 degraded file

    degraded_files counts DISTINCT FILES, not error strings:
    e5_artikel.sql with 2 E5 errors still contributes only 1 to degraded_files.

    Plan doc: plan/sprints/coverage_parse_failures.md §PR-1 Step 1.1 Integration test.
    """
    (tmp_path / "e5_afdeling.sql").write_text(_E5_SQL_AFDELING)
    (tmp_path / "e5_artikel.sql").write_text(_E5_SQL_ARTIKEL)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect="snowflake", db=backend)

    # Observable output: the new key must be present
    assert "degraded_files" in summary, (
        "index_repo summary must contain 'degraded_files' key (Step 1.1)"
    )
    n_degraded = summary["degraded_files"]

    # The fixture has exactly 2 degraded files (not 3 — the multi-error file counts once)
    assert n_degraded == 2, (
        f"Expected degraded_files == 2 (2 E5 files, regardless of per-file error count); "
        f"got {n_degraded}.  error_summary={summary.get('error_summary')}"
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
