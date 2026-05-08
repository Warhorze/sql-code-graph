# Sprint Plan: Star Projection Resolution via Graph Backend

## Reviewer Notes — 2026-05-07 (plan-reviewer)

### BLOCKER — Schema version enforcement gap (T-02)

`KuzuBackend.init_schema()` checks whether the `Repo` node table already exists.
If it does, it returns immediately — no DDL is executed. This means an existing
v1 database will pass the check, skip the `STAR_SOURCE` REL TABLE creation, and
fail at runtime when T-04 tries to upsert the first `STAR_SOURCE` edge
("Runtime exception: table STAR_SOURCE not found").

**Fix applied in Risks table**: the mitigation entry for "Schema version bump"
has been strengthened. Developer must also add a schema-version guard to the
`index` command (not just `db info`). Specifically, after `backend.init_schema()`
in `src/sqlcg/cli/commands/index.py` (line 63), add:

```python
stored = backend.get_schema_version()
if stored != SCHEMA_VERSION:
    raise typer.Exit(
        console.print(
            f"[red]Database schema is v{stored}; this build requires v{SCHEMA_VERSION}. "
            "Run 'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-index.[/red]"
        )
    )
```

The same guard must be added in `src/sqlcg/cli/commands/watch.py` (line 30).
The `db info` guard proposed in T-07 is still correct but is insufficient on
its own — it only helps if the user explicitly runs `db info` before indexing.

### WARN — `_extract_column_lineage` signature ambiguity (T-03)

T-03 introduces `_resolve_star_source` which needs the statement's real source
tables as `list[TableRef]`. The plan pseudocode shows:
```python
star_src_table = self._resolve_star_source(qualifier=qualifier or None, sources=query_sources)
```
But the existing `_extract_column_lineage` already has a `sources: dict[str, Any] | None`
parameter (the `sources_map` for `sg_lineage` temp-table resolution). These are
two different things.

**Fix applied in T-03 below**: the new parameter is named `query_sources:
list[TableRef]` and is added as a separate positional-or-keyword parameter AFTER
the existing `sources` dict. The call site in `ansi_parser.py:182` must pass
both: `sources=sources_map` (existing) and `query_sources=sources` (the list
from line 178 of `_parse_statement`). The variable names are confusing because
`sources` means different things at different call frames — the corrected T-03
section makes this explicit.

### WARN — Wiring checklist missing SnowflakeParser check (Wiring Checklist)

T-03 lists `snowflake_parser.py` as an "affected file" (call-site change only).
The wiring checklist only checks `ansi_parser.py` for `star_sources`. Added a
row to confirm `snowflake_parser.py` was not broken by the return-type change.

### WARN — E2E test corpus cannot produce STAR_EXPANSION edges from jaffle_shop

The `tests/fixtures/jaffle_shop` corpus uses explicit column selects only — no
`SELECT *`. The e2e test `test_dwh_corpus_emits_star_expanded_edges` will get
zero `STAR_EXPANSION` edges if run against jaffle_shop. The Risks table already
notes the synthesised fallback. The E2E test section has been updated to make
the synthesised fixture a required deliverable, not an optional fallback.

### OK — KuzuDB 0.11.3 compatibility verified

All of the following were tested against KuzuDB 0.11.3 (verified with live runs):
- `MERGE ... ON CREATE SET` — works.
- MERGE with `RETURN count(r)` via `run_read` (conn.execute) — works and
  auto-commits without an explicit transaction.
- MERGE with `RETURN` inside `BEGIN TRANSACTION / COMMIT` — works.
- `DETACH DELETE` on a query node cascades to attached `STAR_SOURCE` edges — confirmed.
- `CALL show_tables() RETURN *` — works; result columns are `['id', 'name',
  'type', 'database name', 'comment']`; filter on `name` field for STAR_SOURCE.
- Edge property filter `[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]` in
  MATCH clause — works.

### OK — `QueryKind` string values

`QueryKind.CREATE_TABLE = "CREATE_TABLE"` and `QueryKind.CREATE_VIEW =
"CREATE_VIEW"` confirmed in `src/sqlcg/parsers/base.py` lines 20–21. The T-01
pseudocode filter `s.kind in ("CREATE_TABLE", "CREATE_VIEW")` is correct.

### OK — Caller count for `_extract_column_lineage`

`_extract_column_lineage` has exactly one direct call site: `ansi_parser.py:182`.
`SnowflakeParser._parse_scripting_file` calls `AnsiParser._parse_statement`, not
`_extract_column_lineage` directly. The return-type change propagates through
`_parse_statement` only, leaving `snowflake_parser.py` unaffected structurally.
The plan's "single chokepoint" claim is correct.

### OK — `test_data_models.py` and `test_db_info.py` exist

Both files exist and can be extended. `tests/unit/test_data_models.py` currently
tests `TableRef`, `ColumnRef`, `LineageEdge`. `tests/unit/test_db_info.py`
currently tests health-check warnings. Both are appropriate extension points.


---

## Summary

After `sprint_column_lineage_fix` landed, the 1,457-file Snowflake DWH parse
emits **7 column-lineage edges** across 3 files. The dominant remaining pattern
is `SELECT * FROM <table_defined_in_a_DDL_file>`; `sqlglot.lineage` cannot
expand `*` without a column list, so no edges materialise.

This sprint replaces the previously planned "call `qualify()` at parse time"
approach (architecture finding 6, original direction) with **graph-backend
resolution**:

1. The parser **emits a marker** (a new `STAR_SOURCE` graph edge) every time it
   sees `SELECT *` / `SELECT <alias>.*` from a real table, instead of silently
   dropping the projection into `ParsedFile.errors`.
2. The indexer **persists DDL column definitions** (currently dropped on the
   floor — `add_create_table()` exists but is never called from production code,
   and `_upsert_parsed_file` writes Table nodes without their `Column` children).
3. After all files are upserted, a single **post-ingestion Cypher pass** expands
   each `STAR_SOURCE` edge into concrete `COLUMN_LINEAGE` edges by joining the
   source table's `HAS_COLUMN` children to the query's target table's columns.

The split lets parsers stay file-local (no two-pass ordering required for
correctness) and lets the graph do what it is good at: joining sets of nodes
across files.

---

## Scope

### In Scope

- `src/sqlcg/core/schema.py` — add `RelType.STAR_SOURCE`, `RelType.HAS_COLUMN`
  is already present
- `src/sqlcg/core/schema.cypher` — add `STAR_SOURCE` REL TABLE; SqlColumn already exists
- `src/sqlcg/core/queries.py` — add the post-ingestion Cypher expansion query
  and the `DELETE_STAR_SOURCE_FOR_FILE` cleanup query
- `src/sqlcg/core/kuzu_backend.py` — extend `delete_nodes_for_file` to also
  drop `STAR_SOURCE` edges anchored to the file's queries
- `src/sqlcg/parsers/base.py` — replace the "append `col_lineage_skip:star:*` to
  errors and continue" path with "record the star projection on the QueryNode"
- `src/sqlcg/parsers/ansi_parser.py` — extract DDL column names into a new
  `QueryNode.defined_columns` field for `CREATE TABLE` statements
- `src/sqlcg/indexer/indexer.py` — upsert `SqlColumn` nodes + `HAS_COLUMN`
  edges for DDL columns; upsert `STAR_SOURCE` edges for star projections; run
  the expansion pass after the per-file upsert loop completes
- `tests/unit/test_base_parser.py`, new `tests/integration/test_star_resolution.py`,
  new `tests/e2e/test_star_resolution_e2e.py`
- `src/sqlcg/lineage/schema_resolver.py` — implement `add_information_schema()` stub
- `src/sqlcg/cli/commands/load_schema.py` — new `load-schema` CLI command
- `src/sqlcg/cli/main.py` — register `load-schema`
- `src/sqlcg/cli/commands/index.py` — wire `--schema-from-info-schema` (remove NotImplementedError exit)
- `src/sqlcg/core/schema.cypher` — add `source STRING` to `HAS_COLUMN` REL TABLE (fold into T-02 schema v2 commit)

### Non-Goals

- `SchemaResolver.add_information_schema()` from CSV — the
  `NotImplementedError` stub stays. (Out of scope; finding 6 originally bundled
  this in but it is independent.)
- `qualify(stmt, schema=schema)` at parse time — explicitly **superseded** by
  this plan; do not add it.
- Recursive CTE column lineage — orthogonal; would also require sqlglot's
  recursive resolver.
- Type/nullability propagation — only column **names** flow through the
  expansion. Type info is not stored in the graph today and will not be added
  here.
- Wildcard EXCEPT/EXCLUDE (`SELECT * EXCLUDE col1`) — out of scope; the marker
  records the table only, not exclusion lists. A second sprint can refine.
