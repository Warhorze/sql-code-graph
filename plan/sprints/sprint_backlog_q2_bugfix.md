# Sprint Plan: Q2 Bug-Fix & Tech-Debt Backlog

> **Status: REVIEWED** (plan-reviewer 2026-06-13, APPROVED-WITH-AMENDMENTS). The amendments
> below are BINDING and override the in-body text where they conflict. Developers must apply
> the amendment for their PR before/while implementing.
>
> ### Plan-review amendments (fold before implementing)
> **PR-C (#118) — two blockers + one note:**
> - `stmt_idx` does NOT exist in `_extract_column_lineage` (`base.py:961-972` takes the AST `stmt`, no index). Do NOT use the flat-error-string round-trip. **Preferred:** add `qualify_failed: bool = False` to `ParsedStatement` (`base.py:255`) and write `"qualify_failed": stmt.qualify_failed` at `indexer.py:1361` (right where `parse_failed`/`statement_index` are already written). Set the flag from `_extract_column_lineage` via its `LineageExtraction` return (add a `qualify_failed` field) — do not thread a statement index.
> - `SCHEMA_VERSION` lives in `core/schema.py:6` (`"8"`→`"9"`), NOT `duckdb_backend.py` (which only imports it). Fix step 2 + Wiring Checklist.
> - Migration is enforced by the refuse-to-start guard at `tools.py:233-236` (prints `db reset && index`), NOT the #76 reexec self-heal (which keys on package version, not SCHEMA_VERSION). Add a wiring check + acceptance criterion that an old-schema DB yields that message, not a silent column error.
>
> **PR-B (#121/#123):** `_backend_lock` at `tools.py:119` is **fully dead** — there is NO `async with _backend_lock` anywhere (the real lock is the separate `backend_lock` in `server.py`/`writer.py:330`). The plan's "do not delete, write tools read it" claim is FALSE. **Delete `_backend_lock` (119) together with `_set_backend_lock` (127-134).** Do NOT touch the out-of-scope dead `kuzu_backend.py`/`neo4j_backend.py`.
>
> **PR-A (#116/#124):** the #124 acceptance grep must target the **gate line (~coverage_p1_p3_p4.md:495)**, not bare "95%" — 8 occurrences exist incl. the title and a historical "95% Feasibility Analysis" section, which stay. Replace only the pass/fail gate; report coverage as observed. The `cte_source_gap_writes` rename (Option B) must hit all **6** callsites in `coverage.py` (311,312,417,596,754,837) atomically.
>
> **PR-A → PR-C ordering:** both edit `coverage.py` (render block) — the Ticket Table wrongly marks both "Blocks: NONE". **PR-C depends on PR-A; merge PR-A first.**
>
> **PR-E (#119/#120) — highest risk, perf-invariants PASS with these required fixes:** (1) keep the existing `except Exception as exc: logger.error(...); raise` in the rewritten `run_read` and add `finally: cursor.close()` — do NOT drop error logging. (2) The cursor change is INERT for non-blocking reads unless the read path stops acquiring `backend_lock` — make step 4 (remove read-path lock) MANDATORY, and prove DuckDB gives a same-connection cursor a stable snapshot (Scenario A must prove, not assume). (3) Measure #120 RSS in a SUBPROCESS (`getrusage(RUSAGE_CHILDREN)` or `/proc/<pid>/VmHWM`), not process-lifetime `ru_maxrss`. (4) Wrap the hang-signal test's write `BEGIN` in try/finally on a DEDICATED backend instance so a failed assertion can't leak a transaction and poison later in-process tests (cf. #112 flake). The 4 perf-invariant suites must pass UNMODIFIED.
>
> **PR-D (#126/#122) note:** confirm `git.py` warns via a `console.print` object (the monkeypatch target), not bare `print`/`typer.echo`.
> **Version floor:** master is now **v1.27.1** (not 1.27.0); PR-3 holds v1.28.0. Confirm the next free version at branch time — do not hard-code.

**Plan date**: 2026-06-13
**Author**: sprint-planner (claude-sonnet-4-6)
**Source authority**: GitHub issues #116, #118, #119, #120, #121, #122, #123, #124, #126, #128 — triaged against master v1.27.0
**Policy**: Bugs and tech-debt only. No new capability surface. No features.

---

## Summary

Ten issues triaged from the post-v1.27.0 backlog. They cluster into six PR groups:

| Group | Issues | Topic |
|-------|--------|-------|
| PR-A | #116 + #124 | Coverage metric baselining |
| PR-B | #121 + #123 | Dead-code / stale-docstring housekeeping (`graph_db.py` + `tools.py`) |
| PR-C | #118 | qualify_failed persistence (indexer + schema) |
| PR-D | #126 + #122 | CLI UX: git hook binary resolution + test guard replacement |
| PR-E | #119 + #120 | DuckDB read concurrency + memory ceiling verification |
| PR-F | #128 | pr-impact self-healing reindex |

Version sequencing note: master is v1.27.0 at branch time. In-flight PRs above it:
- v1.27.1 — discoverability (rebasing)
- v1.28.0 — #38 PR-3

This sprint's PRs should therefore start at **v1.28.1** at the earliest (after #38 PR-3 merges)
or claim versions from **v1.28.x** in coordinated sequence. Each PR branch must check
`pyproject.toml` at branch time and claim the next available version — do NOT hard-code
version numbers in implementations.

---

## Scope

### In scope
- #116 Coverage metric numerator floor / rename
- #124 Drop the 95% pass/fail gate from the plan doc
- #118 Persist `qualify_failed` as a queryable graph attribute
- #119 Cursor-per-read for non-blocking reads during rebuild
- #120 1,600-file peak write-memory measurement + documented ceiling
- #121 GraphBackend ABC: de-Cypher docstrings + relocate `clear_all_tables` off ABC
- #122 TC6b terminal-sink guard: replace reconstructed-query assertion with CliRunner e2e
- #123 Delete dead `_set_backend_lock` stub in tools.py
- #126 git install-hooks: resolve user-facing binary, not venv path
- #128 pr-impact: self-healing reindex-to-base before analysis

### Non-goals
- #117 self-reload edge kind (closed/out of scope)
- #129 refactor advisor (closed/out of scope)
- E8 temp-chain revival (tracked separately, gated on recall metric)
- Any new CLI surface or MCP tool
- Schema migrations or new graph node types beyond #118

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|----------------|
| #116: `_Q_CTE_SOURCE_GAP_WRITES` counts sourceless literal inserts in numerator | **Confirmed** — `coverage.py:311-318`: query counts write queries with COLUMN_LINEAGE but no SELECTS_FROM; no-FROM inserts qualify because they have inferred COLUMN_LINEAGE edges but no real source table |
| #124: 95% gate is plan-doc only, not enforced in source | **Confirmed** — `grep -rn "95%"` finds references only in `plan/sprints/coverage_p1_p3_p4.md:495`, not in any `.py` file |
| #118: `qualify_failed` is not a standalone per-statement graph attribute | **Partially persisted** — `File.parse_cause = "qualify_failed"` is stored when dominant (`indexer.py:1278`), but per-statement qualify failures are NOT individually queryable; `SqlQuery.parse_failed` is a boolean with no cause bucket. The per-statement gap is confirmed. |
| #119: `run_read` uses `self._conn.execute()` directly — no cursor snapshot path | **Confirmed** — `duckdb_backend.py:652-663`: `run_read` calls `self._conn.execute()` directly; concurrency note at lines 8-17 documents the limitation explicitly |
| #120: 1,600-file write-memory ceiling unverified | **Confirmed** — no test or postmortem records peak RSS at 1,600 files; `test_upsert_batch_invariant.py` pins call-counts only |
| #121: `clear_all_tables` is `NotImplementedError` default on ABC; stale "KuzuDB's MERGE semantics" docstring | **Confirmed** — `graph_db.py:249-255` and `graph_db.py:106` |
| #122: TC6b builds downstream SQL via `_downstream_sql()` and calls `db.run_read` directly — not end-to-end | **Confirmed** — `test_user_surface_recall_guard.py:26` imports `_downstream_sql`; lines 372-378 and 413-414 call `db.run_read(query, ...)` |
| #123: `_set_backend_lock` defined at `tools.py:127`, zero call sites | **Confirmed** — `grep -rn "_set_backend_lock" src/` returns only the definition at line 127 |
| #126: `_resolve_sqlcg_bin` resolves via `shutil.which("sqlcg")` first — returns venv path on editable install | **Confirmed** — `git.py:74`: `shutil.which("sqlcg")` is first; on an editable install in an active venv, this resolves to `.venv/bin/sqlcg` |
| #128: `_compute_pr_impact` returns error hint when `indexed_sha != base_sha` — no self-heal | **Confirmed** — `tools.py:2792-2805`: returns `PrImpactResult(..., hint="Run 'sqlcg index…' first")` and exits without reindexing |

---

## Ticket Table

| ID | Title | Issues | Files | Effort | Priority | Depends on | Blocks |
|----|-------|--------|-------|--------|----------|------------|--------|
| PR-A | Coverage metric baselining | #116, #124 | `coverage.py`, `coverage_p1_p3_p4.md` | S | HIGH | INDEPENDENT | NONE |
| PR-B | Dead-code housekeeping | #121, #123 | `graph_db.py`, `tools.py` | XS | MED | INDEPENDENT | NONE |
| PR-C | Persist qualify_failed per statement | #118 | `duckdb_backend.py`, `indexer.py`, `base.py`, `coverage.py` | S | MED | INDEPENDENT | NONE |
| PR-D | git hook binary + TC6b guard | #126, #122 | `cli/commands/git.py`, `test_user_surface_recall_guard.py` | S | MED | INDEPENDENT | NONE |
| PR-E | DuckDB read concurrency + memory ceiling | #119, #120 | `duckdb_backend.py`, `writer.py`, `server.py` | M | MED | INDEPENDENT | PR-F (same file area) |
| PR-F | pr-impact self-healing reindex | #128 | `server/tools.py`, `indexer/git_delta.py` | M | MED | INDEPENDENT | NONE |

---

## Recommended Implementation Order

### Why this order

**PR-A first** — affects [`coverage.py`](src/sqlcg/cli/coverage.py) which no other PR in this
sprint touches. It is S-effort, purely additive (a metric rename/filter + plan-doc edit), and
has zero exposure to any performance invariant. Landing it first establishes honest metric
baselines before deeper work reads from those metrics.

**PR-B second** — dead-code delete and docstring fixes. Zero functional risk. The
`_set_backend_lock` delete and graph_db.py docstring fixes are XS and combined because both
touch only declarations/docstrings and are reviewable in one pass. Landing this early keeps the
diff surface clean for the riskier PRs.

**PR-C third** — adds a `qualify_failed` column to the `SqlQuery` schema. This is a schema
change (requires re-index after merge, per CLAUDE.md policy) but is self-contained; no other
PR in this sprint reads the new column. Lands before PR-D/PR-E/PR-F so that the schema is
stable when those PRs branch.

**PR-D fourth** — two independent S-effort test/CLI fixes (git hook binary resolution + TC6b
CliRunner guard) with no backend dependency and no perf-invariant exposure. Combined because
they are the same risk tier and reviewable together.

**PR-E fifth** — M-effort DuckDB backend changes. The cursor-per-read path (#119) changes
[`duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py); the memory ceiling test (#120) adds
an integration fixture. These must not regress the bulk-upsert / batch-flush invariants.
Ships after PR-C because PR-C adds a schema column that PR-E's memory-ceiling test must
index against (to exercise the full schema).

**PR-F last** — M-effort pr-impact self-healing. It touches
[`server/tools.py`](src/sqlcg/server/tools.py) which is the largest file in the codebase.
Isolating it last minimises merge-conflict surface with PR-E (different focus areas, but
same test harness). Needs live-DWH re-acceptance before closing.

### Single-developer sequence

1. PR-A — coverage metric baselining (#116 + #124)
2. PR-B — dead-code housekeeping (#121 + #123)
3. PR-C — persist qualify_failed per statement (#118)
4. PR-D — git hook binary + TC6b guard (#126 + #122)
5. PR-E — DuckDB read concurrency + memory ceiling (#119 + #120)
6. PR-F — pr-impact self-healing reindex (#128)

### Two-developer sequence

**Track A** (backend/schema): PR-A → PR-C → PR-E → PR-F

**Track B** (housekeeping/tests): PR-B → PR-D

Both tracks start immediately after the branch is cut. Track B is fully independent of Track A.
Within Track A: PR-A is independent; PR-C schema change should merge before PR-E so the memory
ceiling test exercises the complete schema; PR-F after PR-E so that the read-lock change is
stable before the pr-impact self-heal path is added.

---

## Ticket Specifications

---

### PR-A — Coverage metric baselining (#116 + #124)

**Source**: GitHub #116; #124 (ARCHITECTURE_REVIEW §5 F7 MED)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: Two coupled problems in [`coverage.py`](src/sqlcg/cli/coverage.py).

#116: `_Q_CTE_SOURCE_GAP_WRITES` at `coverage.py:311-318` counts write queries that have
COLUMN_LINEAGE but no SELECTS_FROM source. No-FROM literal inserts
(`INSERT … SELECT NULL AS x`) satisfy this condition by construction — they carry
`inferred_from_source_name` COLUMN_LINEAGE edges but can never gain a SELECTS_FROM row.
This creates a permanent ~48-unit floor that is correct-by-design, not a coverage gap.

#124: The 95% edge-health milestone appears in
[`plan/sprints/coverage_p1_p3_p4.md`](plan/sprints/coverage_p1_p3_p4.md) at lines ~490-495
as a pass/fail gate criterion. Post-P1/P3/P4 measurement shows it is ~30 percentage points
short. The decided action (issue body) is to **drop the numeric gate entirely** and report
coverage as an observed number. The gate is a plan-doc artefact — no source code enforces it.

**What to do**:

1. In [`coverage.py`](src/sqlcg/cli/coverage.py), address the ~48-unit floor via one of two
   approaches:

   **Option A — filter** (preferred if a reliable SQL discriminator exists): add to
   `_Q_CTE_SOURCE_GAP_WRITES` a clause that excludes writes whose COLUMN_LINEAGE edges are
   all of `inferred_from_source_name` kind (i.e. no real-scope-derived edges). Verify the
   filter against the live DWH: the count must drop by ~48.

   **Option B — rename + floor note** (fallback if no reliable SQL discriminator): rename
   `cte_source_gap_writes` → `sourceless_write_gap` and add a module-level constant above
   the query:
   ```python
   # Floor note: ~48 no-FROM literal inserts are permanently sourceless by construction
   # (INSERT … SELECT NULL AS …). The net CTE-addressable gap is
   # sourceless_write_gap minus this floor.
   _LITERAL_INSERT_FLOOR = 48  # measured 2026-06-13 on DWH v1.27.0
   ```
   Update all render sites to print the floor note alongside the metric.

   Choose Option A if the edge kind discriminator is available; Option B otherwise.

2. Update the `CoverageReport` dataclass field name (if Option B / rename).

3. In [`plan/sprints/coverage_p1_p3_p4.md`](plan/sprints/coverage_p1_p3_p4.md), replace the
   95% gate criterion (~line 495) with:
   > 95% numeric gate dropped — coverage is reported as an observed metric (no target).
   > Post-P1/P3/P4 baseline: ~55–65% scoped. Next improvement sprint: BQ-2.
   > See GitHub #124.

4. Update `ARCHITECTURE_REVIEW.md` §5 F7 to mark it resolved with a pointer to this PR.

**Wiring verification**:

- `grep -n "_Q_CTE_SOURCE_GAP_WRITES\|sourceless_write_gap\|cte_source_gap_writes" src/sqlcg/cli/coverage.py`
  must show the updated query and any rename; all render callsites must be consistent.
- `grep -rn "cte_source_gap_writes" src/` must return 0 results if renamed (or show only
  the updated definition and its render sites if kept).
- `grep -n "95%" plan/sprints/coverage_p1_p3_p4.md` must show the replacement note, not
  the old gate criterion.
- No TODO in the happy path of the metric computation.

**Files affected**:
- [`src/sqlcg/cli/coverage.py`](src/sqlcg/cli/coverage.py) — metric query + label + dataclass field
- [`plan/sprints/coverage_p1_p3_p4.md`](plan/sprints/coverage_p1_p3_p4.md) — drop 95% gate
- [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) — mark F7 resolved

**Tests to add**:

- **Scenario A — literal-insert floor excluded**: index a fixture containing only no-FROM
  literal inserts (`INSERT INTO t SELECT NULL AS x`). Assert that the metric returns 0 for
  that fixture (these are not an addressable CTE gap). Assertion: count == 0, not the
  literal-insert count.
- **Scenario B — real CTE gap counted**: index a fixture with a write query that reads from
  a CTE and has COLUMN_LINEAGE but no SELECTS_FROM source table. Assert the metric returns
  ≥ 1.

**Acceptance criteria**:
- `[ ]` `uv run sqlcg gain` on the live DWH no longer conflates the ~48 literal-insert floor
  with addressable CTE gaps, either by filtering them or by printing the floor explicitly
- `[ ]` `grep -n "95%" plan/sprints/coverage_p1_p3_p4.md` shows the replacement note, not a
  pass/fail gate criterion
- `[ ]` No TODO in the happy path of `_Q_CTE_SOURCE_GAP_WRITES` (or its renamed successor)
- `[ ]` Wiring: `grep -n "cte_source_gap_writes\|sourceless_write_gap" src/sqlcg/cli/coverage.py`
  shows the updated query and all rendering callsites updated consistently

---

### PR-B — Dead-code housekeeping (#121 + #123)

**Source**: GitHub #121 (ARCHITECTURE_REVIEW §5 F4 LOW), #123 (ARCHITECTURE_REVIEW §5 F6 LOW)
**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: Two independent dead-code / stale-docstring problems in the same risk tier,
combined because neither warrants a standalone PR.

#123 ([`server/tools.py:127`](src/sqlcg/server/tools.py)): `_set_backend_lock` was written
to wire the MCP event-loop lock into write tools, but the escalation plumbing it belonged to
was deleted during the DuckDB migration. The function body sets `_backend_lock` but no caller
invokes it — `grep -rn "_set_backend_lock" src/` returns only the definition at line 127.
The module-level `_backend_lock` variable at line 119 is still read by write tools and must
NOT be deleted — only the setter function is dead.

#121 ([`core/graph_db.py`](src/sqlcg/core/graph_db.py)): `upsert_edges_bulk` docstring at
line 106 says "silently skipped by KuzuDB's MERGE semantics" — Kuzu was replaced by DuckDB
in v1.4.0. `clear_all_tables` at lines 249-255 is a `NotImplementedError`-default on the ABC
rather than an `@abstractmethod`, which is misleading (`DuckDBBackend` already implements it).
The ABC is intentionally kept for a potential v2 ACCESS_HISTORY second implementation.

**What to do**:

1. In [`server/tools.py`](src/sqlcg/server/tools.py):
   - Confirm zero call sites: `grep -rn "_set_backend_lock" src/` must return only line 127.
   - Delete lines 127-134 (the `_set_backend_lock` function and its docstring).
   - Do NOT delete `_backend_lock` at line 119 — it is read by the `async with _backend_lock:`
     guard in write tool paths.

2. In [`core/graph_db.py`](src/sqlcg/core/graph_db.py):
   - Line 106: replace "silently skipped by KuzuDB's MERGE semantics" with:
     "silently skipped — rows whose src or dst node does not exist are not inserted
     (upsert ordering requirement: node upserts must precede edge upserts in the same
     transaction; see indexer `_upsert_parsed_file`)."
   - Lines 249-255: convert `clear_all_tables` from a `raise NotImplementedError` default
     to `@abstractmethod` so the ABC correctly signals the contract.
   - Scan for and remove any remaining "KuzuDB", "Kuzu", or "Cypher" references that are
     stale post-DuckDB-migration.

**Wiring verification**:

- `grep -rn "_set_backend_lock" src/` must return 0 results after the delete.
- `grep -n "KuzuDB\|kuzudb\|Kuzu" src/sqlcg/core/graph_db.py` must return 0 results.
- `grep -n "class DuckDBBackend\|def clear_all_tables" src/sqlcg/core/duckdb_backend.py`
  confirms the concrete override exists — promoting to `@abstractmethod` is safe.
- `uv run pyright src/sqlcg/core/` must pass after the abstractmethod change.

**Files affected**:
- [`src/sqlcg/server/tools.py`](src/sqlcg/server/tools.py) — delete `_set_backend_lock`
- [`src/sqlcg/core/graph_db.py`](src/sqlcg/core/graph_db.py) — docstrings + abstractmethod

**Tests to add**:

- **Scenario A — abstract method enforced**: define a stub subclass of `GraphBackend` that
  does NOT implement `clear_all_tables`. Assert `pytest.raises(TypeError)` on instantiation.
  Assertion: `TypeError` is raised (not a silent `NotImplementedError` at call time).

**Acceptance criteria**:
- `[ ]` `grep -rn "_set_backend_lock" src/` returns 0 results
- `[ ]` `grep -n "KuzuDB\|kuzudb" src/sqlcg/core/graph_db.py` returns 0 results
- `[ ]` `uv run pyright src/sqlcg/core/` passes with no new errors
- `[ ]` `uv run pytest tests/unit/test_bulk_upsert_invariant.py` passes (no regression)

---

### PR-C — Persist qualify_failed per statement (#118)

**Source**: GitHub #118
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: `qualify_failed` is emitted as an error string
`"col_lineage_skip:qualify_failed:<ExcType>"` inside [`parsers/base.py:1114`](src/sqlcg/parsers/base.py)
and classified by [`indexer/error_classify.py:119-120`](src/sqlcg/indexer/error_classify.py).
At file level it propagates to `File.parse_cause` when dominant (`indexer.py:1278`).
At statement level the only persisted field is the boolean `SqlQuery.parse_failed`, not the
cause bucket. Per-statement qualify failures are therefore not queryable post-hoc: the ~7
vs ~9 A/B delta reported in the #38 PR-2 acceptance review could not be derived from the graph.

The `qualify_failed` error string is appended to `ParsedFile.errors` (flat list for the whole
file, `base.py:1114`), not to a per-statement errors collection. To support per-statement
attribution, we must propagate a statement-index tag at the point the error is recorded.

**What to do**:

1. In [`parsers/base.py`](src/sqlcg/parsers/base.py), at the `qualify_failed` error append
   (line 1113-1115), include the statement index in the error string:
   ```python
   out.errors.append(
       f"col_lineage_skip:qualify_failed:{type(_qualify_exc).__name__}:stmt={stmt_idx}"
   )
   ```
   where `stmt_idx` is the index of the current statement in `_extract_column_lineage`.
   Verify that `_classify_error` in `error_classify.py` still matches the prefix
   `"qualify_failed:"` correctly after the suffix addition.

2. In [`core/duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py), add a `qualify_failed`
   column to the `SqlQuery` CREATE TABLE statement (after `parse_failed`):
   ```sql
   qualify_failed BOOLEAN DEFAULT FALSE,
   ```
   Bump `SCHEMA_VERSION`. Re-index is the migration path (CLAUDE.md policy).

3. In [`indexer/indexer.py`](src/sqlcg/indexer/indexer.py), update `_build_file_row_set`
   to compute per-statement `qualify_failed` from `parsed.errors`. Parse the
   `stmt=<idx>` suffix added in step 1 to build a `qualify_failed_stmts: set[int]` set,
   then set `"qualify_failed": i in qualify_failed_stmts` for each statement row.

4. In [`core/duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py), update
   `upsert_nodes_bulk` for `SqlQuery` to include `qualify_failed` in the INSERT column
   list (the two column lists around lines 238 and 251).

5. In [`cli/coverage.py`](src/sqlcg/cli/coverage.py), add a query constant and metric:
   ```sql
   SELECT COUNT(*) AS qualify_failed_statements
   FROM "SqlQuery" WHERE qualify_failed = TRUE
   ```
   Expose in `uv run sqlcg gain` output so A/B comparisons are citable from the graph.

**Wiring verification**:

- `grep -n "qualify_failed" src/sqlcg/core/duckdb_backend.py` must show the column in
  the CREATE TABLE statement AND in both upsert column lists.
- `grep -n "qualify_failed_stmts\|qualify_failed" src/sqlcg/indexer/indexer.py` must show
  the new per-statement computation and `"qualify_failed"` key in `query_rows`.
- `grep -n "col_lineage_skip:qualify_failed" src/sqlcg/parsers/base.py` must show the
  `stmt=<idx>` suffix in the error string.
- `grep -n "qualify_failed_statements" src/sqlcg/cli/coverage.py` must show the new query
  and its render site.
- `_classify_error("col_lineage_skip:qualify_failed:ValueError:stmt=3")` must still return
  `"qualify_failed"` — verify `error_classify.py:119` prefix match is unchanged.
- No TODO in the populate path.

**Files affected**:
- [`src/sqlcg/parsers/base.py`](src/sqlcg/parsers/base.py) — add `stmt=<idx>` to error string
- [`src/sqlcg/core/duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py) — schema column + upsert
- [`src/sqlcg/indexer/indexer.py`](src/sqlcg/indexer/indexer.py) — per-statement populate
- [`src/sqlcg/cli/coverage.py`](src/sqlcg/cli/coverage.py) — new `qualify_failed_statements` metric

**Tests to add**:

- **Scenario A — qualify failure persisted per statement**: index a fixture SQL file that
  triggers a qualify exception on exactly one statement. Assert
  `SELECT id FROM "SqlQuery" WHERE qualify_failed = TRUE` returns exactly that statement's
  `id`. Assertion: result list length == 1 with the expected `id`.
- **Scenario B — clean file not flagged**: for a fixture that parses cleanly, assert
  `SELECT COUNT(*) FROM "SqlQuery" WHERE qualify_failed = TRUE` returns 0.
- **Scenario C — error classifier unchanged**: unit test `_classify_error` with the new
  `stmt=<idx>` suffix format. Assert it still returns `"qualify_failed"`.

**Acceptance criteria**:
- `[ ]` `SELECT * FROM "SqlQuery" LIMIT 1` on a freshly-indexed graph includes a
  `qualify_failed` column (BOOLEAN)
- `[ ]` After indexing a corpus with known qualify failures, `SELECT COUNT(*) FROM "SqlQuery" WHERE qualify_failed = TRUE` returns a non-zero count matching the index-log `qualify_failed:` bucket total
- `[ ]` `uv run sqlcg gain` shows a `qualify_failed_statements` line that is graph-queryable
- `[ ]` `grep -n "qualify_failed" src/sqlcg/core/duckdb_backend.py` shows the column in
  CREATE TABLE and in both upsert column lists
- `[ ]` No TODO in the `qualify_failed` population path in `_build_file_row_set`

---

### PR-D — git hook binary resolution + TC6b CliRunner guard (#126 + #122)

**Source**: GitHub #126 (ARCHITECTURE_REVIEW §5 F10 LOW), #122 (ARCHITECTURE_REVIEW §5 F5 LOW)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

**Root cause**: Two independent S-effort issues in the same "test/CLI tooling quality" risk
tier, reviewable together.

#126 ([`cli/commands/git.py:63-88`](src/sqlcg/cli/commands/git.py)): `_resolve_sqlcg_bin`
calls `shutil.which("sqlcg")` first. On an editable install with an active venv,
`which("sqlcg")` resolves to `.venv/bin/sqlcg`. If the venv is deleted and rebuilt, the
embedded hook path becomes stale while `~/.local/bin/sqlcg` (user-installed PyPI copy)
remains current. A version-skew surface.

#122 ([`tests/integration/test_user_surface_recall_guard.py:372-414`](tests/integration/test_user_surface_recall_guard.py)):
`test_tc6b_downstream_sink_location_present` imports `_downstream_sql` from
`sqlcg.cli.commands.analyze` and calls `db.run_read(query, ...)` directly, bypassing the
CLI layer. The guard can drift from the shipped command if the CLI argument parsing, context
injection, or output formatting changes — the exact failure mode that makes regression guards
ineffective (cf. #40).

**What to do**:

For #126:

1. In [`cli/commands/git.py`](src/sqlcg/cli/commands/git.py), update `_resolve_sqlcg_bin`
   resolution order to prefer the stable user-installed binary:
   - **New step 0**: attempt `Path(site.getuserbase()) / "bin" / "sqlcg"` (or platform
     equivalent). If it exists and is executable, return it.
   - Existing step 1: `shutil.which("sqlcg")`.
   - Add after step 1: if the resolved path contains `/.venv/` or `\\.venv\\`, emit a
     warning: "Resolved binary is inside a virtualenv ('.venv'); the hook may go stale if
     the venv is rebuilt. Consider `uv tool install sqlcg` for a stable path."
   - Existing steps 2 and 3 unchanged.

2. Do not change `_install_single_hook` or the hook templates.

For #122:

1. In [`tests/integration/test_user_surface_recall_guard.py`](tests/integration/test_user_surface_recall_guard.py):
   - Remove the `_downstream_sql` import from the module-level imports (line 26) if it is
     used only in TC6b. If used elsewhere, keep the import but do not use it in TC6b.
   - Replace the TC6b body with a `CliRunner` invocation:
     ```python
     from typer.testing import CliRunner
     from sqlcg.cli.app import app
     runner = CliRunner()
     result = runner.invoke(app, ["analyze", "downstream", "staging.src_a.x", "--depth", "5"])
     assert result.exit_code == 0, f"CLI exited {result.exit_code}: {result.output}"
     assert "mart.fact_kpi.measure" in result.output
     assert "?" not in result.output.splitlines()[-5:], (
         "Terminal sink location rendered as '?' — fix_downstream_sink_location regression"
     )
     ```
   - The `indexed_db` fixture must still be used to ensure the graph is populated before
     the CLI invocation. If CliRunner cannot share the fixture's backend, use a temporary
     DB path written by the fixture and pass `--db` to the CLI.

**Wiring verification**:

For #126:
- `grep -n "_resolve_sqlcg_bin\|\.venv\|getuserbase" src/sqlcg/cli/commands/git.py` must
  show the updated resolution order with the user-base path and the venv warning.
- `grep -n "shutil.which" src/sqlcg/cli/commands/git.py` must still show `which` as a
  fallback (not removed, just demoted).

For #122:
- `grep -n "_downstream_sql" tests/integration/test_user_surface_recall_guard.py` must
  return 0 results inside the `test_tc6b_*` function body.
- `grep -n "CliRunner\|runner.invoke" tests/integration/test_user_surface_recall_guard.py`
  must show the new CliRunner call within `test_tc6b_*`.
- No TODO in TC6b's assertion block.

**Files affected**:
- [`src/sqlcg/cli/commands/git.py`](src/sqlcg/cli/commands/git.py) — binary resolution order
- [`tests/integration/test_user_surface_recall_guard.py`](tests/integration/test_user_surface_recall_guard.py) — TC6b rewrite

**Tests to add**:

- **Scenario A — venv path warning emitted**: mock `shutil.which("sqlcg")` to return
  `/project/.venv/bin/sqlcg` and mock `site.getuserbase()` to return a path with no
  `sqlcg` binary. Assert `_resolve_sqlcg_bin()` emits a printed warning containing ".venv".
  Assertion: warning text captured via monkeypatched `console.print`.
- **Scenario B — TC6b end-to-end via CliRunner**: covered by the rewrite itself. Assert
  `result.exit_code == 0` and `"mart.fact_kpi.measure"` in `result.output`.

**Acceptance criteria**:
- `[ ]` TC6b passes via `uv run pytest tests/integration/test_user_surface_recall_guard.py::test_tc6b_downstream_sink_location_present`
- `[ ]` `grep -n "_downstream_sql" tests/integration/test_user_surface_recall_guard.py`
  returns 0 results inside the TC6b function body (no internal API import in the guard)
- `[ ]` `_resolve_sqlcg_bin()` prefers `~/.local/bin/sqlcg` over `.venv/bin/sqlcg` when both exist
- `[ ]` No TODO in either changed path

---

### PR-E — DuckDB read concurrency + memory ceiling verification (#119 + #120)

**Source**: GitHub #119 (ARCHITECTURE_REVIEW §5 F2 LOW), #120 (ARCHITECTURE_REVIEW §5 F3 LOW)
**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: PR-F (both touch `duckdb_backend.py` / same test harness; if PR-F branches before PR-E merges, expect a conflict on `run_read`)

**Root cause**: Two related DuckDB backend/scale issues combined because they are the same
M-effort backend risk tier and the memory ceiling test exercises the updated `run_read` path.

#119 ([`core/duckdb_backend.py:652-663`](src/sqlcg/core/duckdb_backend.py)): `run_read` uses
`self._conn.execute()` directly. DuckDB's MVCC would allow a `self._conn.cursor()` to see a
snapshot at cursor-creation time, but `self._conn.execute()` is connection-level — any pending
write on that connection is visible. In the server, `backend_lock` is held by the drain task
for the full rebuild; read calls routed through the server socket are dispatched by the event
loop but do NOT acquire `backend_lock` themselves (the drain holds it). Confirm this path
before changing anything — the actual contention may be lower than the issue suggests.
The fix (cursor path) is correct regardless: cursor-per-read is the documented DuckDB pattern
for non-blocking snapshot reads.

#120 ([`indexer/indexer.py`](src/sqlcg/indexer/indexer.py)): the 1,600-file memory ceiling is
unverified. `test_upsert_batch_invariant.py` pins call-counts only.

**PERF INVARIANT CALLOUT** — This PR must not regress:
- `_flush_row_batch` accumulates rows across all files in a batch and calls `upsert_*_bulk`
  once per batch (invariant pinned by `test_upsert_batch_invariant.py`).
- `_upsert_parsed_file` uses `upsert_nodes_bulk`/`upsert_edges_bulk`, never per-row methods.
- `test_upsert_batch_invariant.py` and `test_perf_scaling_guard.py` must remain GREEN.
- `run_read` is called only from read paths (not from within `_flush_row_batch` or any write
  loop). The cursor change must not introduce a read call into the write hot path.

**What to do**:

For #119:

1. Investigate the actual contention path: trace how a read MCP tool call reaches
   `duckdb_backend.run_read` at runtime. Check whether the server handler for reads
   acquires `backend_lock` before calling `run_read`. Add a comment to the investigation
   finding in the PR description.

2. In [`core/duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py), update `run_read` to
   use a cursor:
   ```python
   def run_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
       """Execute a read-only SQL query via a snapshot cursor."""
       cursor = self._conn.cursor()
       try:
           if params:
               result = cursor.execute(query, self._params_list(params))
           else:
               result = cursor.execute(query)
           columns = [desc[0] for desc in result.description or []]
           return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
       finally:
           cursor.close()
   ```

3. Update the concurrency contract comment at `duckdb_backend.py:8-17` to state that
   `run_read` now uses a snapshot cursor and is non-blocking with respect to `backend_lock`.

4. If the investigation in step 1 reveals that the server handler DOES acquire
   `backend_lock` for reads, remove that acquisition from the read dispatch path in
   [`server/server.py`](src/sqlcg/server/server.py). The drain (`writer.py:330`) must still
   hold the lock for the full write transaction.

For #120:

1. Create `tests/integration/test_write_memory_ceiling.py`. The test must:
   - Generate 1,600 minimal SQL fixture files programmatically (not committed to the repo).
   - Run the indexer against them using a temporary DuckDB path.
   - Capture peak RSS using `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`.
   - Assert `peak_rss_bytes < 3.8 * 1024**3` (3.8 GiB, aligned with CLAUDE.md OOM rule).
   - Include a failure message: `"Peak write-memory must stay below 3.8 GiB on a 1,600-file corpus — guards against OOM regression"`.

2. Record the measured RSS value in `ARCHITECTURE_REVIEW.md` §5 F3 as the closed baseline.

**Wiring verification**:

- `grep -n "cursor()" src/sqlcg/core/duckdb_backend.py` must show cursor usage inside
  `run_read`.
- `grep -n "backend_lock" src/sqlcg/server/server.py` — read handler dispatch must NOT
  acquire `backend_lock` (drain body must still acquire it in `writer.py:330`).
- `uv run pytest tests/unit/test_upsert_batch_invariant.py tests/unit/test_perf_scaling_guard.py`
  must pass — batch-flush invariant intact.
- No TODO in the updated `run_read`.

**Files affected**:
- [`src/sqlcg/core/duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py) — `run_read` cursor
- [`src/sqlcg/server/server.py`](src/sqlcg/server/server.py) — lock scope review (if needed)
- [`src/sqlcg/server/writer.py`](src/sqlcg/server/writer.py) — lock scope review (confirm drain)
- `tests/integration/test_write_memory_ceiling.py` — new 1,600-file RSS test

**Tests to add**:

- **Scenario A — cursor snapshot non-blocking**: start a write transaction on the
  DuckDB connection, invoke `run_read` from a separate thread, assert it returns in < 1 s
  without waiting for the write to commit. Assertion: thread join completes within the timeout
  and `read_result` is non-empty.
- **Scenario B — memory ceiling at 1,600 files**: generate 1,600 minimal SQL fixture files,
  run the indexer, capture peak RSS. Assert `peak_rss_bytes < 3.8 * 1024**3`.
  Failure message: `"Peak write-memory must stay below 3.8 GiB on a 1,600-file corpus — guards against OOM regression"`.

**Acceptance criteria**:
- `[ ]` `grep -n "cursor()" src/sqlcg/core/duckdb_backend.py` returns ≥1 result inside `run_read`
- `[ ]` Read handler in `server.py` does NOT acquire `backend_lock` — grep confirms
- `[ ]` Peak RSS measured at 1,600-file corpus, asserted < 3.8 GiB, and result recorded in `ARCHITECTURE_REVIEW.md` §5 F3
- `[ ]` `uv run pytest tests/unit/test_upsert_batch_invariant.py tests/unit/test_perf_scaling_guard.py` passes
- `[ ]` Live-DWH re-acceptance: `sqlcg index <dwh_path>` + `sqlcg gain` after PR-E merges shows identical edge counts to the pre-PR-E baseline

**Live-DWH re-acceptance required**: YES — concurrency contract change. Edge counts and
index-log error buckets must be identical before and after.

---

### PR-F — pr-impact self-healing reindex (#128)

**Source**: GitHub #128 (ARCHITECTURE_REVIEW §5 F12 MED)
**Effort**: M
**Depends on**: INDEPENDENT (branch after PR-E merges to avoid `tools.py` conflict)
**Blocks**: NONE

**Root cause**: `_compute_pr_impact` in [`server/tools.py:2792-2805`](src/sqlcg/server/tools.py)
returns a `PrImpactResult` with an error hint when `indexed_sha != base_sha`, rather than
self-healing by reindexing to `base_sha`. This makes `pr-impact` single-shot: a second
invocation after a HEAD-advancing first run fails with the hint. Worse, the installed
`post-checkout` / `post-merge` hooks fire background `sqlcg reindex` calls on every branch
switch, racing `pr-impact` off the base-ref mid-analysis.

**What to do**:

1. In `_compute_pr_impact` ([`server/tools.py`](src/sqlcg/server/tools.py)), replace the
   hard-exit at lines 2794-2805 with a self-heal attempt:
   ```python
   indexed_sha = db.get_indexed_sha()
   if indexed_sha != base_sha:
       # Self-heal: reindex graph to base_sha before running the analysis.
       healed = _reindex_to_sha(db, root, base_sha)
       if not healed:
           return PrImpactResult(
               base_ref=base_ref, base_sha=base_sha, head_sha=None,
               detection_only_code_regression=True,
               hint=(
                   f"Graph is at '{indexed_sha}'; auto-reindex to '{base_sha}' failed. "
                   "Run 'sqlcg reindex --from HEAD --to <base>' manually, then retry."
               ),
           )
   ```

2. Implement `_reindex_to_sha(db: GraphBackend, root: Path, target_sha: str) -> bool` as a
   private function in `tools.py`. It must:
   - Use the existing incremental reindex path in
     [`indexer/git_delta.py`](src/sqlcg/indexer/git_delta.py) if available, passing
     `from_sha=db.get_indexed_sha()` and `to_sha=target_sha`.
   - Return `True` on success (confirmed by `db.get_indexed_sha() == target_sha` after the
     reindex), `False` on failure.
   - Not swallow exceptions silently — log at ERROR and return False.

3. After the analysis completes, restore the graph to the original HEAD state. The existing
   `resync_changed` call in `_compute_pr_impact` (step 4 in the current code) already
   advances the graph to HEAD — confirm this restore-to-HEAD is still invoked after the
   self-heal path.

4. Document the known limitation: installed `post-checkout` hooks can still race
   `pr-impact` by triggering a background reindex during the analysis window. Add a
   docstring note to `_compute_pr_impact` recommending that users who run `pr-impact`
   frequently consider disabling the background hooks (`sqlcg git uninstall-hooks`) or
   scheduling `pr-impact` outside of hook-active windows. A full hook-coordination solution
   is a follow-up.

**Wiring verification**:

- `grep -n "_reindex_to_sha" src/sqlcg/server/tools.py` must return ≥2 results: the
  function definition AND its call site inside `_compute_pr_impact`.
- `grep -n "indexed_sha.*base_sha\|base_sha.*indexed_sha" src/sqlcg/server/tools.py` —
  the old hard-exit `return PrImpactResult(..., hint="Run 'sqlcg index…' first")` must be
  replaced by the self-heal block.
- `grep -n "resync_changed\|get_indexed_sha" src/sqlcg/server/tools.py` — confirm
  `get_indexed_sha()` is called after the self-heal to verify success, and `resync_changed`
  is still called to restore HEAD after analysis.
- No TODO in `_reindex_to_sha` or the self-heal path.

**Files affected**:
- [`src/sqlcg/server/tools.py`](src/sqlcg/server/tools.py) — `_compute_pr_impact` + new `_reindex_to_sha`
- [`src/sqlcg/indexer/git_delta.py`](src/sqlcg/indexer/git_delta.py) — possibly extended for reindex-to-sha entry point

**Tests to add**:

- **Scenario A — self-heal fires on SHA mismatch**: create a fixture where
  `db.get_indexed_sha()` returns a stale SHA. Mock `_reindex_to_sha` to succeed. Call
  `_compute_pr_impact`. Assert the result does NOT carry the "please reindex manually"
  hint and `detection_only_code_regression` does not reflect the error-exit path.
  Assertion: `result.hint` is None or does not contain "manually".
- **Scenario B — second invocation succeeds**: invoke `_compute_pr_impact` twice in
  sequence against a fixture where HEAD advances between calls. Assert both return a
  result with a valid `base_sha` and no error hint.

**Acceptance criteria**:
- `[ ]` A second `sqlcg pr-impact --base <ref>` after a first run that advanced HEAD
  completes with a valid result, not a "please reindex" error
- `[ ]` `grep -n "_reindex_to_sha" src/sqlcg/server/tools.py` returns ≥2 results (definition + call site)
- `[ ]` No TODO in `_reindex_to_sha` or the self-heal path
- `[ ]` Live-DWH re-acceptance: run `pr-impact` twice in sequence on the live DWH; second
  run must complete with a result (not an error hint)

**Live-DWH re-acceptance required**: YES — pr-impact is only meaningful on a real corpus with real git history.

---

## Test Strategy

### The single most important regression guard

The most critical regression risk across this sprint is PR-E: the cursor-per-read path change
in `duckdb_backend.run_read` could silently introduce a new form of read-write serialisation
if the cursor is not properly isolated from the write connection's in-progress transaction.

```python
def test_run_read_returns_snapshot_without_blocking_write_transaction():
    """run_read must return a snapshot result without waiting for a concurrent write.

    Guards against the issue #119 regression where reads serialised behind
    backend_lock for the full rebuild duration. Any change to run_read that
    reintroduces connection-level serialisation will make this test hang and
    timeout, not fail gracefully — timeout is the signal.
    """
    import threading
    import time
    from sqlcg.core.duckdb_backend import DuckDBBackend

    db = DuckDBBackend(":memory:")
    db.init_schema()

    write_started = threading.Event()
    read_result: list = []

    def write_thread():
        db._conn.execute("BEGIN")
        write_started.set()
        time.sleep(0.5)  # Hold the write transaction open
        db._conn.execute("ROLLBACK")

    def read_thread():
        write_started.wait()
        rows = db.run_read("SELECT 1 AS x", {})
        read_result.extend(rows)

    wt = threading.Thread(target=write_thread)
    rt = threading.Thread(target=read_thread)
    wt.start()
    rt.start()
    rt.join(timeout=1.0)  # Must return well within the write's 0.5 s hold
    wt.join(timeout=2.0)

    assert read_result, (
        "run_read must return without waiting for the write transaction to commit — "
        "guards against issue #119 read-serialisation regression"
    )
    assert read_result[0]["x"] == 1
```

This test is NOT marked xfail. It will be RED on master today (because `run_read` uses
`self._conn.execute()` directly which may block on the write transaction). It becomes GREEN
after PR-E's cursor change lands. Ticket PR-E is the one that makes it GREEN.

Place in `tests/integration/test_write_memory_ceiling.py` alongside the memory ceiling test,
or as `tests/unit/test_run_read_snapshot_isolation.py`.

---

## Wiring Checklist

| Question | PR-A | PR-B | PR-C | PR-D | PR-E | PR-F |
|----------|------|------|------|------|------|------|
| What calls this? | `gain` CLI via `run_read_routed` calls the metric queries | `DuckDBBackend` instantiation triggers abstractmethod check; tools.py write path reads `_backend_lock` (no change) | `_build_file_row_set` populates `qualify_failed` in `query_rows`; `upsert_nodes_bulk` writes it; `coverage.py` reads via `run_read_routed` | `_resolve_sqlcg_bin()` called by `install_hooks`; TC6b called by pytest via CliRunner | `run_read` called by every read tool via `get_backend` / `_backend`; drain calls `run_write` under `backend_lock` | `_reindex_to_sha` called by `_compute_pr_impact`; `_compute_pr_impact` called by `get_pr_impact` (MCP) and CLI |
| Where is the callback/parameter passed? | Metric rename propagates to `CoverageReport` dataclass field and all `gain` render sites | `_set_backend_lock` has no caller to update (delete only); `@abstractmethod` enforced by Python ABC machinery | `stmt=<idx>` suffix in error string parsed in `_build_file_row_set`; `qualify_failed` key in `rows.query_rows` dict passed to `upsert_nodes_bulk` column list | `_resolve_sqlcg_bin()` return value passed to `_install_single_hook(hooks_dir, spec, sqlcg_bin)`; CliRunner `result.output` checked in TC6b | `cursor()` obtained from `self._conn`; if read-path lock is removed, change is in server dispatch | `_reindex_to_sha(db, root, base_sha)` receives the open backend + root path + target SHA; called from `_compute_pr_impact` before step 3 |
| What constant/path does this align with? | `_WRITE_KINDS_SQL` used in both metric queries — changes must be symmetric between them | `SCHEMA_VERSION` unchanged | `SCHEMA_VERSION` must be bumped in `duckdb_backend.py` after adding the column | `site.getuserbase()` for user-install path; `shutil.which` as fallback | `backend_lock` is the single `anyio.Lock` from `drain_loop` → `server.py` | `get_indexed_sha()` is the authoritative SHA source; `_resolve_git_ref` resolves base_ref to SHA |
| Does any TODO remain in the happy path? | No — floor note or filter must be fully implemented | No | No — `qualify_failed` population must not be a TODO stub | No | No — cursor path and lock scope must be fully implemented | No — `_reindex_to_sha` must be a full implementation, not a stub |

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PR-E cursor path breaks batch-flush invariant | MED | HIGH | Run `test_upsert_batch_invariant.py` and `test_perf_scaling_guard.py` before merge; any RED is a hard blocker |
| PR-C schema change (`qualify_failed` column) causes SCHEMA_VERSION mismatch on existing DBs | HIGH | MED | Re-index is the migration path (CLAUDE.md); bump SCHEMA_VERSION; verify error message on old schema |
| PR-F self-healing reindex races with `post-checkout` hook mid-analysis | MED | MED | Document incompatibility in `_compute_pr_impact` docstring; full hook-coordination is a follow-up |
| PR-A Option B rename breaks callers of `CoverageReport.cte_source_gap_writes` | LOW | LOW | `grep -rn "cte_source_gap_writes"` before rename; update all callsites atomically in same commit |
| In-flight PRs (v1.27.1 discoverability, v1.28.0 #38 PR-3) conflict with this sprint | MED | LOW | Branch each PR off master AFTER in-flight PRs merge; check version in `pyproject.toml` at branch time |
| PR-D TC6b CliRunner invocation slower than raw `run_read` approach, CI timeout | LOW | LOW | Set 30 s pytest timeout on the test; the integration fixture is small |
| DuckDB cursor semantics differ from `self._conn.execute()` for in-transaction reads | LOW | MED | Add an explicit test (Scenario A in PR-E) verifying snapshot isolation before merging |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `uv run sqlcg gain` on the live DWH no longer conflates ~48 literal-insert floor with addressable CTE gaps, OR the metric label makes the floor explicit in its printed output
- `[ ]` `grep -n "95%" plan/sprints/coverage_p1_p3_p4.md` returns the replacement note, not a pass/fail gate criterion
- `[ ]` `SELECT COUNT(*) FROM "SqlQuery" WHERE qualify_failed = TRUE` returns a queryable non-zero count on the live DWH after a fresh index
- `[ ]` `grep -rn "_set_backend_lock" src/` returns 0 results
- `[ ]` `grep -n "KuzuDB\|kuzudb" src/sqlcg/core/graph_db.py` returns 0 results
- `[ ]` `uv run pytest tests/integration/test_user_surface_recall_guard.py::test_tc6b_downstream_sink_location_present` passes and the test body does not import `_downstream_sql`
- `[ ]` `sqlcg git install-hooks` on a machine where both `~/.local/bin/sqlcg` and `.venv/bin/sqlcg` exist resolves to the `~/.local/bin/sqlcg` path
- `[ ]` `grep -n "cursor()" src/sqlcg/core/duckdb_backend.py` returns ≥1 result inside `run_read`
- `[ ]` Peak RSS at a 1,600-file corpus is measured, asserted < 3.8 GiB, and the figure is recorded in `ARCHITECTURE_REVIEW.md` §5 F3
- `[ ]` A second `sqlcg pr-impact --base <ref>` after a first run that advanced HEAD completes with a valid result, not a "please reindex" error
- `[ ]` `uv run pytest tests/unit/test_upsert_batch_invariant.py tests/unit/test_perf_scaling_guard.py` passes after ALL PRs in this sprint merge
