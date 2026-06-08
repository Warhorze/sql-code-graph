# Feature Plan: `diff_impact` under-reports blast radius for ETL producer files

## Summary

[`diff_impact`](../src/sqlcg/server/tools.py) resolves each changed file to its
produced tables via `DEFINED_IN` only, which the indexer populates **solely from
DDL `CREATE TABLE`/`CREATE VIEW` provenance**. On repos where DDL lives in
separate files from the ETL `INSERT…SELECT` producers (the DWH layout: DDL
changelog files vs `etl/sql/...` producers), passing a producer file returns
**no produced table → an empty, silently under-counted blast radius**. This is a
correctness bug for the tool's primary use case ("here are my PR's changed files,
what breaks?"). The fix makes `diff_impact`'s file→table resolution additionally
consult `QUERY_DEFINED_IN` → `SqlQuery.target_table`, unioned with the existing
`DEFINED_IN`-derived set.

This is a **separate issue from #38** (fixed in PR #57): own branch, own PR, own
patch bump.

## Scope

### In Scope
- A new read query (proposed `GET_TARGET_TABLES_FOR_FILE`) joining
  `QUERY_DEFINED_IN` (query→file) → `SqlQuery`, filtered to non-empty
  `target_table`, returning the produced/populated tables for a file.
- Additive change to [`diff_impact`](../src/sqlcg/server/tools.py#L1014) so the
  per-file resolution loop unions `DEFINED_IN`-derived tables with
  `QUERY_DEFINED_IN`-derived target tables, de-duplicated against `seen_changed`.
- An integration test reproducing the gap (separate DDL file + separate
  `INSERT…SELECT` producer file + downstream consumer), mirroring the
  [`test_anchor_tools.py`](../tests/integration/test_anchor_tools.py) style and
  `_index_fixture` helper.
- Patch version bump (re-check current version at implementation time; expected
  `1.4.3` once #38's `1.4.2` ships).

### Non-Goals
- **No** change to `DEFINED_IN` semantics, the indexer's "prefer DDL provenance"
  merge ([`indexer.py` ~L126](../src/sqlcg/indexer/indexer.py#L126)), or any
  perf-invariant surface ([`base.py`](../src/sqlcg/parsers/base.py),
  [`indexer.py`](../src/sqlcg/indexer/indexer.py)).
- **No** re-index requirement: the fix reads edges that already exist in any
  current graph.
- **No** change to `get_change_scope` / `get_backfill_order` / `scope_change`'s
  downstream computation — those key off `HAS_COLUMN` + `COLUMN_LINEAGE` and were
  verified correct on the live DWH. Out of scope.
- The secondary advisory finding (`defining_files` / `get_definition` show only
  the DDL file) — **deferred**, see "Secondary finding" below.
- No tag, no issue close (user verifies and tags).

## Background / evidence

### How an `INSERT…SELECT` producer is recorded
An ETL `INSERT INTO t SELECT …` statement is parsed into a `SqlQuery` node with
`target` set (`QueryKind.INSERT`, see
[`base.py` ~L317/L645](../src/sqlcg/parsers/base.py#L317)), producing:
- a `SqlQuery` row with **`target_table` = the populated table** (schema:
  [`schema.cypher` L44](../src/sqlcg/core/schema.cypher#L44), `target_table STRING`),
- a `QUERY_DEFINED_IN` edge **query→file** (`qdi.src_key = query.id`,
  `qdi.dst_key = file.path`),
- **but no `DEFINED_IN` edge** — that edge is emitted only by DDL
  `CREATE TABLE`/`CREATE VIEW` provenance.

### Why `diff_impact` misses it today
The resolution loop calls `GET_TABLES_DEFINED_IN_FILE`
([`tools.py` L1047](../src/sqlcg/server/tools.py#L1047)), whose query is purely
`SqlTable` ⋈ `DEFINED_IN` ⋈ filter `di.dst_key = ?`
([`queries.sql` L194](../src/sqlcg/core/queries.sql#L194)). A producer file has no
`DEFINED_IN` edge, so `changed_tables` stays empty → empty blast radius. The
existing "No tables are defined in the given files" hint
([`tools.py` L1090](../src/sqlcg/server/tools.py#L1090)) fires, but a caller passing
a real changed producer file reasonably reads an empty result as "safe". Silent
under-count is the worst failure mode for an impact tool.

### The edges/columns the fix relies on already exist and are already used
- `QUERY_DEFINED_IN` query→file is joined identically in `SEARCH_SQL_PATTERN`
  ([`queries.sql` L79](../src/sqlcg/core/queries.sql#L79)),
  `DEPENDENT_FILES_OF_TABLES` ([L227](../src/sqlcg/core/queries.sql#L227)), and
  `FIND_TABLE_USAGES` ([L57](../src/sqlcg/core/queries.sql#L57)).
- `SqlQuery.target_table` filtered to non-empty (`<> ''`) is exactly the pattern
  in `HUB_RANKING` ([L215](../src/sqlcg/core/queries.sql#L215)) and the
  `EXPAND_STAR_SOURCES` family ([L111](../src/sqlcg/core/queries.sql#L111)).

So no schema change, no new edge type, no indexer change, no re-index.

## Chosen fix approach: query-layer union (vs. index-time `DEFINED_IN` edge)

**Chosen: query-layer.** Add `GET_TARGET_TABLES_FOR_FILE` and union its result
into `diff_impact`'s per-file resolution. Rationale:

| Criterion | Query-layer (chosen) | Index-time DEFINED_IN edge (rejected) |
|---|---|---|
| Re-index required | No — reads existing edges | **Yes** — every user must re-index before the fix takes effect |
| Perf-invariant surface | Untouched | Touches `indexer.py` edge emission; must re-justify every CLAUDE.md invariant |
| `DEFINED_IN` semantics | Unchanged (DDL-only) — incremental-reindex closure stays correct | **Changes** the meaning of `DEFINED_IN`; would pollute `find_definition`, `GET_TABLE_DEFINING_FILES`, and the resync closure-frontier seeding ([`indexer.py` L670/L748](../src/sqlcg/indexer/indexer.py#L670)) with producer files, conflating "where defined" with "where populated" |
| Blast radius of change | One query + one loop union in `diff_impact` | Indexer dedup/merge rules, reindex delete-capture, multiple downstream queries |
| Cost | One extra read per changed file (cheap; same JOIN shape already used elsewhere) | N/A |

The index-time alternative has a **fatal coherence flaw**: `DEFINED_IN` is the
backbone of `find_definition` and the incremental-reindex closure. Overloading it
to also mean "populated here" would silently change `find_definition`'s
authoritative-file detection and the resync delete-capture set — exactly the
sensitive paths this plan must NOT disturb. The query-layer fix keeps "defined"
(DDL) and "produced/populated" (DML target) as distinct concepts and unions them
only at the one call site that needs both.

## Design

### New query — `GET_TARGET_TABLES_FOR_FILE`
Add to [`queries.sql`](../src/sqlcg/core/queries.sql) and register in
[`queries.py`](../src/sqlcg/core/queries.py) as
`GET_TARGET_TABLES_FOR_FILE_QUERY` (mirroring the existing
`GET_TABLES_DEFINED_IN_FILE_QUERY` registration at
[L59](../src/sqlcg/core/queries.py#L59)). Return the same column name
`table_qualified` so the `diff_impact` loop can treat both result sets uniformly:

```sql
-- GET_TARGET_TABLES_FOR_FILE
-- params: [file_path]
SELECT DISTINCT q.target_table AS table_qualified
FROM "SqlQuery" q
JOIN "QUERY_DEFINED_IN" qdi ON qdi.src_key = q.id
WHERE qdi.dst_key = ?
  AND q.target_table <> ''
```

Notes:
- `q.target_table <> ''` excludes SELECT-only queries (no produced table). Confirm
  at implementation that the parser writes `''` (not NULL) for SELECT-only
  queries; the `EXPAND_STAR_SOURCES`/`HUB_RANKING` queries already rely on `<> ''`
  on this same column, so `''` is the established empty sentinel. If NULL is
  possible, add `AND q.target_table IS NOT NULL`.
- `target_table` is stored as written by the parser. The `diff_impact` produced
  tables flow straight into `GET_COLUMNS_FOR_TABLE` /
  `_affected_columns_closure`, which already key off `SqlTable.qualified` /
  `HAS_COLUMN`; an unresolved `target_table` simply yields an empty column set
  (no downstream), which is correct degradation, not a crash.

### `diff_impact` change (additive)
In the per-file loop ([`tools.py` L1041-L1052](../src/sqlcg/server/tools.py#L1041)),
after the existing `GET_TABLES_DEFINED_IN_FILE_QUERY` read, run
`GET_TARGET_TABLES_FOR_FILE_QUERY` against the same resolved `str(fp)` and feed its
rows through the **same** `seen_changed` de-dup append. Shape:

```python
for query in (GET_TABLES_DEFINED_IN_FILE_QUERY, GET_TARGET_TABLES_FOR_FILE_QUERY):
    rows = db.run_read(query, {"file_path": str(fp)})
    for row in rows:
        tq = row["table_qualified"]
        if tq and tq not in seen_changed:
            seen_changed.add(tq)
            changed_tables.append(tq)
```

The `seen_changed` set already de-dups a table that is both DDL-defined and
DML-populated in the changed set, so the union is order-preserving and
idempotent. No change to the downstream closure, noise filter, presentation, topo
sort, or external-consumer batch — they all consume `changed_tables` unchanged.

### Data models
No model changes. `DiffImpactResult` is returned unchanged; only `changed_tables`
(and therefore `affected_tables`/`backfill_order`/`external_consumers`) become
correctly populated for producer files.

### Dependencies
None new.

## Implementation Steps

### Phase 1: Query
**Step 1.1**: Add `GET_TARGET_TABLES_FOR_FILE` block to
[`queries.sql`](../src/sqlcg/core/queries.sql) (after `GET_TABLES_DEFINED_IN_FILE`,
~L199) and register `GET_TARGET_TABLES_FOR_FILE_QUERY` in
[`queries.py`](../src/sqlcg/core/queries.py).
- Files affected: `queries.sql`, `queries.py`.
- Acceptance: a unit/integration assertion confirms the query, run against a graph
  containing one `INSERT…SELECT` producer, returns the produced table by qualified
  name and excludes SELECT-only queries.

### Phase 2: Tool
**Step 2.1**: Import `GET_TARGET_TABLES_FOR_FILE_QUERY` in
[`tools.py`](../src/sqlcg/server/tools.py) (alongside the existing import at
[L26](../src/sqlcg/server/tools.py#L26)) and union it into the `diff_impact`
per-file loop as designed above. Add `if tq` guard against empty/NULL.
- Files affected: `tools.py`.
- Acceptance: grep-confirmed call site — `GET_TARGET_TABLES_FOR_FILE_QUERY` is
  imported AND invoked inside `diff_impact` (not merely defined). The new query is
  called, not just registered.

### Phase 3: Repro test
**Step 3.1**: Add `test_diff_impact_producer_file_blast_radius` to
[`test_anchor_tools.py`](../tests/integration/test_anchor_tools.py) using
`_index_fixture`, with three SEPARATE files:
- `ddl.sql`: `CREATE TABLE ba.dim (id INT);` (DDL only — DEFINED_IN edge, no producer query)
- `producer.sql`: `INSERT INTO ba.dim SELECT id FROM ba.source;` plus a
  `CREATE TABLE ba.source (id INT);` (so `ba.source` resolves) — OR put
  `ba.source` DDL in its own file; the key is `ba.dim` is **populated** by
  `producer.sql`, not DDL-defined there.
- `consumer.sql`: `CREATE TABLE ba.mart AS SELECT id FROM ba.dim;` (downstream).

Assertions (observable output, not "no exception"):
- `result = tools.diff_impact([str(tmp_path / "producer.sql")])`
- `"ba.dim" in result.changed_tables` — the produced table IS resolved from the
  producer file (the core fix; fails pre-fix with an empty `changed_tables`).
- `"ba.mart" in result.affected_tables` — its downstream consumer appears in the
  blast radius.
- Optionally assert `result.hint is None` (no "no tables defined" hint) to pin
  that the silent-empty path is gone.
- Files affected: `tests/integration/test_anchor_tools.py`.
- Acceptance: test is RED on `master`+#38 baseline (confirm by running before the
  `tools.py` edit) and GREEN after the fix.

**Step 3.2** (regression guard): keep the existing CTAS-based
`test_diff_impact_file_to_blast_radius` ([L265](../tests/integration/test_anchor_tools.py#L265))
green — it pins the `DEFINED_IN` path still works (the union must be additive, not
a replacement).

### Phase 4: Version + housekeeping
**Step 4.1**: Re-check current version at implementation time. If `1.4.2` (#38)
has shipped to `master`, bump to `1.4.3` (patch — pure bug fix, additive query, no
surface change) in [`pyproject.toml`](../pyproject.toml) and
[`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py); run `uv lock`. If `1.4.2` has
NOT yet merged, coordinate so this branch bumps to the next patch after it. No tag
(user tags after merge), no issue close.

## Test Strategy
- **Integration** (primary): the producer-file repro in Phase 3 — the only test
  that exercises the real DEFINED_IN-vs-QUERY_DEFINED_IN gap end to end through a
  real DuckDB graph and the actual `diff_impact` tool.
- **Regression**: existing CTAS `test_diff_impact_file_to_blast_radius`,
  presentation tests, and the full `test_anchor_tools.py` suite stay green.
- **Reindex non-interference**: assert (or reason in PR) that
  `GET_TABLES_DEFINED_IN_FILE_QUERY` is unchanged and the new query is referenced
  ONLY from `diff_impact`. The incremental-reindex paths at
  [`indexer.py` L670](../src/sqlcg/indexer/indexer.py#L670) and
  [L748](../src/sqlcg/indexer/indexer.py#L748) (delete-capture and closure-frontier
  seeding) intentionally use DDL provenance and must keep using
  `GET_TABLES_DEFINED_IN_FILE_QUERY` only — verify via grep that
  `GET_TARGET_TABLES_FOR_FILE_QUERY` has zero references in
  [`indexer.py`](../src/sqlcg/indexer/indexer.py).
- No new unit test of the query in isolation is required beyond what the
  integration repro covers, but a thin query-level assertion (Step 1.1) is welcome.

## Acceptance Criteria
- [ ] `GET_TARGET_TABLES_FOR_FILE` exists in `queries.sql` and is registered as
      `GET_TARGET_TABLES_FOR_FILE_QUERY` in `queries.py`.
- [ ] `diff_impact` imports AND invokes `GET_TARGET_TABLES_FOR_FILE_QUERY` inside
      its per-file loop (grep-confirmed call site, not just a definition).
- [ ] New integration test: `diff_impact([producer_file])` returns the produced
      table in `changed_tables` and its downstream consumer in `affected_tables`;
      the test is demonstrably RED before the `tools.py` edit and GREEN after.
- [ ] Existing CTAS `test_diff_impact_file_to_blast_radius` and all
      `test_anchor_tools.py` tests remain green (additive, non-regressive).
- [ ] `GET_TARGET_TABLES_FOR_FILE_QUERY` has zero references in
      [`indexer.py`](../src/sqlcg/indexer/indexer.py); `GET_TABLES_DEFINED_IN_FILE`
      query text is byte-for-byte unchanged.
- [ ] No edit to [`base.py`](../src/sqlcg/parsers/base.py) or
      [`indexer.py`](../src/sqlcg/indexer/indexer.py) (perf-invariant surface
      untouched); no re-index required for the fix to take effect.
- [ ] SELECT-only queries (`target_table = ''`/NULL) do not pollute
      `changed_tables`.
- [ ] Version bumped to the correct next patch (expected `1.4.3`) with `uv lock`
      refreshed; no tag, no issue close.
- [ ] Full gate green: `uv run pytest`, `uv run pyright`, `uv run ruff check`.

## Secondary finding — DEFER (recommended)
`get_change_scope.defining_files` and `find_definition` report only the DDL file,
not the ETL producer, because both run `GET_TABLE_DEFINING_FILES_QUERY` (DDL
`DEFINED_IN` only — [`tools.py` L907](../src/sqlcg/server/tools.py#L907),
[`queries.sql` L167](../src/sqlcg/core/queries.sql#L167)).

**Recommendation: defer to a separate follow-up, do NOT bundle here.** Reasons:
1. **Different semantics, different decision.** `defining_files` answers "where is
   this table *defined*" (an authoritativeness/governance question that
   `find_definition` builds is_authoritative/is_backup/duplicate_ddl on top of).
   Folding producer files in changes the meaning of that field and would ripple
   into `find_definition`'s authoritative-file logic and its model — a design
   choice that deserves its own plan, not a rider on an impact-tool patch.
2. **`diff_impact` is the bug with the silent-under-count failure mode**; the
   secondary one is advisory (a user inspecting a *table* still gets correct
   downstream via `get_change_scope`). Coupling them widens the PR's blast radius
   and review surface for no urgency.
3. **Memory rule** "never fold new work into an already-reviewed PR / ship a
   separate patch" applies in spirit: keep this PR tightly scoped to the
   file→produced-table resolution fix.

If pursued later, the shape would be a new `producing_files` field (NOT overloading
`defining_files`) sourced from `QUERY_DEFINED_IN` + non-empty `target_table` for
the given table — a clean additive surface. Note it in `ARCHITECTURE_REVIEW.md` as
a deferred follow-up.

## Risks and Mitigations
- **Risk: unresolved `target_table` (no matching `SqlTable`).** A produced table
  whose `qualified` does not match any `SqlTable` row yields an empty
  `GET_COLUMNS_FOR_TABLE` result → no downstream contribution, but it still
  correctly appears in `changed_tables`. Degrades gracefully; no crash.
  *Mitigation*: covered by the repro (the produced `ba.dim` has DDL elsewhere so it
  resolves; assert it appears in `changed_tables` regardless).
- **Risk: NULL vs empty-string `target_table`.** *Mitigation*: confirm parser
  writes `''`; the `<> ''` filter matches existing `HUB_RANKING`/`EXPAND_STAR`
  convention. Add `IS NOT NULL` if NULL is observed.
- **Risk: double-count when a table is both DDL-defined and DML-populated in the
  same changed set.** *Mitigation*: `seen_changed` de-dup already handles this;
  union is idempotent.
- **Risk: perf — one extra read per changed file.** *Mitigation*: `diff_impact`
  operates on a PR-sized file list (tens, not thousands); the new query reuses an
  index-friendly JOIN already present in three other queries. Not on the
  index/parse hot path; CLAUDE.md perf invariants are untouched.
- **Risk: breaking incremental reindex.** *Mitigation*: the fix never touches
  `GET_TABLES_DEFINED_IN_FILE` or `DEFINED_IN`; acceptance criterion pins zero
  `indexer.py` references to the new query.

## Rollout / rollback
- Rollout: ships in the patch release; takes effect immediately on existing graphs
  (no re-index). 
- Rollback: revert the `tools.py` union and the two query additions — fully
  isolated, no migration, no data shape change.

### Blocking Questions
None. `QUERY_DEFINED_IN` (query→file via `dst_key`) and `SqlQuery.target_table`
are confirmed present in [`schema.cypher`](../src/sqlcg/core/schema.cypher) and
already used by `SEARCH_SQL_PATTERN` / `DEPENDENT_FILES_OF_TABLES` /
`GET_TABLE_DIRECT_UPSTREAMS`; the empty sentinel `''` convention is established.
