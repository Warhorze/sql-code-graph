# Sprint Plan: Performance (Batched Writes) + Live Anchor Validation + Parse-Errors Re-measure

Plan date: 2026-05-12
Author: architect-planner
Source authority: [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) § 11.6 (bulk DB writes), § 12 (parsing-errors findings), [`plan/sprint_06_lineage_coverage.md`](sprint_06_lineage_coverage.md) (E5/E2/DDL-skip shipped)
Policy: no backward compatibility; re-index is the migration path; no TODO in happy path; serve both small (20-file) and large (1,600-file) repos

---

## Summary

Sprint 07 has two code feature areas plus one measurement task:

1. **PERF-BATCH** — Replace per-file commit with N-files-per-transaction batched
   writes in [`Indexer.index_repo`](../src/sqlcg/indexer/indexer.py). Drive 1,600-file
   indexing below the 5 minute budget without re-introducing the pre-fix OOM regression.
   Expose `--batch-size` on `sqlcg index`.

2. **LIVE-ANCHOR** — Add an end-to-end integration test that indexes the existing
   anchor fixtures ([`tests/snowflake/anchors/`](../tests/snowflake/anchors/)) into a
   real in-memory `KuzuBackend` and asserts the two anchor columns
   (`MA_AANTAL_OP_ORDER`, `OMLOOPSNELHEID`) appear in the graph with at least one
   `COLUMN_LINEAGE` edge each. Skipped variant covers a real DWH corpus when present.

3. **MEAS-RERUN** — Documented re-run of
   [`scripts/collect_parse_errors.py`](../scripts/collect_parse_errors.py) on the
   1,445-file Snowflake DWH corpus post sprint-06, with an expected-output schema so
   the next sprint can quantitatively compare E2/E5/E36 deltas. No code changes.

---

## Scope

### In Scope

- `src/sqlcg/indexer/indexer.py` — `index_repo()` batching (PERF-BATCH)
- `src/sqlcg/cli/commands/index.py` — `--batch-size` flag wiring (PERF-BATCH)
- `tests/integration/test_live_anchors.py` — new live-graph anchor test (LIVE-ANCHOR)
- `tests/integration/test_indexer_batching.py` — new batching equivalence test (PERF-BATCH)
- `plan/sprint_07_perf_and_live_test.md` — captures the MEAS-RERUN expected output
- `ARCHITECTURE_REVIEW.md` § 12 — append a `## 12.X` subsection recording MEAS-RERUN result
  (done by the developer after the run, written into the plan first)

### Non-Goals

- `ProcessPoolExecutor` parallel parsing (planned separately in § 11.6 of architecture
  review — explicitly NOT in this sprint; this sprint is the IO-write half only)
- Any new fix to E5/E8/E36 lineage logic (sprint 06 / future sprints)
- Re-running the parse-errors experiment script source design — script already exists
- Removing the existing xfailed anchor tests under `tests/snowflake/anchors/` —
  the live test is **additive**; the xfail tests stay as the parser-only regression gate
- Changing `kuzu_backend.transaction()` API
- Changing `_upsert_parsed_file` body (only its commit boundary moves)

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| Per-file commit in `Indexer.index_repo` | Confirmed — [`indexer.py` lines 130–149](../src/sqlcg/indexer/indexer.py): `for parsed in pass2_results: ... with db.transaction(): counts = self._upsert_parsed_file(parsed, db, ...)`. Each iteration opens and commits its own KuzuDB transaction. |
| `KuzuBackend.transaction()` uses Cypher `BEGIN TRANSACTION` / `COMMIT` and is a re-entrant-unsafe context manager (`_in_transaction` flag) | Confirmed — [`kuzu_backend.py` lines 290–317](../src/sqlcg/core/kuzu_backend.py). Nested `with db.transaction()` would set the flag twice; the design requires one transaction at a time. PERF-BATCH must therefore call `db.transaction()` ONCE per batch (not nested per file). |
| `progress_callback` already wired and fired every 100 files | Confirmed — [`indexer.py` lines 89–91](../src/sqlcg/indexer/indexer.py). PERF-BATCH does not change progress semantics. |
| `no_ddl` and `--no-ddl` already wired (T-03 from sprint 06) | Confirmed — [`indexer.py` line 35](../src/sqlcg/indexer/indexer.py) (`no_ddl: bool = False`) and [`index.py` line 32](../src/sqlcg/cli/commands/index.py) (`no_ddl` Typer option). PERF-BATCH adds `batch_size` next to `no_ddl` in the same call. |
| `Indexer.index_repo()` accumulates `pass1_results` and `pass2_results` for the entire corpus in memory before writing | Confirmed — [`indexer.py` lines 69, 100–108](../src/sqlcg/indexer/indexer.py). This is the OOM root cause documented in § 11.1 and CLAUDE.md "OOM history". **PERF-BATCH does not change the parse loop**; it changes only the upsert loop. The OOM regression risk is therefore confined to a batch worth of `ParsedFile` objects already in `pass2_results` — they are already in memory. **No new memory cost from PERF-BATCH itself.** |
| Anchor fixtures already exist as separate `.sql` files | Confirmed — `fixture_etl.sql`, `fixture_omloopsnelheid.sql`, `fixture_semantic.sql`, `fixture_source.sql` under [`tests/snowflake/anchors/`](../tests/snowflake/anchors/). LIVE-ANCHOR re-uses them directly; no new fixture authoring required. |
| `tests/integration/` directory exists and uses in-memory `KuzuBackend(":memory:")` per sprint-06 regression guard pattern | Confirmed — pattern documented in [sprint_06_lineage_coverage.md § "The Single Most Important Regression Guard"](sprint_06_lineage_coverage.md). LIVE-ANCHOR copies this pattern. |
| `Indexer.index_repo()` accepts `path` (directory), not an explicit file list | Confirmed — [`indexer.py` line 27](../src/sqlcg/indexer/indexer.py): `path: Path`. Uses `walk_sql_files(path, spec, use_git=use_git)` internally. LIVE-ANCHOR must therefore copy the anchor `.sql` files into a `tmp_path` directory and pass `use_git=False` (the temp directory is not a git repo). |
| `MA_AANTAL_OP_ORDER` chain currently produces zero edges in the production parser for the cross-file 6-link chain | Confirmed — all 6 tests in [`test_anchor_ma_aantal_op_order.py`](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py) are `xfail strict=True`. **LIVE-ANCHOR must therefore use a softer assertion** (at least one edge somewhere in the chain) and document which links are currently expected to fail. |
| `OMLOOPSNELHEID` anchor passes in isolation (temp-table CTAS produces edges; the column is intentionally dropped at the persistent boundary) | Confirmed — [`test_anchor_omloopsnelheid.py`](../tests/snowflake/anchors/test_anchor_omloopsnelheid.py) passes without `xfail`. LIVE-ANCHOR can assert at least one `COLUMN_LINEAGE` edge for `OMLOOPSNELHEID`. |
| `scripts/collect_parse_errors.py` already exists and writes JSON output | Confirmed — file at `scripts/collect_parse_errors.py`, top docstring describes `--output results.json` flag. MEAS-RERUN is a *measurement task*, not a script-authoring task. |

