# Sprint Plan: Lineage-Identity Postmortem Fixes

**Status: DRAFT** — gates with `plan-reviewer` before any implementation.

Source of truth: [`lineage_identity_sprint_postmortem.md`](../reports/lineage_identity_sprint_postmortem.md)
(Findings 1–5 + "Recommended fix plan"). Honours the performance-invariant table and
the release/versioning rules in [`CLAUDE.md`](../../CLAUDE.md) and the catalog-enrichment
design rule in [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md) §3.2b.

## Summary

The post-sprint metric "regression" was an **operational** artifact (the v1.18.0 DWH graph
was built without `sqlcg catalog load`, and the catalog was lost when an unisolated unit
test routed `index` through the live MCP server and wiped `~/.sqlcg/graph.db`). The sprint
code shipped correctly. This sprint hardens against the data-loss class (P0), recovers
~3.5k edges lost to a pre-existing parser crash (P1), makes graph keys portable (P1), and
cleans up four residual honesty/accounting issues (P2/P3). No new lineage *features* — every
change either prevents data loss, recovers already-parseable edges, or makes existing
counters honest.

## Scope

### In Scope
- Test-isolation hardening so no test can touch the user's real `~/.sqlcg/` (Finding 5).
- A server-side guard that rolls back an `op=index` drain when the root yields zero SQL
  files (Finding 5 aggravator).
- A `gain`/§G operator warning when the graph has zero `information_schema`-sourced
  `HAS_COLUMN` rows (Finding 1 / lesson 1).
- Fix the `_qualify_bare_tables` `'str' object has no attribute 'args'` crash and the
  `parse_failed=False` mislabel for worker-level failures (Finding 4).
- Re-key per-file CTE/derived namespaces to repo-relative paths (Finding 4 deviation /
  recommended fix 4).
- Stop emitting `<unknown>`-sentinel self-edges (Finding 2, 500 edges).
- Catalog-aware kind upgrade so info-schema-catalogued targets stop being mis-kinded
  `derived` (Finding 3, 41 edges + 3 phantom collisions).
- Persist `col_lineage_skip:*` reasons queryably (Finding 5 deviation note / recommended
  fix 7).

### Non-Goals
- **No change to `dwh/.sqlcg.toml`** — the dwh repo at `/home/ignwrad/Projects/dwh` is
  hands-off for agents. Adding `[sqlcg.catalog] path` there is a USER action (see
  §User actions).
- **No full Liquibase XML-DDL ingestion.** That is the standing
  `project_blindspot_xml_ddl_lever` item, a separate sprint. P2 ships only the
  catalog-aware *kind upgrade*, not XML parsing.
- **No new lineage extraction surface, no parse-time schema feeding** (forbidden by
  CLAUDE.md / ARCHITECTURE_REVIEW.md §3.2b — `schema_sources` into `exp.expand()` /
  `qualify()` stays out).
- No re-baselining of the strict ≥85% milestone (the postmortem shows that needs the
  Finding-4 fix + XML-DDL ingestion, tracked under F7 — out of scope here).
- No backward-compat shims for re-keyed namespaces; **re-index is the migration path.**

## Design

### Performance-invariant guardrails (apply to every PR)
- PR 3 (`_qualify_bare_tables`) and PR 4 (`<unknown>` skip, namespace plumbing) touch
  [`base.py`](../../src/sqlcg/parsers/base.py) / the parsers. Every change must keep the
  hot-path invariants intact:
  - `qualify`/`build_scope` stay module-level imports.
  - `body_scope` stays built once per statement before the column loop.
  - The pure-literal skip, `copy=False`/`trim_selects=False`, and the once-per-statement
    INSERT-body copy stay.
- **The perf guard suites must pass UNMODIFIED**:
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
  [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py).
  A red here means an invariant broke — do not raise the slack.
- Every new method needs a **grep-confirmed call site** before the PR opens.
- Tests assert **observable output**, never "no exception raised".

### Data models / constants
- `SQLCG_DB_PATH` / `SQLCG_LOG_PATH` are the env overrides resolved by
  [`DbConfig.from_env()`](../../src/sqlcg/core/config.py); the autouse fixture sets these,
  never hardcodes a path. Defaults to spoof: `~/.sqlcg/graph.db`, `~/.sqlcg/index.log`,
  `~/.sqlcg/metrics.db`.
