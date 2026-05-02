# Architecture Review — sql-code-graph (sqlcg)

Blueprint version: v1.2 (May 2026)
Review date: 2026-05-02
Reviewer agent: architect-reviewer

---

## Open Questions

> **Status**: All six questions resolved — answers combine user input with patterns
> cross-referenced from [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext)
> v0.4.5, the upstream project sqlcg is modelled on.

1. **Multi-dialect corpus** — **Resolved: single `--dialect` per invocation.**
   `sqlcg index <dir> --dialect snowflake` applies one dialect to the whole directory.
   Users with DDL and ETL in separate dirs can pass `--no-ddl` (or simply point only
   at the ETL dir) to reduce surface area — DDL ingestion is opt-in, not required.
   Mixed-dialect repos are handled by running `sqlcg index` twice against the two dirs.
   Impact on design: `IndexerWalker` receives a single `dialect` enum; `SchemaResolver`
   is instantiated once per run, not per file.

2. **KùzuDB multi-repo isolation** — **Resolved: additive accumulation, reuse
   CodeGraphContext's MERGE-keyed-by-path pattern.**
   CodeGraphContext uses `MERGE (r:Repository {path: $abs_path})` so each repo gets
   its own node and all child nodes reference it. `sqlcg index path-A` then
   `sqlcg index path-B` accumulates both. Selective wipe: `sqlcg db reset` clears
   everything; `sqlcg db reset --repo <path>` removes only one repo's nodes.
   Update the KùzuDB schema DDL to declare `path STRING PRIMARY KEY` on the `Repo`
   node table.

3. **`execute_cypher` intent** — **Resolved: ship in v1 with read-only enforcement.**
   User confirmed all planned features are v1, including type-safety features.
   Reuse CodeGraphContext's blocklist pattern: strip quoted string literals first,
   then reject queries containing any of `{CREATE, MERGE, DELETE, SET, REMOVE, DROP}`.
   `GraphBackend.run_read` already provides the required interface; no new plumbing
   needed. Remove the "excluded from v1" note from the blueprint.

4. **Concurrency model for `sqlcg watch`** — **Resolved: per-file debounced timers,
   same pattern as CodeGraphContext.**
   CodeGraphContext uses one `threading.Timer` (2 s debounce) per file path in a
   `self.timers` dict. Rapid saves cancel and restart the timer for that file;
   different files are independent; concurrent timer expiry can overlap.
   **This confirms finding 3.3 is real and mandatory to fix**: `SchemaResolver`
   must not be shared across concurrent re-index calls — either use a `threading.Lock`
   around cache mutation, or construct a fresh `SchemaResolver` per re-index job.
   `jobs.py` contract: one `threading.Timer` per file, 2 s debounce, no global queue.

5. **Target Python version** — **Resolved: `requires-python = ">=3.12"`.**
   User preference is latest stable. CodeGraphContext targets `>=3.10` with CI on
   3.12; KùzuDB's own FalkorDB-lite extras require `>=3.12`. Set `>=3.12` in
   `pyproject.toml`; CI matrix: 3.12 and 3.13 (verify KùzuDB 0.11.3 on 3.13 before
   enabling). Drop any `sys.version_info` guards for 3.10/3.11 syntax.

6. **DDL requirement / `add_information_schema`** — **Resolved: DDL is not required
   for the primary use case; defer `add_information_schema` to v2.**
   For table-level lineage (the core use case), sqlglot resolves table references from
   DML alone — DDL is not needed. Column-level lineage degrades gracefully when schema
   is absent: `SELECT *` expands to `*` with `confidence=0.3` and `schema_required=true`
   surfaced in the MCP response, which gives Claude enough signal to ask the user for
   DDL if needed. `add_information_schema()` should raise `NotImplementedError` in v1
   (finding 3.8); the `--schema-from-info-schema` CLI flag should error immediately.
   Remove `add_information_schema` from the v1 deliverable list in the blueprint.

---

## 1. What the Blueprint Proposes

sql-code-graph (sqlcg) is a self-contained MCP server that:

- Walks a corpus of `.sql` files and parses them with `sqlglot`
- Persists table nodes, column nodes, query nodes, and column-level lineage edges
  into an embedded KùzuDB graph database
- Exposes lineage queries (upstream, downstream, impact, pattern search) as MCP
  tools consumable by Claude
- Supports five SQL dialects (ANSI, Snowflake, BigQuery, Postgres, T-SQL) via a
  per-dialect parser subclass strategy
- Supports incremental re-indexing via `watchdog` file watching
- Integrates with dbt projects via the `dbt-artifacts` package for schema enrichment
- Provides a `typer`-based CLI (`sqlcg`) for all operations

