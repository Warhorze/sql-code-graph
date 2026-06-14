# Sprint Plan: Snowflake Lineage Pattern Fixes

**Status:** DRAFT (awaiting plan-reviewer gate)
**Branch:** `plan/snowflake-lineage-patterns` (off `master`)
**Author:** architect-planner (Opus)
**Date:** 2026-06-14
**Source research:** [`snowflake_sql_lineage_patterns.md`](../research/snowflake_sql_lineage_patterns.md)
(branch `research/snowflake-sql-patterns`, commit `34ecea0`) — every finding below
cites that doc's *measured* parse behaviour + DWH grep prevalence (1,449 files).

---

## Version baseline (confirmed against live master)

Master at plan time (commit `86a500f`) is **v1.34.3** and **already contains #159** (the
create-kind LABEL fix — `master:src/sqlcg/parsers/base.py:595/599 → QueryKind.OTHER`).
PR-B's dependency on #159 is therefore **satisfied on master**: no rebase onto an in-flight
branch is needed. (A transient snapshot during planning showed master at 1.33.1 / a worktree
at 1.34.2 mid-merge; the authoritative tip is 1.34.3.)

**Version bumps below are provisional** — several PRs may merge interleaved with other
in-flight work, so reconcile the actual `X.Y.Z` at merge time (per CLAUDE.md "Releasing").
The relative SemVer per PR (PATCH vs MINOR) is fixed; the absolute numbers are not. All
anchors below are re-confirmed against `master` @ `86a500f`.

---

## Thesis

The research doc converts "surprise Snowflake bug" into a measured construct map. This
sprint plans the **active-bug** and **future-proofing** slices it ranks, sequenced by
leverage (metric-movers first) and dependency. Out-of-scope items (see bottom) are
handled in other threads and are NOT re-planned here.

## Sequencing overview

| Seq | PR | Finding | Class | Files (DWH) | Metric mover | SemVer | Depends on |
|---|---|---|---|---|---|---|---|
| 1 | **PR-A** | `IDENTIFIER($var)` table indirection | (a) HIGH | 142 / 175 sites | **YES** (coverage↑) | MINOR | — |
| 2 | **PR-B** | Phantom source nodes for non-table CREATE | (a) | ~25 names, 14 w/ SELECTS_FROM | YES (node/edge count↓) | PATCH | #159 (kind=OTHER) — already on master |
| 3 | **PR-C** | MERGE column lineage (un-defer T-07-06) | (a) partial | 17 | **YES** (coverage↑) | MINOR | — |
| 4 | **PR-D** | CREATE STREAM / TASK body unwrap (future-proof) | (c) | 0 / 1 | no | MINOR | — |
| 5 | **PR-E** | PIVOT/UNPIVOT + COPY INTO triage | (a) low / (c) | 11 / 3 bare | maybe | PATCH | — |

### Cross-PR file overlap & serialization

All five touch the parser layer. Overlaps:

| File | PR-A | PR-B | PR-C | PR-D | PR-E |
|---|---|---|---|---|---|
| `snowflake_parser.py` | ✎ (`_transform_statements`) | — | — | ✎ (Command unwrap) | ✎ (`_preprocess`) |
| `ansi_parser.py` | — | ✎ (source gate `:537-580`) | ✎ (`_parse_statement` MERGE arm) | — | — |
| `base.py` | — | — | ✎ (`_can_have_column_lineage`, `_extract_merge_lineage`) | — | maybe (`_classify`) |
| `indexer.py` | — | maybe | — | — | — |

- **PR-A and PR-B are independent** (different files: sf `_transform_statements` vs
  ansi source-gate). Can develop in parallel; merge in seq order so coverage deltas are
  measured against a stable baseline.
- **PR-B's #159 dependency is already satisfied** (#159 is on master @ v1.34.3). PR-B keys
  its gate on `stmt.kind` (AST) anyway, so it is independent of #159's `_classify` change and
  robust to merge order — see PR-B design.
- **PR-C touches `base.py` `_can_have_column_lineage` AND a new `_extract_merge_lineage`**;
  it does not overlap PR-A/PR-B line ranges but must **serialize after PR-A merges** only
  to keep the gain --json baseline clean (two coverage movers should not share one
  measurement window). No code conflict.
- **PR-E may touch `base.py:_classify`** (COPY arm) — serialize after PR-C to avoid a
  base.py merge race; low urgency, likely deferred (see PR-E).

---

# PR-A — `IDENTIFIER($var)` table indirection (HEADLINE METRIC-MOVER)

## Finding (research §"Active DWH bugs" #1)

`FROM IDENTIFIER($v)` / `INSERT INTO IDENTIFIER($v)` parse to an `exp.Table` whose
identity is the unresolved bind variable, so `table.name == ''`. The empty-id guard in
`normalize_keys._norm_ref` then **silently drops** the source/target → partial lineage
loss across **142 files / 175 DML sites** (research-measured, e.g.
[`tthyb_ean_environmentalfeatures_us15770.sql:28`] `FROM identifier($TABLE_STIBAT) sti`).
This is the highest-prevalence *unhandled* lineage loss in the corpus.

