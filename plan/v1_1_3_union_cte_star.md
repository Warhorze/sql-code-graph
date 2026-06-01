# Feature Plan: v1.1.3 — `SELECT *` over a `UNION ALL` of sibling CTEs (residual #38)

> **plan-reviewer corrections — 2026-06-01**
>
> **B1 (BLOCKER)**: The 2-branch UNION ALL fixture in Step 2.1 does NOT reproduce the DWH
> island. `qualify()` mutates the AST in-place: for a 2-branch UNION ALL, `qualify()`
> expands `SELECT *` inside `cte_union` to explicit columns before the CTE-projection loop
> runs. After expansion, `projection_source.expressions` is no longer `[Star]`, so the
> star-skip (`base.py:988-994`) never fires and the loop iterates the explicit columns
> successfully. The 2-branch fixture already works today — no fix is needed for it.
>
> The actual island occurs with **N-branch UNION ALL (N≥3)**, where `cte_body.this` is
> itself a `Union` (not a `Select`). `Union.expressions` is always `[]` (empty), so
> `projection_source.expressions = []` and the inner `for cte_col_expr` loop body never
> executes. **Fix: change the fixture to a 3-branch UNION ALL** (adds a `cte_c` branch)
> so the guard is red on current code for the right reason. Step 1.1 must also be
> corrected to walk to the deepest left-branch Select (not just `cte_body.this`).
>
> **B2 (BLOCKER)**: Step 1.1 only handles the case where `projection_source.expressions`
> is `[Star]` (2-branch, direct left Select). For N≥3 branches, `projection_source` is a
> `Union` with `.expressions = []` — the plan's detection condition is never satisfied.
> **Fix: traverse to the deepest left-branch Select** (`while isinstance(ps, exp.Union):
> ps = ps.this`) before checking for the star. Only then check `ps.expressions == [Star]`.
> The `cte_body_sql` serialization of the full union body is unaffected (still correct).
>
> **Branching note (non-blocking)**: this plan targets v1.1.3 but lives on
> `feat/v1.1.2-bugfix`. Implementation must land on a fresh branch (`fix/v1.1.3-union-cte-star`
> off `master` or after PR #41 merges), not extend PR #41.

## Summary

Star-expand `SELECT *` projections inside a CTE whose body is a `UNION ALL` of
**sibling CTEs**, so the unioning CTE's columns receive incoming `COLUMN_LINEAGE`
edges instead of becoming lineage islands. This reconnects the one residual #38
shape that survived the v1.1.2 general CTE-recall fix (PR #41).

## Scope

### In Scope

- The CTE-projection loop in [`base.py`](../src/sqlcg/parsers/base.py) `_extract_column_lineage`
  (lines ~946–1041): when a CTE body is a `UNION` whose branches are `SELECT *`
  over **same-statement sibling CTEs**, give `sg_lineage` the sibling CTE bodies as
  `sources=` so it can expand the star against each branch's CTE schema and union the
  per-branch lineage onto the unioning CTE's columns.
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
- **Do NOT** expand stars against arbitrary cross-file CTAS bodies here — only the
  same-statement sibling CTEs that the union branches name (keeps it O(N_refs)).
- The 4 timeout files and the server-side write-lock (#28) are out of scope.

## Design

### Root-cause analysis (confirmed against current code)

The unioning CTE's projections are populated by the **CTE-projection loop**, not the
main column loop. The relevant code is
[`base.py:946–1041`](../src/sqlcg/parsers/base.py):

- `base.py:964–975` — for a `UNION` CTE body, `projection_source = cte_body.this`
  (the left branch) is used for **column names**, and `cte_body_sql = cte_body.sql(...)`
  (line 982) serializes the **whole union body in isolation**.
- `base.py:988–994` — the left branch of `cte_insert` is `SELECT *`, so its single
  projection is an `exp.Star`, which the loop **skips** (`continue`). No column name is
  derived from the star, so **no `sg_lineage` call is made at all** for `cte_insert`'s
  real columns.
- `base.py:1011–1015` — even if a concrete column name reached this call, the
  serialized body `"SELECT * FROM cte_voorraad_bouwmarkt UNION ALL SELECT * FROM
  cte_voorraad_webshop UNION ALL SELECT * FROM cte_voorraad_igdc"` is passed to
  `sg_lineage` with **no `sources=`**, so the sibling CTE names are undefined relations
  and sqlglot raises `SqlglotError: Cannot find column '<col>' in query`. The exception
  is swallowed at `base.py:1035`.

**Confirmed empirically** against the real file
`/home/ignwrad/Projects/dwh/etl/sql/fact/wtfe_kpi_voorraad_artikel_voorraadlocatie.sql`
through the full indexer pipeline:

| Column | Incoming `COLUMN_LINEAGE` | Status |
|---|---|---|
| `cte_voorraad_bouwmarkt.ma_vrije_vrd` | `← ba.wtfs_voorraad_dagstand.ma_vrije_vrd` (CTE_PROJECTION) | OK (branch traces) |
| `ba_tmp.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd` | `← cte_insert.ma_vrije_vrd` (SELECT) | OK (final target linked) |
| **`cte_insert.ma_vrije_vrd`** | **(none)** | **ISLAND — the residual bug** |

And the isolated minimal repro of the failing call:

```python
sg_lineage("ma_vrije_vrd",
           "SELECT * FROM cte_a UNION ALL SELECT * FROM cte_b",
           dialect="snowflake")
# → SqlglotError: Cannot find column 'MA_VRIJE_VRD' in query.
```

Note: the whole-statement `qualify` of the real file **succeeds** (so `body_scope` is
built and the *outer* `final ← cte_insert` edge is emitted via the main loop). The gap
is strictly the **upstream side of `cte_insert`**, produced by the CTE-projection loop.

### The fix approach

Two coordinated changes inside the CTE-projection loop (`base.py:946–1041`):

1. **Resolve the union branch column names, not the star.** When `cte_body` is a
   `exp.Union` and the left-branch projection is a bare `exp.Star` (or
   `<alias>.*`), derive the column-name list from the **referenced sibling CTE's**
   projection instead of skipping. The branch's `FROM` relation name (e.g.
   `cte_voorraad_bouwmarkt`) is looked up in `combined_sources` (already built at
   `base.py:626–644`, keyed lowercase, carrying every same-statement CTE body). Use the
   first branch's referenced CTE as the authoritative column list (union branches must
   be union-compatible; the existing explicit-column union path already assumes left-branch
   column names at `base.py:970–973`).

2. **Give `sg_lineage` the sibling CTE bodies as `sources=`.** Build a
   `dependency_filter`-style map containing **only** the CTE names that this union body
   references (parsed once from the union body's `exp.Table` nodes), drawn from
   `combined_sources`. Pass that filtered map as `sources=` to the existing
   `sg_lineage(cte_col_name, cte_body_sql, dialect=..., sources=<filtered_siblings>)`
   call at `base.py:1011`. sqlglot then expands `*` against each branch CTE's schema and
   unions the per-branch lineage. Verified:

   ```python
   sg_lineage("ma_vrije_vrd", "SELECT * FROM cte_a UNION ALL SELECT * FROM cte_b",
              dialect="snowflake", sources={"cte_a": <body>, "cte_b": <body>})
   # → resolves; leaves reach VRD.MA_VRIJE_VRD on both WTFS_VOORRAAD_DAGSTAND
   #   and WTFS_VOORRAAD_DAGSTAND_WEBSHOP branches.
   ```

   The resulting tree leaves are the **physical source tables** of each branch
   (`ba.wtfs_voorraad_dagstand`, `ba.wtfs_voorraad_dagstand_webshop`), so
   `_lineage_node_to_edges` emits, per branch:
   `cte_insert.<col> ← <branch_physical_source>.<col>`, tagged `CTE_PROJECTION`.

### Edge semantics, transform tag, and confidence

- Carry the reconnection with **plain `COLUMN_LINEAGE`**, reusing the existing
  `transform="CTE_PROJECTION"` tag the CTE loop already applies at `base.py:1031`.
  This keeps it identical to how every other CTE projection edge is recorded.
- **Confidence 1.0** — the same as all other `CTE_PROJECTION` edges (`base.py:1032`
  passes `edge.confidence` through; the leaf edges are emitted at confidence 1.0 by
  `_lineage_node_to_edges`). The star is structurally resolved against a known CTE
  schema, not inferred, so it is not a 0.8 `STAR_EXPANSION`.

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

### Why this stays one hop and does not collapse other CTE chains

Passing `sources=` makes sqlglot inline the branch CTE bodies, so the emitted edge is
`cte_insert.<col> ← <branch physical source>.<col>` (the `cte_a` hop is collapsed inside
*this* edge). This is acceptable and does **not** regress the eb19f29 anchor case
because:
- The branch CTE's own projection edge (`cte_a.<col> ← src.<col>`) is still emitted by
  the CTE-projection loop's iteration over `cte_a`, so the `cte_a` intermediate node
  remains in the graph with its upstream intact.
- The downstream `final ← cte_insert` edge already exists (main loop).
- Net effect: `cte_insert` gains incoming edges to the physical sources, reconnecting
  the two healthy halves. The `--include-intermediate` traversal still shows
  `cte_a`/`cte_b` via their own edges.

This `sources=` is scoped to **same-statement siblings only** (typically 2–4 CTEs),
filtered to the names the union body references — it is NOT the cross-file corpus.

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

**Step 1.1**: In the CTE-projection loop, detect a union body whose **deepest
left-branch** projection is a star and derive the column-name list from the referenced
sibling CTE.
- Files affected: [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)
  (`_extract_column_lineage`, the `for cte in cte_expressions` block at ~946–1041).
- Detail: when `isinstance(cte_body, exp.Union)`, walk to the **deepest left-branch
  `exp.Select`** by traversing `ps = cte_body.this` until `isinstance(ps, exp.Select)`
  (i.e. `while isinstance(ps, exp.Union): ps = ps.this`). This handles both 2-branch
  (`cte_body.this` is a `Select`) and N-branch (`cte_body.this` is itself a `Union`
  whose `.expressions` is always `[]`). Only when `ps.expressions` is a single `exp.Star`
  (or `<alias>.*`) do we take the star-expansion path: collect the union body's referenced
  relation names from its `exp.Table` nodes (left branch first), look the first one up in
  `combined_sources`, and use **that** CTE body's non-star projections to supply
  `cte_col_name`s. Fall back to the existing behaviour (skip) if the referenced relation
  is not a known sibling CTE in `combined_sources`.
- **Why `cte_body.this` alone is insufficient**: for a 3-branch `A UNION ALL B UNION ALL C`,
  sqlglot parses as `Union(this=Union(this=A, expression=B), expression=C)`. The outer
  `Union.this` is another `Union`, whose `.expressions` = `[]` (empty). Checking
  `projection_source.expressions == [Star]` never matches. Walking to the deepest
  `Select` is O(N_branches) per CTE — once, before the per-column loop.
- The branch-CTE column-list lookup and the union body's referenced-CTE set are computed
  **once per CTE**, before the per-column loop (perf invariant: no per-column re-parse).
- Acceptance: for the synthetic 3-branch fixture, `cte_union`'s projected columns are
  enumerated from the first referenced branch CTE's explicit column list.

**Step 1.2**: Pass the filtered sibling-CTE bodies to the `sg_lineage` call so the star
expands.
- Files affected: same loop, the `sg_lineage(cte_col_name, cte_body_sql, ...)` call at
  `base.py:1011`.
- Detail: build `union_cte_sources = {name: combined_sources[name] for name in
  referenced_cte_names if name in combined_sources}` **once before the per-column loop**,
  and pass it as `sources=union_cte_sources`. The referenced-name set is the
  `dependency_filter` that keeps this O(N_refs), not O(N_corpus).
- This `sources=` is only added on the union-star path; the existing explicit-column CTE
  path keeps calling `sg_lineage` with no `sources=` (unchanged) so we do not alter the
  general-recall behaviour the v1.1.2 guards pin.
- Acceptance: `sg_lineage` resolves; `_lineage_node_to_edges` emits
  `cte_insert.<col> ← <branch physical source>.<col>` `CTE_PROJECTION` edges for every
  branch.

**Step 1.3**: Keep the loop allocation-light — verify no per-column serialization or
per-column `combined_sources` rebuild was introduced.
- Files affected: same loop.
- Detail: `cte_body_sql` stays hoisted (already at `base.py:982`); the
  `union_cte_sources` map and the branch column-name list are built once per CTE before
  the inner `for cte_col_expr` loop.
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
     the 3-branch fixture, `cte_union.total_amt` has 0 incoming edges today (the island).
     After the fix, incoming edges exist (from physical sources via `sources=` resolution).
     This is the central bug sentinel. Note: the fix emits `staging.src_*.amount ->
     cte_union.total_amt` directly (sqlglot collapses the CTE hop when `sources=` is
     passed), so the upstream set includes physical sources, not just CTE intermediates.
  2. `test_union_star_cte_final_target_reaches_both_sources` — the end-to-end CLI-shaped
     traversal from `mart.union_star_dst.total_amt` upstream reaches both
     `staging.src_a.amount` and `staging.src_b.amount` (chain reconnected across the
     `cte_union` boundary). Uses the production `analyze._kind_filter` helper (as the
     v1.1.2 guards do), so a filter regression also reds it.
  3. `test_explicit_column_union_still_traces` — a sibling test using the **existing**
     `_UNION_ALL_SQL`-style explicit-column union to prove the fix did not change the
     already-working path (anti-regression for the general recall).
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
| `exp.expand()` with file-level `sources` filtered by `dependency_filter` (O(N_refs)) | The new `sources=` passed to `sg_lineage` is the **same-statement sibling CTE** map, filtered to only the union body's referenced names (the dependency_filter). It is NOT the cross-file corpus. No new `exp.expand()` call is added. |
| `body_scope` built once per statement | Untouched — the main loop's `body_scope` logic at `base.py:646–692` is not modified. The CTE loop does not build a `body_scope`. |
| Pure-literal skip | Untouched — the branch CTE column enumeration skips star/literal projections exactly as the existing `cte_projections` loop does. |
| `copy=False` + `trim_selects=False` when a scope is passed | Untouched — the CTE-projection `sg_lineage` call uses a serialized string + `sources=`, not a `scope=`. The scope-path kwargs at `base.py:895–909` are not modified. |
| INSERT body copied once | Untouched — INSERT block at `base.py:713–808` not modified. |
| `body_scope` built before column loop, never lazily inside | Untouched. The new per-CTE work (`union_cte_sources` map + branch column-name list) is computed **once per CTE before the inner `for cte_col_expr` loop**, mirroring the existing hoist of `cte_body_sql` at `base.py:982`. No per-column re-parse or re-serialize. |
| Bulk/batch upsert paths | Untouched — `indexer.py` not modified. |

**Complexity statement**: the added work is O(N_branch_cols + N_referenced_ctes) per
union CTE, computed once before the per-column loop. The per-column `sg_lineage` calls
are unchanged in count (one per branch column, as the existing CTE loop already does);
only their `sources=` argument is populated on the union-star path. There is no new
op that scales with column-count squared, no full-corpus expand, and no per-column
qualify/build_scope. Both scales (20-file and 1600-file) are unaffected: small repos see
the same fast path; the DWH corpus gains 80 reconnected `cte_insert` islands at the same
indexing cost (the failing `sg_lineage` call already ran and threw — we replace a swallowed
exception with a successful resolution at equal call count).

**Gating tests**: `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`,
`test_bulk_upsert_invariant.py`, `test_upsert_batch_invariant.py` must all stay green.

## Acceptance Criteria

- [ ] On the **3-branch** synthetic fixture, `cte_union.total_amt` has ≥1 incoming
      `COLUMN_LINEAGE` edge (currently 0 — confirmed island with 3 branches).
- [ ] `test_union_star_cte_gets_incoming_edges` is **red on `master`/pre-fix** (0 edges;
      verified with the 3-branch fixture) and **green after the fix**.
- [ ] End-to-end: upstream traversal from `mart.union_star_dst.total_amt` reaches both
      physical sources through the reconnected `cte_union` boundary.
- [ ] The existing explicit-column union path is unchanged: `test_union_all_cte_*` in
      [`test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py) stay green.
- [ ] All four perf-invariant guards stay green; `pyright` 0 errors; `ruff` clean.
- [ ] Full suite passes (no new failures, skips, or xfails introduced beyond the new tests).
- [ ] Edges carry `transform="CTE_PROJECTION"` and `confidence=1.0` (no `STAR_SOURCE` row
      and no `STAR_EXPANSION` edge is emitted for the CTE-internal union).
- [ ] Version reports `1.1.3`.
- [ ] (Manual, on the private DWH corpus, not in CI) re-index reduces `cte_insert.*`
      islands from 80 toward ~0 and `ba.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd`
      `--raw --include-intermediate --depth 12` reaches a `da.`/`ban.` raw source again.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| sqlglot collapses the branch-CTE hop, hiding `cte_a`/`cte_b` from a `--include-intermediate` trace | The branch CTE's own projection edge (`cte_a.<col> ← src.<col>`) is still emitted by the loop's iteration over `cte_a`, so the intermediate node survives. Confirmed: `cte_voorraad_bouwmarkt.ma_vrije_vrd` already has its upstream edge in the live graph. |
| `sources=` accidentally pulls in cross-file CTAS bodies and explodes | The map is built strictly from `combined_sources` filtered to the union body's referenced relation names (same-statement siblings only). Explicitly NOT the corpus map. |
| Branch CTEs are not union-compatible / column counts differ | We take the first branch's referenced CTE column list (the existing union path already assumes left-branch column names at `base.py:970–973`). Mismatched branches degrade to the current behaviour (no edge), never to a wrong edge. |
| Regressing the general CTE recall the v1.1.2 guards pin | The `sources=` argument is added **only** on the star-over-union path; the explicit-column path is byte-for-byte unchanged. `test_cte_recall_guard.py` + the new `test_explicit_column_union_still_traces` gate this. |
| Perf regression in the hot loop | New work is once-per-CTE, O(N_refs); same `sg_lineage` call count. Gated by `test_perf_scaling_guard.py`. |
| `qualify()` in-place mutation changes `cte_body_sql` for 2-branch fixtures | `qualify()` mutates the AST in-place; for 2-branch unions it expands `SELECT *` to explicit columns, so `cte_body_sql` (line 982) serializes the qualified SQL. This means the 2-branch case already works today (qualify rescues it). The fix targets the case where the star is NOT expanded: 3+ branches (`projection_source` is a Union with `.expressions = []`) or future edge cases where qualify doesn't expand. The guard MUST use a 3-branch fixture to exercise the true unrescued path. |

## PR Breakdown

Single PR (`fix(#38): expand SELECT * across a UNION ALL of sibling CTEs`):
- `src/sqlcg/parsers/base.py` (CTE-projection loop, Steps 1.1–1.3)
- `tests/integration/test_union_cte_star_recall_guard.py` (NEW, Step 2.1)
- `pyproject.toml`, `src/sqlcg/__init__.py`, `uv.lock` (version bump, Step 3.1)
- (optional) `tests/unit/test_perf_scaling_guard.py` (only if a new counted op is added)

Version bump: **1.1.2 → 1.1.3** (patch — bug fix, no API/schema change). Re-index is the
migration path (CLAUDE.md "No backward compatibility").

## Blocking Questions

**Corrected by plan-reviewer 2026-06-01** — two blockers found and fixed in-plan above.
Original "None blocking" verdict was incorrect.

**B1 (FIXED IN-PLAN)**: 2-branch fixture masked the bug. Changed to 3-branch in Step 2.1
and Test Strategy. The 2-branch case already works today because `qualify()` expands the
star in-place; the real island is the N≥3 branch case.

**B2 (FIXED IN-PLAN)**: Step 1.1 only checked `projection_source.expressions == [Star]`
but for N≥3 branches `projection_source` is a Union with `.expressions = []`. Fixed to
walk to the deepest left-branch Select first.

Non-blocking note:
- ARCHITECTURE_REVIEW.md does not yet record this residual #38 disposition (the finding
  lives in [`findings_v1.1.2_dwh_eval.md`](findings_v1.1.2_dwh_eval.md), which states
  "keep #38 open, narrow scope to UNION-ALL-of-CTEs star"). This plan proceeds on that
  documented disposition and the user's explicit request. Recommend the architect-reviewer
  add a one-line entry to ARCHITECTURE_REVIEW.md when the fix lands (Step 3.2); it is not
  a precondition for implementation.