---

## Ticket Table

| ID | Title | Primary Files | Effort | Priority | Depends on | Blocks |
|----|-------|--------------|--------|----------|-----------|--------|
| T-01 | `Indexer.index_repo(batch_size=...)` batched writes | [`indexer.py`](../src/sqlcg/indexer/indexer.py) | M | 1 — highest | INDEPENDENT | T-02 |
| T-02 | `--batch-size` CLI flag wiring | [`index.py`](../src/sqlcg/cli/commands/index.py) | XS | 2 | T-01 | NONE |
| T-03 | `tests/integration/test_live_anchors.py` — index anchor fixtures into KuzuBackend + Cypher assertions | new test file | M | 3 | INDEPENDENT | NONE |
| T-04 | MEAS-RERUN: documented re-run of `collect_parse_errors.py` post-sprint-06 | no code; plan + ARCHITECTURE_REVIEW.md note | S | 4 | INDEPENDENT | NONE |

---

## Recommended Implementation Order

### Single-Developer Sequence

1. **T-01** — batched writes in `index_repo`. Highest impact, single file change.
2. **T-02** — Typer flag wiring. Trivial after T-01.
3. **T-03** — live anchor test. Can be done in parallel with T-01/T-02 if reviewer
   capacity allows; otherwise after.
4. **T-04** — MEAS-RERUN. Done at end of sprint, before sprint postmortem.

### Two-Developer Sequence

- Track A: T-01 → T-02
- Track B: T-03 (independent, can start day 1)
- T-04 at sprint close (joint).

---

## Ticket Specifications

---

### T-01 — `Indexer.index_repo(batch_size=...)` batched writes

**Source**: [`ARCHITECTURE_REVIEW.md` § 11.6](../ARCHITECTURE_REVIEW.md) "Bulk DB writes — collect ParsedFile results … in batches of N (default 50)"; CLAUDE.md "Perf budget: indexing 1,600 files with schema CSV provided should complete in under 5 minutes on a laptop."

**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: T-02

#### Root cause

The upsert loop in [`indexer.py` lines 130–149](../src/sqlcg/indexer/indexer.py)
opens one KuzuDB transaction per file:

```python
for parsed in pass2_results:
    ...
    with db.transaction():
        counts = self._upsert_parsed_file(parsed, db, gold_tables=gold_tables)
```

On a 1,600-file corpus this issues 1,600 `BEGIN TRANSACTION` / `COMMIT` pairs.
Each KuzuDB commit synchronises WAL state; the per-commit fixed cost (measured in
the ARCHITECTURE_REVIEW.md § 11.6 baseline) dominates total indexing latency.

Batching N files into one transaction amortises the commit cost. With N=50 that
is 32 commits instead of 1,600 — a ~98% reduction in commit overhead while keeping
the unit of failure recoverable (one bad file aborts at most N-1 sibling upserts,
which the developer re-runs).

#### What to do

1. Add `batch_size: int = 50` parameter to `Indexer.index_repo()` between `no_ddl`
   and the end of the signature in
   [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py):

   ```python
   def index_repo(
       self,
       path: Path,
       dialect: str | None,
       db: GraphBackend,
       dbt_manifest: Path | None = None,
       timeout_per_file: int = 30,
       use_git: bool = True,
       progress_callback: Callable[[int, int], None] | None = None,
       schema_csv: Path | None = None,
       no_ddl: bool = False,
       batch_size: int = 50,
   ) -> dict:
   ```

   Update the docstring to add:

   ```text
   batch_size: Number of files committed per KuzuDB transaction in the upsert pass.
       Higher values reduce commit overhead but increase the per-batch recovery cost
       and the working-set size when a transaction holds locks. Default 50 is a
       balance between throughput on large corpora (1,000+ files) and memory/lock
       pressure on small ones. batch_size=1 reproduces the legacy per-file commit
       behaviour. Must be >= 1.
   ```

2. Add an input guard at the top of the method body, before any other work:

   ```python
   if batch_size < 1:
       raise ValueError(f"batch_size must be >= 1, got {batch_size}")
   ```

