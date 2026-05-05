# Architecture Review — sql-code-graph (sqlcg)

Blueprint version: v1.2 (May 2026)
Review date: 2026-05-02 (updated 2026-05-05)
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

---

## 9. Phase 10 — Deployment & PyPI Publishing

Review date: 2026-05-04
Author: architect-planner

### 9.1 Goal

`pip install sql-code-graph` (or `uvx sql-code-graph`) followed by `sqlcg install`
registers the MCP server in Claude Code's `~/.claude/settings.json` with no manual
JSON editing. Tag-triggered GitHub Actions workflow publishes to PyPI automatically.

### 9.2 Architecture Decisions

**Settings file target**: `~/.claude/settings.json` under the `mcpServers` key.
This is the same file used by both Claude Desktop and Claude Code. The `mcp.json`
path previously targeted by `mcp setup --write` was incorrect and is removed.

**Command detection at install time**: `shutil.which("uvx")` determines which
invocation is written to the settings file. If `uvx` is available, the entry uses
`{"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}`. Otherwise it
falls back to `{"command": "sqlcg", "args": ["mcp", "start"]}`.

**Atomic write**: settings file is written via a `.tmp` sibling + `os.replace` to
prevent data corruption if the process is interrupted.

**Idempotency**: if the `sql-code-graph` key already exists in `mcpServers` with
an identical value, the install command prints "Already configured" and exits 0
without touching the file.

**PyPI publishing**: OIDC trusted publishing via `pypa/gh-action-pypi-publish`.
No long-lived API tokens stored in GitHub secrets. The publish job is gated behind
the full test matrix (`needs: test`).

**Version synchronisation**: commitizen `version_files` must include both
`pyproject.toml:version` and `src/sqlcg/__init__.py:__version__`. A version bump
via `uvx commitizen bump` updates both atomically.

### 9.3 New Files

| File | Purpose |
|------|---------|
| `src/sqlcg/cli/commands/install.py` | `sqlcg install` command implementation |
| `tests/unit/test_install.py` | 10+ unit tests for install logic |
| `.github/workflows/publish.yml` | Tag-triggered PyPI publish workflow |

### 9.4 Modified Files

| File | Change |
|------|--------|
| `pyproject.toml` | Author, `[project.urls]`, commitizen `version_files` |
| `src/sqlcg/cli/commands/mcp.py` | Fix `--write` target path and merge strategy |
| `src/sqlcg/cli/main.py` | Register `install_cmd` |

### 9.5 Operational Pre-condition (Blocking)

Before the `v*` tag can trigger a successful PyPI publish, the PyPI project must
have the GitHub repository configured as a trusted publisher:

1. Log in to https://pypi.org as `Warhorze`
2. Navigate to Account Settings > Publishing
3. Add publisher: owner=`Warhorze`, repo=`sql-code-graph`, workflow=`publish.yml`,
   environment=`pypi`
4. Perform the first publish locally via `uv publish --token <api-token>` to create
   the project on PyPI (OIDC cannot create a new project, only publish to an existing one)
5. From the second release onwards, tag-triggered OIDC publishing is fully automatic

### 9.6 Constraints from Existing Architecture

- No new runtime dependencies. All install logic uses stdlib (`json`, `os`,
  `pathlib`, `shutil`).
- The existing `typer` + `rich` stack is used for the install command, consistent
  with all other CLI commands.
- Unit tests use `tmp_path` exclusively; no test may write to `~/.claude/`.
- The publish workflow must pass the full `test.yml` matrix before publishing.
  This is enforced via `needs: test` in the workflow YAML.

Full implementation spec: `plan/phase10_deployment.md`

---

## 10. GitHub Issues Review — v0.2.1 Feedback (2026-05-05)

Review date: 2026-05-05
Source: GitHub issues #5 and #6, Warhorze/sql-code-graph
Tester environment: sqlcg 0.2.1, Snowflake dialect, 1457-file DWH corpus, WSL2/Linux, Claude Code MCP session

Both issues are marked `[feedback]` and were filed by the repo owner after a real
production session. They are high-signal because they reflect actual LLM-agent and
end-user experience, not hypothetical concerns.

---

### 10.1 Issue #6 — No clean uninstall / opt-out path

**Problem statement**: The tool installs side effects in three separate locations
(`~/.claude/settings.json`, `~/.sqlcg/`, `.git/hooks/post-checkout`) with no
corresponding removal command. A user who wants to remove the tool must discover and
manually undo all three locations.

