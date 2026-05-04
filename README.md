# SQL Code Graph (sqlcg)

A self-contained MCP server for analyzing SQL code and building lineage graphs.

## Features

- Parse SQL files across multiple dialects (ANSI, Snowflake, BigQuery, Postgres, T-SQL)
- Build a queryable graph database of table and column lineage
- Expose lineage queries via MCP tools for use with Claude
- Support incremental re-indexing with file watching
- Integration with dbt projects for schema enrichment

## Installation

```bash
uv pip install -e .
```

## Quick Start

```bash
# Initialize the graph database
sqlcg db init

# Index a directory of SQL files
sqlcg index /path/to/sql/files --dialect snowflake

# Watch for changes
sqlcg watch /path/to/sql/files --dialect snowflake

# Query lineage
sqlcg find table my_table
sqlcg analyze upstream schema.my_table.column_name

# Start MCP server
sqlcg mcp start
```

## Development

Install development dependencies:

```bash
uv pip install -e ".[neo4j,dbt,snowflake]"
uv pip install -e ".[dev]"
```

Run tests:

```bash
pytest tests/
```

## License

MIT
