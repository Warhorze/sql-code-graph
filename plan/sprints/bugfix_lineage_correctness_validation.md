# Feature Plan: Bugfix — Lineage-Correctness Validation Findings (#2, #4, #5)

**Status: REVIEWED** (plan-reviewer returned REWORK — 1 blocker + 1 warning; shepherd
folded both 2026-06-14: PR-A narrowed to DDL+CLONE-catalogued disambiguation, the
information_schema-only case moved to Non-Goals + a follow-up sprint; forbidden-path check
PASSED, all anchors verified, four frozen perf suites pinned.)

## Summary

Three MAJOR column-lineage *correctness* bugs surfaced by serialized live-DWH
validation against ground-truth git diffs. All three produce missing or wrong
`COLUMN_LINEAGE` edges (not crashes, not usability). They are distinct from the
in-flight trust/usability sprint (#151/152/153 — **do not touch that**) and from
the spaced/quoted-identifier work (#153 — **out of scope here**).

| Bug | Severity | One-line | Verified |
|-----|----------|----------|----------|
| #5 | MAJOR | Unqualified columns in a 2-table join bind to the FIRST table (or drop entirely) | CONFIRMED (root cause = sqlglot `validate_qualify_columns=False` first-table fallback; catalog not reaching qualify) |
| #4 | MAJOR | `INSERT … SELECT *` `_TMP`→persisted produces no column edge | CONFIRMED, **but only in a narrower shape than reported** (see §Bug #4) |
| #2 | MAJOR | Multi-column staging temp resolves as 1-of-14 columns → upstream of a payment fact points at a PHANTOM backup table | CONFIRMED as a *consequence* of #4 + #5 (see §Bug #2) — not an independent root cause |

> **Reproduction artifacts**: all findings below were reproduced on the current
> tree (master @ `7a6d572`, v1.32.1) with the `SnowflakeParser` and a real
> end-to-end `sqlcg index` run. The exact edge tables are quoted inline. No
> live-DWH index slot was used for verification — synthetic fixtures modelling
> the live shapes were sufficient to reproduce each.

---

## Scope

### In Scope
- Bug #5: disambiguate unqualified columns in multi-table joins using the DDL +
  information_schema catalog already available to the parser, instead of letting
  sqlglot silently bind them to the first FROM/JOIN table.
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
- Any parse-time feeding of CTE/CTAS *bodies* into `exp.expand()` / `qualify()`
  (CLAUDE.md hard prohibition — the zero-measured-win, perf-regressing path).
- Changing the star-expansion SQL to iterate to a fixpoint across arbitrarily deep
  temp chains (the single-pass expansion already chains 2 levels when the leaf is
  catalogued; deeper chains are a separate measured question).

---

## Root-Cause Analysis (verified against current source)

### Bug #5 — unqualified columns in a 2-table join

**Symptom (live DWH):** in a join of two tables, unqualified column references all
resolve to the FIRST table, so the second source table gets no edge.

**Reproduced (current tree):**
```sql
SELECT amount, status FROM orders o JOIN customers c ON o.cid = c.id
```
- With **no catalog for `orders`/`customers`**: both columns hit
  `col_lineage_skip:dynamic_source` → **zero edges emitted** (not even to the first
  table). sqlglot's `qualify(..., validate_qualify_columns=False)` leaves the
  columns unqualified (`amount AS amount`), so `build_scope`/`sg_lineage` cannot
  attach them to any table.
- With a **correct schema** passed to `qualify`, sqlglot binds them correctly
  (`amount → orders`, `status → customers`) — verified directly against sqlglot.
- The "all bind to the FIRST table" symptom is the live-DWH manifestation: when
  the schema is *partial* (the column exists on the first table but the second
  table's columns are absent from the catalog), `validate_qualify_columns=False`
  greedily assigns every unqualified column to the first relation that plausibly
  owns it — the first FROM table.

**Root cause:**
[`base.py:1162-1170`](src/sqlcg/parsers/base.py) — the per-statement `qualify()`
call already receives `schema=schema`, but `schema` is built ONLY from
`SchemaResolver.as_dict()` ([`ansi_parser.py:184`](src/sqlcg/parsers/ansi_parser.py)),
which is populated from parsed `CREATE TABLE` DDL bodies. The richer catalog —
`ddl_columns_by_bare` — is threaded into `_extract_column_lineage`
([`base.py:1029`](src/sqlcg/parsers/base.py)) but is **only consulted for
positional-INSERT ordinal mapping** ([`base.py:1333-1336`](src/sqlcg/parsers/base.py)).
It is **never merged into the `schema=` dict that `qualify` uses to disambiguate
join columns.**

> **CORRECTION (plan-review REWORK, folded by shepherd):** an earlier draft claimed
> `ddl_columns_by_bare` contains "DDL **plus** information_schema **plus** CLONE". That
> is FALSE against the current tree. `ddl_columns_by_bare` is populated ONLY from parsed
> `CREATE TABLE`/`CREATE VIEW` DDL ([`aggregator.py:96-103`](src/sqlcg/lineage/aggregator.py))
> plus CLONE resolution ([`aggregator.py:150-157`](src/sqlcg/lineage/aggregator.py)).
> **information_schema columns NEVER reach qualify time** — they enter post-index via
> `apply_catalog_to_backend` ([`catalog.py:202-243`](src/sqlcg/cli/commands/catalog.py)),
> emitting `HAS_COLUMN(source='information_schema')` directly to the backend AFTER parsing.
> **Consequence:** PR-A (merging `ddl_columns_by_bare` into the qualify schema) can
> disambiguate joins ONLY for tables whose DDL is parsed in the indexed corpus (plus
> CLONE-inherited columns). The information_schema-only case — likely the dominant LIVE
> Bug #5 shape — is **NOT** fixed by PR-A and is now an explicit **Non-Goal** (see below);
> full coverage needs a separate post-index re-qualify design (tracked as a follow-up).

**This is NOT the forbidden path.** CLAUDE.md prohibits passing CTE/CTAS *bodies*
into `exp.expand()`/`qualify()`. Passing a column-NAME `schema` dict to `qualify`
is the existing, sanctioned mechanism (the `schema=schema` arg already present).
Merging catalogue column NAMES into that same dict is additive and stays
once-per-statement.

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
temp (explicit CTAS projection), a real source **whose columns are resolvable at the
relevant stage**, AND a same-bare-named backup table with DDL, asserting (a) all 14 temp
columns get `HAS_COLUMN`, (b) the payment-fact promote edges point at the real source's
columns, and (c) **no** edge points at the backup table.

> **REWORK gap #2 (folded):** the "real source" MUST be given **parsed `CREATE TABLE`
> DDL** (so its columns are in `as_dict()`/`ddl_columns_by_bare` at qualify/derivation
> time) — NOT an information_schema-only catalog entry, which would not resolve via this
> sprint's mechanism and would make the fixture fail for a reason unrelated to the fix.
> If the intent is to model the information_schema-cataloged source, that goes through
> **PR-B's `sqlcg catalog load` path** and the fixture must state explicitly which
> resolution route it exercises. Do not conflate the two catalog sources. This is the criterion the v1.21.0-class dual-write green-on-toy-fixture
failure warns about — the fixture MUST contain the backup table so the phantom edge
*can* appear if the fix is wrong.

---

## Non-Goals (explicit — folded from plan-review REWORK)

- **information_schema-only join-column disambiguation.** PR-A enriches the qualify
  schema from `ddl_columns_by_bare` (parsed DDL + CLONE only). Tables catalogued
  *exclusively* via `sqlcg catalog load` (information_schema) are invisible at qualify
  time and remain ambiguous → today's graceful first-table/`dynamic_source` behavior is
  unchanged for them. **This likely leaves the dominant LIVE Bug #5 shape unfixed by this
  sprint.** Closing it requires a separate, larger **post-index re-qualify** design
  (reviewer's "option (b)") that re-resolves join columns against the backend's
  information_schema `HAS_COLUMN` rows after indexing — out of scope here because it
  changes the hot-loop contract and needs its own plan-review. **Tracked as a follow-up
  (next sprint).**
- **Star-from-star where the ROOT source has no columns anywhere** (no DDL, no
  information_schema) — the XML-DDL coverage gap (Bug #4 residue), unchanged.

## PR Sequencing (by risk; combined where files overlap, split where they don't)

Two PRs. Bug #5 and Bug #2 both center on `base.py` qualify/schema handling and
share the catalog-merge mechanism → **PR-A**. Bug #4 centers on temp-column
derivation (`ansi_parser.py` + indexer/star-expansion) → **PR-B**. Bug #2's
acceptance is the integration criterion verified after **both** land.

Sequence: **PR-A first** (lowest blast radius — it only *enriches* the schema dict
qualify already receives), then **PR-B** (touches column derivation + the
star-expansion contract). Bug #2's compound acceptance is checked last.

---

### PR-A — Bug #5: catalog-aware join-column disambiguation

**Problem:** unqualified columns in multi-table joins bind to the first FROM table
(or drop) because the per-statement `qualify()` schema omits the **CLONE-inherited
DDL catalog columns** present in `ddl_columns_by_bare` but absent from
`SchemaResolver.as_dict()`. (information_schema-only tables are out of scope — see
Non-Goals; they never reach qualify time.)

**Root-cause file:line:**
- [`base.py:1162-1170`](src/sqlcg/parsers/base.py) — `qualify(..., schema=schema)`.
- [`base.py:1333-1336`](src/sqlcg/parsers/base.py) — `ddl_columns_by_bare` consulted
  only for positional-INSERT, never merged into `schema`.
- [`ansi_parser.py:184`](src/sqlcg/parsers/ansi_parser.py) — `schema_dict` built from
  `SchemaResolver.as_dict()` only.

**Proposed fix (design):**
- **Once per statement, before the column loop**, build the qualify `schema` dict by
  merging the source tables' catalog column NAMES from `ddl_columns_by_bare` into the
  existing `schema` dict, keyed by the bare table name as it appears in the FROM/JOIN.
  - Build the merged dict ONCE per statement (not per column) — identical cardinality
    to the existing once-per-statement `qualify` call.
  - Only column NAMES are merged (sqlglot's qualify needs names, not types — produce
    the name-list form `{table: [col, …]}` that `as_dict` already produces).
  - Precedence: existing `as_dict()` parsed-DDL schema entries win over
    `ddl_columns_by_bare` entries on collision. (Both are DDL-sourced; the only delta
    `ddl_columns_by_bare` adds is CLONE-inherited columns + the bare-name keying.
    information_schema is NOT in either source at qualify time.)
- Pass the merged dict as `schema=` to the **first** `qualify` attempt
  ([`base.py:1163`](src/sqlcg/parsers/base.py)). The existing schema-free retry on
  failure ([`base.py:1177-1184`](src/sqlcg/parsers/base.py)) stays untouched.
- **Constant alignment:** the bare-name key form MUST match `ddl_columns_by_bare`'s
  key convention (lowercased bare name — confirm against `CrossFileAggregator`
  before implementing) and the `SchemaResolver.as_dict()` nesting
  (`{catalog: {db: {table: [cols]}}}` per [`schema_resolver.py:158`](src/sqlcg/lineage/schema_resolver.py)).
  The developer must trace the exact `as_dict` shape and produce a merged dict in the
  SAME shape — do not invent a flat shape qualify won't accept.

**Acceptance criteria (observable — assert real edges):**
- [ ] `SELECT amount, status FROM orders o JOIN customers c ON o.cid=c.id`, with
      `orders` catalogued via parsed DDL and `customers` catalogued via a CLONE-inherited
      entry in `ddl_columns_by_bare` (NOT information_schema — that path never reaches
      qualify), emits `orders.amount → tgt.amount` AND `customers.status → tgt.status`-style
      edges — i.e. the SECOND table gets an edge. Assert BOTH edges exist and that NO edge
      attributes `status` to `orders`.
- [ ] A 3+-column unqualified-join projection where columns split across both tables
      attributes each column to its correct owning table.
- [ ] Regression: an already-qualified join (`a.x, b.y`) is unchanged (still emits
      the same edges) — no double-counting, no new skip markers.
- [ ] Degrade: when neither source table is catalogued, behavior is unchanged from
      today (graceful `dynamic_source` skip; no wrong first-table edge fabricated).

**Test strategy:**
- Unit (parser): drive `SnowflakeParser.parse_file` (or `_extract_column_lineage`)
  with a `ddl_columns_by_bare` modelling one parsed-DDL source and one CLONE-inherited
  source (both are what `ddl_columns_by_bare` can actually hold at qualify time); assert
  the emitted `LineageEdge` src tables.
  Fixture MUST use realistic aliasing (`o`, `c`) and at least one column present on
  BOTH tables to exercise the disambiguation (the failure mode cannot appear if every
  column is unique to one table).
- Integration (end-to-end via `sqlcg index`): index a fixture dir with a DDL file +
  an information_schema catalog load + the join ETL; assert the `COLUMN_LINEAGE`
  rows in DuckDB point at the correct source tables.

**Version bump:** patch → **1.32.2** (bug fix only; no new surface).
Update [`pyproject.toml`](pyproject.toml), [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py),
`uv lock`.

**Live-DWH index slot:** YES (recommended for final acceptance) — re-run the live
validation join case and confirm second-table edges appear. Not required for CI.

**Perf-invariant constraint (CLAUDE.md):**
- The four frozen suites — [`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py),
  [`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py) — MUST pass
  UNMODIFIED.
- The schema-merge MUST be built **once per statement, before the column loop** — NO
  new per-column op in the `base.py` hot loop. No new `qualify`/`build_scope`/
  `exp.expand`/`sg_lineage` call is added; the existing single `qualify` call simply
  receives a richer (already-constructed) dict.
- Add a behavioural assertion to the scaling guard if a new per-statement op-count
  axis is warranted (merge built once, not N_cols times).

---

### PR-B — Bug #4: derive temp columns for `SELECT *` CTAS so the promote star expands

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
  so, narrow PR-B to the information_schema-catalogued-source guarantee + the Bug #2
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

**Version bump:** patch → **1.32.3** (after PR-A merges; bug fix only).

**Live-DWH index slot:** YES (recommended) — confirm the payment-fact promote chain
appears against the real corpus.

**Perf-invariant constraint:** same four frozen suites pass UNMODIFIED. Any
parse-time column derivation (Option 2) MUST be once per CREATE statement, AST-only
— no qualify/build_scope/exp.expand/sg_lineage, no per-column work. The
star-expansion SQL stays single-pass (no fixpoint loop added in this PR).

---

### Bug #2 compound acceptance (verified after PR-A + PR-B)

Bug #2 has no standalone code PR — it is fixed by PR-A (correct join binding so all
temp columns resolve) + PR-B (full temp-column population for star promotes). It
gets a dedicated **integration acceptance test** that models the live failure:

**Acceptance criteria:**
- [ ] A fixture with: a 14-column staging temp (`pay_stg_TMP`), a real source
      (`payments` with 14 catalogued columns), a same-bare-named backup table
      (`payment_fact_BACKUP` with DDL), and the promote
      `INSERT INTO payment_fact SELECT * FROM pay_stg_TMP`, produces:
  - all 14 `pay_stg_TMP` columns with `HAS_COLUMN`;
  - 14 `payment_fact.<col>` edges sourced from `payments.<col>` (via the temp);
  - **ZERO** `COLUMN_LINEAGE` edges attributing any `payment_fact` column to the
    backup table.
- [ ] The fixture MUST contain the backup table with DDL so the phantom edge *can*
      appear if the fix is incomplete (no green-on-toy-fixture).

**Live-DWH index slot:** YES — final acceptance re-runs the payment-fact validation
case and confirms (a) 14-of-14 columns, (b) no phantom backup edge.

---

## Cross-cutting constraints (apply to every PR)

- **No backward compatibility** — re-index is the migration path (CLAUDE.md).
- **No TODO in the happy path** of any fix.
- **Every new method must have a grep-confirmed call site** before the PR opens.
- **Path/constant fallbacks must match `KuzuConfig`** — never hardcode the DB path
  in fixtures; drive via `SQLCG_DB_PATH` or config.
- **Tests assert observable output** (real edges in DuckDB / real `LineageEdge`
  objects), never "no exception raised".
- The four perf-invariant suites pass UNMODIFIED after each PR.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| PR-A schema merge accidentally feeds CTE/CTAS *bodies* into qualify (forbidden path) | Merge ONLY column NAME lists from the catalog, never `exp.Query` bodies. Plan-reviewer to confirm the merged dict contains no AST nodes. |
| Per-column cost creep in `base.py` hot loop | Schema merge built once per statement before the loop; behavioural scaling-guard assertion. |
| Bug #4 "fix" is a no-op because Option 1 is already satisfied | Developer re-confirms repro FIRST; if the catalogued-source chain already works, narrow PR-B to the information_schema variant + Bug #2 phantom guard. |
| Green on a toy fixture that cannot exhibit the phantom (v1.21.0-class) | Bug #2 fixture mandates the backup table with DDL + a column shared across source and backup. |
| qualify regresses on a corpus where a catalog entry is wrong | DDL precedence over information_schema; the schema-free retry path is the existing safety net. |
| Uncatalogued source tables (XML-DDL gap) make some live cases still fail | Explicit Non-Goal; degrade is honest-empty, not wrong. Tracked separately. |
