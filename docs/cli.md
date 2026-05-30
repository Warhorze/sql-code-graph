# CLI Reference

This page is auto-generated from CLI command metadata.

Regenerate with:

```bash
bash scripts/generate_cli_docs.sh
```

## Commands

| Command | Description |
| --- | --- |
| `index` | Index SQL files in a directory. |
| `reindex` | Incrementally resync the graph after a git branch change or pull. |
| `watch` | Watch a directory and re-index on SQL file changes. |
| `gain` | Show metrics and feedback analytics. |
| `report` | Generate a metrics report with FP clusters and parse error patterns. |
| `install` | Register sqlcg as an MCP server in Claude Code (~/.claude/settings.json). |
| `uninstall` | Uninstall sqlcg from Claude Code and optionally clean up resources. |
| `version` | Show version. |
| `db` | Database management commands |
| `find` | Search the graph |
| `analyze` | Lineage analysis |
| `mcp` | MCP server commands |
| `git` | Git integration commands |

## `sqlcg`

```bash
sqlcg [OPTIONS] COMMAND [ARGS]...
```

SQL code graph analyzer.

QUICK START:
  1. sqlcg db init
  2. sqlcg index <path> --dialect snowflake
  3. sqlcg git install-hooks
  4. sqlcg install --scope project   # also provisions a Claude skill (SKILL.md)

USING THE MCP TOOLS:
  Read `sqlcg mcp best-practices` first — it explains the fact/heuristic
  boundary so heuristic output (dead-code, risk) is never reported as fact.
  See `sqlcg mcp --help` for all MCP commands.

Note: Binary is `sqlcg`; PyPI package is `sql-code-graph`.

### Global Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --install-completion | BOOLEAN | No | No |  | Install completion for the current shell. |
| --show-completion | BOOLEAN | No | No |  | Show completion for the current shell, to copy it or customize the installation. |

## `sqlcg index`

```bash
sqlcg index [OPTIONS] PATH
```

Index SQL files in a directory.

Schema aliases (staging schema → canonical schema) can be configured in
.sqlcg.toml under sqlcg.schema_aliases, e.g. da_tmp = "da".

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --dialect, -d | TEXT | No | No |  | SQL dialect (or 'auto' to read from .sqlcg.toml) |
| --dbt-manifest | PATH | No | No |  | Path to dbt manifest |
| --timeout-per-file | INTEGER | No | No | 5 | Timeout per file in seconds |
| --buffer-pool-size | INTEGER | No | No | 0 | KuzuDB buffer pool size in MB (0 = default). Set to 256-512 on memory-constrained machines. |
| --batch-size | INTEGER | No | No | 50 | Files per KuzuDB transaction in the upsert pass. Default 50 balances commit-overhead reduction (vs. legacy per-file commits) against per-batch memory cost. Lower values are safer for memory-constrained machines; higher values give marginal speedup at the cost of larger working sets. Set to 1 to reproduce legacy per-file commit behaviour. |
| --no-ddl | BOOLEAN | No | No | False | Skip table-node upserts for DDL-only files |
| --quiet, -q | BOOLEAN | No | No | False | Suppress summary console output |
| --debug | BOOLEAN | No | No | False | Show detailed log output during indexing |
| --profile / --no-profile | BOOLEAN | No | No | False | Emit per-stage timing after indexing |

## `sqlcg reindex`

```bash
sqlcg reindex [OPTIONS] PATH
```

Incrementally resync the graph after a git branch change or pull.

When --from and --to are given (e.g. from the post-checkout hook), only the
files that changed between those two SHAs are re-parsed, plus the cross-file
pass-2 closure (files that SELECT FROM tables defined in changed files).

Without --from/--to, reads the last-indexed SHA from the database and diffs it
against the current HEAD. If no stored SHA is found, falls back to a full index.

Exits with an error if the database schema version does not match the current
build — run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-init.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --from | TEXT | No | No |  | Base git SHA (previously-indexed state) |
| --to | TEXT | No | No |  | Target git SHA (defaults to HEAD when --from is given) |
| --dialect, -d | TEXT | No | No |  | SQL dialect (or 'auto' to read from .sqlcg.toml) |
| --quiet, -q | BOOLEAN | No | No | False | Suppress summary output |
| --batch-size | INTEGER | No | No | 50 | Files per KuzuDB transaction (same default as index command) |
| --timeout-per-file | INTEGER | No | No | 5 | Per-file parse timeout in seconds |