- Multi-table star (`SELECT a.*, b.* FROM a JOIN b`) — supported insofar as
  each star produces an independent `STAR_SOURCE` edge per qualifier. The
  expansion query handles each edge independently, so this works without
  special-casing.
- Cross-database `SELECT *` where DDL lives in another repo — out of scope; the
  expansion only fires when the source table has `HAS_COLUMN` children in the
  same graph.

---

## Design

### New graph schema

Two additions to `src/sqlcg/core/schema.py` (`RelType` enum) and one REL TABLE
in `src/sqlcg/core/schema.cypher`:

```python
# schema.py — add to RelType
STAR_SOURCE = "STAR_SOURCE"
```

```cypher
-- schema.cypher — append after COLUMN_LINEAGE block
CREATE REL TABLE STAR_SOURCE (
    FROM SqlQuery TO SqlTable,
    qualifier STRING,           -- '<unqualified>' or alias name (e.g. 'base')
    target_table STRING,        -- destination table.full_id (or '' for bare SELECT)
    confidence FLOAT            -- 0.8 baseline; reserved for future tuning
);
```

`HAS_COLUMN` already exists in `schema.cypher` (line 64) but is currently never
written. This sprint wires the writes.

### New `QueryNode` field

`src/sqlcg/parsers/base.py` `QueryNode` gains:

```python
star_sources: list[StarSource] = field(default_factory=list)
defined_columns: list[str] = field(default_factory=list)  # CREATE TABLE only
```

where `StarSource` is a new immutable dataclass in `base.py`:

```python
@dataclass(frozen=True)
class StarSource:
    """A SELECT * marker for graph-backend resolution.

    Attributes:
        source: The TableRef the star projects (e.g. 'BA.source_table' or alias)
        qualifier: The alias used in the SQL (None for bare 'SELECT *')
    """
    source: TableRef
    qualifier: str | None = None
```

### Parser changes

`_extract_column_lineage` in `base.py` (lines 481–488) currently appends
`col_lineage_skip:star:<qualifier>` to `out.errors` and continues. It must
**also** record the projection on the `QueryNode`. Because
`_extract_column_lineage` returns only `list[LineageEdge]` today, we widen the
return contract to a small named tuple `LineageExtraction`:

```python
@dataclass
class LineageExtraction:
    edges: list[LineageEdge]
    star_sources: list[StarSource]
```

Callers in `ansi_parser.py:182` update to unpack and assign both fields on the
`QueryNode`. The marker in `out.errors` is **kept** (it is observable in the
`db info` output and existing tests) but is no longer the only signal.

For `SELECT base.*`, the qualifier is `'base'` and the source table is
**resolved by alias lookup** against `QueryNode.sources`. If the alias does not
match any known source (e.g. unqualified `SELECT *` from an unknown table), we
fall back to the first entry in `QueryNode.sources`. If `QueryNode.sources`
is empty, we skip emitting the `StarSource` and keep the existing error marker
(the graph cannot resolve what we cannot identify).

### Indexer changes

`_upsert_parsed_file` in `src/sqlcg/indexer/indexer.py` (lines 173–311) gains
two new responsibilities:

1. **Upsert DDL columns and `HAS_COLUMN` edges** — for every `defined_table`,
   look up the originating `QueryNode` (matched by `target.full_id`) and for
   each name in `QueryNode.defined_columns`, upsert a `SqlColumn` node and a
   `HAS_COLUMN` edge from the table to the column.
2. **Upsert `STAR_SOURCE` edges** — for every `QueryNode` with non-empty
   `star_sources`, upsert one edge per `StarSource` entry from the query node
   to the source table node.

`Indexer.index_repo` (lines 25–125) gains a final step **after** the per-file
upsert loop:

```python
# Post-ingestion: expand STAR_SOURCE edges into concrete COLUMN_LINEAGE edges
expanded = self._expand_star_sources(db)
result["star_edges_expanded"] = expanded
```

The expansion runs as **one Cypher write** per source-table-with-known-columns,
using the query in `core/queries.py` (see below). It is idempotent: re-running
it after a partial index is safe because `MERGE` is used on the
`COLUMN_LINEAGE` edges.

### Expansion Cypher

Stored in `src/sqlcg/core/queries.py`:

```python
EXPAND_STAR_SOURCES_QUERY = """
MATCH (q:SqlQuery)-[s:STAR_SOURCE]->(t:SqlTable)-[:HAS_COLUMN]->(c:SqlColumn)
WHERE q.target_table <> ''
MATCH (tgt:SqlTable {qualified: q.target_table})
MERGE (dst:SqlColumn {id: q.target_table + '.' + c.col_name})
  ON CREATE SET dst.col_name = c.col_name,
                dst.table_qualified = q.target_table,
                dst.catalog = tgt.catalog,
                dst.db = tgt.db,
                dst.table_name = tgt.name
MERGE (tgt)-[:HAS_COLUMN]->(dst)
MERGE (c)-[r:COLUMN_LINEAGE]->(dst)
  ON CREATE SET r.transform = 'STAR_EXPANSION',
                r.confidence = 0.8,
                r.query_id = q.id
RETURN count(r) AS edges_created
"""
```

This single query:

1. Finds every `STAR_SOURCE` edge where the source table has known columns.
2. Resolves the destination table (`q.target_table`) and creates destination
   `SqlColumn` nodes mirroring the source column names (one column per source
   column).
3. `MERGE`s a `COLUMN_LINEAGE` edge from each source column to the
   newly-materialised destination column with `transform='STAR_EXPANSION'` and
   `confidence=0.8`.

`STAR_SOURCE` edges with `q.target_table = ''` (bare `SELECT *` with no
INSERT/CREATE target) are **not** expanded — there is no destination to attach
columns to. They remain in the graph as breadcrumbs.

### Confidence model

| Edge origin | `transform` | `confidence` |
|---|---|---|
| `sg_lineage` from a named column | `SELECT` | `0.9` (current behavior) |
| Star expansion in graph (this sprint) | `STAR_EXPANSION` | `0.8` |
| Schema-mismatch fallback | `UNKNOWN` | `0.5` (current) |
| `sg_lineage` raised | `UNKNOWN` | `0.0` (current) |

Star expansion is one tier below named-column lineage because the schema
snapshot may be stale relative to the ETL's actual runtime schema.

### Ordering

DDL files do **not** need to be parsed before ETL files. Both `defined_columns`
and `star_sources` are stored on the `QueryNode` first, then upserted into the
graph. The expansion Cypher runs **after** every file has been upserted, so it
sees the union of all DDL columns and all star projections regardless of walk
order. This is the key advantage of graph-backend resolution over parse-time
`qualify()`.

The `register_pass1` view-source map in `CrossFileAggregator` is unaffected —
view body propagation continues to run during pass 2 and remains independent
from star expansion.

### `reindex_file` interaction

`Indexer.reindex_file` (lines 127–146) re-parses one file and its dependent
views, writing through the same `_upsert_parsed_file`. After re-indexing, it
**must call `_expand_star_sources(db)` once** so that fresh `STAR_SOURCE` edges
get expanded. `delete_nodes_for_file` already removes the file's queries (which
cascades to `STAR_SOURCE` edges via `DETACH DELETE`), and the `MERGE`
semantics in the expansion query ensure idempotency.

### Constants and config

No new path or filename constants are introduced. The expansion query string is
defined in `src/sqlcg/core/queries.py` alongside the existing query constants
(`STALE_VIEWS_QUERY`, `DELETE_*_QUERIES`, etc.). `KuzuConfig` in
`src/sqlcg/core/config.py:14` is unchanged.

### Dependencies

No new third-party dependencies. KuzuDB MERGE-with-ON-CREATE-SET is supported
in 0.11.3 (the pinned version).

---

## Implementation Tickets

### T-01 — Persist DDL column definitions to the graph

**T-08 amendment**: Before writing `HAS_COLUMN` edges for a DDL table, check
whether the table already has `source='information_schema'` edges in the graph
(loaded by `sqlcg load-schema`). If it does, skip the DDL write for that table
and log at DEBUG level. To avoid N per-file Cypher reads, load a
`gold_tables: frozenset[str]` set once at the start of `index_repo` (before the
file upsert loop) and pass it into `_upsert_parsed_file`:

```python
# In index_repo, before the file loop:
gold_rows = db.run_read(
    "MATCH (t:SqlTable)-[r:HAS_COLUMN {source: 'information_schema'}]->() "
    "RETURN DISTINCT t.qualified AS q",
    {},
)
gold_tables: frozenset[str] = frozenset(row["q"] for row in gold_rows)
```

```python
# In _upsert_parsed_file, after table node upsert, before HAS_COLUMN write:
if table.full_id in gold_tables:
    logger.debug(
        "Skipping DDL columns for %s — information_schema takes precedence",
        table.full_id,
    )
    continue
```

`_upsert_parsed_file` gains a `gold_tables: frozenset[str] = frozenset()`
parameter (keyword-only, default empty so all existing callers remain valid).

