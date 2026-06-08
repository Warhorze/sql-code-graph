# Feature Plan: Coverage fixes P1 / P3 / P4 — push edge health 29% → ≥95%

## Summary

Fix the three SQL-parsing coverage gaps catalogued in
[`column_coverage_findings.md`](../reports/column_coverage_findings.md) so the DWH
corpus (1,342 files) climbs from ~29% edge health to **≥95%**: P1 (CTE/derived
destination leak — 15,102 edges), P3 (quoted view names with spaces drop the schema
from the column catalog — 6,045 edges, 232 tables), and P4 (CTAS tables emit zero
`SqlColumn` rows — 241 tables). All three are parser/indexer changes; the failing
TDD acceptance tests already exist in
[`test_column_coverage_patterns.py`](../../tests/integration/test_column_coverage_patterns.py).

This is **new parser capability** → **minor** version bump `1.6.1` → `1.7.0`.

---

## Scope

### In Scope

- **P1 — CTE/derived destination leak.** When an `INSERT INTO t (col_list) WITH cte …
  SELECT … FROM cte` resolves its authoritative target columns through the existing
  `#25` positional block, the lineage edges must land on the INSERT target `t`, not on
  the CTE node. Fix = suppress the CTE-projection destination edges (and their `kind='cte'`
  SqlTable / `dst_key='cte.*'` rows) **for CTEs that are consumed only as the final
  source of an INSERT whose target columns are already emitted**.
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
| P1 | [`base.py`](../../src/sqlcg/parsers/base.py) | `_extract_column_lineage` (CTE-projection block, ~L1135–1292) | Skip CTE-destination edge emission for the INSERT-consumed final CTE |
| P3 | [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) | `_parse_statement` / new `_extract_select_output_columns` | Give CREATE VIEW-AS-SELECT a `defined_columns` list |
| P3 | [`indexer.py`](../../src/sqlcg/indexer/indexer.py) | `_build_file_rows` DDL-column loop (~L1138–1152) | Qualify `table_name` when the bare name contains a space |
| P4 | [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) | `_parse_statement` / new `_extract_select_output_columns` | Give CTAS a `defined_columns` list from the outer SELECT projection |

P3 and P4 share the **same** new helper and the **same** existing emission seam: once a
CREATE VIEW/TABLE-AS-SELECT statement carries `defined_columns`, the indexer's existing
DDL-column loop (lines 1138–1153) already emits `SqlColumn` rows keyed by `table.full_id`
and `HAS_COLUMN` edges `{src_key: table.full_id, dst_key: col_id, source: "ddl"}`. No new
emission code is needed for the catalog — only the column-name derivation.

### P1 — CTE destination de-leak (interfaces)

The `#25` positional-INSERT block (`base.py` L757–871) already emits the authoritative
`t.col` edges for `INSERT INTO t (c1,c2) … SELECT … FROM cte`. The leak is the **separate**
CTE-projection block (L1135–1292) which unconditionally emits `cte.col` destination edges
(role `"cte"`), and the indexer then materialises a `kind='cte'` SqlTable + `cte.col`
`COLUMN_LINEAGE` dst.

Fix: compute, once per statement (before the CTE-projection loop), the set of CTE aliases
that are the **terminal source of an INSERT whose target columns were already emitted by
the `#25` / Part-B positional blocks**, and skip emitting destination edges for those CTEs.