- CTE/derived namespace key format: the `::`-prefixed key built by `TableRef.full_id`
  ([`base.py:94`](../../src/sqlcg/parsers/base.py)) must consume a **repo-relative**
  namespace string (`etl/sql/fact/wtfa.sql::final`), not an absolute one
  (`/home/ignwrad/Projects/dwh/...sql::final`).
- New skip-reason persistence: a per-file count structure persisted on the `File` node
  (exact shape decided in PR 6 design step — must be queryable via `run_read`, not log-only).

---

## PR sequence

| PR | Title | Priority | Version | Bump rationale |
|----|-------|----------|---------|----------------|
| 1 | Test-isolation hardening + empty-index rollback guard | P0 | **1.18.1** | fix-only (no user surface) |
| 2 | `_qualify_bare_tables` crash fix + worker-failure labelling | P1 | **1.18.2** | fix-only |
| 3 | Repo-relative CTE/derived namespaces | P1 | **1.18.3** | fix-only (re-index migration; keys are internal) |
| 4 | `<unknown>` self-edge drop + catalog-aware kind upgrade + §G catalog warning | P2 | **1.19.0** | new surface (gain warning line, persisted-skip table groundwork) |
| 5 | Persist `col_lineage_skip:*` reasons queryably | P3 | **1.19.1** | fix-only (or fold into PR 4 — see note) |

> **Sequencing rule (Finding 5 lesson)**: PR 1 is **standalone and ships first**. It is a
> data-loss class — nothing else is safe to verify on the live graph until it lands and the
> user has re-indexed with the catalog. PRs 2–5 may proceed once PR 1 is merged.
>
> **PR 4/5 split note**: the §G catalog warning (PR 4) and persisted skip reasons (PR 5)
> are independent. PR 5 may fold into PR 4 if the persistence shape is trivial; keep them
> separate if PR 4 grows large. The developer decides at implementation time — do not fold
> already-reviewed work (house rule).

---

## PR 1 — Test-isolation hardening + empty-index rollback guard (P0, v1.18.1)

### Files affected
- **NEW** [`tests/conftest.py`](../../tests/conftest.py) — top-level autouse fixture.
- [`tests/unit/test_pr07_observability.py`](../../tests/unit/test_pr07_observability.py)
  — `test_scenario_c_log_file_written_to_configured_path`.
- [`tests/unit/test_uninstall.py`](../../tests/unit/test_uninstall.py)
  — `test_uninstall_cmd_with_force_flag` + siblings.
- [`src/sqlcg/server/writer.py`](../../src/sqlcg/server/writer.py) `_do_index` (~line 356).
- [`src/sqlcg/cli/commands/uninstall.py`](../../src/sqlcg/cli/commands/uninstall.py) — only
  if the metrics path needs threading through config (see Step 1.3 decision).

### Step 1.1 — autouse `tmp_path` DB/log isolation fixture
Add a top-level `tests/conftest.py` with an **autouse** fixture (function-scoped) that, for
every test, sets `SQLCG_DB_PATH`, `SQLCG_LOG_PATH` to paths under that test's `tmp_path`
via `monkeypatch.setenv`. This pins `DbConfig.from_env()` to a throwaway location.
- Files affected: new `tests/conftest.py`.
- Acceptance: with the fixture active, a test that calls `get_db_path()` /
  `DbConfig.from_env()` resolves under `tmp_path`, never under `Path.home()`. Verified by a
  meta-test in the new conftest's own test module asserting `get_db_path()` is under the
  test's tmp dir.
