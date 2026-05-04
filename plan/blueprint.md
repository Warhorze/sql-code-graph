# SQL-CGC: Complete Blueprint

Cross-file SQL Lineage as an MCP Graph — v1.2 (May 2026)

Build sql-code-graph (sqlcg) as a sqlglot + KùzuDB twin of CodeGraphContext, exposing column-level lineage to Claude over MCP. This document is the single source of truth, integrating the original architecture plan, the Snowflake dialect analysis, the upstream issue tracker, the package combination findings, and the v1.2 architecture review.

## Changelog: v1.1 → v1.2

- GraphBackend ABC defined explicitly — no longer a stub reference; full interface in §3.1
- LineageEdge replaces raw tuple — typed edge with transform, confidence, query_id; prevents breaking mid-project model change
- SchemaResolver.as_dict() cached — lru_cache + invalidation; was O(n) per column extraction call
- _fallback_table_scan added — build_scope returning None now falls back to direct exp.Table walk with structured logging rather than silent []
- execute_cypher hardened — mutation guard + auto-LIMIT; flagged as optional for v1
- Incremental indexing design added — explicit invalidation graph in §4.5; sqlcg watch semantics clarified
- pyproject.toml scripts entry added — sqlcg CLI entry point was missing
- stdout redirect guard added — enforces stderr-only at MCP server startup
- dbt-artifacts package added — replaces hand-rolled manifest parser
- sqlmesh test suite added — free cross-dialect fixture source for benchmarks
- Week-1 priority reordering — GraphBackend ABC and data model freeze before parser implementation

## 1. Why This Is a Real Gap

The MCP ecosystem has three adjacent buckets that all stop short:

- j4c0bs/mcp-server-sql-analyzer wraps sqlglot as stateless tools (lint, transpile, get_table_references) — no graph, no persistence, no cross-query lineage.
- DataHub MCP does column-level lineage but requires a running DataHub instance plus an ingestion pipeline.
- CodeGraphContext lists SQL among 14 languages but treats it via tree-sitter as syntactic structure only — no JOIN/CTE/lineage semantics, no MappingSchema-based SELECT * resolution.
- kuzudb/kuzu-mcp-server is a generic Cypher client with no SQL awareness at all.

The unfilled niche: a self-contained MCP server that parses a corpus of .sql files with sqlglot, persists tables, columns, queries, and column-lineage edges to embedded KùzuDB, and exposes lineage queries as MCP tools. The closest architectural template is CGC; the closest semantic template is DataHub's SqlParsingAggregator. Combining them is the design.

## 2. Project Structure

Mirrors CGC's src/ -layout exactly, with three deliberate additions: a dedicated lineage/ subpackage (lineage is the entire value proposition), a benchmarks/ layer, and a dbt_adapter.py.

```
sql-code-graph/
├── src/sqlcg/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli/
│   │   ├── main.py                    # Typer app, entry point `sqlcg`
│   │   ├── commands/
│   │   │   ├── index.py               # sqlcg index <path>
│   │   │   ├── find.py                # sqlcg find pattern "..."
│   │   │   ├── analyze.py             # sqlcg analyze {lineage,upstream,downstream,impact}
│   │   │   ├── watch.py               # sqlcg watch <path>
│   │   │   ├── mcp.py                 # sqlcg mcp {setup,start}
│   │   │   └── db.py                  # sqlcg db {init,reset,info}
│   │   └── wizard.py                  # InquirerPy first-run wizard
│   ├── core/
│   │   ├── graph_db.py                # GraphBackend ABC — defined in §3.1
│   │   ├── kuzu_backend.py            # Primary backend
│   │   ├── neo4j_backend.py           # Supported fallback from day one
│   │   ├── schema.py                  # Node/Rel DDL + label constants
│   │   ├── jobs.py                    # Background indexing job manager
│   │   └── config.py                  # .env + dotenv loading
│   ├── parsers/                       # CGC's `languages/` analog
│   │   ├── base.py                    # SqlParser ABC
│   │   ├── registry.py                # dialect → parser class map
│   │   ├── ansi_parser.py             # default sqlglot parser (no dialect)
│   │   ├── snowflake_parser.py        # dialect-specific overrides (see §8)
│   │   ├── bigquery_parser.py
│   │   ├── postgres_parser.py
│   │   └── tsql_parser.py
│   ├── lineage/
│   │   ├── extractor.py               # wraps sqlglot.lineage.lineage()
│   │   ├── scope_walker.py            # wraps build_scope/traverse_scope
│   │   ├── schema_resolver.py         # MappingSchema builder; CREATE-table sniffer
│   │   └── aggregator.py              # cross-file resolver (LineageX-style LIFO)
│   ├── indexer/
│   │   ├── walker.py                  # repo walk + .sqlcgignore via pathspec
│   │   ├── indexer.py                 # orchestrates parse → graph upsert
│   │   ├── watcher.py                 # watchdog observer for `sqlcg watch`
│   │   └── dbt_adapter.py             # reads dbt manifest via dbt-artifacts
│   ├── server/
│   │   ├── server.py                  # FastMCP app; entry: `sqlcg mcp start`
│   │   └── tools.py                   # @mcp.tool() definitions
│   └── utils/
│       ├── logging.py                 # stderr-only logger (stdio MCP safety)
│       ├── ignore.py                  # .sqlcgignore matcher
│       └── hashing.py                 # SQL fingerprinting for change detection
├── tests/
│   ├── unit/
│   │   ├── test_parser.py
│   │   ├── test_lineage_extractor.py
│   │   ├── test_scope_walker.py
│   │   ├── test_schema_resolver.py
│   │   └── test_kuzu_backend.py
│   ├── integration/
│   │   ├── test_indexer_to_graph.py
│   │   ├── test_cross_file_lineage.py
│   │   └── test_dialect_matrix.py
│   ├── e2e/
│   │   ├── test_cli_index.py
│   │   ├── test_mcp_tools.py
│   │   └── test_watch.py
│   ├── benchmarks/
│   │   ├── tpch/                      # TPC-H 24 queries
│   │   ├── tpcds/                     # TPC-DS subset
│   │   ├── sqlmesh/                   # sqlmesh open test suite (cross-dialect; free fixtures)
│   │   ├── golden_corpus/             # hand-curated 100-500 queries + expected edges
│   │   └── adversarial/               # 200-JOIN, 500-UNION stressors
│   └── fixtures/
│       ├── jaffle_shop/               # public dbt project
│       └── synthetic/                 # multi-file dependency chains
├── docs/
│   ├── BENCHMARKS.md
│   ├── DIALECTS.md
│   ├── MCP_TOOLS.md
│   └── CLI_Commands.md
├── .github/workflows/{test.yml,e2e-tests.yml,benchmark.yml}
├── .env.example
├── .sqlcgignore
├── pyproject.toml
├── README.md
└── LICENSE                            # MIT
```

