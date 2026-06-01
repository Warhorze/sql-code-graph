-- DELETE_COLUMNS_FOR_FILE
MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:SqlTable)-[:HAS_COLUMN]->(c:SqlColumn)
DETACH DELETE c

-- DELETE_QUERIES_FOR_FILE
MATCH (f:File {path: $path})<-[:QUERY_DEFINED_IN]-(q:SqlQuery)
DETACH DELETE q

-- DELETE_TABLES_FOR_FILE
MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:SqlTable)
DETACH DELETE t

-- DELETE_FILE
MATCH (f:File {path: $path}) DETACH DELETE f

-- INDEX_REPO_FILES
MATCH (f:File) WHERE f.path STARTS WITH $repo_prefix RETURN f.path AS path

-- TRACE_COLUMN_LINEAGE
MATCH (dst:SqlColumn {id: $id})<-[r:COLUMN_LINEAGE]-(src:SqlColumn)
OPTIONAL MATCH (q:SqlQuery {id: r.query_id})
OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified})
RETURN src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified,
       r.transform AS transform, r.confidence AS confidence,
       q.file_path AS file, q.start_line AS line, q.sql AS expression,
       t.kind AS table_kind

-- FIND_TABLE_USAGES
MATCH (t:SqlTable {name: $name})<-[:SELECTS_FROM]-(q:SqlQuery)
-[:QUERY_DEFINED_IN]->(f:File)
RETURN f.path AS file, q.sql AS sql, q.kind AS kind

-- GET_DOWNSTREAM_DEPENDENCIES
MATCH (src:SqlColumn {id: $id})-[:COLUMN_LINEAGE]->(dst:SqlColumn)
RETURN dst.id AS id, dst.col_name AS col_name, dst.table_qualified AS table_qualified

-- GET_UPSTREAM_DEPENDENCIES
MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
RETURN src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified

-- SEARCH_SQL_PATTERN
MATCH (q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)
WHERE contains(q.sql, $query)
RETURN f.path AS file, q.sql AS sql, q.kind AS kind
LIMIT $limit

-- LIST_DIALECTS_AND_REPOS
MATCH (r:Repo)<-[:BELONGS_TO]-(f:File)
RETURN r.path AS path, r.name AS name, collect(DISTINCT f.dialect) AS dialects

-- EXPAND_STAR_SOURCES
MATCH (q:SqlQuery)-[s:STAR_SOURCE]->(t:SqlTable)-[:HAS_COLUMN]->(c:SqlColumn)
WHERE q.target_table <> ''
MATCH (tgt:SqlTable {qualified: q.target_table})
MERGE (dst:SqlColumn {id: q.target_table + '.' + c.col_name})
  ON CREATE SET dst.col_name = c.col_name,
                dst.table_qualified = q.target_table,
                dst.catalog = tgt.catalog,
                dst.db = tgt.db,
                dst.table_name = tgt.name
MERGE (tgt)-[:HAS_COLUMN]->(dst)
MERGE (c)-[r:COLUMN_LINEAGE]->(dst)
  ON CREATE SET r.transform = 'STAR_EXPANSION',
                r.confidence = 0.8,
                r.query_id = q.id
RETURN count(r) AS edges_created

-- COUNT_STAR_SOURCES
MATCH ()-[r:STAR_SOURCE]->() RETURN count(r) AS n

-- COUNT_STAR_EXPANSIONS
MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n

-- FIND_DEFINITION
MATCH (t:SqlTable {qualified: $table_qualified})-[:DEFINED_IN]->(f:File)
RETURN t.qualified AS table_qualified, t.kind AS kind, t.defined_in_file AS defined_in_file, f.path AS file_path

-- GET_TABLE_DEFINING_FILES
MATCH (t:SqlTable {qualified: $table_qualified})-[:DEFINED_IN]->(f:File)
RETURN f.path AS file_path, t.kind AS kind

-- GET_TABLE_DIRECT_UPSTREAMS
MATCH (q:SqlQuery {target_table: $table_qualified})-[:SELECTS_FROM]->(src:SqlTable)
WHERE src.qualified <> $table_qualified
OPTIONAL MATCH (q)-[:QUERY_DEFINED_IN]->(f:File)
RETURN DISTINCT src.qualified AS upstream_table, f.path AS in_file

-- GET_COLUMNS_FOR_TABLE
MATCH (t:SqlTable {qualified: $table_qualified})-[:HAS_COLUMN]->(c:SqlColumn)
RETURN c.id AS col_id, c.col_name AS col_name

-- GET_TABLES_DEFINED_IN_FILE
MATCH (f:File {path: $file_path})<-[:DEFINED_IN]-(t:SqlTable)
RETURN t.qualified AS table_qualified

-- ANALYZE_UNUSED_TABLES
MATCH (t:SqlTable)
WHERE NOT (t)<-[:SELECTS_FROM]-()
RETURN t.qualified AS table_qualified
ORDER BY t.qualified

-- HUB_RANKING
MATCH (t:SqlTable)<-[:SELECTS_FROM]-(q:SqlQuery)
WHERE q.target_table <> ''
WITH t.qualified AS table_qualified, q.target_table AS consumer_table
WHERE consumer_table <> table_qualified
RETURN table_qualified, count(DISTINCT consumer_table) AS downstream_dependents
ORDER BY downstream_dependents DESC, table_qualified
LIMIT $k

-- DEPENDENT_FILES_OF_TABLES
UNWIND $tables AS tbl
MATCH (t:SqlTable {qualified: tbl})<-[:SELECTS_FROM]-(q:SqlQuery)-[:QUERY_DEFINED_IN]->(f:File)
RETURN DISTINCT f.path AS path

-- GET_TABLE_EXTERNAL_CONSUMERS
MATCH (t:SqlTable {qualified: $table_qualified})-[:CONSUMED_BY]->(e:ExternalConsumer)
RETURN e.name AS name, e.consumer_type AS consumer_type

-- GET_TABLES_EXTERNAL_CONSUMERS_BATCH
UNWIND $table_qualifieds AS tq
MATCH (t:SqlTable {qualified: tq})-[:CONSUMED_BY]->(e:ExternalConsumer)
RETURN tq AS table_qualified, e.name AS name, e.consumer_type AS consumer_type

-- COUNT_EXTERNAL_CONSUMERS
MATCH ()-[r:CONSUMED_BY]->() RETURN count(r) AS n