3. Replace the per-file commit loop with a batched loop. The current block
   ([`indexer.py` lines 130–149](../src/sqlcg/indexer/indexer.py)) becomes:

   ```python
   def _flush_batch(batch: list[ParsedFile]) -> None:
       """Upsert a batch of ParsedFile objects in a single transaction.

       On any per-file exception, the file is recorded as failed but the
       transaction continues for the remaining files in the batch. This
       matches the legacy per-file behaviour where one bad file did not
       abort the whole index run.
       """
       if not batch:
           return
       with db.transaction():
           for parsed_in_batch in batch:
               try:
                   counts = self._upsert_parsed_file(
                       parsed_in_batch, db, gold_tables=gold_tables
                   )
                   nonlocal_counts["tables"] += counts["tables"]
                   nonlocal_counts["edges"] += counts["edges"]
                   nonlocal_counts["star_sources"] += counts.get("star_sources", 0)
                   nonlocal_counts["columns_defined"] += counts.get("columns_defined", 0)
                   q_key = parsed_in_batch.parse_quality.value.lower()
                   nonlocal_counts["quality"][q_key] += 1
               except Exception as exc:
                   logger.warning(
                       "Failed to upsert %s: %s — skipping", parsed_in_batch.path, exc
                   )
                   nonlocal_counts["quality"]["failed"] += 1

   nonlocal_counts: dict = {
       "tables": 0, "edges": 0, "star_sources": 0, "columns_defined": 0,
       "quality": {"full": 0, "table_only": 0, "scripting_fallback": 0, "failed": 0},
   }
   batch: list[ParsedFile] = []
   for parsed in pass2_results:
       skip_upsert = no_ddl and any("pure_ddl_skip" in e for e in parsed.errors)
       if skip_upsert:
           nonlocal_counts["quality"]["scripting_fallback"] += 1
           continue
       batch.append(parsed)
       if len(batch) >= batch_size:
           _flush_batch(batch)
           batch = []   # IMPORTANT — free the references so GC can reclaim parsed.statements
   _flush_batch(batch)   # final partial batch
   ```

   **Memory rationale** (must appear as an inline comment near `batch = []`):
   the OOM regression documented in CLAUDE.md "OOM history" was caused by holding
   *all* `ParsedFile` objects in memory before writing. `pass2_results` already
   holds them all (that is the existing OOM surface — not touched here). The new
   `batch` list holds at most `batch_size` references already in `pass2_results`,
   adding no new memory cost. We do NOT detach `parsed` from `pass2_results` mid-loop
   because the surrounding `for parsed in pass2_results:` iterates the existing list.

4. Update the unpacking of `_upsert_parsed_file` results (currently lines 141–146) —
   move the accumulators into `nonlocal_counts` and read them out at return time:

   ```python
   return {
       "files_parsed": len(pass2_results),
       "parse_errors": parse_errors,
       "tables_found": nonlocal_counts["tables"],
       "lineage_edges_created": nonlocal_counts["edges"],
       "columns_defined": nonlocal_counts["columns_defined"],
       "star_sources": nonlocal_counts["star_sources"],
       "star_edges_expanded": star_edges_expanded,
       "quality": nonlocal_counts["quality"],
       "batch_size": batch_size,
   }
   ```

   Adding `batch_size` to the return dict makes the chosen value observable in CLI
   output and tests without an extra accessor.

5. **Important — `_expand_star_sources` call site**: the current call (line 152) runs
   *after* the upsert loop, outside any transaction. PERF-BATCH does not change this.
   Verify by grep: `_expand_star_sources` must remain outside the batched loop.

#### Choice of default `batch_size = 50`

| Candidate | Pro | Con |
|-----------|-----|-----|
| 1 | Identical to legacy; zero regression risk | No speedup — defeats the ticket |
| 10 | Conservative; small batch | 160 commits for 1,600 files — still expensive |
| 50 (chosen) | 32 commits for 1,600 files; ~50 × `ParsedFile` working set ≈ tens of MB on the typical small-statements file | Slightly higher per-failure blast radius |
| 200 | 8 commits for 1,600 files | Memory cost grows linearly with N for very wide files; one large CTAS-heavy file × 200 could push 100s of MB |
| 1000 | 2 commits | Approaches the pre-fix OOM regime for large corpora |

`50` is the same value `ARCHITECTURE_REVIEW.md § 11.6` recommends and is the
minimum N that gives a meaningful commit reduction without going beyond the "tens
of MB" working-set bound. **Document the choice in the docstring and in the CLI
help text** so a future tuner does not have to re-derive it.

#### Wiring verification

Before opening the PR, run (all four must succeed):

- `grep -n "batch_size" src/sqlcg/indexer/indexer.py` — must show:
  1. The parameter in `index_repo` signature
  2. The `ValueError` guard
  3. The `len(batch) >= batch_size` comparison
  4. The `"batch_size": batch_size` return-dict entry
- `grep -n "_flush_batch\|def _flush_batch" src/sqlcg/indexer/indexer.py` — must
  show the inner function definition and at least two call sites (the in-loop flush
  and the final partial-batch flush).
- `grep -n "with db.transaction()" src/sqlcg/indexer/indexer.py` — must appear
  exactly **once** inside `_flush_batch` (not inside the outer `for parsed in
  pass2_results:` loop). The old per-file transaction must be gone.
- `grep -n "TODO" src/sqlcg/indexer/indexer.py` — must return zero matches in the
  happy path.

#### Files affected

- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — only file modified by T-01

#### Tests to add

Create `tests/integration/test_indexer_batching.py`:

- **Scenario A — `batch_size=1` produces identical graph to `batch_size=50`**:
  Setup: write 12 small SQL files to `tmp_path`, each `CREATE VIEW vN AS SELECT a FROM src;`.
  Action: index with `batch_size=1`, capture edge count + table count. Reset the
  KuzuBackend. Index with `batch_size=50`. Capture again.
  Assertion: edge counts equal; table counts equal; same set of `SqlTable.qualified`
  values via `MATCH (t:SqlTable) RETURN t.qualified ORDER BY t.qualified`.

- **Scenario B — `batch_size=1000` (larger than corpus) commits the partial final
  batch**:
  Setup: 5 SQL files.
  Action: index with `batch_size=1000`.
  Assertion: `summary["files_parsed"] == 5` AND `summary["tables_found"] >= 5` AND
  the graph contains 5 `:File` nodes (`MATCH (f:File) RETURN count(f) AS n`).
  This verifies the trailing-partial-batch flush fires.

