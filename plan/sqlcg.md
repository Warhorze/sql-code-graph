# Feature Plan: sqlcg — Full Implementation

## Summary

Build `sqlcg` (sql-code-graph): a self-contained MCP server that parses `.sql` files
with `sqlglot`, persists table/column/query nodes and column-lineage edges into an
embedded KùzuDB graph, and exposes lineage queries as MCP tools consumable by Claude.
This plan covers all five blueprint phases from project scaffold to benchmarking, with
the four HIGH-risk architecture findings as mandatory constraints on the implementation
sequence.

---

## Scope

### In Scope

- Project scaffold (`uv`, `pyproject.toml`, `src/sqlcg/` layout)
- `GraphBackend` ABC + `KuzuBackend` (primary) + `Neo4jBackend` (stub, CI-tested)
- Frozen data model: `TableRef`, `ColumnRef`, `LineageEdge`, `QueryNode`, `ParsedFile`
- `SchemaResolver` with `lru_cache`/invalidation + thread-safety fix (finding 3.3)
- `SqlParser` base class with fallback table scan
- Five dialect parsers: ANSI, Snowflake, BigQuery, Postgres, T-SQL
- `CrossFileAggregator` two-pass resolver with hardened `resolve_pass2` (finding 3.2)
- `IndexerWalker` + `Indexer` with atomic re-index using real `transaction()` (finding 3.1)
- `sqlcg watch` via `watchdog` with per-file `threading.Timer` (2 s debounce)
- `jobs.py` concurrency contract: one `Timer` per file path, no shared `SchemaResolver` across concurrent jobs
- Hardened `_extract_column_lineage` with structured error recording (finding 3.4)
- Full KùzuDB graph schema (DDL from blueprint §7) plus `Column` cleanup in `delete_nodes_for_file` (finding 5.5)
- Typer CLI: `index`, `watch`, `find`, `analyze`, `mcp`, `db` command groups
- `execute_cypher` MCP tool with read-only blocklist (resolved Q3)
- All named MCP tools (`trace_column_lineage`, `find_table_usages`, etc.)
- `add_information_schema` raises `NotImplementedError` with clear message (resolved Q6)
- `--schema-from-info-schema` CLI flag errors immediately with that message
- `pyproject.toml` with `requires-python = ">=3.12"` (resolved Q5)
- Benchmark suite: TPC-H, TPC-DS subset, SQLMesh fixtures, golden corpus, adversarial
- CI: pytest matrix on 3.12 + 3.13, both backends, benchmark gate

### Non-Goals

- `add_information_schema` implementation (deferred to v2 per resolved Q6)
- `wizard.py` InquirerPy first-run wizard (no spec exists; removed from v1 scope per finding 5.2)
- Mixed-dialect single-directory indexing (users run `sqlcg index` twice, one `--dialect` per invocation)
- DataHub `SqlParsingAggregator` integration beyond the `snowflake` optional extra
- Recursive CTE column-level lineage (sqlglot semantic gap; table-level only)
- Production KùzuDB cluster mode

---

## Design

### Resolved Decisions Honoured

| Decision | Constraint on implementation |
|---|---|
| Single `--dialect` per invocation | `IndexerWalker` takes one `dialect: str \| None`; `SchemaResolver` instantiated once per run |
| Additive MERGE keyed by `abs_path` | `Repo` node DDL has `path STRING PRIMARY KEY`; `sqlcg db reset --repo <path>` deletes one repo's subgraph |
| `execute_cypher` ships in v1 read-only | Strip quoted literals first, then blocklist `{CREATE,MERGE,DELETE,SET,REMOVE,DROP}`; auto-LIMIT 500 |
| Per-file `threading.Timer` (2 s debounce); `SchemaResolver` NOT shared | `jobs.py` constructs a fresh `SchemaResolver` per re-index job; no global shared instance |
| `requires-python = ">=3.12"` | `pyproject.toml` + CI matrix 3.12 and 3.13 |
| `add_information_schema` raises `NotImplementedError` | Method body is `raise NotImplementedError(...)` from day one |

### API Changes

New CLI binary `sqlcg` installed via `[project.scripts]` entry point. No existing API
surface is modified (greenfield). MCP tools are new FastMCP endpoints.

### Data Models

All types live in `src/sqlcg/parsers/base.py` and are frozen before any parser code:

```
TableRef(frozen)      catalog, db, name, alias  → .qualified property
ColumnRef(frozen)     table: TableRef, name
LineageEdge(frozen)   src, dst, transform, confidence, query_id
QueryNode(mutable)    file, statement_index, sql, kind, target, sources, ctes,
                      column_lineage, parse_failed, confidence, parsing_mode
ParsedFile(mutable)   path, dialect, statements, defined_tables, referenced_tables, errors
```

`QueryNode` is intentionally mutable (pass-2 patching via field assignment). The
mutability contract is documented in the class docstring. `LineageEdge` is frozen;
callers must not rely on `QueryNode` identity after pass 2.

`Column.id` construction: `".".join(p for p in (catalog, db, table, col_name) if p)` —
all non-None components, preventing the multi-catalog collision described in finding 5.4.

### Graph Schema Changes vs Blueprint

- `Repo` node: `path STRING PRIMARY KEY` added (resolved Q2)
- `JOINS` relationship: deferred to v2 (finding 5.3 — misleading structural semantics)
- `delete_nodes_for_file` scope extended to `Column` nodes associated with `Table` nodes
  `DEFINED_IN` the file (finding 5.5)

### Dependencies

