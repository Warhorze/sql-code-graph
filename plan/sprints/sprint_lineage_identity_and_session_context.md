# Sprint Plan: Lineage Identity & Session Context

**Status**: REVIEWED — plan-review gate passed 2026-06-11 with amendments (B1/B2
blockers + W1–W3 incorporated below); cleared for implementation
**Planned**: 2026-06-11 (architect-planner role)
**Baseline**: v1.14.2 graph of the DWH corpus, `indexed_sha=d015f8c`, db `~/.sqlcg/graph.db`
(1,379 files / 6,388 tables / 49,466 COLUMN_LINEAGE edges).
**Origin**: live-graph investigation session 2026-06-11 (dashboard build → three
quantified findings). All baseline numbers below were measured directly against
that graph and are reproducible with the SQL snippets in §7.

## Summary

Three independent, measured defects make graph *keys* lie about identity, which
corrupts both the strict edge-health metric and `trace_column_lineage` output:

| # | Finding | Measured size | Class |
|---|---------|--------------|-------|
| F1 | `schema_aliases` applied inconsistently — `SqlQuery.target_table` is aliased but `COLUMN_LINEAGE` dst keys are not (and in some statements the target isn't aliased either) | 597 strict-bad edges into `*_tmp.*` tables, **all 597** with a fully catalogued base table | mis-keyed (edges are correct, keys wrong) |
| F2 | `USE SCHEMA` session state not modeled — unqualified DML targets after `use schema X` are keyed bare | 828 strict-bad edges whose unqualified dst table exists schema-qualified in HAS_COLUMN (incl. `doorbelasting`: 107); 593 corpus files contain `use schema` | mis-keyed |
| F3 | CTE/derived nodes keyed by bare name — same-named CTEs in different files share one graph node; trace BFS hops through them with no file scoping | 218 of 3,561 CTE dst columns receive edges from >1 file; 1,110 edges into colliding keys (`cte_insert`: 8 files, `final`: 5) | false identity (trace returns cross-file lineage that does not exist) |

F1+F2 together account for **~1,425 of ~8,575 strict-bad edges (≈17%)** that flip
to strict-good once keys are correct — projected strict edge health ~85.5% from
82.7%, with the blindspot top-10 losing 7 of its 10 entries. F3 is not a coverage
lever; it is a **trust** defect (MCP tools report heuristic-free *fact*, and a
collided trace is a false fact).

## Scope

### In Scope
- One key-normalization choke point so every graph key (table + column, src + dst)
  passes through identical aliasing/qualification logic (PR 1).
- File-local `USE SCHEMA` / `USE DATABASE` tracking as default qualifier for
  subsequent unqualified **real-table** refs in the same file (PR 2).
- `CREATE TABLE x LIKE y` catalog inheritance, reusing the existing CLONE
  machinery (`clone_source` → `resolve_clone_catalogs`) (PR 2, small).
- Per-file namespacing of CTE/derived node keys (PR 3).
- Two new coverage counters in `sqlcg gain` §G: CTE key collisions and
  alias/qualification divergence (PR 4, measurement + acceptance instrument).

### Non-Goals
- **No parse-time schema feeding.** Nothing here passes `schema_sources` into
  `exp.expand()` or `qualify()` — that path stays forbidden (CLAUDE.md). All fixes
  are key construction (parse metadata) or post-index catalog copies.
- No change to the `_tmp`-tables-as-nodes design. With F1 fixed, the configured
  `schema_aliases` fold `ba_tmp.X` into `ba.X` everywhere, which is the user's
  declared intent; tables in *non-aliased* tmp schemas remain ordinary nodes.
- No session tracking across files (PDI orchestration order is unknowable from
  the repo). `USE SCHEMA` state is strictly file-local, reset per file.
- No Liquibase-XML CDATA DDL ingestion (separate backlog item; see
  `project_blindspot_xml_ddl_lever` / postmortem follow-ups).
- No dashboard/visualization changes.

## Design

### PR 1 — Key normalization choke point (fixes F1)

**Problem evidence** (both from one index run, same config, same code path family):
- `etl/pdi/template/wtfa_website_sessie_us50655.sql:2` — `target_table =
  'ba.wtfa_website_sessie'` (aliased) but its 75 edges have
  `dst_key = 'ba_tmp.wtfa_website_sessie.*'` (un-aliased, all
  `inferred_from_source_name=true`). The A2 inferred-dst path builds the dst
  ColumnRef from the source expression's raw table, bypassing the aliased
  `dst_table`.
- `etl/sql/fact/wtfa_loonkosten.sql:3` — `target_table = 'ba_tmp.wtfa_loonkosten'`
  itself un-aliased (90 edges). A second branch misses `_apply_table_alias`
  entirely (file is `use schema BA_TMP;` + `insert into BA_TMP.WTFA_LOONKOSTEN(...)`
  — column-list INSERT, `stmt.this` is `exp.Schema`).
- **Empty-identity keys** (same class — keys that lie about identity): 311
  `COLUMN_LINEAGE.src_key` rows have a *leading dot* (`.ean_code`, `.@1`,
  `.metadata$filename`, `.file_name`, …) plus one `SqlTable` row whose
  `qualified` is the empty string. Producers are Snowflake **stage reads**
  (`SELECT $1, metadata$filename FROM @stage` in CTAS statements, e.g.
  `etl/sql/da/hyb/rthyb_promotion_normal.sql`: 58 edges): the stage ref yields a
  TableRef with an empty `name`, `full_id` renders as `''`, and
  `ColumnRef.full_id` becomes `.{col}`. All 311 share one phantom "table" node,
  so unrelated stage-loading pipelines are connected through it — a milder
  sibling of the F3 collision, and a contributor to the `<unknown>` blindspot
  bucket.

**Design**: stop relying on every parser branch remembering to call
`_apply_table_alias`. Normalize once, at the parse-output boundary:

- Add `ParsedFile.normalize_keys(aliases: dict[str, str])` (or a free function in
  [`base.py`](src/sqlcg/parsers/base.py)) that rewrites **every** TableRef-bearing
  field of the parse result through the alias map: `QueryNode.target`,
  `QueryNode.sources`, `LineageEdge.src/.dst` table refs, `StarSource.source`,
  `defined_tables`, `referenced_tables`, `clone_source`.
- Call it exactly once per file in the indexer, immediately before
  `_upsert_parsed_file` row construction — i.e. after pass-2 parsing, before any
  graph key is rendered. The existing scattered `_apply_table_alias` call sites
  stay (they keep in-parse resolution consistent) but are no longer the
  correctness boundary.
- `LineageEdge` and `ColumnRef`/`TableRef` are frozen dataclasses — rewrite via
  `dataclasses.replace`; this is a once-per-file O(edges) pass, far outside the
  per-column hot loop. **No CLAUDE.md performance invariant is touched.**
- **Empty-identity guard** (same choke point): the normalizer rejects any
  TableRef whose `full_id` is empty. For stage reads, resolve the ref to a
  stable synthetic identity instead of `''` — `stage://<stage_name>` when the
  `@stage` name is recoverable from the AST, else drop the edge and append a
  `col_lineage_skip:stage:<file>` entry to `ParsedFile.errors` (a dropped edge
  is honest; an empty-keyed edge is a false join point). Never emit a graph key
  with a leading dot, an empty table component, or an empty `SqlTable.qualified`.
  Decision on representation (synthetic stage node vs. drop) is the developer's
  call per what the AST exposes — both satisfy acceptance; document the choice
  in the PR.

**Developer investigation step (bounded)**: before writing the normalizer, confirm
with the two anchors above that no *third* divergence source exists in pass-2
cross-file resolution ([`CrossFileAggregator`](src/sqlcg/lineage/aggregator.py)).
If found, the choke point design already covers it — document it, don't branch-fix.

### PR 2 — `USE SCHEMA` file-local context + `LIKE` catalog inheritance (fixes F2)

**Problem evidence**: `ddl/changelogs/IA-OUTBOUND/MSSPR_IA_OUTBOUND_DOORBELASTING.sql`
line 21 `use schema IA_OUTBOUND;`, line 419 `insert into DOORBELASTING` →
107 edges keyed `doorbelasting.*` while the catalogued table is
`ia_outbound.doorbelasting` (all 4 dst column names exist verbatim in its DDL
catalog). Generalizes to 828 edges / 593 files.

**Design** (amended per plan review 2026-06-11 — B1/W3):
- **Non-scripting Snowflake path: USE SCHEMA tracking ALREADY EXISTS.**
  [`snowflake_parser.py:250`](src/sqlcg/parsers/snowflake_parser.py)
  `_transform_statements` (P5) tracks `exp.Use` with `kind == SCHEMA` and
  qualifies bare table refs via `_qualify_bare_tables`. Do NOT build parallel
  state — extend P5. Two existing gaps to close in P5: (a) verify/cover the
  two-part `USE SCHEMA db.schema` form; (b) support the bare `USE db.schema`
  form (no SCHEMA keyword → `kind_str` is empty; set schema from
  `stmt.this.name`).
- **The primary F2 gap is the scripting path.** The doorbelasting anchor is a
  stored-procedure file → `_has_scripting_block()` →
  `_parse_scripting_file()`, which bypasses `_transform_statements` entirely,
  and `_EMBEDDED_DML` matches only SELECT/INSERT/UPDATE/DELETE/MERGE — `USE`
  statements are dropped. Fix: a **pre-scan/interleaved scan of the raw sql
  text** for `USE SCHEMA <s>` (and `USE <d>.<s>`) statements that tracks the
  active schema *by text position*, so each `_EMBEDDED_DML` match is qualified
  with the schema context in force at its offset (apply via
  `_qualify_bare_tables` on the parsed DML statement).
- **`USE DATABASE X` alone is OUT of qualification scope** (decision, W3): with
  only a database and no schema, no two-part `schema.table` key can be formed;
  the existing code intentionally ignores it and that stays. The `use d.s`
  two-part forms above DO set the schema (the part that matters for keys).
- **ANSI parser**: base `_transform_statements` is a no-op. Add an override only
  if the corpus shows `USE` statements in ANSI-parsed files (verify with a grep
  over the fixture/DWH corpus; document the finding either way).
- Qualification itself reuses `_qualify_bare_tables` semantics: only refs with
  no db/catalog, never CTE alias names (lexically scoped, handled by F3),
  targets and sources both, then `schema_aliases` on top (PR 1's choke point
  makes ordering safe).
- State resets at file end. No cross-statement state other than this string pair;
  zero hot-loop cost (one isinstance check per statement).
- **`LIKE` inheritance** (small, same PR): extend the existing C1 clone capture
  ([`ansi_parser.py:321`](src/sqlcg/parsers/ansi_parser.py)) to also read
  `exp.LikeProperty` from `stmt.args['properties']` —
  `CREATE TABLE x LIKE y` sets `clone_source = y` exactly as `CLONE` does, and
  the existing `resolve_clone_catalogs` pass inherits `y`'s DDL catalog for `x`.
  Anchor: `ddl/changelogs/BA-TABLES/WTFA_LOONKOSTEN.sql` lines 270/522/620/741.
  (With PR 1 the `_tmp` cases largely self-resolve via aliasing; LIKE inheritance
  covers non-aliased schemas and keeps `defined_in_file` truthful.)
- The trace tool's bare-name fallback ([`tools.py:844`](src/sqlcg/server/tools.py))
  stays — it still serves graphs indexed before this fix and any residual
  unqualified writes.

### PR 3 — CTE/derived key namespacing (fixes F3)

**Problem evidence**: trace BFS ([`tools.py:813`](src/sqlcg/server/tools.py),
[`queries.sql` `TRACE_COLUMN_LINEAGE`](src/sqlcg/core/queries.sql)) hops on
`WHERE cl.dst_key = ?` with no file/query scoping; `cte_insert.dn_datum` receives
edges from 7 files, so tracing any one of them returns the other six files'
upstreams as fact.

**Design**:
- Namespace CTE and derived TableRefs per defining file:
  `qualified = f"{repo_relative_path}::{cte_name}"` (e.g.
  `etl/sql/fact/wtfa_loonkosten.sql::final`). Column keys follow automatically via
  `ColumnRef.full_id` = `{table.full_id}.{name}`.
  - Delimiter `::` is safe: the coverage `_DST_TABLE` strip expression
    ([`coverage.py:26`](src/sqlcg/cli/coverage.py)) splits on the **last** `.`,
    so `…wtfa_loonkosten.sql::final.col` strips to `…sql::final` correctly, and
    the scoped/blindspot `kind IN ('cte','derived')` joins keep working because
    `SqlTable.qualified` carries the same namespaced value.
  - File path, not query id: a file's statements may legitimately share a CTE
    body via `sources_map`; per-query namespacing would split real intra-file
    flows. Per-file matches the actual lexical scope closely enough (two
    same-named CTEs in one file is the residual risk — accept and note it).
- Implementation point: where the synthetic CTE dst table is created
  ([`base.py:1232`](src/sqlcg/parsers/base.py) `TableRef(name=cte_alias, role="cte")`)
  and wherever CTE-source refs are resolved to TableRefs — thread the file path
  into the TableRef (a `namespace` field on TableRef that `full_id` prefixes when
  `role != "table"` is the cleanest; keeps `db`/`catalog` semantics untouched).
- **Display**: `LineageNode.table` in trace output will now show the namespaced
  name for CTE hops. That is a feature (it tells the user *which* `final`), but
  verify the mermaid builder and noise filter don't choke on `::`.
- Re-index is the migration (project rule: no backward compat). Old graphs keep
  working — collided nodes simply persist until reindex.
- `_tmp` **tables are explicitly NOT namespaced** — they are real cross-file
  lineage carriers (measured: `da_tmp.tmp_max_dbkey` written by
  `rtikb_planogram_product.sql`, read by `rtikb_planogram_fixture.sql`).

### PR 4 — Coverage counters (instrument for all three)

Two additive queries in [`coverage.py`](src/sqlcg/cli/coverage.py) `CoverageStats`
(+ two lines in `gain` §G output, + `report` JSON):

- **CTE key collisions**: count of distinct CTE/derived dst keys in
  COLUMN_LINEAGE receiving edges from >1 distinct `SqlQuery.file_path`.
  Baseline 218 → target **0** after PR 3 + reindex.
- **Rescuable unqualified edges**: strict-bad edges whose dst table has no `.`
  and whose name exists schema-qualified in HAS_COLUMN (`hc.src_key LIKE
  '%.' || dst_t`). Baseline 828 → target **≈0** after PR 2 + reindex.

Both are read-only aggregates over existing tables; same `run_read_routed`
pattern, same graceful-degrade contract as `collect_coverage()`.

## Implementation order & sizing

| Order | PR | Size | Depends on | Version |
|-------|----|------|-----------|---------|
| 1 | PR 4 metrics first | S | — | minor (new gain surface) |
| 2 | PR 1 key choke point | M | PR 4 (to measure) | minor |
| 3 | PR 2 USE SCHEMA + LIKE | M | PR 1 (alias ordering) | minor |
| 4 | PR 3 CTE namespacing | M–L | PR 4 (collision counter as acceptance) | minor |

Metrics ship **first** so every subsequent PR's postmortem records
before/after from `sqlcg gain` instead of ad-hoc SQL. Each PR bumps the version
and is tagged after merge per CLAUDE.md release rules (user verifies + tags).

## Implementation Steps

### Phase 1: Metrics (PR 4 content, ships first)
**Step 1.1**: Add `_Q_CTE_COLLISIONS` and `_Q_RESCUABLE_UNQUALIFIED` to
[`coverage.py`](src/sqlcg/cli/coverage.py); extend `CoverageStats` (additive
fields + pct properties), `collect_coverage()`, the §G renderer, and the
`report` JSON serializer.
- Files: `src/sqlcg/cli/coverage.py`, `src/sqlcg/cli/gain.py` (renderer callsite),
  tests.
- Acceptance: `sqlcg gain` on the DWH baseline graph prints
  `CTE key collisions: 218` and `Rescuable unqualified edges: 828` (±0 — exact
  reproduction of §7 baselines).

### Phase 2: Key normalization (PR 1)
**Step 2.1**: Bounded investigation — run the two §7.1 anchor reproducers against
a fresh small-fixture index to confirm the divergence points; document in PR.
**Step 2.2**: Implement `normalize_keys` boundary pass; wire into indexer before
row construction (both `index_repo` and `reindex_file` paths).
- **Wiring constraint**: in `index_repo`, normalization must run on
  `pass2_results` **before** `defined_table_registry` is built
  ([`indexer.py:567`](src/sqlcg/indexer/indexer.py)) — wiring it inside
  `_upsert_file_batch` would leave registry keys (and
  `aggregator.canonical_by_bare` consumers) un-normalized.
- **W1 (plan review)**: `QueryNode.ctes` is intentionally EXCLUDED from
  normalization — it is parser-internal pass-2 state, never read by
  key-construction consumers. The dataclass-introspection guard test must
  enumerate the TableRef-bearing fields of `ParsedFile`/`QueryNode` and
  explicitly skip `ctes` (with a comment), so a future field added without
  normalization fails the test.
- Files: `src/sqlcg/parsers/base.py`, `src/sqlcg/indexer/indexer.py`, tests.
- Acceptance: unit test — a ParsedFile containing an aliased-schema target,
  un-aliased edge dst, un-aliased source, and a StarSource yields rows where
  **every** key uses the aliased schema. Integration test with
  `schema_aliases={"ba_tmp": "ba"}` over a two-statement fixture mirroring the
  loonkosten pattern asserts zero `ba_tmp.*` keys in any graph table.
**Step 2.3**: Reindex DWH, record `gain` before/after in the PR postmortem.
- Acceptance: bad edges into `*_tmp.*` tables: 597 → 0; leading-dot/empty-table
  keys: 311 → 0 and the `qualified=''` SqlTable row is gone (stage edges either
  carry a `stage://…` key or are dropped with a logged skip — zero silent empty
  keys); strict edge health rises (expected ≈ +1.2pp); no perf-guard regression
  (`test_perf_scaling_guard.py` green). **N2 (plan review)**: the postmortem also
  records `COUNT(*) WHERE src_key LIKE 'stage://%'` so the 311 leading-dot edges
  are accounted for (converted vs. dropped), not just gone.

### Phase 3: Session context (PR 2)
**Step 3.1** (amended — B1): scripting-path `USE` context. Position-aware
pre-scan of the sql text in `_parse_scripting_file`; qualify each extracted DML
statement with the schema context active at its text offset (reuse
`_qualify_bare_tables`). The doorbelasting proc is the live test — it MUST gain
`ia_outbound.*` dst keys.
**Step 3.2** (amended — B1/W3): non-scripting P5 extension only: two-part
`USE SCHEMA db.schema` + bare `USE db.schema` forms; `USE DATABASE` alone stays
ignored (documented decision). ANSI override only if corpus grep shows `USE` in
ANSI-parsed files. Targets and sources both, ordered before alias application;
CTE refs exempt.
**Step 3.3**: `LikeProperty` → `clone_source` capture.
- Files: `src/sqlcg/parsers/ansi_parser.py`, `src/sqlcg/parsers/snowflake_parser.py`,
  tests.
- Acceptance (unit): a fixture file with `use schema ia_outbound;` followed by an
  unqualified `insert into doorbelasting(...) select ...` produces dst keys
  `ia_outbound.doorbelasting.*`; a `use schema a; … use schema b; …` fixture
  applies the *latest* statement's schema; a CTE named in the same file is NOT
  schema-qualified. A `CREATE TABLE s.x LIKE s.y` fixture inherits `y`'s catalog
  columns for `x` via `resolve_clone_catalogs`.
- Acceptance (DWH): rescuable-unqualified counter 828 → ≤ 50 (residual = refs to
  schemas never catalogued); `doorbelasting` leaves the blindspot top-10.

### Phase 4: CTE namespacing (PR 3)
**Step 4.1**: `namespace` field on TableRef; `full_id` prefixes
`{namespace}::` when set; set it for `role in ("cte","derived")` at every CTE
TableRef creation site (grep `role="cte"` / `role="derived"` — each site needs
the file path in reach; thread via parser instance state set in `parse_file`).
**Step 4.2** (amended — B2/W2/N3): Verify consumers: trace output, mermaid
builder, noise filter, coverage `_DST_TABLE` strip, `find`/`analyze` CLI
rendering. Fix any `::` handling issue found. Known-affected consumers from the
plan review (must be fixed, not just audited):
- **[`noise_filter.py:75`](src/sqlcg/server/noise_filter.py)** (B2):
  `table_qualified.split(".")[-1]` yields `sql::final` for a namespaced key.
  Fix: a `::`-aware short-name helper — take the segment after the last `::`
  when present, else after the last `.`.
- **[`indexer.py`](src/sqlcg/indexer/indexer.py) `_harvest_usage_catalog`**
  (W2): the `"." not in table_qualified` CTE guard (and its line-129 comment)
  becomes a dead/wrong guard once namespaced keys contain `.sql`. Rely on the
  `kind` guard; update guard + comment to match the post-PR3 key shape.
- **[`tools.py`](src/sqlcg/server/tools.py) `_bare_ref`** (N3): splits on all
  `.`; verify behaviour against namespaced keys (unlikely code path — CTE keys
  have no schema prefix — but assert it in a test).
- Files: `src/sqlcg/parsers/base.py`, possibly `src/sqlcg/server/noise_filter.py`,
  `src/sqlcg/server/tools.py` (display only), tests.
- Acceptance (unit): two fixture files each defining a CTE `final` with one
  shared column name produce **disjoint** dst keys; `trace_column_lineage` from
  file A's target returns no node whose `file` is file B.
- Acceptance (DWH): CTE-collision counter 218 → 0 after reindex; scoped edge
  health unchanged (CTE edges were already excluded); total edge count change
  documented (collided edges that were dedup-merged across files may now
  legitimately increase the edge count — record the delta, don't gate on it).

## Test Strategy

- **Unit**: each acceptance above, named `test_<unit>_<scenario>_<expected>`
  per developer.md, docstrings linking this plan. Key-normalization tests assert
  observable graph rows, not "no exception".
- **Integration**: in-memory DuckDB end-to-end per PR (index fixture → query
  keys), mirroring `tests/integration` patterns.
- **Perf invariants**: every PR runs the named-regression suite
  (`test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`,
  `test_upsert_batch_invariant.py`) plus `test_perf_scaling_guard.py`. The
  normalizer (PR 1) and `USE` tracking (PR 2) are once-per-file /
  once-per-statement — if the scaling guard reddens, the implementation is
  wrong, not the guard.
- **Live DWH verification** (user-gated, per `feedback_no_close_verify_before_tag`):
  after each merge the user reindexes the DWH and confirms the §G counters
  before tagging.

## Acceptance Criteria (sprint-level)

- [ ] `sqlcg gain` exposes CTE-collision and rescuable-unqualified counters
      (baselines 218 / 828 reproduced on the v1.14.2 graph).
- [ ] After PR 1 + reindex: zero strict-bad edges into `*_tmp.*` tables whose
      base is catalogued (was 597).
- [ ] After PR 1 + reindex: zero graph keys with a leading dot or empty table
      component (was 311 edges + 1 empty `SqlTable` row); stage reads either
      keyed `stage://<name>` or dropped with a logged skip.
- [ ] After PR 2 + reindex: rescuable-unqualified ≤ 50 (was 828);
      `doorbelasting` out of blindspot top-10.
- [ ] After PR 3 + reindex: CTE-collision counter = 0 (was 218); a trace through
      a previously-collided CTE returns single-file lineage only.
- [ ] Strict edge health ≥ 85% on the DWH (was 82.7%); scoped ≥ 95.8% (no
      regression).
- [ ] All perf-invariant tests green on every PR; indexing wall-time within the
      210–256s DWH baseline band (`project_indexer_perf_baseline`).

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Choke-point normalizer misses a key-bearing field added later | Unit test enumerates ParsedFile's TableRef-bearing fields via dataclass introspection and fails if a new field appears un-normalized |
| `USE` default mis-qualifies a ref that Snowflake would resolve differently (e.g. fully-qualified `db.schema.table` semantics) | Only qualify refs with `db is None`; never touch refs with any existing qualifier; dialect tests for `use db.schema` two-part form |
| `::` delimiter breaks an unaudited consumer | Step 4.2 is an explicit consumer audit; grep for key-splitting on `.` outside coverage.py before merge |
| CTE namespacing changes edge counts and shifts metrics unexpectedly | PR 4 lands first; postmortem records the full §G before/after per PR |
| Scripting-mode (`parsing_mode='scripting'`) statements bypass a fix | doorbelasting anchor IS a scripting-mode file — its acceptance covers this path |
| Two same-named CTEs within one file still collide after PR 3 | Accepted residual (lexically rare); collision counter would still catch it if it produces multi-*query* writes within the file — note in postmortem |

## 7. Baseline reproducers (v1.14.2 graph, sha d015f8c)

### 7.1 F1 anchors
```sql
-- aliased target, un-aliased edges (75 rows, all inferred_from_source_name):
SELECT q.target_table, cl.dst_key FROM COLUMN_LINEAGE cl
JOIN SqlQuery q ON q.id = cl.query_id
WHERE q.id = '/home/ignwrad/Projects/dwh/etl/pdi/template/wtfa_website_sessie_us50655.sql:2';
-- un-aliased target (90 rows):
SELECT target_table FROM SqlQuery
WHERE id = '/home/ignwrad/Projects/dwh/etl/sql/fact/wtfa_loonkosten.sql:3';
-- the 597 lever:
WITH bad AS (SELECT cl.dst_key,
  left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.')) AS dst_t
  FROM COLUMN_LINEAGE cl
  WHERE NOT EXISTS (SELECT 1 FROM HAS_COLUMN hc WHERE hc.dst_key = cl.dst_key))
SELECT COUNT(*) FROM bad WHERE dst_t LIKE '%_tmp.%'
  AND EXISTS (SELECT 1 FROM HAS_COLUMN hc WHERE hc.src_key = replace(dst_t,'_tmp.','.'));
-- = 597
-- empty-identity stage keys (leading dot) + the phantom empty table node:
SELECT COUNT(*) FROM COLUMN_LINEAGE WHERE src_key LIKE '.%';   -- = 311
SELECT COUNT(*) FROM SqlTable WHERE qualified = '';            -- = 1
-- producers: etl/sql/da/hyb/rthyb_promotion_normal.sql (58), rthyb_product_concept.sql (45),
-- rthyb_product_nonconcept.sql (45), etl/sql/da/dyn/rtdyn_factuur_regel.sql (23) — all
-- CREATE_TABLE statements selecting $1 / metadata$filename from @stage refs.
```

### 7.2 F2 anchor
`ddl/changelogs/IA-OUTBOUND/MSSPR_IA_OUTBOUND_DOORBELASTING.sql` —
line 21 `use schema IA_OUTBOUND;`, line 419 `insert into DOORBELASTING`;
107 edges `doorbelasting.*`; catalogued columns live under
`ia_outbound.doorbelasting`. The 828 lever:
```sql
WITH bad AS (SELECT left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.')) AS dst_t
  FROM COLUMN_LINEAGE cl
  WHERE NOT EXISTS (SELECT 1 FROM HAS_COLUMN hc WHERE hc.dst_key = cl.dst_key))
SELECT COUNT(*) FROM bad WHERE instr(dst_t,'.') = 0
  AND EXISTS (SELECT 1 FROM HAS_COLUMN hc WHERE hc.src_key LIKE '%.' || dst_t);
-- = 828
```

### 7.3 F3 anchor
```sql
WITH cte_dst AS (SELECT cl.dst_key, q.file_path FROM COLUMN_LINEAGE cl
  LEFT JOIN SqlQuery q ON q.id = cl.query_id
  WHERE left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
        IN (SELECT qualified FROM SqlTable WHERE kind IN ('cte','derived')))
SELECT COUNT(*) FILTER (WHERE nf > 1), SUM(edges) FILTER (WHERE nf > 1)
FROM (SELECT dst_key, COUNT(DISTINCT file_path) nf, COUNT(*) edges
      FROM cte_dst GROUP BY 1);
-- = 218 colliding columns, 1110 edges
```
