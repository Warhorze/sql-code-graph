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

This plan ships the two follow-ups that are **tractable and correct without runtime/DB
metadata** — disambiguating the empty-closure signal (report follow-up 1) and giving
positional INSERT a target-column catalog whenever one is derivable, flagging the node as
source-name-inferred when one is not (report follow-ups 2 + 3, scoped honestly). It
**explicitly defers** the two sub-cases that cannot be solved by static parsing alone
(CLONE-source column catalog; `SELECT alias.*` per-column star resolution — report
follow-up 4) with recorded reasoning, so a future ticket can pick them up without
re-discovering why.

> Static reads below are pinned to current `master` (v1.5.1). Line numbers are anchors,
> not exact — the developer re-greps before editing.

---

## Pre-work findings (these reshape the obvious plan — read before scoping)

Verifying the four report follow-ups against current code materially changes what is
buildable. The headline correction: **report follow-ups 2 and 3, taken literally, do NOT
fix the report's own example table.**

1. **The report's exact table has NO derivable target-column catalog from static SQL.**
   `BA.WTFS_VOORRAAD_DAGSTAND` is born via `CREATE TABLE IF NOT EXISTS … CLONE …` and the
   `INSERT` is positional **with no column list**. Therefore:
   - There is no `CREATE TABLE (col …)` DDL → [`_extract_defined_columns`](../src/sqlcg/parsers/ansi_parser.py#L400)
     never runs for it; `defined_tables`/`HAS_COLUMN`/`SqlColumn` are empty (confirmed by
     the report's `SELECT COUNT(*) FROM SqlColumn … = 0`).
   - There is no `INSERT INTO t (c1, c2)` column list → the #25 positional-override block
     in [`base.py`](../src/sqlcg/parsers/base.py#L738) (`isinstance(stmt.this, exp.Schema)`)
     **does not fire**; the main loop runs and derives the dst name from the source
     expression (`col_expr.name`, [base.py:869](../src/sqlcg/parsers/base.py#L869)) — this
     is exactly where the phantom `mh_vrije_vrd` name comes from.
   - **`CLONE` is not parsed at all** (grep: zero matches for `clone` in `parsers/`,
     `lineage/`, `indexer/`). So the only other place a catalog *could* come from — the
     CLONE source table's columns — is not harvested either.

   **Consequence:** for a CLONE-built + bare-positional-INSERT table, *no static source of
   target column names exists in the indexed repo*. Direction-2's "resolve positional
   INSERT against target DDL" and direction-3's "catalog from `CREATE TABLE`" both have
   **nothing to read** for this specific table. The honest fix for the report's table is
   **direction 1 (disambiguate the empty closure) + the `inferred_from_source_name` flag**
   so a consumer knows the node key is source-derived and unproven — not a phantom
   correctness claim. Directions 2/3 still pay off for the *broader* class (positional
   INSERT into a table that DOES have a column-list DDL, or an INSERT with a column list),
   which the report notes is "the standard fact-table build pattern" — many siblings DO
   carry DDL even when this one does not. We build the catalog/ordinal machinery so those
   siblings resolve correctly, and we degrade safely (flag, not phantom) when no catalog
   exists.

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
`inferred_from_source_name` consumer signal, and **(B)** a no-column-list positional-INSERT
ordinal mapping against a cross-file DDL column catalog, with a safe `inferred` fallback
when no catalog exists. CLONE-source cataloguing and per-column star resolution are
**deferred with reasoning** (see Non-Goals).

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
  column list, built from DDL-defined tables in the existing `register_pass1` loop, threaded
  into pass-2 parsing the same way schema context already is. Ambiguous bare names (already
  tracked in `_ambiguous_bare`) are excluded — never guess a catalog across schemas.
- **Regression guards** (synthetic, committable) for every behaviour above.
- **Version bump** (patch or minor per SemVer rule — see Phase 6).

### Non-Goals (deferred, with reasoning — do not silently drop)

- **CLONE-source column catalogue (report follow-up 3, the CLONE half).** `CREATE TABLE …
  CLONE src` is not parsed today, and cataloguing `t`'s columns from `src` requires (a)
  parsing CLONE, (b) resolving `src`'s own catalog — which for the report's chain is itself
  another CLONE/positional table with no DDL. This is a **multi-hop schema-inheritance
  feature** with its own blast radius (CLONE parse in every dialect, a new schema-propagation
  pass, ambiguity when `src` has no catalog either). It does not fit a single safe PR and is
  **out of scope**; tracked as a follow-up. The report's exact table is covered here only by
  A1 + A2 (labelled-unproven), which is the correct honest outcome until CLONE cataloguing
  lands. **This is the central scope decision — see "Scope decision: the report's own table"
  below.**
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
  zero measured edge delta — CLAUDE.md "Schema resolution"). The catalog used here is the
  **parsed DDL** the indexer already has, not an external schema source.

---

## Scope decision: the report's own table (CLONE + bare positional INSERT)

The trigger table `ba.wtfs_voorraad_dagstand` is the **worst case**: CLONE-built (no DDL
columns) **and** loaded by a positional INSERT with no column list. For it specifically,
parts B and B-catalog have **no catalog to read** and therefore change nothing — the dst is
still named from the source (`mh_vrije_vrd`). What changes for it:

- **A1** makes `get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` return
  a hint that says, in effect: *this table has no column catalog (likely CLONE / positional
  INSERT); lineage may exist under a source-derived column key — empty here is unproven, not
  negative.* That is the exact disambiguation the report asks for in follow-up 1.
- **A2** flags the `mh_vrije_vrd` destination edge as `inferred_from_source_name=true`, so a
  consumer reading the graph (or `trace_column_lineage` landing on it) knows the key is
  source-derived and may not match the DB column.

This is deliberately **not** a phantom-correctness claim. We do not fabricate an
`ma_vrije_vrd` node for this table because nothing in the indexed SQL tells us the target
column is named `ma_vrije_vrd` — only the live DB / a CLONE-source catalog knows that, and
both are out of scope. Manufacturing the name would be a guess that could be wrong in the
other direction. **Correct, labelled "unproven" beats confidently wrong.** The full
correctness fix for this table is the deferred CLONE-source-catalogue follow-up; this plan
makes the gap *visible and labelled* and fixes the broader catalog-available class.

> If the reviewer/user wants the CLONE-source catalogue folded in here instead of deferred,
> that is a scope expansion that needs its own design pass (CLONE parsing + schema
> inheritance) — flag it as a blocking question rather than stretching this PR.

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

### Data models / API

- `COLUMN_LINEAGE` gains `inferred_from_source_name BOOLEAN` (schema bump,
  [`schema.py`](../src/sqlcg/core/schema.py)).
- `LineageEdge` gains `inferred_from_source_name: bool = False`
  ([`base.py`](../src/sqlcg/parsers/base.py)).
- `DependencyNode` / lineage hop model gains optional `inferred_from_source_name`
  ([`models.py`](../src/sqlcg/server/models.py)).
- No change to tool **signatures**; only richer `hint` + per-node flag.

### Dependencies

None new.

### Performance invariants (CLAUDE.md — row-by-row, Part B is the only hot-path touch)

| Invariant | Affected? | Why not |
|-----------|-----------|---------|
| `qualify`/`build_scope` module-level import | no | no import change |
| `exp.expand()` with file-level `sources` only | no | Part B reuses the #25 path's `sources=` (not the full corpus); no new expand |
| `body_scope` built once per statement | no | unchanged; Part B does not rebuild scope per column |
| pure-literal skip | no | Part B applies the **same** guard the #25 block applies |
| `sources=` not passed when scope present | no | unchanged |
| `copy=False` + `trim_selects=False` with scope | no | unchanged |
| `body_scope` built once before column loop | no | unchanged |
| **INSERT body copied once** | **preserved** | Part B copies `body_no_with` **once** before the per-column loop and swaps only the single projection — identical to the #25 block it mirrors |
| `_upsert_parsed_file` uses bulk upsert | no | A2 adds one column to the existing bulk row dict; no per-row upsert |
| `_flush_row_batch` per-batch flush | no | unchanged cardinality |

- **A1** is read-surface only (one extra `run_read` on the empty path) — outside every
  invariant.
- **Part B adds a once-per-statement catalog lookup + ordinal map build.** This is the same
  cardinality class as the existing #25 block. The [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py)
  fixture must stay flat at N and 2N. If Part B introduces a hot-path op that could scale with
  column count, the developer **adds a counter** to the scaling guard (CLAUDE.md rule) — but
  by construction the per-column work is unchanged (same `sg_lineage` call per mapped
  position the #25 block already makes).

---

## Implementation Steps (dependency-ordered)

> Ordering: A1 first (pure read-surface, immediate value, zero schema risk); then the schema
> bump + A2 carrier; then the catalog + Part B that produces correct names; guards land RED
> before B and flip green after; release plumbing last.

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
- Acceptance: a short note in the PR description naming each confirmed seam; **STOP and
  raise a blocking question** if the no-column-list INSERT does not reach the main column
  loop as assumed (e.g. it is dropped earlier as table-only) — Part B depends on it.

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

### Phase 3 — Guards for Part B (land RED)
**Step 3.1** Synthetic committable fixture: `CREATE TABLE s.fact (a INT, b INT, c INT);`
plus `INSERT INTO s.fact SELECT src.x, src.y, src.z FROM s.src;` (no column list) where
`x/y/z` are deliberately **different** names from `a/b/c` (the rename the blind spot hides).
Add a control: a CLONE-style table with no DDL (just the positional INSERT) to pin the
no-catalog → flagged path.
**Step 3.2** TC-B (RED until Phase 4): after indexing, the `COLUMN_LINEAGE` dst nodes for
`s.fact` are `s.fact.a/b/c` (target names), **not** `s.fact.x/y/z`; `get_upstream_dependencies("s.fact.a")`
returns `s.src.x`; and these edges have `inferred_from_source_name=false`. For the no-DDL
control, dsts stay source-named and carry `inferred_from_source_name=true`.
- Acceptance: TC-B documented-RED before Phase 4, green after (not `xfail`-left).

### Phase 4 — Part B-catalog + Part B ordinal mapping (flips TC-B green)
**Step 4.1** Build `ddl_columns_by_bare` in
[`CrossFileAggregator.register_pass1`](../src/sqlcg/lineage/aggregator.py#L33) from ordered
`defined_columns`, excluding `_ambiguous_bare`. Thread it into pass-2 parsing via the seam
confirmed in Phase 0.
**Step 4.2** Add the no-column-list positional-INSERT block in
[`base.py`](../src/sqlcg/parsers/base.py) next to the #25 block, mirroring its body-copy-once
+ per-position guard structure; map projection *i* → `catalog[i]`; handle length mismatch by
mapping the overlap only. When mapped, dst is the target name and `inferred_from_source_name`
is **not** set; unmapped/no-catalog positions retain the Phase 2 flag.
- Files: [`aggregator.py`](../src/sqlcg/lineage/aggregator.py), [`base.py`](../src/sqlcg/parsers/base.py),
  and the threading seam (`SchemaResolver` or the pass-2 dispatch).
- Call site: the new aggregator index has a grep-confirmed reader (the parser/pass-2 seam).
- Acceptance: TC-B green; **single-file no-op** — when no aggregator index is threaded, the
  block degrades to source-named + flagged (TC-B control still green).
- Invariant guard: [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py),
  [`test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py) all green.

### Phase 5 — Live-DWH re-verification (read-only; never commit to the DWH)
**Step 5.1** Index the real DWH (`/home/ignwrad/Projects/dwh`) into a throwaway DB
(read-only on the repo; `tests/e2e/test_dwh_e2e.py` is gitignored — never commit it).
- Confirm A1: `get_upstream_dependencies("ba.wtfs_voorraad_dagstand.ma_vrije_vrd")` now
  returns the **no-catalog** hint (the report's exact failing call) instead of bare empty.
- Confirm A2: the `mh_vrije_vrd` dst edge carries `inferred_from_source_name=true`.
- Confirm Part B on a **catalog-available** sibling fact (a fact that DOES have column-list
  DDL but a positional INSERT): its dsts now carry target names.
- Confirm perf budget unchanged (~210–256s baseline, CLAUDE.md indexer perf note).
- Record the verdict in the PR description (not committed to the DWH repo).

### Phase 6 — Release plumbing
**Step 6.1** Version bump. **SemVer:** A2 adds a new graph field + tool-surface signal and
Part B is a new resolution capability ⇒ **minor** (`1.5.1` → `1.6.0`) per CLAUDE.md
(additive capability is minor; the schema bump forces a re-index, which is the documented
migration path, and is not a breaking API change). Update
[`pyproject.toml`](../pyproject.toml), [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py),
`uv lock`.
**Step 6.2** Full gate: `uv run pytest`; `uv run pyright`; `uv run ruff check src tests`.
- **Do not tag, do not close any issue** — the user verifies on the live DWH and tags
  (project convention, memory `feedback_no_close_verify_before_tag.md`).

---

## Test Strategy

- **Unit** (no graph): aggregator builds `ddl_columns_by_bare` in order, excludes ambiguous
  bare names; the parser's ordinal block maps positions correctly and handles length
  mismatch without raising; `inferred_from_source_name` defaults `False` and is set only on
  the no-catalog no-column-list dst.
- **Integration** (real in-memory DuckDB via `Indexer().index_repo`):
  - *Empty-closure disambiguation* — table with edges but no `HAS_COLUMN` yields the
    no-catalog hint; normally-catalogued terminal column yields the generic hint.
  - *Ordinal correctness* — positional INSERT (no column list) into a DDL-catalogued table
    produces target-named dsts and a working `get_upstream_dependencies`; the source-rename
    fixture proves the dst is the **target** name, not the source name.
  - *No-catalog fallback* — CLONE-style table (no DDL) keeps source-named dsts flagged
    `inferred_from_source_name=true`, and the empty-closure hint fires.
  - *Single-file no-op* — `reindex_file` of the same file with no aggregator does not crash
    and degrades to the flagged fallback.
- **Perf invariants** — the four guard tests stay green; if Part B adds any per-column op,
  a scaling-guard counter is added (it should not, by construction).
- **Observable-output rule** — every test asserts on returned node sets / hint substrings /
  edge flags, never "no exception raised" (CLAUDE.md).
- **Live-DWH** (read-only, gitignored harness) — the report's exact failing call returns the
  labelled hint; a catalog-available sibling resolves target names.

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
      ambiguous bare names, and is threaded into pass 2 via the confirmed existing seam; the
      block degrades to a no-op (flagged fallback) on the single-file path.
- [ ] No new per-row `upsert_node`/`upsert_edge`; Part B's added work is once-per-statement
      (mirrors the #25 block); the four perf-invariant guard tests stay green.
- [ ] No `# TODO` in the happy path; every new helper has a grep-confirmed call site.
- [ ] CLONE-source cataloguing and per-column star resolution are recorded as **deferred
      follow-ups** (this plan's Non-Goals), not silently dropped.
- [ ] Live-DWH re-verification recorded in the PR (read-only; report's exact call now
      labelled; catalog-available sibling resolves; perf within budget).
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
  no-catalog tables. This is the honest signal the report asked for.

### Blocking Questions

- **Should the CLONE-source column catalogue be folded into this PR rather than deferred?**
  The report's own trigger table is only *labelled* (not *corrected*) by this plan, because
  correcting it requires parsing `CREATE TABLE … CLONE` and inheriting the source table's
  columns — a multi-hop schema feature with its own blast radius (CLONE parsing per dialect,
  a schema-inheritance pass, source-of-source ambiguity). This plan **defers** it with
  reasoning (see "Scope decision: the report's own table"). If the user wants it in-scope,
  that is a separate design pass — flag before implementing, do not stretch this PR.
- **(Resolved, recorded) Per-column `SELECT alias.*` star resolution** (report follow-up 4)
  is a distinct effort the report itself says can be scoped out; deferred as a Non-Goal.
