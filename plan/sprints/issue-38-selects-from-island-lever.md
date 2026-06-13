# Feature Plan: #38 table-level `SELECTS_FROM` island lever (measure-first)

> **Status: PLANNED — not yet implemented.**
> Source of truth: [`plan/research/table_level_island_lever.md`](../research/table_level_island_lever.md)
> (READ-ONLY diagnosis, 2026-06-13, graph schema v8, indexed sha `fdf1b551`).
> Charter: [`plan/reports/e8_temp_chain_postmortem.md`](../reports/e8_temp_chain_postmortem.md) Rec #3
> ("the table-level island lever is elsewhere; a separate investigation is needed").

> **Plan review (2026-06-13):** REVIEWED with amendments. plan-reviewer returned
> APPROVE-WITH-AMENDMENTS; all four amendments applied — A1 (BLOCKER: flat `cte_sources` walk + recursive-CTE
> `union_scopes` known gap, sqlglot-probe-confirmed), A2 (`kind IN _WRITE_KINDS` discriminator on the
> metric), A3 (alias-remap acceptance criterion for CTE-body tables), A4 (explicit from→to version
> bumps). Framing, measure-first ordering, hot-path safety, and non-overlap with
> `issue-38-backfill-cte-bridge.md` were all confirmed clean by the reviewer.

## Summary
Many CTE-wrapped write queries — `INSERT INTO ba.X WITH cte AS (SELECT … FROM ba.real_table) SELECT … FROM cte` —
resolve their target *and* their `COLUMN_LINEAGE` correctly, but emit **zero `SELECTS_FROM` source
rows**, because [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) filters out the top-level CTE
alias and never descends into the CTE child scope. The written table is left with no incoming
`SELECTS_FROM` edge and floats off as a table-level island. This sprint ships the **metric first**
(falsifiable baseline), then the one-line-class **fix** in `_real_tables()` to descend into CTE child
scopes for `stmt.sources`.

---

## ⚠ Framing rule — #38 is NOT E8 (do not violate)

This rule is load-bearing for every number in this plan and is encoded so a reader cannot
accidentally conflate the two findings:

- **#38 is a `SELECTS_FROM` (table-level) source-extraction gap.** E8 is a `COLUMN_LINEAGE`
  (column-level) event (`col_lineage_skip:dynamic_source`). They are **causally independent**.
