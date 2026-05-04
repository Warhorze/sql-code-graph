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

## 8. PR #1 Review — Inline Comments Analysis

Analysis date: 2026-05-04
Source: `gh api repos/Warhorze/sql-code-graph/pulls/1/comments` (8 inline comments)
Reviewer: Warhorze (repo owner)

### 8.1 Comment Summary

The PR covers Phase 4 (CLI) which includes `config.py`, `graph_db.py`, `neo4j_backend.py`,
`kuzu_backend.py`, `queries.py`, and `parsers/base.py`. The reviewer raised eight inline
concerns across these files. No review-level text body was provided (both reviews have an
empty body field — one COMMENTED, one PENDING).

---

### 8.2 Comment-by-Comment Assessment

**Comment 1 — `config.py` line 18: `_load_env()` called at module import**

Reviewer text: "this will fail in when we're live, unless we ship with .env file"

Assessment: **Valid concern, but the severity is low.** `python-dotenv`'s `load_dotenv()`
does not raise if no `.env` file is present — it is a no-op. The real risk is the opposite
of what the reviewer describes: `load_dotenv()` at module import time will silently override
environment variables already set in the process (e.g. from Docker, Kubernetes, or CI
secrets) if `override=True` is passed. The current call uses the default `override=False`,
so already-set variables are preserved and the absence of a `.env` file is harmless.

However, calling `_load_env()` as a module-level side effect is a code smell regardless.
It makes the module harder to test (you cannot import `config` without the side effect
firing), and it introduces load-order coupling (imports of `config` from other modules will
trigger `.env` loading even in contexts where it is unwanted, such as a library consumer
who manages their own env).

Recommended resolution: Remove the module-level `_load_env()` call. Call `load_dotenv()`
explicitly only from `server.main()` and `cli.main()` entry points, before any other
config reads. This is the idiomatic pattern for CLI/server tools.

**Comment 2 — `config.py` line 53: separate `get_db_path()` and `get_backend_type()` functions**

Reviewer text: "the db_path and backend_type are inheritately bound why seperate return
functions? why not a pydantic dataclass? This goes for all database objects, why not a
dataclass per backend?"

Assessment: **Valid architectural concern. Medium priority.**

The reviewer is correct that `db_path` and `backend_type` are logically coupled — they
together describe one backend configuration. Splitting them into independent stateless
functions means `get_backend()` must call both and implicitly couples them by calling
order. A `BackendConfig` Pydantic model (or `dataclass`) would:

- Make the coupling explicit and type-safe
- Allow validation at construction time (e.g. `db_path` is required for `kuzu`, URI/user/
  password are required for `neo4j`)
- Be injectable in tests without environment mutation
- Be serialisable for CLI help output

The "dataclass per backend" suggestion extends this further: a `KuzuConfig(BaseModel)` with
`db_path: Path` and a `Neo4jConfig(BaseModel)` with `uri: str, user: str, password: str`.
A `BackendConfig = KuzuConfig | Neo4jConfig` discriminated union would let `get_backend()`
accept a config object rather than reading env vars internally.

This is a clean architectural improvement. It does not break the existing CLI since
`get_backend()` can continue to be the top-level factory — it just builds the config
from env vars and delegates to the typed constructor.

Effort: low-medium (1–2 hours). Risk of not doing it: medium (growing config surface makes
it harder to support more backends or add validation later).

**Comment 3 — `graph_db.py` line 15: `_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")`**

Reviewer text: "these exists already in base python — print(ascii.charlist()) same for
numbers."

Assessment: **Reviewer is partially correct but the suggestion is imprecise.**

There is no `ascii.charlist()` in the Python standard library. The reviewer is likely
thinking of `string.ascii_letters`, `string.digits`, `string.ascii_lowercase`, etc.
from the `string` module.

The correct standard-library approach would be to validate by trying to compile the
string as a Python identifier using `str.isidentifier()`. For example:
`if not key.isidentifier(): raise ValueError(...)`. Python's `str.isidentifier()` accepts
the same character set as `_IDENT_RE` (letter/underscore start, alphanumeric/underscore
body) and is guaranteed correct across Unicode edge cases.

However, `str.isidentifier()` would also accept Python keywords (`if`, `for`, `True`)
as valid identifiers, which are valid Cypher property names anyway. For the purpose of
preventing Cypher injection via property key interpolation, `str.isidentifier()` is
semantically correct and simpler than a hand-rolled regex.

Recommended resolution: Replace `_IDENT_RE.match(key)` with `key.isidentifier()` in
`_validate_props`. Remove the `_IDENT_RE` constant and the `import re`. This is a
cosmetic/idiomatic fix with no behavioural change.