## 3. Phase 1 — Core Data Model, Backend ABC, and Graph Builder (2–3 weeks)

Revised priority order for week 1:

1. Define GraphBackend ABC and both backends (Kuzu + Neo4j stub) — protects against the biggest project risk
2. Freeze all data model types including LineageEdge — prevents breaking mid-project refactors
3. SqlParser base class with fallback table scan
4. SchemaResolver with caching
5. Only then: wire up single-file parsing tests

Goal: parse a single .sql file and emit graph nodes/edges in memory. No persistence yet, no cross-file resolution.

Deliverable: `parse_file(path) -> ParsedFile`.

Start here on day one:

```bash
uv init sql-code-graph
uv add "sqlglot[rs]" kuzu typer pydantic pytest
```

### 3.1 GraphBackend ABC — define this first

This is the most important abstraction in the project. KùzuDB is a real risk (Kuzu Inc archival concerns); all DB access must go through this interface from day one. Neo4j is a supported fallback, not a future footnote.

```python
# src/sqlcg/core/graph_db.py
from abc import ABC, abstractmethod
from typing import Any
from contextlib import contextmanager


class GraphBackend(ABC):
    """All database access goes through this interface.
    Kuzu is primary; Neo4j is a supported fallback.
    Every Cypher query written must run on both backends."""

    @abstractmethod
    def upsert_node(self, label: str, key: str, props: dict) -> None:
        """Insert or update a node. `key` is the PRIMARY KEY value."""
        ...

    @abstractmethod
    def upsert_edge(
        self,
        rel: str,
        src_label: str,
        src_key: str,
        dst_label: str,
        dst_key: str,
        props: dict | None = None,
    ) -> None:
        """Insert or update a relationship between two nodes."""
        ...

    @abstractmethod
    def run_read(self, query: str, params: dict | None = None) -> list[dict]:
        """Execute a read-only Cypher query and return results as dicts."""
        ...

    @abstractmethod
    def delete_nodes_for_file(self, file_path: str) -> None:
        """Remove all Query nodes (and their edges) associated with a file.
        Used during incremental re-indexing."""
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @contextmanager
    def transaction(self):
        """Optional transactional context. Backends that don't support
        transactions yield self and are a no-op."""
        yield self
```

### 3.2 Core data model — freeze before writing any parser code

The LineageEdge typed dataclass replaces the fragile `list[tuple[ColumnRef, ColumnRef]]`. Adding `transform`, `confidence`, and `query_id` now prevents a breaking model change mid-project.

```python
# src/sqlcg/parsers/base.py
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope
from sqlglot.lineage import lineage as sg_lineage


@dataclass(frozen=True)
class TableRef:
    catalog: str | None
    db: str | None
    name: str
    alias: str | None = None

    @property
    def qualified(self) -> str:
        return ".".join(p for p in (self.catalog, self.db, self.name) if p)


@dataclass(frozen=True)
class ColumnRef:
    table: TableRef
    name: str


@dataclass(frozen=True)
class LineageEdge:
    """A typed, directed column-lineage edge. Replaces raw tuple[ColumnRef, ColumnRef].
    
    Having transform, confidence, and query_id here avoids a breaking data model
    change when these fields are needed for graph persistence (and they will be)."""
    src: ColumnRef
    dst: ColumnRef
    transform: str = "UNKNOWN"
    # IDENTITY | AGGREGATE | CASE | EXPR | FLATTEN | VARIANT_ACCESS | UNKNOWN
    confidence: float = 1.0
    query_id: str | None = None


@dataclass
class QueryNode:
    file: Path
    statement_index: int
    sql: str
    kind: str  # SELECT | INSERT | UPDATE | DELETE | MERGE | CREATE_TABLE | CREATE_VIEW
               # | CTAS | COPY_INTO | CREATE_PROCEDURE | UNKNOWN
    target: TableRef | None
    sources: list[TableRef]
    ctes: list[str]
    column_lineage: list[LineageEdge]  # ← typed edge, not tuple
    parse_failed: bool = False
    confidence: float = 1.0
    parsing_mode: str = "normal"
    # normal | command_fallback | schema_required


@dataclass
class ParsedFile:
    path: Path
    dialect: str | None
    statements: list[QueryNode]
    defined_tables: list[TableRef]
    referenced_tables: list[TableRef]
    errors: list[str] = field(default_factory=list)
```

### 3.3 SqlParser base class — with fallback table scan

`build_scope` returns `None` on statements sqlglot can't fully resolve. The previous code silently returned `[]` — these silent failures are exactly where bugs hide. The fallback walker + structured logging surfaces them instead.

