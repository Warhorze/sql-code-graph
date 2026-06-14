# Bugfix Plan: CREATE catch-all mislabels non-table DDL as CREATE_TABLE

**Status:** DRAFT (plan-reviewer gates before implementation)
**Branch:** `plan/create-kind-mislabel` (rebased onto current `origin/master`
tip `e246cbc`; master is v1.33.1)
**Version bump:** PATCH → v1.33.2 (reconcile at merge — several PRs in flight)
**Owner:** architect-planner
**Type:** Confirmed, live-verified bug fix (single surgical switch-arm edit)

> Anchors re-confirmed against current master tip `e246cbc`. The session evidence
> cited HEAD `3a33ed7a`; master has since advanced through PRs #154/#155/#156.
> `_classify` and all consumer line numbers below are from `e246cbc`. The defect
> itself is identical and the catch-all is still at base.py:548.

## Summary

The `exp.Create` arm of [`_classify`](../../src/sqlcg/parsers/base.py) has a
catch-all that returns `QueryKind.CREATE_TABLE` for every `Create.kind` it does
not explicitly handle. As a result EVERY non-table, non-view, non-proc/func
`CREATE` (SEQUENCE, STAGE, FILE FORMAT, SCHEMA, INDEX, …) is stored with
`SqlQuery.kind = CREATE_TABLE`, and — worse — the parser promotes its target into
`defined_tables`, fabricating a phantom `SqlTable` node and leaking the wrong kind
into `analyze impact`.

## The bug (live-verified)

`src/sqlcg/parsers/base.py` `_classify`, `exp.Create` inner match
(current master `e246cbc`, lines 539–548):

```python
case exp.Create():
    match stmt.kind:
        case "TABLE":
            return QueryKind.CREATE_TABLE
        case "VIEW":
            return QueryKind.CREATE_VIEW
        case "PROCEDURE" | "FUNCTION":
            return QueryKind.CREATE_PROC
        case _:
            return QueryKind.CREATE_TABLE  # Default to table   ← BUG (line 548)
```

**Live evidence (DWH, HEAD `3a33ed7a`, 1306 files):** 11 `CREATE SEQUENCE`,
5 `CREATE STAGE`, 9 `CREATE FILE FORMAT` statements all stored as
`SqlQuery.kind = CREATE_TABLE`. They leak into `analyze impact` output, e.g.
`analyze impact ba.wsdh_klant_s2 --raw` renders
`…/BA-TABLES/WTDH_KLANT.sql kind=CREATE_TABLE` for what is actually a
`CREATE SEQUENCE BA.WSDH_KLANT_S2` at `ddl/changelogs/BA-TABLES/WTDH_KLANT.sql:50`;
`ma.msstg_ingest` similarly for a `CREATE STAGE`. 14 such rows carry SELECTS_FROM
edges. This is the real defect behind validation bug #7 (whose titled
`SET var=(SELECT…)` shape was DISPROVEN — `SET` correctly classifies as `OTHER`).

## Scope

### In Scope
- Replace the `case _:` catch-all return value in the `exp.Create` arm of
  `_classify` so non-table CREATE kinds are NOT classified as `CREATE_TABLE`.
- A fixture-backed acceptance test asserting stored `SqlQuery.kind` is correct for
  SEQUENCE / STAGE / FILE FORMAT plus a real CREATE TABLE control, and that
  `analyze impact` no longer renders non-table DDL as `CREATE_TABLE`.
- A coverage no-regression assertion (write-kind metric — see blast radius §7).
- Version bump to v1.33.2 + lockfile.

### Non-Goals
- Adding dedicated `CREATE_SEQUENCE` / `CREATE_STAGE` / `CREATE_FILE_FORMAT` enum
  values (fork (b), rejected — see §Recommended fix).
- Any change to lineage extraction, the hot loop, or the four frozen perf suites.
- Backfilling existing graphs — reindex is the migration path (no backward compat).
- Re-litigating the disproven `SET` shape from validation bug #7.

## Blast-radius map — every `CREATE_TABLE` consumer

Verified via `grep -rn "CREATE_TABLE"` on `src/` (excluding `__pycache__`) at tip
`e246cbc`. The catch-all only ever produces kinds for **SEQUENCE / STAGE / FILE
FORMAT / SCHEMA / INDEX** (sqlglot `Create.kind` values confirmed in the table
below). Reclassifying those to `OTHER` affects each consumer as follows.

