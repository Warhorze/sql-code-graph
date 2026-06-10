# Sprint Plan: Graph Health — Honest Metrics + Catalog Enrichment

**Status:** draft for review · **Date:** 2026-06-10 · **Author:** planning session (Fable)
**Supersedes:** [`coverage_blindspot_high_impact.md`](coverage_blindspot_high_impact.md) — its
primary lever (Fix B, edge-endpoint canonicalisation) was implemented and measured dead
(blindspot −17, edge health −1%, +18s; stash@{0} marked **DO NOT REVISIT**). This plan is
built on a new root-cause measurement session (2026-06-10, live DWH graph, queries via
`run_read_routed`).

---

### Blocking Questions (plan-reviewer 2026-06-10) — RESOLVED (user decision 2026-06-10)

Two source-grounded blockers were corrected in place; the user has decided all three open
questions. Neither blocked PR 1 (which is read-only and sound).

1. **HAS_COLUMN source-precedence has no mechanism today (affects PR 3 then PR 2).**
   `upsert_edges_bulk` emits plain `INSERT OR REPLACE` (no `ON CONFLICT DO UPDATE`); the
   `source`-precedence clause the plan assumed does not exist.
   **DECIDED: option (a) — precedence-aware backend upsert path + test.** A query-level
   change in the backend (conflict clause honouring `ddl > information_schema > usage`),
   keeping emitters dumb. Must land in **PR 3** (it ships first as the `ddl > usage` guard),
   reused by PR 2.

2. **`DbConfig` is not persisted — cannot store a catalog path there (affects PR 2 §3).**
   `DbConfig` is an env-only Pydantic model with no `save()`.
   **DECIDED: option (a) — `get_catalog_path()` `.sqlcg.toml` helper** mirroring the
   existing `get_*` pattern.

3. **Mixed catalog+DDL reindex** (per-file reindex DELETE is not source-filtered, so a
   catalogued table that later gains a DDL file loses its catalog rows on reindex).
   **DECIDED: source-aware delete** — the per-file deletion path excludes
   `HAS_COLUMN.source IN ('information_schema','usage')` rows (re-apply would only patch
   the window; source-aware closes it). The PR 2 §3 mixed-case test asserts catalog rows
   survive a reindex of the DDL file and DDL rows still replace catalog rows on precedence.

All other items (strict-check join, scoping by `kind`, `transform` consumer audit, PR 5 parser
location/semantics, json render sites) were verified and corrected in place — see the inline
plan-reviewer notes. **No `SCHEMA_VERSION` bump is required** (HAS_COLUMN.source column already
exists; option-a backend precedence is a query change, not a DDL change).

---

## 1. The measured root cause (2026-06-10, live DWH, ~69,076 edges)

All numbers below were measured this session against the live graph (server-routed reads;
raw SQL used for diagnosis only — the release gate remains `sqlcg gain`). The DB has
drifted slightly from the 70,465-edge / 979-blindspot baseline quoted in earlier plans;
re-baseline at Phase 0.

### 1.1 Where edges come from vs. what resolves them

| Edge origin | Edges | Share | Healthy (table-level) |
|---|---|---|---|
| DDL files (`ddl/changelogs/*.sql`) | 24,371 | 35% | **95%** |
| ETL files | 44,705 | 65% | **59%** |

**94% of all bad edges (18,244 / 19,438) come from ETL files** writing to tables whose
`CREATE TABLE` is not indexed. The DDL for those targets lives in Liquibase **XML**
changelogs (`<sql><![CDATA[...]]>`, 147 files, 3,363 distinct CREATE targets incl. 615
`da.*`) — and per user decision we will **not** parse XML. The historical
INFORMATION_SCHEMA feature was removed in 2026-05 based on "zero **edge delta**" — a true
but wrong-axis measurement: schema never creates edges, it makes edge *targets resolvable*
(`HAS_COLUMN`). The edge-health metric that would have shown the win didn't exist until
v1.6.1.

### 1.2 Bad-edge anatomy (19,438 edges, ~930 blindspot tables in current snapshot)

| Cohort | Tables | Edges | Honest fix |
|---|---|---|---|
| CTE/derived chain intermediates (correct by design, ARCHITECTURE_REVIEW §3.2) | 366 | 6,205 | Metric scoping (BQ-1), not data |
| Physical tables, dst column **validated by a read elsewhere** | ~217 | 3,600 | Usage-derived catalog (SQL-only) |
| Physical tables, dst column positionally guessed, **never corroborated** | ~235 | 9,314 | Real schema only (INFORMATION_SCHEMA) |
| Not in `SqlTable` (orphans) + derived | 115 | 494 | Diagnose / document |

### 1.3 The ruler itself is distorted (measured)