**Comment 4 — `neo4j_backend.py` line 57: `init_schema` creates indexes without APOC**

Reviewer text: "shouldn't we use APOC procedures to ensure we're not accidentally dropping
the entire database? https://neo4j.com/developer/kb/protecting-against-cypher-injection/"

Assessment: **The concern is valid but APOC is not the right solution for this code path.**

APOC (Awesome Procedures On Cypher) is a Neo4j plugin that provides utility procedures
like `apoc.schema.assert`. The linked KB article is about preventing Cypher injection via
parameterised queries — which is already handled by `_validate_props()` in this codebase.

APOC is not relevant to `init_schema()`. The reviewer may be conflating two separate
concerns: (a) injection prevention (already addressed) and (b) safe idempotent schema
management (a real concern, already addressed by `IF NOT EXISTS` in all index creation
statements in `Neo4jBackend.init_schema()`).

The more substantive underlying concern — whether a badly formed Cypher query in
`init_schema()` could cause data loss — is real for `KuzuBackend.init_schema()` which
executes raw DDL strings split by a hand-rolled parser (`SCHEMA_DDL.split("\n")` loop).
A malformed statement in `SCHEMA_DDL` could produce unexpected behaviour. The correct
mitigation is not APOC but a transaction wrapper: the entire `init_schema()` DDL
execution should be wrapped in a transaction so that a partial failure rolls back cleanly
rather than leaving a half-initialised schema.

Recommended resolution: Clarify in code comments that injection is prevented by
`_validate_props()` and that idempotency is ensured by `IF NOT EXISTS`. Wrap
`KuzuBackend.init_schema()` DDL execution in a transaction (this is the same gap as
existing finding 3.1 but applied to schema init, not just re-indexing).

**Comment 5 — `kuzu_backend.py` line 144: hand-rolled MERGE queries**

Reviewer text: "why are we hand rolling this? import Kuzu.Graph.Query or
import kuzu / from langchain_kuzu.graphs.kuzu_graph import KuzuGraph"

Assessment: **The suggestion to use `langchain_kuzu` is architecturally significant but
not straightforward to adopt. The hand-rolling is intentional and defensible.**

`langchain_kuzu.graphs.kuzu_graph.KuzuGraph` (from the `langchain-kuzu` package) is a
LangChain integration layer built for adding `GraphDocument` objects to KùzuDB from
LLM-extracted entity/relation triples. Its `add_graph_documents()` method is designed for
LangChain's `GraphDocument` schema, which is fundamentally different from this codebase's
schema of `SqlTable`, `SqlColumn`, `SqlQuery`, `File`, and `Repo` nodes with precise
typed relationships.

Using `KuzuGraph.add_graph_documents()` would require:
1. Converting every `TableRef`, `ColumnRef`, and `LineageEdge` to LangChain
   `GraphDocument` / `Node` / `Relationship` objects
2. Losing control over the KùzuDB DDL schema (KuzuGraph creates its own generic schema)
3. Adding a new heavy dependency (`langchain-kuzu` pulls in the full LangChain graph chain)

The hand-rolled MERGE queries use parameterised bindings and property-key validation,
which is the correct injection-safe approach for a typed, application-controlled schema.
The reviewer's concern likely stems from the verbosity of the MERGE construction in
`upsert_node()` and `upsert_edge()`, which is real but is an implementation detail, not
an architectural problem.

Recommended response to reviewer: Document in a code comment on `upsert_node()` that
`langchain_kuzu.KuzuGraph` was evaluated but rejected because it requires converting to
LangChain's generic `GraphDocument` schema and does not support the typed DDL schema
this project uses. The hand-rolled Cypher is the correct approach given the custom schema.

This does not require a code change. It is a documentation/comment issue.

**Comment 6 — `queries.py` line 6: `DELETE_COLUMNS_FOR_FILE` without APOC**

Reviewer text: "again, shouldn't we use APOC procedures?"

Assessment: **Same analysis as Comment 4. APOC is not applicable here; the concern is
misdirected.**

`queries.py` contains parameterised Cypher strings used with `{path: $path}` bindings.
All user-controlled input (`file_path`) flows through the `$path` parameter, not via
string interpolation. There is no injection risk in these queries. APOC's utility
procedures do not offer a safer pattern for this type of bounded-parameter delete.

The real concern behind this comment is likely `DETACH DELETE` — which deletes a node
and all its relationships. If `$path` matched more nodes than expected (e.g. a path
prefix match), multiple files could be deleted. However, `{path: $path}` is an exact
equality match in Cypher, not a pattern match, so this cannot happen.

