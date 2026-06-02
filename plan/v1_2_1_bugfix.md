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

**Where:** [`CrossFileAggregator`](../src/sqlcg/lineage/aggregator.py) already holds the DDL
identity map. `register_pass1` records `self.sources[table.full_id] = parsed` for every
`parsed.defined_tables`. Add a parallel **bare-name → canonical full_id** index built from the
*DDL-defined* tables only:

```python
# in __init__:
self.canonical_by_bare: dict[str, str] = {}   # bare name (lower) -> sole DDL full_id
self._ambiguous_bare: set[str] = set()        # bare names defined by >1 schema (do NOT rewrite)

# in register_pass1, for each table in parsed.defined_tables:
bare = (table.name or "").lower()
if bare:
    if bare in self.canonical_by_bare and self.canonical_by_bare[bare] != table.full_id:
        self._ambiguous_bare.add(bare)
    self.canonical_by_bare[bare] = table.full_id
```

**Resolution rule (applied in `resolve_pass2`, before re-parse emits the target node):**
for a statement whose `target` is unqualified *or* whose `target.full_id` is not itself a
DDL-defined table, if `target.name.lower()` maps to exactly one DDL canonical (not in
`_ambiguous_bare`), rewrite the target `TableRef` to the canonical `db`/`catalog`/`name`.
Ambiguous bare names are left untouched (the existing `_bare_ref` CLI hint already covers
the multi-schema case — do not silently merge across schemas).

**Critical (CLAUDE.md alias rule):** the rewrite must produce a `TableRef` whose `full_id`
is *identical* to the DDL table's `full_id`, so the INSERT-target `SqlTable` node, its
`SqlColumn.table_qualified`, and the DDL node all share one identity. Mirror the
`#39 Half A` discipline: derive every emitted field from the same resolved `TableRef`.

**Re-emission must dedup to a no-op** — the canonical node already exists from the DDL pass;
`upsert_nodes_bulk` (Phase A→B→C) is keyed on `qualified`, so re-emitting the same identity
is a no-op. Do **not** add a per-row `upsert_node` (Performance invariant).

> Open design point for plan-reviewer / architect-reviewer: confirm the rewrite belongs in
> `resolve_pass2` (so it is applied once, with the full DDL map available) rather than in the
> per-statement parser (which lacks cross-file DDL knowledge in pass 1). The aggregator is the
> only place that has both the target and the complete DDL identity map.

### #45.1 — `file:line` on every source row

**Root cause:** the current query binds the query/location from the edge **into the queried
column** (`OPTIONAL MATCH (src)-[direct:COLUMN_LINEAGE]->(c)`), which only exists for the
immediate neighbour of `c`. Multi-hop sources have no such direct edge → `direct.query_id`
is null → `q` is null → [`_add_file_line_col`](../src/sqlcg/cli/commands/analyze.py) renders `?`.

**Fix:** bind the location from the edge **leaving the source node** — the first
`COLUMN_LINEAGE` edge out of `src` (the edge that defines where `src` is *read*), e.g.

```
OPTIONAL MATCH (src)-[srcedge:COLUMN_LINEAGE]->()
OPTIONAL MATCH (q:SqlQuery {id: srcedge.query_id})
RETURN src.id AS id, q.file_path AS file, q.start_line AS line ...
```

This requires `query_id` to be populated on `COLUMN_LINEAGE` edges (it is — #31/#32 shipped
`query_id`/`confidence`; the DWH eval confirms `start_line` on all 5630 `SqlQuery` nodes).
A source with several outgoing edges may match more than one query; pick deterministically
(e.g. `ORDER BY q.start_line` and take the edge whose `dst` is on the traversed path if
expressible, otherwise any one — a real `file:line` beats `?`). Decide the tie-break in
implementation and assert it in TC6.

> Note for plan-reviewer: `srcedge` is unbounded (`-> ()`), so confirm it does not explode
> row count via the existing `LIMIT 100` + `RETURN src.id AS id ...` dedup. If it multiplies
> rows, aggregate with `min(q.start_line)` per `src` in the RETURN.

### #45.2 — suppress `cte_*` synthetic nodes from filtered output

With the restored `_kind_filter` (OPTIONAL-MATCH + `kind IN ['table','external']`), a node
**with** a `SqlTable` node of `kind='cte'`/`'derived'` is already excluded. The DWH leak was
the synthetic `cte_insert` node: confirm it carries a non-`table` kind (`'cte'`) so the filter
drops it. If `cte_insert`-style synthetic nodes are emitted without a `SqlTable` node at all,
the `IS NULL` branch would *keep* them — so #45.2 needs an explicit belt-and-braces guard:
in [`_filter_column_results`](../src/sqlcg/cli/commands/analyze.py) (the post-query
`NoiseFilter` pass, default path only), drop rows whose table component matches the synthetic
`cte_*`/derived prefix. Keep them under `--raw` / `--include-intermediate`. TC4 pins this.

> Resolve in implementation: prefer the kind-based filter (graph-truth) over a name-prefix
> filter (heuristic). Only add the name-prefix drop if a synthetic node legitimately lacks a
> `SqlTable` node — confirm against the fixture, do not guess.

