# Feature Plan: Coverage Parse-Failure Recovery (E5/E8) + Honest Index Metric

## Summary

Recover lineage coverage lost to the two dominant parse-degradation classes on the live
DWH — **E5** (`lineage_other`, case-sensitive quoted view aliases) and **E8**
(`no_edges_from_root` / `dynamic_source`, unresolved temp-table-chain sources) — and fix
the misleading index-summary metric that reports "1 failed" while 410 files carry
`parse_failed=True`. Three independent PRs, instrumentation-first.

**This plan carries the diagnosis findings inline so the developer does not re-derive the
root cause.** The full investigation is
[`coverage_parse_failure_diagnosis.md`](../research/coverage_parse_failure_diagnosis.md);
this plan is the executable form. Linked findings: ARCHITECTURE_REVIEW.md **F7**, **F8**,
§6.4 (orphan belt), §7 (coverage sprint).

---

## Scope

### In Scope

- **PR-1** — Honest index-summary metric: surface the real degrading/`parse_failed` total
  in `sqlcg index` output so coverage gaps are visible at index time (ARCHITECTURE_REVIEW F8).
- **PR-2** — E5 fix: make the column-lineage path resolve case-sensitive quoted single-token
  view-output aliases (T&T Liquibase views) without uppercasing them, recovering per-column
  lineage for ~211 IA-SEMANTIC / IA-TABLEAU view files.
- **PR-3** — E8 fix, **scoped to the `dynamic_source` / `CREATE TEMP TABLE … AS SELECT`
  chain sub-pattern only**: model the intra-file temp table as an intermediate lineage node
  (mirroring existing CTE chaining) so the final write resolves through it to its leaf
  sources, reconnecting ~89 island tables.

### Non-Goals

- **No parse-time schema feeding.** PR-2/PR-3 MUST NOT pass `schema_sources` into
  `exp.expand()` / `qualify()` (CLAUDE.md hot-loop prohibition, measured-zero in 2026-05).
  Catalog-level enrichment only, where relevant.
- **PR-3 does NOT target the full E8 population.** The residual `star` / `merge_branch`
  columns that ride alongside `dynamic_source` in the same files (e.g. `WTFE_KORTING.sql`:
  `{"dynamic_source":29,"star":9,"merge_branch":1}`) are **explicitly deferred**. Many E8
  files will NOT flip fully clean from PR-3 — partial-per-file recovery is the honest target.
- **No cross-file / cross-session temp resolution.** Only intra-file `CREATE TEMP TABLE`
  chains are in scope (ARCHITECTURE_REVIEW §7: cross-session temps are a static-analysis
  limit needing runtime info). Document any chain that crosses file boundaries as deferred.
- **The 4 `htdyn_*_delete.sql` E5 tail** (`Cannot find column 'PARTITION'` — reserved-word
  misuse) is NOT in PR-2 scope; it is a distinct 4-file mechanism. Note and defer.
- **No timeout work.** Only 1 file times out (`etl/sql/dim/wtdh_artikel.sql`); the brief's
  premise that E5+E8 (not timeouts) are the lever is confirmed.
- No tagging, no issue-closing (user-gated per house rule). No `git add -A`.

---

## Diagnosis carried forward (the developer must NOT re-derive this)

### E5 — what it is and why it fails

