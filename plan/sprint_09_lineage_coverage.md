# Sprint Plan: Lineage Coverage via Qualify-Once + Schema-Aware Parsing + Subprocess Isolation

Plan date: 2026-05-12
Author: architect-planner
Source authority: [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) § 12.4 (FIX-E5, FIX-E2, FIX-DDL-SKIP, OPT-SCOPE priority ranking), § 13.4 (open follow-ups from sprint 07), § 14.5 (open follow-ups from sprint 08), CLAUDE.md "Perf budget — 1,600 files under 5 minutes", [`plan/sprint_08_perf_upsert.md`](sprint_08_perf_upsert.md) (perf baseline, T-04 closure dependency, T-05 literal-skip already shipped), live profiling session 2026-05-12 (7-minute COMBI-file stagnation).
Policy: no backward compatibility; re-index is the migration path; no TODO in happy path; serve both small (20-file) and large (1,600-file) repos; every new method has a grep-confirmed call site before PR opens.

---

## Summary

Sprint 08 cut per-call KuzuDB round-trips (bulk upsert), made `--timeout-per-file`
actually fire, suppressed 1,148 pure-literal E1 noise errors, and added a pass-2
skip predicate. The **performance budget** can now be measured on the real
corpus — but **lineage coverage** on the same corpus is still blocked by:

1. The parser does **not** pass an information schema to `sg_lineage()` /
   `qualify()`, so cross-schema CTE chains in IA-DATAPRODUCTS files fail with
   `E5: Cannot find column 'X' in query` (1,280 occurrences in the sprint-07
   baseline).
2. The lineage extractor builds the scope **per column** instead of once per
   statement. On a 176-column file with 11 FULL OUTER JOINs, this caused a
   7-minute stagnation in live profiling. `infer_schema=True` + a
   reused `body_scope` (sqlmesh `qualify-once` pattern) replaces O(n_columns)
   scope rebuilds with O(1).
3. `_extract_column_lineage` is still entered for **pure-DDL files** — files
   whose only statements are `CREATE TABLE` / `exp.Command` nodes carry no
   lineage but contribute to the p99 slow-file tail (18–21 s).
4. The unaliased fallback `str(col_expr)[:40]` passes `"YEAR(BOEKDATUM_FIS)"`
   to `sg_lineage` for unaliased date functions, generating noise E2 errors.
5. `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)` (T-03 from
   sprint 08) cannot SIGKILL a running Python thread. The COMBI files leaked
   parser threads for 7 minutes after the 30-s timeout fired —
   `multiprocessing.Process` is the only way the OS can hard-kill them.
6. Per-column `WARNING: column lineage extraction failed: …` log spam at INFO
   makes the indexer's terminal output unreadable; the E-code summary in
   ARCHITECTURE_REVIEW.md § 12.7 postmortem still has no place in the live log.

This sprint has **seven tickets** and one measurement task that closes sprint 08:

| ID | Title | Effort |
|----|-------|--------|
| T-09-00 | **MEAS-CLOSE-08** — full-index measurement on changelogs (closes sprint 08) | S (no code) |
| T-09-01 | **PARSER-QUALIFY-ONCE** — wire `columns.csv` into `qualify()` + reuse scope across columns | M-L |
| T-09-02 | **FIX-DDL-SKIP** — bypass `_extract_column_lineage` for pure-DDL files | S |
| T-09-03 | **FIX-E2-FUNC-FALLBACK** — guard the unaliased-expression fallback | S |
| T-09-04 | **SUBPROC-ISOLATE** — replace per-file `ThreadPoolExecutor` with `multiprocessing.Process` | M |
| T-09-05 | **E-CODE-FIXTURES** — date-function + aggregate fixtures as acceptance gates for T-09-01 | S |
| T-09-06 | **LOG-VERBOSITY** — column-lineage warnings → DEBUG; E-code summary at INFO | S |

T-09-00 runs first to formally close sprint 08. T-09-01 is the anchor;
T-09-03 and T-09-05 depend on it. T-09-04 is independent and can land in
parallel.

---

## Scope

### In Scope

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — qualify-once
  rewrite (T-09-01), DDL-skip pre-check (T-09-02), E2 function-fallback guard
  (T-09-03), per-column warning → DEBUG (T-09-06)
- [`src/sqlcg/parsers/snowflake.py`](../src/sqlcg/parsers/snowflake.py) — only
  if the qualify-once entry-point needs a dialect-specific override; expected
  to be untouched (the change is in `base.py`)
- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
  — expose a `mapping_schema()` view (T-09-01) — the nested
  `{catalog: {db: {table: {col: type}}}}` shape sqlglot's `qualify()` expects
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) —
  `_index_single_file` swap thread → subprocess (T-09-04); end-of-run E-code
  summary log (T-09-06)
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — minor confidence-
  scoring tweak: with `infer_schema=True`, every resolved edge is emitted at
  `confidence=1.0`; edges where the source column was inferred (not in
  `mapping_schema`) are emitted at `confidence=0.7` (T-09-01)
- New test fixtures under `tests/snowflake/E_date_functions/` and
  `tests/snowflake/E_aggregates/` (T-09-05)
- [`plan/measurements/sprint_08_changelogs_fullindex.json`](measurements/) —
  NEW measurement artifact (T-09-00)
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — populate § 14
  comparison table (T-09-00) + new § 15 sprint-09 subsection

### Non-Goals

- Implementing the Neo4j bulk-upsert path (still `NotImplementedError` per
  § 14.5 follow-up 3 — KuzuDB remains the sole production backend)
- Adding deep CTE-source introspection to `_needs_pass2` (§ 14.5 follow-up 2 —
  no real-corpus regression has surfaced yet)
- Removing `IA-DATAPRODUCTS` from `.sqlcgignore` as a behavioural change —
  T-09-01 + T-09-04 together make it *safe* to remove, but the actual
  `.sqlcgignore` edit and re-measurement of that subtree is deferred to
  sprint 10
- Backend implementations (Neo4j, in-memory variants, etc.)
- New CLI flags. `SQLCG_LOG_LEVEL=DEBUG` already exists and is the documented
  knob for per-column detail (T-09-06)
