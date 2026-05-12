# Architecture Review — sql-code-graph (sqlcg)

Blueprint version: v1.2 (May 2026)
Review date: 2026-05-02 (updated 2026-05-06)
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

6. **DDL requirement / `add_information_schema`** — **Re-opened (2026-05-07): un-deferred
   to next sprint after column lineage fix.**
   Original resolution was to defer to v2 because table-level lineage works without schema.
   Re-opened because the v0.3.1 postmortem experiment (653 ETL files, Snowflake) confirmed
   that `SELECT *` and `SELECT base.*` are the dominant projection pattern in this corpus —
   T-02 now skips them cleanly but emits no edges. `sqlglot.optimizer.qualify.qualify(stmt,
   schema=schema)` can expand star projections into explicit column names when schema is
   available, unlocking column lineage for the majority of ETL files.

   Required work:
   1. **Two-pass index**: parse DDL corpus first (`ddl/` dir) → populate `SchemaResolver`;
      then parse ETL corpus using the populated schema. CLI needs `--schema-from-ddl <dir>`
      or automatic DDL-first ordering within `index_repo`.
   2. **`add_information_schema` implementation**: feed `CREATE TABLE` column definitions
      into `SchemaResolver.add_information_schema()` during the DDL pass. This was stubbed
      as `NotImplementedError` — it needs to be real.
   3. **Call `qualify` before `sg_lineage`**: in `_extract_column_lineage`, when schema is
      non-empty, call `qualify(stmt, schema=schema, dialect=self.DIALECT)` to expand stars
      before extracting `col_expressions`. Fall back gracefully when `qualify` raises.
   4. **Confidence model**: edges derived from a schema-expanded star carry `confidence=0.8`
      (not 1.0 — schema may be stale); edges from a named column with schema confirmation
      stay at 1.0.

   Risk: schema staleness. If DDL and ETL are out of sync, `qualify` may expand `SELECT *`
   to the wrong column list. Mitigate by surfacing `schema_source_file` on each edge so
   consumers can judge freshness.

   **Previous resolution** (kept for reference): "DDL is not required for the primary use
   case. Column-level lineage degrades gracefully when schema is absent."

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

**Additional improvement from 10.2.7 research**: sqlglot exposes `exp.DML` as a base
class covering `Insert`, `Update`, `Delete`, and `Merge`. The current `_classify`
enumerates these individually, which caused `MERGE` to be missing from the original
DML filter proposal (caught and fixed in 10.2.7). The `match`/`case` refactor should
use `exp.DML` as a base branch to future-proof against new DML types sqlglot may add:

```python
case stmt if isinstance(stmt, exp.DML) and not isinstance(stmt, exp.Copy):
    # handles Insert, Update, Delete, Merge — and any future exp.DML subclass
    ...
case exp.Select():
    ...
```

This ensures `_classify` inherits coverage from sqlglot's own type hierarchy rather
than requiring manual updates each time a new DML expression type is added.

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

### 3.5 ~~[MEDIUM]~~ **RESOLVED** `SnowflakeParser._parse_scripting_file` applies a file-level heuristic that can false-positive

> **Status: already fixed in code.** The token-aware `TokenType.BEGIN` check described
> below is implemented in `_has_scripting_block()` (lines 79–85 of
> `src/sqlcg/parsers/snowflake_parser.py`). This finding is closed.

~~The heuristic `if _SCRIPTING_BLOCK.search(sql): return self._parse_scripting_file()`
matches any file containing the word `BEGIN`. This includes:~~

~~- Comments: `-- BEGIN transaction isolation block`~~
~~- String literals: `SELECT 'BEGIN' AS status_label FROM t`~~
~~- Legitimate `BEGIN TRANSACTION` in non-scripting contexts~~

~~A false positive routes an entire file through the regex DML extractor, which sets
`parse_failed=True` and `confidence=0.3` on all statements and drops all column
lineage. This degrades quality on files that are fully parseable.~~

The implemented fix uses `sqlglot.tokens.Tokenizer.from_dialect("snowflake").tokenize(sql)`
and checks for a `TokenType.BEGIN` token directly — string literals and comments are
never tokenised as `BEGIN`, so false positives on `SELECT 'BEGIN' AS x` or
`-- BEGIN block` are impossible. A regex fallback is retained only for the edge case
where tokenisation itself raises an exception.

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
| 5 | ~~3.5 — `BEGIN` heuristic false-positives~~ | ~~MEDIUM~~ | ~~Low~~ | **RESOLVED** — token-aware `TokenType.BEGIN` check already implemented in `_has_scripting_block()`. No action needed. |
| 6 | 3.6 — `QueryNode` mutability inconsistency with frozen `LineageEdge` | MEDIUM | Low | Either add `frozen=True` to `QueryNode` and use `dataclasses.replace()` for pass-2 patching, or document the mutability contract explicitly in the class docstring |
| 7 | 3.7 — No per-file timeout or SIGINT handling during indexing | MEDIUM | Medium | Add a configurable `--timeout-per-file` (default 30s) enforced via `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=N)`; handle `KeyboardInterrupt` in the walker loop to flush and exit cleanly |
| 8 | 3.8 — `add_information_schema` is a silent no-op stub | MEDIUM | Low | Raise `NotImplementedError("--schema-from-info-schema is not yet implemented")` until the method is built; guard the CLI flag to surface this error immediately |
| 9 | 3.10 — `acryl-datahub` upper bound missing on unstable module | LOW | Low | Change to `"acryl-datahub[sql-parsing]>=0.14.0,<0.15.0"` in `pyproject.toml` |
| 10 | ~~3.12 — `_EMBEDDED_DML` regex greedy match across statement boundaries~~ | ~~LOW~~ | ~~Low~~ | **SUPERSEDED by 10.2.7** — the regex is being replaced entirely with a sqlglot tokenizer + `exp.DML` base class filter. Adding `re.MULTILINE` is no longer the fix. |
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
   `BEGIN/END` bodies currently fall to regex extraction. The blocker is
   **dialect-specific** — this is not a uniform limitation:

   - **Snowflake / Databricks**: sqlglot exposes the `$$...$$` body as an `exp.RawString`
     node inside the `exp.Create` AST. The body is extractable, strippable of its
     `BEGIN`/`END` wrapper, and re-parseable with the tokenizer + `exp.DML` filter
     described in finding 10.2.7. This path is now viable and is part of the 10.2.7
     implementation plan.
   - **BigQuery**: sqlglot cannot fully parse `CREATE PROCEDURE ... BEGIN...END` for
     BigQuery; the body lands as `exp.Command` inside `exp.Create`. For BigQuery, the
     regex fallback (`_EMBEDDED_DML`) remains the only option. This is the true
     "design limitation / workaround only" case.
   - **T-SQL**: not yet verified — likely similar to BigQuery (body as `exp.Command`).

   This is blueprint Gap 5. The Snowflake portion is now addressable via 10.2.7;
   BigQuery procedure bodies remain a fundamental static-analysis limit.

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

1. **Medallion-architecture-aware classification.** In a Bronze/Silver/Gold (medallion)
   warehouse, tables at each layer have different "unused" semantics:

   | Layer | Typical schema prefixes | Expected consumption pattern | "Unused" meaning |
   |---|---|---|---|
   | Bronze / Raw | `RAW_`, `STG_`, `STAGE_` | Loaded by COPY INTO / ELT jobs | Bug if unused — no ETL reads it |
   | Silver / Intermediate | `INT_`, `PREP_`, `DWH_`, `BA_` | Read by ETL INSERT/MERGE | Bug if unused |
   | Gold / Consumption | `DIM_`, `FACT_`, `MART_`, `IA_`, `RPT_` | Queried by BI tools externally | **Expected** — external consumers have no SQL in the corpus |

   `analyze unused` should tag each result with its inferred layer so the LLM can
   distinguish "this Gold view has no SQL consumers — expected, BI queries it" from
   "this Silver staging table has no SQL consumers — likely a dead table."

   Implementation: after the Cypher query, classify each result by matching its schema
   prefix against a configurable tier map (with sensible defaults). Surface the tier
   in the CLI output column and in the MCP tool's result model. The MCP tool docstring
   must explain the tier model so Claude can give informed recommendations without
   the user needing to explain their naming conventions.

2. Add a `--exclude-schema` flag to `analyze unused` for manual exclusion of known
   consumer schemas. The flag description must explain **when to use it**: "Use to
   exclude schemas whose tables are consumed externally (e.g., by BI tools or APIs)
   and will always appear unused within the indexed SQL corpus."

   The MCP tool version of this flag (if exposed) must carry the same explanation in
   its parameter docstring so the LLM knows when to apply it autonomously.

3. Always append a closed-world caveat to results: "Note: 'unused' means no SQL file
   in the indexed corpus selects from this table. External consumers (Tableau, BI tools,
   APIs) are not visible to this tool." This caveat must appear in: CLI output, MCP
   tool docstring, and `analyze unused --help`.

4. The same tier classification and caveat applies to the MCP server's future
   `find_unused_tables` tool (if planned).

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
| 9 | #5.2 | 10.2.7 | `parsers/snowflake_parser.py` | Replace `_EMBEDDED_DML` regex with sqlglot tokenizer-split + `isinstance(stmt, (exp.DML, exp.Select)) and not isinstance(stmt, exp.Copy)` filter; handle Snowflake/Databricks procedure bodies via `exp.RawString` extraction; keep regex fallback for BigQuery `exp.Command` bodies; 3 test fixtures required | M | HIGH |
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

"WE DON'T NEED TO KEEP A VERSION, WE WILL BREAK THINGS CAUSE
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

---