```python
class SqlParser(ABC):
    """Base parser. Subclass per-dialect to override syntax quirks."""
    DIALECT: str | None = None

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver
        self._log = logging.getLogger(f"sqlcg.parser.{self.DIALECT or 'ansi'}")

    def parse_file(self, path: Path, sql: str) -> ParsedFile:
        statements = sqlglot.parse(sql, dialect=self.DIALECT)
        out = ParsedFile(path=path, dialect=self.DIALECT, statements=[],
                         defined_tables=[], referenced_tables=[])
        for i, ast in enumerate(statements):
            if ast is None:
                continue
            try:
                qn = self._parse_statement(ast, path, i)
                out.statements.append(qn)
                if qn.target and qn.kind in ("CREATE_TABLE", "CREATE_VIEW", "CTAS"):
                    out.defined_tables.append(qn.target)
                out.referenced_tables.extend(qn.sources)
            except Exception as e:
                out.errors.append(f"stmt {i}: {e}")
        return out

    def _parse_statement(self, ast: exp.Expression, path: Path, idx: int) -> QueryNode:
        kind, target, sources = self._classify(ast)
        ctes = [c.alias for c in ast.find_all(exp.CTE)]
        col_edges = self._extract_column_lineage(ast)
        is_command = isinstance(ast, exp.Command)
        return QueryNode(
            file=path, statement_index=idx, sql=ast.sql(dialect=self.DIALECT),
            kind=kind, target=target, sources=sources, ctes=ctes,
            column_lineage=col_edges, parse_failed=is_command,
            confidence=0.5 if is_command else 1.0,
            parsing_mode="command_fallback" if is_command else "normal",
        )

    def _classify(self, ast):
        if isinstance(ast, exp.Insert):
            return "INSERT", self._table_of(ast.this), self._real_tables(ast.expression)
        if isinstance(ast, exp.Update):
            return "UPDATE", self._table_of(ast.this), self._real_tables(ast)
        if isinstance(ast, exp.Delete):
            return "DELETE", self._table_of(ast.this), self._real_tables(ast)
        if isinstance(ast, exp.Merge):
            return "MERGE", self._table_of(ast.this), self._real_tables(ast.args.get("using"))
        if isinstance(ast, exp.Create):
            tgt = self._table_of(ast.this)
            body = ast.expression
            if ast.kind == "VIEW": return "CREATE_VIEW", tgt, self._real_tables(body)
            if body is not None: return "CTAS", tgt, self._real_tables(body)
            return "CREATE_TABLE", tgt, []
        if isinstance(ast, (exp.Select, exp.Union)):
            return "SELECT", None, self._real_tables(ast)
        return "UNKNOWN", None, []

    def _real_tables(self, ast) -> list[TableRef]:
        if ast is None:
            return []
        root = build_scope(ast)
        if root is None:
            # build_scope returns None when it can't fully resolve the AST.
            # Log with context and fall back to a direct exp.Table walk.
            self._log.warning(
                "build_scope returned None for %s — falling back to direct table scan",
                ast.__class__.__name__,
            )
            return list(self._fallback_table_scan(ast))
        out = []
        for scope in root.traverse():
            for alias, (_, src) in scope.selected_sources.items():
                if isinstance(src, exp.Table):
                    out.append(TableRef(
                        src.catalog or None, src.db or None, src.name,
                        alias if alias != src.name else None,
                    ))
        return out

    def _fallback_table_scan(self, ast: exp.Expression):
        """Last resort: walk AST for exp.Table nodes without scope resolution.
        Less precise than build_scope (misses alias/CTE disambiguation) but
        never silently drops tables."""
        for tbl in ast.find_all(exp.Table):
            if tbl.name:
                yield TableRef(tbl.catalog or None, tbl.db or None, tbl.name)

    def _extract_column_lineage(self, ast) -> list[LineageEdge]:
        if not isinstance(ast, (exp.Select, exp.Union, exp.Create, exp.Insert)):
            return []
        body = ast.expression if isinstance(ast, (exp.Create, exp.Insert)) else ast
        if not isinstance(body, (exp.Select, exp.Union)):
            return []
        schema = self.schema_resolver.as_dict() if self.schema_resolver else None
        edges: list[LineageEdge] = []
        target = ast.this if isinstance(ast, (exp.Create, exp.Insert)) else None
        target_tbl = self._table_of(target) if target else None
        for proj in body.selects:
            col_name = proj.alias_or_name
            if col_name == "*":
                continue
            try:
                root = sg_lineage(col_name, body, schema=schema, dialect=self.DIALECT)
            except Exception:
                continue
            downstream = ColumnRef(target_tbl, col_name) if target_tbl else ColumnRef(
                TableRef(None, None, "<output>"), col_name
            )
            for n in root.walk():
                if isinstance(n.expression, exp.Table):
                    upstream = ColumnRef(
                        TableRef(n.expression.catalog or None, n.expression.db or None,
                                 n.expression.name),
                        n.name.split(".")[-1],
                    )
                    edges.append(LineageEdge(
                        src=upstream,
                        dst=downstream,
                        transform="UNKNOWN",
                        confidence=1.0,
                    ))
        return edges

    @staticmethod
    def _table_of(node) -> TableRef | None:
        if isinstance(node, exp.Schema): node = node.this
        if isinstance(node, exp.Table):
            return TableRef(node.catalog or None, node.db or None, node.name)
        return None
```

### 3.4 Dialect registry (adapter/strategy pattern)

```python
# src/sqlcg/parsers/registry.py
from .ansi_parser import AnsiSqlParser
from .snowflake_parser import SnowflakeParser
from .bigquery_parser import BigQueryParser
from .postgres_parser import PostgresParser
from .tsql_parser import TSqlParser

PARSERS = {
    None: AnsiSqlParser,
    "snowflake": SnowflakeParser,
    "bigquery": BigQueryParser,
    "postgres": PostgresParser,
    "tsql": TSqlParser,
}

def get_parser(dialect: str | None, schema_resolver=None):
    cls = PARSERS.get(dialect, AnsiSqlParser)
    return cls(schema_resolver=schema_resolver)
```

Adding a new dialect is a 30-line subclass plus one registry entry.

## 4. Phase 2 — Cross-File Indexer with Deferred Resolution (2 weeks)

Goal: walk a repo, build a global symbol table, stitch lineage across CREATE VIEW boundaries.

### 4.1 Two-pass aggregator

Pass 1: walk all .sql files and register every `CREATE TABLE`, `CREATE VIEW`, and `CTAS` as a source definition. Pass 2: reparse each file with the populated sources mapping injected into `sqlglot.lineage()`, which auto-inlines upstream definitions.

```python
# src/sqlcg/lineage/aggregator.py
from sqlglot import exp
import sqlglot

class CrossFileAggregator:
    def __init__(self):
        self.sources: dict[str, exp.Expression] = {}
        self.definitions: dict[str, tuple] = {}

    def register_pass1(self, parsed):
        for stmt in parsed.statements:
            if stmt.kind in ("CREATE_VIEW", "CTAS") and stmt.target:
                qn = stmt.target.qualified
                ast = sqlglot.parse_one(stmt.sql, dialect=parsed.dialect)
                body = ast.expression if isinstance(ast, exp.Create) else ast
                self.sources[qn] = body
                self.definitions[qn] = (parsed.path, stmt.statement_index)

    def resolve_pass2(self, parser, parsed):
        parser.schema_resolver.add_view_sources(self.sources)
        with open(parsed.path) as f:
            return parser.parse_file(parsed.path, f.read())
```

