# Feature Plan: Parsing Errors Experiment

Plan date: 2026-05-06
Author: architect-planner
Status: RESEARCH PLAN — no implementation tickets, no code changes

---

## Summary

This is a **research plan, not an implementation plan**. The goal is to run a
controlled diagnostic experiment against the 1,457-file Snowflake DWH corpus, collect
and categorize every parse/lineage error, measure per-file timing, and emerge with enough
facts to rank and scope fixes in a follow-on sprint. The plan ends at "we know what to
fix and in what order." No fix is written during this experiment.

---

## Scope

### In Scope

- Design of `scripts/collect_parse_errors.py` — the diagnostic script (plan only; the
  developer writes the code)
- Research into sqlglot `lineage()` API parameters (`sources=`, `schema=`, `scope=`) to
  understand what options are available for alias resolution
- Experiment execution steps: exact commands, expected runtime, expected output format
- Error category taxonomy (what counts as category 1 vs 2 vs 3 vs 4)
- Success criteria: what we need to learn before writing any fix

### Non-Goals

- Implementing any fix to `base.py`, `ansi_parser.py`, or `indexer.py`
- Changes to the KuzuDB graph schema
- Anything related to the v0.3.1 sprint tickets (P-01 through P-06, already shipped)
- Performance optimisation of the production indexer
- BigQuery, ANSI, or multi-dialect coverage — this experiment is Snowflake-dialect only

---

## Background: What We Already Know

### sqlglot lineage() API — confirmed by source inspection

The installed `sqlglot.lineage.lineage()` function has this signature:

```python
def lineage(
    column: str | exp.Column,
    sql: str | exp.Expr,
    schema: dict | Schema | None = None,
    sources: Mapping[str, str | exp.Query] | None = None,
    dialect: DialectType = None,
    scope: Scope | None = None,
    trim_selects: bool = True,
    copy: bool = True,
    **kwargs,
) -> Node:
```

The `Node` dataclass fields (confirmed):
- `name: str` — column name at this node
- `expression: exp.Expr` — the expression this node represents
- `source: exp.Expr` — the upstream source (Table or Select for subqueries)
- `downstream: list[Node]` — child nodes (NOT upstream; tree is inverted: root = output)
- `source_name: str` — name of the source scope
- `reference_node_name: str` — for cross-scope references

**Critical: the tree is downstream-first, not upstream-first.** The root node is the
output column. Leaf nodes (those with no `downstream` children) are the ultimate source
tables. The `_lineage_node_to_edges` implementation in `base.py` (P-03) already
traverses `downstream` correctly.

### schema= parameter format mismatch (confirmed)

`SchemaResolver.as_dict()` returns `{catalog: {db: {table: [col, col, ...]}}}` — the
column list is a Python list of strings, not a dict of `{col_name: col_type}`.

`sqlglot.lineage()` passes `schema` to `qualify.qualify()`, which wraps it in a
`sqlglot.Schema` object. `sqlglot.Schema` accepts both list and dict formats for
columns, but treats a list as column names without types. This is **not a bug** — sqlglot
handles the list format gracefully. What matters is whether column qualification succeeds
when schema is passed. This needs empirical verification in the experiment.

### sources= parameter purpose (confirmed)

`sources` accepts `Mapping[str, str | exp.Query]` — a mapping of scope name to SQL
string or parsed query. It is used to expand references to CTEs or subqueries defined
**outside** the current SQL string (e.g., in another file). It calls `exp.expand()` on
the expression before qualification. This is relevant for cross-file lineage chains but
**not** for inline subquery alias resolution (which qualify() handles internally).

### Alias resolution finding (confirmed by experiment)

The error `Cannot find column 't1.MA_AANTAL_FTE' in query` occurs when `col_name` passed
to `sg_lineage()` contains the table alias prefix (e.g., `'t1.MA_AANTAL_FTE'` instead of
`'MA_AANTAL_FTE'`). Testing confirmed:

- For a raw `exp.Column` node with `table='t1'` and `name='MA_AANTAL_FTE'`:
  - `col_expr.alias` = `''` (empty)
  - `col_expr.name` = `'MA_AANTAL_FTE'` (without table prefix)
  - Therefore `col_name = 'MA_AANTAL_FTE'` — **correct**
- `sg_lineage('MA_AANTAL_FTE', body)` on a query with `SELECT t1.col FROM (SELECT col
  FROM src) t1` **succeeds** — qualify() resolves the subquery alias internally