## `sqlcg watch`

```bash
sqlcg watch [OPTIONS] PATH
```

Watch a directory and re-index on SQL file changes.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --dialect, -d | TEXT | No | No |  | SQL dialect |

## `sqlcg gain`

```bash
sqlcg gain [OPTIONS]
```

Show metrics and feedback analytics.

Displays:
- Section A: Total MCP tool calls and calls in the last 7 days
- Section B: Parse success trend (last 5 index runs)
- Section C: True positive feedback rate (if ≥5 samples)
- Section D: Top 3 most-called tools
- Section E: execute_cypher ratio (high ratio = LLM falling back to raw Cypher)
- Section F: Parse quality breakdown from graph (FULL / TABLE_ONLY / SCRIPTING_FALLBACK)

Parse quality legend:
  FULL              — column-level lineage extracted; all tools work
  TABLE_ONLY        — table edges only; trace_column_lineage returns empty
  SCRIPTING_FALLBACK— sqlglot fell back to Command node; partial table edges only

All metrics are opt-in via SQLCG_METRICS environment variable.
If no metrics have been collected, shows a message and exits 0.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --json | BOOLEAN | No | No | False | Output metrics as JSON |
| ---metrics-path | PATH | No | No |  |  |

## `sqlcg report`

```bash
sqlcg report [OPTIONS]
```

Generate a metrics report with FP clusters and parse error patterns.

Analyzes feedback and index run data to identify:
- False positive clusters (files/patterns with >50% FP rate, min 3 samples)
- Parse error clusters (repos with persistent parse errors)
- Provides a pre-filled GitHub issue URL for reporting problems

If no metrics database exists, prints a message and exits 0.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --stdout | BOOLEAN | No | No | False | Print to stdout instead of file |
| --output, -o | PATH | No | No |  | Output file path |

## `sqlcg install`

```bash
sqlcg install [OPTIONS]
```

Register sqlcg as an MCP server in Claude Code (~/.claude/settings.json).

Also provisions a Claude skill file (SKILL.md) at the chosen location.
Pass --scope project or --scope global to specify where the skill is written.
On a TTY without --scope, an interactive prompt asks for the location.
On a non-TTY (CI, scripts) without --scope, the command exits with an error.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --dry-run | BOOLEAN | No | No | False | Print config without writing |
| --scope | TEXT | No | No |  | Install skill location: 'project' (under --repo) or 'global' (~/.claude/skills/). |
| --repo | PATH | No | No |  | Repository root for --scope project (default: current directory). |

## `sqlcg uninstall`

```bash
sqlcg uninstall [OPTIONS]
```

Uninstall sqlcg from Claude Code and optionally clean up resources.

Step 1: Remove MCP registration from ~/.claude/settings.json
Step 2: Optionally delete the KùzuDB graph database
Step 3: Remove git hook sentinel block from .git/hooks/post-checkout
Step 4: Remove sqlcg skill directory from ~/.claude/skills/sqlcg/ and
        <repo>/.claude/skills/sqlcg/

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --keep-db | BOOLEAN | No | No | False | Skip database deletion |
| --force | BOOLEAN | No | No | False | Delete database without prompting; also delete metrics store |
| --repo | PATH | No | No |  | Repository path for git hook removal (default: current directory) |

## `sqlcg version`

```bash
sqlcg version [OPTIONS]
```

Show version.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg db`

```bash
sqlcg db [OPTIONS] COMMAND [ARGS]...
```

Database management commands

### Subcommands

| Subcommand | Description |
| --- | --- |
| `init` | Initialise the graph database (idempotent). |
| `reset` | Wipe the database or a single repo's subgraph. |
| `info` | Show database stats. |
| `list-repos` | List all indexed repositories. |

## `sqlcg db init`

```bash
sqlcg db init [OPTIONS]
```

Initialise the graph database (idempotent).

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --buffer-pool-size | INTEGER | No | No | 0 | KuzuDB buffer pool size in MB (0 = default). Set to 256-512 on memory-constrained machines. |

## `sqlcg db reset`

```bash
sqlcg db reset [OPTIONS]
```

Wipe the database or a single repo's subgraph.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --repo | TEXT | No | No |  | Reset only this repo path |

## `sqlcg db info`

```bash
sqlcg db info [OPTIONS]
```

Show database stats.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg db list-repos`

