# Feature Plan: Coverage P1a / P1b / P5 — Fix orphaned-edge root causes (DWH-verified)

## Summary

The DWH graph (queried live against `~/.sqlcg/graph.db` on 2026-06-09) reveals that the
orphaned-edge picture assumed in
[`coverage_p1_p3_p4.md`](coverage_p1_p3_p4.md) is materially wrong on two counts:

1. **The proposed metric fix (`kind NOT IN ('cte','derived')`) is incorrect.** 2,479 of
   the orphaned edges point to tables that have `kind='derived'` only because of a parser/
   indexer bug — they are real ETL target tables (CTAS targets) that must be fixed to
   `kind='table'`, not excluded from the metric.
2. **P1 is not a single bug.** The 10,056 genuinely orphaned edges split into three
   independent sub-problems, two of which (P1a, P1b) are parser/indexer defects requiring
   code fixes, not metric adjustments. P5 (bare names, USE SCHEMA) was already deferred and
   is confirmed as Sub-problem C here.

**The metric formula stays unchanged.** `_Q_EDGE_HEALTH` and `_Q_BLINDSPOT` in
[`coverage.py`](../../src/sqlcg/cli/coverage.py) remain: count by `HAS_COLUMN` presence,
all edges in the denominator. Fix the underlying data, not the ruler.

This plan covers P1a and P1b. P5 (Sub-problem C) is scoped separately at the end but
may ship in the same PR if the P1a/P1b implementation effort allows.

---

## DWH Findings (authoritative baseline, 2026-06-09)

Queried live from `~/.sqlcg/graph.db` after re-indexing with P3 + P4 shipped (v1.7.0).

| Quantity | Count |
|----------|-------|
| Total `COLUMN_LINEAGE` edges | 51,434 |
| CTE/derived-destination edges (total) | 15,209 |
| — edges with a real-table partner (legitimate chain intermediates) | 5,153 |
| — genuinely orphaned (no real-table partner exists for the same `src_key`) | **10,056** |

The 10,056 orphaned edges break into three sub-problems:

| Sub-problem | Edge count | Table count | Root cause |
|-------------|-----------|-------------|------------|
| **A — CTAS kind misclassification** | 2,479 | 55 tables | Schema-qualified CTAS targets stored as `kind='derived'` instead of `kind='table'` |
| **B — INSERT+CTE UNION terminal** | 4,561 | ~bare-name CTEs: `cte_insert`, `final`, `base`, `totaal`, … | `#25` positional block does not fire for these cases; only the CTE chain edge exists |
| **C — Bare unqualified names** | 3,016 | 149 tables | No schema context (USE SCHEMA not resolved); overlaps with P5 cohort |

The **5,153 legitimate chain intermediates** (CTE/derived nodes that DO have a real-table
partner) do not need any fix — they are structurally correct intermediate graph nodes
required for multi-hop upstream traversal. The metric correctly omits them from `good_edges`
(they point to CTE nodes with no `HAS_COLUMN`), but those same `src_key` values also have
real-target partner edges that ARE counted as good.

---

## Sub-problem A — CTAS kind misclassification (P1a)

### What happens today

Schema-qualified table names like `da.htdyn_vendvendor`, `da.rtint_factuur`,
`ba.wtfa_loonkosten` appear in `SqlTable` with `kind='derived'` instead of `kind='table'`.
These are real ETL target tables produced by `CREATE TABLE <name> AS SELECT …` statements.
They have wrong kind because:

1. The CTAS statement in their defining file is not being registered as a `defined_table` by
   the parser — so the indexer never emits a `kind='table'` row for them through the
   `defined_tables` path in [`indexer.py`](../../src/sqlcg/indexer/indexer.py) (line 1109–1117).
2. When another file INSERTs into them, the INSERT-target path (line 1301–1335 of `indexer.py`)
   fails to resolve the schema-qualified name against `canonical_by_bare` (which is a bare-name
   index) and falls through to `target_kind = "derived"` (line 1335).

