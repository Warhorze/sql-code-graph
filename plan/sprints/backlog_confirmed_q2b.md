# Backlog — Confirmed Q2-B Sprint Plan

**Status:** PR-1..PR-4 **REVIEWED** (plan-reviewer gate cleared 2026-06-14; amendments folded
in below). PR-5 and PR-6 are **DRAFT (needs plan-review)** — added 2026-06-14 from a fresh full
DWH reindex; they have **not** cleared the gate and do **not** ride the REVIEWED status.
Two items remain explicit **maintainer design decisions** (PR-1 shared-predicate placement;
PR-3 read-only-open viability) — flagged, not blocking; the developer must surface them, not
silently pick.
**Owner role:** architect-planner
**Baseline:** triaged against master `HEAD ~v1.28.4`; re-verified by plan-reviewer against
**current master `292da6a`** (PR #141 self-heal merge; `__version__ = "1.28.4"`). All four PR
gaps **still exist** on `292da6a` — nothing already shipped. See the *Plan-reviewer verdict*
block and *Anchor reconciliation* below; several cited line numbers drifted and are corrected.
**Scope:** four confirmed, evidence-backed PRs (PR-1..PR-4, **REVIEWED — frozen**) + two
DRAFT additions folded in 2026-06-14 from a fresh full DWH reindex (schema v9; SELECTS_FROM
6094; islands 147→43): **PR-5** (IA-DATAPRODUCTS coverage gap — now real, decision-gated) and
**PR-6** (EXECUTE IMMEDIATE dynamic-SQL string-literal blindspot). PR-5/PR-6 are
**Status: DRAFT (needs plan-review)** — they have NOT cleared the plan-reviewer gate and do
**not** inherit PR-1..PR-4's REVIEWED status. PR-2 carries one clarifying annotation
(viz-cleanliness, not lineage-island reduction) but stays REVIEWED.

---

## Plan-reviewer verdict (2026-06-14, current master `292da6a`)

**VERDICT: REVIEWED.** The plan is sound: every PR's gap was confirmed to **still exist** on
current master; no item is already-shipped. Path/line drift from the stale baseline is corrected
inline below. Acceptance criteria already encode the house rules. Two maintainer design decisions
remain (PR-1 predicate placement, PR-3 RO-open viability) — both already flagged with planner
recommendations; left to the maintainer/developer to settle, not gating the gate.

**Per-PR gap reconfirmation (file:line on `292da6a`):**

- **PR-1 (#40) - GAP EXISTS.** The golden BFS helpers query **raw `COLUMN_LINEAGE` with no
  `kind` predicate and no `SqlTable` join**: `_reachable_physical_leaves`
  [`tests/e2e/test_golden_lineage.py:97-98`](../../tests/e2e/test_golden_lineage.py),
  `_direct_sources` `:112-113`, `_table_blast_radius` `:154-155`, `_upstream_tables` `:182-183`.
  They filter only on **backup-name** globs via `_table_is_noise` (`:124-127`,
  `_DEFAULT_BACKUP_PATTERNS` `:121`) - that is *not* the `kind IN ('table','external')` CLI
  filter. `test_downstream_count_exact` asserts `== 2` at `:652`. The CLI predicate to mirror is
  the `kind_filter` string in [`analyze.py:30-37`](../../src/sqlcg/cli/commands/analyze.py)
  (`_upstream_sql`) and `:82-89` (`_downstream_sql`). **Note:** the brief's `_leaf_sources`
  function name no longer exists - it is `_reachable_physical_leaves`.
- **#142 coupling - CONFIRMED + DE-RISKED.** `transform` is **already a column on
  `COLUMN_LINEAGE` on current master** ([`duckdb_backend.py:170,:301`](../../src/sqlcg/core/duckdb_backend.py)),
  so PR-1's synthetic-`TEMP_INLINE` regression test is **insertable on master today** - it does
  **not** depend on #142 landing first. `feat/e8-dual-emission` is the branch that *emits*
  `transform='TEMP_INLINE'` edges (`coverage.py:380`, transform-aware upsert at `indexer.py:170-178`).
  Sequencing in *#40 dpos #142* stands: PR-1 must be green both on `292da6a` **and** on a local
  merge with `feat/e8-dual-emission`, and must merge with-or-before #142 so master never has an
  armed-and-red window.
- **PR-2 (#27a) - GAP EXISTS, premise CONFIRMED EXACTLY.** `get_noise_filter_patterns`
  ([`config.py:117-151`](../../src/sqlcg/core/config.py)) defaults exist and ARE wired - but
  **only at the read surface** ([`server/noise_filter.py:175`](../../src/sqlcg/server/noise_filter.py),
  table-NAME layer). Grep finds **no call in `indexer/` or `lineage/`** at file discovery. The
  walker globs `*.sql` (via `git ls-files`, rglob fallback at
  [`walker.py:50-52`](../../src/sqlcg/indexer/walker.py)) and applies only the `.sqlcgignore`
  spec; `load_ignore_spec` ([`utils/ignore.py:8-23`](../../src/sqlcg/utils/ignore.py)) has **no
  default patterns**. `load_ignore_spec` is called from [`indexer.py:461`](../../src/sqlcg/indexer/indexer.py),
  not the walker. So the missing piece is precisely a **file-discovery default-ignore**. Correct.
- **PR-3 (#63) - GAP EXISTS.** `get_backend(read_only=...)` still ignores the flag
  ([`config.py:351-366`](../../src/sqlcg/core/config.py), docstring says "Ignored for DuckDB").
  `duckdb.connect(db_path)` has **no `access_mode`** ([`duckdb_backend.py:332`](../../src/sqlcg/core/duckdb_backend.py));
  `IOException` re-raised `:333-347`. **Live failure path confirmed:** the no-server fallback at
  [`read_client.py:191`](../../src/sqlcg/server/read_client.py) calls `get_backend(read_only=True)`
  - which, because the flag is ignored, opens **read-write** and fails on a locked file. DuckDB
  pin is `>=1.0.0,<2.0` (`pyproject.toml:25`). The empirical-probe gate (below) stands.
- **PR-4 (#94) - GAP EXISTS.** Per-row BELONGS_TO loop confirmed at
  [`index.py:407-415`](../../src/sqlcg/cli/commands/index.py) (`for row in file_rows:
  backend.upsert_edge(... RelType.BELONGS_TO ...)`). `--profile` flag at `:62-63`; summary at
  `:476`; the catalog re-apply stage is not an instrumented bucket. Bulk target
  `upsert_edges_bulk` exists at [`duckdb_backend.py:574`](../../src/sqlcg/core/duckdb_backend.py)
  (grep-confirmed call site for the developer).
- **PR-5 - placeholder REPLACED (2026-06-14, DRAFT).** The fresh index resolved the verdict:
  the IA-DATAPRODUCTS gap is a **deliberate `.sqlcgignore` exclusion with a false rationale**,
  not an indexer-wiring gap. Now planned as 5a (correct the stale comment — ship) + 5b (restore
  coverage — gated on subprocess isolation, maintainer decision). **DRAFT, not yet reviewer-gated.**
- **PR-6 - NEW (2026-06-14, DRAFT).** EXECUTE IMMEDIATE dynamic-SQL string-literal blindspot
  (distinct from PR-5: file IS indexed, literal-wrapping defeats extraction). **DRAFT, not yet
  reviewer-gated.**

---

## House rules (binding on every PR in this plan)

These are acceptance preconditions, not suggestions. A PR is not "done" until all hold.

1. **Never tag or close.** The maintainer owns tagging and issue/PR closure. PRs ship
   the version bump on the branch; the merge + annotated tag is a maintainer step
   (see [`CLAUDE.md`](../../CLAUDE.md) §Releasing). Do **not** run `git tag` or `gh issue close`.
2. **The four perf-invariant suites must pass UNMODIFIED.** No edit, no skip, no slack
   raise to any of:
   - [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py)
   - [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py)
   - [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py)
   - [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)

   A red here means the change broke an invariant — fix the change, not the test.
3. **`git checkout -- plan/measurements/` before every commit.** The measurements dir is
   a scratch surface for running reindex/eval jobs; never commit churn from it.
4. **Metric-moving PRs emit `gain --json` to `plan/metrics/`.** Any PR that claims to
   move a coverage/island/perf number commits the supporting `gain --json` snapshot under
   [`plan/metrics/`](../metrics/) (not `plan/measurements/`, which stays scratch).
5. **Every new method has a grep-confirmed call site before the PR opens** (CLAUDE.md).
6. **Version bump per CLAUDE.md SemVer:** new capability/surface → **minor**;
   bug-fix-only → **patch**. Bump `pyproject.toml`, `src/sqlcg/__init__.py`, run `uv lock`.

---

## Anchor reconciliation (read before implementing — paths drifted from the brief)

The triage brief cited an older master. Re-verification in the working tree found these
deltas. **Use the verified column.** This does not change the findings — only the paths —
but a stale path will send a developer to the wrong file.

| Item | Brief said | Verified (this tree) | Note |
|------|-----------|----------------------|------|
| PR-1 golden test | `tests/integration/test_golden_lineage.py` | [`tests/e2e/test_golden_lineage.py`](../../tests/e2e/test_golden_lineage.py) | moved to `e2e/` |
| PR-1 CLI kind-filter | `src/sqlcg/cli/analyze.py:35,:87` | [`src/sqlcg/cli/commands/analyze.py:30-37`](../../src/sqlcg/cli/commands/analyze.py) (upstream) and `:82-89` (downstream) | now under `commands/`; the `kind IN ('table','external')` filter is the `kind_filter` string built in both `_upstream_sql` and `_downstream_sql` |
| PR-2 ignore loader | `src/sqlcg/core/ignore.py:8-23` | [`src/sqlcg/utils/ignore.py:8-23`](../../src/sqlcg/utils/ignore.py) | moved to `utils/`; `load_ignore_spec` still has **no** default patterns |
| PR-2 noise filter | `src/sqlcg/core/config.py:134-139` | [`src/sqlcg/core/config.py:117-139`](../../src/sqlcg/core/config.py) `get_noise_filter_patterns` | defaults exist (`*_bck`, `*_bck_*`, `*_bck_us`, `*_bck_[0-9]*`, `*_backup`, `*_backup_[0-9]*`) **but have no indexer call site** — confirms the "different layer" claim |
| PR-3 / PR-4 | as cited | confirmed verbatim | `config.py:351-366`, `duckdb_backend.py:332,:333-346`, `index.py:62`, `index.py:407-415` all match |

> **Reviewer flag (PR-1, PR-2):** This tree already contains a kind-filter in
> `analyze.py` and the backup-pattern defaults in `config.py`, *and* the golden test
> already defines a `_DEFAULT_BACKUP_PATTERNS` harness mirror at
> [`test_golden_lineage.py:119-127`](../../tests/e2e/test_golden_lineage.py). It is unclear
> whether part of PR-1/PR-2 already landed on this branch or whether this is divergent
> WIP. **A `git log` provenance check on these two files is a prerequisite the
> plan-reviewer must clear before a developer starts** — otherwise PR-1/PR-2 risk
> re-implementing shipped code. Findings below are written assuming the brief's master
> baseline; if the filter/defaults are already merged, the PRs collapse to the
> *test-traversal* and *indexer-wiring* deltas respectively.

---

# PR-1 (#40) — Golden/anchor lineage guards bypass the CLI kind-filter → armed regression vs OPEN PR #142

**Rank: 1 (URGENT — coupled to OPEN PR #142). Version bump: PATCH (test-only fix; no shipped surface change).**

## Problem
The golden and live-anchor lineage tests traverse the **raw** `COLUMN_LINEAGE` table
directly, bypassing the kind-filter (`kind IN ('table','external')`) that the CLI/MCP
read surface applies. They assert exact counts and a precision/recall floor against that
raw edge set. PR #142 (E8 dual-emission, **OPEN**) adds `transform='TEMP_INLINE'` edges
into that same raw layer. Those new edges land in the test's BFS frontier but **not** in
the filtered surface real users query → the exact-count and floor guards flip red the
moment #142 merges. The guards are an armed regression against unmerged work.

## Root cause (file:line — verified)
- Raw, unfiltered BFS over `COLUMN_LINEAGE` (reviewer-verified on `292da6a`):
  [`tests/e2e/test_golden_lineage.py:97-98`](../../tests/e2e/test_golden_lineage.py)
  (`_reachable_physical_leaves` - the brief's `_leaf_sources` was **renamed**), `:112-113`
  (`_direct_sources`), `:154-155` (`_table_blast_radius` downstream), `:182-183`
  (`_upstream_tables`). None join `SqlTable` / apply a `kind` predicate; they filter ONLY on
  backup-name globs via `_table_is_noise` (`:124-127`), which is orthogonal to the kind-filter.
- Exact-count anchors: `:338` and `:360` (per-statement `precision=1.0 if match`),
  and `test_downstream_count_exact` at [`:643-652`](../../tests/e2e/test_golden_lineage.py)
  asserting `count == 2` ("etl, mart").
- Precision/recall floors: `PRECISION_FLOOR`/`RECALL_FLOOR = 0.8` at `:45-46`, enforced
  at `:457-460`.
- Live anchors over raw `COLUMN_LINEAGE`:
  [`tests/integration/test_live_anchors.py:62-72`](../../tests/integration/test_live_anchors.py),
  `:92-102`, `:115-125`.
- The filter that *should* be mirrored: the `kind_filter` string in
  [`src/sqlcg/cli/commands/analyze.py:30-37`](../../src/sqlcg/cli/commands/analyze.py)
  (`_upstream_sql`) and `:82-89` (`_downstream_sql`):
  `LEFT JOIN "SqlTable" t ON t.qualified = dr.table_qualified WHERE t.kind IS NULL OR t.kind IN ('table','external')`.

## Proposed fix — option evaluation (maintainer design decision flagged)
Three options from the brief, evaluated:

- **(a) Route the test BFS through the same kind-filter/view the CLI uses.** *Strongest.*
  Makes the guard exercise the real read surface, so a future filter regression is also
  caught. Cost: the test traversal is hand-rolled recursive Python over `run_read`, not the
  CLI's recursive-CTE SQL — mirroring the filter means either (i) joining `SqlTable` +
  `kind` predicate inside the test's `run_read` queries, or (ii) calling the actual
  `_upstream_sql`/`_downstream_sql` builders. (ii) is best (single source of truth) but the
  test BFS rolls columns→tables differently than the CTE; needs care that semantics match.
- **(b) Exclude `transform='TEMP_INLINE'` in the test traversal.** *Narrow band-aid.* Fixes
  #142 specifically but does not make the guard exercise the filter layer — the next
  transform kind re-arms the same trap. Rejected as the primary fix.
- **(c) Make the guards assert on the filtered surface, not raw edges.** Equivalent to (a)
  in intent; this is the *assertion-target* framing of the same change.

**Recommendation: (a)/(c) combined** — traverse and assert on the filtered surface by
sharing the CLI filter predicate (prefer reusing the `analyze.py` query builders, or at
minimum a single shared `_KIND_FILTER_SQL` constant imported by both so they cannot drift).
This keeps the guards *meaningful* (counts not loosened — they still assert `== 2`, floor
`≥ 0.8`) while making them pass with #142's `TEMP_INLINE` edges present.

> **Maintainer design decision flagged:** do we (i) extract the kind-filter into a shared
> constant/helper consumed by both `analyze.py` and the test, or (ii) duplicate the
> predicate in the test? (i) is DRY and prevents drift but touches shipped code (would push
> the bump to *minor* if it adds a public surface; keep it module-private to stay patch).
> (ii) is test-only but can silently diverge. Planner recommends (i) **module-private**.

## Acceptance criteria
- Golden + live-anchor BFS traverses the kind-filtered edge set (joins `SqlTable`,
  applies `kind IS NULL OR kind IN ('table','external')`), via the shared CLI predicate.
- Guards remain meaningful: `test_downstream_count_exact` still asserts `== 2`;
  `PRECISION_FLOOR`/`RECALL_FLOOR` stay `0.8` (not loosened).
- New regression test: with a synthetic `transform='TEMP_INLINE'` edge inserted, the
  filtered traversal **excludes** it and the count/floor assertions still pass - proving the
  guard now exercises the filter layer (assert observable output, not "no exception").
  **Reviewer note:** `transform` is already a `COLUMN_LINEAGE` column on master
  (`duckdb_backend.py:170,:301`), so this regression test is writable **against `292da6a`
  alone** - it does NOT block on #142 merging. (#142 is only needed for the *local-merge*
  green check below.)
- The four perf-invariant suites pass **unmodified**.
- `rtk uv run pytest tests/e2e/test_golden_lineage.py tests/integration/test_live_anchors.py`
  green both with and without a simulated #142 edge set.
- `rtk uv run pyright` and `rtk uv run ruff check src tests` clean.

## Risk
- Semantic mismatch between the hand-rolled BFS and the CTE filter could *change* which
  tables are counted → mask a real coverage loss. Mitigate by asserting the count is
  unchanged on the current corpus (still `== 2`) before declaring done.
- If option (i) extracts shared code into `analyze.py`, watch the bump rule (keep private).

## Dependency / sequence
- **Coupled to OPEN PR #142.** See *#40 ↔ #142 sequencing* below. **No other PR depends on PR-1.**
- Metric-moving? No (test-only). No `gain --json` required unless counts shift.

---

# PR-2 (#27a) — Backup/snapshot files not excluded at file discovery → orphan island-noise nodes

**Rank: 2 (small). Version bump: MINOR (new configurable default ignore surface).**

## Problem
Backup/snapshot SQL artifacts (`*_bck`, `_eenmalig_<timestamp>`, dated suffixes) are
walked and indexed, creating orphan "island" nodes that inflate the graph and dilute
island-noise metrics. The existing backup-table machinery is a *table-name* noise filter
applied (if at all) at a different layer than file discovery — and crucially has **no
indexer call site**, so nothing currently excludes these at discovery time.

> **Impact clarification (fresh-index annotation, 2026-06-14 — REVIEWED PR, annotation only):**
> The fresh full reindex shows the 123 backup/snapshot nodes (`_eenmalig_`/`_bck`) inflate the
> **deg-0 SINGLETON visualization mass**, **not** the lineage-island *count*: only 4 of them sit
> in lineage islands, and **0 remain after the `_bck` filter**. So PR-2's true impact is
> **VIZ-CLEANLINESS** — shrinking the ~3,700 deg-0 singleton "sea" the maintainer eyeballs — not
> lineage-island reduction. **Acceptance should therefore measure deg-0 singleton-node reduction
> in the viz, not island-count delta** (the island-count line in the criteria below reads against
> the singleton mass, not the 43-island lineage set). Status stays REVIEWED; this is a scope-of-
> impact clarification, not a plan change.

## Root cause (file:line — verified)
- Walker discovers files via `git ls-files` (tracked `*.sql`) with an `rglob("*.sql")`
  **fallback at [`walker.py:50-52`](../../src/sqlcg/indexer/walker.py)**, applying only the
  `.sqlcgignore` spec (`is_ignored` at `:46,:51`). **Reviewer correction:** the brief's
  `:54 root.rglob` / `:55` lines drifted - the walker was rewritten to a git-first path; the
  `spec` it receives comes from `load_ignore_spec(path)` called in
  **[`indexer.py:461`](../../src/sqlcg/indexer/indexer.py)** (not inside the walker). The
  file-discovery default-ignore must seed that `spec` (option A) or filter in the walker.
- `load_ignore_spec` has **no default patterns** — it reads `.sqlcgignore` or returns an
  empty spec: [`src/sqlcg/utils/ignore.py:8-23`](../../src/sqlcg/utils/ignore.py).
- `get_noise_filter_patterns` (backup globs) lives at
  [`src/sqlcg/core/config.py:117-139`](../../src/sqlcg/core/config.py) — this is a
  **table-NAME** noise layer, and grep finds **no call site in `indexer/` or `lineage/`**.
  Confirms the brief: a different, currently-unwired layer.

## Proposed fix
Add a **conservative default file-discovery ignore** for backup/snapshot artifacts.
Two placement options:

- **(A) Default patterns in `load_ignore_spec`** (`utils/ignore.py`): seed the PathSpec with
  built-in defaults (e.g. `*_bck.sql`, `*_bck_*.sql`, `*_backup*.sql`, `*_eenmalig_*.sql`,
  dated `*_[0-9]{8}*.sql`) that the user's `.sqlcgignore` can extend/override. *Excludes at
  discovery — cheapest, never parses the file.*
- **(B) Table-name exclusion at node creation** by finally wiring `get_noise_filter_patterns`
  into the indexer. *Catches backup tables that live inside otherwise-legitimate files.*

**Recommendation:** (A) as the primary (matches the issue: "*file* discovery"), with the
default list **configurable** (a `.sqlcgignore` entry or a `[sqlcg.noise_filter]` toggle to
disable). Keep the pattern conservative — anchor on clear backup markers (`_bck`, `_backup`,
`_eenmalig_<timestamp>`), **not** bare date suffixes that could hit legitimate dated marts.
(B) can be a follow-up if file-level exclusion proves insufficient; do not bundle both
unless the running reindex (below) shows file-level alone misses material noise.

> **Maintainer design decision flagged:** the exact default glob set. Too aggressive risks
> dropping legitimate tables (the brief's explicit "do NOT exclude legitimate tables"
> constraint). Planner proposes shipping only `_bck`/`_backup`/`_eenmalig_<ts>` markers in
> v1 and gating dated-suffix exclusion behind explicit opt-in.

## Acceptance criteria
- A **configurable** default file-discovery ignore for backup/snapshot patterns; user can
  override/disable via `.sqlcgignore` / config.
- Quantified island-noise reduction: before/after orphan-node count on the DWH corpus,
  with the exact noise-node count referenced from the **separate running reindex**
  (a reindex is currently measuring this; cite its output under `plan/measurements/` once
  it lands — do **not** commit that scratch file).
- Legitimate tables provably untouched: a test fixture with a legit dated mart name asserts
  it is **not** excluded (observable assertion).
- Unit test: walker yields the expected file set with/without the default ignore active.
- `gain --json` snapshot committed to [`plan/metrics/`](../metrics/) showing the
  island-noise delta (this PR moves a metric).
- Four perf-invariant suites pass unmodified; `pyright` + `ruff` clean.

## Risk
- Over-broad glob silently drops real lineage → false "clean graph". Mitigated by the
  conservative pattern set + the legit-table negative test + the configurable override.

## Dependency / sequence
- Independent of PR-1/PR-3. Metric-moving → `gain --json` required.

---

# PR-3 (#63) — DuckDB MCP server takes exclusive R/W lock; 2nd `mcp start` / health-probe always fails

**Rank: 3 (bug). Version bump: PATCH (bug fix; no new user-facing surface).**

## Problem
The DuckDB backend opens the DB file as a single exclusive read/write connection at server
startup. A second process opening the same file — e.g. `claude mcp list`'s health-probe
spawning a 2nd `mcp start` — hits a DuckDB `IOException` and fails hard, with no graceful
message. The `read_only` flag that should allow concurrent readers is accepted but ignored.

## Root cause (file:line — verified)
- `get_backend(read_only=...)` **ignores** the flag:
  [`src/sqlcg/core/config.py:351-366`](../../src/sqlcg/core/config.py) — docstring at
  `:354,:366` literally says "Ignored for DuckDB. Accepted for API compatibility."
- `duckdb.connect(db_path)` opens a single R/W handle with **no `access_mode`**:
  [`src/sqlcg/core/duckdb_backend.py:332`](../../src/sqlcg/core/duckdb_backend.py); the
  resulting `IOException` is caught and re-raised at `:333-346`.
- Existing mitigation: CLI reads route via the server socket
  ([`src/sqlcg/server/read_client.py`](../../src/sqlcg/server/read_client.py), header
  `:1-20`), but that only helps *CLI* reads — a genuine 2nd process opening the file still
  cannot.

## Proposed fix — option evaluation
- **(1) Honor `read_only` via `access_mode='READ_ONLY'`** in `duckdb.connect` when the flag
  is set, and stop lying in the `get_backend` docstring. Lets a health-probe / 2nd reader
  open the file concurrently with the single writer (DuckDB supports 1 writer + N readers
  only across *separate* processes when the writer is also read-only, so verify the
  concurrency matrix — DuckDB single-file is single-writer-process; a R/O second opener
  works only if the **writer has not** taken an exclusive lock). **This needs a real probe.**
- **(2) "Server already running" pre-check on `mcp start`** — detect a live server (the
  existing socket probe) and exit gracefully with a clear message instead of attempting a
  conflicting open.

**Recommendation:** ship **both**: (2) is the correct UX for the health-probe case (don't
start a 2nd server at all — report the running one) and is low-risk; (1) makes genuine
concurrent *read-only* opens succeed where DuckDB permits. Critically, **must not regress
the single-writer drain model** — the writer process keeps its R/W handle; only *additional*
openers go read-only or are short-circuited by the pre-check.

> **Maintainer design decision flagged:** DuckDB's concurrency model means a 2nd R/O open
> can still fail if the writer holds an exclusive lock (file-level). The fix may therefore
> be primarily **(2) graceful pre-check + clear error**, with (1) as a best-effort
> read-only path only when no live writer is detected. A short empirical probe (open writer,
> then attempt R/O open) should settle which path actually unblocks the health-probe
> **before** committing to (1). Capture that probe result in `plan/measurements/` (scratch).

## Acceptance criteria
- A 2nd connection / health-probe either **succeeds** (read-only) or **fails gracefully with
  a clear, actionable message** (not a raw `IOException` stack).
- **Conditional on the empirical probe (reviewer gate):** *only if* the probe (open a live
  R/W writer, then attempt a 2nd R/O open of the same file under DuckDB `>=1.0,<2.0`) shows a
  2nd R/O open **succeeds**, then `get_backend(read_only=True)` passes
  `access_mode='READ_ONLY'` (docstring no longer claims the flag is ignored), with a test
  asserting the connect kwarg. **If the probe shows the writer's lock blocks a 2nd open**
  (the likely DuckDB single-file outcome), drop option (1) and ship **only** option (2) - the
  graceful "server already running" pre-check - as the real fix; do not commit a dead
  `access_mode` path. Capture the probe result in `plan/measurements/` (scratch, not committed).
  **Live failure site:** [`read_client.py:191`](../../src/sqlcg/server/read_client.py)
  already requests `read_only=True` in the no-server fallback and silently gets R/W today.
- No regression to the single-writer drain model: the writer server still opens R/W and the
  socket-routed CLI read path still works (existing `read_client` tests stay green).
- Integration test simulating a 2nd `mcp start` against a live server asserts the graceful
  outcome (observable message or successful R/O open) — not "no exception".
- Four perf-invariant suites pass unmodified; `pyright` + `ruff` clean.

## Risk
- Mis-modeling DuckDB locking → "fix" that still fails under the real probe. Mitigated by
  the empirical probe gating option (1) and by shipping (2) as the guaranteed-safe UX.
- Read-only openers seeing a stale snapshot vs the writer's uncommitted state — acceptable
  for a health-probe; document it.

## Dependency / sequence
- Independent of PR-1/PR-2. Touches the server start path; sequence before PR-4 only because
  PR-4's profiling may exercise the server index path. Not metric-moving (no `gain --json`).

---

# PR-4 (#94) — Indexing perf: per-row BELONGS_TO, un-profiled catalog re-apply, catalog insert cost

**Rank: 4 (perf — sequence LAST). Version bump: PATCH (perf fix; `--profile` extension is
an additive flag-behavior, but no new command/surface — keep patch unless a new flag is added).**

## Problem
Three coupled perf sub-items in the post-rebuild index path:
1. **BELONGS_TO is written via a per-row loop** with no bulk path — the same regression class
   the perf invariants exist to prevent, just on a relation they don't yet cover.
2. **`--profile` does not instrument the ~75s catalog re-apply stage** — a profiling blind
   spot, so the slowest stage is invisible in the breakdown.
3. **Catalog re-apply genuine insert cost is unaddressed** — even with bulk writes, the
   re-apply itself is expensive and uncharacterized.

## Root cause (file:line — verified)
- Per-row BELONGS_TO loop:
  [`src/sqlcg/cli/commands/index.py:407-415`](../../src/sqlcg/cli/commands/index.py) —
  `for row in file_rows: backend.upsert_edge(... RelType.BELONGS_TO ...)` (single-edge
  upsert per file, post-rebuild Repo-wiring).
- `--profile` flag exists but covers only per-stage timing it already wraps:
  [`src/sqlcg/cli/commands/index.py:62`](../../src/sqlcg/cli/commands/index.py)
  (`--profile/--no-profile`), summary rendered at `:476-488`; the catalog re-apply stage is
  not in the instrumented `prof` buckets.

## Proposed fix
1. **Bulk BELONGS_TO upsert:** replace the per-row loop with the existing
   `upsert_edges_bulk` path (the same Phase-C bulk pattern the perf invariants mandate for
   `_upsert_parsed_file`). Grep-confirm the bulk method's signature/call site before opening.
2. **Extend `--profile` to instrument the catalog re-apply stage:** add a timing bucket so
   the ~75s stage shows in the breakdown. This is the *measurement* deliverable — it makes
   sub-item 3 characterizable.
3. **Characterize the catalog insert cost** using the now-visible profile bucket; decide
   (in a follow-up if needed) whether a bulk catalog-insert path is warranted. Do **not**
   speculatively rewrite the catalog path in this PR beyond bulk BELONGS_TO + instrumentation
   unless the profile shows a clear, safe win.

## Acceptance criteria
- `--profile` output includes the **catalog re-apply stage** as its own bucket (observable in
  the rendered summary at `index.py:476-488`).
- BELONGS_TO uses a **bulk upsert** (`upsert_edges_bulk`, confirmed at
  [`duckdb_backend.py:574`](../../src/sqlcg/core/duckdb_backend.py)) replacing the per-row
  `upsert_edge` loop at [`index.py:407-415`](../../src/sqlcg/cli/commands/index.py); grep-confirmed
  call site before the PR opens.
- **Bump tripwire (reviewer):** extending the *existing* `--profile` flag to add a catalog
  bucket stays **patch**. If the PR instead adds a **new** `--profile-catalog` flag (a new
  user-facing surface), the bump becomes **minor** per CLAUDE.md SemVer - pick one and bump
  accordingly; do not ship a new flag under a patch bump.
- The 3-minute full-corpus budget is **measured via the profile breakdown**, not raw
  wall-clock seconds — *this box is a 3.8GB-RAM machine; judge by stage breakdown +
  `test_perf_scaling_guard`, not absolute seconds.*
- **`test_perf_scaling_guard` (and the other three invariant suites) pass UNMODIFIED** — the
  BELONGS_TO bulk change must not regress the op-count scaling; if it touches `indexer.py`,
  re-confirm all bulk/batch invariants.
- `gain --json` snapshot committed to [`plan/metrics/`](../metrics/) with the profile
  breakdown (metric-moving PR).
- `pyright` + `ruff` clean.

## Risk
- Bulk BELONGS_TO touching the indexer write path could trip a perf invariant — that is
  exactly what the four suites guard; treat any red as a real regression.
- Profiling the catalog stage must not itself add hot-loop overhead (cheap timers only).

## Dependency / sequence
- **Sequence LAST.** Benefits from PR-3 (stable server start) when profiling the
  server-routed index path. Metric-moving → `gain --json` required.

---

# PR-5 (#NEW) — IA-DATAPRODUCTS coverage gap: deliberate `.sqlcgignore` exclusion with a stale/incorrect rationale

**Status: DRAFT (needs plan-review).** Does **not** inherit PR-1..PR-4's REVIEWED status.
**Rank: deferred (decision-gated). Two parts: 5a ships now, 5b is gated.**
**Version bump:** 5a → **patch** (config-comment correction, effectively docs-only);
5b → deferred, not shippable now (see gate).

## Problem
The 113 tracked dynamic-table files under `ddl/changelogs/IA-DATAPRODUCTS/` are dropped at
**file discovery — before parsing** — by a `.sqlcgignore` rule. Result on the fresh index:
**124 `ia_dataproducts.*` catalog-only orphan nodes** (empty `defined_in_file`, 0 producing
query, 0 edges). The exclusion is **deliberate** (a perf workaround), but its rationale comment
is **factually wrong** and actively misleading.

## Root cause (file:line — verified, fresh index schema v9)
- **The exclusion rule:** [`/home/ignwrad/Projects/dwh/.sqlcgignore`](file:///home/ignwrad/Projects/dwh/.sqlcgignore)
  contains `ddl/changelogs/IA-DATAPRODUCTS/` (line 5). Its comment (lines 1-4) claims the
  source tables are *"absent from schema (E5), so zero lineage edges are produced anyway."*
- **Honored at discovery, before any parse:** `load_ignore_spec`
  ([`indexer.py:461`](../../src/sqlcg/indexer/indexer.py)) → `walk_sql_files`
  ([`indexer.py:469`](../../src/sqlcg/indexer/indexer.py)) → `is_ignored`
  ([`walker.py:50`](../../src/sqlcg/indexer/walker.py)) → `spec.match_file`
  ([`utils/ignore.py:26-38`](../../src/sqlcg/utils/ignore.py)). All 113 files are excluded
  **before parsing** → the parser never sees them; the 124 nodes exist only as catalog refs
  from downstream consumers.
- **The rationale is FALSE.** The IA-DATAPRODUCTS files read `FROM IA_SEMANTIC."..."`, and
  **`ia_semantic` IS fully indexed (187 files)** on the fresh index. So the sources are
  **present**, not "absent from schema" — un-ignoring would connect at least at table level
  (table-grain SELECTS_FROM), contradicting "zero edges produced anyway." The real reason for
  the exclusion is **indexing cost** (the COMBI/AGG dynamic tables are 100+ cols × 10+ FULL
  OUTER JOINs; sqlglot lineage is O(cols × joins) per file) — a **PERF workaround pending
  subprocess isolation** (`ARCHITECTURE_REVIEW.md` §14.5), **not** an extraction failure.

## Plan — two-part, decision-gated

### 5a — CHEAP NOW (ship): correct the stale/incorrect `.sqlcgignore` rationale
The comment misleads any future reader into believing coverage is impossible ("absent from
schema"). It is a perf gate, not a correctness gate. **Patch the comment** in
`/home/ignwrad/Projects/dwh/.sqlcgignore` to state the accurate reason:
- sources (`ia_semantic`, 187 files) **are** indexed; un-ignoring **would** produce ≥ table-grain
  edges;
- the exclusion exists purely as a **perf workaround** for the O(cols × joins) cost on the
  COMBI/AGG dynamic tables, **pending subprocess isolation** (`ARCHITECTURE_REVIEW.md` §14.5);
- cross-reference 5b below as the revisit trigger.

This is effectively docs-only (a corpus config file in the `dwh` repo, not `sqlcg` source).
**No `sqlcg` code, no version bump on `sqlcg` for 5a** beyond recording the corrected rationale.
No metric moves (still ignored → 124 orphans unchanged).

### 5b — GATED (do NOT plan as shippable now): restore coverage by un-ignoring the 113 files
Un-ignoring is **blocked on the subprocess-isolation perf work** (`ARCHITECTURE_REVIEW.md`
§14.5). These files were excluded for indexing cost; restoring them re-introduces the
O(cols × joins) blow-up the workaround was created to avoid. **Frame as "revisit after
subprocess isolation lands."** Do not plan the un-ignore as shippable in this sprint.

**Estimated connectivity gain (for the decision, fresh index):** un-ignoring closes the
**124 `ia_dataproducts.*` catalog-only orphan nodes** (empty `defined_in_file`, 0 edges today)
by giving them producing queries + ≥ table-grain SELECTS_FROM into `ia_semantic` — pulling them
(and their downstream consumers) **into the giant component**. This is the headline gain to
weigh against the perf cost.

> **MAINTAINER DECISION (priority, gates 5b):** after subprocess isolation lands, do we
> **restore IA-DATAPRODUCTS coverage** (recover the 124 orphan nodes into the giant, accepting
> the dynamic-table indexing cost now bounded by isolation) **or keep the perf workaround**
> (leave them ignored)? This is a priority/cost trade-off the maintainer owns; 5b is not
> actionable until subprocess isolation exists. 5a (the comment fix) is independent of this
> decision and ships regardless.

## Acceptance criteria
- **5a:** the `.sqlcgignore` comment no longer claims "absent from schema"; it states the
  accurate perf-workaround rationale + the `ia_semantic`-is-indexed fact + the §14.5 revisit
  trigger. No `sqlcg` source change; no metric delta (124 orphans intentionally unchanged).
- **5b:** **not in scope this sprint** — blocked on subprocess isolation. When revisited:
  un-ignoring drops the 124 orphan nodes to 0 and produces ≥ table-grain edges into
  `ia_semantic`; `gain --json` snapshot to [`plan/metrics/`](../metrics/) showing the orphan
  delta; **all four perf-invariant suites pass unmodified AND the per-file dynamic-table index
  cost stays within the isolation-bounded budget** (the gate's whole point).

## Dependency / sequence
- **5a:** independent, shippable immediately (corpus-config patch). Not metric-moving.
- **5b:** **blocked** on subprocess isolation (`ARCHITECTURE_REVIEW.md` §14.5); maintainer
  priority decision required before it is scheduled. Metric-moving when it lands.

---

# PR-6 (#NEW) — Dynamic-SQL lineage recovery: unwrap static-literal EXECUTE IMMEDIATE, then compose the intermediate via #142

**Status: DRAFT (needs plan-review).** Does **not** inherit PR-1..PR-4's REVIEWED status.
**Rank: TBD (set by plan-reviewer). Version bump: MINOR (new extraction surface).**
**Shape: ONE PR, two phases (re-scoped 2026-06-14).** **Depends on #142 (E8 dual-emission,
`feat/e8-dual-emission`) — sequence PR-6 AFTER #142 merges.**

## Framing — same shape, reuse #142's machinery
The maintainer's design call: dynamic-SQL extraction and "pick up the intermediate" are the
**same shape** — *lineage hidden behind an indirection → recover it, then thread through the
intermediate*. #142 **already built the composition engine** (`_emit_transitive_temp_edges` +
`transform='TEMP_INLINE'` provenance, `feat/e8-dual-emission`). So PR-6 is re-scoped as a
**single PR with two phases that REUSE #142's machinery** rather than building a parallel
mechanism:
- **Phase 1 (new work):** unwrap a static-literal `EXECUTE IMMEDIATE` DDL so the inner CREATE
  becomes a normal parsed statement in the file.
- **Phase 2 (reuse, likely free):** whatever Phase 1 surfaces — especially TEMP tables created
  *inside* the dynamic SQL — is composed through by the **existing** #142 dual-emission pass,
  not a new one.

This is **distinct from PR-5**: PR-5's files are *excluded* before parsing (`.sqlcgignore`);
PR-6's file *is* parsed but the literal-wrapping defeats extraction.

## Root cause (file:line — verified, fresh index)
- Confirmed on `ba.wtfa_artikel_formule_dagomzet`, file
  [`/home/ignwrad/Projects/dwh/ddl/changelogs/BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql`](file:///home/ignwrad/Projects/dwh/ddl/changelogs/BA-DYNAMIC-TABLES/WTFA_ARTIKEL_FORMULE_DAGOMZET.sql).
  The `CREATE OR REPLACE DYNAMIC TABLE BA.WTFA_ARTIKEL_FORMULE_DAGOMZET ... AS WITH ... SELECT`
  lives inside the `ddl_statement := '...'` string literal (~lines 12-46), executed at
  `EXECUTE IMMEDIATE (:ddl_statement);` (line 48). The parser sees only a variable assignment +
  an EXECUTE IMMEDIATE call, never the CREATE/SELECT. Index result: indexed, `parse_failed=False`,
  **0 SqlQuery / 0 edges**. Extracting it pulls a **4-node island (the table + its 3 IA views)**
  into the giant.

## (a) Prevalence — MEASURED FIRST (sizes the lever; honest ROI)
Grep over the corpus [`/home/ignwrad/Projects/dwh`](file:///home/ignwrad/Projects/dwh):
- **21 files** total use `EXECUTE IMMEDIATE`.
- **Phase-1 recoverable = exactly 1 file today.** Only the WTFA dynamic table matches the
  in-scope pattern `var := '<static CREATE...AS...SELECT>'; EXECUTE IMMEDIATE` (a static literal
  CREATE-DDL string). Grep: `:= 'CREATE ...` → 1 hit; that hit contains `DYNAMIC TABLE`.
- **Out of scope (the other 20):**
  - 1 file (`MA-PROCEDURES/MSSPR_COMPARE_DATA.sql`) does `EXECUTE IMMEDIATE 'CREATE OR REPLACE
    TEMP TABLE ... AS SELECT * FROM (' || :sql_source || ')'` — **concatenated with bind
    variables** (`||`, `:sql_source`) and targeting transient diagnostic `TEMP` tables, **not**
    static. Note: this is the *shape* Phase 2 generalizes (temp-inside-dynamic-SQL), but this
    specific file is **not Phase-1-recoverable** because its body is concatenated, not a static
    literal — it stays out of scope.
  - The rest assign via `CONCAT(...)` / `||` concatenation or are non-CREATE statements (ALTER
    external-table refresh, INFORMATION_SCHEMA selects, MERGE/UPDATE plumbing).
- **Honest ROI:** the immediate lever is **small — 1 file, 1 table, a 4-node island today**.
  Phase 2 adds **zero islands today** (the one temp-inside-dynamic-SQL file is concatenated, so
  Phase 1 can't unwrap it). The value is **structural, not volumetric**: (i) Phase 1's
  static-literal-CREATE is a recurring authoring class (Snowflake dynamic-table-via-EXECUTE-
  IMMEDIATE) likely to grow; (ii) Phase 2 is near-free (reuses #142) and future-proofs the
  temp-inside-dynamic-SQL case for when a *static-literal* instance appears. Plan-reviewer
  should rank "1 island now + new extraction surface for a growing pattern, Phase 2 ~free"
  against the higher-volume PR-2 / PR-4 levers — this is a low-immediate-yield, low-cost,
  structurally-strategic PR.

## (b) Phase 1 — unwrap dynamic SQL (the new work)
Detect an `EXECUTE IMMEDIATE` of a **STATIC literal** DDL — `CREATE [OR REPLACE] [DYNAMIC]
TABLE … AS SELECT …` held either in a `ddl_statement := '<literal>'` assignment immediately
feeding `EXECUTE IMMEDIATE (:ddl_statement)` or as an inline literal `EXECUTE IMMEDIATE
'<literal>'`. Extract the inner SQL string, **re-parse it as a nested statement**, and attribute
the producing-query + source edges to the produced table.
- **In scope:** static literal DDL only — a single string literal, or a single `var :=
  '<literal>'` assignment with **no** concatenation in the lineage-bearing body, fed to
  `EXECUTE IMMEDIATE`.
- **Out of scope (explicit, do not attempt):** concatenated / bind-variable dynamic SQL
  (`|| :var ||`, `CONCAT(...)`, bind-variable interpolation `:sql_source`). Only **static-literal**
  DDL is statically recoverable; anything assembled at runtime cannot be parsed and must be
  **skipped, not guessed**.
- **WAREHOUSE-clause concatenation (the WTFA nuance):** the WTFA literal contains one
  `|| $WAREHOUSENAME ||` concatenation **in the WAREHOUSE clause only** — a **non-lineage**
  clause. The `AS WITH ... SELECT` body is fully static. Design: extract the static literal body
  and substitute a **placeholder** for the runtime `$WAREHOUSENAME` fragment in the WAREHOUSE
  clause (the WAREHOUSE clause carries no lineage, so a placeholder is lossless for graph
  purposes). A concatenation **inside the `AS SELECT` body** would, by contrast, make the file
  non-recoverable → skip.
- **Session-context inheritance:** the inner SQL must inherit the enclosing file's `USE SCHEMA` /
  session context (WTFA sets `USE SCHEMA BA;` at line 5) so the extracted CREATE resolves to
  `BA.WTFA_ARTIKEL_FORMULE_DAGOMZET` and its `FROM ba.wtfe_verkoopinfo` / `ba.wtda_datum` sources
  resolve. **Reuse the existing session-context mechanism** (`SnowflakeParser`'s `USE SCHEMA`
  tracking / `_transform_statements` per-file context — [`ansi_parser.py:137-143`](../../src/sqlcg/parsers/ansi_parser.py)),
  do not invent a parallel one. The cleanest landing is to have the unwrapped inner statement(s)
  join the same `statements` list the per-file loop iterates, so they pass through
  `_transform_statements` and the normal `_parse_statement` path with the file's context already
  applied.

## (c) Phase 2 — pick up the intermediate (REUSE #142, verified ~free)
Whatever Phase 1's unwrapped body surfaces — especially TEMP tables created **inside** the
dynamic SQL (the `EXECUTE IMMEDIATE 'CREATE … TEMP TABLE …'` pattern, e.g. the shape in
`MA-PROCEDURES/MSSPR_COMPARE_DATA.sql`) — must be composed through using the **existing #142
dual-emission engine** (`_emit_transitive_temp_edges`, the `(src,dst,col)` ledger,
`transform='TEMP_INLINE'`), **not a new mechanism**: keep the intermediate temp node visible
AND emit the transitive source→sink edge.

**Design-point verification — is Phase 2 FREE? YES (composition already applies).** Verified by
reading the engine and the parse pipeline on `feat/e8-dual-emission`:
- `_emit_transitive_temp_edges` is defined at
  [`ansi_parser.py:273`](../../src/sqlcg/parsers/ansi_parser.py) and is invoked
  **unconditionally at [`ansi_parser.py:264`](../../src/sqlcg/parsers/ansi_parser.py)**, i.e.
  **after the per-statement loop** (loop at `:206-254`), over `out.statements`.
- The pass is **pure in-memory composition over the file's already-extracted edges**: it builds
  `incoming_to_temp[(temp_full_id, col)] → [edges]` by scanning `stmt.column_lineage` for every
  statement where `e.dst.table.role == "temp"` ([`ansi_parser.py:326-329`](../../src/sqlcg/parsers/ansi_parser.py)),
  then composes out-of-temp edges to a bounded fixpoint. It does **no** `exp.expand` / `qualify` /
  `build_scope` / `sg_lineage` — purely off the hot path.
- **Therefore:** once Phase 1 makes the inner CREATE (and any inner `CREATE TEMP TABLE`)
  **land in `out.statements` with their `column_lineage` populated by the normal
  `_parse_statement` path** (i.e. the unwrapped statements are appended to the same `statements`
  list before the `_emit_transitive_temp_edges` call at `:264`), the #142 pass **automatically**
  picks up any temp role node among them and emits the transitive `source→sink` `TEMP_INLINE`
  edge. **No extra wiring is needed in the composition engine.**
- **The one wiring requirement is in Phase 1, not Phase 2:** the unwrapped inner statements must
  be inserted into the `statements` list (and processed by the statement loop, marking inner temp
  targets with `role == "temp"`) **before** line `:264` runs. If Phase 1 instead parses the inner
  SQL in a *separate* `parse_file` sub-invocation whose `out` is discarded or merged after the
  composition pass, Phase 2 would NOT see those edges → it must merge inner statements into the
  enclosing `out.statements` ahead of the `_emit_transitive_temp_edges` call. **Verdict:
  Phase 2's composition is free; the only obligation is that Phase 1 surfaces the inner
  statements into the same `out` before the post-loop pass.**

## Verdict — one PR or two?
**ONE PR, two phases (confirm the maintainer's instinct).** Reasons:
- **Same shape, shared machinery:** both phases are "recover hidden lineage, thread through the
  intermediate"; Phase 2 is the existing #142 engine, not new code. Splitting would create a
  Phase-1-only PR that surfaces inner statements but a reviewer can't see the temp-composition
  payoff in the same change.
- **Phase 2 is ~free:** it's verified to be automatic composition, not a separate build — there's
  no independent implementation cost to isolate into its own PR.
- **One acceptance surface, one metric snapshot:** the island-closure + edge assertions + the
  `gain --json` delta are one coherent before/after.
- **Caveat for the plan-reviewer:** if Phase-1 unwrapping turns out to need a non-trivial new
  re-parse entry point (e.g. a recursive nested-parse that risks a perf-invariant), the reviewer
  may split *Phase 1 mechanics* from *the WTFA acceptance* — but the default and recommendation
  is the single PR.

## Dependency / sequence
- **Depends on #142 (E8 dual-emission, `feat/e8-dual-emission`). Sequence PR-6 AFTER #142
  merges** — Phase 2 *is* #142's engine; without #142 on master, the transitive-temp
  composition does not exist and Phase 2 has nothing to reuse. (Phase 1's unwrap is independent
  of #142, but the PR's Phase-2 acceptance and "reuse not duplicate" framing require #142
  present.)
- Independent of PR-1..PR-5 otherwise. Distinct from PR-5 (extraction, not exclusion).
- Metric-moving → `gain --json` required. Rank set by plan-reviewer.

## Acceptance criteria
- **Phase 1:** the **artikel-formule-dagomzet 4-node island closes** (table + its 3 IA views join
  the giant): observable before/after island membership for `ba.wtfa_artikel_formule_dagomzet`.
- The extracted inner CREATE produces a `SqlQuery` row + SELECTS_FROM edges into
  `ba.wtfe_verkoopinfo` and `ba.wtda_datum` (assert the edges, not "no exception").
- The inner SQL inherits the file's `USE SCHEMA` context (resolution lands in `BA`, not
  unqualified), via the **reused** session-context mechanism.
- The WTFA `|| $WAREHOUSENAME ||` in the WAREHOUSE clause is handled (static body extracted with
  a placeholder for the runtime fragment); the WAREHOUSE clause carries no lineage so no edge is
  expected from it.
- Concatenated / bind-variable dynamic SQL (e.g. `MSSPR_COMPARE_DATA.sql`) is **skipped**, not
  mis-extracted — a negative test asserts no spurious nodes/edges from a concatenated
  EXECUTE IMMEDIATE.
- **Phase 2 (composition):** **if** a temp table is created inside the unwrapped dynamic SQL, its
  `source→sink` lineage **composes through the existing #142 `_emit_transitive_temp_edges` pass**
  (`transform='TEMP_INLINE'`), with the intermediate temp node still visible — assert the
  transitive edge carries `transform='TEMP_INLINE'` and the intermediate node is not deleted. (No
  static-literal temp-inside-dynamic-SQL instance exists in the corpus *today*, so this is
  asserted via a **synthetic fixture** — a static-literal `EXECUTE IMMEDIATE 'CREATE TEMP TABLE …
  AS SELECT …'` followed by a sink reading the temp — proving Phase 2 is wired, not just claimed.)
- **All four perf-invariant suites pass UNMODIFIED** (nested re-parse must reuse the existing
  parse path; the composition pass is already off the hot path per #142).
- `gain --json` snapshot committed to [`plan/metrics/`](../metrics/) showing the island delta
  (metric-moving PR).
- `pyright` + `ruff` clean.
- **Status stays DRAFT (needs plan-review).**

## Risk
- Over-eager literal extraction could pick up non-DDL or concatenated strings → spurious nodes.
  Mitigate by restricting to static literals and DDL-shaped inner SQL (CREATE/INSERT), plus the
  concatenation negative test.
- Nested re-parse on the hot path could trip a perf invariant if not bounded — the inner parse
  must reuse the existing `_parse_statement` path and surface statements into the enclosing `out`
  (no per-column re-entry); re-confirm the four suites. The Phase-2 composition itself is already
  proven off the hot path by #142 (pure O(edges) in-memory, no expand/qualify/build_scope).
- **Sequencing risk:** if PR-6 is implemented before #142 merges, Phase 2 has no engine to reuse
  and the PR would either stub it (bad) or duplicate the composition (explicitly rejected). Hard
  gate: do not open PR-6 until #142 is on master.

---

## #40 ↔ #142 sequencing recommendation

**PR-1 (#40) must land with, or immediately after, OPEN PR #142 — and must be validated
against #142's edge set before either merges.**

- #142 (E8 dual-emission, OPEN) adds `transform='TEMP_INLINE'` edges to raw `COLUMN_LINEAGE`.
  The PR-1 guards, as written, will turn red the moment #142 merges.
- **Recommended order:** finish PR-1's filter-aware traversal **first** on a branch, then
  rebase/merge it together with (or ahead of) #142 so master never has a window where the
  guards are armed-and-red. Concretely:
  1. Implement PR-1 (filter-aware traversal + the `TEMP_INLINE` regression test that inserts
     a synthetic dual-emission edge and asserts it's excluded).
  2. Verify PR-1 green **both** against current master **and** against a local merge with
     #142's branch.
  3. Merge PR-1 first (it's a strict guard improvement, safe on current master), then #142
     lands on a master whose guards already tolerate `TEMP_INLINE`.
- **Do not merge #142 before PR-1** — that creates a red-master window. If #142 is merged
  first by the maintainer, PR-1 becomes a hotfix and jumps the queue.
- Tagging/closing #40 and #142 remains the maintainer's call (house rule 1).

---

## Suggested overall sequence

| Order | PR | Why here | Bump | Metric-moving (`gain --json`)? |
|-------|----|----------|------|-------------------------------|
| 1 | PR-1 #40 | Armed regression vs OPEN #142 — time-critical | patch | No (unless counts shift) |
| 2 | PR-2 #27a | Small, independent, reduces island noise | minor | **Yes** |
| 3 | PR-3 #63 | Independent bug; precedes PR-4's profiling | patch | No |
| 4 | PR-4 #94 | Perf; benefits from stable server start | patch | **Yes** |
| 5a | PR-5 (DRAFT) | Correct stale `.sqlcgignore` rationale — cheap, ship anytime | patch (docs-ish) | No |
| 5b | PR-5 (DRAFT) | **Gated** on subprocess isolation + maintainer priority decision | deferred | Yes (when it lands) |
| — | PR-6 (DRAFT) | Dynamic-SQL unwrap (Phase 1) + #142 temp composition (Phase 2); **after #142**; rank set by plan-reviewer | minor | **Yes** |

> **PR-5 / PR-6 are Status: DRAFT (needs plan-review)** — folded in 2026-06-14 from the fresh
> index; they have **not** cleared the plan-reviewer gate and are sequenced provisionally.
> PR-1..PR-4 remain **REVIEWED** and frozen.

---

## Non-obvious / maintainer-decision items (summary)

1. **PR-1:** share the kind-filter predicate between `analyze.py` and the test (DRY, private)
   vs duplicate in test (test-only but drift-prone). Planner recommends shared **module-private**.
2. **PR-1/PR-2 provenance:** this working tree already contains a kind-filter and the
   backup-pattern defaults (+ a test harness mirror). Plan-reviewer must run `git log` on
   `analyze.py`/`config.py`/`test_golden_lineage.py` to confirm what (if anything) already
   landed, or PR-1/PR-2 risk re-implementing shipped code.
3. **PR-2:** exact default backup-glob set, and whether dated-suffix exclusion ships on or
   stays opt-in (legitimate-table-safety constraint).
4. **PR-3:** DuckDB locking reality — does a 2nd read-only open actually succeed against a
   live writer, or is the graceful pre-check the real fix? Settle with an empirical probe
   before committing to `access_mode='READ_ONLY'`.
5. **PR-4:** whether to rewrite the catalog insert path now or only instrument + characterize
   it this PR and defer the bulk-catalog rewrite to a follow-up.
6. **PR-5 (DRAFT):** after subprocess isolation lands — **restore IA-DATAPRODUCTS coverage**
   (recover 124 orphan nodes into the giant, accept the dynamic-table indexing cost) **or keep
   the perf workaround** (leave ignored)? Maintainer priority decision; gates 5b. 5a (the
   stale-comment fix) ships regardless of this decision.
7. **PR-6 (DRAFT) — re-scoped 2026-06-14 to ONE PR, two phases, reusing #142.** Resolved
   design points (planner positions, plan-reviewer to confirm):
   - **One PR, two phases** (Phase 1 unwrap static-literal EXECUTE IMMEDIATE; Phase 2 compose the
     intermediate via #142's existing `_emit_transitive_temp_edges`). Confirms the maintainer's
     single-PR instinct — Phase 2 is the existing engine, not new code, so there is nothing to
     split out. Planner recommends keeping it one PR.
   - **Phase 2 is FREE (no extra wiring in the composition engine).** Verified: the #142 pass runs
     unconditionally after the per-statement loop ([`ansi_parser.py:264`](../../src/sqlcg/parsers/ansi_parser.py),
     def at `:273`) over `out.statements`. If Phase 1 surfaces the unwrapped inner statements into
     the same `out.statements` (so inner temps get `role == "temp"`) before `:264`, the transitive
     `TEMP_INLINE` composition applies automatically. **The only obligation is on Phase 1** —
     merge inner statements into the enclosing `out` ahead of the post-loop pass; do not parse the
     inner SQL into a discarded sub-`out`.
   - **WTFA `|| $WAREHOUSENAME ||`:** extract the static `AS SELECT` body, placeholder the runtime
     fragment in the (non-lineage) WAREHOUSE clause. Planner leans extract-the-body; reviewer to
     confirm.
   - **#142 dependency is explicit:** sequence PR-6 AFTER #142 (`feat/e8-dual-emission`) merges —
     Phase 2 reuses #142's engine.
   - **Honest ROI:** 1 file / 1 table / 4-node island *today*; Phase 2 adds zero islands today
     (the one temp-inside-dynamic-SQL file is concatenated, hence out of scope). Value is
     structural (recurring authoring class + near-free future-proofing), not volumetric.
     Plan-reviewer to rank against the higher-volume PR-2 / PR-4 levers.