```toml
requires-python = ">=3.12"
dependencies = [
    "sqlglot[rs]>=28.0,<32.0",      # widened upper bound per finding 3.9
    "kuzu==0.11.3",
    "mcp>=1.27.0,<2.0",
    "typer[all]>=0.9.0",
    "rich>=13.7.0",
    "watchdog>=3.0.0",
    "pathspec>=0.12.1",
    "python-dotenv>=1.0.0",
    "pydantic>=2.0",
    "dbt-artifacts>=1.0.0",
]

[project.optional-dependencies]
neo4j     = ["neo4j>=5.15.0"]
dbt       = ["dbt-core>=1.7"]
snowflake = ["acryl-datahub[sql-parsing]>=0.14.0,<0.15.0"]  # pinned per finding 3.10
```

Note: `inquirerpy` removed (wizard deferred). sqlglot upper bound widened to `<32.0`
(finding 3.9). acryl-datahub upper-bounded to `<0.15.0` (finding 3.10).

---

## Implementation Steps

### Phase 1 — Scaffold, Data Model, Backend ABC (Days 1–5)

**Step 1.1 — Project scaffold**

- Files affected: `pyproject.toml`, `src/sqlcg/__init__.py`, `src/sqlcg/__main__.py`,
  `src/sqlcg/utils/logging.py`, `src/sqlcg/utils/hashing.py`, `.env.example`,
  `.sqlcgignore`, `README.md`
- Tasks:
  - `uv init sql-code-graph && uv add "sqlglot[rs]" kuzu typer pydantic pytest`
  - Set `requires-python = ">=3.12"` in `pyproject.toml`
  - Add `[project.scripts] sqlcg = "sqlcg.cli.main:main"`
  - Create `src/sqlcg/utils/logging.py`: stderr-only logger factory (`getLogger` wrapper
    that always uses `stream=sys.stderr`); no `print()` anywhere in the package
  - Create `src/sqlcg/utils/hashing.py`: `def hash_sql(sql: str) -> str` — returns a
    SHA-256 hex digest of the normalized SQL bytes. Used to populate `File.sha` in the
    graph (DDL §7 declares `sha STRING` on the `File` node). Normalization: strip leading
    and trailing whitespace before hashing.
  - Add `.sqlcgignore` with sensible defaults (`*.bak`, `tmp/`, `node_modules/`)
- Acceptance:
  - `python -m sqlcg --help` exits 0
  - `import sqlcg` does not write to stdout

**Step 1.2 — GraphBackend ABC**

- Files affected: `src/sqlcg/core/graph_db.py`
- Tasks:
  - Implement the ABC exactly as in blueprint §3.1 with one addition: the `transaction()`
    base implementation is a no-op that logs a WARNING "transaction() not overridden —
    no rollback guarantee", making the omission visible in test output
  - Declare the abstract contract in docstrings: each method documents what "upsert"
    means (idempotent MERGE, not INSERT-or-error)
- Acceptance:
  - Cannot instantiate `GraphBackend` directly (`TypeError`)
  - Instantiating a subclass that omits any abstract method raises `TypeError`

**Step 1.3 — KùzuDB graph schema DDL**

- Files affected: `src/sqlcg/core/schema.py`
- Tasks:
  - Define the full DDL string from blueprint §7, with two changes:
    - `CREATE NODE TABLE Repo (path STRING PRIMARY KEY, name STRING)` — primary key on path
    - Remove `CREATE REL TABLE JOINS` — deferred to v2
  - Define label and relationship-name constants (`NODE_FILE`, `REL_COLUMN_LINEAGE`, etc.)
    to prevent typo-prone string literals scattered across query code
  - Add `SCHEMA_VERSION = "1"` constant used by `db init` to detect schema mismatch
- Acceptance:
  - All DDL statements are parseable by KùzuDB 0.11.3 (verified in Step 1.4 test)

**Step 1.4 — KuzuBackend (primary)**

- Files affected: `src/sqlcg/core/kuzu_backend.py`
- Tasks:
  - Implement all abstract methods: `upsert_node`, `upsert_edge`, `run_read`,
    `delete_nodes_for_file`, `close`
  - Implement `transaction()` using KùzuDB's connection-level transaction API.
    **Developer note**: before coding, verify the exact Python method names in KùzuDB
    0.11.3 docs — the connection object exposes `begin_transaction()`, not `begin()`;
    commit and rollback are `commit()` and `rollback()`. The pattern to implement is:
    ```python
    @contextmanager
    def transaction(self):
        # Verify against kuzu 0.11.3 Python API: conn.begin_transaction() /
        # conn.commit() / conn.rollback(). Do not assume neo4j-style begin().
        self._conn.begin_transaction()
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
    ```
    If `begin_transaction()` does not exist on the connection object in 0.11.3, check
    whether KùzuDB 0.11.3 uses `set_auto_commit(False)` / `commit()` / `rollback()`
    instead — document the actual API used in a code comment.
  - `delete_nodes_for_file` must delete BOTH `Query` nodes and orphaned `Column` nodes
    linked to `Table` nodes `DEFINED_IN` the file (finding 5.5).
    **Implementation note**: KùzuDB does not support multiple statements in a single
    `conn.execute()` call. Issue each of the four Cypher statements as a separate
    `self._conn.execute(...)` call within the active transaction:
    ```cypher
    -- Step A: delete Column nodes for tables defined in this file
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)-[:HAS_COLUMN]->(c:Column)
    DETACH DELETE c;
    -- Step B: delete Query nodes and all their edges
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(q:Query)
    DETACH DELETE q;
    -- Step C: delete Table nodes defined in this file (re-inserted on re-parse)
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)
    DETACH DELETE t;
    -- Step D: delete the File node itself
    MATCH (f:File {path: $path})
    DETACH DELETE f;
    ```
  - `upsert_node` uses `MERGE (n:Label {id: $key}) SET n += $props`
  - `upsert_edge` uses `MATCH ... MERGE (src)-[r:REL]->(dst) SET r += $props`
