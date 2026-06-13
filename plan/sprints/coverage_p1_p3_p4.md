# Feature Plan: Coverage fixes P1 / P3 / P4 — push edge health 41% → ~60% (95% deferred, see BQ-2)

## Summary

Push the DWH corpus (1,342 files) toward **≥95% edge health** by addressing the three
coverage gaps catalogued in
[`column_coverage_findings.md`](../reports/column_coverage_findings.md):

- **P1 — CTE/derived destination "leak" (15,102 edges)** is **NOT a parser bug** (revised
  2026-06-08 after the escalation below). The CTE-projection edges are *structural graph
  edges* required for multi-hop upstream traversal — suppressing them breaks
  `test_analyze_case_fold`. P1 is therefore a **metric fix**: exclude `kind IN ('cte',
  'derived')` destinations from the edge-health numerator AND denominator, because CTE/
  derived nodes are never persistent schema and can never carry `HAS_COLUMN`. No parser
  change.
- **P3 — quoted view names with spaces** drop the schema from the column catalog (6,045
  edges, 232 tables). Parser/indexer change.
- **P4 — CTAS tables** emit zero `SqlColumn` rows (241 tables). Parser change.

The TDD acceptance tests already exist in
[`test_column_coverage_patterns.py`](../../tests/integration/test_column_coverage_patterns.py);
the P1 tests are **relaxed** (see below) to match the corrected mental model.

This is **new parser capability + a metric correction** → **minor** version bump
`1.6.1` → `1.7.0`.

---

## Scope

### In Scope

- **P1 — CTE/derived destination "leak" → metric fix (revised).** When an
  `INSERT INTO t (col_list) WITH cte … SELECT … FROM cte` resolves its authoritative target
  columns through the existing `#25` positional block, the lineage edges **already** land on
  the INSERT target `t` (verified — see *P1 verification* below). The CTE-projection block
  *additionally* emits the chain edges (`ba.src.x → base.col_a`, `base.col_a → ba.fact.col_a`)
  that multi-hop upstream traversal walks. These CTE-destination edges are **structural, not
  coverage gaps** — a `kind='cte'`/`kind='derived'` node is never persistent schema and can
  never carry `HAS_COLUMN`. **Fix = scope the `edge_health` metric to real-table destinations
  only**, excluding `kind IN ('cte','derived')` dst tables from both numerator and denominator
  in [`coverage.py`](../../src/sqlcg/cli/coverage.py). **No parser change.** The P1 acceptance
  tests are relaxed to drop the (incorrect) "no `cte_*`/`base.*` dst" assertion and keep only
  the "real target exists" assertion.
- **P3 — Space-name schema strip.** `CREATE OR REPLACE VIEW ia_semantic."ODS Workaround
  Artikel" AS SELECT …` must emit a column catalog (`SqlColumn` + `HAS_COLUMN`) keyed by
  the **full schema-qualified** table identity, so `HAS_COLUMN.src_key` (= `table.full_id`)
  matches and the view stops being a blindspot. The `SqlColumn.table_name` field must
  retain the schema for quoted identifiers containing spaces.
- **P4 — CTAS column discovery.** `CREATE TABLE t AS SELECT a AS x, b FROM s` must derive
  its output column catalog from the outer SELECT projection list (alias if present, else
  the bare column name) and emit `SqlColumn` + `HAS_COLUMN`. `SELECT *` CTAS degrades
  gracefully — `SqlTable` only, no `SqlColumn`, no crash.
- Version bump `1.6.1` → `1.7.0` + `uv lock`.

### Non-Goals

- **No P2 change.** Positional-INSERT phantom edges are already covered (CLONE Part C);
  the two P2 acceptance tests already pass. Do not touch the `inferred_from_source_name`
  degrade path.
- **No P5 (USE SCHEMA) / P6 (alias verification beyond the existing passing tests).**
  Explicitly deferred in the findings report — they need schema-context resolution.
- **No perf-invariant relaxation.** Every invariant in CLAUDE.md's `base.py` / `indexer.py`
  table stays intact. No new per-column `qualify` / `build_scope` / `exp.expand` /
  `sg_lineage` call may be introduced. See the **Performance** section.
- **No schema-version bump, no new graph column.** P3/P4 reuse the existing
  `SqlColumn` / `HAS_COLUMN` rows and the existing `defined_columns` emission seam.
- **No `MERGE` lineage, no `SELECT *` static expansion.**

### Blocking Questions

> **BQ-1 — `SqlColumn.table_name` semantics for quoted-space view names (P3).**
> Today every `SqlColumn.table_name` is the **bare** `TableRef.name` (schema dropped),
> by convention shared with the DDL-column path. The P3 acceptance test
> (`test_P3_quoted_view_with_spaces_preserves_schema_in_sqlcolumn`, lines 336–344)
> asserts `table_name` **contains the schema** (`ia_semantic.…`) for the space-name view.
> **Decision taken (proceed):** the graph join that matters — `HAS_COLUMN.src_key` =
> `SqlTable.qualified` = `SqlColumn.table_qualified` — already uses `full_id` (schema
> included); `table_name` is informational. The fix qualifies `table_name` **only when the
> bare name contains a space** (the quoted-identifier case), leaving the overwhelming
> majority of bare names unchanged. This is scoped, reversible, and satisfies the test
> without altering the join key. If the architect-reviewer prefers `table_name` stay bare
> everywhere and the test be relaxed to assert on `table_qualified` instead, raise it
> before implementation — but the test as written is the contract, so this plan honours it.
> **Non-blocking for the developer** unless the reviewer overrides.