Since P4 (shipped in v1.7.0) now populates `defined_columns` for CTAS bodies, these 55 tables
would get `SqlColumn` + `HAS_COLUMN` rows **if** their kind were correct. After P1a the P4
catalog already in the graph wires to them and their orphaned edges become good.

### Diagnosis steps (developer must run before implementing)

1. Pick a sample affected table (e.g. `da.htdyn_vendvendor`). Find its defining file:
   ```sql
   SELECT defined_in_file FROM "SqlFile" sf
   JOIN "DEFINED_IN" di ON di.dst_key = sf.qualified
   WHERE di.src_key = 'da.htdyn_vendvendor';
   ```
   If no row: the parser is not registering this as a `defined_table` at all. That is the root.
2. Parse the defining file with the Snowflake parser in isolation (or add a debug-print to
   `_parse_statement`). Check whether the `CREATE TABLE … AS SELECT …` statement produces a
   `QueryNode` with `kind='CREATE_TABLE'` and the table in `parsed.defined_tables`.
3. Hypothesis 1 — the statement has a Snowflake-specific syntax variant (e.g.
   `CREATE OR REPLACE TRANSIENT TABLE …` or `CREATE TABLE IF NOT EXISTS …`) that the
   `_parse_statement` regex/AST match does not recognise as a CTAS, so `defined_tables` is
   never populated.