**Implication**: the `t1.MA_AANTAL_FTE` error in the v0.3.0 DWH run originated from a
different code path than the one currently in `base.py` (post P-03). The P-03 fix may
have already eliminated this error category. The experiment will confirm whether this
error still appears after P-03 is merged.

### Unsupported syntax (confirmed)

`ALTER DYNAMIC TABLE`, `ALTER WAREHOUSE IDENTIFIER(...)` → parsed as `Command` nodes.
`ALTER TABLE ... SET TAG` and `CREATE TABLE ... CLONE` → parsed normally (not `Command`).
The `Command` fallback is sqlglot's intended behaviour for unsupported DDL — it is not
an error, just a parse quality degradation.

### Performance baseline (confirmed)

On a complex 5-CTE query, `sg_lineage()` takes 5–20 ms per column. For a 10-column SELECT
in 1,457 files, total lineage time ≈ 1,457 × 10 × 15 ms ≈ 218 seconds. The 5-minute
constraint is achievable if we process files sequentially and skip KuzuDB writes
entirely.

---

## Error Category Taxonomy

The diagnostic script must classify every error into exactly one of these categories:

| Code | Name | Description | Example |
|------|------|-------------|---------|
| `E1` | `alias_col_ref` | `sg_lineage()` raises `SqlglotError: Cannot find column 'X' in query` where X contains a table alias prefix | `t1.MA_AANTAL_FTE` |
| `E2` | `expr_no_name` | Column expression in SELECT has no alias and is not `exp.Column` — already skipped by current code | `ROUND(col, 3)` without alias |
| `E3` | `command_fallback` | sqlglot emits `"contains unsupported syntax. Falling back to parsing as 'Command'"` warning | `ALTER DYNAMIC TABLE` |
| `E4` | `parse_failure` | sqlglot raises during `parse_one()` or `transpile()` — full file parse failure | `Expecting (. Line 7, Col: 30` |
| `E5` | `lineage_other` | `sg_lineage()` raises any exception not covered by E1 | `SqlglotError: Cannot build lineage, sql must be SELECT` |
| `E6` | `schema_mismatch` | Column extracted from SELECT is not in schema dict (low-confidence edge emitted) — **always 0 when schema={}** | `col_name not in table_cols` |
| `E7` | `tree_walk_fail` | `_lineage_node_to_edges` raises during tree traversal | Exception inside `_walk()` |
| `E8` | `no_edges_from_root` | `sg_lineage()` returns a root but `_lineage_node_to_edges` returns empty list | Root has no resolvable leaf sources |

Categories E1–E5 are **parsing/extraction failures** (something goes wrong).
Categories E6–E8 are **quality degradations** (something works but produces less data).

---

## Design: `scripts/collect_parse_errors.py`

### Purpose

Run `AnsiParser` / `SnowflakeParser` directly on every `.sql` file in the corpus,
collect all errors and warnings, and produce a frequency table. No KuzuDB writes.
No `index_repo` call — parse only.

### CLI Interface

```
uv run python scripts/collect_parse_errors.py <corpus_dir> --dialect snowflake [--output results.json] [--slow-threshold 1.0]
```

Arguments:
- `corpus_dir` — path to directory tree of `.sql` files (recursive)
- `--dialect` — sqlglot dialect string (default: `snowflake`)
- `--output` — path to write JSON results (default: `parse_errors_results.json`)
- `--slow-threshold` — files taking longer than this many seconds are flagged as slow
  (default: `1.0`)

### What It Does NOT Use

- `index_repo` or `_index_single_file`
- `KuzuBackend`, `KuzuConfig`, or any graph database
- Any MCP server components
- `CrossFileAggregator` or `SchemaResolver` (schema is empty for this run — we want
  to measure errors without schema influence first)

### Processing Pipeline (per file)

