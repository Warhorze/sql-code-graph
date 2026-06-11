"""Unit tests for the skip_counts_json helper and File.skip_counts persistence.

Guards that col_lineage_skip:* reasons are grouped by reason prefix and persisted
as a queryable JSON map on the File node, enabling §G accounting without log
archaeology.

Plan: plan/sprints/sprint_postmortem_fixes.md §PR 5 (Step 5.1 / Step 5.2).
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# skip_counts_json helper — unit tests
# ---------------------------------------------------------------------------


def test_skip_counts_json_groups_by_reason_prefix():
    """skip_counts_json groups col_lineage_skip:*:<detail> by reason prefix.

    Strings that share the same reason (the segment after 'col_lineage_skip:'
    and before the next ':') must be counted together.
    """
    from sqlcg.indexer.error_classify import skip_counts_json

    errors = [
        "col_lineage_skip:stage:/path/to/file.sql",
        "col_lineage_skip:stage:/path/to/other.sql",
        "col_lineage_skip:unknown_sentinel:my_col",
    ]
    result = skip_counts_json(errors)
    assert result is not None
    parsed = json.loads(result)
    assert parsed["stage"] == 2
    assert parsed["unknown_sentinel"] == 1
    assert len(parsed) == 2


def test_skip_counts_json_empty_returns_none():
    """skip_counts_json returns None for an empty errors list (clean files store NULL)."""
    from sqlcg.indexer.error_classify import skip_counts_json

    assert skip_counts_json([]) is None


def test_skip_counts_json_no_skip_entries_returns_none():
    """skip_counts_json returns None when no col_lineage_skip:* strings are present."""
    from sqlcg.indexer.error_classify import skip_counts_json

    errors = [
        "col_lineage:src_col:Cannot find column 'x'",
        "worker_error:AttributeError:some_detail",
        "timeout:30s",
    ]
    assert skip_counts_json(errors) is None


def test_skip_counts_json_mixed_errors_only_counts_skip_prefix():
    """skip_counts_json ignores non-col_lineage_skip:* entries.

    Only strings starting with 'col_lineage_skip:' contribute to the count;
    other errors (E-codes, timeouts, worker_error) must be ignored.
    """
    from sqlcg.indexer.error_classify import skip_counts_json

    errors = [
        "col_lineage:src.col:Cannot find column 'src'",
        "col_lineage_skip:merge_branch:tgt",
        "col_lineage_skip:merge_branch:other_tgt",
        "timeout:30s",
        "col_lineage_skip:qualify_failed:TypeError",
    ]
    result = skip_counts_json(errors)
    assert result is not None
    parsed = json.loads(result)
    assert parsed == {"merge_branch": 2, "qualify_failed": 1}


def test_skip_counts_json_all_known_reasons():
    """skip_counts_json handles all known col_lineage_skip:* reason prefixes.

    Known reasons (from base.py and normalize_keys):
    - stage, merge_branch, unknown_sentinel, qualify_failed, func_fallback,
      dynamic_source, pure_ddl_file, star.
    """
    from sqlcg.indexer.error_classify import skip_counts_json

    errors = [
        "col_lineage_skip:stage:/f.sql",
        "col_lineage_skip:merge_branch:<unknown>",
        "col_lineage_skip:unknown_sentinel:col_x",
        "col_lineage_skip:qualify_failed:TypeError",
        "col_lineage_skip:func_fallback:Cast",
        "col_lineage_skip:dynamic_source:col_y",
        "col_lineage_skip:pure_ddl_file",
        "col_lineage_skip:star:src_table",
    ]
    result = skip_counts_json(errors)
    assert result is not None
    parsed = json.loads(result)
    assert all(v == 1 for v in parsed.values())
    assert len(parsed) == 8


def test_skip_counts_json_pure_ddl_file_no_trailing_detail():
    """skip_counts_json handles col_lineage_skip:pure_ddl_file (no trailing detail)."""
    from sqlcg.indexer.error_classify import skip_counts_json

    errors = ["col_lineage_skip:pure_ddl_file", "col_lineage_skip:pure_ddl_file"]
    result = skip_counts_json(errors)
    assert result is not None
    parsed = json.loads(result)
    assert parsed == {"pure_ddl_file": 2}


# ---------------------------------------------------------------------------
# _build_file_rows wiring — skip_counts ends up in file_rows dict
# ---------------------------------------------------------------------------


def test_build_file_rows_includes_skip_counts_key():
    """_build_file_rows must include 'skip_counts' in every file row dict.

    This guards the wiring in indexer.py so the key reaches upsert_nodes_bulk
    and is persisted to the File node.
    """
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.parsers.base import ParsedFile

    parsed = ParsedFile(path=Path("/tmp/test.sql"), dialect="ansi")
    parsed.errors = [
        "col_lineage_skip:stage:/tmp/test.sql",
        "col_lineage_skip:unknown_sentinel:col_a",
    ]

    indexer = Indexer()
    file_rows = indexer._build_file_rows(parsed)

    assert len(file_rows.file_rows) == 1
    row = file_rows.file_rows[0]
    assert "skip_counts" in row, (
        "File row dict must include 'skip_counts' key — wiring check for upsert_nodes_bulk path"
    )
    assert row["skip_counts"] is not None
    parsed_counts = json.loads(row["skip_counts"])
    assert parsed_counts["stage"] == 1
    assert parsed_counts["unknown_sentinel"] == 1


def test_build_file_rows_skip_counts_none_for_clean_file():
    """_build_file_rows sets skip_counts=None when no skip reasons are present."""
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.parsers.base import ParsedFile

    parsed = ParsedFile(path=Path("/tmp/clean.sql"), dialect="ansi")
    # No errors at all
    indexer = Indexer()
    file_rows = indexer._build_file_rows(parsed)
    row = file_rows.file_rows[0]
    assert row["skip_counts"] is None
