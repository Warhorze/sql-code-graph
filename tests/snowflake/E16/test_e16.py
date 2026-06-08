"""E16: MERGE multi-branch (deferred, observability recorded).

MERGE statements currently produce zero column lineage because sqlglot's lineage()
API does not handle MERGE branches. The parser records an explicit skip
(col_lineage_skip:merge_branch:*) so MERGE files are visible in reports.

See plan/sprints/sprint_07_open_ecodes.md § T-07-06 for the deferred-decision rationale.
"""

from pathlib import Path

from tests.snowflake.conftest import edges, parse


def test_e16_merge_match_and_insert(parser):
    """MERGE with MATCHED UPDATE and NOT MATCHED INSERT is recorded as deferred.

    # DEFERRED: sqlglot lineage API does not visit MERGE branches.
    # When implemented, flip to assert dst_table=="DST" and srcs == {"COL_A", "COL_B"}.
    # See plan/sprints/sprint_07_open_ecodes.md T-07-06.
    """
    sql = Path(__file__).with_name("e16_merge.sql").read_text()
    result = parse(parser, sql, "e16_merge.sql")

    all_edges = edges(result)
    assert all_edges == [], (
        "E16: MERGE currently produces no column lineage; if this fails, E16 was fixed"
    )

    # Observability: the parser should record the deferred skip explicitly.
    assert any("col_lineage_skip:merge_branch" in e for e in result.errors), (
        f"MERGE should record merge_branch skip; got errors: {result.errors}"
    )


def test_e16_merge_delete(parser):
    """MERGE with DELETE branch is recorded as deferred.

    # DEFERRED: sqlglot lineage API does not visit MERGE branches.
    # When implemented, DELETE should not produce edges, but UPDATE and INSERT should.
    """
    sql = Path(__file__).with_name("e16_merge_delete.sql").read_text()
    result = parse(parser, sql, "e16_merge_delete.sql")

    all_edges = edges(result)
    assert all_edges == [], f"E16: MERGE with DELETE still produces no column lineage: {all_edges}"

    # Observability: the parser should record the deferred skip explicitly.
    assert any("col_lineage_skip:merge_branch" in e for e in result.errors), (
        f"MERGE should record merge_branch skip; got errors: {result.errors}"
    )