---

## Design

### Where each fix lands

| Pattern | File | Function | Mechanism |
|---------|------|----------|-----------|
| P1 | [`coverage.py`](../../src/sqlcg/cli/coverage.py) | `_Q_EDGE_HEALTH` query | Scope edge health to `kind NOT IN ('cte','derived')` dst tables — **no parser change** |
| P3 | [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) | `_parse_statement` / new `_extract_select_output_columns` | Give CREATE VIEW-AS-SELECT a `defined_columns` list |
| P3 | [`indexer.py`](../../src/sqlcg/indexer/indexer.py) | `_build_file_rows` DDL-column loop (~L1138–1152) | Qualify `table_name` when the bare name contains a space |
| P4 | [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) | `_parse_statement` / new `_extract_select_output_columns` | Give CTAS a `defined_columns` list from the outer SELECT projection |

P3 and P4 share the **same** new helper and the **same** existing emission seam: once a
CREATE VIEW/TABLE-AS-SELECT statement carries `defined_columns`, the indexer's existing
DDL-column loop (lines 1138–1153) already emits `SqlColumn` rows keyed by `table.full_id`
and `HAS_COLUMN` edges `{src_key: table.full_id, dst_key: col_id, source: "ddl"}`. No new
emission code is needed for the catalog — only the column-name derivation.

### P1 — CTE/derived "leak" is a metric scope, not a parser bug (REVISED 2026-06-08)

> **This section replaces the original parser-suppression design, which was wrong.**
> The escalation at the bottom of this file documents what broke and why. The corrected
> mental model is captured in `ARCHITECTURE_REVIEW.md`.

**Correct mental model.** For `INSERT INTO t (c1,c2) WITH cte … SELECT … FROM cte`, two
*different* edge families are emitted, both structurally correct:

1. **CTE chain edges** — the CTE node is the `dst`: `ba.src.x → base.col_a`. Emitted by the
   CTE-projection block ([`base.py`](../../src/sqlcg/parsers/base.py) L1135–1292).
2. **Positional block edges** — the CTE node is the `src`, the real INSERT target is the
   `dst`: `base.col_a → ba.fact.col_a`. Emitted by the `#25` positional block
   ([`base.py`](../../src/sqlcg/parsers/base.py) L776–871).

Multi-hop upstream traversal walks **both** to chain
`ba.fact.col_a ← base.col_a ← ba.src.x`. Suppressing family (1) — the original plan's
approach — broke `test_analyze_case_fold` because the chain dead-ended at the CTE node with
no path back to the real source. **Family (1) must be preserved.**

**Why it looked like a "leak".** A `kind='cte'`/`kind='derived'` dst node is never persistent
schema, so it can never have a `HAS_COLUMN` row. The edge-health metric
([`coverage.py`](../../src/sqlcg/cli/coverage.py) `_Q_EDGE_HEALTH`) counts *every*
`COLUMN_LINEAGE` edge in its denominator and marks an edge "good" only when its stripped dst
table has a `HAS_COLUMN` entry. So all 15,102 CTE/derived-dst edges are counted as "bad" —
making ≥95% **mathematically impossible** even when every edge is correct (29% of edges
legitimately point at CTE/derived intermediates).

**Fix — scope the metric to real-table destinations.** Add to the `_Q_EDGE_HEALTH` query an
`EXISTS` clause so CTE/derived-dst edges are excluded from **both** numerator and denominator:

```sql
SELECT SUM(CASE WHEN EXISTS (
           SELECT 1 FROM "HAS_COLUMN" hc
           WHERE hc.src_key = <dst_table_strip>
       ) THEN 1 ELSE 0 END) AS good_edges,
       COUNT(*) AS total_edges
FROM "COLUMN_LINEAGE" cl
WHERE EXISTS (
    SELECT 1 FROM "SqlTable" t
    WHERE t.qualified = <dst_table_strip>
      AND t.kind NOT IN ('cte', 'derived')
)
```

where `<dst_table_strip>` is the existing `_DST_TABLE` expression
(`left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))`). The `WHERE` filter
applies to both `SUM(...)` and `COUNT(*)` because it scopes the whole row set — so a CTE/
derived-dst edge contributes to neither.

- **Verified** (probe, 2026-06-08): for the P1 chain fixture the CTE nodes
  `cte_base`/`cte_insert` are materialised as `SqlTable` rows with `kind='cte'`. The
  `_DST_TABLE` strip of `cte_insert.dn_datum` is exactly `cte_insert`, which matches
  `SqlTable.qualified`. The join key alignment holds.
- The `blindspot` query (`_Q_BLINDSPOT`) **also** counts CTE/derived dst tables today (1,681
  blindspot tables includes ~642 cte/derived tables). Apply the **same** `kind NOT IN
  ('cte','derived')` scoping to `_Q_BLINDSPOT` so the blindspot count reflects only
  *real-table* coverage gaps the developer can actually fix. (`catalog_pct` and `phantom_pct`
  are left unchanged — they are table-catalog and inferred-flag metrics, not dst-resolution
  metrics.)