- **Bucket source:** [`error_classify.py:124-151`](../../src/sqlcg/indexer/error_classify.py#L124).
  An error `col_lineage:<col>:Cannot find column '<NAME>'` where `<NAME>` is a plain
  identifier (no parens → not E2; not literal `NULL` → not E1). Emitted at
  [`base.py:1465`](../../src/sqlcg/parsers/base.py#L1465).
- **Cluster (measured):** 217 files — 120 `ddl/changelogs/IA-SEMANTIC`, 91
  `ddl/changelogs/IA-TABLEAU`, 1 `IA-TABLEAU-OBT`, 1 `ddl/semtex_views.sql` (= 213 Liquibase
  view files), + 4 `etl/sql/int/htdyn_*_delete.sql` (the deferred tail).
- **Root cause (reproduced):** T&T-generated views declare quoted, case-preserved output
  columns and rename them in the SELECT:

  ```sql
  CREATE OR REPLACE VIEW IA_SEMANTIC."Afdeling" (
      "Afdeling koppelcode" COMMENT '...',
      "Afdelingsnummer"     COMMENT '...'
  ) AS SELECT
      S1_AFDELING AS "Afdeling koppelcode",
      DN_AFDELING AS "Afdelingsnummer"
  FROM BA.WTDA_AFDELING;
  ```

  The view output column name is taken from `col_expr.alias`
  ([`base.py:1351-1352`](../../src/sqlcg/parsers/base.py#L1351)), preserving the quoted case
  `"Afdelingsnummer"`. That `col_name` is then passed to `sg_lineage(col_name, body, ...)`
  ([`base.py:1422`](../../src/sqlcg/parsers/base.py#L1422)) against `body_scope`, which is
  built from `qualify(..., identify=False)`
  ([`base.py:1064-1072`](../../src/sqlcg/parsers/base.py#L1064)). qualify **uppercases the
  bareword-looking single-token alias** to `AFDELINGSNUMMER`, so the case-sensitive lookup
  of `"Afdelingsnummer"` misses → raises `Cannot find column 'AFDELINGSNUMMER'` → E5.
- **Why only some columns fail (the discriminator):** in `AFDELING.sql` only
  `Afdelingsnummer` failed; `"Afdeling koppelcode"`, `"Afdeling code"`, `"Afdeling naam"`
  resolved. **Single-token** quoted identifiers look bareword-like and get uppercased; names
  **with spaces** stay quoted and match. `IA-TABLEAU/ARTIKEL.sql` reproduced **39** such
  single-token failures.
- **Satellite impact (measured):** **E5 does NOT create satellites.** All 224 tables defined
  in E5 files already have incoming `COLUMN_LINEAGE` (224/224 = 100%). E5 is a
  **column-level completeness** gap inside already-connected views, not an island driver.
  → PR-2 acceptance: **strict edge health up, islands flat.**

### E8 — what it is and why it fails

- **Bucket source:** [`error_classify.py:121-122`](../../src/sqlcg/indexer/error_classify.py#L121).
  The skip marker `col_lineage_skip:dynamic_source:<col>` emitted at
  [`base.py:1451-1452`](../../src/sqlcg/parsers/base.py#L1451): `sg_lineage` **succeeded**
  (returned a root) but `_lineage_node_to_edges` returned an empty list — the root's leaf
  source doesn't resolve to a known `SqlTable`.
- **Cluster (measured):** 184 files — 79 `etl/sql/fact`, 33 `etl/sql/dim`, 19 `etl/sql/int`,
  16 `etl/sql/da`, 13 `etl/sql/interface`, 12 `etl/pdi/template`, 9 `ddl/changelogs/BA-TABLES`,
  2 validation. ETL transform scripts.
- **Root cause (reproduced):** the **`CREATE TEMP TABLE` chain**. Example
  `etl/sql/fact/wtfa_inkoop.sql`:

  ```sql
  create temp table tmp_inkooporder as select ... from BA.WTFE_INKOOP_ORDER ...;
  -- ... final
  INSERT INTO WTFA_INKOOP SELECT ... FROM tmp_inkooporder ...;
  ```

  The final write's lineage root resolves to a temp table (`tmp_*`) that has no
  DDL/`SqlTable` leaf in scope, so `_lineage_node_to_edges` finds no leaf → empty edge list →
  `dynamic_source` → E8. The fix mirrors the existing CTE-as-intermediate handling at
  [`base.py:1471-1478`](../../src/sqlcg/parsers/base.py#L1471): treat the in-file
  `CREATE TEMP TABLE x AS SELECT …` body as an intermediate source so the final write chains
  through it to the real leaf.
- **E8 is NOT monolithic (skip_counts evidence, measured):**
  `WTFE_KORTING.sql` `{"dynamic_source":29,"star":9,"merge_branch":1}`,
  `WTDH_ARTIKEL.sql` `{"star":12,"dynamic_source":1}`,
  `WTFS_VOORRAAD_ZONDAGSTAND.sql` `{"dynamic_source":4,"star":2}`. `dynamic_source` is the
  dominant driver but `star`/`merge_branch` ride alongside in the same files. → many files
  won't fully clear; **PR-3 targets the `dynamic_source` slice only.**
- **Satellite impact (measured):** **E8 IS the satellite driver.** ~**89 tables** defined in
  E8 files have NO incoming column lineage (the temp-chain write targets). Against the ~147
  reported small islands, E8 plausibly accounts for ~60% (89/147 — 89 measured, 147
  denominator from the brief). → PR-3 acceptance: **islands down ~89, strict up modestly.**

### The index-summary metric bug

- **Build site:** [`index.py:417-444`](../../src/sqlcg/cli/commands/index.py#L417).
  - `n_timeout = err.get("timeout", 0)` ([index.py:425](../../src/sqlcg/cli/commands/index.py#L425))
    — count of `timeout:` error strings. DWH = 1.
  - `n_failed = quality.get("failed", 0)` ([index.py:424](../../src/sqlcg/cli/commands/index.py#L424))
    — incremented **only when `_build_file_rows` throws** at
    [`indexer.py:1634-1641`](../../src/sqlcg/indexer/indexer.py#L1641). A row-**construction**
    crash — a completely different event from a lineage degradation. DWH = 1.
- **What it counts wrong:** nothing in the summary reads `File.parse_failed` / `parse_cause`.
  A file with 39 E5 column errors still has `parse_quality = full`/`table_only` (it emitted
  table edges, no row-build crash), so it lands green/yellow and is invisible to "failed".
  Summary reports **2** problem files while **410** are `parse_failed=True`.
- **The data is already present.** `error_summary` (the per-bucket E-code counts) is in the
  result dict at [`indexer.py:740`](../../src/sqlcg/indexer/indexer.py#L740), populated at
  [`indexer.py:715-719`](../../src/sqlcg/indexer/indexer.py#L715). The `_DEGRADING` frozenset
  at [`error_classify.py:27-29`](../../src/sqlcg/indexer/error_classify.py#L27) defines which
  buckets degrade: `{E1, E2, E3, E5, E8, timeout, worker_error, func_fallback, qualify_failed}`.

---

## Design

### PR-1: Honest index-summary metric

**API/CLI surface change:** a new summary line in `sqlcg index` output. Minor surface
addition (a new printed line, no flag, no schema change).

The summary path at [`index.py:417-444`](../../src/sqlcg/cli/commands/index.py#L417) reads
`summary["error_summary"]` (already in the result dict). Add a degrading-total line that
reports the count of degrading column-error files (or, preferably, the count of
`parse_failed=True` files — see Step 1.1 for the exact source decision).

**Source-of-truth decision the developer must make and document:** there are two candidate
denominators, and they differ:
1. `sum(error_summary[c] for c in _DEGRADING)` — counts **error strings**, not files (a file
   with 39 E5 errors contributes 39). This over-counts files.
2. **File-level `parse_failed=True` count** — the metric the brief and ARCHITECTURE_REVIEW F8
   actually call out ("410 files carry `parse_failed=True`").

**Choose the file-level count (#2)** — it is the number F8 names and the one that maps to the
orphan belt. `dominant_cause()` ([`error_classify.py:32`](../../src/sqlcg/indexer/error_classify.py#L32))
already returns `(parse_cause, parse_failed)` per file; `parse_failed` is persisted on the
`File` node and was the basis for the diagnosis count. The developer must:
- Surface a **count of files with `parse_failed=True`** into the result dict alongside
  `error_summary` (add an aggregate in the same `pass2_results` loop at
  [`indexer.py:715`](../../src/sqlcg/indexer/indexer.py#L715) that calls `dominant_cause()`
  per file — or reuse the already-computed `parse_cause`/`parse_failed` if available on the
  parsed objects; grep the parsed object for an existing field before adding a new loop).
- Print it as a **distinct, non-replacing** line, e.g.:
  `  410 files degraded (parse_failed) — E5: 217 · E8: 184 · timeout: 1 · …`
  alongside the existing quality line. Keep the existing "N failed" / "N timed out" wording
  untouched (it correctly reports row-build crashes + timeouts respectively).

**No parser changes. No hot-path touch. Lowest risk — ships first.**

### PR-2: E5 — case-sensitive quoted single-token view-alias resolution

**The fix is in how the view output-column name is looked up against the body**, NOT in
qualify's normalization (changing `identify`/normalization globally would regress every other
path and break the perf invariants). Options for the developer, in preference order:

1. **Match the projection alias by position / structural identity rather than re-resolving
   the renamed output name** — when the statement is a `CREATE VIEW` (or any SELECT with an
   explicit `AS "<quoted>"` alias), the alias is the *output* name; lineage only needs the
   *source* expression under the alias. Resolve lineage on the source expression (the thing
   left of `AS`) and attach the original quoted alias as `dst_col_name` without round-tripping
   the quoted name through `sg_lineage(col_name, ...)`.
2. **Normalize the lookup name consistently with how qualify normalized the body** — if
   qualify uppercased single-token aliases in the scope, look the column up under the same
   normalization, then restore the original quoted case for the emitted `dst_col_name`. This
   keeps `sg_lineage` resolving against the scope but stops the case mismatch.

The developer chooses based on what keeps the perf invariants intact (see Risk section).
**The emitted edge's destination column name MUST be the original quoted case
(`"Afdelingsnummer"`), not the uppercased form** — the graph column identity must match the
view's declared output.

**Scope of change:** the column-name resolution / `col_name` derivation in the main column
loop ([`base.py:1322-1469`](../../src/sqlcg/parsers/base.py#L1322)), guarded so it only
affects the renamed-alias case and does not change resolution for already-working columns
(spaces-in-name, non-view SELECTs).

### PR-3: E8 — intra-file temp-table-chain intermediate resolution

**Mirror the existing CTE-as-intermediate handling.** CTE bodies are already added to
`combined_sources` ([`base.py:1017-1039`](../../src/sqlcg/parsers/base.py#L1017)) so outer
columns resolve through CTE names, and CTEs are emitted as intermediate `role="cte"`
destinations ([`base.py:1471-1495`](../../src/sqlcg/parsers/base.py#L1471)). PR-3 does the
analogous thing for intra-file `CREATE TEMP TABLE x AS SELECT …`:

- Harvest the `CREATE TEMP TABLE`/`CREATE TEMPORARY TABLE … AS SELECT` bodies from the file
  (the same pass that already populates `_current_file_temp_keys` for namespacing — grep
  `_current_file_temp_keys` for the existing harvest site and reuse it; do NOT add a second
  scan of the file).
- When a downstream write's lineage root resolves to a temp-table name registered in-file,
  feed that temp's SELECT body as an intermediate source so the chain resolves to the temp's
  own leaf sources — exactly as `combined_sources` does for CTEs.
- Respect the existing temp **namespacing** (v1.21.x): the intermediate node must use the
  per-file namespaced key (`<rel_path>::<db>.<name>`, `role="temp"`), never a bare key — the
  dual-write footgun fixed in v1.21.1 must not be reopened. Grep `_temp_identity` /
  `namespace` for the convention.

**Statement ordering:** a temp must be CREATEd before it is READ in the same file (the
v1.21.2 acceptance W1 case). The harvest is file-level so this is satisfied for the common
case; document any backward-reference as out of scope.

**Performance constraint (critical):** the temp bodies must be added to the **file-level**
`combined_sources` and filtered by `dependency_filter` in pass 2, exactly like CTE/CTAS
bodies — NOT the full corpus (CLAUDE.md invariant: `exp.expand()` with only file-level
sources, O(N_refs) not O(N_files × N_corpus_ctas)). The `body_scope`-once-per-statement and
`copy=False`/`trim_selects=False` invariants must hold unchanged.

---

## HARD per-PR gate — Performance invariants (PR-2 and PR-3)

PR-2 and PR-3 touch the `sg_lineage` / `_extract_column_lineage` / `base.py` hot path. **Every
one of these PRs MUST, as a merge gate:**

1. Keep **ALL** invariants in the CLAUDE.md *Performance invariants — DO NOT REMOVE OR
   SIMPLIFY* table intact. Specifically reverify after the change:
   - `qualify`/`build_scope` imported at module level.
   - `exp.expand()` called with **only file-level `sources`**, filtered by `dependency_filter`.
   - `body_scope` built **once per statement** before the column loop (with schema-free retry
     on failure), never lazily inside it.
   - Pure-literal skip intact.
   - When a scope is passed to `sg_lineage`: `sources=` NOT also passed; `copy=False` +
     `trim_selects=False` passed.
   - INSERT column-list path: `body.copy()` + strip-WITH once before the loop.
   - bulk/batch upsert paths untouched.
2. The four guard suites stay **GREEN and UNMODIFIED**:
   - [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)
   - [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py)
   - [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py)
   - [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py)

   A red here means the change regressed a measured perf class — do not raise the slack, do
   not mark "pre-existing". If a new once-per-statement op is added in PR-3 (temp-body
   harvest/expand), add a deterministic op-count counter for it to the scaling guard.

---

## Implementation Steps

### PR-1 (ships first — instrumentation): honest index-summary metric — v1.25.6 (minor)

**Step 1.1** — Aggregate the file-level `parse_failed` count into the index result.
- Files: [`indexer.py`](../../src/sqlcg/indexer/indexer.py) (~L715 `pass2_results` loop / the
  result dict at L729-747).
- First **grep the parsed objects** for an existing `parse_failed` / `parse_cause` field
  (PR 5 / sprint_postmortem_fixes persisted `skip_counts` and `parse_cause` on the File node).
  Reuse it; only call `dominant_cause()` per file if no field already carries the verdict.
- Add `degraded_files` (int) and a per-bucket breakdown (reuse `error_summary`) to the result
  dict. Document the exact denominator chosen in a code comment.
- Acceptance: result dict contains `degraded_files` equal to the number of files whose
  dominant cause is in `_DEGRADING`.

**Step 1.2** — Print a distinct degraded-files summary line.
- Files: [`index.py:417-444`](../../src/sqlcg/cli/commands/index.py#L417).
- Add a new line (do NOT replace the existing quality/timeout/failed line). Wording surfaces
  the degrading total + top buckets, e.g.
  `[yellow]N files degraded (parse_failed)[/yellow] — E5: a · E8: b · timeout: c`.
- Acceptance: on a fixture with ≥1 E5 file and ≥1 E8 file, the printed output contains the
  degraded-files line with the correct count; the existing "N failed" line is unchanged.

**Step 1.3** — Version bump 1.25.5 → **1.25.6** (minor — new user-visible summary line).
Update `pyproject.toml`, `src/sqlcg/__init__.py`, `uv lock`.

### PR-2: E5 fix — v1.25.7 (patch — added resolution, no new surface)

**Step 2.1** — Implement the quoted single-token view-alias resolution (Design PR-2).
- Files: [`base.py`](../../src/sqlcg/parsers/base.py) main column loop (~L1322-1469).
- Guard the new path so it only affects renamed view-output aliases; do not change resolution
  for columns that already resolve.
- Emitted `dst_col_name` MUST preserve the original quoted case.
- Acceptance: on the E5 fixture (below), `AFDELING.sql`-shape view produces a
  `COLUMN_LINEAGE` edge for `"Afdelingsnummer"` (case-preserved dst) and emits **zero**
  `col_lineage:Afdelingsnummer:Cannot find column` errors.

**Step 2.2** — Reverify all perf invariants + the four guard suites (per-PR gate above).

**Step 2.3** — Version bump 1.25.6 → **1.25.7**, `uv lock`.

### PR-3: E8 `dynamic_source` temp-chain fix — v1.25.8 (patch)

**Step 3.1** — Reuse the existing in-file temp harvest; feed temp bodies as file-level
intermediate sources.
- Files: [`base.py`](../../src/sqlcg/parsers/base.py) (`combined_sources` construction ~L1015,
  the temp-key harvest site — grep `_current_file_temp_keys`).
- Add temp `AS SELECT` bodies to `combined_sources` under their namespaced key, filtered by
  `dependency_filter` in pass 2. Do NOT scan the corpus.
- Acceptance: on the E8 fixture (below), the final `INSERT INTO target SELECT … FROM tmp_x`
  resolves through `tmp_x` to its leaf source table; the target gains incoming
  `COLUMN_LINEAGE`; the `col_lineage_skip:dynamic_source` count for that column drops to 0.

**Step 3.2** — Preserve temp namespacing (no bare keys, no dual-write regression). Grep
`_temp_identity` / `namespace`. Acceptance: the intermediate node is the namespaced
`<rel_path>::<db>.<name>` `role="temp"` node, and no bare `tmp_*` `kind='table'` node is
created for it.

**Step 3.3** — Add a deterministic op-count counter to
[`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py) for the temp-body
harvest/expand if it introduces a new once-per-statement op; reverify all invariants + four
guard suites.

**Step 3.4** — Version bump 1.25.7 → **1.25.8**, `uv lock`.

---

## Fixtures — MUST model the live config

A fix proven only on a synthetic fixture that does not reproduce the live shape is
**UNVERIFIED**, not PASS (the v1.21.0 dual-write shipped green on an idealized fixture).

- **E5 fixture** — real T&T view shape: `CREATE OR REPLACE VIEW <SCHEMA>."<QuotedName>" (
  "<single-token quoted alias>" COMMENT '...', "<name with spaces>" COMMENT '...' ) AS SELECT
  BAREWORD AS "<single-token quoted alias>", OTHER AS "<name with spaces>" FROM <db>.<table>;`
  The fixture MUST include **both** a single-token quoted alias (the failing case,
  `"Afdelingsnummer"`) and a spaces-containing quoted alias (the already-working case,
  `"Afdeling koppelcode"`), plus the `COMMENT` clauses, so the test pins that the fix repairs
  the single-token case without regressing the spaces case. Use the Snowflake dialect parser.
- **E8 fixture** — real `CREATE TEMP TABLE … AS SELECT` chain: `create temp table tmp_x as
  select col from <db>.<leaf>; ... INSERT INTO <db>.<target> SELECT col FROM tmp_x;` — a
  multi-statement file where the final write's only source is the temp. To also pin the
  honest non-goal, a second variant SHOULD include a `star` column alongside `dynamic_source`
  and assert the temp-chain column resolves while the star column still skips (PR-3 does not
  fix star).

---

## Test Strategy

> Name tests `test_<unit>_<scenario>_<expected>` and link this plan in the docstring. The
> behaviors below are what to verify — do not invent opaque case codes.

**PR-1**
- Unit: aggregating a result over a list of parsed files whose dominant causes are E5/E8/clean
  yields the correct `degraded_files` count and per-bucket breakdown.
- Unit/CLI: rendering the summary on a result with N degraded files prints the degraded-files
  line with count N and leaves the existing "failed"/"timed out" lines unchanged; a clean
  result prints no degraded line.
- Integration: `index_repo` on a fixture dir containing one E5-shape view and one E8-shape ETL
  file reports `degraded_files == 2` in the result dict.

**PR-2**
- Unit (Snowflake parser, no graph write): parsing the E5 view fixture emits a `COLUMN_LINEAGE`
  edge whose destination column is the original quoted single-token name, and emits **zero**
  `col_lineage:<name>:Cannot find column` errors for the single-token alias.
- Unit: the spaces-containing quoted alias still resolves (no regression).
- Integration: indexing the E5 fixture leaves the view's `parse_failed` cleared for the
  single-token columns; the four perf-guard suites pass unmodified.

**PR-3**
- Unit (Snowflake parser): parsing the temp-chain fixture produces a `COLUMN_LINEAGE` edge
  from the leaf source through the namespaced temp node to the final target; `dynamic_source`
  skip count for that column is 0.
- Unit: the namespaced `role="temp"` intermediate node exists; no bare `tmp_*` `kind='table'`
  node is created (v1.21.1 dual-write regression guard).
- Unit: in the mixed variant, the `star` column still emits `col_lineage_skip:star` (PR-3 does
  not over-claim).
- Perf: scaling-guard op-count counter for the temp-body harvest stays flat N→2N.

---

## Acceptance Criteria — CI-green is NOT done for lineage features

Each PR's live-DWH acceptance is delegated to a developer who runs a fresh DWH re-index and
reports `sqlcg gain` health (strict / scoped / catalogued) + wall-time vs the **3-minute
budget** (note: the v1.21.2 baseline already ran 6m58s due to the catalog-apply regression —
report wall-time and flag if the parser change moves it, but the catalog-apply regression is a
separate known issue, not a PR-2/PR-3 gate).

### PR-1
- [ ] `sqlcg index` output on the live DWH shows a degraded-files line reporting a count on the
      order of ~410 (not "1 failed"); the existing "N failed"/"N timed out" lines are unchanged.
- [ ] Unit + integration tests for the aggregation and rendering pass.
- [ ] All four perf-guard suites pass unmodified (PR-1 should not touch the hot path, but
      confirm).

### PR-2 (expected delta: strict edge health UP, islands FLAT)
- [ ] Before/after live-DWH `sqlcg gain`: **strict (column-level) edge health increases**
      (per-column lineage recovered across IA-SEMANTIC/IA-TABLEAU single-token columns).
- [ ] Before/after **island / WCC count is flat** (E5 does not create islands — if islands
      drop materially, the fix did something unexpected; investigate).
- [ ] `degraded_files` (from PR-1 metric) drops by ~211 (E5 view files flip off `parse_failed`).
- [ ] E5 error-bucket count on the DWH drops to ~6 (the 4 `htdyn_*` + any residual), not ~217.
- [ ] All four perf-guard suites GREEN and UNMODIFIED; all CLAUDE.md invariants intact.

### PR-3 (expected delta: islands DOWN ~89, strict UP modestly)
- [ ] Before/after live-DWH **component / island counts**: ~89 previously-isolated temp-chain
      target tables gain incoming `COLUMN_LINEAGE` (islands down on the order of ~89; partial
      recovery is acceptable and expected — report the actual number).
- [ ] Before/after `sqlcg gain`: strict edge health up modestly; scoped health not regressed.
- [ ] No bare `tmp_*` `kind='table'` node created by the chain fix (v1.21.1 regression guard).
- [ ] All four perf-guard suites GREEN and UNMODIFIED; scaling-guard op-counts flat N→2N; all
      CLAUDE.md invariants intact.
- [ ] PR body honestly states which E8 files did NOT fully clear (residual star/merge_branch)
      and that this is the documented non-goal.

---

## Risks and Mitigations

| Risk | PR | Mitigation |
|------|-----|-----------|
| **Hot-path perf regression** (the v1.0.0 / `4234e5d` class: an op flips O(N)→O(N²) or per-call cost balloons). | PR-2, PR-3 | Per-PR gate: all CLAUDE.md invariants intact + four guard suites GREEN/UNMODIFIED. PR-3 adds a deterministic op-count counter for the temp-body harvest. Prefer behavioural assertions (kwarg present, op once-per-statement) over volume counts. |
| **E5 fix changes resolution for already-working columns** (spaces-in-name, non-view SELECTs). | PR-2 | Fixture pins both single-token AND spaces aliases; guard the new path to the renamed-view-alias case only. |
| **Emitting the uppercased column name** instead of the declared quoted case → graph column-identity mismatch. | PR-2 | Acceptance asserts the emitted `dst_col_name` is the original quoted case. |
| **Reopening the temp dual-write footgun** (bare `tmp_*` `kind='table'` node, fixed v1.21.1). | PR-3 | Intermediate node must use the namespaced `<rel_path>::<db>.<name>` `role="temp"` key; test asserts no bare node created. |
| **Scanning the corpus for temp bodies** instead of file-level sources (O(N_files × N_corpus)). | PR-3 | Temp bodies go into file-level `combined_sources`, filtered by `dependency_filter` in pass 2 — exactly like CTE/CTAS bodies. Scaling guard pins it. |
| **Over-claiming E8 recovery** — many E8 files won't flip clean (residual star/merge_branch). | PR-3 | Non-goal stated explicitly; acceptance reports actual island delta, not "all 184". |
| **Parse-time schema feeding creeping back in** to fix E5/E8. | PR-2, PR-3 | Non-goal: no `schema_sources` into `expand()`/`qualify()`. Catalog-level only. |

---

## Rollout / Rollback

- Three independent PRs, merged in order PR-1 → PR-2 → PR-3. Each is independently revertable.
- No schema-version change in any PR (PR-1 reads existing `parse_failed`; PR-2/PR-3 change
  edge emission, not the graph schema). Re-index is the migration path (no compat shim).
- Tagging and issue-closing are user-gated (house rule) — not done by the agent pipeline.
