-- Repo node: one per indexed repository
CREATE NODE TABLE Repo (
    path STRING PRIMARY KEY,
    name STRING
);

-- File node: one per .sql file
CREATE NODE TABLE File (
    path STRING PRIMARY KEY,
    repo_path STRING,
    sha STRING,
    dialect STRING,
    parse_failed BOOLEAN,
    parse_cause STRING
);

-- Table node: one per unique table reference
CREATE NODE TABLE SqlTable (
    qualified STRING PRIMARY KEY,
    catalog STRING,
    db STRING,
    name STRING,
    kind STRING,
    defined_in_file STRING
);

-- Column node: one per unique column reference
CREATE NODE TABLE SqlColumn (
    id STRING PRIMARY KEY,
    catalog STRING,
    db STRING,
    table_name STRING,
    col_name STRING,
    table_qualified STRING
);

-- Query node: one per SQL statement parsed
CREATE NODE TABLE SqlQuery (
    id STRING PRIMARY KEY,
    file_path STRING,
    statement_index INT64,
    sql STRING,
    kind STRING,
    target_table STRING,
    parse_failed BOOLEAN,
    confidence FLOAT,
    parsing_mode STRING,
    start_line INT64
);

-- File -> Repo: file belongs to this repository
CREATE REL TABLE BELONGS_TO (
    FROM File TO Repo
);

-- File -> Table: table is defined in this file
CREATE REL TABLE DEFINED_IN (
    FROM SqlTable TO File
);

-- Query -> File: query is defined in this file
CREATE REL TABLE QUERY_DEFINED_IN (
    FROM SqlQuery TO File
);

-- Table -> Column: table has this column
CREATE REL TABLE HAS_COLUMN (
    FROM SqlTable TO SqlColumn,
    source STRING
);

-- Query -> Table: query selects from table
CREATE REL TABLE SELECTS_FROM (
    FROM SqlQuery TO SqlTable
);

-- Query -> Table: query inserts into table
CREATE REL TABLE INSERTS_INTO (
    FROM SqlQuery TO SqlTable
);

-- Query -> Table: query deletes from table
CREATE REL TABLE DELETES_FROM (
    FROM SqlQuery TO SqlTable
);

-- Query -> Table: query updates table
CREATE REL TABLE UPDATES (
    FROM SqlQuery TO SqlTable
);

-- Column -> Column: lineage relationship
CREATE REL TABLE COLUMN_LINEAGE (
    FROM SqlColumn TO SqlColumn,
    transform STRING,
    confidence FLOAT,
    query_id STRING
);

-- Query -> Table: query declares/creates this table
CREATE REL TABLE DECLARES (
    FROM SqlQuery TO SqlTable
);

-- Query -> Table: query does SELECT * (or alias.*) from this table
CREATE REL TABLE STAR_SOURCE (
    FROM SqlQuery TO SqlTable,
    qualifier STRING,
    target_table STRING,
    confidence FLOAT
);

-- Schema version tracking
CREATE NODE TABLE SchemaVersion (
    version STRING PRIMARY KEY,
    indexed_sha STRING
);

-- External consumer node: one per declared downstream consumer in .sqlcg.toml
CREATE NODE TABLE ExternalConsumer (
    name STRING PRIMARY KEY,
    consumer_type STRING
);

-- Table -> ExternalConsumer: this table is consumed by an external system
CREATE REL TABLE CONSUMED_BY (
    FROM SqlTable TO ExternalConsumer
);
