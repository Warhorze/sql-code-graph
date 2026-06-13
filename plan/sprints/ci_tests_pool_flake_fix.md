# Fix Plan: CI "Tests" workflow RED — order-dependent kind-classification flake

## Summary

The GitHub Actions **"Tests"** workflow ([`.github/workflows/test.yml`](.github/workflows/test.yml))
fails on every branch — including a docs-only branch — because **one integration test
depends on filesystem-dependent file-indexing order** that differs between the CI runner
and local machines. The test is
`tests/integration/test_pr_impact_integration.py::test_blast_radius_nonempty_for_deleted_producer_with_downstream`.
This is **NOT** the #112 in-process pool/global-state pollution flake; the evidence
refutes #112. The fix makes the test deterministic by removing its reliance on
`walk_sql_files`' unsorted `rglob` order.

## Diagnosis (verified)

### What fails
- Failing node id (identical on both 3.12 and 3.13, runs `27469206547` + `27469181568`):
  `tests/integration/test_pr_impact_integration.py::test_blast_radius_nonempty_for_deleted_producer_with_downstream`
- CI summary: `1 failed, 1450 passed, 4 skipped, 1 xfailed`. The docs-only branch
  `docs/e8-postmortem-and-corrections` Tests run `27465235895` is **also RED** → the
  failure is branch-independent and present on master, confirming it is not a code
  regression in any one PR.
- Assertion: `assert result.blast_radius.value_empty_columns` — got `[]`. The result
  object shows `downstream_count=0`, `value_affected_tables=[]`, and the downstream
  tables in `noise_excluded=['da.child', 'da.grandchild']`.

### Root cause — kind-classification race decided by unsorted rglob
The fixture's own docstring states the dependency explicitly
([`test_pr_impact_integration.py`](tests/integration/test_pr_impact_integration.py) lines 332–335):

> "which is known to give da.orphan kind='table' (da.child's SELECT FROM da.orphan
> sets the kind='table' entry **before** the INSERT INTO da.orphan 'derived' row can
> downgrade it via the DB-level guard)."

The three fixture files are:
- `source.sql`     — `INSERT INTO da.orphan SELECT … FROM da.source_raw`  (writes `da.orphan` as `kind='derived'`)
- `downstream.sql` — `INSERT INTO da.child  SELECT … FROM da.orphan`      (a read of `da.orphan` → `kind='table'` reference row)
- `downstream2.sql`— `INSERT INTO da.grandchild SELECT … FROM da.child`

The cross-batch **kind-downgrade guard** in
[`duckdb_backend.py`](src/sqlcg/core/duckdb_backend.py) lines 456–487 preserves a
committed `kind='table'`/`'view'` against a later `kind='derived'` incomer — **but only
if `da.orphan` was already written as `'table'` first.** Whether the `FROM da.orphan`
reference row (`'table'`) lands before the `INSERT INTO da.orphan` row (`'derived'`)
depends on which file the indexer processes first.

File order comes from
[`walker.py`](src/sqlcg/indexer/walker.py) line 54: `for path in root.rglob("*.sql")`
— **no `sorted()`**. `rglob` yields in filesystem-dependent order. On the CI ubuntu
runner the order downgrades `da.orphan` (or its downstream) to `'derived'`, so
[`tools.py`](src/sqlcg/server/tools.py) `_compute_empty_propagation` →
`_exclude_synthetic_tables` (line 2299, `_SYNTHETIC_TABLE_KINDS = {"cte","derived","temp"}`
at line 404) excludes `da.child`/`da.grandchild` as synthetic → empty blast radius.

### Why local is green and CI is red
Local (WSL/ext4) `rglob` happens to yield the file order that wins the race; the CI
runner's filesystem yields a different order that loses it. Verified locally:
- the test passes in **isolation** (`uv run pytest <nodeid>` → 1 passed),
- the test passes in the **full unit+integration suite** matching CI's exact invocation
  (`uv run pytest tests/unit tests/integration` → `1451 passed, 0 failed`).
So the divergence is environment/filesystem ordering, **not** test order or parallelism
(the workflow runs pytest serially — no `-n`/xdist, no randomly plugin; see
[`test.yml`](.github/workflows/test.yml) line 15).

### #112 refuted
#112 is in-process worker-pool/global-state pollution sensitive to unit→integration
ordering. This failure is **deterministic per-environment**, reproduces with the docs-only
branch, and is fully explained by the `rglob`-order kind race + the synthetic-exclusion
filter. No pool/global-state mechanism is involved. **Verdict: NOT #112.**

## Can CI-green on "Tests" be trusted as a merge gate for #38 right now?

**NO.** The "Tests" job is red on master and on every branch for a reason unrelated to the
change under review (it is red even on docs-only commits). Until this fix lands, a red
"Tests" result carries no signal about the PR's own correctness, and a (hypothetically)
green one would only mean the runner's filesystem happened to win the race that day.
**The shepherd should merge PR #113 on local-suite evidence** (the developer's
`1656 passed, 7 skipped, 2 xfailed, 1 failed` where the sole failure is the gitignored
`tests/e2e/test_dwh_e2e.py::…::test_budget_passes` wall-time gate that CI never runs),
**not on CI "Tests" green.** Trust in "Tests" as a gate is restored only after the
acceptance criterion below (docs-only no-op branch goes green) is met.

