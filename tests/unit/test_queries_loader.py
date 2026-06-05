"""Unit tests for the SQL query loader (queries.py / queries.sql).

These tests verify that all named constants load from queries.sql and
that the loader contract is respected. They serve as a regression guard
against accidental SQL re-embedding in Python.
"""

import pytest

# ---------------------------------------------------------------------------
# loader contract
# ---------------------------------------------------------------------------


def test_all_constants_load():
    """Every named constant in queries.py must be a non-empty string containing SQL."""
    from sqlcg.core import queries

    constants = [
        "DELETE_COLUMNS_FOR_FILE",
        "DELETE_QUERIES_FOR_FILE",
        "DELETE_TABLES_FOR_FILE",
        "DELETE_FILE",
        "INDEX_REPO_FILES_QUERY",
        "TRACE_COLUMN_LINEAGE_QUERY",
        "FIND_TABLE_USAGES_QUERY",
        "GET_DOWNSTREAM_DEPENDENCIES_QUERY",
        "GET_UPSTREAM_DEPENDENCIES_QUERY",
        "SEARCH_SQL_PATTERN_QUERY",
        "LIST_DIALECTS_AND_REPOS_QUERY",
    ]
    sql_keywords = {"SELECT", "DELETE", "INSERT", "WITH"}

    for name in constants:
        value = getattr(queries, name, None)
        assert value is not None, f"queries.{name} is missing"
        assert isinstance(value, str) and value.strip(), f"queries.{name} is empty"
        assert any(kw in value for kw in sql_keywords), (
            f"queries.{name} does not contain any SQL keyword ({sql_keywords}). Got: {value[:80]!r}"
        )


def test_missing_block_raises_key_error():
    """Accessing a nonexistent block key in _Q must raise KeyError, not return None."""
    from sqlcg.core.queries import _Q

    with pytest.raises(KeyError):
        _ = _Q["NONEXISTENT_BLOCK_NAME_XYZ"]


def test_queries_sql_file_exists():
    """queries.sql must exist alongside queries.py (no SQL embedded in Python)."""
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    sql_path = Path(_qmod.__file__).parent / "queries.sql"
    assert sql_path.exists(), (
        f"queries.sql not found at {sql_path}. All SQL must live in queries.sql."
    )


def test_no_raw_sql_in_queries_py():
    """queries.py must not contain embedded SQL strings."""
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    source = Path(_qmod.__file__).read_text(encoding="utf-8")
    embedded_sql_patterns = ["SELECT * FROM", "INSERT INTO (", "DETACH DELETE"]
    for kw in embedded_sql_patterns:
        assert kw not in source, (
            f"Found embedded SQL pattern '{kw}' in queries.py. All SQL must live in queries.sql."
        )


def test_sprint05_query_constants_load():
    """Star expansion query constants must also load from queries.sql."""
    from sqlcg.core import queries

    sprint05_constants = [
        "EXPAND_STAR_SOURCES_QUERY",
        "COUNT_STAR_SOURCES_QUERY",
        "COUNT_STAR_EXPANSIONS_QUERY",
    ]
    for name in sprint05_constants:
        value = getattr(queries, name, None)
        assert value is not None, f"queries.{name} is missing — not yet added to queries.sql"
        assert isinstance(value, str) and value.strip(), f"queries.{name} is empty"


def test_loader_block_count():
    """queries.sql must contain at least 12 named blocks (the original set)."""
    import re
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    sql_path = Path(_qmod.__file__).parent / "queries.sql"
    text = sql_path.read_text(encoding="utf-8")
    block_headers = re.findall(r"^--\s+[A-Z][A-Z0-9_]*\s*$", text, flags=re.MULTILINE)
    assert len(block_headers) >= 12, (
        f"queries.sql must have at least 12 named blocks. Found {len(block_headers)}: "
        f"{block_headers}"
    )