- They **co-occur** in the same complex ETL files (temp chains + wide SELECTs + CTEs trigger both),
  but neither causes the other. The E8 fix (PR #111, parked) moved table-level islands by **0**
  (measured: 147 → 147 — [`e8_temp_chain_postmortem.md`](../reports/e8_temp_chain_postmortem.md) table).
- **NEVER cross-apply a `COLUMN_LINEAGE` count to a `SELECTS_FROM` baseline.** This exact conflation
  (a column-level "89 tables with zero incoming column lineage" cited as "≈60% of the 147
  [table-level] islands") over-projected E8 **8×**
  ([`e8_temp_chain_postmortem.md`](../reports/e8_temp_chain_postmortem.md) "Root cause", Rec #1).
- Every island / edge / orphan number in this plan **names its edge type**. The only place
  `COLUMN_LINEAGE` is used is (a) as a *probe* that the real source tables were resolvable and
  (b) to *synthesise* the projected table-level edges in the research doc — never as a substitute
  for a table-level count.

---

## Reconciliation with the existing #38 plan (read before assuming overlap)

There is an existing 58 KB plan, [`issue-38-backfill-cte-bridge.md`](issue-38-backfill-cte-bridge.md).
**It is a different feature and must NOT be folded into this one:**

| | `issue-38-backfill-cte-bridge.md` (existing) | **This plan** (`issue-38-selects-from-island-lever.md`) |
|---|---|---|
| Surface | MCP table-graph **tools** — [`tools.py`](../../src/sqlcg/server/tools.py), [`queries.sql`](../../src/sqlcg/core/queries.sql), [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) (column case-norm) | The **parser** — [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) in `base.py` |
| Edge type | Kahn ordering + synthetic-node leak over the `COLUMN_LINEAGE` closure | **`SELECTS_FROM`** (table-level) source-row emission |
| Hot-path stance | **"do NOT touch `base.py`/`indexer.py`"** — explicitly routes *around* the `_real_tables()` gap at the tool layer (Blocking Questions, line 455–464; Performance invariants, line 193–199) | **Fixes the `_real_tables()` gap directly** — the thing the other plan routed around |
| Status | Phases 1–5 implemented (PR #57 + case-norm fold); the parser gap was deliberately left alone | Not started |

The existing plan **names this exact gap** as out of scope and the root cause it chose not to touch:
> "the root cause of defect 1 lives in those files (the missing `SELECTS_FROM` emission), but Option A
> deliberately fixes it at the tool layer … precisely to avoid the perf-invariant-bearing parser surface."
> — [`issue-38-backfill-cte-bridge.md`](issue-38-backfill-cte-bridge.md) line 194–199.

This sprint completes the work the other plan deferred. The two are complementary, not redundant.
**Conflicts found:** none — the existing plan is internally consistent with deferring the parser fix.
The only thing to verify before PR-2 (Step 2.0) is that the existing tool-layer fix still behaves
correctly once `SELECTS_FROM` rows start appearing (it reads the `COLUMN_LINEAGE` closure, not
`SELECTS_FROM`, so it should be unaffected — but assert it, do not assume).

---

## Scope

### In Scope
- **PR-1 (metric, ships first):** a graph-derived, `gain`-surfaced, falsifiable metric that counts the
  lever's target population on the live graph — **no manual networkx script**. Establishes the exact
  baseline PR-2 must move.
- **PR-2 (the fix):** extend [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) to also collect
  real source tables from CTE **child** scopes when building `stmt.sources`, so CTE-wrapped
  INSERT…SELECT writes emit their `SELECTS_FROM` source rows — mirroring the descent the column path
  already does in `_extract_column_lineage`.
- A **`SELECTS_FROM`-specific** regression guard (the existing #38 guard is column-level and missed
  this — see §"Regression guard" below).
- Live-DWH re-acceptance for PR-2 (CI-green is not done for a lineage feature).
- Version bumps (PR-1 = minor, PR-2 = patch — see §Versioning).

### Non-Goals
- The MCP table-graph tools (Kahn ordering, synthetic-node leak, column case-norm) — owned by
  [`issue-38-backfill-cte-bridge.md`](issue-38-backfill-cte-bridge.md), already shipped.
- E8 column-level temp-chain inlining (PR #111, parked — a separate `COLUMN_LINEAGE` feature).
- Target-resolution for the ~1,090 targetless writes — a real but **different** coverage story that
  does **not** move the island count (the islands' targets already resolved; their *sources* didn't —
  research doc §5 "Not-a-lever #1"). Out of scope.
- The residual ~99 islands after the fix (genuine reference-only / out-of-corpus Liquibase-XML
  isolates) — no parser lever applies; out of scope.
- Any parse-time schema-CSV feeding into `exp.expand()`/`qualify()` (forbidden per CLAUDE.md).

---

## Design

### PR-1 — the metric

**Home: [`src/sqlcg/cli/coverage.py`](../../src/sqlcg/cli/coverage.py), surfaced via `sqlcg gain`
Section G + `sqlcg db info`.**

Justification for this home (over a fresh report bucket or raw DuckDB):
- `coverage.py` is the **established** pattern for graph-derived `gain` metrics — each is a `_Q_*` SQL
  constant + a `CoverageStats` field + a `render_coverage_lines` line + a `coverage_to_json` key, all
  routed through `run_read_routed` (server-aware, degrades gracefully). The existing
  `zero_edge_write_queries` / `cte_key_collisions` / `rescuable_unqualified_edges` counters are exactly
  this shape.
- Raw DuckDB queries are **project-disfavoured** for coverage metrics
  (memory `feedback_use_gain_not_manual_queries.md`). `gain` is the mandated surface.
- It is reproducible (one re-index → one `gain` run), needs no networkx, and both render sites
  (`db info`, `gain`) share `render_coverage_lines` so there is no drift.

**Metric 1 (primary, the lever's exact target population) — `cte_source_gap_writes`:**
producer queries that have ≥1 outgoing `COLUMN_LINEAGE` edge but **zero** `SELECTS_FROM` source rows.
This is the precise set PR-2 should shrink (the column path resolved the sources; the table path
emitted nothing). SQL shape (mirror the existing `_Q_*` constants; DuckDB double-quoted names):

```sql
SELECT COUNT(*) AS cte_source_gap_writes
FROM "SqlQuery" q
WHERE q.kind IN ('INSERT', 'MERGE', 'CREATE_TABLE', 'UPDATE')   -- write kinds only (reuse coverage._WRITE_KINDS)
  AND q.target_table <> ''
  AND EXISTS (SELECT 1 FROM "COLUMN_LINEAGE" cl WHERE cl.query_id = q.id)
  AND NOT EXISTS (SELECT 1 FROM "SELECTS_FROM" sf WHERE sf.src_key = q.id);
```

> **Amendment 2 — discriminator (do not skip):** filter on `q.kind IN _WRITE_KINDS`
> (`INSERT, MERGE, CREATE_TABLE, UPDATE` — reuse [`coverage._WRITE_KINDS`](../../src/sqlcg/cli/coverage.py#L33)),
> **not** `target_table <> ''` alone. `target_table <> ''` is **not** a strict subset of write kinds:
> a `SELECT`-kind query (e.g. a CTAS parsed as `SELECT`) can carry an inferred `target_table` and would
> inflate the metric. Keep `target_table <> ''` **in addition** (a write with an unresolved target is a
> target-resolution problem, not this lever). The Step 1.4 unit test must include a `SELECT`-kind row
> with a non-empty `target_table` and assert it is **excluded** from the count, so the
> "inverse complement of `zero_edge_write_queries`" claim is provably correct (both now share the same
> `_WRITE_KINDS` discriminator).

> **Developer: confirm the `SELECTS_FROM.src_key`/`query_id` join key against the schema before
> coding** — the SELECTS_FROM emission writes `{"src_key": query_id, "dst_key": src_table.full_id}`
> ([`indexer.py:1385`](../../src/sqlcg/indexer/indexer.py#L1385)) and `COLUMN_LINEAGE` joins on
> `cl.query_id = q.id` (see [`coverage.py` `_Q_ZERO_EDGE_WRITES`](../../src/sqlcg/cli/coverage.py#L224)).
> This metric is the **inverse complement** of the existing `zero_edge_write_queries` (writes with zero
> *column* lineage) — keep them clearly distinct in naming and in the rendered line; do not reuse the
> field.

**Metric 2 (secondary, the headline the user sees) — table-level small-island count.**
The research doc's 147-island figure is computed on the noise-filtered `SELECTS_FROM → target_table`
graph with a weakly-connected-component pass. A WCC count is awkward in pure SQL. **Decision: prefer
Metric 1 as the falsifiable gate** (it is a direct row count, deterministic, and is the exact lever
population). Surface Metric 2 only if it can be expressed as a cheap SQL proxy without networkx; if
not, record the island count from the research-doc reproduction block as the **documented baseline**
in this plan and re-measure it the same way (research doc §2 reproduction) at PR-2 acceptance — it
does **not** need to live in `gain`. **The `gain` gate is Metric 1.**

### PR-2 — the fix

**Site:** [`_real_tables(scope)`](../../src/sqlcg/parsers/base.py#L604) (base.py ~604). Today it
iterates `scope.tables` and drops any name in `scope.cte_sources` (line 629), never descending into
the CTE child scopes that `scope.cte_sources` *values* point at. The fix: after collecting the
non-CTE top-level tables, also walk each CTE child scope and collect its real (non-CTE) tables,
appending them to `tables`. This mirrors the descent the column path
([`_extract_column_lineage`](../../src/sqlcg/parsers/base.py)) already performs — it is the
table-level analogue of an already-working column behaviour, not new traversal logic.

The emitted `stmt.sources` then flows unchanged through the existing emission loop at
[`indexer.py:1372–1385`](../../src/sqlcg/indexer/indexer.py#L1372) (`for src_table in stmt.sources: …
rows.selects_from_edges.append(...)`), so the `SELECTS_FROM` rows appear with **no indexer change**.

**Mechanics the developer must pin (do not guess) — sqlglot probe-confirmed (sqlglot 30.6.0, this repo):**
- **`root_scope.cte_sources` is already FLAT — this is NOT a recursive nested descent.** Probed on
  `INSERT INTO s.b WITH cte1 AS (SELECT x FROM s.a), cte2 AS (SELECT x FROM cte1) SELECT x FROM cte2`:
  `root.cte_sources.keys() == ['cte1', 'cte2']` (ALL CTEs flat at the root), `cte1_scope.tables` name
  set `== {'a'}`, `cte2_scope.tables` name set `== {'cte1'}` (the inner alias appears as a table inside
  the outer CTE's scope), `root.tables == {'cte2'}`. The correct algorithm is a **single-level pass over
  `root_scope.cte_sources.values()`**, and for each CTE child scope filter its `.tables` against
  **`root_scope.cte_sources.keys()`** (the flat set of ALL CTE alias names) — **NOT** against that child
  scope's own `cte_sources`. Filtering against the root's flat key set is exactly what drops the `cte1`
  alias out of `cte2`'s table list and removes any self-reference loop risk. Do **not** implement a
  recursive nested descent; it is unnecessary and would risk re-emitting aliases.
- **Recursive CTEs (`WITH RECURSIVE r AS (… UNION ALL … FROM r)`) are a KNOWN GAP the flat walk does
  not cover.** Probed: the recursive CTE scope has `tables == []` and its real source lives in
  `scope.union_scopes[i].tables` (2 union scopes for the `UNION ALL` recursion). The flat `cte_sources`
  walk therefore yields **zero** real tables for a recursive CTE — **silent data loss, not a crash.**
  Recursive CTEs do **not** appear in the 46-query DWH gap population (research §4), so PR-2 **may
  document this as a known gap for a future ticket** rather than implement `union_scopes` descent now —
  but this plan states the API fact explicitly so the developer does not silently assume it works. If
  the developer chooses to handle it, descend `scope.union_scopes[i].tables` (or use `traverse_scope()`),
  and a test must then pin a recursive CTE producing its real source row.
- De-duplicate: a real table reachable both top-level and via a CTE body must not produce duplicate
  `SELECTS_FROM` rows. The existing emission already de-dupes on `(src_key, dst_key)`
  ([`indexer.py:237–239`](../../src/sqlcg/indexer/indexer.py#L237)), so duplicates are not fatal — but
  `_real_tables` must still return a de-duplicated list for defense-in-depth, so the unit test can
  assert no duplicate at the source.
- Apply `_apply_table_alias` (schema-alias remap `ba_tmp→ba` etc.) on the CTE-body tables exactly as
  the top-level path does (line 631) — otherwise the new edges land on un-aliased table ids and miss
  the giant component. CTE-body table expressions carry the same `db`/`name` structure as top-level
  ones (e.g. `BA.WTDH_ARTIKEL` → `db='BA'`, `name='WTDH_ARTIKEL'`), so `_convert_table_expr_to_ref` +
  `_apply_table_alias` apply unchanged. **A CTE body referencing an aliased schema (e.g.
  `ba_tmp.real_table` where `ba_tmp→ba` is configured) must emit `SELECTS_FROM dst_key = 'ba.real_table'`,
  not `'ba_tmp.real_table'`** — pinned by an acceptance criterion in PR-2 (Amendment 3).

**Why this is OFF the perf hot-path (must be stated in the PR):**
[`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) runs **once per statement** during parse,
after `build_scope()` (which the column path already requires). The CTE-child walk is a cheap
one-pass scope traversal, **independent of** the per-column `qualify`/`body_scope`/`expand` loop that
the [CLAUDE.md performance invariants](../../CLAUDE.md) protect. It does **not** touch:
`exp.expand()` sources/`dependency_filter`, `body_scope` reuse, the pure-literal skip, the
`copy=False`/`trim_selects=False` kwargs, the INSERT body-copy-once path, or the bulk/per-batch upsert
paths. **The fix must be implemented such that all perf-invariant suites pass UNMODIFIED.**

### Data models / API
No schema change. `stmt.sources` is already a `list[TableRef]`; the fix only makes it non-empty for
CTE-wrapped writes. No `SELECTS_FROM` schema change. PR-1 adds additive `CoverageStats` fields + one
JSON key + one render line (same additive pattern as every prior coverage metric).

### Dependencies
None new.

---

## Implementation Steps

### PR-1 — metric (ships first; minor bump)

**Step 1.1** — Add `_Q_CTE_SOURCE_GAP_WRITES` to [`coverage.py`](../../src/sqlcg/cli/coverage.py),
run it in `collect_coverage()` via `run_read_routed`, store `cte_source_gap_writes: int` on
`CoverageStats`.
- Files affected: [`coverage.py`](../../src/sqlcg/cli/coverage.py).
- Acceptance: `collect_coverage()` returns the field populated on a graph that has the gap;
  degrades to `None` (not a crash) when the graph is unavailable, same contract as the other queries.

**Step 1.2** — Render it in `render_coverage_lines` (one line, e.g. `CTE-wrapped writes missing
SELECTS_FROM source: <n>`) and add the JSON key in `coverage_to_json`. Both `db info` and `gain`
inherit it via the shared renderer.
- Files affected: [`coverage.py`](../../src/sqlcg/cli/coverage.py).
- Acceptance: `uv run sqlcg gain` prints the line; `uv run sqlcg gain --json` includes the key.

**Step 1.3** — Establish the baseline. On the **current** DWH graph (pre-fix), the metric must print
the value recorded in §"Baseline" below. The developer indexes the DWH (delegated; read-only on the
DWH repo), runs `gain`, and **records the observed value in this plan** under a `### PR-1 Baseline
(measured)` heading. If it diverges materially from the documented baseline, STOP and reconcile before
PR-2 (the projection assumes this population).

**Step 1.4** — Unit test the metric query against a synthetic in-memory graph that contains one
CTE-wrapped write (COLUMN_LINEAGE present, SELECTS_FROM absent) and one direct write (both present):
assert the count is exactly 1. Assert observable output (the integer), not "no exception".
- Files affected: a new/extended test under `tests/unit/` (mirror
  [`test_selects_from_completeness_unit.py`](../../tests/unit/test_selects_from_completeness_unit.py)
  style).

**Step 1.5** — Version + housekeeping: bump **1.25.8 → 1.26.0** (minor — new metric surface) in
[`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py),
`uv lock`. Do not tag, do not close the issue (user verifies + tags).

### PR-2 — the fix (ships after PR-1; patch bump)

**Step 2.0** — Pre-flight confirmation (no code yet): on the synthetic fixture from Step 2.2, confirm
the existing tool-layer #38 fix ([`issue-38-backfill-cte-bridge.md`](issue-38-backfill-cte-bridge.md)
PR #57) still produces correct `get_backfill_order`/`get_change_scope` output once `SELECTS_FROM` rows
appear. It reads the `COLUMN_LINEAGE` closure, not `SELECTS_FROM`, so it should be unaffected — assert
it, do not assume. If it regresses, STOP and surface a blocking question.

**Step 2.1** — Extend [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) to descend into CTE
child scopes and collect real source tables (Design §"PR-2 — the fix"), with de-dup + alias-remap +
recursive-CTE handling per the mechanics list.
- Files affected: [`base.py`](../../src/sqlcg/parsers/base.py) (`_real_tables` only).
- Call site is unchanged ([`ansi_parser.py:466`](../../src/sqlcg/parsers/ansi_parser.py#L466)) —
  no new method to grep-confirm; if a helper is extracted, grep-confirm its call site.
- Acceptance: indexing `INSERT INTO s.b WITH cte AS (SELECT … FROM s.a) SELECT … FROM cte;` produces a
  `SELECTS_FROM` row `(query→s.a)`; the direct-insert control still produces its row; no duplicate row
  when a real table appears both top-level and in a CTE body.

**Step 2.2** — Regression guard (`SELECTS_FROM`-specific — see §"Regression guard"). New integration
test asserting the **raw `SELECTS_FROM` rows** for a CTE-wrapped write, plus a multi-CTE / CTE-reads-CTE
case, plus the control. Assert the exact `(src_key, dst_key)` rows on the indexed graph.
- Files affected: new `tests/integration/test_selects_from_cte_body_source.py` (or extend
  [`test_issue38_cte_insert_regression.py`](../../tests/integration/test_issue38_cte_insert_regression.py)
  with an explicitly-labelled `SELECTS_FROM` block — but a new file is cleaner since the existing one
  is column-level by charter).

**Step 2.3** — Perf invariants: run the guard suites **UNMODIFIED** and confirm green —
[`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
[`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py).
No slack-raising. A red means an invariant broke — fix the fix, not the test. No new hot-path op is
introduced (the walk is once-per-statement, not per-column), so no new perf counter is required;
confirm the scaling guard stays flat at N and 2N.

**Step 2.4** — Live-DWH re-acceptance (delegated to a developer; **read-only** on
`/home/ignwrad/Projects/dwh`, never commit; the e2e harness is gitignored — never commit it).
Re-index the DWH from scratch into a throwaway DB and report:
- The PR-1 metric (`cte_source_gap_writes`) re-measured — **must move toward 0** from the baseline.
- The table-level island count re-measured via the research-doc §2 reproduction block —
  **must move 147 → ~99** (the projection's ±; an exact 99 is not required, but it must drop
  substantially and in the right direction, with **+~299 `SELECTS_FROM` edges**).
- `gain` table health (strict / scoped / catalogued %) — confirm no regression in the headline
  trust numbers.
- Wall-time vs the **3-minute budget** (canonical baseline ~210–256s; the once-per-statement walk is
  negligible — confirm no regression).
- Record the verdict in the PR description (not in the DWH repo).

**Step 2.5** — Version: bump **1.26.0 → 1.26.1** (patch — the fix alone adds no new surface) in
[`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py),
`uv lock`. Do not tag, do not close the issue.

---

## Regression guard — why the existing #38 guard missed this (coordinate, don't duplicate)

[`test_issue38_cte_insert_regression.py`](../../tests/integration/test_issue38_cte_insert_regression.py)
is **column-level**: it asserts `analyze upstream` / `TRACE_COLUMN_LINEAGE_QUERY` resolves *through*
the CTE node to the real source — i.e. it pins `COLUMN_LINEAGE`, which was always present. It **never
asserts a `SELECTS_FROM` row**, which is exactly why the table-level gap survived undetected
(the research doc's central finding: column path works, table path emits nothing).

This is the same class as **issue #40 ("regression guards ineffective")** — anchor/golden tests that
assert one edge type pass while another silently regresses. The new guard (Step 2.2) must:
- Assert the **raw `SELECTS_FROM` `(src_key, dst_key)` rows** for the CTE-wrapped write (observable
  output, not "no exception") — the assertion the old guard lacks.
- Live in a clearly-named `SELECTS_FROM` file so a future reader does not assume the column-level #38
  guard already covers it.
- Reference this plan in its docstring (per `developer.md` test-naming).

Do **not** duplicate the column-level assertions — those stay in the existing file. The two guards are
complementary by edge type.

---

## Test Strategy
- **Unit (PR-1):** the `cte_source_gap_writes` query returns exactly 1 on a synthetic graph with one
  CTE-wrapped write (COLUMN_LINEAGE present, SELECTS_FROM absent) and one direct write; 0 on an
  all-direct graph. Asserts the integer, not absence of exception.
- **Integration (PR-2):** real in-memory DuckDB via `Indexer().index_repo(..., use_git=False)`. A
  CTE-wrapped INSERT…SELECT emits the expected `SELECTS_FROM` source row(s); a CTE-reads-CTE chain
  resolves to the innermost real table; a real table present both top-level and in a CTE body emits
  no duplicate; the direct-insert control is unchanged. Raw-row assertions.
- **Perf (PR-2):** the four invariant suites pass unmodified; scaling guard flat at N and 2N.
- **Live-DWH (PR-2):** metric re-measured toward 0, islands 147→~99, +~299 edges, no health
  regression, within the 3-min budget (Step 2.4).
- **Full suite:** `uv run pytest` green; `uv run pyright` + `uv run ruff check src tests` clean.

---

## Acceptance Criteria

### PR-1 (metric)
- [ ] `cte_source_gap_writes` is a graph-derived `CoverageStats` field, queried via `run_read_routed`,
      degrading to `None` (not crashing) when the graph is unavailable.
- [ ] `uv run sqlcg gain` (Section G) and `uv run sqlcg db info` both print the metric line via the
      shared `render_coverage_lines`; `gain --json` includes the key.
- [ ] The metric is distinct from `zero_edge_write_queries` (it is its inverse complement) — distinct
      field name and rendered label; no reuse.
- [ ] Unit test asserts the count is exactly 1 on a one-gap synthetic graph and 0 on an all-direct
      graph (observable integer, not "no exception").
- [ ] `### PR-1 Baseline (measured)` recorded in this plan from a live DWH `gain` run, matching (or
      reconciled against) the documented baseline below.
- [ ] No manual networkx script is required to obtain the metric.
- [ ] Version bumped to **1.26.0** (`pyproject.toml` + `__init__.py` + `uv lock`); not tagged, issue
      not closed.

### PR-2 (fix)
- [ ] [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) descends into CTE child scopes and
      collects real source tables, with de-dup, schema-alias remap, and recursive-CTE handling.
- [ ] Indexing a CTE-wrapped INSERT…SELECT emits the expected raw `SELECTS_FROM` source row(s);
      direct-insert control unchanged; no duplicate rows.
- [ ] **(Amendment 3)** A CTE body referencing an aliased schema (`ba_tmp.real_table` with `ba_tmp→ba`
      configured) emits a `SELECTS_FROM` row with `dst_key = 'ba.real_table'` (alias remap applied to
      CTE-body tables, not just top-level), so the new edges land on the giant-component table ids.
- [ ] **(Amendment 1)** Multi-CTE `cte2 selects from cte1` does **not** emit a `SELECTS_FROM` row for
      the `cte1` alias (the flat `root.cte_sources.keys()` filter is applied to each CTE child scope);
      recursive-CTE behaviour is either covered by a `union_scopes` test or documented as a known gap.
- [ ] A `SELECTS_FROM`-specific regression guard asserts the raw rows and references this plan;
      it lives separately from the column-level #38 guard (coordinated with issue #40).
- [ ] No change to [`indexer.py`](../../src/sqlcg/indexer/indexer.py) emission path is required (the
      rows flow through the existing loop).
- [ ] All four perf-invariant suites pass **UNMODIFIED**; scaling guard flat at N and 2N; no new
      hot-path op introduced.
- [ ] Live-DWH re-acceptance recorded in the PR: `cte_source_gap_writes` moves toward 0; table-level
      islands move **147 → ~99** with **+~299 `SELECTS_FROM` edges** (research-doc §2 reproduction);
      no `gain` health regression; within the 3-minute budget.
- [ ] The existing tool-layer #38 fix (PR #57) still behaves correctly with `SELECTS_FROM` rows
      present (Step 2.0 confirmed).
- [ ] No `# TODO` in the happy path; any new helper has a grep-confirmed call site.
- [ ] Version bumped to **1.26.1** (`pyproject.toml` + `__init__.py` + `uv lock`); not tagged, issue
      not closed.

---

## Baseline (documented, from the research doc — PR-1 re-measures and confirms)

All measured on the DWH graph (schema v8, indexed sha `fdf1b551`), noise filter applied
(drop `_bck`, drop exact `ma.rtetl_delta`; schema aliases `ba_tmp→ba, da_tmp→da, ean_tmp→ean` applied
at index time):

| Quantity | Value | Edge type | Source |
|---|---|---|---|
| **`cte_source_gap_writes`** (producers with COLUMN_LINEAGE>0 AND SELECTS_FROM=0) — the PR-1 metric | **46** | mixed probe (COLUMN_LINEAGE present) → **SELECTS_FROM = 0** | research §4 point 3 |
| Table-level small islands (the headline) | **147** | `SELECTS_FROM` | research §2 |
| has-producer zero-incoming island members | 91 | `SELECTS_FROM` | research §3 |
| …of which bucket A (producer has zero `SELECTS_FROM` sources) | 71 | `SELECTS_FROM` | research §3 |

> The **PR-1 `gain` gate is `cte_source_gap_writes` = 46** (the falsifiable number PR-2 must move
> toward 0). The 147-island figure is the headline the user cares about but is a WCC count
> re-measured via the research-doc reproduction block at PR-2 acceptance, not necessarily surfaced in
> `gain`.

### Projected post-fix (research §5, measured synthesis — PR-2 must reproduce on live re-index)

| Metric | Before | After (projected) | Δ | Edge type |
|---|---|---|---|---|
| small table-level islands | 147 | **99** | **−48** | `SELECTS_FROM` |
| table-level edges | 2,930 | 3,229 | **+299** | `SELECTS_FROM` |
| giant component | 1,625 | 1,914 | +289 | `SELECTS_FROM` |
| reconnected members landing in giant | — | **40 / 42** | — | `SELECTS_FROM` |

The projection is a **measured synthesis** (the research doc rebuilt the graph adding the
`COLUMN_LINEAGE`-resolved sources as `SELECTS_FROM` edges and recomputed components), not a post-fix
measurement. It is a tight estimate because the fix performs the same CTE-body descent the column path
already does — but it is **confirmed only by PR-2's live re-index** (Step 2.4). Proof case to spot-check:
`ba.wtfe_bijverkoop_matrix` (currently `SELECTS_FROM dst: []`; its CTE body reads `ba.wtdh_artikel`,
`ba.wtfe_verkoopinfo`, … — all giant-component hubs).

---

## Versioning
- **PR-1 = minor (1.25.8 → 1.26.0):** adds a new metric surface to `gain`/`db info` (additive
  capability) per CLAUDE.md SemVer rule.
- **PR-2 = patch (1.26.0 → 1.26.1):** a bug fix only, no new surface.

## Risks and Mitigations
- **Risk:** the CTE-body descent introduces a per-statement perf regression. **Mitigation:** the walk
  is once-per-statement over an already-built scope, off the per-column hot-path; the four invariant
  suites must pass unmodified (Step 2.3); live-DWH wall-time checked against the 3-min budget (Step 2.4).
- **Risk:** duplicate `SELECTS_FROM` rows when a real table appears both top-level and in a CTE body.
  **Mitigation:** de-dup in `_real_tables` / confirm emission de-dup; integration test asserts no
  duplicate (Step 2.1/2.2).
- **Risk:** CTE-body tables land un-aliased and miss the giant component. **Mitigation:** apply
  `_apply_table_alias` on CTE-body tables exactly as the top-level path does (Step 2.1).
- **Risk:** the projection (147→99) does not reproduce on the live re-index. **Mitigation:** PR-1
  ships the metric first so the result is falsifiable; PR-2 acceptance is the live re-measurement, not
  CI-green. If the live number diverges materially, the PR records the actual delta and the residual is
  re-investigated (out-of-corpus / multi-producer cases) rather than the fix being claimed as
  delivering the projection.
- **Risk:** conflation with E8 / column-level numbers. **Mitigation:** the framing rule (§⚠) is
  encoded at the top; every number names its edge type.
- **Risk:** the existing tool-layer #38 fix regresses when `SELECTS_FROM` rows appear. **Mitigation:**
  Step 2.0 pre-flight confirms it (it reads the COLUMN_LINEAGE closure, not SELECTS_FROM).

### Blocking Questions
- **None outstanding.** Root cause, fix site, and fix class are confirmed by the research doc and by
  direct reads of [`base.py:604`](../../src/sqlcg/parsers/base.py#L604) (the CTE-alias filter at line
  629, no child-scope descent) and [`indexer.py:1372–1385`](../../src/sqlcg/indexer/indexer.py#L1372)
  (the `for src_table in stmt.sources` SELECTS_FROM emission). **STOP and escalate only if** PR-2's
  Step 2.0 shows the existing tool-layer fix regresses, or if PR-1's baseline diverges materially from
  46 (which would mean the lever population differs from the diagnosis and the projection cannot be
  trusted).

---

### PR-1 Baseline (measured)

Measured 2026-06-13, DWH graph sha `fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6`,
1335 files indexed, v1.26.0, from-scratch index into throwaway DB
`/tmp/pr1_baseline.duckdb`.

**`gain` output (Section G, Coverage block):**

```
Tables with catalog: 5788 / 6519 (89%)
Edge health (strict, column-level): 45510 / 53654 (85%)
Edge health (table-level, legacy): 46563 / 53654 (87%)
Edge health (scoped, excl. CTE/derived/temp): 36318 / 37421 (97%)
Write queries with zero outgoing lineage: 1120 / 2343
CTE-wrapped writes missing SELECTS_FROM source: 164
```

**Wall-time:** ~443 seconds (15:49:41 → 15:57:04 CEST). Exceeds the plan's
3-minute budget signal (plan §PR-2 Step 2.4) and the canonical 210–256s baseline.
Per `project_indexer_perf_baseline.md`, >4 min is not treated as an automatic
regression (WSL2 environment; catalog re-apply of 122 815 columns is included).
The figure should be re-checked on a clean run closer to PR-2.

**STOP condition:** The observed value (164) diverges materially from the
plan-documented baseline (46). The plan says to STOP and reconcile. An earlier
agent run on the same sha recorded 168; this fresh-from-scratch run records 164.
The ~4-row variance is likely non-deterministic parse outcomes across runs.
See `### Deviations` below for the discrepancy analysis.

### Deviations

#### Deviation 1: Live baseline is 164, plan documented 46
- **Reason**: The plan's 46 figure came from research §4 point 3, which counted
  the producer queries for the 71 bucket-A island members specifically. The
  implemented metric counts ALL write-kind queries with COLUMN_LINEAGE > 0 and
  SELECTS_FROM = 0 across the entire graph — a broader scope. The `target_table
  <> ''` filter makes no difference (0 rows excluded). Both measurements used
  the same graph sha `fdf1b551`.
- **Change**: The metric is implemented exactly as the plan's SQL spec states.
  The discrepancy is between the plan's documented number (scoped to island
  producers) and the metric's actual scope (all writes with the gap pattern).
- **Impact**: The plan's STOP condition is triggered. PR-2 must not proceed
  until the architect-planner reconciles whether 164 (full-scope) or 46
  (island-producer-scoped) is the correct baseline for the projection. The
  metric implementation itself is correct and complete; only the documented
  baseline number needs reconciliation. (An earlier agent run on the same sha
  recorded 168; this fresh-from-scratch run records 164 — the ~4-row variance
  is within parse non-determinism and does not change the stop condition.)
- **Date**: 2026-06-13