### #40 — guard suite (reused design, no re-derivation)

Land the fixture + TC1–TC7 exactly as specified in
[`fix_issue_40_user_surface_regression_guards.md`](fix_issue_40_user_surface_regression_guards.md):
- Synthetic committable fixture carrying shapes **A** (≥2 CTE hops), **B** (UNION-ALL branch
  CTE), **C** (`INSERT INTO mart.fact_kpi` + separate DDL `CREATE TABLE mart.fact_kpi`),
  **D** (source referenced only inside a CTE body).
- Tests drive the **public surface** (the exact `analyze` filtered query via the production
  `_kind_filter`, and the MCP trace tool) — never a raw `COLUMN_LINEAGE` `MATCH`.
- TC3 (#44 canonical name) and TC6 (#45 `file:line`) are written to be **RED before** the
  #44/#45 fixes land and **green after** — they are the acceptance tests for those fixes.

---

## Implementation steps (dependency-ordered)

> Ordering rationale: the regression fix + restored `_kind_filter` is the foundation the
> guard suite imports, so it goes first; then the guards (some RED); then #44/#45 flip them green.

### Phase 0 — Read-proxy filter regression (foundation; also #39 read-half)
**Step 0.1** Reintroduce `_kind_filter(node_alias, *, include_intermediate)` in
[`analyze.py`](../src/sqlcg/cli/commands/analyze.py); replace both inline filter strings in
`upstream`/`downstream` with calls to it; restore the OPTIONAL-MATCH + `kind IS NULL OR kind IN`
form.
- Call sites (grep-confirmed required): `upstream` (was L38-43), `downstream` (was L98-103),
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
[`CrossFileAggregator.register_pass1`](../src/sqlcg/lineage/aggregator.py).
**Step 2.2** Apply the rewrite in `resolve_pass2` before the target node is emitted; leave
ambiguous bare names untouched.
- Call site: `resolve_pass2` is called from the indexer two-pass loop (grep-confirm the
  existing call in [`indexer.py`](../src/sqlcg/indexer/indexer.py) before opening the PR).
- Test file: `tests/unit/test_canonical_target_resolution.py` (unit: ambiguous bare name not
  rewritten; sole-DDL match rewritten; emitted node `full_id` == DDL `full_id`).
- Acceptance: TC3 green — querying `mart.fact_kpi.measure` returns the **same** source set as
  `fact_kpi.measure`; the schema-less duplicate node no longer appears in `find`.

### Phase 3 — #45 (flips TC6 green) + cte_* leak
**Step 3.1** Rebind `file:line` to the source node's outgoing edge in `upstream`/`downstream`
queries; handle the multi-edge tie-break deterministically.
**Step 3.2** Confirm/repair `cte_*` suppression in the default path (kind filter first;
name-prefix belt only if a synthetic node lacks a `SqlTable` node — verify on the fixture).
- Test file: `tests/integration/test_user_surface_recall_guard.py` (TC6 + TC4 assertions).
- Acceptance: TC6 green — every physical-source row carries a real `file:line`, only synthetic
  nodes may be `?`; TC4 green — no `cte_*` row in default output, present under `--raw`.

### Phase 4 — Release plumbing
**Step 4.1** Bump `version = "1.2.1"` in [`pyproject.toml`](../pyproject.toml) and
`__version__` in [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py); run `uv lock`.
(Tagging is post-merge per CLAUDE.md — not part of this plan.)
**Step 4.2** Full suite green: `uv run pytest`; `uv run pyright`; `uv run ruff check src tests`.
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
      only synthetic nodes may show `?`.
- [ ] TC4: no `cte_*`/derived row appears in default output; present under `--raw`.
- [ ] TC1/TC2/TC5/TC7 green (multi-hop recall, UNION-ALL completeness, CLI↔MCP parity, find/unused honesty).
- [ ] #44 rewrite leaves ambiguous bare names untouched (unit test asserts no cross-schema merge).
- [ ] No new per-row `upsert_node`/`upsert_edge`; perf scaling + bulk/batch upsert guards green.
- [ ] `version == "1.2.1"` in pyproject + `__init__.py`; `uv lock` refreshed; pyright + ruff clean.

## Scope decision: residual #38 and #28

- **Residual #38 (UNION-ALL of sibling CTEs)** — **OUT.** Already fixed in 1.1.3 (`f6f276b`),
  guarded by `test_union_cte_star_recall_guard.py`. The findings doc reporting 80 islands ran
  against 1.1.2 (pre-fix). Re-opening it would duplicate shipped work. If a *fresh* DWH eval on
  1.2.1 still shows islands, file a new narrow issue with a 1.2.x repro — do not pad this patch
  on a stale measurement.
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
- **R3 — #45.1 row multiplication** from the unbounded `srcedge` match.
  *Mitigation*: aggregate `min(start_line)` per `src`; TC6 asserts row identity, not just `file:line`.
- **R4 — Phase 2 hot-path regression.** *Mitigation*: change is in the node-emission loop with
  dedup-no-op upsert; perf scaling + bulk/batch guards are blocking acceptance criteria.
