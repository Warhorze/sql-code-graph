# Sprint Plan: Star Projection Resolution via Graph Backend

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
  asserts `db.run_read("CALL show_tables() RETURN *", {})` includes a row for
  `STAR_SOURCE`. (KuzuDB exposes its catalog via `show_tables`.)
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

Pass `query_sources` from `_parse_statement` (it already has `sources` in
scope at line 178).

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

### Integration tests (`tests/integration/test_star_resolution.py` — new file)
- `test_ddl_columns_persisted` (T-01)
- `test_star_source_edge_persisted` (T-04)
- `test_star_expansion_creates_edges` (T-05) — the **headline** assertion
- `test_star_expansion_idempotent` (T-05)
- `test_no_ddl_means_no_expansion` (T-05)
- `test_alias_star_expansion` (T-05)
- `test_reindex_clears_star_edges` (T-06)
- `test_reindex_re_expands` (T-06)

### E2E test (`tests/e2e/test_star_resolution_e2e.py` — new file)
- `test_dwh_corpus_emits_star_expanded_edges`: run the existing
  `tests/fixtures/jaffle_shop` corpus (or a redacted DWH slice if available;
  see Risks) end-to-end via the CLI (`uv run sqlcg index ...`). Assert that
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
| Schema version bump breaks existing user databases without warning | `KuzuBackend.get_schema_version()` already exists. Add a `db info` warning in T-07's CLI changes when `get_schema_version() != SCHEMA_VERSION` ("Database schema is outdated; re-index required"). |
| `SELECT t1.*, t2.*` produces two `StarSource` markers — expansion may double-count if the target table only has one of them | Each `StarSource` produces an independent `COLUMN_LINEAGE` edge under MERGE; collisions on `(src.id, dst.id)` are absorbed. Add an explicit test in a follow-up sprint, not blocking. |
| `target_table = ''` (bare `SELECT *` with no INSERT/CREATE) | Expansion query's `WHERE q.target_table <> ''` filter skips them. Documented in design. |

---

## Rollout

- Implement T-01 → T-02 → T-03 → T-04 → T-05 → T-06 → T-07 in order. Each PR
  is independently mergeable except T-04 (depends on T-02/T-03) and T-05
  (depends on T-01/T-04).
- Recommended PR grouping for review velocity:
  - **PR 1**: T-01 (DDL columns) — small, isolated, unblocks observability.
  - **PR 2**: T-02 + T-03 (schema + parser markers) — graph schema bump
    forces re-index, do this once.
  - **PR 3**: T-04 + T-05 + T-06 (indexer wiring + expansion + reindex) —
    headline functionality lands together so the e2e test goes green in one
    PR.
  - **PR 4**: T-07 (CLI surfacing) — small polish, ships last.
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
| `HAS_COLUMN` is written by indexer | `grep -n "RelType.HAS_COLUMN" src/sqlcg/indexer/` | at least 1 match |
| `STAR_SOURCE` REL TABLE in schema.cypher | `grep -n "STAR_SOURCE" src/sqlcg/core/schema.cypher` | exactly 1 match |
| `STAR_SOURCE` enum value | `grep -n "STAR_SOURCE = " src/sqlcg/core/schema.py` | exactly 1 match |
| `_resolve_star_source` is called | `grep -n "_resolve_star_source" src/sqlcg/parsers/base.py` | at least 2 matches (def + call) |
| `star_sources` is populated by parser | `grep -n "star_sources" src/sqlcg/parsers/` | matches in `base.py` and `ansi_parser.py` |
| `_expand_star_sources` is called | `grep -n "_expand_star_sources" src/sqlcg/indexer/indexer.py` | exactly 3 matches (def + 2 calls) |
| Expansion query exists | `grep -n "EXPAND_STAR_SOURCES_QUERY" src/sqlcg/` | matches in `core/queries.py` and `indexer/indexer.py` |
| Star metrics in `db info` | `grep -n "STAR_EXPANSION" src/sqlcg/cli/commands/db.py` | at least 1 match |
| No TODO in expansion path | `grep -n "TODO" src/sqlcg/indexer/indexer.py src/sqlcg/core/queries.py` | no new TODOs introduced by this sprint |
| `SCHEMA_VERSION` bumped | `grep -n 'SCHEMA_VERSION = ' src/sqlcg/core/schema.py` | result is `"2"` |

---

## Ticket Order Summary

1. **T-01** — Persist DDL columns (`HAS_COLUMN` writes) — unblocks expansion target side.
2. **T-02** — Add `STAR_SOURCE` schema + `StarSource` dataclass — additive, no behavior change.
3. **T-03** — Parser emits `StarSource` markers — feeds the indexer.
4. **T-04** — Indexer upserts `STAR_SOURCE` edges — graph now has all inputs.
5. **T-05** — Run expansion Cypher post-ingestion — headline edges materialise.
6. **T-06** — `reindex_file` re-runs expansion + cleanup test — keeps the graph correct on edits.
7. **T-07** — `db info` surfaces star metrics — closes the silent-failure loop.