**Why first**: the entire expansion mechanism is useless if DDL column nodes
never reach the graph. Currently `_upsert_parsed_file` writes `SqlTable` nodes
for `defined_tables` but never inspects the underlying CREATE statement for
`exp.ColumnDef` children. Wire that gap before doing anything else.

**Files**:
- `src/sqlcg/parsers/base.py` — add `defined_columns: list[str] = field(default_factory=list)`
  to `QueryNode` (after line 160, the `parsing_mode` field).
- `src/sqlcg/parsers/ansi_parser.py` — in `_parse_statement` (lines 116–201),
  for `isinstance(stmt, exp.Create) and stmt.kind == 'TABLE'` extract column
  names from `stmt.find_all(exp.ColumnDef)` and assign to
  `query_node.defined_columns`. Use the existing pattern from
  `SchemaResolver.add_create_table()` (lines 70–73) for consistency.
- `src/sqlcg/indexer/indexer.py` — extend `_upsert_parsed_file` (after the
  defined_tables loop ends at line 217) to upsert `SqlColumn` nodes and
  `HAS_COLUMN` edges for each `defined_columns` entry on the matching
  `QueryNode`. The column id format must match the existing `COLUMN_LINEAGE`
  upsert at lines 273–296: `f"{table.full_id}.{col_name}"`.

**Pseudocode (indexer)**:

```python
# After the defined_tables block (line 217) and before the queries block (line 220):

# Map target.full_id -> QueryNode for column lookup
defined_by_query = {
    s.target.full_id: s for s in parsed.statements
    if s.target and s.kind in ("CREATE_TABLE", "CREATE_VIEW") and s.defined_columns
}

for table in parsed.defined_tables:
    qnode = defined_by_query.get(table.full_id)
    if not qnode:
        continue

    # Guard: detect duplicate DDL for the same table across files.
    # Read the existing defined_in_file BEFORE writing columns so we can warn
    # when two separate SQL files both declare CREATE TABLE <same_id>.
    # The HAS_COLUMN union will still be written (confidence 0.8 already signals
    # approximation); the warning makes the conflict observable without a schema
    # change.
    existing = db.run_read(
        f"MATCH (t:SqlTable {{qualified: $qid}}) RETURN t.defined_in_file AS f",
        {"qid": table.full_id},
    )
    if existing and existing[0]["f"] and existing[0]["f"] != parsed.path_str:
        logger.warning(
            "Table %s already defined in %s — %s will add columns to the union; "
            "star expansion may include columns from the earlier DDL file",
            table.full_id,
            existing[0]["f"],
            parsed.path_str,
        )
        out_errors = getattr(parsed, "errors", None)
        if out_errors is not None:
            out_errors.append(
                f"duplicate_ddl:{table.full_id}:already_in:{existing[0]['f']}"
            )

    for col_name in qnode.defined_columns:
        col_id = f"{table.full_id}.{col_name}"
        db.upsert_node(
            NodeLabel.COLUMN,
            col_id,
            {
                "id": col_id,
                "col_name": col_name,
                "table_qualified": table.full_id,
                "catalog": table.catalog or "",
                "db": table.db or "",
                "table_name": table.name,
            },
        )
        db.upsert_edge(
            NodeLabel.TABLE,
            table.full_id,
            NodeLabel.COLUMN,
            col_id,
            RelType.HAS_COLUMN,
            {},
        )
        counts["columns_defined"] = counts.get("columns_defined", 0) + 1
```

**Acceptance**:
- Unit test `tests/unit/test_base_parser.py::test_create_table_extracts_column_names`
  parses `CREATE TABLE BA.t (a INT, b STRING, c DATE)` and asserts
  `parsed.statements[0].defined_columns == ['a', 'b', 'c']`. Order MUST match
  source order (sqlglot guarantees AST ordering of ColumnDef siblings).
- Integration test
  `tests/integration/test_star_resolution.py::test_ddl_columns_persisted` indexes
  a one-file repo with `CREATE TABLE BA.src (col1 INT, col2 STRING)` and asserts
  via `db.run_read("MATCH (:SqlTable {qualified: 'BA.src'})-[:HAS_COLUMN]->(c) RETURN c.col_name AS n ORDER BY n", {})`
  that the result list is `[{"n": "col1"}, {"n": "col2"}]` (exact equality, not
  `len > 0`).
- `grep -n "RelType.HAS_COLUMN" src/sqlcg/indexer/indexer.py` returns at least
  one match (proves it is wired into the upsert path).
- `grep -rn "defined_columns" src/sqlcg/` returns matches in `base.py`,
  `ansi_parser.py`, and `indexer.py` (proves it is read, not just defined).
- Integration test `tests/integration/test_star_resolution.py::test_duplicate_ddl_warns`:
  index a two-file repo where both files contain `CREATE TABLE BA.src (col1 INT)`.
  Assert `logger.warning` is called with a message containing both file paths and
  `"will overwrite"`. Assert the graph ends up with exactly one `SqlTable` node
  for `BA.src` and exactly one `HAS_COLUMN` edge for `col1` (no duplicate edges).

---

### T-02 — Add `STAR_SOURCE` graph schema and `StarSource` parser model

**Depends on**: none (pure additive schema + dataclass)

**Files**:
- `src/sqlcg/core/schema.py` — add `STAR_SOURCE = "STAR_SOURCE"` to `RelType`
  (line 18–28 enum). Bump `SCHEMA_VERSION` from `"1"` to `"2"` (line 6).
- `src/sqlcg/core/schema.cypher` — append the `STAR_SOURCE` REL TABLE block
  after the existing `COLUMN_LINEAGE` block (line 90):

  ```cypher
  -- Query -> Table: query does SELECT * (or alias.*) from this table
  CREATE REL TABLE STAR_SOURCE (
      FROM SqlQuery TO SqlTable,
      qualifier STRING,
      target_table STRING,
      confidence FLOAT
  );
  ```

- `src/sqlcg/parsers/base.py` — add the `StarSource` frozen dataclass after
  `LineageEdge` (line 122) and add
  `star_sources: list[StarSource] = field(default_factory=list)` to `QueryNode`
  (after line 160).

