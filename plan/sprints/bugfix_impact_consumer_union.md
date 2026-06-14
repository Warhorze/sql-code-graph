# Fix Plan: `analyze impact` under-reports consumers (Bug #6)

**Status:** REVIEWED (plan-reviewer 2026-06-14 returned REWORK; shepherd folded. FORK-1 RESOLVED
— union is correct, `impact` single-hop preserved, not intentional semantics (the divergence is an
incomplete impl predating the lineage rollup `find_table_usages` already has). FORK-2 RESOLVED — the
reviewer EMPIRICALLY PROVED the nested-CTE indexer fixture cannot fail pre-fix (it yields a namespaced
`kind='cte'` gap node, not the live non-namespaced `kind='table'` from cross-file promotion). The 3
blockers (all one root cause) are folded: the authoritative repro is now **graph-layer seeded** (A1);
the indexer fixture is demoted to no-regression only (A2); PR #156 docstring + disproven-invariant test
rewrites are reconciled to the seeded repro. The fix QUERY itself was proven correct against the live
shape by the reviewer. Version PATCH, reconcile at merge (master now v1.34.1). READY FOR DISPATCH.)
**Branch (plan):** `plan/impact-consumer-union` (off `master` @ v1.34.0, commit `2047bc0`)
**Implementation branch (developer creates):** `fix/impact-consumer-union`
**Version:** PATCH → `1.34.1` (reconcile at merge; CLI-output completeness fix, no surface break)
**Bug ref:** #6 (CONFIRMED, live-verified on the DWH this session)

---

## Summary

`analyze impact <T>` lists only the queries that read `<T>` through a **direct
`SELECTS_FROM` edge** (single hop `SqlTable → SELECTS_FROM → SqlQuery`). It
misses consumers that read `<T>` only through `COLUMN_LINEAGE` / `STAR_SOURCE`
— the CTE-wrapped / promoted-intermediate case. The sibling commands
`downstream` and `empty-impact` already surface those consumers, so `impact`
silently under-reports relative to them. Fix: give `impact` the same
`SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE` consumer union the other commands
already rely on, deduped, with the output schema unchanged.

---

## Live evidence (this session, real DWH)

| Command | Result for `ba.all_rows_in_selection` |
|---|---|
| `analyze impact ba.all_rows_in_selection` | **No results** (BUG) |
| `analyze empty-impact ba.all_rows_in_selection` | 2 consumers: `ba.wtfa_loonkosten_functie`, `ia_analytics.ba_wtfa_loonkosten_functie` |
| `analyze downstream ba.all_rows_in_selection.dn_functie_naam` | `ba.wtfa_loonkosten_functie` |

`ba.all_rows_in_selection` is a promoted inner-CTE intermediate node:
`kind='table'`, **non-namespaced** (no `::`), **0 `SELECTS_FROM` in-edges**,
**55 `COLUMN_LINEAGE` out-edges** (`transform='CTE_PROJECTION'`). The single-hop
`SELECTS_FROM` join therefore returns nothing, while the lineage-based commands
follow `COLUMN_LINEAGE` and find the real consumers.

Systematic live scan: **exactly 1** such divergence table (`kind='table'`,
non-namespaced) on the corpus, plus 2 terminal `_tmp` tables that are *genuinely*
empty (no consumer of any kind — correctly "No results").

---

## Anchors (re-confirmed against `origin/master` @ `2047bc0`)

