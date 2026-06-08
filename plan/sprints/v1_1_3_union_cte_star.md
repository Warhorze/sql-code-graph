# Feature Plan: v1.1.3 — `SELECT *` over a `UNION ALL` of sibling CTEs (residual #38)

> **architect-planner hop-preserving redesign — 2026-06-01**
>
> Implementation surfaced a design flaw in the prior `sources=` approach. The prior plan
> passed the sibling CTE bodies as `sources=` to `sg_lineage` on the union-star path. That
> **collapses the CTE hop**: it emits `cte_union.col ← physical_source.col` directly,
> skipping the `cte_a`/`cte_b`/`cte_c` intermediate. This is wrong for two reasons:
>
> 1. **It breaks the anchor tests.** [`test_anchor_ma_aantal_op_order.py`](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py)
>    asserts the intermediate CTE hop is PRESERVED — e.g. link 3:
>    `bm_orders.aantal_op_order → openstaand_combined.aantal_op_order`. A `sources=`
>    union-star path would have produced `openstaand_combined ← physical` directly.
> 2. **It makes the graph inconsistent.** Explicit-column unions (the `openstaand_combined`
>    anchor case, and `test_union_all_cte_*` in `test_cte_recall_guard.py`) already preserve
>    the CTE hop because they call `sg_lineage(col, body)` with **no `sources=`**. Star-unions
>    must behave identically — the CTE hop must be preserved, consistent with explicit-column
>    unions and the rest of the graph.
>
> **User decision: star-unions preserve the CTE hop too.** The corrected fix is therefore a
> **simplification, not a `sources=` addition**. The `sources=` / `union_cte_sources`
> mechanism is removed from this plan entirely. See Design § "The fix approach" below for
> the corrected, verified design.
>
> The earlier plan-reviewer blockers (B1: 3-branch fixture required; B2: walk to deepest
> left-branch Select) are CORRECT and retained — the column-derivation walk is the only real
> change. Only the `sources=` half is removed.
>
> **Branching note**: implementation lands on `feat/v1.1.3-union-cte-star`.

## Summary

