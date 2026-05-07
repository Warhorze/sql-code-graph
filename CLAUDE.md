# CLAUDE.md

## Project

SQL Code Graph (`sqlcg`) — indexes SQL repositories into a KuzuDB graph for lineage
tracing via MCP tools. Python 3.12, no FastAPI (CLI + MCP server only).

## Key docs (read before acting)

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE_REVIEW.md` | Living architecture review — findings, decisions, priorities, postmortems |
| `plan/WORKFLOW.md` | Agent roles, phase sequence, compliance ownership |
| `plan/sprint_*.md` | Active sprint plans — check latest before implementing anything |

## Agents (`.claude/agents/`)

| Agent | Use when |
|-------|----------|
| `architect-reviewer` | Update `ARCHITECTURE_REVIEW.md` from findings or user input |
| `architect-planner` | Plan a single feature → `plan/<feature>.md` |
| `sprint-planner` | Plan a sprint from multiple findings → `plan/sprint_<name>.md` |
| `plan-reviewer` | Gate before implementation — catch gaps in any plan |
| `developer` | Implement an approved plan |
| `code-reviewer` | Review an open PR for code quality |
| `api-documenter` | Improve OpenAPI output after API changes |

Compliance ownership: `architect-planner` owns `plan/<feature>.md`;
`sprint-planner` owns `plan/sprint_*.md`. See `plan/WORKFLOW.md` for the full flow.

## Source layout

```
src/sqlcg/
  cli/commands/     # Typer CLI commands
  core/             # KuzuBackend, config, graph_db
  indexer/          # index_repo, reindex_file
  parsers/          # AnsiParser, SnowflakeParser, base.py
  lineage/          # CrossFileAggregator, SchemaResolver
  server/           # MCP tools, models
tests/
  unit/             # No graph backend
  integration/      # Real KuzuDB in-memory
  e2e/              # Full CLI runs
plan/               # Sprint plans, WORKFLOW.md, progress.txt
```

## Commands

This project uses `uv`. Never activate a virtualenv manually, never use `pip`, never use bare `python` or `pytest`.

| Task | Command |
|------|---------|
| Run tests | `uv run pytest` |
| Run a single test | `uv run pytest tests/unit/test_foo.py::TestClass::test_name -x` |
| Run the CLI | `uv run sqlcg` |
| Type check | `uv run pyright` |
| Lint | `uv run ruff check src tests` |
| Format | `uv run ruff format src tests` |

## Non-negotiable rules

- No backward compatibility. Re-index is the migration path.
- No TODO in the happy path of any feature.
- Every new method must have a grep-confirmed call site before PR opens.
- Path/constant fallbacks must match `KuzuConfig` — never hardcode.
- Tests must assert observable output, not just "no exception raised".