Recommended resolution: Add a code comment above each `DETACH DELETE` in `queries.py`
stating "DETACH DELETE is safe here because `$path` is an exact equality filter — only
one File node per path exists (primary key on File.path)" to address the reviewer's
concern directly.

**Comment 7 — `parsers/base.py` line 232: `_classify` uses `elif` chains instead of `match`**

Reviewer text: "why not use match case? also why are we using strings here and not just
the exp?"

Assessment: **Valid style concern. Using `match`/`case` is idiomatic Python 3.10+ and
this project targets 3.12+.**

The reviewer raises two sub-questions:

1. "Why not `match`/`case`?" — The `elif isinstance(...)` chain is Python 3.9-style.
   A `match stmt` statement with `case exp.Select()`, `case exp.Insert()`, etc. is
   cleaner and more readable. Since the project requires `>=3.12`, there is no reason to
   avoid `match`. This is a style issue with no correctness consequence.

2. "Why are we using strings here and not just the `exp`?" — The reviewer suggests
   returning sqlglot expression types directly (`type(stmt)` or the `exp.X` class) rather
   than opaque string literals like `"SELECT"`, `"INSERT"`, `"CREATE_TABLE"`. This is a
   more substantive design question. The string-based `kind` field is already persisted to
   the graph database (it is a property on `SqlQuery` nodes) and is used in MCP tool
   responses. Changing it to a sqlglot type reference would couple the graph schema to the
   sqlglot class hierarchy, which is undesirable. The string `kind` is the correct
   serialisation-safe representation.

   However, the reviewer's concern about arbitrary magic strings is addressed by the
   existing docstring which lists the valid values. Extracting those strings into an
   `Enum` would be the idiomatic improvement.

Recommended resolution: Refactor `_classify` to use `match`/`case` (pure style, safe).
Leave the string `kind` values but extract them into a `QueryKind` `StrEnum` in
`parsers/base.py` or `core/schema.py` to eliminate magic string duplication.

**Comment 8 — `parsers/base.py` line 263 and line 294: `_real_tables` and `_convert_table_expr_to_ref` use `elif`**

Reviewer text: "same here match statements are much cleaner" / "match"

Assessment: **Same as Comment 7, first sub-point. Valid style concern. Apply `match`/
`case` to both methods.**

`_real_tables` uses `isinstance` checks on `hasattr` results (not type dispatch), so it
does not benefit from `match`/`case` for the `hasattr` parts. The `_convert_table_expr_to_ref`
method uses `elif isinstance(...)` chains on sqlglot expression types, which is directly
replaceable with `match`/`case`.

---

### 8.3 Action List (Ranked by Importance)

| Rank | Comment | File | Action | Priority |
|------|---------|------|--------|----------|
| 1 | Comment 2 — config coupling | `src/sqlcg/core/config.py` | Introduce `KuzuConfig` and `Neo4jConfig` Pydantic models; `get_backend()` builds the config from env vars and validates before constructing the backend | MEDIUM |
| 2 | Comment 1 — module-level side effect | `src/sqlcg/core/config.py` | Remove `_load_env()` call from module level; call `load_dotenv()` explicitly from entry-point functions only (`server.main`, `cli.main`) | MEDIUM |
| 3 | Comment 7 — `_classify` style | `src/sqlcg/parsers/base.py` | Refactor `_classify` from `elif isinstance` to `match`/`case`; extract kind strings to a `QueryKind` `StrEnum` | LOW |
| 4 | Comment 8 — `_convert_table_expr_to_ref` style | `src/sqlcg/parsers/base.py` | Refactor `_convert_table_expr_to_ref` from `elif isinstance` to `match`/`case` | LOW |
| 5 | Comment 3 — regex vs `isidentifier` | `src/sqlcg/core/graph_db.py` | Replace `_IDENT_RE.match(key)` with `key.isidentifier()`; remove `import re` from this module | LOW |
| 6 | Comment 4 & 6 — APOC misunderstanding | `src/sqlcg/core/neo4j_backend.py`, `queries.py` | Add code comments clarifying why APOC is not applicable and why the current parameterised approach prevents injection | LOW (docs only) |
| 7 | Comment 5 — `langchain_kuzu` suggestion | `src/sqlcg/core/kuzu_backend.py` | Add code comment explaining why `KuzuGraph.add_graph_documents()` was not used (schema mismatch, dependency cost) | LOW (docs only) |

---

### 8.4 Architectural Findings from PR Review

**New finding — Config module has a side effect at import time (amplifies finding 3.3)**