- Acceptance:
  - `test_kuzu_backend.py`: upsert idempotency (call twice, assert one node)
  - `test_kuzu_backend.py`: `transaction()` rollback — inject exception inside context,
    assert no nodes were persisted
  - `test_kuzu_backend.py`: `delete_nodes_for_file` removes `Column` nodes too

**Step 1.5 — Neo4jBackend (CI-tested stub)**

- Files affected: `src/sqlcg/core/neo4j_backend.py`
- Tasks:
  - Implement all abstract methods using `neo4j` driver (optional dependency)
  - `transaction()` uses `session.begin_transaction()` with `tx.commit()` / `tx.rollback()`
  - Cypher queries must be the exact same strings as `KuzuBackend` where possible;
    document any divergences inline
  - Guard instantiation: if `neo4j` package not installed, raise `ImportError` with
    install instructions
- Acceptance:
  - `test_kuzu_backend.py` tests run against `Neo4jBackend` via a `backend` fixture
    parametrized over both backends (skips Neo4j tests if package not installed)
  - Both backends produce identical `run_read` results for the same inserted data

**Step 1.6 — Freeze data model**

- Files affected: `src/sqlcg/parsers/base.py`
- Tasks:
  - Implement `TableRef`, `ColumnRef`, `LineageEdge` as `@dataclass(frozen=True)`
  - Implement `QueryNode` as mutable `@dataclass` with class docstring:
    "Mutable by design — pass-2 patching uses direct field assignment. Do not freeze."
  - Implement `ParsedFile` as mutable `@dataclass`
  - Add `TableRef.full_id` property: `".".join(p for p in (catalog, db, name) if p)` —
    used as `Table.qualified` graph key
  - Add `ColumnRef.full_id` property: `f"{table.full_id}.{name}"` — used as `Column.id`
    graph key (finding 5.4)
- Acceptance:
  - `hash(LineageEdge(...)) == hash(LineageEdge(...))` for equal instances
  - `TableRef` with catalog collision test: two catalogs, same db.table, different `full_id`
  - `ColumnRef.full_id` includes all non-None components

---

### Phase 2 — Parser Layer (Days 6–10)

**Step 2.1 — SchemaResolver**

- Files affected: `src/sqlcg/lineage/schema_resolver.py`
- Tasks:
  - Implement `SchemaResolver` exactly as in blueprint §4.2
  - Add `threading.Lock` (`self._lock = threading.Lock()`) protecting all mutations and
    the `_invalidate_cache` call (finding 3.3):
    ```python
    def __init__(self, dialect=None):
        self.dialect = dialect
        self._tables: dict = {}
        self._view_bodies: dict = {}
        self._lock = threading.Lock()
        self._cache: dict | None = None  # manual cache; lru_cache is not usable on
                                         # instance methods (self is unhashable)

    def add_create_table(self, ast):
        with self._lock:
            # ... mutation ...
            self._cache = None  # invalidate

    def as_dict(self) -> dict:
        with self._lock:
            if self._cache is None:
                self._cache = self._build_dict()
            return self._cache

    def _build_dict(self) -> dict:
        # Called only under self._lock. Build the nested schema dict.
        out = {}
        for (cat, db, name), cols in self._tables.items():
            cur = out
            for k in [cat, db]:
                if k:
                    cur = cur.setdefault(k, {})
            cur[name] = cols
        return out

    def _invalidate_cache(self):
        # May be called while lock is held or without lock (depends on caller).
        # Prefer calling via add_* methods which hold the lock.
        self._cache = None
    ```
    **Rationale**: `functools.lru_cache` on an instance method uses `self` as part of
    the cache key. Plain class instances are not hashable by default, so calling
    `lru_cache`-decorated instance methods raises `TypeError: unhashable type` at
    runtime. Use a `_cache: dict | None` field with manual invalidation instead.
    The `threading.Lock` guards both the mutation and the cache read/write, making
    the lock-then-read sequence atomic.
  - `add_information_schema(self, csv_path)` body:
    `raise NotImplementedError("--schema-from-info-schema is not yet implemented (v2)")`
  - `add_view_sources`, `add_dbt_manifest` implemented as per blueprint
  - Class docstring: "Thread-safety: a Lock guards all cache mutations. The lock is
    re-entrant only within a single thread. Do not share a SchemaResolver instance
    across concurrent jobs — construct one per re-index job instead (see jobs.py)."
- Acceptance:
  - `test_schema_resolver.py`: cache is cleared after `add_create_table`
  - `test_schema_resolver.py`: concurrent `add_create_table` + `as_dict` calls from two
    threads do not raise or return partial state (use `threading.Barrier`)
  - `test_schema_resolver.py`: `add_information_schema` raises `NotImplementedError`

**Step 2.2 — SqlParser base class**