| Anchor | Location |
|---|---|
| `analyze impact` consumer query (SELECTS_FROM-only) | [`analyze.py`](src/sqlcg/cli/commands/analyze.py) `impact()` lines **316–323** |
| `analyze downstream` consumer path | [`analyze.py`](src/sqlcg/cli/commands/analyze.py) `_downstream_sql` (COLUMN_LINEAGE recursive CTE) + terminal external consumers via `GET_TABLE_EXTERNAL_CONSUMERS_QUERY`, lines **159–203, 287–307** |
| `empty-impact` consumer derivation | [`tools.py`](src/sqlcg/server/tools.py) `_compute_empty_propagation` + `GET_TABLE_READS_ADJACENCY_QUERY` + COLUMN_LINEAGE BFS, lines **~920–1000, 322+, 454+** |
| `GET_TABLE_READS_ADJACENCY` | [`queries.sql`](src/sqlcg/core/queries.sql) lines **373–389** — `SELECTS_FROM ∪ STAR_SOURCE` (table-level) |
| `FIND_TABLE_USAGES` | [`queries.sql`](src/sqlcg/core/queries.sql) lines **51–59** — SELECTS_FROM only (comment claims "D1+D2" but body has **no STAR_SOURCE join** — see note) |
| `FIND_TABLE_USAGES_VIA_LINEAGE` (PR 4) | [`queries.sql`](src/sqlcg/core/queries.sql) lines **390–407** — the COLUMN_LINEAGE source-table rollup (D3) |
| `find_table_usages` dedup pattern (direct + via-lineage) | [`tools.py`](src/sqlcg/server/tools.py) `find_table_usages` lines **920–1000** |
| PR #156 parity test (asserts the now-disproven claim) | [`tests/integration/test_impact_consumer_parity.py`](tests/integration/test_impact_consumer_parity.py) |

> **Note on `FIND_TABLE_USAGES`:** its docstring/comment in `tools.py` says
> "Direct reads: SELECTS_FROM and STAR_SOURCE (D1 + D2)", but the SQL body joins
> **only `SELECTS_FROM`**. The product completeness for `find_table_usages` comes
> entirely from the D3 via-lineage query. This means STAR_SOURCE-only consumers
> are *also* currently under-reported by `find_table_usages`. This fix's union
> must explicitly include STAR_SOURCE so `impact` does not inherit the same gap.
> Fixing `FIND_TABLE_USAGES` itself is **OUT OF SCOPE** (see Non-Goals) — but the
> plan-reviewer should note it as a follow-up.

---

## Root-cause finding: WHY does `impact` use SELECTS_FROM only?

**Finding: it is not a deliberate "queries that physically read this table"
semantic — it is an incomplete implementation that predates the PR 4 lineage
rollup.** Evidence:

1. The `impact` query is the *oldest, simplest* of the three consumer paths — a
   bare single-hop join, no recursion, no lineage. The sibling commands were each
   subsequently extended (PR 4 added the `FIND_TABLE_USAGES_VIA_LINEAGE` D3 rollup
   to `find_table_usages`; `empty-impact` was built from the start on
   `GET_TABLE_READS_ADJACENCY` ∪ COLUMN_LINEAGE). `impact` was never updated.
