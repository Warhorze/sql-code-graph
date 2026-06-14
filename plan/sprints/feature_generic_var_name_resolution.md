# Feature Plan: Generic variable-name resolution for dynamic table names

**Status:** DRAFT (a plan-reviewer gates this before implementation)
**Source study:** `git show research/generic-var-name-resolution:plan/research/generic_variable_name_resolution.md` (commit `60635c7`, VERDICT **HOLDS**)
**Foundation (dropped):** [`fix/identifier-var-indirection`](src/sqlcg/parsers/snowflake_parser.py) commit `0f655d0` — handles the LITERAL case only; this feature extends "is the RHS a literal?" → "FOLD the RHS".
**Branch:** `plan/generic-var-name-resolution`
**Target version:** **MINOR** — new lineage capability (no break). Master is **v1.34.3** at planning time (see [Version note](#version-note)); plan targets **v1.35.0**, reconcile the exact number at merge.

---

## Summary

Generic, bounded constant-propagation of string-valued Snowflake session variables to
resolve dynamic table names (`IDENTIFIER($var)` in DML position), recovering ~187
currently-dropped DML-context references across ~136 files at `schema.table` grain.
A dialect-agnostic fold core does the work; only a one-line Snowflake sink predicate is
dialect-coupled.

---

## Version note

The dispatch brief says "master ~v1.35.x / v1.35.0+". The actual current master at
planning time is **v1.34.3** ([`pyproject.toml`](pyproject.toml), confirmed via
`git show master:pyproject.toml`). The worktree this plan was authored alongside carried
unrelated in-flight work bumped to 1.34.4, which is **not** master. Treat the version as
**the next MINOR after whatever master is at merge** — currently `v1.35.0`. Reconcile at
merge time, as the project's release notes routinely do (e.g. the v10/1.33.0 collision).

---

## Scope

### In Scope

- A dialect-agnostic `resolve_dynamic_name(rhs_expr, var_env, *, chain_depth=1)` that
  partial-constant-folds a string-expression AST: string literals, `||`/`CONCAT`,
  1-hop `$var` chains; marks runtime funcs / scalar subqueries / bind params OPAQUE;
  interprets the fold as `[catalog.]db.name` (name-last, dot-split); keeps the
  statically-determined trailing components; wildcards a leading OPAQUE catalog but
  keeps a static literal catalog; gives up honestly otherwise.
- A Snowflake-only sink predicate `is_dynamic_name_sink(table)` recognising
  `IDENTIFIER($var)` (an `exp.Anonymous` named `identifier` wrapping an
  `exp.Parameter`) **in a table position** (FROM/JOIN/INTO/UPDATE/MERGE/USING).
- Landing PR-A's scaffolding (the literal-only `_resolve_identifier_tables` + the
  `exp.Set` / `exp.PropertyEQ` var pre-scan) and rewiring it to call the fold core;
  extending the pre-scan to store the **RHS AST**, not just literal strings.
- A perf-guard counter so the fold stays once-per-statement (no qualify/expand/sg_lineage).
- Live-DWH `gain --json` before/after acceptance with the no-literal-catalog-collision
  assertion.

### Non-Goals

- **Other-dialect dynamic-SQL sinks** (T-SQL `sp_executesql` / `EXEC(@sql)`, Postgres
  `format()`+`EXECUTE`, Spark string-built names). The core is built generic so a later
  dialect adds only its own `is_dynamic_name_sink`; **no other predicate ships here.**
- The 40 scalar-subquery delta vars that are genuinely runtime-dynamic (no static tail)
  stay unresolved — they hit the honest give-up.
- A general dataflow interpreter / fixpoint var resolution / chain depth > 1
  (corpus max is 1; see [The bound](#the-bound-flow-gate--enforced-limits)).
- `ALTER WAREHOUSE IDENTIFIER()` (`exp.Command`), `CALL …($v)` proc-args, scalar/WHERE
  vars — these are excluded **structurally** (the walk visits `exp.Table` sinks only);
  preserve that auto-exclusion, do not add a name blocklist.
- Parse-time schema feeding into `exp.expand()`/`qualify()` (forbidden by CLAUDE.md).

---

## Design

### Fold-core API (dialect-agnostic)

Suggested home: a new module [`parsers/dynamic_name.py`](src/sqlcg/parsers/) (the study's
preference; keeps `base.py` lean and the seam obvious). Pure function, no parser state.

```python
def resolve_dynamic_name(
    rhs_expr: exp.Expression,
    var_env: dict[str, exp.Expression],   # lowercased var name → its RHS AST
    *,
    chain_depth: int = 1,
) -> exp.Table | None:
    """Bounded partial constant-fold of a string-expression AST into a TableRef.

    Returns an exp.Table for the resolvable trailing identifier components, or None
    (give up honestly — caller leaves the sink dropped, today's behaviour).
    """
```

#### Fold rules (the OPAQUE/LIT classification)

Fold the RHS into an ordered list of parts, each `LIT(s)` or `OPAQUE`:

- Descend through `exp.Subquery` / `exp.Select` **single-projection only**,
  `exp.Paren`, `exp.DPipe`, `exp.Concat`.
- `exp.Literal(is_string=True)` → `LIT(s)`.
- `exp.Parameter`→`exp.Var` (`$other_var`): look it up in `var_env` **once** (chain
  depth 1); fold that RHS one level; if it folds to a leading constant prefix use it,
  else `OPAQUE`. Do **not** re-follow `$vars` discovered inside that value
  (chain-depth bound).
- Everything else — runtime funcs (`exp.CurrentDatabase`, `current_warehouse`,
  `current_schema`), `exp.SplitPart`, `exp.Substring`, a scalar subquery that is not a
  pure single-projection of folds, a bind param (`?`, `:x`), an unresolved/undefined var
  → `OPAQUE`.

#### Name-extraction rules (the keep/wildcard/give-up policy)

A SQL object id is `[catalog.]db.name`, dot-separated, **name-last**. From the folded
parts:

1. Take the **rightmost run of `LIT` parts** (the static suffix), concatenate, and
   `to_table(suffix, dialect=...)` it.
2. **KEEP the statically-determined trailing components.** The tail is normally
   `.db.name` (= `schema.table`); the leading catalog position is OPAQUE.
3. **Wildcard / drop a leading OPAQUE catalog** → emit `db.name` with empty catalog.
   This is the existing catalog-less identity (`to_table('DHB.KOSTEN')` → cat='' db=DHB
   name=KOSTEN, **probed live**), so the resolved node **merges** with DDL / plain-SQL
   refs to the same table.
4. **KEEP a static literal catalog when present.** If the fold yields a fully-literal
   `catalog.schema.table` (no OPAQUE in the leading position), do NOT collapse it —
   `to_table('PRD_DB.DHB.KOSTEN')` → cat=PRD_DB (**probed live**). This is the cross-DB
   safety valve ([fork F1](#design-forks-needing-a-maintainer-decision)).
5. **Give up honestly** (return `None`) when: the result is all-OPAQUE; the static tail
   has no dot (a bare name like `'TMP_TABLE_DATASET'`); or the tail lacks a resolvable
   `db.name`. Never emit a guessed/partial edge — silence over a wrong edge.

#### Worked folds (from the study; the PR-1 fixture spec, behaviourally)

| RHS | Parts | Result |
|---|---|---|
| `'DL_' \|\| split_part(current_database(),'_',2) \|\| '.DHB.KOSTEN'` | `LIT('DL_'),OPAQUE,LIT('.DHB.KOSTEN')` | `DHB.KOSTEN` (cat='') |
| `(SELECT 'DL_' \|\| split_part(...) \|\| '.DHB.KOSTEN')` | same (descend Subquery) | `DHB.KOSTEN` (cat='') |
| `$ODS_DB \|\| '.ods.ig_stibat'` where `ODS_DB`→OPAQUE | `OPAQUE,LIT('.ods.ig_stibat')` | `ods.ig_stibat` (cat='') |
| `'TMP_TABLE_DATASET'` | `LIT('TMP_TABLE_DATASET')` | **give up** (bare name) |
| all-runtime, no literal tail | `OPAQUE…` | **give up** |
| `'PRD_DB.DHB.KOSTEN'` (literal catalog) | `LIT(...)` | `PRD_DB.DHB.KOSTEN` (cat KEPT) |

### Sink predicate (the only dialect-specific piece)

```python
# SnowflakeParser
@staticmethod
def is_dynamic_name_sink(table: exp.Table) -> exp.Expression | None:
    """Return the $var Parameter to resolve if `table` is IDENTIFIER($var) in a
    table position, else None.  Snowflake's IDENTIFIER() is the dynamic-name sink:
    exp.Table.this == exp.Anonymous(name='identifier') wrapping an exp.Parameter."""
```

- Returns the `exp.Parameter` (the `$var`) when `table.this` is an `exp.Anonymous` named
  `identifier` wrapping a Parameter; else `None`.
- Base/ANSI parser: no override → no generic sink ships (Non-Goal).
- **Why this auto-excludes the 91 `ALTER WAREHOUSE` cases + proc-args + WHERE vars:**
  those never parse to an `exp.Table` (`ALTER WAREHOUSE IDENTIFIER()` → `exp.Command`;
  `CALL …($v)` arg is a Parameter not a Table). The resolver only walks `exp.Table`
  sinks, so they are never even asked. This structural gate is the bound — **do not
  replace it with a name blocklist.**

### Var pre-scan (RHS-AST, extends PR-A)

PR-A's pre-scan (on its branch; on **current master** lines **355-364** cover only
`PropertyEQ`) stores `var name → literal string`. Extend it to store
`var name → RHS AST` so the fold can run on demand:

- `exp.PropertyEQ` (`var := <expr>`) — store `stmt.expression` (the AST), not just when
  it is an `exp.Literal`.
- `exp.Set` ▸ `exp.SetItem(kind=VARIABLE)` ▸ `exp.EQ(Column, <expr>)` — store
  `eq.expression` (the AST). This is the **session-variable** form the DWH actually uses;
  PR-A added the `exp.Set` branch on its branch but stored only literals.
- **Last-write-wins** in file source order (study §3.4: only reassignment-looking case is
  the UPDATE-column trap, which is `exp.Update`, structurally distinct — never captured).
- Keep the existing literal-string map used by the EXECUTE IMMEDIATE path
  ([`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py) lines **639-651**,
  `_recover_execute_immediate` / `_EXEC_IMMEDIATE_VAR_RE`, and `_inner_stmts_from_command`
  at line **655**) **untouched** — that path needs the resolved string, not the AST; do
  not regress it. Either keep two maps, or derive the literal map from the AST map for the
  literal-only entries. See [fork F4](#design-forks-needing-a-maintainer-decision).

### Wiring

Rewire `_resolve_identifier_tables` (the literal-only method on PR-A's branch) to:
1. `for table in stmt.find_all(exp.Table)`,
2. `param = self.is_dynamic_name_sink(table)`; if `None`, continue,
3. `new = resolve_dynamic_name(var_env[param.name.lower()], var_env)`; if `None`,
   continue (leave dropped),
4. preserve the FROM/INSERT `alias`, `table.replace(new)`.

Call it **before** `_qualify_bare_tables` (as PR-A does, current master ~line **427**) so
a catalog-less resolved tail still inherits the active `USE SCHEMA` prefix.

### Data Models / API surface

- No graph-schema change. Resolved nodes are ordinary `SqlTable` `db.name` refs that
  merge with existing identities.
- No CLI change. No new MCP tool. `gain` already reports the coverage metrics used in PR-3.

### Dependencies

- sqlglot AST nodes only (`exp.DPipe`, `exp.Concat`, `exp.Subquery`, `exp.Literal`,
  `exp.Parameter`, `exp.Var`, `exp.Anonymous`, `exp.Set`, `exp.SetItem`, `exp.EQ`,
  `exp.PropertyEQ`, `exp.CurrentDatabase`, `exp.SplitPart`, `exp.Substring`) — all
  confirmed present in the study §3.2.
- PR-A scaffolding on `fix/identifier-var-indirection` (NOT yet on master — see
  [fork F2](#design-forks-needing-a-maintainer-decision)).

---

## Implementation Steps

### PR-1 — Generic fold core + exhaustive unit fixtures (no wiring)

**Scope:** Pure `resolve_dynamic_name` (fold + extract + opaque-wildcard + give-up).
No parser change, no sink predicate — falsifiable in isolation.

**Files:**
- `src/sqlcg/parsers/dynamic_name.py` (new) — the core.
- `tests/unit/test_dynamic_name_resolution.py` (new) — fixtures below.

**Anchor (re-confirmed against current master, v1.34.3):** none in the hot path; this is
a new module. The empty-id drop it works around is in
[`base.py`](src/sqlcg/parsers/base.py) (`full_id` lines ~95-99 + the reject-empty rule)
— unchanged by this PR.

**Acceptance (each a behaviour, not a code):**
- direct-concat fold → `DHB.KOSTEN`, catalog empty.
- `(SELECT …)`-wrapped concat → same `DHB.KOSTEN` (descends Subquery/Select).
- 1-hop `$var` chain (`$ODS_DB || '.ods.ig_stibat'`, `ODS_DB`→OPAQUE) → `ods.ig_stibat`.
- all-opaque RHS (only `current_database()`/`split_part`) → returns `None`.
- bare-name give-up (`'TMP_TABLE_DATASET'`, no dot) → returns `None`.
- dynamic-schema give-up (OPAQUE in the schema/name position, tail has no static
  `db.name`) → returns `None`.
- literal-catalog-keep (`'PRD_DB.DHB.KOSTEN'`) → catalog `PRD_DB` retained, NOT collapsed.
- chain-depth bound: a `$a` whose value references `$b` resolves only `$a`'s one hop;
  `$b` is not re-followed (assert the depth-2 ref stays OPAQUE).

**Version:** none on its own (lands with the trio). **Metric impact:** none (no wiring).

**Frozen-perf note:** new module, not on the parser hot path; the four frozen suites
([`test_T09_01_qualify_once.py`](tests/unit/test_T09_01_qualify_once.py),
[`test_bulk_upsert_invariant.py`](tests/unit/test_bulk_upsert_invariant.py),
[`test_upsert_batch_invariant.py`](tests/unit/test_upsert_batch_invariant.py),
[`test_perf_scaling_guard.py`](tests/unit/test_perf_scaling_guard.py)) are untouched and
must stay green.

### PR-2 — Snowflake sink predicate + wiring + RHS-AST pre-scan + perf counter

**Scope:** Land PR-A's scaffolding, add `is_dynamic_name_sink`, extend the pre-scan to
store RHS ASTs, rewire `_resolve_identifier_tables` to call the PR-1 core, add a
perf-guard counter.

**Files:**
- `src/sqlcg/parsers/snowflake_parser.py` — pre-scan (master lines **355-364**, currently
  `PropertyEQ`-only; add the `exp.Set` branch and switch both to store ASTs);
  `is_dynamic_name_sink` (new static method); `_resolve_identifier_tables` (port from
  PR-A branch + rewire to the core); wiring call before `_qualify_bare_tables`
  (master ~line **427**).
- `tests/unit/test_perf_scaling_guard.py` — add a `resolve_dynamic_name` op counter to
  the `count_hot_ops()` context (the file's established counter pattern; see the
  `sg_lineage`/`build_scope`/`traverse_scope` axes ~lines **226-253**); assert it stays
  flat when column count doubles AND fires once per `IDENTIFIER` sink, not per column.
- `tests/unit/test_identifier_var_indirection.py` + the integration test
  (`tests/integration/snowflake/test_identifier_var_indirection_integration.py`) — port
  from PR-A branch, extend with fold cases.

**Anchors (re-confirmed against current master, v1.34.3):**
- Pre-scan: [`snowflake_parser.py`](src/sqlcg/parsers/snowflake_parser.py) lines
  **355-364** (`var_literals` dict, `PropertyEQ` branch only). The `exp.Set` branch and
  `_resolve_identifier_tables` method do **NOT** exist on master — they are PR-A-branch
  only (confirmed: `git show master:… | grep _resolve_identifier_tables` → empty).
- Wiring point: before `_qualify_bare_tables(stmt, current_schema)`, master line **427**.
- EXECUTE IMMEDIATE var path to NOT regress: lines **639-651**
  (`_recover_execute_immediate`, `_EXEC_IMMEDIATE_VAR_RE`) and `_inner_stmts_from_command`
  line **655** — these consume the literal-string map; the AST-map change must keep them
  working.

**Acceptance:**
- `SET v='DL_'||split_part(current_database(),'_',2)||'.DHB.KOSTEN'; FROM identifier($v)`
  → a `DHB.KOSTEN` source edge with non-empty `full_id` (no empty-id leak).
- INSERT INTO `identifier($v)` target resolves the write target.
- JOIN `identifier($v)` resolves.
- MERGE INTO `identifier($v)` resolves.
- `ALTER WAREHOUSE IDENTIFIER($w)` produces **no** resolved table (stays `exp.Command`).
- `CALL MA.MERGE_PREDICTION($SRC_TABLE_NAME, …)` arg produces **no** resolved table.
- 1-hop chain (`$ODS_DB || '.ods.ig_stibat'`) resolves end-to-end through the parser.
- A file with no `IDENTIFIER` sink: edge output unchanged (no-op path).

**Version:** ships the capability → **v1.35.0** (MINOR). Bump
[`pyproject.toml`](pyproject.toml) + [`src/sqlcg/__init__.py`](src/sqlcg/__init__.py) +
`uv lock` in the feature branch per CLAUDE.md release steps.

**Metric impact:** wiring active; real gain measured in PR-3.

**Frozen-perf note:** the fold is once-per-statement on demand — no qualify / build_scope
/ exp.expand / sg_lineage, no per-column op. All four frozen suites must stay green; the
new counter pins the fold's once-per-statement behaviour (a **behavioural** assertion, per
CLAUDE.md guidance, not a volume count that hides in a too-simple fixture).

**Grep-confirmed call sites (required before PR opens):**
- `resolve_dynamic_name` called from `_resolve_identifier_tables`.
- `is_dynamic_name_sink` called from `_resolve_identifier_tables`.
- `_resolve_identifier_tables` called from the file-level statement loop (master ~line
  427 area), before `_qualify_bare_tables`.
- the RHS-AST pre-scan map read by both `_resolve_identifier_tables` and (literal subset)
  the EXECUTE IMMEDIATE path.
No method ships defined-but-uncalled.

### PR-3 — Live-DWH acceptance + `gain --json` before/after

**Scope:** Index the real DWH (`/home/ignwrad/Projects/dwh`) on master and on the feature
branch; record `sqlcg gain --json` before/after; assert the gain and no regression.

**Files:**
- `plan/metrics/generic_var_name_before_v1.34.3.json` (or the actual master version).
- `plan/metrics/generic_var_name_after_v1.35.0.json`.
- `plan/metrics/generic_var_name_gain_context.md` (before/after narrative + the
  catalog-wildcard assumption documented + DWH timing).

**Acceptance (run by the planner during compliance, or the DWH tester):**
- **~136 files gain edges** that previously had none from their `IDENTIFIER($var)` sink
  (count files whose primary FROM/INSERT target newly resolves).
- **No regression:** `good_edges_strict` and `good_edges_scoped` must **not drop** vs the
  before baseline.
- **No-literal-catalog-collision assertion** (the study's decisive safety check): assert
  that no two **distinct literal-catalog** tables were merged into one node by the
  wildcard. The wildcard only fires when the catalog position is OPAQUE; a fully-literal
  `catalog.schema.table` keeps its catalog (PR-1 rule 4), so distinct literal catalogs
  cannot collapse. Verify the corpus still has **zero** fully-literal
  `catalog.schema.table` references that would collide (study §3.4 found zero) — record
  the grep result in the context doc.

**Version:** none (metrics/docs only); merges with the trio under v1.35.0.

**Metric impact:** the headline — ~187 DML refs / ~136 files recovered; record the exact
delta.

**Frozen-perf note:** confirm DWH index time stays within the project's measured baseline
(~210-256s, CLAUDE.md memory) — the fold adds only an O(nodes-in-RHS) walk per
`IDENTIFIER` sink. Record the timing in the context doc.

---

## Test Strategy

- **Unit (PR-1):** the eight fold/extract behaviours above, each named
  `test_<behaviour>_<scenario>_<expected>`, docstring linking this plan. Pure-function,
  no parser, no graph.
- **Unit (PR-2):** sink-predicate positive (FROM/JOIN/INSERT/MERGE) and negative
  (ALTER WAREHOUSE → Command, CALL-arg) cases; end-to-end parse → edge assertions on
  observable `full_id` pairs (the PR-A test pattern: assert edge `src.full_id` /
  `dst.full_id`, and that no empty-id endpoint leaks).
- **Perf (PR-2):** extend `count_hot_ops()` with a `resolve_dynamic_name` counter; assert
  it is flat under doubled columns and fires once per sink (behavioural, not volume).
- **Integration (PR-2):** real in-memory DuckDB, a Snowflake file with a folded
  `IDENTIFIER($v)` source and target, assert the SELECTS_FROM + column-lineage edges land
  with the resolved `db.name`.
- **Live acceptance (PR-3):** `gain --json` before/after on the DWH; the three PR-3
  acceptance assertions above.

> Tests describe the behaviour they verify. The developer names them
> `test_<unit>_<scenario>_<expected>` and links this plan in the docstring. No opaque
> case codes are seeded here.

---

## Acceptance Criteria

- [ ] `resolve_dynamic_name` folds literal/concat/`(SELECT …)`/1-hop-chain to the correct
      catalog-empty `db.name`; gives up (returns `None`) on all-opaque, bare-name, and
      dynamic-schema inputs; keeps a static literal catalog. (PR-1 unit tests pass.)
- [ ] `is_dynamic_name_sink` returns the `$var` Parameter for `IDENTIFIER($v)` in a table
      position and `None` otherwise; `ALTER WAREHOUSE` and `CALL`-arg never resolve.
      (PR-2 unit tests pass.)
- [ ] `FROM/INSERT/JOIN/MERGE identifier($v)` with a folded RHS emits a real edge whose
      endpoints have non-empty `full_id`; no empty-id endpoint leaks. (PR-2 tests pass.)
- [ ] The four frozen perf suites stay green; the new `resolve_dynamic_name` counter stays
      flat when columns double and fires once per sink. (PR-2.)
- [ ] EXECUTE IMMEDIATE literal-var path (lines 639-651 / `_inner_stmts_from_command`) is
      not regressed by the RHS-AST pre-scan change. (PR-2 — existing tests stay green.)
- [ ] Live DWH: ~136 files gain edges; `good_edges_strict` / `good_edges_scoped` do not
      drop; no distinct-literal-catalog collision (grep confirms zero literal
      `catalog.schema.table` refs). (PR-3 — metrics recorded.)

---

## The bound (flow-gate + enforced limits)

This is **not** "resolve all variables". Four enforced gates (study §2.3):

1. **Flow gate — string var → table-position sink only.** The fold fires only when a var
   is dereferenced in an `exp.Table` sink via `is_dynamic_name_sink`. Demand-driven: start
   at the sink, resolve backwards; `SET` definitions are never enumerated.
2. **Fold-depth bound.** Structural recursion over one RHS AST, no fixpoint, no SQL
   execution. O(nodes-in-RHS).
3. **Chain-depth bound = 1** (default, configurable param). One lookup hop into `var_env`;
   the looked-up value's own `$vars` are not re-followed. Corpus max is 1.
4. **Give-up-honestly.** All-opaque / no `db.name` tail / unbound var → leave the sink
   dropped (today's behaviour). Silence over a wrong edge.

Gates (1)+(4) are the structural answer to the maintainer's "resolve all variables" worry:
we resolve the **one** name a table sink demands, fold what is constant, and stop.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Catalog-wildcard collapse merges distinct cross-DB tables | KEEP a static literal catalog (PR-1 rule 4); the wildcard only fires on an OPAQUE catalog. PR-3 asserts zero literal `catalog.schema.table` refs to collide with (study §3.4 = zero). Document the env-parameterisation assumption (fork F1). |
| RHS-AST pre-scan regresses the EXECUTE IMMEDIATE literal path | Keep the literal-string map intact (or derive it); existing tests for lines 639-651 stay green (acceptance criterion). |
| Over-reach (a var resolving where it should not) | Structural flow gate — non-`exp.Table` sinks never reach the resolver; verified for ALTER WAREHOUSE / CALL / WHERE. Do not add a name blocklist that could rot. |
| Fold starts scaling with column/edge count | New perf-guard counter pins once-per-statement / once-per-sink, behaviourally. |
| Branch-scoped reassigned vars | Last-write-wins in source order; none in corpus (study §3.4). Acceptable (better than dropping). |

---

## Design forks needing a maintainer decision

### F1 — Catalog-wildcard env-parameterisation ASSUMPTION (document, do not block)

Collapsing every DB's `DHB.KOSTEN` into one catalog-less node is **correct** in a
single-warehouse, env-parameterised setup (DEV/TST/ACC/PRD are the *same* logical table)
and **safe here** because the corpus has zero fully-literal `catalog.schema.table`
references to collide with (study §3.4). It would be **wrong** in a true cross-DB topology
where `PROD_DB.DHB.KOSTEN` and `STAGING_DB.DHB.KOSTEN` are distinct *and* referenced
literally. **Decision needed:** accept the env-parameterisation assumption (recommended —
it matches the existing catalog-less identity behaviour, and PR-1 rule 4 already keeps a
*static* literal catalog), and document it in the PR-3 context doc + the postmortem. No
code gate beyond rule 4 is proposed; flag if the maintainer wants a hard config switch.

### F2 — PR-A landing strategy (genuine fork — pick one)

PR-A (`fix/identifier-var-indirection`, the literal-only scaffolding) is **NOT merged to
master** and its referenced plan (`sprint_snowflake_lineage_patterns.md`) does not exist on
master. Two options:

- **Option A (recommended):** absorb PR-A's scaffolding directly into PR-2 of this feature
  (port `_resolve_identifier_tables` + the `exp.Set` pre-scan branch + the two test files),
  rewiring to the fold core in the same PR. The literal case becomes a special case of the
  fold (a single `LIT` part), so PR-A as a standalone literal-only PR adds little value and
  would resolve **zero** real DWH cases (study §1).
- **Option B:** merge PR-A first as-is (literal-only), then this feature extends it. Costs
  an extra round-trip for a PR the study says resolves zero real corpus cases on its own.

**Recommendation: Option A** — fold the literal case into PR-2. Maintainer to confirm.

### F3 — Chain depth > 1

Planner found **no** chain deeper than 1 hop in the corpus (study §3.4 re-confirmed). The
`chain_depth` param defaults to 1 and is the enforced bound. No fork unless the maintainer
wants depth > 1 pre-emptively — **not recommended** (would only fold more OPAQUE here).

### F4 — Pre-scan map shape (minor)

Keep two maps (literal-string for EXECUTE IMMEDIATE + RHS-AST for the fold), or one AST
map with a literal-string accessor. Recommend **two maps** for clarity / zero regression
risk to the EXECUTE IMMEDIATE path. Plan-reviewer to confirm the minimal-risk shape.