| Defect | Measured size |
|---|---|
| Edge health is **table-level**: an edge counts good if the dst *table* has any `HAS_COLUMN` row — the dst *column* is never checked | **1,504 edges counted good whose dst column does not exist** (1,028 of them are catalog-**contradicted** positional guesses — the worst edges in the graph, filed under "good"). Strict column-level health = **69.9%**, not 71.9% |
| Phantom rate conflates validated and unvalidated guesses, and is frozen at parse time | Of 18,318 phantom edges: **5,990 catalog-confirmed**, **1,028 catalog-contradicted**, **11,300 unverified** |
| Blindspot count is unweighted | 292 of ~930 tables carry ≤5 edges (4% of bad volume); top 21 tables carry 5,466 edges (28%) |
| No recall measurement, no context | All four metrics are precision ratios over *existing* edges — suppressing edges or indexing a stale corpus improves them. This is how the d88898c "100%" claim happened (real ≈68–72%), and why `anchor_omloopsnelheid: 0 edges` in [`sprint_08_fullcorpus_index.json`](../measurements/sprint_08_fullcorpus_index.json) is invisible to `gain` |
| Data-integrity wart | 122 edges whose exact dst column exists in `HAS_COLUMN` but whose table-strip lookup misses (inconsistent `src_key`/`dst_key` prefix rows) — diagnose in PR 1 |

### 1.4 Dead levers — do not revisit

- **P1b** (`_resolve_bare_source`, node layer): 0 metric delta. Stash@{1}.
- **Fix B** (edge-endpoint canonicalisation via `canonical_by_bare`): blindspot −17,
  edge health −1%, +18s index time. Stash@{0} ("DO NOT REVISIT"). Root cause of both
  failures: the canonical targets had no `HAS_COLUMN` either — folding bare names onto
  uncatalogued tables moves nothing. Catalog first; only then is any fold worth rethinking
  (and after PR 2/3 it is likely unnecessary: the metric counts the dst table itself,
  which will then be catalogued directly).
- **Schema-CSV prohibition** (CLAUDE.md): conditional on "no measured lineage-coverage
  win". The bar is now met (≤12,914 reachable bad edges, §1.2). PR 2 updates CLAUDE.md and
  ARCHITECTURE_REVIEW with this measurement as the recorded justification.

---

## 2. Goals, sequencing, and targets