- Files affected: `src/sqlcg/parsers/base.py` (extend), `src/sqlcg/parsers/registry.py`
- Tasks:
  - Implement `SqlParser` ABC as per blueprint §3.3 with the following changes to
    `_extract_column_lineage` (finding 3.4):
    ```python
    try:
        root = sg_lineage(col_name, body, schema=schema, dialect=self.DIALECT)
    except Exception as exc:
        self._log.warning(
            "column lineage extraction failed: file=%s col=%s error=%s",
            path, col_name, exc,
        )
        out.errors.append(f"col_lineage:{col_name}:{exc}")
        # Emit a zero-confidence placeholder edge so the column appears in
        # the graph with explicit signal that lineage is unknown.
        edges.append(LineageEdge(
            src=ColumnRef(TableRef(None, None, "<unknown>"), col_name),
            dst=downstream,
            transform="UNKNOWN",
            confidence=0.0,
        ))
        continue
    ```
  - `_real_tables` implements the `build_scope` fallback exactly as in blueprint §3.3
  - `_classify` handles all nine `kind` values
  - `registry.py` with `PARSERS` dict and `get_parser(dialect, schema_resolver)` factory
- Acceptance:
  - `test_parser.py`: ten queries from `sqlglot/tests/fixtures/identity.sql` — correct
    `kind`, `target`, `sources`
  - `test_parser.py`: failed `sg_lineage` call appends to `ParsedFile.errors` and emits
    a `confidence=0.0` edge (not a silent skip)
  - `test_parser.py`: `build_scope` returning `None` triggers fallback and logs a WARNING

**Step 2.3 — Snowflake parser**

- Files affected: `src/sqlcg/parsers/snowflake_parser.py`
- Tasks:
  - Implement `SnowflakeParser` as per blueprint §8.3 with one change to the scripting
    block heuristic (finding 3.5):
    ```python
    def _has_scripting_block(self, sql: str) -> bool:
        """Token-aware BEGIN detection — avoids false-positives on string literals
        and comments that contain the word BEGIN."""
        from sqlglot import tokens as sg_tokens
        try:
            toks = sg_tokens.Tokenizer.from_dialect("snowflake").tokenize(sql)
        except Exception:
            return bool(_SCRIPTING_BLOCK.search(sql))  # fallback to regex
        return any(
            t.token_type == sg_tokens.TokenType.BEGIN
            for t in toks
        )
    ```
    Replace `_SCRIPTING_BLOCK.search(sql)` in `parse_file` with `self._has_scripting_block(sql)`.
  - Fix `_EMBEDDED_DML` regex: add `re.MULTILINE` and replace `$` end-of-string anchor
    with `(?=;|\Z)` to prevent greedy merge across statement boundaries (finding 3.12):
    ```python
    _EMBEDDED_DML = re.compile(
        r'(SELECT\s+.+?(?=;|\Z)|INSERT\s+INTO.+?(?=;|\Z)'
        r'|UPDATE\s+.+?(?=;|\Z)|DELETE\s+.+?(?=;|\Z))',
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    ```
  - Gap 1 retry-with-quoting: on `sqlglot.ParseError`, quote the identifier before the
    colon and retry once; on second failure emit `confidence=0.0` edge
  - Gap 6 `COPY INTO @stage`: emit `STAGE` node + `LOADS_FROM_STAGE` rel, not table lineage
- Acceptance:
  - `test_parser.py` + golden corpus fixtures:
    - `colon_reserved_word.sql` — does not crash, emits `confidence<1.0` edge
    - `scripting_block.sql` — `parse_failed=True`, `confidence=0.3`
    - `copy_into.sql` — no `Table` node created for `@stage`, `LOADS_FROM_STAGE` rel emitted
    - `lateral_flatten.sql` — table-level edge present, no column edges, `transform="FLATTEN"`
    - `identifier_dynamic.sql` — placeholder node `<dynamic:IDENTIFIER>` in sources
    - Comment with word BEGIN: `_has_scripting_block` returns `False`
    - String literal `'BEGIN'`: `_has_scripting_block` returns `False`

**Step 2.4 — BigQuery, Postgres, T-SQL parsers**

- Files affected: `src/sqlcg/parsers/bigquery_parser.py`, `postgres_parser.py`, `tsql_parser.py`
- Tasks:
  - Each is a thin `SqlParser` subclass setting `DIALECT` and overriding only what is
    dialect-specific (BigQuery: `DECLARE/IF/CALL → exp.Command` handled same as Snowflake
    scripting; T-SQL: `BEGIN/END` same pattern; Postgres: no special cases for v1)
  - `BigQueryParser._has_scripting_block` reuses the token-aware approach with
    `dialect="bigquery"` and `TokenType.DECLARE` / `TokenType.IF` in addition to `BEGIN`
- Acceptance:
  - `test_dialect_matrix.py`: each dialect parser produces non-empty `sources` for a
    representative five-query fixture per dialect

---

### Phase 3 — Cross-File Indexer (Days 11–16)

**Step 3.1 — CrossFileAggregator with hardened resolve_pass2**

- Files affected: `src/sqlcg/lineage/aggregator.py`
- Tasks:
  - Implement `register_pass1` as per blueprint §4.1
  - Implement `resolve_pass2` with mandatory error handling (finding 3.2):
    ```python
    def resolve_pass2(self, parser, parsed):
        parser.schema_resolver.add_view_sources(self.sources)
        try:
            sql = parsed.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
                parsed.path, exc,
            )
            return parsed
        return parser.parse_file(parsed.path, sql)
    ```
- Acceptance:
  - `test_cross_file_lineage.py`: view defined in file A resolves correctly from file B
  - `test_cross_file_lineage.py`: `resolve_pass2` with a deleted file returns the
    pass-1 `ParsedFile` unchanged and logs a WARNING (no exception propagated)

**Step 3.2 — IndexerWalker**

- Files affected: `src/sqlcg/indexer/walker.py`, `src/sqlcg/utils/ignore.py`
- Tasks:
  - Walk directory tree respecting `.sqlcgignore` via `pathspec`
  - Yield `Path` objects for all `.sql` files
  - `ignore.py`: load and match `.sqlcgignore` patterns