The stated niche is the gap between stateless SQL linters (sqlglot tools), full
DataHub pipelines, and syntactic-only graph tools (CodeGraphContext). The design
template is CodeGraphContext for structure plus DataHub's `SqlParsingAggregator` for
lineage semantics.

The blueprint is at v1.2 after incorporating an architecture review that added: an
explicit `GraphBackend` ABC, a typed `LineageEdge` dataclass, `lru_cache` on
`SchemaResolver.as_dict()`, a fallback table scan for `build_scope` returning `None`,
and a Snowflake-specific parser with six documented gap handlers.

---

## 2. Architectural Strengths

### 2.1 GraphBackend ABC with neo4j fallback from day one

Defining the abstraction before any parser code is written is the right call.
KùzuDB's archival risk is real and acknowledged. Having `KuzuBackend` and
`Neo4jBackend` both wired to the same schema DDL from day one, and running every
Cypher query against both in CI, is the correct mitigation. The interface is minimal
and correct (`upsert_node`, `upsert_edge`, `run_read`, `delete_nodes_for_file`,
`close`, `transaction`).

### 2.2 Frozen, typed data model before parser code

`LineageEdge` as a frozen dataclass (rather than `tuple[ColumnRef, ColumnRef]`)
is the right shape. Including `transform`, `confidence`, and `query_id` at model
freeze time prevents a downstream breaking change that would otherwise require
rewriting graph schema, parser output, and all downstream consumers simultaneously.

### 2.3 Honest accuracy expectations with per-pattern breakdown

The accuracy table in section 8.5 is the most intellectually honest section of the
blueprint. Publishing separate precision/recall metrics (not just "coverage") and
surfacing `schema_required` flags to the caller via MCP tool responses is the correct
approach. It sets accurate user expectations and gives Claude actionable information
to request missing context.

### 2.4 Explicit failure mode inventory with structured outcomes

The four failure modes (dynamic SQL, scripting blocks, `SELECT *` without schema,
`COPY INTO @stage`) each map to a defined outcome: a specific `parsing_mode` value,
a specific confidence score, and a specific graph node type. This prevents silent
failures and makes the failure taxonomy queryable from Cypher.

### 2.5 Fallback table scan over silent empty return

Replacing `build_scope() → None` silent `[]` returns with a logged fallback to
`_fallback_table_scan()` is the correct reliability pattern. The combination of
structured logging (with AST class name) and a best-effort result means the graph
never silently loses a table reference.

### 2.6 SchemaResolver caching with explicit invalidation

`lru_cache(maxsize=1)` on `as_dict()` with `_invalidate_cache()` called on every
mutation is a clean, correct pattern for a single-threaded context. The per-dialect
normalization hook (`_normalize_key`) centralizes case-folding rather than scattering
`toUpper()` calls across Cypher queries.

### 2.7 dbt-artifacts for manifest parsing

Replacing a hand-rolled manifest parser with the `dbt-artifacts` package that handles
v4–v12 schema differences is the correct tradeoff. The blueprint's caution about
treating `datahub.sql_parsing` as an internal, pinned dependency (not part of the
stable SDK) is also the right posture.

### 2.8 stdout redirect guard

Redirecting `sys.stdout = sys.stderr` at MCP server startup before `mcp.run()` is a
necessary defense for stdio-mode MCP. Any stray `print()` in the dependency chain
(including inside `sqlglot` or `kuzu` error paths) will corrupt the JSON-RPC stream
silently without this guard.

### 2.9 Upstream issue tracking with actionable contribution plan

Documenting six upstream sqlglot issues, their status, and the action to contribute
failing test cases before writing workarounds is the correct approach. Upstream fixes
become free upgrades; the regression tests remain as protection against version
regressions.

---

## 3. Risks, Gaps, and Missing Considerations

### 3.1 [HIGH] `transaction()` context manager is a no-op by default — write operations have no atomicity guarantee

The `GraphBackend.transaction()` base implementation yields `self` and does nothing.
The `reindex_file` method calls `db.delete_nodes_for_file(file_path)` followed by
`self._index_single_file(file_path, db)`. If indexing fails mid-way, the file's nodes
are deleted but not re-inserted, leaving the graph in a permanently inconsistent state.
This is not a theoretical concern — parse failures, I/O errors, and sqlglot exceptions
can all interrupt `_index_single_file`.

The `KuzuBackend` must implement `transaction()` using KùzuDB's connection-level
transaction API (`conn.begin()` / `conn.commit()` / `conn.rollback()`). The `Neo4jBackend`
must use Neo4j's explicit transaction. The base class no-op is acceptable only if
both concrete backends override it — the blueprint does not enforce this.