**Note on schema bump**: per `CLAUDE.md` non-negotiables ("No backward
compatibility. Re-index is the migration path"), bumping the schema version
forces existing databases to be re-indexed. Document this in the commit message
and the sprint postmortem section.

**Acceptance**:
- Unit test `tests/unit/test_data_models.py::test_star_source_dataclass`
  constructs `StarSource(source=TableRef(name="t"), qualifier="base")` and
  asserts both fields, plus that the dataclass is `frozen` (mutation raises
  `dataclasses.FrozenInstanceError`).
- Unit test `tests/unit/test_kuzu_backend.py::test_star_source_table_created`
  initialises a fresh `KuzuBackend(":memory:")`, calls `init_schema()`, and
  asserts `db.run_read("CALL show_tables() RETURN *", {})` includes a row where
  the `name` column equals `STAR_SOURCE`. (Verified: KuzuDB 0.11.3 `show_tables()`
  returns columns `['id', 'name', 'type', 'database name', 'comment']`;
  filter on `row["name"] == "STAR_SOURCE"`.)
- `grep -n "STAR_SOURCE" src/sqlcg/core/schema.py src/sqlcg/core/schema.cypher`
  returns three matches (enum, REL TABLE, comment).
- `grep -n "SCHEMA_VERSION = " src/sqlcg/core/schema.py` returns `"2"`.

---

### T-03 — Parser emits `StarSource` markers on `QueryNode`

**Depends on**: T-02 (needs the `StarSource` dataclass and `QueryNode.star_sources`)

**Files**:
- `src/sqlcg/parsers/base.py` — modify `_extract_column_lineage` (lines
  421–578) to also collect `StarSource` entries and return them. Two
  approaches:

  **Approach A (preferred)**: change the return type from `list[LineageEdge]`
  to a new dataclass `LineageExtraction(edges, star_sources)`. Update both
  callers (`ansi_parser.py:182` and `snowflake_parser.py:123` via
  `AnsiParser._parse_statement` — single chokepoint).

  **Approach B (rejected)**: append star sources to `out.errors` and have the
  caller re-parse them out. Rejected because it bakes an ad-hoc string protocol
  into the error channel and is hard to test.

- `src/sqlcg/parsers/ansi_parser.py` — in `_parse_statement` (line 182),
  unpack the new return value:

  ```python
  extraction = self._extract_column_lineage(
      stmt, path, out, schema, dst_table=target, sources=sources_map
  )
  column_lineage = extraction.edges
  star_sources = extraction.star_sources
  ...
  return QueryNode(
      ...
      column_lineage=column_lineage,
      star_sources=star_sources,
      ...
  )
  ```

**Resolving the source table for a star**:

```python
# Inside _extract_column_lineage, replace the existing star block (lines 482-488):

if isinstance(col_expr, exp.Star) or (
    isinstance(col_expr, exp.Column) and isinstance(col_expr.this, exp.Star)
):
    qualifier = col_expr.table if isinstance(col_expr, exp.Column) else None
    out.errors.append(f"col_lineage_skip:star:{qualifier or '<unqualified>'}")

    # NEW: also record a StarSource for graph-backend expansion.
    # Resolve the source table by alias match, fall back to first source.
    star_src_table = self._resolve_star_source(
        qualifier=qualifier or None,
        # dst_table is already in scope; sources are in stmt's scope object
        # — pass them in as a new param `query_sources: list[TableRef]`
        sources=query_sources,
    )
    if star_src_table is not None:
        star_sources.append(
            StarSource(source=star_src_table, qualifier=qualifier or None)
        )
    continue
```

Add `_resolve_star_source` as a new instance method on `SqlParser`:

```python
def _resolve_star_source(
    self,
    qualifier: str | None,
    sources: list[TableRef],
) -> TableRef | None:
    """Match a star qualifier (alias) to one of the statement's source tables.

    Returns the matched TableRef, or the first source as a fallback when the
    qualifier doesn't match any alias and the SELECT has at least one source.
    Returns None when there are no sources to attach the star to.
    """
    if not sources:
        return None
    if qualifier:
        q_lower = qualifier.lower()
        for s in sources:
            # alias match on TableRef.alias OR name match (e.g. SELECT BA.src.*)
            if (s.alias and s.alias.lower() == q_lower) or s.name.lower() == q_lower:
                return s
    return sources[0]
```

**REVIEWER NOTE — parameter naming**: `_extract_column_lineage` already has a
`sources: dict[str, Any] | None` parameter (the `sources_map` dict for
`sg_lineage` temp-table resolution). The new `query_sources: list[TableRef]`
parameter for `_resolve_star_source` is a SEPARATE, ADDITIONAL parameter
appended to the signature after `sources`. The call site in `ansi_parser.py:182`
must pass BOTH:
```python
extraction = self._extract_column_lineage(
    stmt, path, out, schema,
    dst_table=target,
    sources=sources_map,          # existing: dict for sg_lineage
    query_sources=sources,         # new: list[TableRef] for _resolve_star_source
                                   # 'sources' here is the list from line 178
)
```
Do NOT rename the existing `sources` parameter — it is passed to `sg_lineage()`
internally and must remain a dict.

**Files affected**:
- `src/sqlcg/parsers/base.py`
- `src/sqlcg/parsers/ansi_parser.py`
- `src/sqlcg/parsers/snowflake_parser.py` (call-site change only — same
  signature, `_parse_statement` is still the chokepoint)

**Acceptance**:
- Unit test
  `tests/unit/test_base_parser.py::test_select_star_records_star_source`:

  ```python
  sql = "INSERT INTO BA.target SELECT * FROM BA.src"
  parsed = AnsiParser(SchemaResolver()).parse_file(Path("t.sql"), sql)
  insert = parsed.statements[0]
  assert len(insert.star_sources) == 1
  assert insert.star_sources[0].source.full_id == "BA.src"
  assert insert.star_sources[0].qualifier is None
  assert insert.column_lineage == []  # no concrete edges from the parser
  ```

- Unit test
  `tests/unit/test_base_parser.py::test_select_alias_star_records_qualifier`:

  ```python
  sql = "INSERT INTO BA.target SELECT base.* FROM BA.src AS base"
  parsed = AnsiParser(SchemaResolver()).parse_file(Path("t.sql"), sql)
  insert = parsed.statements[0]
  assert len(insert.star_sources) == 1
  assert insert.star_sources[0].source.full_id == "BA.src"
  assert insert.star_sources[0].qualifier == "base"
  ```

- Unit test
  `tests/unit/test_base_parser.py::test_no_sources_skips_star_source`:

  ```python
  # When sources list is empty (rare — DDL only), no StarSource emitted
  sql = "SELECT *"  # parses but resolves to zero sources
  parsed = AnsiParser(SchemaResolver()).parse_file(Path("t.sql"), sql)
  assert parsed.statements[0].star_sources == []
  # Existing error marker still appended
  assert any("col_lineage_skip:star:" in e for e in parsed.errors)
  ```

- Regression: existing
  `tests/unit/test_base_parser.py::test_star_qualified_skip` (added in T-02 of
  the previous sprint, see `sprint_column_lineage_fix.md`) still passes —
  `parsed.errors` continues to contain the `col_lineage_skip:star:` marker.
- `grep -n "_resolve_star_source\|star_sources" src/sqlcg/parsers/base.py` shows
  the helper is defined and called from `_extract_column_lineage`.
- `grep -n "star_sources" src/sqlcg/parsers/ansi_parser.py` shows the
  field is read from the extraction result and assigned to the QueryNode.

---

### T-04 — Indexer upserts `STAR_SOURCE` edges

**Depends on**: T-02 (graph schema), T-03 (parser populates `star_sources`)

**Files**:
- `src/sqlcg/indexer/indexer.py` — in `_upsert_parsed_file` (after the
  `column_lineage` block ends at line 309), add:

  ```python
  # Upsert STAR_SOURCE edges for graph-backend expansion
  for star in stmt.star_sources:
      db.upsert_node(
          NodeLabel.TABLE,
          star.source.full_id,
          {
              "qualified": star.source.full_id,
              "name": star.source.name,
              "catalog": star.source.catalog or "",
              "db": star.source.db or "",
              "kind": "TABLE",
              "defined_in_file": "",
          },
      )
      db.upsert_edge(
          NodeLabel.QUERY,
          query_id,
          NodeLabel.TABLE,
          star.source.full_id,
          RelType.STAR_SOURCE,
          {
              "qualifier": star.qualifier or "<unqualified>",
              "target_table": stmt.target.full_id if stmt.target else "",
              "confidence": 0.8,
          },
      )
      counts["star_sources"] = counts.get("star_sources", 0) + 1
  ```

**Files affected**:
- `src/sqlcg/indexer/indexer.py`

**Acceptance**:
- Integration test
  `tests/integration/test_star_resolution.py::test_star_source_edge_persisted`:
  index a single-file repo containing
  `CREATE TABLE BA.src (a INT, b INT); INSERT INTO BA.tgt SELECT * FROM BA.src;`
  and assert via Cypher:

  ```python
  rows = db.run_read(
      "MATCH (q:SqlQuery)-[s:STAR_SOURCE]->(t:SqlTable) "
      "RETURN t.qualified AS src, s.qualifier AS q, s.target_table AS tgt, "
      "       s.confidence AS conf",
      {},
  )
  assert rows == [{
      "src": "BA.src",
      "q": "<unqualified>",
      "tgt": "BA.tgt",
      "conf": 0.8,
  }]
  ```

- `grep -n "RelType.STAR_SOURCE" src/sqlcg/indexer/indexer.py` returns at
  least one match (wiring confirmed).

---

### T-05 — Post-ingestion expansion query and indexer step

**Depends on**: T-01 (DDL columns in graph), T-04 (`STAR_SOURCE` edges in graph)

**Files**:
- `src/sqlcg/core/queries.py` — append `EXPAND_STAR_SOURCES_QUERY` (full text
  given in the Design section above).
- `src/sqlcg/indexer/indexer.py` — add a new private method
  `_expand_star_sources(db: GraphBackend) -> int` that runs the query inside a
  transaction and returns the row count from the `RETURN count(r)` clause.
  Call it once at the end of `index_repo` (after the upsert loop closes at
  line 117) and once at the end of `reindex_file` (after the
  `_reindex_view_definition` loop at line 146).
- `src/sqlcg/indexer/indexer.py` — extend the returned summary dict from
  `index_repo` (lines 119–125) with `"star_edges_expanded": expanded`.

**Pseudocode**:

```python
def _expand_star_sources(self, db: GraphBackend) -> int:
    """Run the post-ingestion star expansion. Returns edge count."""
    with db.transaction():
        rows = db.run_read(EXPAND_STAR_SOURCES_QUERY, {})
    if not rows:
        return 0
    return int(rows[0].get("edges_created", 0))
```

Note: the query uses `RETURN`, so it must be invoked through `run_read` even
though it has write side effects. KuzuDB supports MERGE in read-execute paths
because the connection is single-mode; if a separation is required by future
KuzuDB versions, switch to `run_write` and a follow-up `run_read` for the
count. A unit test pins this behaviour (see acceptance below).

**Files affected**:
- `src/sqlcg/core/queries.py`
- `src/sqlcg/indexer/indexer.py`

**Acceptance**:
- Integration test
  `tests/integration/test_star_resolution.py::test_star_expansion_creates_edges`:

  ```python
  fixtures = tmp_path / "repo"
  fixtures.mkdir()
  (fixtures / "ddl_src.sql").write_text(
      "CREATE TABLE BA.src (col1 INT, col2 STRING);"
  )
  (fixtures / "ddl_tgt.sql").write_text(
      "CREATE TABLE BA.tgt (col1 INT, col2 STRING);"
  )
  (fixtures / "etl.sql").write_text(
      "INSERT INTO BA.tgt SELECT * FROM BA.src;"
  )
  result = Indexer().index_repo(fixtures, dialect=None, db=temp_db, use_git=False)

  assert result["star_edges_expanded"] == 2
  rows = temp_db.run_read(
      "MATCH (s:SqlColumn)-[r:COLUMN_LINEAGE]->(d:SqlColumn) "
      "WHERE r.transform = 'STAR_EXPANSION' "
      "RETURN s.id AS src, d.id AS dst, r.confidence AS c "
      "ORDER BY src",
      {},
  )
  assert rows == [
      {"src": "BA.src.col1", "dst": "BA.tgt.col1", "c": 0.8},
      {"src": "BA.src.col2", "dst": "BA.tgt.col2", "c": 0.8},
  ]
  ```

- Integration test
  `tests/integration/test_star_resolution.py::test_star_expansion_idempotent`:
  call `Indexer()._expand_star_sources(db)` twice on the same fixture and
  assert the second call returns 0 new edges (because all edges already exist
  via MERGE).
- Integration test
  `tests/integration/test_star_resolution.py::test_no_ddl_means_no_expansion`:
  index a single-file repo with `INSERT INTO BA.tgt SELECT * FROM BA.unknown_src;`
  (no DDL for `BA.unknown_src` anywhere) and assert
  `result["star_edges_expanded"] == 0` and that the `STAR_SOURCE` edge is
  still present (it just has no `HAS_COLUMN` children to expand).
- Integration test
  `tests/integration/test_star_resolution.py::test_alias_star_expansion`:
  ```python
  (fixtures / "ddl.sql").write_text("CREATE TABLE BA.src (a INT, b INT);")
  (fixtures / "etl.sql").write_text(
      "CREATE TABLE BA.tgt AS SELECT base.* FROM BA.src AS base;"
  )
  ```
  assert two `STAR_EXPANSION` edges are created and that the `STAR_SOURCE`
  edge has `qualifier == "base"`.
- `grep -n "_expand_star_sources" src/sqlcg/indexer/indexer.py` returns at
  least 3 matches: definition, call from `index_repo`, call from
  `reindex_file`.
- `grep -n "EXPAND_STAR_SOURCES_QUERY" src/sqlcg/` returns matches in
  `core/queries.py` (definition) and `indexer/indexer.py` (use).

---

### T-06 — `delete_nodes_for_file` cleans up `STAR_SOURCE` edges; `reindex_file` re-runs expansion

**Depends on**: T-04 (edges exist), T-05 (expansion exists)

**Why**: `STAR_SOURCE` edges anchored to a file's queries must vanish when the
file is re-indexed, otherwise stale star projections will keep getting
re-expanded against new DDL columns. Currently `DELETE_QUERIES_FOR_FILE`
already uses `DETACH DELETE` so KuzuDB removes attached `STAR_SOURCE` edges
automatically — we just need to **prove this in a test**, not add new SQL.

But the expansion-driven `COLUMN_LINEAGE` edges that point at the re-indexed
file's columns are NOT cleaned by `DELETE_COLUMNS_FOR_FILE` because that
query only deletes columns of tables `DEFINED_IN` the file. Star-expansion
columns are also `DEFINED_IN` the file (they hang off the target table), so
the existing query catches them.

**Confirmation work**:
- Add `tests/integration/test_star_resolution.py::test_reindex_clears_star_edges`:
  index a fixture, assert `STAR_SOURCE` count > 0; call `reindex_file` on the
  ETL file; assert `STAR_SOURCE` count is the same (the file is re-parsed and
  re-emits the edge — net zero change because of MERGE). Then change the ETL
  file to `SELECT col1 FROM BA.src` (no star); call `reindex_file`; assert
  `STAR_SOURCE` count is now 0.
- Add `tests/integration/test_star_resolution.py::test_reindex_re_expands`:
  after re-indexing, assert `result` (well, `reindex_file` returns None today
  — see "Files" below) leaves the graph with the expected
  `STAR_EXPANSION` edges via Cypher.

**Files affected**:
- `src/sqlcg/indexer/indexer.py` — `reindex_file` (lines 127–146) calls
  `self._expand_star_sources(db)` after the existing dependent-view loop
  (after line 146). No new constants required.

**Acceptance**:
- The two integration tests above pass.
- `grep -n "_expand_star_sources" src/sqlcg/indexer/indexer.py` returns 3
  matches (definition, `index_repo`, `reindex_file`) — already required by
  T-05 grep but re-asserted here.
- No change to `core/queries.py` `DELETE_*` constants (verify via `git diff`
  in the PR).

---

### T-07 — `db info` surfaces star-expansion metrics

**Depends on**: T-04, T-05

**Why**: per `CLAUDE.md` non-negotiable "Tests must assert observable output"
and per `ARCHITECTURE_REVIEW.md` finding 5 (silent failures). The user must be
able to see how many star edges were expanded without writing Cypher by hand.

**Files**:
- `src/sqlcg/cli/commands/db.py` — extend the `db info` output with two new
  rows: `STAR_SOURCE edges` and `STAR_EXPANSION lineage edges`. Use the
  existing rich/text rendering pattern in that file (read it first, reuse the
  same table style; do not introduce new rendering logic).
- `src/sqlcg/core/queries.py` — add two count queries:

  ```python
  COUNT_STAR_SOURCES_QUERY = (
      f"MATCH ()-[r:{RelType.STAR_SOURCE}]->() RETURN count(r) AS n"
  )
  COUNT_STAR_EXPANSIONS_QUERY = (
      f"MATCH ()-[r:{RelType.COLUMN_LINEAGE} "
      f"{{transform: 'STAR_EXPANSION'}}]->() RETURN count(r) AS n"
  )
  ```

**Acceptance**:
- Unit test `tests/unit/test_db_info.py::test_star_metrics_in_info_output`
  (extend the existing test file): build an in-memory KuzuDB, manually upsert
  one `STAR_SOURCE` edge and one `STAR_EXPANSION` `COLUMN_LINEAGE` edge, then
  invoke the `db info` command via the Typer testing harness and assert the
  output text contains both `STAR_SOURCE edges: 1` and
  `STAR_EXPANSION lineage edges: 1` (exact substring match — no
  "no exception raised").
- `grep -n "STAR_EXPANSION" src/sqlcg/cli/commands/db.py` returns at least
  one match.

---

### T-08 — Load information schema CSV as graph-authoritative `HAS_COLUMN` edges

**Depends on**: T-02 (HAS_COLUMN needs `source STRING` property — fold into T-02's schema.cypher
commit); T-01 amendment (skip-guard reads `gold_tables` loaded here)

**Why**: Multiple ETLs can target the same destination table with `SELECT *` from
different source tables. DDL-inferred `HAS_COLUMN` edges produce a union of all
source columns, which may not match production reality. The `INFORMATION_SCHEMA.COLUMNS`
export from the production environment is the authoritative source of truth.
When loaded, it takes precedence over DDL columns — the star expansion Cypher
then uses production-verified column sets, eliminating phantom columns.

**Recommended UX (primary path)**:

Point `index` at the ETL folder only — no DDL folder needed when the CSV is
present. Fewer files to scan, faster indexing, full star resolution:

```bash
# One-time: drop the CSV into the repo
cp ~/Downloads/columns.csv <repo>/.sqlcg/schema.csv

# From then on — just this
sqlcg index etl/
```

`index` auto-discovers `.sqlcg/schema.csv` in the repo root and loads it
before the upsert loop (same as running `load-schema` manually first).
Use `--schema <path>` to point at a different file; use `load-schema` for
ad-hoc or CI-driven refreshes.

**Phase ordering**:

```
# Convention path (schema.csv in .sqlcg/):
sqlcg index etl/                     # auto-loads .sqlcg/schema.csv → Phase 2+3+4

# Explicit path:
sqlcg load-schema columns.csv        # Phase 2 — writes gold HAS_COLUMN to graph
sqlcg index etl/                     # Phase 3 — DDL HAS_COLUMN skipped for gold tables
                                     # Phase 4 — star expansion uses gold columns
```

**Generating the CSV from Snowflake**:

Run in a Snowflake worksheet and export as CSV (File → Download):

```sql
SELECT
    TABLE_CATALOG,
    TABLE_SCHEMA,
    TABLE_NAME,
    COLUMN_NAME,
    ORDINAL_POSITION,
    DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
```

Place the file at `<repo>/.sqlcg/schema.csv`. `DATA_TYPE` is exported for
forward-compatibility but not read by the current implementation.

**Convention-based auto-discovery**:

`src/sqlcg/core/config.py` (`KuzuConfig`) gains a `schema_csv` property:

```python
@property
def schema_csv(self) -> Path | None:
    candidate = self.repo_root / ".sqlcg" / "schema.csv"
    return candidate if candidate.exists() else None
```

`src/sqlcg/cli/commands/index.py` — before the upsert loop, if
`config.schema_csv` is not None (and `--schema` was not passed), call the
same CSV-loading logic as `load_schema_cmd`. `--schema <path>` takes
precedence over the convention path.

**Schema change (coordinate with T-02)**:

`HAS_COLUMN` REL TABLE in `src/sqlcg/core/schema.cypher` gains a `source STRING`
property. Both STAR_SOURCE and the `source` property land in the same schema v2
DDL — one re-index, not two. Developer modifies the `HAS_COLUMN` block in T-02's
commit:

```cypher
-- Table -> Column: table has this column
CREATE REL TABLE HAS_COLUMN (
    FROM SqlTable TO SqlColumn,
    source STRING    -- 'information_schema' | 'ddl'
);
```

All existing `upsert_edge(..., RelType.HAS_COLUMN, {})` calls in T-01 must pass
`{"source": "ddl"}` as the properties dict.

**`qualified` key construction from CSV**:

`TableRef.full_id` joins non-None parts: `catalog.db.name` or `db.name` or
`name`. For INFORMATION_SCHEMA rows, apply the same logic:

```python
def _make_qualified(catalog: str, schema: str, table: str, include_catalog: bool) -> str:
    parts = [p for p in ([catalog] if include_catalog else []) + [schema, table] if p]
    return ".".join(parts)
```

Default (`--include-catalog` absent): `{TABLE_SCHEMA}.{TABLE_NAME}` — matches
the 2-part `BA.src` pattern used by ETLs that omit the catalog. Pass
`--include-catalog` when the repo uses 3-part references (`MYDB.BA.src`).

**Files**:

- `src/sqlcg/core/schema.cypher` — add `source STRING` to `HAS_COLUMN` (T-02 commit).
- `src/sqlcg/lineage/schema_resolver.py` — implement `add_information_schema`:

  ```python
  def add_information_schema(self, csv_path: str | Path) -> int:
      """Load table schemas from an INFORMATION_SCHEMA.COLUMNS CSV.

      Expected columns: TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME,
      COLUMN_NAME, ORDINAL_POSITION. Returns the number of tables loaded.
      """
      import csv as _csv
      required = {"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME",
                  "COLUMN_NAME", "ORDINAL_POSITION"}
      tables: dict[tuple[str | None, str | None, str], list[tuple[int, str]]] = {}
      with open(Path(csv_path), newline="", encoding="utf-8") as f:
          reader = _csv.DictReader(f)
          if reader.fieldnames is None or not required.issubset(reader.fieldnames):
              missing = required - set(reader.fieldnames or [])
              raise ValueError(f"CSV missing required columns: {missing}")
          for row in reader:
              key = (
                  row["TABLE_CATALOG"] or None,
                  row["TABLE_SCHEMA"] or None,
                  row["TABLE_NAME"],
              )
              tables.setdefault(key, []).append(
                  (int(row["ORDINAL_POSITION"]), row["COLUMN_NAME"])
              )
      with self._lock:
          for key, cols in tables.items():
              self._tables[key] = [c for _, c in sorted(cols)]
          self._cache = None
      return len(tables)
  ```

- `src/sqlcg/cli/commands/load_schema.py` — new file:

  ```python
  """Load INFORMATION_SCHEMA CSV into the graph as authoritative HAS_COLUMN edges."""
  import csv
  from pathlib import Path

  import typer
  from rich.console import Console

  from sqlcg.core.config import get_backend
  from sqlcg.core.schema import NodeLabel, RelType
  from sqlcg.utils.logging import getLogger

  logger = getLogger(__name__)
  console = Console()


  def load_schema_cmd(
      csv_path: Path = typer.Argument(..., help="Path to INFORMATION_SCHEMA.COLUMNS CSV"),
      include_catalog: bool = typer.Option(
          False,
          "--include-catalog",
          help="Prefix qualified names with TABLE_CATALOG (use for 3-part references).",
      ),
  ) -> None:
      """Load production column schema from an INFORMATION_SCHEMA CSV.

      Writes HAS_COLUMN edges tagged source='information_schema'. Run this before
      'sqlcg index' so DDL-inferred columns are suppressed for covered tables.
      Idempotent: safe to run multiple times.
      """
      required = {"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME",
                  "COLUMN_NAME", "ORDINAL_POSITION"}
      tables: dict[str, list[tuple[int, str]]] = {}

      with open(csv_path, newline="", encoding="utf-8") as f:
          reader = csv.DictReader(f)
          if reader.fieldnames is None or not required.issubset(reader.fieldnames):
              missing = required - set(reader.fieldnames or [])
              console.print(f"[red]CSV missing columns: {missing}[/red]")
              raise typer.Exit(1)
          for row in reader:
              parts = (
                  [row["TABLE_CATALOG"]] if include_catalog and row["TABLE_CATALOG"] else []
              ) + [row["TABLE_SCHEMA"], row["TABLE_NAME"]]
              qualified = ".".join(p for p in parts if p)
              tables.setdefault(qualified, []).append(
                  (int(row["ORDINAL_POSITION"]), row["COLUMN_NAME"])
              )

      with get_backend() as db:
          cols_written = 0
          for qualified, raw_cols in tables.items():
              cols = [c for _, c in sorted(raw_cols)]
              name_parts = qualified.rsplit(".", maxsplit=1)
              table_name = name_parts[-1]
              db.upsert_node(
                  NodeLabel.TABLE,
                  qualified,
                  {
                      "qualified": qualified,
                      "name": table_name,
                      "catalog": "",
                      "db": name_parts[0] if len(name_parts) == 2 else "",
                      "kind": "TABLE",
                      "defined_in_file": "",
                  },
              )
              for col_name in cols:
                  col_id = f"{qualified}.{col_name}"
                  db.upsert_node(
                      NodeLabel.COLUMN,
                      col_id,
                      {
                          "id": col_id,
                          "col_name": col_name,
                          "table_qualified": qualified,
                          "catalog": "",
                          "db": name_parts[0] if len(name_parts) == 2 else "",
                          "table_name": table_name,
                      },
                  )
                  db.upsert_edge(
                      NodeLabel.TABLE,
                      qualified,
                      NodeLabel.COLUMN,
                      col_id,
                      RelType.HAS_COLUMN,
                      {"source": "information_schema"},
                  )
                  cols_written += 1

      console.print(
          f"[green]Loaded[/green] {len(tables)} tables, {cols_written} columns "
          f"from {csv_path}"
      )
  ```

- `src/sqlcg/cli/main.py` — import and register:

  ```python
  from sqlcg.cli.commands import load_schema   # add to existing import block
  ...
  app.command("load-schema")(load_schema.load_schema_cmd)  # after existing commands
  ```

- `src/sqlcg/cli/commands/index.py` — remove the `schema_from_info_schema` exit guard.
  Wire it: when provided, call `SchemaResolver.add_information_schema(schema_from_info_schema)`
  on the resolver used by the indexer, AND call `load_schema.load_schema_cmd` logic inline
  (or extract it to a shared helper) so graph HAS_COLUMN edges are also written.
  Make `--schema-from-info-schema` visible (remove `hidden=True`).

- `src/sqlcg/indexer/indexer.py` — `index_repo` loads `gold_tables` before the file
  loop (see T-01 amendment above). `_upsert_parsed_file` gains
  `gold_tables: frozenset[str] = frozenset()` keyword parameter.

**Risks addressed**:

| Risk | Mitigation |
|---|---|
| CSV TABLE_SCHEMA case differs from graph `qualified` | Normalize both sides to lower-case in `_make_qualified` and in the skip-check lookup. Document in command help. |
| CSV covers table but graph has no matching node yet | `load-schema` upserts the SqlTable node with `defined_in_file=''`. When `index` runs later, the node already exists; the DDL upsert is a MERGE (no duplicate). |
| CSV does not cover a table (partial CSV) | `gold_tables` frozenset is empty for that table; DDL columns proceed normally. |
| Re-running `load-schema` with updated CSV | MERGE on column node (keyed on `col_id`) is idempotent. New columns are added; removed columns are NOT deleted (deletion is out of scope — re-index or `db reset` is the migration path per CLAUDE.md). |
| `HAS_COLUMN` edges written by T-01 before schema change adds `source` | Schema v2 requires fresh `db init`. All T-01 HAS_COLUMN writes must pass `{"source": "ddl"}` — the wiring checklist grep confirms this. |

**Acceptance**:

- Unit test `tests/unit/test_schema_resolver.py::test_add_information_schema_populates_tables`:
  write a temporary CSV with 2 tables / 3 columns each, call
  `resolver.add_information_schema(tmp_path / "cols.csv")`, assert
  `resolver.as_dict()` contains both tables with columns in ORDINAL_POSITION
  order. Assert return value is `2` (table count).

- Unit test `tests/unit/test_schema_resolver.py::test_add_information_schema_missing_column_raises`:
  pass a CSV missing `ORDINAL_POSITION`; assert `ValueError` is raised with
  `"ORDINAL_POSITION"` in the message.

- Integration test `tests/integration/test_star_resolution.py::test_load_schema_writes_has_column`:
  call `load_schema_cmd` on a 2-table CSV, then assert via Cypher:

  ```python
  rows = db.run_read(
      "MATCH (t:SqlTable)-[r:HAS_COLUMN]->(c:SqlColumn) "
      "RETURN t.qualified AS tq, r.source AS src, c.col_name AS col "
      "ORDER BY tq, col",
      {},
  )
  assert all(r["src"] == "information_schema" for r in rows)
  assert len(rows) == <expected column count>
  ```

- Integration test `tests/integration/test_star_resolution.py::test_gold_schema_suppresses_ddl_columns`:
  load CSV covering `BA.src (col1, col2)`, then index a DDL file adding `BA.src (col1, col2, col3)`.
  Assert graph has exactly 2 `HAS_COLUMN` edges for `BA.src` (not 3), all
  `source='information_schema'`.

- Integration test `tests/integration/test_star_resolution.py::test_partial_csv_leaves_ddl_intact`:
  load CSV covering only `BA.other`; index DDL for `BA.src (col1)`. Assert `BA.src`
  has 1 `HAS_COLUMN` edge with `source='ddl'`.

- Integration test `tests/integration/test_star_resolution.py::test_load_schema_idempotent`:
  run `load_schema_cmd` twice on the same CSV. Assert column count is the same
  after the second run (no duplicates). Use `RETURN count(r)` assertion.

- `grep -n "load.schema\|load_schema" src/sqlcg/cli/main.py` returns at least
  1 match (command registered).
- `grep -n "NotImplementedError" src/sqlcg/lineage/schema_resolver.py` returns 0
  matches (stub implemented).
- `grep -n "source.*information_schema\|information_schema.*source"
  src/sqlcg/cli/commands/load_schema.py` returns at least 1 match.
- `grep -n "gold_tables" src/sqlcg/indexer/indexer.py` returns at least
  2 matches (load site + pass-through to `_upsert_parsed_file`).
- `grep -n '"source": "ddl"' src/sqlcg/indexer/indexer.py` returns at least
  1 match (T-01's DDL HAS_COLUMN write passes the source tag).
- `grep -n "source STRING" src/sqlcg/core/schema.cypher` returns exactly 1 match
  (in HAS_COLUMN block).
- Integration test `tests/integration/test_star_resolution.py::test_index_autodiscovers_schema_csv`:
  write a `schema.csv` to `tmp_path / ".sqlcg" / "schema.csv"`, call
  `index_repo(tmp_path / "etl", ...)` (ETL folder only, no DDL). Assert
  `HAS_COLUMN` edges are present with `source='information_schema'` and that
  star expansion runs correctly — proving the ETL-folder-only path works end-to-end.
- E2E: `sqlcg index etl/` with `.sqlcg/schema.csv` present prints no
  "schema.csv not found" warning and `db info` reports non-zero
  `STAR_EXPANSION lineage edges`.

---

## Test Strategy

### Unit tests (no graph backend)
- `tests/unit/test_data_models.py` — `StarSource` frozen dataclass invariants (T-02).
- `tests/unit/test_base_parser.py`:
  - `test_create_table_extracts_column_names` (T-01)
  - `test_select_star_records_star_source` (T-03)
  - `test_select_alias_star_records_qualifier` (T-03)
  - `test_no_sources_skips_star_source` (T-03)
  - regression: `test_star_qualified_skip` (existing) still passes
- `tests/unit/test_kuzu_backend.py::test_star_source_table_created` (T-02).
- `tests/unit/test_db_info.py::test_star_metrics_in_info_output` (T-07).
- `tests/unit/test_schema_resolver.py`:
  - `test_add_information_schema_populates_tables` (T-08)
  - `test_add_information_schema_missing_column_raises` (T-08)

### Integration tests (`tests/integration/test_star_resolution.py` — new file)
- `test_ddl_columns_persisted` (T-01)
- `test_star_source_edge_persisted` (T-04)
- `test_star_expansion_creates_edges` (T-05) — the **headline** assertion
- `test_star_expansion_idempotent` (T-05)
- `test_no_ddl_means_no_expansion` (T-05)
- `test_alias_star_expansion` (T-05)
- `test_reindex_clears_star_edges` (T-06)
- `test_reindex_re_expands` (T-06)
- `test_load_schema_writes_has_column` (T-08)
- `test_gold_schema_suppresses_ddl_columns` (T-08)
- `test_partial_csv_leaves_ddl_intact` (T-08)
- `test_load_schema_idempotent` (T-08)

### E2E test (`tests/e2e/test_star_resolution_e2e.py` — new file)

**REVIEWER NOTE**: `tests/fixtures/jaffle_shop` uses only explicit column selects
(no `SELECT *`) so it cannot produce any `STAR_EXPANSION` edges. The synthesised
fixture is NOT optional — it is the only corpus that will make this test green.

Required deliverable: create `tests/fixtures/star_corpus/` with at minimum:
- `ddl_src.sql`: `CREATE TABLE star_corp.src_table (id INT, name STRING, amount DECIMAL);`
- `ddl_tgt.sql`: `CREATE TABLE star_corp.tgt_table (id INT, name STRING, amount DECIMAL);`
- `etl_star.sql`: `INSERT INTO star_corp.tgt_table SELECT * FROM star_corp.src_table;`

- `test_dwh_corpus_emits_star_expanded_edges`: index `tests/fixtures/star_corpus/`
  end-to-end via the CLI (`uv run sqlcg index ...`). Assert that
  `db info` reports a non-zero `STAR_EXPANSION lineage edges` count and that
  the `lineage_edges_created` summary key in the indexer return value is
  greater than the pre-T-05 baseline (record the baseline in the test file as
  a constant: `BASELINE_EDGES = 7` from the v0.3.1 postmortem).

### Sprint-level regression guard
- The integration test `test_star_expansion_creates_edges` (T-05) is the
  **regression guard** for the entire sprint. It must NOT be marked
  `@pytest.mark.skip` or `@pytest.mark.xfail` at any time.
- A second guard: `test_no_ddl_means_no_expansion` (T-05) ensures the
  expansion does not silently invent edges when DDL is absent.

---

## Acceptance Criteria (sprint-level)

- [ ] T-01: DDL `CREATE TABLE` columns are extracted into
      `QueryNode.defined_columns` and persisted as `SqlColumn` + `HAS_COLUMN`
      edges. Cypher returns the exact column list, in source order.
- [ ] T-02: `RelType.STAR_SOURCE` exists; `STAR_SOURCE` REL TABLE created in
      a fresh in-memory KuzuDB. `SCHEMA_VERSION` is `"2"`.
- [ ] T-03: `_extract_column_lineage` returns a `LineageExtraction` containing
      both edges and `star_sources`. `QueryNode.star_sources` is populated for
      `SELECT *` and `SELECT alias.*`. `out.errors` retains the existing
      `col_lineage_skip:star:*` marker (no regression).
- [ ] T-04: One `STAR_SOURCE` edge per `StarSource` is upserted with
      `qualifier`, `target_table`, and `confidence=0.8`.
- [ ] T-05: After `index_repo`, every `STAR_SOURCE` edge whose source table
      has `HAS_COLUMN` children produces concrete `COLUMN_LINEAGE` edges with
      `transform='STAR_EXPANSION'` and `confidence=0.8`. `index_repo` returns
      a `star_edges_expanded` count.
- [ ] T-06: `reindex_file` removes stale `STAR_SOURCE` edges (via existing
      `DETACH DELETE`) and re-runs expansion. Tests confirm cleanup.
- [ ] T-07: `db info` surfaces both `STAR_SOURCE edges` and
      `STAR_EXPANSION lineage edges` counts.
- [ ] T-08: `sqlcg load-schema <csv>` writes `HAS_COLUMN` edges tagged
      `source='information_schema'`. DDL-inferred columns are suppressed for
      tables covered by the CSV. `SchemaResolver.add_information_schema()` stub
      is implemented (no `NotImplementedError`). Running `load-schema` twice
      produces no duplicate edges.
- [ ] On the existing `tests/fixtures/jaffle_shop` corpus (or a representative
      DDL+ETL fixture), the e2e test reports `STAR_EXPANSION` edges > 0.
- [ ] `uv run pytest` is green; `uv run pyright` is clean; `uv run ruff check src tests`
      is clean.
- [ ] Regression: all 298 previously-passing tests still pass.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| KuzuDB MERGE-with-ON-CREATE-SET behavior differs from Neo4j on conflict | Pin assertions to KuzuDB-specific behavior in tests; document in T-05 acceptance. If `Neo4jBackend` ever runs the expansion query, port the syntax separately — out of scope here (Neo4j is not in any test path today). |
| Schema staleness — DDL says column `foo` but ETL runtime has dropped it | Confidence `0.8` flags the edge as approximate; downstream consumers can filter on `confidence > 0.85` for strict mode. The architecture review already documents this acceptance (finding 6, lines 78–80). |
| `test_dwh_corpus_emits_star_expanded_edges` relies on the real DWH | If the real DWH corpus is not in the repo, fall back to a synthesised DDL+ETL fixture under `tests/fixtures/star_corpus/` containing 3 DDL files and 5 ETL files. Test must be deterministic and green in CI. |
| Wildcard `EXCLUDE` columns produce wrong expansions (`SELECT * EXCLUDE (col1)` expands `col1` anyway) | Out-of-scope per Non-Goals. Add a `# TODO` ONLY in a comment under `_resolve_star_source` documenting the limitation, NOT in the happy path of the expansion query. Regression test will catch it the day a future sprint addresses it. |
| Schema version bump breaks existing user databases without warning | `init_schema()` returns early for existing databases without checking the stored version — a v1 database will silently lack STAR_SOURCE REL TABLE and fail at T-04 upsert time. Developer MUST add a version guard in `index.py` (and `watch.py`) BEFORE the upsert loop: if `backend.get_schema_version() != SCHEMA_VERSION` raise a user-facing error instructing `db reset && db init && index`. The `db info` warning added in T-07 is necessary but insufficient — see Reviewer Notes BLOCKER at the top of this plan. |
| `SELECT t1.*, t2.*` produces two `StarSource` markers — expansion may double-count if the target table only has one of them | Each `StarSource` produces an independent `COLUMN_LINEAGE` edge under MERGE; collisions on `(src.id, dst.id)` are absorbed. Add an explicit test in a follow-up sprint, not blocking. |
| Multiple DDL files define the same table (`BA.src` appears in two `.sql` files) | **Addressed in T-01.** Before writing `HAS_COLUMN` edges for a DDL table, the indexer reads the existing `defined_in_file` value from the graph. If it is non-empty and belongs to a different file, a structured `logger.warning` is emitted AND a `duplicate_ddl:<table>:already_in:<prior_file>` entry is appended to `parsed.errors` (visible in `db info` parse quality). The `HAS_COLUMN` union is still written — preventing it would require storing file-provenance per column (a schema change out of scope here). Confidence `0.8` already signals approximation to consumers. Integration test `test_duplicate_ddl_warns` (T-01 acceptance) covers this path. |
| `target_table = ''` (bare `SELECT *` with no INSERT/CREATE) | Expansion query's `WHERE q.target_table <> ''` filter skips them. Documented in design. |

---

## Rollout

- Implement T-08 → T-01 → T-02 → T-03 → T-04 → T-05 → T-06 → T-07. T-08 is
  independent and ships first. Each subsequent PR is independently mergeable
  except T-04 (depends on T-02/T-03) and T-05 (depends on T-01/T-04).
- PR grouping: see Ticket Order Summary at the bottom of this file.
- After each PR, the **sprint-planner** runs the plan-compliance check on the
  affected tickets and updates `## Plan Compliance — YYYY-MM-DD — <ticket>`
  in this file.
- Schema version bump: announce in `CHANGELOG.md` (next release) and the PR
  description for PR 2 with the line "All existing databases must be
  re-indexed; no auto-migration is provided per project policy."
- No data migration needed beyond re-index — per `CLAUDE.md` non-negotiable.

---

## Wiring Checklist (developer must complete with grep evidence)

| Item | Grep command | Expected result |
|---|---|---|
| `defined_columns` is read by indexer | `grep -n "defined_columns" src/sqlcg/indexer/` | at least 1 match in `indexer.py` |
| duplicate DDL guard logs and records error | `grep -n "duplicate_ddl" src/sqlcg/indexer/indexer.py` | at least 1 match (the warning + error append path in `_upsert_parsed_file`) |
| `HAS_COLUMN` is written by indexer | `grep -n "RelType.HAS_COLUMN" src/sqlcg/indexer/` | at least 1 match |
| `STAR_SOURCE` REL TABLE in schema.cypher | `grep -n "STAR_SOURCE" src/sqlcg/core/schema.cypher` | exactly 1 match |
| `STAR_SOURCE` enum value | `grep -n "STAR_SOURCE = " src/sqlcg/core/schema.py` | exactly 1 match |
| `_resolve_star_source` is called | `grep -n "_resolve_star_source" src/sqlcg/parsers/base.py` | at least 2 matches (def + call) |
| `star_sources` is populated by parser | `grep -n "star_sources" src/sqlcg/parsers/` | matches in `base.py` and `ansi_parser.py` |
| `snowflake_parser.py` not broken by return-type change | `grep -n "_extract_column_lineage" src/sqlcg/parsers/snowflake_parser.py` | zero matches (Snowflake only calls `_parse_statement`, not `_extract_column_lineage` directly) |
| `_expand_star_sources` is called | `grep -n "_expand_star_sources" src/sqlcg/indexer/indexer.py` | exactly 3 matches (def + 2 calls) |
| Expansion query exists | `grep -n "EXPAND_STAR_SOURCES_QUERY" src/sqlcg/` | matches in `core/queries.py` and `indexer/indexer.py` |
| Star metrics in `db info` | `grep -n "STAR_EXPANSION" src/sqlcg/cli/commands/db.py` | at least 1 match |
| No TODO in expansion path | `grep -n "TODO" src/sqlcg/indexer/indexer.py src/sqlcg/core/queries.py` | no new TODOs introduced by this sprint |
| `SCHEMA_VERSION` bumped | `grep -n 'SCHEMA_VERSION = ' src/sqlcg/core/schema.py` | result is `"2"` |
| `source STRING` in HAS_COLUMN schema | `grep -n "source STRING" src/sqlcg/core/schema.cypher` | exactly 1 match (in HAS_COLUMN block) |
| `add_information_schema` stub removed | `grep -n "NotImplementedError" src/sqlcg/lineage/schema_resolver.py` | 0 matches |
| `load-schema` command registered | `grep -n "load.schema\|load_schema" src/sqlcg/cli/main.py` | at least 1 match |
| `gold_tables` skip-guard wired | `grep -n "gold_tables" src/sqlcg/indexer/indexer.py` | at least 2 matches (load + pass-through) |
| DDL HAS_COLUMN writes pass source tag | `grep -n '"source": "ddl"' src/sqlcg/indexer/indexer.py` | at least 1 match |
| information_schema source tag in load_schema | `grep -n "information_schema" src/sqlcg/cli/commands/load_schema.py` | at least 1 match |
| `.sqlcg/schema.csv` auto-discovery wired | `grep -n "schema_csv" src/sqlcg/core/config.py src/sqlcg/cli/commands/index.py` | at least 1 match in each file |
| Snowflake SQL in CLI help or docstring | `grep -n "INFORMATION_SCHEMA.COLUMNS" src/sqlcg/cli/commands/load_schema.py` | at least 1 match |

---

## Ticket Order Summary

1. **T-08** — Load information schema CSV (`load-schema` command + `SchemaResolver` stub) — run
   first so `gold_tables` is populated before DDL HAS_COLUMN writes in T-01. Can be
   implemented in parallel with T-02/T-03 since it is purely additive.
2. **T-01** — Persist DDL columns (`HAS_COLUMN` writes, suppressed for gold tables) — unblocks expansion target side.
3. **T-02** — Add `STAR_SOURCE` schema + `StarSource` dataclass + `source STRING` on HAS_COLUMN — one schema v2 bump.
4. **T-03** — Parser emits `StarSource` markers — feeds the indexer.
5. **T-04** — Indexer upserts `STAR_SOURCE` edges — graph now has all inputs.
6. **T-05** — Run expansion Cypher post-ingestion — headline edges materialise.
7. **T-06** — `reindex_file` re-runs expansion + cleanup test — keeps the graph correct on edits.
8. **T-07** — `db info` surfaces star metrics — closes the silent-failure loop.

**PR grouping** (revised for T-08):
- **PR 1**: T-08 (load-schema command + SchemaResolver stub) — independent, ships first.
- **PR 2**: T-01 + T-02 (DDL columns with gold-table skip + schema v2 bump with source STRING) — schema change forces re-index once.
- **PR 3**: T-03 + T-04 + T-05 + T-06 (parser markers + indexer wiring + expansion + reindex) — headline functionality lands together.
- **PR 4**: T-07 (CLI surfacing) — small polish, ships last.
