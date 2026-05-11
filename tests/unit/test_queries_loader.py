"""Unit tests for the Cypher query loader (queries.py / queries.cypher).

Sprint: sprint_star_resolution.md  Ticket: T-09

These tests verify that all named constants load from queries.cypher and
that the loader contract is respected. They pass once T-09 lands (loader
already wired if queries.cypher exists) and serve as a regression guard
against accidental Cypher re-embedding in Python.
"""

import pytest

# ---------------------------------------------------------------------------
# T-09 — loader contract
# ---------------------------------------------------------------------------


def test_all_constants_load():
    """Every named constant in queries.py must be a non-empty string containing Cypher."""
    from sqlcg.core import queries

    constants = [
        "DELETE_COLUMNS_FOR_FILE",
        "DELETE_QUERIES_FOR_FILE",
        "DELETE_TABLES_FOR_FILE",
        "DELETE_FILE",
        "STALE_VIEWS_QUERY",
        "INDEX_REPO_FILES_QUERY",
        "TRACE_COLUMN_LINEAGE_QUERY",
        "FIND_TABLE_USAGES_QUERY",
        "GET_DOWNSTREAM_DEPENDENCIES_QUERY",
        "GET_UPSTREAM_DEPENDENCIES_QUERY",
        "SEARCH_SQL_PATTERN_QUERY",
        "LIST_DIALECTS_AND_REPOS_QUERY",
    ]
    cypher_keywords = {"MATCH", "MERGE", "RETURN", "DELETE"}

    for name in constants:
        value = getattr(queries, name, None)
        assert value is not None, f"queries.{name} is missing"
        assert isinstance(value, str) and value.strip(), f"queries.{name} is empty"
        assert any(kw in value for kw in cypher_keywords), (
            f"queries.{name} does not contain any Cypher keyword "
            f"({cypher_keywords}). Got: {value[:80]!r}"
        )


def test_missing_block_raises_key_error():
    """Accessing a nonexistent block key in _Q must raise KeyError, not return None."""
    from sqlcg.core.queries import _Q

    with pytest.raises(KeyError):
        _ = _Q["NONEXISTENT_BLOCK_NAME_XYZ"]


def test_queries_cypher_file_exists():
    """queries.cypher must exist alongside queries.py (no Cypher embedded in Python)."""
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    cypher_path = Path(_qmod.__file__).parent / "queries.cypher"
    assert cypher_path.exists(), (
        f"queries.cypher not found at {cypher_path}. "
        "T-09 requires all Cypher to live in queries.cypher."
    )


def test_no_raw_cypher_in_queries_py():
    """queries.py must not contain embedded Cypher strings after T-09."""
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    source = Path(_qmod.__file__).read_text(encoding="utf-8")
    cypher_keywords = ["MATCH (", "MERGE (", "DETACH DELETE", "RETURN count"]
    for kw in cypher_keywords:
        assert kw not in source, (
            f"Found embedded Cypher keyword '{kw}' in queries.py. "
            "All Cypher must live in queries.cypher (T-09)."
        )


def test_sprint05_query_constants_load():
    """Sprint-05 query constants (T-05, T-07) must also load from queries.cypher."""
    from sqlcg.core import queries

    sprint05_constants = [
        "EXPAND_STAR_SOURCES_QUERY",
        "COUNT_STAR_SOURCES_QUERY",
        "COUNT_STAR_EXPANSIONS_QUERY",
    ]
    for name in sprint05_constants:
        value = getattr(queries, name, None)
        assert value is not None, (
            f"queries.{name} is missing — T-05/T-07 query not yet added to queries.cypher"
        )
        assert isinstance(value, str) and value.strip(), f"queries.{name} is empty"


def test_loader_block_count():
    """queries.cypher must contain at least 12 named blocks (the original set)."""
    import re
    from pathlib import Path

    import sqlcg.core.queries as _qmod

    cypher_path = Path(_qmod.__file__).parent / "queries.cypher"
    text = cypher_path.read_text(encoding="utf-8")
    block_headers = re.findall(r"^--\s+[A-Z][A-Z0-9_]*\s*$", text, flags=re.MULTILINE)
    assert len(block_headers) >= 12, (
        f"queries.cypher must have at least 12 named blocks. Found {len(block_headers)}: "
        f"{block_headers}"
    )