Risk: HIGH. A failed re-index leaves orphaned graph nodes that produce incorrect
lineage results until the next full re-index.

### 3.2 [HIGH] `CrossFileAggregator.resolve_pass2` re-opens files without error handling

```python
def resolve_pass2(self, parser, parsed):
    parser.schema_resolver.add_view_sources(self.sources)
    with open(parsed.path) as f:
        return parser.parse_file(parsed.path, f.read())
```

This re-reads the file from disk during pass 2. Between pass 1 and pass 2 (which can
span seconds for large repos), the file may have been deleted, moved, or modified by
the user or `sqlcg watch`. There is no error handling around `open()`. A missing file
raises an unhandled `FileNotFoundError` that will crash the entire indexing run rather
than logging a warning and continuing.

Risk: HIGH for the `sqlcg watch` use case where files are volatile.

### 3.3 [HIGH] `SchemaResolver.as_dict()` lru_cache is not thread-safe

`functools.lru_cache` on an instance method shares state across all callers of that
instance. If `sqlcg watch` processes file events concurrently (the `jobs.py` background
job manager suggests this is the intent), simultaneous calls to `add_create_table()`
(which calls `_invalidate_cache()`) and `as_dict()` (which reads the cache) are a
data race. Python's GIL protects individual bytecode operations but not the
multi-step cache-clear-then-rebuild sequence.

Risk: HIGH if `jobs.py` uses threads. The blueprint does not specify the concurrency
model (see Open Question 4).

### 3.4 [MEDIUM] `_extract_column_lineage` silently swallows all `sg_lineage` exceptions

```python
try:
    root = sg_lineage(col_name, body, schema=schema, dialect=self.DIALECT)
except Exception:
    continue
```

A bare `except Exception: continue` discards all failure information. For a column
that fails lineage extraction, no error is recorded in `ParsedFile.errors`, no
`confidence` downgrade is applied, and no `parsing_mode` is set. The column simply
produces zero edges, which is indistinguishable from a column with no upstream source.
This directly undermines the "explicit failure mode inventory" strength described in
section 2.4.

### 3.5 [MEDIUM] `SnowflakeParser._parse_scripting_file` applies a file-level heuristic that can false-positive

The heuristic `if _SCRIPTING_BLOCK.search(sql): return self._parse_scripting_file()`
matches any file containing the word `BEGIN`. This includes:

- Comments: `-- BEGIN transaction isolation block`
- String literals: `SELECT 'BEGIN' AS status_label FROM t`
- Legitimate `BEGIN TRANSACTION` in non-scripting contexts

A false positive routes an entire file through the regex DML extractor, which sets
`parse_failed=True` and `confidence=0.3` on all statements and drops all column
lineage. This degrades quality on files that are fully parseable.

### 3.6 [MEDIUM] `QueryNode` is a mutable dataclass but `LineageEdge` is frozen — inconsistency

`LineageEdge` is correctly `@dataclass(frozen=True)`. `QueryNode` is `@dataclass`
(mutable). Since `QueryNode.column_lineage: list[LineageEdge]` is a mutable list
containing frozen elements, the frozenness of the edge does not protect the list.
More importantly, `QueryNode` instances are accumulated into `ParsedFile.statements`
and then passed to the graph backend. If the graph backend or aggregator mutates a
`QueryNode` (e.g. patching `confidence` during pass 2), the mutation is invisible
to the caller and produces surprising behavior. Either `QueryNode` should also be
frozen (use `replace()` for modifications), or mutations should go through explicit
methods.

### 3.7 [MEDIUM] No error budget or circuit-breaker for large repos

The benchmark target is `p95 < 500ms` per file and `>= 50 files/sec` throughput.
For a 10,000-file repo, that is 200 seconds of indexing. There is no timeout per
file, no circuit-breaker for pathological inputs (the `adversarial/` benchmarks
include 200-JOIN and 500-UNION queries), and no way to interrupt a running `sqlcg index`
gracefully (SIGINT handling is not mentioned). The `jobs.py` module is listed but
not specified.

### 3.8 [MEDIUM] `add_information_schema` is left as `...` with no contract

```python
def add_information_schema(self, csv_path):
    """..."""
    ...
```

This is a public method on `SchemaResolver` that is advertised in the CLI (`--schema-
from-info-schema`). Leaving it as a stub means the CLI flag will silently do nothing.
If this is a v1 deliverable, the blueprint should specify the CSV/Parquet column
schema. If it is deferred, the method should raise `NotImplementedError` and the
CLI flag should surface a clear error rather than silently accepting the argument.

