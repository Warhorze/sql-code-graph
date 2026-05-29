# Feature Plan: First-User Findings + Table-Identity Case Bug

## Summary

Fix the critical case-sensitivity bug that splits one physical table into multiple
phantom graph nodes (breaking lineage), plus six first-user UX findings on the MCP
lineage tools surfaced by the [`e2e_firstuser_report.md`](../e2e_firstuser_report.md)
run against the 1,340-file DWH repo.

## Architecture Alignment

- `ARCHITECTURE_REVIEW.md` §2.6 already documents the per-dialect `_normalize_key`
  case-folding hook on `SchemaResolver` and notes that the schema side lowercases
  catalog/db/table/column (verified: [`schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
  lines 217-220, 328-331). The graph-key side (`TableRef`/`ColumnRef.full_id`) does
  **not** lowercase. This plan closes that gap so graph keys align with schema keys.
- `ARCHITECTURE_REVIEW.md` line 1846 tracks finding #12 "`find table` case-sensitivity
  (Open)". Category 2 of this plan resolves it. The plan stays within that documented
  priority — no new architecture decision is required.
- No backward compatibility: `sqlcg db reset` + reindex is the migration path
  (per `CLAUDE.md`). Documented in §Rollout below.

### Blocking Questions

None. The case-folding direction (lowercase) is already fixed by the schema side,
so the normalization target is unambiguous.

## Scope

### In Scope

- **C2 (Critical)**: Normalize `TableRef` identity components to lowercase so the
  graph primary keys (`Table.qualified`, `SqlColumn.id`) collapse case variants of the
  same physical table into one node.
- **C2b**: Verify schema aliases are applied to scripting-mode source refs (the
  observed `BA_TMP.WTFE_...` survivor).
- **F1**: Document the required `schema.table.column` format in tool descriptions and
  improve the empty-lineage hint to name the missing schema prefix.
- **F2**: Dedup the `lineage` / `nodes` lists before returning from the four traversal
  tools.
- **F3**: Surface the owning table on each `LineageNode` (and `DependencyNode`) using
  `SqlColumn.table_qualified`, already stored in the graph.
- **F4**: Distinguish "no downstream consumers (terminal)" from "lookup failed" in the
  downstream hint.
- **F5 (investigate)**: Measure the duplicate-query-node claim and fix only if a real
  duplication exists; otherwise record the measurement and close.
- **F6**: Lower the log level of the benign cross-file `tmp_*` / DDL-collision warning.

### Non-Goals

- Indexing throughput rewrite (F5 perf budget). This plan only investigates the
  duplicate-node lead; a full perf sprint is out of scope.
- MERGE-branch lineage, star-expansion accuracy, or schema-CSV ingestion changes.
- Changing the on-disk schema shape (column property set is unchanged; we only read
  an existing property in the Cypher RETURN clauses).
- Case-insensitive *matching* of user input in `find_table_usages` beyond what C2
  requires. (See §Cross-cutting note — C2 lowercases `name`, so the tool input must be
  lowercased to match; this is included, but no fuzzy matching is added.)

## Design

### C2 — Where to normalize (decision)

Normalize the **identity components** of `TableRef` at construction via
`__post_init__`, lowercasing `catalog`, `db`, and `name`. Rationale:

- `full_id` and `qualified` derive from these components, so the graph keys
  (`indexer.py` lines 514, 550, 600-601, 661) become lowercase automatically.
- `ColumnRef.full_id` is `f"{table.full_id}.{name}"`; the column name part must also
  be lowercased, so `ColumnRef` gets the same `__post_init__` treatment on `name`.
- The `SqlTable.name` property (used by `FIND_TABLE_USAGES` `MATCH (t:SqlTable {name:$name})`)
  also becomes lowercase, keeping table-name lookups internally consistent with the
  lowercase schema side (§2.6). The MCP `find_table_usages` tool must lowercase its
  `table_name` argument to match — included as Step 1.4.
- `alias` is **not** an identity field and is **not** lowercased (it is only used for
  star/qualifier matching, which already lowercases on both sides — see
  `_resolve_star_source`, base.py lines 1091-1094).

`TableRef` and `ColumnRef` are `@dataclass(frozen=True)`. Frozen dataclasses cannot
assign in `__post_init__` directly; use `object.__setattr__` (standard frozen-dataclass
pattern) to set the lowercased values.

Confidence-scoring alignment: `mapping_schema_tables` (base.py lines 648-654) is built
from the already-lowercase `mapping_schema`. Today, source refs carry original case, so
`schema_key in mapping_schema_tables` silently misses for upper-case Snowflake refs and
mislabels confidence as 0.7. Lowercasing components fixes this latent bug as a bonus —
call this out in the test assertions.

### C2b — Scripting-mode alias survivor

`_apply_table_alias` (base.py lines 407-416) only remaps when `ref.db` matches a key in
`self._schema_aliases` (keyed lowercase). The observed `BA_TMP.WTFE_...` survivor means
either (a) `ba_tmp` is not in the configured `schema_aliases`, or (b) the scripting-mode
DML path constructs a ref whose `_apply_table_alias` is never called. Investigation step:
confirm via grep whether `_parse_scripting_file` → `AnsiParser._parse_statement` runs the
same `_real_tables` / `_convert_table_expr_to_ref` → `_apply_table_alias` chain. If the
scripting path bypasses alias application, route its source refs through
`_apply_table_alias`. If `ba_tmp` simply is not configured, document that aliasing is a
config concern (not a code bug) and that lowercase normalization alone collapses the
case variants (`BA_TMP` vs `ba_tmp`) into one node — the remaining `ba_tmp` vs `ba`
collapse is the user's `schema_aliases` responsibility.

### API Changes (MCP tool surface)

- `LineageNode` ([`models.py`](../src/sqlcg/server/models.py)): add
  `table: str | None = Field(None, description="Qualified table the column belongs to (schema.table)")`.
  Keep `name` as the bare column name (back-compatible field set; analysts get table
  context without breaking existing consumers).
- `DependencyNode` ([`models.py`](../src/sqlcg/server/models.py)): add the same
  `table: str | None` field.
- Tool docstrings for `trace_column_lineage`, `get_upstream_dependencies`,
  `get_downstream_dependencies` ([`tools.py`](../src/sqlcg/server/tools.py)): change the
  `table_col` arg doc to state the **schema-qualified** form is required, e.g.
  `"ba.table_name.column_name"`.

### Data Models / Cypher

No node-property additions. The four traversal queries in
[`queries.cypher`](../src/sqlcg/core/queries.cypher) already select `col_name`; add
`table_qualified` to their RETURN:

```
-- TRACE_COLUMN_LINEAGE / GET_UPSTREAM_DEPENDENCIES
RETURN src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified
-- GET_DOWNSTREAM_DEPENDENCIES
RETURN dst.id AS id, dst.col_name AS col_name, dst.table_qualified AS table_qualified
```

`table_qualified` is populated on every `SqlColumn` row at index time
(indexer.py lines 555, 606, 616) and by the star-expansion `MERGE` in `queries.cypher`,
so the property is always present — no null happy-path.

### Dependencies

None new.

## Implementation Steps

### Phase 1 — C2 Critical: table identity normalization (do first)

**Step 1.1**: Add `__post_init__` to `TableRef` lowercasing `catalog`, `db`, `name`.
- Files: [`base.py`](../src/sqlcg/parsers/base.py) (`TableRef`, lines 44-73)
- Use `object.__setattr__(self, "name", self.name.lower())` etc., guarding `None`.
- Acceptance: `TableRef(db="BA", name="WTFE_KPI").full_id == "ba.wtfe_kpi"`;
  `TableRef(db="ba", name="wtfe_kpi").full_id == "ba.wtfe_kpi"` — equal strings.

**Step 1.2**: Add `__post_init__` to `ColumnRef` lowercasing `name`.
- Files: [`base.py`](../src/sqlcg/parsers/base.py) (`ColumnRef`, lines 76-95)
- Acceptance: `ColumnRef(TableRef(db="BA", name="T"), "MA_ROTATIE").full_id ==
  "ba.t.ma_rotatie"`.

**Step 1.3**: Verify confidence scoring now matches for upper-case source tables.
- Files: read-only verification in [`base.py`](../src/sqlcg/parsers/base.py) lines 483-487,
  837-838 (no code change expected; the lowercase refs now hit `mapping_schema_tables`).
- Acceptance: a unit test feeding an upper-case Snowflake source whose table is in
  `mapping_schema` produces a `confidence == 1.0` edge (was 0.7 before).

**Step 1.4**: Lowercase the `find_table_usages` input to match lowercased `SqlTable.name`.
- Files: [`tools.py`](../src/sqlcg/server/tools.py) `find_table_usages` (line 346) — pass
  `{"name": table_name.lower()}`.
- Acceptance: `find_table_usages("WTFE_KPI_ROTATIE_WEBSHOP")` returns the same usages as
  `find_table_usages("wtfe_kpi_rotatie_webshop")`.

**Step 1.5 (C2b investigation)**: Confirm scripting-mode source refs pass through
`_apply_table_alias`.
- Files: [`snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py)
  `_parse_scripting_file` (lines 224-315), cross-checked against
  [`base.py`](../src/sqlcg/parsers/base.py) `_real_tables` / `_convert_table_expr_to_ref`.
- Acceptance (one of):
  - If the chain already applies aliases: a unit test parsing a scripting block whose
    source is `BA_TMP.X` produces a single source ref `ba_tmp.x` (case collapsed) and,
    when `schema_aliases={"ba_tmp":"ba"}` is configured, `ba.x`.
  - If the scripting path bypasses aliasing: add the `_apply_table_alias` call and the
    same test passes; document the wiring in the plan-compliance note.

### Phase 2 — F3: table context on lineage nodes

**Step 2.1**: Add `table: str | None` to `LineageNode` and `DependencyNode`.
- Files: [`models.py`](../src/sqlcg/server/models.py) (lines 6-12, 49-53)
- Acceptance: model instantiates with `table="ba.foo"`; field appears in serialized JSON.

**Step 2.2**: Add `table_qualified` to the four traversal RETURN clauses.
- Files: [`queries.cypher`](../src/sqlcg/core/queries.cypher)
  (TRACE_COLUMN_LINEAGE, GET_UPSTREAM_DEPENDENCIES, GET_DOWNSTREAM_DEPENDENCIES)
- Acceptance: integration test asserts each row dict has a non-null `table_qualified`.

**Step 2.3**: Populate `table` in the four tool builders from `row["table_qualified"]`.
- Files: [`tools.py`](../src/sqlcg/server/tools.py) lines 306-313 (trace), 415 (downstream),
  475 (upstream).
- Acceptance: integration test on a small indexed repo asserts a returned node has
  `table == "<schema>.<table>"` and `name == "<column>"` (not null, not the full id).

### Phase 3 — F2: dedup traversal results

**Step 3.1**: Dedup emitted nodes by `node_id` (graph id), not by display name, before
returning, in all four traversal tools.
- Files: [`tools.py`](../src/sqlcg/server/tools.py) — the BFS loops at lines 293-314,
  402-416, 462-476.
- Approach: track an `emitted: set[str]` of `node_id` and append to the result list only
  when first seen. (The existing `visited` guard fires on dequeue, after emission, which
  is why a node enqueued twice in one frontier emits twice — gate emission on `emitted`.)
- Acceptance: a constructed graph where one source feeds two columns of the target emits
  each upstream `node_id` exactly once; result length equals the distinct-id count.

### Phase 4 — F1 + F4: hints and documented format

**Step 4.1**: Improve the empty-lineage hint in `trace_column_lineage` and
`get_upstream_dependencies` to name the schema prefix.
- Files: [`tools.py`](../src/sqlcg/server/tools.py) lines 316-322, 478-484.
- New text must contain the literal example `ba.table_name.column_name` and keep the
  existing `db info` guidance as a secondary clause.
- Acceptance: `test_tools_hints.py` asserts the substring
  `"ba.table_name.column_name"` is present in the empty-result hint.

**Step 4.2**: Distinguish the downstream empty hint (terminal vs lookup failure).
- Files: [`tools.py`](../src/sqlcg/server/tools.py) `get_downstream_dependencies`
  lines 418-424.
- New text: "No downstream consumers found — this column may be a terminal output. If you
  expected consumers, confirm the consuming files were indexed and that the column
  reference includes the schema prefix (e.g. `ba.table_name.column_name`)."
- Acceptance: hint differs from the upstream hint string; test asserts both the
  "terminal output" phrase and the schema-prefix example are present.

**Step 4.3**: Update the three traversal tool docstrings to require the schema-qualified
form.
- Files: [`tools.py`](../src/sqlcg/server/tools.py) lines 267-270, 376-378, 436-438.
- Acceptance: docstring contains "schema" and the `ba.table_name.column_name` example;
  a doc-content unit test asserts the substring in `trace_column_lineage.__doc__`.

### Phase 5 — F6: quiet the benign collision warning

**Step 5.1**: Lower the `logger.warning` at indexer.py line 502 to `logger.debug`.
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py) lines 502-508.
- Keep the structured `parsed.errors.append("duplicate_ddl:...")` (line 510) intact so the
  signal is still queryable; only the console noise is removed.