> **No parser change for P1.** The CTE-projection and positional blocks both stay exactly as
> they are. P1 reduces to a `coverage.py` SQL edit + a test-assertion relaxation.

### P3 / P4 — output-column derivation (new helper)

Add to [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py):

```python
@staticmethod
def _extract_select_output_columns(select_body: exp.Select) -> list[str]:
    """Derive output column names from a SELECT projection list.

    For each projection: alias if present, else the bare column name. Star
    projections and unnamed/literal expressions yield nothing (graceful degrade).
    Names are lowercased + quote-stripped, identical to _extract_defined_columns /
    ColumnRef.__post_init__, so the catalog shares node identity with lineage cols.
    """
```

Naming rule (matches the existing CTE-projection logic at `base.py` L1205–1213 and the P4
tests):
- `a AS x` → `x`; `b` (plain `exp.Column`) → `b`; `d.dn_datum` → `dn_datum`.
- `*` / `t.*` (`exp.Star`) → skip (no static name; SELECT-* CTAS degrades to table-only).
- literal / unnamed function with no alias → skip.

Wire-up in `_parse_statement` (after the existing `_extract_defined_columns` block,
L362–374). The statement already computes `defined_body` (the outer SELECT) for CTAS at
L367–374; reuse the **same** body resolution for both VIEW and TABLE:

```python
if isinstance(stmt, exp.Create) and stmt.kind in ("TABLE", "VIEW") and not defined_columns:
    body = stmt.expression
    if isinstance(body, exp.Subquery):
        body = body.this
    if isinstance(body, exp.Select):
        defined_columns = self._extract_select_output_columns(body)
```

- `not defined_columns` guard: an explicit-column-list CREATE TABLE keeps its existing
  authoritative catalog; only AS-SELECT creates (which have an empty `defined_columns`
  today) gain the derived catalog. CLONE creates have no SELECT body → unchanged.
- The `SnowflakeParser` subclasses `AnsiParser` and inherits `_parse_statement` unchanged —
  the helper is available to both dialects with no Snowflake-specific code.

The new `defined_columns` flows into the **existing** aggregator path: `register_pass1`
(L91–103) already indexes `defined_columns` into `ddl_columns_by_bare` for any CREATE
TABLE/VIEW, and `_build_file_rows` (L1133–1153) already emits `SqlColumn` + `HAS_COLUMN`
for any `defined_table` whose QueryNode has `defined_columns`. No aggregator or emission
change is required for the catalog itself.

### P3 — schema-qualified `table_name` for space names (indexer)

In [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `_build_file_rows`, the DDL-column
loop (L1138–1149) currently sets `"table_name": table.name` (bare). Change to:

```python
tname = table.full_id if " " in table.name else table.name
```

(applied to the `column_rows` entry's `table_name` only; `id`, `table_qualified`, and the
`HAS_COLUMN` keys already use `table.full_id` and are unchanged).

- Rationale: a quoted identifier containing a space is the only case where the bare name is
  insufficient/ambiguous; for all normal names `table_name` stays bare (zero behavioural
  change to the 99%+ of rows). The graph join key (`table_qualified` / `HAS_COLUMN.src_key`)
  is `full_id` regardless, so this only affects the informational `table_name` column the P3
  test reads.

### Data Models

No new dataclass, no graph-schema change, no schema-version bump. `QueryNode.defined_columns`
(already present) is now populated for VIEW-AS-SELECT and CTAS. `SqlColumn` /`HAS_COLUMN`
rows reuse existing shapes.

### Dependencies

- sqlglot AST only (`exp.Select`, `exp.Subquery`, `exp.Star`, `exp.Column`, `exp.Alias`,
  `exp.Table`) — already imported.
- Existing aggregator (`ddl_columns_by_bare`), existing indexer emission seam. No new
  third-party dependency.

---

## Implementation Steps

> Implement in order **P4 → P3 → P1**: P4/P3 share the helper and are low-risk additive
> catalog emission; P1 is now a pure metric/test change (no parser diff) and is verified
> last so the new catalogs from P3/P4 are already present when the scoped edge-health metric
> is re-measured.

### Phase 1: P4 — CTAS column discovery

**Step 1.1** — Add `_extract_select_output_columns` to `AnsiParser`.
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py).
- Function: new `@staticmethod _extract_select_output_columns(select_body) -> list[str]`,
  placed next to `_extract_defined_columns`. Alias-or-name rule above; skip star/literal;
  lowercase + strip quotes.
- Acceptance: unit test on the helper alone — `SELECT x AS col_a, y AS col_b, z AS col_c`
  → `["col_a","col_b","col_c"]`; `SELECT d.dn_datum, s.col_x` → `["dn_datum","col_x"]`;
  `SELECT *` → `[]`; `SELECT 1 AS lit, COUNT(*) FROM s` → `["lit"]` (count skipped, no alias).

**Step 1.2** — Populate `defined_columns` for CTAS in `_parse_statement`.
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) `_parse_statement`
  (after L374, the `defined_body` block).
- Add the `kind in ("TABLE","VIEW") and not defined_columns` block from the Design.
- Acceptance: `test_P4_ctas_columns_derived_from_select_body` and
  `test_P4_ctas_with_cte_body_columns_derived` pass — `SqlColumn` rows
  `{col_a,col_b,col_c}` / `{dn_datum,col_x}` for `ba.derived_fact` / `ba.tmp_base`, and
  `HAS_COLUMN` rows wired with `src_key='ba.derived_fact'`. `test_P4_ctas_star_select_gracefully_degrades`
  still passes (no `SqlColumn`, `SqlTable` present, no crash).