### 4.2 Schema resolver — with caching

`as_dict()` rebuilds the full nested dict on every call. For a 1000-file repo with 500 column lineage extractions per file, this is called 500,000 times without caching. The `lru_cache` + explicit invalidation on mutation fixes this.

```python
# src/sqlcg/lineage/schema_resolver.py
import functools
from sqlglot import exp

class SchemaResolver:
    def __init__(self, dialect=None):
        self.dialect = dialect
        self._tables: dict[tuple, dict] = {}
        self._view_bodies: dict[str, exp.Expression] = {}

    def _normalize_key(self, name: str) -> str:
        # Snowflake uppercases unquoted identifiers — must normalize at index time
        # to avoid silent schema-lookup misses (e.g. `orders` vs `ORDERS`).
        # Store pre-normalized values in KùzuDB; never do runtime toUpper() in Cypher.
        if self.dialect == "snowflake":
            return name.upper()
        return name.lower()

    def add_create_table(self, ast: exp.Create):
        if ast.kind != "TABLE" or not isinstance(ast.this, exp.Schema):
            return
        tbl = ast.this.this
        cols = {
            self._normalize_key(c.name): (c.args.get("kind").sql() if c.args.get("kind") else "UNKNOWN")
            for c in ast.this.expressions if isinstance(c, exp.ColumnDef)
        }
        key = (tbl.catalog or None, tbl.db or None, self._normalize_key(tbl.name))
        self._tables[key] = cols
        self._invalidate_cache()

    def add_dbt_manifest(self, manifest_path):
        """Load schemas from dbt manifest via the `dbt-artifacts` package.
        Handles manifest v4–v12 schema differences automatically.
        Provides all model columns, types, and resolves ref() to qualified names."""
        from dbt_artifacts.manifest import Manifest
        manifest = Manifest.from_filepath(manifest_path)
        for node in manifest.nodes.values():
            if node.columns:
                key = (None, node.schema_, self._normalize_key(node.name))
                self._tables[key] = {
                    self._normalize_key(col): meta.data_type or "UNKNOWN"
                    for col, meta in node.columns.items()
                }
        self._invalidate_cache()

    def add_information_schema(self, csv_path):
        """Load from a previously-dumped information_schema.columns CSV/Parquet snapshot
        for users with warehouse read access who want offline schema without dbt."""
        ...

    def add_view_sources(self, sources: dict):
        self._view_bodies.update(sources)
        self._invalidate_cache()

    @functools.lru_cache(maxsize=1)
    def as_dict(self) -> dict:
        out = {}
        for (cat, db, name), cols in self._tables.items():
            cur = out
            for k in [cat, db]:
                if k: cur = cur.setdefault(k, {})
            cur[name] = cols
        return out

    def as_sources(self) -> dict:
        return dict(self._view_bodies)

    def _invalidate_cache(self):
        self.as_dict.cache_clear()
```

### 4.3 Four known failure modes

Four patterns get explicit handling rather than silent failure:

1. **Dynamic SQL** (IDENTIFIER(), string concatenation) — emits empty lineage with confidence=0.0 and a structured warning; parsing_mode="command_fallback".
2. **Stored procedures / Scripting blocks** that fall back to exp.Command — regex-extracts embedded DML from inside $$...$$  or BEGIN / END bodies, parses each independently, sets parse_failed=True, confidence=0.3, column_lineage=[], parsing_mode="command_fallback".
3. **SELECT * without schema** — emits a single table-level edge plus schema_required flag surfaced back to Claude via the MCP tool response. Claude can then ask the user to provide DDL or run dbt compile.
4. **COPY INTO @stage** — emits a STAGE source node with kind="STAGE" rather than a table-level lineage edge, preventing false upstream relationships from polluting the graph.

### 4.4 Schema-awareness ingestion paths (open source only)

Three paths, all feeding the same `SchemaResolver.add_table()` interface:

- **DDL sniffing (Pass 1)** — free in any repo where CREATE TABLE lives alongside DML. Best coverage, zero config.
- **dbt manifest.json** — every dbt project has one after `dbt compile`. Uses the dbt-artifacts package to handle manifest v4–v12 schema differences; provides all model columns, types, and resolves ref() to qualified names. Activated with `--dbt-manifest path/to/manifest.json`.
- **--schema-from-info-schema** — loads a previously-dumped information_schema.columns snapshot (CSV or Parquet) for users with warehouse read access who want offline schema without dbt.

### 4.5 Incremental indexing and cache invalidation

The `sqlcg watch` command uses watchdog to detect file changes. A naive full re-index is acceptable for small repos but must be stated explicitly. For larger repos, dependency-aware invalidation is required.

Invalidation logic (executed on any file change):

```python
# src/sqlcg/indexer/indexer.py
def reindex_file(self, file_path: str, db: GraphBackend) -> None:
    """Dependency-aware incremental re-index for a single file.
    Steps:
    1. Delete all Query nodes (and their COLUMN_LINEAGE edges) defined in this file.
    2. Find all views/CTAs that SELECT FROM tables defined in this file — they are
       now stale and must be re-resolved too.
    3. Re-parse this file and re-insert.
    4. Trigger re-resolution of stale downstream views.
    """
    stale_views = db.run_read(
        """
        MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)
        <-[:SELECTS_FROM]-(q:Query)-[:DECLARES]->(v:Table {kind: 'VIEW'})
        RETURN DISTINCT v.qualified AS view_name
        """,
        {"path": file_path},
    )
    db.delete_nodes_for_file(file_path)
    self._index_single_file(file_path, db)
    for row in stale_views:
        self._reindex_view_definition(row["view_name"], db)
```

sqlcg watch semantics: for repos under 200 files, full re-index on change is the default and is fast enough (target: <5s). For larger repos, dependency-aware invalidation activates automatically. Document both behaviors explicitly.

## 5. Phase 3 — CLI (1 week)

Mirrors CGC's command surface exactly.