- Acceptance: a unit test indexing two files defining the same table asserts the
  `duplicate_ddl:` error string is still recorded, and (via `caplog`) that no WARNING-level
  record is emitted for the collision.

### Phase 6 — F5: investigate duplicate query nodes (measure, then decide)

**Step 6.1**: Measure. Index a small fixture and run
`MATCH (q:SqlQuery) RETURN q.id, count(*) AS n ORDER BY n DESC` to confirm whether any
`q.id` has `n > 1`. Note: `query_id = f"{path}:{i}"` is unique per statement and
`query_rows` is dedup'd by id (indexer.py line 675), so duplication, if real, must come
from a re-index without reset or from the same statement counted in multiple batches.
- Files: read-only; record finding in the plan-compliance note.
- Acceptance: a measurement number is recorded. If `n == 1` for all ids, F5 is closed as
  "not reproduced; the report's count was pre-reset stale data" and no code changes are
  made. If `n > 1`, open a follow-up with the offending code path identified (do **not**
  expand this plan to a perf rewrite).

## Test Strategy

- **Unit (`tests/unit/`)**:
  - `test_base_parser.py` (extend): `TableRef`/`ColumnRef` lowercasing and `full_id`
    equality across case variants (Steps 1.1, 1.2).
  - `test_column_lineage_wiring.py` or new `test_firstuser_confidence.py`: upper-case
    source table in mapping_schema yields confidence 1.0 (Step 1.3).
  - `test_tools_hints.py` (extend): F1/F4 hint substrings (Steps 4.1, 4.2); docstring
    content (Step 4.3).
  - `test_tools_warnings.py` or new: F6 log-level downgrade with `caplog` (Step 5.1).
  - Tool result dedup with a constructed BFS scenario (Step 3.1) — use a fake backend or
    monkeypatched `run_read` returning a node twice.