- Acceptance:
  - `test_indexer_to_graph.py`: walker yields only `.sql` files, respects ignore patterns

**Step 3.3 — Indexer (orchestrator) with atomic re-index**

- Files affected: `src/sqlcg/indexer/indexer.py`
- Tasks:
  - `index_repo(path, dialect, db, dbt_manifest=None)`: full two-pass index
    1. Pass 1: walk all files, `parse_file` each, `aggregator.register_pass1`
    2. (Optional) load dbt manifest into `schema_resolver`
    3. Pass 2: `aggregator.resolve_pass2` for each file
    4. Upsert all nodes/edges via `db`
  - `reindex_file(file_path, db, dialect)`: dependency-aware incremental re-index
    (finding 3.1 — use `db.transaction()`).
    Define `STALE_VIEWS_QUERY` as a module-level constant in `indexer.py` (taken from
    blueprint §7):
    ```python
    STALE_VIEWS_QUERY = """
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)
      <-[:SELECTS_FROM]-(q:Query)-[:DECLARES]->(v:Table {kind: 'VIEW'})
    RETURN DISTINCT v.qualified AS view_name
    """
    ```
    Implementation:
    ```python
    def reindex_file(self, file_path: str, db: GraphBackend, dialect: str | None) -> None:
        stale_views = db.run_read(STALE_VIEWS_QUERY, {"path": file_path})
        with db.transaction():
            db.delete_nodes_for_file(file_path)
            schema_resolver = SchemaResolver(dialect=dialect)  # fresh per job (finding 3.3)
            parser = get_parser(dialect, schema_resolver)
            sql = Path(file_path).read_text(encoding="utf-8")
            parsed = parser.parse_file(Path(file_path), sql)
            self._upsert_parsed_file(parsed, db)
        for row in stale_views:
            self._reindex_view_definition(row["view_name"], db, dialect)
    ```
  - Note: a fresh `SchemaResolver` is constructed inside `reindex_file`, not shared
    from the outer run (this is the mandatory fix for finding 3.3 in the watch path)
  - `_upsert_parsed_file`: maps `ParsedFile` → graph nodes/edges using label constants
    from `schema.py`; builds `Column.id` via `ColumnRef.full_id` (finding 5.4)
  - Per-file timeout: if `--timeout-per-file N` is set (default 30), wrap
    `_index_single_file` in `concurrent.futures.ThreadPoolExecutor` with
    `future.result(timeout=N)` (finding 3.7); on `TimeoutError` log WARNING and continue
  - SIGINT handler: catch `KeyboardInterrupt` in the walker loop, flush current progress
    to DB, then re-raise
- Acceptance:
  - `test_indexer_to_graph.py`: full index of `tests/fixtures/synthetic/` produces
    expected node counts
  - `test_indexer_to_graph.py`: inject an exception in `_index_single_file` during
    `reindex_file`; assert the graph is in the same state as before the call (rollback)
  - `test_indexer_to_graph.py`: SIGINT during index flushes and exits cleanly

**Step 3.4 — jobs.py: watch concurrency contract**

- Files affected: `src/sqlcg/core/jobs.py`
- Tasks:
  - Implement `WatchJobManager`:
    ```python
    class WatchJobManager:
        def __init__(self, indexer, db, dialect, debounce_seconds=2.0):
            self._timers: dict[str, threading.Timer] = {}
            self._lock = threading.Lock()
            ...

        def schedule(self, file_path: str) -> None:
            """Cancel any pending timer for this path and start a fresh one."""
            with self._lock:
                if file_path in self._timers:
                    self._timers[file_path].cancel()
                t = threading.Timer(
                    self._debounce,
                    self._run_job,
                    args=[file_path],
                )
                self._timers[file_path] = t
                t.start()

        def _run_job(self, file_path: str) -> None:
            """Executed after debounce. Fresh SchemaResolver per job."""
            try:
                self._indexer.reindex_file(file_path, self._db, self._dialect)
            except Exception as exc:
                logger.error("reindex_file failed: %s: %s", file_path, exc)
            finally:
                with self._lock:
                    self._timers.pop(file_path, None)
    ```
  - Two concurrent timer expirations for different files are allowed to overlap;
    each uses its own `SchemaResolver` (constructed inside `reindex_file`)
  - The `_run_job` exception handler ensures the timer is removed from `_timers` even
    on failure, preventing stale entries
- Acceptance:
  - `test_watch.py` (e2e): modify file A and file B within 1 s; assert both are
    re-indexed and the graph reflects both changes.
    **Implementation note**: do not use wall-clock `time.sleep()` assertions in tests
    — they are flaky under CI load. Use `threading.Event` (or replace
    `threading.Timer` with a synchronous mock in unit tests):
    - In unit tests: inject a fake timer factory that calls the callback synchronously
      so the debounce fires immediately.
    - In e2e tests: use an `Event` set inside the mocked `reindex_file` and
      `Event.wait(timeout=10)` rather than a hard sleep.
  - `test_watch.py`: rapid saves to the same file within the 2 s debounce window result
    in exactly one re-index call (verified via call-count mock on `reindex_file`)

**Step 3.5 — Watcher (watchdog integration)**

- Files affected: `src/sqlcg/indexer/watcher.py`
- Tasks:
  - `watchdog` `FileSystemEventHandler` subclass; on `on_modified` / `on_created` /
    `on_moved` (to path) call `job_manager.schedule(file_path)`
  - `on_deleted`: call `db.delete_nodes_for_file(file_path)` directly (no re-parse needed)
  - Filter: only `.sql` files not matched by `.sqlcgignore`
