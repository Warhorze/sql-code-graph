# Feature Plan: Parse Diagnostics — Profiler CLI + Parse-Failure Cause Surfacing

Author: architect-planner
Date: 2026-05-29
Source motivation: [`e2e_firstuser_report.md`](../e2e_firstuser_report.md) — devs and LLMs
know which unparsed files matter; the graph does not. Surfacing *which files failed and
why* lets users discount missing lineage intelligently.

Constraints doc: [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md),
[`CLAUDE.md`](../CLAUDE.md).

---

## Summary

Two related diagnostic capabilities, one PR:

1. **Productize the profiler** as a supported `sqlcg index --profile` flag that emits
   per-stage timing (parse vs upsert vs star-expansion) and per-file wall-time stats,
   replacing the throwaway `scratch_profile_*.py` scripts.
2. **Persist and expose the parse-failure cause** per file: write the already-computed
   E-code/skip bucket onto the `File` graph node, then read it back via a new
   `sqlcg analyze failures` subcommand.

---

## Scope Decisions (resolved — not left open)

These four decisions were called out in the request as must-resolve. Each is settled here.

### (a) `sqlcg profile` command vs `index --profile` flag → **`index --profile` flag**

Chosen: an **`--profile` flag on the existing `index` command**, not a new `profile`
subcommand.

Rationale:
- The scratch scripts all profile *an index run* (`scratch_profile_index.py` calls
  `indexer.index_repo`; `scratch_profile_100.py` profiles the parse loop). There is no
  meaningful "profile" operation independent of indexing — a standalone command would
  have to re-run indexing anyway, doubling work.
- A flag keeps the **zero-config small-repo path intact**: a 20-ETL user runs
  `sqlcg index ./sql` and sees nothing new. Profiling is strictly opt-in.
- A separate `profile` subcommand would duplicate every `index` option (dialect,
  timeout, batch-size, no-ddl) to mirror real conditions — a maintenance trap.

The flag turns on per-stage timing collection inside `index_repo` (already structured
into discrete phases: pass1 parse, pass2 resolve, upsert batches, star expansion) and
prints a timing table after the normal summary.

### (b) Delete the `scratch_profile_*.py` scripts once the feature lands → **Yes, delete all five**

Once `index --profile` ships, the five scratch scripts at repo root
(`scratch_profile_10.py`, `scratch_profile_100.py`, `scratch_profile_combi.py`,
`scratch_profile_index.py`, `scratch_profile_slow.py`) are superseded. They are
explicitly marked "Do NOT commit" in their own docstrings, are dev-only, and indexing
profiling becomes a first-class feature. Deleting them is in-scope for this PR (Phase 4).
The unrelated `scratch_bootstrap_golden.py` (currently untracked, golden-harness work) is
**out of scope** — do not touch it.

### (c) `File` node property name + E-code mapping → **`parse_cause STRING`, reusing `_classify_error` buckets**

Decisions:
- **Property name: `parse_cause`** on the `File` node (a `STRING`, empty `""` when the
  file parsed cleanly). Paired with a new **`parse_failed BOOLEAN`** on the `File` node
  (see correction note below — `File` does NOT currently have this column; only `SqlQuery`
  does).
- **Vocabulary: reuse the existing E-code buckets** from
  [`error_classify._classify_error`](../src/sqlcg/indexer/error_classify.py) — do NOT
  invent a parallel vocabulary. The buckets are already the canonical mapping to the
  E{n} taxonomy: `E1`, `E2`, `E3`, `E5`, `E8`, `timeout`, `pure_ddl_skip`,
  `func_fallback`, `qualify_failed`, `other`.
- **Per-file cause = the dominant bucket for that file.** A file can emit many
  `parsed.errors`; the file-level `parse_cause` is the **most frequent non-`other`
  bucket** across that file's errors (ties broken by the taxonomy priority order
  E36/timeout-class first, then by first-seen). When all errors classify as `other` or
  the file has no errors, `parse_cause = ""`.

  This is a new helper, `dominant_cause(errors: list[str]) -> str`, added to
  `error_classify.py` next to `_classify_error`. The existing per-corpus
  `error_summary` aggregation in `indexer.py` (lines 283-308) stays unchanged — the new
  helper is a per-file rollup that reuses `_classify_error`, not a replacement.

