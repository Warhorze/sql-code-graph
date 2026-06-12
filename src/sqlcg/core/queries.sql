-- DuckDB SQL query library.
-- Format identical to queries.cypher: blocks separated by "-- BLOCK_NAME" lines.
-- All queries use ? positional parameters (list order matches the named params below).
-- Named params in comments are for documentation — callers pass values as a list.

-- DELETE_COLUMNS_FOR_FILE
-- params: [path, path]
DELETE FROM "SqlColumn" WHERE id IN (
  SELECT hc.dst_key FROM "HAS_COLUMN" hc
  WHERE hc.src_key IN (
    SELECT di.src_key FROM "DEFINED_IN" di WHERE di.dst_key = ?
  )
)

-- DELETE_QUERIES_FOR_FILE
-- params: [path]
DELETE FROM "SqlQuery" WHERE file_path = ?

-- DELETE_TABLES_FOR_FILE
-- params: [path, path]
DELETE FROM "SqlTable" WHERE qualified IN (
  SELECT di.src_key FROM "DEFINED_IN" di WHERE di.dst_key = ?
)

-- DELETE_FILE
-- params: [path]
DELETE FROM "File" WHERE path = ?

-- INDEX_REPO_FILES
-- params: [repo_prefix]
SELECT path FROM "File" WHERE path LIKE ? || '%'

-- TRACE_COLUMN_LINEAGE
-- params: [id]
SELECT
  src.id         AS id,
  src.col_name   AS col_name,
  src.table_qualified AS table_qualified,
  cl.transform   AS transform,
  cl.confidence  AS confidence,
  q.file_path    AS file,
  q.start_line   AS line,
  q.sql          AS expression,
  t.kind         AS table_kind
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
LEFT JOIN "SqlQuery" q ON q.id = cl.query_id
LEFT JOIN "SqlTable" t ON t.qualified = src.table_qualified
WHERE cl.dst_key = ?

-- FIND_TABLE_USAGES
-- params: [name]
SELECT f.path AS file, q.sql AS sql, q.kind AS kind
FROM "SqlTable" t
JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified
JOIN "SqlQuery" q ON q.id = sf.src_key
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
JOIN "File" f ON f.path = qdi.dst_key
WHERE t.name = ?

-- GET_DOWNSTREAM_DEPENDENCIES
-- params: [id]
SELECT dst.id AS id, dst.col_name AS col_name, dst.table_qualified AS table_qualified
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" dst ON dst.id = cl.dst_key
WHERE cl.src_key = ?

-- GET_UPSTREAM_DEPENDENCIES
-- params: [id]
SELECT src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
WHERE cl.dst_key = ?

-- SEARCH_SQL_PATTERN
-- params: [query, limit]
SELECT f.path AS file, q.sql AS sql, q.kind AS kind
FROM "SqlQuery" q
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
JOIN "File" f ON f.path = qdi.dst_key
WHERE q.sql LIKE '%' || ? || '%'
LIMIT ?

-- LIST_DIALECTS_AND_REPOS
-- params: []
SELECT r.path AS path, r.name AS name,
       list(DISTINCT f.dialect) AS dialects
FROM "Repo" r
JOIN "BELONGS_TO" bt ON bt.dst_key = r.path
JOIN "File" f ON f.path = bt.src_key
GROUP BY r.path, r.name

-- EXPAND_STAR_SOURCES
-- Inserts new SqlColumn destination nodes and COLUMN_LINEAGE edges from STAR_SOURCE.
-- Returns count of new edges created.
-- params: []
INSERT OR REPLACE INTO "SqlColumn" (id, col_name, table_qualified, catalog, db, table_name)
SELECT DISTINCT
  q.target_table || '.' || c.col_name AS id,
  c.col_name,
  q.target_table AS table_qualified,
  tgt.catalog,
  tgt.db,
  tgt.name AS table_name
FROM "STAR_SOURCE" ss
JOIN "SqlQuery" q ON q.id = ss.src_key
JOIN "SqlTable" t ON t.qualified = ss.dst_key
JOIN "HAS_COLUMN" hc ON hc.src_key = t.qualified
JOIN "SqlColumn" c ON c.id = hc.dst_key
JOIN "SqlTable" tgt ON tgt.qualified = q.target_table
WHERE q.target_table <> ''
  AND q.target_table <> t.qualified

-- EXPAND_STAR_SOURCES_HAS_COLUMN
-- Insert HAS_COLUMN edges for the new destination columns.
-- params: []
INSERT OR REPLACE INTO "HAS_COLUMN" (src_key, dst_key, source)
SELECT DISTINCT
  q.target_table AS src_key,
  q.target_table || '.' || c.col_name AS dst_key,
  'star_expansion' AS source
FROM "STAR_SOURCE" ss
JOIN "SqlQuery" q ON q.id = ss.src_key
JOIN "SqlTable" t ON t.qualified = ss.dst_key
JOIN "HAS_COLUMN" hc ON hc.src_key = t.qualified
JOIN "SqlColumn" c ON c.id = hc.dst_key
JOIN "SqlTable" tgt ON tgt.qualified = q.target_table
WHERE q.target_table <> ''
  AND q.target_table <> t.qualified

