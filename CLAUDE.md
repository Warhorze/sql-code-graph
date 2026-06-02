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

## Releasing (version bump + tag)

Every feature/fix PR that ships bumps the version; after it merges to `master`, tag the release.
Do **all** of this — the tag is the step most easily forgotten.

1. **Bump the version** (in the feature branch, before the PR merges):
   - [`pyproject.toml`](pyproject.toml) `version = "X.Y.Z"`
   - [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py) `__version__ = "X.Y.Z"`
   - `uv lock` (refreshes the lockfile's own version entry)
   - **SemVer**: new capability/surface → **minor** (`1.1.x` → `1.2.0`); bug fix only → **patch**.
     No backward-compat shims, so an additive feature is still minor (not major) when nothing breaks.
2. **Merge the PR to `master`** (squash or `--merge` per the existing `Merge #NN` history).
3. **Tag the merge commit on `master`** — **annotated**, `v`-prefixed, message `vX.Y.Z — <one-line summary>`:
   ```bash
   git checkout master && git pull --ff-only origin master
   git tag -a vX.Y.Z -m "vX.Y.Z — <summary>" <merge-commit-sha>
   git push origin vX.Y.Z
   ```
   All existing tags are annotated and look like `v1.1.3` (never bare `1.1.3`). Confirm with
   `git tag -l --format='%(contents)' vX.Y.Z` and verify it points at the master merge commit.

## Target scale — serve both ends of the spectrum

This project must work correctly and comfortably at two very different scales:

| | Small repo | Large repo |
|---|---|---|
| **Size** | ~20 ETL files | 1,000–1,600 SQL files |
| **Schema source** | DDL files + in-file CTE/CTAS bodies | Same — DDL + cross-file CTAS resolution |
| **Indexing time** | Seconds — slow parsing is invisible | Must be fast; file-at-a-time is too slow |
| **Memory** | No OOM concern | Must not OOM; batch writes are necessary |
| **Setup friction** | Zero — `sqlcg index ./sql` just works | Zero — same command, no schema export |

**Design rule**: never solve a large-repo problem in a way that degrades the small-repo
experience. A user with 20 ETLs should not need to configure batching or understand
graph internals to get useful lineage.

**Schema resolution**: lineage resolves from parsed DDL (`CREATE TABLE`/`CREATE VIEW`)
and cross-file CTAS bodies harvested in pass 1. An earlier INFORMATION_SCHEMA-CSV
feature (in-memory `schema_sources` + a graph-level `load-schema` gold-table path) was
removed in 2026-05 after profiling measured **zero edge delta** vs. no-schema on the
DWH corpus — cross-file pass-2 resolution already dominated. Do not reintroduce a
schema-CSV ingestion path without a measured lineage-coverage win to justify it.

**OOM history**: a prior approach loaded all parsed files into memory before writing to
KuzuDB, causing OOM on large repos. The fix (write one file at a time + commit) is
correct for correctness but unacceptably slow at scale. Any future memory solution
must preserve throughput — batched writes (e.g. N files per transaction) are preferred
over single-file commits. Document the chosen batch size and its memory/speed rationale
in the relevant sprint plan.

**Perf budget**: indexing 1,600 files should complete in under 5 minutes on a laptop.
Measure and record actual timings in sprint postmortems.

## Performance invariants — DO NOT REMOVE OR SIMPLIFY

These patterns exist because measured profiling showed catastrophic regressions without them.
Any refactor that touches [`base.py`](src/sqlcg/parsers/base.py) or [`indexer.py`](src/sqlcg/indexer/indexer.py) must keep all of these intact.

| Invariant | Location | Why it exists |
|-----------|----------|---------------|
| `qualify` and `build_scope` imported at **module level** | `base.py` top | Tests patch `sqlcg.parsers.base.qualify`; inline imports make the patch miss |
| `exp.expand()` called with **only file-level `sources`** (CTE/CTAS bodies), filtered by `dependency_filter` in pass 2 | `_extract_column_lineage` / `parse_file` | Passing the full corpus of cross-file CTAS bodies is O(N_files × N_corpus_ctas); `dependency_filter` keeps it O(N_refs) |
| **`body_scope` built once per statement**, reused across all columns | `_extract_column_lineage` | Wide SELECTs (176 cols × 11 joins) took 7 min with per-column qualify; once-per-statement is the fix |
| **Pure-literal skip** (`if not list(col_expr.find_all(exp.Column)): continue`) | column loop in `_extract_column_lineage` | Literals have no source column; without the skip sg_lineage raises noise E1 errors and wastes time |
| When `body_scope` is available, **`sources=` is NOT passed to `sg_lineage`** | `sg_kwargs` construction | The scope already embeds full column→table resolution; passing `sources=` would make sg_lineage redundantly re-expand them |
| When a scope is passed to `sg_lineage`, **`copy=False` + `trim_selects=False` are passed too** | `sg_kwargs` construction | Without `copy=False`, sqlglot deep-copies the whole scope on **every per-column call** → O(cols × scope_size). Regressed in `4234e5d`; measured 28.8s on one 3,344-line file |
| **`body_scope` is built once per statement BEFORE the column loop** (with a schema-free qualify retry on failure), never lazily inside it | `_extract_column_lineage` | Building it inside the loop re-ran expand+qualify+build_scope for *every* column whenever qualify failed (the failure path). Regressed in `4234e5d` |
| In the INSERT column-list aliasing path, **`body.copy()` + strip-WITH happen once** before the loop; only the single projection is swapped per column | `_extract_column_lineage` INSERT block | A full-body deepcopy per column is O(cols × body_size). Regressed in `4234e5d` |
| **`_upsert_parsed_file` uses `upsert_nodes_bulk`/`upsert_edges_bulk`** (Phase A→B→C), never `upsert_node`/`upsert_edge` per row | `indexer.py` `_upsert_parsed_file` | Measured: bulk=181s vs per-node=1020s+ on 1,340-file DWH (~10 execute() calls per file vs ~14,500 total). Commit `4234e5d` accidentally regressed this by rewriting the method during C2 normalization. |
| **`_flush_row_batch` accumulates rows across all files in a batch and calls each `upsert_*_bulk` once per batch** (not once per file) | `indexer.py` `_flush_batch`/`_upsert_file_batch` → `_flush_row_batch` | Measured: per-file flush = ~13,400 `execute()` on 1,340-file DWH (10/file); per-batch flush = ~270 (10/batch). v1.1.1. Guard: [`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py) |

Tests covering these invariants live in [`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py), and
[`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py) (each pins
one *named* regression). If any fail after a refactor, the invariant was broken — do not mark
the test as pre-existing and move on.

**The general guard is [`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py).**
Every perf blow-up here has been the same class of bug: an op that must run once per statement
(build_scope/qualify) started scaling with column count / edge count — O(N) flipping to O(N²).
The scaling guard parses a fixture at size N and 2N and asserts these stay flat. It catches *future* regressions of this
class, not just the named ones, and uses deterministic op-counts (never wall-clock) so it cannot
flake by machine speed. A red here means something started scaling — find what, do not raise the
slack. When adding a new hot-path op that must be once-per-statement, add a counter for it there.

The guard's **call-count** axes alone are not enough: the three `4234e5d` regressions kept call
counts flat/linear but regressed *per-call cost* (`copy=False` dropped → per-column scope deepcopy),
the *failure path* (per-column re-qualify only when qualify throws), and a *per-column body copy*
(INSERT aliasing). A clean fixture stays green because its qualify succeeds and its scope is small.
The guard therefore also pins these **behaviourally**: scope-path `sg_lineage` must pass `copy=False`;
a forced qualify failure must not re-qualify per column; the INSERT body must be copied once. When
adding a hot-path optimization, prefer a behavioural assertion (kwarg present, op once-per-statement)
over a volume count — volume counts hide inside a too-simple fixture, which is how this shipped in v1.0.0.

- When referring to files in docs or plans, use markdown hyperlinks (e.g. [`schema.py`](src/sqlcg/core/schema.py)) so stale references are immediately visible when file names change.
- No backward compatibility. Re-index is the migration path.
- No TODO in the happy path of any feature.
- Every new method must have a grep-confirmed call site before PR opens.
- Path/constant fallbacks must match `KuzuConfig` — never hardcode.
- Tests must assert observable output, not just "no exception raised".