**Architectural assessment**: The `sqlcg install` command was designed with symmetry in
mind (Phase 10), but only the install direction was implemented. The three side-effect
locations map directly to the three install operations:

1. `sqlcg install` writes `mcpServers["sql-code-graph"]` to `~/.claude/settings.json`
2. The user's KùzuDB lives at `~/.sqlcg/` (controlled by `SQLCG_DB_PATH` or the default
   set in `config.py`)
3. `sqlcg git install-hooks` writes `.git/hooks/post-checkout`

A `sqlcg uninstall` command is the natural symmetric counterpart and follows the exact
same pattern as `install.py`:

- Remove `mcpServers["sql-code-graph"]` from `~/.claude/settings.json` using atomic
  `.tmp` + `os.replace` write (same guard as install)
- Delete `~/.sqlcg/` (or the path from `SQLCG_DB_PATH`) via `shutil.rmtree` with
  a `--keep-db` flag to allow MCP deregistration without data loss
- Remove the `# sqlcg post-checkout hook` sentinel block from `.git/hooks/post-checkout`
  in the specified repo (or `Path.cwd()` by default); if the hook file becomes empty
  after stripping, delete it

**Risk**: LOW severity (no data is corrupted). HIGH usability impact — an unclean
opt-out path erodes trust and makes the tool feel invasive.

**New finding (10.A)**: The three side-effect locations are not documented anywhere in
`--help` or the README. Even without `sqlcg uninstall`, documenting them explicitly
would reduce user friction when opting out.

---

### 10.2 Issue #5 — LLM Agent Experience: Silent Failures and False Positives

This is a compound issue with nine distinct sub-problems. Each is assessed separately
below in order of severity.

---

#### 10.2.1 [CRITICAL] LLM cannot self-recover from package/binary name mismatch

The PyPI package is `sql-code-graph`; the binary is `sqlcg`. When an LLM agent
invokes the MCP server by its package name (`sql-code-graph`) it gets "executable not
found" and cannot recover without human intervention.

The `mcp setup` / `sqlcg install` JSON already writes the correct invocation
(`uvx sql-code-graph` or `sqlcg`), but the LLM does not read its own MCP settings
file — it relies on the MCP server's tool descriptions to understand how to invoke it.

**Root cause**: No tool or `--help` output names the binary explicitly with context.
The `list_dialects_and_repos` tool description could carry this hint, but currently
does not.

**Resolution**: Add a one-line note to the `sqlcg mcp setup` JSON output and to the
`list_dialects_and_repos` / `index_repo` tool docstrings: "Binary is `sqlcg`; PyPI
package is `sql-code-graph`." This is a docstring-only change with no code impact.

---

#### 10.2.2 [CRITICAL] No workflow order surfaced in `--help` or tools

The required sequence (`db init` -> `index <path>` -> `git install-hooks`) is not
implied by `--help`, `mcp setup`, or any tool description. During the test session
the LLM jumped directly to `find`/`analyze` and received empty results for the entire
session because `db init` and `index` were never run.

The `watch` command currently has no initialization guard: it calls
`backend.init_schema()` internally (correct), but `sqlcg watch` on an empty database
with no index will start the watcher, produce no results, and give no indication that
the database has zero content.

**Resolution**:

1. Add a `QUICK START` block to the top-level `--help` text in `cli/main.py`
   (within the `Typer` `help=` parameter string), listing the three required steps
   in order. `typer` renders this in the `--help` output verbatim.

2. In every tool that calls `_assert_indexed()`, surface the error message as:
   "No repos indexed. Run `sqlcg db init` then `sqlcg index <path>` first."
   (The current `NotIndexedError` message already says this but uses the CLI form;
   confirm it matches the actual binary name.)

3. The `watch` command is fine architecturally — it calls `init_schema()` before
   starting the observer — but it should print a warning if zero repos are indexed
   after the initial full index: "Warning: 0 tables indexed. Check that the path
   contains .sql files and the dialect is correct."

---

#### 10.2.3 [HIGH] `db info` gives no warning when `SqlColumn: 0`

`db info` shows node counts for all labels including `SqlColumn`. When `SqlColumn: 0`,
the `trace_column_lineage` and `get_downstream_dependencies` / `get_upstream_dependencies`
tools are completely non-functional — they will always return empty results. The user
cannot distinguish "column not found" from "column graph not built."