- "Terminal source" = a CTE named in the outer SELECT's `FROM`/`JOIN` list (the CTEs the
  final projection actually reads from). Use the already-parsed `body` AST (`find_all(exp.Table)`
  on the outer SELECT's `FROM`, filtered to CTE names in `combined_sources`). This is a
  structural read of the existing AST — **no `qualify`, no `build_scope`, no `sg_lineage`** —
  computed **once per statement**, identical cost class to the existing `cte_source_tables`
  computation already done once per CTE body.
- Gate on `isinstance(stmt, exp.Insert) and dst_table is not None` and that the positional
  block actually emitted at least one edge (i.e. `positional_col_names` is non-empty). When
  the INSERT has no resolvable target catalog, leave the CTE edges in place (no regression
  to the existing degrade behaviour).
- Transitive chains (`cte_insert ← cte_base`): suppress the **terminal** CTE only (the one
  the outer SELECT FROMs). Intermediate CTEs whose only consumer is the suppressed terminal
  CTE produce edges whose dst is the suppressed CTE; those become orphaned `cte.*` dst rows.
  To fully clear the leak (the test asserts **no** `cte_*` dst), suppress **all** CTE
  destinations in the WITH clause when the statement is an INSERT-with-emitted-target — the
  authoritative `t.col` edges already carry the full source→target lineage through the
  positional block, so the intermediate CTE-node edges are redundant for this statement.

> **Design note:** for a plain `SELECT`/`CREATE VIEW` (no INSERT target) the CTE-projection
> edges remain exactly as today — they are the only destinations available. The suppression
> applies **only** to the INSERT-with-emitted-target case, where a real target exists and the
> CTE node is a pure intermediate.

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
> catalog emission; P1 is the one suppression change and is verified last so its
> regression surface (existing CTE-projection tests) is checked against a green baseline.

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
- Verify no other `SqlColumn` emission site (lineage `edge.src`/`edge.dst` at L1203/1231,
  CLONE at L1376) regresses: their `table_name` stays bare for normal names; apply the same
  `" " in name` qualify there **only if** an existing test needs it — default is to change
  the DDL-column loop only (the path the P3 catalog now flows through).

### Phase 3: P1 — CTE/derived destination de-leak

**Step 3.1** — Suppress CTE-destination edges for INSERT-with-emitted-target.
- Files: [`base.py`](../../src/sqlcg/parsers/base.py) `_extract_column_lineage`,
  CTE-projection block (L1135–1292).
- Compute once, before the `for cte in cte_expressions` loop, a boolean
  `_suppress_cte_dsts = isinstance(stmt, exp.Insert) and dst_table is not None and
  bool(positional_col_names)`. When true, `continue` past the CTE-destination edge
  emission (skip the whole CTE-projection block for this statement) — the positional
  block already emitted the authoritative `t.col` edges.
- This is a single guard read of values already computed earlier in the function; **no new
  qualify / build_scope / sg_lineage / expand**, **once per statement**.
- Acceptance: `test_P1_cte_destination_does_not_leak_to_cte_node` and
  `test_P1_cte_chain_final_cte_does_not_leak` pass — edges land on `ba.fact.*` /
  `ba.kpi_fact.*` and **no** `base.*` / `cte_*` dst keys remain.
  `test_P1_derived_subquery_does_not_leak` stays green (already passing).
- Regression guard: run the existing CTE-projection / cross-file chain tests
  (`test_T07_*`, union-CTE, `test_38*`) — a plain `SELECT … FROM cte` (no INSERT target)
  must be unchanged (the guard is gated on `isinstance(stmt, exp.Insert)`).

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
| P1 CTE-dst suppression | "body_scope built once per statement"; the scope-path `copy=False`/`trim_selects=False`; the #25 body-copy-once; the per-column failure-path-no-requalify | The guard is a boolean computed from values already in scope (`stmt`, `dst_table`, `positional_col_names`), read **once** before the CTE loop. It only **skips** work (emits fewer edges); it adds no qualify/scope/expand/lineage call. The `#25` and Part-B positional blocks above it are untouched. |

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
- **P1** — INSERT-from-CTE / multi-CTE chain edges land on the real target, never on a
  `base.*` / `cte_*` node (2 tests). Derived-subquery test already passes (regression guard).
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
  `test_38*`): a plain `SELECT … FROM cte` and `CREATE VIEW … FROM cte` keep their CTE-node
  destination edges (P1 suppression is INSERT-only).
- **Positional INSERT / CLONE blindspot** (`test_F2_*`, clone-blindspot, `test_backfill_*`):
  the `#25` / Part-B / CLONE catalog paths are untouched; `inferred_from_source_name`
  semantics unchanged.
- **Perf guards** (the four files in the Performance section).
- **Full gate**: `uv run pytest`, `uv run pyright`, `uv run ruff check src tests` all green.

### Coverage-metric sanity (manual, post-merge on DWH)

Re-index the DWH corpus and run `uv run sqlcg gain` / `db info` (the dashboard shipped in
`gain_coverage_metrics`); assert **edge health ≥95%** and blindspot-table count collapses
toward the P3+P4 catalog gains (~1,115 tables, ~35,559 edges recovered per the findings
projection). Record the before/after numbers in the postmortem.

---

## Acceptance Criteria

