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
| `watch` | Watch a directory and re-index on SQL file changes. |
| `gain` | Show metrics and feedback analytics. |
| `report` | Generate a metrics report with FP clusters and parse error patterns. |
| `version` | Show version. |
| `db` | Database management commands |
| `find` | Search the graph |
| `analyze` | Lineage analysis |
| `mcp` | MCP server commands |
| `install` | Install sqlcg tools and hooks |

## `sqlcg`

```bash
sqlcg [OPTIONS] COMMAND [ARGS]...
```

SQL code graph analyzer

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

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| --dialect, -d | TEXT | No | No |  | SQL dialect |
| --dbt-manifest | PATH | No | No |  | Path to dbt manifest |
| --timeout-per-file | INTEGER | No | No | 30 | Timeout per file in seconds |
| --no-ddl | BOOLEAN | No | No | False | Skip DDL statements (not yet fully implemented) |
| --schema-from-info-schema | TEXT | No | No |  | (Not yet implemented) |

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
| _none_ |  |  |  |  |  |

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

## `sqlcg install`

```bash
sqlcg install [OPTIONS] COMMAND [ARGS]...
```

Install sqlcg tools and hooks

### Subcommands

| Subcommand | Description |
| --- | --- |
| `hooks` | Install or verify git hooks for the current repository. |

## `sqlcg install hooks`

```bash
sqlcg install hooks [OPTIONS]
```

Install or verify git hooks for the current repository.

Creates a post-checkout hook that automatically runs `sqlcg index --dialect auto --quiet`
to keep the code graph in sync when switching branches.

### Options

| Option | Type | Required | Repeatable | Default | Description |
| --- | --- | --- | --- | --- | --- |
| _none_ |  |  |  |  |  |