```bash
sqlcg db list-repos [OPTIONS]
```

List all indexed repositories.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg find`

```bash
sqlcg find [OPTIONS] COMMAND [ARGS]...
```

Search the graph

### Subcommands

| Subcommand | Description |
| --- | --- |
| `table` | Find a table by name. |
| `column` | Find a column by table.column reference. |
| `pattern` | Find queries containing a SQL pattern. |

## `sqlcg find table`

```bash
sqlcg find table [OPTIONS] NAME
```

Find a table by name.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg find column`

```bash
sqlcg find column [OPTIONS] REF
```

Find a column by table.column reference.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg find pattern`

```bash
sqlcg find pattern [OPTIONS] PATTERN
```

Find queries containing a SQL pattern.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg analyze`

```bash
sqlcg analyze [OPTIONS] COMMAND [ARGS]...
```

Lineage analysis

### Subcommands

| Subcommand | Description |
| --- | --- |
| `upstream` | Trace upstream column lineage. |
| `downstream` | Trace downstream column lineage. |
| `impact` | Show all queries impacted by a table. |
| `failures` | List files that failed to parse, with their dominant cause (E-code bucket). |
| `unused` | Find tables with no query references. |

## `sqlcg analyze upstream`

```bash
sqlcg analyze upstream [OPTIONS] REF
```

Trace upstream column lineage.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --depth | INTEGER | No | No | 5 | Maximum traversal depth |

## `sqlcg analyze downstream`

```bash
sqlcg analyze downstream [OPTIONS] REF
```

Trace downstream column lineage.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --depth | INTEGER | No | No | 5 | Maximum traversal depth |

## `sqlcg analyze impact`

```bash
sqlcg analyze impact [OPTIONS] TABLE
```

Show all queries impacted by a table.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg analyze failures`

```bash
sqlcg analyze failures [OPTIONS]
```

List files that failed to parse, with their dominant cause (E-code bucket).

Requires a graph indexed with sqlcg >= v3 (schema version 3). Re-index
with 'sqlcg db reset && sqlcg index <path>' if the graph was built with
an earlier version.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --cause | TEXT | No | No |  | Filter by E-code bucket (e.g. E5, timeout) |
| --limit | INTEGER | No | No | 100 | Maximum rows to return |

## `sqlcg analyze unused`

```bash
sqlcg analyze unused [OPTIONS]
```

Find tables with no query references.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --threshold | INTEGER | No | No | 0 | Minimum reference count threshold |

## `sqlcg mcp`

```bash
sqlcg mcp [OPTIONS] COMMAND [ARGS]...
```

MCP server commands

### Subcommands

| Subcommand | Description |
| --- | --- |
| `setup` | Print or write MCP server config JSON. |
| `start` | Start the MCP server. |
| `best-practices` | Print MCP tool best-practices (the fact/heuristic boundary). |

## `sqlcg mcp setup`

```bash
sqlcg mcp setup [OPTIONS]
```

Print or write MCP server config JSON.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --print / --write | BOOLEAN | No | No | True |  |

## `sqlcg mcp start`

```bash
sqlcg mcp start [OPTIONS]
```

Start the MCP server.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg mcp best-practices`

```bash
sqlcg mcp best-practices [OPTIONS]
```

Print MCP tool best-practices (the fact/heuristic boundary).

Same guidance as the bundled Claude skill — useful for humans or agents
that have not installed the skill.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |

## `sqlcg git`

```bash
sqlcg git [OPTIONS] COMMAND [ARGS]...
```

Git integration commands

### Subcommands

| Subcommand | Description |
| --- | --- |
| `install-hooks` | Install git hooks for sqlcg integration. |

## `sqlcg git install-hooks`

```bash
sqlcg git install-hooks [OPTIONS]
```

Install git hooks for sqlcg integration.

Writes a post-checkout hook that triggers incremental resync after branch switches
and a post-merge hook that triggers resync after pulls/merges.
Idempotent: running multiple times produces one hook entry per hook.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --repo, -r | PATH | No | No |  | Path to git repository (default: current directory) |