- **Scenario C — `batch_size=0` raises `ValueError`**:
  Action: `indexer.index_repo(tmp_path, ..., batch_size=0)`.
  Assertion: `pytest.raises(ValueError, match="batch_size must be >= 1")`.

- **Scenario D — one bad file does not abort the batch**:
  Setup: two SQL files; one valid CREATE VIEW, one with content that triggers
  an upsert-time exception. Mock `_upsert_parsed_file` so the second call raises
  but the first succeeds.
  Action: index with `batch_size=10`.
  Assertion: `summary["quality"]["failed"] == 1` AND the valid file's table
  is present in the graph (`MATCH (t:SqlTable {name: "v_ok"}) RETURN count(t)`
  returns 1).

- **Scenario E — `batch_size` is observable in the return dict**:
  Action: `summary = indexer.index_repo(..., batch_size=7)`.
  Assertion: `summary["batch_size"] == 7`.

#### Acceptance criteria

- [ ] `batch_size: int = 50` parameter present in `index_repo` signature (grep confirmed)
- [ ] `ValueError` raised for `batch_size < 1` (Scenario C passes)
- [ ] `with db.transaction()` appears exactly once in `indexer.py`, inside `_flush_batch` (grep confirmed)
- [ ] Scenario A passes — edge counts identical across `batch_size=1` and `batch_size=50`
- [ ] Scenario B passes — final partial batch is committed
- [ ] Scenario D passes — one failed file in a batch does not lose its peers
- [ ] No TODO in the happy path of `indexer.py`
- [ ] Docstring documents the default-50 rationale and the OOM constraint

---

### T-02 — `--batch-size` CLI flag wiring

**Source**: [`ARCHITECTURE_REVIEW.md` § 11.6](../ARCHITECTURE_REVIEW.md) ("Batch size should be tunable via `--batch-size N`")

**Effort**: XS
**Depends on**: T-01
**Blocks**: NONE

#### Root cause

After T-01 lands, the only way to tune `batch_size` is to edit the indexer. The
CLI must expose the flag with the same default and a help string that warns about
the memory tradeoff documented in CLAUDE.md OOM history.

#### What to do

1. In [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py), add a
   Typer option after `no_ddl` (alphabetical / co-located with related perf flags):

   ```python
   batch_size: int = typer.Option(  # noqa: B008
       50,
       "--batch-size",
       help=(
           "Files per KuzuDB transaction in the upsert pass. "
           "Default 50 balances commit-overhead reduction (vs. legacy per-file commits) "
           "against per-batch memory cost. Lower values are safer for memory-constrained "
           "machines; higher values give marginal speedup at the cost of larger working sets. "
           "Set to 1 to reproduce legacy per-file commit behaviour."
       ),
   ),
   ```

2. Pass `batch_size=batch_size` to the `indexer.index_repo(...)` call (currently
   at [`index.py` lines 128–137](../src/sqlcg/cli/commands/index.py)):

   ```python
   summary = indexer.index_repo(
       path,
       dialect,
       backend,
       dbt_manifest,
       timeout_per_file,
       progress_callback=_make_progress_callback(total_files),
       schema_csv=None,
       no_ddl=no_ddl,
       batch_size=batch_size,
   )
   ```

3. **No** `--batch-size` echo in the summary output is required — the value is
   already in `summary["batch_size"]` (from T-01) and the regular summary line
   stays unchanged.

#### Wiring verification

- `grep -n "batch_size" src/sqlcg/cli/commands/index.py` — must show: the Typer
  option, the docstring/help mentioning "1 to reproduce legacy", AND the
  `batch_size=batch_size` kwarg in the `index_repo` call.
- `grep -n "TODO" src/sqlcg/cli/commands/index.py` — zero matches in the happy path.

#### Files affected

- [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py) — only file modified by T-02

#### Tests to add

- **Scenario A — CLI passes the value through to `index_repo`**:
  Use Typer's `CliRunner` (or a direct call to `index_cmd` mocking `Indexer`).
  Patch `Indexer.index_repo` and run `sqlcg index <tmp> --batch-size 7`.
  Assertion: `Indexer.index_repo` was called with `batch_size=7`.

- **Scenario B — default value is 50**:
  Run without `--batch-size` and assert the patched call received `batch_size=50`.

#### Acceptance criteria

- [ ] `--batch-size` flag present in `index_cmd` with default 50 (grep confirmed)
- [ ] Help text references the "1 to reproduce legacy" guidance
- [ ] `batch_size` reaches `Indexer.index_repo` (Scenario A passes)
- [ ] Default value is 50 (Scenario B passes)
- [ ] No TODO in the happy path of `index.py`

---

### T-03 — `tests/integration/test_live_anchors.py` — live KuzuBackend anchor graph assertions

**Source**: User request; supplements the parser-only xfail tests in
[`tests/snowflake/anchors/`](../tests/snowflake/anchors/) with end-to-end graph
state assertions.

**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

The current anchor tests (`tests/snowflake/anchors/test_anchor_*.py`) operate at
the parser level — they call `SnowflakeParser.parse_file()` and inspect
`stmt.column_lineage` in memory. They do **not** validate that the indexer writes
the same edges into KuzuDB, nor that MCP/Cypher consumers can find the anchors via
graph queries. A regression in `_upsert_parsed_file` or in the cross-file aggregator
that drops edges between the parser and the graph would not be caught by the
existing tests.

The two anchors are:

- **`OMLOOPSNELHEID`** — intra-file CTAS (temp tables `tmp_a`, `tmp_b`). Currently
  passes at the parser level (no xfail). **Expected to produce ≥1 `COLUMN_LINEAGE`
  edge in the graph today.**
- **`MA_AANTAL_OP_ORDER`** — 6-link cross-file chain. All 6 links currently
  `xfail strict=True` on E5 (CTE resolution with `SUM` aggregation, CTE-to-CTE
  resolution, CTE-to-INSERT, quoted-identifier view-column resolution).
  **Expected to produce 0 cross-chain edges in the graph today.**

