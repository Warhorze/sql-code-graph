# E8 revival — temp-chain `dynamic_source` multi-key COLUMN_LINEAGE fix

Status: BLOCKED — NEEDS USER DECISION

> **BLOCKED (2026-06-14, implementation conflict).** The §1.2 "register all three
> `sources_map` key forms unconditionally" instruction is **incompatible** with the
> shipped temp-table-namespacing feature (PR `b08e238`, which postdates this E8 branch).
> Implementing it verbatim regresses 3 namespacing tests. **Option (a)** — scoping the
> qualified-key registration to "db injected by the `USE SCHEMA`/`_qualify_bare_tables`
> session-context path" — was investigated and is **INFEASIBLE**: the failing namespacing
> test `test_lineage_chain_through_temp_with_alias` is *itself* a USE-SCHEMA-injected-db
> case, so the proposed distinguisher does not separate the two. E8 and temp-namespacing
> demand **opposite lineage shapes for the identical SQL pattern**. This is a genuine
> product fork. See **§0.1 (conflict) and §0.2 (user decision required)** below; the
> original REVIEWED runbook (§2–§9) is preserved verbatim and is only mergeable AFTER the
> fork in §0.2 is resolved.

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

## 0.1 The implementation conflict (discovered 2026-06-14)

Implementing the §1.2 three-key registration **verbatim** (unconditional bare +
post-alias-qualified `db.name` + pre-alias `ast_db.name`) makes the full suite fail:
**3 temp-table-namespacing tests regress** (the namespacing feature, PR `b08e238`,
postdates this E8 branch — the E8 branch's own copies of those test files predated the
stricter `b08e238` assertions, so the branch never caught this):

- [`tests/unit/test_temp_table_namespacing.py`](../../tests/unit/test_temp_table_namespacing.py)`::TestLineageLeafStamping::test_column_lineage_src_from_temp_is_namespaced`
- [`tests/integration/test_temp_table_namespacing.py`](../../tests/integration/test_temp_table_namespacing.py)`::TestLineageChainThroughTemp::test_column_lineage_edges_span_the_temp`
- [`tests/integration/test_temp_table_namespacing.py`](../../tests/integration/test_temp_table_namespacing.py)`::TestSchemaAliasDualWriteRegression::test_lineage_chain_through_temp_with_alias`

**Root cause.** For an explicitly-or-injected-qualified temp write
(`CREATE TEMPORARY TABLE ba.tmp_x AS SELECT a FROM ba.src; INSERT INTO ba.dst SELECT a FROM ba.tmp_x;`),
registering the **qualified** key `ba.tmp_x` in `sources_map` makes `exp.expand()` **inline**
the temp body → the INSERT's column lineage collapses to a direct `ba.src → ba.dst` edge,
**bypassing** the namespaced `role='temp'` intermediate node. The temp-namespacing feature
**deliberately requires** the temp to remain a *visible* node with COLUMN_LINEAGE edges on
*both* sides (`src → temp → dst`). E8 wants the opposite: column lineage resolved *through*
(i.e. inlining) the temp.