The current `db info` implementation (in `db.py`) iterates all `NodeLabel` values and
prints counts without any interpretation. A zero count on `SqlColumn` is printed
identically to a non-zero count.

**Resolution**: After printing counts, add a structured health check section to
`db info` output:

- If `SqlColumn == 0` and `SqlQuery > 0`: print a yellow warning: "Column lineage
  not available. Tools `trace_column_lineage`, `get_downstream_dependencies`, and
  `get_upstream_dependencies` will return empty results."
- If `SqlQuery == 0` and `Repo > 0`: print a yellow warning: "No queries indexed.
  Run `sqlcg index <path>` to populate the graph."
- If `Repo == 0`: print a red error: "Database is empty. Run `sqlcg db init` and
  `sqlcg index <path>` first."

This is also relevant to the MCP server: `list_dialects_and_repos` could append
a `warnings` field to its `DialectRepoResult` listing health status, so Claude
can surface it proactively.

---

#### 10.2.4 [HIGH] `analyze unused` returns 100% false positives for external consumers

`analyze unused` returned ~100 `IA_ANALYTICS` / `IA_TABLEAU` / `IA_BUSINESSOBJECTS`
views as "unused" because these are consumer-facing views queried by Tableau and BI
tools — external consumers with no SQL references within the indexed corpus.

The current query in `analyze.py`:
```
MATCH (t:SqlTable) WHERE NOT (t)<-[:SELECTS_FROM]-() RETURN t.qualified
```
is logically correct for the closed-world assumption (only SQL files in the indexed
corpus), but this assumption is never stated to the user.

**Resolution**:

1. Add a `--exclude-schema` flag to `analyze unused` that filters out tables whose
   qualified name starts with the specified schema prefix. This allows users to exclude
   known consumer schemas: `sqlcg analyze unused --exclude-schema IA_ANALYTICS`. 

WR: it needs to be very clear to the user what it is that they're doing when to exclude ? when not? agian this information should be past to the llm otherwise it is unclear. it might be usefull if you'd look at it from a medalion architectur perspective.

2. Always print a caveat after results: "Note: results include tables not referenced
   within the indexed files. Externally consumed tables (e.g., Tableau/BI views) will
   appear as unused." This caveat should also appear in the MCP tool docstring for
   `find_table_usages` and in the `analyze unused` `--help`.


3. The same caveat applies to the MCP server's future `find_unused_tables` tool (if
   planned).

---

#### 10.2.5 [HIGH] `analyze impact` vs `find pattern` return inconsistent results for the same table

For table `BA.WTFV_VOORRAAD_DAGSTAND_IGDC`, `analyze impact` returned only DDL files
while `find pattern` additionally found an ETL `INSERT INTO` in `etl/sql/fact/`. The
two commands should return overlapping or identical results for this query.

**Root cause analysis**: `analyze impact` uses the `SELECTS_FROM` relationship to find
queries that reference the table. If the ETL `INSERT INTO` statement was indexed as a
query with `sources` pointing to `BA.WTFV_VOORRAAD_DAGSTAND_IGDC` via a `SELECTS_FROM`
edge, it would appear in both commands. The inconsistency suggests one of:

(a) The ETL INSERT statement was not indexed with the correct `SELECTS_FROM` edges (the
    parser did not extract table sources for it), or

(b) The INSERT was indexed under a different table name/qualified form than the DDL
    table node, causing the relationship to point to a different node.

**Resolution**: This is a parser correctness issue, not a CLI design issue. The fix
is to verify that `INSERT INTO ... SELECT FROM <table>` correctly emits a `SELECTS_FROM`
edge from the `SqlQuery` node to the `SqlTable` node. Add a test fixture with a known
INSERT-SELECT pattern and assert that `analyze impact` finds it. The diagnostic step
is: run `execute_cypher("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable {qualified:
'BA.WTFV_VOORRAAD_DAGSTAND_IGDC'}) RETURN q.id LIMIT 10")` and compare to
`find pattern "WTFV_VOORRAAD_DAGSTAND_IGDC"`.

New finding: **the impact/pattern inconsistency is a measurable regression test gap**.
The `_upsert_parsed_file` path must create `SELECTS_FROM` edges for INSERT statements,
not only SELECT statements. Verify this is handled.

---

#### 10.2.6 [MEDIUM] Feedback loop has no false-negative path

