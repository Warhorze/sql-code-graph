# sql-code-graph

> **Pre-1.0 — expect breaking changes.** APIs, CLI flags, and graph schema may
> change between releases without a deprecation period. Pin to an exact version
> in production. Re-indexing is always the migration path.

> **Dialect coverage.** This tool has been developed and tested against a
> production Snowflake data warehouse with ~1,400 SQL files. Other dialects
> (`bigquery`, `postgres`, `ansi`, `tsql`) are wired up but have not been
> exercised against a real corpus — edge cases will surface. If you use sqlcg
> with a non-Snowflake dialect and hit parse failures or missing lineage, please
> [open an issue](https://github.com/Warhorze/sql-code-graph/issues) with a
> minimal reproducer; we want to support other dialects but need a corpus to
> develop against.

SQL lineage and dependency analysis as an MCP server for Claude Code.

Indexes a directory of `.sql` files into a graph database and exposes lineage
queries as MCP tools — so Claude can answer questions like *"what tables does
this view depend on?"* or *"where is `orders.customer_id` derived from?"*
without reading every file.

## Quick start

Choose one:

**Permanent install** (recommended):
```bash
uv tool install sql-code-graph    # Fast, managed, no isolation needed
sqlcg install                     # Register MCP server in Claude Code
```

**One-shot try** (cold cache warning):
```bash
uvx sql-code-graph                # First run is slow (downloads deps)
                                  # Subsequent runs use cache, ~1s startup
```

Restart Claude Code, then inside your project ask:

```
Index my SQL files at ./sql --dialect snowflake
```

That's it. The MCP tools are now available to Claude in every conversation
for that project.

### Workflow (3 steps)

1. **Initialize**: `sqlcg db init`
2. **Index**: `sqlcg index ./sql --dialect snowflake`
3. **Keep fresh**: `sqlcg git install-hooks` (optional)

## Full setup (recommended)

```bash
# 1. Install
pip install sql-code-graph

# 2. Register with Claude Code (~/.claude/settings.json)
sqlcg install

# 3. Restart Claude Code

# 4. Index your SQL repo
# Only git-tracked files are indexed — build artefacts, node_modules,
# and .venv are ignored automatically.
sqlcg db init
sqlcg index ./sql --dialect snowflake   # or: bigquery, postgres, ansi

# 5. (Optional) Keep the graph fresh on branch switches
cd /your/sql/repo
sqlcg git install-hooks
```

Step 5 installs a `post-checkout` git hook that re-indexes automatically
whenever you switch branches. Without it the graph may be stale after a
`git checkout` until you re-run `sqlcg index` manually.

## Dialect config

To avoid passing `--dialect` every time, create `.sqlcg.toml` in your repo root:

```toml
[sqlcg]
dialect = "snowflake"   # snowflake | bigquery | postgres | ansi
```

The git hook and `sqlcg index --dialect auto` both read this file.

## Add to your project CLAUDE.md (recommended)

Adding a short note to your project's `CLAUDE.md` helps Claude know the tools
are available and when to use them:

```markdown
## SQL lineage
This project uses sql-code-graph. MCP tools are available:
- `db_info` — check graph health and parse quality before running lineage queries
- `index_repo` — index or re-index a directory of SQL files
- `find_table_usages` — find all queries that read a table
- `trace_column_lineage` — trace where a column's value comes from
- `get_upstream_dependencies` / `get_downstream_dependencies` — dependency chains
- `search_sql_pattern` — full-text search across all indexed SQL
- `execute_cypher` — raw graph query for advanced analysis
```

The MCP server works without this — Claude can discover the tools on its own —
but the CLAUDE.md snippet ensures they get used proactively.

## Parse quality

After indexing, `sqlcg gain` shows a **parse quality breakdown** that tells you how
much column-level lineage was extracted:

| Quality | Meaning | Tools affected |
|---|---|---|
| `FULL` | Column-level lineage extracted | All tools work |
| `TABLE_ONLY` | Table edges only — no column lineage | `trace_column_lineage`, `get_*_dependencies` return empty |
| `SCRIPTING_FALLBACK` | sqlglot fell back to raw command node | Partial table edges; column lineage unavailable |
| `FAILED` | File failed to parse entirely | File invisible to all queries |

Quality is shown per-file after `sqlcg index` and in `sqlcg gain` Section F.
`list_dialects_and_repos()` warns when scripting fallback exceeds 20% of queries.

**What causes TABLE_ONLY?** Mostly `SELECT *` — sqlglot can't trace column names through
a wildcard without knowing the source table's columns. See [Resolving SELECT *](#resolving-select-)
below to fix this without rewriting your ETLs.

**What causes SCRIPTING_FALLBACK?** Snowflake `$$` procedure bodies or `BEGIN…END` scripting
blocks. sqlglot parses the block as a raw `Command` node and extracts DML via tokenizer
fallback. Table edges are usually correct; column edges are not.

Check `sqlcg db info` for the parsing mode distribution across all indexed queries.

## Resolving SELECT *

By default, `SELECT *` ETLs produce `TABLE_ONLY` parse quality because sqlglot needs
to know the source table's column list to expand the wildcard. The fastest fix is to
provide your production information schema as a CSV — sqlcg uses it as the authoritative
column source, replacing any DDL-inferred columns.

**Step 1 — Export from Snowflake** (run in a worksheet, export as CSV):

```sql
SELECT
    TABLE_CATALOG,
    TABLE_SCHEMA,
    TABLE_NAME,
    COLUMN_NAME,
    ORDINAL_POSITION,
    DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
```

**Step 2 — Drop it in your repo**:

```bash
cp ~/Downloads/columns.csv <your-sql-repo>/.sqlcg/schema.csv
```

**Step 3 — Index your ETL folder only**:

```bash
sqlcg index etl/
```

That's the entire workflow. `sqlcg index` auto-discovers `.sqlcg/schema.csv` and loads
it before indexing — no separate command needed. Point it at your ETL folder rather
than the full repo: fewer files to scan, faster indexing, and the schema CSV covers
what DDL parsing was doing anyway.

<!-- TODO: add benchmark numbers after testing — expected savings from ETL-folder-only
     indexing vs full-repo scan (file count ratio, wall-clock time, star edges resolved) -->

After indexing, `sqlcg db info` will show non-zero `STAR_EXPANSION lineage edges`,
and `trace_column_lineage` will return results for queries that previously returned
empty.

**Alternative: explicit load**

If your schema CSV lives elsewhere or you want to refresh it without re-indexing:

```bash
sqlcg load-schema /path/to/columns.csv   # load or refresh schema
sqlcg index etl/                         # re-index using updated schema
```

**Precedence**: production CSV > DDL files > nothing. Partial CSVs are fine — tables
not covered fall back to DDL-inferred columns automatically.

## MCP tools reference

| Tool | Description |
|------|-------------|
| `index_repo(repo_path, dialect)` | Index a directory of SQL files |
| `trace_column_lineage(table_col)` | Trace column lineage upstream |
| `find_table_usages(table_name)` | Find all queries that read a table |
| `get_upstream_dependencies(table_col)` | Full upstream dependency chain |
| `get_downstream_dependencies(table_col)` | Full downstream dependency chain |
| `search_sql_pattern(query)` | Full-text search across indexed SQL |
| `list_dialects_and_repos()` | List indexed repos and dialects (catalogue) |
| `db_info()` | Graph health, node counts, parse quality breakdown, warnings |
| `execute_cypher(query)` | Raw Cypher query against the graph |

> **LLM agent tip**: call `db_info()` before lineage queries to check that
> `SqlColumn > 0` and `warnings` is empty. If `parse_quality["scripting_block"]`
> is high, column lineage will be limited for those files — use table-level tools
> (`find_table_usages`, `get_*_dependencies`) instead.

## CLI reference

Full option reference: [docs/cli.md](docs/cli.md)

```bash
sqlcg install                          # register MCP server in Claude Code
sqlcg db init                          # initialise graph database
sqlcg index <path> --dialect <d>       # index SQL files
sqlcg index <path> --dialect auto      # read dialect from .sqlcg.toml
sqlcg load-schema <csv>                # load production schema (auto-discovered from .sqlcg/schema.csv)
sqlcg load-schema <csv> --include-catalog  # use 3-part names (CATALOG.SCHEMA.TABLE)
sqlcg watch <path>                     # watch for file changes
sqlcg git install-hooks                # install post-checkout hook
sqlcg gain                             # show usage metrics
sqlcg report                           # generate FP/error report
sqlcg mcp start                        # start MCP server manually
sqlcg version                          # show installed version
```

## Supported dialects

| Dialect | Status |
|---------|--------|
| `snowflake` | Tested against a production DWH (~1,400 files) |
| `bigquery` | Wired up, untested against a real corpus |
| `postgres` | Wired up, untested against a real corpus |
| `ansi` | Wired up, untested against a real corpus |
| `tsql` | Wired up, untested against a real corpus |
| `dbt` | Via optional extra, untested against a real corpus |

If you use a non-Snowflake dialect and run into issues, open an issue with a
minimal SQL reproducer. Contributions of test fixtures from real (anonymised)
corpora are especially welcome.

## Development

```bash
git clone https://github.com/Warhorze/sql-code-graph
cd sql-code-graph
uv sync --all-extras
uv run pytest tests/unit
```

## Issues

Bug reports and feature requests: [github.com/Warhorze/sql-code-graph/issues](https://github.com/Warhorze/sql-code-graph/issues)

Questions and discussion: [github.com/Warhorze/sql-code-graph/discussions](https://github.com/Warhorze/sql-code-graph/discussions)

## License

MIT