```python
# src/sqlcg/cli/main.py
import typer
from . import commands

app = typer.Typer(no_args_is_help=True, help="SQL Code Graph: lineage indexer for AI agents.")
app.add_typer(commands.analyze.app, name="analyze")
app.add_typer(commands.find.app, name="find")
app.add_typer(commands.mcp.app, name="mcp")
app.add_typer(commands.db.app, name="db")
app.command("index")(commands.index.run)
app.command("watch")(commands.watch.run)
app.command("list")(commands.db.list_repos)

def main(): app()
```

| Command | Purpose | Example |
|---------|---------|---------|
| `sqlcg index <path> [--dialect snowflake] [--dbt-manifest path]` | Walk + parse + persist | `sqlcg index ./warehouse --dialect snowflake` |
| `sqlcg watch <path> [--dialect ...]` | Live updates via watchdog | `sqlcg watch ./warehouse` |
| `sqlcg find table <name>` | Symbol search | `sqlcg find table orders` |
| `sqlcg find column <table.col>` | Column search | `sqlcg find column orders.amount` |
| `sqlcg find pattern "<sql>"` | AST pattern search | `sqlcg find pattern "SUM(*)"` |
| `sqlcg analyze upstream <table.col> [--depth 5]` | Trace lineage backward | `sqlcg analyze upstream marts.daily.total --depth 3` |
| `sqlcg analyze downstream <table.col> [--depth 5]` | Impact analysis | `sqlcg analyze downstream raw.orders.amount` |
| `sqlcg analyze impact <table>` | All downstream queries/views | `sqlcg analyze impact raw.orders` |
| `sqlcg analyze unused` | Tables/columns with no downstream | `sqlcg analyze unused --threshold 0` |
| `sqlcg mcp setup / mcp start` | Register with Claude / run server | `sqlcg mcp setup` |
| `sqlcg db {init,reset,info}` | Manage KùzuDB | `sqlcg db info` |

## 6. Phase 4 — MCP Server (1 week)

Uses FastMCP over stdio. All logging to stderr — stdout is reserved for JSON-RPC. Docstrings kept under 2 KB (Claude Code truncates longer ones). Tools return Pydantic models so FastMCP auto-generates JSON schemas.

### 6.1 stdout redirect guard — enforce at startup

One accidental `print()` anywhere in the call stack corrupts the JSON-RPC stream. This guard runs before `mcp.run()`:

```python
# src/sqlcg/server/server.py
import sys
import logging
import re

def _configure_mcp_logging():
    """Redirect ALL output to stderr before the MCP server starts.
    A stray print() on stdout kills the JSON-RPC stream silently."""
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
```

### 6.2 Tool definitions

```python
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("SQL Code Graph")

class ColumnLineagePath(BaseModel):
    path: list[str] = Field(..., description="Ordered table.column hops from source to sink")
    confidence: float
    files_traversed: list[str]

class TableUsage(BaseModel):
    file: str
    statement_index: int
    kind: str
    sql_preview: str

# Configure into Claude Desktop / Claude Code:
# sqlcg mcp setup
# or manually:
# claude mcp add sql-code-graph -- uvx sql-code-graph-mcp

@mcp.tool()
def index_repo(path: str, dialect: str | None = None, dbt_manifest: str | None = None) -> dict:
    """Walk a directory, parse every .sql file, and (re)build the graph.
    Returns files_parsed, parse_errors, tables_found, lineage_edges_created."""
    ...

@mcp.tool()
def trace_column_lineage(column: str, direction: str = "upstream", max_depth: int = 5) -> list[ColumnLineagePath]:
    """Trace a column's lineage. column is db.schema.table.col or table.col.
    direction: upstream (where data comes from) or downstream (impact analysis)."""
    ...

@mcp.tool()
def find_table_usages(table: str) -> list[TableUsage]:
    """Find every .sql statement that reads from or writes to table."""
    ...

@mcp.tool()
def get_downstream_dependencies(table: str, max_depth: int = 5) -> dict:
    """List every view, table, or query downstream of table. Use for impact analysis."""
    ...

@mcp.tool()
def get_upstream_dependencies(table: str, max_depth: int = 5) -> dict:
    """List every source table ultimately depends on. Use for root-cause analysis."""
    ...

@mcp.tool()
def search_sql_pattern(query: str, limit: int = 20) -> list[dict]:
    """Free-text + AST search. Matches table names, column names, function calls."""
    ...

@mcp.tool()
def list_dialects_and_repos() -> dict:
    """List indexed repositories and their detected SQL dialects."""
    ...

# NOTE: execute_cypher is intentionally omitted from v1.
# Named tools above are safer and more useful. Raw Cypher over MCP is a footgun:
# Claude will construct valid read queries that are still expensive on large graphs.
# If included in future versions, enforce: mutation guard + auto-LIMIT.
#
# _BLOCKED = re.compile(r'\b(CREATE|MERGE|SET|DELETE|DROP|DETACH)\b', re.IGNORECASE)
# _MAX_LIMIT = 500
# if _BLOCKED.search(query): raise ValueError("Write operations not permitted via MCP")
# if "LIMIT" not in query.upper(): query = query.rstrip(";") + f" LIMIT {_MAX_LIMIT}"

def main():
    _configure_mcp_logging()
    mcp.run() # stdio
```

## 7. KùzuDB Graph Schema

Key Cypher queries — written to the common subset that runs on both KùzuDB and Neo4j:

```sql
-- Upstream lineage for a column, up to 10 hops
MATCH p = (sink:Column {id: $col})<-[:COLUMN_LINEAGE*1..10]-(src:Column)
RETURN [n IN nodes(p) | n.id] AS path, [r IN rels(p) | r.confidence] AS confidences;

-- Downstream impact: every Query reading from a table
MATCH (t:Table {qualified: $tbl})<-[:SELECTS_FROM|INSERTS_INTO|UPDATES|DELETES_FROM|MERGES_INTO]-(q:Query)
RETURN q.file, q.stmt_index, q.kind, q.sql LIMIT 100;

-- Incremental indexing: stale views after a file change
MATCH (f:File {path: $path})<-[:DEFINED_IN]-(t:Table)
  <-[:SELECTS_FROM]-(q:Query)-[:DECLARES]->(v:Table {kind: 'VIEW'})
RETURN DISTINCT v.qualified AS view_name;
```