| # | Consumer (file:line @ `e246cbc`) | Gate | Effect of catch-all → OTHER |
|---|---|---|---|
| 1 | [`ansi_parser.py:242`](../../src/sqlcg/parsers/ansi_parser.py) — `if query_node.kind in ("CREATE_TABLE","CREATE_VIEW"): out.defined_tables.append(query_node.target)` | kind == CREATE_TABLE | **DESIRED FIX.** A `CREATE SEQUENCE`/`STAGE`/`FILE FORMAT` currently has a `target` (set unconditionally for any `exp.Create` at ansi_parser.py:457–458) and is promoted into `defined_tables` → fabricates a **phantom `SqlTable` node**. Reclassifying to OTHER stops the phantom-table fabrication. No legitimate table is affected (all real tables have `Create.kind == "TABLE"`, handled explicitly). |
| 2 | [`snowflake_parser.py:781`](../../src/sqlcg/parsers/snowflake_parser.py) — same `defined_tables.append` (main statement loop) | kind == CREATE_TABLE | Same as #1, Snowflake path. Stops phantom table for the live SEQUENCE/STAGE/FILE-FORMAT statements. DESIRED. |
| 3 | [`snowflake_parser.py:941`](../../src/sqlcg/parsers/snowflake_parser.py) — same `defined_tables.append` (extracted-DML / scripting path) | kind == CREATE_TABLE | Same as #2 for dynamic/scripting-extracted statements. DESIRED. |
| 4 | [`aggregator.py:93`](../../src/sqlcg/lineage/aggregator.py) — DDL-column catalog (`ddl_columns_by_bare`) | kind in (CREATE_TABLE,CREATE_VIEW) **AND** `defined_columns` | No change in practice: SEQUENCE/STAGE/FILE FORMAT have **no `defined_columns`**, so they never entered this map even while mislabeled. Reclassification is a no-op here. SAFE. |
| 5 | [`aggregator.py:107`](../../src/sqlcg/lineage/aggregator.py) — CLONE-target capture (`_clone_targets`) | kind == CREATE_TABLE **AND** `clone_source is not None` | No change: a SEQUENCE/STAGE/FILE FORMAT has no `clone_source`. SAFE. |
| 6 | [`indexer.py:1538`](../../src/sqlcg/indexer/indexer.py) — `defined_by_query` map for HAS_COLUMN emission | kind in (CREATE_TABLE,CREATE_VIEW) **AND** `defined_columns` | No change: same `defined_columns` guard as #4. SAFE. |
| 7 | [`coverage.py:34`](../../src/sqlcg/cli/coverage.py) — `_WRITE_KINDS = ("INSERT","MERGE","CREATE_TABLE","UPDATE")` → feeds `zero_edge_write_queries`, `total_write_queries`, resolvable-write metrics | kind IN _WRITE_KINDS | **METRIC SHIFT (intended correction).** The 14 mislabeled non-table CREATEs currently count as write-kind queries. Most have zero COLUMN_LINEAGE (DDL with no SELECT body) and so currently **inflate `zero_edge_write_queries`** (false "unresolved write" gap). After fix they classify as OTHER and drop out of the write population — `total_write_queries` and `zero_edge_writes` both decrease. This is a *correction*, not a regression: a CREATE SEQUENCE is genuinely not a write-with-lineage. See acceptance §AC4. |
| 8 | [`analyze.py:317`](../../src/sqlcg/cli/commands/analyze.py) — `impact` reads `q.kind AS kind` directly from `SqlQuery` and prints it | reads stored kind | **DESIRED FIX surfaces here.** The live `kind=CREATE_TABLE` leak for the SEQUENCE/STAGE rows is this exact query echoing the stored kind. After reindex these render `kind=OTHER` (and, per #1–#3, no longer have a fabricated target table on the dst side). No code change needed in analyze.py — it is a pure read of the corrected stored value. |
| 9 | [`base.py:239`, `:525`](../../src/sqlcg/parsers/base.py) — docstrings listing valid kinds | doc only | No behavioural effect. Optionally update prose. |

**Net effect of the fix:** items #1/#2/#3/#8 are the *intended corrections*
(stop phantom tables + stop wrong kind in impact). Items #4/#5/#6 are provably
no-ops (the `defined_columns`/`clone_source` co-guards already excluded these
statements). Item #7 is an intended metric correction with a no-regression
assertion. **No consumer drops a legitimate table node or edge** — the safety
property rests on the fact that every genuine table carries `Create.kind ==
"TABLE"` (handled explicitly), so the catch-all never touches a real table.

## `Create.kind` values: what falls through today, and which are tables

Measured directly with sqlglot (snowflake dialect) on this master tip:

| SQL | `Create.kind` | Handled by | Genuinely a table? |
|---|---|---|---|
| `CREATE TABLE t (a int)` | `"TABLE"` | explicit `case "TABLE"` | YES (unchanged) |
| `CREATE TRANSIENT TABLE t (a int)` | `"TABLE"` | explicit `case "TABLE"` | YES (unchanged) |
| `CREATE TABLE t AS SELECT …` (CTAS) | `"TABLE"` | explicit `case "TABLE"` | YES (unchanged) |
| `CREATE OR REPLACE TABLE t (a int)` | `"TABLE"` | explicit `case "TABLE"` | YES (unchanged) |
| `CREATE VIEW v AS SELECT …` | `"VIEW"` | explicit `case "VIEW"` | view (unchanged) |
| `CREATE PROCEDURE p() …` | `"PROCEDURE"` | explicit `case "PROCEDURE"\|"FUNCTION"` | no (unchanged) |
| `CREATE SEQUENCE ba.s …` | `"SEQUENCE"` | **catch-all** | **NO** → must change |
| `CREATE STAGE ma.stg` | `"STAGE"` | **catch-all** | **NO** → must change |
| `CREATE FILE FORMAT ff TYPE=CSV` | `"FILE FORMAT"` | **catch-all** | **NO** → must change |
| `CREATE SCHEMA s` | `"SCHEMA"` | **catch-all** | **NO** → must change |
| `CREATE INDEX i ON t(a)` | `"INDEX"` | **catch-all** | **NO** → must change |

**Conclusion:** every variant that is genuinely a table (incl. TRANSIENT,
CTAS, OR REPLACE) carries `Create.kind == "TABLE"` and is handled by the explicit
arm. The catch-all catches ONLY non-table kinds. Flipping the catch-all to `OTHER`
cannot reclassify a real table — this is the safety property the fix relies on.

## Recommended fix — (a) catch-all returns `QueryKind.OTHER`

**Chosen: (a).** Change `_classify`'s `exp.Create` catch-all from
`return QueryKind.CREATE_TABLE  # Default to table` to
`return QueryKind.OTHER  # non-table CREATE (sequence/stage/file format/etc.)`.

Rationale:
- The blast-radius map proves no legitimate table node/edge is dropped: the only
  affected statements are non-table CREATEs, and the table-fabrication consumers
  (#1–#3) are exactly the ones we *want* to stop firing for them.
- `OTHER` is the existing, already-consumed terminal kind for non-classified
  statements (the outer `case _:` at base.py:551 already returns it; `SET`
  already lands there). No new enum value, no new switch arm to thread through
  every consumer → **no new method/enum value requiring a grep-confirmed call
  site** (the CLAUDE.md invariant), so the change carries zero new-symbol surface.
- Matches the validation report's suggestion and is the minimal surgical edit.

**Rejected: (b) dedicated kinds (CREATE_SEQUENCE/CREATE_STAGE/CREATE_FILE_FORMAT).**
Heavier: adds ≥3 `QueryKind` enum values, each needing a grep-confirmed consumer
(none exists today — they would be write-only dead values, violating CLAUDE.md's
"every new enum value needs a grep-confirmed call site"), and forces a decision
about whether they enter `_WRITE_KINDS`, `defined_tables`, etc. No current
consumer distinguishes a sequence from a stage; until one does, the distinction is
unused state. If a future feature needs object-type granularity, (b) can be
revisited then with a real call site.

> **Design fork for maintainer (non-blocking):** if there is appetite to model
> non-table SQL objects (sequences/stages/file formats) as first-class graph
> nodes for catalog completeness, fork (b) becomes justified. Absent that
> requirement, (a) is correct. **Recommend (a) now; defer (b) to a feature with a
> concrete consumer.**

## Implementation Steps

### Phase 1: Fix the classification

**Step 1.1** — Edit the catch-all in `_classify`.
- Files affected: [`src/sqlcg/parsers/base.py`](../../src/sqlcg/parsers/base.py)
  (the `exp.Create` inner `case _:` at line ~548).
- Change: `return QueryKind.CREATE_TABLE  # Default to table`
  → `return QueryKind.OTHER  # non-table CREATE (sequence/stage/file format/etc.)`
- Optionally refresh the two docstrings (base.py:239, :525) listing valid kinds.
- Acceptance: `_classify` on a SEQUENCE/STAGE/FILE FORMAT `exp.Create` returns
  `QueryKind.OTHER`; on a TABLE / CTAS / TRANSIENT TABLE returns
  `QueryKind.CREATE_TABLE`.

**Perf-invariant statement (required by CLAUDE.md):** this is a pure switch-arm
return-value edit inside `_classify`, which runs **once per statement** during
classification. It adds **no** per-column op, no `qualify`/`expand`/`build_scope`
call, no copy, and no loop. None of the four frozen perf invariants
([`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
[`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)) is
touched. The change introduces no hot-path op; scaling-guard op counts are
unaffected. These suites must remain byte-unchanged.

### Phase 2: Version bump

**Step 2.1** — Bump version to v1.33.2.
- [`pyproject.toml`](../../pyproject.toml) `version = "1.33.2"`,
  [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) `__version__ = "1.33.2"`,
  then `uv lock`.
- PATCH per SemVer (bug fix, no new surface). Reconcile the exact number at merge
  if a colliding PR lands first.

## Test Strategy

> Tests named `test_<unit>_<scenario>_<expected>`, docstring links this plan.
> Each test asserts observable output, never "no exception raised".

- **Unit (classification):** parse a SQL string containing `CREATE SEQUENCE`,
  `CREATE STAGE`, `CREATE FILE FORMAT`, and a control `CREATE TABLE` (snowflake
  dialect); assert `_classify` (or the resulting `QueryNode.kind`) is `OTHER` for
  the three non-table statements and `CREATE_TABLE` for the control. This is the
  behaviour that was wrong. (Pattern: [`tests/unit/test_parser.py`](../../tests/unit/test_parser.py).)
- **Unit (no phantom table):** parse a file whose only CREATE is `CREATE SEQUENCE`;
  assert `ParsedFile.defined_tables` does **not** contain the sequence name — i.e.
  no phantom `SqlTable` is fabricated (pins blast-radius items #1–#3 behaviourally).
- **Integration (indexed graph + impact):** index a fixture containing a real
  `CREATE TABLE` plus a `CREATE SEQUENCE`/`CREATE STAGE` that have SELECTS_FROM
  edges; query `SqlQuery.kind` and assert the non-table rows are `OTHER` not
  `CREATE_TABLE`; run the `analyze impact` query (analyze.py:317 shape) and assert
  no impacted producer for the sequence renders `kind=CREATE_TABLE`, and that no
  `SqlTable` row exists for the sequence/stage/file-format name. (Pattern:
  `tests/integration/`.)
- **Coverage no-regression:** on the same fixture, assert the non-table CREATEs are
  **not** counted in the write-kind population — `total_write_queries` reflects
  only the real INSERT/MERGE/CREATE TABLE/UPDATE statements, and the sequence/stage
  rows do not inflate `zero_edge_write_queries`. (Pins blast-radius item #7.
  Pattern: [`tests/unit/test_resolvable_write_col_edges_metric.py`](../../tests/unit/test_resolvable_write_col_edges_metric.py).)

## Acceptance Criteria

- [ ] **AC1** A fixture with `CREATE SEQUENCE`, `CREATE STAGE`, `CREATE FILE FORMAT`
      stores each as `SqlQuery.kind = OTHER` (NOT `CREATE_TABLE`) after indexing.
- [ ] **AC2** The same fixture's real `CREATE TABLE` control still stores
      `SqlQuery.kind = CREATE_TABLE` (no regression on genuine tables; CTAS and
      TRANSIENT TABLE likewise classify as CREATE_TABLE).
- [ ] **AC3** `analyze impact` on a table consumed by a `CREATE SEQUENCE`/`STAGE`
      no longer renders that producing statement as `kind=CREATE_TABLE`; and no
      phantom `SqlTable` node exists for the sequence/stage/file-format name.
- [ ] **AC4** Coverage metric no-regression: the non-table CREATEs are excluded
      from `_WRITE_KINDS` population — `total_write_queries` counts only genuine
      writes and `zero_edge_write_queries` is not inflated by the non-table DDL.
- [ ] **AC5** All four frozen perf suites pass unchanged; full suite green;
      `pyright` + `ruff` clean.

## Rollout / Rollback

- **Rollout:** merge to master, reindex. Existing graphs keep the wrong kind until
  reindexed — **reindex is the migration path** (no backward-compat shim, per
  CLAUDE.md). Tag v1.33.2 after merge (reconcile number with in-flight PRs).
- **Rollback:** single-line revert of the catch-all return value; no schema
  change, no data migration required.

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| A `Create.kind` we think is non-table is actually a table on some dialect | Low | Measured the full DWH-relevant set on snowflake dialect (§values table); all real tables emit `"TABLE"`. Control assertion AC2 + CTAS/TRANSIENT coverage guards this. |
| Coverage metric shift read as a regression in a dashboard | Low | AC4 documents the shift as a correction; the PR body must state `total_write_queries` legitimately decreases by the count of reclassified non-table CREATEs. |
| Some downstream report relies on the wrong phantom table nodes | Very low | Blast-radius map enumerates all consumers; none consumes the phantom positively — they are noise (the live `analyze impact` leak is the symptom, not a feature). |

## Blocking Questions

None. The fix is unambiguous given the measured `Create.kind` values and the
blast-radius map. The only fork (model non-table objects as nodes, fork (b)) is a
**future feature**, not a blocker for this bug fix — recommend (a) now.