LIVE-ANCHOR must therefore:
1. Assert positive presence for `OMLOOPSNELHEID` (≥1 edge); regression guard.
2. Assert presence of the **target column** `ma_aantal_op_order` as a `:SqlColumn`
   node (DDL-derived; will exist as long as the INSERT target table is upserted).
3. Make a **softer** assertion for the chain: ≥0 edges today, with a parametrised
   xfail marker per link that mirrors the parser-level xfails. When sprint 06/07's
   fixes flip the parser xfail to pass, this test's corresponding xfail must also flip.

#### What to do

1. Create [`tests/integration/test_live_anchors.py`](../tests/integration/test_live_anchors.py)
   with this structure:

   ```python
   """Live end-to-end anchor tests.

   Indexes the synthetic anchor fixtures from tests/snowflake/anchors/ into a real
   in-memory KuzuBackend and asserts the anchor columns appear in the graph with the
   expected edges. Complements the parser-only tests in tests/snowflake/anchors/ by
   covering the parser → aggregator → indexer → graph path end-to-end.

   Companion tests for the real DWH corpus are marked
   @pytest.mark.skip(reason="requires DWH corpus") and are intentionally NOT run in CI.
   """

   import shutil
   from pathlib import Path

   import pytest

   from sqlcg.core.kuzu_backend import KuzuBackend
   from sqlcg.indexer.indexer import Indexer


   ANCHOR_FIXTURES_DIR = Path(__file__).parent.parent / "snowflake" / "anchors"


   def _stage_anchor_corpus(tmp_path: Path, fixture_names: list[str]) -> Path:
       """Copy named anchor fixture files into a fresh corpus directory.

       Uses use_git=False on the indexer call site because tmp_path is not a git repo.
       """
       corpus = tmp_path / "corpus"
       corpus.mkdir()
       for name in fixture_names:
           src = ANCHOR_FIXTURES_DIR / name
           assert src.exists(), f"fixture missing: {src}"
           shutil.copy(src, corpus / name)
       return corpus


   def _index(corpus: Path) -> tuple[KuzuBackend, dict]:
       backend = KuzuBackend(":memory:")
       backend.init_schema()
       summary = Indexer().index_repo(
           corpus,
           dialect="snowflake",
           db=backend,
           use_git=False,
           batch_size=10,
       )
       return backend, summary


   def test_live_anchor_omloopsnelheid_has_column_lineage_edges(tmp_path):
       """OMLOOPSNELHEID must produce ≥1 COLUMN_LINEAGE edge into a temp table.

       The fixture defines tmp_a / tmp_b CTAS with omloopsnelheid = afzet / NULLIF(...).
       At least one of:
           stg_a.afzet -> tmp_a.omloopsnelheid
           stg_a.gemiddelde_vrd -> tmp_a.omloopsnelheid
       must exist as a COLUMN_LINEAGE edge in the graph.
       """
       corpus = _stage_anchor_corpus(tmp_path, ["fixture_omloopsnelheid.sql"])
       backend, summary = _index(corpus)

       rows = backend.run_read(
           "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) "
           "WHERE LOWER(d.col_name) = 'omloopsnelheid' "
           "RETURN s.table_name AS s_t, s.col_name AS s_c, "
           "       d.table_name AS d_t, d.col_name AS d_c",
           {},
       )
       assert len(rows) >= 1, (
           "OMLOOPSNELHEID anchor: expected ≥1 COLUMN_LINEAGE edge into a temp table "
           f"with dst col_name='omloopsnelheid'. Index summary: {summary}. "
           f"This is a regression against the parser-level "
           f"tests/snowflake/anchors/test_anchor_omloopsnelheid.py."
       )


   def test_live_anchor_ma_target_column_exists_in_graph(tmp_path):
       """MA_AANTAL_OP_ORDER target column node must exist post-index.

       The INSERT target wtfs_openstaande_orders.ma_aantal_op_order is referenced
       in the INSERT column list, so the indexer must upsert at least the target
       column node even when sprint-06 E5 fixes do not yet wire the full chain.
       """
       corpus = _stage_anchor_corpus(
           tmp_path,
           ["fixture_source.sql", "fixture_etl.sql", "fixture_semantic.sql"],
       )
       backend, summary = _index(corpus)

       rows = backend.run_read(
           "MATCH (c:SqlColumn) "
           "WHERE LOWER(c.col_name) = 'ma_aantal_op_order' "
           "RETURN c.id AS id",
           {},
       )
       assert len(rows) >= 1, (
           "MA_AANTAL_OP_ORDER target column must exist as a SqlColumn node. "
           f"Index summary: {summary}."
       )


   @pytest.mark.xfail(
       strict=False,
       reason=(
           "MA_AANTAL_OP_ORDER cross-file chain: E5 (CTE-to-INSERT with SUM and UNION) "
           "not yet implemented end-to-end. Mirrors the parser-level xfails in "
           "tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py. "
           "When the parser xfails flip to pass, remove this marker — the assertion "
           "below will start passing automatically."
       ),
   )
   def test_live_anchor_ma_chain_has_at_least_one_lineage_edge(tmp_path):
       """≥1 COLUMN_LINEAGE edge anywhere along the MA_AANTAL_OP_ORDER chain.

       Targets the broadest possible assertion: any edge whose destination is
       wtfs_openstaande_orders.ma_aantal_op_order, OR an edge whose source is
       source_facts.ma_order_aantal. Either signals partial chain coverage.
       """
       corpus = _stage_anchor_corpus(
           tmp_path,
           ["fixture_source.sql", "fixture_etl.sql", "fixture_semantic.sql"],
       )
       backend, _summary = _index(corpus)

       rows = backend.run_read(
           "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) "
           "WHERE (LOWER(d.table_name) = 'wtfs_openstaande_orders' "
           "       AND LOWER(d.col_name) = 'ma_aantal_op_order') "
           "   OR (LOWER(s.table_name) = 'source_facts' "
           "       AND LOWER(s.col_name) = 'ma_order_aantal') "
           "RETURN count(e) AS n",
           {},
       )
       assert rows[0]["n"] >= 1


   # ---------------------------------------------------------------------------
   # Real DWH corpus variants — skipped by default. Run manually with:
   #   uv run pytest tests/integration/test_live_anchors.py -k dwh --runxfail
   # after exporting SQLCG_DWH_CORPUS=/path/to/dwh.
   # ---------------------------------------------------------------------------

   @pytest.mark.skip(reason="requires DWH corpus (set SQLCG_DWH_CORPUS to run)")
   def test_live_anchor_omloopsnelheid_dwh():
       """Production-corpus version of the OMLOOPSNELHEID anchor."""
       ...

   @pytest.mark.skip(reason="requires DWH corpus (set SQLCG_DWH_CORPUS to run)")
   def test_live_anchor_ma_aantal_op_order_dwh():
       """Production-corpus version of the MA_AANTAL_OP_ORDER anchor."""
       ...
   ```