- **Integration (`tests/integration/`)**:
  - New `test_firstuser_table_identity.py`: index a fixture with `BA.T` and `ba.t`
    references to the same physical table; assert a single `SqlColumn` node and that
    upstream lineage from the lowercase ref reaches the upper-case-sourced edge
    (the core C2 regression guard).
  - Extend an existing lineage integration test to assert each returned node has a
    non-null `table` and that `name` is the bare column (Steps 2.2, 2.3).
  - `find_table_usages` case-insensitive input (Step 1.4).
- **Observable-output rule**: every test asserts on returned node fields, hint strings,
  recorded error entries, or node counts — never "no exception raised".

## Acceptance Criteria

- [ ] C2: After reindex, the six phantom nodes for `wtfe_kpi_rotatie_webshop` collapse;
      a `MATCH (c:SqlColumn) WHERE c.col_name='ma_rotatie' AND c.table_qualified CONTAINS
      'wtfe_kpi_rotatie_webshop'` returns case-collapsed `table_qualified` values (all
      lowercase), and upstream lineage from `ba.wtfe_kpi_rotatie_webshop.ma_rotatie`
      returns a non-empty list.
- [ ] C2: `TableRef`/`ColumnRef.full_id` is lowercase for any input case (unit).
- [ ] C2b: scripting-mode `BA_TMP.X` source produces a single lowercase ref; with
      `schema_aliases={"ba_tmp":"ba"}` it produces `ba.x` (unit).
