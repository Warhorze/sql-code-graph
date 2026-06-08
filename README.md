# sql-code-graph

> **No backward-compatibility guarantees.** APIs, CLI flags, and graph schema may
> change between releases without a deprecation period. Pin to an exact version
> in production. Re-indexing is always the migration path.

> **Dialect support.** sqlcg is built on [sqlglot](https://github.com/tobymao/sqlglot),
> so in theory it can work for any dialect sqlglot parses. In practice we've only
> tested it against a production **Snowflake** warehouse (~1,400 SQL files) — and
> getting that one dialect working properly was already challenging. Other dialects
> (`bigquery`, `postgres`, `ansi`, `tsql`, `dbt`) may well work, but their lineage
> hasn't been validated, so expect rough edges. We'd gladly collaborate to get
> another dialect integrated — [open an issue](https://github.com/Warhorze/sql-code-graph/issues)
> with a minimal, anonymised corpus we can test against.

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

# 2. Register with Claude Code (writes ~/.claude.json)
sqlcg install

# 3. Restart Claude Code

# 4. Index your SQL repo
# Only git-tracked files are indexed — build artefacts, node_modules,
# and .venv are ignored automatically.
sqlcg db init
sqlcg index ./sql --dialect snowflake   # snowflake is the only tested dialect

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
dialect = "snowflake"   # the only tested dialect
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
- `execute_sql` — raw read-only SQL query for advanced analysis
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
below for how sqlcg expands wildcards automatically from your DDL and CTAS bodies.

**What causes SCRIPTING_FALLBACK?** Snowflake `$$` procedure bodies or `BEGIN…END` scripting
blocks. sqlglot parses the block as a raw `Command` node and extracts DML via tokenizer
fallback. Table edges are usually correct; column edges are not.

Check `sqlcg db info` for the parsing mode distribution across all indexed queries.

## Resolving SELECT *

A `SELECT *` ETL produces `TABLE_ONLY` parse quality because sqlglot needs the source
table's column list to expand the wildcard into individual columns. sqlcg resolves this
**automatically, with no extra setup** — there is no CSV to export or command to run.

Wildcards are expanded from two sources harvested while indexing:

1. **DDL files** — `CREATE TABLE` / `CREATE VIEW` statements give sqlcg the column list
   for any table they define.
2. **Cross-file CTAS bodies** — a `CREATE TABLE … AS SELECT` (or CTE) in one file is used
   to resolve `SELECT *` against that table in another file.

So the only thing you need to do is **index the DDL alongside your ETLs** — point `sqlcg
index` at a path that contains both (e.g. the repo root, or both `ddl/` and `etl/`). The
more of your `CREATE` statements sqlcg can see, the more wildcards it resolves.

```bash
sqlcg index . --dialect snowflake   # index DDL + ETLs together
```

After indexing, `sqlcg db info` shows non-zero `STAR_EXPANSION lineage edges`, and
`trace_column_lineage` returns results for queries that previously returned empty.

> **Note:** earlier versions accepted an exported `INFORMATION_SCHEMA` CSV (`sqlcg
> load-schema`). That path was **removed** — profiling showed it added zero lineage
> edges over DDL + cross-file CTAS resolution on a real warehouse. DDL is now the
> single source of column truth; no CSV is needed or accepted.

## MCP tools reference

| Tool | Description |
|------|-------------|
| `index_repo(repo_path, dialect)` | Index a directory of SQL files |
| **Lineage & dependencies** | |
| `trace_column_lineage(table_col)` | Trace a column's value upstream to its sources |
| `get_upstream_dependencies(table_col)` | Full upstream dependency chain |
| `get_downstream_dependencies(table_col)` | Full downstream dependency chain |
| `find_table_usages(table_name)` | Find all queries that read a table |
| `find_definition(table_qualified)` | Find where a table/view is defined |
| **Change impact** | |
| `get_change_scope(table_qualified)` | Blast radius of changing a table (impact + risk) |
| `diff_impact(changed_files)` | What a set of changed files affects downstream |
| `get_backfill_order(table_qualified)` | Topological rebuild/backfill order |
| `scope_change(target)` | Synthesised change-scope summary for a target |
| **Analysis** | |
| `get_hub_ranking(k)` | Top-k tables by downstream dependent count (hub/centrality) |
| `analyze_unused()` | Tables with no within-corpus consumers (dead-code candidates, heuristic) |
| **Search & meta** | |
| `search_sql_pattern(query)` | Full-text search across indexed SQL |
| `list_dialects_and_repos()` | List indexed repos and dialects (catalogue) |
| `db_info()` | Graph health, node counts, parse quality breakdown, warnings, freshness (indexed SHA vs HEAD) |
| `execute_sql(query)` | Raw read-only SQL query against the graph |
| `submit_feedback(...)` | Report a false positive/negative to improve metrics |

> **Input format**: lineage/dependency tools expect a **schema-qualified** column
> reference — `schema.table.column` (e.g. `ba.orders.customer_id`), not a bare
> `table.column`. Each returned node carries both `name` (the bare column) and
> `table` (the owning `schema.table`), so results are navigable without a second lookup.

> **Provenance fields**: lineage edges now carry `file`, `line`, and `expression`
> (where the lineage was derived from), a `confidence` of `1.0` for plainly-parsed
> facts (lower for inferred edges, with a `reason`), and a `table_kind`
> (`table` / `cte` / `derived` / `external`) so CTE and derived aliases are
> distinguishable from real tables.

> **LLM agent tip**: call `db_info()` before lineage queries to check that
> `SqlColumn > 0` and `warnings` is empty. If `parse_quality["scripting_block"]`
> is high, column lineage will be limited for those files — use table-level tools
> (`find_table_usages`, `get_*_dependencies`) instead.

## CLI reference

Full option reference: [docs/cli.md](docs/cli.md)

```bash
sqlcg install                          # register MCP server in Claude Code
sqlcg db init                          # initialise graph database
sqlcg index <path> --dialect snowflake # index SQL files (snowflake is the tested dialect)
sqlcg index <path> --dialect auto      # read dialect from .sqlcg.toml
sqlcg index <path> --profile           # index + print per-stage timing and slowest files
sqlcg index <path> --include-working-tree  # also index uncommitted changes (marks graph dirty)
sqlcg reindex <path> --from <sha> --to <sha>  # incremental resync of only changed files
sqlcg analyze unused                   # tables with no query references
sqlcg analyze upstream/downstream      # trace lineage from the CLI
sqlcg find table/column/pattern        # search the graph
sqlcg watch <path>                     # watch for file changes
sqlcg db info                          # graph stats + freshness (indexed SHA vs HEAD)
sqlcg git install-hooks                # install post-checkout + post-merge resync hooks
sqlcg gain                             # show usage metrics
sqlcg report                           # generate FP/error report
sqlcg mcp best-practices               # print the fact/heuristic boundary for the MCP tools
sqlcg mcp start                        # start MCP server manually
sqlcg mcp status                       # server status JSON — incl. running version + stale_by_version
sqlcg mcp stop                         # stop the running MCP server gracefully
sqlcg mcp restart                      # stop the server (client must respawn it)
sqlcg version                          # show installed version
```

### Staying on the latest build (v1.5.0)

The installed package, the CLI, and the running MCP server all report the **same**
version (`sqlcg.__version__`). After upgrading, an editor may still be talking to an
old MCP process — `sqlcg mcp status` surfaces this directly: it reports the running
server's `version` and a `stale_by_version` flag that is `true` when the live server
differs from the installed build. Re-running `sqlcg install` stops the stale server so
your editor respawns it on the new build, so you never debug against an outdated server.

### Reads while the server is running (v1.2.0)

DuckDB takes an exclusive lock on the database file, so while the MCP server is
live it holds that lock (other processes cannot open the file, even read-only).
CLI **read** commands (`find`, `analyze`, `db info`, `list-repos`,
`gain`) automatically route their query through the running server over its
control socket and return rows as usual — no flag, no config. When no server is
running they open the database directly, exactly as before. If the server is
mid-reindex the read waits for it to finish rather than failing with
"Database is locked".

## Supported dialects

sqlcg is built on [sqlglot](https://github.com/tobymao/sqlglot), so other dialects
*can* be parsed in theory. Only Snowflake has been tested against a real corpus,
though — the table reflects what's actually been exercised, not what sqlglot can do.

| Dialect | Status |
|---------|--------|
| `snowflake` | ✅ Tested against a production DWH (~1,400 files) |
| `bigquery` | ⚠️ Unproven — parses via sqlglot, lineage not validated |
| `postgres` | ⚠️ Unproven — parses via sqlglot, lineage not validated |
| `ansi` | ⚠️ Unproven — parses via sqlglot, lineage not validated |
| `tsql` | ⚠️ Unproven — parses via sqlglot, lineage not validated |
| `dbt` | ⚠️ Unproven — via optional extra, lineage not validated |

Want another dialect properly supported? We'd be glad to collaborate — open an issue
with a minimal, anonymised corpus we can develop and test against. Getting Snowflake
right was real work, so a representative corpus is what makes the difference.

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