### Phase 2: P3 — quoted-space view catalog

**Step 2.1** — VIEW-AS-SELECT gains `defined_columns` (covered by Step 1.2's
`("TABLE","VIEW")` condition — verify VIEW path explicitly).
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) (same block as 1.2).
- Acceptance: a CREATE VIEW-AS-SELECT now produces a non-empty `QueryNode.defined_columns`;
  the view becomes a `defined_table` with a catalog (HAS_COLUMN emitted by the existing seam).

**Step 2.2** — Schema-qualified `table_name` for space-containing names.
- Files: [`indexer.py`](../../src/sqlcg/indexer/indexer.py) `_build_file_rows` DDL-column
  loop (~L1147): `"table_name": table.full_id if " " in table.name else table.name`.
- Acceptance: `test_P3_quoted_view_with_spaces_preserves_schema_in_sqlcolumn` passes —
  `SqlColumn.table_name` for the view contains `ia_semantic`, and `HAS_COLUMN` rows exist.
  `test_P3_quoted_view_lineage_reaches_source_via_has_column` passes — `SqlColumn`,
  `COLUMN_LINEAGE` (dst on the qualified view name), and `HAS_COLUMN` all agree on the
  same qualified key.
- Verify no other `SqlColumn` emission site in [`indexer.py`](../../src/sqlcg/indexer/indexer.py)
  regresses (e.g. the COLUMN_LINEAGE src/dst `table_name` fields set elsewhere in
  `_build_file_rows`, and the CLONE path). Those fields use `table.name` (bare) and are
  unrelated to the DDL-column loop being changed here; apply the same `" " in name` qualify
  there **only if** an existing P3 test fails after Step 2.2 — default is to change the
  DDL-column loop only (L1147, the path the P3 catalog now flows through).
  **Note:** earlier draft references to L1203/1231/L1376 referred to `base.py` line numbers,
  not `indexer.py` — do not search `base.py` for this change.

### Phase 3: P1 — edge-health metric scope (no parser change)

**Step 3.1** — Scope `_Q_EDGE_HEALTH` to real-table destinations.
- Files: [`coverage.py`](../../src/sqlcg/cli/coverage.py) `_Q_EDGE_HEALTH`.
- Add the `WHERE EXISTS (SELECT 1 FROM "SqlTable" t WHERE t.qualified = <_DST_TABLE strip>
  AND t.kind NOT IN ('cte','derived'))` clause shown in the Design. Use the existing
  `_DST_TABLE`-style strip already used inline in the query (do **not** introduce a regex).
  The filter scopes the whole `FROM "COLUMN_LINEAGE" cl` row set, so CTE/derived-dst edges
  drop out of **both** `good_edges` and `total_edges`.
- **No `base.py` / `indexer.py` change.** The CTE-projection and `#25` positional blocks are
  untouched — the chain edges required for upstream traversal stay exactly as today.

**Step 3.2** — Scope `_Q_BLINDSPOT` the same way.
- Files: [`coverage.py`](../../src/sqlcg/cli/coverage.py) `_Q_BLINDSPOT`.
- Add `AND EXISTS (SELECT 1 FROM "SqlTable" t WHERE t.qualified = <strip> AND t.kind NOT IN
  ('cte','derived'))` so the blindspot count reflects only real-table coverage gaps. Leave
  `_Q_CATALOG_COVERAGE` and `_Q_PHANTOM_RATE` unchanged.

**Step 3.3** — Relax the P1 acceptance tests (assertion change only, no fixture change).
- Files: [`test_column_coverage_patterns.py`](../../tests/integration/test_column_coverage_patterns.py).
- `test_P1_cte_destination_does_not_leak_to_cte_node`: **remove** the
  `@pytest.mark.xfail` marker and the negative assertion
  `assert not any(k.startswith("base.") …)`. Keep
  `assert any(k.startswith("ba.fact.") …)` — the real target must exist. Rename/retitle the
  docstring to reflect "real target edge exists alongside the CTE chain edges (which are
  required for upstream traversal)".
- `test_P1_cte_chain_final_cte_does_not_leak`: **remove** the `@pytest.mark.xfail` marker and
  the `assert not cte_leaks` block. Keep `assert any(k.startswith("ba.kpi_fact.") …)`.
- `test_P1_derived_subquery_does_not_leak`: **unchanged** — it already asserts only that
  *all dst land on `ba.fact.*`* for the queried slice (`WHERE dst_key LIKE 'ba.fact.%'`) and
  a source edge exists; it never asserts the absence of the derived chain edge. It passes
  today and stays green.
- **Verified today (2026-06-08 probe, no code change):** for fixture 1 the edges are
  `ba.src.x → base.col_a`, `base.col_a → ba.fact.col_a` (+ `col_b`); for fixture 2 the full
  chain `ba.src.dn_datum → cte_base.dn_datum → cte_insert.dn_datum → ba.kpi_fact.dn_datum`.
  The real targets (`ba.fact.*`, `ba.kpi_fact.*`) **already exist**, so the relaxed
  assertions **pass on today's code with no parser change**. Removing `xfail(strict=True)` is
  mandatory because the relaxed tests now pass (a strict-xfail that passes is itself a red).