`submit_feedback` only fires when there are results to rate. The most valuable signal —
empty result when the user expected one — is never collected. In the test session,
`trace_column_lineage` was called 7 times (all returning empty) and 0 feedback samples
were collected.

Additionally, `execute_cypher` was the #1 MCP tool with 15 calls vs
`trace_column_lineage` at 7. A high `execute_cypher`/`high-level-tool` ratio is a
proxy signal that the high-level tools are failing and the LLM is falling back to raw
Cypher. This ratio is not surfaced in `sqlcg gain`.

**Resolution**:

1. In `trace_column_lineage`, `get_downstream_dependencies`, and
   `get_upstream_dependencies`: when the result `lineage` / `nodes` list is empty,
   include a `hint` field in the returned model: "If you expected results, check that
   `db info` shows SqlColumn > 0. Submit feedback with `submit_feedback` tool if this
   was a false negative."

2. In `submit_feedback`, add a `FN` (false negative) label to the allowed set alongside
   `TP` and `FP`. Currently only `TP` and `FP` are valid labels.

3. In `sqlcg gain`, add a `execute_cypher / total_calls` ratio section. A ratio above
   0.3 (more than 30% of calls are raw Cypher) is a signal that the high-level
   abstractions are not working.

---

#### 10.2.7 [HIGH] Parser: noise statements in scripting blocks

`ALTER WAREHOUSE IDENTIFIER($var) SET WAREHOUSE_SIZE = 'X-Large'` is a standard
Snowflake DDL rebuild pattern that fails to parse and falls back to `exp.Command`.
`CALL MA.MSSPR_*()` stored procedure calls similarly produce noise. These were the
two observed instances, but scripting blocks can contain many other non-DML statement
types: `SET variable = value`, `LET x := y`, `EXECUTE IMMEDIATE`, `RETURN`, `RAISE`,
`OPEN/FETCH/CLOSE` (cursor ops), `TRUNCATE TABLE`, and any `CREATE/DROP/ALTER` inside
a block. Enumerating them per-dialect is a maintenance trap.

**Root cause**: `_EMBEDDED_DML` is a regex applied to the raw SQL text. It does not
understand statement boundaries (semicolons inside string literals break it — finding
3.12) and has no way to classify what it captures. Any statement fragment that leaks
through gets handed to `sqlglot.parse()` and produces a noisy `exp.Command` error.

**Resolution**: Replace the `_EMBEDDED_DML` regex extraction with a two-step
sqlglot-native approach in `SnowflakeParser._parse_scripting_file`:

1. **Split on statement boundaries** using `sqlglot.tokenize()` + semicolon tokens.
   The tokenizer handles semicolons inside string literals correctly — no regex needed.
2. **Classify each chunk** using sqlglot's built-in `exp.DML` base class:

   ```python
   isinstance(stmt, (exp.DML, exp.Select)) and not isinstance(stmt, exp.Copy)
   ```

   `exp.DML` is a sqlglot base class that covers `Delete`, `Insert`, `Update`, and
   `Merge` in a single isinstance check — no per-statement enumeration needed.
   `exp.Select` is added separately (it inherits from `Query`, not `DML`).
   `exp.Copy` (Snowflake `COPY INTO`) is excluded because its source is a file stage,
   not a table — no table lineage to extract.
   Everything else — `exp.Command`, `exp.DDL`, utility statements — is dropped and
   logged at DEBUG level, not WARNING.

**DML coverage after this fix:**

| Statement | sqlglot type | Included |
|---|---|---|
| SELECT | `exp.Select` | Yes |
| INSERT INTO … SELECT / VALUES | `exp.Insert` (via `exp.DML`) | Yes |
| UPDATE | `exp.Update` (via `exp.DML`) | Yes |
| DELETE | `exp.Delete` (via `exp.DML`) | Yes |
| MERGE INTO | `exp.Merge` (via `exp.DML`) | Yes — was missing from original proposal |
| COPY INTO (Snowflake) | `exp.Copy` (via `exp.DML`) | No — stage source, not a table |
| CREATE TABLE AS SELECT | `exp.Create` (via `exp.DDL`) | Handled separately (see below) |
| ALTER WAREHOUSE, CALL, SET, LET, … | `exp.Command` or specific DDL | Dropped at DEBUG |

