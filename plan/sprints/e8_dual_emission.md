# E8 dual-emission — feature sprint

**Status:** REVIEWED-by-research (feasibility proven in
[`plan/research/e8_dual_emission_feasibility.md`](../research/e8_dual_emission_feasibility.md))
**Version:** 1.29.0 (minor — additive new resolution surface, no break)
**Branch:** `feat/e8-dual-emission`

## Problem

The E8 temp-chain recall fix and the shipped temp-table-namespacing feature demand
**opposite lineage shapes for the identical SQL pattern**: E8 wants the
`source→sink` edge inlined through the temp; namespacing wants the temp to stay a
visible intermediate node with edges on both sides. E8's original mechanism (registering
the schema-qualified `sources_map` key so `exp.expand()` *replaces* the temp body) is
**destructive** — it collapses the temp out of the graph, regressing 3 namespacing tests.

## Design (one line)

Leave `sources_map` registration **bare-key-only** (no `exp.expand()` inlining → temp
stays visible → namespacing green). After the per-statement loop in `parse_file`, run an
**O(edges) composition pass** that, for every out-of-temp edge `E` (`E.src.table.role ==
'temp'`), splices in the temp's incoming edges `F` (matched on column name) and emits an
**additive** `LineageEdge(src=F.src, dst=E.dst, transform='TEMP_INLINE')`, dropping
post-alias self-loops. Consumers pick the view via the `transform` tag. No schema
migration — the `transform STRING` column ([`schema.cypher:95`](../../src/sqlcg/core/schema.cypher))
is already plumbed.

## What shipped

### 1. Parser
- New helper `AnsiParser._emit_transitive_temp_edges(out)`
  ([`ansi_parser.py:264`](../../src/sqlcg/parsers/ansi_parser.py)), called from
  `parse_file` immediately before the namespace reset
  ([`ansi_parser.py:256`](../../src/sqlcg/parsers/ansi_parser.py)).
- Class constant `_TEMP_INLINE_MAX_DEPTH = 8`
  ([`ansi_parser.py:33`](../../src/sqlcg/parsers/ansi_parser.py)) bounds the gap-B
  fixpoint.
- Pure in-memory edge composition: builds one `dict[(temp_full_id, col) → incoming
  edges]` then a bounded-fixpoint pass over out-of-temp edges. **No `exp.expand`/
  `qualify`/`build_scope`/`sg_lineage`** — off the per-column hot path. Refs are already
  alias-normalized at construction (`_apply_table_alias`), so the self-loop guard sees
  post-alias identities.

### 2. Metrics — structural-only counting + separate transitive counter
Recall/health count **STRUCTURAL edges only** (the transitive edge double-counts
provenance already captured by the two structural hops). Added a NULL-safe
`cl.transform IS DISTINCT FROM 'TEMP_INLINE'` filter to:
- `_Q_RESOLVABLE_WRITE_COL_EDGES` ([`coverage.py:367`](../../src/sqlcg/cli/coverage.py))
- `_Q_EDGE_HEALTH_STRICT` ([`coverage.py:62`](../../src/sqlcg/cli/coverage.py))
- `_Q_EDGE_HEALTH_SCOPED` ([`coverage.py:74`](../../src/sqlcg/cli/coverage.py))

(`IS DISTINCT FROM`, not `<>`: a NULL `transform` on a synthetic fixture row must NOT be
excluded — `NULL <> 'X'` is NULL/false in 3-valued logic, which would silently drop real
edges.)

NEW separate counter `transitive_col_edges`
([`coverage.py:381` `_Q_TRANSITIVE_COL_EDGES`](../../src/sqlcg/cli/coverage.py)) =
`COUNT(*) WHERE transform='TEMP_INLINE'`, surfaced in `gain` (render + JSON) and the
`CoverageStats` dataclass. **Deliberately NOT folded into recall/health.**

### 3. Consumer — view selector
`trace_column_lineage(table_col, max_depth, view='structural')`
([`tools.py:756`](../../src/sqlcg/server/tools.py)):
- `view='structural'` (default): filters OUT `TEMP_INLINE` rows. The BFS still reaches
  every leaf via the structural hops, so the shortcut adds no reachability — only
  diagram clutter.