- A new positive assertion may be added documenting the *intended* shape (the CTE chain edge
  exists), but it is optional — the contract is "real target reachable", which the kept
  assertion already pins.

> **Acceptance for Phase 3:** the metric, re-run against the DWH, no longer counts CTE/
> derived edges as bad; the three P1 tests are green; no `base.py`/`indexer.py` diff exists in
> the P1 changeset.

### Phase 4: Version bump

**Step 4.1** — Bump `1.6.1` → `1.7.0` (minor — new parser capability).
- Files: [`pyproject.toml`](../../pyproject.toml) `version`,
  [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) `__version__`, then `uv lock`.
- Acceptance: all three agree on `1.7.0`; `uv run sqlcg --version` prints `1.7.0`.

---

## Performance

Indexing budget: **≤180s at 1,342 files** (baseline ~117s from
[`sprint_08_fullcorpus_index.json`](../metrics/sprint_08_fullcorpus_index.json)). None of
the three changes add per-column work; all are once-per-statement structural reads or
once-per-row string checks.

### Invariants at risk per change

| Change | Invariant(s) at risk | Why it stays safe |
|--------|---------------------|-------------------|
| P4/P3 `_extract_select_output_columns` | "body_scope built once per statement"; "exp.expand only file-level sources" | The helper walks `select_body.expressions` (a Python list iteration over the already-parsed projection). It does **not** call `qualify`, `build_scope`, `exp.expand`, or `sg_lineage`. O(N_projections) once per CREATE statement — same class as the existing `_extract_defined_columns` `find_all(exp.ColumnDef)` walk. |
| P4/P3 `_parse_statement` wire-up | none in the hot path | Runs once per CREATE statement, after lineage extraction; CTAS/VIEW are a small fraction of statements. No effect on INSERT/SELECT statement cost. |
| P3 `indexer.py` `table_name` | none (indexer emission, not parser hot path) | A single `" " in table.name` substring check per DDL-column row in `_build_file_rows`. `_build_file_rows` is the bulk-row builder, not a `qualify`/`sg_lineage` site. Does not touch `_flush_row_batch` / bulk-upsert batching invariants. |
| P1 metric scope | none (no parser change) | P1 is a `coverage.py` SQL edit + test-assertion relaxation. The `base.py` hot path is **not touched**. The added `EXISTS` clause is read-only at `db info`/`gain` time on the already-built graph — not in the indexing hot path. |

### How the developer verifies the guard stays green after each step

1. After **each** phase: `uv run pytest tests/unit/test_perf_scaling_guard.py
   tests/unit/test_T09_01_qualify_once.py tests/unit/test_bulk_upsert_invariant.py
   tests/unit/test_upsert_batch_invariant.py -x`. A red here means an op started scaling —
   stop and find what, do not raise the slack.
2. The scaling guard pins op-counts (qualify/build_scope/lineage call counts and the
   behavioural `copy=False` assertions) at N and 2N. None of these three changes adds such
   a call; the counts must stay flat. If P1's guard read is accidentally placed **inside**
   the per-CTE or per-column loop instead of once before it, the guard will not catch a
   constant-factor read — so the **plan-compliance / code review** must confirm the
   `_suppress_cte_dsts` boolean is computed **once before** the `for cte` loop (a behavioural,
   not volume, check — see CLAUDE.md's note that volume counts hide in too-simple fixtures).
3. Whole-corpus timing sanity (optional, when a DWH checkout is available): index the
   1,342-file corpus and confirm wall-clock ≤180s; record the figure in the postmortem.
   Do **not** treat a one-off >3min as an auto-regression without re-running (baseline noise
   per `project_indexer_perf_baseline` memory) — but a consistent >180s **is** a stop
   condition.

---

## Test Strategy

> Each test is described by the behaviour it verifies. The developer names tests
> `test_<unit>_<scenario>_<expected>` and links this plan in the docstring.

### Acceptance (already written — must all pass)

[`tests/integration/test_column_coverage_patterns.py`](../../tests/integration/test_column_coverage_patterns.py):
- **P1 (relaxed)** — INSERT-from-CTE / multi-CTE chain edges **reach** the real target
  (`ba.fact.*` / `ba.kpi_fact.*`); the `xfail` markers and the negative "no `cte_*`/`base.*`
  dst" assertions are removed (those were written on a wrong premise — the CTE chain edges are
  required for upstream traversal). All three P1 tests pass on today's code after the
  assertion relaxation; the derived-subquery test is unchanged.

**Metric regression — `_Q_EDGE_HEALTH` / `_Q_BLINDSPOT` scope (new coverage):**
[`tests/integration/test_coverage_metrics_integration.py`](../../tests/integration/test_coverage_metrics_integration.py)
- The existing fixture has only `kind='table'` tables, so its current assertions
  (`good_edges=3`, `total_edges=6`, `blindspot_tables=2`) **stay green** unchanged.
- **Add a fixture row** with `kind='cte'` (e.g. `mydb.sch.stg_cte`) plus a `COLUMN_LINEAGE`
  edge whose dst strips to that CTE table, and assert it is **excluded** from `total_edges`,
  `good_edges`, and `blindspot_tables`. This is the load-bearing proof that the scope filter
  drops CTE/derived edges from both numerator and denominator. Without it the metric change
  has no test pinning the new behaviour.