-- EXPAND_STAR_SOURCES_LINEAGE
-- Insert COLUMN_LINEAGE edges for the star expansion.
-- params: []
INSERT OR REPLACE INTO "COLUMN_LINEAGE" (src_key, dst_key, transform, confidence, query_id, inferred_from_source_name)
SELECT DISTINCT
  c.id AS src_key,
  q.target_table || '.' || c.col_name AS dst_key,
  'STAR_EXPANSION' AS transform,
  0.8 AS confidence,
  q.id AS query_id,
  FALSE AS inferred_from_source_name
FROM "STAR_SOURCE" ss
JOIN "SqlQuery" q ON q.id = ss.src_key
JOIN "SqlTable" t ON t.qualified = ss.dst_key
JOIN "HAS_COLUMN" hc ON hc.src_key = t.qualified
JOIN "SqlColumn" c ON c.id = hc.dst_key
JOIN "SqlTable" tgt ON tgt.qualified = q.target_table
WHERE q.target_table <> ''
  AND q.target_table <> t.qualified

-- COUNT_STAR_SOURCES
-- params: []
SELECT count(*) AS n FROM "STAR_SOURCE"

-- COUNT_STAR_EXPANSIONS
-- params: []
SELECT count(*) AS n FROM "COLUMN_LINEAGE" WHERE transform = 'STAR_EXPANSION'

-- FIND_DEFINITION
-- params: [table_qualified]
SELECT t.qualified AS table_qualified, t.kind AS kind,
       t.defined_in_file AS defined_in_file, f.path AS file_path
FROM "SqlTable" t
JOIN "DEFINED_IN" di ON di.src_key = t.qualified
JOIN "File" f ON f.path = di.dst_key
WHERE t.qualified = ?

-- GET_TABLE_DEFINING_FILES
-- params: [table_qualified]
SELECT f.path AS file_path, t.kind AS kind
FROM "SqlTable" t
JOIN "DEFINED_IN" di ON di.src_key = t.qualified
JOIN "File" f ON f.path = di.dst_key
WHERE t.qualified = ?

-- GET_PRODUCER_FILES_FOR_TABLE
-- ETL INSERT...SELECT producers populate a table without a DEFINED_IN edge
-- (that edge is DDL-only). Resolve SqlQuery.target_table -> QUERY_DEFINED_IN -> File
-- so get_definition / get_change_scope can also surface "populated here" producer
-- files, not just "defined here" DDL files (mirror of GET_TARGET_TABLES_FOR_FILE,
-- the reverse-direction lookup). table_qualified is stored lowercase.
-- params: [table_qualified]
SELECT DISTINCT f.path AS file_path
FROM "SqlQuery" q
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
JOIN "File" f ON f.path = qdi.dst_key
WHERE q.target_table = ?

-- GET_TABLE_DIRECT_UPSTREAMS
-- params: [table_qualified, table_qualified]
SELECT DISTINCT src.qualified AS upstream_table, f.path AS in_file
FROM "SqlQuery" q
JOIN "SELECTS_FROM" sf ON sf.src_key = q.id
JOIN "SqlTable" src ON src.qualified = sf.dst_key
LEFT JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
LEFT JOIN "File" f ON f.path = qdi.dst_key
WHERE q.target_table = ?
  AND src.qualified <> ?

-- GET_COLUMNS_FOR_TABLE
-- params: [table_qualified]
SELECT c.id AS col_id, c.col_name AS col_name
FROM "SqlTable" t
JOIN "HAS_COLUMN" hc ON hc.src_key = t.qualified
JOIN "SqlColumn" c ON c.id = hc.dst_key
WHERE t.qualified = ?

-- GET_TABLES_DEFINED_IN_FILE
-- params: [file_path]
SELECT t.qualified AS table_qualified
FROM "SqlTable" t
JOIN "DEFINED_IN" di ON di.src_key = t.qualified
WHERE di.dst_key = ?

-- GET_TARGET_TABLES_FOR_FILE
-- ETL INSERT...SELECT producers populate a table without a DEFINED_IN edge
-- (that edge is DDL-only). Resolve query->file QUERY_DEFINED_IN -> SqlQuery.target_table
-- so diff_impact can also see "populated here" producers, not just "defined here" DDL.
-- params: [file_path]
SELECT DISTINCT q.target_table AS table_qualified
FROM "SqlQuery" q
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
WHERE qdi.dst_key = ?
  AND q.target_table <> ''