- Fixes to E4 / E8 / E36 — sprint 09 is about getting the
  qualify-once architecture in place so a future sprint can attack those with
  schema context available

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| `_extract_column_lineage` builds a scope per column when no `scope` kwarg is passed | Confirmed — [`base.py` lines 687–723](../src/sqlcg/parsers/base.py). The `body_scope is None and scope is None` guard short-circuits after the first column, but `body_scope` is local to the call — every `_extract_column_lineage` invocation per statement starts with `body_scope = None`. For a 176-column SELECT this still amortises to one build per statement, but the surrounding `qualify()` is invoked inside `sg_lineage` once *per column* (sqlglot's `lineage()` re-qualifies internally). Reusing a pre-qualified body + cached scope across columns is the sqlmesh pattern. |
| `columns.csv` exists at `/home/ignwrad/Projects/sql-code-graph/columns.csv` with 144,310 rows | Confirmed via `head -1` and `wc -l`. Columns: `TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE`. |
| The KuzuConfig fallback for schema CSV is `<path>/.sqlcg/schema.csv` | Confirmed — [`indexer.py` line 50–51](../src/sqlcg/indexer/indexer.py) docstring: "auto-discovers `<path>/.sqlcg/schema.csv` if present". T-09-01 reuses this same path; tests MUST NOT hardcode `columns.csv` anywhere outside the dev environment. |
| `SchemaResolver.as_dict()` returns `{table: [cols]}` (depth-1) | Confirmed — see the comment at [`base.py` line 726](../src/sqlcg/parsers/base.py): "resolver.as_dict() returns `{table:[cols]}` which has nesting depth 1, causing sqlglot nesting-level errors." Sprint 09 must expose a NEW `mapping_schema()` method returning the depth-3 format sqlglot's `qualify()` accepts: `{catalog: {db: {table: {col: type}}}}`. |
| Sprint-07 baseline error counts: E1=1148, E5=1280 | Confirmed in [`sprint_07_changelogs_noschema.json`](measurements/sprint_07_changelogs_noschema.json) and ARCHITECTURE_REVIEW.md § 12.1. T-05 already drove E1 to 0 (verified in sprint-08 tests); T-09-01 targets the E5 1,280. |
| `cancel_futures=True` does NOT kill a running thread | Confirmed by sprint 08 plan T-03 thread-leak caveat AND live profiling session: COMBI files leaked threads for 7+ minutes after `--timeout-per-file 30` fired. Python's threading model does not allow forced termination. Subprocess is the only path. |
| `ThreadPoolExecutor` is used at [`indexer.py` line ~239](../src/sqlcg/indexer/indexer.py) (`_index_single_file`) | Confirmed by sprint 08 T-03 PASS row; the file currently uses `executor = ThreadPoolExecutor(max_workers=1); …; finally: executor.shutdown(wait=False, cancel_futures=True)`. T-09-04 replaces this with `multiprocessing.Process`. |
| Pass-2 skip identity contract (`resolved is parsed`) is in indexer | Confirmed in § 14.2 of ARCHITECTURE_REVIEW.md. T-09-04's subprocess swap MUST preserve the identity invariant — `_index_single_file` returns the `ParsedFile` produced by the child process via a `Pipe` or `Queue`. The pickled `ParsedFile` is a NEW object; pass-2 skip remains correct because `_needs_pass2` runs on the parent's object identity, not the original. **Critical**: the pass-2 path operates on the *return value* of `_index_single_file`, so as long as that path receives a fully-formed `ParsedFile` (whether built in-process or unpickled from a child), the identity check in `resolve_pass2` is unaffected. |
| `out.errors.append(f"col_lineage:{col}: …")` happens around [`base.py` line 757–765](../src/sqlcg/parsers/base.py) | Confirmed. Error codes are encoded by *substring* in the message (e.g. `"Cannot find column"` → E5). T-09-06 must keep the message format so the existing E-code bucketing keeps working — only the log *level* changes. |
| `tests/snowflake/E*/test_e*.py` is the established fixture-test layout | Confirmed by sprint 07 / sprint 08 (e.g. `tests/snowflake/E8/test_e8.py`). T-09-05 follows the same layout under new `E_date_functions/` and `E_aggregates/` subdirs. |
| Sprint-07 perf budget per CLAUDE.md: 1,600 files under 5 min on a laptop with the schema CSV provided | Confirmed in CLAUDE.md "Target scale" table. The qualify-once rewrite must NOT regress the small-repo path (no `columns.csv` provided). When `mapping_schema` is empty, `qualify()` behaves identically to the pre-T-09-01 call shape (modulo `infer_schema=True`, which only changes error suppression — see T-09-01 confidence-scoring section). |

---

## Ticket Table

| ID | Title | Primary Files | Effort | Priority | Depends on | Blocks |
|----|-------|--------------|--------|----------|-----------|--------|
| T-09-00 | MEAS-CLOSE-08 (closes sprint 08) | new JSON + ARCHITECTURE_REVIEW.md | S | 1 — must run first | sprint 08 merged | T-09-01 baseline comparison |
| T-09-01 | PARSER-QUALIFY-ONCE | [`base.py`](../src/sqlcg/parsers/base.py), [`schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py) | M-L | 2 — anchor | T-09-00 (baseline) | T-09-03, T-09-05 |
| T-09-02 | FIX-DDL-SKIP | [`base.py`](../src/sqlcg/parsers/base.py) | S | 3 | INDEPENDENT | NONE |
| T-09-03 | FIX-E2-FUNC-FALLBACK | [`base.py`](../src/sqlcg/parsers/base.py) | S | 4 | T-09-01 | T-09-05 (Scenario reuse) |
| T-09-04 | SUBPROC-ISOLATE | [`indexer.py`](../src/sqlcg/indexer/indexer.py) | M | 5 | INDEPENDENT (parallel with T-09-01) | NONE |
| T-09-05 | E-CODE-FIXTURES | new fixture files | S | 6 | T-09-01, T-09-03 | NONE |
| T-09-06 | LOG-VERBOSITY | [`base.py`](../src/sqlcg/parsers/base.py), [`indexer.py`](../src/sqlcg/indexer/indexer.py) | S | 7 | INDEPENDENT | NONE |

---

## Recommended Implementation Order

### Single-Developer Sequence

1. **T-09-00** — measurement task, no code. Closes sprint 08, locks in the
   baseline numbers that T-09-01 / T-09-04 will compare against.
2. **T-09-02** — independent and small. Land first so subsequent T-09-01
   measurements aren't polluted by pure-DDL file timings.
3. **T-09-06** — log-verbosity cleanup. Makes the developer's terminal during
   T-09-01 development readable and unblocks the E-code summary used in T-09-00.
4. **T-09-01** — the anchor ticket. Land + measure against the T-09-00 baseline.
5. **T-09-03** — small targeted guard; the qualify-once rewrite already
   addresses most of E2 but the targeted guard is still worthwhile for the
   unaliased case.
6. **T-09-05** — fixtures land last so they capture the post-T-09-01 +
   post-T-09-03 behaviour as the acceptance gate.
7. **T-09-04** — subprocess isolation. Independent of T-09-01; land whenever
   convenient. Keep it out of the same PR as T-09-01 to keep the diff
   reviewable.

### Two-Developer Sequence

- Track A: T-09-00 → T-09-02 → T-09-06 → T-09-01 → T-09-03 → T-09-05
- Track B: T-09-04 (subprocess isolation, independent)

---

## Ticket Specifications

---

### T-09-00 — MEAS-CLOSE-08 (closes sprint 08)

**Source**: [`sprint_08_perf_upsert.md`](sprint_08_perf_upsert.md) Plan Compliance
"Outstanding Work" — T-04 measurement was the only blocker preventing sprint 08
from formally closing.

**Effort**: S (measurement task; no code changes)
**Depends on**: sprint 08 merged
**Blocks**: T-09-01 baseline-comparison cells

#### What to do

**Step 0.1 — Ensure `.sqlcgignore` excludes `IA-DATAPRODUCTS`**

The live profiling session showed COMBI files (176 cols × 11 FULL OUTER JOINs,
all cross-schema) leaked threads for 7+ minutes. Until T-09-01 and T-09-04 land,
those files will dominate the wall-clock and produce misleading numbers. Confirm
the existing `.sqlcgignore` keeps `IA-DATAPRODUCTS/**` out of the run. If it is
absent, **add** that single line — this is the only allowed `.sqlcgignore` edit
in T-09-00.

**Step 0.2 — Run full corpus index**

```bash
uv run sqlcg index /home/ignwrad/Projects/dwh/ddl/changelogs \
    --dialect snowflake \
    --batch-size 50 \
    --db-path /tmp/sqlcg_sprint08_close.kuzu \
    --timeout-per-file 30
```

Record wall time, RSS delta (`/usr/bin/time -v` or `psutil` snapshot at start/end),
and the summary dict.

**Step 0.3 — Anchor edge queries**

Run the two anchor queries from sprint_08 plan Step 4.2 against
`/tmp/sqlcg_sprint08_close.kuzu`:

```cypher
MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn)
WHERE LOWER(d.col_name) = 'omloopsnelheid' RETURN count(e)
```

```cypher
MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn)
WHERE LOWER(d.col_name) = 'ma_aantal_op_order'
   OR LOWER(s.col_name) = 'ma_aantal_op_order' RETURN count(e)
```

**Step 0.4 — Write the measurement JSON**

Path: [`plan/measurements/sprint_08_changelogs_fullindex.json`](measurements/)
(the file specified by sprint_08 Step 4.2). Schema matches the sprint_08 plan's
Step 4.2 spec verbatim — `runtime_seconds_total`, `runtime_seconds_parse`,
`runtime_seconds_upsert`, `pass2_skipped`, `lineage_edges_created`,
`tables_found`, `columns_defined`, `anchor_omloopsnelheid` /
`anchor_ma_aantal_op_order` blocks, `comparison_with_sprint_07`, and the
`budget_check_1600_files` cell.

Add **one new top-level key** beyond sprint_08's spec:

```json
"error_summary": {
  "E1": <int>, "E2": <int>, "E3": <int>, "E5": <int>, "E8": <int>,
  "timeout": <int>, "other": <int>
}
```

This bucket-count is the input to ARCHITECTURE_REVIEW.md § 14 and the
zero-baseline for T-09-01's E5 reduction target.

**Step 0.5 — Populate § 14 comparison table + apply Step 4.5 decision rule**

Sprint_08 plan Step 4.4 specified the table layout. Fill every `__` cell. Then
apply Step 4.5:

- **Budget passes AND `omloopsnelheid` count ≥ 1** → mark sprint 08 CLOSED in
  ARCHITECTURE_REVIEW.md § 14.6 (NEW subsection).
- **Budget passes but `omloopsnelheid` is 0** → file finding under § 14.5
  "synthetic-pass / real-fail parser gap" and CLOSE sprint 08 anyway (T-09-01
  is the structural fix).
- **Budget does NOT pass** → STOP. Sprint 09 cannot start until the architect-
  reviewer is consulted on whether to bring forward parallel parsing.

#### Wiring verification

- `ls plan/measurements/sprint_08_changelogs_fullindex.json` — exists, ≥ 2 KB.
- `grep -c "anchor_omloopsnelheid\|anchor_ma_aantal_op_order\|budget_check_1600_files\|error_summary" plan/measurements/sprint_08_changelogs_fullindex.json` — ≥ 4 matches.
- `grep -n "### 14.6 Sprint-08 Close-Out" ARCHITECTURE_REVIEW.md` — section exists with explicit close / re-open / escalate decision.

#### Files affected

- [`plan/measurements/sprint_08_changelogs_fullindex.json`](measurements/) — NEW
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — § 14 table populated; § 14.6 added

#### Tests to add

None — measurement task.

#### Acceptance criteria

- [ ] Measurement JSON exists with every key listed in sprint_08 Step 4.2 plus
      `error_summary`.
- [ ] Every `__` placeholder in § 14 comparison table is replaced.
- [ ] § 14.6 records the close / re-open / escalate decision verbatim.
- [ ] If budget does NOT pass, sprint 09 is paused and architect-reviewer notified.

---

### T-09-01 — PARSER-QUALIFY-ONCE

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-E5 (rank 1) + OPT-SCOPE (rank 5),
sqlmesh's `core/lineage.py` pattern, live profiling session 2026-05-12 (COMBI
file 7-minute stagnation).

**Effort**: M-L
**Depends on**: T-09-00 (baseline for comparison)
**Blocks**: T-09-03 (Scenario reuse), T-09-05 (acceptance fixtures)

#### Root cause

Two structural problems in [`_extract_column_lineage`](../src/sqlcg/parsers/base.py):

1. **No schema is passed to `qualify()`** — the existing call at
   [`base.py` lines 712–719](../src/sqlcg/parsers/base.py) passes the
   `SchemaResolver.as_dict()` result, but the comment at line 726 confirms
   "schema is NOT passed to sg_lineage because resolver.as_dict() returns
   `{table:[cols]}` (depth-1)". The result: cross-schema CTE columns trigger
   `Cannot find column 'X' in query` (E5, 1,280 occurrences).
2. **Scope is built per `_extract_column_lineage` call, but `sg_lineage()`
   re-qualifies internally** — sqlglot's `lineage()` function re-runs `qualify()`
   for every column unless `scope=` is passed AND the body has already been
   qualified. The current code passes `scope=body_scope` (line 736) but the
   `body` it passes (line 737) is the **unqualified** body, so sqlglot re-qualifies.

The sqlmesh pattern (`sqlmesh/core/lineage.py`):

```python
qualified = qualify(
    query,
    schema=normalize_mapping_schema(mapping_schema, dialect),
    dialect=dialect,
    validate_qualify_columns=False,
    infer_schema=True,
)
scope = build_scope(qualified)
# Then per column:
node = lineage(column_name, qualified, scope=scope, dialect=dialect)
```

The two changes together: qualify the body **once** with the loaded
`columns.csv` mapping schema, build the scope **once**, and pass both
qualified body and scope to every `sg_lineage` call. `infer_schema=True`
ensures that columns absent from `mapping_schema` are inferred from the
SELECT's structural shape instead of raising — making E5 unreachable and
shifting the signal to a `confidence < 1.0` edge.

#### What to do

**Step 1.1 — Add `mapping_schema()` to `SchemaResolver`**

In [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py),
add a method that returns the depth-3 nested format sqlglot's `qualify(schema=…)`
accepts:

```python
def mapping_schema(self) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
    """Return schema in sqlglot's mapping_schema format.

    Shape: ``{catalog: {db: {table: {col: type}}}}``. Used by qualify() to
    resolve cross-schema column references during pass-2 lineage extraction.

    Distinct from as_dict(), which returns the depth-1 ``{table: [cols]}``
    shape used by parser-internal source maps (see base.py:726).

    Empty dict when no schema has been loaded — qualify() then operates in
    infer-only mode (validate_qualify_columns=False + infer_schema=True),
    which is the small-repo default.
    """
```

**Implementation note**: when DATA_TYPE is unknown (a CSV row omits the column),
emit `"UNKNOWN"` as the type. `qualify()` does not validate types — only names
matter. Use `str()` of any non-empty value; do not raise on type unknowns.

**Step 1.2 — Refactor `_extract_column_lineage` to qualify-once pattern**

The new shape — applied **after** the existing source-expansion block at
[`base.py` lines 569–593](../src/sqlcg/parsers/base.py), before the column
iteration loop starts on line 595:

```python
# T-09-01: qualify the body ONCE and cache the scope. Replaces the per-column
# qualify() rebuild that previously dominated wall-time on wide SELECT files
# (live profiling 2026-05-12: 176-col file × 11 joins → 7-minute stagnation).
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

mapping_schema = (
    self._schema.mapping_schema()
    if hasattr(self, "_schema") and self._schema is not None
    else {}
)
try:
    qualified_body = qualify(
        body,
        dialect=self.DIALECT,
        schema=mapping_schema,
        sources=combined_sources or None,
        validate_qualify_columns=False,
        infer_schema=True,
    )
    cached_scope = build_scope(qualified_body)
except Exception as exc:
    # qualify() may fail on dialect edge cases; record and fall back to the
    # raw body. sg_lineage will attempt its own internal qualification per
    # column — slow but correct.
    out.errors.append(f"col_lineage_skip:qualify_failed:{type(exc).__name__}")
    qualified_body = body
    cached_scope = None
```

Then in the per-column loop, replace lines 687–737's "if scope is None build it
now" block with a direct use:

```python
sg_kwargs = {"sources": combined_sources, "dialect": self.DIALECT}
if cached_scope is not None:
    sg_kwargs["scope"] = cached_scope
root = sg_lineage(col_name, qualified_body, **sg_kwargs)
```

**The existing `scope` keyword parameter on `_extract_column_lineage` becomes
unused** — qualify-once removes the caller's ability to pass in a pre-built
scope (callers always passed `None` anyway; grep-verified). Remove the parameter
from the signature; update the two call sites in `base.py` /
`snowflake.py` accordingly.

**Step 1.3 — Confidence scoring under `infer_schema=True`**

With `infer_schema=True`, `sg_lineage()` will succeed (returning a non-None
root) for columns whose source table is absent from `mapping_schema`. The
returned `LineageNode` has no error signal — the caller must distinguish
"resolved from real schema" vs "inferred from structure":

- A column whose source `LineageNode.source` has a leaf table that **IS** in
  `mapping_schema` → emit edge with `confidence=1.0`.
- A column whose source leaf table is **NOT** in `mapping_schema` (inferred-only)
  → emit edge with `confidence=0.7`.
- A column that raises (e.g. genuinely unparseable expression) → emit edge with
  `confidence=0.0` and append `col_lineage:<col>:<exc>` to `out.errors` (existing
  E5 path; signal preserved for debugging even though the bucket count should
  approach zero).

Update `_lineage_node_to_edges` to accept a `mapping_schema_tables` set
(union of `(catalog, db, table)` tuples from the loaded schema) and stamp
confidence accordingly. Pass the set from `_extract_column_lineage`.

**Step 1.4 — Wire `mapping_schema` through `parse_file`**

`SchemaResolver` is already attached to the parser as `self._schema` (see line
[`base.py` line 575](../src/sqlcg/parsers/base.py) — `parser._schema.add_view_sources`).
No new constructor argument needed; T-09-01 reuses the existing
`SchemaResolver` instance. **Critical**: the schema CSV is loaded by
[`indexer.py` lines 67–70](../src/sqlcg/indexer/indexer.py) into the **graph**,
not into `SchemaResolver`. The fix is to **also** call
`schema_resolver.load_from_csv(schema_csv)` at the same point, mirroring the
graph load. Verify the method exists; if it does not, add a thin wrapper that
reuses `_load_schema_into_graph`'s CSV parsing — but DO NOT duplicate the parse.

Grep-confirm the path used by the indexer:

```bash
grep -n "schema.csv\|_load_schema_into_graph\|schema_resolver.load" \
    src/sqlcg/indexer/indexer.py src/sqlcg/lineage/schema_resolver.py
```

If `SchemaResolver` lacks a load-from-csv method, add one with grep-confirmed
call site in the indexer's `__init__` block. **No copying CSV-parsing code** —
extract the parse into a shared helper if needed.

**Step 1.5 — Small-repo regression guard**

When no schema CSV is present, `mapping_schema` is `{}`. `qualify()` with
`schema={}` + `validate_qualify_columns=False` + `infer_schema=True` returns a
qualified body that is structurally identical to the unqualified one for
single-source SELECTs. The small-repo synthetic corpus tests
([`tests/integration/test_indexer_batching.py`](../tests/integration/test_indexer_batching.py)
12-file corpus from sprint 07) must produce byte-identical graphs pre- and
post-T-09-01.

#### Wiring verification

- `grep -n "def mapping_schema" src/sqlcg/lineage/schema_resolver.py` — exactly one definition.
- `grep -n "mapping_schema()" src/sqlcg/parsers/base.py` — at least one call site inside `_extract_column_lineage`.
- `grep -n "infer_schema=True" src/sqlcg/parsers/base.py` — exactly one.
- `grep -n "cached_scope\|qualified_body" src/sqlcg/parsers/base.py` — both names present in the per-column loop.
- `grep -c "build_scope(qualified_body)" src/sqlcg/parsers/base.py` — exactly one (was previously one per column path).
- `grep -n "self._schema.load_from_csv\|schema_resolver.load_from_csv" src/sqlcg/indexer/indexer.py` — exactly one call, adjacent to the existing `_load_schema_into_graph` block.
- `grep -n "TODO" src/sqlcg/parsers/base.py src/sqlcg/lineage/schema_resolver.py` — no new matches in the happy path.

#### Files affected

- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py)

#### Tests to add

Create `tests/unit/test_qualify_once.py`:

- **Scenario A — `mapping_schema()` returns depth-3 nested dict**:
  Build a `SchemaResolver`, load a 3-row CSV (cat=DWH_PRD, db=BA, table=X, cols=[a,b]).
  Assertion: `resolver.mapping_schema() == {"DWH_PRD": {"BA": {"X": {"a": "TEXT", "b": "TEXT"}}}}`.

- **Scenario B — qualify-once: scope is built exactly once for a 100-column SELECT**:
  Use a monkeypatch / counter on `sqlglot.optimizer.scope.build_scope`.
  Action: parse a single statement with 100 projected columns and a single source table.
  Assertion: `build_scope.call_count == 1`. **This is the single most important regression guard for T-09-01.**

- **Scenario C — E5 disappears for cross-schema CTE references**:
  SQL fixture mirrors the IA-DATAPRODUCTS pattern documented in ARCHITECTURE_REVIEW.md § 12.6 Q-12-2 (CTE selecting from `IA_SEMANTIC."Schapbeschikbaarheid"`, outer SELECT referencing the CTE alias). Load `mapping_schema` containing `IA_SEMANTIC.Schapbeschikbaarheid(Rotatie text)`.
  Assertion: `parsed.errors` contains zero `Cannot find column` messages; `column_lineage` contains ≥ 1 edge whose source qualified-id matches `IA_SEMANTIC.Schapbeschikbaarheid`.

- **Scenario D — confidence scoring**:
  SQL: `INSERT INTO db.s.t (a) SELECT src.a FROM db.s.src` with `mapping_schema` containing `db.s.src`.
  Assertion: the edge has `confidence == 1.0`.
  Same SQL with empty `mapping_schema`:
  Assertion: the edge has `confidence == 0.7` (inferred-only).

- **Scenario E — small-repo regression: empty `mapping_schema` produces same graph state**:
  Run the sprint-07 12-file synthetic corpus through `Indexer` with no `schema_csv`.
  Assertion: graph state byte-identical to the pre-T-09-01 snapshot
  (`tables_set`, `columns_set`, edge counts).

- **Scenario F — qualify failure does not crash**:
  Use a malformed AST (e.g. `body` is `None` or an unsupported node).
  Assertion: `parsed.errors` contains exactly one `col_lineage_skip:qualify_failed:<ExcName>` entry; the statement still returns a `LineageExtraction` (potentially with zero edges).

#### Acceptance criteria

- [ ] `mapping_schema()` exists on `SchemaResolver` and is called by `_extract_column_lineage` (grep-confirmed).
- [ ] `build_scope()` is invoked at most **once** per `_extract_column_lineage` call (Scenario B).
- [ ] On the changelogs corpus, `error_summary.E5` drops from 1,280 to **≤ 100** (T-09-05 fixtures + T-09-00 baseline make this quantitative). Verified during the sprint-09 closing measurement.
- [ ] Small-repo synthetic 12-file graph state byte-identical (Scenario E).
- [ ] No new TODO in `base.py` happy path.
- [ ] pyright + ruff clean.

---

### T-09-02 — FIX-DDL-SKIP

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-DDL-SKIP (rank 3).

**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

`_extract_column_lineage` is called for every statement type (`exp.Select`,
`exp.Insert`, `exp.Create`, etc.) — see [`base.py` line 544](../src/sqlcg/parsers/base.py).
For files whose only statements are pure DDL (`CREATE TABLE name (col TYPE, …)`
with no `AS SELECT` body) OR raw `exp.Command` nodes (unparsed Snowflake-
specific syntax), there is no lineage to extract — but the function still
enters its setup block, builds (now-cached) scope, and exits at the
`isinstance(stmt, (exp.Select, exp.Insert, exp.Create))` guard. For a file with
20 such DDL statements that is 20 wasted setup calls.

More importantly, **at the file level**, a file containing only DDL produces
no useful lineage edges. The current code still iterates every statement
inside `parse_file`. ARCHITECTURE_REVIEW.md § 12.4 ranks this an S-effort win
because pure-DDL files appear in the p99 tail (18–21 s observed).

#### What to do

**Step 2.1 — Add a file-level DDL classifier**

In `base.py`, add a helper invoked **once** from `parse_file` before the
per-statement lineage loop:

```python
def _is_pure_ddl_file(self, statements: list[Any]) -> bool:
    """Return True iff every statement is a DDL definition with no SELECT body.

    Pure DDL files (CREATE TABLE name (cols), CREATE OR REPLACE FILE FORMAT,
    exp.Command nodes for Snowflake-specific DDL) cannot produce column-level
    lineage edges. Skipping the lineage extraction loop avoids the p99 slow-file
    tail observed in ARCHITECTURE_REVIEW.md § 12.4.

    A CREATE TABLE … AS SELECT (CTAS) is NOT pure DDL — its expression is an
    exp.Select and it produces lineage. The classifier must let CTAS through.
    """
    import sqlglot.expressions as exp
    if not statements:
        return False
    for stmt in statements:
        if isinstance(stmt, exp.Command):
            continue
        if isinstance(stmt, exp.Create):
            # CTAS or CREATE VIEW AS SELECT — has a SELECT body
            if isinstance(stmt.expression, (exp.Select, exp.Subquery, exp.Union)):
                return False
            continue
        # Any non-DDL statement (Select, Insert, Update, Delete, Merge, …)
        return False
    return True
```

**Step 2.2 — Wire it into `parse_file`**

Where `parse_file` currently iterates `for stmt in parsed.statements:` to call
`_extract_column_lineage`, short-circuit when `_is_pure_ddl_file(parsed.statements)`
is True. Emit a single info-level signal in `parsed.errors`:

```python
if self._is_pure_ddl_file(parsed.statements):
    parsed.errors.append("col_lineage_skip:pure_ddl_file")
    # Defined tables and per-statement metadata are still recorded above; we
    # only skip the lineage-extraction loop.
else:
    for stmt in parsed.statements:
        # … existing _extract_column_lineage call
```

The `col_lineage_skip:pure_ddl_file` marker contributes to a new "skipped"
bucket in T-09-06's E-code summary.

#### Wiring verification

- `grep -n "_is_pure_ddl_file" src/sqlcg/parsers/base.py` — definition + exactly one call site in `parse_file`.
- `grep -n "col_lineage_skip:pure_ddl_file" src/sqlcg/parsers/base.py` — exactly one append.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)

#### Tests to add

Create `tests/unit/test_pure_ddl_skip.py`:

- **Scenario A — Pure DDL file is skipped**:
  SQL: `CREATE TABLE db.s.t (a INT, b TEXT); CREATE TABLE db.s.u (c DATE);`.
  Assertion: `parsed.errors` contains `"col_lineage_skip:pure_ddl_file"`; `parsed.statements[*].column_lineage == []`; defined-tables count is 2.

- **Scenario B — CTAS file is NOT skipped**:
  SQL: `CREATE TABLE db.s.t AS SELECT src.a FROM db.s.src`.
  Assertion: `parsed.errors` does NOT contain `pure_ddl_file`; `parsed.statements[0].column_lineage` has ≥ 1 edge.

- **Scenario C — Mixed DDL + DML file is NOT skipped**:
  SQL: `CREATE TABLE db.s.t (a INT); INSERT INTO db.s.t (a) SELECT src.a FROM db.s.src`.
  Assertion: lineage extracted for the INSERT.

- **Scenario D — `exp.Command` statements still classify as pure-DDL**:
  SQL: `GRANT SELECT ON db.s.t TO ROLE r; CREATE TABLE db.s.t (a INT);`.
  Assertion: `pure_ddl_file` marker present; no lineage attempted.

#### Acceptance criteria

- [ ] `_is_pure_ddl_file` exists with exactly one call site (grep-confirmed).
- [ ] Scenarios A–D pass.
- [ ] No new TODO in happy path.

---

### T-09-03 — FIX-E2-FUNC-FALLBACK

**Source**: ARCHITECTURE_REVIEW.md § 12.4 FIX-E2 (rank 2). Plan input topic 3.

**Effort**: S
**Depends on**: T-09-01 (the qualify-once rewrite is the primary fix for most
E2 — this ticket adds the targeted guard for the residual unaliased case)
**Blocks**: NONE

#### Root cause

For an unaliased expression like `SELECT DATE(boekdatum_fis) FROM …`, the
fallback at [`base.py` around line 615–625](../src/sqlcg/parsers/base.py)
computes `col_name = col_expr.alias or col_expr.name or str(col_expr)[:40]`.
For `exp.Func` nodes with no alias, `col_expr.name` is empty and
`str(col_expr)[:40]` yields `"DATE(boekdatum_fis)"` — which is then passed to
`sg_lineage(col_name=…)`. sqlglot raises `"Cannot find column 'DATE(...)' in
query"` (E2).

Post-T-09-01 with `infer_schema=True`, `sg_lineage` will not raise — but it
will return a `LineageNode` whose `name` is the malformed string, producing
useless edges. A targeted guard at the fallback site emits a structured skip
marker instead.

#### What to do

**Step 3.1 — Replace the silent fallback with a structured skip**

At the call site that currently computes `col_name = col_expr.alias or
col_expr.name or str(col_expr)[:40]`, replace with:

```python
import sqlglot.expressions as exp

if col_expr.alias:
    col_name = col_expr.alias
elif isinstance(col_expr, exp.Column) and col_expr.name:
    col_name = col_expr.name
else:
    # Unaliased expression (function call, arithmetic, CASE WHEN, …). The
    # historical fallback str(col_expr)[:40] passed garbage like
    # "YEAR(BOEKDATUM_FIS)" into sg_lineage and produced E2 noise.
    # Record a structured skip and continue — the target column is unnamed,
    # so the downstream graph cannot model the lineage anyway.
    expr_type = type(col_expr).__name__
    out.errors.append(f"col_lineage_skip:func_fallback:{expr_type}")
    continue
```

This sits **after** the T-05 literal-skip guard (zero `exp.Column` descendants
→ continue) and **after** the T-09-02 pure-DDL file skip. The order is:

1. Star-skip (existing)
2. Literal-skip (T-05, exists)
3. Function/expression fallback skip (T-09-03, NEW)
4. Real column → resolve via sg_lineage

#### Wiring verification

- `grep -n "col_lineage_skip:func_fallback" src/sqlcg/parsers/base.py` — exactly one append.
- `grep -n "str(col_expr)\[:40\]" src/sqlcg/parsers/base.py` — zero matches after the rewrite.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)

#### Tests to add

Add to `tests/snowflake/E_date_functions/test_e_date_functions.py` (shared
with T-09-05):

- **Scenario A — `SELECT DATE(boekdatum_fis)` (unaliased) emits a skip marker**:
  Assertion: `parsed.errors` contains exactly one `"col_lineage_skip:func_fallback:Anonymous"` (or `:Date`, whichever sqlglot's class name resolves to — verify by walking the AST in the test setup); no `col_lineage:` E2 error.

- **Scenario B — `SELECT DATE(boekdatum_fis) AS d` (aliased) resolves normally**:
  Assertion: ≥ 1 lineage edge with target column `d`.

- **Scenario C — `SELECT DATEDIFF(a, b)` (unaliased two-arg) emits a skip marker**:
  Assertion: skip marker recorded; no E2 error.

#### Acceptance criteria

- [ ] `str(col_expr)[:40]` fallback is fully removed (grep-confirmed).
- [ ] Scenarios A–C pass.
- [ ] T-09-00's `error_summary.E2` count drops to **≤ 50** on the changelogs corpus when re-measured at sprint close.
- [ ] No new TODO in happy path.

---

### T-09-04 — SUBPROC-ISOLATE

**Source**: Plan input topic 4. ARCHITECTURE_REVIEW.md § 14.5 follow-up 4.
Live profiling session 2026-05-12 (COMBI files leaked threads for 7+ minutes
after `--timeout-per-file 30`).

**Effort**: M
**Depends on**: INDEPENDENT (parallel with T-09-01)
**Blocks**: NONE

#### Root cause

`ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)` from sprint 08
T-03 prevents the executor from **blocking** on a runaway thread, but cannot
**kill** the thread. Python provides no API to forcibly terminate a thread.
The only way to enforce a hard timeout is to run the parser in a separate OS
process and send SIGKILL on timeout.

#### What to do

**Step 4.1 — Replace `_index_single_file` thread path with subprocess**

Use `multiprocessing.Process` + a `multiprocessing.Queue` to ship the result
back. Sketch:

```python
def _index_single_file(self, parser, path: Path, sql: str, timeout: int) -> ParsedFile:
    if timeout <= 0:
        return parser.parse_file(path, sql)

    import multiprocessing as mp

    ctx = mp.get_context("spawn")  # avoid fork-inherit pitfalls (KuzuDB conn etc.)
    q: mp.Queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_subprocess_parse_worker,
        args=(parser.__class__, parser.DIALECT, path, sql, q),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        # Hard kill — this is the whole point of T-09-04.
        proc.kill()  # SIGKILL on POSIX; TerminateProcess on Windows
        proc.join(timeout=5)
        logger.warning("Timeout parsing %s (>%ds) — subprocess killed", path, timeout)
        out = ParsedFile(path=path, dialect=parser.DIALECT)
        out.errors.append(f"timeout:{timeout}s")
        return out

    try:
        result = q.get(timeout=2)  # finished — pull the pickled ParsedFile
    except queue.Empty:
        logger.warning("Subprocess for %s exited but produced no result", path)
        out = ParsedFile(path=path, dialect=parser.DIALECT)
        out.errors.append("subprocess_empty_result")
        return out

    if isinstance(result, BaseException):
        # Worker raised — re-raise into the caller (matches pre-T-09-04 thread behaviour).
        raise result
    return result
```

Module-level worker (NOT a method — must be pickleable):

```python
def _subprocess_parse_worker(parser_cls, dialect, path, sql, q):
    """Parse a single file in a subprocess; queue the ParsedFile (or exception).

    parser_cls must be the *class* (pickleable), not an instance. The worker
    instantiates the parser inside the child process so that any thread-local
    state from the parent does not leak.
    """
    try:
        parser = parser_cls()
        out = parser.parse_file(path, sql)
        q.put(out)
    except BaseException as exc:
        # Send the exception back; parent will re-raise.
        try:
            q.put(exc)
        except Exception:
            # If exc isn't pickleable, send a RuntimeError summary instead.
            q.put(RuntimeError(f"subprocess parse failed: {type(exc).__name__}: {exc}"))
```

**Step 4.2 — Pickleability check**

`ParsedFile` and its nested `StatementNode` / `LineageEdge` / `TableRef`
dataclasses must pickle cleanly through the queue. Confirm via a unit test
(Scenario B below). If a field (e.g. a sqlglot AST node retained on
`StatementNode`) is not pickleable, refactor to strip it before queueing OR
mark the field with `repr=False, compare=False, default=None` and rebuild on
the parent side. **Do not** silently drop fields — the developer must
explicitly approve any stripping in a separate commit before T-09-04 lands.

**Step 4.3 — `spawn` context choice**

`mp.get_context("spawn")` is required. `fork` would inherit the KuzuDB
connection file descriptor; on POSIX the child closing it could damage the
parent's connection. `spawn` ensures a fresh interpreter — incurs ~50 ms
startup cost per file. This is **acceptable** because the surrounding
indexer pipeline is already file-at-a-time; 796 files × 50 ms = 40 s amortised
overhead. T-09-00 measurement provides the upper bound; if the overhead
pushes the 1,600-file extrapolation above the 5-min budget, revisit with a
worker-pool pattern (sprint 10).

**Step 4.4 — Small-repo regression**

For repos with `--timeout-per-file 0` (or unset), the function short-circuits
and runs `parser.parse_file` in the parent process. The 20-file ETL fixture
test (sprint-07 small-repo regression) must remain byte-identical.

#### Wiring verification

- `grep -n "multiprocessing\|mp.get_context\|proc.kill" src/sqlcg/indexer/indexer.py` — at least three matches inside `_index_single_file` or an adjacent module-level worker.
- `grep -n "_subprocess_parse_worker" src/sqlcg/indexer/indexer.py` — definition + exactly one call site.
- `grep -n "ThreadPoolExecutor" src/sqlcg/indexer/indexer.py` — zero matches (the old path is fully removed).
- `grep -n "TODO" src/sqlcg/indexer/indexer.py` — no new matches.

#### Files affected

- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py)

#### Tests to add

Create `tests/unit/test_subprocess_isolate.py`:

- **Scenario A — slow parser is hard-killed within `timeout + 5 s`**:
  Use a fake parser whose `parse_file` runs `time.sleep(60)`. Call
  `_index_single_file(fake, Path("x.sql"), "select 1", timeout=2)`.
  Assertion: wall-clock < 8 s; returned `ParsedFile.errors == ["timeout:2s"]`. **Critical**: this proves the OS killed the worker — a leaked thread would block far longer. **Most important regression guard for T-09-04.**

- **Scenario B — `ParsedFile` round-trips through the queue without loss**:
  Use a real parser on a tiny SQL string. Assert all fields on the returned `ParsedFile` (dialect, statements, errors, defined_tables, sources) match an in-process parse of the same SQL byte-for-byte.

- **Scenario C — Worker exception propagates to the parent**:
  Use a parser whose `parse_file` raises `ValueError("bad sql")`. Assertion: `_index_single_file` raises `ValueError` (or wrapping `RuntimeError` if the exception is not pickleable; verify exactly one path is taken).

- **Scenario D — `timeout=0` bypasses subprocess**:
  Patch `multiprocessing.Process` to raise if instantiated. Call `_index_single_file(parser, path, sql, timeout=0)`. Assertion: returns directly; the patch never fires.

- **Scenario E — Subprocess exit without queue write produces structured error**:
  Use a parser whose `parse_file` calls `os._exit(0)` (worker exits before queueing). Assertion: `ParsedFile.errors == ["subprocess_empty_result"]`; no hang.

#### Acceptance criteria

- [ ] `multiprocessing.Process` is the only timeout mechanism in `_index_single_file` (grep-confirmed; no `ThreadPoolExecutor` remains).
- [ ] Scenarios A–E pass.
- [ ] On the changelogs corpus (re-measured at sprint close), `--timeout-per-file 5` returns from a known-slow 22-s file in < 8 s wall-clock — a 60% improvement over the sprint-08 thread-abandon behaviour.
- [ ] No new TODO in happy path.
- [ ] No regression on the 20-file ETL small-repo fixture (byte-identical graph state).

---

### T-09-05 — E-CODE-FIXTURES

**Source**: Plan input topic 6.

**Effort**: S
**Depends on**: T-09-01, T-09-03 (acceptance gates for those tickets)
**Blocks**: NONE

#### Root cause

The existing `tests/snowflake/E*/` directories cover E2, E3, E5, E8, E36 — but
have no fixtures for date functions or aggregate expressions, which are the
specific patterns that exposed the qualify-once + E2-fallback gaps. Without
fixtures, the sprint-09 acceptance criteria for T-09-01 / T-09-03 are
unverifiable in CI.

#### What to do

**Step 5.1 — Date-function fixtures**

Create `tests/snowflake/E_date_functions/`:

- `fixture_date_unaliased.sql` — `INSERT INTO db.s.t (a) SELECT DATE(boekdatum_fis) FROM db.s.src`
- `fixture_date_aliased.sql` — same with `… AS d`
- `fixture_year_unaliased.sql` — `INSERT INTO db.s.t (a) SELECT YEAR(boekdatum_fis) FROM db.s.src`
- `fixture_datediff_unaliased.sql` — `INSERT INTO db.s.t (a) SELECT DATEDIFF(d1, d2) FROM db.s.src`

`test_e_date_functions.py` — one parameterised test asserting:
- Unaliased variants emit `col_lineage_skip:func_fallback:*` and zero E2 errors.
- Aliased variants produce ≥ 1 lineage edge.

**Step 5.2 — Aggregate fixtures**

Create `tests/snowflake/E_aggregates/`:

- `fixture_sum_present_source.sql` — `INSERT INTO db.s.t (a) SELECT SUM(amount) AS s FROM db.s.src` with `mapping_schema` providing `db.s.src(amount NUMBER)`.
- `fixture_sum_case_when.sql` — `… SELECT SUM(CASE WHEN flag = 1 THEN amount END) AS s FROM db.s.src`.
- `fixture_sum_absent_cross_schema.sql` — `… SELECT SUM(amount) AS s FROM IA_SEMANTIC."Transactie"` with **empty** `mapping_schema`.

`test_e_aggregates.py` — assertions:
- `fixture_sum_present_source` produces an edge with `confidence == 1.0` (source in schema).
- `fixture_sum_case_when` produces an edge with `confidence == 1.0`.
- `fixture_sum_absent_cross_schema` produces an edge with `confidence == 0.7` (inferred-only), zero E5 errors.

**Step 5.3 — Wire into the existing test runner**

The pre-existing `tests/snowflake/conftest.py` (if present) loads fixtures by
walking the `tests/snowflake/E*/` directory tree. Verify the new subdirs match
the discovery pattern; if not, extend the fixture loader **without** introducing
a hardcoded list.

#### Wiring verification

- `ls tests/snowflake/E_date_functions/*.sql` — 4 files.
- `ls tests/snowflake/E_aggregates/*.sql` — 3 files.
- `grep -n "test_e_date_functions\|test_e_aggregates" tests/` — at least one `def test_*` in each new file.

#### Files affected

- `tests/snowflake/E_date_functions/` — NEW
- `tests/snowflake/E_aggregates/` — NEW

#### Tests to add

See Step 5.1 / 5.2 (the fixtures *are* the tests).

#### Acceptance criteria

- [ ] All 7 fixture files exist.
- [ ] Each fixture's `test_*` passes against the post-T-09-01 + post-T-09-03 parser.
- [ ] Each fixture's `test_*` **fails** against the pre-T-09-01 parser — verify by running once on master before merging. (Document the master-side failure in the PR description.)

---

### T-09-06 — LOG-VERBOSITY

**Source**: Plan input topic 7. ARCHITECTURE_REVIEW.md § 12.7 postmortem.

**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

[`base.py` line 756–758](../src/sqlcg/parsers/base.py) emits
`logger.warning("column lineage extraction failed: file=%s col=%s error=%s", …)`
for every column that hits the E5 path. On the 796-file changelogs corpus the
indexer's stderr is buried under thousands of these lines. The signal — "how
much lineage are we losing, and to what error class?" — is invisible without
post-processing the captured output.

The fix is two-part:

1. Lower the per-column message to `DEBUG`. The existing structured
   `out.errors.append(...)` already records the same information programmatically
   — that path is the canonical signal for measurement.
2. At end of indexing, emit one `INFO`-level summary line: `Indexing complete:
   796 files — E5: 312, E8: 67, E2: 45, E3: 8, timeout: 3, pure_ddl_skip: 124,
   func_fallback: 89`.

The user controls finer detail via `SQLCG_LOG_LEVEL=DEBUG` — already the
documented knob. No new CLI flag.

#### What to do

**Step 6.1 — Demote per-column warning to DEBUG in `base.py`**

Replace `self._log.warning(...)` at [`base.py` line 756](../src/sqlcg/parsers/base.py)
with `self._log.debug(...)`. Same call signature; same message format. The
`out.errors.append(...)` call on the next line is **unchanged** — it remains
the programmatic signal.

Apply the same demotion to **any other** `self._log.warning("column lineage
…")` call in `base.py` / `snowflake.py`. Grep before editing.

**Step 6.2 — Aggregate E-code summary in indexer**

In [`indexer.py`](../src/sqlcg/indexer/indexer.py), at the end of `index_repo`
just before returning the summary dict, classify each `ParsedFile.errors`
entry into a bucket and emit:

```python
def _classify_error(msg: str) -> str:
    """Map a structured error message to its E-code bucket.

    Buckets:
      - "E1"   : col_lineage:NULL:Cannot find column 'NULL' (driven to 0 by sprint 08 T-05)
      - "E2"   : col_lineage:...:Cannot find column '...' where the column name contains '(' or non-identifier chars
      - "E3"   : col_lineage:...:Expecting / Invalid expression / Unexpected token
      - "E5"   : col_lineage:...:Cannot find column (plain identifier)
      - "E8"   : col_lineage_skip:dynamic_source
      - "timeout": timeout:Ns
      - "pure_ddl_skip": col_lineage_skip:pure_ddl_file
      - "func_fallback": col_lineage_skip:func_fallback:*
      - "qualify_failed": col_lineage_skip:qualify_failed:*
      - "other": anything else
    """
```

The classifier MUST be implemented as a single function in
[`src/sqlcg/indexer/error_classify.py`](../src/sqlcg/indexer/) (NEW file) so
T-09-00's measurement script and the in-line summary share the same logic.
Grep-confirm the call site.

Emit the summary as one `INFO` log line at the very end of `index_repo`:

```python
logger.info(
    "Indexing complete: %d files — %s",
    summary["files_parsed"],
    ", ".join(f"{k}: {v}" for k, v in summary["error_summary"].items() if v > 0),
)
```

Add `"error_summary": <dict>` to the returned summary dict so MEAS-CLOSE-08
and future measurement runs read the same shape.

#### Wiring verification

- `grep -n "self._log.warning.*column lineage" src/sqlcg/parsers/base.py src/sqlcg/parsers/snowflake.py` — zero matches.
- `grep -n "self._log.debug.*column lineage" src/sqlcg/parsers/base.py` — at least one match.
- `grep -n "_classify_error\|error_summary" src/sqlcg/indexer/error_classify.py src/sqlcg/indexer/indexer.py` — definition + at least one call site in indexer.
- `grep -n "Indexing complete:" src/sqlcg/indexer/indexer.py` — exactly one INFO log.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py)
- [`src/sqlcg/parsers/snowflake.py`](../src/sqlcg/parsers/snowflake.py)
- [`src/sqlcg/indexer/error_classify.py`](../src/sqlcg/indexer/) — NEW
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py)

#### Tests to add

Create `tests/unit/test_error_classify.py`:

- **Scenario A — Each documented bucket maps correctly**:
  Table-driven test of (message, expected_bucket) pairs covering all 10 buckets.

- **Scenario B — Unknown message maps to `"other"`**:
  Input: `"some new error string"`. Assertion: bucket == `"other"`.

- **Scenario C — End-to-end summary on small-repo fixture**:
  Index the 12-file synthetic corpus. Assertion: `summary["error_summary"]` dict has every bucket key present (value may be 0).

- **Scenario D — INFO log line is emitted exactly once**:
  Capture logs via `caplog`. Assertion: exactly one record with `"Indexing complete:"` and level INFO.

- **Scenario E — Per-column WARN is silent at default level**:
  Trigger a known E5 (parse a SQL with unresolvable column on the small-repo).
  Assertion: no WARNING records contain `"column lineage extraction failed"`; the same message DOES appear as DEBUG.

#### Acceptance criteria

- [ ] `error_classify.py` exists with `_classify_error()` and the 10 documented buckets.
- [ ] Per-column WARNINGS demoted to DEBUG (grep-confirmed).
- [ ] One INFO `Indexing complete:` line emitted per run (Scenario D).
- [ ] `summary["error_summary"]` dict present (Scenario C).
- [ ] No new TODO in happy path.

---

## Test Strategy

### The Single Most Important Regression Guard

**File**: `tests/unit/test_qualify_once.py::test_build_scope_called_once_per_statement`
(Scenario B of T-09-01).

**Why**: The whole point of T-09-01 is that scope-build no longer runs per
column. If a future refactor accidentally re-introduces the per-column rebuild
(easy to do — the `body_scope is None` short-circuit was already in the code
pre-T-09-01 but operated at the wrong scope), the 7-minute COMBI-file
stagnation will silently return. The `build_scope.call_count == 1` assertion
is the contract.

### Secondary regression guards

| Ticket | Test file | Layer |
|--------|-----------|-------|
| T-09-00 | n/a — measurement task | n/a |
| T-09-01 | `tests/unit/test_qualify_once.py` | unit (parser-only) + small-repo equivalence integration |
| T-09-02 | `tests/unit/test_pure_ddl_skip.py` | unit (parser-only) |
| T-09-03 | `tests/snowflake/E_date_functions/test_e_date_functions.py` | snowflake-fixture unit |
| T-09-04 | `tests/unit/test_subprocess_isolate.py` | unit (real subprocess, fake parser) |
| T-09-05 | `tests/snowflake/E_date_functions/`, `tests/snowflake/E_aggregates/` | snowflake-fixture unit |
| T-09-06 | `tests/unit/test_error_classify.py` | unit (no graph backend) |

### Carried-forward guards

- `tests/integration/test_bulk_upsert.py::test_bulk_path_equivalence_vs_baseline` (sprint 08 T-01) — must keep passing.
- `tests/integration/test_live_anchors.py::test_live_anchor_omloopsnelheid_has_column_lineage_edges` (sprint 07) — must keep passing AND its real-corpus counterpart in T-09-00 must produce ≥ 1 edge.

---

## Wiring Checklist

| Question | T-09-00 | T-09-01 | T-09-02 | T-09-03 | T-09-04 | T-09-05 | T-09-06 |
|----------|---------|---------|---------|---------|---------|---------|---------|
| What calls this? | CLI `sqlcg index` (existing). | `parse_file` calls `_extract_column_lineage`; the qualify-once block sits inside `_extract_column_lineage`. `mapping_schema()` is called from `_extract_column_lineage`. `schema_resolver.load_from_csv()` is called from `Indexer.__init__`. | `parse_file` calls `_is_pure_ddl_file` once before the lineage loop. | The unaliased-fallback branch inside `_extract_column_lineage` (replacing the `str(col_expr)[:40]` line). | `index_repo` pass-1 loop calls `_index_single_file`. The module-level `_subprocess_parse_worker` is the subprocess entry point. | The pytest collector under `tests/snowflake/E_date_functions/` and `tests/snowflake/E_aggregates/`. | `index_repo` calls `_classify_error` at end of run. `self._log.debug` replaces `self._log.warning` inline. |
| Where is the parameter passed? | No new CLI flag. The measurement script writes JSON. | No new CLI flag. `mapping_schema` flows from `SchemaResolver` already attached to the parser as `self._schema`. The schema CSV path is the existing `--schema-csv` indexer argument with `<path>/.sqlcg/schema.csv` fallback (matches `KuzuConfig` convention; see [`indexer.py` line 51](../src/sqlcg/indexer/indexer.py)). | No new parameter. | No new parameter. | The `--timeout-per-file` argument already exists; T-09-04 changes the implementation, not the surface. | No new parameter — fixtures are discovered. | `SQLCG_LOG_LEVEL=DEBUG` is the existing knob — documented in CLAUDE.md. |
| What constant/path does this align with? | Output filename matches sprint_08 plan Step 4.2: `sprint_08_changelogs_fullindex.json`. Corpus path matches sprint-07 / sprint-08 baseline. | Schema CSV fallback path: `<path>/.sqlcg/schema.csv` per [`indexer.py` line 51](../src/sqlcg/indexer/indexer.py). DEV-MACHINE schema file at `/home/ignwrad/Projects/sql-code-graph/columns.csv` is NOT to be referenced in code or tests — it is a local convenience, not a project constant. | `exp.Command` is sqlglot's canonical class for unparsed statements (used elsewhere in `base.py`). | `exp.Column` is the same canonical class used by the T-05 literal-skip and the star-skip block. | `multiprocessing.get_context("spawn")` is the cross-platform-safe default; no project-level constant. `daemon=True` matches the indexer's run-to-completion semantics. | Fixture directory naming follows the existing `tests/snowflake/E*/` convention (e.g. `tests/snowflake/E8/`). | Error-bucket names match the E{n} taxonomy documented in `.claude/projects/.../project_en_taxonomy.md` (memory) AND in ARCHITECTURE_REVIEW.md § 12.3. The 10 bucket names are the source of truth — no new bucket names introduced without updating both. |
| Does any TODO remain in the happy path? | No. | No. | No. | No. | No. | No. | No. |

---

## Acceptance Criteria (sprint-level)

- [ ] T-09-00 measurement JSON exists; sprint 08 is formally closed in ARCHITECTURE_REVIEW.md § 14.6.
- [ ] `error_summary.E5` on the changelogs corpus drops from sprint-08 baseline (≥ 1,000) to ≤ 100. Recorded in the new sprint-09 closing measurement under [`plan/measurements/sprint_09_changelogs_fullindex.json`](measurements/).
- [ ] `error_summary.E2` drops to ≤ 50 on the same corpus.
- [ ] `error_summary.pure_ddl_skip` is observable (T-09-02 + T-09-06 wired correctly).
- [ ] `build_scope` is called at most once per `_extract_column_lineage` invocation (T-09-01 Scenario B).
- [ ] `--timeout-per-file 5` returns from a known 22-s slow file in < 8 s wall-clock (T-09-04 Scenario A).
- [ ] All 7 T-09-05 fixtures pass post-T-09-01 + post-T-09-03; documented as failing on master in the PR.
- [ ] `Indexing complete: …` INFO line appears exactly once per indexer run (T-09-06 Scenario D).
- [ ] Per-column WARNING noise eliminated at default log level (T-09-06 Scenario E).
- [ ] Small-repo 20-file ETL fixture: byte-identical graph state pre- and post-sprint (no small-repo regression).
- [ ] 1,600-file budget extrapolation: re-measured at sprint close — must remain ≤ 300 s.
- [ ] No new TODO in any happy-path file.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `infer_schema=True` masks **real** unresolvable columns as low-confidence inferred edges, hiding correctness regressions | MEDIUM | Confidence-scoring rule (T-09-01 Step 1.3) tags inferred-only edges with `0.7`. The MCP tools can filter on confidence. T-09-05 Scenario D pins the contract. If a sprint-10 measurement shows misclassified edges, tighten the rule to emit `confidence=0.5` when the source table is also missing. |
| Pickling a `ParsedFile` through a subprocess `Queue` fails for some field type (e.g., a sqlglot AST retained on `StatementNode`) | MEDIUM | T-09-04 Scenario B exercises the round-trip on a real ParsedFile. If a field fails, the developer must explicitly refactor that field (strip AST refs, hold only stringified summaries). Stripping is gated by a separate commit per Step 4.2. |
| Subprocess `spawn` startup cost (50 ms/file × 1,600 files = 80 s) overshoots the budget | LOW–MEDIUM | T-09-04 only spawns when `--timeout-per-file > 0`. The sprint-close measurement quantifies the overhead; if it pushes the extrapolation past 5 min, sprint 10 brings forward a process-pool variant. |
| `qualify()` with `validate_qualify_columns=False` silently changes lineage shape on existing fixtures (e.g., aliased references inside CTEs) | LOW | Small-repo synthetic 12-file corpus equivalence (T-09-01 Scenario E) is the regression guard. Anchor `omloopsnelheid` synthetic test must keep passing. |
| `_is_pure_ddl_file` mis-classifies a CTAS as pure-DDL (false positive) | LOW | T-09-02 Scenario B pins the CTAS case. `exp.Create.expression` is the canonical sqlglot attribute checked. |
| `mapping_schema()` shape mismatches what sqlglot's current `qualify()` version accepts | LOW | sqlglot version is pinned in `pyproject.toml`; the qualify-once pattern is the documented sqlmesh approach. T-09-01 Scenario A pins the dict shape; if sqlglot upgrades and changes the API, that test fails loudly. |
| E-code bucket "other" grows large because a new error message format slips in unclassified | LOW | T-09-06 Scenario B explicitly asserts "other" is the safety net. The architect-reviewer reviews the bucket counts at sprint close — if "other" > 5% of total, add a finding to ARCHITECTURE_REVIEW.md § 12.3 and add a bucket. |
| T-09-00's measurement run takes longer than expected because `IA-DATAPRODUCTS` is mis-included (no `.sqlcgignore` rule) | MEDIUM | Step 0.1 makes the `.sqlcgignore` check an explicit gate. If the file is missing the line, T-09-00 stops and adds it before running. |
| Removing the `scope=` parameter from `_extract_column_lineage` (T-09-01 Step 1.2) breaks an unknown caller | LOW | Grep-confirm zero non-test callers pass `scope=` before removal. Tests in `tests/unit/test_extract_column_lineage.py` (if present) are updated to drop the kwarg. The plan-reviewer should re-grep before approving the PR. |

---

## Blocking Questions

None. All architecture decisions are documented:

- **Qualify-once pattern** — explicit sqlmesh reference (`core/lineage.py`) with the exact kwargs (`infer_schema=True`, `validate_qualify_columns=False`).
- **Schema CSV path convention** — `<path>/.sqlcg/schema.csv` per the existing `KuzuConfig`-aligned fallback in [`indexer.py` line 51](../src/sqlcg/indexer/indexer.py).
- **Confidence-scoring rule** — `1.0` for schema-backed, `0.7` for inferred-only, `0.0` for raised; matches the existing `LineageEdge.confidence` field semantics.
- **Subprocess context** — `spawn` (cross-platform; no fork-inherit pitfalls on the KuzuDB connection).
- **E-code bucket names** — sourced from ARCHITECTURE_REVIEW.md § 12.3 and the `project_en_taxonomy.md` memory file.
- **Log knob** — `SQLCG_LOG_LEVEL=DEBUG` (existing, documented in CLAUDE.md).
- **`.sqlcgignore` IA-DATAPRODUCTS exclusion** — already in place per sprint 08; T-09-00 only verifies, does not edit beyond confirmation.

---

## Postmortem

To be filled in at sprint close by the developer / architect-reviewer.

### Measurement Targets vs. Actuals

| Target | Goal | Actual | Pass |
|--------|------|--------|------|
| `error_summary.E5` on changelogs corpus | ≤ 100 (from ≥ 1,000 baseline) | __ | __ |
| `error_summary.E2` on changelogs corpus | ≤ 50 | __ | __ |
| `build_scope` calls per `_extract_column_lineage` | ≤ 1 | __ | __ |
| `--timeout-per-file 5` on 22-s slow file | < 8 s wall-clock | __ | __ |
| 1,600-file budget extrapolation | ≤ 300 s | __ | __ |
| Small-repo 20-file regression | byte-identical graph state | __ | __ |
| Per-column WARNING noise at default level | zero records | __ | __ |
| `Indexing complete:` INFO log | exactly one per run | __ | __ |
| All 7 T-09-05 fixtures | pass post-merge, fail on master | __ | __ |

### What Worked

- _(developer fills in)_

### What Did Not Work

- _(developer fills in)_

### Findings to Carry Forward

- _(architect-reviewer fills in)_

### Decision Outcome

- _Did the qualify-once rewrite drive E5 to ≤ 100? If yes, sprint 10 can target E8 / E36 with schema context available. If no, re-open T-09-01 with a sub-ticket and consult architect-reviewer on the residual E5 root cause._
- _Did subprocess isolation enable safe removal of IA-DATAPRODUCTS from `.sqlcgignore`? Sprint 10 scope decision._