**CTAS**: `CREATE TABLE AS SELECT` produces `exp.Create`, not `exp.DML`. The existing
`AnsiParser._parse_statement` already handles CTAS by detecting a SELECT inside a
Create node. In the scripting-block path, pass `exp.Create` instances through to
`_parse_statement` rather than dropping them — it will handle them or ignore them
cleanly.

**Procedure bodies — dialect differences:**

The tokenizer approach above handles scripting-block files (non-procedure files with
`BEGIN` blocks) correctly across all dialects. Procedure bodies are different:

| Dialect | How sqlglot exposes the body | Extractable? |
|---|---|---|
| Snowflake | `exp.RawString` node inside `exp.Create` | Yes — extract, strip `BEGIN`/`END`, re-apply step 1–2 |
| Databricks | `exp.RawString` (same `$$` delimiter) | Yes — same as Snowflake |
| BigQuery | `exp.Command` inside `exp.Create` | No — body is opaque; regex fallback same as today |

For Snowflake/Databricks: detect `exp.Create` nodes that contain an `exp.RawString`
body, extract the string, strip the outer `BEGIN ... END` wrapper, then run the
tokenizer-split + DML filter on the inner content recursively. For BigQuery: leave the
current `_EMBEDDED_DML` regex path as a fallback for `exp.Command` bodies only — do
not regress existing behaviour.

**Files**: `src/sqlcg/parsers/snowflake_parser.py` — remove `_EMBEDDED_DML` regex,
rewrite `_parse_scripting_file`. No schema or MCP impact. Test fixtures required:
- Scripting block with `ALTER WAREHOUSE`, `CALL`, `MERGE INTO`, and `INSERT INTO … SELECT` — assert only MERGE and INSERT are indexed
- Snowflake procedure with embedded DML — assert body DML is indexed
- BigQuery procedure — assert existing regex fallback still extracts DML

---

#### 10.2.8 [MEDIUM] Missing persistent index config (`.sqlcg.toml`)

After a `db reset` or fresh clone there is no way to replay the index without
remembering the original `sqlcg index <path> --dialect <dialect>` invocation. The
git hook calls `sqlcg index --dialect auto --quiet` which reads `.sqlcg.toml` if it
exists, but this file is never created by any `sqlcg` command.

**Current state**: The `get_dialect()` helper in `config.py` reads `.sqlcg.toml`
(via `[sqlcg] dialect = "..."`) if present, but `sqlcg index` never writes this file.
A user must create `.sqlcg.toml` manually.

**Resolution**: `sqlcg index <path> --dialect <dialect>` should write (or update) a
`.sqlcg.toml` file at the root of the indexed path on first successful index. The
write should be idempotent (do not overwrite if the file already exists with a matching
dialect). This makes the git hook self-healing after a `db reset` — the hook will read
the dialect from `.sqlcg.toml` without the user needing to remember the original
invocation.

Spec for `.sqlcg.toml`:
```toml
[sqlcg]
path = "/absolute/path/to/repo"
dialect = "snowflake"
```

The `path` field allows `sqlcg index` (with no arguments) to infer the target
directory from the config file when run from the repo root.

**Risk**: Low — no breaking changes. The file should be added to `.gitignore` templates
in the README since it contains an absolute path that is machine-specific.

---

#### 10.2.9 [LOW] No progress feedback during `sqlcg index`

`sqlcg index` on 1457 files took ~17 seconds with zero stdout output until the final
summary line appeared. The `edges: 0` signal (indicating the graph has no relationships
and lineage tracing will return nothing) appeared only in the raw log, never surfaced
in `db info`.

**Resolution**:

1. Add a simple periodic progress line to `Indexer.index_repo()`: print
   `"INFO: indexed N/total files..."` every 100 files (or every 5 seconds on a timer).
   Use `rich.progress` if available, or a plain `console.print` with carriage return.

2. After indexing, if `lineage_edges_created == 0`, print a yellow warning to stdout
   (not just the log): "Warning: 0 lineage edges created. Column-level lineage tracing
   will not be available. Check parse errors above."

3. Surface `lineage_edges_created` (renamed to `edges`) in `db info` as an explicit
   field. A graph with zero edges cannot answer any lineage query, and this must be
   immediately visible without reading the raw index log.

---

#### 10.2.10 [LOW] `uvx` cold-start is invisible (6.5 minutes with no output)

The MCP server config (`"command": "uvx"`) means Claude Code pays a `uvx` download
cost on first run (and potentially on each session if the `uvx` cache is cold). In
the test session this took 6.5 minutes with zero output, indistinguishable from a hang.