2. The user-facing intent of `impact` ("Show all queries impacted by a table") is
   **the same** as `find_table_usages` ("Find all queries that use a given
   table") — both answer "who reads this table?". `find_table_usages` already
   resolved this exact gap via the union; `impact` simply did not get the same
   treatment.
3. The promoted-intermediate node `ba.all_rows_in_selection` is a legitimate
   `kind='table'` node and a legitimate `impact` target — a user who sees it in
   `downstream`/`empty-impact` reasonably runs `impact` on it and gets a wrong
   "No results". There is no semantic distinction that justifies the divergence.

**Conclusion: the union IS the right fix, not a semantic conflation.** "Impacted
by a table" and "reads a table" are the same question; the union is the complete
answer to it. `downstream` answers a *different* (column-flow, transitive)
question and is intentionally distinct — we are **not** turning `impact` into
`downstream`; we are bringing `impact` to the same *one-hop consumer completeness*
that `find_table_usages` already has.

### Design fork for the maintainer (plan-reviewer / user decision)

> **FORK-1 — Is `impact`'s single-hop SELECTS_FROM-only behaviour intentional
> "physical read" semantics?** This plan asserts NO (it is an incomplete impl)
> and recommends the union. If the maintainer instead intends `impact` to mean
> strictly "queries with a literal top-level `FROM <T>`" (excluding
> CTE-derived/star reads by design), then the correct fix is **not** the union
> but a **documentation + hint** change: `impact` keeps SELECTS_FROM-only but,
> when it returns 0 rows AND a COLUMN_LINEAGE/STAR consumer exists, prints a hint
> pointing the user to `downstream`/`empty-impact` (mirroring the existing
> bare-name hint pattern in `upstream`/`downstream`). **Recommended: the union.**
> The reviewer must confirm or pick the hint variant before the developer starts.

---

## Recommended union design

Mirror the **`find_table_usages` two-query + dedup pattern** (already proven,
already shipped). `impact` keeps its existing output columns (`id`, `kind`,
`target`) and `_print_table(results, ["id", "kind"])` rendering — **no schema
change**.

### New query: `IMPACT_CONSUMERS_VIA_LINEAGE`

Add to [`queries.sql`](src/sqlcg/core/queries.sql) (next to
`FIND_TABLE_USAGES_VIA_LINEAGE`) and export the constant in
[`queries.py`](src/sqlcg/core/queries.py). It must return the **same column shape
as the existing impact query** (`id`, `kind`, `target`) so the two result lists
merge without reshaping:

```sql
-- IMPACT_CONSUMERS_VIA_LINEAGE
-- Bug #6: consumers that read a table only via COLUMN_LINEAGE or STAR_SOURCE
-- (CTE-wrapped / promoted-intermediate reads that emit no top-level SELECTS_FROM
-- edge). Returns the same (id, kind, target) shape as the SELECTS_FROM-only
-- impact query so the two result sets merge + dedup on q.id.
-- params: [table_qualified]  (bound to BOTH branches — see impact() call site)
SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target
FROM "SqlColumn" src
JOIN "COLUMN_LINEAGE" cl ON cl.src_key = src.id
JOIN "SqlQuery" q ON q.id = cl.query_id
WHERE src.table_qualified = ?
UNION
SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target
FROM "STAR_SOURCE" ss
JOIN "SqlQuery" q ON q.id = ss.src_key
WHERE ss.dst_key = ?
```

Notes on the query:
- The COLUMN_LINEAGE branch keys on `src.table_qualified` (matches the live
  divergence shape: `ba.all_rows_in_selection`'s columns are
  `COLUMN_LINEAGE.src` columns feeding consumers). `cl.query_id` is the consumer
  query — same join `find_table_usages` D3 uses.
- The STAR_SOURCE branch keys on `ss.dst_key = <table>` (the read table) →
  `ss.src_key = q.id` (the reading query). This closes the
  STAR_SOURCE-only-consumer gap that even `find_table_usages` currently has.
- `impact` keys on **`t.qualified`** (the qualified name), not `t.name`. So the
  new query must also key on the qualified table name (`src.table_qualified` and
  `ss.dst_key` are qualified). The developer must bind the **same `?` value
  (`table`) to both placeholders** at the call site.

### Modified call site: `impact()` in `analyze.py`

Keep the existing SELECTS_FROM query as the "direct" pass, then run the new query
and merge+dedup on `id` (the query id), preferring the row already present:

```python
direct = run_read_routed(<existing SELECTS_FROM query>, {"t": table})
via    = run_read_routed(IMPACT_CONSUMERS_VIA_LINEAGE_QUERY, {"t": table, "t2": table})
seen   = {r["id"] for r in direct}
results = direct + [r for r in via if r["id"] not in seen]
```

(Param binding shape must match `run_read_routed`'s positional/`?` convention —
the developer confirms whether two distinct named keys or one repeated bind is
required; `find_table_usages` binds a single `name` once because its D3 query has
one `?`. This query has two `?`, both the same table — bind accordingly.)

Then apply the **existing** noise filter (unchanged) and
`_print_table(results, ["id", "kind"])`.

### No-regression guarantee for the normal SELECTS_FROM case

For an ordinary physical table read via top-level `FROM`, the `direct` pass
returns the consumer and the `via` pass returns the same `q.id` (its columns also
flow via COLUMN_LINEAGE) — deduped away. Result set is **identical** to today's.
Pinned by an explicit no-regression assertion (B3 below). The `LIMIT 100` is
preserved on the direct query; the merged list is sliced to 100 after dedup to
keep the existing bound.

---

## PR #156 reconciliation (REQUIRED)

PR #156 (merged) added
[`tests/integration/test_impact_consumer_parity.py`](tests/integration/test_impact_consumer_parity.py)
under a **NOT-REPRODUCED** gate. Its module docstring asserts the now-DISPROVEN
claim: *"The only tables that have a COLUMN_LINEAGE/STAR read with no SELECTS_FROM
edge are namespaced CTE intermediates (kind='cte')."* The live DWH scan
disproves this: `ba.all_rows_in_selection` is `kind='table'`, non-namespaced, and
exactly that gap. The reconciliation:

### (a) Correct the module docstring
Rewrite the docstring to state the bug **IS reproduced** (live + the new fixture),
remove the "structural reason it cannot reproduce" paragraph, and replace it with
the real root cause: a nested-CTE INSERT…SELECT whose inner CTE is promoted to a
bare `kind='table'` node yields COLUMN_LINEAGE out-edges with **no** SELECTS_FROM
in-edge, so the SELECTS_FROM-only `impact` query under-reports.

### (b) Convert the xfail slot into a real failing-then-passing repro — **GRAPH-LAYER SEEDED (mandated, plan-review FORK-2 resolved)**

**MEASURED FACT (plan-review, empirical):** the nested-CTE INSERT…SELECT fixture
below does **NOT** reproduce the gap in a from-scratch in-memory index — the inner
CTE is **not** promoted to a non-namespaced `kind='table'` node; the only gap node
it produces is `etl.sql::outer_c` (namespaced `kind='cte'`), and `ba.src` (the sole
physical source) already has a SELECTS_FROM edge so `impact('ba.src')` already
passes. The live `ba.all_rows_in_selection` is non-namespaced `kind='table'` only
because of a **cross-file / qualified-name promotion effect** (indexer.py:240-256:
a source-ref node defaults to `kind='table'` and is upgraded to `kind='cte'` ONLY
when the same qualified name is later confirmed a CTE destination in the same pass;
the single-file alias namespaces+confirms → `cte`). A unit fixture cannot reproduce
it. **Do NOT use an indexer fixture as the bug repro** — that is the v1.21.0
ship-green-on-toy-fixture trap.

**MANDATED authoritative repro = graph-layer seeded** (the developer SEEDS the rows
directly, no indexer):
- a `SqlTable` with a **non-namespaced** qualified name (e.g. `ba.promoted_intermediate`), `kind='table'`;
- a `SqlColumn` whose `table_qualified` = that name;
- a `COLUMN_LINEAGE` edge from that column to a consumer `SqlQuery` (e.g. an INSERT into `ba.consumer`);
- **zero** `SELECTS_FROM` in-edges to the seeded table.
Then assert: the OLD `_IMPACT_SQL` (`analyze.py:316-323`) returns **∅** for the seeded
node (the bug), and the NEW union (`IMPACT_CONSUMERS_VIA_LINEAGE` ∪ direct) returns
the consumer (the fix). This test FAILS pre-fix, PASSES post-fix. Param binding for
the two-`?` query: a 2-entry ordered dict (`{"t": name, "t2": name}`) — `_params_list`
([`duckdb_backend.py:650`](src/sqlcg/core/duckdb_backend.py)) returns `list(values())`
in insertion order.

The nested-CTE INSERT…SELECT fixture (below) is **demoted to a NO-REGRESSION case
only** — it proves `impact('ba.src')` is unchanged (direct == merged for a physical
source), NOT that the bug reproduces:

```sql
CREATE TABLE ba.src (col1 INT, col2 INT);
CREATE TABLE ba.tgt (col1 INT, col2 INT);
INSERT INTO ba.tgt WITH outer_c AS (
  WITH all_rows AS (SELECT col1, col2 FROM ba.src) SELECT col1, col2 FROM all_rows
) SELECT col1, col2 FROM outer_c;
```

Remove the `xfail(strict=True)` slot; the seeded test is a normal failing→passing test.

### (c) Reconcile the PR #156 docstring + invariant test (plan-review B2/B3)
- **Docstring (a):** rewrite to state the bug reproduces **at DWH scale via cross-file
  qualified-name promotion** (cite the live `ba.all_rows_in_selection`), and is pinned
  in tests by a **graph-layer seeded** fixture — NOT by an indexer fixture (which the
  measurement shows produces only a namespaced `kind='cte'` gap node).
- **`test_impact_matches_full_consumer_set_for_physical_target`** genuinely holds (union
  is a superset, never drops the physical consumer) — keep green.
- **`test_only_cte_intermediates_have_lineage_read_without_selects_from`** asserts the
  **disproven** `all("::" in g)` invariant. Rewrite to ONE of: (i) assert the corrected
  positive behaviour on the **seeded** graph (post-fix `impact` surfaces the consumer of
  a seeded non-namespaced `kind='table'` gap node); OR (ii) keep an indexer-level
  invariant scoped to what indexers actually emit (`all("::" in g)` holds for
  *indexer-produced* gaps) and document that the DWH cross-file case is covered by the
  seeded test. **Do NOT assert that an indexer produces a non-namespaced `kind='table'`
  gap node — it does not.** Recommended: (i).

---

## Implementation Steps

### Phase 1 — Query + call site
**Step 1.1** — Add `IMPACT_CONSUMERS_VIA_LINEAGE` to
[`queries.sql`](src/sqlcg/core/queries.sql) and export
`IMPACT_CONSUMERS_VIA_LINEAGE_QUERY` in
[`queries.py`](src/sqlcg/core/queries.py).
- Acceptance: the constant loads (no `KeyError` from the `_Q` loader); grep shows
  the export.

**Step 1.2** — Rewire `impact()` in [`analyze.py`](src/sqlcg/cli/commands/analyze.py)
to run direct + via-lineage and merge+dedup on `id`, preserving `LIMIT 100` on the
merged result and the existing noise filter + `_print_table(results, ["id","kind"])`.
- Files: `analyze.py` (`impact`), `queries.py`, `queries.sql`.
- Acceptance: grep-confirmed call site for `IMPACT_CONSUMERS_VIA_LINEAGE_QUERY`
  (the `impact()` body). No new helper without a call site.

### Phase 2 — Tests (PR #156 reconciliation)
**Step 2.1** — Correct the module docstring (a).
**Step 2.2** — Add the promoted-intermediate repro fixture + test (b);
verify it fails on pre-fix, passes on post-fix.
**Step 2.3** — Rewrite `test_only_cte_intermediates_have_lineage_read_without_selects_from`
to the corrected invariant; keep `test_impact_matches_full_consumer_set_for_physical_target`
green (c).

### Phase 3 — Version + housekeeping
**Step 3.1** — Bump `pyproject.toml` + `src/sqlcg/__init__.py` to `1.34.1`; `uv lock`.
**Step 3.2** — `ruff check` / `ruff format` / `pyright` clean; full `pytest` green.

---

## Acceptance Criteria (observable)

- [ ] **A1 — Promoted-intermediate parity (GRAPH-LAYER SEEDED, plan-review FORK-2):**
  a test SEEDS the graph directly — a non-namespaced `kind='table'` `SqlTable`, a
  `SqlColumn` with `table_qualified` = that name, a `COLUMN_LINEAGE` edge to a consumer
  `SqlQuery`, and ZERO `SELECTS_FROM` in-edges — then asserts the OLD `_IMPACT_SQL`
  returns **∅** (bug) and the NEW union returns the consumer (fix). **FAILS on pre-fix,
  PASSES on post-fix.** This is the authoritative repro. **Do NOT use an indexer fixture
  here — measurement proved it produces only a namespaced `kind='cte'` gap node and
  cannot fail pre-fix.**
- [ ] **A2 — No-regression via the nested-CTE indexer fixture:** index the nested-CTE
  fixture and assert `impact('ba.src')` (the physical source) is UNCHANGED before/after
  (direct == merged). This fixture is a no-regression case ONLY, NOT the bug repro
  (the bug needs the cross-file promotion the fixture doesn't produce). The live
  `ba.all_rows_in_selection` is cited in the docstring as the field case.
- [ ] **A3 — No regression on normal tables:** a physical table read via a
  top-level `FROM` returns the **exact same** consumer set from `impact` before
  and after the fix (the parametrized `test_impact_matches_full_consumer_set_for_physical_target`
  stays green; add an explicit assertion that `direct == merged` for a plain
  `INSERT … SELECT … FROM ba.src` so the dedup is proven not to add/drop rows).
- [ ] **A4 — No double-counting:** for a consumer reachable via BOTH SELECTS_FROM
  and COLUMN_LINEAGE, `impact` lists it exactly once (dedup on `q.id` asserted).
- [ ] **A5 — Output schema unchanged:** `impact` still renders columns `id`,
  `kind` (asserted on the result-row keys), no new/renamed columns.
- [ ] **A6 — PR #156 docstring corrected** and the disproven-invariant test
  rewritten; full suite green.

> Test naming: developer names each `test_<unit>_<scenario>_<expected>` and links
> this plan in the docstring. Describe by behaviour, not by criterion code.

---

## Test Strategy

- **Integration** (real in-memory DuckDB + `Indexer`, the existing
  `test_impact_consumer_parity.py` harness):
  - promoted-intermediate repro (A1/A2): index the nested-CTE fixture, compare
    `impact` consumer set vs the union helper already in the file (`_union_consumers`).
  - no-regression (A3/A4): plain INSERT…SELECT — `impact` set unchanged + single-row dedup.
  - schema (A5): assert result-row keys.
- **Unit:** none required (pure SQL/call-site change); the query loader test
  already validates `_Q` keys exist.
- Reuse the file's existing `_index` / `_impact_consumers` / `_union_consumers`
  helpers — no new helper without a call site.

---

## Metric note

This is a **CLI-output completeness fix**, not a graph-coverage change — it does
**not** move a `gain` coverage % (no edges added/removed; the graph is
unchanged). No new `gain` gate is required. It does NOT touch an impact-related
counter (e.g. `COUNT_EXTERNAL_CONSUMERS`), so no counter gate. If the
plan-reviewer wants a regression signal, the integration parity test is the
guard (a future query change that re-narrows `impact` turns A1 red). State
explicitly in the PR body: "no gain delta expected; verified the graph row/edge
counts are identical pre/post on the fixture."

---

## Constraints / safety

- **Four frozen perf suites untouched** — this is a CLI read-query change; no
  parser/indexer hot path. `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`,
  `test_bulk_upsert_invariant.py`, `test_upsert_batch_invariant.py` must stay green
  but require no edits.
- **Every new query/helper needs a grep-confirmed call site** before the PR opens
  (`IMPACT_CONSUMERS_VIA_LINEAGE_QUERY` → `impact()`).
- **No backward-compat shims.** No TODO in the happy path.
- **Path/constant fallbacks must match `KuzuConfig`** — N/A here (no path/constant added).
- **Version PATCH** `1.34.1`; reconcile at merge if another PATCH lands first.

---

## Non-Goals

- Fixing `FIND_TABLE_USAGES`'s own missing STAR_SOURCE join (separate follow-up;
  note for the plan-reviewer).
- Changing `downstream`/`empty-impact` (they already work — they are the parity
  reference).
- Making `impact` transitive / multi-hop (that is `downstream`'s job; `impact`
  stays one-hop "who reads this table").
- Any parser/indexer change to alter how promoted intermediates are materialized.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The repro fixture doesn't reproduce a non-namespaced `kind='table'` promoted node in a from-scratch in-memory index (promotion may be a DWH-scale/cross-file effect) | FORK-2: pin the repro at the graph layer (synthesize the node+edges or assert on the indexed graph shape) so it genuinely exhibits the gap; plan-reviewer confirms before sign-off |
| Union changes behaviour for normal tables (over-report) | A3/A4 dedup assertions: `direct == merged` on plain reads; single row per consumer |
| `q.target_table` NULL/'' sentinel rows leak as bogus consumers | Existing noise filter already drops `target == ''`; the new query returns `q.kind`/`q.target` like the old one — same downstream filter applies |
| Param-binding mismatch (two `?`, same table) breaks at runtime | Developer confirms `run_read_routed` bind convention against `find_table_usages`/existing two-placeholder queries; integration test exercises the real call path |