> ### Grounding correction (load-bearing — do not skip)
> The request stated `File.parse_failed` already exists at `schema.cypher:43`. **This is
> incorrect.** Line 43 of [`schema.cypher`](../src/sqlcg/core/schema.cypher) is
> `parse_failed BOOLEAN` on the **`SqlQuery`** node (lines 36-46), NOT on `File`. The
> `File` node (lines 8-13) currently has only `path`, `repo_path`, `sha`, `dialect` — it
> has **no `parse_failed` and no `parse_cause`**. So this feature must add **both**
> columns to the `File` node. The "half-exposed" state in the grounding refers to
> `SqlQuery.parse_failed` (per-statement) plus the in-memory `error_summary` (per-corpus
> aggregate) — neither is queryable per-file. This plan closes that gap at the File level.

### (d) MCP surface in-scope-now or deferred → **Deferred (documented), with a forward note**

Decision: **the MCP tool/field is deferred to a follow-up PR.** This PR delivers the
graph persistence + CLI read surface only.

Rationale:
- This is meant to be "the cleanest first PR of a larger analysis-engine direction."
  Persisting `parse_cause` and adding `analyze failures` is a self-contained, fully
  testable slice.
- The 14 existing MCP tools (the report says 9 register after the `functools.wraps` fix;
  the grounding counts 14 defined) are lineage-traversal tools. Adding a
  `get_parse_failures` MCP tool is a clean follow-up *once the property exists* — the
  property is the prerequisite, the tool is additive.
- Deferring keeps the PR small and avoids touching `server/tools.py` /
  `server/models.py` in the same change that touches the indexer hot path.

Forward note recorded in Rollout: once `parse_cause` is on the graph, a follow-up adds a
`get_parse_failures(path_prefix: str | None) -> list[ParseFailure]` MCP tool reading the
same query as `analyze failures`, so LLMs can factor unparsed-file gaps into lineage
answers. **Not implemented in this PR.**

---

## In Scope

- New `File` node columns: `parse_failed BOOLEAN`, `parse_cause STRING`.
- `dominant_cause()` helper in `error_classify.py` (reuses `_classify_error`).
- File-row enrichment in `_upsert_parsed_file` (Phase A `file_rows` dict) — folded into
  the existing `upsert_nodes_bulk(NodeLabel.FILE, ...)` call. **No new upsert calls.**
- `sqlcg index --profile` flag: per-stage timing + per-file wall-time stats table.
- `sqlcg analyze failures` subcommand: lists failed files + cause, reading the graph.
- Deletion of the five `scratch_profile_*.py` scripts.
- Tests asserting observable output (cause strings persisted, failure rows returned,
  profiler timing keys present).

## Non-Goals

- **No MCP tool** for parse failures (deferred — see decision (d)).
- **No change to the `report` command** or `MetricsStore`. Per the grounding, `report`
  reads a *separate* `~/.sqlcg/metrics.db` ([`report.py`](../src/sqlcg/cli/commands/report.py)),
  not the graph. Parse-failure cause lives in the graph; mixing it into the metrics-db
  report would couple two unrelated stores. Out of scope.
- **No new E-codes / no taxonomy expansion.** Reuse existing buckets only.
- **No fix to any parser** to reduce failures. This feature *surfaces* causes; it does
  not change parse behaviour.
- **No profiling of the MCP server or query path** — only the index pipeline.
- **No schema-CSV / INFORMATION_SCHEMA reintroduction** (forbidden by CLAUDE.md).

---

## Design

### Data Model — graph schema change

[`schema.cypher`](../src/sqlcg/core/schema.cypher), `File` node (lines 8-13):