### Node tables

```sql
CREATE NODE TABLE File (path STRING PRIMARY KEY, dialect STRING, sha STRING, indexed_at TIMESTAMP);
CREATE NODE TABLE Repo (path STRING PRIMARY KEY, name STRING);
CREATE NODE TABLE Table (qualified STRING PRIMARY KEY, catalog STRING, db STRING, name STRING, kind STRING);
  -- kind: TABLE | VIEW | CTE | EXTERNAL | STAGE | UNKNOWN
CREATE NODE TABLE Column (id STRING PRIMARY KEY, table STRING, name STRING, data_type STRING);
  -- id = "qualified_table.col_name"
CREATE NODE TABLE Query (id STRING PRIMARY KEY, file STRING, stmt_index INT64,
  kind STRING, sql STRING, confidence DOUBLE, parse_failed BOOL,
  parsing_mode STRING);
CREATE NODE TABLE StoredProc(qualified STRING PRIMARY KEY, file STRING);
```

### Rel tables

```sql
CREATE REL TABLE CONTAINS (FROM Repo TO File);
CREATE REL TABLE DEFINED_IN (FROM Table TO File);
CREATE REL TABLE HAS_COLUMN (FROM Table TO Column);
CREATE REL TABLE DECLARES (FROM Query TO Table);
CREATE REL TABLE SELECTS_FROM (FROM Query TO Table, alias STRING);
CREATE REL TABLE INSERTS_INTO (FROM Query TO Table);
CREATE REL TABLE UPDATES (FROM Query TO Table);
CREATE REL TABLE DELETES_FROM (FROM Query TO Table);
CREATE REL TABLE MERGES_INTO (FROM Query TO Table);
CREATE REL TABLE LOADS_FROM_STAGE (FROM Query TO Table); -- COPY INTO @stage
CREATE REL TABLE JOINS (FROM Table TO Table, on_columns STRING, query_id STRING);
CREATE REL TABLE REFERENCES_COLUMN (FROM Query TO Column);
CREATE REL TABLE COLUMN_LINEAGE (FROM Column TO Column,
  query_id STRING, file STRING, confidence DOUBLE,
  transform STRING);
  -- transform: IDENTITY | CASE | AGGREGATE | EXPR | FLATTEN | VARIANT_ACCESS | UNKNOWN
```

## 8. Snowflake Dialect — Full Analysis

### 8.1 What works out of the box with dialect="snowflake"

- **Identifier normalization** — unquoted identifiers uppercase to FOO. `normalize_identifiers(dialect="snowflake")` handles this correctly. Pin sqlglot>=28.0 for the PR #7046 colon-extract precedence fix.
- **Standard DML** — SELECT, INSERT, UPDATE, DELETE, MERGE parse cleanly.
- **CTEs, window functions, subqueries, QUALIFY** — handled well by sqlglot's lineage module.
- **Three-part naming** — db.schema.table resolves correctly.
- **Colon extract** — col:field::type parses as exp.JSONExtract in sqlglot>=28.0.
- **SAMPLE, CLONE, standard DDL** — parse and produce table-level edges correctly.

### 8.2 The six gaps and their handling

| Gap | Nature | Our handling |
|-----|--------|--------------|
| 1 | Colon + reserved word (col:values::string) | Parser bug — upstream issue open | Retry-with-quoting in SnowflakeParser |
| 2 | LATERAL FLATTEN column lineage (source_name empty) | Semantic gap — upstream issue open, partially addressed | Table-level edge only, confidence=0.5, transform="FLATTEN" |
| 3 | Snowflake Scripting (BEGIN / END) → exp.Command | Design limitation — already in blueprint Phase 2 | Regex DML extraction, confidence=0.3 |
| 4 | IDENTIFIER() dynamic reference | Static analysis limit — already in blueprint Phase 2 | Placeholder <dynamic:IDENTIFIER> node, confidence=0.0 |
| 5 | Stored procedures (SQL body) → exp.Command | Design limitation — already in blueprint Phase 2 | Regex extraction from $$...$$ , StoredProc node |
| 6 | COPY INTO @stage → false table edge | Parser gap — upstream issue open | STAGE node + LOADS_FROM_STAGE rel, not table lineage |

### 8.3 SnowflakeParser implementation

```python
# src/sqlcg/parsers/snowflake_parser.py
import re
from sqlglot import exp
from .base import SqlParser, QueryNode, TableRef, ParsedFile, LineageEdge
from pathlib import Path

_SCRIPTING_BLOCK = re.compile(r'\bBEGIN\b', re.IGNORECASE)
_EMBEDDED_DML = re.compile(
    r'(SELECT\s+.+?(?=;|$)|INSERT\s+INTO.+?(?=;|$)|UPDATE\s+.+?(?=;|$)|DELETE\s+.+?(?=;|$))',
    re.DOTALL | re.IGNORECASE,
)

class SnowflakeParser(SqlParser):
    DIALECT = "snowflake"

    def parse_file(self, path: Path, sql: str) -> ParsedFile:
        if _SCRIPTING_BLOCK.search(sql):
            return self._parse_scripting_file(path, sql)
        return super().parse_file(path, sql)

    def _parse_scripting_file(self, path: Path, sql: str) -> ParsedFile:
        """Extract and parse embedded DML from Snowflake Scripting blocks."""
        out = ParsedFile(
            path=path, dialect=self.DIALECT, statements=[],
            defined_tables=[], referenced_tables=[],
            errors=["scripting_block_detected: table-level only"],
        )
        for i, match in enumerate(_EMBEDDED_DML.finditer(sql)):
            try:
                inner = super().parse_file(path, match.group(0))
                for stmt in inner.statements:
                    out.statements.append(QueryNode(
                        **{**stmt.__dict__,
                        'confidence': 0.3,
                        'parse_failed': True,
                        'column_lineage': [],
                        'parsing_mode': 'command_fallback'}
                    ))
            except Exception as e:
                out.errors.append(f"embedded_dml_{i}: {e}")
        return out

    def _real_tables(self, ast) -> list[TableRef]:
        tables = super()._real_tables(ast)
        if ast is None:
            return tables
        # Gap 4: IDENTIFIER() → placeholder
        for anon in ast.find_all(exp.Anonymous):
            if anon.name.upper() == "IDENTIFIER":
                tables.append(TableRef(None, None, "<dynamic:IDENTIFIER>"))
        # Gap 2: LATERAL FLATTEN → extract INPUT => source table
        for lat in ast.find_all(exp.Lateral):
            for kwarg in lat.find_all(exp.Kwarg):
                if kwarg.name.upper() == "INPUT":
                    col = kwarg.find(exp.Column)
                    if col and col.table:
                        tables.append(TableRef(None, None, col.table))
        return tables

    def _classify(self, ast):
        # Gap 6: COPY INTO @stage
        if isinstance(ast, exp.Copy):
            tgt = self._table_of(ast.this)
            if tgt and tgt.name.startswith("@"):
                return "COPY_INTO", None, [TableRef(None, None, tgt.name)]
            return "COPY_INTO", tgt, []
        # Gap 5: CREATE PROCEDURE
        if isinstance(ast, exp.Create) and getattr(ast, 'kind', None) == "PROCEDURE":
            return "CREATE_PROCEDURE", self._table_of(ast.this), []
        return super()._classify(ast)
```