## Scope

### In Scope
- Make `test_blast_radius_nonempty_for_deleted_producer_with_downstream` deterministic
  regardless of filesystem `rglob` order.
- Make the indexer's file-processing order **deterministic** by sorting in
  [`walker.py`](src/sqlcg/indexer/walker.py) — this is the general fix that removes the
  whole class of order-dependent kind races from every test and every real index.

### Non-Goals
- Re-architecting kind classification or the downgrade guard (the guard is correct; the
  bug is non-deterministic input order, not the guard logic).
- Fixing or investigating #112 (separate, deferred; not the cause here).
- Changing the `_SYNTHETIC_TABLE_KINDS` exclusion semantics.
- Any change to the CI invocation itself (parallelism/randomization) — the workflow is
  not the cause and must not be weakened to mask a real determinism bug.

## Design

### Fix A (primary, root cause) — deterministic walk order
In [`walker.py`](src/sqlcg/indexer/walker.py), sort the `rglob` branch:

```python
for path in sorted(root.rglob("*.sql")):
    if not is_ignored(path, root, spec):
        yield path
```

Rationale: a non-deterministic index order is a latent correctness footgun far beyond
this one test — any two files that write/read the same table can flip its `kind` based
on OS filesystem order. Sorting yields a stable, reproducible graph across machines and
CI. Cost is negligible (sort of a file-path list). The `git ls-files` branch already
yields a deterministic order from git; sort it too for parity so behaviour does not
depend on `use_git`.

> **Perf note:** this touches `walker.py`, not `base.py`/`indexer.py` hot loops; it does
> not alter any [Performance invariant](../../CLAUDE.md). `sorted()` on the file list is
> O(F log F) on file *count*, not column/edge count — outside the O(N²) class the
> scaling guard protects. Confirm `test_perf_scaling_guard.py` stays green.

### Fix B (defence in depth) — make the fixture order-independent
The fixture should not depend on a race at all. Restructure it so `da.orphan` is
unambiguously a real table independent of file order — either:
- (B1) add an explicit DDL file `orphan_ddl.sql` containing
  `CREATE TABLE da.orphan (id INT, amount INT)` so `da.orphan` is `kind='table'` by DDL
  provenance (DDL wins over usage in the backend precedence), **or**
- (B2) assert on the *intended* invariant directly and, if Fix A alone makes it pass
  deterministically, keep the fixture as-is but add a comment that determinism now comes
  from sorted walk order, not a race.

Recommended: **B1** — it removes the documented race from the fixture entirely and makes
the test robust even if walk order ever changes again. The fixture docstring's
"known to give … via the race" language must be deleted; a test must not encode a race
as its operating assumption.

### Data Models / API Changes
None. No public surface changes.

### Dependencies
None added.

## Implementation Steps

### Phase 1: Deterministic indexing order
**Step 1.1**: Sort both branches of `walk_sql_files`.
- Files affected: [`src/sqlcg/indexer/walker.py`](src/sqlcg/indexer/walker.py)
- Sort the `rglob("*.sql")` iteration and the `git_files` iteration before yielding.
- Acceptance: indexing the same repo twice (or on two machines) yields the same
  `SqlTable.kind` for every table; `da.orphan` resolves to `kind='table'` regardless of
  filesystem order.

**Step 1.2**: Add a unit test pinning deterministic walk order.
- Files affected: new `tests/unit/test_walk_order_deterministic.py`
- Behaviour to verify: given a directory of `.sql` files created in a non-sorted name
  order, `list(walk_sql_files(root, spec, use_git=False))` returns paths in sorted order.
  Assert the exact returned sequence, not just "no exception".

### Phase 2: De-race the fixture
**Step 2.1**: Make `da.orphan` an unambiguous table via explicit DDL (Fix B1).
- Files affected:
  [`tests/integration/test_pr_impact_integration.py`](tests/integration/test_pr_impact_integration.py)
  `test_blast_radius_nonempty_for_deleted_producer_with_downstream`
- Add a `CREATE TABLE da.orphan (id INT, amount INT)` source file to the base commit so
  `da.orphan` is `kind='table'` by DDL provenance. Delete the docstring text that
  describes relying on the SELECT-before-INSERT race; replace with a note that
  determinism now comes from sorted walk order + explicit DDL.
- Acceptance: the test asserts the same observable outputs it does today
  (`value_empty_columns` non-empty, `da.child ∈ value_affected_tables`, a `da.child.*`
  column in `value_empty_columns`) and passes under any file order.

**Step 2.2** (optional hardening): if any *other* test in
[`test_pr_impact_integration.py`](tests/integration/test_pr_impact_integration.py) (e.g.
`test_genuine_deletion_lost_producer_and_blast_radius`, which shares the same fixture
comment about the race) relies on the same race, apply the same DDL de-racing. Audit by
grepping the file for "race"/"before the INSERT"/"downgrade".