**Resolution**: Documentation-only change. The README `QUICK START` section should:
- Recommend `uv tool install sql-code-graph` for persistent local installs
- Reserve `uvx sql-code-graph` for one-shot tries
- Note that first run will be slow due to package download
- The `sqlcg install` confirmation message should note: "Using uvx — first MCP startup
  may take several minutes on a cold cache. Run `uv tool install sql-code-graph` for
  faster startup."

---

### 10.3 Cross-Cutting Findings from Issue Analysis

**Finding 10.B — LLM agent is a first-class user of this tool but the tool was not designed for it**

The issue session revealed a consistent pattern: the tool produces correct outputs but
does not communicate machine-readable semantics about its own state. A human user who
gets "No results" knows to check if the database was indexed. An LLM agent does not.
Every tool that can return empty due to a missing prerequisite (unindexed graph, zero
column nodes, schema mismatch) must include a `hint` field in the returned model so
Claude can self-diagnose without falling back to `execute_cypher`.

This is the root cause of the `execute_cypher`-dominance pattern (15 calls vs 7 for
`trace_column_lineage`): Claude fell back to raw Cypher because the high-level tools
returned empty with no explanation.

**Action**: Add a `hint: str | None` field to `LineageResult`, `DependencyResult`,
`TableUsageResult`, and `SqlPatternResult` models. Populate it when the result list
is empty with a diagnostic string. This is a backward-compatible model extension
(new optional field).

**Finding 10.C — Index success rate is misleading**

The index reported 100% success rate with 900KB of parse warnings and `SqlColumn: 0`.
"Success" in the current codebase means "the file was processed without raising an
unhandled exception." It does not mean "lineage was extracted." A file that falls back
to `exp.Command` (scripting block) is counted as "success" even though it produced
zero edges.

This is related to finding 3.4 (bare `except Exception: continue`) — it means errors
are swallowed and the success rate is a vanity metric.

**Action**: Introduce a `parse_quality` metric in `ParsedFile`:
- `full`: sqlglot parsed fully, column lineage extracted
- `table_only`: tables extracted, column lineage not available (schema required or
  `SELECT *`)
- `scripting_fallback`: fell back to regex extraction, confidence 0.3
- `failed`: unhandled exception, no lineage

Report these four categories in the `index` summary line and in `db info`. This
replaces "parse_errors" (which is binary) with a quality breakdown that accurately
reflects what Claude can and cannot answer.

---

### 10.4 Prioritized Implementation Plan — Issues #5 and #6

Ranked by impact (breadth of user pain) multiplied by implementation effort (inverse:
low effort ranks higher).

| Rank | Issue | Finding | File(s) | Action | Effort | Impact |
|------|-------|---------|---------|--------|--------|--------|
| 1 | #5.1 | 10.2.2 | `cli/main.py` | Add `QUICK START` block to top-level `--help` with ordered steps; update `NotIndexedError` message to include step hint | XS | CRITICAL |
| 2 | #5.2 | 10.2.3 | `cli/commands/db.py` | Add health check interpretation to `db info` output (red/yellow warnings for empty Repo, zero SqlQuery, zero SqlColumn) | XS | HIGH |
| 3 | #6 | 10.A, 10.1 | `cli/commands/uninstall.py` (new) | Implement `sqlcg uninstall`: remove MCP entry, delete `~/.sqlcg/`, strip git hook; `--keep-db` flag; register in `cli/main.py` | S | HIGH |
| 4 | #5.2 | 10.2.9 | `indexer/indexer.py`, `cli/commands/index.py` | Add periodic progress output; surface `edges: 0` warning on stdout; add `edges` to `db info` | S | HIGH |
| 5 | #5.2 | 10.B | `server/models.py`, `server/tools.py` | Add `hint: str | None` to `LineageResult`, `DependencyResult`, `TableUsageResult`; populate on empty returns with diagnostic message | S | HIGH |
| 6 | #5.2 | 10.2.8 | `cli/commands/index.py`, `core/config.py` | Write `.sqlcg.toml` on successful index (idempotent); update `get_dialect()` to also read `path` from toml; allow `sqlcg index` with no args | S | MEDIUM |
| 7 | #5.2 | 10.2.4 | `cli/commands/analyze.py` | Add `--exclude-schema` to `analyze unused`; always append closed-world caveat to output | S | MEDIUM |
| 8 | #5.2 | 10.2.6 | `server/tools.py`, `metrics/store.py` | Add `FN` label to `submit_feedback`; add `execute_cypher` ratio to `sqlcg gain` output | S | MEDIUM |
| 9 | #5.2 | 10.2.7 | `parsers/snowflake_parser.py` | Add pre-filter for `ALTER WAREHOUSE` and `CALL` statements in `_parse_scripting_file`; log at DEBUG not WARNING | XS | MEDIUM |
| 10 | #5.2 | 10.2.5 | `indexer/indexer.py`, `parsers/base.py` | Verify `SELECTS_FROM` edges are created for INSERT-SELECT queries; add test fixture asserting `analyze impact` finds ETL INSERT — **elevated to HIGH (confirmed parser bug, see 10.6 Q4)** | M | HIGH |
| 11 | #5.2 | 10.C | `indexer/indexer.py`, `parsers/base.py`, `cli/commands/index.py` | Introduce `parse_quality` breakdown (full / table_only / scripting_fallback / failed); surface in index summary and `db info` | M | MEDIUM |
| 12 | #5.2 | 10.2.1 | `server/tools.py`, `cli/commands/mcp.py` | Add binary/package name note to `mcp setup` output and `index_repo`/`list_dialects_and_repos` tool docstrings | XS | LOW |
| 13 | #5.2 | 10.2.10 | `README.md` / install docs | Add `uv tool install` recommendation; note cold-start latency in `sqlcg install` confirmation message | XS | LOW |

