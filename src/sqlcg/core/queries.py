"""SQL query loader. All query strings live in queries.sql."""

import re
from pathlib import Path

_SQL_FILE = Path(__file__).parent / "queries.sql"


def _load() -> dict[str, str]:
    """Load named SQL blocks from queries.sql.

    Format: blocks are separated by lines matching "-- BLOCK_NAME" at the start.
    Each block name becomes a key in the returned dict.  Comment lines that start
    with "-- " followed by lowercase words (e.g. "-- params: ...") are included in
    the block text so the regex split is only on UPPER_SNAKE_CASE block headers.
    """
    text = _SQL_FILE.read_text(encoding="utf-8")
    # Split on lines like "-- BLOCK_NAME" (all-caps with underscores, start of line)
    blocks = re.split(r"^--\s+([A-Z][A-Z0-9_]+)\s*$", text, flags=re.MULTILINE)
    result: dict[str, str] = {}
    for i in range(1, len(blocks), 2):
        name = blocks[i]
        body = blocks[i + 1].strip()
        # Strip leading comment lines (-- params: ...) from block body
        body_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            body_lines.append(line)
        result[name] = "\n".join(body_lines).strip()
    return result


_Q = _load()

DELETE_COLUMNS_FOR_FILE = _Q["DELETE_COLUMNS_FOR_FILE"]
DELETE_QUERIES_FOR_FILE = _Q["DELETE_QUERIES_FOR_FILE"]
DELETE_TABLES_FOR_FILE = _Q["DELETE_TABLES_FOR_FILE"]
DELETE_FILE = _Q["DELETE_FILE"]
INDEX_REPO_FILES_QUERY = _Q["INDEX_REPO_FILES"]
TRACE_COLUMN_LINEAGE_QUERY = _Q["TRACE_COLUMN_LINEAGE"]
FIND_TABLE_USAGES_QUERY = _Q["FIND_TABLE_USAGES"]
FIND_TABLE_USAGES_VIA_LINEAGE_QUERY = _Q["FIND_TABLE_USAGES_VIA_LINEAGE"]
GET_DOWNSTREAM_DEPENDENCIES_QUERY = _Q["GET_DOWNSTREAM_DEPENDENCIES"]
GET_UPSTREAM_DEPENDENCIES_QUERY = _Q["GET_UPSTREAM_DEPENDENCIES"]
SEARCH_SQL_PATTERN_QUERY = _Q["SEARCH_SQL_PATTERN"]
LIST_DIALECTS_AND_REPOS_QUERY = _Q["LIST_DIALECTS_AND_REPOS"]
# EXPAND_STAR_SOURCES is implemented as three DML steps in DuckDBBackend.expand_star_sources()
# rather than a single query (DuckDB cannot do MERGE in the Cypher sense).
EXPAND_STAR_SOURCES_QUERY = _Q["EXPAND_STAR_SOURCES"]
EXPAND_STAR_SOURCES_HAS_COLUMN_QUERY = _Q["EXPAND_STAR_SOURCES_HAS_COLUMN"]
EXPAND_STAR_SOURCES_LINEAGE_QUERY = _Q["EXPAND_STAR_SOURCES_LINEAGE"]
RESOLVE_JOIN_COLUMNS_QUERY = _Q["RESOLVE_JOIN_COLUMNS"]
COUNT_JOIN_COL_RESOLVED_QUERY = _Q["COUNT_JOIN_COL_RESOLVED"]
COUNT_STAR_SOURCES_QUERY = _Q["COUNT_STAR_SOURCES"]
COUNT_STAR_EXPANSIONS_QUERY = _Q["COUNT_STAR_EXPANSIONS"]
FIND_DEFINITION_QUERY = _Q["FIND_DEFINITION"]
GET_TABLE_DEFINING_FILES_QUERY = _Q["GET_TABLE_DEFINING_FILES"]
GET_PRODUCER_FILES_FOR_TABLE_QUERY = _Q["GET_PRODUCER_FILES_FOR_TABLE"]
GET_TABLE_DIRECT_UPSTREAMS_QUERY = _Q["GET_TABLE_DIRECT_UPSTREAMS"]
GET_COLUMNS_FOR_TABLE_QUERY = _Q["GET_COLUMNS_FOR_TABLE"]
GET_TABLES_DEFINED_IN_FILE_QUERY = _Q["GET_TABLES_DEFINED_IN_FILE"]
GET_TARGET_TABLES_FOR_FILE_QUERY = _Q["GET_TARGET_TABLES_FOR_FILE"]
GET_TABLE_ADJACENCY_FOR_COLUMNS_QUERY = _Q["GET_TABLE_ADJACENCY_FOR_COLUMNS"]
GET_TABLE_KINDS_BATCH_QUERY = _Q["GET_TABLE_KINDS_BATCH"]
ANALYZE_UNUSED_TABLES_QUERY = _Q["ANALYZE_UNUSED_TABLES"]
HUB_RANKING_QUERY = _Q["HUB_RANKING"]
DEPENDENT_FILES_OF_TABLES_QUERY = _Q["DEPENDENT_FILES_OF_TABLES"]
GET_TABLE_EXTERNAL_CONSUMERS_QUERY = _Q["GET_TABLE_EXTERNAL_CONSUMERS"]
GET_TABLES_EXTERNAL_CONSUMERS_BATCH_QUERY = _Q["GET_TABLES_EXTERNAL_CONSUMERS_BATCH"]
COUNT_EXTERNAL_CONSUMERS_QUERY = _Q["COUNT_EXTERNAL_CONSUMERS"]
GET_TABLE_READS_ADJACENCY_QUERY = _Q["GET_TABLE_READS_ADJACENCY"]
GET_PRODUCER_TABLES_QUERY = _Q["GET_PRODUCER_TABLES"]