### 3.9 [LOW] `pyproject.toml` sqlglot upper bound `<31.0` may be too aggressive

sqlglot has a high release velocity (multiple releases per month). Pinning `<31.0`
when the current version at blueprint time requires `>=28.0` means only a ~3-minor-
version window is acceptable. If sqlglot 31.0 ships with bug fixes for Gap 1 (colon
reserved word) or Gap 2 (LATERAL FLATTEN), the project is blocked from adopting them
without a dependency audit. The upper bound should be `<32.0` initially, with the
understanding that each bump is audited via CHANGELOG review in CI.

### 3.10 [LOW] `acryl-datahub` optional dependency version constraint is underspecified

`"acryl-datahub[sql-parsing]>=0.14.0"` specifies no upper bound on an explicitly
unstable internal module. The blueprint text says "pin to a specific acryl-datahub
version and treat as internal," but the `pyproject.toml` snippet contradicts this by
using an unbounded `>=` constraint. Use `==0.14.x` or at minimum `>=0.14.0,<0.15.0`.

### 3.11 [LOW] MCP tool docstrings have no explicit error documentation

The blueprint notes "docstrings kept under 2 KB" but does not specify what error
conditions each tool can return. FastMCP will surface exceptions as MCP error
responses, but the tool schema gives Claude no guidance on what errors to expect or
how to recover. At minimum, tools that depend on the graph being indexed should
document a `NotIndexedError` condition.

### 3.12 [LOW] `_EMBEDDED_DML` regex in `SnowflakeParser` uses `re.DOTALL` with greedy matching

```python
_EMBEDDED_DML = re.compile(
    r'(SELECT\s+.+?(?=;|$)|INSERT\s+INTO.+?(?=;|$)|...)',
    re.DOTALL | re.IGNORECASE,
)
```

The `(?=;|$)` lookahead with `re.DOTALL` and end-of-string `$` will match across
multi-statement blocks when the last statement has no trailing semicolon. This can
merge two independent DML statements into one malformed SQL string that then fails
to parse. The regex should use `re.MULTILINE` with `$` anchoring to line ends, or
be replaced with a proper token-level block extractor.

---

## 4. Improvements Ranked by Risk/Reward

Ranking uses: **Impact** (correctness/stability consequence if unaddressed) x
**Effort** (estimated implementation cost). High-impact, low-effort items rank first.

| Rank | Finding | Risk | Effort | Action |
|------|---------|------|--------|--------|
| 1 | 3.1 — `transaction()` no-op allows corrupt graph state after failed re-index | HIGH | Low | Implement `transaction()` with rollback in both `KuzuBackend` and `Neo4jBackend`; add a test that injects a failure mid-index and asserts graph consistency |
| 2 | 3.4 — `_extract_column_lineage` bare `except Exception: continue` | HIGH | Low | Log the exception at WARNING with column name and file path; append to `ParsedFile.errors`; set `confidence=0.0` on the affected edge slot |
| 3 | 3.2 — `resolve_pass2` re-opens file without error handling | HIGH | Low | Wrap `open()` in `try/except (FileNotFoundError, OSError)`; log a warning and return the pass-1 `ParsedFile` unchanged |
| 4 | 3.3 — `SchemaResolver.as_dict()` cache is not thread-safe | HIGH | Medium | Document the single-threaded assumption explicitly in the class docstring; if `jobs.py` uses threads, use `threading.Lock` around cache mutation and read, or switch to a per-parse-job `SchemaResolver` instance |
| 5 | 3.5 — `BEGIN` heuristic false-positives on comments and string literals | MEDIUM | Low | Use a token-aware check: call `sqlglot.tokenize(sql, dialect="snowflake")` and look for a `TokenType.BEGIN` token that is not inside a string or comment, rather than a raw regex |
| 6 | 3.6 — `QueryNode` mutability inconsistency with frozen `LineageEdge` | MEDIUM | Low | Either add `frozen=True` to `QueryNode` and use `dataclasses.replace()` for pass-2 patching, or document the mutability contract explicitly in the class docstring |
| 7 | 3.7 — No per-file timeout or SIGINT handling during indexing | MEDIUM | Medium | Add a configurable `--timeout-per-file` (default 30s) enforced via `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=N)`; handle `KeyboardInterrupt` in the walker loop to flush and exit cleanly |
| 8 | 3.8 — `add_information_schema` is a silent no-op stub | MEDIUM | Low | Raise `NotImplementedError("--schema-from-info-schema is not yet implemented")` until the method is built; guard the CLI flag to surface this error immediately |
| 9 | 3.10 — `acryl-datahub` upper bound missing on unstable module | LOW | Low | Change to `"acryl-datahub[sql-parsing]>=0.14.0,<0.15.0"` in `pyproject.toml` |
| 10 | 3.12 — `_EMBEDDED_DML` regex greedy match across statement boundaries | LOW | Low | Add `re.MULTILINE`; test against a two-statement scripting block with no trailing semicolon on the last statement |
| 11 | 3.9 — sqlglot upper bound `<31.0` too narrow | LOW | Low | Widen to `<32.0`; add CHANGELOG review step to CI upgrade process |
| 12 | 3.11 — MCP tools lack explicit error documentation | LOW | Low | Add a `Raises` section to each tool docstring listing `NotIndexedError`, `InvalidColumnRef`, etc.; define a small custom exception hierarchy in `server/exceptions.py` |