- [ ] All 10 tests in `test_column_coverage_patterns.py` pass (6 currently failing now green;
      4 already-passing stay green).
- [ ] `_extract_select_output_columns` exists on `AnsiParser`, is **called** from
      `_parse_statement` (grep-confirmed call site), and inherited unchanged by
      `SnowflakeParser`.
- [ ] CTAS and CREATE VIEW-AS-SELECT statements emit `SqlColumn` rows + `HAS_COLUMN` edges
      keyed on `table.full_id`; explicit-column-list CREATE TABLE catalogs are unchanged.
- [ ] `SELECT *` CTAS produces a `SqlTable` node and **no** `SqlColumn` rows, no exception.
- [ ] Quoted-space view `SqlColumn.table_name` contains the schema; bare-name tables keep
      a bare `table_name`.
- [ ] INSERT-from-CTE edges land on the INSERT target; no `cte_*`/`base.*` destination rows
      for those statements; plain `SELECT/VIEW … FROM cte` CTE-node edges unchanged.
- [ ] All four perf-guard test files green after every phase; no new
      qualify/build_scope/expand/sg_lineage call introduced (behavioural check: P1 guard
      boolean computed once before the CTE loop).
- [ ] Indexing 1,342 files ≤180s (record actual in postmortem).
- [ ] DWH edge health ≥95% measured via `sqlcg gain`/`db info` after re-index.
- [ ] Version `1.6.1` → `1.7.0` across `pyproject.toml`, `__init__.py`, lockfile;
      `sqlcg --version` prints `1.7.0`.
- [ ] Full gate (pytest / pyright / ruff) green.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| P1 suppression over-reaches and drops CTE-node edges for plain `SELECT/VIEW … FROM cte` | Guard is gated on `isinstance(stmt, exp.Insert) and dst_table is not None and positional_col_names` — non-INSERT statements are untouched. Existing CTE-projection/union tests are the regression guard. |
| P1 leaves orphaned intermediate-CTE `cte.*` dst rows on multi-CTE chains | Suppress **all** WITH-clause CTE destinations for the INSERT-with-emitted-target case (the positional block already carries full source→target lineage); the chain test (`cte_base`→`cte_insert`) asserts **no** `cte_*` dst remains. |
| P4 CTAS where the outer body is a UNION (no projections on `Union` node) | Mirror the existing CTE-projection handling (`base.py` L1165–1170): walk to the deepest left-branch `Select` for projection names. Add this to `_extract_select_output_columns` if a UNION-bodied CTAS fixture surfaces; otherwise UNION CTAS degrades to table-only (safe). |
| P3 `table_name` qualification breaks a query that filters on bare `table_name` | Only names **containing a space** are qualified; normal names unchanged. Join keys use `table_qualified`/`full_id`, not `table_name`. Full gate catches any consumer that filters on `table_name`. |
| New `defined_columns` on AS-SELECT shifts a table into the `ddl_columns_by_bare` ambiguity set, perturbing Part-B positional INSERT mapping | The aggregator already de-duplicates identical catalogs and excludes genuinely-ambiguous bare names (L98–103); a CTAS-derived catalog matching its own DDL is identical → no new ambiguity. Regression: clone-blindspot / backfill tests stay green. |
| Perf regression from a hot-path scaling flip | No new per-column op; all changes once-per-statement or once-per-row. Four perf-guard files run after each phase; whole-corpus timing ≤180s checked post-merge. |
| Edge health misses ≥95% because residual P2 phantom edges remain | Findings project P2 residual collapses once P3/P4 supply catalogs; if measured health lands below 95%, that is a **stop condition** — re-run the diagnostic (`scripts/column_coverage_check.py`) to identify the residual pattern before claiming the milestone, do not relax the gate. |

## Rollout / Rollback

- **Rollout:** ships in `1.7.0`. Re-index is the migration path (no backward-compat shims,
  per CLAUDE.md) — existing graphs gain the new catalogs only after `sqlcg index` re-runs.
- **Rollback:** revert the PR; no schema/data state to unwind. A graph indexed under `1.7.0`
  is still readable by `1.6.1` tooling (same `SqlColumn`/`HAS_COLUMN` shapes).
- **Milestone gate:** the project ships the coverage milestone when `sqlcg gain`/`db info`
  reports edge health ≥95% on the DWH corpus.
