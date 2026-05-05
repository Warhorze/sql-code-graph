"""Centralized Cypher query strings for graph operations."""

from sqlcg.core.schema import NodeLabel, RelType

# Scope is bounded by exact path match; APOC procedures are not required.
# Delete Column nodes for tables defined in a file
DELETE_COLUMNS_FOR_FILE = (
    f"MATCH (f:{NodeLabel.FILE} {{path: $path}})"
    f"<-[:{RelType.DEFINED_IN}]-(t:{NodeLabel.TABLE})"
    f"-[:{RelType.HAS_COLUMN}]->(c:{NodeLabel.COLUMN})"
    " DETACH DELETE c"
)

# Delete Query nodes and their edges
DELETE_QUERIES_FOR_FILE = (
    f"MATCH (f:{NodeLabel.FILE} {{path: $path}})"
    f"<-[:{RelType.QUERY_DEFINED_IN}]-(q:{NodeLabel.QUERY})"
    " DETACH DELETE q"
)

# Delete Table nodes defined in a file
DELETE_TABLES_FOR_FILE = (
    f"MATCH (f:{NodeLabel.FILE} {{path: $path}})"
    f"<-[:{RelType.DEFINED_IN}]-(t:{NodeLabel.TABLE})"
    " DETACH DELETE t"
)

# Delete the File node itself
DELETE_FILE = f"MATCH (f:{NodeLabel.FILE} {{path: $path}}) DETACH DELETE f"

# Find views that depend on tables defined in a file
STALE_VIEWS_QUERY = (
    f"MATCH (f:{NodeLabel.FILE} {{path: $path}})"
    f"<-[:{RelType.DEFINED_IN}]-(t:{NodeLabel.TABLE})"
    f"<-[:{RelType.SELECTS_FROM}]-(q:{NodeLabel.QUERY})"
    f"-[:{RelType.DECLARES}]->(v:{NodeLabel.TABLE} {{kind: 'VIEW'}})"
    " RETURN DISTINCT v.qualified AS view_name"
)

# Get all files in a repo by path prefix
INDEX_REPO_FILES_QUERY = (
    "MATCH (f:File) WHERE f.path STARTS WITH $repo_prefix RETURN f.path AS path"
)

# Trace upstream lineage of a column
TRACE_COLUMN_LINEAGE_QUERY = (
    "MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn) "
    "RETURN src.id AS id, src.col_name AS col_name"
)

# Find table usages in queries
FIND_TABLE_USAGES_QUERY = (
    "MATCH (t:SqlTable {name: $name})<-[:SELECTS_FROM]-(q:SqlQuery)"
    "-[:QUERY_DEFINED_IN]->(f:File) "
    "RETURN f.path AS file, q.sql AS sql, q.kind AS kind"
)

# Get downstream column dependencies
GET_DOWNSTREAM_DEPENDENCIES_QUERY = (
    "MATCH (src:SqlColumn {id: $id})-[:COLUMN_LINEAGE]->(dst:SqlColumn) "
    "RETURN dst.id AS id, dst.col_name AS col_name"
)

# Get upstream column dependencies
GET_UPSTREAM_DEPENDENCIES_QUERY = (
    "MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn) "
    "RETURN src.id AS id, src.col_name AS col_name"
)

# Search SQL patterns in indexed queries
SEARCH_SQL_PATTERN_QUERY = (
    "MATCH (q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File) "
    "WHERE contains(q.sql, $query) "
    "RETURN f.path AS file, q.sql AS sql, q.kind AS kind "
    "LIMIT $limit"
)

# List dialects and repos
LIST_DIALECTS_AND_REPOS_QUERY = (
    "MATCH (r:Repo)<-[:BELONGS_TO]-(f:File) "
    "RETURN r.path AS path, r.name AS name, collect(DISTINCT f.dialect) AS dialects"
)