- Acceptance:
  - `test_watch.py`: file deletion removes nodes from graph

**Step 3.6 — dbt adapter**

- Files affected: `src/sqlcg/indexer/dbt_adapter.py`
- Tasks:
  - `load_dbt_manifest(manifest_path, schema_resolver)`: calls `dbt-artifacts` to load
    manifest, then `schema_resolver.add_dbt_manifest(manifest_path)` for each node
  - Errors loading manifest are logged and do not abort indexing
- Acceptance:
  - `test_indexer_to_graph.py`: indexing `tests/fixtures/jaffle_shop/` with manifest
    produces column-level edges where DDL-only indexing produces table-level only

---

### Phase 4 — CLI (Days 17–20)

**Step 4.1 — CLI skeleton and `db` commands**

- Files affected: `src/sqlcg/cli/main.py`, `src/sqlcg/cli/commands/db.py`,
  `src/sqlcg/core/config.py`
- Tasks:
  - `main.py`: Typer app with all subcommand groups registered
  - `config.py`: load `.env` via `python-dotenv`; expose `get_db_path()` (default
    `~/.sqlcg/graph.db`), `get_backend()` (default `"kuzu"`)
  - `db.py`:
    - `sqlcg db init`: create KùzuDB file, run schema DDL, write `SCHEMA_VERSION`
    - `sqlcg db reset [--repo <path>]`: full wipe or repo-scoped wipe
    - `sqlcg db info`: node/edge counts, schema version, backends available
    - `list_repos`: `MATCH (r:Repo) RETURN r.path, r.name`
- Acceptance:
  - `test_cli_index.py`: `sqlcg db init` idempotent (run twice, no error)
  - `sqlcg db reset --repo <path>` removes only that repo's nodes; other repos intact

**Step 4.2 — `index` command**

- Files affected: `src/sqlcg/cli/commands/index.py`
- Tasks:
  - `sqlcg index <path> [--dialect D] [--dbt-manifest M] [--no-ddl] [--timeout-per-file N]`
  - Opens/creates KùzuDB, calls `Indexer.index_repo`, prints summary with Rich
  - `--schema-from-info-schema` flag: immediately raises `NotImplementedError` message
    and exits non-zero (resolved Q6)
- Acceptance:
  - `test_cli_index.py`: `sqlcg index tests/fixtures/synthetic/` exits 0 and prints
    file count
  - `test_cli_index.py`: `sqlcg index ... --schema-from-info-schema x.csv` exits non-zero
    with the `NotImplementedError` message

**Step 4.3 — `watch` command**

- Files affected: `src/sqlcg/cli/commands/watch.py`
- Tasks:
  - `sqlcg watch <path> [--dialect D]`
  - Starts initial full index, then starts `watchdog` observer + `WatchJobManager`
  - Runs until SIGINT/SIGTERM; on exit stops observer and cancels pending timers
- Acceptance:
  - `test_watch.py` (e2e): process starts, prints "Watching", file modification triggers
    re-index within debounce window + 1 s margin

**Step 4.4 — `find` and `analyze` commands**

- Files affected: `src/sqlcg/cli/commands/find.py`, `src/sqlcg/cli/commands/analyze.py`
- Tasks:
  - `find table <name>`, `find column <table.col>`, `find pattern "<sql>"`
  - `analyze upstream <table.col> [--depth N]`
  - `analyze downstream <table.col> [--depth N]`
  - `analyze impact <table>`
  - `analyze unused [--threshold N]`
  - All delegate to `db.run_read` with the Cypher queries from blueprint §7
  - Output formatted with Rich tables
- Acceptance:
  - `test_cli_index.py`: `sqlcg find table orders` returns results after indexing
    `tests/fixtures/synthetic/` (which contains an `orders` table reference)

**Step 4.5 — `mcp` command**

- Files affected: `src/sqlcg/cli/commands/mcp.py`
- Tasks:
  - `sqlcg mcp setup`: writes MCP server config to `~/.claude/mcp.json` (or prints
    the JSON to stdout for manual use)
  - `sqlcg mcp start`: starts the FastMCP server (delegates to `server.py:main()`)
- Acceptance:
  - `sqlcg mcp setup` prints valid JSON without error

---

### Phase 5 — MCP Server (Days 21–25)

**Step 5.1 — Server bootstrap with stdout guard**

- Files affected: `src/sqlcg/server/server.py`, `src/sqlcg/server/exceptions.py`
- Tasks:
  - `exceptions.py`: define `NotIndexedError`, `InvalidColumnRefError` — used by tools
    and surfaced in tool docstrings (finding 3.11)
  - `server.py`: `_configure_mcp_logging()` redirects `sys.stdout = sys.stderr` before
    `mcp.run()` exactly as in blueprint §6.1
  - Expose `mcp = FastMCP("SQL Code Graph")` for tool registration
- Acceptance:
  - Import `sqlcg.server.server` and assert `sys.stdout is sys.stderr` after calling
    `_configure_mcp_logging()`

**Step 5.2 — Named MCP tools**