Note (2026-05-05): Ranks 3, 6, 8, and 10 were blocked on open questions. All four
are now resolved — see section 10.6. Rank 10 was elevated from MEDIUM to HIGH after
the user confirmed that `analyze impact` must include ETL INSERT statements (confirmed
parser bug, not a design constraint).

---

### 10.5 Impact on Existing Architecture Review Sections

**Finding 3.4 amplified (10.C)**: The bare `except Exception: continue` in
`_extract_column_lineage` was already a HIGH finding. Issue #5 confirms its real-world
consequence: a 1457-file corpus reports 100% success with `SqlColumn: 0`. This
elevates finding 3.4 from a theoretical correctness concern to a confirmed user-visible
failure mode. The `parse_quality` breakdown (rank 11 above) is the correct systemic fix.

**Finding 5.2 (wizard.py unspec'd) is now confirmed scope creep risk**: The wizard
was never mentioned in the test session. The LLM relied on `--help`. This confirms that
the QUICK START in `--help` (rank 1 above) is the highest-value onboarding investment,
not a GUI wizard.

**`analyze unused` (finding 5.3 / JOINS) extended**: The `analyze unused` false positive
issue (#5.3) confirms finding 5.3 from the original review — the graph uses a
closed-world assumption that must be made explicit to users. The `--exclude-schema` flag
and caveat text address this without changing the graph schema.

**`analyze impact` vs `find pattern` inconsistency (10.2.5)** is a new finding not
previously identified in this review. It points to a potential gap in `_upsert_parsed_file`:
`SELECTS_FROM` edges may not be created for all query kinds that reference tables as
sources. This requires a developer investigation before a fix can be scoped.

---

### 10.6 Open Questions from Issue Analysis — RESOLVED (2026-05-05)

All four open questions were resolved by user comments on the GitHub issues.

---

**Q1 — `sqlcg uninstall` interaction model: RESOLVED**

Decision: prompt the user before deleting the graph database; support `--force` to
skip the prompt. DB deletion is only offered when the database is local (KùzuDB
embedded at `~/.sqlcg/` or `SQLCG_DB_PATH`). Neo4j remote backends are never
deleted by `sqlcg uninstall` — only the MCP registration and git hook are removed.

Concrete interaction contract for `cli/commands/uninstall.py`:

- Step 1: always remove `mcpServers["sql-code-graph"]` from `~/.claude/settings.json`
  (atomic `.tmp` + `os.replace`, same guard as install).
- Step 2: if the active backend is local KùzuDB, prompt:
  "This will delete the graph database at `<path>`. Continue? [y/N]"
  If `--force` is passed, skip the prompt and proceed. If `--keep-db` is passed,
  skip both prompt and deletion.
- Step 3: remove the `# sqlcg post-checkout hook` sentinel block from
  `.git/hooks/post-checkout` in `Path.cwd()` (or `--repo <path>`). Delete the hook
  file if it becomes empty after stripping.
- Print a confirmation line per step taken.

The `--force` flag also deletes the SQLite metrics store (same `~/.sqlcg/` path)
per the user's comment "maybe also delete the database (graph + sqlite) when run
with --force."

Impact on implementation plan: rank 3 (`uninstall.py`) spec is now complete. No
further design questions.

---

**Q2 — `.sqlcg.toml` committing policy: RESOLVED**

Decision: make `path` optional in `.sqlcg.toml`; resolve to `cwd` when absent.
The file is therefore committable (no machine-specific absolute path).

Revised spec for `.sqlcg.toml`:

```toml
[sqlcg]
dialect = "snowflake"
# path is optional; defaults to the directory containing this file (cwd at index time)
```

`sqlcg index <path> --dialect <dialect>` writes this file at the root of the indexed
path. If `path` equals `cwd`, the `path` key is omitted entirely. `sqlcg index`
with no arguments reads `.sqlcg.toml` from cwd and uses `dialect` from it; `path`
defaults to cwd.

The file should NOT be added to `.gitignore`. Teams can commit it to share the
dialect config. The README should note that `.sqlcg.toml` without a `path` key is
safe to commit; a `path` key (if the user adds one manually for a non-cwd root)
is machine-specific and should be gitignored.

Impact on implementation plan: rank 6 (`.sqlcg.toml` write) spec is now complete.

---

**Q3 — `FN` label for `submit_feedback`: RESOLVED**

Decision: yes, add `FN` as a valid label. The MetricsStore schema change is
acceptable — the project has a no-backward-compatibility policy ("WE DON'T NEED TO
KEEP A VERSION, WE WILL BREAK THINGS CAUSE THE PACKAGE IS LIVE"). Schema migrations
are handled by re-initialising the SQLite metrics store on startup if the schema
version changes.

Implementation note: add `FN` to the `FeedbackLabel` enum (or string literal set)
in `metrics/store.py` and update the `submit_feedback` tool docstring and any
validation that guards the label field. No migration script is needed — the store
is a local append-only log; old records with `TP`/`FP` labels remain valid.

Impact on implementation plan: rank 8 (`submit_feedback` `FN` label) is unblocked.

---

**Q4 — `analyze impact` expected scope: RESOLVED**

Decision: `analyze impact` must include ETL INSERT statements that use the table as
a source. The inconsistency reported in issue #5 ("`analyze impact` returned only
DDL files while `find pattern` additionally found the ETL INSERT") is confirmed as a
parser bug, not an intentional design constraint. The user's issue description states
"the two commands should return consistent results."

Implication: `analyze impact` is not DDL-only. Its scope is all `SqlQuery` nodes
that reference the target table via `SELECTS_FROM` edges — SELECT, INSERT-SELECT,
CTAS, MERGE, and UPDATE-FROM all qualify. The bug is that `_upsert_parsed_file` may
not create `SELECTS_FROM` edges for INSERT statements. This must be verified and
fixed.

Priority escalation: the inconsistency (originally rank 10 in section 10.4) is now
confirmed as a parser bug and should be treated as HIGH priority. Elevate to rank 4
(tied with the `hint` field addition, rank 5). The regression test fixture
(`tests/fixtures/synthetic/etl_chain.sql` or a new `insert_select_impact.sql`)
is now a mandatory deliverable for the developer.

---

### 10.7 Policy Note — No Backward Compatibility Constraint

The user confirmed: "WE DON'T NEED TO KEEP A VERSION, WE WILL BREAK THINGS CAUSE
THE PACKAGE IS LIVE."

This policy applies to: MetricsStore SQLite schema, KùzuDB graph schema, MCP tool
response model shapes, `.sqlcg.toml` format, and the `pyproject.toml` dependency
pins. Breaking changes between releases are acceptable without a deprecation cycle.
The architect-reviewer notes this means:

- Finding 3.6 (`QueryNode` mutability) can be fixed with `frozen=True` without a
  migration path for serialised graph data (re-index is the migration).
- The `FN` label schema change (Q3) needs no migration script.
- The `parse_quality` breakdown (finding 10.C) can replace the existing `parse_errors`
  field in `ParsedFile` without an adapter layer.
- The `hint` field addition to result models (finding 10.B) can be non-optional if
  desired, though keeping it `str | None` is still the cleaner design.