## Root-cause anchors (re-confirmed against `master`)

- **Empty-id drop:** `normalize_keys._norm_ref` — `master:src/sqlcg/parsers/base.py:398-413`
  (`if not ref.full_id:` at `base.py:410` → `return None`), applied to sources/target/star in
  the `normalize_keys` loop that follows. (Research cited "base.py:~398-413" — exact match on
  master @ `86a500f`; the drop is real and confirmed.)
- **Var-literal map already exists:** `SnowflakeParser._transform_statements` —
  `master:src/sqlcg/parsers/snowflake_parser.py:359-364` builds
  `var_literals: dict[str,str]` from `exp.PropertyEQ` (`var := 'literal'`, the `LET`/`:=`
  scripting form). Runs **once per file**, pure AST walk, explicitly perf-safe
  (`sf.py:341-343`).
- **Per-file transform hook:** `_transform_statements` is called once at
  `master:src/sqlcg/parsers/ansi_parser.py:143`, after `_do_parse`, before the statement
  loop — outside the per-column hot loop.

## Measured AST shapes (probed on sqlglot 30.6.0, this session)

```
SET TABLE_STIBAT = 'da.stibat_x'
  → exp.Set ▸ exp.SetItem ▸ exp.EQ(this=Column(name='TABLE_STIBAT'),
                                    expression=Literal(is_string=True, this='da.stibat_x'))

SELECT * FROM identifier($TABLE_STIBAT) sti
  → exp.Select ▸ exp.Table(name='', alias='sti',
                           this=exp.Anonymous(name='identifier',
                                              ▸ exp.Parameter(name='TABLE_STIBAT')))

INSERT INTO IDENTIFIER($tgt) SELECT a FROM raw.src
  → exp.Insert(this=exp.Table(name='', this=exp.Anonymous(name='IDENTIFIER',
                                                          ▸ Parameter(name='tgt'))))
```

**Two distinct var-binding forms must both feed the map:**
- `LET v := '…'` / `v := '…'` → `exp.PropertyEQ` (already captured, `sf.py:361`).
- `SET v = '…'` → `exp.Set ▸ exp.SetItem ▸ exp.EQ(Column, Literal)` (NOT captured today —
  this is the form the 142 IDENTIFIER files use; `SET` is the Snowflake session-variable
  setter). **Adding `exp.Set` scanning is the load-bearing change.**

## Design

A pure-AST, once-per-file rewrite inside `SnowflakeParser._transform_statements`, **before**
`_qualify_bare_tables` and before any statement reaches `_parse_statement` / qualify.

### Step A.1 — widen the var-literal pre-scan to cover `SET`

Extend the existing pre-scan loop (`sf.py:360-364`) so the map is built from BOTH forms:

```python
# existing (LET / := scripting form) — keep
if isinstance(stmt, exp.PropertyEQ) and isinstance(stmt.expression, exp.Literal):
    lit = stmt.expression
    if lit.is_string and stmt.this is not None and stmt.this.name:
        var_literals[stmt.this.name.lower()] = str(lit.this)
# NEW (SET session-variable form)
if isinstance(stmt, exp.Set):
    for item in stmt.find_all(exp.SetItem):
        eq = item.this
        if (isinstance(eq, exp.EQ)
                and eq.this is not None and eq.this.name
                and isinstance(eq.expression, exp.Literal)
                and eq.expression.is_string):
            var_literals[eq.this.name.lower()] = str(eq.expression.this)
```

- **Last-write-wins** within a file (a var reassigned later resolves to its latest literal
  seen in source order). This is a documented simplification: a var set inside a branch /
  loop is not statically resolvable — out of scope, falls through to the empty-id drop
  (same as today). Record this in the PR body.
- Concatenation / `CONCAT` / non-literal RHS is skipped (not `exp.Literal`) — identical to
  the existing EXECUTE-IMMEDIATE policy. Falls through to empty-id drop. No regression.

### Step A.2 — new method `_resolve_identifier_tables(stmt, var_literals)`

Once-per-statement AST walk that rewrites resolvable `IDENTIFIER($v)` table nodes in place:

```python
@staticmethod
def _resolve_identifier_tables(stmt: Any, var_literals: dict[str, str]) -> None:
    """Rewrite FROM/INSERT-INTO IDENTIFIER($var) table nodes to real tables using
    the file's SET/LET literal map. Pure AST walk, once per statement; no qualify/
    build_scope/expand/sg_lineage. No-op when var_literals is empty."""
    if not var_literals:
        return
    for table in stmt.find_all(exp.Table):
        inner = table.this
        if not isinstance(inner, exp.Anonymous) or inner.name.lower() != "identifier":
            continue
        param = inner.find(exp.Parameter)
        if param is None or not param.name:
            continue
        literal = var_literals.get(param.name.lower())
        if literal is None:
            continue                       # unresolved → left for empty-id drop
        new_table = sqlglot.to_table(literal, dialect="snowflake")
        alias = table.args.get("alias")     # preserve the FROM/INSERT alias
        if alias is not None:
            new_table.set("alias", alias)
        table.replace(new_table)
```