Five PRs. **Execution order (user-decided 2026-06-10): PR 1 → PR 5 → PR 3 → PR 2 → PR 4.**
Rationale: fix the ruler first, then exhaust every **repo-native** lever (extraction
recall + usage catalog) and record that plateau, *then* load INFORMATION_SCHEMA so its
incremental lift is cleanly attributable. Ship each PR separately (MEMORY: separate PRs,
don't fold). PR numbers below are names, not sequence.

| Order | PR | What | Hot-path cost | Expected effect (strict column-level health, from 69.9%) |
|---|---|---|---|---|
| 1 | **PR 1** | Honest metrics in `gain`/`db info` | **Zero** (read-time only) | No data change; baseline becomes strict + scoped + fingerprinted + recall-aware |
| 2 | **PR 5** | ETL extraction recall — parse-failure reduction (diagnosis-first) | **Parse-time, but bounded per fixed shape**; more successful parses = more lineage work, so `--profile`-gated per shape | Recall, not health %: 1,855 ETL queries (58%) are `parse_failed` today and emit no column lineage. **Also the only repo-native star-expansion lever** — every recovered changelog `.sql` parse feeds the pass-1 schema harvest that pass-2 star expansion reads |
| 3 | **PR 3** | Usage-derived catalog (`source='usage'`) | **~Zero** (batch post-pass, no parse-time op) | +~3,600 edges, repo-native; together with PR 5 this is the "no-info-schema plateau" measurement |
| 4 | **PR 2** | INFORMATION_SCHEMA catalog enrichment | **Zero** (post-index, one-shot bulk) | Incremental lift over the repo-native plateau — the number that answers "was the export worth it". Pre-PR5 estimate: up to ~88% unscoped / ~95%+ scoped |
| 5 | **PR 4** (optional) | Transform-kind on edges (rename vs calculation) | **Small, bounded, measured** (per-column AST `isinstance` walk, no serialization) | New capability, no metric change |

**Star-expansion boundary (read before assuming):** `SELECT *` expansion is resolved at
**parse time** from pass-1 harvested DDL/CTAS bodies. PR 5 therefore improves star
expansion (recovered DDL parses enter the harvest); PR 3 and PR 2 deliberately do **not**
— they write catalog rows only, never feed the parse/qualify path (the forbidden hot
loop, and the design that measured zero in 2026-05). If, after PR 2's measurement, the
residual star-expansion gap justifies feeding info-schema into pass-2 resolution, that is
a **separate plan** with its own perf measurement — not an extension of PR 2.

**Gate metric:** `sqlcg gain` (MEMORY: never raw SQL for the gate). Raw SQL only inside
diagnosis steps.

**Perf budget (user-set, this sprint): full DWH index ≤ 180 s wall (`sqlcg index
--profile`).** Existing measurements disagree (72.85 s profile in one session; 210–256 s
typical per MEMORY; 313 s in sprint-08 measurement runs which include measurement
overhead). Phase 0 re-baselines; every PR records before/after `--profile`. PRs 1–3 are
designed to add **zero parse-time work**, so their delta must be within noise; PR 4 (§6)
and PR 5 (§7) are the only ones allowed a measurable delta, each individually gated —
PR 5's rise is legitimate work (recovered statements now extract lineage) and its
recall-vs-seconds trade-off is an explicit user call.

---

## 3. PR 1 — Honest metrics (`gain` Section G + `db info`)

### Design

All changes in [`coverage.py`](../../src/sqlcg/cli/coverage.py) (+ the render sites
[`db.py`](../../src/sqlcg/cli/commands/db.py) L178-199, [`gain.py`](../../src/sqlcg/cli/commands/gain.py)).
Read-only queries at command time — **nothing in the indexing path**. `idx_HAS_COLUMN_dst`
already exists ([`duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py) L210), so the
strict check is index-backed. No schema change, no `SCHEMA_VERSION` bump.

> **Source-verified render contract (plan-reviewer 2026-06-10):** `gain.py` builds the
> `--json` `coverage` block at **two** sites — the no-metrics path (L62-78) and the
> populated path (L181-199, `payload["coverage"]` at L193). **Both** dicts must gain the
> new keys or `sqlcg gain --json` is inconsistent depending on whether metrics exist.
> `db info` renders coverage at [`db.py`](../../src/sqlcg/cli/commands/db.py) L178-199 (text only,
> no JSON). `CoverageStats` (coverage.py L56-90) is a flat dataclass with three percent
> properties; new fields + properties extend it, existing six fields and their meaning are
> untouched (the "add, never repurpose" rule is satisfiable as written).

1. **Strict column-level edge health** (new primary): an edge is good iff
   `cl.dst_key IN (SELECT dst_key FROM HAS_COLUMN)`. **Verified against source:** both
   `COLUMN_LINEAGE.dst_key` and `HAS_COLUMN.dst_key` hold the full column id
   `{table.full_id}.{col_name}` ([`indexer.py`](../../src/sqlcg/indexer/indexer.py) L1148/L1160
   for HAS_COLUMN; the `_DST_TABLE` strip in [`coverage.py`](../../src/sqlcg/cli/coverage.py) L19
   confirms `dst_key` carries a trailing `.column`), so a direct equality join is correct and
   `idx_HAS_COLUMN_dst` (L210) backs it. Keep the existing table-level number, rename its
   label to "table-level (legacy)" — do not silently change the meaning of an existing
   field; `--json` adds new keys, never repurposes old ones.
2. **Scoped vs unscoped split** (resolves BQ-1): scoped excludes edges whose stripped dst
   resolves to `SqlTable.kind IN ('cte','derived')`. Report **both** lines. The unscoped
   formula is untouched — this adds a row, it does not change the ruler retroactively.
3. **Phantom three-way split**: `confirmed` (dst column exists in catalog),
   `contradicted` (dst table catalogued but column absent), `unverified` (dst table not
   catalogued). `contradicted` is the headline trust number — render it red > 0.5%.
4. **Edge-weighted blindspot view**: keep the raw count; add "top 10 blindspot tables by
   bad-edge count" and "tables covering 80% of bad-edge volume".
5. **Corpus fingerprint** printed with Section G: files indexed, index timestamp,
   `indexed_sha` (from `SchemaVersion`), DB path. A health % can no longer be quoted
   without its context.
6. **Recall lines** (the metrics above are all precision — they improve when edges are
   suppressed or files fail to parse; recall makes that visible):
   - parse-failure rate split by dir prefix (measured 2026-06-10: **58% of ETL queries**
     and 77% of DDL queries are `SqlQuery.parse_failed = true` on the live DWH);
   - count of **write-kind queries with zero outgoing `COLUMN_LINEAGE`** (measured: 92 in
     etl/) — these are statements that parsed but extracted nothing;
   - all from existing `SqlQuery`/`COLUMN_LINEAGE` columns, read-time only.
7. **Diagnosis (in-PR, no fix unless trivial):** the 122 prefix-inconsistent `HAS_COLUMN`
   rows — one query, root-cause note in the PR description; fix only if it is a one-line
   emission bug.

### Tests

- Unit: `CoverageStats` new fields + percent properties (zero-division guards), colour
  thresholds for `contradicted`.
- Integration (real DuckDB fixture, extend
  [`test_coverage_metrics_integration.py`](../../tests/integration/test_coverage_metrics_integration.py)):
  seed a graph containing (a) a good strict edge, (b) a table-level-good but
  column-contradicted edge, (c) a CTE chain edge, (d) a phantom-confirmed edge — assert
  each lands in exactly the right bucket with exact counts. Assert `--json` exposes
  strict/scoped/phantom-split keys and the legacy key unchanged.
- Render: Section G shows fingerprint fields (assert observable strings, not "no
  exception").

### Acceptance

- [ ] Strict, scoped, phantom-split, weighted-blindspot, fingerprint all visible in
      `sqlcg gain` and `sqlcg db info`; legacy table-level number still present and
      unchanged in meaning.
- [ ] No queries added to the indexing path (`--profile` delta = noise; grep-confirm no
      indexer/parsers imports from `coverage.py`).
- [ ] 122-row inconsistency diagnosed and written up.
- [ ] Full gate green (`pytest`, `pyright`, `ruff`); version bump **minor** (new metric
      surface).

---

## 4. PR 2 — INFORMATION_SCHEMA catalog enrichment (the lever)

### Design — catalog-level, never parse-time

This is **not** a revival of the removed feature. The 2026-05 implementation fed
`schema_sources` into the parse/qualify path (the hot loop) — that is exactly where it
measured zero and exactly what the perf invariants forbid. The new design never touches
the parser:

1. **New CLI command** `sqlcg catalog load <file>` (new module
   `src/sqlcg/cli/commands/catalog.py`):
   - Input: CSV export of `INFORMATION_SCHEMA.COLUMNS` (header-detected columns:
     `table_catalog?, table_schema, table_name, column_name`; extra columns ignored).
     Delimiter sniffed (`,`/`;`). Names case-folded to lower to match graph keys
     (the graph is already lower-cased — verified on live keys).
   - Emits `SqlColumn` + `HAS_COLUMN(source='information_schema')` rows via the existing
     `upsert_nodes_bulk`/`upsert_edges_bulk` (Phase A→B→C) — **one bulk call per type for
     the whole file**, never per-row (bulk-upsert invariant).
   - Also upserts a bare `SqlTable` row (`kind='table'`, `defined_in_file=''`) when the
     table is unknown, so catalog-only tables are visible to `find`/`db info`.
2. **Conflict policy — DDL wins:** never overwrite an existing `HAS_COLUMN` row whose
   `source='ddl'`; `information_schema` wins over `usage` (PR 3).
   **⚠ BLOCKER (plan-reviewer 2026-06-10): not implementable as the backend writes edges today.**
   `upsert_edges_bulk` ([`duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py) L463-505)
   emits a plain `INSERT OR REPLACE INTO "<rel>" ... SELECT unnest(...)` (L502-504) — **no
   `ON CONFLICT DO UPDATE`, no source precedence**: last writer wins unconditionally. Only
   `upsert_nodes_bulk` for `NodeLabel.TABLE` (L420-456) has an `ON CONFLICT DO UPDATE` guard;
   HAS_COLUMN edges do **not**. The `source`-precedence clause this plan assumes does not exist.
   Pick **one** before implementing (developer must not invent it silently):
   - **(a)** Add a precedence-aware upsert path for `HAS_COLUMN` in the backend (new method or a
     branch in `upsert_edges_bulk` that, for `RelType.HAS_COLUMN`, uses
     `ON CONFLICT (src_key, dst_key) DO UPDATE SET source = CASE ... END` ranking
     `ddl > information_schema > usage`). This is a backend change — add a unit test pinning the
     precedence and confirm it does not regress the bulk-upsert invariant. **Still no SCHEMA_VERSION
     bump** (no DDL change — the column already exists), but it is a real code change, not a config of
     an existing clause.
   - **(b)** Read-merge in the catalog/usage emitter: query existing `HAS_COLUMN` for the target
     `src_key`s, drop catalog/usage rows that would lower the precedence of an existing higher-source
     row, then bulk-upsert only the survivors. One extra read per `catalog load` / per batch (constant,
     not per-row) — keeps `upsert_edges_bulk` untouched.
   Whichever is chosen, the precedence is a **named, tested** behaviour, not "expressible in the
   existing clause".
3. **Survival across reindex:** per-file reindex (`delete_nodes_for_file`,
   [`duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py) L550-578) deletes
   `HAS_COLUMN` rows by `src_key IN (SELECT src_key FROM DEFINED_IN WHERE dst_key = <file>)`
   (L572-578) — i.e. keyed on the table's `DEFINED_IN` edge, **not** on `HAS_COLUMN.source`.
   A catalog-only table (`defined_in_file=''`, no `DEFINED_IN` edge) therefore survives
   incremental reindex. **Caveat the developer must handle (plan-reviewer):** if a catalogued
   table *also* gains a DDL file later, that file's reindex deletes **all** its `HAS_COLUMN`
   rows under the shared `src_key` — including the `information_schema`/`usage` rows — because
   the DELETE is not source-filtered. Test the mixed case explicitly (catalog row + later DDL
   reindex of the same table) and decide: either re-apply catalog after such a reindex, or make
   the per-file delete source-aware (delete only `source='ddl'`). Document the chosen behaviour.
   For full rebuilds, the catalog must be re-applied after `index_repo`:
   **⚠ BLOCKER (plan-reviewer 2026-06-10): `DbConfig` is not a persisted/savable object.**
   `DbConfig` ([`config.py`](../../src/sqlcg/core/config.py) L14-35) is a Pydantic model built only
   from env vars (`from_env`) with two fields (`db_path`, `log_path`) and **no `save()` / no on-disk
   write**. All user-facing settings live in `.sqlcg.toml`, read ad-hoc by module-level
   `get_*(path)` helpers (e.g. `get_dialect`, `get_schema_aliases` L62-114). There is nowhere to
   "persist the catalog path in `DbConfig`". Choose one:
   - **(a)** Add a `get_catalog_path(path)` helper that reads `[sqlcg.catalog] path = "..."` from
     `.sqlcg.toml` (mirrors the existing `get_*` pattern exactly); `index_repo` reads it at the end
     and re-applies if set + readable. No `DbConfig` change, no save logic needed.
   - **(b)** Require the user to re-run `sqlcg catalog load` after a full rebuild (documented two-step:
     `index` then `catalog load`). Simpler, zero new config surface, but a manual step.
   At the **end** of `index_repo` (after the last batch flush, outside all hot loops), if option (a)
   is chosen and a catalog path resolves + is readable, re-apply it (same one-shot bulk path). If
   missing/unreadable: one warning line, index result unaffected.
4. **Zero-config preserved:** no flag, no behaviour change for users who never run
   `catalog load`. Small repos see nothing new (CLAUDE.md design rule).
5. **Docs:** update CLAUDE.md "Schema resolution" paragraph and ARCHITECTURE_REVIEW —
   the prohibition's condition ("no measured win") is now disproven by §1; record these
   numbers as the justification and state the new rule: catalog-level enrichment is
   allowed, parse-time schema feeding remains forbidden.

### Why this beats the alternatives (decided this session)

- **XML CDATA parsing**: user-rejected; also strictly dominated — INFORMATION_SCHEMA
  covers the XML-bound tables *plus* truly-external tables *plus* true column names that
  falsify wrong positional guesses (turning silent wrongness into measurable
  `contradicted`).
- **Bare-name folds**: dead twice (§1.4); after this PR the dst tables are catalogued
  directly, so the fold has nothing left to win for the metric.

### Hot-path / perf analysis

| Concern | Why it stays inside budget |
|---|---|
| Indexing wall time | Nothing runs during file parsing or batch flushing. The optional end-of-index re-apply is one bulk upsert of ~30–50k rows (DuckDB executor: single-digit `execute()` calls, expected ≤ 2 s — measured in Step 2.3). |
| Bulk/batch invariants | Reuses `upsert_*_bulk` (option b) or a new precedence-aware HAS_COLUMN path (option a); either way adds a **constant** number of `execute()` calls per `catalog load`, zero per file. [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py) must stay green. |
| Memory | CSV streamed row-by-row into lists of dicts (~50k rows ≈ a few MB); no per-file accumulation change. |

### Tests

- Unit: CSV header detection, delimiter sniff, case-folding, malformed-row skip count
  (assert reported skip count, not silence).
- Integration: load a fixture CSV → assert exact `HAS_COLUMN` rows with
  `source='information_schema'`; conflict test (pre-existing `ddl` row not overwritten;
  `usage` row upgraded); previously-bad edge flips to strict-good; a contradicted phantom
  edge stays bad and is counted in PR 1's `contradicted` bucket (the false-guess
  detection is the test).
- Reindex survival: `catalog load` → `reindex_file` → rows still present; full
  `index_repo` with configured path → rows re-applied; with the file deleted → warning,
  index succeeds.
- Perf guard: assert `catalog load` calls each bulk-upsert entry point once per load
  (counter-based, mirrors the batch-invariant test style).

### DWH verification (Step 2.3, recorded in postmortem)

User exports `INFORMATION_SCHEMA.COLUMNS` from Snowflake (one query, any warehouse), then:
`sqlcg catalog load cols.csv` → `sqlcg gain`. Record strict/scoped health, phantom split
(confirmed/contradicted/unverified), blindspot count, `catalog load` wall time, and
`sqlcg index --profile` before/after to prove the index path is untouched.

### Acceptance

- [ ] `sqlcg catalog load` exists, documented in README; idempotent (second load = no row
      growth).
- [ ] DDL-precedence conflict policy enforced in SQL, tested.
- [ ] Catalog survives per-file reindex; re-applied after full index when configured.
- [ ] DWH: strict health delta recorded **against the post-PR5+PR3 repo-native plateau
      snapshot** (the "was the export worth it" number), plus contradicted-phantom count.
      Pre-PR5 estimate was ≥85% unscoped from 69.9%; restate the expected floor from the
      plateau before implementing. If the export covers all `da.*`/`ba.*` targets and the
      lift lands far short, stop and diagnose before merging — the gap is then something
      new and unmeasured.
- [ ] `sqlcg index --profile` delta vs Phase-0 baseline within noise (< 5 s); ≤ 180 s
      total.
- [ ] CLAUDE.md + ARCHITECTURE_REVIEW updated (prohibition condition resolved, rationale
      recorded).
- [ ] Version bump **minor**; full gate green.

---

## 5. PR 3 — usage-derived catalog (runs **before** PR 2 in the decided order)

**Role in the sequence:** PR 3 + PR 5 together define the **repo-native plateau** — the
best the graph gets without any database access. Its `sqlcg gain` snapshot (recorded in
measurements/) is the baseline PR 2's incremental lift is judged against. Note the
read-validated payload (~3,600 edges) was sized *before* PR 5; re-size after PR 5 lands
(recovered parses may validate more, or catalog targets directly via recovered DDL).

### Design

- In [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `_flush_row_batch`: the batch's
  `column_lineage_edges` rows are already in memory. Post-pass (pure Python, set-dedup):
  for each edge **src** endpoint whose table resolves to `kind IN ('table','view')`
  (never cte/derived — lookup against the batch's table rows + `canonical` dict already
  in scope), collect `(table, column)` pairs → emit `SqlColumn` +
  `HAS_COLUMN(source='usage')` rows appended to the same batch bulk upserts.
- Honesty guard: src endpoints come from sqlglot-resolved leaves (real parsed refs), never
  from `inferred_from_source_name` dst guessing. A read is proof the column exists.
- Precedence: `usage` loses to both `ddl` and `information_schema`. **Uses the same
  precedence mechanism decided in PR 2 step 2** (option a backend path, or option b read-merge) —
  **not** an `ON CONFLICT` clause that does not exist in `upsert_edges_bulk` today (see PR 2
  blocker). Because PR 3 ships **before** PR 2 in the decided order, PR 3 is the first to need
  the precedence mechanism: the `ddl > usage` guard must land **in PR 3** (a `usage` row must not
  overwrite a `ddl` row), and PR 2 then only adds the `information_schema` tier on top. Move the
  precedence-mechanism decision earlier accordingly.

### Hot-path analysis

O(edges-in-batch) set operations per flush; **zero** new `execute()` (rows ride the
existing bulk calls); **zero** parse-time work; no per-column qualify/scope/sg_lineage.
Add a counter assertion to the batch-invariant test (bulk entry points still called once
per batch). `--profile` before/after recorded; expected delta < 2 s.

### Tests

- Integration: file reads `t.a`, `t.b` with no DDL anywhere → `HAS_COLUMN(t, a/b,
  source='usage')` exist; a CTE read produces **no** usage rows; precedence respected.
- Metric coupling: a previously read-validated bad edge flips strict-good.

---

## 6. PR 4 (optional) — transform kind on lineage edges

Motivation (user question, answered this session): renames (`a AS b`) and calculations
(`x * y AS total`) both already produce correct, traversable edges (verified empirically —
two component edges for the calculation), but `transform` is `'SELECT'` for everything, so
a rename and a multiplication are indistinguishable and the formula is reachable only via
the edge's `query_id` file:line.

### Design (bounded)

- In `_extract_column_lineage` ([`base.py`](../../src/sqlcg/parsers/base.py)), classify
  the projection expression **per column, AST-only, no serialization**:
  `PASS_THROUGH` (bare column, same name) / `RENAME` (bare column, aliased) /
  `AGGREGATION` (`find(exp.AggFunc)` within the projection) / `EXPRESSION` (everything
  else). Set as the edge `transform` for SELECT-shaped edges; `CTE_PROJECTION*` values
  untouched.
- Explicitly **not** storing expression SQL text (a per-column `.sql()` serialization is
  the exact per-call-cost class that regressed in `4234e5d`). If formula text is wanted
  later, it is a separate plan with its own measurement.

### Hot-path gate (this PR is the only one allowed a measurable delta)

- Classification is `isinstance` checks + one bounded `find` on the *single projection
  expression* (not the body), per column — same order as existing per-column work.
- Behavioural guard in [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py):
  classification runs once per column, zero `qualify`/`build_scope`/`exp.expand` calls,
  and **zero `.sql()` calls** on the classification path (counter).
- `sqlcg index --profile` before/after; budget: delta ≤ 5 s on full DWH, hard stop and
  redesign if exceeded.

### Tests

Fixture with one pass-through, one rename, one aggregation, one arithmetic expression →
assert the four exact `transform` values on the four edges (observable output).

**Consumer audit (already done by plan-reviewer 2026-06-10 — confirm, then proceed):** the
only readers of `transform` in MCP/CLI are
[`tools.py`](../../src/sqlcg/server/tools.py) L839/L880 (`row.get("transform") or "SELECT"` —
used purely as a Mermaid edge **label**, defaults to `"SELECT"`, does **not** branch on the value)
and `_reason_for` / `_REASON_MAP` (L707-728), which key on `STAR_EXPANSION` / `UNKNOWN` / `None` —
**never on `'SELECT'`**. [`models.py`](../../src/sqlcg/server/models.py) L78 only documents
`SELECT, CAST` as field-description examples (doc string, no logic). **No consumer filters on
`'SELECT'`**, so emitting `PASS_THROUGH`/`RENAME`/`AGGREGATION`/`EXPRESSION` instead changes
Mermaid labels (intended, more informative) and breaks nothing. If the developer adds any new
`transform`-filtering consumer, it must be added to this audit before merge (wiring rule).

---

## 7. PR 5 — ETL extraction recall (parse-failure reduction, diagnosis-first)

Measured 2026-06-10 on the live DWH (`SqlQuery` table): **1,855 of 3,225 etl/ queries
(58%) have `parse_failed = true`** and contribute zero column lineage; a further 92 etl/
write-kind queries parse but emit no edges. This is the recall axis — invisible to every
health percentage, and untouched by PRs 1–4 (catalog work can only resolve edges that
exist). The E{n} failure taxonomy and `gain` Section F already classify parse errors;
this PR turns the top classes into fixes.

> **Source-verified semantics caveat (plan-reviewer 2026-06-10):** `parse_failed` is set
> **per statement** on the *scope-build / column-lineage degraded path*, not only on a hard
> tokenizer failure — [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) L333/L353/L361
> set `parse_failed=True` whenever `build_scope` returns falsy or raises, then fall back to
> `_fallback_table_scan` (so the statement still emits **table-level** source edges, just no
> column lineage); `snowflake_parser.py` L408-409 and `bigquery_parser.py` L50 set it for
> scripting blocks. So "parse_failed ⇒ zero **column** lineage" is correct, but "parse_failed ⇒
> the statement is unparsed/contributes nothing" is **not** — many of the 1,855 already emit
> table edges. The diagnosis step (1) must therefore bucket by *what is missing* (column lineage
> absent vs. statement entirely unparsed), because a statement that already has table edges and
> a correct `target_table` may not be the highest-value recall target. This sharpens, not
> contradicts, the existing splitting-inflation sanity check.

### Steps

1. **Diagnosis (gate for everything else):** bucket the 1,855 failures by E-code /
   exception shape and by file; same for the 92 zero-edge write queries. Rank by
   *distinct write-target tables affected*, not raw query count (a failure repeated in
   100 changelogs may matter less than one in a core ETL). Record the table in
   measurements/. Sanity-check the counter itself: confirm `parse_failed` means
   statement-level failure, not file-level double-counting — if the metric is inflated
   by splitting artifacts, fix the measurement first (PR 1 owns the line).
2. **Fix the top shapes only**, one commit per shape, each: minimal repro fixture →
   once-per-statement fix → assert edges now exist (observable output). **Correct fix location
   (plan-reviewer 2026-06-10):** the cited `ALTER ... SET TAG` pre-strip precedent lives in
   [`snowflake_parser.py`](../../src/sqlcg/parsers/snowflake_parser.py) L187-225 (not in
   `ansi_parser.py`/`base.py`). The DWH indexes with the **snowflake** dialect, so statement
   pre-strips / dialect tweaks for changelog `.sql` and ETL shapes belong in `snowflake_parser.py`
   (or its `preprocess`/strip hooks). Use `ansi_parser.py`/`base.py` only for dialect-agnostic
   splitter or scope-build fixes. Likely candidates from prior sessions: Liquibase directive
   comments and `${property}` tokens in changelog `.sql` files; scripting-block detection.
   **Stop condition:** any shape needing per-column work or a new hot-loop op is out —
   record it as structural instead.
3. **Re-measure:** parse-failure rate by dir (PR 1 line) + edge count + `sqlcg gain` +
   `sqlcg index --profile`.

### Perf note (this PR can *legitimately* raise index time)

Every failure fixed means a statement that now runs full lineage extraction — more work
by design. Budget discipline: record `--profile` after each shape lands; the 180 s budget
(§2) is the ceiling. If a single shape unlocks thousands of statements and blows the
budget, ship it behind nothing — instead measure, then decide with the user whether the
recall is worth the seconds (that trade-off is explicitly a user call, per the small-repo
/ large-repo design rule).

### Acceptance

- [ ] Failure taxonomy table committed to measurements/ (shape × query count × distinct
      write targets × fixed/structural verdict).
- [ ] Each shipped shape: repro fixture test green, edges asserted, regression-guard
      block green, `--profile` recorded.
- [ ] ETL parse-failure rate reduction recorded via the PR 1 recall line (target set
      after diagnosis — escalate if the top 3 shapes cover < 20% of failures).
- [ ] Version bump **patch or minor** per shipped surface; full gate green.

### Phase A diagnosis result (2026-06-10)

Full taxonomy: [`pr5_extraction_recall_taxonomy.json`](../measurements/pr5_extraction_recall_taxonomy.json).
Re-indexed `/home/ignwrad/Projects/dwh` into a throwaway DB
(`/tmp/pr5_diag/graph.db`, 6,857 `SqlQuery` rows, 4,727 `parse_failed=true` —
68.9%, close to the 4,770/7,008 baseline; corpus drift accounts for the small
delta). Bucketed all `parse_failed=true` rows by `kind` and sampled SQL text:

| Shape | Count | % of failures | Distinct write targets | Verdict |
|---|---|---|---|---|
| `kind=OTHER` (TRUNCATE, USE, COMMENT, SET *var*, ALTER VIEW/TABLE incl. SET TAG w/o trailing `;`, ALTER EXTERNAL, DROP, CALL) | 3,957 | 83.7% | 1 | **fixed** (measurement) |
| `kind=CREATE_TABLE` w/o `AS SELECT` (LIKE, CLONE, column-defs, SEQUENCE) | 524 | 11.1% | 296 | **fixed** (measurement) |
| `kind=INSERT ... VALUES` (no SELECT) | 63 | 1.3% | 4 | **fixed** (measurement) |
| `kind=UPDATE/DELETE/MERGE/CREATE_PROC` | 183 | 3.9% | 1 | structural (no UPDATE/DELETE lineage path; MERGE = T-07-06) |

**Root cause (single, dialect-agnostic):** `build_scope(stmt)` returns `None`
for almost every non-`exp.Select`-rooted statement by sqlglot design — including
statement types that `_extract_column_lineage` (base.py L663/672-674/681) was
never going to attempt extraction for in the first place (Use, Set, Comment,
TruncateTable, Drop, Alter, Command, CREATE without AS-SELECT, INSERT without
SELECT, Update, Delete, Merge). `ansi_parser.py` `_parse_statement` (L307-332)
unconditionally set `parse_failed=True` whenever `build_scope` was falsy/raised,
conflating "structurally lineage-free statement" with "degraded extraction of a
statement that should have produced lineage". This is the W4-flagged "sanity-check
the counter itself" case — **96.1% of `parse_failed=true` rows (4,544/4,727) were a
measurement artifact, not a recall opportunity**.

**Fix (PR5, ships as Phase B "shape 1"):** add `SqlParser._can_have_column_lineage(stmt)`
(base.py, mirrors `_extract_column_lineage`'s own type/body checks exactly) and gate
both `parse_failed=True` branches in `ansi_parser.py` `_parse_statement` on it.
Dialect-agnostic, zero new hot-loop ops, no per-column work — pure reclassification
of an existing boolean. This is the only shape shipped; the remaining 183 (3.9%)
UPDATE/DELETE/MERGE/CREATE_PROC rows are a structural gap (new column-lineage
extraction path needed for UPDATE/DELETE; MERGE is the existing T-07-06 deferral) and
are explicitly out of scope per the "no per-column work" stop condition.

**Top-3-shapes-cover-≥20%-gate:** 96.1% ≫ 20% — proceed to Phase B.

**122-row `HAS_COLUMN` prefix-inconsistency (carried-over open item):** measured 61
in this snapshot (plan baseline 122; same root cause, corpus drift). Root cause:
`coverage.py`'s `_DST_TABLE` macro strips everything after the **last** `.` in
`dst_key` to derive the destination table id — this breaks for column names that
themselves contain a literal `.` (Dutch BI names like `"Omzet excl."`,
`"Gegeven korting incl."`). All 5 sampled rows are this exact shape. **Verdict:
structural, not a one-line fix** — a purely syntactic "strip after last dot" cannot
disambiguate `schema.table.col.with.dots` from `schema.table.col`; a correct fix
needs a `HAS_COLUMN`-keyed longest-prefix lookup or a stored `table_id` column.
Filed as a future `coverage.py` follow-up, not fixed in PR5.

---

## 8. Cross-cutting

### Phase 0 (before PR 1 merges): re-baseline

Fresh full DWH index off master → record `sqlcg gain`, `sqlcg db info`, `sqlcg index
--profile` (settle the 72.85 s vs 210–256 s vs 313 s discrepancy; the recorded number is
this sprint's reference). Commit to
[`plan/measurements/`](../measurements/) as `graph_health_phase0_baseline.json`.

### Mandatory regression-guard block (any PR touching `base.py`/`indexer.py` — PR 3, PR 4)

```
uv run pytest tests/unit/test_perf_scaling_guard.py \
              tests/unit/test_T09_01_qualify_once.py \
              tests/unit/test_bulk_upsert_invariant.py \
              tests/unit/test_upsert_batch_invariant.py \
              tests/integration/test_analyze_case_fold.py -x
```

### Non-goals

- XML changelog parsing (user decision; dominated by PR 2).
- Any bare→canonical edge/node rewrite (twice-dead, §1.4).
- Parse-time schema feeding (`schema_sources` into qualify) — still forbidden.
- Stored-procedure parsing (`<unknown>`, 764 edges) — structural, documented.
- Changing/removing any existing metric field's meaning (PR 1 adds, never repurposes).
- Temporal rename tracking (graph is a snapshot; out of scope, noted for the backlog).

### Risks

| Risk | Mitigation |
|---|---|
| INFORMATION_SCHEMA export is stale vs repo (env drift) | Provenance is explicit (`source='information_schema'`); fingerprint (PR 1) shows load date; `contradicted` bucket surfaces disagreements instead of hiding them |
| Strict metric makes the headline number drop (~72% → ~70%) before PR 2 lifts it | Communicate in PR 1: the old number is kept as legacy; the drop is measurement honesty, not regression |
| Catalog re-apply slows `index` | One-shot bulk after the last flush; measured in Step 2.3; warning-and-skip on failure |
| PR 4 transform values break a consumer filtering on `'SELECT'` | Grep-confirmed consumer audit in-PR; wiring rule |
| Conflicting perf baselines cause false alarms | Phase 0 single authoritative baseline, recorded in measurements/ |

### Rollout / rollback

Re-index (+ optional `catalog load`) is the migration path; no schema change in any PR
(`HAS_COLUMN.source` already exists). Rollback = revert the PR; catalog rows are removable
with `db reset` + reindex. Versioning: PR 1 and PR 2 are each a **minor** bump; PR 3/PR 4
minor if shipped. Tags after merge by the user (MEMORY: agent never tags).