4. Hypothesis 2 — the table IS in `defined_tables` but re-indexing under another file's
   INSERT-target path **overwrites** the `kind='table'` row with `kind='derived'` because
   `upsert_nodes_bulk` does not guard against downgrading kind from 'table' to 'derived'.
   Check [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `_structural_kinds` merge logic
   (line 128–134) — `_structural_kinds = {"cte", "derived", "external"}`. The merge logic at
   line 131–132 already says "if existing is not structural and incoming is structural, keep
   existing" — i.e. a `kind='table'` row should resist being downgraded to `kind='derived'`.
   Verify the merge actually runs on the upsert path for these tables.

### Fix

**If Hypothesis 1:** In [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) (and/or
[`snowflake_parser.py`](../../src/sqlcg/parsers/snowflake_parser.py)), extend the CTAS
recognition in `_parse_statement` to cover the failing syntax variants. This is a
once-per-statement structural change with no perf impact — same class as P4.

**If Hypothesis 2:** In [`indexer.py`](../../src/sqlcg/indexer/indexer.py), verify the
`_structural_kinds` merge guard actually prevents the downgrade. If it is not called on the
INSERT-target `table_rows` accumulation path, wire it there. If it is called but the guard
fires in the wrong direction, fix the condition.

**Both hypotheses may be true simultaneously** (e.g. the CTAS is parsed correctly in its
own file but a later INSERT-target path in a different file re-emits a `kind='derived'` row
that is not guarded). Verify with the sample table before fixing only one.

### Acceptance

- All 55 misclassified tables have `kind='table'` after a re-index.
- Their edges are counted in `good_edges` (they now have `HAS_COLUMN` via P4).
- No regression in `test_analyze_case_fold`, CTE-chain tests, or perf guards.

---

## Sub-problem B — INSERT+CTE with UNION terminal (P1b)

### What happens today

For patterns like:

```sql
INSERT INTO ba.fact (col_list)
WITH cte_datum AS (…),
     cte_insert AS (SELECT … UNION SELECT …)
SELECT col_list FROM cte_insert;
```

Only the CTE chain edges exist (`ba.src.x → cte_insert.x`). The `#25` positional block
(which should emit `cte_insert.x → ba.fact.col`) does not fire. The result: 4,561 edges
orphaned on bare CTE names like `cte_insert`, `final`, `base`, `totaal`.

The hypothesis is that a **UNION inside the terminal CTE** prevents the positional block
from resolving the columns. The `#25` positional block in
[`base.py`](../../src/sqlcg/parsers/base.py) (L776–871) likely expects the CTE body to be
a simple `SELECT`, and when it encounters a `UNION` (an `exp.Union` AST node instead of
`exp.Select`), the column-count or column-name extraction fails, so the block silently
emits nothing.

### Diagnosis steps (developer must run before implementing)

1. Pick a sample affected file (any INSERT with `cte_insert` or `final` as the terminal CTE).
   Check whether the INSERT target appears in the graph at all:
   ```sql
   SELECT * FROM "COLUMN_LINEAGE" WHERE dst_key LIKE 'ba.fact.%' LIMIT 5;
   ```
   If absent: the positional block skipped entirely (not just UNION).
2. Reproduce in a unit test fixture with a minimal INSERT+CTE+UNION pattern. Run the parser
   and inspect the emitted `column_lineage` edges. Confirm the terminal CTE's columns are
   missing from the real-target dst.
3. In `base.py`, find the positional block (`#25`, ~L776–871). Trace the column-extraction
   logic: is the terminal CTE body fetched from the parsed WITH clause? Does the code walk
   to the inner SELECT of a UNION (`exp.Union.this`) or does it fail on a non-`Select` body?
4. Check also whether the fix for **bare CTE terminal names** (no schema prefix on
   `cte_insert`) affects the `canonical_by_bare` lookup — the unqualified bare name may also
   cause the INSERT target to be unresolved, compounding the UNION failure.

### Fix

**If the UNION body is the cause:** In the positional block, when the terminal CTE body is
an `exp.Union`, walk to the left-most leaf `exp.Select` (deepest `union.this`) to extract
column names and count. This mirrors the existing CTE-projection handling at `base.py`
L1165–1170.

**If the bare target name is also a cause:** The INSERT target `ba.fact` is schema-qualified
in the SQL but appears bare in the graph. Check whether the INSERT target's name is being
stripped of its schema somewhere in the `#25` block or the CTE alias resolution. If so, add
the same `canonical_by_bare` resolution used for regular INSERT targets.

> **This is the harder fix.** If the UNION case requires non-trivial rework of the positional
> block (e.g. multi-branch column enumeration), or if the fix regresses existing tests
> (union-CTE star recall guard, `test_backfill_impact_consistency`), document the gap clearly
> and treat P1b as a follow-up investigation sprint rather than forcing a fragile fix into
> this PR. A well-documented known gap is better than a silent partial fix.

### Acceptance

- The minimal INSERT+CTE+UNION fixture emits edges to the real INSERT target, not just to
  the terminal CTE node.
- `test_union_cte_star_recall_guard` and `test_backfill_impact_consistency` stay green.
- Perf guards stay green (the positional block change must not introduce per-column
  qualify/build_scope calls).
- DWH re-index: Sub-problem B orphaned edge count drops measurably (partial fix acceptable
  if only the UNION-terminal case is addressed; document what remains).

---

## Sub-problem C — Bare unqualified names / USE SCHEMA (P5)

3,016 edges / 149 tables. Bare table names that should be schema-qualified (e.g.
`wtfe_verkoopinfo`, `hthyb_promotion`). These overlap with the deferred P5 cohort from
[`coverage_p1_p3_p4.md`](coverage_p1_p3_p4.md). **P5 is explicitly deferred to a follow-up
sprint** — it requires schema-context resolution (USE SCHEMA tracking or a schema-inference
pass) and is a significantly larger change. This plan documents it for completeness but does
**not** implement it.

---

## Revised 95% Feasibility Analysis

> All figures against the **original metric formula** (all 51,434 edges in denominator,
> good = has HAS_COLUMN). The metric formula does NOT change.

### Current state (post P3 + P4, v1.7.0)

| Quantity | Value |
|----------|-------|
| Total `COLUMN_LINEAGE` edges (denominator) | 51,434 |
| Good edges (before this sprint) | ~14,884 (pre-P3/P4 baseline) |
| Good edges (estimated after P3+P4 re-index) | ~14,884 + ~6,045 (P3) + unknown (P4) |
| CTE/derived-dst edges (total) | 15,209 |
| — legitimate chain intermediates (correct, have real-table partner) | 5,153 |
| — genuinely orphaned | 10,056 |

P3 shipped: +6,045 good edges (232 view tables with HAS_COLUMN catalog).
P4 shipped: +unknown edge count (241 CTAS tables got column catalog; their edges become good
when those tables were the dst). Rough estimate: 241 tables × ~10 edges/table = ~2,410 edges
(unverified — measure after re-index).

### Projected gain from P1a + P1b

| Fix | Good edge gain | Confidence |
|-----|----------------|------------|
| P1a — kind fix for 55 CTAS tables | **+2,479** (all Sub-problem A orphans) | High — P4 already created HAS_COLUMN for these tables; only kind is wrong |
| P1b — positional block UNION fix | Up to **+4,561** (Sub-problem B) | Medium — depends on how many UNION-terminal INSERTs the fix covers; partial fix possible |
| P5 — USE SCHEMA / bare names | Up to **+3,016** (Sub-problem C) | Low — deferred; requires schema-context work |

### Updated projections

| Stage | Good edges (estimate) | Edge health |
|-------|----------------------|-------------|
| Pre-P3/P4 baseline | 14,884 | ~28.9% |
| + P3 (shipped) | ~20,929 | ~40.7% |
| + P4 (shipped, estimated) | ~23,339 | ~45.4% |
| + P1a (kind fix, high confidence) | ~25,818 | ~50.2% |
| + P1b (UNION fix, if complete) | ~30,379 | ~59.1% |
| + P5 (deferred) | ~33,395 | ~65.0% |
| **95% target** | **48,862** | **95.0%** |

**Conclusion: P1a + P1b + P5 together bring edge health to ~65%, not 95%.** The remaining
~30 percentage points require tackling the broad residual: P2 phantom edges (13,412
`inferred_from_source_name` edges on 304 tables with no HAS_COLUMN), unseen-DDL tables, and
temp tables. These are a separate sprint. The **95% gate remains deferred** as established
in the BQ-2 decision in `coverage_p1_p3_p4.md`.

### Sprint gate for this plan

**Edge health ≥ 50% after P1a + re-index** (≈50.2% projected). P1b is a secondary target;
if P1b is complex and risks perf regression, it ships as a follow-up. The sprint does not
claim ≥ 95%.

---

## Scope

### In Scope

- **P1a** — Diagnose and fix why 55 CTAS target tables get `kind='derived'` instead of
  `kind='table'`. Fix is in the parser (CTAS recognition) and/or the indexer (kind merge
  guard). After fix + re-index: those tables have correct kind, P4's HAS_COLUMN catalog
  wires to them, 2,479 edges become good.
- **P1b** — Diagnose and fix the `#25` positional block for INSERT+CTE patterns where the
  terminal CTE body is a UNION. Fix is in
  [`base.py`](../../src/sqlcg/parsers/base.py) positional block (~L776–871). If the fix is
  non-trivial or risks regression, document the gap and defer.
- Version bump: **patch** `1.7.0` → `1.7.1` (bug fix, no new capability beyond what P4
  already shipped — P1a restores correct classification; P1b fixes a silent positional-block
  failure). If P1b requires a meaningful new resolution path, bump to minor `1.8.0`.
- Postmortem update: record the before/after DWH edge health numbers.

### Non-Goals

- **No metric formula change.** `_Q_EDGE_HEALTH` and `_Q_BLINDSPOT` stay as-is. Do not add
  `kind NOT IN ('cte','derived')` — that was invalidated by Sub-problem A data. If a future
  sprint wants to revise the metric scope after all kind bugs are fixed, that is a separate
  decision with fresh DWH measurement.
- **No P5 / USE SCHEMA.** Bare-name Sub-problem C is deferred.
- **No P2 residual** (phantom edges / unseen-DDL / temp tables). Different cohort, different
  sprint.
- **No new MCP tools, no schema-version bump, no schema change.**
- **No perf-invariant relaxation.** All invariants in `CLAUDE.md`'s `base.py`/`indexer.py`
  table stay intact.

---

## Design

### P1a — where the kind bug lives

Two candidate locations; the developer diagnoses which applies (see Diagnosis above):

**Candidate 1: Parser — CTAS not registered as `defined_table`**

In [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) `_parse_statement`, the
condition that populates `stmt.target` in the `QueryNode` and appends to
`parsed.defined_tables` must match all CTAS variants. The Snowflake corpus likely has:

```sql
CREATE OR REPLACE TRANSIENT TABLE da.htdyn_vendvendor AS SELECT …
CREATE TABLE IF NOT EXISTS da.rtint_factuur AS SELECT …
```

If the `stmt.kind` check for `CREATE_TABLE` only matches base `Create(kind="TABLE")` and
not `TRANSIENT TABLE` or `TEMPORARY TABLE`, the target never enters `defined_tables`.

Fix: ensure `_parse_statement` matches all `exp.Create` nodes where `stmt.args.get("kind")`
is `TABLE` (regardless of `properties` like `TRANSIENT` or `TEMPORARY`). sqlglot normalises
these to `exp.Create(kind="TABLE")` regardless of TRANSIENT/TEMPORARY — verify with a quick
`sqlglot.parse_one("CREATE OR REPLACE TRANSIENT TABLE t AS SELECT 1 AS x").find(exp.Create).kind`.
If sqlglot normalises them, the parser already handles it and Candidate 2 is the real cause.

**Candidate 2: Indexer — kind merge guard not firing on INSERT-target path**

In [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `_build_file_rows`, the INSERT-target
block (L1301–1346) emits a `kind='derived'` row when `canonical_by_bare` cannot resolve the
table. The `upsert_nodes_bulk` merge (called later) has a `_structural_kinds` guard (L128–134)
that should prevent overwriting `kind='table'` with `kind='derived'`. But this guard only
applies when **both the old and new rows are already in the upsert buffer**. If:

- File A defines `da.htdyn_vendvendor` as a CTAS → emits `kind='table'` row (correct).
- File B INSERTs into `da.htdyn_vendvendor` → emits `kind='derived'` row.
- File B is processed AFTER File A in the batch, and the batch flush calls `upsert_nodes_bulk`
  with the `kind='derived'` row winning because the guard only protects within a single flush
  (not across batches).

Fix: the guard in `_structural_kinds` merge must also apply at the database level — when the
existing DB row is `kind='table'` and the incoming row is `kind='derived'`, keep the existing.
Check whether `upsert_nodes_bulk` uses INSERT-or-REPLACE or INSERT-or-UPDATE; if it replaces
unconditionally, add a `WHERE existing.kind NOT IN ('table','view')` clause so structural
kinds cannot downgrade a real table.

### P1b — positional block UNION handling

The `#25` positional block in [`base.py`](../../src/sqlcg/parsers/base.py) (~L776–871)
emits column lineage from INSERT+CTE patterns. When the terminal CTE body is an `exp.Union`,
something in the block silently fails to emit INSERT-target edges.

> **BLOCKER NOTE (plan-reviewer):** The `#25` block at L776–871 operates on
> `col_expressions` drawn from the outer SELECT body (e.g. `SELECT c1, c2 FROM cte_final`)
> — not from the CTE body itself. The `body` variable at L780 is the outer INSERT's SELECT,
> which is a plain `exp.Select`, not a Union. The Union lives inside the CTE definition
> (`cte_final AS (SELECT … UNION SELECT …)`). The fix direction — a Union walk — is correct
> in principle, but the **target variable** must be identified during diagnosis (Step 2.1),
> not assumed from the Design section. The pseudo-code below is intentionally omitted to
> avoid misdirecting the developer. Step 2.1 must pinpoint the exact line and variable
> before any code is written.

Directional guidance (to be verified in Step 2.1):
- The UNION structure is inside the CTE body, not the outer INSERT SELECT.
- The fix likely involves resolving the CTE body to its left-most `exp.Select` when the
  block looks up the terminal CTE's column names or projection count:
  `while isinstance(cte_body, exp.Union): cte_body = cte_body.this`
- Column names in a UNION are consistent across branches (SQL semantics); the left branch
  is authoritative for name extraction.
- No new `qualify`/`build_scope`/`exp.expand`/`sg_lineage` calls — purely an AST walk on
  the already-parsed CTE body.
- O(UNION depth) — constant for any realistic CTE.

**Perf invariant check:** The walk is pure Python AST traversal. It must run once per
INSERT-with-CTE statement, not once per column. Add a behavioural assertion in the unit
test confirming this.

---

## Implementation Steps

> Implement in order **P1a → P1b** (diagnosis first for both). Version bump last.

### Phase 1: P1a — Diagnose and fix CTAS kind misclassification

**Step 1.1** — Diagnose (required before coding).
- Pick `da.htdyn_vendvendor` or another Sub-problem A table.
- Find its defining file via `DEFINED_IN` join (or grep the DWH repo for the CREATE statement).
- Parse the defining file in isolation and inspect `parsed.defined_tables`.
- Determine which candidate (parser or indexer merge guard) is the root cause.
- **If both:** fix both. They are independent one-line changes.

**Step 1.2** — Fix.
- **Parser path:** [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) `_parse_statement`:
  extend or verify the `exp.Create` matching to cover TRANSIENT/TEMPORARY/IF NOT EXISTS variants.
- **Indexer path:** [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `upsert_nodes_bulk` or
  the INSERT-target path: add a `kind NOT IN ('table','view')` guard so a `kind='derived'` row
  cannot overwrite an existing `kind='table'` row in the database.

**Step 1.3** — Acceptance.
- New integration test: `test_P1a_ctas_target_kind_is_table_not_derived`.
  Fixture: a minimal `CREATE TABLE da.htdyn_vendvendor AS SELECT x FROM s` followed by
  `INSERT INTO da.htdyn_vendvendor SELECT x FROM t`. After indexing both, assert
  `SqlTable.kind == 'table'` for `da.htdyn_vendvendor`, and `HAS_COLUMN` rows exist
  (from P4 catalog). Assert the COLUMN_LINEAGE edge to `da.htdyn_vendvendor.x` is counted
  in `good_edges` by the coverage metric.
- Re-index DWH. Verify Sub-problem A table count drops from 55 toward 0.
- **Remove `xfail` markers** from `test_P1_cte_destination_does_not_leak_to_cte_node` and
  `test_P1_cte_chain_final_cte_does_not_leak` in
  [`tests/integration/test_column_coverage_patterns.py`](../../tests/integration/test_column_coverage_patterns.py)
  for any pattern that P1a now fixes. If P1b is required to fix those tests, defer xfail
  removal to Step 2.2. Leaving `strict=True` xfail on a now-passing test causes CI failure.

### Phase 2: P1b — Diagnose and fix positional block UNION terminal

**Step 2.1** — Diagnose (required before coding).
- Find a sample DWH file with `cte_insert` as a terminal CTE whose body is a UNION.
- Add a minimal reproduction fixture (2-branch UNION inside the terminal CTE).
- Run the parser and inspect `column_lineage` edges. Confirm only CTE-destination edges are
  emitted, not INSERT-target edges.
- Identify the exact line in the `#25` block where the UNION body causes the failure
  (typically an `isinstance(body, exp.Select)` guard that returns early).

**Step 2.2** — Fix.
- [`base.py`](../../src/sqlcg/parsers/base.py) positional block: add the UNION-walk before
  column extraction (see Design above). The walk must be performed **once** before the column
  loop, not per column.

**Step 2.3** — Acceptance.
- New integration test: `test_P1b_insert_cte_with_union_terminal_reaches_real_target`.
  Fixture: `INSERT INTO ba.fact (c1, c2) WITH cte_data AS (…), cte_final AS (SELECT … UNION
  SELECT …) SELECT c1, c2 FROM cte_final`. Assert COLUMN_LINEAGE edges land on `ba.fact.c1`
  and `ba.fact.c2`, not only on `cte_final.*`.
- **Stop condition:** if the fix causes `test_union_cte_star_recall_guard` or
  `test_backfill_impact_consistency` to fail, revert the P1b change and document the
  constraint. Ship P1a only; open a follow-up issue for P1b.

### Phase 3: Version bump

**Step 3.1** — Bump version.
- If only P1a (kind fix): `1.7.0` → `1.7.1` (patch — bug fix in kind classification).
- If P1a + P1b: `1.7.0` → `1.7.1` (patch — both are bug fixes, no new capability).
- Files: [`pyproject.toml`](../../pyproject.toml) `version`,
  [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) `__version__`, then `uv lock`.

---

## Performance

No new per-column operations. P1a is a parser or upsert-guard change (once-per-statement
or once-per-upsert-batch); P1b is an AST walk in the positional block that runs once per
INSERT-with-CTE statement (same class as the existing `#25` body resolution). The four perf
guard files must pass after each phase.

### Invariants at risk per change

| Change | Invariant(s) at risk | Why it stays safe |
|--------|---------------------|-------------------|
| P1a parser fix | "body_scope built once per statement" | CTAS recognition change only touches `_parse_statement` `isinstance(stmt, exp.Create)` guard — no qualify/scope call added |
| P1a indexer kind-guard fix | `_upsert_parsed_file` uses `upsert_nodes_bulk` | The guard is a SQL-level WHERE condition in the upsert, not a per-row Python loop — `execute()` call count unchanged |
| P1b UNION walk | "body_scope built once per statement"; perf scaling | The walk is pure Python AST traversal (`while isinstance(body, exp.Union): body = body.this`), not a qualify/expand call. It runs once per INSERT-with-CTE, before the column loop. The scaling guard cannot flag it directly, so add a behavioural assertion: the walk executes once per INSERT statement, not once per column. |

---

## Test Strategy

### New tests

| Test | Location | Pins |
|------|----------|------|
| `test_P1a_ctas_target_kind_is_table_not_derived` | `tests/integration/test_column_coverage_patterns.py` | After indexing CTAS-then-INSERT, `SqlTable.kind='table'`. **Passes today** — regression guard (Candidate 1 ruled out; real DWH failure is cross-batch, not reproducible in minimal fixture). |
| `test_P1a_kind_guard_prevents_derived_overwrite` | `tests/integration/test_column_coverage_patterns.py` | CTAS+INSERT same run: kind='table' and HAS_COLUMN wired. **Passes today** — regression guard for Candidate 2 in-batch case. |
| `test_P1b_insert_cte_with_union_terminal_reaches_real_target` | `tests/integration/test_column_coverage_patterns.py` | INSERT+CTE+UNION terminal emits real-target edges, not CTE-only |
| `test_P1b_union_walk_happens_once_per_statement` | `tests/unit/test_perf_scaling_guard.py` (or new `test_T_p1b_union_walk.py`) | UNION walk is per-statement, not per-column |

### Regression guard

Run after each phase:
```
uv run pytest tests/unit/test_perf_scaling_guard.py \
              tests/unit/test_T09_01_qualify_once.py \
              tests/unit/test_bulk_upsert_invariant.py \
              tests/unit/test_upsert_batch_invariant.py -x
```

Also run:
```
uv run pytest tests/integration/test_union_cte_star_recall_guard.py \
              tests/integration/test_backfill_impact_consistency.py \
              tests/integration/test_analyze_case_fold.py -x
```

Full gate: `uv run pytest`, `uv run pyright`, `uv run ruff check src tests` — all green.

### DWH sanity (manual, post-merge)

Re-index the DWH corpus. Run `uv run sqlcg gain` / `db info`. Assert:
- Sub-problem A: tables with `kind='derived'` that should be `kind='table'` — count drops from
  55 toward 0.
- Edge health rises from ≈45% (post P3+P4 estimate) to ≥ 50% (sprint gate).
- Record before/after numbers in the postmortem.

---

## Acceptance Criteria

- [ ] P1a: all 55 CTAS misclassified tables have `kind='table'` after re-index (or the count
      drops as far as the fix covers; document any residual).
- [ ] P1a: `HAS_COLUMN` rows exist for those tables (P4 catalog wires correctly); their
      COLUMN_LINEAGE edges are counted in `good_edges`.
- [ ] P1a: `test_P1a_ctas_target_kind_is_table_not_derived` and
      `test_P1a_kind_guard_prevents_derived_overwrite` both pass.
- [ ] P1b: the minimal INSERT+CTE+UNION fixture emits COLUMN_LINEAGE edges to the real INSERT
      target (or, if P1b is deferred, the constraint is documented with a filed follow-up and
      Sub-problem B's edge count is noted in the postmortem).
- [ ] P1b (if shipped): `test_P1b_insert_cte_with_union_terminal_reaches_real_target` passes;
      `test_union_cte_star_recall_guard` and `test_backfill_impact_consistency` stay green.
- [ ] **No change to `_Q_EDGE_HEALTH` or `_Q_BLINDSPOT` in
      [`coverage.py`](../../src/sqlcg/cli/coverage.py).** The metric formula is unchanged;
      the `kind NOT IN ('cte','derived')` exclusion is NOT added.
- [ ] All four perf-guard test files green after every phase.
- [ ] DWH edge health measured and recorded via `sqlcg gain`/`db info` after re-index.
      Sprint gate: ≥ 50% (P1a only) or ≥ 59% (P1a + P1b complete). The ≥ 95% milestone
      remains deferred — not claimed by this sprint.
- [ ] Version bumped to `1.7.1` across `pyproject.toml`, `__init__.py`, lockfile; `sqlcg
      --version` prints `1.7.1`.
- [ ] Full gate (pytest / pyright / ruff) green.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| P1a diagnosis reveals the root cause is in the upsert cross-batch merge (harder to fix safely) | The `_structural_kinds` merge is already tested in `test_bulk_upsert_invariant.py` — extend that test with the cross-batch scenario before coding. If the fix requires a schema-level `INSERT OR IGNORE`/`UPDATE WHERE` pattern in DuckDB, prototype it in a scratch query against the live graph first. |
| P1b UNION fix changes the positional block in a way that breaks star-recall or backfill tests | Step 2.3 stop condition: run those tests immediately after the P1b change. If red, revert and file a follow-up. P1a alone ships. |
| P1b fix only covers 2-branch UNION; DWH has 3+ branch UNION terminals | The `while isinstance(body, exp.Union): body = body.this` walk handles arbitrary depth. Add a 3-branch fixture to confirm. |
| Re-index finds fewer than 55 tables fixed (some have a different root cause) | Document the residual in the postmortem. The sprint gate is "Sub-problem A count drops measurably and edge health ≥ 50%", not "exactly 55 tables fixed". |
| P5 (Sub-problem C, 3,016 edges) is tempting to fold in — resist | P5 requires schema-context resolution and is a larger change. Per the "separate PRs" rule in memory, it ships independently. The 149 bare-name tables remain blindspots until P5 lands. |
| The `coverage_p1_p3_p4.md` plan still says to add `kind NOT IN ('cte','derived')` to the metric | That plan is superseded on P1 by this one. If a developer reads both plans: this plan takes precedence. **Do not implement the metric exclusion from `coverage_p1_p3_p4.md`'s Step 3.1/3.2.** |

---

## Supersession Note

The P1 section of [`coverage_p1_p3_p4.md`](coverage_p1_p3_p4.md) (Steps 3.1–3.3: add
`kind NOT IN ('cte','derived')` to `_Q_EDGE_HEALTH` and `_Q_BLINDSPOT`) is **superseded by
this plan**. The DWH measurement (2026-06-09) showed that 2,479 of the CTE/derived-dst edges
point to real tables misclassified as `kind='derived'` — excluding them from the metric would
hide a genuine data defect. Fix the data, not the ruler.

P3 and P4 from `coverage_p1_p3_p4.md` shipped in v1.7.0 and are complete. This plan covers
the remaining work.

## Rollout / Rollback

- **Rollout**: ships in `1.7.1`. Re-index is the migration path — existing graphs gain correct
  `kind` values only after `sqlcg index` re-runs. No backward-compat shims.
- **Rollback**: revert the PR; no schema or data state to unwind. A graph indexed under
  `1.7.1` is readable by `1.7.0` tooling (same `SqlTable`/`HAS_COLUMN` shapes).
- **Milestone gate**: this sprint ships when DWH edge health is measured at ≥ 50% after
  P1a lands. The ≥ 95% coverage milestone is a separate, later gate.