Verified by probe: `FROM identifier($T) sti` with `{'t':'da.stibat_x'}` →
`da.stibat_x AS sti` (`name='stibat_x', db='da'`); an unresolved `identifier($U)` is left
untouched (still dropped — correct). Quoted literals (`'"My Schema"."My Tbl"'`) flow
through `to_table` and inherit the existing quoted-identifier handling — **add a fixture
but defer quoted-IDENTIFIER correctness to #153** (cross-reference; do not duplicate that
work here).

### Step A.3 — call site

In `_transform_statements`, inside the per-statement loop (after the EXECUTE-IMMEDIATE
splice at `sf.py:376-384` and after the `Use`/`PropertyEQ` suppression, before
`_qualify_bare_tables` at `sf.py:427-428`), call
`self._resolve_identifier_tables(stmt, var_literals)`. Rewriting BEFORE `_qualify_bare_tables`
means a resolved-but-bare `identifier($v)='stibat_x'` (no schema in the literal) still gets
the active `USE SCHEMA` prefix — correct.

**INSERT target:** `exp.Insert.this` is itself an `exp.Table` and is reached by
`stmt.find_all(exp.Table)`, so the same walk rewrites `INSERT INTO IDENTIFIER($v)` targets.
Confirmed by probe (target name `''` → real table after rewrite). No separate target path.

**`ALTER … IDENTIFIER($full)`** parses to `exp.Command` (research) — NOT an `exp.Table` walk
target; out of scope here (stays `OTHER`, no lineage). Note it; do not attempt.

## Perf invariants (four-frozen-suite note)

- `_resolve_identifier_tables` is **once per statement**, never per column. It runs in the
  file-level `_transform_statements` loop, the same loop as the existing var-scan and
  `_qualify_bare_tables`. No `qualify` / `build_scope` / `exp.expand` / `sg_lineage` call is
  added. The `find_all(exp.Table)` walk is O(N_tables_per_stmt), bounded and statement-local.
- **No change to `base.py` hot loop** — by the time `_extract_column_lineage` runs, the AST
  already contains real tables; the per-column path is untouched.