```
for each .sql file:
    t0 = time.perf_counter()
    
    1. Attempt sqlglot.parse(sql, dialect=dialect)
       - If parse warns about 'Command' fallback → record E3 per statement
       - If parse raises → record E4, skip remaining steps for this file
    
    2. For each parsed statement:
       a. Classify statement type (SELECT, CREATE VIEW, INSERT, DDL, Command, Other)
       b. Extract column expressions from SELECT list (same logic as _extract_column_lineage)
       c. For each column expression:
          - If has alias → col_name = alias
          - Elif isinstance Column → col_name = col_expr.name
          - Else → record E2, skip
       d. Call sg_lineage(col_name, body, schema={}, dialect=dialect):
          - If raises SqlglotError with 'Cannot find column' AND col_name contains '.' → E1
          - If raises SqlglotError with 'Cannot find column' AND col_name is plain → E5
          - If raises any other exception → E5
          - If returns root:
            i. Call _lineage_node_to_edges(root, ...)
            ii. If returns empty list → record E8
            iii. If raises → record E7
            iv. If returns edges → SUCCESS (record per-file success count)
    
    t1 = time.perf_counter()
    elapsed = t1 - t0
    if elapsed > slow_threshold: mark file as slow
```

### Output Format

The script writes a JSON file with this structure:

```json
{
  "run_date": "2026-05-06T...",
  "corpus_dir": "/home/ignwrad/Projects/dwh",
  "dialect": "snowflake",
  "total_files": 1457,
  "total_statements": 8234,
  "runtime_seconds": 187.4,
  "error_summary": {
    "E1": {"count": 0, "example_files": [], "example_messages": []},
    "E2": {"count": 1203, "example_files": ["etl/sql/fact/orders.sql"], "example_messages": ["ROUND(MA_BRUTO_UREN_CONTRACT, 3)"]},
    "E3": {"count": 47, "example_files": ["ddl/alter_dynamic_table.sql"], "example_messages": ["ALTER DYNAMIC TABLE dt1 RESUME"]},
    "E4": {"count": 12, "example_files": ["ddl/bad_syntax.sql"], "example_messages": ["Expecting (. Line 7, Col: 30"]},
    "E5": {"count": 0, "example_files": [], "example_messages": []},
    "E6": {"count": 0, "example_files": [], "example_messages": []},
    "E7": {"count": 0, "example_files": [], "example_messages": []},
    "E8": {"count": 234, "example_files": ["etl/sql/dim/article.sql"], "example_messages": ["col=ARTIKELOMSCHRIJVING root has no leaf sources"]}
  },
  "success_edges": 4521,
  "slow_files": [
    {"file": "etl/sql/dim/wtdh_artikel.sql", "elapsed_seconds": 4.2, "statement_count": 47}
  ],
  "per_file_timing_p50_ms": 85,
  "per_file_timing_p95_ms": 340,
  "per_file_timing_p99_ms": 1200
}
```

For each error category, store at most 10 example files and 5 example messages.

### Console Output (during run)

```
Scanning /home/ignwrad/Projects/dwh for .sql files...
Found 1,457 files.

Processing:   23%|█████▎                 | 334/1457 [00:28<01:32, 12.1 files/s]

Errors so far: E2=203 E3=8 E4=2 E8=41

Done in 187.4s.

=== Error Frequency Table ===
E1  alias_col_ref      0 occurrences (0.0%)
E2  expr_no_name    1203 occurrences (14.6% of column exprs)  ← skipped (expected)
E3  command_fallback  47 occurrences (in 31 files)
E4  parse_failure     12 occurrences (12 files, full skip)
E5  lineage_other      0 occurrences
E6  schema_mismatch    0 occurrences (schema was empty in this run)
E7  tree_walk_fail     0 occurrences
E8  no_edges_from_root 234 occurrences (28.4% of successful sg_lineage calls)

=== Success ===
sg_lineage edges emitted:  4,521 (from 1,223 of 1,457 files)

=== Slow Files (> 1.0s) ===
4.2s  etl/sql/dim/wtdh_artikel.sql  (47 statements)
2.1s  etl/sql/fact/orders.sql  (31 statements)

Results written to: parse_errors_results.json
```

### Implementation Notes for the Developer

These notes describe the intended implementation — the developer must follow them but
may deviate and document it.

**Import the parser directly, do not use `index_repo`**:

```python
from sqlcg.parsers.snowflake_parser import SnowflakeParser
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.lineage.schema_resolver import SchemaResolver

# SqlParser.__init__ requires a SchemaResolver; pass an empty one (no schema loaded)
_resolver = SchemaResolver(dialect=dialect if dialect != "ansi" else None)
parser = SnowflakeParser(_resolver) if dialect == "snowflake" else AnsiParser(_resolver)
```

**Capture the `Command` fallback warning** by redirecting sqlglot's logger:

```python
import logging
import sqlglot

class CommandFallbackCapture(logging.Handler):
    """Capture sqlglot 'Command' fallback warnings."""
    def __init__(self):
        super().__init__()
        self.warnings: list[str] = []
    def emit(self, record):
        if "Command" in record.getMessage() or "unsupported syntax" in record.getMessage():
            self.warnings.append(record.getMessage())

handler = CommandFallbackCapture()
logging.getLogger("sqlglot").addHandler(handler)
```

**Reuse the same column extraction logic as `_extract_column_lineage` in `base.py`**.
Do not duplicate it — call `parser._extract_column_lineage(stmt, path, out, schema={})`.
Then tally error categories by scanning `out.errors` for the string prefixes that
`_extract_column_lineage` writes (all entries are free-form strings, not structured codes):

- **E5** (lineage_other): entries with prefix `col_lineage:<col>:` where message does
  NOT contain `tree_walk`
- **E7** (tree_walk_fail): entries with prefix `col_lineage:tree_walk:<col>:`
- **E8** (no_edges_from_root): NOT written to `out.errors` by production code — detect
  directly in the script by checking `if root and not new_edges` after calling
  `_lineage_node_to_edges`; do not try to infer E8 from error strings
- **E1 vs E5 disambiguation**: check if the exception message contains
  `'Cannot find column'` AND the col_name contains `.`; E1 only if both are true

This ensures the script measures real production behaviour, not a parallel reimplementation.

**Count E3 separately** from `out.errors` because `Command` fallbacks are sqlglot log
warnings, not exceptions. They appear before the statement is even handed to the parser.

**Timing**: use `time.perf_counter()` per file. Use `statistics.quantiles()` to compute
p50/p95/p99 from the full timing list at the end.

**Progress**: use `tqdm` if available, else fall back to printing every 100 files.
`tqdm` is not a production dependency — check with `importlib.util.find_spec("tqdm")`.

**File discovery**: use `pathlib.Path(corpus_dir).rglob("*.sql")`. Sort the list before
iterating for deterministic output.

---

## sqlglot API Research Findings

These findings are confirmed by source inspection and small reproducers. They must be
documented here so the follow-on sprint can use them without re-doing the research.

### Finding R-01: alias_col_ref (E1) is eliminated by P-03

The current `_extract_column_lineage` code (post P-03) extracts `col_name` from
`col_expr.name` (for `exp.Column` nodes) or `col_expr.alias` (for aliased expressions).
For a raw `exp.Column` with `table='t1'` and `name='col'`, `col_expr.name = 'col'`
(without table prefix). Therefore `col_name = 'col'`, which is what sqlglot's
`lineage()` expects.

**Predicted outcome**: E1 count = 0 after P-03 is merged. If the experiment shows E1 > 0,
the root cause must be a different code path (e.g., an expression type where `.name`
returns a qualified name). This would be a new finding.

### Finding R-02: sources= is for cross-file CTE expansion, not inline aliases

`sources=` calls `exp.expand()` before qualification. It is useful when a CTE is defined
in file A and referenced in file B. It does NOT help with inline subquery aliases
(e.g., `SELECT t1.col FROM (SELECT col FROM src) t1`) — those are handled by
`qualify.qualify()` which runs inside `lineage()` automatically.

**Implication**: passing `sources=` with upstream file content could improve cross-file
lineage quality. This is a research direction for a future sprint, not for this experiment.

### Finding R-03: schema= format is compatible with SchemaResolver.as_dict()

`SchemaResolver.as_dict()` returns `{catalog: {db: {table: [col, col, ...]}}}` with
column lists. `sqlglot.lineage()` passes this to `qualify.qualify()` which wraps it in
`sqlglot.Schema`. sqlglot's Schema accepts lists as column names without types. This is
supported and not a bug.

**However**: the experiment runs with `schema={}` (empty) to isolate parse errors from
schema-influence effects. A second run with a populated schema (if the corpus has DDL
files) would measure E6 separately. This is a stretch goal for the experiment.

### Finding R-04: E8 (no edges from root) is the dominant quality gap

When `sg_lineage()` succeeds but `_lineage_node_to_edges()` returns an empty list, the
usual cause is that the leaf `Node.source` is a `exp.Select` (subquery) rather than
`exp.Table`. The `_lineage_node_to_table_ref()` method returns `None` for non-Table
sources. This is by design — we cannot resolve an inline subquery to a named table
without further expansion.