Star-expand `SELECT *` projections inside a CTE whose body is a `UNION ALL` of
**sibling CTEs**, so the unioning CTE's columns receive incoming `COLUMN_LINEAGE`
edges instead of becoming lineage islands. This reconnects the one residual #38
shape that survived the v1.1.2 general CTE-recall fix (PR #41).

## Scope

### In Scope

- The CTE-projection loop in [`base.py`](../src/sqlcg/parsers/base.py) `_extract_column_lineage`
  (lines ~946–1041): when a CTE body is a `UNION` whose branches are `SELECT *`
  over **same-statement sibling CTEs**, derive the column list by walking to the deepest
  left-branch `exp.Select` (whose star the statement-level `qualify` has already expanded
  into explicit columns) and run the **existing no-`sources=`** `sg_lineage` call per column
  so each branch CTE column is preserved as the upstream (`cte_union.col ← cte_X.col`).
- A synthetic integration fixture reproducing the shape (≥2 sibling CTEs with
  identical explicit column lists, unioned via `UNION ALL`, star-selected) plus a guard
  that goes red on the bug and green on the fix.
- Re-scope GitHub issue #38 to this residual (documentation note; no code).
- Version bump 1.1.2 → 1.1.3.

### Non-Goals

- **Do NOT re-open general CTE recall.** Explicit-column CTEs and explicit-column
  `UNION ALL` CTEs already trace (guarded by
  [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py)
  `test_union_all_cte_*`). This plan touches only the `SELECT *`-over-union shape.
- **Do NOT** introduce a schema-CSV / INFORMATION_SCHEMA ingestion path (removed
  2026-05, CLAUDE.md). The branch CTE schemas come from the same-statement WITH bodies
  already parsed in pass 1 — no new schema source.
- **Do NOT** change the `STAR_SOURCE` post-ingestion expansion query
  (`EXPAND_STAR_SOURCES`) or the `StarSource` dataclass. That mechanism keys off a
  query's `target_table` (INSERT/CREATE target); a CTE has no target table, so it is
  the wrong carrier for this shape (see Design § "Why not STAR_SOURCE").
- **Do NOT** pass `sources=` to `sg_lineage` on the union-star path, and do NOT build a
  `union_cte_sources` map. The corrected design preserves the CTE hop by reusing the
  existing no-`sources=` call; introducing `sources=` would collapse the hop and break the
  anchor tests. There is no cross-file or sibling-CTE inlining here.
- The 4 timeout files and the server-side write-lock (#28) are out of scope.

## Design

### Root-cause analysis (confirmed against current code)

The unioning CTE's projections are populated by the **CTE-projection loop**, not the
main column loop. The relevant code is
[`base.py:946–1041`](../src/sqlcg/parsers/base.py):

- `base.py:972–975` — for a `UNION` CTE body, `projection_source = cte_body.this`
  (the *immediate* left child) is used for **column names**, and
  `cte_body_sql = cte_body.sql(...)` (line 982) serializes the **whole union body in
  isolation**.
- For a **2-branch** union, `cte_body.this` is an `exp.Select`, and the statement-level
  `qualify` at `base.py:670` (which mutates `body` in place) has already expanded
  `SELECT *` into explicit columns there — so `projection_source.expressions` carries the
  real columns and the loop works today.
- For an **N≥3-branch** union (`A UNION ALL B UNION ALL C`), sqlglot parses as
  `Union(this=Union(this=A, expression=B), expression=C)`. So `cte_body.this` is *itself a
  `Union`*, and **`Union.expressions` is always `[]`**. Thus `projection_source.expressions
  == []`, the inner `for cte_col_expr` loop body **never executes**, and `cte_union.*`
  receive **zero incoming edges** → islands. This is the residual #38 bug.

**Confirmed empirically** by indexing the synthetic 3-branch fixture (Step 2.1) through the
full `Indexer().index_repo` pipeline against in-memory KuzuDB:

| Node | Incoming `COLUMN_LINEAGE` today | Status |
|---|---|---|
| `cte_a.total_amt` | `← staging.src_a.amount` (CTE_PROJECTION) | OK (branch traces) |
| `cte_b.total_amt` | `← staging.src_b.amount` (CTE_PROJECTION) | OK (branch traces) |
| `cte_c.total_amt` | `← staging.src_c.amount` (CTE_PROJECTION) | OK (branch traces) |
| `mart.union_star_dst.total_amt` | `← staging.src_{a,b,c}.amount` (SELECT) | OK (outer scope collapses the missing CTE) |
| **`cte_union.total_amt`** | **(node does not exist — never a lineage target)** | **ISLAND — the residual bug** |

Note: the whole-statement `qualify` succeeds, and it **expands `SELECT *` into the deepest
left-branch `Select` of `cte_union`** (verified: that Select's projections become
`CTE_A.GRP_KEY AS GRP_KEY`, `CTE_A.TOTAL_AMT AS TOTAL_AMT`). The columns ARE available — the
loop simply reads the *wrong* node (`cte_body.this`, an empty-`expressions` `Union`) instead
of walking down to that deepest `Select`.

### The fix approach

**One change** inside the CTE-projection loop (`base.py:946–1041`): correct the
column-derivation source. **No `sources=` is introduced anywhere.**

1. **Walk to the deepest left-branch `Select` for column names.** When `cte_body` is an
   `exp.Union`, set `projection_source` by walking
   `ps = cte_body.this; while isinstance(ps, exp.Union): ps = ps.this`, then use
   `ps.expressions` as the column list. This is computed **once per CTE**, O(N_branches),
   before the per-column loop. Because the statement-level `qualify` (base.py:670) already
   expanded the star into this deepest `Select` in place, `ps.expressions` holds the real,
   explicit columns (`grp_key`, `total_amt`) — not a bare `Star`. This single change fixes
   the N≥3 island: the loop now iterates real columns. For N=2 the walk is a no-op
   (`cte_body.this` is already a `Select`), so existing behaviour is preserved.

2. **Run the SAME existing no-`sources=` `sg_lineage` call per column.** The call at
   `base.py:1011` is left exactly as-is:

   ```python
   root = sg_lineage(cte_col_name, cte_body_sql, dialect=self.DIALECT)  # NO sources=
   ```

   With no `sources=`, sqlglot resolves the union body's column to its **branch-CTE leaves**
   — it stops at the CTE boundary because the branch relations (`CTE_A`, `CTE_B`, `CTE_C`)
   are the `FROM` targets within the serialized body and are not further inlined. Verified
   against the qualified `cte_union` body:

   ```python
   sg_lineage("total_amt",
              'SELECT "CTE_A"."GRP_KEY" AS "GRP_KEY", "CTE_A"."TOTAL_AMT" AS "TOTAL_AMT" '
              'FROM "CTE_A" AS "CTE_A" UNION ALL ... UNION ALL ...',
              dialect="snowflake")
   # leaves: "CTE_A"."TOTAL_AMT", "CTE_B"."TOTAL_AMT", "CTE_C"."TOTAL_AMT"
   ```

   `_lineage_node_to_edges` therefore emits, per branch:
   `cte_union.<col> ← cte_X.<col>`, tagged `CTE_PROJECTION` — **the CTE hop is preserved**,
   identical in spirit to the explicit-column union path (`openstaand_combined ← bm_orders`).

3. **Physical sources are reached by multi-hop traversal, not by collapse.** The branch
   CTEs already receive their own `cte_X.<col> ← physical.<col>` `CTE_PROJECTION` edges from
   normal processing (see the empirical table above). So a multi-hop upstream walk from
   `cte_union.total_amt` reaches `staging.src_*.amount` *through* the preserved `cte_X` hop —
   no edge collapses, no `sources=` inlining.

### Edge semantics, transform tag, and confidence

- Carry the reconnection with **plain `COLUMN_LINEAGE`**, reusing the existing
  `transform="CTE_PROJECTION"` tag the CTE loop already applies at `base.py:1031`.
  This keeps it identical to how every other CTE projection edge — including explicit-column
  unions — is recorded.
- **Confidence 1.0** — the same as all other `CTE_PROJECTION` edges (`base.py:1032`
  passes `edge.confidence` through; the leaf edges are emitted at confidence 1.0 by
  `_lineage_node_to_edges`). The star is structurally resolved by the statement-level
  `qualify`, not inferred, so it is not a 0.8 `STAR_EXPANSION`.

### Why not STAR_SOURCE / STAR_EXPANSION

The `STAR_SOURCE` → `EXPAND_STAR_SOURCES` path
([`queries.cypher` `EXPAND_STAR_SOURCES`](../src/sqlcg/core/queries.cypher),
[`indexer.py:1180–1201`](../src/sqlcg/indexer/indexer.py)) requires
`q.target_table <> ''` and resolves `source HAS_COLUMN c → target.c`. A CTE has **no
target table** (`stmt.target` is the INSERT/CREATE target of the whole statement, not the
CTE), so a `StarSource` emitted here would either be dropped by the `target_table <> ''`
guard or, worse, mis-attach the branch columns to the statement's final target. The
sqlglot-resolved `CTE_PROJECTION` path is correct because it attaches to the **CTE name**
(`cte_dst_table = TableRef(name=cte_alias, role="cte")`, `base.py:963`).

### Why this preserves the CTE hop (and matches explicit-column unions)

Because **no `sources=`** is passed, sqlglot does NOT inline the branch CTE bodies. The
emitted edge is `cte_union.<col> ← cte_X.<col>` — the `cte_X` intermediate is the direct
upstream, preserving the hop. This is the same behaviour the explicit-column union path
already exhibits (`openstaand_combined.aantal_op_order ← bm_orders.aantal_op_order`,
anchor link 3) and is consistent with the rest of the graph:
- The branch CTE's own projection edge (`cte_X.<col> ← physical.<col>`) is emitted by the
  CTE-projection loop's iteration over `cte_X`, so the physical source is reachable via a
  **multi-hop** walk through the preserved `cte_X` node.
- The downstream `final ← cte_union` edge already exists (main loop).
- Net effect: `cte_union` gains incoming edges to the **branch CTE columns**, reconnecting
  the chain without collapsing any hop. A multi-hop upstream traversal reaches the physical
  sources; a 1-hop traversal stops at the branch CTEs — exactly like every other CTE in the
  graph.

There is **no `sources=`** and no `union_cte_sources` map. The fix does not reference the
cross-file corpus at all — it only corrects which AST node the column list is read from.

### API Changes

None. No CLI flags, no MCP tool signatures, no schema version change
(`SCHEMA_VERSION` stays at its current value — only new edges of an existing
`COLUMN_LINEAGE`/`CTE_PROJECTION` shape are produced).

### Data Models

No new node or edge types. Edges produced reuse `COLUMN_LINEAGE` with
`transform="CTE_PROJECTION"`, `confidence=1.0`.

### Dependencies

No new third-party dependencies. Uses existing sqlglot `lineage()` `sources=` parameter.

## Implementation Steps

### Phase 1: Star-over-union-CTE resolution in the parser

**Step 1.1**: In the CTE-projection loop, when the CTE body is a `UNION`, derive the
column list from the **deepest left-branch `exp.Select`** instead of `cte_body.this`.
- Files affected: [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)
  (`_extract_column_lineage`, the `for cte in cte_expressions` block at ~946–1041),
  specifically the `projection_source` assignment at lines 972–975.
- Detail: replace
  ```python
  if isinstance(cte_body, exp.Union):
      projection_source = cte_body.this
  else:
      projection_source = cte_body
  ```
  with a walk to the deepest left-branch `Select`:
  ```python
  if isinstance(cte_body, exp.Union):
      projection_source = cte_body.this
      while isinstance(projection_source, exp.Union):
          projection_source = projection_source.this
  else:
      projection_source = cte_body
  ```
  This is computed **once per CTE**, before the per-column loop — O(N_branches). It is the
  ONLY production change required.
- **Why this works**: the statement-level `qualify` at `base.py:670` mutates `body` in place
  and expands `SELECT *` into the deepest left-branch `Select` of the union. After the walk,
  `projection_source.expressions` holds the real explicit columns (`grp_key`, `total_amt`),
  so the existing star-skip at lines 990–994 no longer fires on every projection and the
  inner loop iterates real column names. For 2-branch unions the `while` is a no-op
  (`cte_body.this` is already a `Select`), so the existing behaviour is byte-for-byte
  preserved.
- **Why `cte_body.this` alone is insufficient**: for `A UNION ALL B UNION ALL C`, sqlglot
  parses as `Union(this=Union(this=A, expression=B), expression=C)`. The outer `Union.this`
  is another `Union`, whose `.expressions == []`. Reading `cte_body.this.expressions` yields
  `[]` → zero iterations → the island.
- Acceptance: for the synthetic 3-branch fixture, `cte_union`'s projected columns
  (`grp_key`, `total_amt`) are enumerated and reach the per-column `sg_lineage` call.

**Step 1.2**: Do NOT change the `sg_lineage` call. It MUST remain `sg_lineage(cte_col_name,
cte_body_sql, dialect=self.DIALECT)` with **no `sources=`**.
- Files affected: same loop; this is a **negative** requirement — the call at `base.py:1011`
  stays exactly as-is.
- Detail: with no `sources=`, sqlglot resolves each union-body column to its branch-CTE
  leaves (`cte_a.total_amt`, `cte_b.total_amt`, `cte_c.total_amt`), so
  `_lineage_node_to_edges` emits `cte_union.<col> ← cte_X.<col>` `CTE_PROJECTION` edges —
  the CTE hop is preserved, identical to the explicit-column union path. Introducing
  `sources=` here would collapse the hop and break the anchor tests; do NOT do it.
- Acceptance: incoming edges to `cte_union.total_amt` have branch-CTE-column sources
  (`cte_a.total_amt` etc.), NOT physical sources; the call signature is unchanged from HEAD.

**Step 1.3**: Keep the loop allocation-light — verify no per-column serialization or
per-column work was introduced.
- Files affected: same loop.
- Detail: `cte_body_sql` stays hoisted (already at `base.py:982`); the deepest-Select walk
  is done once per CTE in the `projection_source` assignment (Step 1.1), before the inner
  `for cte_col_expr` loop. No map is built, no `sources=` is constructed.
- Acceptance: see Perf-invariant section; `test_perf_scaling_guard.py` stays green.

### Phase 2: Tests

**Step 2.1**: Add the synthetic reproduction fixture + guards.
- Files affected: NEW
  `tests/integration/test_union_cte_star_recall_guard.py`.
- Fixture (no private corpus needed):
  ```sql
  INSERT INTO mart.union_star_dst
  WITH cte_a AS (
      SELECT k AS grp_key, SUM(amount) AS total_amt
        FROM staging.src_a GROUP BY k
  ),
  cte_b AS (
      SELECT k AS grp_key, SUM(amount) AS total_amt
        FROM staging.src_b GROUP BY k
  ),
  cte_c AS (
      SELECT k AS grp_key, SUM(amount) AS total_amt
        FROM staging.src_c GROUP BY k
  ),
  cte_union AS (
      SELECT * FROM cte_a
      UNION ALL SELECT * FROM cte_b
      UNION ALL SELECT * FROM cte_c
  )
  SELECT grp_key, total_amt FROM cte_union;
  ```
  **Three** sibling CTEs (not two) with **identical explicit column lists**, unioned via
  `UNION ALL`. A 3-branch fixture is required because for N=2, `qualify()` mutates the AST
  in-place and expands `SELECT *` to explicit columns before the CTE-projection loop runs,
  masking the bug. For N=3, `cte_body.this` is a nested `Union` (not a `Select`), so
  `projection_source.expressions = []` and the current loop body never executes — producing
  zero incoming edges to `cte_union`. This is the shape that matches the DWH islands.
- Acceptance: see Test Strategy + Acceptance Criteria.

**Step 2.2**: Add a behavioural assertion to the perf scaling guard if a new
hot-path op is introduced (only if Step 1 adds a counted op).
- Files affected: [`tests/unit/test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py)
  — only if a new once-per-statement op is added; otherwise no change.
- Acceptance: the guard's op-count axes stay flat between N and 2N columns.

### Phase 3: Version bump + issue re-scope

**Step 3.1**: Bump version 1.1.2 → 1.1.3.
- Files affected: [`pyproject.toml`](../pyproject.toml) line 7,
  [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) line 3, `uv.lock`.
- Acceptance: `uv run sqlcg --version` (or `sqlcg.__version__`) reports `1.1.3`.

**Step 3.2**: Re-scope GitHub issue #38 to the residual (documentation only).
- The general CTE-recall regression is fixed (v1.1.2); #38 should now read
  *"`SELECT *` not expanded across a `UNION ALL` of sibling CTEs"*. The
  architect-reviewer should add a short entry to
  [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) recording this residual and its
  fix. Not a code change in this PR.
- Acceptance: PR description links #38 and states the narrowed scope.

## Test Strategy

- **Integration test (primary guard)** — `test_union_cte_star_recall_guard.py`, real
  in-memory KuzuDB via `Indexer().index_repo`, asserting **observable graph output**
  (the #40 lesson — assert real edges/ids, never "no exception"):
  1. `test_union_star_cte_gets_incoming_edges` — after indexing the **3-branch** fixture,
     `MATCH (s)-[:COLUMN_LINEAGE]->(c:SqlColumn {id:'cte_union.total_amt'}) RETURN s.id`
     returns **a non-empty set** (any incoming edge). **Goes red on current code**: with
     the 3-branch fixture, `cte_union.total_amt` has 0 incoming edges today (the island —
     the node is never even created). After the fix, incoming edges exist from the **branch
     CTE columns**. This is the central bug sentinel.
  2. `test_union_star_cte_incoming_edges_are_cte_projection` — the incoming edges to
     `cte_union.total_amt` carry `transform="CTE_PROJECTION"` and their source is a **branch
     CTE column** (`cte_a.total_amt`), NOT a collapsed physical source. This is the
     hop-preserving sentinel: it would FAIL if a future `sources=` regression collapsed the
     hop to `staging.src_*.amount`. RED today (0 edges).
  3. `test_union_star_cte_mcp_upstream_reaches_physical_sources` — a multi-hop (CLI-shaped)
     upstream traversal from `cte_union.total_amt` reaches `staging.src_a/b/c.amount`
     **through the preserved `cte_X` hop**. Uses the production `analyze._kind_filter` helper
     (as the v1.1.2 guards do), so a filter regression also reds it. RED today (island; the
     node has no incoming edges so the traversal returns nothing). This validates the
     end-to-end chain reaches physical sources WITHOUT collapsing the hop — note the 1-hop
     `GET_UPSTREAM_DEPENDENCIES_QUERY` would only return the branch CTEs (correct), so the
     physical-source assertion MUST use a multi-hop `*1..N` traversal.
  4. `test_explicit_column_union_still_traces` — a sibling test using an explicit-column
     2-branch union to prove the fix did not change the already-working path
     (anti-regression for the general recall). GREEN today and after the fix.
- **Perf invariant suite (gate, must stay green)**:
  [`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py),
  [`test_T09_01_qualify_once.py`](../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py).
- **Existing CTE-recall guards (must stay green)**:
  [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py),
  [`test_cte_source_node_invariant.py`](../tests/integration/test_cte_source_node_invariant.py).
- **Full suite**: `rtk uv run pytest` (fall back to `uv run pytest`), `rtk uv run pyright`,
  `rtk uv run ruff check src tests`.

## Perf-Invariant Preservation (CLAUDE.md "DO NOT REMOVE OR SIMPLIFY")

The fix lives in the **CTE-projection loop**, which is a distinct, less hot path than the
main per-column scope path. It must still respect every invariant:

| Invariant | How this fix preserves it |
|---|---|
| `qualify`/`build_scope` imported at module level | Untouched — no new imports added in `base.py` body. |
| `exp.expand()` with file-level `sources` filtered by `dependency_filter` (O(N_refs)) | Untouched — no new `exp.expand()` call is added, and **no `sources=` is passed to `sg_lineage`**. The fix only changes which AST node the column list is read from (deepest left-branch Select). |
| `body_scope` built once per statement | Untouched — the main loop's `body_scope` logic at `base.py:646–692` is not modified. The CTE loop does not build a `body_scope`. |
| Pure-literal skip | Untouched — the branch CTE column enumeration skips star/literal projections exactly as the existing `cte_projections` loop does. |
| `copy=False` + `trim_selects=False` when a scope is passed | Untouched — the CTE-projection `sg_lineage` call uses a serialized string with no `scope=` and no `sources=`. The scope-path kwargs at `base.py:895–909` are not modified. |
| INSERT body copied once | Untouched — INSERT block at `base.py:713–808` not modified. |
| `body_scope` built before column loop, never lazily inside | Untouched. The only new per-CTE work (the deepest-left-`Select` walk in the `projection_source` assignment) is done **once per CTE before the inner `for cte_col_expr` loop**, O(N_branches). No per-column re-parse, re-serialize, or `sg_lineage` call-count change. |
| Bulk/batch upsert paths | Untouched — `indexer.py` not modified. |

**Complexity statement**: the only added work is the O(N_branches) deepest-left-`Select`
walk per union CTE, computed once before the per-column loop. The per-column `sg_lineage`
calls are **unchanged in count and signature** (one per branch column, no `sources=`) — for
N≥3 unions the loop previously made *zero* calls (empty `expressions`), so the fix turns a
no-op into the same call the explicit-column path already makes. There is no new op that
scales with column-count squared, no full-corpus expand, no `sources=` inlining, and no
per-column qualify/build_scope. Both scales (20-file and 1600-file) are unaffected: small
repos see the same fast path; the DWH corpus gains its reconnected `cte_union` islands at
negligible incremental cost (one cheap pointer walk per union CTE).

**Gating tests**: `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`,
`test_bulk_upsert_invariant.py`, `test_upsert_batch_invariant.py` must all stay green.

## Acceptance Criteria

- [ ] On the **3-branch** synthetic fixture, `cte_union.total_amt` has ≥1 incoming
      `COLUMN_LINEAGE` edge (currently 0 — confirmed island with 3 branches).
- [ ] The incoming edges to `cte_union.total_amt` have **branch CTE columns** as their
      source (`cte_a.total_amt`, `cte_b.total_amt`, `cte_c.total_amt`), NOT a collapsed
      physical source — the CTE hop is preserved.
- [ ] `test_union_star_cte_gets_incoming_edges` and
      `test_union_star_cte_incoming_edges_are_cte_projection` are **red at HEAD/pre-fix**
      (0 edges; verified with the 3-branch fixture) and **green after the fix**.
- [ ] End-to-end: a **multi-hop** upstream traversal from `cte_union.total_amt` reaches the
      physical sources `staging.src_{a,b,c}.amount` *through* the preserved `cte_X` hop.
- [ ] The existing explicit-column union path is unchanged: `test_union_all_cte_*` in
      [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py) and the
      anchor tests in
      [`test_anchor_ma_aantal_op_order.py`](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py)
      (which assert the intermediate CTE hop is preserved) stay green.
- [ ] No `sources=` is passed to `sg_lineage` anywhere on the union-star path; no
      `union_cte_sources` map exists in the code.
- [ ] All four perf-invariant guards stay green; `pyright` 0 errors; `ruff` clean.
- [ ] Full suite passes (no new failures, skips, or xfails introduced beyond the new tests).
- [ ] Edges carry `transform="CTE_PROJECTION"` and `confidence=1.0` (no `STAR_SOURCE` row
      and no `STAR_EXPANSION` edge is emitted for the CTE-internal union).
- [ ] Version reports `1.1.3`.
- [ ] (Manual, on the private DWH corpus, not in CI) re-index reconnects the `cte_*` union
      islands and `ba.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd`
      `--raw --include-intermediate --depth 12` reaches a `da.`/`ban.` raw source again
      through the preserved branch-CTE hops.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| A future change reintroduces `sources=` and collapses the CTE hop | `test_union_star_cte_incoming_edges_are_cte_projection` asserts the source is a **branch CTE column**, not a physical source — it reds if the hop collapses. The anchor tests also red. The plan explicitly forbids `sources=` (Step 1.2). |
| The statement-level `qualify` fails, so the star is NOT expanded into the deepest Select | On qualify failure the deepest `Select` still holds a bare `Star`, the existing star-skip fires, and the CTE degrades to today's behaviour (no edge) — never a wrong edge. This matches the current fallback exactly. |
| Branch CTEs are not union-compatible / column counts differ | Column names come from the qualified deepest left-branch `Select` (union branches must be union-compatible; sqlglot's qualify already enforces left-branch column names). Mismatched branches degrade to no edge, never to a wrong edge. |
| Regressing the general CTE recall the v1.1.2 guards pin | The only change is the `projection_source` walk; the `sg_lineage` call (no `sources=`) is byte-for-byte unchanged, and for 2-branch unions the walk is a no-op. `test_cte_recall_guard.py`, the anchor tests, and the new `test_explicit_column_union_still_traces` gate this. |
| Perf regression in the hot loop | New work is once-per-CTE, O(N_branches) pointer walk; `sg_lineage` call count and signature unchanged. Gated by `test_perf_scaling_guard.py`. |
| `qualify()` in-place mutation changes `cte_body_sql` for 2-branch fixtures | `qualify()` mutates the AST in-place; for 2-branch unions it expands `SELECT *` to explicit columns, so `cte_body_sql` (line 982) serializes the qualified SQL. This means the 2-branch case already works today (qualify rescues it). The fix targets the case where the star is NOT expanded: 3+ branches (`projection_source` is a Union with `.expressions = []`) or future edge cases where qualify doesn't expand. The guard MUST use a 3-branch fixture to exercise the true unrescued path. |

## PR Breakdown

Single PR (`fix(#38): trace SELECT * across a UNION ALL of sibling CTEs (hop-preserving)`):
- `src/sqlcg/parsers/base.py` (CTE-projection loop, Steps 1.1–1.3 — the deepest-left-`Select`
  walk only; the `sg_lineage` call is unchanged, no `sources=`)
- `tests/integration/test_union_cte_star_recall_guard.py` (NEW, Step 2.1)
- `pyproject.toml`, `src/sqlcg/__init__.py`, `uv.lock` (version bump, Step 3.1)
- (optional) `tests/unit/test_perf_scaling_guard.py` (only if a new counted op is added)

Version bump: **1.1.2 → 1.1.3** (patch — bug fix, no API/schema change). Re-index is the
migration path (CLAUDE.md "No backward compatibility").

## Blocking Questions

**None blocking.** The hop-preserving redesign (2026-06-01) resolves the design flaw the
prior `sources=` approach introduced. The verified facts:
- The statement-level `qualify` (base.py:670) expands `SELECT *` into the deepest
  left-branch `Select` of the union CTE (verified: deepest projections =
  `CTE_A.TOTAL_AMT AS TOTAL_AMT`).
- The existing no-`sources=` `sg_lineage` call on that body yields branch-CTE-column leaves
  (`CTE_A.TOTAL_AMT`, `CTE_B.TOTAL_AMT`, `CTE_C.TOTAL_AMT`), preserving the CTE hop.
- Today's 3-branch fixture produces zero incoming edges to `cte_union.total_amt` (the node
  never exists) — confirmed by indexing through the full pipeline.

**B1 (retained)**: 3-branch fixture required — the 2-branch case already works because
`qualify()` expands the star in-place into a direct `Select`. Step 2.1 uses 3 branches.

**B2 (retained)**: Step 1.1 walks to the deepest left-branch `Select`
(`while isinstance(ps, exp.Union): ps = ps.this`); for N≥3 branches `cte_body.this` is a
`Union` with `.expressions == []`. This walk is the ONLY production change.

**Superseded**: the prior `sources=` / `union_cte_sources` mechanism is removed entirely —
it collapsed the CTE hop and broke the anchor tests.

Non-blocking note:
- ARCHITECTURE_REVIEW.md does not yet record this residual #38 disposition (the finding
  lives in [`findings_v1.1.2_dwh_eval.md`](findings_v1.1.2_dwh_eval.md), which states
  "keep #38 open, narrow scope to UNION-ALL-of-CTEs star"). This plan proceeds on that
  documented disposition and the user's explicit request. Recommend the architect-reviewer
  add a one-line entry to ARCHITECTURE_REVIEW.md when the fix lands (Step 3.2); it is not
  a precondition for implementation.