- The four guards
  ([`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
  [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py))
  must stay green unchanged. **No new counter needed** (the rewrite adds no qualify-class op);
  if the dev adds any once-per-statement op the scaling guard does not already track, add a
  behavioural assertion there, not a volume count.

## Acceptance criteria (observable — assert real edges/kinds, live `$var`/SET shapes)

- [ ] **AC-A1 (FROM resolution → real source edge).** Fixture
  `SET TABLE_STIBAT='da.stibat_x'; INSERT INTO da.sink SELECT sti.id AS id FROM identifier($TABLE_STIBAT) sti`
  parses to a `LineageEdge` whose `src.table.full_id == 'da.stibat_x'` and
  `dst.table.full_id == 'da.sink'`. Assert the edge exists in `parsed.statements[*].column_lineage`
  (not "no exception"). Today this edge is absent.
- [ ] **AC-A2 (INSERT-INTO target resolution).** Fixture
  `SET tgt='da.result'; INSERT INTO IDENTIFIER($tgt) SELECT a AS a FROM raw.src`
  yields a QueryNode with `target.full_id == 'da.result'` and a source `raw.src`. Assert
  `target` is non-None and correctly qualified.
- [ ] **AC-A3 (unresolved var still dropped, no crash).** Fixture `SELECT a FROM identifier($UNSET) x`
  (no SET) → the IDENTIFIER table is left unrewritten and dropped by the empty-id guard;
  `parsed.errors` contains a `col_lineage_skip:stage:` (or equivalent) entry and NO edge is
  emitted. Assert graceful degradation, not a raise.
- [ ] **AC-A4 (USE SCHEMA interplay).** Fixture
  `USE SCHEMA da; SET t='stibat_x'; SELECT id AS id FROM identifier($t)` → source resolves to
  `da.stibat_x` (schema prefix applied after IDENTIFIER rewrite). Assert `src.table.full_id == 'da.stibat_x'`.
- [ ] **AC-A5 (LET/:= form still works).** Fixture using `v := 'da.x'` followed by
  `FROM identifier($v)` resolves identically (regression guard for the existing PropertyEQ path).
- [ ] **AC-A6 (perf suite green).** All four frozen perf tests pass unchanged.
- [ ] **AC-A7 (coverage gate — DWH).** `sqlcg gain --json` before/after on the live DWH
  shows a **net increase** in strict/catalogued edges with NO regression in any coverage
  axis. Record both JSON payloads under `plan/metrics/`. (Expectation: meaningful uplift —
  142 files currently lose ≥1 source/target each.)

## gain --json before/after gate

Required (coverage mover). Capture `uv run sqlcg gain --json` on the DWH **before** (master
baseline) and **after** the branch, save as
`plan/metrics/pr_a_identifier_before.json` / `_after.json`. PR blocks on a non-negative
delta across every axis and a positive delta on at least strict-or-catalogued edges.

## Version bump

**MINOR** (additive capability: new resolved-lineage surface, nothing breaks). Reconcile
absolute number at merge.

## Grep-confirmed call sites (pre-PR-open requirement)

- `_resolve_identifier_tables` — single call site in `_transform_statements`
  (`snowflake_parser.py`, the per-statement loop). Dev must `grep` the call before PR open.
- No other new method.

---

# PR-B — Suppress phantom source nodes for non-table CREATE (finishes #159's story)

## Finding (research §"Active DWH bugs" #2)

#159 (shipped, on master @ v1.34.3, `base.py:595/599`) fixed the **kind LABEL**:
non-table `CREATE` (SEQUENCE/STAGE/FILE FORMAT/SCHEMA/INDEX) now classifies as `OTHER`
instead of `CREATE_TABLE`. **The phantom SOURCE node persists**: the object's own name
still leaks into `referenced_tables` as a `SqlTable` source (research: ~25 names, 14 with
`SELECTS_FROM` edges). **Do NOT re-plan the kind-label fix — that is done.** This PR closes
the open half: suppress the ref.

## Root-cause anchors (re-confirmed against `master`)

- **Where the phantom enters sources:** for a non-table CREATE, `_extract_target_table`
  returns None (`ansi_parser.py` guards kind ∈ {TABLE,VIEW}), but `build_scope(stmt)`
  returns **None** for these statements (probed: `CREATE SEQUENCE ba.s` → scope None), so
  source extraction falls to `_fallback_table_scan(stmt)` at
  `master:src/sqlcg/parsers/ansi_parser.py:566-571` / `575-579`, which picks up the object's
  own name (`ba.wsdh_s1`) via `find_all(exp.Table)`.
- **Where it leaves the parser — THREE extend sites, all fed by `_parse_statement`:**
  `out.referenced_tables.extend(query_node.sources)` at `master:ansi_parser.py:246` (main
  path) and `master:snowflake_parser.py:784` + `:944` (scripting / dynamic-SQL paths). The
  research cited `sf:784/944` — both confirmed on master. **Critically, all three consume the
  `query_node.sources` produced by `AnsiParser._parse_statement`** (the scripting paths call
  `_parse_statement` at `sf.py:760` / the analogous site near `:910`). Gating `sources` inside
  `_parse_statement` therefore fixes ALL THREE extend sites with a single change — no need to
  touch the extend lines themselves. The indexer (`indexer.py`, research cited `:1590`) emits
  whatever `referenced_tables` contains; the fix belongs in the parser, not the indexer.

## Design

Gate source extraction off for CREATE statements that are not TABLE/VIEW, **before** sources
are computed. In `_parse_statement` (`ansi_parser.py:536-584`), guard the `sources`
extraction block:

```python
# Non-table/non-view CREATE (SEQUENCE/STAGE/FILE FORMAT/SCHEMA/INDEX …) has no
# data-flow source — its only Table node is its own object name, a phantom. Suppress.
is_non_table_create = (
    isinstance(stmt, exp.Create) and stmt.kind not in ("TABLE", "VIEW")
)
if is_non_table_create:
    sources = []
else:
    # ... existing scope / _real_tables / _fallback_table_scan block (537-580) ...
```

- **Key off `stmt.kind` (the AST), NOT `QueryKind`** — this makes PR-B independent of whether
  #159 has merged the `_classify` change. `stmt.kind` is sqlglot's value (`'SEQUENCE'` etc.)
  and is correct on master regardless of #159. (If reviewer prefers keying off the classified
  `QueryKind.OTHER`, that hard-couples to #159; AST-keying is the lower-risk choice. **DESIGN
  FORK — see Maintainer decisions.**)
- `target` already None for these (unchanged). `defined_columns`/`defined_body` already empty.
  Net effect: the statement contributes no `defined_tables`, no `referenced_tables`, no edges.
- **PROCEDURE/FUNCTION:** `CREATE PROCEDURE` (kind `'PROCEDURE'`) would also be gated by
  `kind not in (TABLE,VIEW)`. Today proc bodies are not inlined (lineage lives in recovered
  EXECUTE IMMEDIATE), and a proc's top-level `find_all(exp.Table)` sources are not meaningful
  lineage. Confirm via the DWH gain gate that gating PROCEDURE does not *drop* any real edge
  (it should not — proc lineage comes from the dynamic-SQL recovery path, not from
  `query_node.sources`). If the gate shows an edge loss for PROCEDURE, narrow the suppression
  set to `{'SEQUENCE','STAGE','FILE FORMAT','SCHEMA','INDEX','MASKING POLICY','ROW ACCESS POLICY'}`
  explicitly. **DESIGN FORK — see Maintainer decisions.**

## Perf invariants

- One `isinstance` + tuple-membership check per CREATE statement; **removes** a
  `_fallback_table_scan` walk for non-table creates (net faster). No hot-loop op added. Four
  frozen perf tests unchanged.

## Acceptance criteria (observable)

- [ ] **AC-B1 (no phantom source).** Fixture `CREATE SEQUENCE IF NOT EXISTS ba.wsdh_s1` →
  `parsed.referenced_tables == []` and the statement's `QueryNode.sources == []`. Today
  `referenced_tables` contains `ba.wsdh_s1`. Assert the list is empty.
- [ ] **AC-B2 (STAGE / FILE FORMAT).** Same for `CREATE STAGE ma.msstg_ingest URL='s3://x'`
  and `CREATE FILE FORMAT ma.msfmt_parquet TYPE=PARQUET` → no source node.
- [ ] **AC-B3 (no SELECTS_FROM edge into graph).** Integration test: index a fixture file
  containing a `CREATE SEQUENCE ba.s` + a real `INSERT INTO ba.t SELECT … FROM ba.real` and
  assert the graph has NO `SqlTable` node named `ba.s` and NO `SELECTS_FROM` edge touching it,
  while the real `ba.real → ba.t` lineage is intact.
- [ ] **AC-B4 (real table CREATE unaffected).** Control: `CREATE TABLE t AS SELECT a FROM s`
  still yields source `s` and target `t` (safety — gate must not touch kind TABLE).
- [ ] **AC-B5 (node/edge count gate).** `sqlcg gain --json` before/after on DWH shows
  `SqlTable` node count drops by the phantom count (~25) and SELECTS_FROM by ~14, with NO
  drop in any *real* coverage axis (strict/scoped/catalogued edges unchanged or up). Record
  payloads.

## gain --json before/after gate

Required (node/edge count mover). `plan/metrics/pr_b_phantom_before.json` / `_after.json`.
Block on: phantom node/edge counts down, real coverage axes not regressed.

## Version bump

**PATCH** (bug fix: removes incorrect nodes; no new capability). Reconcile at merge.

## Dependency

Builds conceptually on **#159** (kind=OTHER), which is **already on master @ v1.34.3**
(`base.py:595/599`). PR-B is additionally **code-independent** of it by keying the gate off
`stmt.kind` (AST), so it is robust to merge order. Do NOT re-add or re-test the `_classify`
flip (already covered by
[`test_classify_non_table_create_kind.py`](../../tests/unit/test_classify_non_table_create_kind.py)).

## Grep-confirmed call sites

No new method — an inline guard in existing `_parse_statement`. No call-site grep needed; the
guard is in the hot path's own function. Dev confirms the existing source block still has its
single entry.

---

# PR-C — MERGE column lineage (un-defer T-07-06)

## Finding (research §"Active DWH bugs" #3)

Table-grain MERGE edge exists (`_classify → QueryKind.MERGE`, sources captured); **column
edges are skipped** — `_can_have_column_lineage` explicitly returns False for `exp.Merge`
(deferred T-07-06). Research: **17 files**, e.g.
[`nabewerking_htwfm_contract_us28254.sql:12`]. The `WHEN MATCHED UPDATE SET` and
`WHEN NOT MATCHED INSERT VALUES` clauses are structurally extractable **without** sqlglot's
`lineage()` — a direct AST walk.

## Root-cause anchors (re-confirmed against `master`)

- **Skip site:** `SqlParser._can_have_column_lineage` — `master:src/sqlcg/parsers/base.py:636`+;
  `exp.Merge` falls through to `return False`. Docstring names the T-07-06 defer.
- **Table-grain already present:** `_classify → QueryKind.MERGE` (`base.py:564` `_classify`,
  `exp.Merge()` arm); sources via `_real_tables` are captured for MERGE in `_parse_statement`
  (build_scope works for Merge).
- **Edge model:** `LineageEdge(src=ColumnRef(table,name), dst=ColumnRef(table,name), transform=...)`
  — `base.py:107-191` (`ColumnRef` 107, `LineageEdge` 143, `LineageExtraction` 207).

## Measured AST shape (probed, sqlglot 30.6.0)

```
MERGE INTO da.htwfm_contract AS target USING raw.src AS s ON target.id=s.id
  WHEN MATCHED THEN UPDATE SET target.name=s.name, target.amt=s.amt
  WHEN NOT MATCHED THEN INSERT (id,name) VALUES (s.id, s.name)

exp.Merge(
  this   = Table('da.htwfm_contract', alias='target'),   # MERGE target
  using  = Table('raw.src', alias='s'),                   # source
  whens  = [ exp.When(matched=True,  then=exp.Update(▸ EQ(target.name, s.name), EQ(target.amt, s.amt))),
             exp.When(matched=False, then=exp.Insert(this=(id,name) tuple,
                                                      expression=VALUES (s.id, s.name) tuple)) ])
```

- **MATCHED UPDATE:** each `exp.EQ` → `dst = LHS column on target`, `src = RHS column(s) on
  source` (RHS may be an expression; collect all `exp.Column` in the RHS as sources).
- **NOT MATCHED INSERT:** positional alignment of `this` (target column tuple) with
  `expression` (VALUES tuple). For each position `i`: `dst = target.cols[i]`,
  `src = all exp.Column in values[i]`. If the column list is absent (`INSERT VALUES …` with no
  `(cols)`), fall back to the target's DDL catalog by position (reuse existing positional-INSERT
  catalog logic if reachable) OR skip with a `col_lineage_skip:merge_no_collist:` error —
  **DESIGN FORK, see Maintainer decisions**.

## Design

### Step C.1 — admit MERGE into the lineage-eligible set

In `_can_have_column_lineage` (`base.py:588-614`), add an `exp.Merge` arm returning True. Update
the docstring (remove the "explicitly skipped (T-07-06)" sentence; cite this plan).

### Step C.2 — new method `_extract_merge_lineage(stmt, dst_table, sources, ...) -> list[LineageEdge]`

A dedicated structural walk (NOT via `sg_lineage`):

1. Resolve target ref from `stmt.this` (already the parsed MERGE target) and source ref(s) from
   `stmt.args['using']` + aliases. Build an alias→TableRef map from target+using.
2. For each `exp.When`:
   - `matched=True, then=exp.Update`: for each `exp.EQ` in the SET list, `dst = ColumnRef(target,
     lhs_col_name)`; for each `exp.Column` in the RHS, emit `LineageEdge(src=ColumnRef(resolve_alias,
     col), dst, transform='MERGE_UPDATE')`.
   - `matched=False, then=exp.Insert`: zip `then.this` (target cols) with `then.expression` (values);
     per position emit edges from each RHS `exp.Column` to the target column, `transform='MERGE_INSERT'`.
3. Pure-literal RHS (no `exp.Column`) → skip that assignment (mirrors the existing pure-literal skip
   invariant). Alias not resolvable → skip that edge with a logged `col_lineage_skip:merge_unresolved:`.

### Step C.3 — route MERGE through it in `_extract_column_lineage` (or `_parse_statement`)

Where `_extract_column_lineage` branches by stmt type, add an `isinstance(stmt, exp.Merge)` branch
that returns `LineageExtraction(edges=self._extract_merge_lineage(...), star_sources=[])`. Keep it
**out of the per-column `sg_lineage` path entirely** — MERGE uses its own bounded walk.

## Perf invariants

- `_extract_merge_lineage` is **once per MERGE statement**, O(N_set_clauses + N_insert_cols). No
  `qualify` / `build_scope` / `exp.expand` / `sg_lineage` call (it is a structural AST walk, the
  whole point of the research recommendation). Pure-literal skip preserved. The four frozen perf
  tests are unaffected (MERGE never entered the column hot loop before and does not now).
- **Add a behavioural assertion** to the perf suite OR a dedicated guard test: MERGE extraction must
  NOT call `sg_lineage`/`qualify` (assert via a patch/spy that those are not invoked for a MERGE
  fixture). This pins the "structural, not lineage()" invariant the research relies on.

## Acceptance criteria (observable)

- [ ] **AC-C1 (MATCHED UPDATE column edges).** Fixture (above) → edges
  `(raw.src.name → da.htwfm_contract.name)` and `(raw.src.amt → da.htwfm_contract.amt)` present
  with `transform='MERGE_UPDATE'`. Assert both edges in `column_lineage`.
- [ ] **AC-C2 (NOT MATCHED INSERT column edges).** Same fixture → `(raw.src.id → …contract.id)`,
  `(raw.src.name → …contract.name)` with `transform='MERGE_INSERT'`.
- [ ] **AC-C3 (table-grain edge unchanged).** The existing table-grain MERGE source/target
  (`src=['raw.src'], target='da.htwfm_contract'`) is still present (no regression of the shipped
  table-grain behaviour).
- [ ] **AC-C4 (expression RHS multi-source).** `UPDATE SET target.total = s.a + s.b` → two edges
  (`s.a → total`, `s.b → total`). Assert both.
- [ ] **AC-C5 (pure-literal skip).** `UPDATE SET target.flag = 1` → no edge for `flag`, no error
  noise. Assert absence.
- [ ] **AC-C6 (live config — qualified + aliased).** Fixture must model live shape: schema-qualified
  target+source, table aliases (`AS target`/`AS s`), and a `USE SCHEMA` context if the corpus file
  has one. Assert edges land on fully-qualified ids, not bare names.
- [ ] **AC-C7 (no sg_lineage/qualify for MERGE).** Spy assertion: extracting the MERGE fixture does
  not invoke `sg_lineage` or `qualify`.
- [ ] **AC-C8 (perf suite green).** Four frozen perf tests pass.
- [ ] **AC-C9 (coverage gate — DWH).** `gain --json` before/after shows MERGE column edges added
  (17 files), no axis regressed. Record payloads.

## gain --json before/after gate

Required (coverage mover). `plan/metrics/pr_c_merge_before.json` / `_after.json`.

## Version bump

**MINOR** (additive lineage surface). Reconcile at merge.

## Grep-confirmed call sites

- `_extract_merge_lineage` — single call site in `_extract_column_lineage`'s MERGE branch (or
  `_parse_statement`). Dev greps the call before PR open.

---

# PR-D — CREATE STREAM / TASK … AS <dml> body unwrap (future-proofing)

## Finding (research §"Future-proofing" #5)

`CREATE STREAM` / `CREATE TASK` fall to `exp.Command` (sqlglot 30.6.0 cannot parse them) →
`OTHER`, no lineage. A `CREATE TASK … AS <dml>` hides the inner DML's lineage. **0 STREAM / 1
TASK in this DWH** — not a current metric mover. The research explicitly asks for an
**inversion-target fixture now** so the gap is pinned before another warehouse hits it.

## Design (minimal, fixture-first)

1. **Inversion-target fixture (REQUIRED, ships even if the unwrap is deferred):** add
   `tests/unit/snowflake/fixtures/create_task_with_dml.sql` containing
   `CREATE TASK t WAREHOUSE=wh SCHEDULE='1 minute' AS INSERT INTO da.sink SELECT a FROM da.src`
   and a test asserting **today's behaviour** (`kind=OTHER`, no edge) — the *inversion target*:
   when the unwrap lands, this test flips to asserting the `da.src → da.sink` edge. This pins the
   gap so it cannot silently regress or be forgotten.
2. **Optional unwrap (DESIGN FORK — recommend DEFER):** reuse the EXECUTE-IMMEDIATE Command-unwrap
   machinery (`snowflake_parser.py:655 _inner_stmts_from_command`) to detect a `CREATE TASK … AS
   <dml>` Command body, extract the trailing DML substring, re-parse it, and splice it into the
   statement stream (inheriting USE SCHEMA context) — exactly the EXECUTE-IMMEDIATE pattern. STREAM
   has no DML body (CDC metadata), so STREAM only needs the no-phantom guarantee (Command produces
   no extractable Table — already safe).

**Recommendation:** ship the fixture + behaviour-pin now; **defer the actual unwrap** until a
corpus shows prevalence (research: 1 task here). Document the unwrap design so the next agent can
implement against the pinned fixture. This keeps the PR cheap and honors the research's "fixture-now"
ask without speculative hot-path code.

## Perf invariants

If the unwrap is implemented: it reuses `_inner_stmts_from_command` (once per file, pure regex/AST,
no qualify-class op) — same perf class as EXECUTE IMMEDIATE. Fixture-only variant: zero perf impact.

## Acceptance criteria

- [ ] **AC-D1 (fixture pins current behaviour).** `create_task_with_dml.sql` parses with the task
  body's DML producing NO edge today; test asserts `kind=OTHER` and `column_lineage == []` for the
  task statement. (Inversion target — documented to flip on unwrap.)
- [ ] **AC-D2 (STREAM no phantom).** `CREATE STREAM s ON TABLE da.t` → `OTHER`, no source node, no
  edge. Assert clean.
- [ ] **AC-D3 (if unwrap implemented).** Task DML edge `da.src → da.sink` present. (Skipped if
  deferred.)

## gain --json gate

**Not required** — 0/1 files, no coverage movement on this DWH. (If unwrap is implemented and a
future corpus has tasks, add the gate then.)

## Version bump

**MINOR** if unwrap ships (new capability); **PATCH** if fixture-only behaviour-pin (no behaviour
change). Recommend PATCH (defer unwrap). Reconcile at merge.

## Grep-confirmed call sites

If unwrap implemented: extends existing `_inner_stmts_from_command` (already has its call site at
`sf.py:376`). No new top-level method needed.

---

# PR-E — PIVOT/UNPIVOT + COPY INTO (triage — recommend DEFER)

## Finding (research §"Active DWH bugs" #4 + "Future-proofing")

- **PIVOT/UNPIVOT (11 files):** dynamic output columns → sqlcg sees `SELECT *` → star-skip → **0
  edges**, with `col_lineage_skip:star:<unqualified>` error. Research: coarse fix = detect
  `exp.Pivot` and emit edges from the aggregated value column to all output columns (coarse but
  non-empty). Low urgency.
- **COPY INTO (3 bare / 17 dynamic):** `kind=OTHER`, no `exp.Copy` arm. Bare-3 are stage→table
  ingest boundaries (no in-graph upstream). 17/19 are inside dynamic strings (overlaps PR-A /
  dynamic-SQL). Research: low priority.

## Triage decision: **DEFER both**, with rationale and pinned fixtures

- **COPY INTO:** 3 bare occurrences, each a stage (external) → table boundary with **no in-graph
  upstream** to draw an edge to. The useful part (capturing the inner `SELECT` source on unload) is
  already done. An `exp.Copy` arm would add a `QueryKind.COPY` classification and drop the `@stage`
  ref (the empty-id guard already drops stage refs) — net zero lineage gain for 3 files. **Defer**;
  not worth a base.py `_classify` change + merge race with PR-C/PR-D. Pin a fixture asserting the
  bare COPY produces no phantom stage node (confirm the empty-id guard already handles `@stage`).
- **PIVOT/UNPIVOT:** 11 files; the coarse fix emits value→output edges that are *approximate* (the
  pivot output columns are data-dependent). Risk: coarse edges could pollute strict coverage with
  low-confidence edges. **Defer** until measured demand; pin an inversion-target fixture asserting
  today's 0-edge + `star:` error so the gap is visible.

**If the reviewer wants PR-E in scope:** scope it to fixtures-only behaviour-pins (PATCH, no
behaviour change), NOT the coarse PIVOT edge emission (which needs a confidence-tagging design
decision first). **DESIGN FORK — see Maintainer decisions.**

## Acceptance criteria (fixtures-only variant)

- [ ] **AC-E1 (COPY no phantom).** `COPY INTO da.t FROM @ma.stg/p` → no `@stage` source node in the
  graph (empty-id guard). Assert.
- [ ] **AC-E2 (PIVOT inversion pin).** `SELECT * FROM (SELECT m,amt FROM da.src) PIVOT(SUM(amt) FOR
  m IN ('a','b'))` → today 0 edges + `col_lineage_skip:star:` error. Assert (inversion target).

## Version bump

**PATCH** (fixtures-only). Reconcile at merge.

---

## Maintainer decisions required (DESIGN FORKS — resolve before plan-reviewer PASS)

1. **PR-A var-form scope.** Confirm `SET v = 'lit'` (`exp.Set/SetItem/EQ`) is the form to support
   alongside the existing `:=`/`LET` (`exp.PropertyEQ`). The 142 IDENTIFIER files use `SET` — without
   it PR-A moves ~nothing. (Recommendation: YES, support both.) **Also: last-write-wins for
   reassigned vars + skip branch/loop-scoped sets — acceptable simplification?** (Recommendation: yes,
   matches existing EXECUTE-IMMEDIATE policy.)

2. **PR-B gate key.** Key the source-suppression off `stmt.kind` (AST, #159-independent) vs off the
   classified `QueryKind.OTHER` (couples to #159). (Recommendation: `stmt.kind`.) **And: gate ALL
   `kind not in (TABLE,VIEW)` (includes PROCEDURE) vs an explicit non-table set
   `{SEQUENCE,STAGE,FILE FORMAT,SCHEMA,INDEX,…}`?** PROCEDURE gating must be proven edge-neutral by
   the DWH gain gate. (Recommendation: start with the broad `not in (TABLE,VIEW)` gate; narrow only
   if the gain gate shows a PROCEDURE edge loss.)

3. **PR-C INSERT-without-column-list.** When `WHEN NOT MATCHED INSERT VALUES (…)` has no `(cols)`
   list, fall back to the target DDL catalog by position (reuse positional-INSERT logic) vs skip with
   a logged `col_lineage_skip:merge_no_collist:`. (Recommendation: skip-and-log first; positional
   fallback only if the 17 DWH files actually use the no-collist form — verify against the corpus.)

4. **PR-D / PR-E scope.** Confirm DEFER-with-fixture for STREAM/TASK unwrap, PIVOT coarse edges, and
   COPY arm (recommendation), OR pull any into active implementation. The research ranks all of these
   below the metric-movers.

---

## Out of scope (handled elsewhere — explicitly NOT in this sprint)

- **#153 quoted/spaced identifiers** — separate REVIEWED plan + pending maintainer decision. PR-A's
  quoted-IDENTIFIER literal case cross-references #153; does not duplicate it.
- **#6 impact-union** — fix in flight.
- **Create-kind KIND label (#159)** — shipped, on master @ v1.34.3 (`base.py:595/599`). PR-B
  finishes its open half (phantom node) but does NOT re-plan the label fix.
- **Join-col resolution (#5)** — shipped.
- **Star-promote (#4)** — shipped.
- **IA-DATAPRODUCTS dynamic-table perf exclusion** — research §DYNAMIC TABLE; subprocess isolation,
  out of scope.

---

## Definition of done (sprint)

- PR-A, PR-B, PR-C merged with all ACs green and gain --json before/after recorded under
  `plan/metrics/`.
- PR-D, PR-E: fixtures + behaviour-pins merged (unwrap/coarse-edge deferred unless a fork resolves
  to implement).
- All four frozen perf tests green on every PR; no new per-column hot-loop op.
- Each new method (`_resolve_identifier_tables`, `_extract_merge_lineage`) has a grep-confirmed call
  site before its PR opens.
- Version bumps reconciled against master-at-merge (relative SemVer per PR table; absolute numbers
  TBD).