## 11. v0.3.0 Live Session Findings (2026-05-06)

Review date: 2026-05-06
Source: Live Claude Code session — full DWH corpus (Snowflake, ~1200 SQL files, WSL2/Linux, 3.8 GiB RAM)
Version: sqlcg 0.3.0 (installed via `uv tool install sql-code-graph`)

This section records new findings from a v0.3.0 test session. The session attempted
to index the full DWH corpus, trace `OMLOOPSNELHEID` column lineage via the MCP
server, and test the new `sqlcg uninstall` command. GitHub issues #10–#13 were filed
from this session.

---

### 11.1 [CRITICAL] KuzuDB OOM during edge building blocks large repo indexing

**Observed behaviour**: `sqlcg index` on the full DWH corpus (~1200 files) OOMs mid-run
with `RuntimeError: Buffer manager exception: Unable to allocate memory! The buffer pool
is full and no memory could be freed!`. The error occurs during the edge upsert phase
(`SELECTS_FROM`, `COLUMN_LINEAGE` edges), not during parsing. It is reproducible even
after reducing `buffer_pool_size` to 512 MB.

**Root cause**: KuzuDB's default `buffer_pool_size` is ~80% of system RAM (~3 GiB on
this machine). Edge accumulation exhausts the buffer before it is flushed to disk
because the current indexer batches all edges for the entire corpus before writing.
There is no per-file commit boundary.

**Workaround applied**: patched `KuzuBackend.__init__` in `site-packages` to pass
`buffer_pool_size=512 * 1024 * 1024`. This is not acceptable as a user-facing solution.

**Resolution**:

1. **Expose `--buffer-pool-size` CLI flag** (quick fix, low effort). Add to `sqlcg db init`
   and persist in `.sqlcg.toml` or an env var so subsequent commands use the same value.

2. **Per-file commit pattern** (proper fix, medium effort). The `sqlcg watch` watchdog
   already processes one file at a time — the `index_repo` loop should use the same
   per-file pipeline:
   ```python
   for file in discover_sql_files(path):
       process_file(file, dialect, backend)   # same fn the watcher calls
       backend.commit()                        # flush edges to disk per file
   ```
   This unifies the two code paths, eliminates OOM regardless of corpus size, and makes
   memory usage predictable and flat.

3. **Optional `--batch-size N`** as a middle ground: commit every N files. Allows
   tuning the memory/speed tradeoff without requiring per-file commits everywhere.

**Interaction with finding 3.1** (transaction no-op): per-file commits also address
the atomicity gap — each file either commits fully or is rolled back, keeping the graph
consistent without requiring the entire corpus to succeed.

**Filed as**: GitHub issue #10.