- Note: this is a **safety net**, not a substitute for the targeted fixes below — the
  scenario-C wipe happened *before* any DB-path mock because routing resolved the socket
  from the real path. The autouse env var does not by itself defeat routing (the live
  server's socket is keyed off the path the *server* opened with). Hence Step 1.2.

### Step 1.2 — `test_scenario_c` route-helper patch
Patch `sqlcg.cli.commands.index._try_route_index_via_server` to `return_value=False` inside
`test_scenario_c_log_file_written_to_configured_path`, mirroring the documented defence in
[`test_index_flags.py`](../../tests/unit/test_index_flags.py) (lines 42–45, 105–108). Also
change its `get_db_path` patch away from `Path.home() / ".sqlcg" / "graph.db"` to a
`tmp_path`-based value so even the routing-resolution step cannot reach the real socket.
- Files affected: `test_pr07_observability.py`.
- Acceptance: the test forces the direct-write path; with a live MCP server present on the
  host it can no longer route the index through the socket. Assert observable: the patched
  `_try_route_index_via_server` is called and the indexer mock (not a real server) handles
  the call. The test must still verify its original behaviour (clean exit, configured log
  path) — do not weaken its assertions.

### Step 1.3 — uninstall tests patch `Path.home()`
`test_uninstall_cmd_with_force_flag` (and any sibling exercising the force path) must patch
`Path.home()` (or the `metrics_path` resolution) so the **real** `~/.sqlcg/metrics.db` is
never touched. The production code at
[`uninstall.py:124`](../../src/sqlcg/cli/commands/uninstall.py) resolves
`Path.home() / ".sqlcg" / "metrics.db"` directly.
- **Design decision for the developer**: prefer patching `sqlcg.cli.commands.uninstall.Path.home`
  in the test (minimal, no production change). If a sibling test cannot be isolated by
  patching alone, route the metrics path through config instead of a bare `Path.home()` —
  but only if needed; the fallback must still match `DbConfig`'s `~/.sqlcg` convention
  (CLAUDE.md: "Path/constant fallbacks must match `KuzuConfig`").
- Acceptance: running the full uninstall test module leaves a real `~/.sqlcg/metrics.db`
  untouched. Verified by a test that creates a sentinel file at the patched home's
  `metrics.db`, runs the force path, and asserts only the *patched* (tmp) metrics file was
  removed — the real home is never referenced (assert the patch target was called).

### Step 1.4 — server-side empty-index rollback guard
In [`writer.py`](../../src/sqlcg/server/writer.py) `_do_index` (~line 356–370): the drain
runs `clear_all_tables()` then `index_repo(root)` inside one `transaction()`. Add a guard:
if `index_repo` reports **zero SQL files found** (the walker yielded nothing), **raise**
inside the transaction so it rolls back instead of committing an empty graph over a
populated one. `index_repo` returns a summary dict with `files_parsed`
([`indexer.py`](../../src/sqlcg/indexer/indexer.py) `total_files = len(files)`); the guard
keys off the file count, not `files_parsed` (a file can parse to zero rows legitimately —
the wipe signature is *zero files walked*).
- **Decision**: surface the count the guard needs. If the summary already exposes the
  walked-file count, use it; otherwise add a `files_found` key to the `index_repo` summary
  (grep-confirm the new key is read in `_do_index`). The raise must produce a clear error to
  the waiters ("refusing to index empty root — graph preserved") and leave the prior graph
  intact via rollback.
- Files affected: `writer.py`; possibly `indexer.py` (summary key only).
- Acceptance (measurable): indexing an empty directory through the MCP drain leaves an
  existing populated graph **unchanged** (row counts in `SqlTable`/`COLUMN_LINEAGE` before
  == after); the waiter receives an error. Counterpart: indexing a non-empty directory
  still commits normally.

### Test plan (PR 1)
- Unit: meta-test for the autouse fixture (db path under tmp).
- Unit: `test_scenario_c` asserts the route helper is patched/called and the real socket is
  never reached.
- Unit: uninstall force-path test asserts real `~/.sqlcg/metrics.db` is untouched.
- Integration (DuckDB in-memory or tmp file): populate a graph, run an `op=index` drain on
  an empty dir, assert the graph row counts are preserved and an error reaches the waiter.
- Integration: same drain on a populated dir commits normally (regression guard for the
  rollback).
- Full suite green; perf guards unmodified.

### Acceptance criteria (PR 1)
- [ ] A top-level autouse fixture pins `SQLCG_DB_PATH` + `SQLCG_LOG_PATH` under `tmp_path`
      for every test (meta-test proves it).
- [ ] `test_scenario_c` patches `_try_route_index_via_server` and uses a tmp DB path; with a
      live server it can no longer wipe the real graph.
- [ ] Uninstall force-path tests never touch the real `~/.sqlcg/metrics.db`.
- [ ] An `op=index` drain on a zero-SQL-file root rolls back; an existing populated graph is
      byte-for-byte preserved (row counts equal); waiters get an error.
- [ ] A non-empty `op=index` drain still commits.
- [ ] All four perf-guard suites pass unmodified.

---

## PR 2 — `_qualify_bare_tables` crash fix + worker-failure labelling (P1, v1.18.2)

### Files affected
- [`src/sqlcg/parsers/snowflake_parser.py`](../../src/sqlcg/parsers/snowflake_parser.py)
  `_qualify_bare_tables` (lines 358–392; the `.args` access at line 375).
- The worker-failure labelling path: [`pool.py`](../../src/sqlcg/indexer/pool.py) /
  [`error_classify.py`](../../src/sqlcg/indexer/error_classify.py) /
  [`base.py`](../../src/sqlcg/parsers/base.py) `_classify_error` site (developer locates the
  exact File-row `parse_failed` assignment).

### Step 2.1 — skip non-Expression / Command candidates
At [`snowflake_parser.py:372`](../../src/sqlcg/parsers/snowflake_parser.py) the loop
iterates `(stmt, getattr(stmt, "expression", None))` and calls `candidate.args.get(...)`.
For `sqlglot.exp.Command` fallback nodes, `.expression` is a `str`, and `str.args` raises
`'str' object has no attribute 'args'` — which propagates out and nukes **all** statements
in the file. Fix: skip any candidate that is not a `sqlglot.exp.Expression`
(`if not isinstance(candidate, exp.Expression): continue`). Also guard the outer
`stmt.find_all(exp.Table)` path so a `Command` `stmt` itself is handled (Command supports
`find_all`, but confirm it returns no `exp.Table` and does not raise).
- Acceptance (measurable, from Finding 4): the 67 previously-crashing files (trigger
  pattern `USE SCHEMA x;` followed by `ALTER EXTERNAL TABLE … REFRESH` / `CALL` /
  `ALTER WAREHOUSE` / `PUT` / `REMOVE`) parse without raising; re-indexing the DWH yields
  ~411 additional `SqlQuery` rows, ~3,563 additional edges, and ~105 previously-absent
  target tables. The number to *gate* on is a behavioural fixture (below), not the live DWH
  count.

### Step 2.2 — worker-level failures mark the File row failed with a cause
Finding 4: the crashed files showed `parse_failed=False` with empty cause — the graph
reported them healthy. A worker-level exception (caught in
[`pool.py:89`](../../src/sqlcg/indexer/pool.py) and sent back as an exception object) must
result in the corresponding `File` node being marked `parse_failed=True` with a non-empty
cause (e.g. `worker_error:<ExcType>`), routed through the existing
[`error_classify.py`](../../src/sqlcg/indexer/error_classify.py) taxonomy.
- **Decision for developer**: trace where a worker-returned exception currently lands. If it
  is silently dropped / produces an empty `ParsedFile`, ensure that path sets
  `parse_failed=True` + cause. Reuse the existing `dominant_cause` plumbing; add a new
  `worker_error` classification only if no existing bucket fits. Grep-confirm the new
  classification (if any) has a call site.
- Acceptance: a file that raises at worker level produces a `File` row with
  `parse_failed=True` and a non-empty `parse_cause` (observable via `run_read`), not a
  silent healthy row.

### Test plan (PR 2)
- Unit fixture reproducing the exact crash: a Snowflake SQL string with
  `USE SCHEMA da;` then `ALTER EXTERNAL TABLE da.x REFRESH;` — assert `parse_file` returns a
  `ParsedFile` with ≥1 `SqlQuery`/statement and **does not raise**. Pre-fix this fixture
  raises `'str' object has no attribute 'args'`.
- Unit: confirm `_qualify_bare_tables` is a no-op on a `Command` node (no raise, no
  qualification) and still qualifies bare tables on a normal `Select`/`Insert`.
- Integration: index a small corpus containing one crash-pattern file; assert the file's
  statements land in the graph (non-zero `SqlQuery` for that file) AND that a separately
  injected genuinely-broken file is marked `parse_failed=True` with a cause.
- Perf guards unmodified.

### Acceptance criteria (PR 2)
- [ ] The crash-pattern fixture parses without raising and yields ≥1 statement.
- [ ] `_qualify_bare_tables` skips `exp.Command`/non-Expression candidates (no `.args` on a
      `str`).
- [ ] A worker-level failure marks the `File` row `parse_failed=True` with a non-empty
      cause (observable).
- [ ] On the live DWH (operator verification, not CI-gated): the 67 files contribute their
      statements/edges back (~3.5k edges recovered) — recorded in the postmortem follow-up.
- [ ] Perf guards unmodified.

---

## PR 3 — Repo-relative CTE/derived namespaces (P1, v1.18.3)

### Files affected
- [`src/sqlcg/parsers/snowflake_parser.py`](../../src/sqlcg/parsers/snowflake_parser.py)
  `self._current_file_namespace = str(path)` at lines **445** and **545**.
- [`src/sqlcg/parsers/ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py)
  same assignment at lines **118** and **244**.
- [`src/sqlcg/indexer/indexer.py`](../../src/sqlcg/indexer/indexer.py) task-dict
  construction (p1 task line **406**, p2 task line **533**) — thread the repo root /
  relative path.
- [`src/sqlcg/indexer/pool.py`](../../src/sqlcg/indexer/pool.py) `_run_task` (line 105) and
  `parse_file` call sites (lines 109, 130) — pass the relative path through.

### Problem (Finding 4 deviation / postmortem line 180)
PR #83 keyed CTE/derived namespaces with the **absolute** path
(`self._current_file_namespace = str(path)`), so graph keys are machine-dependent
(`/home/ignwrad/Projects/dwh/...sql::cte`). The plan required `<repo-relative-path>::<cte>`.

### Step 3.1 — thread the repo root to the parser
The walker yields **absolute** paths ([`walker.py:45`](../../src/sqlcg/indexer/walker.py)
`root = Path(root).resolve()`), so the parser cannot relativize on its own. Two viable
designs — the developer picks one and documents the choice:
- **(A, preferred)** Compute the repo-relative path string in `index_repo`
  (`fp.relative_to(root_resolved)`) and pass it in the task dict as a `rel_path` key; the
  pool forwards it to `parse_file`, which sets `self._current_file_namespace = rel_path`.
  The on-disk `path` used everywhere else stays unchanged (the File node `path` is a
  separate concern — do not change it in this PR unless it is already relative; confirm
  first).
- **(B)** Pass the resolved `root` into `parse_file` and let the parser compute
  `path.relative_to(root)`.
- Use forward-slash normalization (`as_posix()`) so keys are stable across OSes.
- Acceptance: the namespace string is repo-relative (no leading `/home/...`), forward-slash
  normalized.

### Step 3.2 — re-index is the migration
No compat shim, no key rewrite. Document in the PR description that existing graphs must be
re-indexed (CLAUDE.md house rule).

### Test plan (PR 3)
- Unit: parse a CTE-bearing file given an absolute path under a known root; assert the
  emitted CTE/derived `TableRef.full_id` (and the resulting COLUMN_LINEAGE keys) are of the
  form `relative/path/file.sql::cte_name` with **no absolute prefix**.
- Unit: the same file parsed from two different absolute roots (same relative layout)
  produces **identical** namespaced keys (portability assertion).
- Integration: index a small corpus, query `COLUMN_LINEAGE` / `SqlTable`, assert no key
  contains an absolute path segment (`grep`-equivalent assertion: no `::` key starts with
  `/` or a drive letter).
- Perf guards unmodified (namespace string change must not alter call counts).

### Acceptance criteria (PR 3)
- [ ] CTE/derived namespace keys are `<repo-relative-posix-path>::<name>`.
- [ ] Identical relative layouts under different absolute roots yield identical keys.
- [ ] No `::` key in the graph contains an absolute-path prefix.
- [ ] Re-index documented as the migration; no compat shim added.
- [ ] Perf guards unmodified.

---

## PR 4 — `<unknown>` self-edge drop + catalog-aware kind upgrade + §G catalog warning (P2, v1.19.0)

### Files affected
- [`src/sqlcg/parsers/base.py`](../../src/sqlcg/parsers/base.py) lines **1326–1327** and
  **1406–1407** (the two `<unknown>` self-edge emission sites).
- [`src/sqlcg/indexer/indexer.py`](../../src/sqlcg/indexer/indexer.py) line **1476**
  (`target_kind = "derived"`) — catalog-aware upgrade.
- [`src/sqlcg/cli/coverage.py`](../../src/sqlcg/cli/coverage.py) `render_coverage_lines`
  (line **548**) + the `CoverageStats` model + the query layer (the
  `information_schema`-source count).
- [`src/sqlcg/cli/commands/gain.py`](../../src/sqlcg/cli/commands/gain.py) §G render (line
  **76–78**) inherits the new warning line via `render_coverage_lines`.

### Step 4.1 — stop emitting `<unknown>` sentinel self-edges
Finding 2: 500 strict-bad edges are self-edges where `src_key == dst_key` and the table ref
resolved to the `<unknown>` sentinel
([`base.py:1326-1327`](../../src/sqlcg/parsers/base.py),
[`1406-1407`](../../src/sqlcg/parsers/base.py)). Same "honest drop" principle as the PR #81
stage guard: when both endpoints resolve to `<unknown>` (or the table name is the
`<unknown>` sentinel), **skip the edge** and append a counted skip reason
(`col_lineage_skip:unknown_sentinel:<col>`), matching the existing
`col_lineage_skip:stage` / `col_lineage_skip:merge_branch` convention
([`base.py:918`](../../src/sqlcg/parsers/base.py)).
- **Hot-path note**: this is a cheap guard inside the existing per-column emission, not a new
  pass — must not add a per-column qualify/scope op. Keep it a string/identity check only.
- Acceptance (measurable): re-indexing the DWH yields **0** `COLUMN_LINEAGE` edges where
  `src_key == dst_key` and the table component is `<unknown>` (was 500). Top emitters from
  Finding 2 (`ddl/changelogs/IA-SEMANTIC/ODS_WORKAROUND_ARTIKELDIMENSIE_CONCEPT.sql` = 109,
  `ddl/semtex_views.sql` = 86) emit zero such edges.

### Step 4.2 — catalog-aware kind upgrade
Finding 3: tables like `ba.wtfv_cyclische_telling` whose DDL lives in un-indexed Liquibase
XML get `kind='derived'` at [`indexer.py:1476`](../../src/sqlcg/indexer/indexer.py), which
(1) pollutes `_Q_CTE_COLLISIONS` (counts multi-file writers of a derived-kinded key) and
(2) wrongly scoped-excludes 41 edges. Fix: when a key is catalogued via
`HAS_COLUMN(source='information_schema')`, it is a **physical table** — upgrade its kind to
`'table'` (do not leave it `'derived'`). This is a post-index reconciliation (the catalog is
loaded post-index per ARCHITECTURE_REVIEW §3.2b), so the upgrade belongs where the catalog
is applied / reconciled, NOT in the parse-time `target_kind` assignment.
- **Decision for developer**: the cleanest seam is the `catalog load` / `_reapply_catalog`
  path — after info-schema rows are upserted, run a single bulk `UPDATE "SqlTable" SET
  kind='table' WHERE kind='derived' AND qualified IN (<catalogued keys>)`. Confirm precedence
  (`ddl > information_schema > usage`) is not violated — only `derived`→`table`, never
  downgrading a `view`/`table`. Grep-confirm the call site.
- Acceptance (measurable): after `catalog load`, `ba.wtfv_cyclische_telling` and
  `ba.wtfi_promotie_afzet` have `kind='table'`; `_Q_CTE_COLLISIONS` returns **0** (was 3,
  the counter artifact); the 41 previously scoped-excluded edges into these tables become
  scoped-eligible.

### Step 4.3 — §G catalog-missing warning line
Finding 1 / lesson 1: add a `gain`/§G warning line when the graph has **zero**
`HAS_COLUMN` rows with `source='information_schema'`. The line is informational (not an
error) and should hint at the operator runbook (`sqlcg catalog load <csv>`). Add the
underlying count to `CoverageStats` (a `catalog_info_schema_tables` / `*_rows` field) and a
query for it; `render_coverage_lines` emits the warning when the count is 0.
- **Refinement (postmortem wording "but a catalog path is configured or catalog data was
  previously present")**: gating the warning on prior presence is hard to detect reliably
  on a fresh graph. Ship the simpler, always-correct rule: **warn whenever
  `information_schema`-sourced rows == 0**, since a DWH that needs the catalog always wants
  this reminder and a small repo with no catalog can ignore it. If the developer can cheaply
  detect "a catalog path is configured" (via `get_catalog_path` on the indexed repo root),
  prefer wording the warning more strongly in that case — but the count==0 trigger is the
  baseline requirement.
- Acceptance (measurable): on a graph with zero info-schema rows, `gain` §G prints a warning
  line containing the catalog-load hint; on a graph with ≥1 info-schema row, the line is
  absent.

### Test plan (PR 4)
- Unit (base.py): the two `<unknown>` self-edge sites no longer emit an edge when both
  endpoints are the sentinel; assert a `col_lineage_skip:unknown_sentinel:*` reason is
  appended instead. Use a focused fixture that previously produced a self-edge.
- Unit/integration (indexer + backend): seed a `derived`-kinded table, load an info-schema
  catalog row for it, run the reconcile; assert its `kind` flips to `'table'` and the
  collision query returns 0.
- Integration (coverage): build a graph with no info-schema rows → §G warning present; add
  an info-schema row → warning absent. Assert on the rendered line text (observable output).
- Behavioural perf assertion: the `<unknown>` skip adds no per-column qualify/scope op —
  perf guards unmodified.

### Acceptance criteria (PR 4)
- [ ] Zero `<unknown>` self-edges (`src_key == dst_key`, sentinel table) in a re-indexed
      graph; a counted skip reason is recorded instead.
- [ ] Info-schema-catalogued targets get `kind='table'`; `_Q_CTE_COLLISIONS` returns 0; the
      41 edges become scoped-eligible.
- [ ] `gain` §G prints a catalog-missing warning iff info-schema row count is 0.
- [ ] No precedence violation (never downgrade a `view`/`table`).
- [ ] Perf guards unmodified.

---

## PR 5 — Persist `col_lineage_skip:*` reasons queryably (P3, v1.19.1)

### Files affected
- [`src/sqlcg/parsers/base.py`](../../src/sqlcg/parsers/base.py) — where `col_lineage_skip:*`
  strings are appended to `ParsedFile.errors` (e.g. lines 918, plus the stage/unknown
  reasons).
- [`src/sqlcg/lineage/aggregator.py`](../../src/sqlcg/lineage/aggregator.py) `_build_file_rows`
  / [`indexer.py`](../../src/sqlcg/indexer/indexer.py) File-row build — persist the counts.
- [`src/sqlcg/core/duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py) — schema
  addition if a dedicated structure is chosen (bumps `SCHEMA_VERSION` in
  [`schema.py`](../../src/sqlcg/core/schema.py); re-index is the migration).

### Problem (postmortem sprint-scorecard deviation, line 175; recommended fix 7)
`col_lineage_skip:*` reasons (stage, merge_branch, unknown_sentinel — the last added in
PR 4) live only in logs. The `File` row stores only the *dominant* parse cause, so the §G /
N2 accounting (converted vs dropped) is derivable only from log archaeology.

### Step 5.1 — choose the persistence shape (design step, decide before coding)
Two options; the developer picks and documents:
- **(A)** A `skip_counts` JSON/map column on the `File` node (per-file `{reason: count}`).
  Simple, no new table, queryable via `run_read` JSON extraction. Bumps `SCHEMA_VERSION`.
- **(B)** A dedicated `LINEAGE_SKIP` node/edge table keyed by file + reason. More queryable
  but heavier; only if (A) is too coarse for §G needs.
- Prefer **(A)** unless §G needs per-(file,reason) joins that (A) cannot serve. Whatever is
  chosen must be populated from the existing `ParsedFile.errors` skip strings (no new parse
  pass) and queryable without reading logs.

### Step 5.2 — wire §G accounting to read it
The §G "converted vs dropped" accounting that previously needed logs should query the
persisted structure. (If §G does not currently render this, the persistence + a query is
sufficient for this sprint; rendering it in §G is optional polish — call it out in the PR.)

### Test plan (PR 5)
- Unit: a file that produces `col_lineage_skip:stage` and `col_lineage_skip:unknown_sentinel`
  yields a persisted `{reason: count}` structure with the right counts (observable via
  `run_read`).
- Integration: index a corpus, query the persisted skip structure, assert counts match the
  emitted skip reasons (not log scraping).
- Schema-gate test: re-open after the schema bump fails the gate unless re-indexed
  (existing schema-version behaviour).
- Perf guards unmodified.

### Acceptance criteria (PR 5)
- [ ] `col_lineage_skip:*` reasons are persisted per-file and queryable via `run_read`
      (not log-only).
- [ ] Counts match the emitted skip strings on a fixture.
- [ ] `SCHEMA_VERSION` bumped if a schema change is made; re-index is the migration.
- [ ] Perf guards unmodified.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Autouse fixture masks tests that *intend* to exercise default paths | Scope the fixture to env-var override only; the few e2e tests already set `SQLCG_DB_PATH` explicitly and will keep working (their explicit `monkeypatch.setenv` wins). Audit e2e tests that read the *default* home path. |
| Empty-index rollback guard breaks a legitimate "index an empty repo" case | Acceptable: an empty repo wiping a populated graph is never desired; the guard only fires on *zero files walked*, and the operator gets a clear error, not silent failure. A genuinely-empty first index (no prior graph) still works (nothing to preserve). |
| `_qualify_bare_tables` fix changes parse output for non-crash files | The fix only adds an `isinstance` skip; on non-Command nodes behaviour is identical. Unit test pins normal-statement qualification unchanged. |
| Repo-relative re-key invalidates existing graphs | Expected — re-index is the documented migration (no compat shim, house rule). |
| `<unknown>` skip drops a legitimate edge | Only self-edges (`src_key == dst_key`) on the sentinel are dropped — these carry no lineage information by definition. Skip reason is counted (PR 5 makes it auditable). |
| Kind upgrade downgrades a real `view` | Guard restricts the UPDATE to `kind='derived'` only; never touches `view`/`table`. |
| Perf regression from any parser/indexer touch | All four perf-guard suites must pass unmodified; behavioural assertions (kwarg present, op once-per-statement) enforced. |

---

## User actions (out of agent scope — required for full restore)

1. **Add the catalog section to the dwh config** (the dwh repo is hands-off for agents):
   ```toml
   # /home/ignwrad/Projects/dwh/.sqlcg.toml
   [sqlcg.catalog]
   path = "<absolute path>/columns.csv"
   ```
   This makes the v1.11.0 `_reapply_catalog_if_configured` hook fire on every full index,
   so the catalog survives a rebuild without a manual `sqlcg catalog load`.
2. **Restore the current live graph** (operator runbook, postmortem §Operator runbook):
   ```
   sqlcg catalog load columns.csv
   sqlcg gain   # expect ~80.6% strict / ~97.2% scoped / ~88% catalogued
   ```
3. **Re-index after PR 2 and PR 3 merge** — PR 2 recovers the 67 crashed files (~3.5k edges)
   and PR 3 re-keys namespaces; both require a fresh full index to take effect on the live
   graph.

---

## User verification queue

Per house rule, the agent never tags releases; the user tags after verifying. **On hold:**

- **v1.15.0–v1.18.0** — the lineage-identity sprint (PRs #80–#83), pending user verification
  of [`lineage_identity_sprint_postmortem.md`](../reports/lineage_identity_sprint_postmortem.md)
  and the restored (catalogued) metrics.
- **v1.18.1** (PR 1, this sprint) — test-isolation + empty-index rollback guard.
- **v1.18.2** (PR 2) — `_qualify_bare_tables` crash fix + worker-failure labelling.
- **v1.18.3** (PR 3) — repo-relative CTE/derived namespaces.
- **v1.19.0** (PR 4) — `<unknown>` self-edge drop + catalog-aware kind upgrade + §G warning.
- **v1.19.1** (PR 5) — persist skip reasons.

Each version is bumped in [`pyproject.toml`](../../pyproject.toml) +
[`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) + `uv lock` in its feature branch
before merge; tagged annotated `vX.Y.Z` on the master merge commit per CLAUDE.md.
