# Fix Plan: `analyze impact` under-reports consumers (Bug #6)

**Status:** DRAFT (a plan-reviewer gates this before any implementation)
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

### (b) Convert the xfail slot into a real failing-then-passing repro
The file currently has no live fixture in `IMPACT_PARITY_CASES` that exhibits the
gap (every listed shape resolves to a *physical* source that DOES have a
SELECTS_FROM edge). Add the **verifier's fixture recipe** — a nested-CTE
INSERT…SELECT where an inner CTE is promoted to a bare `kind='table'` node:

```sql
-- DDL
CREATE TABLE ba.src (col1 INT, col2 INT);
CREATE TABLE ba.tgt (col1 INT, col2 INT);
-- ETL (inner CTE all_rows promoted to a bare kind='table' node ba.all_rows)
INSERT INTO ba.tgt
WITH outer_c AS (
  WITH all_rows AS (SELECT col1, col2 FROM ba.src)
  SELECT col1, col2 FROM all_rows
)
SELECT col1, col2 FROM outer_c;
```
target node: **`ba.all_rows`** (the promoted intermediate — verify with the
indexer that it materializes as `kind='table'`, non-namespaced, with
COLUMN_LINEAGE out-edges and 0 SELECTS_FROM in-edges; if the indexer namespaces
it as `ba.all_rows::...`, the developer must capture the **actual** promoted key
the indexer emits and assert on that — the fixture's value is that it reproduces
the *gap*, not the exact string).

The repro test must, **on the pre-fix code, FAIL** (old `impact` query returns
empty for the promoted node while the union/`downstream`/`empty-impact` set is
non-empty) and **on the post-fix code, PASS** (impact matches the union). The
developer adds it as a standalone test (not an `xfail` — the
gate is now REPRODUCED, so it is a normal failing→passing test) and removes any
`xfail(strict=True)` slot tied to the disproven gate.

> If the indexer does **not** reproduce a non-namespaced `kind='table'` promoted
> node in a from-scratch in-memory index of this fixture (i.e. the promotion only
> happens at DWH scale / cross-file), this becomes **FORK-2**: the repro must be
> pinned at a lower layer — assert directly on the graph that a `kind='table'`
> non-namespaced node with COLUMN_LINEAGE out-edges and 0 SELECTS_FROM in-edges
> makes the old query return ∅ and the new union return the consumer. The
> developer must report which path reproduces and the plan-reviewer confirms the
> fixture genuinely exhibits the failure mode before sign-off (do not ship a
> fixture that cannot exhibit it — that is how the v1.21.0 dual-write shipped
> green).

### (c) Keep the parity assertions that genuinely hold
`test_impact_matches_full_consumer_set_for_physical_target` (the parametrized
physical-table parity) genuinely holds and must stay green **after** the fix too
(the union is a superset, never drops the physical consumer). Keep it; update its
docstring only where it references the NOT-REPRODUCED gate. The
`test_only_cte_intermediates_have_lineage_read_without_selects_from` test asserts
the **disproven** invariant (`all("::" in g for g in gap)`) — it must be
**rewritten** to assert the *correct* invariant: a promoted non-namespaced
`kind='table'` node CAN appear in the gap, and `analyze impact` (post-fix) still
surfaces its consumer. Do not delete the coverage; re-aim it at the true behaviour.

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

- [ ] **A1 — Promoted-intermediate parity:** an integration test indexes the
  nested-CTE fixture and asserts `impact`'s consumer set on the promoted node
  equals the `SELECTS_FROM ∪ STAR_SOURCE ∪ COLUMN_LINEAGE` union set **and is
  non-empty** (the same set `downstream`/`empty-impact` reach). FAILS on pre-fix
  code, PASSES on post-fix.
- [ ] **A2 — Live-case guard:** a test (or the A1 fixture, documented as the
  reduction of the live case) demonstrates that a `kind='table'`, non-namespaced
  node with COLUMN_LINEAGE out-edges and 0 SELECTS_FROM in-edges — the
  `ba.all_rows_in_selection` shape — now surfaces its consumer via `impact`. The
  docstring cites the live case.
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