- **P3** — quoted-space view `SqlColumn.table_name` retains the schema; `HAS_COLUMN` wired;
  `SqlColumn`/`COLUMN_LINEAGE`/`HAS_COLUMN` agree on the qualified key (2 tests).
- **P4** — CTAS and CTAS-with-CTE produce the expected `SqlColumn` + `HAS_COLUMN`; SELECT-*
  CTAS degrades to table-only without crashing (3 tests, one already passing).

### Unit (new, on the helper)

`tests/unit/` — **`_extract_select_output_columns` derivation**: aliases win; bare columns
use their name; `t.col` uses `col`; star and unnamed-literal projections are skipped;
output is lowercased + quote-stripped. This pins the naming contract independently of the
full index path so a future refactor of the projection walk can't silently drop columns.

### Regression (existing — must stay green)

- **CTE-projection / cross-file chain** (`test_T07_*`, union-CTE star recall guard,
  `test_38*`, **`test_analyze_case_fold`**): unchanged by P1 — there is no parser diff. The
  CTE chain edges that multi-hop upstream traversal walks stay exactly as today.
  `test_analyze_case_fold::test_uppercase_upstream_anchor_returns_same_ids_as_lowercase` —
  which the original suppression approach broke — must stay green (it is the canary for the
  corrected mental model).
- **Positional INSERT / CLONE blindspot** (`test_F2_*`, clone-blindspot, `test_backfill_*`):
  the `#25` / Part-B / CLONE catalog paths are untouched; `inferred_from_source_name`
  semantics unchanged.
- **Perf guards** (the four files in the Performance section).
- **Full gate**: `uv run pytest`, `uv run pyright`, `uv run ruff check src tests` all green.

### Coverage-metric sanity (manual, post-merge on DWH)

Re-index the DWH corpus and run `uv run sqlcg gain` / `db info` (the dashboard shipped in
`gain_coverage_metrics`, now with the scoped `_Q_EDGE_HEALTH`/`_Q_BLINDSPOT`). Assert
**edge health rises from ≈41 % to ≈55–65 %** (the measured P3+P4 gain — see *95 % Feasibility
Analysis*) and the real-table blindspot count drops by ~473 (232 P3 + 241 P4). Record the
before/after numbers in the postmortem. The **95 % gate is deferred** to the BQ-2 follow-up
decision — do **not** treat a sub-95 % result here as a failure of this sprint.

---

## Acceptance Criteria

- [ ] All 10 tests in `test_column_coverage_patterns.py` pass (P3/P4 fixed; the 3 P1 tests
      pass after assertion relaxation with no parser change).
- [ ] `_extract_select_output_columns` exists on `AnsiParser`, is **called** from
      `_parse_statement` (grep-confirmed call site), and inherited unchanged by
      `SnowflakeParser`.
- [ ] CTAS and CREATE VIEW-AS-SELECT statements emit `SqlColumn` rows + `HAS_COLUMN` edges
      keyed on `table.full_id`; explicit-column-list CREATE TABLE catalogs are unchanged.
- [ ] `SELECT *` CTAS produces a `SqlTable` node and **no** `SqlColumn` rows, no exception.
- [ ] Quoted-space view `SqlColumn.table_name` contains the schema; bare-name tables keep
      a bare `table_name`.
- [ ] INSERT-from-CTE edges **reach** the INSERT target (`ba.fact.*` / `ba.kpi_fact.*`); the
      CTE chain edges are preserved (required for upstream traversal); `test_analyze_case_fold`
      stays green. **No `base.py` / `indexer.py` diff in the P1 changeset.**
- [ ] `_Q_EDGE_HEALTH` and `_Q_BLINDSPOT` exclude `kind IN ('cte','derived')` dst tables from
      both numerator and denominator; a `kind='cte'` fixture row proves the exclusion.
- [ ] All four perf-guard test files green after every phase; no new
      qualify/build_scope/expand/sg_lineage call introduced (P1 adds none — it is a metric
      query change only).
- [ ] Indexing 1,342 files ≤180s (record actual in postmortem).
- [ ] DWH edge health **measured and recorded** via `sqlcg gain`/`db info` after re-index;
      ≈55–65 % after P3+P4+metric-scope (≥55 % is the sprint gate). The ≥95 % milestone is
      deferred pending the **BQ-2** decision (re-baseline vs. follow-up sprint).
- [ ] Version `1.6.1` → `1.7.0` across `pyproject.toml`, `__init__.py`, lockfile;
      `sqlcg --version` prints `1.7.0`.