### Phase 3: Verify the gate is restored
**Step 3.1**: Confirm on a docs-only no-op branch that the "Tests" workflow goes green
(the falsifiable gate — see Acceptance Criteria).

## Test Strategy

- **Unit (new)**: `walk_sql_files` returns `.sql` paths in deterministic sorted order for
  a directory whose files were created in reverse-name order. Assert the returned list
  equals the sorted list.
- **Integration (modified)**: the existing blast-radius test must pass deterministically.
  To prove order-independence within the test suite, the test should run the index and
  assert `da.orphan` is `kind='table'` (a direct observable of the fix) in addition to
  the existing blast-radius assertions. This pins the actual mechanism, not just the
  downstream symptom.
- **Regression guard**: run the full `tests/unit tests/integration` suite locally (CI's
  exact invocation) and confirm `0 failed`. Run `test_perf_scaling_guard.py` to confirm
  no perf-invariant regression from the added `sorted()`.
- Describe each test by behaviour; the developer names them
  `test_walk_sql_files_unsorted_input_returns_sorted` /
  `test_blast_radius_orphan_kind_is_table_and_radius_nonempty` and links this plan in the
  docstring.

## Acceptance Criteria

- [ ] `walk_sql_files(root, use_git=False)` returns `.sql` paths in deterministic
      (sorted) order — pinned by a unit test that creates files in reverse-name order.
- [ ] `test_blast_radius_nonempty_for_deleted_producer_with_downstream` passes and no
      longer documents a reliance on file-processing order; `da.orphan` is asserted
      `kind='table'` independent of OS filesystem order.
- [ ] Full `uv run pytest tests/unit tests/integration` is `0 failed` locally.
- [ ] `test_perf_scaling_guard.py` stays green (no O(N²) regression introduced).
- [ ] **Falsifiable CI gate**: after the fix merges to master, the **"Tests"** workflow
      is GREEN on a **docs-only no-op branch** (e.g. a branch that only touches a markdown
      file). This is the criterion that proves the branch-independent breakage is gone and
      that "Tests" green can again be trusted as a merge gate.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Sorting changes real-DWH index output / breaks another order-sensitive test | Sorting only makes order *deterministic*; it cannot reduce coverage. Run the full suite; any newly-red test was relying on the same latent race and should be de-raced the same way (Step 2.2). |
| `sorted()` perceived as a hot-loop perf risk | It sorts the file-path list once per index (O(F log F) on file count), not in any per-column/per-edge loop. Outside the scaling-guard class; confirm guard green. |
| Other tests silently depend on the *old* unsorted order | Full-suite run after Fix A surfaces them immediately; treat each as the same bug class. |
| Fix masks a deeper kind-classification ambiguity | Out of scope here; the guard logic is correct given deterministic input. File a follow-up if DDL-less table/derived disambiguation needs review. |

## Version Impact

Bug fix only (CI determinism + test correctness; no public surface change) → **patch**
bump per [CLAUDE.md](../../CLAUDE.md) SemVer rules. Bump `pyproject.toml`,
`src/sqlcg/__init__.py`, and `uv lock` in the fix branch before the PR merges. Suggested
`v1.25.8` (next patch after the current `v1.25.7` master baseline at branch time) — confirm against the latest
tag at PR time.

## Rollback

Single-commit revert of the `walker.py` sort + the fixture change restores prior
behaviour. No data migration; re-index is the migration path (no persisted-format change).

### Deviations

#### Deviation 1: Fix B implemented via file-name ordering, not DDL provenance
- **Reason**: The plan proposed Fix B1 (add explicit DDL file `da.orphan (id INT, amount INT)`)
  to establish kind='table' via defined_in_file provenance. Investigation revealed two blockers:
  (a) DDL files create SqlQuery entries for their table, so an `aa_orphan_ddl.sql` file means
  da.orphan still has a producer after source.sql is deleted — `_compute_pr_impact` returns
  empty `lost_producer_tables` and the test fails. (b) Similarly, a DDL file for da.child
  creates a spurious SqlQuery producer entry.
- **Change**: The fixture names its files so that `aaa_downstream2.sql` sorts before
  `downstream.sql` before `source.sql`. With the now-sorted walker, this guarantees
  da.child is established as kind='table' (from the SELECT FROM da.child reference in
  aaa_downstream2.sql) before downstream.sql's INSERT INTO da.child (derived) arrives in
  the in-batch dedup. da.orphan is established as kind='table' (from downstream.sql's
  SELECT FROM da.orphan) before source.sql's INSERT INTO da.orphan (derived) arrives.
  This mirrors the local-filesystem ordering the original test relied on, but now done
  deterministically via sorted path names.
- **Impact**: Test is fully de-raced — no reliance on filesystem order. da.orphan correctly
  loses its producer when source.sql is deleted (no spurious DDL-based SqlQuery). The
  orphan kind='table' assertion is retained to pin the mechanism.
- **Date**: 2026-06-13