-- GET_TABLE_ADJACENCY_FOR_COLUMNS
-- Aggregate table-level producer->consumer adjacency derived from COLUMN_LINEAGE,
-- restricted to a closure's column-id set (Option A — issue #38 backfill fix).
-- Replaces the N x GET_TABLE_DIRECT_UPSTREAMS loop with a single query: CTE-wrapped
-- INSERT...SELECT statements emit no SELECTS_FROM adjacency at all (the real source
-- table is nested in the CTE child scope and never surfaces into the statement's
-- top-level sources). The raw COLUMN_LINEAGE topology bridges producer -> consumer
-- via a TWO-HOP path through the synthetic cte/derived node (producer -> cte ->
-- consumer, not a parallel direct edge) — this query returns ALL rolled-up table
-- pairs (including synthetic endpoints); the caller contracts synthetic-node hops
-- into direct real-table adjacency in one pass over this small edge set (still
-- ONCE per closure, never per-table/per-column). Also returns each endpoint's
-- SqlTable.kind so the caller can identify synthetic nodes without a second query.
-- params: [col_ids, col_ids]
SELECT DISTINCT
  src.table_qualified AS upstream_table,
  ut.kind AS upstream_kind,
  dst.table_qualified AS downstream_table,
  dt.kind AS downstream_kind
FROM "COLUMN_LINEAGE" cl
JOIN "SqlColumn" src ON src.id = cl.src_key
JOIN "SqlColumn" dst ON dst.id = cl.dst_key
LEFT JOIN "SqlTable" ut ON ut.qualified = src.table_qualified
LEFT JOIN "SqlTable" dt ON dt.qualified = dst.table_qualified
WHERE cl.src_key = ANY(?)
  AND cl.dst_key = ANY(?)
  AND src.table_qualified <> dst.table_qualified

-- GET_TABLE_KINDS_BATCH
-- Batch lookup of SqlTable.kind for a set of qualified names — the authoritative
-- marker for synthetic cte/derived nodes (issue #38 synthetic-node-leak fix).
-- params: [table_qualifieds]
SELECT t.qualified AS table_qualified, t.kind AS kind
FROM "SqlTable" t
WHERE t.qualified = ANY(?)

-- ANALYZE_UNUSED_TABLES
-- params: []
SELECT t.qualified AS table_qualified
FROM "SqlTable" t
WHERE t.qualified NOT IN (SELECT DISTINCT dst_key FROM "SELECTS_FROM")
ORDER BY t.qualified

-- HUB_RANKING
-- params: [k]
SELECT t.qualified AS table_qualified,
       count(DISTINCT q.target_table) AS downstream_dependents
FROM "SqlTable" t
JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified
JOIN "SqlQuery" q ON q.id = sf.src_key
WHERE q.target_table <> ''
  AND q.target_table <> t.qualified
GROUP BY t.qualified
ORDER BY downstream_dependents DESC, t.qualified
LIMIT ?

-- DEPENDENT_FILES_OF_TABLES
-- params: [tables]  (list — caller must expand with unnest or IN clause)
SELECT DISTINCT f.path AS path
FROM "SqlTable" t
JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified
JOIN "SqlQuery" q ON q.id = sf.src_key
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
JOIN "File" f ON f.path = qdi.dst_key
WHERE t.qualified = ANY(?)

-- GET_TABLE_EXTERNAL_CONSUMERS
-- params: [table_qualified]
SELECT e.name AS name, e.consumer_type AS consumer_type
FROM "SqlTable" t
JOIN "CONSUMED_BY" cb ON cb.src_key = t.qualified
JOIN "ExternalConsumer" e ON e.name = cb.dst_key
WHERE t.qualified = ?

-- GET_TABLES_EXTERNAL_CONSUMERS_BATCH
-- params: [table_qualifieds]  (list)
SELECT t.qualified AS table_qualified, e.name AS name, e.consumer_type AS consumer_type
FROM "SqlTable" t
JOIN "CONSUMED_BY" cb ON cb.src_key = t.qualified
JOIN "ExternalConsumer" e ON e.name = cb.dst_key
WHERE t.qualified = ANY(?)

-- COUNT_EXTERNAL_CONSUMERS
-- params: []
SELECT count(*) AS n FROM "CONSUMED_BY"

-- GET_TABLE_READS_ADJACENCY
-- View 1 (row_empty_tables) adjacency: source_table -> dest_table derived from
-- queries that read FROM a source (SELECTS_FROM or STAR_SOURCE) and write to a
-- dest (SqlQuery.target_table). The query OUTPUT table is NOT an edge in the
-- graph (INSERTS_INTO/DECLARES are never written by the indexer) — so the table
-- adjacency must be derived by joining the read-edge to the query's target_table.
-- Empty-string sentinel excluded: target_table = '' marks bare-SELECT queries.
-- params: []
SELECT DISTINCT sf.dst_key AS source_table, q.target_table AS dest_table
FROM "SqlQuery" q
JOIN "SELECTS_FROM" sf ON sf.src_key = q.id
WHERE q.target_table <> '' AND q.target_table IS NOT NULL
UNION
SELECT DISTINCT ss.dst_key AS source_table, q.target_table AS dest_table
FROM "SqlQuery" q
JOIN "STAR_SOURCE" ss ON ss.src_key = q.id
WHERE q.target_table <> '' AND q.target_table IS NOT NULL
