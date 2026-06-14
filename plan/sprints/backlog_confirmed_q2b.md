# Backlog — Confirmed Q2-B Sprint Plan

**Status:** REVIEWED (plan-reviewer gate cleared 2026-06-14; amendments folded in below).
Two items remain explicit **maintainer design decisions** (PR-1 shared-predicate placement;
PR-3 read-only-open viability) — flagged, not blocking; the developer must surface them, not
silently pick.
**Owner role:** architect-planner
**Baseline:** triaged against master `HEAD ~v1.28.4`; re-verified by plan-reviewer against
**current master `292da6a`** (PR #141 self-heal merge; `__version__ = "1.28.4"`). All four PR
gaps **still exist** on `292da6a` — nothing already shipped. See the *Plan-reviewer verdict*
block and *Anchor reconciliation* below; several cited line numbers drifted and are corrected.
**Scope:** four confirmed, evidence-backed PRs + one pending-verification stub.

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
- **PR-5 - placeholder, correctly marked not-ready.** No change.

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

# PR-5 (pending verification) — IA-DATAPRODUCTS dynamic-table data-products possibly orphaned

**Status: PLACEHOLDER — do NOT plan a fix yet.**

A separate reindex is confirming whether the 112-file dynamic-table (`IA-DATAPRODUCTS`)
layer connects on a **fresh** index or is genuinely orphaned. The parser **does** extract
their sources, so if the layer is orphaned the gap is **indexer-side** (wiring), not a parse
gap. Once the reindex verdict lands, this section will be filled with problem / root cause
(file:line) / fix / acceptance / version bump. Until then: **no fix, no sequence commitment.**

Relevant scratch evidence (do not commit; `git checkout -- plan/measurements/`):
`plan/measurements/pr5_extraction_recall_taxonomy.json`.

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
| — | PR-5 | Pending reindex verdict | TBD | TBD |

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
