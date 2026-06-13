# Feature Plan: #38 PR-3 — residual `SELECTS_FROM` source-extraction (subscope descent)

> **Status: REVIEWED** (plan-reviewer 2026-06-13: BLOCKED→resolved; corrected the §5 gate from
> 110→≤~10 to **110→≤~50** reconciling the planner's probe against the live-test classification
> [`issue38_pr2_live_acceptance.md`](../reports/issue38_pr2_live_acceptance.md), added `traverse_scope`
> module-level import discipline, named the ~48 literal + ~29 self-reload buckets as out-of-scope.
> Ready to implement as **v1.28.0** AFTER v1.27.0 merges. Not yet implemented.
> Predecessors: PR-1 (metric `cte_source_gap_writes`, v1.26.0) + PR-2 (`_real_tables` CTE-body
> descent, v1.26.1) — both shipped; see
> [`issue-38-selects-from-island-lever.md`](issue-38-selects-from-island-lever.md) for format/context.
> Measurement substrate: `/tmp/ab_pr2.duckdb` (master v1.26.1 A/B graph, DWH 1335 files) + faithful
> re-parse of the offending source files through the live `SnowflakeParser`.

## Summary
After PR-2, **110** write-kind queries (`INSERT`/`CREATE_TABLE`) on the live DWH resolve their
`COLUMN_LINEAGE` (the column path found the sources) but emit **zero `SELECTS_FROM` source rows**.
The measured root cause is uniform: [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) reads only
the **root** scope's `.tables` plus its flat `cte_sources`, and never descends into the **derived /
subquery child subscopes** (`INSERT … SELECT … FROM (SELECT … FROM real)`) nor the **`Subquery`
wrapper** of a `CREATE TABLE … AS (SELECT …)`. For these 110 statements the real source tables live
one or more scope levels below the root, so the root `scope.tables` is empty and `_real_tables`
returns `[]`. PR-3 extends `_real_tables` to descend the **already-built** scope tree
(`traverse_scope`) — once per statement, off the per-column hot path — recovering the source tables.

---

## ⚠ Framing rule — #38 is `SELECTS_FROM` (table-level), NOT `COLUMN_LINEAGE`

(Carried verbatim in spirit from PR-2; load-bearing for every number here.)
- This is a **`SELECTS_FROM` (table-level) source-extraction gap.** `COLUMN_LINEAGE` is used here
  **only as a probe** that the source tables were resolvable (the 110 all have it) — never as a
  substitute for a table-level count.
- Every number in this plan names its edge type. The gate metric is `cte_source_gap_writes`
  (a graph-wide row count), **not** the island count.

---

## Scope

### In Scope
- **PR-3 (the fix):** extend [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) to descend into
  **derived/subquery child subscopes** and the **CTAS `Subquery` wrapper** when building
  `stmt.sources`, so `INSERT … SELECT FROM (subquery)` and `CREATE TABLE … AS (SELECT …)` writes emit
  their `SELECTS_FROM` source rows. This is the table-level analogue of the descent the **column path
  already performs internally** (sg_lineage traverses subscopes — which is exactly why
  `COLUMN_LINEAGE` resolved while the table path emitted nothing).
- A new **once-per-statement perf counter** (`traverse_scope`) in
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py), asserting the descent
  stays flat at N and 2N (off the per-column hot path).
- A `SELECTS_FROM`-specific integration guard for the new shapes (subquery-source SELECT, CTAS-as-
  subquery, comment-prefixed INSERT, BA_TMP→BA alias remap on a subscope table).
- Live-DWH re-acceptance: the `cte_source_gap_writes` metric must drop substantially from **110**.
- Version bump (PR-3 = **minor → v1.28.0**; additive `SELECTS_FROM` coverage class, per CLAUDE.md).

### Non-Goals
- **The ~97 table-level islands are NOT the target.** Their missing-DDL source tables are already
  catalogued via the Snowflake `INFORMATION_SCHEMA` load (`HAS_COLUMN source='information_schema'`),
  so an islanded target with no in-corpus producer/consumer is **most likely dead code** (a removed or
  external ETL), not a coverage gap. Reconnecting islands would require inventing producers that do
  not exist in the corpus. **No island-reconnection work is planned.** The gate is the
  column-resolvable residual (`cte_source_gap_writes`), not the WCC island count.
