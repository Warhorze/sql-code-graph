# Feature Plan: #35 — External Downstream Lineage Injection at the Egress Boundary

**Plan date**: 2026-05-31
**Author**: architect-planner
**Issue**: [#35](https://github.com/Warhorze/sql-code-graph/issues/35) — inject external downstream
consumers at the terminal/egress boundary so lineage answers show where data goes *after* it leaves
the modeled SQL.
**Branch**: `feat/cluster-b-provenance` (carries trust layer #31/#32/#33, F1 living-codebase
#28/#29/#30/#24, and #34 presentation segregation via ancestry; #35 lands on top).
**Policy**: No TODO in any happy path. Every new method needs a grep-confirmed call site before its PR
opens. Tests assert observable output. **`SCHEMA_VERSION` bump `"5" → "6"` is owned by this feature**
(PR-1). Path/constant fallbacks match `KuzuConfig` and the locked `.sqlcg.toml` conventions. **No
backward compat — re-index is the migration path.** Small-repo experience must not regress: with no
`[sqlcg.external_consumers]` configured the graph, the indexer, and every tool behave byte-identically
to today. Injection is strictly opt-in.

---

## Summary

Today the graph models lineage only WITHIN the SQL corpus. Once data leaves a presentation-facing
table (a Tableau extract, an outbound feed, a BI dashboard, a `COPY INTO`/reverse-ETL sink), the trail
ends — `get_downstream_dependencies` and `diff_impact` report the presentation table as a terminal
leaf with no consumer. #35 lets a user declare external consumers in `.sqlcg.toml` and attach them to
presentation-facing tables, so lineage answers can show the named external destination as the final
downstream hop. The external consumers are persisted as first-class `ExternalConsumer` nodes joined by
a `CONSUMED_BY` edge from the presentation-facing `SqlTable`, ingested in a dedicated post-index pass
(never inside the per-file parse/upsert hot path), via the bulk upsert API.

This makes #34's `presentation_facing` bucket *precise*: a presentation table with declared consumers
is provably an egress point; one with none is a candidate orphan even inside the egress layer.

---

## Code-vs-Plan Verification

All evidence verified against the working tree on `feat/cluster-b-provenance` at commit `748918e`.

| Claim under test | Verified state (file:line evidence) | Verdict |
|------------------|-------------------------------------|---------|
| A `kind IN ['table','external']` filter exists and references an `external` value no emission site writes | [`queries.cypher:44`](../src/sqlcg/core/queries.cypher) `GET_UPSTREAM_DEPENDENCIES_FILTERED` and [`analyze.py:41`](../src/sqlcg/cli/commands/analyze.py) (`upstream`) + [`analyze.py:102`](../src/sqlcg/cli/commands/analyze.py) (`downstream`) all filter `WHERE t.kind IN ['table', 'external']`. **No indexer emission site writes `kind="external"`** — `_upsert_parsed_file` writes only `"table"` ([`indexer.py:931,1003,1071,1095`](../src/sqlcg/indexer/indexer.py)) and `"cte"` ([`indexer.py:1048`](../src/sqlcg/indexer/indexer.py)). `src_table.role` (`indexer.py:1003`) is `{table,cte,derived}` per `TableRef.role`; no `external` role is produced. | **CONFIRMED — `external` is a live but dormant hook.** #35 is its intended writer. |
| `SCHEMA_VERSION` is `"5"` and is the single source of the re-index gate | [`schema.py:6`](../src/sqlcg/core/schema.py) `SCHEMA_VERSION = "5"`. Gate enforced in [`index.py:186`](../src/sqlcg/cli/commands/index.py), [`reindex.py:167`](../src/sqlcg/cli/commands/reindex.py), [`watch.py:36`](../src/sqlcg/cli/commands/watch.py) — all compare `backend.get_schema_version() != SCHEMA_VERSION` and abort with a `db reset && db init && index` message. | **CONFIRMED — three gate sites, one constant.** |
| `#34` added `[sqlcg.presentation] schema_prefixes` + `presentation_facing` concept | [`config.py:238`](../src/sqlcg/core/config.py) `get_presentation_prefixes` (default `[]`). `analyze_unused` segregates into `presentation_facing` ([`tools.py:1600,1607`](../src/sqlcg/server/tools.py)); `diff_impact` flags `presentation_facing` ([`tools.py:949,968`](../src/sqlcg/server/tools.py)). Model `PresentationCandidate` at [`models.py:397`](../src/sqlcg/server/models.py); `UnusedTablesResult.presentation_facing` at [`models.py:426`](../src/sqlcg/server/models.py). | **CONFIRMED — egress boundary already named and reachable.** |
| The schema is defined in a `.cypher` DDL file and version-stamped in a transaction | [`schema.cypher`](../src/sqlcg/core/schema.cypher) holds all `CREATE NODE TABLE`/`CREATE REL TABLE`; `kuzu_backend.py:115-133` executes the DDL and upserts `SCHEMA_VERSION` in one transaction. `NodeLabel`/`RelType` are `StrEnum`s in [`schema.py:9-30`](../src/sqlcg/core/schema.py). | **CONFIRMED — node/rel additions are DDL + enum + version-bump.** |
| The indexer upsert path uses bulk upsert exclusively (perf invariant) | [`indexer.py:1108-1128`](../src/sqlcg/indexer/indexer.py) `_upsert_parsed_file` flushes via `upsert_nodes_bulk`/`upsert_edges_bulk` only. `index_repo` batches files through `_flush_batch` ([`indexer.py:289-335`](../src/sqlcg/indexer/indexer.py)); post-ingestion runs `_expand_star_sources` ([`indexer.py:342`](../src/sqlcg/indexer/indexer.py)) then persists `indexed_sha` ([`indexer.py:347-363`](../src/sqlcg/indexer/indexer.py)). | **CONFIRMED — a post-ingestion pass slots cleanly between star-expansion and sha-persist.** |
| Bulk upsert API shape for new node/edge labels | [`kuzu_backend.py:210`](../src/sqlcg/core/kuzu_backend.py) `upsert_nodes_bulk(label, rows)` requires every row carry the label's PK and homogeneous keys; [`kuzu_backend.py:245`](../src/sqlcg/core/kuzu_backend.py) `upsert_edges_bulk(src_label, dst_label, rel_type, rows)` requires `src_key`/`dst_key` per row. PK resolved via `_pk_field(label)` — defined ONCE on the shared base [`graph_db.py:192`](../src/sqlcg/core/graph_db.py) as a `@staticmethod` `match` (`REPO`/`FILE`→`"path"`, `TABLE`→`"qualified"`, **`_` default → `"id"`**), used by BOTH `kuzu_backend.py` and `neo4j_backend.py`. | **CONFIRMED — and critical: `ExternalConsumer` PK is `name`, which is NOT `id`. Without an explicit `case`, the default returns `"id"` and the bulk upsert would target a non-existent column. Step 1.3 MUST add an explicit `case NodeLabel.EXTERNAL_CONSUMER: return "name"` in `graph_db.py`, not `kuzu_backend.py`.** |
| `get_downstream_dependencies` traverses `COLUMN_LINEAGE` only, column-to-column | [`tools.py:1124`](../src/sqlcg/server/tools.py) runs `GET_DOWNSTREAM_DEPENDENCIES_QUERY` = `MATCH (src:SqlColumn)-[:COLUMN_LINEAGE]->(dst:SqlColumn)` ([`queries.cypher:34`](../src/sqlcg/core/queries.cypher)). Terminal columns produce the "may be a terminal output" hint ([`tools.py:1150`](../src/sqlcg/server/tools.py)). `DependencyNode` has `name/kind/table` ([`models.py:120`](../src/sqlcg/server/models.py)) — `kind` is a free string, so `kind="external_consumer"` needs no model change. | **CONFIRMED — egress hop is a table-level append after the column closure, not a new column-edge type.** |
| Config readers are pure functions over `.sqlcg.toml`, defaulting to empty/benign with `try/except pass` | `get_presentation_prefixes` ([`config.py:238`](../src/sqlcg/core/config.py)), `get_schema_aliases` ([`config.py:96`](../src/sqlcg/core/config.py)), `get_ignored_tables` ([`config.py:166`](../src/sqlcg/core/config.py)) all follow `[sqlcg.<section>]` → list/dict, lowercased, default empty. | **CONFIRMED — `get_external_consumers` follows this exact template.** |
| Schema has a "backward-compatible aliases" comment that contradicts the no-compat rule | [`schema.py:32-47`](../src/sqlcg/core/schema.py) keeps `NODE_*`/`REL_*` aliases. Pre-existing; **out of scope** — #35 adds new enum members + aliases consistently with the existing pattern, does not refactor it. | **NOTED, NOT TOUCHED.** |

---

## KEY DECISIONS

### Decision 1 — Manifest format and location: a `[sqlcg.external_consumers]` section in `.sqlcg.toml`

**Decision**: Declare external consumers inline in `.sqlcg.toml` under `[sqlcg.external_consumers]`,
read by a new `get_external_consumers(path) -> list[ExternalConsumerSpec]` in
[`config.py`](../src/sqlcg/core/config.py), matching the locked convention of every other config reader
(`get_presentation_prefixes`, `get_schema_aliases`, `get_ignored_tables`). **No separate manifest file.**

**Format** (TOML array-of-tables — the only `.sqlcg.toml` shape that carries per-entry fields):

```toml
[[sqlcg.external_consumers]]
name = "Tableau: Sales Dashboard"
kind = "tableau"                       # free-form category label, lowercased
consumes = ["ia_sales.fct_orders", "ia_sales.dim_customer"]

[[sqlcg.external_consumers]]
name = "Reverse-ETL: HubSpot sync"
kind = "reverse_etl"
consumes = ["ia_marketing.audience_export"]
```

**Justification**:
- **Convention alignment** — every existing config knob lives in `.sqlcg.toml` under `[sqlcg.*]`; a
  separate file (e.g. `consumers.json` referenced by a path key) would introduce a second config
  surface, a path-resolution fallback (which must match `KuzuConfig` — it has no such path), and a new
  failure mode. Inline TOML reuses the exact `tomllib.load` + `try/except pass` + default-empty pattern
  at [`config.py:256-266`](../src/sqlcg/core/config.py).
- **Zero friction for small repos** — absent the section, `get_external_consumers` returns `[]` and the
  ingestion pass is a no-op (Decision 3's wiring). A 20-ETL user never sees it.
- **`consumes` references qualified table names**, matched case-insensitively against
  `SqlTable.qualified` exactly like `get_ignored_tables` matches `schema.table` ([`config.py:181`](../src/sqlcg/core/config.py)).
- **Validation policy** — a `consumes` entry that matches NO indexed table is reported as a warning in
  the index summary (observable, testable), not a hard error: the manifest can legitimately reference a
  table that was renamed/removed, and a hard failure would break the small-repo "just works" promise.
  A consumer with an empty/missing `name` or `consumes` is skipped with a warning.

> **Open sub-decision for the plan-reviewer**: should an external consumer be allowed to attach to a
> NON-presentation table (one not matching any `[sqlcg.presentation]` prefix)? **Recommendation: yes,
> but emit a warning.** The egress boundary is *conceptually* the presentation layer, but enforcing it
> would couple #35 to a non-empty `[sqlcg.presentation]` config and break the case where a user declares
> consumers without declaring prefixes. We attach the edge regardless and warn when the target is not
> presentation-facing, so the data is never silently dropped. See PR-1 Step 1.5.

### Decision 2 — Dedicated `ExternalConsumer` node label, NOT a reused `external`-kind `SqlTable`

**Decision**: Add a new node label `ExternalConsumer` (PK `name`) and a new edge `CONSUMED_BY`
(`SqlTable -> ExternalConsumer`). **Do not** model external consumers as `SqlTable` rows with
`kind="external"`.

**Justification** (the `kind IN ['table','external']` hook from #33 informs but does not bind this):
- **Semantics** — a `SqlTable` row is keyed by `qualified` (`schema.table`) and carries `catalog/db/
  name/defined_in_file`. An external consumer ("Tableau: Sales Dashboard") has no schema-qualified name,
  no catalog, no defining SQL file. Forcing it into `SqlTable` would require synthetic `qualified`
  values, polluting `analyze_unused` (every consumer would surface as a zero-consumer table),
  `get_hub_ranking`, and `find_definition`.
- **The `external` kind hook is for a different thing** — #33 reserved `kind="external"` on `SqlTable`
  for *upstream* tables that are referenced but not defined in the corpus (an external SOURCE), so the
  `upstream`/`downstream` filters keep them while dropping `cte`/`derived`. #35 is about external
  *destinations*. Conflating source-external and sink-external in one `kind` value would make the
  existing filter ambiguous. **We leave `kind="external"` reserved for its #33 meaning and add a clean
  node label for sinks.** (No emission site writes `external` today, so nothing regresses; a future
  v1.2 source-external feature can still claim it.)
- **Query clarity** — a dedicated label lets the downstream traversal emit
  `DependencyNode(kind="external_consumer", name=consumer.name)` as a distinct terminal type, and lets
  `analyze_unused` answer "this presentation table HAS a declared consumer" via a single
  `OPTIONAL MATCH (t)-[:CONSUMED_BY]->()` without string-matching on `kind`.
- **Cleanest blast-radius story** — `diff_impact` can report external consumers in the blast radius as a
  named list distinct from `affected_tables`.

### Decision 3 — Split into TWO sub-PRs on `feat/cluster-b-provenance`

**Decision**: Two serialized PRs, mirroring sprint_13's PR-2→PR-3 ride-on pattern. **PR-1 owns the
schema bump + persistence + ingestion; PR-2 surfaces it in the query/tool layer.**

| | PR-1 — Schema, config, ingestion | PR-2 — Query/tool surfacing |
|---|---|---|
| Owns `SCHEMA_VERSION "5"→"6"` | **Yes** | rides on it |
| Files | `schema.py`, `schema.cypher`, `config.py`, `indexer.py`, `kuzu_backend.py` (`_pk_field`), `index.py` (warning surfacing) | `queries.cypher`, `queries.py`, `tools.py`, `models.py`, `analyze.py`, `skill.py` |
| Deliverable alone | Persists `ExternalConsumer` nodes + `CONSUMED_BY` edges; verifiable via `execute_cypher`/`db info` node counts | Makes the edges visible in `get_downstream_dependencies`, `diff_impact`, `analyze_unused`, CLI `downstream` |
| Risk | MED — DDL + forced re-index + new ingestion pass | LOW — read-side traversal append + model field |
| Hot-path risk | NONE if ingestion is a separate post-index pass (enforced by PR-1 Step 1.4 + perf test) | NONE — read queries only |

**Why split**: PR-1 is a schema migration (re-index forcing) plus an ingestion subsystem; PR-2 is pure
read-side. Bundling would produce one large PR mixing a `SCHEMA_VERSION` bump with query changes and
make the perf/bulk-upsert invariant review harder to isolate. Splitting lets PR-1's perf invariant be
gated independently before any tool change lands. They serialize on the same branch (both touch
adjacent concerns); do NOT parallelize — `tools.py`/`models.py` are PR-2-only, `indexer.py` is PR-1-only,
so the only shared file is conceptual (`queries.cypher` gains the `CONSUMED_BY` query in PR-2). **PR-1
first** because PR-2's traversal needs the persisted edges to test against.

---

## Scope

### In Scope
- New `[sqlcg.external_consumers]` array-of-tables section in `.sqlcg.toml` + `get_external_consumers`
  reader in `config.py` (default `[]`).
- New `ExternalConsumer` node label (PK `name`, props `kind`, `consumer_type`) + `CONSUMED_BY` rel
  (`SqlTable -> ExternalConsumer`) in `schema.cypher` + `NodeLabel`/`RelType` enums.
- `SCHEMA_VERSION "5" → "6"` and the `_pk_field` registration for `ExternalConsumer`.
- A dedicated `_ingest_external_consumers(db, path)` pass in `index_repo`, after `_expand_star_sources`,
  before `set_indexed_sha`, using `upsert_nodes_bulk`/`upsert_edges_bulk` only.
- Surfacing in `get_downstream_dependencies` (table-level egress hop appended after the column closure),
  `diff_impact` (`external_consumers` field), `analyze_unused` (`has_external_consumer` flag on
  `PresentationCandidate`), and CLI `analyze downstream`.
- Skill-doc update so the LLM knows external consumers are injected egress facts.
- Index-summary warnings for unmatched/invalid manifest entries.

### Non-Goals
- Auto-discovery of consumers (parsing Tableau workbooks, BI catalogs) — manifest-declared only (v1.2).
- Column-level external lineage (`CONSUMED_BY` is table→consumer, not column→consumer). v1.2.
- External *upstream* sources via `kind="external"` on `SqlTable` — reserved, separate feature (v1.2).
- A standalone `sqlcg consumers` CLI verb. Ingestion is folded into `index`/`reindex`; no new top-level
  command in v1.1.0. (A read-only `analyze consumers` listing is optional polish, see PR-2 Step 2.6 —
  gated, may defer.)
- Watch-mode live re-ingestion of the manifest on `.sqlcg.toml` change (v1.2).

---

## Design

### Data Model

```
CREATE NODE TABLE ExternalConsumer (
    name STRING PRIMARY KEY,     -- "Tableau: Sales Dashboard" (the [[...]] name)
    consumer_type STRING         -- the manifest `kind`, lowercased: "tableau" | "reverse_etl" | ...
);

CREATE REL TABLE CONSUMED_BY (
    FROM SqlTable TO ExternalConsumer
);
```

- PK is `name` (human-declared, unique per manifest). `consumer_type` is a separate prop so we do NOT
  reuse the word `kind` (which is overloaded across `SqlTable.kind` and `SqlQuery.kind`).
- `CONSUMED_BY` carries no edge properties (the direction and the two endpoints are the full fact).

### Config Reader (PR-1)

```python
class ExternalConsumerSpec(BaseModel):
    name: str
    consumer_type: str          # lowercased manifest `kind`
    consumes: list[str]         # lowercased qualified table names

def get_external_consumers(path: Path) -> list[ExternalConsumerSpec]:
    # reads [[sqlcg.external_consumers]]; default [] on absent/malformed; try/except pass
```

Mirrors `get_presentation_prefixes` exactly: `tomllib.load`, navigate `sqlcg.external_consumers`,
lowercase `consumes` entries and `kind`, skip malformed rows, default `[]`.

### Ingestion Pass (PR-1) — where it runs and why it is safe

`_ingest_external_consumers(self, db, path) -> dict` is called from `index_repo` **once**, immediately
after `star_edges_expanded = self._expand_star_sources(db)` ([`indexer.py:342`](../src/sqlcg/indexer/indexer.py))
and before the `set_indexed_sha` block. It:
1. `specs = get_external_consumers(path)`; if empty, return `{"consumers": 0, "edges": 0, "warnings": []}`
   immediately (small-repo no-op).
2. Build `consumer_rows` (one per spec) and `consumed_by_edges` (one per `(table, consumer)` pair).
3. For each `consumes` target, check it exists as a `SqlTable` (single `MATCH ... RETURN count`
   aggregation, NOT per-row in a loop body that touches parsing) — accumulate unmatched targets and
   non-presentation targets into a warnings list.
4. `db.upsert_nodes_bulk(NodeLabel.EXTERNAL_CONSUMER, consumer_rows)` then
   `db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.EXTERNAL_CONSUMER, RelType.CONSUMED_BY, consumed_by_edges)`.

**Performance invariant compliance**: this pass runs ONCE per index, OUTSIDE `_flush_batch`,
`_upsert_parsed_file`, and `_extract_column_lineage`. It adds ZERO ops to the per-file parse loop, the
per-column lineage loop, or the per-edge upsert-row loop. It uses the bulk API exclusively — never
`upsert_node`/`upsert_edge`. Existence-checking is one aggregation query per target via `UNWIND` (a
single round-trip), not a Python per-row `execute`. (See Test T35-PERF.)

### Query Surfacing (PR-2)

New blocks in [`queries.cypher`](../src/sqlcg/core/queries.cypher):

```
-- GET_TABLE_EXTERNAL_CONSUMERS
MATCH (t:SqlTable {qualified: $table_qualified})-[:CONSUMED_BY]->(e:ExternalConsumer)
RETURN e.name AS name, e.consumer_type AS consumer_type

-- COUNT_EXTERNAL_CONSUMERS
MATCH ()-[r:CONSUMED_BY]->() RETURN count(r) AS n

-- ANALYZE_UNUSED_TABLES (extend with consumer flag — see PR-2 Step 2.4)
MATCH (t:SqlTable)
WHERE NOT (t)<-[:SELECTS_FROM]-()
OPTIONAL MATCH (t)-[c:CONSUMED_BY]->()
RETURN t.qualified AS table_qualified, count(c) AS external_consumer_count
ORDER BY t.qualified
```

`get_downstream_dependencies` (table or column root): after the existing `COLUMN_LINEAGE` closure
completes, roll the terminal columns up to their tables (reuse `_rollup_to_tables`, present at
`tools.py`), then for each terminal table run `GET_TABLE_EXTERNAL_CONSUMERS` and append a
`DependencyNode(name=consumer.name, kind="external_consumer", table=terminal_table)` for each. This is a
bounded append (number of terminal tables × consumers), outside the 50k column-traversal loop.

`diff_impact` gains `external_consumers: list[str]` — the union of consumer names attached to any
`affected_tables` entry.

`analyze_unused`: `PresentationCandidate` gains `has_external_consumer: bool` populated from the
extended `ANALYZE_UNUSED_TABLES` query, so the LLM can distinguish a presentation table with a declared
egress (provable egress point) from one without (candidate orphan even in the egress layer).

### Migration / Re-index Gate (owned here)

- `schema.py:6` `SCHEMA_VERSION = "5" → "6"`.
- The three existing gate sites ([`index.py:186`](../src/sqlcg/cli/commands/index.py),
  [`reindex.py:167`](../src/sqlcg/cli/commands/reindex.py), [`watch.py:36`](../src/sqlcg/cli/commands/watch.py))
  need **no code change** — they compare against the imported `SCHEMA_VERSION` constant. After the bump,
  any v5 graph triggers the existing message: `Database schema is v5; this build requires v6. Run
  'sqlcg db reset && sqlcg db init && sqlcg index <path>' to re-index.` This is the migration path; no
  data migration code is written (no backward compat).
- `db info` ([`db.py:79`](../src/sqlcg/cli/commands/db.py)) reports the version from `get_schema_version`
  — automatically shows `6` post-reindex.

---

## Ticket / PR Table

| PR | Ticket | Title | Files | Owns schema bump |
|----|--------|-------|-------|------------------|
| PR-1 | #35a | Schema + config + ingestion pass | schema.py, schema.cypher, config.py, graph_db.py (`_pk_field`), indexer.py, index.py | **Yes (5→6)** |
| PR-2 | #35b | Query + tool + CLI surfacing | queries.cypher, queries.py, tools.py, models.py, analyze.py, skill.py | rides on it |

---

## Implementation Steps

### Phase 1 — PR-1: Schema, config, ingestion

**Step 1.1 — Bump schema version.**
- Files: [`schema.py:6`](../src/sqlcg/core/schema.py).
- `SCHEMA_VERSION = "5"` → `"6"`.
- Acceptance: `db info` on a fresh re-indexed graph reports `6`; an existing v5 graph triggers the
  re-index gate message on next `index`.

**Step 1.2 — Add node label + rel type enums and DDL.**
- Files: [`schema.py`](../src/sqlcg/core/schema.py) (add `EXTERNAL_CONSUMER = "ExternalConsumer"` to
  `NodeLabel`, `CONSUMED_BY = "CONSUMED_BY"` to `RelType`, plus matching `NODE_*`/`REL_*` aliases for
  consistency with the existing block), [`schema.cypher`](../src/sqlcg/core/schema.cypher) (append the
  two `CREATE` statements from Design § Data Model).
- Acceptance: `init_schema` on a clean DB succeeds; `execute_cypher("MATCH (e:ExternalConsumer) RETURN
  count(e)")` returns `0` (table exists, empty).

**Step 1.3 — Register the new PK in `_pk_field`.**
- Files: [`graph_db.py:192`](../src/sqlcg/core/graph_db.py) `_pk_field` (the SHARED base `@staticmethod`,
  NOT `kuzu_backend.py` — that file only calls it). **Add an explicit `case NodeLabel.EXTERNAL_CONSUMER:
  return "name"` BEFORE the `case _: return "id"` default.** Without it the default returns `"id"`,
  and every `ExternalConsumer` bulk upsert would `MERGE (n:ExternalConsumer {id: ...})` against a
  column that does not exist → silent wrong-key or runtime error.
- Acceptance: `upsert_nodes_bulk(NodeLabel.EXTERNAL_CONSUMER, [{"name": "X", "consumer_type": "t"}])`
  succeeds and `execute_cypher("MATCH (e:ExternalConsumer {name:'X'}) RETURN e.consumer_type")`
  returns `"t"`.

**Step 1.4 — Config reader.**
- Files: [`config.py`](../src/sqlcg/core/config.py) — add `ExternalConsumerSpec(BaseModel)` and
  `get_external_consumers(path) -> list[ExternalConsumerSpec]`.
- Mirror `get_presentation_prefixes` structure: `tomllib.load`, `try/except pass`, default `[]`,
  lowercase `consumes` + `consumer_type`. Skip rows missing `name` or with empty `consumes`.
- Acceptance: a `.sqlcg.toml` with two `[[sqlcg.external_consumers]]` tables yields two specs with
  lowercased `consumes`; an absent section yields `[]`; a malformed section yields `[]` (no exception).

**Step 1.5 — Ingestion pass.**
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py) — add `_ingest_external_consumers(self, db,
  path)`; call it in `index_repo` immediately after `star_edges_expanded = self._expand_star_sources(db)`
  (line 342) and before the `set_indexed_sha` block (line 347).
- Build rows/edges; existence-check targets via one `UNWIND $names AS n MATCH (t:SqlTable {qualified:
  n}) RETURN n` aggregation; accumulate `unmatched` and (using `get_presentation_prefixes(path)`)
  `non_presentation` warning lists; bulk-upsert nodes then edges.
- Add `external_consumers`, `external_consumer_edges`, and `external_consumer_warnings` keys to the
  `index_repo` return dict (line ~392).
- **Grep-confirmed call site required**: `_ingest_external_consumers` must be called from `index_repo`
  before PR opens.
- Acceptance: indexing a fixture repo with a manifest referencing two real tables persists two
  `ExternalConsumer` nodes and the `CONSUMED_BY` edges (asserted via `execute_cypher` count); the return
  dict reports `external_consumers == 2`.

**Step 1.6 — Surface warnings in the index summary.**
- Files: [`index.py`](../src/sqlcg/cli/commands/index.py) `_run_index` — after the index call, print a
  yellow warning line per unmatched target and per non-presentation attachment (unless `--quiet`).
- Acceptance: indexing a manifest that references a non-existent table prints `Warning: external
  consumer 'X' references unknown table 'y.z'` to console (asserted in an e2e/integration CLI test).

### Phase 2 — PR-2: Query + tool + CLI surfacing

**Step 2.1 — Add Cypher query blocks + loader constants.**
- Files: [`queries.cypher`](../src/sqlcg/core/queries.cypher) (`GET_TABLE_EXTERNAL_CONSUMERS`,
  `COUNT_EXTERNAL_CONSUMERS`, extend `ANALYZE_UNUSED_TABLES`), [`queries.py`](../src/sqlcg/core/queries.py)
  (`GET_TABLE_EXTERNAL_CONSUMERS_QUERY`, `COUNT_EXTERNAL_CONSUMERS_QUERY`).
- Acceptance: `from sqlcg.core.queries import GET_TABLE_EXTERNAL_CONSUMERS_QUERY` imports; the loader
  parses the new blocks (existing `_load` covers it).

**Step 2.2 — Surface in `get_downstream_dependencies`.**
- Files: [`tools.py`](../src/sqlcg/server/tools.py) `get_downstream_dependencies`.
- After the column closure (line ~1146), roll terminal columns to tables, run
  `GET_TABLE_EXTERNAL_CONSUMERS_QUERY` per terminal table, append
  `DependencyNode(name=..., kind="external_consumer", table=...)`. Adjust the empty-result hint
  (line 1150) so it is only shown when there are also no external consumers.
- Acceptance: tracing downstream from a column whose table has a `CONSUMED_BY` edge returns a node with
  `kind="external_consumer"` and the consumer name; tracing a column with no consumer is byte-identical
  to today.

**Step 2.3 — Surface in `diff_impact`.**
- Files: [`models.py`](../src/sqlcg/server/models.py) (`DiffImpactResult.external_consumers:
  list[str]`), [`tools.py`](../src/sqlcg/server/tools.py) `diff_impact` (union consumer names over
  `affected_tables`; line ~964 result construction).
- Acceptance: `diff_impact` over a changed file whose downstream blast radius reaches a consumed table
  lists that consumer in `external_consumers`; empty when no consumers exist.

**Step 2.4 — Surface in `analyze_unused`.**
- Files: [`models.py`](../src/sqlcg/server/models.py) (`PresentationCandidate.has_external_consumer:
  bool = False`), [`tools.py`](../src/sqlcg/server/tools.py) `analyze_unused` (read
  `external_consumer_count` from the extended query; set the flag).
- Acceptance: a presentation-facing table with a `CONSUMED_BY` edge reports `has_external_consumer=True`;
  one without reports `False`; non-presentation behaviour unchanged.

**Step 2.5 — CLI `analyze downstream` egress hop.**
- Files: [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) `downstream`.
- After the column-level results, append external-consumer rows for terminal tables (display only,
  respects `--raw`). Reuse `GET_TABLE_EXTERNAL_CONSUMERS_QUERY`.
- Acceptance: `sqlcg analyze downstream <col>` on a consumed terminal prints the external consumer
  name as a final row.

**Step 2.6 — (Optional, may defer) `analyze consumers` listing + skill doc.**
- Files: [`skill.py`](../src/sqlcg/server/skill.py) (`_WORKFLOWS`/tool table mention that
  `get_downstream_dependencies` and `analyze_unused` now surface declared external egress).
- Skill update is REQUIRED; the standalone `analyze consumers` verb is optional polish.
- Acceptance: the skill string mentions external consumers / egress injection.

---

## Wiring Checklist (grep-confirmed before each PR opens)

PR-1:
- [ ] `_ingest_external_consumers` defined AND called from `index_repo`:
      `grep -n "_ingest_external_consumers" src/sqlcg/indexer/indexer.py` ≥ 2 hits (def + call).
- [ ] `get_external_consumers` defined AND called from `_ingest_external_consumers`:
      `grep -n "get_external_consumers" src/sqlcg/` ≥ 2 hits.
- [ ] `NodeLabel.EXTERNAL_CONSUMER` / `RelType.CONSUMED_BY` referenced in `indexer.py` upsert calls.
- [ ] `_pk_field` in `graph_db.py` has an explicit `case NodeLabel.EXTERNAL_CONSUMER: return "name"`
      BEFORE the `case _: return "id"` default (grep the mapping in graph_db.py).
- [ ] `SCHEMA_VERSION = "6"` (grep, exactly one definition).
- [ ] No `upsert_node(`/`upsert_edge(` (singular) introduced anywhere in `_ingest_external_consumers`.
- [ ] No new op inside `_extract_column_lineage`, `_upsert_parsed_file`, or `_flush_batch`
      (`git diff` shows zero changes to those method bodies).

PR-2:
- [ ] `GET_TABLE_EXTERNAL_CONSUMERS_QUERY` defined in `queries.py` AND used in `tools.py`/`analyze.py`:
      `grep -rn "GET_TABLE_EXTERNAL_CONSUMERS_QUERY" src/` ≥ 3 hits.
- [ ] `DependencyNode(... kind="external_consumer"` constructed in `get_downstream_dependencies`.
- [ ] `external_consumers=` populated in `diff_impact` result; field exists on `DiffImpactResult`.
- [ ] `has_external_consumer=` populated in `analyze_unused`; field exists on `PresentationCandidate`.
- [ ] skill string contains "external consumer" or "egress".

---

## Test Strategy

Tests assert observable output (node/edge counts, returned model fields, printed lines), never
"no exception".

### Unit
- **T35-CFG-1** (`test_config_external_consumers.py`): two `[[sqlcg.external_consumers]]` tables →
  two specs, `consumes` lowercased, `consumer_type` lowercased.
- **T35-CFG-2**: absent section → `[]`; malformed (string instead of array) → `[]` (no raise);
  row missing `name` or empty `consumes` → skipped.

### Integration (real in-memory KuzuDB)
- **T35-IDX-1**: index a fixture repo + manifest referencing two real defined tables → `execute_cypher`
  confirms 2 `ExternalConsumer` nodes and 2+ `CONSUMED_BY` edges; `index_repo` return dict reports the
  counts.
- **T35-IDX-2 (small-repo no-manifest safety)**: index the SAME fixture repo WITHOUT a manifest →
  `ExternalConsumer` node count is `0`, `CONSUMED_BY` count is `0`, and the rest of the graph
  (table/column/edge counts) is byte-identical to a control index run. This is the explicit
  "20-ETL user must not regress" gate.
- **T35-IDX-3 (warnings)**: manifest references one real and one unknown table → graph has 1 valid
  `CONSUMED_BY` edge; the return dict's warning list names the unknown target.
- **T35-DOWN-1**: `get_downstream_dependencies` on a column whose table has a consumer → result includes
  a `DependencyNode(kind="external_consumer")` with the consumer name. Control: a column with no
  consumer returns the unchanged terminal hint.
- **T35-DIFF-1**: `diff_impact` whose blast radius reaches a consumed table lists the consumer in
  `external_consumers`; control with no consumers → empty list.
- **T35-UNUSED-1**: a presentation-facing table WITH a consumer → `has_external_consumer=True`; WITHOUT
  → `False`. Control: non-presentation behaviour and the `candidates`/`presentation_facing` split from
  #34 unchanged.

### Perf / invariant
- **T35-PERF** (extends [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py) /
  [`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py)):
  - Assert `_ingest_external_consumers` issues only bulk calls — patch/spy on the backend so
    `upsert_node`/`upsert_edge` (singular) call counts are `0` and `upsert_nodes_bulk`/
    `upsert_edges_bulk` are each called at most once per ingest.
  - Assert ingest cost does NOT scale with corpus size: with N consumers attached and the fixture
    parsed at file-count N and 2N, the per-file parse-loop op counts (the existing guard's axes) are
    unchanged vs. the no-manifest control — i.e. the manifest pass adds zero per-file ops.
  - Behavioural assertion (matching the CLAUDE.md guidance): the existence-check is a single `UNWIND`
    round-trip, not one `run_read` per target (spy on `run_read` call count = O(1), not O(targets)).

### E2E (CLI)
- **T35-E2E-1**: `sqlcg index <fixture>` with a manifest then `sqlcg analyze downstream <col>` prints
  the external consumer as a terminal row. `sqlcg db info` reports schema version `6`.
- **T35-E2E-2 (re-index gate)**: a v5 graph + `sqlcg index` prints the `requires v6 ... db reset` gate
  message and exits non-zero.

---

## Acceptance Criteria

PR-1:
- [ ] `SCHEMA_VERSION == "6"`; v5 graphs trigger the existing re-index gate at all three sites.
- [ ] `ExternalConsumer` node table and `CONSUMED_BY` rel table created by `init_schema`.
- [ ] `get_external_consumers` reads `[[sqlcg.external_consumers]]`, defaults `[]`, never raises.
- [ ] `_ingest_external_consumers` runs once per index, after star-expansion, before sha-persist, using
      bulk upsert only; grep-confirmed call site in `index_repo`.
- [ ] No-manifest index is byte-identical to pre-#35 (T35-IDX-2 green).
- [ ] Unmatched/non-presentation targets surface as warnings, not failures.
- [ ] Perf invariant: zero new ops in `_extract_column_lineage`/`_upsert_parsed_file`/`_flush_batch`;
      T35-PERF green.

PR-2:
- [ ] `get_downstream_dependencies` appends `kind="external_consumer"` terminal nodes for consumed
      tables; unchanged for unconsumed columns.
- [ ] `diff_impact` reports `external_consumers`; `analyze_unused` reports `has_external_consumer`.
- [ ] CLI `analyze downstream` prints external consumers as terminal rows (respects `--raw`).
- [ ] Skill doc mentions external/egress consumers.
- [ ] `pyright` clean, `ruff` clean, full suite (excl. e2e) green.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Ingestion pass accidentally placed inside `_flush_batch` / per-file loop → O(N) regression | PR-1 Step 1.5 fixes the call site (post-star-expansion, once); T35-PERF gates it; wiring checklist greps for zero diff in the three hot methods. |
| Reusing `kind="external"` would pollute `analyze_unused`/`hub_ranking` | Decision 2: dedicated node label; `external` kind stays reserved for #33's source-external meaning. |
| Manifest references a renamed/removed table → hard failure breaks small-repo "just works" | Warnings, not errors (Decision 1 validation policy; T35-IDX-3). |
| Schema bump forces re-index for users who do not use the feature | Documented as the migration path (no backward compat rule). The bump is unavoidable once new tables exist; the existing gate message already guides the user. |
| Existence-check done per-target with N `run_read` calls → O(N) round-trips | Single `UNWIND` aggregation; T35-PERF asserts `run_read` is O(1) in target count. |
| `non_presentation` attachment silently couples #35 to a non-empty `[sqlcg.presentation]` | Decision 1 sub-decision: attach regardless, warn — surfaced for plan-reviewer confirmation. |

### Blocking Questions

None blocking. One sub-decision (Decision 1: allow non-presentation attachment with a warning) is
flagged for the plan-reviewer to confirm or override; the recommended default (attach + warn) is
implementable as planned and does not block PR-1.
