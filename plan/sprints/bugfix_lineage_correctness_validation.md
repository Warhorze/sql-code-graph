# Feature Plan: Bugfix — Lineage-Correctness Validation Findings (#2, #4, #5)

**Status: REVIEWED** (option-b design gated by plan-reviewer 2026-06-14 — forbidden
parse-time-schema check PASSED, all key-forms + the four frozen perf suites verified.
Returned REWORK on ONE blocker: the resolver was wired only into the full-index path,
not the two incremental paths (`resync_changed` @indexer.py:1330, `reindex_file`
@indexer.py:1383) — an incremental reindex of a join file would have permanently
dropped its join-column edges. Shepherd FOLDED 2026-06-14: resolver now wired into all
three call sites (no catalog reapply in the incremental paths — they resolve against
persisted HAS_COLUMN); added an incremental-reindex regression acceptance test; pinned
the one-owner high-confidence value and a case-mismatched-CSV fixture (notes 3+4) into
acceptance criteria; reconciled version 1.33.0→**1.34.0** and SCHEMA_VERSION "9"→"10"
to **"10"→"11"** (PR #154 took v1.33.0/v10 on master first). PR-4 Option-1-vs-2 stays
deferred to the developer's repro re-confirmation as the plan directs — dev reports the
choice before opening PR-4. READY FOR INDEX-HEAVY DISPATCH.

> *(Superseded prior status — DRAFT/REWORKED 2026-06-14: maintainer decision to replace
> Bug #5's narrowed DDL+CLONE qualify-schema merge with option (b), the post-index
> re-resolution pass; information_schema case now IN scope; PR-4 star-expansion kept.)*

## Summary

Three MAJOR column-lineage *correctness* bugs surfaced by serialized live-DWH
validation against ground-truth git diffs. All three produce missing or wrong
`COLUMN_LINEAGE` edges (not crashes, not usability). They are distinct from the
in-flight trust/usability sprint (#151/152/153 — **do not touch that**) and from
the spaced/quoted-identifier work (#153 — **out of scope here**).

| Bug | Severity | One-line | Verified |
|-----|----------|----------|----------|
| #5 | MAJOR | Unqualified columns in a 2-table join all bind to the FIRST table (or drop), because the SECOND table's columns are catalogued only via information_schema and never reach parse-time qualify | CONFIRMED (root cause = parse-time qualify cannot see information_schema columns; they enter post-index) |
| #4 | MAJOR | `INSERT … SELECT *` `_TMP`→persisted produces no column edge | CONFIRMED, **but only in a narrower shape than reported** (see §Bug #4) |
| #2 | MAJOR | Multi-column staging temp resolves as 1-of-14 columns → upstream of a payment fact points at a PHANTOM backup table | CONFIRMED as a *consequence* of #4 + #5 (see §Bug #2) — not an independent root cause |

> **Reproduction artifacts**: all findings below were reproduced on the current
> tree (master @ `7a6d572`, v1.32.1) with the `SnowflakeParser` and a real
> end-to-end `sqlcg index` run. The exact edge tables are quoted inline. No
> live-DWH index slot was used for verification — synthetic fixtures modelling
> the live shapes were sufficient to reproduce each.

---

## Why the narrowed PR-A was abandoned (maintainer decision, 2026-06-14)

The prior REVIEWED draft narrowed PR-A to: merge `ddl_columns_by_bare` (parsed
`CREATE TABLE`/`CREATE VIEW` DDL + CLONE-inherited columns) into the per-statement
`qualify()` schema dict, so the second join table's columns are visible at qualify
time. That fix is correct **only** when the second table's DDL is parsed in the
indexed corpus.

The dominant LIVE Bug #5 shape is the opposite: the second join table's columns
live **only** in `HAS_COLUMN(source='information_schema')`, loaded **post-index**
by `apply_catalog_to_backend`
([`catalog.py:202-245`](src/sqlcg/cli/commands/catalog.py)) — they are NEVER in
`ddl_columns_by_bare` ([`aggregator.py:91-103`](src/sqlcg/lineage/aggregator.py))
and NEVER reach parse-time `qualify()`
([`base.py:1162-1170`](src/sqlcg/parsers/base.py)). The maintainer's verdict:
shipping the narrowed merge would pass CI on DDL-only fixtures while leaving the
live failure intact (the v1.21.0-class green-on-toy-fixture trap). **Do not ship
it.** Plan option (b) — a post-index re-resolution pass — instead.

Feeding information_schema columns INTO parse-time `qualify()` is the **forbidden
path** (CLAUDE.md "Schema resolution" + "Performance invariants"): the
measured-zero, perf-regressing hot-loop cost class. Option (b) stays entirely in
the **post-index world** the catalog already lives in.

---

## Scope

### In Scope
- **Bug #5 (option b):** a post-index re-resolution pass that disambiguates
  unqualified columns in multi-table joins against the **full** catalog — DDL +
  **information_schema** + usage — available only AFTER indexing in `HAS_COLUMN`.
  This fixes the dominant live shape (second table catalogued via
  `sqlcg catalog load` / information_schema), which the narrowed PR-A could not.
- Bug #4: emit a real `COLUMN_LINEAGE` edge for `INSERT … SELECT *` promotes out
  of a `_TMP` table **whose columns are derivable** (the temp has a column-list
  DDL, an explicit-projection CTAS, or a catalogued upstream).
- Bug #2: ensure a multi-column staging temp contributes ALL its columns to the
  promote, removing the phantom-backup misattribution — delivered transitively by
  fixing #4 and #5 plus one explicit guard against the phantom backup edge.

### Non-Goals
- **Spaced / quoted-identifier resolution** — that is #153, a separate sprint.
- **Ingesting Liquibase XML CDATA DDL** — the deepest root of "uncatalogued source
  table" coverage (tracked separately, see MEMORY `blindspot_xml_ddl_lever`). When a
  join's or temp's source table has NO columns anywhere in the graph (no DDL, no
  information_schema), these fixes degrade gracefully (no wrong edge) but cannot
  conjure the missing columns. That gap is explicitly out of scope.
- Any parse-time feeding of CTE/CTAS *bodies* — or information_schema columns — into
  `exp.expand()` / `qualify()` (CLAUDE.md hard prohibition — the zero-measured-win,
  perf-regressing path). Option (b) does NOT touch parse-time qualify.
- **Disambiguation when a bare column name exists on BOTH source tables and the
  catalog cannot break the tie** (e.g. both tables have a `status` column). Option
  (b) resolves the *unambiguous* case (the bare name exists on exactly one of the
  query's source tables) and OVER-ATTRIBUTES the genuinely-ambiguous case (one edge
  per candidate table, low confidence) — the same safe failure mode the existing
  `CTE_PROJECTION_AMBIGUOUS` path already uses
  ([`base.py:1725-1740`](src/sqlcg/parsers/base.py)). It never silently picks one.
- Changing the star-expansion SQL to iterate to a fixpoint across arbitrarily deep
  temp chains (the single-pass expansion already chains 2 levels when the leaf is
  catalogued; deeper chains are a separate measured question).

> **NOTE — what moved IN scope vs. the prior draft.** The prior REVIEWED draft
> listed "information_schema-only join-column disambiguation" as a Non-Goal /
> follow-up. That follow-up is now THIS sprint's PR-5 (option b). The XML-DDL
> coverage gap (source table absent from the graph entirely) remains out of scope.

---

## Root-Cause Analysis (verified against current source)

### Bug #5 — unqualified columns in a 2-table join (option b)

**Symptom (live DWH):** in a join of two tables, unqualified column references all
resolve to the FIRST table, so the second source table gets no edge.

**Reproduced (current tree):**
```sql
INSERT INTO tgt SELECT amount, status
FROM orders o JOIN customers c ON o.cid = c.id
-- orders: parsed DDL.  customers: catalogued ONLY via `sqlcg catalog load`.
```
- With **no catalog for `orders`/`customers`**: both columns hit
  `col_lineage_skip:dynamic_source` → zero edges, or sqlglot binds both to the
  first relation (`amount → orders`, `status → orders`).
- With `customers` in **information_schema only**: parse-time `qualify()` cannot
  see it (information_schema is post-index), so `status` is mis-bound to `orders`
  (or dropped). The wrong edge is `orders.status → tgt.status`; the correct edge
  `customers.status → tgt.status` is MISSING.

**Root cause (parse-time, unfixable at qualify):**
[`base.py:1162-1170`](src/sqlcg/parsers/base.py) — the per-statement `qualify()`
receives `schema=schema`, built ONLY from `SchemaResolver.as_dict()`
([`ansi_parser.py:184`](src/sqlcg/parsers/ansi_parser.py)) (parsed-DDL bodies).
information_schema columns enter the graph only post-index via
`apply_catalog_to_backend` ([`catalog.py:202-245`](src/sqlcg/cli/commands/catalog.py)),
emitting `HAS_COLUMN(source='information_schema')` directly to the backend. **They
are structurally unavailable at qualify time.** No parse-time merge can fix this
case without reintroducing the forbidden information_schema-into-qualify path.

**The fix must therefore run post-index**, when the full catalog is present.

> **This is NOT the forbidden path.** CLAUDE.md prohibits feeding schema-CSV /
> information_schema / CTE-CTAS bodies into `exp.expand()`/`qualify()` in the
> parser hot loop. Option (b) adds NO parser-loop work: it (a) emits a lightweight
> parse-time *marker* for ambiguous bare join columns (one row per such column,
> built inside the existing once-per-statement projection loop with the same
> cardinality as the existing `CTE_PROJECTION_AMBIGUOUS` emit), and (b) resolves
> those markers in a **post-index SQL pass** against `HAS_COLUMN`, mirroring
> `expand_star_sources` ([`duckdb_backend.py:843-869`](src/sqlcg/core/duckdb_backend.py)).

### Bug #4 — `INSERT … SELECT *` `_TMP`→persisted promote, no column edge

**Symptom (live DWH):** promoting a `_TMP` into a persisted table via
`INSERT … SELECT *` emits no `COLUMN_LINEAGE` edge — the star is not expanded
against the temp's known columns at promote time.

**Reproduced — what WORKS today (no fix needed):**
- `CREATE TEMP stg_TMP AS SELECT s.c1 AS c1, s.c2, s.c3 FROM raw_src` then
  `INSERT INTO persisted SELECT * FROM stg_TMP` → **edges present**
  (`raw_src.cN → stg_tmp.cN → persisted.cN`, `transform=STAR_EXPANSION`). The
  explicit-projection CTAS gives the temp `defined_columns` → `HAS_COLUMN(ddl)` →
  star expansion fires.
- `CREATE TEMP stg_TMP (c1,c2,c3); INSERT INTO stg_TMP SELECT …; INSERT INTO
  persisted SELECT * FROM stg_TMP` → **edges present** (column-list DDL gives the
  temp its `HAS_COLUMN(ddl)` rows).
- `CREATE TABLE raw_src(a,b,c); CREATE TEMP stg_TMP AS SELECT * FROM raw_src;
  INSERT INTO persisted SELECT * FROM stg_TMP` → **edges present and chained**
  (`raw_src → stg_tmp → persisted`): the single-pass star expansion resolves the
  two-level chain when the leaf source is catalogued.

**Reproduced — what BREAKS (the real bug):**
```sql
CREATE TEMPORARY TABLE stg_TMP AS SELECT * FROM raw_src;   -- raw_src NOT catalogued
INSERT INTO persisted_fact SELECT * FROM stg_TMP;
```
- `stg_TMP` gets **NO `defined_columns`**: `_extract_select_output_columns`
  ([`ansi_parser.py:756-758`](src/sqlcg/parsers/ansi_parser.py)) skips star
  projections, returning an empty list → **no `HAS_COLUMN` rows** for the temp.
- The promote registers a `STAR_SOURCE(query → stg_tmp)` correctly, but the star
  expansion SQL
  ([`queries.sql:97-149`](src/sqlcg/core/queries.sql)) JOINs `STAR_SOURCE → SqlTable
  → HAS_COLUMN → SqlColumn` — with the temp having zero `HAS_COLUMN` rows, **zero
  edges are produced.**

**Root cause:** the star-from-star chain dies at the deepest hop only when the
**root source table's columns are unknown to the graph** (external table, no DDL,
no information_schema). When the root is catalogued, the existing single-pass
expansion already works (Bug4-C2 above). So the *fixable* portion of Bug #4 is:
make the temp's promote benefit from the catalogue when the temp's own columns are
derivable from a catalogued upstream. The unfixable residue (root source has no
columns anywhere) is the XML-DDL coverage gap (Non-Goal).

> **Scope-smell check (CLAUDE.md):** the reported "no edge" is NOT a one-line TODO.
> It is a genuine two-level star-expansion gap whose fix interacts with the catalog.
> Do not reduce it to "add a log line."

### Bug #2 — multi-column staging temp parsed as 1-of-14 columns → phantom backup

**Symptom (live DWH):** a ~14-column staging temp is parsed as if it had only 1
column, so the upstream lineage of a core payment fact resolves to a PHANTOM
backup table instead of the real source.

**Reproduced / root cause:** Bug #2 is **not an independent parser bug** — it is the
compound effect of #4 and #5 on a real payment-fact pipeline:
1. The staging temp is a `SELECT *` CTAS (or built by joins with unqualified
   columns), so only the column(s) the parser *could* statically resolve land in
   `defined_columns` / `HAS_COLUMN` (the "1-of-14" symptom is exactly the Bug #4
   star-CTAS-with-uncatalogued-source path, where only a single explicitly-aliased
   projection survives).
2. With the temp under-populated, the promote's star expansion attaches the
   payment-fact columns to whatever table DOES have matching `HAS_COLUMN` rows —
   in the live corpus a same-named **backup** table (`*_BACKUP` / `*_BU`) whose DDL
   IS catalogued — producing the phantom edge.

**Verification approach for the fix:** a fixture that models a 14-column staging
temp (explicit CTAS projection), a real source **catalogued via the same route the
live corpus uses** (information_schema via `sqlcg catalog load` — now resolvable
through PR-5's post-index pass), AND a same-bare-named backup table with DDL,
asserting (a) all 14 temp columns get `HAS_COLUMN`, (b) the payment-fact promote
edges point at the real source's columns, and (c) **no** edge points at the backup
table.

> The fixture MUST contain the backup table so the phantom edge *can* appear if the
> fix is wrong — this is the criterion the v1.21.0-class dual-write
> green-on-toy-fixture failure warns about. The real source is now catalogued via
> **information_schema** (PR-5's resolution route), not parsed DDL, so the fixture
> models the live configuration that produced the phantom.

---

## PR Sequencing (REWORKED)

Two code PRs. **PR-5** (Bug #5, option b — the post-index re-resolution pass) is now
the larger, higher-blast-radius change: it adds a parse-time marker, a new edge
table, a schema-version bump, and a post-index SQL pass. **PR-4** (Bug #4 — temp
star-column derivation; KEPT AS-IS from the prior draft's PR-B) is unchanged.
Bug #2's acceptance is the integration criterion verified after BOTH land.

Sequence: **PR-5 first** (it establishes the post-index resolution machinery and the
full-catalog HAS_COLUMN read that Bug #2's correct-source attribution depends on),
then **PR-4** (temp-column derivation + the star-expansion contract). Bug #2's
compound acceptance is checked last.

> **Why PR-5 before PR-4 (changed from the prior PR-A→PR-B order):** Bug #2's
> phantom-backup misattribution is driven *primarily* by the join columns binding to
> the wrong table (#5), and the correct-source resolution now depends on PR-5's
> post-index pass reading information_schema. Landing PR-5 first lets the Bug #2
> acceptance test exercise the real resolution route. The two PRs otherwise touch
> disjoint code paths.

---

### PR-5 — Bug #5: post-index re-resolution of unqualified join columns (option b)

**Problem:** unqualified columns in a multi-table join bind to the FIRST table (or
drop) because the second table's columns are catalogued ONLY via information_schema,
which is unavailable at parse-time `qualify()`. The correct binding is only knowable
AFTER indexing, when `HAS_COLUMN(source='information_schema')` rows exist.

**Design — two cooperating pieces (parse-time marker + post-index resolver),
mirroring the existing `STAR_SOURCE` → `expand_star_sources` pattern:**

#### Piece 1 — parse-time marker (no new hot-loop op)

Today the top-level INSERT…SELECT projection loop calls `sg_lineage` per projection
and emits an edge to whatever table sqlglot bound the bare column to
([`base.py:1421-1440`](src/sqlcg/parsers/base.py) and the main column loop below
it). The CTE branch already has the precedent we extend: when a CTE body has ≥2
source tables and a bare column, it does NOT trust sqlglot's mis-bind — it emits one
`CTE_PROJECTION_AMBIGUOUS` edge per candidate source table
([`base.py:1701-1740`](src/sqlcg/parsers/base.py)).

PR-5 extends that *same* over-attribution-instead-of-mis-bind discipline to the
TOP-LEVEL join projection, but **defers resolution to post-index** instead of
emitting one edge per candidate immediately:

- When a top-level projection's expression contains a **bare** column (`exp.Column`
  with no `.table` qualifier) AND the statement's source set has **≥2 tables**,
  the parser records a marker row instead of the sqlglot edge for that column:
  `JOIN_COL_RESOLVE(query_id, dst_col_id, bare_col)`.
- This is built INSIDE the existing once-per-projection loop — the bare-column scan
  (`expr.find_all(exp.Column)`) is the SAME scan the `CTE_PROJECTION_AMBIGUOUS` path
  already runs ([`base.py:1720-1724`](src/sqlcg/parsers/base.py)), and the
  source-table count is the SAME `query_sources` already computed once per statement.
  **No new `qualify`/`build_scope`/`exp.expand`/`sg_lineage` call is added; no new
  per-column op is added to the hot loop** — the marker is a dict-append, identical
  in cardinality to the existing edge append.
- **Marker storage:** a new edge table `JOIN_COL_RESOLVE`
  (`src_key=query_id`, `dst_key=<dst_table>.<dst_col>` SqlColumn id, plus a `bare_col`
  VARCHAR column) added to `_EDGE_DDLS`
  ([`duckdb_backend.py:111-202`](src/sqlcg/core/duckdb_backend.py)) and to `RelType`
  ([`schema.py:19-31`](src/sqlcg/core/schema.py)). Bump `SCHEMA_VERSION` from `"10"`
  to `"11"` ([`schema.py:6`](src/sqlcg/core/schema.py)) — re-index is the migration
  path (CLAUDE.md "No backward compatibility"). Add it to `clear_all_tables`
  ([`duckdb_backend.py:894-907`](src/sqlcg/core/duckdb_backend.py)) and the
  file-delete cascade ([`duckdb_backend.py:745-770`](src/sqlcg/core/duckdb_backend.py))
  so reindex/incremental delete the temp markers like any other query-scoped edge.
  Register its property list in the prop-name map next to the other edges
  ([`duckdb_backend.py:301-303`](src/sqlcg/core/duckdb_backend.py)).

> **Marker-vs-edge precedence (developer to confirm against the over-attribution
> precedent):** the marker must SUPPRESS the sqlglot mis-bind edge for that
> projection (otherwise the wrong `orders.status` edge survives alongside the
> resolved one). The cleanest design mirrors the CTE branch: for an ambiguous bare
> top-level projection, the parser emits NO COLUMN_LINEAGE edge for that column and
> emits the marker instead — exactly as the CTE branch `continue`s past `sg_lineage`
> after emitting `CTE_PROJECTION_AMBIGUOUS` ([`base.py:1740`](src/sqlcg/parsers/base.py)).
> The post-index pass is then the SOLE producer of that column's edges. Qualified
> projections (`o.amount`) are untouched: they have a `.table`, fail the bare test,
> take the existing `sg_lineage` path unchanged.

#### Piece 2 — post-index SQL resolver (mirrors `expand_star_sources`)

A new backend method `resolve_join_columns()` on `DuckDBBackend`
([`duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py), next to
`expand_star_sources` at line 843) and a query in
[`queries.sql`](src/sqlcg/core/queries.sql) (next to `EXPAND_STAR_SOURCES_LINEAGE`).
It resolves each `JOIN_COL_RESOLVE` marker against the full post-index catalog:

```sql
-- RESOLVE_JOIN_COLUMNS (sketch — developer writes the exact DML)
-- For each marker (query_id, bare_col, dst): find which of the query's
-- SELECTS_FROM source tables OWNS a column named bare_col (any HAS_COLUMN
-- source: ddl | information_schema | star_expansion | usage).  Emit a
-- COLUMN_LINEAGE edge src=<owning_table>.<bare_col> → dst.
INSERT OR REPLACE INTO "COLUMN_LINEAGE"
  (src_key, dst_key, transform, confidence, query_id, inferred_from_source_name)
SELECT
  hc.dst_key                AS src_key,   -- the owning table's SqlColumn id
  m.dst_key                 AS dst_key,
  'JOIN_COL_RESOLVED'       AS transform,
  <conf>                    AS confidence,
  m.src_key                 AS query_id,
  FALSE                     AS inferred_from_source_name
FROM "JOIN_COL_RESOLVE" m
JOIN "SELECTS_FROM" sf  ON sf.src_key = m.src_key        -- query's source tables
JOIN "HAS_COLUMN"   hc  ON hc.src_key = sf.dst_key       -- columns those tables own
JOIN "SqlColumn"    c   ON c.id = hc.dst_key AND c.col_name = m.bare_col
...
```

**Confidence / over-attribution rule (the disambiguation contract):**
- **Exactly one** source table owns `bare_col` → high confidence (e.g. `0.9`); this
  is the resolved, correct edge — the dominant live case (the second table is now
  visible via information_schema).
- **More than one** source table owns `bare_col` → genuinely ambiguous: emit ONE
  edge per owning table at LOW confidence (e.g. `0.5`, mirroring
  `CTE_PROJECTION_AMBIGUOUS`'s `confidence=0.5`
  [`base.py:1737`](src/sqlcg/parsers/base.py)). Over-attribution is the documented
  safe failure mode for impact analysis — never silently pick one.
- **Zero** source tables own `bare_col` (no DDL, no information_schema anywhere) →
  emit NOTHING (honest empty; the XML-DDL coverage gap, a Non-Goal). The marker
  produces no fabricated first-table edge.

> The high/low-confidence split can be done either in one SQL pass with a window
> COUNT over owning tables, or in two passes; the developer chooses, but the
> observable contract above is fixed by the acceptance criteria.

**Pipeline hook (constant alignment — critical):** the resolver MUST run AFTER the
catalog is applied, so information_schema columns are present in `HAS_COLUMN`. The
index pipeline order is fixed at
[`indexer.py:849-883`](src/sqlcg/indexer/indexer.py):
`_expand_star_sources` (line 850) → external consumers → `set_indexed_sha` →
`_reapply_catalog_if_configured` (line 881, `_t_catalog_end` at 883). PR-5 inserts
the new call `self._resolve_join_columns(db)` **immediately after line 883**, before
the error-classification block at line 885. Add a thin
`_resolve_join_columns(self, db)` wrapper next to `_expand_star_sources`
([`indexer.py:1922-1931`](src/sqlcg/indexer/indexer.py)) that calls
`db.resolve_join_columns()` and returns the count, plus the `GraphBackend` ABC stub
next to `expand_star_sources` ([`graph_db.py:257-263`](src/sqlcg/core/graph_db.py)).

> **BLOCKING (plan-review 2026-06-14 — incremental/resync paths MUST also resolve):**
> `_expand_star_sources` has **three** call sites, not one. The full-index path is
> [`indexer.py:850`](src/sqlcg/indexer/indexer.py) (hooked above). The two incremental
> paths also run star expansion but the prior draft left them unhooked:
> - `resync_changed` (the `sqlcg reindex` delta path) — star expansion at
>   [`indexer.py:1330`](src/sqlcg/indexer/indexer.py) (Step 8).
> - `reindex_file` (single-file incremental) — star expansion at
>   [`indexer.py:1383`](src/sqlcg/indexer/indexer.py).
>
> Because the marker design SUPPRESSES the sqlglot mis-bind edge (`continue` past
> `sg_lineage`) and the file-delete cascade ([`duckdb_backend.py:745-770`](src/sqlcg/core/duckdb_backend.py))
> deletes both the old `JOIN_COL_RESOLVE` markers AND the old resolved `COLUMN_LINEAGE`
> edges (via `query_id`), an incremental `reindex_file`/`resync_changed` on a join file
> re-creates the markers but never resolves them → **the file's join-column edges vanish
> until the next full `sqlcg index`. That is a correctness REGRESSION this feature
> introduces, not a mere omission.** Fix: add `self._resolve_join_columns(db)` to BOTH
> incremental paths, immediately after their `_expand_star_sources` call —
> [`indexer.py:1330`](src/sqlcg/indexer/indexer.py) (after Step 8) and
> [`indexer.py:1383`](src/sqlcg/indexer/indexer.py) (after the star re-run in
> `reindex_file`).
>
> **Do NOT add a catalog reapply to these paths.** `resync_changed`/`reindex_file` do
> not call `_reapply_catalog_if_configured` (it lives only at line 881, full-index
> path) — and they should not. The information_schema `HAS_COLUMN` rows from the prior
> full index PERSIST in the DB across a single-file reindex, so the resolver in these
> paths correctly resolves against the already-persisted catalog. The grep-confirmed
> call sites for the new `_resolve_join_columns` are therefore THREE: `indexer.py` 883,
> 1330, 1383.

> **Constant-alignment notes for the developer (do not guess):**
> - `SqlColumn.id` = `<table_qualified>.<col_name>`
>   ([`queries.sql:97-104`](src/sqlcg/core/queries.sql)); `SELECTS_FROM.dst_key` =
>   the source table's `qualified`. The resolver DML must use these exact key forms.
> - The marker's `dst_key` must equal the SqlColumn id the original projection would
>   have produced (`<dst_table>.<dst_col_name>`), using the SAME `ColumnRef`/`dst_table`
>   resolution the existing projection loop uses
>   ([`base.py:1730`](src/sqlcg/parsers/base.py)), so the resolved edge keys match the
>   rest of the graph.
> - The bare-name key for `JOIN_COL_RESOLVE.bare_col` must match the parsed bare name
>   (`exp.Column.name`) and the `SqlColumn.col_name` stored by the catalog
>   ([`catalog.py:184`](src/sqlcg/cli/commands/catalog.py)) — confirm casing
>   (information_schema rows store `col_name` as exported; the ambiguous-bare key
>   convention in the aggregator is lowercased
>   [`aggregator.py:96`](src/sqlcg/lineage/aggregator.py)). Trace which casing
>   `SqlColumn.col_name` actually carries before writing the equality join.
> - `SCHEMA_VERSION` lives at [`schema.py:6`](src/sqlcg/core/schema.py); the schema
>   gate rejects a stale DB on re-open, so the bump forces a clean re-index.

**Acceptance criteria (observable — assert real edges; run against a DB indexed
with an information_schema catalog loaded):**
- [ ] **Dominant live case.** Fixture: `orders` parsed-DDL with column `amount`;
      `customers` catalogued ONLY via `sqlcg catalog load` (information_schema) with
      column `status`; ETL `INSERT INTO tgt SELECT amount, status FROM orders o JOIN
      customers c ON o.cid=c.id`. After index + catalog load, the `COLUMN_LINEAGE`
      rows include `orders.amount → tgt.amount` AND `customers.status → tgt.status`.
      Assert BOTH edges exist, and that NO edge attributes `status` to `orders`. This
      is the case the narrowed PR-A could NOT fix. **Also assert the resolved
      one-owner edges carry the HIGH confidence value** (e.g. `0.9`) — not merely that
      the edge exists (plan-review note 3: the high/low split is part of the contract,
      so pin the value).
- [ ] **Case-insensitive owner match (plan-review note 4).** The dominant-case fixture
      MUST use a catalog CSV whose column casing DIFFERS from the SQL — e.g. CSV column
      `STATUS` (information_schema-exported casing) vs SQL `status`. A naive
      case-sensitive equality join (`c.col_name = m.bare_col`) MUST fail this test;
      the resolver normalizes casing on both sides. This guards against shipping
      green-on-toy-fixture and breaking live (the live IA_* corpus has exported-case ids).
- [ ] **Incremental reindex preserves join edges (plan-review BLOCKER — regression
      guard).** Full-index the join fixture WITH the information_schema catalog loaded,
      assert `customers.status → tgt.status` exists; then `reindex_file` the join file
      (single-file incremental, no catalog reapply); assert the
      `customers.status → tgt.status` edge STILL exists afterward (the resolver ran in
      the incremental path against the persisted `HAS_COLUMN` rows). A `resync_changed`
      variant of the same assertion is recommended. Without this, the incremental path
      silently drops the edge until the next full index.
- [ ] **Both sources get edges.** A 3+-column unqualified-join projection where
      columns split across both tables attributes each column to its correct owning
      table (`orders` columns → orders, `customers` columns → customers), with one
      table catalogued via DDL and the other via information_schema.
- [ ] **Genuine ambiguity → over-attribute, never mis-pick.** When `bare_col` exists
      on BOTH source tables, the pass emits one `JOIN_COL_RESOLVED` edge per owning
      table at the documented low confidence — never a single confident wrong edge.
- [ ] **Safe-identifier regression.** An already-qualified join (`o.amount, c.status`)
      is unchanged: no marker emitted, no `JOIN_COL_RESOLVED` edge, the existing
      `sg_lineage` edges are byte-identical to today (same src/dst/transform). Assert
      the qualified case produces ZERO `JOIN_COL_RESOLVE` markers.
- [ ] **Degrade (XML-DDL gap).** When neither source table is catalogued anywhere
      (no DDL, no information_schema), the pass emits NO edge for the bare column and
      NO fabricated first-table edge — honest empty, matching today's graceful skip.
- [ ] **No mis-bind survivor.** For the dominant-case fixture, assert there is NO
      stray `orders.status → tgt.status` edge left over from the suppressed sqlglot
      bind (the marker must replace, not augment, the mis-bind).

**Test strategy (describe by behavior; developer names tests
`test_<unit>_<scenario>_<expected>` and links this plan in the docstring):**
- Integration (end-to-end, the load-bearing tier): index a fixture dir containing a
  DDL file for `orders`, an information_schema catalog CSV cataloguing `customers`,
  and the join ETL; run `sqlcg catalog load` (or configure `[sqlcg.catalog]` so the
  index reapplies it); assert the `COLUMN_LINEAGE` rows in DuckDB. The fixture MUST
  catalogue the second table via the information_schema CSV route — NOT parsed DDL —
  so it exercises the exact path the narrowed PR-A could not, and models the live
  configuration (USE SCHEMA context / schema aliases as the live corpus has them).
  Fixtures MUST use realistic aliasing (`o`, `c`).
- Unit (parser): assert that a ≥2-table top-level join projection with a bare column
  emits a `JOIN_COL_RESOLVE` marker (and suppresses the sqlglot edge), while a
  qualified projection emits none. Verify the marker carries the correct
  `query_id` / `bare_col` / `dst` keys.
- Unit (resolver SQL): seed `JOIN_COL_RESOLVE` + `SELECTS_FROM` + `HAS_COLUMN`
  (one row `source='information_schema'`) directly and assert `resolve_join_columns()`
  emits the right `COLUMN_LINEAGE` rows for the one-owner, two-owner, and zero-owner
  cases.

**Version bump:** minor → **1.34.0** (new edge table + new post-index pass +
SCHEMA_VERSION bump = new surface/capability; SemVer per CLAUDE.md "Releasing").
Update [`pyproject.toml`](pyproject.toml), [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py),
`uv lock`.

> **VERSION/SCHEMA RECONCILED 2026-06-14:** the prior draft targeted 1.33.0 + SCHEMA_VERSION
> `"9"→"10"`, but PR #154 (tool-version stamp) landed first and CLAIMED both `v1.33.0`
> AND `SCHEMA_VERSION "10"` on master. PR-5 therefore shifts up: version **1.34.0** and
> SCHEMA_VERSION bump **`"10"→"11"`** ([`schema.py:6`](src/sqlcg/core/schema.py)) — master
> is now at `"10"`. Re-confirm the on-master `SCHEMA_VERSION` value at dispatch (it is `"10"`
> after #154 merged) and bump to the next integer.

**Live-DWH index slot:** YES (recommended for final acceptance) — re-run the live
validation join case and confirm second-table (information_schema-catalogued) edges
appear. Not required for CI.

**Perf-invariant constraint (CLAUDE.md):**
- The four frozen suites — [`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py),
  [`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py) — MUST pass
  UNMODIFIED.
- **No new per-column op in `base.py`'s hot loop.** The marker emit reuses the
  existing once-per-projection bare-column scan and the once-per-statement
  source-table set; it adds NO `qualify`/`build_scope`/`exp.expand`/`sg_lineage`
  call. If a new per-statement op-count axis is warranted, add a behavioural
  assertion to the scaling guard (marker emit is once-per-ambiguous-projection, never
  re-qualifies).
- The post-index resolver is a constant number of `execute()` calls (mirrors
  `expand_star_sources`'s three-step DML) — it does NOT scale per column or per file.
  It runs ONCE per index_repo call, after the catalog reapply, like the star pass.
  It MUST use set-based SQL (one INSERT … SELECT), never a Python row loop calling
  `upsert_*` per marker (bulk-upsert invariant).

---

### PR-4 — Bug #4: derive temp columns for `SELECT *` CTAS so the promote star expands

> **KEPT AS-IS from the prior draft's PR-B.** Renamed PR-B → PR-4 only for
> sequencing clarity; design, root cause, acceptance, and perf constraints are
> unchanged.

**Problem:** `INSERT … SELECT *` out of a `_TMP` produces no edge when the temp was
built as `CREATE TEMP … AS SELECT *` from a source — the temp has no `HAS_COLUMN`
rows, so the promote's `STAR_SOURCE` expansion finds nothing to expand.

**Root-cause file:line:**
- [`ansi_parser.py:644-649`](src/sqlcg/parsers/ansi_parser.py) — CTAS output columns
  derived via `_extract_select_output_columns`, which
  [skips star projections](src/sqlcg/parsers/ansi_parser.py) at lines 756-758,
  leaving `defined_columns` empty for a `SELECT *` CTAS.
- [`queries.sql:97-149`](src/sqlcg/core/queries.sql) — star expansion requires the
  source temp to have `HAS_COLUMN` rows; empty temp ⇒ zero edges.
- [`duckdb_backend.py:843-863`](src/sqlcg/core/duckdb_backend.py) — single-pass
  `expand_star_sources` (chains 2 levels only when the leaf is catalogued).

**Proposed fix (design) — pick ONE, plan-reviewer to confirm:**
- **Option 1 (preferred — reuse existing star machinery):** for a
  `CREATE TEMP/TABLE t AS SELECT * FROM src`, ensure a `StarSource(query→src)` is
  registered for the CREATE (so the temp's OWN columns get populated by the existing
  star expansion from `src`'s `HAS_COLUMN`). Verified-working precedent: Bug4-C2
  already chains `raw_src → stg_tmp → persisted` once `src` is catalogued — and the
  repro showed a `STAR_SOURCE(query0 → raw_src)` row already exists for the CTAS.
  **Developer must first re-confirm whether Option 1 is already satisfied** (i.e. the
  only residual break is when `src` itself has no columns anywhere — a Non-Goal). If
  so, narrow PR-4 to the information_schema-catalogued-source guarantee + the Bug #2
  phantom guard, and pin the working chains as regression guards.
- **Option 2 (if Option 1 insufficient):** when `defined_columns` is empty for a
  CTAS whose body is `SELECT *` and the single source IS catalogued
  (`ddl_columns_by_bare`/`as_dict`), populate `defined_columns` from the source's
  catalog column list at parse time — once per CREATE statement, AST-only, no
  qualify/expand. This makes `HAS_COLUMN(ddl)` rows exist for the temp directly.

**Acceptance criteria (observable):**
- [ ] `CREATE TABLE raw_src(a,b,c); CREATE TEMP stg_TMP AS SELECT * FROM raw_src;
      INSERT INTO persisted SELECT * FROM stg_TMP` emits the full chain
      `raw_src.{a,b,c} → stg_tmp.{a,b,c} → persisted.{a,b,c}` (already PASSES today —
      pin it as a guard so a refactor cannot regress it).
- [ ] When `raw_src` is catalogued ONLY via information_schema (`sqlcg catalog load`,
      not parsed DDL), the same chain is produced (this is the live-DWH-representative
      case).
- [ ] Degrade: when `raw_src` has NO columns anywhere in the graph, the promote emits
      no fabricated/phantom edge (no wrong edge; honest empty — matches Non-Goal).
- [ ] The promote into a persisted table whose own DDL exists prefers the temp's
      real source columns over the persisted table's DDL ordinal guess.

**Test strategy:**
- Integration (end-to-end `sqlcg index`): the four cases above, asserting the
  `COLUMN_LINEAGE` rows (src_key/dst_key/transform) in DuckDB. Fixtures MUST use the
  `_TMP` naming and `CREATE TEMPORARY` shape from the live corpus, and the
  information_schema variant MUST load the catalog via `sqlcg catalog load` (not parsed
  DDL) so it models the live configuration.
- Unit (parser): assert `defined_columns` / `star_sources` for the `SELECT *` CTAS
  temp shape.

**Version bump:** patch → **1.33.1** (after PR-5 merges; bug fix only, no new surface).

**Live-DWH index slot:** YES (recommended) — confirm the payment-fact promote chain
appears against the real corpus.

**Perf-invariant constraint:** same four frozen suites pass UNMODIFIED. Any
parse-time column derivation (Option 2) MUST be once per CREATE statement, AST-only
— no qualify/build_scope/exp.expand/sg_lineage, no per-column work. The
star-expansion SQL stays single-pass (no fixpoint loop added in this PR).

---

### Bug #2 compound acceptance (verified after PR-5 + PR-4)

Bug #2 has no standalone code PR — it is fixed by PR-5 (correct join binding so all
temp columns resolve to the right source, including the information_schema-catalogued
real source) + PR-4 (full temp-column population for star promotes). It gets a
dedicated **integration acceptance test** that models the live failure:

**Acceptance criteria:**
- [ ] A fixture with: a 14-column staging temp (`pay_stg_TMP`), a real source
      (`payments` with 14 columns **catalogued via information_schema**), a
      same-bare-named backup table (`payment_fact_BACKUP` with DDL), and the promote
      `INSERT INTO payment_fact SELECT * FROM pay_stg_TMP`, produces:
  - all 14 `pay_stg_TMP` columns with `HAS_COLUMN`;
  - 14 `payment_fact.<col>` edges sourced from `payments.<col>` (via the temp);
  - **ZERO** `COLUMN_LINEAGE` edges attributing any `payment_fact` column to the
    backup table.
- [ ] The fixture MUST contain the backup table with DDL so the phantom edge *can*
      appear if the fix is incomplete (no green-on-toy-fixture). The real source is
      catalogued via information_schema (PR-5's resolution route), so the fixture
      exercises the live configuration.

**Live-DWH index slot:** YES — final acceptance re-runs the payment-fact validation
case and confirms (a) 14-of-14 columns, (b) no phantom backup edge.

---

## Cross-cutting constraints (apply to every PR)

- **No backward compatibility** — re-index is the migration path (CLAUDE.md). The
  PR-5 SCHEMA_VERSION bump forces a clean re-index; that is the intended migration.
- **No TODO in the happy path** of any fix.
- **Every new method must have a grep-confirmed call site** before the PR opens
  (`resolve_join_columns`, `_resolve_join_columns`).
- **Path/constant fallbacks must match `KuzuConfig`** — never hardcode the DB path
  in fixtures; drive via `SQLCG_DB_PATH` or config.
- **Tests assert observable output** (real edges in DuckDB / real `LineageEdge`
  objects), never "no exception raised".
- The four perf-invariant suites pass UNMODIFIED after each PR.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| PR-5 marker emit accidentally reintroduces parse-time schema feeding (forbidden path) | The marker carries only NAMES (query_id, bare_col, dst keys) — never an AST node or schema dict; resolution is post-index SQL against HAS_COLUMN. Plan-reviewer to confirm no `schema=`/`sources=` change to the `qualify`/`expand` calls. |
| Per-column cost creep in `base.py` hot loop | Marker emitted inside the existing once-per-projection bare-column scan; no new qualify/scope/expand/lineage call. Behavioural scaling-guard assertion: marker emit scales with ambiguous projections, NOT with re-qualify calls. |
| Resolver scales per marker (Python row loop) | Resolver is a single set-based INSERT … SELECT (mirrors `expand_star_sources`), constant execute() count per index run; never a per-marker upsert. |
| Resolver runs before catalog applied → information_schema columns missing | Hook the call AFTER `_reapply_catalog_if_configured` (indexer.py:881-883), verified pipeline order. Acceptance test indexes WITH a catalog load to prove the information_schema path. |
| Green on a DDL-only fixture that can't exhibit the information_schema case (the v1.21.0 / narrowed-PR-A trap) | The dominant-case acceptance fixture catalogues the second table ONLY via the information_schema CSV route — NOT parsed DDL. A PASS requires the post-index pass to read information_schema. |
| Mis-bind survivor: the suppressed sqlglot edge leaks alongside the resolved one | Explicit acceptance criterion asserting NO stray first-table edge; parser must replace (not augment) the mis-bind, mirroring the CTE branch `continue`. |
| Genuine ambiguity (bare name on both tables) silently picks one | Over-attribute (one edge per owning table, low confidence) — documented safe failure mode; never a single confident wrong edge. |
| Bug #4 "fix" is a no-op because Option 1 is already satisfied | Developer re-confirms repro FIRST; if the catalogued-source chain already works, narrow PR-4 to the information_schema variant + Bug #2 phantom guard. |
| SCHEMA_VERSION bump breaks existing DBs | Intended — re-index is the migration path; the schema gate rejects stale DBs on re-open with a clear message. |
| Uncatalogued source tables (XML-DDL gap) make some live cases still fail | Explicit Non-Goal; degrade is honest-empty, not wrong. Tracked separately. |