- **Liquibase XML / CDATA DDL ingestion — explicitly OUT OF SCOPE** (user hard constraint). This is a
  SQL-parser-only lever; the 110 are all already-parsed SQL statements whose sources live in
  already-built scopes. No new ingestion source is introduced.
- The **744 graph-wide source-less writes that have NO column lineage** (out-of-corpus / literal-only
  SELECTs) — not column-resolvable, so not in this lever's population.
- **The ~48 sourceless literal inserts INSIDE the 110** (`INSERT … SELECT NULL AS …` with no FROM, that
  carry `COLUMN_LINEAGE` only via the `inferred_from_source_name` heuristic) — `traverse_scope` finds no
  scope table for them, so they stay in `cte_source_gap_writes` after PR-3. They inflate the metric
  numerator by construction; **excluding no-FROM writes from the metric (or renaming it) is a valid
  follow-up ticket, NOT a PR-3 defect.** Do not investigate them as a PR-3 failure.
- **The ~29 self-reload writes INSIDE the 110** (`INSERT INTO da_tmp.X SELECT … FROM DA.X`) — after
  `da_tmp→da` remap the source id equals the target id and is correctly dropped by the
  [`ansi_parser.py:455`](../../src/sqlcg/parsers/ansi_parser.py#L455) target-exclusion filter (a
  self-reload IS a self-loop). Unfixable by subscope descent and correct as-is; out of scope.
- **Recursive CTEs** (`WITH RECURSIVE`) — measured **0** in the residual (confirmed below). The
  PR-2-documented `union_scopes` known gap does not bite the live corpus; not addressed here.
- Any parse-time schema-CSV feeding into `exp.expand()`/`qualify()` (forbidden per CLAUDE.md).
- Target-resolution for targetless writes (a different coverage story; does not move this metric).

---

## 1. Measurement (done first — the fix is justified by what dominates)

All measured against `/tmp/ab_pr2.duckdb` (master **v1.26.1**, DWH sha as indexed, 1335 files,
schema aliases `ba_tmp→ba, da_tmp→da, ean_tmp→ean`, noise filter `_bck` + `ma.rtetl_delta`). The
metric population:

```sql
SELECT q.id, q.kind, q.file_path, q.target_table
FROM "SqlQuery" q
WHERE q.kind IN ('INSERT','MERGE','CREATE_TABLE','UPDATE')
  AND q.target_table <> ''
  AND EXISTS (SELECT 1 FROM "COLUMN_LINEAGE" cl WHERE cl.query_id = q.id)
  AND NOT EXISTS (SELECT 1 FROM "SELECTS_FROM" sf WHERE sf.src_key = q.id);
```

→ **`cte_source_gap_writes` = 110.** By kind: **63 `INSERT`, 47 `CREATE_TABLE`** (0 `MERGE`/`UPDATE`).

> **Measurement gotcha (recorded so the developer does not repeat it):** the `SqlQuery.sql` column is
> **truncated at 500 chars** in the graph (`max(length(sql)) == 500`). Re-parsing from `q.sql` is
> unreliable (44/98 throw `Expecting )`). The faithful classification below re-reads the **source
> files** and matches the write statement by **kind + target table** (NOT by `statement_index` — the
> stored index does **not** align with a fresh `_do_parse` split because Snowflake scripting/`USE
> SCHEMA` transforms re-segment statements; matching by index lands on `TruncateTable`/`Set`/`Command`
> for 67/110 and is a red herring).

### 1a. Coarse SQL-shape split (matches the brief exactly)

| Shape | Count | Note |
|---|---|---|
| Recursive `WITH RECURSIVE` | **0** | PR-2's documented `union_scopes` known gap does NOT bite the live corpus |
| Non-recursive `WITH` (CTE) | **12** | investigated below — NOT a flat-CTE miss; same subscope cause |
| Plain `INSERT … SELECT` / CTAS, no `WITH` | **98** | the prize; the new class |

### 1b. Faithful sqlglot-scope mechanism (re-parsed from source, matched by target)

