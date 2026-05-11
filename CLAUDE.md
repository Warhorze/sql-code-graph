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
plan/               # Sprint plans, WORKFLOW.md
.claude/            # Agent configs, progress.txt (gitignored)
```

## Commands

This project uses `uv`. Never activate a virtualenv manually, never use `pip`, never use bare `python` or `pytest`.

| Task | Command |
|------|---------|
| Run tests | `rtk uv run pytest` (fall back to `uv run pytest` if output lacks detail) |
| Run a single test | `rtk uv run pytest tests/unit/test_foo.py::TestClass::test_name -x` |
| Run the CLI | `uv run sqlcg` |
| Type check | `rtk uv run pyright` |
| Lint | `rtk uv run ruff check src tests` |
| Format | `uv run ruff format src tests` |

## Target scale — serve both ends of the spectrum

This project must work correctly and comfortably at two very different scales:

| | Small repo | Large repo |
|---|---|---|
| **Size** | ~20 ETL files | 1,000–1,600 SQL files |
| **Schema source** | DDL files are enough | Production information schema CSV required |
| **Indexing time** | Seconds — slow parsing is invisible | Must be fast; file-at-a-time is too slow |
| **Memory** | No OOM concern | Must not OOM; batch writes are necessary |
| **Setup friction** | Zero — `sqlcg index ./sql` just works | Acceptable to require `.sqlcg/schema.csv` |

**Design rule**: never solve a large-repo problem in a way that degrades the small-repo
experience. A user with 20 ETLs should not need to export an information schema CSV,
configure batching, or understand graph internals to get useful lineage.

**OOM history**: a prior approach loaded all parsed files into memory before writing to
KuzuDB, causing OOM on large repos. The fix (write one file at a time + commit) is
correct for correctness but unacceptably slow at scale. Any future memory solution
must preserve throughput — batched writes (e.g. N files per transaction) are preferred
over single-file commits. Document the chosen batch size and its memory/speed rationale
in the relevant sprint plan.

**Perf budget**: indexing 1,600 files with schema CSV provided should complete in
under 5 minutes on a laptop. Measure and record actual timings in sprint postmortems.
<!-- TODO: replace with real benchmark numbers after large-repo testing -->

- When referring to files in docs or plans, use markdown hyperlinks (e.g. [`schema.py`](src/sqlcg/core/schema.py)) so stale references are immediately visible when file names change.
- No backward compatibility. Re-index is the migration path.
- No TODO in the happy path of any feature.
- Every new method must have a grep-confirmed call site before PR opens.
- Path/constant fallbacks must match `KuzuConfig` — never hardcode.
- Tests must assert observable output, not just "no exception raised".