2. **Cypher query alignment** — the column-property names used in the queries
   (`col_name`, `table_name`, `table_qualified`, `id`) must match
   [`indexer.py` lines 326–336, 405–425](../src/sqlcg/indexer/indexer.py).
   Verified above in code-vs-plan: those property names ARE what `_upsert_parsed_file`
   writes today. Do not invent new property names in the test.

3. The `xfail strict=False` marker on `test_live_anchor_ma_chain_has_at_least_one_lineage_edge`
   is intentional: when sprint 06's T-01 (FIX-E5) plus future cross-file work
   eventually start producing edges for this chain, the test must **start passing
   without code change**. `strict=False` lets it pass silently when the implementation
   catches up. Document this in a comment above the marker.

#### Wiring verification

- `ls tests/integration/test_live_anchors.py` — file exists
- `grep -n "ANCHOR_FIXTURES_DIR" tests/integration/test_live_anchors.py` — points
  to the existing `tests/snowflake/anchors/` directory (no new fixtures authored)
- `grep -n "use_git=False" tests/integration/test_live_anchors.py` — must appear
  in `_index()` (tmp_path is not a git repo; otherwise `walk_sql_files` returns nothing)
- `grep -n "@pytest.mark.skip" tests/integration/test_live_anchors.py` — must
  appear on at least two DWH variants
- `grep -n "col_name\|table_name" src/sqlcg/indexer/indexer.py` — confirm property
  names used in the test match those written by `_upsert_parsed_file`

#### Files affected

- `tests/integration/test_live_anchors.py` — NEW

#### Tests to add

This ticket *is* the test. The acceptance criteria below describe what each
test in the file must observe.

#### Acceptance criteria

- [ ] `test_live_anchor_omloopsnelheid_has_column_lineage_edges` passes
- [ ] `test_live_anchor_ma_target_column_exists_in_graph` passes
- [ ] `test_live_anchor_ma_chain_has_at_least_one_lineage_edge` is collected (xfail
  or pass — either is acceptable; the marker is `strict=False`)
- [ ] Both DWH-corpus variants are present with `@pytest.mark.skip` markers and
  reasons that explain how to opt in
- [ ] All Cypher queries use property names that grep-confirm in
  `src/sqlcg/indexer/indexer.py` (`col_name`, `table_name`, `id`, `table_qualified`)
- [ ] `use_git=False` is passed to `index_repo` (tmp_path is not a git repo)
- [ ] No `cwd` manipulation; all paths are constructed from `tmp_path` and
  `Path(__file__).parent`

---

### T-04 — MEAS-RERUN: documented re-run of `collect_parse_errors.py` post-sprint-06

**Source**: [`plan/sprint_06_lineage_coverage.md`](sprint_06_lineage_coverage.md)
"Code-vs-Plan Verification" section; user request to measure E5/E2 deltas post-sprint-06.

**Effort**: S (measurement task — no code changes)
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

Sprint 06 shipped FIX-E5 (T-01), FIX-E2 (T-02), FIX-DDL-SKIP (T-03), FIX-E4 (T-04),
OPT-SCOPE (T-05). The real impact on E5 / E2 / E36 error counts in the production
corpus has not been re-measured since the sprint-06 PRs merged. Without a measured
delta, sprint 08 planning has no quantitative basis to choose its next target.

This ticket is **not** a code change. It is a structured measurement task with a
fixed input, a fixed expected output schema, and a documented place to file the
result. Treat it as a runbook step the developer executes once during the sprint.

#### What to do

