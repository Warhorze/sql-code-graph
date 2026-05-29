"""Cypher query loader. All query strings live in queries.cypher."""

import re
from pathlib import Path

_CYPHER_FILE = Path(__file__).parent / "queries.cypher"


def _load() -> dict[str, str]:
    """Load named Cypher blocks from queries.cypher.

    Format: blocks are separated by lines matching "-- BLOCK_NAME" at the start.
    Each block name becomes a key in the returned dict.
    """
    text = _CYPHER_FILE.read_text(encoding="utf-8")
    blocks = re.split(r"^--\s+(\w+)\s*$", text, flags=re.MULTILINE)
    return {blocks[i]: blocks[i + 1].strip() for i in range(1, len(blocks), 2)}


_Q = _load()

DELETE_COLUMNS_FOR_FILE = _Q["DELETE_COLUMNS_FOR_FILE"]
DELETE_QUERIES_FOR_FILE = _Q["DELETE_QUERIES_FOR_FILE"]
DELETE_TABLES_FOR_FILE = _Q["DELETE_TABLES_FOR_FILE"]
DELETE_FILE = _Q["DELETE_FILE"]
STALE_VIEWS_QUERY = _Q["STALE_VIEWS"]
INDEX_REPO_FILES_QUERY = _Q["INDEX_REPO_FILES"]
TRACE_COLUMN_LINEAGE_QUERY = _Q["TRACE_COLUMN_LINEAGE"]
FIND_TABLE_USAGES_QUERY = _Q["FIND_TABLE_USAGES"]
GET_DOWNSTREAM_DEPENDENCIES_QUERY = _Q["GET_DOWNSTREAM_DEPENDENCIES"]
GET_UPSTREAM_DEPENDENCIES_QUERY = _Q["GET_UPSTREAM_DEPENDENCIES"]
SEARCH_SQL_PATTERN_QUERY = _Q["SEARCH_SQL_PATTERN"]
LIST_DIALECTS_AND_REPOS_QUERY = _Q["LIST_DIALECTS_AND_REPOS"]
EXPAND_STAR_SOURCES_QUERY = _Q["EXPAND_STAR_SOURCES"]
COUNT_STAR_SOURCES_QUERY = _Q["COUNT_STAR_SOURCES"]
COUNT_STAR_EXPANSIONS_QUERY = _Q["COUNT_STAR_EXPANSIONS"]
FIND_DEFINITION_QUERY = _Q["FIND_DEFINITION"]
GET_TABLE_DEFINING_FILES_QUERY = _Q["GET_TABLE_DEFINING_FILES"]
GET_TABLE_DIRECT_UPSTREAMS_QUERY = _Q["GET_TABLE_DIRECT_UPSTREAMS"]
GET_COLUMNS_FOR_TABLE_QUERY = _Q["GET_COLUMNS_FOR_TABLE"]