### 8.4 Identifier case normalization (critical correctness requirement)

Snowflake uppercases unquoted identifiers: a query writing to `orders` actually writes to `ORDERS`. If SchemaResolver stores keys in lowercase (sqlglot's default), every schema lookup silently misses and SELECT * produces zero column edges.

Fix: normalize all identifiers to uppercase at index time (implemented in `SchemaResolver._normalize_key()` above). Store pre-normalized values in KùzuDB — never do runtime toUpper() on Cypher queries.

### 8.5 Honest accuracy expectations for Snowflake

| Pattern | Column lineage | Table lineage | Notes |
|---------|---|---|---|
| Standard SELECT / INSERT / CTAS | 90–95% | 99% | With schema; ~70% without |
| CTEs, subqueries, JOINs | 85–92% | 98% | sqlglot lineage handles well |
| QUALIFY, SAMPLE, CLONE | N/A | 95% | Structural — no column edges needed |
| Colon/dot VARIANT access | 0% | 90% | Sub-field paths unresolvable statically |
| LATERAL FLATTEN | 0% | 85% | Source table captured, output columns not |
| Snowflake Scripting blocks | 0% | 60% | Regex DML extraction — many edge cases |
| IDENTIFIER() dynamic | 0% | 0% | Fundamentally unresolvable |
| Stored procedures (SQL body) | 0% | 65% | Regex extraction, table-level only |

User-facing framing: sqlcg gives full column lineage on the ~80% of Snowflake SQL that is standard DML, and table-level lineage with explicit warnings on the remaining ~20%. That's the same ceiling DataHub ships for Snowflake — and more than any agent gets from grep.

## 9. Package Combination Findings

The research question: do combining packages (sqllineage, DataHub's SqlParsingAggregator, LineageX) fill any of the six gaps?

| Gap | sqlglot alone + SqlParsingAggregator | Other packages |
|-----|---|---|
| 1 — colon reserved word | Crashes | No change | No help |
| 2 — LATERAL FLATTEN column | Table-level only | No change | No help |
| 3 — Scripting blocks | 0% col / 60% tbl | 0% col / 75% tbl | No help |
| 4 — IDENTIFIER() | 0% / 0% | 0% / 0% | No help — fundamental limit |
| 5 — Stored procs (SQL body) | 0% col / 65% tbl | 0% col / 78% tbl | No help |
| 6 — COPY INTO @stage | False edge | No change | No help |

DataHub's SqlParsingAggregator is the one meaningful addition: it maintains session-scoped temp table state across statements, improving table-level accuracy for scripting blocks and stored procs by ~15 points. Add as an optional dependency:

```ini
[project.optional-dependencies]
snowflake = ["acryl-datahub[sql-parsing]>=0.14.0"]
```

Use it specifically inside `_parse_scripting_file()` for cross-statement temp table resolution:

```python
from datahub.sql_parsing.sql_parsing_aggregator import SqlParsingAggregator, ObservedQuery

aggregator = SqlParsingAggregator(platform="snowflake", graph=None, generate_lineage=True)
for stmt_sql in extracted_stmts:
    aggregator.add_observed_query(ObservedQuery(query=stmt_sql, session_id="proc_1"))
result = aggregator.gen_metadata()
```

Note: the `datahub.sql_parsing` module is explicitly marked as not part of the stable SDK. Pin to a specific acryl-datahub version and treat as internal — test our wrapper, not the module directly.

## 10. Upstream Issue Tracker

Issues in the packages we depend on. Status as of May 2026.

| Gap | Package | Issue | Failing example? | Test suite covers it? | Nature |
|-----|---------|-------|---|---|---|
| 1 | sqlglot | #2957 — open, filed Feb 2024 | In issue | Not covered in tests/dialects/test_snowflake.py | Parser bug — fixable |
| 2 | sqlglot | #3356 — partially addressed in PR #3360 | In issue | Added in PR, documents incomplete behavior | Semantic gap — partially fixable |
| 3 | sqlglot | No issue — documented design choice | Self-contained repro (see §8.3) | No scripting block tests | Design limitation — workaround only |
| 4 | sqlglot + DataHub | No issue — explicitly documented as out-of-scope by DataHub | Self-contained repro (see §8.3) | Excluded by design in both packages | Fundamental static-analysis limit |
| 5 | sqlglot | No issue — same design as Gap 3 | Self-contained repro (see §8.3) | No proc body tests | Design limitation — workaround only |
| 6 | sqlglot | #3388 — open, filed May 2024 | In issue | COPY partially covered, @stage false-edge case not tested | Parser gap — partially fixable |

Action: Before writing our SnowflakeParser workarounds, contribute failing test cases for Gaps 3, 4, and 5 to sqlglot's `tests/dialects/test_snowflake.py` and open upstream issues. Upstream fixes become free upgrades; regression tests protect us from sqlglot version changes.

## 11. Phase 5 — Effectiveness Benchmarking

DataHub's "97–99% accuracy" is coverage (queries producing some output), not precision/recall. We publish both, separately.

| Layer | What it tests | Source | Target |
|-------|---|---|---|
| Parse success rate | % files without exp.Command fallback | sqlglot dialect fixtures + TPCH/TPC-DS | ≥99% standard SQL, ≥85% procedural |
| Table-level precision/recall | Correct upstream tables vs. ground truth | Hand-curated golden corpus (100–500 queries) | ≥98% |
| Column-level, schema-aware | Correct column→column edges with full DDL | Same corpus, full schema | 90–97% |
| Column-level, schema-naive | Same, no schema | Same corpus | 60–80% |
| Cross-file resolution rate | References to other-file views correctly resolved | Synthetic multi-file fixtures + jaffle_shop | ≥90% |
| Query latency p50/p95 | parse-to-graph per file | TPC-H + adversarial (200-JOIN, 500-UNION) | p95 < 500ms |
| Index throughput | Files/second on 1000-file repo | jaffle_shop scaled-up | ≥50 files/sec |

Benchmark fixture sources:

- **TPC-H / TPC-DS** — standard cross-dialect SQL corpus
- **SQLMesh open test suite** — large cross-dialect fixture set with known semantics; use instead of writing 500 golden queries by hand
- **jaffle_shop** — public dbt project for cross-file resolution and dbt adapter tests
- **Synthetic multi-file chains** — hand-authored for dependency invalidation edge cases
- **Known failure modes** — one regression test each: dynamic SQL via IDENTIFIER(), BigQuery scripting (DECLARE / IF / CALL → exp.Command), MERGE (table-level only), SELECT * without schema, UNNEST, struct subfield access, recursive CTEs, Snowflake COPY INTO @stage (assert STAGE node emitted, no false table edge), Snowflake LATERAL FLATTEN (assert table-level edge from INPUT => source, no column edges).

Snowflake golden corpus fixtures (`tests/benchmarks/golden_corpus/snowflake/`):

| File | Tests |
|------|-------|
| qualify.sql | QUALIFY ROW_NUMBER() OVER (...) |
| lateral_flatten.sql | LATERAL FLATTEN(INPUT => col) |
| colon_extract.sql | src:field::string, src:nested.deep::int |
| colon_reserved_word.sql | col2:values::string — graceful fallback, not crash |
| scripting_block.sql | BEGIN ... SELECT ... FROM t; ... END |
| identifier_dynamic.sql | SELECT * FROM IDENTIFIER($tbl) |
| copy_into.sql | COPY INTO my_table FROM @my_stage/path |
| three_part.sql | SELECT * FROM db.schema.table |
| create_procedure.sql | Full stored proc with embedded SELECTs |
| case_normalization.sql | Mix of orders, ORDERS, "orders" — all same graph node |

## 12. Dependency Pinning

```ini
# pyproject.toml
[project]
name = "sql-code-graph"
dependencies = [
    "sqlglot[rs]>=28.0,<31.0",      # parser + rust tokenizer; pin tightly — minor versions break
    "kuzu==0.11.3",                 # pinned due to Kuzu Inc archival concerns
    "mcp>=1.27.0,<2.0",             # FastMCP server SDK
    "typer[all]>=0.9.0",
    "rich>=13.7.0",
    "inquirerpy>=0.3.4",            # first-run wizard, parity with CGC
    "watchdog>=3.0.0",              # NOT watchfiles — CGC uses watchdog
    "pathspec>=0.12.1",             # .sqlcgignore parsing
    "python-dotenv>=1.0.0",
    "pydantic>=2.0",
    "dbt-artifacts>=1.0.0",         # typed dbt manifest parsing; handles v4–v12 schema differences
]

[project.optional-dependencies]
neo4j = ["neo4j>=5.15.0"]
dbt = ["dbt-core>=1.7"]
snowflake = ["acryl-datahub[sql-parsing]>=0.14.0"]  # SqlParsingAggregator for scripting/proc temp table tracking

# CLI entry point — required for `sqlcg` command to exist after install
[project.scripts]
sqlcg = "sqlcg.cli.main:main"
```

Risk mitigations:

- **KùzuDB archival** — Kuzu Inc is working on a successor. Mitigation: pin to 0.11.3, keep all DB access behind the GraphBackend ABC (defined explicitly in §3.1 — not a stub), write Cypher to the common subset Neo4j also accepts.
- **Neo4j backend** is a supported fallback from day one, continuously tested in CI.
- **sqlglot breaking changes** — minor versions break parser behavior more often than semver implies. Pin <31.0, run `make check` against pinned version in CI, review CHANGELOG on every bump.
- **DataHub sql_parsing stability** — the datahub.sql_parsing module is explicitly marked as not part of the stable SDK. Pin to a specific acryl-datahub version and treat as internal — test our wrapper, not the module directly.
- **dbt-artifacts stability** — mature package tracking manifest schema changes; preferable to hand-rolling manifest parsing which breaks on every dbt major version.

## 13. What "Actionable" Means for Phase 1 — Revised Priority Order

The original plan deferred the GraphBackend ABC to "later." That's the highest-risk item in the project. Revised week-1 order:

1. `uv init sql-code-graph && uv add "sqlglot[rs]" kuzu typer pydantic pytest`
2. Define GraphBackend ABC (`src/sqlcg/core/graph_db.py`) — the full interface from §3.1, not a stub
3. Implement KuzuBackend and stub Neo4jBackend — both wired to the same schema DDL from §7. Run every Cypher query against both in CI.
4. Freeze the data model — TableRef, ColumnRef, LineageEdge, QueryNode, ParsedFile from §3.2. No parser code before these are settled.
5. SchemaResolver with lru_cache and _invalidate_cache() from §4.2
6. SqlParser base class with _fallback_table_scan from §3.3
7. Write `tests/unit/test_parser.py` against ten queries from `sqlglot/tests/fixtures/identity.sql` plus five canonical CTE/JOIN/CTAS examples
8. Verify parse_file produces correct kind, target, sources, and column_lineage for each
9. Wire parser output to the graph schema from §7 via KuzuBackend
10. Cross-file resolution (Phase 2) and MCP server (Phase 4) come only after a solid single-file parser

The MCP server is straightforward once the core is solid. Get it running against real .sql files by end of week 2 — it's how you get feedback early, before the benchmark layer reveals what's actually broken.
