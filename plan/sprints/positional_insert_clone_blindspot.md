# Feature Plan: Column-lineage blind spot on CLONE / positional-INSERT fact tables

## Summary

Fact tables that are (1) created via `CREATE TABLE … CLONE …` (no column-list DDL to
catalog from) and (2) loaded by **positional `INSERT INTO … SELECT`** with no column
aliases get their lineage destination nodes named after the **source expression**, not
the **target column**. The graph therefore carries a phantom node
(`ba.wtfs_voorraad_dagstand.mh_vrije_vrd`) and lacks the real one (`…ma_vrije_vrd`), so
`get_upstream_dependencies` / `find_table_usages` return **empty** at that boundary even
though 48+ `COLUMN_LINEAGE` edges exist — keyed under the wrong column name.

Full diagnosis: [`dwh_positional_insert_column_blindspot.md`](../reports/dwh_positional_insert_column_blindspot.md).

This plan ships **all of the statically-tractable** report follow-ups: disambiguating the
empty-closure signal (follow-up 1), giving positional INSERT a target-column catalog
whenever one is derivable and flagging the node as source-name-inferred when one is not
(follow-ups 2 + 3, scoped honestly), **and — folded in per user decision 2026-06-08 —
CLONE-source column cataloguing (follow-up 3, the CLONE half)**: parsing
`CREATE TABLE … CLONE <source>` and inheriting the source table's `SqlColumn`/`HAS_COLUMN`
catalog so a CLONE-built fact gets a real target-column catalog. This is the piece that
actually **corrects** the report's trigger table `ba.wtfs_voorraad_dagstand` (gives the #25
/ Part-B positional override a real catalog to map against, producing the true `ma_vrije_vrd`
node) rather than merely labelling the gap. It **explicitly defers** the one sub-case that
genuinely cannot be solved by static parsing — `SELECT alias.*` per-column star resolution
(report follow-up 4) — with recorded reasoning, so a future ticket can pick it up without
re-discovering why.

> **Scope-expansion note (2026-06-08):** an earlier revision of this plan *deferred* CLONE
> cataloguing as a separate multi-hop schema-inheritance feature. The user decided to fold
> it into this same plan/PR. The CLONE work below (Part C) is re-derived with its own perf
> and pipeline-placement analysis; the previously-deferred reasoning is preserved in
> "Part C — design notes & why the placement is safe" so the trade-offs stay visible.

> Static reads below are pinned to current `master` (v1.5.1). Line numbers are anchors,
> not exact — the developer re-greps before editing.

---

## Pre-work findings (these reshape the obvious plan — read before scoping)

Verifying the four report follow-ups against current code materially changes what is
buildable, and the **scope expansion to fold in CLONE cataloguing changes the headline
again**. Two findings drive the design:

1. **The report's exact table has NO target-column catalog *from its own SQL* — but the
   CLONE source does.** `BA.WTFS_VOORRAAD_DAGSTAND` is born via
   `CREATE TABLE IF NOT EXISTS … CLONE …` and the `INSERT` is positional **with no column
   list**. Therefore:
   - There is no `CREATE TABLE (col …)` DDL on the table itself →
     [`_extract_defined_columns`](../src/sqlcg/parsers/ansi_parser.py#L400) returns `[]` for
     it; `defined_columns`/`HAS_COLUMN`/`SqlColumn` are empty (confirmed by the report's
     `SELECT COUNT(*) FROM SqlColumn … = 0`).
   - There is no `INSERT INTO t (c1, c2)` column list → the #25 positional-override block
     in [`base.py`](../src/sqlcg/parsers/base.py#L738) (`isinstance(stmt.this, exp.Schema)`)
     **does not fire**; the main loop runs and derives the dst name from the source
     expression (`col_expr.name`, [base.py:869](../src/sqlcg/parsers/base.py#L869)) — this
     is exactly where the phantom `mh_vrije_vrd` name comes from.
   - **`CLONE` is not parsed at all today** (grep: zero matches for `clone` in `parsers/`,
     `lineage/`, `indexer/` — only unrelated git-clone/shallow-clone strings). So the
     catalog that *does* exist — the CLONE **source** table's `CREATE TABLE (col …)`
     columns — is never harvested. **sqlglot already models it**: confirmed
     `sqlglot.parse_one("CREATE TABLE t CLONE s", dialect="snowflake")` yields
     `Create(kind=TABLE, this=Table(t), clone=Clone(this=Table(s)))`. The source ref is
     directly readable from `stmt.args["clone"].this` / `exp.Clone.this`.

   **Consequence (revised for the folded-in CLONE work):** a CLONE-built table *can* be
   given a real catalog by **inheriting the columns of its CLONE source** — provided that
   source is itself DDL-catalogued (directly, or transitively via another CLONE) **and is
   indexed in the same repo**. Part C below adds exactly this inheritance. The honest
   degrade path (direction 1 + the `inferred_from_source_name` flag) is **kept** as the
   safety net for the residual cases CLONE inheritance cannot reach: a CLONE source that is
   not in the indexed repo (cross-database CLONE), or a CLONE chain that bottoms out in a
   table with no DDL columns anywhere. Directions 2/3 (the positional-ordinal mapping, Part
   B) remain the machinery that *uses* the catalog — Part C feeds the catalog in; Part B
   maps positional projections onto it.

   **How the three parts compose on the report's table:**
   `Part C` catalogs `ba.wtfs_voorraad_dagstand` from its CLONE source's DDL → the table now
   has ordered `SqlColumn`s including `ma_vrije_vrd` at the right ordinal. `Part B` then sees
   a catalog for the positional `INSERT … SELECT` and maps projection *i* (`vrd.mh_vrije_vrd`)
   onto target column *i* (`ma_vrije_vrd`) → the real `MH_ → MA_` rename edge is recorded
   under the **correct** target node. `A1/A2` only fire if Part C found no source catalog
   (cross-DB / un-indexed source), in which case the table stays labelled-unproven instead
   of phantom.

2. **#25 already solves positional INSERT *with a column list*.** The block at
   [base.py:738](../src/sqlcg/parsers/base.py#L738) overrides the SELECT alias with the
   INSERT target column name at each position when `stmt.this` is an `exp.Schema` (the
   `(c1, c2, …)` form). **This is correct and in scope to preserve, not re-do.** The gap is
   only the **no-column-list** positional INSERT, where the authoritative target names must
   come from a *catalog* (the target's DDL columns), not from the statement itself.

3. **The empty-closure signal is genuinely ambiguous today and cheap to fix.**
   [`get_upstream_dependencies`](../src/sqlcg/server/tools.py#L1477) emits a generic hint
   when `nodes` is empty ([tools.py:1558](../src/sqlcg/server/tools.py#L1558)) that does
   **not** distinguish "the table has no column catalog at all" (this blind spot) from
   "this column genuinely has no upstream edges." A single extra read —
   [`GET_COLUMNS_FOR_TABLE`](../src/sqlcg/core/queries.sql#L199) for the parsed `table_id`
   returning zero rows — disambiguates them. This is the highest value-per-risk change in
   the report (it directly converts a silent false-negative into a labelled "unproven")
   and touches only the read surface — **no parser/indexer hot path, no perf invariant**.

4. **The aggregator already carries cross-file table identity** (`canonical_by_bare`,
   [aggregator.py:29](../src/sqlcg/lineage/aggregator.py#L29)) and a per-table DDL column
   catalog exists in [`SchemaResolver`](../src/sqlcg/lineage/schema_resolver.py#L36)
   (`_tables[(catalog,db,name)] = [col_names]`). A cross-file **bare-name → ordered DDL
   columns** index is the natural carrier for the no-column-list ordinal mapping, built in
   the same `register_pass1` loop that already builds `canonical_by_bare`. The mapping is
   applied **once per statement** (not per column) in `base.py`, mirroring the existing #25
   block's structure, so the per-column perf invariants are untouched.

Net effect: in-scope work is **(A)** the empty-closure disambiguation +
`inferred_from_source_name` consumer signal, **(B)** a no-column-list positional-INSERT
ordinal mapping against a cross-file DDL column catalog with a safe `inferred` fallback when
no catalog exists, **and (C — folded in) CLONE-source column cataloguing**: parse
`CREATE TABLE … CLONE src` and inherit `src`'s DDL column catalog onto the CLONE target so
that target gets real `SqlColumn`/`HAS_COLUMN` rows and Part B has a catalog to map against.
Only **per-column `SELECT alias.*` star resolution** (report follow-up 4) is deferred with
reasoning (see Non-Goals).

---

## Scope

### In Scope

- **(A1) Disambiguate empty closures** in
  [`get_upstream_dependencies`](../src/sqlcg/server/tools.py#L1477),
  [`get_downstream_dependencies`](../src/sqlcg/server/tools.py#L1349), and
  [`trace_column_lineage`](../src/sqlcg/server/tools.py#L733): when the result is empty,
  branch the hint on whether the table has a column catalog (`HAS_COLUMN` rows) at all.
  When the table has **zero** catalogued columns, surface a specific hint naming the
  CLONE / positional-INSERT blind spot and pointing at how to verify (raw `COLUMN_LINEAGE`
  may exist under a source-derived key). Read-surface only.
- **(A2) `inferred_from_source_name` provenance flag** on `COLUMN_LINEAGE` edges (and the
  derived destination `SqlColumn`) whose destination column name was taken from the source
  expression because no target catalog was available (the no-column-list positional INSERT
  with no resolvable catalog). Surface it on the relevant tool results so a consumer knows
  the key may not match the real DB column.
- **(B) No-column-list positional-INSERT ordinal mapping.** When an `INSERT INTO t SELECT …`
  has **no** column list and a cross-file **ordered DDL column catalog** for `t` is
  available, map the SELECT projection at position *i* to the target's *i*-th DDL column
  name (the authoritative target name), exactly mirroring the #25 column-list path's intent.
  Emit the destination node under the **target** column name (correctness restored), and do
  **not** set `inferred_from_source_name`. When **no** catalog is available, keep today's
  behaviour (dst named from source) but set `inferred_from_source_name=true` (A2).
- **(B-catalog) Cross-file ordered DDL column index** in
  [`CrossFileAggregator`](../src/sqlcg/lineage/aggregator.py): bare-name → ordered DDL
  column list, built from DDL-defined tables in the existing `register_pass1` loop.
  Ambiguous bare names (already tracked in `_ambiguous_bare`) are excluded — never guess a
  catalog across schemas. **Threading note:** this index is consumed in the **main-process
  upsert seam** (`index_repo` lines 416–475), not threaded into pass-2 worker subprocesses —
  see Part C placement analysis for why this is both correct and cheaper. The same index
  also feeds Part C's CLONE inheritance.
- **(C) CLONE-source column cataloguing** (folded in 2026-06-08):
  - **(C1) Parse `CREATE TABLE … CLONE src`.** Capture the CLONE source `TableRef` on the
    CLONE target's `QueryNode` (new `clone_source: TableRef | None` field) by reading
    `stmt.args["clone"].this` (`exp.Clone`) in
    [`_parse_statement`](../src/sqlcg/parsers/ansi_parser.py#L236). The CLONE target already
    registers as a `defined_table` with empty `defined_columns` today; C1 only *records the
    source link*, it does not yet change the catalog.
  - **(C2) Inherit the source catalog onto the CLONE target** in the main-process seam: for
    each CLONE target whose own `defined_columns` is empty, resolve the source's ordered DDL
    columns via the Part-B-catalog index (transitively following CLONE-of-CLONE chains, with
    a visited-set cycle guard and a bounded hop count), and **populate the CLONE target's
    catalog rows** (`SqlColumn` + `HAS_COLUMN`) from the inherited columns, tagged
    `source="clone_inherited"` (vs `"ddl"`). When the source is not in the index (cross-DB
    CLONE) or the chain bottoms out with no DDL columns, **inherit nothing** — the table
    stays catalog-less and A1/A2 label it (no fabrication).
- **Regression guards** (synthetic, committable) for every behaviour above, including a
  CLONE-of-CLONE chain and an un-indexed-source CLONE (degrade path).
- **Version bump** (minor per SemVer rule — see Phase 6).

### Non-Goals (deferred, with reasoning — do not silently drop)

- **Per-column `SELECT alias.*` star resolution (report follow-up 4).** The report flags
  this as a *second, independent gap*: `SELECT vrd.*` in the `*_base`/`*_enriched` temp CTEs
  is materialised as `STAR_SOURCE` rows expanded post-hoc by
  [`EXPAND_STAR_SOURCES`](../src/sqlcg/core/queries.sql#L93), not as per-column edges at
  parse time. Turning star into per-column lineage is a distinct effort (it changes the
  parse-time vs expansion-time contract and the perf profile of the expansion query) and the
  report itself says it "can be scoped out." **Out of scope**, recorded as a separate
  follow-up; this plan does not touch `STAR_SOURCE` / star expansion.
- **Any change to the per-column or per-statement hot-path invariants** in CLAUDE.md
  (qualify-once, body_scope-once, `copy=False`/`trim_selects=False`, INSERT body-copy-once,
  bulk/batched upsert). Part (B) adds work that is **once per statement** (build the ordinal
  alias map once, mirroring the #25 block which already copies the body once), never per
  column. See "Performance invariants" below for the row-by-row impact statement.
- **Re-introducing an INFORMATION_SCHEMA / schema-CSV ingestion path** (removed 2026-05 for
  zero measured edge delta — CLAUDE.md "Schema resolution"). The catalog used here — for both
  Part B and Part C — is the **parsed DDL** the indexer already has, not an external schema
  source. Part C inherits a CLONE source's *parsed-DDL* columns; it does not read live DB or
  INFORMATION_SCHEMA metadata.
- **CLONE inheritance from a source outside the indexed repo** (cross-database CLONE where
  `src` is never parsed). There is no parsed DDL to inherit, so Part C inherits nothing and
  A1/A2 label the table unproven. Resolving these would require live-DB metadata (out of
  scope by the rule above).

---

## Scope decision: the report's own table (CLONE + bare positional INSERT)

The trigger table `ba.wtfs_voorraad_dagstand` is the **worst case**: CLONE-built (no DDL
columns of its own) **and** loaded by a positional INSERT with no column list. With Part C
folded in, this table is now **corrected, not merely labelled** — *iff its CLONE source is
indexed*:

- **Part C** parses the table's `CREATE TABLE … CLONE src`, resolves `src`'s ordered DDL
  columns (directly or down a CLONE chain), and inherits them onto
  `ba.wtfs_voorraad_dagstand` as real `SqlColumn`/`HAS_COLUMN` rows
  (`source="clone_inherited"`), including `ma_vrije_vrd` at its true ordinal.
- **Part B** then sees a catalog for the positional `INSERT … SELECT`, maps projection *i*
  (`vrd.mh_vrije_vrd`) → target column *i* (`ma_vrije_vrd`), and records the real
  `MH_ → MA_` rename edge under the **correct** `ma_vrije_vrd` node. `inferred_from_source_name`
  is **false** on these edges (catalog-backed).
- `get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` now returns the
  upstream — the report's exact failing call is **fixed**, not just disambiguated.

**Residual / degrade path (A1 + A2 still earn their keep):** if the CLONE source is *not*
indexed (cross-database CLONE), or the CLONE chain bottoms out in a table with no DDL columns
anywhere, Part C inherits nothing and the table stays catalog-less. Then:

- **A1** makes the empty closure return the no-catalog hint (*unproven, not negative*).
- **A2** flags the source-named dst edge `inferred_from_source_name=true`.

We never fabricate a target name we cannot derive from parsed SQL. **Correct when a source
catalog exists; correctly-labelled "unproven" when it does not — never confidently wrong.**
Whether the report's *exact* table resolves end-to-end depends on whether its CLONE source
is in the indexed DWH; Phase 5 verifies this on the live corpus and records which path fired.

---

## Design

### Part A1 — empty-closure disambiguation (read surface)

Add a "does this table have any catalogued columns?" probe used only when a closure comes
back empty. The probe reuses the existing
[`GET_COLUMNS_FOR_TABLE`](../src/sqlcg/core/queries.sql#L199) query (joins
`HAS_COLUMN`→`SqlColumn`, returns the table's columns) — zero rows ⇒ no catalog.

In each of the three tools, in the existing `if not nodes:` / empty-result branch:

```python
# in get_upstream_dependencies / get_downstream_dependencies / trace_column_lineage,
# only when the result is empty:
cols = db.run_read(GET_COLUMNS_FOR_TABLE_QUERY, {"table_qualified": table_id})
if not cols:
    hint = (
        f"No column catalog for '{table_id}' (0 catalogued columns). This table was "
        "likely created via CREATE TABLE … CLONE and/or loaded by a positional "
        "INSERT … SELECT with no column list, so sqlcg has no authoritative target "
        "column names. Lineage edges may still exist keyed on a SOURCE-derived column "
        "name — check raw COLUMN_LINEAGE for this table. Empty here is UNPROVEN, not "
        "negative."
    )
else:
    hint = <the existing generic hint>
```

- **Cost:** one extra `run_read` only on the empty path (not the hot path; empty results
  are rare and already terminal). No traversal change.
- **Reuse, don't duplicate:** `trace_column_lineage`'s empty branch should call the same
  helper. Extract a small `_empty_closure_hint(db, table_id) -> str` so all three tools
  share one wording and one probe — grep-confirm the call sites (CLAUDE.md rule).
- **Observable:** the hint string is the asserted surface (TC-A1).

### Part A2 — `inferred_from_source_name` provenance

The destination of a no-column-list positional INSERT, when no catalog resolves it, is
named from the source expression. Mark that edge so consumers can tell.

- **Where the flag is set:** in [`base.py`](../src/sqlcg/parsers/base.py)'s main column loop,
  on the `LineageEdge` whose `dst` column name was derived from `col_expr.name`/alias for an
  `INSERT` statement that has **no column list and no resolved ordinal catalog**. The flag
  belongs on `LineageEdge` (a new field, default `False`) so it flows through
  `column_lineage` → `_build_file_rows` → the `COLUMN_LINEAGE` edge row.
- **Schema carrier:** add an `inferred_from_source_name BOOLEAN` column to the
  `COLUMN_LINEAGE` edge table in [`schema.py`](../src/sqlcg/core/schema.py) /
  [`duckdb_backend.py`](../src/sqlcg/core/duckdb_backend.py) and bump `SCHEMA_VERSION`
  (the schema gate — re-index is the migration path, CLAUDE.md). The
  [`_build_file_rows`](../src/sqlcg/indexer/indexer.py#L1211) `column_lineage_edges` dict
  gains the field; the bulk-upsert column list gains one column. **No new `execute()` per
  row** — it rides the existing per-batch bulk upsert.
- **Surfacing:** the tool result nodes that land on an inferred edge carry the flag (extend
  `DependencyNode` / the lineage-result node model in
  [`models.py`](../src/sqlcg/server/models.py) with an optional `inferred_from_source_name`).
  `trace_column_lineage` landing on such an edge sets it on the hop; the empty-closure hint
  (A1) is the table-level signal, this is the edge-level one.
- **Default value discipline:** every existing (DDL-catalogued, column-list, or resolved-
  ordinal) edge gets `False`. Only the genuinely-source-named dst is `True`. Verify against
  the constant that owns the edge schema (`schema.py`) — do not hardcode the column name in
  two places.

### Part B — no-column-list positional-INSERT ordinal mapping (parser, once per statement)

Mirror the **structure** of the existing #25 block ([base.py:738](../src/sqlcg/parsers/base.py#L738))
for the *no-column-list* case. The #25 block fires for `INSERT INTO t (c1,c2) SELECT …`
(`isinstance(stmt.this, exp.Schema)`). Add a sibling path for `INSERT INTO t SELECT …`
(`isinstance(stmt, exp.Insert)` and `stmt.this` is **not** a `Schema`) **iff** an ordered
DDL column catalog for `t` is available:

1. **Resolve the catalog once per statement:** look up `t`'s ordered DDL columns from the
   cross-file index (Part B-catalog) by bare name, excluding ambiguous names. If absent →
   do nothing here; the main loop runs as today and Part A2 flags the resulting edges.
2. **Build the positional override once** (mirror #25): the target column name at projection
   position *i* is `catalog[i]` (authoritative), not the source alias/name. Reuse the #25
   machinery — register `positional_col_names[i] = catalog[i]` so the main loop skips those
   positions, and wrap each projection as `Alias(inner, catalog[i])` before `sg_lineage`,
   exactly as the #25 block already does (`body_no_with` copied **once** before the loop;
   only the single projection swapped per column — CLAUDE.md INSERT body-copy-once invariant
   preserved). Apply the **same guards** the #25 block applies (star skip, pure-literal skip,
   no func_fallback for column-bearing exprs).
3. **Length mismatch handling:** if the SELECT has more projections than the catalog (or
   vice-versa), map only the positions that exist in both; positions beyond the catalog fall
   through to the main loop and are flagged `inferred_from_source_name` (A2). Do **not**
   raise. (Real DWH positional INSERTs occasionally project more than the catalog when the
   catalog is partial — degrade, don't crash.)

**Why this is once-per-statement, not per-column:** the catalog lookup is a single dict
read; the `positional_col_names` map and `body_no_with` copy are built once before the
per-column `sg_lineage` calls — identical cardinality to the existing #25 block. No extra
qualify/build_scope/expand. See the invariant table below.

### Part B-catalog — cross-file ordered DDL column index (aggregator → pass 2)

In [`CrossFileAggregator`](../src/sqlcg/lineage/aggregator.py):

```python
# __init__:
# bare name (lower) -> ordered DDL column list (from CREATE TABLE col order).
# Only populated for UNambiguous bare names; ambiguous ones are excluded so we
# never apply a wrong-schema catalog.
self.ddl_columns_by_bare: dict[str, list[str]] = {}

# register_pass1, in the existing `for table in parsed.defined_tables:` loop —
# pull the ordered defined_columns from the matching CREATE statement (the same
# defined_by_query lookup _build_file_rows uses, indexer.py:1081).
```

- **Source of column order:** `StatementNode.defined_columns` (already ordered, already
  lowercased — [ansi_parser.py:419](../src/sqlcg/parsers/ansi_parser.py#L419)). The
  aggregator pulls it from the parsed file's CREATE statements in `register_pass1`, keyed by
  bare name, skipping any bare name already in `_ambiguous_bare`.
- **Threading into pass 2:** the index is handed to the pass-2 parser the same way schema
  context is threaded today (via the pass-2 dispatch in `index_repo` → the worker's
  `parse_file(..., dependency_filter=...)` path / `SchemaResolver`). The developer confirms
  the exact seam in Phase 0 — **prefer reusing the existing `SchemaResolver` cross-file
  channel** ([`register_cross_file_sources`](../src/sqlcg/lineage/schema_resolver.py#L90)
  pattern) over inventing a new parameter, so the catalog rides an already-tested boundary.
- **No-op when absent:** single-file / `reindex_file` paths that have no aggregator pass the
  index as `None`; Part B then degrades to the source-named + flagged behaviour. Mirror the
  documented #44 single-file limitation ([v1_2_1_bugfix.md](v1_2_1_bugfix.md) Phase 2 "Known
  limitation").

### Part C — CLONE-source column cataloguing (parse + main-process inheritance)

#### C1 — parse the CLONE source (parser, once per statement)

In [`_parse_statement`](../src/sqlcg/parsers/ansi_parser.py#L236), when `stmt` is an
`exp.Create` with `kind=="TABLE"` and `stmt.args.get("clone")` is an `exp.Clone`, extract the
source `TableRef` from `clone.this` (a `Table`) via the existing
[`_convert_table_expr_to_ref`](../src/sqlcg/parsers/ansi_parser.py) and store it on the
`QueryNode` as a new field `clone_source: TableRef | None`.

- **No column lineage / no body change.** CLONE has no SELECT body, so column lineage
  extraction is skipped exactly as today (a CLONE create already produces empty
  `defined_columns` and `defined_body=None`). C1 is **metadata only** — one attribute read
  per CREATE statement, gated on `args.get("clone")` being present. Zero impact on the
  per-column hot path.
- **Dialect note.** CLONE is a Snowflake/BigQuery construct; sqlglot parses it under the
  `snowflake` dialect (verified). ANSI parses without `clone` (the arg is simply absent), so
  the `args.get("clone")` guard is a no-op for non-CLONE dialects — no parser branching
  beyond the single `if`.

#### C2 — inherit the source catalog (main-process seam, after both passes)

**Placement: the main-process `index_repo` seam at
[indexer.py:416–475](../src/sqlcg/indexer/indexer.py#L416), NOT pass-2 worker subprocesses.**
This is the central architectural decision for the folded-in work — see "why the placement is
safe" below. The `defined_table_registry` build loop (lines 430–433) already iterates **every
defined table across every file in the main process**, after pass 1 + pass 2 have completed
and before the upsert batches flush. Part C piggybacks on this single pass:

1. **Build a bare-name → ordered DDL columns index** (this is exactly the Part-B-catalog
   `ddl_columns_by_bare`, built in `register_pass1` from `defined_columns`, ambiguous names
   excluded). Part C reuses it — one index serves both B and C.
2. **Collect CLONE targets.** During the same `pass2_results` walk, gather
   `(clone_target_ref, clone_source_ref)` for every `QueryNode` carrying a `clone_source`
   whose target's own `defined_columns` is empty (a table with its own DDL column list does
   not inherit — its own catalog wins).
3. **Resolve each CLONE target's inherited catalog** by following the source link:
   - Look up the source's ordered DDL columns in `ddl_columns_by_bare` (by bare name,
     excluding ambiguous).
   - **CLONE-of-CLONE:** if the source itself is a catalog-less CLONE target, follow *its*
     `clone_source` link transitively. Use a **visited-set cycle guard** and a **bounded hop
     limit** (e.g. 16) so a pathological `A CLONE B … CLONE A` cycle or a deep chain
     terminates deterministically; on cycle/limit/no-DDL-found, inherit nothing.
   - **Source not indexed** (cross-DB CLONE — bare name absent from the index): inherit
     nothing.
4. **Emit inherited catalog rows** onto the CLONE target: for each inherited column at
   ordinal *i*, append a `SqlColumn` row and a `HAS_COLUMN` edge keyed on the CLONE
   *target*'s `full_id`, tagged `source="clone_inherited"` (distinct from `"ddl"`). The
   simplest, lowest-blast-radius wiring: **populate the CLONE target `QueryNode`'s
   `defined_columns` with the inherited list before `_build_file_rows` runs**, so the existing
   catalog-emission path ([indexer.py:1080–1111](../src/sqlcg/indexer/indexer.py#L1080))
   produces the rows unchanged — the only delta is the `source` tag, set from a per-table
   "inherited?" flag. (The developer confirms in Phase 0 whether populating `defined_columns`
   vs. emitting rows directly is cleaner; both keep the bulk-upsert path intact.)
5. **Feed Part B.** Because the CLONE target now has a catalog in `ddl_columns_by_bare`
   (extend the index with inherited catalogs once C2 resolves them), Part B's positional
   ordinal mapping finds it and maps the positional INSERT correctly. **Ordering constraint:**
   C2 inheritance must complete (and extend `ddl_columns_by_bare` with inherited entries)
   **before** Part B reads the catalog for a CLONE target's INSERT. Since Part B runs at parse
   time (pass 2) and C2 runs in the main-process upsert seam (after pass 2), there is a
   sequencing question the developer MUST resolve in Phase 0 — see the **Blocking-Question
   resolution** below.

#### Part C — design notes & why the placement is safe

- **Why main-process, not pass-2 workers.** Pass-2 parsing runs in worker **subprocesses**
  ([indexer.py:388 `parse_pass2`](../src/sqlcg/indexer/indexer.py#L388)); the aggregator and
  the cross-file CLONE graph live in the **main** process. Threading a mutable, globally-
  resolved CLONE catalog into subprocesses would require serializing the whole cross-file
  index per task and re-resolving chains per worker — more data movement and duplicated work,
  with no benefit (catalog emission is a main-process upsert concern anyway). The
  `defined_table_registry` loop proves the main process already has the full cross-file table
  set in hand at the right moment. **This keeps CLONE resolution O(N_clone_targets × chain
  depth) once, in one process — not per-worker, not per-column.**
- **Why this does not touch any CLAUDE.md per-column / per-statement hot-path invariant.**
  C1 is one `args.get` per CREATE statement. C2 runs **once, after all parsing**, over the
  set of CLONE targets — it does no `qualify`, no `build_scope`, no `exp.expand`, no
  `sg_lineage`, no per-column work at all. It only resolves dict lookups and appends row
  dicts that ride the **existing bulk/batched upsert** (`source` tag is one extra dict value;
  no new `execute()`). The invariant table below has a Part-C column.
- **Sequencing resolution for Part B × Part C (the one real design risk).** Part B reads the
  catalog at parse time (pass 2), but inherited CLONE catalogs only exist after C2 (post-pass-
  2). Two acceptable resolutions; **Phase 0 picks one and records it:**
  - **(Preferred) Resolve CLONE catalogs in the aggregator at the pass-1→pass-2 boundary**,
    *before* pass-2 dispatch, so `ddl_columns_by_bare` already contains inherited entries when
    pass-2 Part B reads it. CLONE sources are DDL tables registered in pass 1, so their
    catalogs are known by the time pass-1 completes; the CLONE chain can be resolved in the
    aggregator right after the `register_pass1` loop finishes (a `aggregator.resolve_clone_catalogs()`
    step called once before pass-2 dispatch). Catalog **emission** (the `SqlColumn`/`HAS_COLUMN`
    rows) still happens in the main-process upsert seam; only the *index* is resolved early so
    Part B can read it. This keeps everything in the main process and removes the ordering
    hazard entirely.
  - **(Fallback) Two-tier catalog in Part B**: Part B reads `ddl_columns_by_bare` for direct
    DDL catalogs at parse time; for CLONE targets whose catalog only resolves post-pass-2,
    Part B's mapping is applied in the **same main-process seam** as C2 (re-key the already-
    parsed positional edges from source-name to target-name using the inherited catalog). This
    is more code and re-touches edges, so prefer the first resolution.
  - **STOP / blocking** if neither holds — e.g. a CLONE source is *itself* only resolvable in
    pass 2 (a CTAS, not a DDL table). CLONE-of-CTAS source cataloguing is **out of scope**
    (CTAS has a body, not a fixed DDL column list; inheriting from it is the star-resolution
    problem). Record it as a follow-up; degrade to A1/A2.

### Data models / API

- `COLUMN_LINEAGE` gains `inferred_from_source_name BOOLEAN` (schema bump,
  [`schema.py`](../src/sqlcg/core/schema.py)).
- `LineageEdge` gains `inferred_from_source_name: bool = False`
  ([`base.py`](../src/sqlcg/parsers/base.py)).
- `DependencyNode` / lineage hop model gains optional `inferred_from_source_name`
  ([`models.py`](../src/sqlcg/server/models.py)).
- `QueryNode` gains `clone_source: TableRef | None = None`
  ([`base.py`](../src/sqlcg/parsers/base.py)) — set by C1 for CLONE creates.
- `CrossFileAggregator` gains `ddl_columns_by_bare: dict[str, list[str]]` (shared by B and C)
  and a `resolve_clone_catalogs()` step that extends that index with inherited CLONE
  catalogs (chain-resolved, cycle-guarded) before pass-2 dispatch.
- `HAS_COLUMN` `source` value gains `"clone_inherited"` (alongside `"ddl"`) — a data value,
  **not** a schema column change. No new graph column for C; only Part A2's
  `inferred_from_source_name` is a schema bump.
- No change to tool **signatures**; only richer `hint` + per-node flag.

### Dependencies

None new. (CLONE parsing uses sqlglot's existing `exp.Clone` — no new library.)

### Performance invariants (CLAUDE.md — row-by-row; Part B is the only per-column touch, Part C is post-pass main-process only)

| Invariant | Affected? | Why not |
|-----------|-----------|---------|
| `qualify`/`build_scope` module-level import | no | no import change |
| `exp.expand()` with file-level `sources` only | no | Part B reuses the #25 path's `sources=` (not the full corpus); no new expand. Part C does no expand at all |
| `body_scope` built once per statement | no | unchanged; neither B nor C rebuilds scope per column |
| pure-literal skip | no | Part B applies the **same** guard the #25 block applies |
| `sources=` not passed when scope present | no | unchanged |
| `copy=False` + `trim_selects=False` with scope | no | unchanged |
| `body_scope` built once before column loop | no | unchanged |
| **INSERT body copied once** | **preserved** | Part B copies `body_no_with` **once** before the per-column loop and swaps only the single projection — identical to the #25 block it mirrors. Part C touches no INSERT body |
| `_upsert_parsed_file` uses bulk upsert | no | A2 adds one column to the existing bulk row dict; **Part C's inherited `SqlColumn`/`HAS_COLUMN` rows ride the same bulk upsert** (they are appended to `column_rows`/`has_column_edges` exactly like DDL columns); no per-row upsert |
| `_flush_row_batch` per-batch flush | no | unchanged cardinality |

- **A1** is read-surface only (one extra `run_read` on the empty path) — outside every
  invariant.
- **C1** is one `args.get("clone")` per CREATE statement at parse time — O(N_create), not
  per-column. No qualify/scope/expand/sg_lineage.
- **C2** runs **once, after both passes**, over the set of CLONE targets in the main process.
  Cost is O(N_clone_targets × chain_depth) dict lookups + row-dict appends — bounded by the
  hop limit. It is **not** in any per-column or per-statement parse loop. The catalog-chain
  resolution (`resolve_clone_catalogs`) is a single main-process pass before pass-2 dispatch
  (preferred sequencing), or a single pass in the upsert seam (fallback). It cannot regress
  the scaling guard because it does no parse-time work.
- **Part B adds a once-per-statement catalog lookup + ordinal map build.** This is the same
  cardinality class as the existing #25 block. The [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py)
  fixture must stay flat at N and 2N. If Part B introduces a hot-path op that could scale with
  column count, the developer **adds a counter** to the scaling guard (CLAUDE.md rule) — but
  by construction the per-column work is unchanged (same `sg_lineage` call per mapped
  position the #25 block already makes).
- **Perf budget (CLAUDE.md ~210–256s on the DWH).** Part C adds a single bounded pass over
  CLONE targets (a small fraction of files); the inherited rows are columns that were
  *missing* before, so total upsert volume rises by roughly the catalog size of CLONE-built
  facts — measured in Phase 5. Expected delta is small (column rows are cheap in the bulk
  path); a regression beyond the 5-minute budget is a STOP condition.

---

## Implementation Steps (dependency-ordered)

> Ordering: A1 first (pure read-surface, immediate value, zero schema risk); then the schema
> bump + A2 carrier; then the shared catalog index; then **Part C CLONE inheritance** (it
> *populates* the catalog Part B consumes, so it lands before/with B); then Part B ordinal
> mapping. Guards for B+C land RED before the implementation and flip green after; release
> plumbing last. **Part C and Part B share the `ddl_columns_by_bare` index and the catalog
> chain must be resolved before Part B reads it — see Phase 0 sequencing decision.**

### Phase 0 — Confirm seams (no code change)
**Step 0.1** Grep-confirm: the #25 block boundary (`isinstance(stmt.this, exp.Schema)`,
[base.py:739](../src/sqlcg/parsers/base.py#L739)); the empty-result branches in all three
tools ([tools.py:1558](../src/sqlcg/server/tools.py#L1558) upstream, the analogous
downstream + trace branches); the `defined_by_query`/`defined_columns` source for ordered
DDL columns ([indexer.py:1081](../src/sqlcg/indexer/indexer.py#L1081),
[ansi_parser.py:419](../src/sqlcg/parsers/ansi_parser.py#L419)); the cross-file threading
seam (`SchemaResolver.register_cross_file_sources` pattern,
[schema_resolver.py:90](../src/sqlcg/lineage/schema_resolver.py#L90)). Record the exact
seam chosen for Part B-catalog threading.
**Step 0.2 (CLONE seams + sequencing — folded in).** Confirm:
- `CREATE TABLE … CLONE src` parses to `exp.Create` with `stmt.args["clone"]` an `exp.Clone`
  whose `.this` is the source `Table` (verified for the `snowflake` dialect; re-confirm the
  exact attribute path before C1).
- The CLONE target currently registers as a `defined_table` with empty `defined_columns`
  ([ansi_parser.py:204](../src/sqlcg/parsers/ansi_parser.py#L204)) and the catalog-emission
  loop ([indexer.py:1080–1111](../src/sqlcg/indexer/indexer.py#L1080)) skips it because
  `defined_by_query` requires non-empty `defined_columns`.
- The main-process seam that has the full cross-file table set in hand — the
  `defined_table_registry` build at [indexer.py:430–433](../src/sqlcg/indexer/indexer.py#L430),
  after both passes, before batch flush.
- **DECIDE AND RECORD the Part B × Part C sequencing** (see "Sequencing resolution" in Part C
  design): preferred = resolve CLONE catalogs in the aggregator before pass-2 dispatch so the
  inherited catalog is in `ddl_columns_by_bare` when Part B reads it; fallback = re-key in the
  upsert seam. Name the chosen approach in the PR note.
- Acceptance: a short note in the PR description naming each confirmed seam **and the chosen
  B×C sequencing**; **STOP and raise a blocking question** if (a) the no-column-list INSERT
  does not reach the main column loop as assumed (Part B depends on it), or (b) a CLONE source
  in the corpus is itself a CTAS rather than a DDL table so its catalog is not in
  `ddl_columns_by_bare` (CLONE-of-CTAS is out of scope — degrade to A1/A2).

### Phase 1 — A1 empty-closure disambiguation (read surface; highest value/risk)
**Step 1.1** Add `_empty_closure_hint(db, table_id) -> str` (shared helper) and call it from
the empty branch of `get_upstream_dependencies`, `get_downstream_dependencies`, and
`trace_column_lineage`. Branch on `GET_COLUMNS_FOR_TABLE` returning zero rows.
- Files: [`tools.py`](../src/sqlcg/server/tools.py).
- Call sites (grep-confirmed required): the three tools' empty branches.
- Acceptance (TC-A1): on a synthetic table with **lineage edges but no `HAS_COLUMN`**, an
  empty `get_upstream_dependencies` returns the *no-catalog* hint (names CLONE/positional,
  says "unproven"); on a normally-catalogued table with a genuinely terminal column, the
  empty result returns the *generic* hint. Assert the exact hint substring, not "no
  exception."

### Phase 2 — Schema bump + A2 carrier
**Step 2.1** Add `inferred_from_source_name` to the `COLUMN_LINEAGE` edge schema
([`schema.py`](../src/sqlcg/core/schema.py), [`duckdb_backend.py`](../src/sqlcg/core/duckdb_backend.py)
bulk-upsert column list) and bump `SCHEMA_VERSION`. Add the field to `LineageEdge`
([`base.py`](../src/sqlcg/parsers/base.py), default `False`) and to the
`column_lineage_edges` row dict in [`_build_file_rows`](../src/sqlcg/indexer/indexer.py#L1211).
**Step 2.2** Set the flag in [`base.py`](../src/sqlcg/parsers/base.py)'s main column loop for
a no-column-list INSERT dst when no ordinal catalog resolved (this is wired to Part B in
Phase 4 — until then it flags **all** no-column-list INSERT dsts, which is already the
correct interim signal).
**Step 2.3** Surface the flag on `DependencyNode` / lineage hop model
([`models.py`](../src/sqlcg/server/models.py)) and populate it in the three tools.
- Acceptance (TC-A2): a no-column-list positional INSERT with no DDL catalog yields a
  `COLUMN_LINEAGE` edge with `inferred_from_source_name=true`, surfaced on the tool result
  node; a DDL-catalogued column-list INSERT yields `false`.
- Invariant guard: bulk/batch upsert + scaling guards green (A2 adds one column to the
  existing bulk row; no per-row upsert).

### Phase 3 — Guards for Part B + Part C (land RED)
**Step 3.1** Synthetic committable fixtures:
- **B fixture (catalog-available positional INSERT):** `CREATE TABLE s.fact (a INT, b INT, c INT);`
  plus `INSERT INTO s.fact SELECT src.x, src.y, src.z FROM s.src;` (no column list) where
  `x/y/z` are deliberately **different** names from `a/b/c` (the rename the blind spot hides).
- **C fixture (the report's actual shape — CLONE + bare positional INSERT):**
  `CREATE TABLE s.src_ddl (a INT, b INT, c INT);` (the catalog donor),
  `CREATE TABLE s.fact_clone CLONE s.src_ddl;` (no column list of its own),
  `INSERT INTO s.fact_clone SELECT t.x, t.y, t.z FROM s.t;` — proves Part C inherits
  `a/b/c` onto `s.fact_clone` and Part B then maps `x/y/z`→`a/b/c`.
- **C-chain fixture:** `CREATE TABLE s.fact_clone2 CLONE s.fact_clone;` — proves CLONE-of-CLONE
  inheritance follows the chain to the DDL donor.
- **C-degrade control (un-indexed source):** `CREATE TABLE s.fact_orphan CLONE ext.unknown;`
  plus a positional INSERT — `ext.unknown` is **not** defined in the fixture, so Part C
  inherits nothing; dsts stay source-named, `inferred_from_source_name=true`, A1 hint fires.
- **C-cycle control:** two CLONE creates referencing each other — proves the cycle guard
  terminates and inherits nothing (no hang, no crash).
**Step 3.2** TC-B (RED until Phase 4): for `s.fact`, `COLUMN_LINEAGE` dst nodes are
`s.fact.a/b/c` (target names), **not** `s.fact.x/y/z`; `get_upstream_dependencies("s.fact.a")`
returns `s.src.x`; edges have `inferred_from_source_name=false`.
**Step 3.3** TC-C (RED until Phase 4):
- `s.fact_clone` has `SqlColumn` rows `a/b/c` with `HAS_COLUMN` `source="clone_inherited"`
  (assert the catalog exists and the source tag), and its INSERT dsts are `a/b/c`
  (`inferred_from_source_name=false`); `get_upstream_dependencies("s.fact_clone.a")` returns `s.t.x`.
- `s.fact_clone2` inherits `a/b/c` via the chain (catalog exists).
- `s.fact_orphan` has **no** catalog; dsts source-named, flagged true; A1 hint fires.
- The cycle control terminates with no catalog and no exception.
- Acceptance: TC-B and TC-C documented-RED before Phase 4, green after (not `xfail`-left).

### Phase 4 — Part B-catalog + Part C CLONE inheritance + Part B ordinal mapping (flips TC-B/TC-C green)
**Step 4.1 (shared catalog index).** Build `ddl_columns_by_bare` in
[`CrossFileAggregator.register_pass1`](../src/sqlcg/lineage/aggregator.py#L33) from ordered
`defined_columns`, excluding `_ambiguous_bare`.
**Step 4.2 (C1 — parse CLONE source).** In
[`_parse_statement`](../src/sqlcg/parsers/ansi_parser.py#L236), set `QueryNode.clone_source`
from `stmt.args["clone"].this` when an `exp.Clone` is present. Add the `clone_source` field to
`QueryNode` ([`base.py`](../src/sqlcg/parsers/base.py)).
**Step 4.3 (C2 — inherit catalog).** Add `aggregator.resolve_clone_catalogs()` (called once at
the pass-1→pass-2 boundary, per the Phase-0 sequencing decision): for each CLONE target with
empty own `defined_columns`, resolve its source's ordered columns from `ddl_columns_by_bare`,
following CLONE chains with a visited-set + bounded hop limit; extend `ddl_columns_by_bare`
with the inherited entry (so Part B sees it). Emit the inherited `SqlColumn`/`HAS_COLUMN` rows
in the main-process upsert path tagged `source="clone_inherited"` — preferably by populating
the CLONE target `QueryNode.defined_columns` before `_build_file_rows` so the existing
emission loop ([indexer.py:1080–1111](../src/sqlcg/indexer/indexer.py#L1080)) produces them,
with the `source` tag switched via a per-table inherited flag.
**Step 4.4 (B — ordinal mapping).** Add the no-column-list positional-INSERT block in
[`base.py`](../src/sqlcg/parsers/base.py) next to the #25 block, mirroring its body-copy-once
+ per-position guard structure; map projection *i* → `catalog[i]` (catalog now includes
CLONE-inherited entries); handle length mismatch by mapping the overlap only. When mapped, dst
is the target name and `inferred_from_source_name` is **not** set; unmapped/no-catalog
positions retain the Phase 2 flag.
- Files: [`aggregator.py`](../src/sqlcg/lineage/aggregator.py),
  [`ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py),
  [`base.py`](../src/sqlcg/parsers/base.py),
  [`indexer.py`](../src/sqlcg/indexer/indexer.py), and the threading seam.
- Call site: the new aggregator index + `resolve_clone_catalogs` have grep-confirmed readers
  (the parser/pass-2 seam and the upsert seam respectively) — CLAUDE.md call-site rule.
- Acceptance: TC-B and TC-C green; **single-file no-op** — when no aggregator is present
  (`reindex_file`), Part B and Part C both degrade to source-named + flagged (controls green).
- Invariant guard: [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py),
  [`test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py) all green —
  Part C adds no per-column op and rides the bulk upsert, so they cannot regress.

### Phase 5 — Live-DWH re-verification (read-only; never commit to the DWH)
**Step 5.1** Index the real DWH (`/home/ignwrad/Projects/dwh`) into a throwaway DB
(read-only on the repo; `tests/e2e/test_dwh_e2e.py` is gitignored — never commit it).
- **Confirm Part C on the report's exact table.** Check whether `ba.wtfs_voorraad_dagstand`'s
  CLONE source is indexed. If yes: `SELECT COUNT(*) FROM SqlColumn WHERE table_name =
  'ba.wtfs_voorraad_dagstand'` is now **> 0** (was 0), a `ma_vrije_vrd` node exists with
  `HAS_COLUMN source='clone_inherited'`, and
  `get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` returns the upstream
  (the report's exact failing call is **fixed**). Record the CLONE source's identity.
- **If the CLONE source is NOT indexed** (cross-DB): confirm the degrade path — A1 returns the
  no-catalog hint and A2 flags the `mh_vrije_vrd` edge `inferred_from_source_name=true`. Record
  which path fired; this is an acceptable outcome, not a failure.
- Confirm Part B on a **catalog-available** sibling fact (DDL column-list + positional INSERT):
  its dsts now carry target names.
- Confirm perf budget unchanged (~210–256s baseline, CLAUDE.md indexer perf note); record the
  inherited-catalog row-count delta. A regression beyond the 5-minute budget is a STOP.
- Record the verdict in the PR description (not committed to the DWH repo).

### Phase 6 — Release plumbing
**Step 6.1** Version bump. **SemVer:** A2 adds a new graph field + tool-surface signal, Part B
is a new positional-resolution capability, and **Part C is a new CLONE-cataloguing
capability** ⇒ **minor** (`1.5.1` → `1.6.0`) per CLAUDE.md (additive capability is minor; the
schema bump forces a re-index, which is the documented migration path, and is not a breaking
API change). Update
[`pyproject.toml`](../pyproject.toml), [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py),
`uv lock`.
**Step 6.2** Full gate: `uv run pytest`; `uv run pyright`; `uv run ruff check src tests`.
- **Do not tag, do not close any issue** — the user verifies on the live DWH and tags
  (project convention, memory `feedback_no_close_verify_before_tag.md`).

---

## Test Strategy

- **Unit** (no graph): aggregator builds `ddl_columns_by_bare` in order, excludes ambiguous
  bare names; `resolve_clone_catalogs` inherits a source catalog onto a CLONE target, follows
  a CLONE-of-CLONE chain, terminates on a cycle (visited-set guard) and on the hop limit,
  and inherits nothing for an un-indexed source; C1 sets `QueryNode.clone_source` from
  `exp.Clone` and leaves it `None` for non-CLONE creates; the parser's ordinal block maps
  positions correctly and handles length mismatch without raising; `inferred_from_source_name`
  defaults `False` and is set only on the no-catalog no-column-list dst.
- **Integration** (real in-memory DuckDB via `Indexer().index_repo`):
  - *Empty-closure disambiguation* — table with edges but no `HAS_COLUMN` yields the
    no-catalog hint; normally-catalogued terminal column yields the generic hint.
  - *Ordinal correctness* — positional INSERT (no column list) into a DDL-catalogued table
    produces target-named dsts and a working `get_upstream_dependencies`; the source-rename
    fixture proves the dst is the **target** name, not the source name.
  - *CLONE inheritance end-to-end* — `CREATE TABLE fact CLONE src_ddl` + positional INSERT
    yields `SqlColumn` rows on `fact` (`HAS_COLUMN source='clone_inherited'`), and the INSERT
    dsts carry the **inherited target** names (not source names) with
    `inferred_from_source_name=false`; `get_upstream_dependencies` on an inherited column
    returns the source — the report's table shape, corrected.
  - *CLONE chain* — `fact2 CLONE fact1 CLONE src_ddl` inherits `src_ddl`'s catalog onto `fact2`.
  - *CLONE degrade (un-indexed source)* — `fact_orphan CLONE ext.unknown` (source absent) keeps
    source-named dsts flagged `inferred_from_source_name=true`, and the empty-closure hint
    fires — no fabricated catalog.
  - *CLONE cycle* — mutually-referencing CLONEs terminate with no catalog and no exception.
  - *Single-file no-op* — `reindex_file` of the same file with no aggregator does not crash
    and degrades to the flagged fallback (no CLONE inheritance, no positional mapping).
- **Perf invariants** — the four guard tests stay green; if Part B adds any per-column op,
  a scaling-guard counter is added (it should not, by construction).
- **Observable-output rule** — every test asserts on returned node sets / hint substrings /
  edge flags, never "no exception raised" (CLAUDE.md).
- **Live-DWH** (read-only, gitignored harness) — the report's exact failing call is **fixed**
  (returns upstream) if the CLONE source is indexed, else returns the labelled hint; a
  catalog-available sibling resolves target names; the inherited-catalog row delta is recorded.

## Acceptance Criteria

- [ ] `get_upstream_dependencies` / `get_downstream_dependencies` / `trace_column_lineage`
      return a **distinct** hint when the table has zero catalogued columns vs. when a
      catalogued column is genuinely terminal; the no-catalog hint names CLONE / positional
      INSERT and says the empty result is "unproven, not negative" (asserted substring).
- [ ] The three tools share one `_empty_closure_hint` helper with grep-confirmed call sites
      (no duplicated wording).
- [ ] `COLUMN_LINEAGE` carries `inferred_from_source_name`; it is `true` only for dst nodes
      named from the source expression (no-column-list INSERT, no resolved catalog) and
      `false` for DDL/column-list/resolved-ordinal dsts; `SCHEMA_VERSION` bumped.
- [ ] The flag is surfaced on the relevant tool result nodes.
- [ ] A no-column-list positional `INSERT INTO t SELECT …` into a table with an ordered DDL
      column catalog produces dst nodes under the **target** column names (the source-rename
      fixture proves dst ≠ source name); `get_upstream_dependencies` on a target column
      returns the correct source column.
- [ ] When **no** catalog is available, dsts keep source-derived names **and** carry
      `inferred_from_source_name=true` (no fabricated target name).
- [ ] `CrossFileAggregator.ddl_columns_by_bare` is built from ordered DDL columns, excludes
      ambiguous bare names, and is shared by Part B and Part C; the
      block degrades to a no-op (flagged fallback) on the single-file path.
- [ ] **(Part C) `CREATE TABLE … CLONE src` is parsed** — `QueryNode.clone_source` is set
      from `exp.Clone` for CLONE creates and `None` otherwise.
- [ ] **(Part C) A CLONE-built table with no own column-list DDL inherits its CLONE source's
      ordered DDL catalog** as `SqlColumn`/`HAS_COLUMN` rows tagged `source='clone_inherited'`;
      CLONE-of-CLONE chains resolve to the DDL donor; cycles and the hop limit terminate
      cleanly inheriting nothing; an un-indexed source inherits nothing (degrades to A1/A2).
- [ ] **(Part C × B) After inheritance, a positional INSERT into a CLONE-built table maps
      projection *i* → inherited target column *i*** — dsts carry the real target names with
      `inferred_from_source_name=false`; `get_upstream_dependencies` on an inherited column
      returns the source (the report's table shape, corrected, when the source is indexed).
- [ ] No new per-row `upsert_node`/`upsert_edge`; Part B's added work is once-per-statement
      (mirrors the #25 block); Part C's inherited rows ride the bulk upsert and Part C does no
      per-column work; the four perf-invariant guard tests stay green.
- [ ] No `# TODO` in the happy path; every new helper (`_empty_closure_hint`,
      `resolve_clone_catalogs`) has a grep-confirmed call site.
- [ ] Per-column `SELECT alias.*` star resolution is recorded as the **only** deferred
      follow-up (this plan's Non-Goals), not silently dropped; CLONE cataloguing is now
      in-scope and delivered.
- [ ] Live-DWH re-verification recorded in the PR (read-only; report's exact call fixed if the
      CLONE source is indexed, else labelled; catalog-available sibling resolves; CLONE source
      identity + inherited row delta recorded; perf within budget).
- [ ] Version bumped to **1.6.0** in `pyproject.toml` + `__init__.py` + `uv lock`; not
      tagged; no issue closed.

## Risks and Mitigations

- **R1 — Part B applies a wrong-schema catalog** (two schemas define the same bare name with
  different column orders). *Mitigation:* `_ambiguous_bare` already excludes multi-schema
  bare names; `ddl_columns_by_bare` skips them, so an ambiguous target falls through to the
  flagged source-named path rather than mapping against a guessed catalog.
- **R2 — Ordinal mapping is wrong when the SELECT order doesn't match DDL order** (a genuine
  positional-INSERT bug in the source SQL, or a partial projection). *Mitigation:* sqlcg
  faithfully models what the SQL says — positional INSERT *is* ordinal by definition, so
  mapping projection *i* → DDL column *i* is the correct semantics. Length mismatch maps the
  overlap and flags the remainder; we never raise. The fixture pins both the matched and the
  over-projected case.
- **R3 — Schema bump forces a full re-index.** *Mitigation:* re-index is the documented
  migration path (CLAUDE.md "No backward compatibility"); the schema gate
  (`SCHEMA_VERSION`) already enforces it cleanly.
- **R4 — Part B hot-path regression.** *Mitigation:* the per-column cardinality is unchanged
  (same `sg_lineage` call per mapped position as #25); body-copy-once preserved; the four
  guard tests are blocking acceptance criteria; add a scaling-guard counter if any new
  per-column op sneaks in.
- **R5 — A1 hint mislabels a genuinely-terminal column on a CLONE table** (correctly has no
  upstream AND no catalog). *Mitigation:* the hint is explicitly framed as "unproven, not
  negative" — it does not claim lineage exists, only that absence is not authoritative for
  no-catalog tables. This is the honest signal the report asked for. With Part C, this only
  applies to CLONE tables whose source is un-indexed (cross-DB) — a smaller residual set.
- **R6 (Part C) — Part B reads the catalog before Part C has inherited it** (sequencing
  hazard: Part B at parse time / pass 2, Part C in the main process). *Mitigation:* the
  preferred sequencing (Phase 0) resolves CLONE catalogs in the aggregator **before** pass-2
  dispatch, so `ddl_columns_by_bare` already contains inherited entries when Part B reads it;
  the fallback re-keys in the upsert seam. Phase 0 picks and records one; this is the single
  highest-attention integration point.
- **R7 (Part C) — CLONE cycle / unbounded chain hangs the indexer.** *Mitigation:* visited-set
  cycle guard + bounded hop limit in `resolve_clone_catalogs`; on cycle/limit, inherit nothing.
  A cycle-control fixture pins termination.
- **R8 (Part C) — wrong-schema CLONE inheritance** (the CLONE source's bare name is ambiguous
  across schemas). *Mitigation:* the source lookup uses `ddl_columns_by_bare`, which already
  excludes `_ambiguous_bare` — an ambiguous source inherits nothing rather than a guessed
  catalog (degrade to A1/A2), same discipline as Part B (R1).
- **R9 (Part C) — CLONE-of-CTAS source.** A CLONE whose source is a CTAS (body, not fixed DDL
  columns) has no entry in `ddl_columns_by_bare`. *Mitigation:* out of scope (it is the
  star/body-resolution problem); Part C inherits nothing and degrades to A1/A2. Phase 0 STOPs
  and records if this shape dominates the corpus.
- **R10 (Part C) — perf budget regression** from the extra inherited catalog rows.
  *Mitigation:* the rows ride the existing bulk/batched upsert and are bounded by CLONE-fact
  catalog size; Phase 5 measures the delta against the ~210–256s baseline; >5 min is a STOP.

### Blocking Questions

- **(Resolved 2026-06-08 by user) Fold CLONE-source cataloguing into this PR.** The user
  decided to fold it in rather than defer. It is now Part C above (in-scope, designed,
  sequenced, with its own perf and pipeline-placement analysis). The previously-deferred
  reasoning is preserved in "Part C — design notes & why the placement is safe."
- **(Open for Phase 0, not blocking the plan) Part B × Part C sequencing.** The developer
  resolves the parse-time-vs-main-process ordering in Phase 0 (preferred: resolve CLONE
  catalogs in the aggregator before pass-2 dispatch). STOP only if a CLONE source is itself a
  CTAS (CLONE-of-CTAS is out of scope) and that shape is common in the corpus.
- **(Resolved, recorded) Per-column `SELECT alias.*` star resolution** (report follow-up 4)
  is the only remaining deferred item — a distinct effort the report itself says can be scoped
  out; deferred as a Non-Goal.