```sql
CREATE NODE TABLE File (
    path STRING PRIMARY KEY,
    repo_path STRING,
    sha STRING,
    dialect STRING,
    parse_failed BOOLEAN,   -- NEW: true when the file parsed with a degrading failure
    parse_cause STRING      -- NEW: dominant E-code/skip bucket, "" when clean
);
```

`parse_failed` semantics: true when the file's dominant cause is a **degrading** bucket
(`E1`, `E2`, `E3`, `E5`, `E8`, `timeout`, `func_fallback`, `qualify_failed`). The
**non-degrading** skip `pure_ddl_skip` does NOT set `parse_failed` (a pure-DDL file is a
deliberate skip, not a failure) but is still recorded in `parse_cause` so users can see
why a DDL file has no lineage. `other` → `parse_failed=false`, `parse_cause=""`.

`SCHEMA_VERSION` in [`schema.py`](../src/sqlcg/core/schema.py) bumps `"2"` → `"3"`
(adding columns is a schema change; re-index is the migration path — see Rollout).

### `dominant_cause` helper

In [`error_classify.py`](../src/sqlcg/indexer/error_classify.py):

```python
# Priority order when one file emits multiple distinct buckets (highest blast radius first).
_CAUSE_PRIORITY = ["timeout", "E8", "E3", "E2", "E5", "E1",
                   "qualify_failed", "func_fallback", "pure_ddl_skip"]
_DEGRADING = {"E1", "E2", "E3", "E5", "E8", "timeout", "func_fallback", "qualify_failed"}

def dominant_cause(errors: list[str]) -> tuple[str, bool]:
    """Return (parse_cause, parse_failed) for one file's error list.

    Reuses _classify_error per message, counts buckets, returns the most frequent
    non-"other" bucket (ties broken by _CAUSE_PRIORITY). Empty/all-other -> ("", False).
    """
```

Returns a tuple consumed directly into the `file_rows` dict. `parse_failed` is
`cause in _DEGRADING`.

### Indexer wiring (hot-path-safe)

[`indexer.py`](../src/sqlcg/indexer/indexer.py), `_upsert_parsed_file`, line 453-454:

```python
# File node — BEFORE:
file_rows.append({"path": parsed.path_str, "dialect": parsed.dialect or ""})

# AFTER:
cause, failed = dominant_cause(parsed.errors)
file_rows.append({
    "path": parsed.path_str,
    "dialect": parsed.dialect or "",
    "parse_failed": failed,
    "parse_cause": cause,
})
```

This is the ONLY indexer write change. It flows through the existing
`db.upsert_nodes_bulk(NodeLabel.FILE, file_rows)` at line 632 — **no new `upsert_node`
call, no per-row write.** Bulk-upsert invariant preserved.

