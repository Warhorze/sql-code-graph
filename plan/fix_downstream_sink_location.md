# Feature Plan: Downstream sink `file:line` recall

> Status: **plan-reviewed — READY FOR IMPLEMENTATION.**
> Source: ARCHITECTURE_REVIEW.md §19.2 (v1.2.1 live DWH re-verification, 2026-06-02).

## Summary

`analyze downstream` renders `file:line='?'` for ~81% of result rows (29,827 of
36,892 distinct downstream columns on the DWH corpus) because terminal sinks have no
*outgoing* `COLUMN_LINEAGE` edge to bind location from. Bind downstream-result
location from the **incoming** edge (the producing query) instead, flipping ~81% of
`?` rows to a real `file:line`.

## Scope

### In Scope
- The two downstream queries in [`analyze.py`](src/sqlcg/cli/commands/analyze.py)
  (`downstream` primary + bare-name fallback): change the location-binding
  `OPTIONAL MATCH` from `(dst)-[dstedge]->()` to `()-[dstedge:COLUMN_LINEAGE]->(dst)`.
- A behavioural guard asserting a **terminal sink** (a column with no outgoing edge)
  renders a real `file:line`, not `?`.

### Non-Goals
- The upstream queries (`<-[…]-(src)` + `(src)-[srcedge]->()`). The outgoing-edge
  binding is **correct** for upstream — do not touch lines 73–94 of `analyze.py`.
- Finding 19.1 (scratch-object classification leak) — separate, LOW-priority ticket.
- Any change to the kind-filter (`_kind_filter`, s18.3), schema, indexes, or parser.
- Re-binding `q.start_line`/`q.file_path` semantics anywhere else (server tools).

## Design

### Query change (the load-bearing edit)

Current downstream binding ([`analyze.py`](src/sqlcg/cli/commands/analyze.py)
lines 134–137, and the bare-name fallback 146–149):

```cypher
OPTIONAL MATCH (dst)-[dstedge:COLUMN_LINEAGE]->()
OPTIONAL MATCH (q:SqlQuery {id: dstedge.query_id})
WITH dst, min(q.start_line) AS line, min(q.file_path) AS file
```

Change the first arrow to bind from the **producing** query (incoming edge):

```cypher
OPTIONAL MATCH ()-[dstedge:COLUMN_LINEAGE]->(dst)
OPTIONAL MATCH (q:SqlQuery {id: dstedge.query_id})
WITH dst, min(q.start_line) AS line, min(q.file_path) AS file
```

Rationale: every downstream result `dst` has an incoming edge by definition (that is
how it became reachable), and that edge's `query_id` points at the query that
produced the column — the user-meaningful "where is this column written" location.
A terminal sink has no *outgoing* edge (today's `?`) but always has an incoming one.

Keep `OPTIONAL MATCH` (not `MATCH`) so a `dst` whose incoming edge has a NULL/missing
`query_id` still returns a row (degrades to `?` rather than dropping the row).

### Data Models / Dependencies
None. No schema, index, or dependency change.

## Implementation Steps

### Phase 1: Query rewrite
**Step 1.1**: Flip the location-binding arrow in both downstream queries.
- Files affected: [`analyze.py`](src/sqlcg/cli/commands/analyze.py) (downstream
  primary ~134, bare-name fallback ~146).
- Acceptance: both queries bind `()-[dstedge]->(dst)`; upstream queries unchanged
  (grep confirms `(src)-[srcedge]->()` still present at lines 77 / 89).

### Phase 2: Guard
**Step 2.1**: Add `test_tc6b_downstream_sink_location_present` to
[`test_user_surface_recall_guard.py`](tests/integration/test_user_surface_recall_guard.py).
- The terminal sink to use from the existing A–D corpus is **`mart.fact_kpi.measure`**.
  It is the final INSERT target — it has no outgoing `COLUMN_LINEAGE` edge (nothing
  downstream consumes it), but it IS reachable as a downstream result from
  `staging.src_a.x`. Confirm this via `MATCH (c:SqlColumn {id: 'mart.fact_kpi.measure'})-
  [:COLUMN_LINEAGE]->() RETURN count(*)` returning 0 before relying on it.
- Build the downstream query using `_kind_filter("dst", include_intermediate=False)` —
  the same helper `analyze downstream` calls — so the guard drives the production query
  path, not raw edge traversal (ARCHITECTURE_REVIEW.md §19.3).
- Start the traversal from `staging.src_a.x` (a physical source) and assert that the
  row for `mart.fact_kpi.measure` carries a non-null `file` field. Assert observable
  output (non-null `file` value), not "no exception raised".
- Acceptance: the new test is RED against the current outgoing-edge query on master and
  GREEN after the Step 1.1 flip. Verify by running it before and after the edit.

## Test Strategy
- Integration: the new TC6b guard on the in-fixture sink (above).
- Manual/live (optional, recorded in plan postmortem, not committed — DWH e2e is
  gitignored): re-run `analyze downstream` on a DWH column and confirm the `?` rate
  drops from ~81% toward ~0% for sinks.
- Regression: full `uv run pytest` — confirm TC6 (upstream) and the kind-filter
  guards (TC1/TC4) stay green; no perf/scaling guard touched (query-only change).

## Acceptance Criteria
- [ ] Both downstream queries bind location from the incoming `COLUMN_LINEAGE` edge.
- [ ] Upstream queries are unchanged (outgoing-edge binding preserved).
- [ ] A terminal-sink downstream result renders a real `file:line` (TC6b green).
- [ ] TC6b is demonstrably RED on `master` before the fix (proves it guards the bug).
- [ ] Full test suite green; no perf/scaling guard regression.
- [ ] Version bumped to **1.2.2** (patch) per CLAUDE.md release process.
      **Bundling decision (2026-06-02, supersedes the original standalone plan):** this
      sink fix and the v1.2.1 bugfix scope ship together as a single **v1.2.2** release;
      the lower `v1.2.1` tag is **skipped** (precedent: v1.1.2→v1.1.3). The bundled work
      lives on `feat/v1.2.1-bugfix` (PR #48, fast-forwarded to `b5bf7a3`). The combined
      plan-compliance verdict is recorded in
      [`v1_2_1_bugfix.md`](v1_2_1_bugfix.md) under "Bundled v1.2.2 release compliance".

## Risks and Mitigations
- **Risk: a `dst` with multiple incoming edges from different queries.** `min()`
  aggregation already collapses fanout (same pattern as #45.1); pick the
  lowest-line producing query deterministically. Mitigation: keep the
  `WITH dst, min(...), min(...)` aggregate-before-`LIMIT` shape exactly as upstream.
- **Risk: TC6 false-confidence (it passes on a located case).** Mitigation: Phase 2
  explicitly targets a *sink* — `mart.fact_kpi.measure` with zero outgoing edges —
  the case TC6 misses; prove RED-on-master before the fix.
- **Risk: scope creep into server tools.** Out of scope — this ticket is the CLI
  `analyze downstream` path only.