- [ ] F1: empty-lineage hint and the three traversal docstrings contain the literal
      `ba.table_name.column_name`.
- [ ] F2: each traversal tool returns each upstream/downstream `node_id` at most once.
- [ ] F3: every returned `LineageNode`/`DependencyNode` carries a non-null `table` equal
      to the column's `table_qualified`, with `name` still the bare column.
- [ ] F4: downstream empty hint contains "terminal output" and differs from the upstream
      hint.
- [ ] F5: a measured per-id query-node count is recorded; code changed only if `n > 1`.
- [ ] F6: the cross-file collision is no longer logged at WARNING; the `duplicate_ddl:`
      error entry is still recorded.
- [ ] `uv run pytest`, `uv run pyright`, `uv run ruff check src tests` all pass.

## Risks and Mitigations

- **R1 — Lowercasing breaks a case-sensitive consumer.** Snowflake is case-insensitive
  for unquoted identifiers, and the schema side is already lowercase (§2.6), so this
  aligns rather than diverges. Mitigation: the C2 integration regression guard plus the
  `find_table_usages` input-lowercasing step (1.4) cover the one place that matched on raw
  `name`. Grep for other `{name:` / `{qualified:` Cypher matches before opening the PR.
- **R2 — Quoted/case-sensitive Snowflake identifiers** (rare, `"MixedCase"`). Out of scope
  for this corpus; the report shows the real collisions are unquoted case variants of the
  same name. Document as a known limitation; do not special-case quoted identifiers now.
- **R3 — `__post_init__` on frozen dataclass.** Must use `object.__setattr__`; a naive
  assignment raises `FrozenInstanceError`. Covered by the Step 1.1/1.2 unit tests.
- **R4 — F5 scope creep into a perf rewrite.** Hard-gated: Phase 6 only measures and, at
  most, files a follow-up. No perf work in this PR.

## Rollout

No backward compatibility. Migration path for existing graphs:

```
sqlcg db reset
sqlcg index <path> --dialect snowflake
```

Document this in the PR description. Existing data indexed before the normalization will
retain split nodes until reindexed; there is no in-place migration (per `CLAUDE.md`).