> Homogeneity constraint (verified): `KuzuBackend.upsert_nodes_bulk`
> ([`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) lines 225-230) requires every
> row of a label to have an **identical key set**, else it raises `ValueError`. There is
> exactly one File row per `_upsert_parsed_file` call, so adding the two keys to that
> single dict is safe — but the new keys must be present on **every** File row produced
> anywhere (currently only this one site), and `parse_cause` must always be a string
> (`""`, never `None`) and `parse_failed` always a bool so the column types stay
> consistent across files. `dominant_cause` runs once per
file over an already-materialized `parsed.errors` list — O(errors), not coupled to column
count, so it cannot trip the perf-scaling guard. **`parsers/base.py` is NOT touched**, so
the once-per-statement scope/qualify invariants are untouched by definition.

### Profiler — `index --profile`

[`index.py`](../src/sqlcg/cli/commands/index.py): add a `--profile / --no-profile`
Typer option (default `False`). When set, pass `profile=True` into `index_repo`.

`index_repo` ([`indexer.py`](../src/sqlcg/indexer/indexer.py)) gains a `profile: bool =
False` param. When true, it records `time.perf_counter()` deltas around its existing
discrete phases and adds a `"profile"` dict to the returned summary:

```python
summary["profile"] = {
    "pass1_parse_s": ...,        # pass-1 parse loop
    "pass2_resolve_s": ...,      # cross-file pass-2 resolution
    "upsert_s": ...,             # batch upsert loop (sum of _flush_batch)
    "star_expand_s": ...,        # _expand_star_sources
    "total_s": ...,
    "files": n,
    "ms_per_file": total_s / n * 1000,
    "slowest_files": [(path, ms), ...],  # top 10 by per-file parse time
}
```

When `profile=False`, no timing dict is added and there is **zero overhead** (no
`perf_counter` calls in the hot loop — gate them behind the flag). `index.py` prints the
profile table only when present. This replicates what the scratch scripts measured
(per-file ms, phase isolation, extrapolation) as supported output.

> Implementer note: the phase boundaries already exist in `index_repo` (pass1 results,
> pass2_results, the `_flush_batch` loop, `_expand_star_sources` at line 281). Wrap those
> existing boundaries — do not restructure the pipeline.

### Read surface — `analyze failures`

[`analyze.py`](../src/sqlcg/cli/commands/analyze.py): add a `failures` subcommand
modeled on the existing `unused` command (lines 71-82).

```python
@app.command("failures")
def failures(
    cause: str | None = typer.Option(None, "--cause", help="Filter by E-code bucket"),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    """List files that failed to parse, with their cause (E-code bucket)."""
    with get_backend() as backend:
        cypher = (
            f"MATCH (f:{NodeLabel.FILE}) WHERE f.parse_failed = true "
            "AND ($cause IS NULL OR f.parse_cause = $cause) "
            "RETURN f.path AS path, f.parse_cause AS cause "
            f"ORDER BY f.parse_cause LIMIT {limit}"
        )
        rows = backend.run_read(cypher, {"cause": cause})
        _print_table(rows, ["path", "cause"])
```

Reuses the existing `_print_table` helper (line 85) and `NodeLabel` import. The empty-set
message ("No results") already distinguishes "no failures" cleanly via `_print_table`.

---

## Implementation Steps

### Phase 1: Graph schema + cause persistence (the prerequisite slice)

**Step 1.1** — Add `dominant_cause()` to `error_classify.py`.
- Files: [`src/sqlcg/indexer/error_classify.py`](../src/sqlcg/indexer/error_classify.py)
- Acceptance: unit test feeds a list mixing `E5` (×3) and `E8` (×1) → returns
  `("E5", True)`; `["col_lineage_skip:pure_ddl_file"]` → `("pure_ddl_skip", False)`;
  `[]` → `("", False)`; all-`other` → `("", False)`.

**Step 1.2** — Add `parse_failed BOOLEAN`, `parse_cause STRING` to the `File` node in
`schema.cypher`; bump `SCHEMA_VERSION` to `"3"`.
- Files: [`schema.cypher`](../src/sqlcg/core/schema.cypher),
  [`schema.py`](../src/sqlcg/core/schema.py)
- Acceptance: a fresh in-memory KuzuDB init + `MATCH (f:File) RETURN f.parse_cause`
  succeeds (column exists). `SCHEMA_VERSION == "3"`.

**Step 1.3** — Wire `dominant_cause` into `_upsert_parsed_file` file-row dict.
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py) (lines 11 import, 453-454)
- Call site: grep-confirmed — `dominant_cause` is imported alongside the existing
  `from sqlcg.indexer.error_classify import _classify_error` (line 11) and called once at
  line ~454.
- Acceptance: integration test indexes a fixture file that produces an `E5` error and
  asserts `MATCH (f:File {path:$p}) RETURN f.parse_failed, f.parse_cause` returns
  `(true, "E5")`. A clean file returns `(false, "")`.

### Phase 2: Read surface — `analyze failures`

**Step 2.1** — Add the `failures` subcommand to `analyze.py`.
- Files: [`analyze.py`](../src/sqlcg/cli/commands/analyze.py)
- Call site: registered automatically by Typer `@app.command("failures")` (same
  mechanism as `unused`). Grep-confirm `analyze` app is mounted in
  [`main.py`](../src/sqlcg/cli/main.py).
- Acceptance: e2e/CLI test indexes a corpus with ≥1 failing file, runs
  `sqlcg analyze failures`, asserts the failing file's path AND its cause string appear
  in stdout. `--cause E5` filters to only E5 rows. Clean corpus → "No results".

### Phase 3: Profiler — `index --profile`

**Step 3.1** — Add `profile: bool = False` to `index_repo`; record phase timings and
`slowest_files` when true; attach `summary["profile"]`.
- Files: [`indexer.py`](../src/sqlcg/indexer/indexer.py)
- Acceptance: unit/integration test calls `index_repo(..., profile=True)` and asserts
  `summary["profile"]` contains keys `pass1_parse_s`, `pass2_resolve_s`, `upsert_s`,
  `star_expand_s`, `total_s`, `ms_per_file`, `slowest_files`, and that `total_s > 0`.
  With `profile=False`, asserts `"profile" not in summary`.

**Step 3.2** — Add `--profile` flag to the `index` CLI command; print the timing table
when present.
- Files: [`index.py`](../src/sqlcg/cli/commands/index.py)
- Acceptance: CLI test runs `sqlcg index <fixture> --profile`, asserts stdout contains a
  per-stage timing line and an `ms/file` figure. Without `--profile`, asserts no timing
  table is printed (the normal summary is unchanged).

### Phase 4: Remove scratch scripts

**Step 4.1** — Delete the five `scratch_profile_*.py` files.
- Files: `scratch_profile_10.py`, `scratch_profile_100.py`, `scratch_profile_combi.py`,
  `scratch_profile_index.py`, `scratch_profile_slow.py` (repo root)
- Do NOT touch `scratch_bootstrap_golden.py`.
- Acceptance: `git status` shows the five files deleted; `grep -rl scratch_profile` finds
  no remaining references in `src/` or `tests/`.

---

## Test Strategy

- **Unit** (`tests/unit/`):
  - `test_dominant_cause.py` — bucket counting, priority tie-break, degrading vs
    non-degrading flag, empty/all-other → `("", False)`.
  - Profile dict shape from `index_repo(profile=True)` and absence when `False`.
- **Integration** (`tests/integration/`, real in-memory KuzuDB):
  - Index a fixture with a known `E5`-producing statement → assert
    `File.parse_failed`/`File.parse_cause` persisted correctly via Cypher.
  - Assert the cause flows through the **bulk** upsert (no per-row write) by indexing and
    checking the File node — and confirm `test_bulk_upsert_invariant.py` and
    `test_perf_scaling_guard.py` still pass unchanged.
- **e2e / CLI** (`tests/e2e/`):
  - `sqlcg analyze failures` prints failing path + cause; `--cause` filter narrows.
  - `sqlcg index --profile` prints timing table; absence without the flag.
- All assertions check **observable output** (cause strings, timing keys, CLI stdout
  rows), never "no exception raised."

---

## Acceptance Criteria

- [ ] `File` node has `parse_failed BOOLEAN` and `parse_cause STRING`; `SCHEMA_VERSION == "3"`.
- [ ] `dominant_cause()` exists, reuses `_classify_error`, returns `(cause, failed)` with
      documented tie-break; covered by unit tests on real inputs.
- [ ] `_upsert_parsed_file` sets both new columns via the existing
      `upsert_nodes_bulk(NodeLabel.FILE, ...)` path — no new `upsert_node`/`upsert_edge`
      call introduced (grep-confirmed).
- [ ] An indexed file with an `E5` error reports `parse_failed=true, parse_cause="E5"`
      in the graph; a clean file reports `false, ""`.
- [ ] `sqlcg analyze failures` lists failing files with their cause; `--cause <bucket>`
      filters; empty result prints "No results".
- [ ] `sqlcg index --profile` prints per-stage timing + `ms/file`; without the flag the
      summary is byte-identical to today and zero `perf_counter` calls run in the hot loop.
- [ ] The five `scratch_profile_*.py` scripts are deleted; `scratch_bootstrap_golden.py`
      untouched.
- [ ] `test_bulk_upsert_invariant.py` and `test_perf_scaling_guard.py` pass unchanged.
- [ ] No `# TODO` in any happy path of the new code.

---

## Rollout / Rollback

**Rollout — re-index is the migration path.** Adding `parse_failed`/`parse_cause` to the
`File` node means **existing graphs (schema v2) will not have these columns populated**.
Per CLAUDE.md "no backward compatibility — re-index is the migration path." The
`SCHEMA_VERSION` bump to `"3"` makes the version mismatch detectable. Users must run
`sqlcg db reset` + `sqlcg index` (or re-init) to populate cause data. Document this in the
PR description and the `analyze failures` help text ("requires a graph indexed with
sqlcg ≥ this version").

**Forward note (next PR, out of scope here):** add a `get_parse_failures` MCP tool reading
the same `File.parse_failed`/`parse_cause` query so LLMs can factor unparsed-file gaps into
lineage answers. The property added in this PR is the prerequisite.

**Rollback.** Pure-additive: the two File columns are nullable/defaulted and unread by any
existing code path, the `--profile` flag and `analyze failures` subcommand are opt-in. To
roll back, revert the PR and re-index (or leave the columns — they are harmless to v2
readers). No data migration needed either direction.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Touching `indexer.py` regresses the bulk-upsert or perf-scaling invariant | Change is one dict literal flowing into the existing bulk call; `parsers/base.py` untouched; CI runs `test_bulk_upsert_invariant.py` + `test_perf_scaling_guard.py` (must pass unchanged — listed as acceptance). |
| `dominant_cause` adds per-file overhead at scale | O(len(errors)) over an already-materialized list, once per file, not per column/edge — cannot flip O(N)→O(N²). Reuses `_classify_error` which already runs in the per-corpus summary loop. |
| Profiler `perf_counter` calls slow the hot path even when off | Gate all timing behind `if profile:`; assert in test that `profile=False` adds no `"profile"` key and changes nothing. |
| Small-repo experience degraded | Both capabilities are strictly opt-in (`--profile` flag, `analyze failures` subcommand). Default `sqlcg index ./sql` output is unchanged. |
| Grounding said `File.parse_failed` exists — risk of assuming a no-op | Corrected in decision (c): `File` had neither column; both are added. Acceptance 1 verifies the column exists post-init. |

---

## Open Decisions Couldn't Resolve

None blocking. All four called-out scope decisions are resolved above. The single
correction (File node did not have `parse_failed`; both columns are new) is documented and
folded into Phase 1. The MCP tool is a deliberate, documented deferral, not an unresolved
question.

### Deviations

#### Deviation 1: Integration test uses func_fallback instead of E5 as the degrading-bucket fixture
- **Reason**: The plan specified `parse_cause="E5"` for the integration test fixture
  (`WITH x AS (SELECT a FROM table_not_in_schema) SELECT a FROM x`). In practice, the
  current ANSI parser resolves this CTE successfully (confidence 0.7) without emitting
  any `col_lineage:...:Cannot find column` error marker — sqlglot traces lineage back to
  `table_not_in_schema` instead of raising. The fixture produces zero errors.
- **Change**: `test_failing_file_has_correct_cause` uses `SELECT SUM(amount) FROM orders;`
  which reliably triggers `col_lineage_skip:func_fallback:Sum` via the unaliased-aggregate
  guard (T-09-03). The acceptance criterion (a degrading file has `parse_failed=true` and a
  populated `parse_cause`) is fully satisfied. `dominant_cause()` unit tests cover E5 directly
  via `_classify_error` input strings.
- **Impact**: No scope change. `func_fallback` is a first-class degrading bucket and a real
  production trigger path. The e2e `analyze failures` tests also use `func_fallback`.
- **Date**: 2026-05-29
