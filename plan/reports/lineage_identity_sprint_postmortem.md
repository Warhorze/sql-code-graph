# Postmortem: Lineage Identity & Session Context Sprint (2026-06-11)

Plan: [`sprint_lineage_identity_and_session_context.md`](../sprints/sprint_lineage_identity_and_session_context.md).
Executed in one day as 4 PRs, all merged to master. **Tags v1.15.0–v1.18.0 ON HOLD
pending user verification of this postmortem.**

| PR | # | Shipped | Version |
|---|---|---|---|
| PR 4 (first) | #80 | §G identity counters: CTE key collisions, rescuable unqualified edges | 1.15.0 |
| PR 1 | #81 | `normalize_keys` alias choke point + empty-identity guard (stage edges dropped, `col_lineage_skip:stage`) | 1.16.0 |
| PR 2 | #82 | USE SCHEMA scripting-path context + LIKE catalog inheritance | 1.17.0 |
| PR 3 | #83 | Per-file `::` CTE/derived key namespacing | 1.18.0 |

## Why this postmortem exists

The post-sprint fresh DWH index (v1.18.0, sha `d015f8c`, 1,379 files) appeared to
show large regressions vs the canonical v1.14.2 baseline
([`v1_14_dialect_query_config_postmortem.md`](v1_14_dialect_query_config_postmortem.md),
memory `project_coverage_actual_baseline`):

| §G metric | v1.14.2 baseline | v1.18.0 observed | Sprint projection |
|---|---|---|---|
| Strict edge health | 82.7% | **73%** | ~85.5% |
| Scoped edge health | 97% | **88%** | ≥ 95.8% |
| Tables with catalog | 89% | **68%** | — |
| Total SqlTable | 6,388 | **2,814** | — |
| Phantom contradicted | 242 (0.5%) | **3,074 (6.4%)** | — |
| Total edges | 49,466 | 47,974 | delta documented, not gated |
| CTE key collisions | 218 | **3** ✅ | 0 |
| Rescuable unqualified | 828 | **0** ✅ | ≤ 50 |

## TL;DR — root causes

1. **The dominant cause is operational, not sprint code: the v1.18.0 graph was
   built without `sqlcg catalog load`.** The baseline included the
   INFORMATION_SCHEMA CSV (4,862 tables / ~71k rows); the current graph has
   **zero** `HAS_COLUMN` rows with `source='information_schema'` (measured).
   Verified by a read-only simulation that unions `columns.csv` into the
   catalog check and recomputes §G on the live graph: scoped 88% → **97.2%**,
   contradicted 3,074 → **242 (0.50%)**, catalogued 68% → **88%** — all back at
   baseline. The graph is fine; the ruler was missing half its markings.
2. **Why the catalog was missing**: the user's `~/.sqlcg/graph.db` was wiped at
   ~11:30 by an unisolated unit test routing through the live MCP server
   (Finding 5), and the 15:09 rebuild didn't re-apply the catalog because
   `dwh/.sqlcg.toml` has **no `[sqlcg.catalog]` section** — the v1.11.0
   auto-reapply hook (`_reapply_catalog_if_configured`,
   [`indexer.py`](../../src/sqlcg/indexer/indexer.py)) was never configured, so
   catalog durability has silently depended on a manual step all along.
3. **The sprint itself shipped correctly.** Both sprint counters hit target
   (828→0 rescuable, 218→3 collisions, and the 3 residuals are a counter
   artifact, not a namespacing bug — Finding 3). Four genuine residual findings
   are filed below (absolute-path namespaces, `<unknown>` self-edges,
   derived-kind artifact, stage-skip not persisted).