---

## 5. Additional Observations

### 5.1 `jobs.py` is listed but has no spec

The `src/sqlcg/core/jobs.py` module is described as "Background indexing job manager"
in the file tree but receives no further specification in the blueprint. It is
referenced implicitly by the `sqlcg watch` command and likely by `sqlcg index` for
large repos. The absence of a spec creates risk because the concurrency model (threads
vs processes vs async) determines whether `SchemaResolver`, `GraphBackend`, and the
`CrossFileAggregator` need thread-safety guarantees. This should be specified before
Phase 2 implementation begins.

### 5.2 The `wizard.py` first-run wizard has no spec

`src/sqlcg/cli/wizard.py` is listed with the note "InquirerPy first-run wizard" but
is not described anywhere in the blueprint. If it is in scope for v1, it needs a spec.
If it is a v2 item, remove it from the Phase 1 file tree to avoid scope creep.

### 5.3 `JOINS` relationship in the graph schema may produce incorrect edges

```sql
CREATE REL TABLE JOINS (FROM Table TO Table, on_columns STRING, query_id STRING);
```

A `JOINS` edge from `Table → Table` implies a structural relationship between tables
independent of any query. In practice, the same two tables may be joined with different
ON conditions in different queries, and a join in one query does not imply a permanent
relationship between the tables. This edge type risks polluting the graph with
misleading structural relationships. Consider making `JOINS` a relationship from
`Query → Table` (with a `joined_to STRING` property), or removing it from v1 until
the use case is specified.

### 5.4 `Column.id` construction is collision-prone for multi-catalog schemas

```sql
CREATE NODE TABLE Column (id STRING PRIMARY KEY, table STRING, name STRING, data_type STRING);
-- id = "qualified_table.col_name"
```

If `qualified_table` is `schema.table` (no catalog), then a column named `id` in
`reporting.orders` produces `reporting.orders.id`. But if two catalogs contain a table
`reporting.orders`, both columns produce the same `id` string. The `id` construction
must include the catalog component when present. Confirm the construction function is
`f"{catalog}.{db}.{table}.{col}"` (all non-None components joined with `.`) rather
than using `TableRef.qualified` which elides `None` components.

### 5.5 `delete_nodes_for_file` scope should extend to `Column` nodes

The Cypher in `reindex_file` deletes `Query` nodes and their `COLUMN_LINEAGE` edges.
However, `Column` nodes are defined by `HAS_COLUMN` edges from `Table` nodes, not from
`Query` nodes. If a file defines a `CREATE TABLE` and the file is re-indexed, the
`Column` nodes from the previous version of the table may persist (e.g. a column that
was renamed). The `delete_nodes_for_file` contract should also clean up `Column` nodes
associated with `Table` nodes `DEFINED_IN` the file.

---

## 6. Summary Assessment

The blueprint is at a high quality for a pre-implementation design document. The v1.2
changes (typed `LineageEdge`, explicit `GraphBackend` ABC, caching with invalidation,
fallback table scan, stdout guard) address the most critical design risks. The Snowflake
analysis is thorough and the accuracy expectations are honest.

The primary outstanding risk is the atomicity gap in incremental re-indexing (finding
3.1): a failed re-index leaves the graph corrupted with no recovery path other than
full re-index. This must be addressed before `sqlcg watch` is usable in production.
The secondary risk cluster is the three exception-handling gaps (3.2, 3.4, 3.8) that
convert errors into silent incorrect behavior.

The blueprint is implementation-ready for Phase 1 (data model freeze, `GraphBackend`
ABC, single-file parser) with the caveat that the six open questions (particularly
questions 1, 2, and 4) should be resolved before Phase 2 (cross-file indexer) begins.
