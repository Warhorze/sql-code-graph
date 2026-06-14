# Bugfix Sprint: Impact-Analysis Output Layer (live-DWH validation)

**Status: DRAFT**  (plan-reviewer gate runs next — do NOT mark REVIEWED)

Owner: architect-planner. Compliance owner: architect-planner (this doc).

## Summary

Three bugs found by serialized live-DWH validation against ground-truth git diffs,
all in the **impact / pr-impact OUTPUT layer** ([`analyze.py`](src/sqlcg/cli/commands/analyze.py),
`_compute_pr_impact` in [`tools.py`](src/sqlcg/server/tools.py), and `_classify` in
[`base.py`](src/sqlcg/parsers/base.py)). Distinct from the in-flight trust/usability
sprint (#151/152/153 — do NOT touch) and the column-lineage-correctness sprint
(bugs #2/#4/#5 — not in scope here).

| Bug | Severity | Reproduced here? | PR |
|-----|----------|------------------|----|
| #3 sole-producer deletion = FALSE SAFETY SIGNAL | MAJOR (priority) | **YES — root cause pinned** | PR-A |
| #6 `analyze impact` under-reports `INSERT…SELECT` / `CREATE VIEW` consumers | MINOR | **NO — not reproduced in fixtures; structural difference documented** | PR-B |
| #7 scripting `SET var = (SELECT …)` mislabeled `CREATE_TABLE` | MINOR | **NO — `OTHER` in fixtures; latent fallthrough pinned** | PR-C |

> **Reproduction-evidence rule.** Bug #3 is pinned to an exact `file:line` with a green
> repro. Bugs #6 and #7 did **not** reproduce in standalone fixtures — the live trigger
> is a DWH-specific statement shape I could not isolate from the planning sandbox. Their
> PRs therefore have a **mandatory "capture the minimal reproducing fixture first"**
> gate: the developer must obtain a failing fixture from the live corpus (branch
> `us55425` / the validation harness) BEFORE writing the fix, and the regression test
> must assert on that fixture. A fix shipped against a fixture that cannot exhibit the
> failure mode is UNVERIFIED, not done (the v1.21.0 dual-write lesson).

## Non-Goals

- No runtime-emptiness detection (pr-impact is code-regression-only — invariant).
- No change to the resync-to-HEAD side effect of `pr-impact` (separate follow-up).
- No touch to #151/152/153 (trust/usability) or #2/#4/#5 (column-lineage correctness).
- BLOCKER #1 (stale post-checkout hook binary pinned to v1.25.6) is an OPS fix owned by
  PR-A of the other sprint — explicitly OUT of scope here.
- No new parse-time schema feeding (CLAUDE.md performance prohibition).

## Perf-invariant note (applies to ALL three PRs)

The four frozen perf suites MUST pass **UNMODIFIED**:
[`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py),
[`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py).
None of these fixes touch the parser hot loop, `exp.expand`, `qualify`/`build_scope`,
or the bulk-upsert path, so all four should pass without edit. If any requires editing,
STOP — the invariant was broken.

---

## PR-A — Bug #3: sole-producer deletion is a FALSE SAFETY SIGNAL (PRIORITY)

### Problem

When a file deletion removes the **sole producer** of a table that has **no surviving
consumers**, `pr-impact` emits the green **"No genuine lost producers detected."** /
hint **"No producers lost — no data-loss blast radius."** — the exact opposite of the
truth. This is the headline data-loss-guard use case, and it fails silently. Reproduced
on dwh branch `us55425`; reproduced deterministically in the planning sandbox (below).

### Exact condition under which the wrong signal is emitted

`pr-impact` renders the green "No genuine lost producers detected." path
([`analyze.py:683-686`](src/sqlcg/cli/commands/analyze.py)) and the engine sets the hint
**"No producers lost — no data-loss blast radius."**
([`tools.py:2968-2969`](src/sqlcg/server/tools.py)) **iff `genuine_lost == []`**.

The defect: `genuine_lost` is computed empty even though the producer-set diff is
correct. Trace (all observed in the sandbox repro, single in-process run):

1. `producers_before = {da.lonely, da.keeper}`, `producers_after = {da.keeper}` →
   `before - after = {da.lonely}` ✓ (correct).
2. `_diff_lost_producers` ([`tools.py:2489`](src/sqlcg/server/tools.py)) receives
   `lost_raw = {da.lonely}` and calls
   `_exclude_synthetic_tables(db, ["da.lonely"])` at
   **[`tools.py:2531`](src/sqlcg/server/tools.py)**.
3. **ROOT CAUSE:** after resync-to-HEAD, the orphaned table's `SqlTable` node still
   exists with **`kind = 'derived'`** (observed: `kinds for da.lonely -> 'derived'`).
   `_exclude_synthetic_tables` ([`tools.py:388-423`](src/sqlcg/server/tools.py)) treats
   `kind ∈ {'cte','derived','temp'}` (`_SYNTHETIC_TABLE_KINDS`,
   [`tools.py:385`](src/sqlcg/server/tools.py)) as synthetic and **drops `da.lonely`**
   → `lost_set = {}` → `genuine_lost = []`.

### Why the kind is `'derived'` (the deeper cause)

An `INSERT … SELECT` **target table that is never independently read** as a source gets
`kind = 'derived'` from the INSERT-target indexing path; only a table that is *referenced
as a source somewhere* is upgraded to `kind = 'table'` (the indexer intentionally does
NOT let `'derived'` upgrade `'table'`, see
[`indexer.py:247-254`](src/sqlcg/indexer/indexer.py), comment "real ETL target tables").
So:

- Orphan **with** a consumer (the existing passing test, `da.orphan` read by `da.child`)
  → `kind='table'` → survives `_exclude_synthetic_tables` → correctly reported.
- Orphan **with no** consumer (`da.lonely`) → only ever an INSERT target → `kind='derived'`
  → wrongly excluded as "synthetic CTE/derived intermediate".

This is exactly the *no surviving consumers* condition in the bug report: the absence of
a consumer is what leaves the table tagged `derived`, which is what triggers the false
safety signal.

### Sandbox reproduction (evidence)

Two SQL files, `da.lonely` is a sole-producer INSERT target with **no** consumer:

```
prod.sql : INSERT INTO da.lonely SELECT id, amount FROM da.source_raw
other.sql: INSERT INTO da.keeper SELECT x FROM da.up
```

Index at base; delete `prod.sql`; commit; run `_compute_pr_impact(base_ref=base)`:

```
producers_base: ['da.keeper', 'da.lonely']          # da.lonely IS a producer
before - after: ['da.lonely']                        # diff correct
_diff_lost_producers -> genuine_lost: []             # WRONG — dropped
hint: "No producers lost — no data-loss blast radius."   # FALSE SAFETY SIGNAL
SqlTable kind for da.lonely after resync: 'derived'  # the misclassification
_exclude_synthetic_tables(["da.lonely"]) -> real=[], synthetic=['da.lonely']
```

Contrast (existing test, with a consumer): `da.orphan` keeps `kind='table'` and IS
reported — which is why the bug escaped the integration suite.

### Proposed fix

The producer-set membership (`SqlQuery.target_table`, via `GET_PRODUCER_TABLES`) is the
**authoritative signal that a table is a real written table**. A table that lost its
producer is by construction a real ETL target — it must NOT be discarded merely because
its `SqlTable.kind` is `'derived'`. The synthetic-exclusion was designed to drop genuine
CTE/derived *intermediates* (issue #38 synthetic-node leak), not lost INSERT targets.

**Fix (preferred): in `_diff_lost_producers`, do NOT apply `_exclude_synthetic_tables`
to the lost-producer set.** The lost set already comes exclusively from
`GET_PRODUCER_TABLES` (`target_table <> ''`), which a pure CTE/derived intermediate can
never be (a CTE alias is never a `SqlQuery.target_table`). So the synthetic filter on the
lost side is both unnecessary and actively wrong here. Keep the synthetic filter on the
**new** set used for rename matching (that side is unaffected by this bug).

- If the developer finds (with evidence) that a genuine synthetic node CAN appear in the
  producer set, replace the blanket `kind ∈ synthetic` exclusion with a guard that
  **keeps any table present in `producers_before`** (i.e. exclude synthetic only when the
  table is NOT a known producer). Document which path emits a synthetic `target_table` if
  so. Default to the simpler fix unless that evidence exists.

- **Files affected:** [`tools.py`](src/sqlcg/server/tools.py) — `_diff_lost_producers`
  (the `_exclude_synthetic_tables(db, sorted(lost_raw))` call at ~L2531). No SQL-query
  change required. No render change required (the existing
  `if result.lost_producer_tables:` branch will fire once the set is non-empty).

### Acceptance criteria (observable)

- [ ] **A1 (the bug):** A new integration test — a sole-producer INSERT target with
      **zero surviving consumers** whose producer file is deleted — asserts the table
      **appears in `result.lost_producer_tables`** and the hint does **NOT** contain
      "No producers lost". (Mirror `test_genuine_deletion_lost_producer_and_blast_radius`
      in [`test_pr_impact_integration.py`](tests/integration/test_pr_impact_integration.py)
      but **remove the downstream consumer file**, so the orphan keeps `kind='derived'`.)
      This is the required regression test called out in the brief.
- [ ] **A2 (attribution):** the deleted file appears in
      `result.attribution[<orphan table>]` (the deeper data-loss signal still resolves
      the responsible file).
- [ ] **A3 (no over-report regression):** the existing
      `test_modified_still_producing_not_reported` and the rename-classification tests
      still PASS unmodified — a still-produced table is NOT newly reported, and a genuine
      rename is still suppressed.
- [ ] **A4 (consumer case unbroken):** the existing
      `test_genuine_deletion_lost_producer_and_blast_radius` (orphan WITH consumer) still
      PASSES unmodified.
- [ ] **A5 (blast radius for no-consumer orphan):** for a no-consumer orphan, an empty
      blast radius is acceptable (there are no downstream tables) — but the table MUST
      still be listed under "Lost producers (genuine data-loss risk)". Assert
      `lost_producer_tables` non-empty even when `blast_radius` views are empty.

### Version bump

PATCH → **1.32.2** (bugfix to existing pr-impact behaviour, no surface change). Bump
[`pyproject.toml`](pyproject.toml) + [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py),
run `uv lock`.

### Live-DWH index slot

**Not required.** The integration test reproduces the bug deterministically with an
in-memory DuckDB + temp git repo. A live-DWH re-run on `us55425` is a nice-to-have
confirmation for the user, not a gate (and the planner must not run the indexer).

### Perf-invariant note

Four frozen suites pass UNMODIFIED — this PR touches only `_diff_lost_producers`
(post-resync analysis), never the parser/upsert hot paths.

---

## PR-B — Bug #6: `analyze impact` under-reports `INSERT…SELECT` / `CREATE VIEW` consumers

### Problem (and reproduction status)

Per live validation, `analyze impact <table>` misses some `INSERT…SELECT` / `CREATE VIEW`
consumers that `downstream` and `empty-impact` do surface — an inconsistency between the
impact code paths.

**NOT reproduced in standalone fixtures.** I tried plain `INSERT…SELECT`, CTE-wrapped
`INSERT…SELECT`, CTE-wrapped `CREATE VIEW … AS SELECT`, and `INSERT … SELECT * FROM` —
in **every** case `analyze impact` found the consumer via `SELECTS_FROM`. So the exact
live trigger is a DWH-specific shape I could not isolate.

### Structural root cause (documented difference between the three paths)

- **`analyze impact`** ([`analyze.py:310-329`](src/sqlcg/cli/commands/analyze.py)) joins
  **only** `SqlTable → SELECTS_FROM → SqlQuery`. It sees a consumer **iff** a
  `SELECTS_FROM` edge exists from the consuming query to the table. It does NOT consult
  `STAR_SOURCE` and does NOT consult `COLUMN_LINEAGE`.
- **`downstream`** ([`analyze.py:247-307`](src/sqlcg/cli/commands/analyze.py)) traverses
  `COLUMN_LINEAGE` (kind-filtered) **plus** external consumers via
  `GET_TABLE_EXTERNAL_CONSUMERS_QUERY`.
- **`empty-impact`** (`_compute_empty_propagation`,
  [`tools.py:2248`](src/sqlcg/server/tools.py)) uses `COLUMN_LINEAGE` closure + gating-join
  views.

So `analyze impact` will miss any consumer whose edge to the table exists **only** in
`STAR_SOURCE` or **only** in `COLUMN_LINEAGE` but NOT in `SELECTS_FROM`. The live DWH
almost certainly contains such a shape (e.g. a `SELECT *` consumer where the planner
sandbox happened to also emit a `SELECTS_FROM` row, or a CTE-nested read where the
top-level `SELECTS_FROM` was absent for that particular statement structure).

### Mandatory pre-fix gate

Developer MUST first capture a **minimal SQL fixture** from the live corpus (branch
`us55425` / validation harness) where `analyze impact <T>` omits a consumer that
`downstream`/`empty-impact` report. Add it as an integration fixture. If no such fixture
can be reproduced after a bounded effort, STOP and report back — do not ship a
speculative query rewrite.

### Proposed fix (after the fixture is captured)

Bring `analyze impact` to **consumer-detection parity** with `downstream`/`empty-impact`
by UNIONing the consumer-discovery signals it currently lacks:

- `SELECTS_FROM` (existing), `STAR_SOURCE`, and the `COLUMN_LINEAGE`-derived consumer
  (the query that writes a column whose source column belongs to the target table).

Implement as a single UNION query (mirror the `unused` command's three-signal UNION at
[`analyze.py:378-392`](src/sqlcg/cli/commands/analyze.py), which already proves the
`SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE` read-signal pattern). Keep `LIMIT 100` and
the noise filter. The exact column-lineage join must resolve to the consuming
`SqlQuery.id` / `kind` / `target_table` so the output columns are unchanged.

- **Files affected:** [`analyze.py`](src/sqlcg/cli/commands/analyze.py) `impact` command;
  possibly a new query in [`queries.sql`](src/sqlcg/core/queries.sql) +
  [`queries.py`](src/sqlcg/core/queries.py) if the UNION is non-trivial.

### Acceptance criteria (observable)

- [ ] **B1:** the captured live-derived fixture — where `analyze impact <T>` previously
      omitted a consumer — now lists that consumer (assert the specific query id / target
      table appears). The test must use the fixture that *exhibits* the omission, not a
      shape where `SELECTS_FROM` already covers it.
- [ ] **B2 (parity):** for the same fixture, the consumer set from `analyze impact`
      is a **superset of or equal to** the consuming-query set implied by `downstream`'s
      result for the same table (the inconsistency is closed).
- [ ] **B3 (no double-count):** `DISTINCT` holds — a consumer reachable via two signals
      appears once.
- [ ] **B4 (noise filter intact):** noise-target consumers are still filtered unless
      `--raw`.

### Version bump

PATCH → next patch after PR-A (e.g. **1.32.3**). Bug fix to existing command, no surface
change.

### Live-DWH index slot

**Capture-only, not a full index slot.** Needs a single minimal fixture extracted from
the live corpus (developer obtains it; planner does not run the indexer). The regression
test runs on in-memory DuckDB.

### Perf-invariant note

Four frozen suites pass UNMODIFIED — read-side query change only; no parser/upsert path.

---

## PR-C — Bug #7: scripting `SET var = (SELECT …)` mislabeled `CREATE_TABLE`

### Problem (and reproduction status)

A scripting variable assignment from a subquery (`SET my_var = (SELECT …)`) is rendered
with the wrong statement kind (`CREATE_TABLE`) in impact output.

**NOT reproduced in standalone fixtures.** Standalone `SET v = (SELECT …)`,
`SET v := (…)`, `LET v := (…)`, `LET rs RESULTSET := (…)`, and `SET (a,b) := (…)` all
classify as `OTHER` (or are suppressed) under the snowflake dialect in the sandbox — none
yield `CREATE_TABLE`. The live trigger is a DWH-specific scripting context (procedure
body / multi-statement / inner-statement re-parse) where sqlglot parses the assignment
into an `exp.Create` node with a non-standard `.kind`.

### Root cause (latent fallthrough — pinned)

`_classify` ([`base.py:518-552`](src/sqlcg/parsers/base.py)) has a catch-all inside the
`exp.Create()` arm:

```python
case exp.Create():
    match stmt.kind:
        case "TABLE":     return QueryKind.CREATE_TABLE
        case "VIEW":      return QueryKind.CREATE_VIEW
        case "PROCEDURE" | "FUNCTION": return QueryKind.CREATE_PROC
        case _:           return QueryKind.CREATE_TABLE   # <-- base.py:548 "Default to table"
```

**Any** `exp.Create` whose `.kind` is not one of the three known values is silently
labeled `CREATE_TABLE`. When the snowflake scripting `SET … = (SELECT …)` parses (in a
procedure/scripting context) to an `exp.Create` with an unexpected/empty `.kind`, it
falls through here → mislabeled `CREATE_TABLE` in impact output.

### Mandatory pre-fix gate

Developer MUST capture the **minimal scripting snippet** (from `us55425` / the live
corpus, likely inside a `CREATE PROCEDURE` or `EXECUTE IMMEDIATE` body) that parses to an
`exp.Create` with the unexpected kind and reproduces the `CREATE_TABLE` mislabel.
Add it as a unit fixture for `_classify` (or the snowflake-parser path). If the trigger
cannot be reproduced, STOP and report — do not change the fallthrough blindly without a
pinning test (the change is low-risk, but the test must demonstrate the live mislabel).

### Proposed fix

1. **Change the `exp.Create` catch-all to NOT default to `CREATE_TABLE`.** An unknown
   `Create.kind` is not a table — return `QueryKind.OTHER` (the same bucket the standalone
   `SET` forms already get). This removes the false `CREATE_TABLE` label.
2. If the captured fixture shows the node is specifically a scripting assignment, prefer
   an explicit guard: detect the SET/assignment shape and return `OTHER` (or a dedicated
   `ASSIGN` kind only if other code already consumes such a kind — do NOT invent a kind
   with no consumer; every new kind needs a grep-confirmed reader).

Default to option 1 (return `OTHER` from the catch-all) unless the fixture demands more.

- **Files affected:** [`base.py`](src/sqlcg/parsers/base.py) `_classify` (the
  `case _:` at L548); possibly [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py)
  if the SET-assignment must be recognised and suppressed at the inner-statement seam
  (mirror the existing `exp.PropertyEQ` suppression at
  [`snowflake_parser.py:422-424`](src/sqlcg/parsers/snowflake_parser.py)).

### Acceptance criteria (observable)

- [ ] **C1:** the captured scripting `SET var = (SELECT …)` fixture classifies as `OTHER`
      (NOT `CREATE_TABLE`) — assert the `SqlQuery.kind` value in the indexed graph (or the
      direct `_classify` return).
- [ ] **C2 (no regression on real DDL):** `CREATE TABLE …`, `CREATE TABLE … AS SELECT`,
      `CREATE VIEW … AS SELECT`, `CREATE PROCEDURE …` still classify as
      `CREATE_TABLE` / `CREATE_TABLE` / `CREATE_VIEW` / `CREATE_PROC` respectively
      (assert each — the catch-all change must not demote a real `CREATE TABLE`, which is
      already handled by the `"TABLE"` arm above the catch-all).
- [ ] **C3:** the table the subquery reads (`da.src`) still appears as a source in the
      graph — fixing the kind label must not drop the lineage edge.

### Version bump

PATCH → next patch after PR-B (e.g. **1.32.4**). Classification-label bug fix.

### Live-DWH index slot

**Capture-only.** Needs one minimal scripting fixture from the live corpus; regression
test runs on in-memory DuckDB.

### Perf-invariant note

Four frozen suites pass UNMODIFIED. `_classify` is a pure match on the already-parsed AST
node — it does not call `qualify`/`build_scope`/`exp.expand`/`sg_lineage` and is not in
the bulk-upsert path. The scaling guard is unaffected.

---

## PR sequencing (by risk / value)

1. **PR-A (Bug #3)** — FIRST. Highest value (false safety signal in the data-loss guard),
   fully reproduced, surgical one-call change in `_diff_lost_producers`, deterministic
   regression test. Ship alone.
2. **PR-B (Bug #6)** — after PR-A. Gated on capturing a live fixture; read-side query
   parity change. Ship separately.
3. **PR-C (Bug #7)** — after PR-B. Gated on capturing a live fixture; smallest blast
   radius (single classification label). Ship separately.

Never fold a new bug's work into an already-reviewed PR (project rule). Each PR bumps the
version, merges to master, then the user tags.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| PR-A fix re-introduces the #38 synthetic-node leak elsewhere | Only the **lost** side of `_diff_lost_producers` is changed; the synthetic filter on `change_scope`/`backfill_order`/`diff_impact` (tools.py L1120/1198/1303/2188) is untouched. A4/A3 pin no-regression. |
| PR-B / PR-C ship without a real reproducing fixture | Mandatory pre-fix capture gate; UNVERIFIED if the test fixture cannot exhibit the failure mode. |
| PR-B UNION query regresses read latency on large graphs | Keep `LIMIT 100`; reuse the proven `unused`-command three-signal UNION pattern; no recursive CTE added. |
| PR-C catch-all change demotes a genuine `CREATE TABLE` | The `"TABLE"` arm precedes the catch-all; C2 pins all four real-DDL kinds. |

## Open items for the plan-reviewer

- Confirm the PR-A preferred fix (drop `_exclude_synthetic_tables` on the lost side) over
  the guarded alternative (keep producers regardless of kind). I recommend the simpler
  drop because a CTE/derived intermediate can never be a `SqlQuery.target_table`.
- Confirm PR-B / PR-C may proceed with the capture-gate-first model (no full live index
  slot), or whether the user wants the live fixtures captured before these PRs are
  scheduled at all.
