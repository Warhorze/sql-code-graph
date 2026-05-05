# sql-code-graph

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
- `index_repo` — index or re-index a directory of SQL files
- `find_table_usages` — find all queries that read a table
- `trace_column_lineage` — trace where a column's value comes from
- `get_upstream_dependencies` / `get_downstream_dependencies` — dependency chains
- `search_sql_pattern` — full-text search across all indexed SQL
- `execute_cypher` — raw graph query for advanced analysis
```

The MCP server works without this — Claude can discover the tools on its own —
but the CLAUDE.md snippet ensures they get used proactively.

## MCP tools reference

| Tool | Description |
|------|-------------|
| `index_repo(repo_path, dialect)` | Index a directory of SQL files |
| `trace_column_lineage(table_col)` | Trace column lineage upstream |
| `find_table_usages(table_name)` | Find all queries that read a table |
| `get_upstream_dependencies(table_col)` | Full upstream dependency chain |
| `get_downstream_dependencies(table_col)` | Full downstream dependency chain |
| `search_sql_pattern(query)` | Full-text search across indexed SQL |
| `list_dialects_and_repos()` | List indexed repos and dialects |
| `execute_cypher(query)` | Raw Cypher query against the graph |

## CLI reference

Full option reference: [docs/cli.md](docs/cli.md)

```bash
sqlcg install                          # register MCP server in Claude Code
sqlcg db init                          # initialise graph database
sqlcg index <path> --dialect <d>       # index SQL files
sqlcg index <path> --dialect auto      # read dialect from .sqlcg.toml
sqlcg watch <path>                     # watch for file changes
sqlcg git install-hooks                # install post-checkout hook
sqlcg gain                             # show usage metrics
sqlcg report                           # generate FP/error report
sqlcg mcp start                        # start MCP server manually
sqlcg version                          # show installed version
```

## Supported dialects

`snowflake` · `bigquery` · `postgres` · `ansi` · `tsql` · `dbt` (via optional extra)

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