4. **A pre-existing parser crash (PR #70 / v1.12.0, NOT this sprint) silently
   drops 67 files** — `'str' object has no attribute 'args'` in
   `_qualify_bare_tables` (Finding 4). Cost: ~411 statements / ~3,563 edges /
   ~105 target tables absent from the graph, and the files are mislabeled
   `parse_failed=False`.

## Finding 1 — SqlTable 6,388 → 2,814 and catalog 89% → 68%

Measured on the live graph (read-only):

- `HAS_COLUMN` by source: ddl 18,771 rows / 1,063 tables; star_expansion 11,733 / 341;
  usage 10,228 / 703; clone_inherited 296 / 23; **information_schema 0 / 0**.
- Current SqlTable by kind: table 2,233 · cte 578 · derived 3. All 578 cte rows
  carry `::` namespaced keys and exactly cover every namespaced key appearing in
  COLUMN_LINEAGE (src ∪ dst = 578) — **no missing CTE nodes**.
- `sqlcg catalog load` emits one SqlTable row per CSV table (`kind='table'`,
  INSERT OR IGNORE). The CSV contributes 4,373 tables not otherwise in the graph;
  2,814 + 4,373 = 7,187 ≈ the baseline's 6,388–7,217 band (corpus drift accounts
  for the rest). The "collapse" is the absent load, not lost nodes.

The post-PR3 `_harvest_usage_catalog` guard works as the plan (W2) required:
kind-guard first, explicit `'::'` skip — zero `::` keys in HAS_COLUMN (measured).

## Finding 2 — strict 73% / scoped 88% decomposition

The 12,902 strict-bad edges split exactly into:

| category | edges | mechanism |
|---|---|---|
| namespaced `::` CTE/derived dst | 8,218 | by design — CTE columns are never catalogable; **all 8,218 are correctly scoped-excluded** (8,218 + 41 derived = 8,259 = 47,974 − 39,715 ✓) |
| `<unknown>` sentinel | 500 | pre-existing parser leak: self-edges (`src_key == dst_key`) emitted by CREATE_VIEW DDL files (top: `ddl/changelogs/IA-SEMANTIC/ODS_WORKAROUND_ARTIKELDIMENSIE_CONCEPT.sql` 109, `ddl/semtex_views.sql` 86) — not sprint-related, not stage-related |
| schema-qualified, uncatalogued column | 4,184 | 95% are tables only partially catalogued by usage harvest (e.g. `da.htdyn_vendvendor`: 14 usage columns vs 575 INSERT dst columns) — exactly the set the information_schema CSV covers |

With the catalog simulated in: **strict 80.6%, scoped 97.2%**. The scoped KPI
(the honest denominator) shows **no regression**. Strict's residual gap vs the
82.7% baseline is mechanical: PR 3 un-merged previously-collided CTE edges
(all strict-bad by design, inflating the denominator) and the 67 crashed files'
~3.5k mostly-good edges are absent from the numerator. The plan's ~85.5%
projection assumed both a catalogued graph and that the F1/F2 flipped edges add
to the numerator; many instead dedup-merged into existing good edges (total
edges fell 1,492, of which −311 are the stage-edge drop; the remainder is
re-key merge minus CTE un-merge, within graph-provenance noise).

The exact equality strict-good == scoped-good (35,072) is expected, not a bug:
no CTE/derived column can be strict-good post-PR3 (the baseline had the same
equality at 38,508).

Phantom contradicted 3,074: 95% (2,926) have usage-only-catalogued dst tables.
A usage catalog proves the columns it saw, not the columns that exist — absence
is not disproof. With the info-schema catalog simulated, contradicted returns
to exactly the baseline **242**. (Metric-semantics caveat worth a doc note:
"contradicted" is only meaningful against ddl/info-schema-sourced catalogs.)

## Finding 3 — the 3 residual "CTE collisions" are a counter artifact

All 3 colliding keys are columns of **one physical table**,
`ba.wtfv_cyclische_telling` (fed by `etl/sql/fact/wtfv_cyclische_telling.sql`
and `..._deleted_items.sql`). The table's DDL lives in Liquibase XML (not
indexed), so the pre-existing #45.2 path marks it `kind='derived'` — and
`_Q_CTE_COLLISIONS` counts any multi-file writer of a cte/derived-kinded key as
a collision. **No un-namespaced CTE key exists anywhere in the graph
(measured).** PR 3's acceptance (zero cross-file CTE identity collisions) is
genuinely met; the same mis-kinding also wrongly scoped-excludes 41 edges into
this table and `ba.wtfi_promotie_afzet`.

## Finding 4 — 67 files crash with `'str' object has no attribute 'args'` (pre-existing)