The `role='temp'` stamping + namespacing happens at
[`ansi_parser.py:343-352`](../../src/sqlcg/parsers/ansi_parser.py#L343) (detect
`exp.TemporaryProperty`, stamp `role="temp"`, record `_temp_identity` in
`_current_file_temp_keys`); the namespaced INSERT-source rewrite that keeps the temp a
distinct node is at [`base.py:993-1007`](../../src/sqlcg/parsers/base.py#L993). The E8
qualified-key write would land in `sources_map` at the CTAS block
[`ansi_parser.py:222-232`](../../src/sqlcg/parsers/ansi_parser.py#L222), consumed by
`exp.expand()` *before* that namespaced rewrite matters — so the temp body is inlined and
the namespaced node is left with zero edges on the inlined side.

### Empirical confirmation (this investigation, 2026-06-14)

Applied the §1.2 three-key block to
[`ansi_parser.py:229-232`](../../src/sqlcg/parsers/ansi_parser.py#L229) and ran both suites
(changes reverted afterward — investigation only):

- All **8** E8 tests in `tests/unit/test_e8_temp_chain_key_mismatch.py` **PASS**.
- All **3** namespacing tests above **FAIL** (e.g. `test_lineage_chain_through_temp_with_alias`:
  `out_of_temp == []` — the temp `etl/sql/fact/wtfv_bon_style.sql::ba.tmp_base` has zero
  outgoing COLUMN_LINEAGE edges; the INSERT edge went `da.real_src → real_target` directly).
- Direct edge probe of E8's own alias test fixture (`USE SCHEMA BA_TMP; CREATE OR REPLACE
  TEMPORARY TABLE tmp_inkoop AS SELECT col FROM da.leaf_src; INSERT INTO ba_tmp.final_tgt
  SELECT col FROM tmp_inkoop;`, alias `ba_tmp→ba`) with E8 applied yields:
  - `stmt0 (CREATE): da.leaf_src.col → etl/x.sql::ba.tmp_inkoop.col`  ← into the temp
  - `stmt1 (INSERT): da.leaf_src.col → ba.final_tgt.col`  ← **bypasses** the temp

## 0.2 Feasibility verdict on Option (a) — INFEASIBLE → USER DECISION REQUIRED

**Option (a)** asked: scope the qualified-key registration to *only* the case where the
target's `.db` was **injected** by the `USE SCHEMA`/`_qualify_bare_tables` session-context
qualify path (not when `.db` is a literal `ba.tmp_x` in the SQL).

**VERDICT: INFEASIBLE.** Option (a) rests on the premise that the failing namespacing tests
are *literal-db* cases and E8's wanted cases are *injected-db* cases — i.e. that the
injection source is the discriminator. **It is not.** The decisive counter-evidence:

1. The third failing namespacing test,
   [`TestSchemaAliasDualWriteRegression::test_lineage_chain_through_temp_with_alias`](../../tests/integration/test_temp_table_namespacing.py),
   uses fixture `_WTFV_BON_STYLE_SQL`
   ([`test_temp_table_namespacing.py:535-547`](../../tests/integration/test_temp_table_namespacing.py#L535)):
   `USE SCHEMA BA_TMP;` + bare `CREATE OR REPLACE TEMPORARY TABLE tmp_base AS …` +
   `INSERT INTO real_target … FROM tmp_base`, with alias `ba_tmp→ba`. The temp's `.db` here
   is **INJECTED** by `_qualify_bare_tables` ([`snowflake_parser.py:404`](../../src/sqlcg/parsers/snowflake_parser.py#L404)
   `table.set("db", …)`), then folded `ba_tmp→ba`. This is **exactly** the injected-db case
   Option (a) would scope the qualified-key registration *into* — yet this test requires the
   temp to **NOT** be inlined.

2. E8's own alias test,
   [`TestE8TempChainKeyMismatchFix::test_temp_chain_with_schema_alias_resolves_to_leaf`](../../tests/unit/test_e8_temp_chain_key_mismatch.py)
   (ported from `origin/fix/e8-temp-chain`, lines 130-172), uses a **structurally identical**
   fixture (`USE SCHEMA BA_TMP;` + bare `CREATE OR REPLACE TEMPORARY TABLE tmp_inkoop AS …` +
   `INSERT INTO ba_tmp.final_tgt … FROM tmp_inkoop`, alias `ba_tmp→ba`) and asserts the **leaf
   edge** `da.leaf_src → final_tgt` exists — i.e. the temp **IS** inlined.

Both fixtures are the same SQL shape — `USE SCHEMA` + injected-db + alias-folded bare
`CREATE TEMPORARY TABLE` + downstream INSERT — and they assert **opposite** lineage shapes
(one demands the temp be inlined/bypassed; the other demands it be preserved as a visible
node with edges on both sides). **No registration-time flag** (an injection marker on the
ref, comparison of `target.db` against the session USE-SCHEMA context, or raw-statement-text
inspection) can separate them, because the disagreement is **not about how the `.db` arrived**
— it is about **what lineage shape a USE-SCHEMA temp chain should have**. The two features
encode contradictory product intents for identical input. This is a genuine fork, not a
parser-mechanism gap.

(For completeness, the *other* E8 wins — e.g.
[`test_temp_chain_with_use_schema_resolves_to_leaf`](../../tests/unit/test_e8_temp_chain_key_mismatch.py)
with plain `USE SCHEMA da; CREATE TEMP TABLE tmp_inkoop …` and **no** schema alias — are
*also* injected-db cases that demand inlining. So even the simplest E8 target case sits on
the same side of the contradiction as the namespacing tests it breaks.)

### THE USER FORK

The user must choose one of these two mutually-exclusive product behaviours for the
**USE-SCHEMA / injected-db temp chain** (`CREATE TEMPORARY TABLE <bare> … ; INSERT … FROM <bare temp>`):

#### Option (a-collapse): accept E8 collapsing temps — re-decide the 3 namespacing assertions
- **Mechanism:** ship E8's unconditional three-key registration (§1.2 as written). The temp
  body is inlined; COLUMN_LINEAGE resolves `real_source → final_target` directly.
- **Lineage the user GAINS:** end-to-end source→sink column lineage across USE-SCHEMA temp
  chains (the +1,728-edge / `dynamic_source` recall win E8 was built for). A consumer asking
  "what real table feeds `final_target.col`?" gets `real_source.col` in one hop.
- **Lineage the user LOSES:** the temp stops being a queryable intermediate node on the
  inlined (INSERT) side. The `<rel>::<db>.<name>` `role='temp'` node still EXISTS (the CREATE
  side still wires `real_source → temp`), but it has **zero outgoing** edges — a consumer can
  no longer trace "what reads from `tmp_base`?" through column lineage. The 3 namespacing
  tests must be **re-decided** (relaxed to allow the inlined shape, or scoped to non-injected
  cases). NOTE: this is a behaviour change to a SHIPPED feature (`b08e238`); it needs the
  namespacing feature's owner to sign off, and a postmortem note on why the temp-visible
  invariant was loosened.
- **Risk:** the namespacing dual-write footgun (v1.21.1) was specifically about temps being
  collapsed into shared bare nodes; re-decide carefully so collapsing does not reintroduce
  cross-file temp fusion (the two-file `_FILE_A/_FILE_B` defusion tests must stay green —
  inlining the *body* into the consumer is NOT the same as fusing the temp *node*, but verify).

#### Option (b-preserve): keep temp-namespacing visible — drop/limit E8 to non-namespaced cases
- **Mechanism:** do NOT register the qualified key for a target that was stamped `role='temp'`
  (i.e. when `_temp_identity(target.db, target.name) ∈ self._current_file_temp_keys` at the
  CTAS block, [`ansi_parser.py:343-352`](../../src/sqlcg/parsers/ansi_parser.py#L343)). Register
  the qualified key ONLY for non-temp CTAS targets (plain `CREATE TABLE … AS`). The bare-key
  write stays unconditional (no regression to the existing ANSI path).
- **Lineage the user GAINS:** the temp-namespacing invariant is preserved — every temp stays a
  visible node with edges on both sides (`src → temp → dst`); the two-file defusion guarantee
  holds. Non-temp CTAS qualified-key resolution (if any exists on the USE-SCHEMA path) still
  gets the E8 fix.
- **Lineage the user LOSES:** the headline E8 win is **forfeited for temps** — the USE-SCHEMA
  *temporary*-table `dynamic_source` dead-end is NOT fixed (the temp is kept, not resolved
  through). Since E8's measured +1,728-edge win was driven by `CREATE TEMP TABLE` chains, the
  recall delta would be **far below** the gate's +1,000 floor — likely near zero on temp-heavy
  corpora. **E8 as a recall lever is effectively a no-op under (b-preserve)** unless a material
  population of non-temp `CREATE TABLE AS` chains on the USE-SCHEMA path exists (UNMEASURED —
  would need an A/B before committing to (b)).
- **Risk:** low (it strictly narrows E8); but the recall win that justified reviving E8 may
  evaporate — re-measure before merging, and if the non-temp delta is < +1,000, **do not revive
  E8** (gate item 1 already says so).

#### Recommendation for the user to weigh
The decision hinges on **one product question**: *for a USE-SCHEMA temporary-table ETL chain,
is the temp a meaningful lineage node (keep it — option b) or a transient that should be seen
through (collapse it — option a)?* The namespacing feature (`b08e238`, the LATER decision)
already answered "keep it" and shipped tests pinning that. Reviving E8 reverses that answer
for the temp case. Absent a new product reason to reverse `b08e238`, **(b-preserve) is the
lower-risk default** — but it likely guts E8's recall value, so the honest framing is: *E8 and
temp-namespacing-visibility cannot both hold for the same chain; pick which lineage shape the
product wants.*

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

3. **Version parity — bump ALL THREE to `1.29.0`** (per [`CLAUDE.md`](../../CLAUDE.md) "Releasing",
   in the feature branch before merge). Current master is **`1.28.3` in both source files** (not yet
   bumped); the three must move together or version-skew detection fires:
   - [`pyproject.toml`](../../pyproject.toml) **line 7** — `version = "1.29.0"`
   - [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py) **line 3** — `__version__ = "1.29.0"`
   - `uv lock` (refreshes the lockfile's own version entry), then `uv sync` (so the installed env matches)

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
- **Must NOT port the E8 branch's [`tests/unit/test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)
  edits (anti-regression NOTE).** The `origin/fix/e8-temp-chain` version of this file **DELETES master's
  `traverse_scope` counter** — a PR-3 (#134 / #38) addition that postdates the branch (the branch predates
  it; `git diff master origin/fix/e8-temp-chain -- tests/unit/test_perf_scaling_guard.py` shows the
  `traverse_scope` counter/wrapper/assertion removed and a NEW `expand_with_sources` guard added). Porting
  the branch's version would silently drop a live master perf invariant. The four perf-invariant suites
  MUST pass **UNMODIFIED** (gate item 3) — that includes keeping master's `traverse_scope` counter intact.
  Port ONLY [`tests/unit/test_e8_temp_chain_key_mismatch.py`](../../tests/unit/test_e8_temp_chain_key_mismatch.py)
  and the two surgical [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) changes from commits
  `a841c4b` + `3a31953` — this is a **cherry-pick of three artifacts, NOT a branch merge** (the branch has
  drifted 70 files).

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
> 2. **No quality regression (pinned to the exact `gain --json` keys).** `gain --json` emits NO
>    "catalogued %" field — that metric does not exist. The catalog key it DOES emit is
>    `catalogued_tables`, a legacy table COUNT (verified: [`coverage.py:852`](../../src/sqlcg/cli/coverage.py#L852)
>    `"catalogued_tables": coverage.catalogued_tables` — sourced from `_Q_CATALOG_COVERAGE`,
>    `COUNT(DISTINCT src_key)` in HAS_COLUMN at [`coverage.py:38-42`](../../src/sqlcg/cli/coverage.py#L38)).
>    The gate compares these exact JSON keys WITH-E8 vs the WITHOUT-E8 control:
>    - **`catalogued_tables` count must NOT decrease.**
>    - **`good_edges_strict`** ([`coverage.py:859`](../../src/sqlcg/cli/coverage.py#L859)) + its ratio
>      **`edge_health_strict_pct`** ([`coverage.py:860`](../../src/sqlcg/cli/coverage.py#L860)).
>    - **`good_edges_scoped`** ([`coverage.py:862`](../../src/sqlcg/cli/coverage.py#L862)) + its ratio
>      **`edge_health_scoped_pct`** ([`coverage.py:864`](../../src/sqlcg/cli/coverage.py#L864)).
>
>    Rule: the **absolute good-edge COUNT** (`good_edges_strict`, `good_edges_scoped`) must **NOT drop**.
>    A **≤1pp dip in `edge_health_strict_pct` OR `edge_health_scoped_pct`** that is explained by
>    denominator growth (new edges landing on legitimate temp/CTE/derived intermediate dst nodes —
>    exactly the remeasure's −1pp scoped) is **acceptable** and is NOT a regression. A drop in either
>    absolute good-count IS a regression. (Catalog config MUST be identical across both runs — see §3 D1
>    — so this comparison is apples-to-apples, since `catalogued_tables`/strict/scoped ARE catalog-sensitive.)
> 3. **Perf invariants intact, UNMODIFIED:** all four perf-invariant suites pass with **zero edits**
>    to the test files:
>    - [`tests/unit/test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py)
>    - [`tests/unit/test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py)
>    - [`tests/unit/test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py)
>    - [`tests/unit/test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)
>      (op-counts `build_scope`, `qualify`, `traverse_scope` stay flat; `sg_lineage` at most linear;
>      `scope_without_copy_false == 0`; INSERT body copied once — all assertions green unchanged).
> 4. **Perf budget held (measured by phase, not raw wall-clock).** The **parse + CLONE + ingest phase**
>    of a full-corpus index (~1,335–1,600 files) completes **under ~2m30**, as measured by the indexer's
>    own phase timing (`profile=True` — the pass-1/pass-2/upsert/star accumulators at
>    [`indexer.py:369-377`](../../src/sqlcg/indexer/indexer.py#L369)), with the **catalog re-apply phase
>    measured and reported SEPARATELY**. The catalog re-apply is a one-shot bulk path at
>    [`indexer.py:700`](../../src/sqlcg/indexer/indexer.py#L700) (`_reapply_catalog_if_configured`), is
>    **off the E8 code path**, and dominated the remeasure's 5m57 (with) / 4m18 (without) WSL2 wall-times —
>    so a raw "~3-min total, catalog excluded" wall-clock is unmeasurable as an E8 signal and is dropped.
>    If the parse+CLONE+ingest phase exceeds ~2m30, the developer must show via
>    `test_perf_scaling_guard.py` op-counts that E8 did not introduce the regression before proceeding.
> 5. **No silent edge loss (literal SQL — no hand-waving).** Confirm the open postmortem question —
>    the column-graph "−46 table-nodes / −25 tables-with-incoming" — is benign temp-node consolidation
>    and that **no REAL table lost its only incoming COLUMN_LINEAGE edge** WITH-E8 vs the control.
>
>    A COLUMN_LINEAGE edge is `SqlColumn → SqlColumn` ([`schema.cypher:93-99`](../../src/sqlcg/core/schema.cypher#L93)),
>    exposed relationally with `src_key`/`dst_key` (the SqlColumn ids). The destination **table**'s
>    key is the `dst_key` with its trailing `.<column>` stripped — which equals `SqlTable.qualified`
>    (the `<rel>::<db>.<name>` primary key, [`schema.cypher:18-25`](../../src/sqlcg/core/schema.cypher#L18));
>    the strip expression is the committed `_DST_TABLE` at [`coverage.py:29`](../../src/sqlcg/cli/coverage.py#L29).
>    A **real** table is one whose `SqlTable.kind` is NOT a synthetic kind. The canonical synthetic set
>    is `{'cte','derived','temp'}` — [`tools.py:385`](../../src/sqlcg/server/tools.py#L385)
>    `_SYNTHETIC_TABLE_KINDS = frozenset({"cte", "derived", "temp"})` — and is exactly the predicate the
>    *scoped* health metric already excludes ([`coverage.py:84`](../../src/sqlcg/cli/coverage.py#L84)
>    `t.kind IN ('cte', 'derived', 'temp')`). Real-table filter (the EXACT predicate):
>    `kind NOT IN ('cte','derived','temp','external') AND kind IS NOT NULL` — `external` is excluded as
>    out-of-corpus noise, NULL-kind rows are excluded as unresolved.
>
>    Run this **committed** query against BOTH the control db (`/tmp/e8_without.duckdb`) and the
>    WITH-E8 db; it returns the SET of real tables having ≥1 incoming COLUMN_LINEAGE edge:
>
>    ```sql
>    -- gate-5: real tables with ≥1 incoming COLUMN_LINEAGE edge.
>    -- dst_key strip == _DST_TABLE (coverage.py:29); real == kind not synthetic/external/null.
>    SELECT DISTINCT t.qualified
>    FROM "COLUMN_LINEAGE" cl
>    JOIN "SqlTable" t
>      ON t.qualified =
>         left(cl.dst_key, len(cl.dst_key) - instr(reverse(cl.dst_key), '.'))
>    WHERE t.kind NOT IN ('cte', 'derived', 'temp', 'external')
>      AND t.kind IS NOT NULL
>    ORDER BY t.qualified;
>    ```
>
>    **Gate:** the WITH-E8 result set must be a **SUPERSET** of the control result set — i.e.
>    `(control_set EXCEPT with_set)` must be **EMPTY**. If any real table present in the control is
>    absent WITH-E8, a real table lost its only provenance ⇒ **FAIL**. (Set difference, runnable directly:
>    `SELECT ... FROM control EXCEPT SELECT ... FROM with;` against the two dbs, or diff the two committed
>    result lists.)
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
`resolvable_write_col_edges` delta and the strict/scoped/`catalogued_tables` comparison in the gate are
reproducible from committed artifacts, not transient terminal output.

### 3.2 Runbook decisions (folded from review — formerly open questions)

**D1 — `resolvable_write_col_edges` is catalog-INSENSITIVE; the +1,000 delta is attributable to E8
alone (DECISION, not an open question).** The catalog re-apply path
(`_reapply_catalog_if_configured` → `apply_catalog_to_backend`,
[`catalog.py:217-224`](../../src/sqlcg/cli/commands/catalog.py#L217)) writes ONLY `SqlTable` (insert-if-absent),
`SqlColumn`, and `HAS_COLUMN(source='information_schema')` rows, plus a `derived→table` kind upgrade
([`catalog.py:230-231`](../../src/sqlcg/cli/commands/catalog.py#L230)). It writes **ZERO** `COLUMN_LINEAGE`
and **ZERO** `SELECTS_FROM` rows — and those two tables are the **only** ones the recall metric reads
(`_Q_RESOLVABLE_WRITE_COL_EDGES`, [`coverage.py:358-365`](../../src/sqlcg/cli/coverage.py#L358):
`FROM "COLUMN_LINEAGE" cl JOIN "SqlQuery" … EXISTS (SELECT 1 FROM "SELECTS_FROM" …)`). Therefore the
catalog state cannot move `resolvable_write_col_edges`, and the +1,000 delta is attributable to E8's
`sources_map` keys alone — **PROVIDED both control and treatment use the IDENTICAL catalog config**.
That identical-config requirement is mandatory anyway so gate item 2's
`catalogued_tables`/strict/scoped comparison is apples-to-apples, since THOSE keys ARE catalog-sensitive
(they read `HAS_COLUMN`, which the catalog path writes).

**D2 — perf budget is the parse+CLONE+ingest phase under ~2m30, catalog measured separately (DECISION).**
See gate item 4 above. The unmeasurable "~3-min total, catalog excluded" wall-clock framing is dropped:
the catalog re-apply is a one-shot off-E8 bulk path ([`indexer.py:700`](../../src/sqlcg/indexer/indexer.py#L700))
that dominated the remeasure's WSL2 wall-times (5m57 / 4m18), so only the indexer's own `profile=True`
phase timing is a valid E8 signal.

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

## 8. Unresolved design questions

**None.** Both questions raised at NEEDS-REVIEW were resolved at review and folded into the runbook as
affirmative decisions:

- Former Q1 (recall metric vs catalog state) → **§3.2 D1** (catalog-insensitive; +1,000 delta is E8's).
- Former Q2 (3-min budget vs measured wall-times) → **gate item 4 + §3.2 D2** (phase-timed ~2m30
  parse+CLONE+ingest, catalog measured separately).

---

## 9. Review Trail

**Verdict: APPROVE-WITH-AMENDMENTS** (plan-reviewer). All six amendments below are applied; Status moved
NEEDS-REVIEW → **REVIEWED**.

1. **Gate item 2 named a non-existent metric.** `gain --json` emits no "catalogued %" field — only
   `catalogued_tables`, a legacy table COUNT (verified at [`coverage.py:852`](../../src/sqlcg/cli/coverage.py#L852),
   sourced from `_Q_CATALOG_COVERAGE`, [`coverage.py:38-42`](../../src/sqlcg/cli/coverage.py#L38)). Gate
   item 2 rewritten: "`catalogued_tables` count must not decrease" and pinned to the exact JSON keys
   `good_edges_strict` + `edge_health_strict_pct` and `good_edges_scoped` + `edge_health_scoped_pct`
   (absolute good-edge COUNT must not drop; a ≤1pp ratio dip from denominator growth is acceptable).
2. **Gate item 5 was hand-wavy — authored literal SQL.** Wrote a committed query returning the SET of
   REAL tables (`kind NOT IN ('cte','derived','temp','external') AND kind IS NOT NULL`) with ≥1 incoming
   COLUMN_LINEAGE edge, runnable against `/tmp/e8_without.duckdb` and the with-E8 db; gate = treatment
   set is a SUPERSET of control. Predicate justified by `_SYNTHETIC_TABLE_KINDS` at
   [`tools.py:385`](../../src/sqlcg/server/tools.py#L385), the scoped exclusion at
   [`coverage.py:84`](../../src/sqlcg/cli/coverage.py#L84), `_DST_TABLE` strip at
   [`coverage.py:29`](../../src/sqlcg/cli/coverage.py#L29), and the SqlTable.qualified/kind schema at
   [`schema.cypher:18-25`](../../src/sqlcg/core/schema.cypher#L18).
3. **Folded former Q1 into the runbook as DECISION §3.2 D1** (removed from §8). `resolvable_write_col_edges`
   is catalog-INSENSITIVE — the catalog path writes only SqlTable/SqlColumn/HAS_COLUMN + a derived→table
   upgrade ([`catalog.py:217-231`](../../src/sqlcg/cli/commands/catalog.py#L217)) and zero
   COLUMN_LINEAGE/SELECTS_FROM, the only two tables the metric reads
   ([`coverage.py:358-365`](../../src/sqlcg/cli/coverage.py#L358)); +1,000 delta is E8's, given identical
   catalog config both runs.
4. **Folded former Q2 into gate item 4 as DECISION §3.2 D2** (removed from §8). Perf budget retargeted to
   "parse+CLONE+ingest under ~2m30 via the indexer's `profile=True` phase timing
   ([`indexer.py:369-377`](../../src/sqlcg/indexer/indexer.py#L369)), catalog re-apply
   ([`indexer.py:700`](../../src/sqlcg/indexer/indexer.py#L700)) measured separately". Dropped the
   unmeasurable "~3-min total" framing.
5. **Version parity prerequisite.** §2.2 item 3 now bumps ALL THREE to 1.29.0 — `pyproject.toml` line 7,
   `src/sqlcg/__init__.py` line 3, and `uv lock` (+ `uv sync`). Noted current master is 1.28.3 in both
   source files.
6. **Anti-regression NOTE (§2.3).** Do NOT port the E8 branch's `test_perf_scaling_guard.py` edits — that
   branch DELETES master's `traverse_scope` counter (a PR-3 #134/#38 addition postdating the branch) and
   adds an E8-specific `expand_with_sources` guard. The four perf-invariant suites must pass UNMODIFIED
   (gate item 3). Port ONLY `test_e8_temp_chain_key_mismatch.py` and the two surgical ansi_parser.py
   changes from `a841c4b` + `3a31953` — a cherry-pick of three artifacts, NOT a branch merge (the branch
   has drifted 70 files).

---

## 10. Changelog / Review-Trail — 2026-06-14 BLOCKED amendment

**Status moved REVIEWED → BLOCKED — NEEDS USER DECISION** (architect-planner, implementation-conflict revision).

**What happened.** During implementation, applying §1.2's "register all three `sources_map` key forms
unconditionally" verbatim regressed **3 shipped temp-table-namespacing tests** (PR `b08e238`, which
postdates this E8 branch — the branch's stale copies of those test files predated `b08e238`'s stricter
assertions, so the conflict was invisible on-branch). The qualified key makes `exp.expand()` inline the
temp body, collapsing `src → temp → dst` to `src → dst` and stripping the namespaced `role='temp'` node's
outgoing edges. Full root cause + empirical run captured in new **§0.1**.

**Option (a) investigated and found INFEASIBLE** (new **§0.2**). Option (a) — scope the qualified-key
registration to "db injected by `USE SCHEMA`/`_qualify_bare_tables`" — does NOT resolve the conflict: the
failing namespacing test `test_lineage_chain_through_temp_with_alias` is *itself* an injected-db USE-SCHEMA
case (fixture `_WTFV_BON_STYLE_SQL`, [`test_temp_table_namespacing.py:535`](../../tests/integration/test_temp_table_namespacing.py#L535)),
structurally identical to E8's own `test_temp_chain_with_schema_alias_resolves_to_leaf`
([`test_e8_temp_chain_key_mismatch.py`](../../tests/unit/test_e8_temp_chain_key_mismatch.py) on
`origin/fix/e8-temp-chain`), yet the two demand opposite lineage shapes. The injection-source is not the
discriminator; the features encode contradictory product intent for identical SQL.

**The fork** (new **§0.2 → THE USER FORK**): **(a-collapse)** ship E8, re-decide the 3 namespacing
assertions (gain end-to-end source→sink column lineage; lose the temp as a traceable intermediate on the
inlined side; requires namespacing-owner sign-off + a v1.21.1-footgun re-check) vs **(b-preserve)** keep
temp-namespacing visible, scope E8's qualified-key write to non-`role='temp'` CTAS targets (preserve the
temp node + defusion guarantee; forfeit E8's headline temp-driven recall win — likely below the +1,000 gate
floor; re-measure before merging).

**The original REVIEWED runbook (§1–§9) is preserved verbatim.** It is only mergeable AFTER the user picks
a fork branch; under (b-preserve) the §1.2 registration instruction and gate item 1's +1,000 floor must be
revised (re-enter NEEDS-REVIEW); under (a-collapse) the gate must add the namespacing re-decision.
