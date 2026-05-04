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
    -- Step A: delete SqlColumn nodes for tables defined in this file (Deviation 1: SqlTable/SqlColumn labels)
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:SqlTable)-[:HAS_COLUMN]->(c:SqlColumn)
    DETACH DELETE c;
    -- Step B: delete SqlQuery nodes and their edges (Deviation 2: QUERY_DEFINED_IN relationship)
    MATCH (f:File {path: $path})<-[:QUERY_DEFINED_IN]-(q:SqlQuery)
    DETACH DELETE q;
    -- Step C: delete SqlTable nodes defined in this file (re-inserted on re-parse)
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:SqlTable)
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
  - **COMPLIANCE NOTE (Phase 2)**: The `_extract_column_lineage` error-recording path and the
    `build_scope None → WARNING` path are implemented in `base.py` but are NOT covered by
    any test in the current 57-test suite. These two criteria must be added as explicit tests
    before Phase 3 begins. Suggested test locations: `tests/unit/test_parser.py` (mock
    `sg_lineage` to raise; mock `build_scope` to return None) or a dedicated
    `tests/unit/test_lineage_extractor.py` as named in the Test Strategy section.

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
    MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:SqlTable)
      <-[:SELECTS_FROM]-(q:SqlQuery)-[:DECLARES]->(v:SqlTable {kind: 'VIEW'})
    RETURN DISTINCT v.qualified AS view_name
    """
    # NOTE: Uses SqlTable/SqlQuery labels (Deviation 1). DEFINED_IN is Table→File;
    # QUERY_DEFINED_IN is Query→File. This query traverses t→File (DEFINED_IN) and
    # q→t (SELECTS_FROM), so it does NOT use QUERY_DEFINED_IN — correct as written.
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
  - `load_dbt_manifest(manifest_path, schema_resolver)`: loads dbt manifest via raw dict
    access (Phase 3 Step 3.6 note: fragile against dbt manifest version changes; 
    typed models via `dbt-artifacts` deferred to v2), then calls
    `schema_resolver.add_dbt_manifest(manifest_path)`
  - Errors loading manifest are logged and do not abort indexing
  - **Phase 3 Step 3.6 note**: The plan listed `dbt-artifacts>=1.0.0` as a core
    dependency in the blueprint, but this package does not exist in public registries.
    v1 uses raw JSON dict access instead (fragile but functional). For v2, introduce
    `dbt-artifacts` typed models under the `dbt` optional extra.
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
  - **Note on p95 benchmark gate**: Adversarial files (`200_join.sql`, `500_union.sql`)
    that hit the `--timeout-per-file` ceiling must be excluded from the p95 latency
    distribution. Measure them in a separate "adversarial" bucket. Otherwise the gate
    always fails because those files exceed the timeout.
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

## Deviations

### Deviation 1: Node Label Naming (Cypher Reserved Words)

- **Reason**: KùzuDB Cypher parser treats "Table", "Column", "Query" as reserved words,
  causing parser exceptions when used as node labels in MERGE statements. To avoid
  workaround complexity and maintain clean query syntax, we renamed labels.
- **Change**: 
  - `Table` → `SqlTable`
  - `Column` → `SqlColumn`
  - `Query` → `SqlQuery`
- **Impact**: No functional impact — the renamed labels are purely internal implementation.
  All Cypher queries and constants use the new names. Future cross-database support
  (e.g., Neo4j) will see identical logic, just without this constraint.
- **Date**: 2026-05-02

### Deviation 2: Added QUERY_DEFINED_IN Relationship

- **Reason**: The blueprint schema defines DEFINED_IN only from SqlTable to File, but
  QueryNode needs to reference the File it's defined in (for delete_nodes_for_file).
  Without a separate relationship type, we cannot distinguish Query→File from Table→File
  in Cypher.
- **Change**: Added `CREATE REL TABLE QUERY_DEFINED_IN (FROM SqlQuery TO File)` to
  the schema DDL.
- **Impact**: Minimal — Cypher queries for delete_nodes_for_file now use QUERY_DEFINED_IN
  for queries and DEFINED_IN for tables. This provides clarity and prevents relationship
  ambiguity.
- **Date**: 2026-05-02

---

### Phase 7 — Airbnb Fixture End-to-End Validation (Days 31–33)

**Context**

Phase 7 validates the full sqlcg pipeline against the public dbt Airbnb dataset (from
dbt Learn tutorials). The corpus is committed to the repository under
`tests/fixtures/airbnb/` — no external path dependency, no environment variable guard,
and all tests run in CI.

The Airbnb dataset has a known four-layer lineage structure (raw → staging → dimension/fact
→ mart) that enables concrete assertions about graph content, not just exit codes. All files
use Snowflake dialect and are syntactically valid sqlglot-parseable SQL.

**Fixture structure** (`tests/fixtures/airbnb/`):

Raw layer — source DDL:
- `raw_listings.sql` — `CREATE TABLE raw_listings (id, listing_url, name, room_type, minimum_nights, host_id, price, created_at, updated_at)`
- `raw_hosts.sql` — `CREATE TABLE raw_hosts (id, name, is_superhost, created_at, updated_at)`
- `raw_reviews.sql` — `CREATE TABLE raw_reviews (listing_id, date, reviewer_name, comments, sentiment)`

Staging layer — views selecting from raw:
- `src_listings.sql` — `CREATE OR REPLACE VIEW src_listings AS SELECT ... FROM raw_listings WHERE minimum_nights > 0`
- `src_hosts.sql` — `CREATE OR REPLACE VIEW src_hosts AS SELECT ... FROM raw_hosts`
- `src_reviews.sql` — `CREATE OR REPLACE VIEW src_reviews AS SELECT ... FROM raw_reviews WHERE sentiment != 'NOT VERIFIED'`

Dimension/fact layer — joins across staging:
- `dim_listings_cleansed.sql` — JOIN of `src_listings` + `src_hosts`
- `dim_hosts_cleansed.sql` — SELECT from `src_hosts` with transforms
- `fct_reviews.sql` — JOIN of `src_reviews` + `dim_listings_cleansed`

Mart layer:
- `mart_fullmoon_reviews.sql` — JOIN of `fct_reviews` with a date condition

This gives 3 lineage hops (raw → staging → dim/fact → mart) with known edge topology.

**Note on the existing `tests/e2e/test_dwh_e2e.py`**

The prior implementation of Phase 7 (`feat/phase-7-dwh-validation` branch) targeted the
private DWH at `/home/ignwrad/Projects/dwh`. That file must be replaced entirely with the
new Airbnb-based test file. The developer must replace `tests/e2e/test_dwh_e2e.py` with
`tests/e2e/test_airbnb_e2e.py` as described in Steps 7.2 and 7.3 below, and delete the
old file.

---

**Step 7.1 — Airbnb fixture creation**

- Files affected: `tests/fixtures/airbnb/` (10 new `.sql` files, no source code changes)
- Tasks:
  - Create the directory `tests/fixtures/airbnb/`
  - Author all 10 SQL files listed in the fixture structure above. Requirements:
    - Each file must be syntactically valid Snowflake SQL parseable by
      `sqlglot.parse(sql, dialect="snowflake")` without raising
    - Raw layer files use `CREATE TABLE` DDL with explicit column lists and Snowflake
      data types (e.g. `STRING`, `NUMBER`, `BOOLEAN`, `TIMESTAMP_NTZ`)
    - Staging layer files use `CREATE OR REPLACE VIEW name AS SELECT ...` selecting from
      the corresponding raw table, with at least one `WHERE` clause filter
    - `dim_listings_cleansed.sql` and `fct_reviews.sql` must contain an explicit `JOIN`
      clause referencing two upstream tables by name — this is what generates SELECTS_FROM
      lineage edges to both parents
    - `mart_fullmoon_reviews.sql` must reference `fct_reviews` as a source table
    - No Snowflake scripting blocks (`BEGIN ... END`) — clean SQL only; the fixture
      tests parser correctness, not scripting-block fallback
  - Commit the fixtures to the repository
- Acceptance:
  - `sqlglot.parse(open(f).read(), dialect="snowflake")` does not raise for all 10 files
  - The 10 files form at least 3 distinct layers (raw, staging, downstream) as required
    for the Step 7.2 lineage assertions

**Step 7.2 — Index and assert structural results**

- Files affected: `tests/e2e/test_airbnb_e2e.py` (new file; replaces `test_dwh_e2e.py`)
- Tasks:
  - Create a session-scoped pytest fixture `airbnb_indexed` that:
    1. Runs `sqlcg db init` against a `tmp_path_factory`-scoped KùzuDB path
    2. Runs `sqlcg index tests/fixtures/airbnb/ --dialect snowflake` against that path
    3. Captures the summary dict (`files_parsed`, `parse_errors`, `tables_found`,
       `lineage_edges_created`)
    4. Yields the tmp db path and summary for use by all tests in this module
  - No `pytest.mark.skipif` on any env var — the fixture is always available and runs in CI
  - Assertions must verify actual graph content, not just exit codes
- Acceptance criteria (each is a separate test method; class name must read as plain English):
  - `tables_found >= 9` — raw, staging, dimension/fact, and mart objects all indexed
  - `lineage_edges_created > 0` — staging views produce SELECTS_FROM edges to raw tables
  - `parse_errors == 0` — clean Snowflake SQL produces zero parse failures
  - `sqlcg find table raw_listings` (via subprocess) exits 0 and stdout contains
    `"raw_listings"`
  - `sqlcg find table dim_listings_cleansed` (via subprocess) exits 0 and stdout contains
    `"dim_listings_cleansed"`
  - `sqlcg find pattern "SELECT"` (via subprocess) exits 0 and stdout is non-empty
  - `sqlcg analyze upstream fct_reviews` (via subprocess) exits 0 and stdout is non-empty
    (has upstream lineage through the JOIN in `fct_reviews.sql`)

**Step 7.3 — MCP tools with real assertions**

- Files affected: `tests/e2e/test_airbnb_e2e.py` (continued), requires Phase 5 complete
- Tasks:
  - Reuse the `airbnb_indexed` session fixture from Step 7.2; call MCP tool functions
    directly via import (not via subprocess) so return types are verifiable
  - Call `find_table_usages("raw_listings")`: assert the result is non-empty — `src_listings`
    selects from `raw_listings` so at least one usage must exist
  - Call `list_dialects_and_repos`: assert the result includes dialect `"snowflake"`
  - Call `execute_cypher("MATCH (t:SqlTable) RETURN t.name LIMIT 10")`: assert the return
    value is a list and at least one entry has the name `"raw_listings"`
  - Call `trace_column_lineage` with a known column reference: assert it does not raise
    any exception (lineage may be partial — assert no exception only, not depth)
  - Start `mcp start` via subprocess: assert the process is alive after 2 seconds; terminate
    after check; skip (not fail) if the `sqlcg` entry point is not found in PATH
- Acceptance:
  - `find_table_usages("raw_listings")` returns at least one usage
  - `list_dialects_and_repos` result includes dialect `"snowflake"`
  - `execute_cypher(...)` returns a list containing a row where `t.name == "raw_listings"`
  - `trace_column_lineage` does not raise
  - `mcp start` subprocess is alive after 2 seconds (or test is skipped)

**Step 7.4 — Parse quality report**

- Files affected: `tests/e2e/test_airbnb_e2e.py`, `docs/AIRBNB_PARSE_REPORT.md`
- Tasks:
  - After indexing in the `airbnb_indexed` fixture, emit a structured parse quality report
    to `docs/AIRBNB_PARSE_REPORT.md` (generated, not hand-authored) with:
    - **Per-layer breakdown**: raw layer totals, staging layer totals, dim/fact layer totals,
      mart layer totals — identified by filename prefix (`raw_`, `src_`, `dim_`/`fct_`, `mart_`)
    - Total files, parsed, errored, success rate per layer
    - List of errored files with error category (parse_failed, exception)
    - Table/view count and lineage edge count
    - Distribution of `parsing_mode` values across all indexed queries (full-parse vs
      scripting-block fallback)
  - The report generation is a pytest fixture with `autouse=False` and a `--fixture-report`
    flag (renamed from `--dwh-report` — the flag now applies to any committed fixture corpus,
    not DWH-specific data)
- Acceptance:
  - `pytest tests/e2e/test_airbnb_e2e.py --fixture-report` generates
    `docs/AIRBNB_PARSE_REPORT.md`
  - Report contains all four required sections (per-layer breakdown, error list, table/edge
    counts, parsing_mode distribution)
  - For the clean Airbnb corpus, the error list section is empty and all `parsing_mode`
    values are `"full_parse"` or `"standard"` — no `"scripting_block_fallback"` entries

---

**Phase 7 pre-conditions**

- Phase 5 (MCP Server) must be complete before Step 7.3 can run
- Phase 4 db reset close-before-rmtree fix must be in place (Step 7.2 session fixture
  uses tmp KùzuDB paths that depend on correct `close()` ordering before `rmtree`)
- All 10 fixture files must parse without error under sqlglot Snowflake dialect before
  Step 7.2 tests are written — verify with a one-off parse call during fixture authoring
- The existing `tests/e2e/test_dwh_e2e.py` must be deleted and replaced with
  `tests/e2e/test_airbnb_e2e.py`; the old file must not remain in the working tree

**Phase 7 known risks**

| Risk | Mitigation |
|---|---|
| Snowflake dialect syntax in fixtures rejected by sqlglot | Validate each file with `sqlglot.parse(..., dialect="snowflake")` during Step 7.1; fix syntax before committing |
| `tables_found` assertion off-by-one if a staging view is not indexed as a SqlTable node | KùzuDB schema counts CREATE TABLE DDL-defined tables and CREATE VIEW nodes; lower bound is 9 to allow one possible view/table collapse |
| `fct_reviews` upstream chain too shallow for `analyze upstream` to return output | Ensure `fct_reviews.sql` JOINs `src_reviews` and `dim_listings_cleansed` by name, creating at least two SELECTS_FROM edges |
| KùzuDB single-writer lock conflicts if tests run in parallel | Session-scoped `airbnb_indexed` fixture creates one DB per test session; do not use `pytest -n` with e2e tests |
| `trace_column_lineage` returns empty without schema enrichment | Expected — assert no exception only, not non-empty lineage; column lineage degrades gracefully per finding 3.4 |


---

### Phase 8 — Metrics and Feedback Layer (Days 34–38)

**Context**

Phase 8 adds a lightweight observability layer that records tool call activity,
indexing run history, and LLM-submitted relevance feedback to a local SQLite database
at `~/.sqlcg/metrics.db`. No new runtime dependencies are introduced (stdlib `sqlite3`
only). All writes are append-only; reads are aggregates. Two new CLI commands expose
the data: `sqlcg gain` (numeric summary) and `sqlcg report` (FP/error cluster report
with a pre-filled GitHub issue URL).

**Phase 8 Non-Goals**

- Server-side telemetry or any outbound network call
- Automatic GitHub issue creation (report command generates the URL only)
- Retention policy or pruning of old rows (all rows are kept for the v1 lifetime)
- Metrics encryption or access control (local single-user tool)
- False negative (FN) feedback collection — LLMs cannot reliably self-report missed
  results; TP/FP only

---

**Step 8.1 — SQLite metrics store**

- Files affected:
  - `src/sqlcg/metrics/__init__.py` (new — empty init, makes package importable)
  - `src/sqlcg/metrics/store.py` (new — all SQLite read/write logic)

- Tasks:
  - `store.py` must be importable without a KùzuDB backend present (no import of
    `kuzu`, `graph_db`, or any `sqlcg.core` symbol at module level)
  - Define `METRICS_DB_PATH: Path` constant:
    `Path.home() / ".sqlcg" / "metrics.db"` — consistent with the KùzuDB path
    convention in `get_db_path()` from `config.py`
  - Define `MetricsStore` class:
    ```python
    class MetricsStore:
        def __init__(self, db_path: Path = METRICS_DB_PATH) -> None: ...
        def init_schema(self) -> None: ...  # CREATE TABLE IF NOT EXISTS
        def record_tool_call(
            self,
            tool_name: str,
            timestamp: float,       # time.time()
            duration_ms: float,     # wall-clock, milliseconds
            row_count: int,
            repo_path: str,
        ) -> None: ...
        def record_index_run(
            self,
            timestamp: float,
            repo_path: str,
            duration_ms: float,
            files_parsed: int,
            parse_errors: int,
            lineage_edges_created: int,
        ) -> None: ...
        # Derived metrics (computed properties)
        def get_parse_success_rate(self) -> float: ...  # parse_errors / files_parsed
        def close(self) -> None: ...
    ```
  - DDL (all tables created with `CREATE TABLE IF NOT EXISTS`):
    ```sql
    CREATE TABLE IF NOT EXISTS tool_calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name   TEXT    NOT NULL,
        timestamp   REAL    NOT NULL,   -- Unix epoch float
        duration_ms REAL    NOT NULL,
        row_count   INTEGER NOT NULL DEFAULT 0,
        repo_path   TEXT    NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS index_runs (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp            REAL    NOT NULL,
        repo_path            TEXT    NOT NULL,
        duration_ms          REAL    NOT NULL,
        files_parsed         INTEGER NOT NULL,
        parse_errors         INTEGER NOT NULL,
        lineage_edges_created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS feedback (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name  TEXT NOT NULL,
        query      TEXT NOT NULL,
        label      TEXT NOT NULL CHECK(label IN ('TP', 'FP')),
        note       TEXT NOT NULL DEFAULT '',
        timestamp  REAL NOT NULL
    );
    ```
  - `init_schema()` creates the database file (including parent directory) and all three
    tables. It is called once at process startup; calling it multiple times is safe
    (`IF NOT EXISTS` guards prevent duplication).
  - All write methods must be wrapped in `try/except Exception`: log a WARNING via
    `sqlcg.utils.logging.getLogger` and return without raising. A metrics write failure
    must never propagate to the caller or crash a tool call.
  - `MetricsStore.__enter__` / `__exit__` for use as a context manager (calls `close()`
    on exit).
  - Opt-out: check `os.environ.get("SQLCG_METRICS", "1") == "0"` at the top of each
    write method; return immediately (no write, no error) when metrics are disabled.
    Read methods are not guarded — they should always return data if the database exists.
  - `db_path.parent.mkdir(parents=True, exist_ok=True)` before connecting (consistent
    with how `get_db_path()` creates its parent in `index_cmd`).
  - Use `sqlite3.connect(str(db_path))` — do not use `pathlib.Path` directly with
    `sqlite3.connect` (stdlib behaviour varies across Python minor versions).

- Acceptance:
  - `from sqlcg.metrics.store import MetricsStore` succeeds in an environment where
    `kuzu` is not installed
  - `MetricsStore().init_schema()` is idempotent (call twice, no error, tables exist)
  - `record_tool_call(...)` with `SQLCG_METRICS=0` set is a no-op (no row written)
  - `record_tool_call(...)` exception is caught and logged as WARNING; caller does not
    see the exception
  - `record_index_run(...)` appends a row; reading it back returns the stored values

---

**Step 8.2 — Instrument MCP tools and the indexer**

- Files affected:
  - `src/sqlcg/server/tools.py` (instrument every `@mcp.tool()` call)
  - `src/sqlcg/indexer/indexer.py` (instrument `index_repo`)

- Tasks (tools.py):
  - Import `MetricsStore` at the top of `tools.py` (lazy import inside each decorator
    is acceptable if it avoids a circular import, but module-level import is preferred).
  - Define a module-level `_metrics: MetricsStore | None = None`. Initialise it inside
    `init_backend()` immediately after `_backend` is set:
    ```python
    _metrics = MetricsStore()
    _metrics.init_schema()
    ```
    Add a corresponding `_metrics.close()` call in `shutdown_backend()`.
  - For each of the eight `@mcp.tool()` functions (`index_repo`, `trace_column_lineage`,
    `find_table_usages`, `get_downstream_dependencies`, `get_upstream_dependencies`,
    `search_sql_pattern`, `list_dialects_and_repos`, `execute_cypher`), wrap the body
    in a timing block:
    ```python
    import time
    _t0 = time.monotonic()
    try:
        result = <original body>
    finally:
        if _metrics is not None:
            _metrics.record_tool_call(
                tool_name="<name>",
                timestamp=time.time(),
                duration_ms=(time.monotonic() - _t0) * 1000,
                row_count=<len(result) if iterable else 1>,
                repo_path=<repo_path arg or "" if not applicable>,
            )
    return result
    ```
  - `row_count` extraction rules per tool:
    - `index_repo`: 0 (summary dict, not a row collection)
    - `trace_column_lineage`: `len(result.lineage)`
    - `find_table_usages`: `len(result.usages)`
    - `get_downstream_dependencies` / `get_upstream_dependencies`: `len(result.nodes)`
    - `search_sql_pattern`: `len(result.matches)`
    - `list_dialects_and_repos`: `len(result.repos)`
    - `execute_cypher`: `len(result)` (returns `list[dict]`)
  - `repo_path` extraction: use the `repo_path` argument where the tool accepts one
    (only `index_repo`); pass `""` for all query tools.
  - The timing block must not change any existing exception propagation — exceptions
    from the tool body must still propagate after the metrics write.

- Tasks (indexer.py):
  - Import `MetricsStore` at the top of `indexer.py`. Instantiate it in `index_repo`:
    ```python
    _ms = MetricsStore()
    _ms.init_schema()
    t0 = time.monotonic()
    ```
    After the final return dict is assembled, before the `return`:
    ```python
    _ms.record_index_run(
        timestamp=time.time(),
        repo_path=str(path),
        duration_ms=(time.monotonic() - t0) * 1000,
        files_parsed=result["files_parsed"],
        parse_errors=result["parse_errors"],
        lineage_edges_created=result["lineage_edges_created"],
    )
    _ms.close()
    ```
  - The `MetricsStore` instance in `index_repo` is local (not module-level) because
    `index_repo` can be called concurrently (watcher) and SQLite is not safe to share
    across threads without connection-per-thread. Each call opens and closes its own
    connection.
  - Wrap the `record_index_run` + `close` block in `try/finally` so `close()` is always
    called even if the metrics write silently fails.

- Acceptance:
  - After `sqlcg index tests/fixtures/synthetic/ --dialect ansi`, a row exists in
    `~/.sqlcg/metrics.db` table `index_runs`
  - After calling any MCP tool, a row exists in `tool_calls`
  - With `SQLCG_METRICS=0`, neither table receives a row
  - Raising an exception inside a tool (e.g. `NotIndexedError`) still results in a row
    in `tool_calls` (metrics write happens in `finally`)

---

**Step 8.3 — `submit_feedback` MCP tool**

- Files affected:
  - `src/sqlcg/server/tools.py` (add new `@mcp.tool()`)

- Tasks:
  - Add `submit_feedback` as the ninth MCP tool:
    ```python
    @mcp.tool()
    def submit_feedback(
        tool_name: str,
        query: str,
        label: str,
        note: str = "",
    ) -> dict:
        """Rate a tool result as useful (TP) or not useful (FP).
        Call after evaluating any tool result.

        Args:
            tool_name: Name of the tool whose result is being rated.
            query: The query or input that produced the result.
            label: "TP" (result was correct) or "FP" (result was wrong).
            note: Optional explanation (max 500 chars).

        Returns:
            {"status": "recorded"} on success, {"status": "skipped"} if metrics off.

        Raises:
            ValueError: If label is not "TP" or "FP".
        """
    ```
  - Validate `label in {"TP", "FP"}` — raise `ValueError` with a clear message if not.
  - Validate `len(note) <= 500` — silently truncate to 500 chars (do not raise; the
    LLM may produce long notes).
  - Write via `_metrics.record_feedback(tool_name, query, label, note, time.time())`
    (add `record_feedback` method to `MetricsStore`).
  - Return `{"status": "recorded"}` on success, `{"status": "skipped"}` if
    `SQLCG_METRICS=0` is set (the `record_feedback` no-op returns without writing;
    the tool still needs to communicate that skip to the caller).
  - Docstring system prompt hint: "Call after evaluating any tool result." — this is the
    nudge (10 words) that encourages the LLM to call it without requiring a full system
    prompt change.
  - The tool must NOT call `_assert_indexed()` — feedback can be submitted even on an
    unindexed graph (e.g. to record a FP from a cached result).
  - `note` truncation and label validation happen before the metrics write attempt.

- `MetricsStore.record_feedback` addition to `store.py`:
  ```python
  def record_feedback(
      self,
      tool_name: str,
      query: str,
      label: str,         # "TP" or "FP" — caller validates
      note: str,
      timestamp: float,
  ) -> None: ...
  ```
  Uses the same try/except + opt-out pattern as other write methods.

- Acceptance:
  - `submit_feedback("trace_column_lineage", "orders.amount", "TP")` returns
    `{"status": "recorded"}` and a row exists in the `feedback` table
  - `submit_feedback("trace_column_lineage", "orders.amount", "FN")` raises `ValueError`
  - `submit_feedback(..., note="x" * 600)` stores exactly 500 characters, no exception
  - With `SQLCG_METRICS=0`, returns `{"status": "skipped"}` and no row is written
  - The tool is visible in the FastMCP tool listing alongside the eight existing tools

---

**Step 8.4 — `sqlcg gain` CLI command**

- Files affected:
  - `src/sqlcg/cli/commands/gain.py` (new)
  - `src/sqlcg/cli/main.py` (register command)

- Tasks:
  - `gain.py` defines a single Typer command `gain_cmd` registered as `sqlcg gain`.
  - `gain_cmd` signature:
    ```python
    def gain_cmd(
        json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    ) -> None:
        """Show metrics summary: tool call counts, parse quality trend, TP rate."""
    ```
  - Open `MetricsStore(METRICS_DB_PATH)` — if the file does not exist, print a message
    "No metrics collected yet. Run sqlcg index or use an MCP tool first." and exit 0.
    Do not create the file just by running `sqlcg gain`.
  - Compute and display (or serialise) the following sections. All queries use SQLite
    aggregate functions — no in-Python loops over raw rows:

    **Section A — Total MCP tool calls**
    ```sql
    SELECT COUNT(*) AS total FROM tool_calls;
    SELECT COUNT(*) AS last_7d FROM tool_calls
    WHERE timestamp > strftime('%s', 'now') - 7*86400;
    ```
    Display: "Total tool calls: {total} (last 7 days: {last_7d})"

    **Section B — Parse success rate trend (last 5 index runs)**
    ```sql
    SELECT timestamp, repo_path,
           CAST(files_parsed - parse_errors AS REAL) / NULLIF(files_parsed, 0) AS success_rate
    FROM index_runs
    ORDER BY timestamp DESC
    LIMIT 5;
    ```
    Display: one row per run, formatted as
    `  {date} {repo_path}: {success_rate:.0%}` (newest first).
    If fewer than 1 run: "No indexing runs recorded yet."
    No sparkline required — plain percentages are acceptable. A simple ASCII sparkline
    (characters `▁▂▃▄▅▆▇█` proportional to the rate) is optional but not required for
    acceptance.

    **Section C — TP rate from feedback**
    ```sql
    SELECT
        SUM(CASE WHEN label = 'TP' THEN 1 ELSE 0 END) AS tp,
        COUNT(*) AS total
    FROM feedback;
    ```
    Display: "Feedback TP rate: {tp}/{total} ({rate:.0%})" only if `total >= 5`.
    If `total < 5`: "Feedback: {total} sample(s) — need 5 to show TP rate."

    **Section D — Top 3 most-called tools**
    ```sql
    SELECT tool_name, COUNT(*) AS calls
    FROM tool_calls
    GROUP BY tool_name
    ORDER BY calls DESC
    LIMIT 3;
    ```
    Display: numbered list `  1. {tool_name}: {calls} calls`.
    If no calls: "No tool calls recorded yet."

  - `--json` output: return a single JSON object with keys `total_calls`, `last_7d_calls`,
    `index_runs` (list of `{timestamp, repo_path, success_rate}`), `feedback_tp`,
    `feedback_total`, `top_tools` (list of `{tool_name, calls}`). Use `json.dumps(...,
    indent=2)` and print to stdout.
  - Register in `main.py`:
    ```python
    from sqlcg.cli.commands import gain
    app.command("gain")(gain.gain_cmd)
    ```

- Acceptance:
  - `sqlcg gain` with no metrics database exits 0 with the "No metrics" message
  - `sqlcg gain` after `sqlcg index` and one MCP tool call shows non-zero values in
    Sections A and B
  - `sqlcg gain --json` outputs valid JSON with all required keys
  - TP rate section is hidden when fewer than 5 feedback samples exist
  - `sqlcg gain` exits 0 in all cases (metrics absence is not an error)

---

**Step 8.5 — `sqlcg report` CLI command**

- Files affected:
  - `src/sqlcg/cli/commands/report.py` (new)
  - `src/sqlcg/cli/main.py` (register command)

- Tasks:
  - `report.py` defines a single Typer command `report_cmd` registered as `sqlcg report`.
  - `report_cmd` signature:
    ```python
    def report_cmd(
        stdout: bool = typer.Option(False, "--stdout", help="Print to stdout instead of file"),
        output: Path = typer.Option(
            Path("docs/METRICS_REPORT.md"),
            "--output", "-o",
            help="Output file path",
        ),
    ) -> None:
        """Generate a metrics report with FP clusters and parse error patterns."""
    ```
  - If metrics database does not exist: print "No metrics collected yet." and exit 0.
  - Compute the following sections via SQL queries:

    **Section 1 — FP clusters** (files or query patterns with FP rate > 50%, min 3 samples)
    ```sql
    SELECT query,
           SUM(CASE WHEN label = 'FP' THEN 1 ELSE 0 END) AS fp_count,
           COUNT(*) AS total,
           CAST(SUM(CASE WHEN label = 'FP' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) AS fp_rate
    FROM feedback
    GROUP BY query
    HAVING total >= 3 AND fp_rate > 0.5
    ORDER BY fp_rate DESC;
    ```
    Render as a Markdown table: `| Query | FP Count | Total | FP Rate |`.
    If no qualifying rows: "No FP clusters found (need ≥3 samples per query pattern)."

    **Section 2 — Parse error clusters** (files that errored across multiple index runs)
    ```sql
    SELECT repo_path, COUNT(*) AS run_count,
           SUM(parse_errors) AS total_errors,
           CAST(SUM(parse_errors) AS REAL) / NULLIF(SUM(files_parsed), 0) AS error_rate
    FROM index_runs
    GROUP BY repo_path
    HAVING run_count >= 2 AND total_errors > 0
    ORDER BY total_errors DESC;
    ```
    Render as a Markdown table: `| Repo Path | Runs | Total Errors | Error Rate |`.
    If no qualifying rows: "No parse error clusters found."

    **Section 3 — Pre-filled GitHub issue URL**
    - Compute a `report_hash`: `hashlib.md5(report_content.encode()).hexdigest()[:8]`
      (used in the issue title to identify the specific report; stdlib `hashlib` only).
    - Issue title: `[sqlcg metrics] FP clusters report {report_hash}`
    - Issue body (URL-encoded): the Markdown content of Sections 1 and 2, truncated to
      2000 characters if longer (GitHub URL length limit is 8192; 2000 chars is safe).
    - Pre-filled URL format:
      ```
      https://github.com/Warhorze/sql-code-graph/issues/new?title={title}&body={body}
      ```
      Use `urllib.parse.quote` (stdlib) for URL encoding. No requests/httpx import.
    - Render this URL under a "## File an Issue" heading in the report.
    - The URL is copy-paste ready — it must be a single line with no line breaks inside
      the URL itself.

  - Output:
    - With `--stdout`: print the Markdown to stdout.
    - Without `--stdout`: write to `--output` path (default `docs/METRICS_REPORT.md`),
      creating parent directories as needed. Print "Report written to {output}" to
      the console (stderr-safe via `console.print()`).
  - Register in `main.py`:
    ```python
    from sqlcg.cli.commands import report
    app.command("report")(report.report_cmd)
    ```

- Acceptance:
  - `sqlcg report --stdout` with no metrics database exits 0 with the "No metrics" message
  - `sqlcg report --stdout` with seed data produces valid Markdown with all three sections
  - FP cluster table appears only when ≥3 samples with FP rate >50% exist
  - Parse error cluster table appears only when ≥2 runs with parse errors exist
  - GitHub issue URL is a valid URL (parseable by `urllib.parse.urlparse`) with no
    newlines embedded
  - `report_hash` changes when the report content changes

---

**Step 8.6 — Unit tests**

- Files affected:
  - `tests/unit/test_metrics.py` (new — all Phase 8 unit tests)

- Tasks:
  - All tests use a `tmp_path` fixture (pytest built-in) to write to a throwaway SQLite
    file, never to `~/.sqlcg/metrics.db`.
  - Test organisation — test classes per concern:

    **`TestMetricsStore`** — store.py unit tests:
    - `test_init_schema_is_idempotent`: call `init_schema()` twice, assert no error and
      three tables exist (query `sqlite_master`)
    - `test_record_tool_call_writes_row`: call `record_tool_call(...)`, assert one row
      in `tool_calls` with correct field values
    - `test_record_index_run_writes_row`: call `record_index_run(...)`, assert row in
      `index_runs`
    - `test_record_feedback_writes_row`: call `record_feedback(..., label="TP")`, assert row
    - `test_write_is_noop_when_disabled`: set `os.environ["SQLCG_METRICS"] = "0"`, call
      `record_tool_call(...)`, assert table has 0 rows
    - `test_write_does_not_raise_on_sqlite_error`: monkeypatch `sqlite3.Connection.execute`
      to raise `sqlite3.OperationalError`; assert `record_tool_call(...)` returns without
      raising and that a WARNING was logged
    - `test_context_manager_closes_connection`: use `with MetricsStore(...) as ms:` block;
      after the block, assert `ms._conn` is closed (check `ms._conn` is None or
      `_closed` attribute depending on implementation)

    **`TestSubmitFeedbackTool`** — submit_feedback MCP tool unit tests:
    - `test_submit_feedback_tp_recorded`: call with `label="TP"`, assert return
      `{"status": "recorded"}` and row in `feedback` table
    - `test_submit_feedback_invalid_label_raises`: call with `label="FN"`, assert
      `ValueError` raised
    - `test_submit_feedback_note_truncated`: call with `note="x" * 600`, assert stored
      note is exactly 500 characters
    - `test_submit_feedback_skipped_when_metrics_off`: set `SQLCG_METRICS=0`, assert
      return is `{"status": "skipped"}` and no row written

    **`TestGainCommand`** — gain.py unit tests (use `typer.testing.CliRunner`):
    - `test_gain_no_database_exits_0`: run `sqlcg gain` with no metrics.db, assert
      exit 0 and "No metrics" in output
    - `test_gain_shows_total_calls`: seed `tool_calls` with 3 rows, assert output
      contains "Total tool calls: 3"
    - `test_gain_hides_tp_rate_below_5_samples`: seed `feedback` with 3 rows, assert
      "need 5" appears in output
    - `test_gain_shows_tp_rate_at_5_samples`: seed `feedback` with 5 TP rows, assert
      TP rate section is shown with "5/5"
    - `test_gain_json_flag_produces_valid_json`: run `sqlcg gain --json`, parse output
      as JSON, assert all required keys present

    **`TestReportCommand`** — report.py unit tests (use `typer.testing.CliRunner`):
    - `test_report_no_database_exits_0`: run `sqlcg report --stdout` with no metrics.db,
      assert exit 0
    - `test_report_fp_cluster_appears_in_output`: seed `feedback` with 4 FP rows for the
      same query (FP rate = 1.0 > 0.5, count ≥ 3), assert the query appears in
      `--stdout` output
    - `test_report_fp_cluster_hidden_when_insufficient_samples`: seed 2 FP rows for a
      query (count < 3), assert the query does NOT appear in output
    - `test_report_parse_error_cluster_appears`: seed `index_runs` with 2 runs for the
      same repo, both with parse_errors > 0, assert the repo path appears in output
    - `test_report_github_url_is_valid`: seed any data, run `--stdout`, extract the
      GitHub URL line, assert `urllib.parse.urlparse(url).scheme == "https"`
    - `test_report_writes_to_file`: run without `--stdout`, assert the output file is
      created and non-empty

- Acceptance:
  - `pytest tests/unit/test_metrics.py` passes with 0 failures
  - All tests use `tmp_path`; no test touches `~/.sqlcg/`
  - Coverage of `store.py`, `gain.py`, `report.py`, and the `submit_feedback` function
    in `tools.py` reaches ≥ 80% (measured; not enforced as a gate, but recorded)

---

## Phase 8 Acceptance Criteria

- [ ] `from sqlcg.metrics.store import MetricsStore` succeeds without `kuzu` installed
- [ ] `MetricsStore.init_schema()` is idempotent
- [ ] With `SQLCG_METRICS=0`, no rows are written to any metrics table
- [ ] A failed metrics write logs WARNING and does not propagate to the caller
- [ ] After `sqlcg index`, a row exists in `index_runs` with correct `files_parsed` value
- [ ] After calling any MCP tool, a row exists in `tool_calls` with correct `tool_name`
- [ ] `submit_feedback("t", "q", "FN")` raises `ValueError`
- [ ] `submit_feedback("t", "q", "TP", note="x"*600)` stores 500 chars, returns `{"status": "recorded"}`
- [ ] `sqlcg gain` exits 0 with no metrics database present
- [ ] `sqlcg gain --json` outputs valid JSON with keys: `total_calls`, `last_7d_calls`, `index_runs`, `feedback_tp`, `feedback_total`, `top_tools`
- [ ] `sqlcg report --stdout` exits 0 with no metrics database present
- [ ] FP cluster table is hidden when no query has ≥3 samples with FP rate > 50%
- [ ] GitHub issue URL in `sqlcg report` output is a valid HTTPS URL parseable by `urllib.parse.urlparse`
- [ ] `pytest tests/unit/test_metrics.py` passes with 0 failures (minimum 20 test cases)

---

## Phase 8 Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| SQLite write failure on low-disk-space or permission error | Low | Low | try/except wraps every write; WARNING logged; tool call is unaffected |
| `metrics.db` grows unbounded on high-use installations | Low | Low | v1 has no pruning; document that users can delete the file at any time; v2 can add retention policy |
| LLM calls `submit_feedback` with FN label | Medium | None | `ValueError` raised before any write; no FN rows can exist in the database |
| Concurrent MCP tool calls race on `_metrics` singleton | Low | Low | SQLite's serialised write mode (`check_same_thread=True`) + WAL journal mode prevents corruption; each watcher job uses its own local `MetricsStore` instance (no singleton in indexer path) |
| GitHub issue URL exceeds browser limit | Low | Low | Body is truncated to 2000 chars; title is fixed-length; total URL is well within 8192 char limit |
| `gain.py` or `report.py` SQL queries time out on large metrics.db | Very Low | Low | All queries use indexed columns (timestamp, label, tool_name); no full-table scans; no index DDL needed for v1 volumes |
| `submit_feedback` docstring hint is ignored by LLM | Medium | Low | The tool is always available and surfaced in the tool listing; LLM may call it if Claude's tool-selection improves; no correctness risk if it is never called |

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
