# Feature Plan: v1.2.1 — open lineage bug fixes + effective regression guards

## Summary

A patch release (`1.2.0` → `1.2.1`) that fixes the remaining user-facing lineage
defects (#44 canonical-name split, #45 `file:line=?` + `cte_*` leak), repairs a
filter/guard regression introduced by the v1.2.0 read-proxy, and lands the
[#40 user-surface regression-guard](fix_issue_40_user_surface_regression_guards.md)
design as the verification layer so these classes of bug cannot ship green again.

---

## Pre-work findings (read before scoping — these reshape the obvious plan)

The brief lists four "open" bugs and two "residual" bugs. Verifying against current
`master` (1.2.0) instead of the 1.1.2-era findings doc materially changes the scope:

1. **#39 source-table node emission is ALREADY in code.** [`indexer.py`](../src/sqlcg/indexer/indexer.py)
   `_build_file_rows` (around the `for edge in stmt.column_lineage:` loop, ~L1129)
   carries a comment `# Half A (#39): emit a SqlTable node for the source table` and
   emits `edge.src.table` with `kind = edge.src.table.role`, derived from the same
   `TableRef` as the column's `table_qualified` (the alias-desync guard #39 asked for).
   Guard tests exist: [`test_cte_source_node_invariant.py`](../tests/integration/test_cte_source_node_invariant.py),
   [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py).
   **#39 the *parser/indexer* half is done.** What is NOT done is the *read-surface*
   half — see finding 3.

2. **Residual #38 (UNION-ALL of sibling CTEs, `SELECT *`) is ALREADY fixed.** Commit
   `f6f276b fix(#38): derive union-CTE columns from deepest branch SELECT — preserve hop`
   shipped in 1.1.3; [`base.py`](../src/sqlcg/parsers/base.py) ~L964-992 handles
   `Union(Union(A,B),C)` by walking to the deepest left-branch `Select` and passing the
   whole `Union` body to `sg_lineage`. Guard:
   [`test_union_cte_star_recall_guard.py`](../tests/integration/test_union_cte_star_recall_guard.py).
   The `findings_v1.1.2_dwh_eval.md` doc that reports the 80-island residual was run
   against **1.1.2**, *before* this fix. **→ Out of scope for 1.2.1** (already shipped);
   re-verification against the DWH corpus belongs to a separate eval pass, not a code change.

3. **v1.2.0 read-proxy regressed the #38/#39 read-surface filter (NEW, must fix).**
   The read-proxy rewrite (`b7b3449`) replaced the `analyze._kind_filter` helper with an
   **inline inner-join** clause in [`analyze.py`](../src/sqlcg/cli/commands/analyze.py)
   (L38-43 `upstream`, L98-103 `downstream`):
   ```
   MATCH (t:SqlTable {qualified: src.table_qualified}) WHERE t.kind IN ['table','external']
   ```
   This is exactly the #38 inner-join pattern. The earlier fix used an **OPTIONAL MATCH +
   `t.kind IS NULL OR t.kind IN [...]`** ("Half B") so that a node-*less* physical source
   (one whose `SqlTable` node is still missing, the #39 tail case) is *kept*, not dropped.
   The revert silently re-narrows recall, and it deleted `_kind_filter`, so
   `test_cte_recall_guard.py` (which imports it) now **errors at collection** — the guard
   that should have caught this is itself broken. Confirmed:
   `ImportError: cannot import name '_kind_filter'`.
   **→ In scope, and it is the de-facto #39 read-surface fix.**

4. **#28 server-side write lock is partly addressed and the remainder is not a patch.**
   v1.2.0 shipped a server-routed read proxy ([`read_client.py`](../src/sqlcg/server/read_client.py)
   `run_read_routed`), so CLI reads now route *through* the server when it is live —
   the practical #28 symptom (CLI reads failing while the server holds the lock) is
   handled by routing, not by changing the lock. The deeper "server should open read-only
   and escalate around reindex" change is a connection-lifecycle redesign.
   **→ Out of scope for 1.2.1** (see §"Scope decision: #28" for reasoning).

Net effect: the real 1.2.1 code work is **#44**, **#45**, and the **read-proxy filter
regression** (which is #39's read half), all verified by **landing the #40 guard suite**.

---

## Scope

### In scope
- **Read-proxy filter regression** — restore the OPTIONAL-MATCH / `kind IS NULL OR kind IN [...]`
  filter and re-introduce a single shared `_kind_filter` helper (used by both `upstream` and
  `downstream`, and importable by the guard test). Repairs the broken guard import.
- **#44** — INSERT-target node normalization: resolve an unqualified / wrong-schema INSERT
  target (`ba_tmp.<t>`, or schema-less `<t>`) to the canonical DDL-defined table (`ba.<t>`)
  when exactly one DDL table carries that bare name, so lineage attaches to the name a user types.
- **#45.1** — populate `file:line` on every physical-source upstream/downstream row, not just
  the immediate neighbour of the queried column.
- **#45.2** — stop synthetic `cte_*` / derived-alias nodes leaking into default (non-`--raw`)
  output.
- **#40** — land the user-surface regression-guard suite from
  [`fix_issue_40_user_surface_regression_guards.md`](fix_issue_40_user_surface_regression_guards.md)
  (synthetic committable fixture A–D + TC1–TC7), as the verification layer for the above.
- **`resolve_pass2` dead-code dedup** — delete the dead `CrossFileAggregator.resolve_pass2`
  (zero production call sites; production inlines its logic in `index_repo`) and realign its tests
  at the production pass-2 path. Pure dedup + test realignment, no graph-output change. This is the
  root cause of B1 — the same #40-class blind spot (tests measuring a path production doesn't run).
- **Version bump** to `1.2.1` (pyproject, `__init__.py`, `uv lock`).

### Non-goals
- Residual #38 union-CTE-star (already fixed in 1.1.3 — see finding 2).
- Server read-only-by-default + reindex escalation for #28 (separate minor — see scope decision).
- Re-deriving the #40 design — it is reused as-is from the existing doc.
- Any change to `base.py` / `indexer.py` hot paths that touches the
  [Performance invariants](../CLAUDE.md). #44's node fix is in the *node-emission* loop
  (`_build_file_rows`), not the per-column lineage loop, and must dedup to a no-op on re-emit.
- DWH-corpus re-evaluation (no committable fixture; the synthetic A–D fixture is the CI line of defence).

---

## Design

### Read-proxy filter regression (restore `_kind_filter`, OPTIONAL-MATCH form)

`analyze.py` currently inlines the inner-join filter twice. Reintroduce one helper:

```python
def _kind_filter(node_alias: str, *, include_intermediate: bool) -> str:
    if include_intermediate:
        return ""
    return (
        f"OPTIONAL MATCH (t:SqlTable {{qualified: {node_alias}.table_qualified}}) "
        f"WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
    )
```

- `upstream` calls `_kind_filter("src", ...)`; `downstream` calls `_kind_filter("dst", ...)`.
- **Why OPTIONAL + `IS NULL`, not inner-join**: a node-less physical source (the #39 tail
  case, before re-index, or any source the indexer still misses) must survive the filter —
  an inner join drops it (that *is* #38). The CTE/derived intermediates it must still hide
  carry `kind='cte'`/`'derived'`, which `kind IN [...]` excludes and `IS NULL` does not match.
- This is the production source of the string the guard test imports — never mirror it in the test.

### #44 — INSERT-target canonical-name resolution

**Where the index is BUILT vs. where it is APPLIED — these are two different sites:**

*Build the index in `register_pass1`* — [`CrossFileAggregator`](../src/sqlcg/lineage/aggregator.py)
already holds the DDL identity map. `register_pass1` iterates `parsed.defined_tables`
(aggregator.py L34) and records `self.sources[table.full_id] = parsed`. Add a parallel
**bare-name → canonical full_id** index built from those *DDL-defined* tables, in the same loop:

```python
# in __init__:
self.canonical_by_bare: dict[str, str] = {}   # bare name (lower) -> sole DDL full_id
self._ambiguous_bare: set[str] = set()        # bare names defined by >1 schema (do NOT rewrite)

# in register_pass1, in the existing `for table in parsed.defined_tables:` loop (L34):
bare = (table.name or "").lower()
if bare:
    if bare in self.canonical_by_bare and self.canonical_by_bare[bare] != table.full_id:
        self._ambiguous_bare.add(bare)
    self.canonical_by_bare[bare] = table.full_id
```

*Apply the rewrite in `_build_file_rows`* (indexer.py L974), **not** in
`CrossFileAggregator.resolve_pass2`. **`resolve_pass2` has zero production call sites** — the
production pass-2 path in `index_repo` reimplements the dep-filter+reparse logic inline
(indexer.py ~L353-373 computes `dep_names` and dispatches `parse_pass2` tasks; the worker calls
`parser_p2.parse_file(..., dependency_filter=...)` directly in [`pool.py`](../src/sqlcg/indexer/pool.py)
L129; `resync_changed` calls `parse_file()` directly too). `resolve_pass2` is a duplicate kept
alive only by tests (see Phase 5). Targeting it would land the #44 fix in dead code.

The INSERT-target synthetic `SqlTable` node is emitted in `_build_file_rows` at indexer.py
**~L1204-1215** — the `if stmt.target and stmt.target.full_id not in defined_table_ids:` block
that appends a `table_rows` dict with `kind: "table"`. This is the row whose `qualified` becomes
the duplicate schema-less node. Apply the canonical rewrite here, when the row is built:

- **Thread the canonical index in alongside `defined_table_registry`**, which already flows into
  `_build_file_rows` (signature `_build_file_rows(self, parsed, defined_table_registry=None)`,
  indexer.py L974-977; called from `_upsert_file_batch` L1246 and `_upsert_parsed_file` L1286).
  Pass `aggregator.canonical_by_bare` / `aggregator._ambiguous_bare` down the same call chain
  (`index_repo` → `_upsert_file_batch` / `_upsert_parsed_file` → `_build_file_rows`).
- **Resolution rule:** when building the INSERT-target row for a `stmt.target` that is unqualified
  *or* whose `full_id` is not itself a DDL-defined table (`stmt.target.full_id not in
  defined_table_ids`), if `stmt.target.name.lower()` maps to exactly one DDL canonical (present
  in `canonical_by_bare`, **not** in `_ambiguous_bare`), use the canonical `full_id` (and its
  `name`/`db`/`catalog`) for the emitted row instead of the raw target. Ambiguous bare names are
  left untouched — the existing `_bare_ref` CLI hint already covers the multi-schema case; do not
  silently merge across schemas.

**Critical (CLAUDE.md alias rule):** the emitted row's `qualified` must be *identical* to the DDL
table's `full_id`, so the INSERT-target `SqlTable` node, its `SqlColumn.table_qualified`, and the
DDL node all share one identity. Mirror the `#39 Half A` discipline: derive every emitted field
from the same resolved canonical, not a mix of canonical id + raw name.

**Re-emission must dedup to a no-op** — when the canonical node already exists from the DDL pass,
`upsert_nodes_bulk` (Phase A→B→C) is keyed on `qualified`, so re-emitting the same identity is a
no-op. Do **not** add a per-row `upsert_node` (Performance invariant). The change is pure row-dict
mutation inside the existing node-emission loop — no new `execute()`.

**Kind decision for the emitted node (resolves W2 — see #45.2 below):** today this block hardcodes
`kind: "table"` for *every* INSERT target, including synthetic ones. Phase 2 changes this so that
**only** rows resolved to a DDL canonical keep `kind: "table"`; a synthetic INSERT target that does
*not* resolve to a DDL table is emitted with a distinct non-physical kind (`kind: "derived"`) so the
read-surface `kind_filter` excludes it. This makes the #45.2 `cte_insert` leak a graph-truth drop
rather than a name-prefix heuristic. See #45.2 for the verification prerequisite.

### #45.1 — `file:line` on every source row

**Root cause:** the current query binds the query/location from the edge **into the queried
column** (`OPTIONAL MATCH (src)-[direct:COLUMN_LINEAGE]->(c)`), which only exists for the
immediate neighbour of `c`. Multi-hop sources have no such direct edge → `direct.query_id`
is null → `q` is null → [`_add_file_line_col`](../src/sqlcg/cli/commands/analyze.py) renders `?`.

**Fix:** bind the location from the edge **leaving the source node** — a `COLUMN_LINEAGE` edge
out of `src` (an edge that defines where `src` is *read*). The
`OPTIONAL MATCH (src)-[srcedge:COLUMN_LINEAGE]->()` pattern is **one row per outgoing edge per
`src`**, so it multiplies rows. **Aggregation is REQUIRED, not conditional** — without it the
existing `LIMIT 100` silently drops the 101st *distinct* `src.id`. Aggregate per source **before**
the `LIMIT`, e.g.:

```
MATCH (c:SqlColumn {id: $ref})<-[:COLUMN_LINEAGE*1..$depth]-(src:SqlColumn)
{kind_filter}
OPTIONAL MATCH (src)-[srcedge:COLUMN_LINEAGE]->()
OPTIONAL MATCH (q:SqlQuery {id: srcedge.query_id})
WITH src, min(q.start_line) AS line, ... AS file   // one row per src.id
RETURN src.id AS id, file AS file, line AS line LIMIT 100
```

(`downstream` mirrors this with `dst` in place of `src`.) The `WITH … min(q.start_line) … ` group
**must collapse to one row per `src.id` before `LIMIT 100`** so the limit counts distinct sources,
not edge-fanout rows.

This requires `query_id` to be populated on `COLUMN_LINEAGE` edges (it is — #31/#32 shipped
`query_id`/`confidence`; the DWH eval confirms `start_line` on all 5630 `SqlQuery` nodes).
A source with several outgoing edges resolves to multiple `(file, line)` pairs; the aggregation
picks deterministically — take `min(q.start_line)` and the `file` of that same edge (carry both
in the grouping key or use the matching `file` for the chosen `min` line). A real `file:line`
beats `?`. TC6 asserts both: every physical-source row carries a real `file:line` **and** the row
count equals the distinct-source count (no fanout multiplication).

### #45.2 — suppress `cte_*` synthetic nodes from filtered output

**Decision (resolves W2): the kind-based filter alone does NOT drop the leak as code stands today.**
The current `_build_file_rows` INSERT-target block (indexer.py ~L1212) emits *every* synthetic
INSERT target with `kind: "table"` — including the `cte_insert` node. So the restored
`kind_filter` (`kind IN ['table','external']`) would still *keep* it. The fix is therefore
**structural, in Phase 2's node emission**, not a separate read-surface heuristic:

- **Chosen approach (graph-truth):** the Phase 2 (#44) node-emission change assigns a **distinct
  non-physical kind (`kind: "derived"`)** to any INSERT-target / synthetic node that does *not*
  resolve to a DDL canonical (see #44 "Kind decision"). With that, the restored
  `kind IN ['table','external']` filter excludes the `cte_insert` leak by graph truth — one change
  fixes both #44 (canonical rows stay `kind: "table"` and survive) and #45.2 (unresolved synthetic
  rows become `kind: "derived"` and are filtered) cleanly, with no name-prefix heuristic.
- **Fixture-verification prerequisite for Phase 3:** before relying on the kind filter alone,
  Phase 3 must confirm on the A–D fixture (shape C: `INSERT INTO mart.fact_kpi` against
  `CREATE TABLE mart.fact_kpi`, plus a synthetic `cte_insert`-style target) that (a) the resolved
  canonical target is emitted `kind: "table"` and survives the filter, and (b) the unresolved
  synthetic target is emitted `kind: "derived"` and is dropped from default output, present under
  `--raw`/`--include-intermediate`.
- **Fallback (name-prefix belt) only if the structural kind change proves infeasible** against the
  fixture (e.g. a synthetic node is emitted with no `SqlTable` row at all, so `kind` is `NULL` and
  the `IS NULL` branch keeps it): then add a post-query drop in
  [`_filter_column_results`](../src/sqlcg/cli/commands/analyze.py) (default path only) for rows
  whose table component matches the synthetic `cte_*`/derived prefix, kept under `--raw`. This is
  the belt, not the primary mechanism. TC4 pins the observable outcome either way.

### #40 — guard suite (reused design, no re-derivation)

Land the fixture + TC1–TC7 exactly as specified in
[`fix_issue_40_user_surface_regression_guards.md`](fix_issue_40_user_surface_regression_guards.md):
- Synthetic committable fixture carrying shapes **A** (≥2 CTE hops), **B** (UNION-ALL branch
  CTE), **C** (`INSERT INTO mart.fact_kpi` + separate DDL `CREATE TABLE mart.fact_kpi`),
  **D** (source referenced only inside a CTE body).
- **Shape B must include a computed-measure projection** (e.g. a unioned column that is an
  arithmetic/ratio expression over branch columns, not a plain pass-through), and TC2 must
  assert its sources resolve. Rationale: the live DWH re-verification (see "Scope decision")
  found 47 surviving `cte_insert` islands that are overwhelmingly computed measures — this is
  the only assertion that would catch whether that residual is a real un-fixed shape.
- Tests drive the **public surface** (the exact `analyze` filtered query via the production
  `_kind_filter`, and the MCP trace tool) — never a raw `COLUMN_LINEAGE` `MATCH`.
- TC3 (#44 canonical name) and TC6 (#45 `file:line`) are written to be **RED before** the
  #44/#45 fixes land and **green after** — they are the acceptance tests for those fixes.

---

## Implementation steps (dependency-ordered)

> Ordering rationale: the regression fix + restored `_kind_filter` is the foundation the
> guard suite imports, so it goes first; then the guards (some RED); then #44/#45 flip them green;
> then the `resolve_pass2` dead-code dedup + test realignment (independent of #44, runs after it);
> finally release plumbing.

### Phase 0 — Read-proxy filter regression (foundation; also #39 read-half)
**Step 0.1** Reintroduce `_kind_filter(node_alias, *, include_intermediate)` in
[`analyze.py`](../src/sqlcg/cli/commands/analyze.py); replace **all four** inline filter strings
with calls to it; restore the OPTIONAL-MATCH + `kind IS NULL OR kind IN` form.
- The inline filter appears **4 times** (verified), and all four must be replaced:
  1. `upstream` primary query — analyze.py ~L38-43 (`{qualified: src.table_qualified}`)
  2. `upstream` bare-name **fallback** query — analyze.py ~L56-63 (reuses `kind_filter`, same string)
  3. `downstream` primary query — analyze.py ~L98-103 (`{qualified: dst.table_qualified}`)
  4. `downstream` bare-name **fallback** query — analyze.py ~L116-123
  (`upstream` calls `_kind_filter("src", …)`; `downstream` calls `_kind_filter("dst", …)`; each
  command computes the helper string once and reuses it in both its primary and fallback query, so
  the fallback paths are covered by the same single call — but the developer must confirm both
  fallback strings actually use the helper variable, not a second inline copy.)
- Call sites (grep-confirmed required): `upstream` + its fallback, `downstream` + its fallback,
  and the import in [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py) L32.
- Acceptance: `test_cte_recall_guard.py` collects and passes; `_half_b_keeps_node_less_physical_source`
  green; deleting a source's `SqlTable` node still returns the source via the CLI-filtered query.

### Phase 1 — #40 guard suite (verification layer; TC3/TC6 land RED)
**Step 1.1** Add the synthetic A–D fixture corpus (generic names, committable).
**Step 1.2** Add TC1–TC7 driving the CLI filtered query (via `_kind_filter`) + MCP trace tool.
- File: a new `tests/integration/test_user_surface_recall_guard.py` (or extend the existing
  `test_cte_recall_guard.py` family — pick to match existing layout; one named file per concern).
- Acceptance: TC1/TC2/TC4/TC5/TC7 green now; **TC3 and TC6 RED** (documented expected-fail
  markers tied to #44/#45, to be flipped to asserting-green by Phases 2–3, not `xfail`-left).
- Pre/post check from the #40 doc §5: reverting Phase 0 reds ≥1 guard.

### Phase 2 — #44 canonical-name resolution (flips TC3 green)
**Step 2.1** Add `canonical_by_bare` / `_ambiguous_bare` index in
[`CrossFileAggregator.register_pass1`](../src/sqlcg/lineage/aggregator.py) — populated in the
existing `for table in parsed.defined_tables:` loop (aggregator.py L34). DDL-defined tables only.
**Step 2.2** Apply the rewrite in **`_build_file_rows`** (indexer.py L974), at the INSERT-target
node-emission block (~L1204-1215). Thread `aggregator.canonical_by_bare` /
`aggregator._ambiguous_bare` down the call chain alongside the existing `defined_table_registry`
(`index_repo` → `_upsert_file_batch` L1246 / `_upsert_parsed_file` L1286 → `_build_file_rows`).
When the target resolves to a sole DDL canonical, emit the row with the canonical `full_id`/`name`/
`db`/`catalog` and keep `kind: "table"`; when it does **not** resolve to a DDL table, emit
`kind: "derived"` (W2 — feeds #45.2). Leave ambiguous bare names untouched.
- Call site: `_build_file_rows` is called from `_upsert_file_batch` (indexer.py L1246) and
  `_upsert_parsed_file` (L1286) — both grep-confirmed. **Do NOT target `resolve_pass2`** — it has
  zero production call sites (the production pass-2 path inlines its logic; see Phase 5 / #44 design).
- Test file: `tests/unit/test_canonical_target_resolution.py` (unit on the resolution helper:
  ambiguous bare name not rewritten; sole-DDL match rewritten; emitted row `qualified` == DDL
  `full_id`; unresolved synthetic target gets `kind: "derived"`).
- Acceptance: TC3 green — querying `mart.fact_kpi.measure` returns the **same** source set as
  `fact_kpi.measure`; the schema-less duplicate node no longer appears in `find`.
- Invariant guard: the change is pure row-dict mutation in the node-emission loop — no new
  `execute()`, no per-row `upsert_node`. Perf scaling + bulk/batch upsert guards must stay green.
- **Known limitation (single-file paths):** `reindex_file` (indexer.py L897) and `resync_changed`
  call `_upsert_parsed_file`/`_upsert_file_batch` **without a `CrossFileAggregator`**, so
  `canonical_by_bare` is not threaded in there — the #44 rewrite applies only on a full
  `index_repo` run. A single-file re-index of a previously-indexed file will re-create the
  schema-less node until the next full re-index. This is acceptable per CLAUDE.md ("re-index is
  the migration path"); the resolution helper **must degrade to a no-op (no rewrite) when
  `canonical_by_bare` is `None`/absent** — never raise on a missing lookup. State this in the
  helper's contract.

### Phase 3 — #45 (flips TC6 green) + cte_* leak
**Step 3.1** Rebind `file:line` to the source node's outgoing edge in `upstream`/`downstream`
queries. **Aggregate per `src`/`dst` BEFORE `LIMIT 100`** (required — the `srcedge` match
multiplies rows; without the `WITH … min(q.start_line) …` grouping the limit drops distinct
sources). Pick `min(q.start_line)` with its matching `file` deterministically.
**Step 3.2** Confirm `cte_*` suppression via the **kind filter** (graph-truth) now that Phase 2
emits unresolved synthetic INSERT targets as `kind: "derived"`. **Fixture-verification prerequisite:**
confirm on shape C that the resolved canonical target is `kind: "table"` (survives) and the
synthetic target is `kind: "derived"` (dropped from default, kept under `--raw`). Only add the
name-prefix belt in `_filter_column_results` if a synthetic node turns out to carry a `NULL` kind
on the fixture.
- Test file: `tests/integration/test_user_surface_recall_guard.py` (TC6 + TC4 assertions).
- Acceptance: TC6 green — every physical-source row carries a real `file:line`, only synthetic
  nodes may be `?`, **and the row count equals the distinct-source count** (no edge-fanout
  multiplication); TC4 green — no `cte_*`/derived row in default output, present under `--raw`.

### Phase 5 — `resolve_pass2` dead-code dedup + test realignment (the root cause of B1)
**Why this phase exists:** B1 happened because the production pass-2 path (`index_repo` →
`parse_pass2` dispatch → [`pool.py`](../src/sqlcg/indexer/pool.py) worker → `parse_file(...,
dependency_filter=...)`) *reimplements* `CrossFileAggregator.resolve_pass2` inline. So
`resolve_pass2` is **dead production code**, and every test that exercises it
([`test_cross_file_lineage.py`](../tests/integration/test_cross_file_lineage.py),
[`test_resolve_pass2_passes_dependency_filter.py`](../tests/unit/test_resolve_pass2_passes_dependency_filter.py),
[`test_aggregator.py`](../tests/unit/test_aggregator.py),
[`test_aggregator_skip.py`](../tests/unit/test_aggregator_skip.py)) validates a layer production
never runs — the **same blind-spot class as #40** (tests measuring a path production doesn't use).

**Chosen approach (preferred — delete + repoint):** delete `resolve_pass2` from
[`aggregator.py`](../src/sqlcg/lineage/aggregator.py) and repoint its cross-file-lineage *behaviour*
tests at the production path. The pure-pure pieces production still uses (`register_pass1`,
`cross_file_sources`, `_needs_pass2`) stay. Justification: the dep-filter + reparse logic already
lives once in `index_repo`'s pass-2 dispatch; extracting a shared helper that both `index_repo` and
the tests call is the fallback only if the worker path is impractical to unit-test — but the
behaviour these tests care about (view/CTAS cross-file column lineage) is already covered through
the production indexer by the integration suites that run `index_repo` end-to-end, so repointing
is cleaner than introducing a new helper seam.

**Step 5.1** Delete `resolve_pass2` from `aggregator.py`. Confirm no production caller (grep:
only tests + the inline `index_repo` logic reference it). **Also fix the stale doc reference at
[`ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) L88**, which names
`CrossFileAggregator.resolve_pass2` in a docstring — after deletion it points at a non-existent
method. Update it to reference the production `index_repo` pass-2 dispatch instead.
**Step 5.2** Rewrite/realign the four test files:
- `test_resolve_pass2_passes_dependency_filter.py` — the `dependency_filter` contract it pins is
  the inline `dep_names` computation in `index_repo` (indexer.py ~L356) + the worker call in
  `pool.py` L129. Repoint these to assert the dispatch path builds `dep_names` from
  `referenced_tables` and passes it as `dependency_filter` (drive `index_repo` on a tiny two-file
  cross-file fixture and assert the resulting graph carries the cross-file edge, OR unit-test the
  extracted `dep_names` computation if Step 5 takes the helper fallback).
- `test_cross_file_lineage.py` — replace the `resolve_pass2(parser, parsed)` loop with an
  `index_repo` run over the temp fixture; assert the same cross-file column-lineage edges on the
  resulting graph (observable output, not the dead method's return).
- `test_aggregator.py` / `test_aggregator_skip.py` — the parts that assert `register_pass1` /
  `_needs_pass2` / identity-skip behaviour stay (those are live); drop only the `resolve_pass2`
  re-read/identity assertions, re-expressing the "deleted file during pass 2" warning as a check on
  the production reparse path if that warning still exists there, else delete it with a note.
- **Confirm: pure dedup + test-realignment, no production behaviour change.** No graph output
  changes; this only removes a duplicate and points tests at the real path.

**Sequencing:** independent of #44 — the #44 fix lands in `_build_file_rows` regardless of whether
`resolve_pass2` exists. Run Phase 5 **after** Phase 2 so the #44 work isn't entangled with the test
churn; it can also land as the final code phase before release plumbing.

**Patch-scope judgement:** this stays a **patch (1.2.1)** — it removes a duplicate and realigns
tests at the production path; it ships no new capability and changes no graph output. The
test-realignment is the only sizeable part; if during implementation it proves larger than a patch
warrants (e.g. the integration fixtures need substantial new scaffolding), **stop and recommend
splitting Phase 5 into a follow-up `chore/` PR** — but the default, per the user's instruction, is
to include it here.

### Phase 6 — Release plumbing
**Step 6.1** Bump `version = "1.2.1"` in [`pyproject.toml`](../pyproject.toml) and
`__version__` in [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py); run `uv lock`.
(Tagging is post-merge per CLAUDE.md — not part of this plan.)
**Step 6.2** Full suite green: `uv run pytest`; `uv run pyright`; `uv run ruff check src tests`.
- Acceptance: perf scaling guard ([`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py))
  and bulk/batch upsert invariants stay green (no hot-path regression from Phase 2).

---

## Test strategy

- **Unit**: `test_canonical_target_resolution.py` (#44 aggregator logic, no graph).
- **Integration (real KuzuDB)**: the #40 surface guard suite TC1–TC7 against the A–D fixture,
  driving the CLI filtered query (via `_kind_filter`) and the MCP trace tool. This is the
  primary recall defence.
- **Regression repair proof**: `test_cte_recall_guard.py` collects again and its
  `_half_b_keeps_node_less_physical_source` is green (proves Phase 0).
- **Phase 5 realignment**: `test_cross_file_lineage.py`, `test_resolve_pass2_passes_dependency_filter.py`,
  `test_aggregator.py`, `test_aggregator_skip.py` rewritten so the cross-file-lineage assertions run
  through the production `index_repo`/pool pass-2 path, not the deleted `resolve_pass2`. Live
  `register_pass1`/`_needs_pass2` assertions retained.
- **Invariants untouched**: perf scaling + bulk/batch upsert guards must stay green (Phase 2
  only adds dedup-no-op node emission in the node loop, not the per-column lineage loop).
- **Observable-output rule**: every TC asserts on rendered CLI output / returned source sets,
  never on raw edge counts (the #40 anti-pattern).

## Acceptance criteria

- [ ] `analyze upstream/downstream` use a single `_kind_filter` helper with the OPTIONAL-MATCH +
      `kind IS NULL OR kind IN ['table','external']` form; `test_cte_recall_guard.py` collects and passes.
- [ ] Reverting Phase 0 (inner-join filter) reds ≥1 surface guard (the #40 pre/post check).
- [ ] TC3: `analyze upstream "mart.fact_kpi.measure"` (schema-qualified DDL name) returns the
      same physical-source set as the schema-less spelling; no schema-less duplicate node in `find`.
- [ ] TC6: every physical-source row in `analyze upstream/downstream` carries a real `file:line`;
      only synthetic nodes may show `?`; **row count == distinct-source count** (the per-`src`/`dst`
      aggregation collapses edge fanout before `LIMIT 100`, so no distinct source is silently dropped).
- [ ] TC4: no `cte_*`/derived row appears in default output; present under `--raw`. The drop is
      graph-truth: unresolved synthetic INSERT targets are emitted `kind: "derived"` (Phase 2) and the
      `kind IN ['table','external']` filter excludes them (name-prefix belt only if the fixture forces it).
- [ ] TC1/TC2/TC5/TC7 green (multi-hop recall, UNION-ALL completeness, CLI↔MCP parity, find/unused honesty).
- [ ] #44 rewrite lands in `_build_file_rows` (not the dead `resolve_pass2`); resolved canonical row
      `qualified` == DDL `full_id` and `kind: "table"`; ambiguous bare names untouched
      (unit test asserts no cross-schema merge).
- [ ] `CrossFileAggregator.resolve_pass2` is deleted (or deduped behind a shared helper) and its
      tests assert cross-file lineage through the production `index_repo`/pool path; no graph-output
      change (pure dedup + test realignment).
- [ ] No new per-row `upsert_node`/`upsert_edge`; perf scaling + bulk/batch upsert guards green.
- [ ] `version == "1.2.1"` in pyproject + `__init__.py`; `uv lock` refreshed; pyright + ruff clean.

## Scope decision: residual #38 and #28

- **Residual #38 (UNION-ALL of sibling CTEs)** — **OUT.** Already fixed in 1.1.3 (`f6f276b`),
  guarded by `test_union_cte_star_recall_guard.py`. The findings doc reporting 80 islands ran
  against 1.1.2 (pre-fix). Re-opening it would duplicate shipped work.
  - **Live re-verification on current `master` (2026-06-02, DWH 1335 files)** confirms the fix
    holds: the findings-doc dead-end column `cte_insert.ma_vrije_vrd` now has 3 upstream
    `COLUMN_LINEAGE` edges (the three branch CTEs) and its chain reaches physical `da.*` raw
    sources. Corpus-wide `cte_insert` islands dropped **80 → 47**.
  - **Known-unknown — 47 `cte_insert` islands remain** (down from 80, *not* zero). Sampling them
    they are overwhelmingly computed/derived measures (`ma_aantal_…_r_k`, `…_x_i`,
    `ma_vrije_vrd_1_op_1`, bare `ma`) — likely arithmetic/ratio expressions that may be
    legitimately source-less, but possibly a distinct un-fixed shape. **This is a measured fact,
    not a regression claim.** Do *not* scope a fix into 1.2.1 on it; instead, the #40 guard's
    UNION-ALL case (shape B) must also assert a **computed-measure** projection so a real gap here
    would turn the guard red. If 1.2.1's eval still shows islands after that guard lands green,
    file a fresh narrow issue with a 1.2.x repro — do not pad this patch on a stale measurement.
- **#28 server write lock** — **OUT of 1.2.1; recommend a separate minor (1.3.0).** The read
  half is already handled by the v1.2.0 routing proxy (CLI reads route through the live server).
  The remaining ask — server opens read-only by default and *escalates* to read-write only around
  a reindex — is a connection-lifecycle redesign (reopen semantics, kuzu can't upgrade an open
  connection, reindex-window coordination). That is a new capability with its own risk surface,
  not a bug fix, so it is a **minor** per CLAUDE.md SemVer, not a patch. A cheap, in-scope-elsewhere
  mitigation (suggest the `SQLCG_DB_PATH` side-DB in the "Database is locked" CLI read error) can
  ride along *if trivial*, but is explicitly not required for 1.2.1.

## Risks and mitigations

- **R1 — Phase 0 over-widens recall** (`IS NULL` keeps genuinely-noisy node-less rows).
  *Mitigation*: TC4 + `NoiseFilter` post-pass; the `IS NULL` branch only fires for sources that
  legitimately lack a node (the #39 tail), which #39's node emission already minimizes.
- **R2 — #44 cross-schema mis-merge** (two schemas define the same bare name).
  *Mitigation*: `_ambiguous_bare` excludes them; unit test asserts no rewrite; the existing
  `_bare_ref` CLI hint stays for the ambiguous case.
- **R3 — #45.1 row multiplication** from the unbounded `srcedge` match silently dropping the 101st
  distinct source under `LIMIT 100`. *Mitigation*: aggregation per `src`/`dst` before `LIMIT` is a
  **required** step, not conditional; TC6 asserts row count == distinct-source count, not just `file:line`.
- **R4 — Phase 2 hot-path regression.** *Mitigation*: change is pure row-dict mutation in the
  node-emission loop with dedup-no-op upsert (no new `execute()`); perf scaling + bulk/batch guards
  are blocking acceptance criteria.
- **R5 — Phase 5 test-realignment scope creep.** Deleting `resolve_pass2` and repointing four test
  files could balloon beyond a patch. *Mitigation*: it changes no graph output (pure dedup); if the
  fixture scaffolding proves large the plan permits splitting Phase 5 into a follow-up `chore/` PR
  without blocking the #44/#45 fixes (which are independent of Phase 5).
- **R6 — #44/#45.2 kind change mis-tags a real physical target as `derived`.** If a genuine physical
  INSERT target fails to resolve to a DDL canonical (no `CREATE TABLE` for it), Phase 2 would emit it
  `kind: "derived"` and the filter would hide it. *Mitigation*: only targets that are unqualified
  *or* not DDL-defined are eligible for `derived`; a target that *is* its own DDL table keeps
  `kind: "table"`. Shape C's fixture-verification prerequisite (Phase 3) pins both branches; if a
  real physical target legitimately has no DDL, that is the #39 node-less tail, handled by the
  `IS NULL` branch of the restored filter — confirm against the fixture before relying on the kind drop.

---

### Deviations

#### Deviation 1: _kind_filter missing WITH…WHERE clause
- **Reason**: The Phase 0 implementation of `_kind_filter` used `OPTIONAL MATCH ... WHERE t.kind IS NULL OR t.kind IN [...]` without an explicit `WITH {alias}, t WHERE` clause after it. In KuzuDB, the WHERE in an OPTIONAL MATCH applies to the match attempt only — it does not filter surrounding rows. Without the `WITH … WHERE`, all rows pass through regardless of the CTE node's kind. The original PR1 fix (commit `4ce0ded`) used `WITH c, src/dst, t WHERE t.kind IS NULL OR t.kind IN [...]` — this form is correct.
- **Change**: Added `WITH {node_alias}, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external']` to the `_kind_filter` return value, replacing the lone OPTIONAL MATCH WHERE clause.
- **Impact**: Filter now correctly excludes CTE/derived intermediate nodes from default output. TC1–TC7 all green.
- **Date**: 2026-06-02

#### Deviation 2: _flush_row_batch dedup preferred first-seen kind (CTE kind overwritten)
- **Reason**: When a CTE alias appears in `stmt.sources` (outer SELECT FROM my_cte), it is emitted with `kind='table'` (default). Later in the column-lineage loop, it is also emitted with `kind='cte'` (CTE destination). The dedup kept the first occurrence (kind='table'), so the CTE node ended up with the wrong kind in the graph.
- **Change**: Added Rule 2 to the table_rows dedup: prefer structural kinds (`cte`, `derived`, `external`) over the default `kind='table'`. This ensures the CTE destination emission wins over the incidental source-reference emission.
- **Impact**: CTE nodes now retain `kind='cte'` in the graph, enabling the kind filter to correctly exclude them.
- **Date**: 2026-06-02

#### Deviation 3: Pre-existing v1.2.0 test regressions fixed in same PR
- **Reason**: `test_pr3_kind_tagging.py`, `test_readonly_under_lock.py`, `test_indexer_commits.py`, and `test_star_resolution.py` were already failing on `master` due to the v1.2.0 read-proxy refactor removing `get_backend` from analyze/find/db/gain and changing `_build_file_rows`'s signature. These are not regressions from v1.2.1 work but were uncovered by running the full test suite.
- **Change**: Updated tests to patch `run_read_routed` (the v1.2.0 seam) instead of `get_backend`; updated `_build_file_rows` wrapper signatures; added DDL for `dst` in scenario_c so the fixture has a DDL-resolved target.
- **Impact**: Full suite now green (999 passed). No production behaviour changed.
- **Date**: 2026-06-02

---

## Plan Compliance — 2026-06-02

**Verdict: PASS** — all in-scope phases implemented as planned; deviations documented.
Verified against `git diff master...feat/v1.2.1-bugfix` (PR #48, head `feat/v1.2.1-bugfix`,
base `master`) and a live run of the recall guard suite (7/7 green).

> **Branch note (not a defect):** the implementation commits live on `feat/v1.2.1-bugfix`
> (PR #48 head), while the plan/docs branch is `plan/v1.2.1-bugfix`. `git diff master...plan/v1.2.1-bugfix`
> shows only the plan file — the code is on `feat/`. This is a branch-naming split, not missing work.

### Phase-by-phase

- **Phase 0 (read-proxy filter regression / #39 read-half) — PASS.** `_kind_filter("src"/"dst")`
  reintroduced in [`analyze.py`](../src/sqlcg/cli/commands/analyze.py); all four inline filter strings
  (upstream primary + fallback, downstream primary + fallback) replaced with helper calls; OPTIONAL-MATCH
  + `WITH … WHERE t.kind IS NULL OR t.kind IN ['table','external']` form restored. Two unplanned bugs
  surfaced and were fixed (Deviations 1 + 2) — see assessment below.
- **Phase 1 (#40 guard suite) — PASS.** [`test_user_surface_recall_guard.py`](../tests/integration/test_user_surface_recall_guard.py)
  TC1–TC7 present, driving the production `_kind_filter` query + MCP parity. Verified 7/7 green on a live run.
- **Phase 2 (#44 canonical INSERT-target) — PASS.** `canonical_by_bare` / `_ambiguous_bare` built in
  [`CrossFileAggregator.register_pass1`](../src/sqlcg/lineage/aggregator.py) from DDL tables only; rewrite
  applied in [`_build_file_rows`](../src/sqlcg/indexer/indexer.py) (not the dead `resolve_pass2`), threaded
  via `_upsert_file_batch` from `index_repo`. Resolved canonical row keeps `kind:"table"`, derives all
  fields from the canonical `full_id`; unresolved synthetic target emitted `kind:"derived"`; ambiguous
  bare names left untouched. Degrades to no-op when `canonical_by_bare is None` (single-file path), per the
  documented known limitation. TC3 green.
- **Phase 3 (#45 — #45.1 file:line + #45.2 cte_* leak) — PASS (folded into commits `0505c9d` + `7dda6f8`,
  not labelled "Phase 3").** This is the key reconciliation point (developer's report omitted #45 by name):
  - **#45.1 (`file:line` on every source row) — implemented.** All four analyze queries rebind location
    from the source's *outgoing* edge (`(src)-[srcedge:COLUMN_LINEAGE]->()`) and aggregate
    `WITH src, min(q.start_line) AS line, min(q.file_path) AS file` **before** `LIMIT 100`. Landed in
    commit `0505c9d` ("Phase 0 + #45.1"). TC6 asserts both non-null `file:line` **and** row count ==
    distinct-source count (the required anti-fanout check) — green.
  - **#45.2 (`cte_*` / synthetic leak suppression) — implemented structurally.** The graph-truth approach
    chosen in the plan: unresolved synthetic INSERT targets are emitted `kind:"derived"` (Phase 2,
    commit `7dda6f8`), and the restored `kind IN ['table','external']` filter excludes them. No name-prefix
    belt was needed. TC4 green. The dedup Rule 2 fix (Deviation 2) is what makes this hold for CTE aliases
    that are first seen as `kind='table'` source references.
  - **Why the developer's report didn't name #45:** #45 was split into #45.1 (in the Phase 0/#45.1 commit)
    and #45.2 (in the Phase 2/#44 commit). The work shipped under those two commits rather than a separate
    "Phase 3" — so it was reported by mechanism (kind filter, file:line aggregation) rather than by issue
    number. **#45 was NOT dropped; it shipped and is guarded by TC4 + TC6.**
- **Phase 5 (`resolve_pass2` dead-code dedup) — PASS.** `resolve_pass2` deleted from
  [`aggregator.py`](../src/sqlcg/lineage/aggregator.py); stale docstring reference in
  [`ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) updated; four affected test files realigned to the
  production `index_repo` pass-2 path.
- **Phase 6 (release plumbing) — PASS.** Version `1.2.1` in pyproject + `__init__.py`; `uv.lock` refreshed;
  pyright + ruff clean per developer report.

### Assessment of the two unplanned bugs (Deviations 1 + 2)

Both are **within the plan's intent, not scope creep** — they are implementation-mechanism corrections to
behaviour the plan already specified, and both are now load-bearing for the plan's own acceptance criteria:

1. **OPTIONAL MATCH `WHERE` does not filter rows in KuzuDB (Deviation 1).** The plan's Design §"Read-proxy
   filter regression" already prescribed "the OPTIONAL MATCH + `t.kind IS NULL OR t.kind IN [...]` form".
   The plan's example snippet (lines 107–114) omitted the trailing `WITH {alias}, t WHERE …` that KuzuDB
   requires to actually filter the surrounding row — a fidelity gap in the *plan snippet*, not a new
   requirement. The developer's fix restores the original PR1 pattern. This is a **plan-snippet correction**;
   the intent (Half B keeps node-less sources, excludes CTE/derived) is unchanged and is exactly what TC1/TC4
   now verify.
2. **Dedup kind-precedence (Deviation 2).** The plan's #45.2 mechanism *depends on* CTE/derived nodes
   carrying their structural kind in the graph, but did not anticipate that a CTE alias is emitted twice
   (first `kind='table'` as a source reference, then `kind='cte'` as a destination) and that the existing
   first-seen-wins batch dedup would keep the wrong one. Without the new Rule 2 (structural kind beats
   default `table`), the kind filter would silently fail to suppress CTE nodes and #45.2 would not hold.
   This is a **necessary precondition the plan missed**, now correctly handled in `_flush_row_batch`.

**Risk recorded:** Deviation 2 touches `_flush_row_batch`, a documented performance-invariant hot path
(per-batch dedup). The change is pure dict-comparison logic in the existing dedup loop — no new `execute()`,
no change to flush cardinality — so the batch-upsert invariant is preserved; the developer reports the
batch/scaling guards green. This is captured in ARCHITECTURE_REVIEW.md (see update below). No new risk to
the perf budget.

### Minor note (non-blocking, no action required for merge)

TC4's docstring ([`test_user_surface_recall_guard.py`](../tests/integration/test_user_surface_recall_guard.py)
L228–232) reads as if Phase 2's `kind:"derived"` emission is still pending ("will be extended … after
Phase 2"). Phase 2 has landed, so the prose is mildly stale; the assertion itself is correct and green. The
synthetic fixture uses short CTE names (a, b, u, j) rather than `cte_*`, so TC4's naming-pattern assertion is
satisfied trivially on the fixture while the structural `kind:"derived"` mechanism is what defends the real
DWH corpus (where `cte_insert` names appear). A future doc-only tidy could update the docstring; not a
compliance failure.

## Bundled v1.2.2 release compliance — 2026-06-02

**Verdict: PASS (combined).** The v1.2.1 bugfix scope (this plan) and the downstream-sink
`file:line` fix ([`fix_downstream_sink_location.md`](fix_downstream_sink_location.md), ARCHITECTURE_REVIEW
§19.2) ship together as a single **v1.2.2** release. This is the authoritative combined sign-off for the
full bundled scope, superseding the two prior per-scope sign-offs (the v1.2.1 "Plan Compliance — 2026-06-02"
above, and the plan-reviewer GO on the sink fix) as the single release gate.

### Bundling decision

The lower `v1.2.1` tag is **skipped**; the bundle releases as `v1.2.2` (precedent: v1.1.2→v1.1.3). Both
change sets live on `feat/v1.2.1-bugfix` (PR #48), fast-forwarded to commit `b5bf7a3`. The stale acceptance
criterion in the sink plan ("standalone release … after `v1.2.1` is tagged. Do NOT fold into PR #48") has
been corrected in [`fix_downstream_sink_location.md`](fix_downstream_sink_location.md) to record the bundle.

### Branch contents verified (`git log --oneline master..feat/v1.2.1-bugfix`, 18 commits)

The branch contains exactly the v1.2.1 bugfix commits plus the four sink-fix commits, and nothing stray:
- v1.2.1 bugfix set: `b36aa5a`, `ab0fcf2`, `8621705`, `018d6ee`, `0505c9d`, `ee8b2a0`, `7dda6f8`, `ac765d3`,
  `9cd1f6e`, `1f3834f`, `f48d8fb`, `418730b`, `4868d55`.
- `4714a8a` — docs(arch): triage v1.2.1 residuals (findings §19).
- `e5bb4c7` — docs(plan): downstream sink fix plan (§19.2).
- `b17f4f8` — test(TC6b): RED acceptance guard for the terminal-sink location bug.
- `b5bf7a3` — fix(TC6b): flip downstream location binding to the incoming edge — v1.2.2.

### Version verified — 1.2.2 (no leftover v1.2.1 release expectation)

- [`pyproject.toml`](../pyproject.toml) `version = "1.2.2"`.
- [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) `__version__ = "1.2.2"`.
- [`uv.lock`](../uv.lock) `sql-code-graph` package entry `version = "1.2.2"` (the other `1.2.2` matches in
  the lock are unrelated third-party packages — `python-dotenv`, etc.).
- No doc reference now implies a separate `v1.2.1` release/tag is still expected; the sole stale reference
  (sink plan line 102) was corrected.

### Scope coverage

- **v1.2.1 scope (Phases 0–6).** PASS — see the per-phase "Plan Compliance — 2026-06-02" log above. Code
  state unchanged by the bundling; the prior per-phase verdicts stand.
- **Sink fix (§19.2).** PASS. Both downstream queries in
  [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) (primary L134, bare-name fallback L146) bind location
  from the **incoming** edge `()-[dstedge:COLUMN_LINEAGE]->(dst)`. The upstream queries (L77/L89) retain the
  **outgoing** `(src)-[srcedge]->()` binding — unchanged, as the plan's non-goals require. The TC6b terminal-
  sink guard landed in [`test_user_surface_recall_guard.py`](../tests/integration/test_user_surface_recall_guard.py)
  (test at L377, helper `_downstream_filtered_query()` at L381), and was demonstrably RED on master before the
  fix (committed separately as `b17f4f8`).

### Suite / static checks (per release report)

Full suite green: 1000 passed / 7 skipped / 2 xfailed / 0 failed; pyright + ruff clean.

### Out-of-scope follow-up flagged (recorded in ARCHITECTURE_REVIEW §19.4, LOW)

The TC6b guard's helper `_downstream_filtered_query()` **reconstructs the downstream Cypher query string**
rather than invoking `analyze.downstream()` end-to-end. It can therefore drift from the production query if
`analyze.py` changes and the helper is not updated in lockstep — the exact §19.3 blind-spot class (guard
not driving the user-facing path). Future hardening: replace it with a `CliRunner`-based guard that runs the
real `analyze downstream` command. LOW priority; recorded as ARCHITECTURE_REVIEW §19.4.