- [ ] Full gate (pytest / pyright / ruff) green.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Metric scope hides a *real* coverage gap by over-broad `kind` exclusion | Only `kind IN ('cte','derived')` is excluded — these are provably non-schema intermediates that can never carry `HAS_COLUMN`. `kind='table'`/`'view'` dst edges stay fully counted. The existing metric integration fixture (all `kind='table'`) is unchanged, proving real tables are not dropped. |
| Excluding CTE edges inflates edge health past a true picture | The exclusion is symmetric (numerator AND denominator) and removes only structurally-uncatalogable edges. The 95% target is re-derived against the scoped denominator in the *95% Feasibility Analysis* section — it is **not** a free pass; P3/P4 catalog gains still have to land. |
| P4 CTAS where the outer body is a UNION (no projections on `Union` node) | Mirror the existing CTE-projection handling (`base.py` L1165–1170): walk to the deepest left-branch `Select` for projection names. Add this to `_extract_select_output_columns` if a UNION-bodied CTAS fixture surfaces; otherwise UNION CTAS degrades to table-only (safe). |
| P3 `table_name` qualification breaks a query that filters on bare `table_name` | Only names **containing a space** are qualified; normal names unchanged. Join keys use `table_qualified`/`full_id`, not `table_name`. Full gate catches any consumer that filters on `table_name`. |
| New `defined_columns` on AS-SELECT shifts a table into the `ddl_columns_by_bare` ambiguity set, perturbing Part-B positional INSERT mapping | The aggregator already de-duplicates identical catalogs and excludes genuinely-ambiguous bare names (L98–103); a CTAS-derived catalog matching its own DDL is identical → no new ambiguity. Regression: clone-blindspot / backfill tests stay green. |
| Perf regression from a hot-path scaling flip | No new per-column op; all changes once-per-statement or once-per-row. Four perf-guard files run after each phase; whole-corpus timing ≤180s checked post-merge. |
| 95% numeric gate dropped (GitHub #124) | 95% gate removed — coverage is reported as an observed metric (no pass/fail target). Post-P1/P3/P4 baseline: ~55–65% scoped. Next improvement sprint: BQ-2. See GitHub #124. |

## 95% Feasibility Analysis (added 2026-06-08)

> Derived from [`column_coverage_findings.md`](../reports/column_coverage_findings.md) +
> the live-graph diagnostic. All figures are against the **scoped** edge-health denominator
> (CTE/derived dst edges excluded, per the revised P1 metric).

### Denominator after the metric scope

| Quantity | Value | Source |
|----------|-------|--------|
| Total `COLUMN_LINEAGE` edges | 51,434 | findings top-line |
| CTE/derived-dst edges (excluded) | 15,102 | P1: 6,944 cte + 8,158 derived |
| **Scoped denominator** | **36,332** | 51,434 − 15,102 |
| Current good edges (dst has HAS_COLUMN) | 14,884 | live diagnostic |
| **Current scoped edge health** | **≈ 41.0 %** | 14,884 / 36,332 |

The metric scope alone lifts the headline from ~29 % (old denominator) to ~41 % — it does
**not** manufacture the milestone; it removes only edges that can never be catalogued.

### Projected health after each fix

| Stage | Good edges | Scoped health | Notes |
|-------|-----------|---------------|-------|
| Today (metric scoped) | 14,884 | **41.0 %** | baseline |
| + P3 (232 view tables, **6,045** edges) | 20,929 | **57.6 %** | every P3 edge has a real-table dst already in the denominator → becomes good |
| + P4 (241 CTAS tables, edges **unknown**) | 20,929 + X | ? | edge count not measured in the findings — see below |
| Target for 95 % | 34,515 | 95.0 % | 0.95 × 36,332 |

**Gap to 95 % after P3 = 34,515 − 20,929 = 13,586 good edges still required.**

### Does P3 + P4 alone reach 95 %? — No (measured, not assumed)

P4's edge count was **not** captured in the findings (`unknown` in the P4 row of the
patterns-ranked table). Even an optimistic bound shows P3+P4 fall short:

- The 13,412 `inferred_from_source_name` (P2 phantom) edges are the bulk of the remaining
  real-table-dst-but-uncatalogued edges. The findings *assert* "P2 residual collapses once
  P3/P4 supply catalogs," but that is a projection, not a measurement. P4 only covers the
  **241 CTAS tables**; P2's phantom edges span **304 tables**, many of which are neither
  CTAS nor quoted-space views (temp tables, tables whose DDL the indexer never saw —
  findings §P2).
- **Real-table blindspot tables remaining**: 1,681 total blindspot tables − ~642 cte/derived
  (now excluded) = **~1,039 real-table blindspots**. P3 fixes 232, P4 fixes 241 →
  **~566 real-table blindspot tables remain uncatalogued** after this sprint.

**Conclusion: P3 + P4 + the metric fix land edge health at roughly 55–65 %, not 95 %.**
Reaching 95 % requires catalogs for the ~566 residual real-table blindspots — predominantly
the P2 cohort (temp tables, cross-file/unseen-DDL tables) and the deferred P5 (`USE SCHEMA`)
bare-name group (956 tables in `BARE_UNQUALIFIED`).

### Revised milestone recommendation (BLOCKING — needs architect-reviewer/user sign-off)

> **BQ-2 — the 95 % target is not reachable in this sprint.** Pick one:
>
> 1. **Re-baseline the milestone.** Ship P3 + P4 + the metric fix as a measured step
>    (≈41 % → ≈55–65 %), and set the 95 % gate against a **follow-up** sprint that tackles the
>    P2-residual + P5 cohort (temp/unseen-DDL catalogs, `USE SCHEMA` resolution). This is the
>    honest reading of the data. **Recommended.**
> 2. **Expand this sprint's scope** to include the P2-residual catalog path (derive catalogs
>    for temp/CTAS-chained/unseen-DDL tables) and P5 schema-context resolution. This is a much
>    larger change, crosses into the deferred Non-Goals, and risks the perf budget — it should
>    be its own plan, not folded here (per the "separate PRs" rule).
>
> Do **not** claim the 95 % milestone on the strength of P3/P4 alone — the diagnostic shows it
> is ~30 points short. The acceptance criterion "DWH edge health ≥ 95 %" below is therefore
> **re-scoped to "edge health measured and recorded; ≥ 55 % after the three fixes"** pending
> the BQ-2 decision.