- `view='source_to_sink'`: keeps ONLY `TEMP_INLINE` rows (temps-elided one-hop "what real
  table feeds this column?"). Filter applied via `_select_rows` on both BFS loops.

Table-level (`analyze impact` / `table_graph` via SELECTS_FROM) is unchanged — unaffected
by dual-emission.

## Gap resolutions (shepherd-review findings)

### Gap A — dedup collision (structural precedence)
The batch dedup chokepoint keys on `(src_key, dst_key)` only, NOT `transform`. If a sink
reads a source BOTH directly (`PASS_THROUGH leaf→sink`) AND via a temp (`TEMP_INLINE
leaf→sink`), they collide and the simple last-wins dict would drop the real-provenance
edge. **Resolution:** extracted `_dedup_column_lineage_edges(rows)`
([`indexer.py:167`](../../src/sqlcg/indexer/indexer.py), called at
[`indexer.py:~248`](../../src/sqlcg/indexer/indexer.py)) — transform-aware with
deterministic **structural precedence**: an existing non-`TEMP_INLINE` row is never
replaced by a `TEMP_INLINE` row for the same key (both orderings handled). Test:
`TestDualEmissionGapACollisionPrecedence`.

### Gap B — multi-temp chains (chosen: transitive closure)
A single-level pass yields `temp1→sink` and `leaf→temp2` but never `leaf→sink`.
**Chosen (i): iterate to a bounded fixpoint** (`_TEMP_INLINE_MAX_DEPTH=8`) so
`leaf→temp1→temp2→sink` emits a fully-composed `leaf→sink` `TEMP_INLINE` edge. Each round
re-feeds the prior round's `TEMP_INLINE` edges (whose dst is itself a temp) into the
incoming index; a per-`(src,sink,col)` dedup ledger guarantees termination and bounds the
fan-in×fan-out multiplication (the §7d cap-by-construction). Cost stays O(edges·depth).
**Why closure over documenting-the-limit:** it is cheap (the ledger collapses repeats),
deterministic, and gives the complete `source→sink` answer the E8 recall intent wants.
Test: `TestDualEmissionMultiTempChain` asserts the closure edge.

## Gates (all reported with exact numbers)

### Full suite — `uv run pytest`
**1716 passed, 7 skipped, 1 xfailed, 0 failed** (457.73s). ruff clean, pyright 0 errors.
The 4 perf-invariant suites pass **UNMODIFIED** (not in the diff): `test_T09_01_qualify_once.py`,
`test_bulk_upsert_invariant.py`, `test_upsert_batch_invariant.py`,
`test_perf_scaling_guard.py` — 13 passed. E8 suite (8) + namespacing (21+32=53) all green.

### Live-DWH A/B — corpus `/home/ignwrad/Projects/dwh` (1335 indexed files)
Baseline = master `292da6a` (`/tmp/e8_baseline.duckdb`); feature = this branch
(`/tmp/e8_feature.duckdb`). Identical corpus, 189 parse_failed both sides.

| metric | baseline | feature | delta |
|---|---|---|---|
| `resolvable_write_col_edges` (STRUCTURAL) | 35432 | 35429 | −3 (noise) |
| `transitive_col_edges` (TEMP_INLINE) | 0 | **4579** | **+4579** |
| `good_edges_strict` | 46156 | 46153 | −3 (noise) |
| `good_edges_scoped` | 36593 | 36590 | −3 (noise) |
| `total_edges` | 54736 | 59312 | +4576 |
| structural CL edges (direct) | 54736 | 54733 | −3 (noise) |

(i) **Structural delta:** dual-emission is **additive** and by design (§6.1) does NOT add
   structural edges, so `resolvable_write_col_edges` is held flat (−3 = nondeterminism on
   the 1 timeout-boundary file; a second feature re-index reproduced structural=54733
   identically). **The new resolution surface — +4579 — lives entirely in
   `transitive_col_edges`**, well above the predicted ≥+1,728 floor. See Deviations below
   for the gate-wording reconciliation.
(ii) **Multiplication check:** TEMP_INLINE total 4579 = **0.62× the 7354 structural temp→*
   edges** — bounded, NOT pathological (cap trigger is >10× structural). Per-column fan:
   max 27 TEMP_INLINE into one sink column, max 85 out of one source column. **No §7(d)
   cap applied** (not needed).
(iii) **Perf:** parse+clone+ingest profile (feature): pass1 13.18s, pass2 8.85s, upsert
   56.68s, star 5.18s, **total 84.21s** (baseline 91.86s). Faster than baseline and well
   under the ~150s budget. pass1/pass2 unchanged within noise vs baseline → the
   composition pass adds no measurable parse cost, consistent with O(edges) off-hot-path.
(iv) **Health:** strict/scoped −3 each (noise, not a drop); catalogued_tables unchanged
   (5794). No drop vs baseline.

### Gain snapshot
`plan/metrics/gain_1.29.0_<sha>.json` (committed). Before→after: the only metric that
moved is the new `transitive_col_edges` (0 → 4579); all structural/health metrics held
flat within index nondeterminism.

## Version
1.28.4 → **1.29.0** (minor): pyproject.toml, src/sqlcg/__init__.py, uv.lock.

## Deviations / Deferrals
- **Gate (i) wording vs §6.1 policy.** The gate asks for a STRUCTURAL
  `resolvable_write_col_edges` delta ≥ +1,000, but the spike's own metric-counting policy
  (§6.1, which this PR implements) holds structural recall flat and routes the new surface
  to a SEPARATE `transitive_col_edges` counter to avoid double-counting. These are
  mutually exclusive: a structural-only metric **cannot** rise from an additive transitive
  edge. We satisfied the policy (structural held flat, no drop) and the intent (+4579 new
  resolved `source→sink` edges, > the +1,728 floor) via `transitive_col_edges`. Reported
  transparently rather than inflating the structural counter.
- **No §7(d) cap** — the fan-in×fan-out check came in at 0.62× structural, far below the
  trigger.
- Multi-temp chains: chose transitive closure (gap B option i), not the
  document-as-limitation path.