- **Cause**: [`snowflake_parser.py:375`](../../src/sqlcg/parsers/snowflake_parser.py)
  `_qualify_bare_tables` iterates `(stmt, stmt.expression)`; for
  `sqlglot.exp.Command` (unsupported-syntax fallback) `.expression` is a `str`,
  and `.args` raises. Trigger pattern (all 63 reproduced files): `USE SCHEMA x;`
  followed by any Command-fallback statement — dominated by
  `ALTER EXTERNAL TABLE … REFRESH` (58/63), plus `CALL`, `ALTER WAREHOUSE`,
  `PUT`, `REMOVE`.
- **Introduced**: commit `c9ab5ad` (PR #70 salvage, v1.12.0, 2026-06-09) —
  **before this sprint**; v1.14.2 reproduces identically, so the canonical
  baseline already silently carried these failures.
- **Cost (measured)**: one bad statement nukes the whole file — 0 SqlQuery rows
  for all 63 files; re-parsing with the bug bypassed yields 411 statements,
  ~3,563 edges, 109 target tables (105 absent from SqlTable today). Worse, the
  File rows show `parse_failed=False` with empty cause — the graph reports them
  healthy.

## Finding 5 — the graph.db wipe (~11:30, 2026-06-11)

- **Mechanism** (unique code-path match for the signature "all data tables 0
  rows, SchemaVersion survives"): the MCP server drain op `index` runs
  `clear_all_tables()` ([`duckdb_backend.py:799`](../../src/sqlcg/core/duckdb_backend.py),
  which deliberately spares SchemaVersion) then `index_repo(root)`
  ([`writer.py:363`](../../src/sqlcg/server/writer.py)).
- **Trigger (high confidence)**:
  `tests/unit/test_pr07_observability.py::test_scenario_c_log_file_written_to_configured_path`
  invokes `sqlcg index <empty tmp dir>` mocking the indexer — but
  `_try_route_index_via_server()` runs **before** any mock and resolves the
  socket from the real unpatched `get_db_path()`. With the user's live MCP
  server up (as it was during the sprint's PR test runs), the request bypasses
  every mock, the real server wipes and "re-indexes" an **empty directory**,
  zero files are written, and the transaction commits. The test passes; the
  wipe is silent. It is the only index-invoking test with neither
  `SQLCG_DB_PATH` isolation nor a route-helper patch (`test_index_flags.py`
  documents and defends against exactly this hazard). The test is
  long-standing — the sprint merely ran it while a server was live.
- **Aggravator**: the server-side drain happily commits an empty graph when the
  root yields zero SQL files — no guard.
- **Second confirmed hole (same class)**:
  `tests/unit/test_uninstall.py::test_uninstall_cmd_with_force_flag` deletes the
  **real** `~/.sqlcg/metrics.db` via unpatched `Path.home()`
  ([`uninstall.py:123`](../../src/sqlcg/cli/commands/uninstall.py)) — confirmed:
  `metrics.db` is missing from `~/.sqlcg/` with orphaned `-shm`/`-wal` files.
- **Why the rebuild lost the catalog**: `_reapply_catalog_if_configured` reads
  `[sqlcg.catalog] path` from `.sqlcg.toml`; the dwh repo's `.sqlcg.toml` never
  got that section, so the v1.11.0 durability hook is inert and catalog
  presence has depended on remembering a manual `sqlcg catalog load` after
  every full index.

## Sprint acceptance scorecard

| Criterion | Result |
|---|---|
| §G counters reproduce 218 / 828 baselines | ✅ (PR #80) |
| `*_tmp` strict-bad with catalogued base: 597 → 0 | ✅ (rescuable counter 0; zero un-aliased `*_tmp` divergence found) |
| Leading-dot/empty keys: 311 → 0, empty SqlTable row gone | ✅ — developer chose the **drop** path (0 `stage://` keys); deviation: `col_lineage_skip:stage` reasons are **not persisted** anywhere queryable (File stores only the dominant parse_cause), so the N2 accounting (converted vs dropped) is only derivable from logs |
| Rescuable unqualified ≤ 50; doorbelasting out of top-10 | ✅ (0; gone) |
| CTE collisions = 0; single-file traces | ✅ in substance — residual 3 is the derived-kind counter artifact (Finding 3), not a collision |
| Strict ≥ 85%, scoped ≥ 95.8% | ❌ as observed (73/88) / **✅ for scoped (97.2%) once the catalog is restored**; strict lands ~80.6% — the projection over-credited F1/F2 numerator gains that instead dedup-merged, and didn't model the CTE un-merge denominator growth. Strict ≥ 85% needs the Finding-4 fix + XML-DDL catalog ingestion, not more key work |
| Perf invariants green, wall-time in band | ✅ (107.6 s full corpus; band 210–256 s refers to a different measurement config — no regression signal) |
| Deviation: CTE namespace uses **absolute paths** (`/home/ignwrad/Projects/dwh/…sql::cte`), plan said repo-relative | ❌ portability bug — graph keys are machine-dependent; fix below |

## Recommended fix plan (candidate next sprint, priority order)

1. **P0 — test-isolation hardening** (Finding 5): top-level `tests/conftest.py`
   autouse fixture pinning `SQLCG_DB_PATH` (and log path) to `tmp_path` for every
   test; targeted fixes to `test_scenario_c` (patch `_try_route_index_via_server`)
   and the uninstall tests (patch `Path.home()`); server-side guard — drain
   `op=index` must roll back instead of committing when the root yields zero
   files. This is a data-loss class; nothing else is safe to verify until it
   lands.
2. **P0 — operational durability**: add `[sqlcg.catalog] path = ".../columns.csv"`
   to the dwh `.sqlcg.toml` (user action — repo is hands-off for agents) so the
   auto-reapply hook actually fires; consider a §G warning line when
   `information_schema` source count is 0 but a catalog path is configured/known.
3. **P1 — `_qualify_bare_tables` crash** (Finding 4): skip non-Expression
   candidates / `exp.Command` nodes; plus fix the `parse_failed=False` mislabel
   for worker-level failures. Recovers ~3.5k edges / 105 tables across 67 files.
4. **P1 — repo-relative CTE namespaces**: key as `<repo-relative-path>::<cte>`
   per plan; re-index is the migration.
5. **P2 — `<unknown>` sentinel self-edges** (500): stop emitting edges when the
   table ref resolves to the sentinel (same "honest drop" principle as the stage
   guard).
6. **P2 — derived-kind artifact**: catalog-aware kind upgrade (or XML-DDL
   ingestion, the standing `project_blindspot_xml_ddl_lever` item) so
   multi-file physical targets like `ba.wtfv_cyclische_telling` stop polluting
   the collision counter and scoped exclusion (41 edges).
7. **P3 — persist skip reasons**: store `col_lineage_skip:*` entries queryably
   so §G accounting doesn't depend on log archaeology.

## Key lessons

1. **A metric regression on a fresh graph is a provenance question first.** Every
   headline drop reproduced the missing-catalog signature; an hour of mechanism
   hunting in sprint code would have been wasted without first asking "was this
   graph built the same way as the baseline?" The §G fingerprint line helped;
   a catalog-source line (`HAS_COLUMN` rows by source) would have made it
   instant.
2. **Durability hooks that are never configured are not durability.** The
   v1.11.0 auto-reapply existed precisely for this scenario and sat inert for
   lack of one TOML section.
3. **Mocked CLI tests are not isolated if routing happens before the mocks.**
   The socket-routing hazard was already documented in one test file and still
   bit through another.
4. **Counter semantics need their catalog assumptions stated.** "Contradicted"
   and "CTE collision" both over-report against a usage-only catalog /
   mis-kinded tables.

## Operator runbook (restore baseline-comparable numbers on the live graph)

```
sqlcg catalog load columns.csv   # immediate fix for the current graph
sqlcg gain                       # expect ~80.6% strict / ~97.2% scoped / ~88% catalogued
# durable: add [sqlcg.catalog] path = "<abs path>/columns.csv" to dwh/.sqlcg.toml
```

Tags pending at close: v1.15.0–v1.18.0 — user tags after verifying this
postmortem and the restored metrics, per house rule.