For each of the 110, `build_scope(stmt)` was run on the real statement and `_real_tables(scope)` (the
**shipped v1.26.1** implementation) was invoked. Classified by **write-statement AST shape** and by
**where the source tables actually live in the scope tree**:

| Write shape | Count | Root `scope.tables` | Real sources live in… |
|---|---|---|---|
| `INSERT … SELECT` | **63** | empty (or CTE-alias only) | FROM-subscopes (derived tables, subqueries, intra-file temp sources) |
| `CREATE TABLE … AS SELECT` (`Create.expression` is `Select`) | **24** | empty | child subscopes of the SELECT |
| `CREATE TABLE … AS (SELECT …)` (`Create.expression` is `Subquery`) | **23** | empty | inside the `Subquery` wrapper's child scope |

**The single uniform mechanism:** in **all 110**, the source tables are **NOT in the root scope's
`.tables`**. They are reachable only by descending the scope tree. Proof — a `traverse_scope`-based
descent (collect every subscope's `.tables`, drop names in any subscope's `cte_sources`, drop the
target) **descends to source tables for 110 / 110** (`recovered=110, still_empty=0`).

> **Plan-review correction:** `still_empty=0` reflects what `traverse_scope` *descends to* — it does
> **not** account for the [`ansi_parser.py:455`](../../src/sqlcg/parsers/ansi_parser.py#L455)
> target-exclusion filter that re-suppresses self-reload sources after `da_tmp→da` alias remap (source
> id == target id → dropped). The live expected residual is therefore **~50, not ~10**: ~48 sourceless
> literal inserts + ~29 self-reload self-loops are out-of-scope-by-design, leaving ~33 genuinely
> recoverable. See §5 for the corrected, measured gate.

By **source identity** (which tables the descent finds), to confirm we are not chasing dead temp
plumbing:

| Source composition | Count | Meaning |
|---|---|---|
| All sources are **real (non-temp) tables** | **75** | genuine cross-table lineage currently missing — the prize |
| All sources are **intra-file temp tables** (created earlier in the file via `CREATE TEMP/CLONE`) | **21** | still valuable: temp namespacing connects these through to the real upstream |
| **Mixed** real + intra-file temp | **14** | partial real lineage missing |

So **75 + 14 = 89 of the 110** carry at least one **real (non-temp) source table** that is currently
invisible at the table level — this is the coverage prize. The 21 all-temp cases still benefit (the
temp source feeds the existing temp-namespacing chain to the real producer).

### 1c. Why the 12 `WITH` cases miss (the brief asked specifically)

They are **NOT** flat-CTE walk misses (PR-2's `cte_sources` pass handles flat CTEs correctly). They
miss for the **same subscope reason as the 98**: the CTE body's own source is itself a derived
table / subquery (`WITH cte AS (SELECT … FROM (SELECT … FROM real) x) …`) or the write wraps the whole
thing in a CTAS `Subquery`. The flat `cte_sources.values()` pass reaches the CTE child scope but not
that child's *own* derived subscopes. The general subscope descent (§2) subsumes the flat CTE pass —
it is not a special case.

### 1d. Concrete worked examples (developer fixtures should model these)

- **`INSERT … SELECT FROM <intra-file temp>`** —
  [`etl/sql/da/rtmop_after_purchase_copy_into.sql`](—): `CREATE TEMP TABLE DA_TMP.TMP_AFTER_PURCHASE
  LIKE DA_TMP.RTMOP_AFTER_PURCHASE; INSERT INTO DA_TMP.RTMOP_AFTER_PURCHASE SELECT … FROM
  DA_TMP.TMP_AFTER_PURCHASE a`. `scope.tables == []`, `_real_tables == []`; the source is one subscope
  down. `da_tmp→da` alias must apply.
- **CTAS-as-`Subquery`** —
  [`ddl/changelogs/IA-TABLEAU-OBT/TRANSACTIE_OBT.sql`](—): `CREATE OR REPLACE TABLE
  IA_TABLEAU."Transactie OBT" AS (SELECT … FROM IA_TABLEAU."Transactie" tra JOIN … 8 tables)`.
  `Create.expression` is `Subquery`; root `scope.tables == []`; the 8 real joined tables live in the
  Subquery's child scope.
- **Plain `INSERT … SELECT FROM real JOIN real`** —
  [`etl/sql/dim/wtda_inkoop_order.sql`](—): inner SELECT's `.args['from']` is `None` at the root
  scope; 5 real DA/BA tables (`ttcbs_order`, `ttaxi_ontvangst`, `wtdh_bouwmarkt`, …) are split across
  subscopes.
- **`WITH` + derived body** — one of the 12, e.g.
  [`etl/sql/dim/wtdh_klant_kaart.sql`](—): CTE bodies read `htkdb_customer_cards` / `wtdh_klant`
  through nested derived tables.

---

## 2. Design — the fix

**Site:** [`_real_tables(scope)`](../../src/sqlcg/parsers/base.py#L604) ONLY. No indexer change — the
recovered `stmt.sources` flow unchanged through the existing emission loop at
[`indexer.py:1372–1385`](../../src/sqlcg/indexer/indexer.py#L1372)
(`for src_table in stmt.sources: rows.selects_from_edges.append(...)`), de-duped on `(src_key,
dst_key)`.

**Algorithm (descend the already-built scope tree once):**
1. Keep Step 1 (root `scope.tables`, CTE-alias filtered) and Step 2 (flat `cte_sources` child pass)
   exactly as shipped in v1.26.1 — do not regress them.
2. Add Step 3: walk the scope subtree with `from sqlglot.optimizer.scope import traverse_scope`
   over **the root statement's scope** (the scope `_real_tables` is already handed), collecting each
   subscope's `.tables`. Build the CTE-alias filter set as the **union of every subscope's
   `cte_sources.keys()`** (so a CTE alias referenced as a "table" in another subscope is dropped, same
   discipline as PR-2's flat-key filter). De-dup against the existing `seen` set so a table reachable
   both top-level and via a subscope is emitted once.
3. Apply `_apply_table_alias(self._convert_table_expr_to_ref(table_expr))` on subscope tables exactly
   as the top-level and CTE-body paths do (so `ba_tmp.x → ba.x`, landing on giant-component ids).

> **Implementation note — `traverse_scope` vs manual recursion.** `traverse_scope(stmt)` yields all
> scopes (root + derived + subquery + CTE + union) for a statement in a single pass over the
> already-built tree. It does **not** call `build_scope`/`qualify`/`expand` — it walks the cached
> `Scope.subquery_scopes`/`cte_sources`/`union_scopes` links. The developer may instead recurse the
> `scope.subquery_scopes` + `scope.cte_sources.values()` + `scope.union_scopes` children manually if a
> tighter filter is wanted; either is once-per-statement. Confirm against sqlglot 30.6.0 that
> `traverse_scope` reaches the CTAS `Subquery` child (the §1d TRANSACTIE_OBT case proves it does —
> the descent recovered all 8 tables). **Pin the chosen call with a grep-confirmed single call site.**
>
> **IMPORT DISCIPLINE (plan-review, REQUIRED):** if `traverse_scope` is used, it MUST be imported at
> **module level** in [`base.py`](../../src/sqlcg/parsers/base.py) alongside `build_scope`/`qualify`
> (the existing module-level imports exist precisely so tests can patch
> `sqlcg.parsers.base.build_scope`/`.qualify`). The §3.3 scaling-guard counter patches
> `sqlcg.parsers.base.traverse_scope`, which only works if it is a module-level name — an inline import
> inside `_real_tables` would make that binding site unpatchable, exactly as CLAUDE.md documents for
> `build_scope`/`qualify`. If the manual-recursion path is chosen instead, the counter must target
> whatever module-level scope accessor it calls; pick one and keep the guard patchable.

### Why this is OFF the perf hot path (must be stated verbatim in the PR)

[`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) runs **once per statement** at
[`ansi_parser.py:435`](../../src/sqlcg/parsers/ansi_parser.py#L435), on the scope sqlglot **already
built** in the file-level `file_scopes` loop ([`ansi_parser.py:172`](../../src/sqlcg/parsers/ansi_parser.py#L172)).
`traverse_scope` is a pointer-walk over cached subscope links — it does **NOT** call any of the ops the
[CLAUDE.md performance invariants](../../CLAUDE.md#performance-invariants--do-not-remove-or-simplify)
protect. It does not touch: `exp.expand()` / `dependency_filter`, the per-column `body_scope` build,
the pure-literal skip, the `copy=False`/`trim_selects=False` `sg_lineage` kwargs, the INSERT
body-copy-once path, or the bulk/per-batch upsert paths. It is independent of column count: a wide
176-col SELECT has the same subscope count whether it has 1 column or 176. **All four perf-invariant
suites must pass UNMODIFIED.** Because this introduces a **new** once-per-statement scope op, §3.3 adds
a `traverse_scope` counter to the scaling guard (per the CLAUDE.md rule: "When adding a new hot-path op
that must be once-per-statement, add a counter for it there").

### Data models / API
No schema change. `stmt.sources` is already `list[TableRef]`; the fix only makes it non-empty for
subscope-sourced writes. No new method on the public surface (the descent lives inside `_real_tables`;
if a private helper is extracted, grep-confirm its single call site).

### Dependencies
None new (`traverse_scope` is already in `sqlglot.optimizer.scope`, same module as `build_scope`).

---

## 3. Implementation Steps (single PR — PR-3)

**Step 3.1** — Extend [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) with the subscope
descent (Design §2). Preserve Steps 1+2 unchanged. Build the CTE-alias filter as the union of all
subscopes' `cte_sources.keys()`. De-dup via the existing `seen` set. Apply `_apply_table_alias` on
subscope tables.
- Files: [`base.py`](../../src/sqlcg/parsers/base.py) (`_real_tables` only).
- Call site unchanged ([`ansi_parser.py:435`](../../src/sqlcg/parsers/ansi_parser.py#L435)); if a
  helper is extracted, grep-confirm its call site before PR opens.
- Acceptance: indexing `INSERT INTO s.b SELECT … FROM (SELECT x FROM s.a) d;` emits a `SELECTS_FROM`
  row `(query → s.a)`; `CREATE TABLE s.b AS (SELECT … FROM s.a JOIN s.c …)` emits rows for `s.a` and
  `s.c`; CTE-with-derived-body emits its real source; the PR-2 flat-CTE control still emits its row;
  no duplicate when a table appears both top-level and in a subscope.

**Step 3.2** — `SELECTS_FROM`-specific integration guard. New
`tests/integration/test_selects_from_subscope_source.py` (clearly named so a reader does not assume
the column-level #38 guard covers it; docstring references this plan per `developer.md` test-naming).
Assert the **raw `(src_key, dst_key)` rows** on the indexed graph for:
- `INSERT … SELECT FROM (SELECT … FROM real) d` → row to the real table.
- `CREATE TABLE t AS (SELECT … FROM real_a JOIN real_b)` (CTAS-as-`Subquery`) → rows to both.
- A comment-prefixed `/* … */ INSERT INTO … SELECT … FROM <subscope>` (the §1d shape) → still emits.
- **Alias remap:** `INSERT INTO ba.x SELECT … FROM (SELECT … FROM ba_tmp.real) d` with `ba_tmp→ba`
  configured → `dst_key = 'ba.real'`, **not** `'ba_tmp.real'`.
- A multi-CTE `cte2 selects from cte1` control → **no** `SELECTS_FROM` row for the `cte1` alias.
- The direct-insert + flat-CTE controls (PR-2 shapes) unchanged.

**Step 3.3** — Perf guard. Add a `traverse_scope` counter to
[`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)'s `HotOpCounters` +
`count_hot_ops` (patch at `sqlcg.parsers.base.traverse_scope` binding site, mirroring the
`build_scope`/`qualify` patch discipline at lines 104–113), and assert it stays **flat / once-per-
statement** at N and 2N (does not scale with column count). Run the four invariant suites UNMODIFIED;
confirm green. A red means an invariant broke — fix the fix, not the test.

**Step 3.4** — Live-DWH re-acceptance (delegated to a developer; **read-only** on
`/home/ignwrad/Projects/dwh`, never commit; the e2e harness is gitignored). Re-index DWH from scratch
into a throwaway DB and report the gates in §5. Box runs ~2m40s/index — within the 3-min budget;
confirm no regression. **A/B method: same 2-state sequential index** as PR-2 (index master-without-fix
→ record metric; index with-fix → record metric).

**Step 3.5** — Version: bump **1.27.x → 1.28.0** (see §6) in
[`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py),
`uv lock`. Do not tag, do not close the issue (user verifies + tags).

---

## 4. Test Strategy
- **Integration (PR-3):** real in-memory DuckDB via `Indexer().index_repo(..., use_git=False)`. Each
  §3.2 shape asserts the exact raw `SELECTS_FROM (src_key, dst_key)` rows on the indexed graph
  (observable output, not "no exception"). Fixtures must model the **live** shapes: comment-prefixed
  INSERT, CTAS-wrapped-in-`Subquery`, derived-table FROM, and a `ba_tmp→ba` aliased subscope source —
  not an idealized minimal case. (Mirrors the v1.21.0 dual-write lesson: a fixture that cannot exhibit
  the failure mode would pass while live stays broken.)
- **Perf (PR-3):** the four invariant suites pass UNMODIFIED; the new `traverse_scope` counter is flat
  at N and 2N; scaling guard green.
- **Live-DWH (PR-3):** the metric gate (§5), reported as a 2-state A/B delta with a residual
  breakdown.
- **Full suite:** `uv run pytest` green; `uv run pyright` + `uv run ruff check src tests` clean.

---

## 5. Acceptance Criteria

- [ ] [`_real_tables()`](../../src/sqlcg/parsers/base.py#L604) descends derived/subquery subscopes and
      the CTAS `Subquery` wrapper, with de-dup, schema-alias remap, and the union-of-all-subscopes
      `cte_sources` alias filter; Steps 1+2 (v1.26.1 behaviour) preserved.
- [ ] Indexing each §3.2 shape emits the expected raw `SELECTS_FROM` row(s); the direct-insert and
      flat-CTE PR-2 controls are unchanged; no duplicate rows; the multi-CTE control emits no row for
      the inner CTE alias.
- [ ] Alias remap applies to subscope tables: an aliased subscope source (`ba_tmp.real`, `ba_tmp→ba`
      configured) emits `dst_key = 'ba.real'`.
- [ ] All four perf-invariant suites pass **UNMODIFIED**; the new `traverse_scope` counter is flat at
      N and 2N; no per-column `build_scope`/`qualify`/`sg_lineage`/`expand`/body-copy introduced.
- [ ] **Metric gate (graph-wide, off the 110 base) — the falsifiable gate.** `cte_source_gap_writes`
      (write-kind, `COLUMN_LINEAGE > 0`, `SELECTS_FROM = 0`) drops **substantially** from **110** on a
      2-state A/B DWH re-index. **Projected: 110 → ≤ ~50** (corrected at plan-review — the planner's
      "recovered 110/110, still_empty=0" counted what `traverse_scope` *descends to*, NOT what survives
      the target-exclusion filter at [`ansi_parser.py:455`](../../src/sqlcg/parsers/ansi_parser.py#L455)
      and lands as a `SELECTS_FROM` row). Of the 110: **~48 are sourceless literal inserts**
      (`INSERT … SELECT NULL AS …` — no FROM; `COLUMN_LINEAGE` exists only via the
      `inferred_from_source_name` heuristic, no scope table at any depth) and **~29 are self-reload
      writes** (`INSERT INTO da_tmp.X SELECT … FROM DA.X` — after `da_tmp→da` alias canonicalisation the
      source id equals the target id, so it is correctly removed by the `ansi_parser.py:455`
      target-exclusion filter; a self-reload IS a self-loop and must drop). These **~77 are
      out-of-scope-by-design** and remain in the metric by construction. The genuinely subscope-recoverable
      population is the remaining **~33** (12 `WITH` + ~21 subquery/derived FROM). **Before claiming
      delivery the developer MUST measure the three sub-populations (literal / self-reload /
      subscope-recoverable) explicitly from the live residual — not assume the split — and record the
      breakdown.** A residual **above ~50 that cannot be attributed to the literal/self-reload buckets**
      is a STOP — re-investigate before claiming
      delivery. (Decoupled from any island count, per PR-2.)
- [ ] **Edge-coverage corollary (directional, not a hard gate):** `gain` table-health %
      (strict / scoped / catalogued) shows **no regression** and the `SELECTS_FROM` edge total rises
      by ≥ the number of recovered writes (≥ ~89 real-source writes × their source count). Record the
      `SELECTS_FROM` row-count delta.
- [ ] No island-reconnection work performed; non-goals (Liquibase XML, islands, recursive CTE)
      respected.
- [ ] No `# TODO` in the happy path; any new helper has a grep-confirmed call site.
- [ ] Within the 3-minute index budget (~2m40s baseline; the descent is once-per-statement,
      negligible).
- [ ] Version bumped to **1.28.0** (`pyproject.toml` + `__init__.py` + `uv lock`); not tagged, issue
      not closed.

---

## 6. PR sequencing + versioning

- **Predecessors merged:** PR-1 (v1.26.0, metric) + PR-2 (v1.26.1, CTE-body descent).
- **In-flight (must merge first):** the v1.27.0 recall-metric PR (per the user). PR-3 branches off
  master **after** v1.27.0 lands so its A/B baseline includes the recall metric and the 110 figure is
  re-confirmed on the then-current master (re-run the §1 query; if v1.27.0 shifts the 110 materially,
  record the new baseline — the *mechanism* is unchanged).
- **PR-3 = v1.28.0 (minor).** It materially extends `SELECTS_FROM` coverage (a new resolution class),
  so it is a minor bump per the CLAUDE.md "additive capability → minor" rule, consistent with PR-1's
  minor bump for a coverage surface. The developer confirms the exact from→to against the merged
  v1.27.0 number at branch time.

---

## 7. Risks and Mitigations
- **Risk:** the subscope descent re-introduces a per-statement perf cost that scales with subquery
  depth/count on pathological files. **Mitigation:** it walks the already-built scope tree once;
  §3.3 pins a `traverse_scope` counter flat at N and 2N; live wall-time checked against the 3-min
  budget (§3.4). It is independent of column count (the historical O(N²) axis).
- **Risk:** duplicate `SELECTS_FROM` rows when a table appears both top-level and in a subscope.
  **Mitigation:** the existing `seen` set in `_real_tables` plus the `(src_key, dst_key)` emission
  de-dup; integration test asserts no duplicate.
- **Risk:** subscope tables land un-aliased and miss the giant component. **Mitigation:** apply
  `_apply_table_alias` on subscope tables (Step 3.1); acceptance criterion pins `ba_tmp→ba`.
- **Risk:** a CTE alias referenced as a table inside another subscope leaks as a phantom source.
  **Mitigation:** filter against the **union of all subscopes' `cte_sources.keys()`** (Design §2),
  the same discipline PR-2 used for the flat-CTE pass; multi-CTE control test pins it.
- **Risk:** the projected 110 → ≤~50 does not reproduce live. **Mitigation:** the metric shipped in
  PR-1 makes it falsifiable; PR-3 acceptance is the live re-measurement with the **measured** three-way
  residual breakdown (literal / self-reload / subscope-recoverable, §5), not CI-green. A residual above
  ~50 not attributable to the literal/self-reload buckets is a STOP, not a silent pass.
- **Risk:** conflation with the island count or with `COLUMN_LINEAGE` numbers. **Mitigation:** the
  framing rule (§⚠) and the explicit non-goal that islands are out of scope (likely dead code,
  already catalogued).

### Blocking Questions
- **None outstanding.** Mechanism (subscope descent), fix site (`_real_tables`), and recovery rate
  (110/110 via `traverse_scope`) are all directly measured against `/tmp/ab_pr2.duckdb` + faithful
  source re-parse through the live `SnowflakeParser`.
- **STOP and escalate only if** PR-3's live re-index leaves a `cte_source_gap_writes` residual that is
  NOT explained by noise-filtered sources or pure self-reference (i.e. an undiscovered mechanism).