**E8 is expected to be high** for a DWH corpus that uses temp table patterns
(`CREATE TEMPORARY TABLE t1 AS SELECT ...`) and CTEs extensively. It is not a bug —
it is the documented limitation of single-file lineage without cross-file schema knowledge.

### Finding R-05: per-file timing budget

From the confirmed benchmark: complex 5-CTE query → 5–20 ms per column with `sg_lineage()`.
For the DWH corpus:
- Estimated avg columns per statement: 8
- Estimated avg statements per file: 5
- Total lineage calls: 1,457 × 5 × 8 = 58,280
- At 15 ms/call: 58,280 × 0.015s ≈ 874 seconds — **exceeds 5 minutes**

**Mitigation strategy for the script**: apply a per-file timeout (e.g., 10 seconds).
Files that exceed the timeout are classified as `E_TIMEOUT` (new category) and counted
separately. The 5-minute target assumes most files are simple (1–3 statements, 3–5 cols
per statement). Files like `wtdh_artikel.sql` (47 statements) must be skipped or bounded.

**Revised timing estimate with 10s cap**: 
- ~1,440 normal files × 0.5s avg = 720s (too slow)
- With a 2s cap per file: ~1,440 × 0.3s avg + 17 capped files × 2s = 466s ≈ 8 min
- **Revised constraint**: 10 minutes (not 5), or add `--max-files N` flag for quick sampling

Add an explicit note in the script: `--max-files 200` for a quick validation run.

### Finding R-06: scope= parameter for pre-built scope reuse

`scope=` accepts a pre-built `Scope` from `sqlglot.optimizer.build_scope()`. Passing a
pre-built scope skips the `qualify.qualify()` step inside `lineage()`. This could be
a performance optimisation if we build the scope once per statement and call `lineage()`
multiple times (one per column). However:

- The current code calls `sg_lineage(col_name, body, ...)` per column — `body` is
  re-parsed and re-qualified on each call
- Using `scope=` would avoid redundant qualification; estimated 40–60% speedup per file
- This is a production optimisation, not part of this experiment

**Document as a performance fix candidate** for the follow-on sprint.

---

## Experiment Execution Steps

### Step 1 — Verify P-03 is merged

Before running the experiment, confirm the feat/v0.3.1-postmortem PR is merged to master
and `sqlcg` is re-installed:

```bash
git checkout master && git pull
uv run sqlcg --version
```

Expected: version shows 0.3.1 or higher.

### Step 2 — Create the diagnostic script

The developer writes `scripts/collect_parse_errors.py` following the design in the
"Design" section above. The script does not need tests — it is a one-off diagnostic tool.

File path: `/home/ignwrad/Projects/sql-code-graph/scripts/collect_parse_errors.py`

Time estimate: 1–2 hours.

### Step 3 — Quick validation run (200 files)

```bash
uv run python scripts/collect_parse_errors.py /home/ignwrad/Projects/dwh \
  --dialect snowflake \
  --max-files 200 \
  --output results_200.json
```

Expected runtime: under 3 minutes.

**Pass criteria**: script completes without crashing, JSON is written, at least one
error category has count > 0.

### Step 4 — Full corpus run

```bash
uv run python scripts/collect_parse_errors.py /home/ignwrad/Projects/dwh \
  --dialect snowflake \
  --output results_full.json \
  --slow-threshold 2.0
```

Expected runtime: 8–15 minutes (due to DWH complexity).

**Capture the full console output** for the handoff record.

### Step 5 — Schema-aware run (stretch goal)

If the DWH corpus contains DDL files (CREATE TABLE statements), run a second pass with
schema populated from those DDL files. This requires:

1. Running `sqlcg index /path/to/ddl --dialect snowflake` to populate the graph
2. Exporting the schema from KuzuDB (or extracting it by parsing DDL files directly)
3. Passing `schema=` to the diagnostic script

This step is only worth doing if E6 (schema_mismatch) appears interesting in the full
corpus run. If E8 (no_edges_from_root) dominates, the schema pass adds limited value.

### Step 6 — Analyse and document findings

Read `results_full.json` and answer these questions (document answers in
`ARCHITECTURE_REVIEW.md` section 12):

1. Is E1 (alias_col_ref) actually zero after P-03? If not, reproduce with a minimal SQL
   example and identify the code path.

2. What fraction of SELECT columns are E2 (expr_no_name — expressions without aliases)?
   This tells us how much lineage coverage we lose by skipping unaliased expressions.
   If > 30%, we should plan a "best-effort expression name extraction" feature.