The module-level `_load_env()` call in `config.py` is a form of shared mutable state
initialisation at import time. It interacts with finding 3.3 (SchemaResolver thread
safety): if two threads import `config` concurrently during a `watch` job and the `.env`
file happens to be present, `load_dotenv()` will be called twice. While `python-dotenv`
is itself thread-safe for the read path, the pattern of hiding side effects in module
imports is a code quality issue that should be resolved before the project matures.

**New finding — No typed config validation layer (related to finding 3.8)**

The current `get_backend()` function reads env vars and constructs a backend with no
validation. If `SQLCG_DB_PATH` is set to a path that is not writable, the error surfaces
as a KùzuDB internal exception rather than a helpful config validation error. Pydantic
validators on `KuzuConfig` / `Neo4jConfig` models would surface this at startup with a
clear message. This is consistent with the reviewer's Comment 2.

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

## 6. ETL Lineage Gap

Analysis date: 2026-05-03
Triggered by: architect-reviewer Q&A — "why is ETL excluded and does that make sense?"

### 6.1 Is ETL explicitly excluded?

ETL is not named in the Non-Goals section of either the blueprint or `plan/sqlcg.md`.
The Non-Goals list covers: `add_information_schema` implementation, the wizard,
mixed-dialect single-directory indexing, DataHub integration beyond the snowflake
optional extra, recursive CTE column-level lineage, and production KùzuDB cluster
mode. There is no line that says "ETL pipelines are out of scope."

ETL is excluded by omission — the design documents simply never name ETL as a target
use case. The stated niche is "a corpus of `.sql` files" with the example being DDL
and DML that already live in version-controlled repos. The DWH validation target
(Phase 7) indexes `ddl/` only, which is pure DDL.

### 6.2 What SQL patterns are currently in scope

The `_classify()` method on `SqlParser` recognises nine statement kinds:
`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE_TABLE`, `CREATE_VIEW`,
`CTAS` (CREATE TABLE ... AS SELECT), `COPY_INTO`, and `CREATE_PROCEDURE`/`UNKNOWN`.

`INSERT INTO ... SELECT` is fully in scope — it is classified as `INSERT` with
`sources` set to all tables in the SELECT body and column lineage extracted through
`sg_lineage`. `CTAS` (CREATE TABLE AS SELECT) is also explicitly handled. Standard
`COPY INTO table` (Snowflake loading from a stage) is handled with a `STAGE` source
node and `LOADS_FROM_STAGE` relationship, preventing a false table-to-table edge.

### 6.3 What ETL patterns are only partially handled

**Scripting blocks and stored procedures** (blueprint gaps 3 and 5) are the central
ETL gap. Snowflake Scripting blocks (`BEGIN ... END`) and `CREATE PROCEDURE` bodies
fall back to `exp.Command` in sqlglot. The `SnowflakeParser._parse_scripting_file`
method extracts embedded DML via a `_EMBEDDED_DML` regex and re-parses each match
independently. This recovers table-level edges at roughly 65-75% recall (blueprint
accuracy table), but column lineage is always `[]` and `confidence` is capped at 0.3.
The key limitation is that these extracted statements are parsed in isolation — there
is no session context. A `CREATE TEMP TABLE t AS SELECT ...` followed by
`INSERT INTO result SELECT * FROM t` produces two disconnected parse results, not
a chained lineage edge through `t`.

**DataHub `SqlParsingAggregator` as a partial fix.** The blueprint notes that the
`acryl-datahub[sql-parsing]` optional extra provides `SqlParsingAggregator`, which
maintains session-scoped temp table state across statements. This improves table-level
accuracy by ~15 points for scripting blocks and stored procedures. It is already
wired into the design for use inside `_parse_scripting_file`, but is an optional
dependency (`[snowflake]` extra) and still produces 0% column-level lineage for
procedural code.

**`COPY INTO ... FROM @stage`** is handled as a load event, not as lineage. The
graph records that a table was loaded from a stage, but there are no column-level
edges between the stage file schema and the target table columns, because the file
schema is not available to a static parser.

**dbt model chains.** The `dbt_adapter.py` loads `manifest.json` to enrich the
`SchemaResolver` with model column types and resolves `ref()` to qualified names.
This is schema enrichment, not ETL pipeline lineage. It resolves column types for
SQL inside dbt models, but it does not represent the multi-step dbt DAG as a lineage
graph. The dbt DAG is implicit — each dbt model's SQL is indexed as an independent
file; `CrossFileAggregator.resolve_pass2` resolves cross-model view references, which
approximates dbt lineage but is not a direct representation of it.

