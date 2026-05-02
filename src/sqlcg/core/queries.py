"""Centralized Cypher query strings for graph operations."""

from sqlcg.core.schema import NodeLabel, RelType

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