- Files affected: `src/sqlcg/server/tools.py`
- Tasks: implement all seven named tools from blueprint §6.2:

  | Tool | Cypher backing query |
  |---|---|
  | `index_repo` | calls `Indexer.index_repo`; returns summary dict |
  | `trace_column_lineage` | `COLUMN_LINEAGE*1..max_depth` path query |
  | `find_table_usages` | all query relationships to the table |
  | `get_downstream_dependencies` | BFS on `COLUMN_LINEAGE` and relationship tables |
  | `get_upstream_dependencies` | reverse BFS |
  | `search_sql_pattern` | `WHERE q.sql CONTAINS $query` with `LIMIT` |
  | `list_dialects_and_repos` | `MATCH (r:Repo)` + dialect aggregation |

  Each tool docstring must include a `Raises` section documenting `NotIndexedError`
  when the graph has no indexed repos (finding 3.11). Docstrings stay under 2 KB.

  Return types: all tools except `index_repo` return a Pydantic model (auto-JSON
  by FastMCP). `index_repo` returns a plain `dict` (matching the blueprint §6.2
  signature `-> dict` with keys `files_parsed`, `parse_errors`, `tables_found`,
  `lineage_edges_created`). FastMCP serialises both to JSON; the distinction is that
  the `dict` return does not produce a named schema in the tool's JSON schema output.

- Acceptance:
  - `test_mcp_tools.py`: each tool called against a pre-indexed fixture graph returns
    the expected Pydantic model without error
  - `test_mcp_tools.py`: `trace_column_lineage` on a non-indexed graph raises
    `NotIndexedError` (surfaced as MCP error response)

**Step 5.3 — `execute_cypher` MCP tool**

- Files affected: `src/sqlcg/server/tools.py`
- Tasks:
  - Implement `execute_cypher(query: str) -> list[dict]` as a named `@mcp.tool()`
  - Read-only enforcement (resolved Q3):
    1. Strip quoted string literals from `query` (replace `'...'` and `"..."` with
       empty strings) to prevent mutation commands hiding inside strings
    2. Check stripped query against blocklist regex:
       `r'\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP)\b'` (case-insensitive)
    3. If matched, raise `ValueError("Write operations are not permitted via execute_cypher")`
    4. If `LIMIT` not in `query.upper()`, append `LIMIT 500`
  - Docstring documents the blocklist and the auto-LIMIT behaviour
- Acceptance:
  - `test_mcp_tools.py`: `execute_cypher("MATCH (n) RETURN n LIMIT 10")` succeeds
  - `test_mcp_tools.py`: `execute_cypher("MATCH (n) DELETE n")` raises `ValueError`
  - `test_mcp_tools.py`: query without `LIMIT` has `LIMIT 500` appended before execution
  - `test_mcp_tools.py`: `execute_cypher("MATCH (n) WHERE n.sql = 'DROP TABLE x' RETURN n")`
    succeeds (mutation keyword inside string literal is stripped before blocklist check)

---

### Phase 6 — Benchmarks and CI (Days 26–30)

**Step 6.1 — Benchmark fixtures**

- Files affected: `tests/benchmarks/tpch/`, `tests/benchmarks/sqlmesh/`,
  `tests/benchmarks/golden_corpus/`, `tests/benchmarks/adversarial/`,
  `tests/fixtures/synthetic/`, `tests/fixtures/jaffle_shop/`
- Tasks:
  - Download TPC-H 24 queries into `tests/benchmarks/tpch/`
  - Copy or symlink the SQLMesh open test suite dialect fixtures into
    `tests/benchmarks/sqlmesh/` (source: `tests/fixtures/` in the sqlmesh GitHub repo).
    The blueprint §11 names this as a primary fixture source for cross-dialect coverage.
    Select the ANSI, BigQuery, Snowflake, and T-SQL subdirectories; exclude dialects not
    in scope for v1. Record the SQLMesh commit SHA in `tests/benchmarks/sqlmesh/SOURCE.txt`.
  - Populate `tests/benchmarks/golden_corpus/snowflake/` with the ten files from
    blueprint §11 (qualify, lateral_flatten, colon_extract, colon_reserved_word,
    scripting_block, identifier_dynamic, copy_into, three_part, create_procedure,
    case_normalization)
  - `tests/benchmarks/adversarial/`: generate `200_join.sql` (200-table join) and
    `500_union.sql` (500 UNION ALL branches)
  - `tests/fixtures/synthetic/`: hand-author a 10-file multi-file dependency chain
    with known expected lineage edges
  - Download `jaffle_shop` public dbt project into `tests/fixtures/jaffle_shop/`
- Acceptance:
  - All fixture files are valid UTF-8 SQL parseable by sqlglot without crashing

**Step 6.2 — Benchmark suite**

- Files affected: `tests/benchmarks/bench_indexer.py`, `docs/BENCHMARKS.md`
- Tasks:
  - `pytest-benchmark` suite measuring:
    - Parse success rate (% files without `exp.Command` fallback)
    - Table-level precision/recall against golden corpus
    - Column-level precision/recall (schema-aware and schema-naive)
    - Cross-file resolution rate against synthetic fixtures
    - Latency p50/p95 per file (TPC-H + adversarial)
    - Throughput files/sec on 1000-file synthetic repo
  - Targets (fail CI if not met):
    - `p95 < 500ms` per file
    - `>= 50 files/sec` throughput
    - `>= 90%` cross-file resolution rate
  - `docs/BENCHMARKS.md`: publish precision/recall separately from coverage (blueprint §11)
- Acceptance:
  - `pytest tests/benchmarks/ --benchmark-only` passes all threshold assertions

**Step 6.3 — CI configuration**

- Files affected: `.github/workflows/test.yml`, `.github/workflows/e2e-tests.yml`,
  `.github/workflows/benchmark.yml`
- Tasks:
  - `test.yml`: pytest matrix on Python 3.12 and 3.13; install optional `neo4j` extra;
    run all unit + integration tests against both `KuzuBackend` and `Neo4jBackend`
    (Neo4j via Docker service in CI)
  - `e2e-tests.yml`: run `tests/e2e/` against a real KùzuDB file
  - `benchmark.yml`: run on push to main; post results as PR comment; fail if
    performance regresses >10% vs previous run
  - Verify KùzuDB 0.11.3 installs on Python 3.13 (note in CI if it fails — do not
    block 3.13 gate, but record it)