**Retrospective — sloppy or out of control?**
The OOM trigger (KuzuDB's 80%-RAM default buffer pool) is library behaviour and outside
our control. Everything else is on us. The `sqlcg watch` command already processes one file
at a time with implicit per-file flush; `index_repo` was written separately and accumulated
all edges with no commit boundary. Two code paths doing the same job with opposite memory
models is an architectural inconsistency that should have been caught in review. Additionally,
the `progress_callback` parameter was implemented in `indexer.py` (lines 79-80) but never
wired up in `cli/commands/index.py` — the scaffolding was there, the last connection was not
made. A basic end-to-end smoke test on a corpus of even 200 files would have surfaced both
the OOM and the missing progress output before shipping.

---

### 11.2 [CRITICAL] 0 COLUMN_LINEAGE edges even on small subsets — possible v0.3.0 regression

**Observed behaviour**: `sqlcg index etl/sql/fact --dialect snowflake` (184 files) completed
without OOM and reported `Indexed 184 files — 307 tables, 0 edges, 16 errors`. `db info`
confirmed `SqlColumn: 0` and `COLUMN_LINEAGE edges: 0`. Column lineage tools return empty.

**Root cause confirmed (2026-05-06 postmortem)**: finding 3.4 is NOT the live cause. Code
inspection revealed two independent wiring gaps:

1. **`_extract_column_lineage` is never called.** `AnsiParser._parse_statement`
   (`ansi_parser.py:140`) hardcodes `column_lineage = []` and never invokes
   `_extract_column_lineage`. Every `QueryNode` leaves the parser with an empty list
   regardless of SQL content. The indexer loop at `indexer.py:265` is correct but has
   nothing to iterate.

2. **The sqlglot → LineageEdge conversion is a TODO.** Inside `_extract_column_lineage`
   (`base.py:396-403`), when `sg_lineage()` returns a root node the code logs a debug
   message and stops — `# TODO: convert root to LineageEdge(s)`. Even if bug 1 were
   fixed, `_extract_column_lineage` would still return an empty list on the happy path.

**Why tests stayed green**: the integration test (`test_cross_file_lineage.py`) only
asserts that source tables are registered and that no file-I/O errors occur. It never
checks that any `QueryNode.column_lineage` is non-empty. The unit tests in
`test_base_parser.py` call `_extract_column_lineage` directly (bypassing bug 1) and
only cover error-handling paths and the TODO log message — not the success path.
`test_parser.py` parses views and selects but never asserts `column_lineage` is populated.

**Resolution**:

1. Wire the call: in `_parse_statement`, replace `column_lineage = []` with
   `column_lineage = self._extract_column_lineage(stmt, path, out, schema)`.

2. Implement the conversion: in `_extract_column_lineage`, after `sg_lineage()` returns
   a root, walk the lineage tree and emit `LineageEdge` objects mapping each source
   column to its destination column.

3. Add regression tests that assert `column_lineage` is non-empty after parsing a
   `CREATE VIEW AS SELECT` with explicit column aliases (marked `xfail` until fixed).

**Priority escalation**: column lineage is completely non-functional in production.
The feature has never emitted a single `COLUMN_LINEAGE` edge. Elevate to CRITICAL.

**Retrospective — sloppy or out of control?**
Entirely sloppy. Both bugs are wiring mistakes in code we own. The sprint plan (T-06)
already knew about the TODO and reduced the task to "add one logger.debug line" — meaning
the developer saw the TODO, decided it was a minor observability gap, and did not recognise
that the method itself is never called. This is the most consequential failure mode: the
feature looked finished because the scaffolding compiled and the tests passed, but zero
user-visible value was delivered. The core mistake was writing tests that covered error
paths and placeholder log messages instead of the observable output. A single assertion —
`assert len(stmt.column_lineage) > 0` after parsing a one-line `CREATE VIEW AS SELECT` —
would have caught both bugs before the first commit. The principle here: never ship a
feature with a TODO in the happy path, and always write at least one test that asserts
the feature's actual output, not just its exception handling.

---

### 11.3 [HIGH] MCP server not connected in active session — `sqlcg install` idempotency too strict

**Observed behaviour**: The MCP server was registered in `~/.claude/settings.json` with
`{"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}` from a prior session.
After upgrading to `uv tool install sql-code-graph` (v0.3.0), running `sqlcg install`
printed "Already configured" and left the stale `uvx` entry unchanged. The MCP tools
were not available in the Claude Code session because the `uvx` invocation used a
different (older) cached version.

**Root cause** (corrected): `install.py:41` does perform a full content comparison
(`if mcp_servers.get(_SERVER_KEY) == entry`), not an existence-only check. The actual
bug is in the entry computation: when `uvx` is on PATH, the ideal entry is always set to
`{"command": "uvx", …}` regardless of whether `sqlcg` is also installed locally. After
`uv tool install sql-code-graph`, both `uvx` and `sqlcg` are on PATH, so the computed
ideal entry is still uvx — which matches the stale uvx entry — and "Already configured"
is printed. The entry is correct in the narrow sense (uvx still works) but suboptimal:
it uses a cached version rather than the freshly installed one.

**Resolution**: Invert the priority — prefer the locally-installed `sqlcg` binary over
`uvx` when both are available. The detection order should be: `shutil.which("sqlcg")` →
`shutil.which("uvx")` → error. If the installed entry uses uvx but `sqlcg` is now
available locally, prompt: "Updating MCP entry from `uvx` to local `sqlcg` binary for
faster startup. Continue? [Y/n]". Add `--force` to skip the prompt.

**Related to section 9.2** (command detection at install time): the detection logic is
correct but the priority order is wrong. Fix the priority; apply on every `sqlcg install`
call, not just first-run.

**Retrospective — sloppy or out of control?**
Sloppy. The architecture review's original root cause ("existence-only check") was itself
wrong — a reminder that diagnosis written during a live session should be verified against
the code. The real issue is a one-line priority inversion: `uvx` should fall below `sqlcg`
in the preference order. The developer thought about idempotency (content equality check)
but did not think through the upgrade scenario where both binaries are present. A unit test
covering "reinstall after `uv tool install`" — asserting the entry switches from uvx to
sqlcg — would have caught this. The fix and the test are each one line of logic.

---

### 11.4 [HIGH] No observable progress or lock context during indexing

**Observed behaviour**: During a ~4-minute indexing run (before OOM), no stdout output was
produced. Attempting `sqlcg db info` in a separate terminal returned:
`RuntimeError: IO exception: Could not set lock on file: /home/ignwrad/.sqlcg/graph.db`.
The only way to determine that indexing was still running was `lsof ~/.sqlcg/graph.db`.

**Two distinct gaps**:

1. **No progress during `sqlcg index`**: `rich.progress` is already a dependency —
   a file-count progress bar costs ~10 lines of code. This is finding 10.2.9 confirmed
   again at higher urgency.

2. **Lock error gives no useful context**: the KuzuDB lock exception should be caught
   in `get_backend()` and re-raised as: "Database is locked — another sqlcg process
   (PID NNN) is running. Wait for it to finish or kill it with `kill NNN`." The PID
   can be obtained from the lock file or via `lsof`.

**Resolution**: Both are independent fixes in `kuzu_backend.py` and `indexer.py`.
The lock-aware message should be added alongside the `buffer_pool_size` fix (section 11.1)
as it affects the same `KuzuBackend.__init__` path.

**Filed as**: GitHub issue #13 (stdout noise/progress), and the lock-aware message is a
new sub-finding not previously tracked.

**Retrospective — sloppy or out of control?**
Mixed. The lock error message is library-controlled text — out of control. Catching it and
re-raising with a user-friendly message is purely within our control and was not done.
The progress gap is a direct repeat of the 11.2 pattern: `indexer.py` already accepts a
`progress_callback` parameter (lines 33 and 79-80) and calls it every 100 files. The CLI
command (`cli/commands/index.py`) never passes a callback — the last connection was not
made. Progress was planned in T-08 but T-08 was not shipped in v0.3.0. Running the tool
once against any non-trivial corpus (even 50 files) would have surfaced both the silent
stdout and the unhelpful lock error before shipping.

---

### 11.5 [RESOLVED] `sqlcg uninstall` leaves DB at default path

**Observed behaviour**: `sqlcg uninstall` printed "Removed MCP registration" and "No
database configured" but left `~/.sqlcg/graph.db` (50 MB) on disk.

**Root cause**: `uninstall.py` only checks `SQLCG_DB_PATH` env var. When unset it
reports "No database configured" rather than falling back to `KuzuConfig().db_path`
(the hardcoded default `~/.sqlcg/graph.db`).

**Resolution**: `_get_db_path()` in `uninstall.py:206` falls back to
`~/.sqlcg/kuzu.db` but `KuzuConfig.db_path` defaults to `~/.sqlcg/graph.db`
(config.py:17). Align the fallback to use `KuzuConfig.from_env().db_path` rather than a
hardcoded string, so the two modules cannot drift again.

**Status**: `sqlcg uninstall` command exists (resolves the core ask of issue #6) but
this gap means issue #6 remains open.

**Filed as**: GitHub issue #11.

**Retrospective — sloppy or out of control?**
Entirely sloppy. The T-03 spec explicitly said "fall back to `KuzuConfig().db_path`";
the implementation instead hardcodes a path string that doesn't match what the config
module produces. This is a copy-paste error between two files in the same codebase —
the developer wrote `"kuzu.db"` where they should have called the config. A single unit
test with no env var set — asserting that `sqlcg uninstall` prompts to delete
`~/.sqlcg/graph.db` — would have caught it immediately. The correct fix is one line:
replace the hardcoded string with `str(KuzuConfig.from_env().db_path)`.

---

### 11.6 Column lineage result via grep fallback — `OMLOOPSNELHEID`

Because MCP column lineage was non-functional (sections 11.1–11.2), the session
fell back to grep. The result is recorded here for completeness.

**`OMLOOPSNELHEID` is an intermediate calculation — not persisted to any BA table or IA view.**

Location: `etl/sql/dim/wtdh_artikel.sql` lines 2741 and 2772.

Formula (two variants):
- `tmp_berekening_bestaande_artikelen` (line 2741): `AFZET / GEMIDDELDE_VRD` (existing articles, 13-period rolling window). Sources: `tmp_afzet_bestaande_artikelen` ← sales data; `tmp_artikelen_voorraad` + `DA.TTGMD_OPSLAG` ← average stock.
- `tmp_berekening_segment` (line 2772): `GEMIDDELDE_SEGMENT_AFZET / GEMIDDELDE_VRD_SEGMENT` (segment fallback).

Both temp tables feed `tmp_opslag_berekend` (UNION), but only `KOSTEN_PER_VERKOCHT_WEB_ARTIKEL`
is carried forward — `OMLOOPSNELHEID` is dropped after the union and never written to
`BA.WTDH_ARTIKEL` or any DDL column.

The file `ddl/changelogs/IA-DATAPRODUCTS/AGG_GMROI_OS_WEEK_SEGMENT_FORMULE_VOORRAADLOCATIE.sql`
references "Omloopsnelheid" only in a comment (`-- GMROI en Omloopsnelheid`) — it computes
GMROI/omloopsnelheid inline from `IA_SEMANTIC` views without a named column.

**Relevance to ETL lineage gap (section 6)**: this is a concrete example of the
intra-file temp table chain described in section 6.3 — `OMLOOPSNELHEID` is computed in
a temp table and used (as a divisor) two lines later, but because column lineage through
temp tables is untracked, `sqlcg` would not have been able to trace it even with fully
functional column lineage.

---

### 11.7 Updated Open Issues Summary (2026-05-06)

| Issue | Title | Status | Notes |
|-------|-------|--------|-------|
| #5 | LLM agent experience, silent failures | Open | Column lineage still 0 in v0.3.0; quick-start block added ✅ |
| #6 | No clean uninstall / opt-out path | Open | `sqlcg uninstall` added ✅; DB not removed ❌ — see #11 |
| #10 | OOM during indexing | Open | New — v0.3.0, full DWH corpus |
| #11 | Uninstall leaves `~/.sqlcg/` | Open | New — v0.3.0 implementation gap |
| #12 | `find table` case-sensitivity | Open | New — extracted from #9 |
| #13 | Index warnings to stdout | Open | New — extracted from #9 |

Issue #9 (session summary) closed 2026-05-06 — actionable items extracted to #12 and #13.

---

### 11.8 Updated Implementation Plan — v0.3.0 Priorities

Additions and escalations relative to section 10.4:

| Rank | Finding | Action | Effort | Priority |
|------|---------|--------|--------|----------|
| 0 | 11.2 — 0 column edges in v0.3.0 | ~~Diagnose `_extract_column_lineage` exception swallowing; fix finding 3.4~~ **DONE — sprint_column_lineage_fix** | XS–S | ~~CRITICAL~~ CLOSED |
| 1 | 6 — star projections yield no edges | Two-pass DDL index → `add_information_schema` → `qualify` before `sg_lineage` | L | **HIGH** |
| 2 | 11.6 — indexer too slow | `ProcessPoolExecutor` for parsing; bulk commits (N=50); `--workers` / `--batch-size` flags | L | **HIGH** |
| 3 | 11.1 — OOM during edge building | Add `--buffer-pool-size` flag; implement per-file commit pattern in `index_repo`; unify with watchdog pipeline | M | HIGH |
| 4 | 11.3 — stale MCP entry not updated | `sqlcg install` should compare existing vs ideal entry and prompt to update | XS | HIGH |
| 5 | 11.4 — no progress + lock error context | `rich.progress` in `index_repo`; lock-aware error message in `get_backend()` | S | HIGH |
| 6 | 11.7 — dynamic identifiers yield no edges (E8) | Emit `col_lineage_skip:dynamic_source` marker; surface in MCP as `resolution=unresolvable` | XS | LOW |
| 7 | ~~11.5 — uninstall ignores default DB path~~ | ~~Fall back to `KuzuConfig().db_path` in `uninstall.py`~~ **DONE** | XS | ~~MEDIUM~~ CLOSED |

### minor ui changes
add version flag to cli `--version` 

the database command `--reset` is ambiguous, we should give use something like `--drop` some that explains the user how serious there action is 

this command should default to writing to a log file to much output is generated
```bash
sqlcg index /home/ignwrad/Projects/dwh --dialect <dialect> --buffer-pool-size 256 2>&1
```
---

### 11.7 [LOW] Dynamic source identifiers produce E8 — `sg_lineage` root has no leaf sources

**Observed behaviour** (2026-05-07 experiment, 100 ETL files): 39 columns across at least
4 files reach `sg_lineage`, get a root node back, but the tree walker finds no leaf table
sources — recorded as E8 (`no_edges_from_root`). The dominant example is
`rtga4_analytics_events.sql` (10 of the 39). No exception is raised; the column simply
produces zero edges and the failure is silent.

**Root cause**: the source table is a Snowflake dynamic identifier:

```sql
INSERT INTO DA_TMP.RTGA4_ANALYTICS_EVENTS
SELECT value:origin_dataset::varchar AS ORIGIN_DATASET, ...
FROM identifier($full_tablename) A   -- ← runtime variable
```

`sqlglot` parses `identifier($full_tablename)` as an opaque `exp.Anonymous` node. When
`sg_lineage` walks the lineage tree to find leaf sources it hits this node, cannot resolve
it to a real table, and returns a root whose `source` subtree is empty. The tree walker in
`_lineage_node_to_edges` reaches a leaf with no `TableRef` and emits nothing.

**This is a hard static-analysis ceiling** — the actual table name is a runtime value
(`SET full_tablename = (SELECT 'DL_' || SPLIT_PART(CURRENT_DATABASE(),'_',2) || '.GA4.ANALYTICS_EVENTS')`).
No amount of parser improvement will resolve it without executing the `SET` statement against
a live database. It is fundamentally different from the star projection gap (finding 6), where
schema injection unlocks the edges, and from T-03's temp-table gap, where `sources_map`
provides the missing context.

**Distinction from fixed issues**:

| Issue | Cause | Fixed? |
|-------|-------|--------|
| E5 — `col_lineage:` errors swallowed | `out_temp` throwaway (T-01) | Yes — sprint_column_lineage_fix |
| E2 — star crash | `sg_lineage('')` raises (T-02) | Yes — sprint_column_lineage_fix |
| Star projections — no edges | Schema absent; `qualify` can't expand `*` | Next sprint (finding 6) |
| E8 — dynamic identifiers | Runtime `identifier($var)` unresolvable | No — structural limit |

**Recommended action** (low effort, low reward): detect the E8 case in `_lineage_node_to_edges`
and emit a `col_lineage_skip:dynamic_source:<col_name>` marker to `out.errors`, matching the
convention established by T-02's `col_lineage_skip:star:` markers. This makes the failure
visible and distinguishable from the star-projection gap without claiming to fix it. The MCP
tool can surface `resolution=unresolvable` on these columns so consumers know not to wait for
lineage that will never arrive.

No further investment is recommended unless the corpus gains a pattern where the variable is
resolvable from context (e.g., `SET var = 'literal_table'`) — that would be a separate,
targeted ticket.

---

### 11.6 [HIGH] Indexer is too slow for real corpora — needs parallel parsing and bulk DB commits

**Observed behaviour** (2026-05-07): indexing 653 ETL files takes wall-clock minutes. The
v0.3.1 postmortem experiment run (`collect_parse_errors.py`, 200 DDL files) took **43 s**
with a single-threaded parser and per-file `backend.commit()`. Full re-index of the DWH
repo (1,457 files) will likely exceed 10 minutes on current hardware. This is too slow to
be interactive and makes the dev loop painful.

**Root causes**:

1. **Single-threaded parsing** — `index_repo` iterates files serially. `sqlglot.parse` and
   `sg_lineage` are CPU-bound and hold the GIL; they do not benefit from `asyncio`. Each
   file is independent, so parsing is embarrassingly parallel.

2. **Per-file commit to KuzuDB** — every file flushes a commit to disk. For 1,457 files
   this is 1,457 fsync-equivalent round-trips. KuzuDB's write path is not optimised for
   high-frequency small commits; batching amortises the cost.

**Design**:

1. **`ProcessPoolExecutor` for parsing** — spawn a pool of worker processes (default:
   `min(cpu_count, 8)`). Each worker receives a `(path, sql, dialect)` tuple and returns a
   `ParsedFile`. No shared state; `SchemaResolver` is read-only after the DDL pass and can
   be passed by value (pickle-safe). Pool size should be configurable via
   `--workers N` / `SQLCG_WORKERS` env var. Use `chunksize` tuning to reduce IPC overhead
   on large corpora.

   Note: `threading.Thread` / `ThreadPoolExecutor` will **not** help here because `sqlglot`
   parsing and `sg_lineage` are CPU-bound and hold the GIL. Use processes, not threads.
   The existing `threading.Lock` on `SchemaResolver` (finding 3.3) is only needed in the
   `sqlcg watch` path where the resolver is mutated — the bulk index path uses a frozen
   resolver and needs no lock.

2. **Bulk DB writes** — collect `ParsedFile` results from the worker pool in batches of N
   (default: 50) and write the batch inside a single KuzuDB transaction. This replaces the
   current per-file commit with at most `ceil(total_files / 50)` commits. Batch size should
   be tunable via `--batch-size N`.

   Interaction with finding 11.1 (OOM): per-file commits were introduced to cap memory
   usage. Bulk commits re-introduce the risk if N is large. The safe default is N=50 and
   the `--buffer-pool-size` flag (finding 11.1) should be set explicitly. Document this
   tradeoff in the CLI help text.

3. **Progress reporting** — the existing `progress_callback` hook in `indexer.py` (lines
   33, 79–80) is already wired; the CLI just needs to pass a `rich.progress` callback
   (finding 11.4). With a worker pool, update progress on each `Future` completion via
   `as_completed()`.

**Expected gain**: on an 8-core machine, parsing throughput should scale ~6–7× (leaving
headroom for IPC). The 43 s DDL run should drop to ~7 s; a full 1,457-file re-index should
complete in under 2 minutes.

**Ordering**: implement after the schema/DDL pass sprint (finding 6) because the DDL pass
introduces a `SchemaResolver` that must be serialisable for pickling into worker processes.
If done before, the resolver will need to be refactored twice.

**Files affected**:
- `src/sqlcg/indexer/index_repo.py` — main loop, commit boundaries, pool wiring
- `src/sqlcg/cli/commands/index.py` — `--workers`, `--batch-size` flags

---

## 12. Column Lineage — Diagnostic Findings (2026-05-12)

Supersedes all prior sprint-06 postmortem metrics. The sprint-06 "24 edges" figure
was a measurement artifact — the script was measuring a broken code path, not the
production parser. The parser was working correctly throughout.

---

### 12.1 Real Baseline — Production Parser (2026-05-12)

Measured by running `scripts/collect_parse_errors.py` directly against the DWH
corpus sub-directories after confirming the script reads from `stmt.column_lineage`
on the production parser path.

| Directory | Files | Success edges | Coverage | E5 | E8 |
|-----------|------:|-------------:|----------|---:|---:|
| `etl/sql/fact/` | 184 | **12,242** | 146 / 184 (79%) | 154 | 1,489 |
| `ddl/changelogs/IA-SEMANTIC/` | 187 | **3,907** | 183 / 187 (98%) | 632 | 27 |
| `ddl/changelogs/IA-DATAPRODUCTS/` | 113 | **3,373** | 109 / 113 (97%) | 116 | 706 |

E4 (parse failures): **0** across all three directories.

The prior "24 edges" figure came from a sprint-06 script run that OOM-crashed before
processing any ETL files and left only DDL-changelog output from a broken shadow
code path. That number should be ignored entirely.

---

### 12.2 Anchor Test Cases

Two columns chosen as acceptance gates for any future sprint. If the tool cannot
trace these, there is no point starting a sprint.

**Anchor 1 — `OMLOOPSNELHEID`** (`etl/sql/dim/wtdh_artikel.sql`)

Computed as `AFZET / GEMIDDELDE_VRD` in two temp tables
(`tmp_berekening_bestaande_artikelen`, `tmp_berekening_segment`). Intentionally
dropped when merged into `tmp_opslag_berekend` — the UNION at line 2796 selects
only `DN_ARTIKEL_NUMMER`, `DN_ARTIKEL_FORMULE_CODE`, `KOSTEN_PER_VERKOCHT_WEB_ARTIKEL`.
Never reaches any persistent table as `OMLOOPSNELHEID`.

Expected lineage behaviour: intra-file edges from `AFZET` / `GEMIDDELDE_VRD` to
`OMLOOPSNELHEID` are traceable within the temp-table CTEs. No cross-file edge should
exist. A tool that reports any cross-file destination for `OMLOOPSNELHEID` is wrong.

**Anchor 2 — `MA_AANTAL_OP_ORDER`** (`etl/sql/fact/wtfs_openstaande_orders.sql`)

Full 3-file, 4-hop chain:

```
BA.WTFE_INKOOP_ORDER.ma_order_aantal
  → bm_orders.aantal_op_order          (SUM / verhoudingsgetal, intra-file CTE)
BA.WTFE_INKOOP_ORDER_IGDC.ma_inkoopaantal / ma_nog_te_leveren_aantal
  → igdc_openstaand.aantal_op_order    (SUM GREATEST, parallel UNION ALL branch)
  → openstaand_combined.aantal_op_order (UNION ALL)
  → BA_TMP.WTFS_OPENSTAANDE_ORDERS.MA_AANTAL_OP_ORDER  (SUM aggregate, INSERT target)
  → BA.WTFS_OPENSTAANDE_ORDERS.MA_AANTAL_OP_ORDER      (persistent fact table)
  → IA_SEMANTIC."Openstaande orders"."Aantal op order"  (semantic rename, separate file)
```

Confirmed working: the production parser produces 53 edges from this file alone,
including `WTFE_INKOOP_ORDER.MA_ORDER_AANTAL → WTFS_OPENSTAANDE_ORDERS.MA_AANTAL_OP_ORDER`
at confidence 0.9. The UNION ALL branches are both traced correctly.

The cross-file semantic rename (`BA.WTFS_OPENSTAANDE_ORDERS` →
`IA_SEMANTIC."Openstaande orders"`) resolves correctly when both files are indexed,
because `sg_lineage()` handles quoted Dutch column names with spaces without error.

---

### 12.3 Remaining Error Taxonomy

- [x] **E1 count confirmed** — E1 = 0 on both runs. P-03 alias\_col\_ref fix is verified
      effective on the full 1,445-file corpus. No new E1 code path was found.

- [x] **E2 count and fraction documented** — E2 = 441, representing **60.4% of all column
      expressions**. More than half of SELECT columns are unaliased non-`exp.Column` nodes and
      are skipped by current code. Best-effort name extraction would unlock 60% more lineage
      attempts.

- [x] **E3 count and DDL constructs documented** — E3 = 2,507 fallbacks concentrated in 5
      files. The affected files are `WTDH_ARTIKEL*` and `COMBI_*` changelog files containing
      hundreds of `ALTER DYNAMIC TABLE`, `ALTER WAREHOUSE IDENTIFIER(...)`, and similar
      unsupported DDL statements. These are the p99 slow-file culprits (18–21 s each).

- [x] **E4 count and failing files documented** — E4 = 6 files with full parse failure.
      Root sqlglot errors to be sampled from `results_full.json`. Whether these are DML or
      DDL files has not yet been confirmed; see open question Q-12-1 below.

- [x] **E8 count and fraction documented** — E8 = 248 (without schema) / 261 (with schema).
      As a fraction of `sg_lineage()` calls that returned a root, the exact denominator
      requires counting successful root returns minus empty-edge results; given only 37 edges
      were emitted from approximately 285 successful root returns (248 + 37), E8 accounts for
      roughly **87% of sg\_lineage() calls that returned a root**. Cross-file temp-table
      chains are therefore the dominant quality ceiling for single-file lineage.

- [x] **Top 5 slow files identified** — all slow files are DDL changelog files with many
      statements. p99 = 7,200 ms. The `WTDH_ARTIKEL*` and `COMBI_*` files are the confirmed
      outliers (18–21 s in the schema-aware run). Statement count, not CTE nesting, is the
      primary driver.

- [x] **Schema-aware run completed** — Step 5 (schema-aware run with `columns.csv`) was
      executed. Result: **the schema-aware run produced fewer edges (24 vs 37) and slightly
      more E8 errors (261 vs 248)**. E6 = 0 in both runs. The schema-aware run is not worth
      repeating in a future sprint unless E5 is first reduced; schema cannot help when E5
      blocks before edges are produced.

- [x] **Findings written to ARCHITECTURE_REVIEW.md section 12** — this section.

---

### 12.3 Key Analytical Conclusions

**C-1: Schema had no meaningful impact.**
Results were identical or slightly worse with the 144k-row production schema CSV. E5 errors
occur before `sg_lineage` can use schema information — the exception is thrown during
column extraction or qualification, not during source-table lookup. Until E5 is resolved,
injecting schema produces no coverage gain and introduces mild regressions (E8 +13,
edges -13 in the schema run). The schema-aware path is not the next priority.

**C-2: E5 is the dominant blocker.**
1,280 `lineage_other` errors at 175% of column expressions — the count exceeds total column
expressions because some files throw multiple exceptions per column. The root cause is
unknown. E5 must be investigated before any other fix is scoped; all other improvements
(E2, E8) operate on columns that survive E5. Sampling 5–10 E5 messages from
`results_full.json` is the required next step.

**C-3: E2 at 60.4% is the largest coverage gap.**
More than half of all column expressions are unaliased non-`exp.Column` nodes. Current code
skips them entirely. A best-effort fallback — `col_expr.alias or col_expr.name or
str(col_expr)[:40]` — would unlock approximately 60% more lineage attempts. This is the
highest-reward single change once E5 is understood.

**C-4: E3 is concentrated in changelog files.**
2,507 command fallbacks appear in only 5 files. These files are pure-DDL changelog
files (`WTDH_ARTIKEL*`, `COMBI_*`); they will never produce column lineage edges regardless
of parser improvements. They are also the p99 slow outliers. Detecting and skipping
pure-DDL files before calling `sg_lineage` eliminates both the E3 noise and the worst
tail latency.

**C-5: E1 = 0 — P-03 fix is verified.**
The alias\_col\_ref fix from sprint v0.3.1-postmortem is confirmed effective across the
full 1,445-file corpus. This error category is closed.

**C-6: Slow files are all DDL changelog files.**
p50 = 22.5 ms (healthy); p95 = 836 ms; p99 = 7,200 ms. The long tail is entirely caused
by files with hundreds of DDL statements. `scope=` reuse (finding R-06 from the experiment
plan) would help normal multi-column SELECT files but would not fix the p99 outliers.
The correct fix for p99 is early DDL-file detection and skip.

---

### 12.4 Priority Ranking for Follow-On Sprint

Ranked by impact × effort (high impact, low effort first). Updated after E4/E5
investigation (Q-12-1 and Q-12-2 resolved — see section 12.6):

| Rank | Code | Title | Rationale | Effort |
|------|------|-------|-----------|--------|
| 1 | FIX-E5 | `sources=` schema expansion in `sg_lineage()` | Root cause confirmed: `qualify()` cannot expand cross-file CTE sources when the schema is absent. Fix: pass the loaded information schema (`columns.csv`) as `sources=` to `sg_lineage()`. Affects 1,280 errors, all IA-DATAPRODUCTS DML aggregates. High impact, well-scoped. | M |
| 2 | FIX-E2 | Best-effort expression name extraction | `col_expr.alias or col_expr.name or str(col_expr)[:40]` fallback unlocks 60% more lineage attempts. Simple guard in `_extract_column_lineage`. | S |
| 3 | FIX-DDL-SKIP | Skip pure-DDL files early | Detect files where all parsed statements are DDL/`Command` nodes and bypass `sg_lineage` entirely. Removes p99 slow-file outliers (18–21 s) and E3 noise. | S |
| 4 | FIX-E4 | Separate ticket: sqlglot Snowflake dialect gaps | 6 DML files fail at parse time due to three distinct sqlglot gaps: (1) `IF NOT EXISTS` misread as `If()` function call (2 files), (2) complex DML/duplicate blocks cause unexpected-token errors (2 files), (3) UNPIVOT clause not fully supported (1 file), (4) one file has no captured message. Medium priority — lineage is lost for all 6, but these are real upstream sqlglot bugs, not local fixes. | M |
| 5 | OPT-SCOPE | `scope=` reuse per statement | Build scope once per statement, call `sg_lineage` per column. Estimated 40–60% speedup for multi-column SELECT files. Only beneficial after DDL-file skip reduces the p99 tail. | M |

FIX-E5 is now unblocked (INV-E5 investigation completed). FIX-E2, FIX-DDL-SKIP, and
FIX-E4 are independent of each other and can proceed in parallel with FIX-E5.

---

### 12.5 Deferred Questions Answered

From `plan/parsing_errors_experiment.md` § "Deferred Decisions":

**Q: Should we fix E8 (no edges from subquery sources)?**
E8 accounts for approximately 87% of `sg_lineage()` calls that returned a root. Cross-file
temp-table chains dominate. `sources=`-based cross-file expansion (finding R-02) would
address this — it is the correct long-term fix. However, given that E5 is blocking
production of almost all edges, and only 285 columns even reach the E8 check, E8 is not
the immediate priority. Revisit after E5 is fixed and edge volume increases.

**Q: Is `scope=` reuse worth implementing?**
p99 = 7,200 ms on changelog files — but these files are candidates for DDL-file skip
(FIX-DDL-SKIP), not `scope=` optimisation. For normal SELECT files, p50 = 22.5 ms is
healthy. `scope=` reuse (rank 4) is a worthwhile medium-term optimisation but not urgent.

**Q: Should schema pass `{table: {col: type}}` format instead of the nested catalog format?**
E6 = 0 in both runs — schema format is not causing mismatches. The nested format from
`SchemaResolver.as_dict()` is compatible with sqlglot's `Schema` object. No format change
is needed. The schema path is simply not reached because E5 blocks earlier.

**Q: Is E4 (parse failure) concentrated in DDL or DML files?**
Confirmed DML — all 6 E4 files are hybrid DDL+DML or pure DML (CREATE TEMP TABLE +
INSERT, SELECT with CTEs, UNPIVOT). Lineage impact is HIGH for all 6. Three distinct
sqlglot dialect gaps identified (IF NOT EXISTS, unexpected token, UNPIVOT). A separate
medium-priority ticket (FIX-E4) has been raised. See Q-12-1 in section 12.6 for the
full breakdown.

---

### 12.6 Open Questions

Both questions resolved after E4/E5 investigation (2026-05-11).

---

**Q-12-1** (E4) — RESOLVED

All 6 E4 parse-failure files contain DML. Lineage impact is HIGH for all.

| File | Path | DML? | Parse Error | Lineage Impact |
|------|------|------|-------------|----------------|
| WTDH_CONTRACT.sql | ddl/changelogs/BA-TABLES/ | YES (hybrid DDL+DML, has INSERT/MERGE/WITH-CTE) | `Expecting ).` at line 7 in `CREATE TABLE ... IF NOT EXISTS` | HIGH |
| htwfm_contract_us28254.sql | etl/pdi/template/ | YES (CREATE TEMP TABLE + INSERT + SELECT) | `Required keyword: 'true' missing for If()` — sqlglot misparses Snowflake `IF NOT EXISTS` as function call | HIGH |
| htwfm_employee_us28254.sql | etl/pdi/template/ | YES (CREATE TEMP TABLE + INSERT + SELECT) | Same IF() error | HIGH |
| us15770-check-controles-herstelactie.sql | etl/pdi/template/ | YES (CREATE TEMP TABLE + INSERT + SELECT + UNIONs) | `Invalid expression / Unexpected token` at line 114 | HIGH |
| test.sql | etl/sql/int/ | YES (large SELECT with CTE, FULL OUTER JOIN, UNPIVOT) | `Expecting ).` at line 329 — UNPIVOT clause not fully supported | HIGH (dev file, limited production impact) |
| outbound_ga4_artikelen.sql | etl/sql/interface/ | YES (multiple INSERT + SELECT + JOINs) | No error message captured (only 5 of 6 stored by diagnostic script) | HIGH |

**Error patterns identified:**

1. IF() dialect gap (2 files): sqlglot misparses Snowflake `CREATE TABLE ... IF NOT EXISTS`
   as an `If()` function call. Upstream sqlglot bug or dialect definition gap.
2. Syntax / Unexpected token (2 files): complex DML or duplicate SQL blocks cause parser
   failure at tokenisation time.
3. UNPIVOT unsupported (1 file): sqlglot does not fully support Snowflake UNPIVOT in this
   context.
4. No message captured (1 file): only 5 examples stored by the diagnostic script.

**Action taken**: Separate fix ticket FIX-E4 raised (rank 4 in section 12.4, medium
priority). These are real sqlglot Snowflake dialect gaps; not local code issues.

---

**Q-12-2** (E5) — RESOLVED

- Distinct exception types across 50 sampled messages: 1 (homogeneous).
- Dominant message: `Cannot find column '<COLUMN>' in query.` — 100% of samples.
- E1/E5 boundary check: zero of 50 sampled messages contained a `.` in the column name;
  no E1-pattern contamination confirmed.

**SQL pattern**: All E5-affected files are `CREATE OR REPLACE DYNAMIC TABLE` statements
with multi-level CTEs whose source tables are cross-file semantic views (e.g.,
`IA_SEMANTIC."Schapbeschikbaarheid"`, `IA_SEMANTIC."Transactie"`). The failing column is
defined inside a CTE via a CASE expression, then referenced via CTE alias in the outer
SELECT. Because `sg_lineage()` is called without `schema=` or `sources=`, `qualify()`
cannot expand the CTE to resolve the column to a leaf table.

Concrete example (`AGG_BESTELADVIEZEN_WEEK_SEGMENT_FORMULE_VOORRAADLOCATIE.sql`):

```sql
WITH MOVERTYPE_PER_DAG_BOUWMARKT_ARTIKEL AS (
    SELECT CASE WHEN AVG("Rotatie") >= 0.5 THEN 'Fast-mover' ... END AS "Movertype"
    FROM IA_SEMANTIC."Schapbeschikbaarheid"   -- cross-file source, absent from schema={}
    ...
)
SELECT mvt."Movertype"   -- Cannot find column 'MOVERTYPE' — CTE not expandable
FROM ... LEFT JOIN MOVERTYPE_PER_DAG_BOUWMARKT_ARTIKEL mvt ON ...
```

**Hypothesis confirmed**: `qualify()` inside `sg_lineage()` cannot expand CTE references
when the CTE's source tables are absent from the `schema=` parameter.

**Fix direction confirmed**: Pass the information schema already loaded via `columns.csv`
as `sources=` to `sg_lineage()`, enabling `qualify()` to resolve cross-file CTE sources.
This is Architecture Finding R-02 applied. Tracked as FIX-E5 (rank 1 in section 12.4,
high priority — 1,280 errors, all DML aggregates in IA-DATAPRODUCTS).
- `src/sqlcg/core/kuzu_backend.py` — expose `begin_transaction` / `commit` for batch mode

---

### 12.7 Sprint 06 Postmortem — Measurement broken, T-01 broken on real corpus

Date: 2026-05-11
Branch: `feat/sprint-06` (commits `d10f289`..`12ab2d3`)
Status: **Unverified. Edge count flat at 24; T-01 does not function on the DWH corpus. Pick up here tomorrow.**

#### Summary

Sprint 06 shipped T-01..T-05 plus three follow-up fixes (commits `5fac2d8`,
`1c5a2fc`, `12ab2d3`) intended to make the experiment script measure the
production parser and to repair a `combined_sources` type mismatch found during
root-cause analysis. Re-running `scripts/collect_parse_errors.py` against the
DWH corpus with `--schema-csv columns.csv` produced **no result JSON, no stdout,
and 1,809 schema_resolver WARNING lines** before the Python process exited
silently (exit 0 per task notification, but stdout buffer never flushed; suspect
OOM during eager parse of synthetic SELECTs).

#### Root cause: T-01 synthetic SELECTs are unparseable on real schemas

[`SchemaResolver.as_sources_dict()`](src/sqlcg/lineage/schema_resolver.py) emits
synthetic SELECT bodies of the form
`SELECT col1, col2, ... FROM <schema>.<table>`. The DWH `INFORMATION_SCHEMA.COLUMNS`
contains identifiers that require quoting:

- Spaces in column names: `Feedback ID`, `Jaar en week`, `Aantal jaren geleden (week)`.
- Spaces in table names: `Klantfeedback na aankoop`, `Webshop orderregel`, `WEEK_BOUWMARKT`.
- Parens, dots, and reserved-word collisions inside column names: `Jaar (week)`,
  `Omzet excl.`, `8WK indicator`.

Concrete example from stderr (one of 1,809):

```
SELECT Feedback ID, Formule koppelcode, ... , Datum
FROM IA_TABLEAU.Klantfeedback na aankoop
```

`sqlglot.parse_one(..., dialect="snowflake")` rejects this — unquoted multi-word
identifiers cannot be tokenized.

#### Failure distribution across schemas (1,804 of ~5,672 tables fail)

| Schema | Failed synthetic SELECTs | Tables in schema | Relevance to E5 |
|--------|--:|--:|---|
| **IA_SEMANTIC** | 720 | 198 | The cross-file CTE source schema named in §12.6 Q-12-2 — primary E5 root cause |
| IA_TABLEAU | 596 | 373 | |
| **IA_DATAPRODUCTS** | 484 | 124 | All 1,280 E5 errors target DML aggregates in this schema |
| SCRATCHPAD | 4 | 3 | |
| BA, BA_TMP, DA, DA_TMP, IA_ANALYTICS, IA_BUSINESSOBJECTS, IA_OUTBOUND, FUSION_TMP, MA | 0 | ~5,000 | ASCII-clean snake_case — parse fine; operational/staging schemas not involved in E5 |

The failures concentrate in **exactly the schemas the sprint-06 fix was
designed to help**: IA_SEMANTIC owns the cross-file views referenced via CTEs,
and IA_DATAPRODUCTS owns the DML aggregates that produce the 1,280 E5 errors.
The ~5,000 ASCII-clean tables that parse correctly belong to operational and
staging schemas (BA*, DA*, *_TMP) that do not appear in the E5-affected files.

After the synthetic-SELECT parse failures, `as_sources_dict()` returns a dict
populated only with the ASCII-clean operational tables. **T-01 functionally
covers zero of the tables it was designed to cover.** Edge count stayed at 24
in the prior sprint-06 v1 run because of this — and is expected to stay at 24
in v2 even after the eager-parse change, because the underlying string
generator is the same.

#### Secondary issue: eager parse at CSV load time

The Task 3 fix changed `as_sources_dict()` to return `dict[str, exp.Select]`
parsed once at `add_information_schema()` time rather than lazily per call. On
the DWH 144,311-row CSV this triggers ~5,000 `sqlglot.parse_one` calls during
parser construction. Coupled with each failure logging a WARNING-level message
containing the full synthetic SELECT body, this:

1. Generated 1.5 MB of stderr in ~5 seconds.
2. Held ~5,000 `exp.Select` AST nodes in memory plus the failed-parse exception
   chains.
3. Caused the Python process to exit silently before reaching the file-scan
   stage (`print("Scanning ...")` never appeared in stdout — the stdout buffer
   was not flushed before exit). Suspect OOM; not confirmed.

#### Why this was not caught in unit tests

The unit and integration tests added in commit `12ab2d3` use ASCII-clean,
quoted fixtures (e.g., `IA_SEMANTIC.Schapbeschikbaarheid` with column
`Rotatie`). They pass because those identifiers happen to parse without
quoting. The regression-guard test exercises one such file. The DWH corpus
breaks because real Snowflake schemas use natural-language Dutch column names
with spaces — a property no fixture in the repo captures.

This is a **fixture realism gap**, not just a code bug. Future schema-loading
work needs at least one fixture CSV that mirrors the real-world identifier
character set: spaces, parens, dots, mixed case.

#### Tertiary issue: experiment-script stdout buffering

The rewritten `scripts/collect_parse_errors.py` (commit `5fac2d8`) uses `print()`
without `flush=True`. When run with `> /tmp/exp_stdout.log`, none of the
progress prints (`Scanning ...`, `Found N files`, `Loading schema ...`) reach
the log unless the script completes cleanly. On crash, the user sees only the
warnings stream. Either pass `-u` to Python in invocation, set
`PYTHONUNBUFFERED=1`, or change the prints to flush explicitly. Trivial fix
but cost diagnostic time today.

#### Status of each sprint-06 ticket

| Ticket | Implementation | Verified on DWH corpus? | Notes |
|--------|----------------|-------------------------|-------|
| T-01 FIX-E5 | Lands; no-op on real schemas | **No** — synthetic SELECTs unparseable for IA_TABLEAU / IA_DATAPRODUCTS | Must quote identifiers; consider whether the `sources=` shape is correct at all (see "Open question" below) |
| T-02 FIX-E2 | Lands | **Unknown** — script crashed before any file processed | Expected to fire ~441 fallback attempts; cannot confirm |
| T-03 FIX-DDL-SKIP | Lands; spec deviation (does not early-return, sets flag + iterates with `skip_column_lineage=True`) | **Unknown** | Confirm pure-DDL files now produce `parse_mode:pure_ddl_skip` in `parsed.errors` |
| T-04 FIX-E4 | Lands (preprocessing + `_parse_with_recovery`) | **Unknown** | Baseline showed E4 6→2 in v1 numbers but those came from the broken shadow path; re-verify |
| T-05 OPT-SCOPE | Deviation: pre-built scope from `parse_file` is NOT passed to `_extract_column_lineage` (outer-stmt scope vs inner-body scope mismatch). Fallback path inside `_extract_column_lineage` builds `body_scope` from the inner body and uses that. Documented in commit `1c5a2fc`. Dead branch `if scope is not None` remains at [`base.py:679`](src/sqlcg/parsers/base.py) | **Unknown** | Either delete the dead branch or actually wire scope= through |

#### Open question — is `sources=` the right shape at all?

Sprint 06 assumed sqlglot's `sg_lineage(sources=...)` accepts a mapping of
table name → synthetic SELECT body, which `qualify()` then expands. The empirical
result on the DWH corpus is that no synthetic body for any IA_TABLEAU /
IA_DATAPRODUCTS table is parseable, and even for tables where it is parseable
(BA_*), edge count did not move from 24.

Alternative shapes worth investigating tomorrow before another round of fixes:

1. **Pass a real `sqlglot.MappingSchema` via `schema=`**, not synthetic SELECTs
   via `sources=`. `MappingSchema` supports `{db: {table: {col: type}}}` and
   handles identifier quoting internally. This is `R-02` from the experiment plan
   but implemented via `schema=`, not `sources=`. Confirm whether `qualify()`
   uses `schema=` to expand cross-file CTE sources, or only to type-check.
2. **Pre-resolve CTE references in the parser**, not in `qualify()`. Rewrite the
   CTE's source-table reference to the resolved column list at AST level before
   calling `sg_lineage`. More invasive but bypasses the `qualify()` quirks
   entirely.
3. **Verify the sqlglot version**. The `sources=` semantics may differ across
   sqlglot versions; pin and document.

Sprint 06 assumed option 1 was insufficient (Q-12-2 in §12.6 says "Because
`sg_lineage()` is called without `schema=` or `sources=`, `qualify()` cannot
expand the CTE"). Worth empirically re-testing with `schema=` only (no
`sources=`) on a real quoted fixture before committing to a sources-shape fix.

#### Action items for tomorrow

In order:

1. **Add a fixture CSV with realistic identifiers** (spaces, parens, mixed case,
   Dutch) to `tests/fixtures/`. Re-run the integration regression-guard test
   against it. Expected: the test fails today.
2. **Decide T-01 fix shape**: quoted synthetic SELECTs vs `MappingSchema` via
   `schema=` vs AST-level CTE rewrite. Update sprint plan with the decision
   before any code change.
3. **Apply the chosen fix**; the existing regression-guard test (updated to use
   the realistic fixture) is the acceptance gate.
4. **Stdout buffering fix** in `scripts/collect_parse_errors.py` — add
   `flush=True` to prints or `sys.stdout.reconfigure(line_buffering=True)` at
   the top of `collect_parse_errors`.
5. **Downgrade the synthetic-SELECT parse-failure log** from WARNING to DEBUG.
   At 5,000 tables this is unactionable noise. Failure should be counted into a
   single summary log line (e.g. "N of M synthetic SELECTs failed to parse").
6. **Delete the dead `scope is not None` branch** at `base.py:679` or wire it
   through `_parse_statement` properly. Pick one.
7. **Re-run** `uv run -m scripts.collect_parse_errors /home/ignwrad/Projects/dwh
   --dialect snowflake --schema-csv columns.csv` and read the new JSON. Only
   then can we judge T-02..T-05 status.

#### Provisional metrics (do not trust)

The v2 run produced no JSON. The v1 sprint-06 run produced the table at the
top of this conversation (edges flat at 24, E5 +206). Both runs measured a
broken path. Sprint 06 should be considered **unmeasured** until the open
issues above are resolved.

---

## 13. Sprint 07 Postmortem — Open E-Code Fixes (2026-05-12)

Source: [`plan/sprint_07_open_ecodes.md`](plan/sprint_07_open_ecodes.md), branch
`feat/sprint-07-t02-t06`. Final suite: **506 passed, 4 skipped, 1 unrelated xfail,
0 failures.** All six tickets shipped.

### 13.1 What landed

| Ticket | Mechanism |
|--------|-----------|
| T-07-01 FIX-E36-XFILE (merged in #20) | `CrossFileAggregator.cross_file_sources` harvests CTAS bodies during pass 1; `SchemaResolver.register_cross_file_sources` seeds each pass-2 `parse_file`'s `sources_map`. The cross-file temp-table chain (38/184 zero-edge files in the fact corpus) now resolves end-to-end. |
| T-07-02 FIX-E5-XFILE-CHAIN | `_extract_column_lineage` in [`base.py`](src/sqlcg/parsers/base.py) iterates `body.find_all(exp.CTE)` and emits a `LineageEdge` per CTE projection with `transform="CTE_PROJECTION"` and the CTE name as the destination table. All 6 anchor links in `tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py` flipped from xfail to PASS — the sprint gate. |
| T-07-03 FIX-E25 | `edges_full_id()` helper exported from `tests/snowflake/conftest.py`; new `test_e25_full_id.py` asserts `TableRef.full_id` carries the 2- and 3-part qualifier. |
| T-07-04 DEFER-E12 | Upstream sqlglot now traces LATERAL FLATTEN + JSON path expressions. Plain positive assertions, no xfail. |
| T-07-05 DEFER-E27 | Same shape — sqlglot now propagates UDF argument lineage. Plain PASS. |
| T-07-06 DEFER-E16 | `_extract_column_lineage` records `col_lineage_skip:merge_branch:<dst>` in `result.errors` for `exp.Merge` statements. MERGE files are now visible in `sqlcg report` rather than vanishing silently. |

### 13.2 Architectural observations worth preserving

#### 13.2.1 The `CTE_PROJECTION` transform is a new public surface

A consumer (MCP tool, report renderer, downstream UI) inspecting the `transform`
field on `LineageEdge` will now see a new value `"CTE_PROJECTION"` alongside the
existing `"SELECT"`, `"UNKNOWN"`, `"AGGREGATION"` etc. The destination table of these
edges is a **synthetic** `TableRef` whose name equals the CTE alias — not a real
indexed table. Any consumer that joins lineage edges to `SqlTable` nodes must handle
the case where the destination is a CTE alias with no corresponding `SqlTable`. This
is by design (CTE-as-destination is intermediate lineage), but it changes the contract.

#### 13.2.2 Cross-file `sources_map` precedence rule (T-07-01)

The seeding order is **cross-file first, intra-file overwrites**. Two files both
defining a CTAS named `t` resolve to the intra-file body in the file being parsed.
This is correct for the small-repo / refactor scenario (rename collides locally), but
means cross-file collisions in a large unified corpus are silently won by whichever
file is being re-parsed. If this surfaces as a real issue in production indexing,
the resolver could be extended to record collisions and emit a `cross_file_collision:t`
diagnostic; today it does not.

#### 13.2.3 Upstream sqlglot moved twice in our favour

Both T-07-04 (LATERAL FLATTEN + JSON path) and T-07-05 (UDF argument propagation)
were planned as `xfail(strict=False)` "deferred to upstream" tickets. By
implementation time, upstream sqlglot had already shipped the fixes — the tests
became plain PASS. **Process lesson:** when planning a DEFER ticket against an
upstream gap, the developer should re-run the failing scenario against current
upstream once before adding xfail markers. We could have shipped these as positive
assertions on day one.

#### 13.2.4 Pyright + typed dict spread

A typed `dict[str, str | None]` cannot be safely spread (`**kwargs`) into a function
that does not accept `None` for every keyword. Pyright flagged this in T-07-02's
sg_lineage call site. The fix was to drop the spread and pass `dialect=self.DIALECT`
inline. Future parser plumbing should prefer explicit kwargs over a typed-dict spread
when the receiving signature has narrow types.

#### 13.2.5 MERGE observability without lineage

T-07-06 records `col_lineage_skip:merge_branch:<dst>` rather than implementing MERGE
column lineage. This makes MERGE files visible in `sqlcg report` (the error mapping
table now includes the new skip string) and keeps the parser honest — silent
zero-edge MERGE files used to look identical to genuine zero-edge bugs. If a future
sprint implements MERGE per-branch synthetic SELECTs against sqlglot's lineage API,
this skip string is the search anchor for what to remove.

### 13.3 Coverage delta

Anchor `MA_AANTAL_OP_ORDER` is now fully traced end-to-end across the 3-file chain
documented in [§12.2](#122-anchor-test-cases). Combined with T-07-01's E36 fix
(38/184 zero-edge files in the fact corpus), this is the largest single-sprint
coverage improvement since the project began. No re-measurement of the DWH corpus
has been run yet — schedule one for the next sprint kickoff to quantify the lift.

### 13.4 Open follow-ups

1. **Re-measure the DWH corpus** to quantify the lift from T-07-01 + T-07-02.
   Expected: E36 drops from 38 to near-zero; total edges in `etl/sql/fact/` rises
   above the 12,242 baseline in [§12.1](#121-real-baseline--production-parser-2026-05-12)
   thanks to CTE projection edges.
2. **Consider whether `CTE_PROJECTION` edges should be filterable** in MCP tool
   responses. If a consumer wants only "real" table-to-table lineage, exposing a
   `include_intermediate_ctes: bool = True` flag on `trace_column_lineage` may be
   desirable. Not in scope for this sprint.
3. **Cross-file collision diagnostic** (see 13.2.2) — add only if production
   indexing flags a real collision.
4. **Process improvement**: re-test DEFER tickets against current upstream before
   adding `xfail` markers (see 13.2.3).

---

## 14. Sprint 08 — Bulk Upsert, Pass-2 Skip, Timeout Fix, E1 Literal-Skip

Review date: 2026-05-12
Branch: `feat/sprint-08-perf-upsert`
Tickets reviewed: T-01 (bulk upsert), T-02 (pass-2 skip), T-03 (timeout fix), T-05 (E1 literal-skip)
Source: commits `020714c`–`81d30d8`; diff against master in
[`src/sqlcg/core/graph_db.py`](src/sqlcg/core/graph_db.py),
[`src/sqlcg/core/kuzu_backend.py`](src/sqlcg/core/kuzu_backend.py),
[`src/sqlcg/core/neo4j_backend.py`](src/sqlcg/core/neo4j_backend.py),
[`src/sqlcg/indexer/indexer.py`](src/sqlcg/indexer/indexer.py),
[`src/sqlcg/lineage/aggregator.py`](src/sqlcg/lineage/aggregator.py),
[`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py)

### 14.1 T-01: Bulk Upsert Architecture

#### What changed

[`GraphBackend`](src/sqlcg/core/graph_db.py) gains two abstract methods:
`upsert_nodes_bulk(label, rows)` and `upsert_edges_bulk(src_label, dst_label,
rel_type, rows)`. [`KuzuBackend`](src/sqlcg/core/kuzu_backend.py) implements
both with a single `UNWIND $rows AS row MERGE …` query per call. The
[`_upsert_parsed_file`](src/sqlcg/indexer/indexer.py) method in the indexer was
fully rewritten from a flat sequence of ~100 individual `upsert_node` / `upsert_edge`
calls into a three-phase collect-deduplicate-flush pattern:

- **Phase A** — iterate the `ParsedFile` model and accumulate typed row lists
  (`file_rows`, `table_rows`, `column_rows`, `query_rows`, and five edge-list
  variants).
- **Phase B** — deduplicate within each list by primary key before flushing, so
  re-encountered tables (a table appears as both a `defined_table` and a `source`
  in the same file) are written once.
- **Phase C** — issue one bulk call per non-empty group, reducing per-file
  round-trips from ~100 to ~10 (2–4 node batches + 4–6 edge batches).

[`Neo4jBackend`](src/sqlcg/core/neo4j_backend.py) receives stub implementations
that raise `NotImplementedError`. `reindex_file` (the watch path) is deliberately
left on the old per-row API — it processes one file at a time and a single
transaction overhead there is acceptable.

#### Correctness properties

The homogeneity constraint (every row in a bulk call must share the same property
key set) is enforced at call time in `KuzuBackend` — it raises `ValueError` on
the first heterogeneous row. This matches the homogeneous schema that
`_upsert_parsed_file` produces: all table rows have the same four fields, all
column rows have the same three fields, etc. The constraint is internal rather
than API-contractual (the docstring says "MAY raise") which means future callers
that build heterogeneous batches will get a runtime error rather than a type error.
This is an acceptable tradeoff for now given there is only one call site.

`_validate_props` is applied per-row before the UNWIND query is issued, preserving
the injection-safety guarantee from the single-row path.

Identity deduplication is by primary key string equality. For `TABLE` nodes this is
`full_id` (schema-qualified name). Tables with different qualifiers that represent
the same physical object (e.g. `DWH.SCHEMA.T` vs `SCHEMA.T` in different files)
will not be merged — this is consistent with the existing per-row behaviour and is
not a regression.

#### Neo4j gap

Both bulk methods raise `NotImplementedError` on `Neo4jBackend`. If the Neo4j
backend is ever activated for a production corpus, indexing will fail immediately
on any file. The presence of a `NotImplementedError` stub means tests that use a
`Neo4jBackend` instance will raise rather than silently producing wrong results,
which is the correct failure mode. Implementing the Neo4j bulk path would require
either the same UNWIND pattern (supported in Neo4j Cypher) or a `neo4j.BulkWriter`
session. This is low priority while KuzuDB is the sole production backend.

#### Risk: T-04 measurement not yet completed

The 5-minute budget for 1,600 files remains unverified. The plan
([`plan/sprint_08_perf_upsert.md`](plan/sprint_08_perf_upsert.md)) makes a
credible case that bulk upsert plus pass-2 skip reduce round-trips by ~10×, which
should bring the estimated 23-minute baseline well under budget. However, actual
throughput numbers are absent from this branch. A post-merge measurement run is
the first open follow-up listed in §14.5.

### 14.2 T-02: Pass-2 Skip Predicate

#### What changed

[`CrossFileAggregator._needs_pass2(parsed)`](src/sqlcg/lineage/aggregator.py)
returns `True` only when at least one statement source in `parsed` resolves against
a table registered by a different file — specifically against `self.sources`
(full-qualified `defined_tables` from other files) or `self.cross_file_sources`
(CTAS body bare names from other files), after excluding same-file-defined tables
from the cross-file set.

`resolve_pass2` short-circuits immediately when `_needs_pass2` returns `False`:
it returns the original `parsed` object unchanged without reading the file from
disk or calling `parser.parse_file`. The indexer detects the skip using Python
object identity (`resolved is parsed`) and increments `pass2_skipped` in the
summary dict.

#### CTE scope limitation

The skip predicate examines `stmt.sources` — the statement-level source table
list. This list is populated from the outermost `FROM`/`JOIN` clauses and from
CTAS/INSERT targets. CTEs that are named in the top-level `WITH` clause but whose
_body_ references a cross-file table are not represented in `stmt.sources` directly;
they appear only in the inner `SELECT` sources list of the CTE body, which is not
surfaced on `StatementNode.sources`.

Consequence: a file containing only CTE-mediated cross-file references (no
direct `FROM cross_file_table` in the outer `SELECT`) may be incorrectly classified
as skip-eligible and miss the pass-2 re-parse. In practice this is rare —
most ETL files in the DWH corpus have at least one direct `FROM` or `INSERT INTO`
referencing a cross-file table. The risk is recorded here for the sprint that
eventually adds deep CTE source introspection (see §14.5, follow-up 2).

The identity semantics contract is documented in the `resolve_pass2` docstring:
callers MUST NOT introduce a `.copy()` on the skip path, as that would silently
break the identity check in the indexer.

### 14.3 T-03: Timeout Fix and Thread-Leak Caveat

#### What changed

[`_index_single_file`](src/sqlcg/indexer/indexer.py) previously used a `with
ThreadPoolExecutor(max_workers=1) as ex:` context manager. When the
`future.result(timeout=N)` call raised `concurrent.futures.TimeoutError`,
Python's context manager `__exit__` called `executor.shutdown(wait=True)`,
which blocked indefinitely waiting for the still-running parser thread to
finish — defeating the purpose of the timeout.

The fix replaces the `with` block with an explicit `try/finally`, calling
`executor.shutdown(wait=False, cancel_futures=True)` unconditionally on both
the success path and the timeout path. `cancel_futures=True` was added in
Python 3.9; the project requires 3.12, so this is safe.

#### Thread-leak caveat

`cancel_futures=True` cancels futures that have not yet started. Because
`max_workers=1` and the future is submitted immediately before
`future.result()`, the future will have already been picked up by the worker
thread by the time `shutdown` is called. The running thread is therefore not
cancelled — it is simply abandoned. The thread continues executing until
sqlglot finishes parsing or raises. Its result is discarded.

This is the documented and intended behaviour (see the docstring added in this
commit). The leak is bounded: the indexer runs at `concurrency=1` (one file at
a time), so at most one abandoned thread exists at any moment. The thread will
exit naturally when sqlglot returns; it holds no database connection (parsing is
pure-Python with no I/O). On a large repo with many timeout events, the process
will accumulate threads temporarily, but because each thread is CPU-bound and
eventually terminates, there is no permanent resource leak.

A stricter mitigation — running sqlglot in a subprocess so the OS can forcibly
kill it — was explicitly listed as a non-goal in the sprint plan. It remains an
option if the production profiling in T-04 shows widespread timeout events.

### 14.4 T-05: E1 Literal-Skip Parser Improvement

#### What changed

In [`parsers/base.py`](src/sqlcg/parsers/base.py) at the point where
`_extract_column_lineage` iterates over projected column expressions, a
two-line guard was added:

```python
if not list(col_expr.find_all(exp.Column)):
    continue
```

Expressions that contain no `exp.Column` descendants — `NULL`, numeric literals,
string literals, zero-argument functions such as `CURRENT_TIMESTAMP()`, and
arithmetic on literals — are skipped without calling `sg_lineage`. This
eliminates the 1,148 false `"Cannot find column 'NULL' in query"` (E1) fires
observed in the production corpus baseline.

#### Correctness

The guard is conservative: any expression with at least one real column reference
(e.g. `COALESCE(src_col, NULL)` or `amount * 0.9`) contains an `exp.Column` node
and passes through unchanged. Only expressions that are fully literal pass the
guard. `exp.Column` covers simple column references, qualified references
(`schema.table.col`), and aliased columns — all syntactic forms that produce a
meaningful lineage link. The `find_all` traversal is depth-unlimited, so nested
literal expressions (e.g. `CASE WHEN NULL THEN NULL ELSE NULL END`) are correctly
classified as column-free.

The fix does not silence genuine E1 errors from unresolved column names — those
arise in expressions that do contain `exp.Column` and reach `sg_lineage`, which
may still raise. Only literal-only expressions are suppressed.

#### Scope

The fix is deliberately narrow: E1 errors from actual unresolved columns (e.g.
a column spelled incorrectly or from a CTE body not available in the parse scope)
are out of scope and remain visible in `parsed.errors`. The guard's comment in
code makes this distinction explicit.

### 14.5 Open Follow-ups from Sprint 08

1. **T-04 measurement pending** — run a full index on the DWH/DDL/changelogs
   corpus (target: 1,600 files), record timing and edge counts to
   `plan/measurements/sprint_08_changelogs_fullindex.json`, and verify the
   5-minute budget. Compare against the sprint-07 baseline
   (`plan/measurements/sprint_07_changelogs_noschema.json`). If the budget is
   not met, profile to determine whether the remaining bottleneck is commit
   overhead, star-source expansion, or another factor.
2. **CTE body sources not inspected by _needs_pass2** — files whose only
   cross-file references are inside CTE bodies (no direct outer `FROM`) may
   be incorrectly skipped. Low probability in the DWH corpus but worth adding
   a regression fixture in the unit test if a real case is encountered.
3. **Neo4j bulk upsert** — both methods are `NotImplementedError` stubs. If
   Neo4j support is re-activated, implement using the same `UNWIND $rows MERGE`
   pattern (Cypher syntax is identical) or the `neo4j.BulkWriter` session API.
4. **Subprocess isolation for runaway parsers** — the current thread-abandon
   approach is acceptable for infrequent timeouts. If T-04 measurement shows
   a significant fraction of files timing out (>1%), consider migrating to a
   subprocess with `multiprocessing.Process.kill()` to guarantee hard termination.
