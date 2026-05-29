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

## Performance invariants — DO NOT REMOVE OR SIMPLIFY

These patterns exist because measured profiling showed catastrophic regressions without them.
Any refactor that touches [`base.py`](src/sqlcg/parsers/base.py) or [`indexer.py`](src/sqlcg/indexer/indexer.py) must keep all of these intact.

| Invariant | Location | Why it exists |
|-----------|----------|---------------|
| `qualify` and `build_scope` imported at **module level** | `base.py` top | Tests patch `sqlcg.parsers.base.qualify`; inline imports make the patch miss |
| `exp.expand()` called with **`sources` only**, never `schema_sources` | `_extract_column_lineage` | Schema CSV has ~5–10k entries; passing them to `expand()` is O(N_files × N_schema) — measured at 400 s/file |
| **`body_scope` built once per statement**, reused across all columns | `_extract_column_lineage` | Wide SELECTs (176 cols × 11 joins) took 7 min with per-column qualify; once-per-statement is the fix |
| **Pure-literal skip** (`if not list(col_expr.find_all(exp.Column)): continue`) | column loop in `_extract_column_lineage` | Literals have no source column; without the skip sg_lineage raises noise E1 errors and wastes time |
| **`mapping_schema_tables` built once per statement**, passed to `_lineage_node_to_edges` | `_extract_column_lineage` | Confidence scoring (1.0 schema-backed / 0.7 inferred) must not call `mapping_schema()` per edge |
| When `body_scope` is available, **`sources=` is NOT passed to `sg_lineage`** | `sg_kwargs` construction | Scope embeds full resolution; passing `combined_sources` (including schema_sources) would re-introduce the O(N_schema) bloat through sg_lineage's internal expand |
| **`_upsert_parsed_file` uses `upsert_nodes_bulk`/`upsert_edges_bulk`** (Phase A→B→C), never `upsert_node`/`upsert_edge` per row | `indexer.py` `_upsert_parsed_file` | Measured: bulk=181s vs per-node=1020s+ on 1,340-file DWH (~10 execute() calls per file vs ~14,500 total). Commit `4234e5d` accidentally regressed this by rewriting the method during C2 normalization. |

Tests covering these invariants live in [`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
[`test_expand_excludes_schema_sources.py`](tests/unit/test_expand_excludes_schema_sources.py),
and [`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py) (each pins
one *named* regression). If any fail after a refactor, the invariant was broken — do not mark
the test as pre-existing and move on.

**The general guard is [`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py).**
Every perf blow-up here has been the same class of bug: an op that must run once per statement
(build_scope/qualify/exp.expand/mapping_schema) or carry only file-level sources started scaling
with column count / edge count / schema size — O(N) flipping to O(N²). The scaling guard parses
a fixture at size N and 2N and asserts these stay flat. It catches *future* regressions of this
class, not just the named ones, and uses deterministic op-counts (never wall-clock) so it cannot
flake by machine speed. A red here means something started scaling — find what, do not raise the
slack. When adding a new hot-path op that must be once-per-statement, add a counter for it there.

- When referring to files in docs or plans, use markdown hyperlinks (e.g. [`schema.py`](src/sqlcg/core/schema.py)) so stale references are immediately visible when file names change.
- No backward compatibility. Re-index is the migration path.
- No TODO in the happy path of any feature.
- Every new method must have a grep-confirmed call site before PR opens.
- Path/constant fallbacks must match `KuzuConfig` — never hardcode.
- Tests must assert observable output, not just "no exception raised".