1. Run the existing diagnostic script on the same 1,445-file Snowflake DWH corpus
   used in the sprint-06 baseline. Use the command pattern from
   [`plan/parsing_errors_experiment.md`](parsing_errors_experiment.md) § "Experiment
   Execution":

   ```bash
   uv run python scripts/collect_parse_errors.py \
       /home/ignwrad/Projects/dwh \
       --dialect snowflake \
       --output plan/measurements/sprint_07_postsprint06_results.json
   ```

   (`plan/measurements/` is a new directory — the developer creates it as part of
   T-04. The file is intended to be committed; it is the sprint's measurement of record.)

2. **Expected output format** — the JSON produced by the script (per the script's
   existing schema) must contain at least these fields, which the developer
   summarises in the sprint postmortem:

   | Field | Meaning |
   |-------|---------|
   | `summary.files_total` | Files scanned |
   | `summary.files_parsed_ok` | E4 = 0 (full parse success) |
   | `summary.column_lineage_edges` | **Primary metric** — total edges across corpus |
   | `errors_by_code.E5` | "Cannot find column" count |
   | `errors_by_code.E2` | unaliased-expression-skip count |
   | `errors_by_code.E3` | scripting-fallback / DDL-noise count |
   | `errors_by_code.E36` | cross-file scope failures |
   | `timing.p50_s`, `timing.p95_s`, `timing.p99_s` | Per-file latency distribution |

3. **Comparison table to fill in** — after the run, the developer appends this
   table to [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) under a new
   subsection `### 12.7 Sprint-06 Post-Merge Re-measurement (2026-05-12)`:

   | Metric | Sprint-06 baseline | Sprint-07 re-run | Delta | Sign |
   |--------|-------------------:|----------------:|------:|:-----|
   | Column lineage edges (fact/) | 12,242 | __ | __ | __ |
   | Column lineage edges (IA-SEMANTIC/) | 3,907 | __ | __ | __ |
   | Column lineage edges (IA-DATAPRODUCTS/) | 3,373 | __ | __ | __ |
   | E5 count (fact/) | 154 | __ | __ | __ |
   | E5 count (IA-SEMANTIC/) | 632 | __ | __ | __ |
   | E2 count (corpus-wide) | ~441 cols skipped | __ | __ | __ |
   | E36 count (cross-file scope) | (38/184 zero-edge files) | __ | __ | __ |
   | p99 indexing latency | 18–21s (WTDH_ARTIKEL) | __ | __ | __ |
   | E4 (parse failure) | 0 | __ | __ | __ |

   "Sign" column: ✅ improved, ➖ unchanged within ±5%, ❌ regression.

4. **Acceptance** of the measurement (not the code): the JSON file must be
   committed under `plan/measurements/sprint_07_postsprint06_results.json` and the
   table above must be filled in with concrete numbers — no `__` placeholders.

5. **Decision rule for sprint 08**: if E5 count drops by ≥80% (sprint-06 T-01
   worked as planned), sprint 08 targets E36 (cross-file scope). If E5 stayed flat
   or got worse, sprint 08 re-opens E5 with an Opus-led postmortem first
   (escalation rule per `~/.claude/projects/.../memory/MEMORY.md`
   `project_lineage_coverage_escalation.md`). The architect-reviewer owns this
   decision and writes it into `ARCHITECTURE_REVIEW.md` § 12 after the data lands.

#### Wiring verification

T-04 has no code wiring. The verification is purely artifact-level:

- `ls plan/measurements/sprint_07_postsprint06_results.json` — file exists, ≥1 KB
- `grep -c "summary.column_lineage_edges\|column_lineage_edges" plan/measurements/sprint_07_postsprint06_results.json`
  — at least 1 match (JSON contains the primary metric)
- `grep -n "### 12.7 Sprint-06 Post-Merge Re-measurement" ARCHITECTURE_REVIEW.md`
  — section exists and table is populated (no `__` placeholders)

#### Files affected

- `plan/measurements/sprint_07_postsprint06_results.json` — NEW (measurement artifact)
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — new subsection `### 12.7`

#### Tests to add

None — measurement task.

#### Acceptance criteria

- [ ] `plan/measurements/sprint_07_postsprint06_results.json` exists and contains
  the fields listed in step 2
- [ ] `ARCHITECTURE_REVIEW.md` § 12.7 exists and the comparison table is fully populated
  (no `__` placeholders, every "Sign" cell filled)
- [ ] Decision rule outcome (E36 next OR re-open E5 with Opus) is recorded in § 12.7
- [ ] The run uses the same corpus path as the sprint-06 baseline (documented in § 12.1)
  — same root, same dialect flag

---

## Test Strategy

### The Single Most Important Regression Guard

**File**: `tests/integration/test_indexer_batching.py::test_batch_size_equivalence_1_vs_50`
(Scenario A of T-01).

**Why**: PERF-BATCH is the highest-risk change in the sprint because it touches
the commit boundary that historically caused OOM. Equivalence between
`batch_size=1` (legacy behaviour) and `batch_size=50` (new default) is the
single property that proves the change is **observably non-behavioural at the
graph level**. Any future "let's make batches even bigger" optimisation can
re-run this test against the new value as a fast check.

```python
def test_batch_size_equivalence_1_vs_50(tmp_path):
    """Graph state after index_repo(batch_size=1) must equal
    graph state after index_repo(batch_size=50) for the same input."""
    # 12 small SQL files mixing CREATE VIEW, CREATE TABLE, and INSERT
    _write_corpus(tmp_path / "c1", N=12)
    _write_corpus(tmp_path / "c50", N=12, identical_to=tmp_path / "c1")

    b1 = KuzuBackend(":memory:"); b1.init_schema()
    b50 = KuzuBackend(":memory:"); b50.init_schema()

    Indexer().index_repo(tmp_path / "c1", "snowflake", b1, use_git=False, batch_size=1)
    Indexer().index_repo(tmp_path / "c50", "snowflake", b50, use_git=False, batch_size=50)

    def _state(b):
        return {
            "tables": sorted(r["q"] for r in b.run_read(
                "MATCH (t:SqlTable) RETURN t.qualified AS q", {})),
            "edges": b.run_read(
                "MATCH ()-[e:COLUMN_LINEAGE]->() RETURN count(e) AS n", {})[0]["n"],
            "columns": b.run_read(
                "MATCH (c:SqlColumn) RETURN count(c) AS n", {})[0]["n"],
        }

    assert _state(b1) == _state(b50)
```

### General test layout

| Ticket | Test file | Layer |
|--------|-----------|-------|
| T-01 | `tests/integration/test_indexer_batching.py` | integration (in-memory KuzuBackend) |
| T-02 | `tests/unit/test_index_cli.py` (extend or add) | unit (Typer CliRunner + Indexer mock) |
| T-03 | `tests/integration/test_live_anchors.py` | integration (in-memory KuzuBackend) |
| T-04 | n/a — measurement task | n/a |

---

## Wiring Checklist

| Question | T-01 | T-02 | T-03 | T-04 |
|----------|------|------|------|------|
| What calls this? | `Indexer.index_repo` is called by `index_cmd` (CLI) and by `reindex_file` indirectly via `_upsert_parsed_file` (no — `reindex_file` is its own path and is unaffected). Tests call `index_repo` directly. | `index_cmd` is the Typer command; its `--batch-size` value reaches `Indexer.index_repo(batch_size=...)`. | The test file is collected by pytest; no other caller. | The script `collect_parse_errors.py` is invoked manually; output is read by the developer when filling § 12.7. |
| Where is the callback/parameter passed? | `batch_size` flows: CLI option → `Indexer.index_repo(batch_size=...)` → local variable in the method → `len(batch) >= batch_size` comparison. The transaction is opened inside `_flush_batch(batch)`. | `batch_size = typer.Option(50, ...)` → `indexer.index_repo(..., batch_size=batch_size)`. | The test uses `Indexer().index_repo(..., batch_size=10)` explicitly (T-01 must be merged first). | N/A — no parameter flow. |
| What constant/path does this align with? | Default `50` aligns with [`ARCHITECTURE_REVIEW.md` § 11.6](../ARCHITECTURE_REVIEW.md) explicit recommendation. **Do not invent a different default.** | Same — `50` matches the indexer default; no second source of truth. | Anchor fixtures live at `tests/snowflake/anchors/` — relative path from `tests/integration/test_live_anchors.py` is `Path(__file__).parent.parent / "snowflake" / "anchors"`. Do not hardcode an absolute path. | The corpus path is the same as `ARCHITECTURE_REVIEW.md § 12.1` ("etl/sql/fact/", etc.). The output filename pattern `sprint_07_postsprint06_results.json` is plan-specified. |
| Does any TODO remain in the happy path? | No — T-01 does not introduce a TODO. | No. | No — `@pytest.mark.skip` markers on DWH variants are explicit feature gates, not TODOs. | No. |

---

## Acceptance Criteria (sprint-level)

- [ ] `sqlcg index <dir> --batch-size 50` is the default behaviour and produces an
  identical graph (table set, column set, edge count) to `--batch-size 1` on a
  12-file synthetic corpus (T-01 Scenario A).
- [ ] `sqlcg index <dir> --batch-size 0` exits with `ValueError` / non-zero exit code
  (T-01 Scenario C).
- [ ] On a 1,600-file corpus with `schema.csv` provided, `sqlcg index ./sql
  --batch-size 50` completes in under 5 minutes on a laptop (CLAUDE.md perf budget).
  Measured time is recorded in the sprint postmortem.
- [ ] On a 20-file ETL corpus with no schema CSV, `sqlcg index ./sql` (no flag)
  produces identical graph state to before T-01 — confirmed by re-running the
  small-repo e2e test suite. **Small-repo regression must be zero.**
- [ ] `tests/integration/test_live_anchors.py` exists, has the two non-skipped tests
  passing, and the one xfail-soft test collected (T-03).
- [ ] `plan/measurements/sprint_07_postsprint06_results.json` exists with non-empty
  metrics (T-04).
- [ ] `ARCHITECTURE_REVIEW.md` § 12.7 is populated with concrete numbers for every
  cell of the comparison table (T-04).
- [ ] No new TODO comments in any file changed by this sprint (grep across
  `indexer.py`, `index.py`).

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Single large transaction holds row locks long enough to cause a wait timeout in concurrent `sqlcg watch` runs | LOW — `sqlcg watch` uses `reindex_file`, which is a separate code path with its own short transaction (per file). PERF-BATCH only changes `index_repo`. | Verified by code-vs-plan: `reindex_file` ([`indexer.py` lines 165–197](../src/sqlcg/indexer/indexer.py)) is untouched. Add a comment in `_flush_batch` noting this isolation. |
| `batch_size=50` × a file with 10,000 columns causes a working-set spike that approximates the pre-fix OOM | LOW — `ParsedFile` for the worst observed file (`WTDH_ARTIKEL*.sql`) is ~10 MB in memory; 50× is ~500 MB. After T-03 of sprint-06 (DDL-skip) such files are already filtered out before reaching the upsert loop. | Document the bound in T-01's docstring. If a user hits OOM, `--batch-size 1` reproduces legacy behaviour exactly. |
| One bad file in a batch aborts 49 sibling upserts in the same transaction | MEDIUM — KuzuDB rollback would discard the successful peers' writes | **Mitigated in T-01 step 3**: `_flush_batch` wraps `_upsert_parsed_file` in a try/except inside the transaction so per-file exceptions are caught and logged. The transaction commits the successful peers. Scenario D of T-01 tests this. |
| `test_live_anchor_ma_chain_has_at_least_one_lineage_edge` flips from xfail to pass mid-sprint (E5 partial fix lands) and `strict=False` hides the change | LOW | The test prints sufficient context (no behaviour change required when it flips); the developer notices via pytest output that an `XPASS` happened. The marker docstring explicitly says to remove it on flip. |
| MEAS-RERUN takes longer than expected (script runs > 30 min on the corpus) | MEDIUM — script timing is already known (1,457 files in ~3 min on baseline machine) | Acceptable — T-04 is a one-shot measurement task budgeted as S. If runtime is unexpectedly high, that fact itself is a sprint finding worth recording. |
| `walk_sql_files(tmp_path, use_git=False)` does not pick up the staged fixtures because of an unrelated walker bug | LOW — `use_git=False` falls back to `rglob` per [`indexer.py` line 46](../src/sqlcg/indexer/indexer.py) docstring | Scenario A of T-03 implicitly verifies this; if the test sees zero files indexed, the walker is the suspect. |

---

## Blocking Questions

None. All architecture decisions are documented in
[`ARCHITECTURE_REVIEW.md` § 11.6](../ARCHITECTURE_REVIEW.md) (batch size choice and
rationale) and CLAUDE.md (OOM constraint, perf budget). Anchor fixtures already
exist. Script already exists. No new design choice is required.