---

## Escalation — P1 conflicts with multi-hop upstream traversal

**Date**: 2026-06-08  
**Status**: **RESOLVED 2026-06-08** — the planner redesigned P1 as a *metric scope*, not a
parser change (see *P1 — CTE/derived "leak" is a metric scope* in the Design section and the
*95 % Feasibility Analysis*). The escalation record below is kept for provenance: it documents
the wrong premise (CTE chain edges are leaks) and the canary test (`test_analyze_case_fold`)
that proves CTE chain edges are structurally required. **Resolution: alternative #2 below was
chosen — keep the CTE chain edges, relax the P1 tests, fix the metric.**

### What was tried

Implemented P1 suppression: computed `_suppress_cte_dsts = isinstance(stmt, exp.Insert) and
dst_table is not None and bool(positional_col_names)` once before the CTE-projection loop
and guarded the outer `if isinstance(body, exp.Select) and body.args.get("with_"):` block
with `if not _suppress_cte_dsts:`.

P1 acceptance tests passed (XPASS → need xfail removal). But `test_analyze_case_fold.py::
test_uppercase_upstream_anchor_returns_same_ids_as_lowercase` broke.

### Why the plan's approach breaks existing functionality

The CTE-projection block emits TWO kinds of edges for INSERT-with-CTE:

1. **CTE chain edges** (needed): `staging.src_raw.val → raw.m` — where the CTE node is
   the `dst`. These are required for multi-hop upstream traversal:
   `mart.fact_enriched.m ← raw.m ← staging.src_raw.val`.

2. **Leak edges** (the P1 problem): same edges viewed differently — the "leak" IS the chain
   edge. The P1 test asserts `raw.*` / `base.*` does NOT appear as a dst_key. But upstream
   traversal REQUIRES this edge to exist (following dst→src).

The plan's claim that "the authoritative `t.col` edges already carry the full
source→target lineage" is only correct for DIRECT (1-hop) lineage. The positional block
emits `raw.m → mart.fact_enriched.m`, but the upstream tool needs to continue from `raw.m`
back to `staging.src_raw.val`. That second hop requires the suppressed edge.

### Affected test

`tests/integration/test_analyze_case_fold.py::test_uppercase_upstream_anchor_returns_same_ids_as_lowercase`

SQL fixture:
```sql
INSERT INTO mart.fact_enriched (m, k)
WITH raw AS (SELECT val AS m, key AS k FROM staging.src_raw)
SELECT m, k FROM raw;
```
Expected upstream from `mart.fact_enriched.m`: `['staging.src_raw.val']`  
Actual with P1 fix: `['raw.m']` (chain broken, can only reach CTE node, not original source)

### Constraint

Cannot suppress all CTE-projection edges for INSERT-with-emitted-target because:
- The positional block emits edges where the CTE IS the src (e.g. `raw.m → mart.fact_enriched.m`)
- The CTE-projection block emits edges where the CTE IS the dst (e.g. `staging.src_raw.val → raw.m`)
- Upstream traversal requires BOTH to chain the full path

### Planner action needed

Redesign P1 so that:
- CTE chain edges (`src_table.col → cte_name.col`) are preserved (needed for traversal)
- EITHER the P1 acceptance tests are relaxed (the "leak" is an acceptable graph shape for
  INSERT-with-CTE since traversal needs these edges), OR
- A different fix is found (e.g. the P1 "leak" is measured differently, or the upstream
  traversal is redesigned to follow a different path)

Alternative approaches to explore:
1. **Accept dual-destination graph shape**: Keep CTE chain edges. The "leak" in the DWH
   was that 15,102 edges landed ONLY on CTE nodes (with no path forward). If both the real
   target AND the CTE chain are present, traversal works. The P1 test assertion `not any
   cte_* in dst_keys` may be too strict.
2. **Rewrite P1 test to allow CTE chain but require real target**: Change the test to assert
   `ba.fact.*` exists in dst_keys, without asserting `base.*` must NOT be in dst_keys.
3. **Emit direct source edges in the positional block**: Instead of relying on CTE-chain
   edges, have the positional block resolve the full transitive source. This is a deeper
   change to the `#25` block.

## Rollout / Rollback

- **Rollout:** ships in `1.7.0`. Re-index is the migration path (no backward-compat shims,
  per CLAUDE.md) — existing graphs gain the new catalogs only after `sqlcg index` re-runs.
- **Rollback:** revert the PR; no schema/data state to unwind. A graph indexed under `1.7.0`
  is still readable by `1.6.1` tooling (same `SqlColumn`/`HAS_COLUMN` shapes).
- **Milestone gate:** this sprint ships when P3/P4 catalogs + the scoped metric land and
  `sqlcg gain`/`db info` records the new (≈55–65 %) edge health. The **≥95 % coverage
  milestone is a separate, later gate** (BQ-2) covering the P2-residual + P5 cohort — it is
  explicitly **not** claimed by this sprint.