- Acceptance:
  - All three CI workflows pass on the main branch after Phase 6 is complete

---

## Test Strategy

### Unit Tests

- `tests/unit/test_kuzu_backend.py`: upsert idempotency, transaction rollback, `delete_nodes_for_file` Column cleanup
- `tests/unit/test_schema_resolver.py`: cache invalidation, thread-safety, `NotImplementedError` on `add_information_schema`
- `tests/unit/test_parser.py`: ANSI parser on 15 canonical queries (kind, target, sources, column_lineage); failed `sg_lineage` → structured error, not silent skip
- `tests/unit/test_scope_walker.py`: `build_scope` fallback triggers on known failure inputs; logs WARNING
- `tests/unit/test_lineage_extractor.py`: `LineageEdge` fields populated correctly for standard SELECT/INSERT/CTAS

### Integration Tests

- `tests/integration/test_indexer_to_graph.py`: full index of synthetic fixtures → correct node/edge counts; `reindex_file` rollback on injected failure; SIGINT flush
- `tests/integration/test_cross_file_lineage.py`: view-to-view lineage across three files; `resolve_pass2` deleted-file fallback
- `tests/integration/test_dialect_matrix.py`: five-query fixture for each of five dialects → non-empty sources

### E2E Tests

- `tests/e2e/test_cli_index.py`: CLI invocations via `subprocess`; `--schema-from-info-schema` exits non-zero
- `tests/e2e/test_mcp_tools.py`: each MCP tool against pre-indexed jaffle_shop; `NotIndexedError` when unindexed; `execute_cypher` blocklist; auto-LIMIT
- `tests/e2e/test_watch.py`: multi-file concurrent saves; single-file rapid saves; file deletion

### Snowflake Golden Corpus

Each of the ten fixture files in `tests/benchmarks/golden_corpus/snowflake/` has a
corresponding test in `tests/unit/test_parser.py` asserting the exact failure mode
and confidence score documented in blueprint §8.2.

---

## Acceptance Criteria

- [ ] `python -m sqlcg --help` exits 0; no stdout output during import
- [ ] `sqlcg db init` is idempotent (run twice, no error, schema version written)
- [ ] `sqlcg index tests/fixtures/synthetic/ --dialect ansi` exits 0; node counts > 0
- [ ] `sqlcg index ... --schema-from-info-schema x.csv` exits non-zero with `NotImplementedError` message
- [ ] `sqlcg find table orders` returns results after indexing a fixture that references `orders`
- [ ] `sqlcg analyze upstream <col>` traces lineage correctly on the synthetic multi-file chain
- [ ] `reindex_file` with injected mid-index failure leaves graph in pre-call state (rollback verified)
- [ ] Two concurrent `reindex_file` calls for different files do not corrupt `SchemaResolver`
- [ ] Rapid saves to the same file within 2 s result in exactly one re-index
- [ ] `execute_cypher("MATCH (n) DELETE n")` raises `ValueError`; mutation inside string literal does not trigger blocklist
- [ ] `trace_column_lineage` on unindexed graph raises `NotIndexedError` (MCP error response)
- [ ] `Column.id` for two catalogs with identical `db.table.col` produces distinct values
- [ ] `delete_nodes_for_file` removes `Column` nodes for tables defined in the file
- [ ] `_extract_column_lineage` failure appends to `ParsedFile.errors` and emits `confidence=0.0` edge
- [ ] `resolve_pass2` with deleted file returns pass-1 result and logs WARNING (no crash)
- [ ] `add_information_schema` raises `NotImplementedError`
- [ ] Snowflake BEGIN in comment or string literal: `_has_scripting_block` returns False
- [ ] Benchmark: p95 parse latency < 500ms; throughput >= 50 files/sec
- [ ] CI: tests pass on Python 3.12 and 3.13 against both backends

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| KùzuDB 0.11.3 not installable on Python 3.13 | CI records failure but does not block 3.13 gate; `GraphBackend` ABC ensures Neo4j fallback is always available |
| sqlglot minor version breaks parser behaviour | Pin `<32.0`; CHANGELOG review step in CI before any bump |
| acryl-datahub internal module changes | Pin `<0.15.0`; test the wrapper (`_parse_scripting_file`), not the module directly |
| `threading.Timer` expiry overlap corrupts graph | Each job constructs a fresh `SchemaResolver`; `db.transaction()` provides rollback; `WatchJobManager._lock` guards `_timers` dict |
| `JOINS` edge type produces misleading lineage | Deferred to v2; not in v1 schema DDL |
| Multi-catalog `Column.id` collision | `ColumnRef.full_id` includes all non-None components |
| `resolve_pass2` file deleted between passes | Caught, logged, returns pass-1 result |
| Benchmark targets not met on adversarial inputs | `--timeout-per-file` (default 30 s) prevents runaway; circuit-breaker logs WARNING and continues |

---

## Rollout Notes

This is a greenfield project with no existing users; no migration or rollback plan is
required for v1. The `SCHEMA_VERSION` constant in `schema.py` enables future schema
migrations: `sqlcg db init` checks the stored version and refuses to operate against
a mismatched schema, prompting `sqlcg db reset`.

For the `sqlcg watch` production use case, document that large repos (>1000 files)
should use `sqlcg index` for the initial full index and only then start `watch` —
running `watch` from scratch on a 10,000-file repo will trigger 10,000 concurrent
timer expirations.