**Multi-step pipeline SQL.** A pattern common in Snowflake ETL:
```sql
CREATE OR REPLACE TEMP TABLE stage_orders AS SELECT ...;
INSERT INTO dwh.orders SELECT o.*, c.name FROM stage_orders o JOIN customers c ...;
```
These two statements, if they appear in the same file, are parsed as independent
`QueryNode` objects. `stage_orders` in the `INSERT` will resolve as a source table
reference, and if it was defined earlier in the same file it will appear in
`defined_tables`. However, column lineage through the temp table is broken: the
`sg_lineage` call for the INSERT sees `stage_orders` as a table with unknown schema
unless the DDL sniffer already registered its columns from the CTAS above.
Pass-1 DDL sniffing does register CTAS targets into `SchemaResolver`, so within a
single file this should partially work. Cross-file temp table chains do not resolve.

### 6.4 What full ETL lineage support would require

Four capabilities are missing and each requires non-trivial work:

1. **Cross-statement session context.** Temp tables and variables defined in one
   statement must be visible as schema to subsequent statements in the same session.
   `DataHub SqlParsingAggregator` with `session_id` is the right tool for intra-file
   session tracking. For cross-file session tracking (Snowflake tasks, dbt run
   sequences), there is no off-the-shelf solution — it would require an explicit
   pipeline execution order model, which is not representable in a static file walker.

2. **Stored procedure body full parsing.** Snowflake `$$...$$` blocks and T-SQL
   `BEGIN/END` bodies currently fall to regex extraction. A proper solution would
   use sqlglot's `dialect="snowflake"` parser on the extracted body after stripping
   the procedure wrapper. The blocker is that sqlglot classifies these as `exp.Command`
   and does not expose a stored-procedure-body parser. This is blueprint Gap 5,
   classified as a "design limitation" with "workaround only" resolution path.

3. **COPY INTO column lineage.** Inferring column mapping from a stage file to a
   target table requires either an explicit column list in the COPY statement or an
   external schema definition. This is a fundamental static-analysis limit (same
   category as Gap 4 — IDENTIFIER() dynamic references).

4. **Pipeline execution order.** For multi-step ETL (dbt DAG, Snowflake tasks,
   Airflow SQL operators), the pipeline DAG determines which temp table from step N
   is the source for step N+1. Without a DAG model, the graph can only record that a
   temp table exists and which columns it has — it cannot assert that a particular
   downstream table was populated from that temp table in a specific run order.

### 6.5 Deliberate deferral, plan gap, or fundamental limit?

It is all three, depending on the ETL pattern:

- **Deliberate deferral**: The Non-Goals section defers `add_information_schema` and
  DataHub pipeline integration. The spirit of this is "we index static SQL files, not
  pipeline orchestrators." ETL pipeline SQL *files* are in scope; the pipeline
  execution model is not.

- **Plan gap**: Multi-step intra-file temp table chains are partially solvable with
  the existing `SchemaResolver` DDL sniffer plus `DataHub SqlParsingAggregator` for
  session context. This is not described as an explicit gap anywhere in the plan — it
  falls out of the existing machinery silently rather than being named and handled.
  The benchmark targets do not include a "multi-step ETL" fixture, so the quality of
  this path is unmeasured.

- **Fundamental limit**: Dynamic SQL via `IDENTIFIER()`, cross-session temp tables,
  and COPY INTO column mapping are fundamental static-analysis limits acknowledged in
  the blueprint as such. They cannot be resolved without runtime information.

### 6.6 Does the exclusion make sense?

The exclusion is reasonable for v1 with one caveat. The design targets "a corpus of
`.sql` files" — the canonical example being DDL and DML in a data warehouse repo.
In that context, the most common lineage questions are "which tables does this view
read?" and "which downstream views depend on this table?" — both answerable with the
current scope.

The caveat is that the DWH validation target in Phase 7 explicitly indexes Snowflake
DDL and views, but the DWH's actual data movement happens in ETL jobs that are not
`.sql` files in `ddl/`. If the goal is full production lineage for the DWH, the
Phase 7 results will show good lineage within the DDL layer and zero lineage for the
ETL-to-DDL boundary. This should be stated explicitly in the Phase 7 parse quality
report (`docs/DWH_PARSE_REPORT.md`) rather than leaving it as an unexplained gap in
the lineage graph.

The practical recommendation: add a synthetic multi-step ETL fixture
(`tests/fixtures/synthetic/etl_chain.sql`) with known expected lineage to benchmark
whether the current pass-1 DDL-sniffing + SchemaResolver path resolves intra-file
temp table chains correctly. If it does, document it as a supported case. If it does
not, name it as a known limitation rather than leaving it as unmeasured silent
degradation.

---

## 7. Summary Assessment

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
