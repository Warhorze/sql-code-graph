# E8 revival — temp-chain `dynamic_source` multi-key COLUMN_LINEAGE fix

Status: NEEDS-REVIEW

**Author role:** architect-planner
**Date:** 2026-06-14
**Branch (plan):** `plan/e8-revival-column-lineage`
**Owner doc:** this file (`architect-planner` owns `plan/sprints/<feature>.md` per [`CLAUDE.md`](../../CLAUDE.md))
**Target version bump:** `1.28.3 → 1.29.0` (**minor** — new resolution surface, no break; SemVer rule in [`CLAUDE.md`](../../CLAUDE.md) "Releasing")

---

## 0. One-paragraph summary

Revive the parked E8 fix (PR #111, branch `origin/fix/e8-temp-chain`): in the CTAS
`sources_map` registration block of [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py),
register each temp/CTAS target under **all three key forms** (bare, post-alias-qualified,
pre-alias-qualified) so `exp.expand()` resolves Snowflake `USE SCHEMA`-prefixed temp references
through the chain instead of missing the lookup and emitting `col_lineage_skip:dynamic_source`.
The prior A/B measured **+1,728 COLUMN_LINEAGE edges** on the 1,335-file DWH corpus. The fix is
**off the perf hot-path** (it adds two dict writes per CTAS target; it does not feed schema into
`exp.expand()`/`qualify()`, does not touch the per-column loop, and does not change the SELECTS_FROM
emission path). It is now gate-able by the `resolvable_write_col_edges` monotone recall counter
(shipped v1.27.0), which did not exist when E8 was parked on the wrong (island) ruler.

---

## 1. What E8 is, what it was, why it was disabled

### 1.1 The defect (the `col_lineage_skip:dynamic_source` / "E8" class)

On the Snowflake `USE SCHEMA` path, `_qualify_bare_tables` prefixes bare table names with the
active schema **before** `_parse_statement` runs (e.g. `tmp_x → ba_tmp.tmp_x`, and after the
`ba_tmp→ba` schema-alias fold, the CREATE target's `db` becomes `ba`). A subsequent statement that
reads the temp (`INSERT … FROM tmp_x`) therefore carries a *qualified* `exp.Table` node in its AST.
`exp.expand()` normalises that node to a qualified key (`ba_tmp.tmp_x` or `ba.tmp_x`) and looks it
up in `sources_map`.

The current master registration block writes only the **bare** key:

- [`ansi_parser.py:222-232`](../../src/sqlcg/parsers/ansi_parser.py#L222) (master `ecbe60c`):
  ```python
  # Register CTAS bodies in sources_map for downstream temp table references
  if isinstance(stmt, exp.Create):
      _expr = stmt.expression
      if isinstance(_expr, exp.Subquery):
          _expr = _expr.this
      if isinstance(_expr, exp.Select) and query_node.target:
          target_name = (query_node.target.name or "").lower()
          if target_name:
              sources_map[target_name] = _expr   # bare key ONLY
  ```

Bare key `tmp_x` ≠ normalised qualified node `ba_tmp.tmp_x` → **lookup miss** → `exp.expand()`
cannot inline the temp body → the chain breaks → `col_lineage_skip:dynamic_source` and **no
column lineage past the temp link**. This is the "E8" class.

### 1.2 What E8 was (the fix — verbatim from `origin/fix/e8-temp-chain`)

Two commits — `a841c4b` (post-alias qualified key) and `3a31953` (pre-alias key for the schema-alias
case) — replace the single bare write with three writes:

```python
if isinstance(_expr, exp.Select) and query_node.target:
    target_name = (query_node.target.name or "").lower()
    if target_name:
        # Bare key — required for the non-qualified / ANSI path.
        sources_map[target_name] = _expr
        # Post-alias qualified key (e.g. "ba" when ba_tmp→ba aliased): the
        # Snowflake USE SCHEMA path where _qualify_bare_tables sets db on the temp.
        target_db = (query_node.target.db or "").lower()
        if target_db:
            sources_map[f"{target_db}.{target_name}"] = _expr
        # Pre-alias key from the raw AST db: when ba_tmp→ba is aliased the CREATE
        # target is "ba" but _qualify_bare_tables in later statements still uses the
        # raw USE SCHEMA name "ba_tmp" → exp.expand() normalises "ba_tmp.tmp_x" and
        # needs a matching "ba_tmp.tmp_x" key (the AST db).
        stmt_target = stmt.this  # exp.Table node (pre-alias)
        if isinstance(stmt_target, exp.Table):
            ast_db = (stmt_target.db or "").lower()
            if ast_db and ast_db != target_db:
                sources_map[f"{ast_db}.{target_name}"] = _expr
```

Critically, **only the `exp.expand()` lookup key changes**. The graph-node identity is unchanged:
the namespaced `<rel>::<db>.<name>` `role='temp'` key produced via `_lineage_node_to_table_ref`
is untouched, so the v1.21.1 dual-write footgun is **not** reopened (the E8 unit test
`test_no_bare_tmp_table_role_in_defined_tables` pins this).

### 1.3 Why it was disabled — file:line + postmortem evidence

E8 was **never merged**; PR #111 was parked open. The postmortem
(`git show origin/docs/e8-postmortem-and-corrections:plan/reports/e8_temp_chain_postmortem.md`)
records:

> "PR #111 parked unmerged. The code is correct (3 review concerns answered …; gates green
> 979/14-perf/21-namespacing). It was **not worth merging**: ~11-island gain + 1pp strict
> regression do not justify the hot-path 3-key `sources_map` registration complexity."

The disablement was a **judgement error on the ruler, not a correctness or perf failure**:

- E8 was judged on **SELECTS_FROM-WCC island count** (147 → 147). The remeasure
  (`git show origin/research/e8-column-vs-island-remeasure:plan/research/e8_column_vs_island_remeasure.md`)
  proves this is the **wrong ruler**: E8 only feeds `sources_map` → `exp.expand()` on the
  COLUMN_LINEAGE path; it structurally **cannot** move SELECTS_FROM-WCC (the SELECTS_FROM emission
  path is populated before `sources_map` is consulted — confirmed at
  [`ansi_parser.py:239`](../../src/sqlcg/parsers/ansi_parser.py#L239) `out.referenced_tables.extend(query_node.sources)`,
  independent of the CTAS body registration above it).
- On its **correct** ruler (COLUMN_LINEAGE), the remeasure showed **+1,728 COLUMN_LINEAGE edges**
  (53,162 → 54,890), **+1,080 strict-good**, **+490 scoped-good** on the same 1,335-file corpus.
- The 1pp strict-% dip (84%→84% in remeasure; 84%→83% in the older postmortem) is **denominator
  growth, not lost resolution**: the fix surfaces more candidate edges (+1,728) than it strict-resolves
  (+1,080); the new edges legitimately land on temp/CTE intermediate dst nodes (which the *scoped*
  metric excludes by design). This is expected and acceptable, not a regression of existing edges.

**Why parking no longer holds** (per `sql_only_coverage_lever_post_pr2.md` §3): the recall lever now
has a **falsifiable monotone metric** — `resolvable_write_col_edges`, shipped v1.27.0
([`coverage.py:359`](../../src/sqlcg/cli/coverage.py#L359)) — that did not exist when E8 was judged.
The metric counts COLUMN_LINEAGE edges on source-resolved write queries and can only rise when a
recall fix lands more edges, so it gives E8 a ruler it can pass or fail on honestly.

---

## 2. Revival approach — explicitly off the hot path

The revival is **un-revert + re-gate**, not a rewrite. Master does not contain E8 (confirmed:
`tests/unit/test_e8_temp_chain_key_mismatch.py` does not exist on master; the registration block
at [`ansi_parser.py:222`](../../src/sqlcg/parsers/ansi_parser.py#L222) writes only the bare key).
The `origin/fix/e8-temp-chain` branch has drifted far from master (70 files differ — it predates many
master commits) and **must not be merged wholesale**. Only the two surgical changes below are in scope.

### 2.1 Off-hot-path argument (must hold; the plan-reviewer should verify)

- The change is in the **per-statement file loop** in `parse_file`, *after* `_parse_statement`
  returns — it adds at most **two `dict.__setitem__` writes per `exp.Create` statement**. This is
  O(N_statements), not O(N_columns) or O(N_corpus). It is not inside the per-column loop.
- It does **NOT** feed parse-time schema into `exp.expand()`/`qualify()` — the forbidden hot-loop
  cost class in [`CLAUDE.md`](../../CLAUDE.md) "Schema resolution / Parse-time schema feeding remains
  forbidden". `sources_map` already holds CTE/CTAS *bodies* (file-level sources, the allowed input to
  `exp.expand()`); E8 only changes the **keys** those existing bodies are filed under, not the volume
  or class of what `exp.expand()` receives.
- It does **not** touch `_real_tables`, the per-column loop in `_extract_column_lineage`,
  `body_scope` construction, the `sg_lineage` kwargs (`copy=False`/`trim_selects=False`), or any
  bulk-upsert/batch path in [`indexer.py`](../../src/sqlcg/indexer/indexer.py). Every invariant in the
  [`CLAUDE.md`](../../CLAUDE.md) "Performance invariants — DO NOT REMOVE OR SIMPLIFY" table is untouched.
- The `dependency_filter` invariant ([`ansi_parser.py:184-196`](../../src/sqlcg/parsers/ansi_parser.py#L184))
  is preserved: E8 registers keys for the **current file's own** CTAS targets as they are parsed, which
  is the same set already registered today (just under more key aliases). It does not widen the
  cross-file seed.

### 2.2 Exact files / functions to touch (file:line anchors, master)

1. **[`src/sqlcg/parsers/ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) — function
   `AnsiParser.parse_file`, CTAS registration block at lines 222–232.**
   Replace the single `sources_map[target_name] = _expr` with the three-key block from §1.2
   (cherry-pick / port commits `a841c4b` + `3a31953`; do **not** merge the whole branch). The
   surrounding `isinstance(stmt, exp.Create)` / Subquery-unwrap / `query_node.target` guards are
   unchanged. Keep the explanatory comment (it documents the `_qualify_bare_tables` interaction and
   the "bare key is the CLAUDE.md file-level-sources invariant" rationale).

2. **`tests/unit/test_e8_temp_chain_key_mismatch.py` — new file** (port verbatim from
   `git show origin/fix/e8-temp-chain:tests/unit/test_e8_temp_chain_key_mismatch.py`, 365 lines).
   It pins E8 behaviour with these classes/tests:
   - `TestE8TempChainKeyMismatchFix::test_temp_chain_with_use_schema_resolves_to_leaf`
   - `…::test_temp_chain_qualified_sources_map_key_registered`
   - `…::test_temp_chain_with_schema_alias_resolves_to_leaf`
   - `TestE8NoBareTableNodeCreated::test_no_bare_tmp_table_role_in_defined_tables`
   - `…::test_temp_target_is_namespaced_not_bare`
   - `…::test_column_lineage_src_is_namespaced_temp_not_bare_table`
   - `TestE8StarColumnStillSkips::test_temp_column_resolves_while_star_still_skips`
   - `TestE8AnsiPathNoRegression::test_ansi_bare_temp_chain_still_resolves`

   These import `SchemaResolver`, `AnsiParser`, `SnowflakeParser` (all on master) and assert on
   observable COLUMN_LINEAGE edges + that the temp target is namespaced (not a bare `role='temp'`
   node). The developer must run them **failing-then-passing** against the revert/restore boundary
   to prove the change is what carries the behaviour (the remeasure did exactly this).

3. **Version bump** (per [`CLAUDE.md`](../../CLAUDE.md) "Releasing", in the feature branch before merge):
   - [`pyproject.toml`](../../pyproject.toml) `version = "1.29.0"`
   - [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) `__version__ = "1.29.0"`
   - `uv lock`

**No other source change is in scope.** In particular: do NOT port any of the test deletions/edits
on the drifted E8 branch, do NOT modify `coverage.py` (the gate metric already exists), do NOT touch
`indexer.py`, `base.py`, `_real_tables`, or any perf-invariant test.

### 2.3 Things the developer must NOT do

- Must NOT widen `exp.expand()`'s input beyond file-level CTE/CTAS bodies (CLAUDE.md invariant).
- Must NOT register a bare-`role='temp'` graph node or dual-write the graph key (v1.21.1 footgun;
  pinned by `test_no_bare_tmp_table_role_in_defined_tables`).
- Must NOT modify any of the four perf-invariant suites to make them pass (see §3 gate).
- Must NOT raise the slack in [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)
  if it goes red — a red there means E8 started something scaling; find and fix it, do not loosen the guard.

---

## 3. Acceptance gate (falsifiable — verbatim)

> **E8-REVIVAL ACCEPTANCE GATE.** The revival PASSES if and only if ALL of the following hold,
> measured on a single re-indexed corpus (WITHOUT-E8 control vs WITH-E8, same corpus, same config):
>
> 1. **Recall rises:** `resolvable_write_col_edges` (from `uv run sqlcg gain --json`) rises by
>    **≥ +1,000** WITH-E8 vs the WITHOUT-E8 control on the same corpus. (The prior A/B predicts
>    ≈+1,790; the +1,000 floor absorbs corpus drift and sits far above noise. The threshold is the
>    **delta on one corpus**, not an absolute value — the baseline is corpus-relative, 33,475 on
>    `/tmp/ab_pr2.duckdb`, 25,246 on the `/tmp/e8_without.duckdb` proxy.) **If the delta is < +1,000,
>    do NOT revive E8.**
> 2. **No quality regression:** the **strict %**, **scoped %**, and **catalogued %** column-lineage
>    health metrics (from the same `gain --json`) do **NOT** regress WITH-E8 vs the WITHOUT-E8
>    control. A ≤1pp dip in strict OR scoped that is explained by denominator growth (new edges
>    landing on legitimate temp/CTE/derived intermediate dst nodes — exactly the remeasure's −1pp
>    scoped) is acceptable and is NOT a regression; a drop in the absolute count of *strict-good*
>    or *scoped-good* edges IS a regression. Catalogued % must not move.
> 3. **Perf invariants intact, UNMODIFIED:** all four perf-invariant suites pass with **zero edits**
>    to the test files:
>    - [`tests/unit/test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py)
>    - [`tests/unit/test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py)
>    - [`tests/unit/test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py)
>    - [`tests/unit/test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)
>      (op-counts `build_scope`, `qualify`, `traverse_scope` stay flat; `sg_lineage` at most linear;
>      `scope_without_copy_false == 0`; INSERT body copied once — all assertions green unchanged).
> 4. **Perf budget held:** a full-corpus index (~1,335–1,600 files) completes under the **~3-minute**
>    budget on the reference laptop (excluding the catalog re-apply phase, which is independently
>    measured and not attributable to E8). If wall-time exceeds budget, the developer must show via
>    `test_perf_scaling_guard.py` op-counts that E8 did not introduce the regression before proceeding.
> 5. **No silent edge loss:** confirm the open postmortem question — the column-graph "−46 table-nodes
>    / −25 tables-with-incoming" — is benign temp-node consolidation and that **no real table lost its
>    only incoming COLUMN_LINEAGE edge** WITH-E8 vs the control. (Query the WITHOUT/WITH graphs for the
>    set of real, non-temp/CTE/derived, non-noise tables with ≥1 incoming COLUMN_LINEAGE; the WITH set
>    must be a superset of the WITHOUT set.)
> 6. **Full suite green:** `uv run pytest` passes, including the new
>    `tests/unit/test_e8_temp_chain_key_mismatch.py`; `uv run ruff check src tests` and
>    `uv run pyright` clean.
>
> Any single failure ⇒ FAIL ⇒ do not revive (revert the registration change and the test file).

### 3.1 Live-DWH acceptance artifact (CLAUDE.md house rule)

The live-DWH acceptance MUST emit the gain snapshot as JSON into the metrics tree:

```bash
uv run sqlcg gain --json > plan/metrics/gain_1.29.0_<shortsha>.json
```

where `<shortsha>` is the indexed commit's short SHA. Capture **both** the WITHOUT-E8 control and the
WITH-E8 run (e.g. `gain_1.29.0_<sha>_without.json` / `gain_1.29.0_<sha>_with.json`) so the
`resolvable_write_col_edges` delta and the strict/scoped/catalogued comparison in the gate are
reproducible from committed artifacts, not transient terminal output.

---

## 4. Test plan

| Layer | Test | Asserts |
|---|---|---|
| Unit (new) | `tests/unit/test_e8_temp_chain_key_mismatch.py` (8 tests, §2.2) | USE-SCHEMA + schema-alias temp chains resolve to the leaf via COLUMN_LINEAGE; qualified `sources_map` keys registered; bare ANSI path still resolves; star still skips; temp target namespaced (no bare `role='temp'`) |
| Unit (perf, UNMODIFIED) | `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`, `test_upsert_batch_invariant.py` | Gate item 3 — op-counts flat, behavioural invariants green, no edits |
| Unit (regression) | full `uv run pytest` unit suite | No collateral break (temp-table namespacing, skip counts, coverage metrics) |
| Live-DWH A/B (measure-first) | re-index DWH WITHOUT-E8 (control) then WITH-E8 | Gate items 1, 2, 4, 5; emit JSON per §3.1 |

**Failing-then-passing protocol:** the developer runs the new unit tests against master (E8 absent)
to confirm they FAIL, applies the §2.2 change, and confirms they PASS — proving the registration
change carries the behaviour (mirrors the remeasure's revert/restore proof).

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Recall delta < +1,000 (corpus drift since the +1,728 A/B) | Low | Measure-first gate (item 1) — FAIL ⇒ revert; the floor sits well below the +1,728 measured, +1,790 predicted |
| Strict/scoped % dips beyond denominator-growth explanation | Low-med | Gate item 2 distinguishes ratio dip (acceptable) from absolute-good-count drop (regression) |
| Hidden edge loss (the −25 tables-with-incoming open question) | Med | Gate item 5 — explicit superset check on real-table incoming COLUMN_LINEAGE |
| Perf scaling regression | Very low | Off-hot-path by construction (§2.1); gate item 3 pins it with unmodified op-count guard |
| Merging the whole drifted `fix/e8-temp-chain` branch by mistake | Med | §2.2 explicitly scopes to two ported changes only; do NOT merge the branch |
| Re-index OOM on the 3.8 GB box (concurrent-index history) | Med | Run WITHOUT/WITH sequentially, never concurrently (the remeasure's constraint) |

---

## 6. Rollback

The change is two surgical edits. Rollback = revert the `ansi_parser.py` registration block to the
single bare-key write ([`ansi_parser.py:222-232`](../../src/sqlcg/parsers/ansi_parser.py#L222)) and
delete `tests/unit/test_e8_temp_chain_key_mismatch.py`. No data migration: re-index is the migration
path ([`CLAUDE.md`](../../CLAUDE.md) "No backward compatibility"). Because the graph-node key is
unchanged, a rolled-back re-index simply stops resolving the temp chains again (returns to the master
baseline) with no schema or stored-key incompatibility.

---

## 7. Version bump target

**`1.28.3 → 1.29.0` (minor).** E8 adds a new resolution surface (more COLUMN_LINEAGE edges resolve)
with no breaking change — minor per the SemVer rule in [`CLAUDE.md`](../../CLAUDE.md) "Releasing".
After merge to `master`, tag annotated `v1.29.0 — E8 temp-chain dynamic_source multi-key revival`
on the merge commit, pushed alone.

---

## 8. Unresolved design question (for plan-reviewer)

1. **Recall metric vs catalog state.** `resolvable_write_col_edges` joins COLUMN_LINEAGE to write
   queries that have ≥1 SELECTS_FROM source. E8 adds COLUMN_LINEAGE edges but does **not** change
   SELECTS_FROM. The prior A/B predicts ≈+1,790 of the +1,728 new edges land on already-SELECTS_FROM-
   resolved write queries — but that depends on the temp-chain targets being write queries that
   themselves already have a SELECTS_FROM source. Should the gate ALSO require a +1,000 rise to be
   measured on the **same catalog** (`columns.csv`, 122,815 cols) for both control and treatment, to
   avoid the catalog re-apply confounding the count? (The plan assumes "same config both runs"; the
   reviewer should confirm whether catalog application happens before or after the metric snapshot and
   pin it explicitly in the runbook.)
2. **3-min budget vs measured wall-times.** The remeasure clocked 5m57 (with) / 4m18 (without) on a
   loaded WSL2 box, both over the ~3-min budget, attributed to WSL2 load + the 122k-column catalog
   re-apply, not E8. Gate item 4 carves out the catalog phase — the reviewer should confirm that
   carve-out is acceptable, or tighten it to "parse+CLONE phase under ~2m30 as measured", since a
   strict 3-min wall-clock could fail for reasons unrelated to E8.