3. How many files produce E4 (full parse failure)? Which sqlglot constructs are
   responsible? Are they in DML files (blocking ETL lineage) or DDL-only?

4. What fraction of `sg_lineage()` calls produce E8 (no edges from root)?
   If > 40%, the limiting factor is cross-file temp table chains, not parsing bugs.
   A `sources=`-based cross-file expansion feature becomes the top priority.

5. What are the slowest files? Is the slowness due to statement count, column count,
   or CTE nesting depth? This determines whether `scope=` reuse (R-06) is worth
   implementing.

---

## Success Criteria

The experiment is complete when all of the following are answered:

- `[ ]` E1 count confirmed (expected: 0 after P-03)
- `[ ]` E2 count and fraction of total column expressions documented
- `[ ]` E3 count and list of unsupported DDL constructs documented
- `[ ]` E4 count and list of failing files documented (with sqlglot error messages)
- `[ ]` E8 count and fraction of `sg_lineage()` calls documented
- `[ ]` Top 5 slow files identified with per-file timing
- `[ ]` Whether schema-aware run (Step 5) is worth doing — yes/no with rationale
- `[ ]` Findings written to `ARCHITECTURE_REVIEW.md` section 12

The experiment does NOT produce fix tickets. Fix tickets are written by the
sprint-planner in a separate session after the findings are documented.

---

## Deferred Decisions

These are questions this experiment will answer — they are NOT answered here:

1. **Should we fix E8 (no edges from subquery sources)?** Requires knowing E8 frequency
   first. If E8 is < 10% of lineage calls, it may not be worth the complexity of
   `sources=` expansion. If E8 is > 50%, cross-file expansion is the top-priority fix.

2. **Is `scope=` reuse worth implementing?** Requires the slow files list from Step 5.
   If p99 < 2s, the current per-column `qualify()` calls are acceptable. If p99 > 5s
   on common file patterns, `scope=` reuse becomes a performance ticket.

3. **Should schema pass `{table: {col: type}}` format instead of `{catalog: {db: {table: [cols]}}}`?**
   Requires empirical testing with the schema-aware run (Step 5). If E6 is non-trivial,
   we need to understand whether sqlglot is failing to match columns due to the nested
   format or because schema is just absent.

4. **Is E4 (parse failure) concentrated in DDL or DML files?** If DML files are failing,
   that directly impacts ETL lineage coverage and is high priority. If only DDL files
   fail, impact is limited (we don't need DDL for table-level lineage per section 6 of
   `ARCHITECTURE_REVIEW.md`).

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Full corpus run exceeds 15 minutes | MEDIUM | Add `--max-files 500` option for partial sampling; run full corpus overnight |
| `wtdh_artikel.sql` (47 stmts) causes script to hang | MEDIUM | Per-file `signal.alarm` timeout (Linux) or `multiprocessing` with timeout; record as `E_TIMEOUT` |
| P-03 not yet merged when experiment runs | LOW | Step 1 verifies merge status; experiment is invalid on old code |
| Script imports from `sqlcg` but `sqlcg` not in PYTHONPATH | LOW | Run with `uv run python scripts/...` which resolves the project's virtualenv |
| `tqdm` not installed | LOW | Script falls back to plain-text progress every 100 files |
| Schema-aware run (Step 5) requires KuzuDB write | MEDIUM | Step 5 is a stretch goal; skip if Step 4 results are sufficient |

---

## Artifacts

| Artifact | Owner | Notes |
|----------|-------|-------|
| `scripts/collect_parse_errors.py` | developer | Written per this plan; not production code |
| `results_200.json` | developer | Quick validation run output |
| `results_full.json` | developer | Full corpus run output |
| `ARCHITECTURE_REVIEW.md` § 12 | architect-reviewer | Written after findings are analysed |
| Follow-on sprint plan | sprint-planner | Written after § 12 is complete |

---

## Handoff Sequence

1. **architect-planner** → this plan → `plan/parsing_errors_experiment.md`
2. **developer** → writes `scripts/collect_parse_errors.py` per "Design" section →
   runs Steps 3–5 → says "finished" with results
3. **architect-planner** → plan-compliance check (did the script match the design?)
4. **architect-reviewer** → reads results → writes `ARCHITECTURE_REVIEW.md` § 12
5. **sprint-planner** → converts § 12 into fix tickets → new sprint plan
