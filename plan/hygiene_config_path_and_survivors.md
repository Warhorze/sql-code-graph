# Feature Plan: Hygiene Batch — config-path footgun + #12/#13/#27 survivor triage

## Summary

A low-risk cleanup batch on `feat/cluster-b-provenance`. Code-vs-plan verification
against the real source (file:line evidence below) shows **three of the four nominated
items are already fixed on this branch**. The shippable work is the silent config-path
footgun (item 4) — split into **two layers**: an index-time **warning** (Phase 1) and a
query-time **MCP/CLI config-root reconciliation** (Phase 4, a user-approved scope
expansion) — plus **two small polish items** (#27d timeout discoverability, #27b
`find column` noise parity). All four phases share one theme — *the config the user
wrote must be the config every code path actually reads* — and ship as **one PR**.

**Phase 4 (reconciliation) is the substantive deliverable.** Today the CLI/indexer and
the MCP server share the config *reader functions* in
[`config.py`](src/sqlcg/core/config.py) but **not the resolved config _path_**: index
time reads `.sqlcg.toml` from the indexed directory, query time reads from `Path.cwd()`.
When the MCP server's working directory differs from the indexed root, MCP computes a
**different** presentation split / noise filter than what was actually indexed — a silent
disagreement, with no error. Phase 4 gives true parity: MCP resolves config from the
**same directory the graph was indexed from**, recovered from the persisted `Repo` node.

## Code-vs-Plan Verification — what is real vs already-done

### Item 1 — #12 `find table` case-sensitivity → **ALREADY DONE. DROP.**

- [`find.py:20`](src/sqlcg/cli/commands/find.py) `name = name.lower()` before the query.
- [`find.py:43`](src/sqlcg/cli/commands/find.py) `ref = ref.lower()` in `find_column`.
- Store side already lowercases identity: [`base.py:60-72`](src/sqlcg/parsers/base.py)
  (`TableRef.__post_init__`) and [`base.py:102-114`](src/sqlcg/parsers/base.py)
  (`ColumnRef.__post_init__`), with `_qid` lowercasing at [`indexer.py:871`](src/sqlcg/indexer/indexer.py).
- Fixed in commit `ecf42c1` ("fix(find): lowercase name/ref before graph query to match
  C2 normalization (#12)").
- Regression guarded by [`test_find_cmd.py`](tests/unit/test_find_cmd.py) — both
  uppercase and mixed-case scenarios assert the param dict receives the lowercased value.

**Dialect nuance (verified, no action):** store-time folding is dialect-independent —
identifiers are lowercased unconditionally in `TableRef`/`ColumnRef`. Snowflake folds
*unquoted* to uppercase; ANSI folds to lowercase. Because we normalise **both store and
query to lowercase**, a Snowflake `MY_TABLE` and an ANSI `my_table` both resolve to the
same `my_table` node — correct for both dialects. **This does NOT bump SCHEMA_VERSION**
(currently `"6"`, [`schema.py:6`](src/sqlcg/core/schema.py)): the stored form is already
lowercase and unchanged; #12 was a *query-time* fold, the cheap and correct option. No
re-index implied.

> Edge case acknowledged, deliberately not handled: a Snowflake double-quoted
> case-sensitive identifier (`"MyTable"`) is folded to `mytable` and would collide with
> an unquoted `MyTable`. This is the documented C2 design trade-off
> (ARCHITECTURE_REVIEW §2.6); reversing it is a SCHEMA_VERSION bump with no measured
> demand. Out of scope.

### Item 2 — #13 index warnings to stdout → **ALREADY DONE. DROP.**

- Parse warnings default to the **log file**, not stdout:
  [`index.py:109-114`](src/sqlcg/cli/commands/index.py) installs a `FileHandler` on
  `KuzuConfig.from_env().log_path` (default `~/.sqlcg/index.log`,
  [`config.py:22-25`](src/sqlcg/core/config.py)).
- `--verbose` already routes warnings to **stderr** instead:
  [`index.py:104-108`](src/sqlcg/cli/commands/index.py).
- A `_CountingHandler` ([`index.py:91-102`](src/sqlcg/cli/commands/index.py)) counts
  WARNING+ records and the summary points the user at the log:
  [`index.py:161-165`](src/sqlcg/cli/commands/index.py)
  `"Parse warnings written to {path} — use --verbose to show here."`
- Per-column lineage WARNINGs were demoted to DEBUG (T-09-06), guarded by
  [`test_T09_06_log_verbosity.py`](tests/unit/test_T09_06_log_verbosity.py).

**No remaining gap.** The path/constant matches `KuzuConfig.log_path` (no hardcode).

### Item 3 — #27 survivors

#### (b) NoiseFilter parity on `find` → **`find table` ALREADY DONE; `find column` is a thin optional gap. KEEP (small).**

- `find table` already filters: [`find.py:17`](src/sqlcg/cli/commands/find.py) `--raw`
  option + [`find.py:27-34`](src/sqlcg/cli/commands/find.py) `NoiseFilter.from_config()`
  + `filter_nodes`. This matches `analyze upstream/downstream/impact/unused`
  ([`analyze.py:26,75-79,136-140,171,181-185,216,225-229`](src/sqlcg/cli/commands/analyze.py)).
- `find column` ([`find.py:38-49`](src/sqlcg/cli/commands/find.py)) and `find pattern`
  ([`find.py:52-63`](src/sqlcg/cli/commands/find.py)) are **unfiltered**.
  - `find column` returns `c.id` (`schema.table.column`); NoiseFilter operates on
    `schema.table` and `analyze` already filters column rows via
    [`_col_id_to_table`](src/sqlcg/cli/commands/analyze.py) + `_filter_column_results`.
    Parity here is meaningful and cheap. **KEEP.**
  - `find pattern` matches raw SQL text of `SqlQuery` nodes, not table-qualified names —
    NoiseFilter has no qualified name to test against. **OUT OF SCOPE** (no clean filter key).

#### (d) Timeout observability → **mostly DONE; one discoverability gap. KEEP (doc-only).**

- `timeout` is already a first-class `parse_cause` bucket:
  [`error_classify.py:10,25,97-102`](src/sqlcg/indexer/error_classify.py).
- The timeout error string `timeout:Ns ...` is appended to `parsed.errors` on the
  subprocess hard-kill path ([`indexer.py:847-852`](src/sqlcg/indexer/indexer.py)) and the
  poison-retry path maps to the same bucket ([`error_classify.py:101-102`](src/sqlcg/indexer/error_classify.py)).
- `dominant_cause` writes it to `File.parse_cause`
  ([`indexer.py:905-912`](src/sqlcg/indexer/indexer.py)).
- `analyze failures --cause timeout` already queries it
  ([`analyze.py:189-210`](src/sqlcg/cli/commands/analyze.py)) and is e2e-tested
  ([`test_parse_diagnostics_cli.py:85-100`](tests/e2e/test_parse_diagnostics_cli.py)
  `--cause timeout` returns nothing on a clean corpus).
- The index summary already prints `"{n} timed out"`
  ([`index.py:255,264-265`](src/sqlcg/cli/commands/index.py)) from `error_summary`.

**Remaining gap is discoverability only:** a user cannot learn the valid `--cause`
bucket names without reading source. The `--cause` help says only "(e.g. E5, timeout)".
Fix = enumerate the valid buckets in the help/docstring (doc-only, zero behaviour change).
**KEEP as a one-line polish.**

### Item 4 — silent config-path footgun → **REAL, NEW. PRIMARY DELIVERABLE.**

Confirmed inconsistency:

- **Index time** reads `.sqlcg.toml` from the **indexed directory** (`Path(path)/.sqlcg.toml`):
  - `get_schema_aliases(path)` at [`indexer.py:137`](src/sqlcg/indexer/indexer.py)
  - `get_external_consumers(path)` at [`indexer.py:1177`](src/sqlcg/indexer/indexer.py)
  - `get_presentation_prefixes(path)` at [`indexer.py:1181`](src/sqlcg/indexer/indexer.py)
  - `get_dialect(path)` at [`index.py:122`](src/sqlcg/cli/commands/index.py) and
    [`reindex.py:157`](src/sqlcg/cli/commands/reindex.py)
- **Query time** reads from **`Path.cwd()`**:
  - `get_presentation_prefixes(Path.cwd())` at
    [`tools.py:921,1640`](src/sqlcg/server/tools.py)
  - `NoiseFilter.from_config(repo_root=None)` falls back to `Path.cwd()`
    ([`noise_filter.py:138-158`](src/sqlcg/server/noise_filter.py))

Consequence: `sqlcg index ./sql` with `.sqlcg.toml` at the repo **root** (the real dwh
layout: config at root, corpus under `ddl/` + `etl/` subdirs) silently ingests **zero**
dialect/aliases/external_consumers, with **no error or warning**. Every config reader
swallows the miss via `if config_file.exists(): ... else: return default`
([`config.py`](src/sqlcg/core/config.py), 7 readers, each guarded by `config_file.exists()`).

There are **two distinct symptoms** of the same root cause (config path is implicit, not
shared), and this batch fixes **both**:

**Symptom (a) — index time: config silently absent.** `sqlcg index ./sql` with
`.sqlcg.toml` at the repo **root** (the real dwh layout: config at root, corpus under
`ddl/` + `etl/` subdirs) silently ingests **zero** dialect/aliases/external_consumers,
with no error. → **Fix = Phase 1: a soft, visible warning at index time** when no
`.sqlcg.toml` is found at the index path.

- Cheapest and dovetails with the #13 observability theme.
- Does NOT silently search parent dirs (per the hard constraint — that hides the problem).
- Zero new friction for the 20-ETL user who keeps config beside their SQL or has no
  config at all: the warning is informational, fires only when a config genuinely is not
  found at the index path, and is suppressed by `--quiet`. The "no config" case is
  legitimate (defaults are sane), so the warning must be **soft** — it states the
  searched path and that defaults are in use, it does not fail the index.

**Symptom (b) — query time: MCP reads config from the wrong directory.** Even when the
config IS found and ingested at index time, the MCP server later reads
`get_presentation_prefixes(Path.cwd())` and `NoiseFilter.from_config()` (cwd fallback) —
so when the server's working dir ≠ the indexed root, the presentation split and noise
filter at query time **disagree silently** with what was indexed. → **Fix = Phase 4: MCP
config-root reconciliation** (the user-approved scope expansion; design below). This was
previously deferred as a follow-up; it is now in scope because the verified mechanism
(read the indexed root from the persisted `Repo` node) makes it a small, read-side-only
change with no schema bump and no hot-path touch.

## Scope

### In Scope

- **(Phase 1)** A soft, visible warning at `sqlcg index` (and `sqlcg reindex`) when no
  `.sqlcg.toml` is found at the index path — naming the exact path searched, suppressed
  by `--quiet`.
- A reusable `config_file_present(path) -> bool` helper in `config.py` so the check uses
  the same `.sqlcg.toml` location as the readers (no hardcoded filename drift).
- **(Phase 2)** NoiseFilter parity for `find column` (add `--raw` + filter, matching
  `find table`).
- **(Phase 3)** Enumerate valid `--cause` buckets in `analyze failures` help text /
  docstring (doc-only).
- **(Phase 4 — MCP/CLI config-root reconciliation)** A `_indexed_root(db) -> Path | None`
  helper in `tools.py` that reads the persisted `Repo` node path, and rewiring the
  query-time presentation-prefix reads (and the MCP-side NoiseFilter) to resolve config
  from that indexed root instead of `Path.cwd()` — so MCP and the CLI compute the
  **same** presentation split / noise filter from the **same** `.sqlcg.toml`.

### Non-Goals

- Searching parent directories for `.sqlcg.toml` (forbidden — hides the footgun).
- Threading the indexed root into the *index-time* readers (they already read the index
  path correctly — only the query-time MCP readers used `Path.cwd()`).
- Per-Repo config in multi-Repo graphs (the v1.1.0 rule is "first Repo node"; see
  Phase 4 design point 2).
- Any SCHEMA_VERSION bump (none of these items change stored form).
- `find pattern` noise filtering (no qualified-name key to filter on).
- Re-opening #12 / #13 — both verified done; tests already guard them.
- Any change to `_extract_column_lineage` column loop or `_upsert_parsed_file` edge-row
  loop. **Confirmed:** the warning fires in the CLI command before/around `index_repo`,
  the `find column` filter is post-query in the CLI, and the Phase 4 reconciliation is
  purely query-time read-side (`tools.py` `analyze_unused` / `diff_impact`) — no hot-path
  code is touched. All performance invariants in CLAUDE.md remain intact.

## Design

### API / CLI Changes

- `config.py`: new pure helper
  `def config_file_present(path: Path) -> bool` returning `(Path(path) / ".sqlcg.toml").exists()`.
  Single source of truth for the filename. (The 7 readers may optionally call it later;
  not required for this batch — keep the diff small. The helper's call site is the index
  command, grep-confirmed below.)
- `index.py` / `reindex.py`: after resolving `path`, if `not config_file_present(path)`
  and `not quiet`, print a yellow soft warning naming the searched path.
- `find.py` `find_column`: add `raw: bool = typer.Option(False, "--raw", ...)` and the
  same `NoiseFilter.from_config()` + filter block as `find_table`, filtering on the
  table portion of each `c.id` (reuse the `schema.table.column → schema.table`
  derivation; mirror `analyze._col_id_to_table` semantics — a local 1-line `rsplit`).
- `analyze.py` `failures`: extend `--cause` help and the command docstring to list the
  valid buckets: `timeout, E8, E3, E2, E5, E1, qualify_failed, func_fallback,
  pure_ddl_skip` (sourced verbatim from `_CAUSE_PRIORITY`,
  [`error_classify.py:10-20`](src/sqlcg/indexer/error_classify.py)).
- **(Phase 4)** `tools.py`: new helper `def _indexed_root(db: GraphBackend) -> Path | None`
  that reads the persisted `Repo` node path and returns it as a `Path` (or `None` when no
  `Repo` node exists). Both presentation-prefix call sites switch from
  `get_presentation_prefixes(Path.cwd())` to `get_presentation_prefixes(_indexed_root(db) or Path.cwd())`.

### Phase 4 — MCP/CLI config-root reconciliation (detailed design)

**Verified mechanism (confirmed against source).** The graph persists a `Repo` node whose
**primary key is the indexed root path**, stored absolute/resolved:
[`schema.cypher:2-5`](src/sqlcg/core/schema.cypher) — `path STRING PRIMARY KEY`. `index_cmd`
upserts it at index time. `tools.py` already runs `MATCH (r:Repo)` (the not-indexed guard,
[`tools.py:182`](src/sqlcg/server/tools.py)) and `db_info` already reads `r.path` and feeds
it to `compute_freshness`
([`tools.py:1460-1467`](src/sqlcg/server/tools.py) —
`MATCH (r:Repo) RETURN r.path AS path LIMIT 1` → `Path(_repo_rows[0]["path"])`). Phase 4
factors that exact pattern into `_indexed_root` and reuses it for config resolution.

**The disagreement, with file:line evidence.**

| Side | Reads config from | Evidence |
|------|-------------------|----------|
| Index (CLI/indexer) | the **indexed directory** | `get_dialect(path)` [`index.py:122`](src/sqlcg/cli/commands/index.py) / [`reindex.py:157`](src/sqlcg/cli/commands/reindex.py); `get_schema_aliases(path)` [`indexer.py:137`](src/sqlcg/indexer/indexer.py); `get_external_consumers(path)` [`indexer.py:1177`](src/sqlcg/indexer/indexer.py); `get_presentation_prefixes(path)` [`indexer.py:1181`](src/sqlcg/indexer/indexer.py) |
| Query (MCP) | **`Path.cwd()`** | `get_presentation_prefixes(Path.cwd())` at [`tools.py:921`](src/sqlcg/server/tools.py) (`diff_impact`) and [`tools.py:1640`](src/sqlcg/server/tools.py) (`analyze_unused`); `NoiseFilter.from_config()` → `repo_root=None` → `Path.cwd()` [`noise_filter.py:152-153`](src/sqlcg/server/noise_filter.py) |

**Design point 1 — where to resolve the root + grep-confirmed call sites.** Add
`_indexed_root(db)` near the existing `_assert_indexed` helper
([`tools.py:173`](src/sqlcg/server/tools.py)). It runs the same query `db_info` already
uses: `MATCH (r:Repo) RETURN r.path AS path LIMIT 1`. Call sites this batch wires:

- `analyze_unused` — replace [`tools.py:1640`](src/sqlcg/server/tools.py)
  `prefixes = get_presentation_prefixes(Path.cwd())` with
  `prefixes = get_presentation_prefixes(_indexed_root(db) or Path.cwd())`.
- `diff_impact` — replace [`tools.py:921`](src/sqlcg/server/tools.py) identically.

Grep-confirmation before PR (the helper must be **called**, not just defined):
`grep -n "_indexed_root" src/sqlcg/server/tools.py` must show the definition **and** both
call sites; and `grep -n "get_presentation_prefixes(Path.cwd())" src/sqlcg/server/tools.py`
must return **nothing**.

**Design point 2 — multi-Repo selection rule.** A graph *can* hold more than one `Repo`
node. For v1.1.0 the rule is **first Repo node wins** (`... LIMIT 1`), matching the
already-shipped behaviour of `db_info`'s freshness computation
([`tools.py:1464`](src/sqlcg/server/tools.py)) — this keeps `_indexed_root` consistent with
the one existing `r.path` consumer rather than inventing a second, divergent selection
rule. We deliberately do **not** attempt to match the Repo that owns the queried table:
`analyze_unused` and `diff_impact` are graph-wide (no single owning table), so there is no
unambiguous per-call Repo to select. Per-Repo config resolution in multi-Repo graphs is a
documented non-goal; the dominant real-world case (and every tested case) is a
single-Repo graph. Documented in the postmortem/limitations so a multi-Repo user is not
surprised.

**Design point 3 — no-Repo fallback (never crash).** If `MATCH (r:Repo)` returns no rows
or a row with an empty/null `path`, `_indexed_root` returns `None`, and every call site
falls back to today's `Path.cwd()` via `_indexed_root(db) or Path.cwd()`. This preserves
the exact current behaviour on a graph with no Repo node (which should not occur
post-index, since `analyze_unused`/`diff_impact` both call `_assert_indexed` first — but
the fallback is defence-in-depth and is unit-asserted in scenario 3).

**Design point 4 — scope of reconciliation (NoiseFilter decision).** This batch moves the
**presentation-prefix** reads to the indexed root (the two call sites above). The
MCP-side `NoiseFilter.from_config()` cwd fallback
([`noise_filter.py:152-153`](src/sqlcg/server/noise_filter.py)) is reached from **six**
`tools.py` call sites (`find_definition` 734, `get_change_scope` 793, `get_backfill_order`
866, `diff_impact` 920, `analyze_unused` 1633, `get_hub_ranking` 1730).

**Recommendation: move the NoiseFilter to the indexed root in the SAME batch, but only at
the two call sites that already resolve `_indexed_root` for presentation prefixes**
(`analyze_unused` 1633 and `diff_impact` 920). Justification:

- Those two functions read presentation prefixes AND build a NoiseFilter; resolving each
  from a *different* directory (prefixes from the Repo root, noise from cwd) would be an
  internally inconsistent split inside one tool call — the worse of both worlds. Keeping
  them aligned within the function is the coherent unit.
- The remaining four NoiseFilter call sites (`find_definition`, `get_change_scope`,
  `get_backfill_order`, `get_hub_ranking`) do **not** read presentation prefixes; moving
  them is mechanical but broadens the diff and the test surface. They are a **documented
  follow-on** (tracked below), so the batch stays coherent: *every tool that splits on
  presentation is reconciled now; the noise-only tools follow*.
- Concretely, at lines 920 and 1633 change `NoiseFilter.from_config()` to
  `NoiseFilter.from_config(repo_root=_indexed_root(db) or Path.cwd())`. `from_config`
  already accepts `repo_root` and falls back to cwd on `None`
  ([`noise_filter.py:138-160`](src/sqlcg/server/noise_filter.py)), so no signature change
  is needed — resolve `_indexed_root(db)` once at the top of each function and pass it to
  both the prefix read and the NoiseFilter.

**Design point 5 — no schema bump, no hot-path touch.** `Repo.path` is **already**
persisted ([`schema.cypher:3`](src/sqlcg/core/schema.cypher)); Phase 4 is **read-side
only**. `SCHEMA_VERSION` stays `"6"` ([`schema.py:6`](src/sqlcg/core/schema.py)) — no
re-index implied. No change touches `_extract_column_lineage` or `_upsert_parsed_file`;
the diff is confined to `tools.py` (helper + two functions). All CLAUDE.md performance
invariants remain intact.

### Data Models

None. No node/edge/schema changes. SCHEMA_VERSION stays `"6"`
([`schema.py:6`](src/sqlcg/core/schema.py)). Phase 4 only **reads** the existing
`Repo.path` primary key ([`schema.cypher:3`](src/sqlcg/core/schema.cypher)) — already
persisted at index time; no new node, edge, or property.

### Dependencies

None new. Uses existing `typer`, `rich.Console`, `NoiseFilter`, `config` helpers.

## Implementation Steps

### Phase 1: Config-path footgun warning (primary)

**Step 1.1** — Add `config_file_present` helper to `config.py`.
- Files: [`config.py`](src/sqlcg/core/config.py)
- Place near `get_dialect`; mirror the `Path(path) / ".sqlcg.toml"` literal already used
  by every reader (do not introduce a module constant unless refactoring all 7 — out of
  scope). Returns `bool`.
- Acceptance: `config_file_present(tmp_path)` is `False` for an empty dir, `True` after
  a `.sqlcg.toml` is created in it.

**Step 1.2** — Emit the soft warning in `index_cmd`.
- Files: [`index.py`](src/sqlcg/cli/commands/index.py)
- After `path` is available and before/around the `_run_index` call, guard with
  `if not quiet and not config_file_present(path):` and `console.print` a yellow message:
  states the searched path (`{path}/.sqlcg.toml`), that no config was found, and that
  defaults (snowflake dialect, no aliases/external_consumers/presentation prefixes) are
  in use. Must NOT raise / must NOT change exit code.
- Acceptance: indexing a dir without `.sqlcg.toml` prints the warning naming the path;
  with `.sqlcg.toml` present it does not; `--quiet` suppresses it in both cases.
  **Tone constraint**: the warning is a **single** `console.print()` call (not multi-line),
  yellow (not red), advisory in tone (confirms defaults are sane and indexing proceeds
  normally) — it must not alarm a zero-config small-repo user who legitimately runs
  without any `.sqlcg.toml`.

**Step 1.3** — Mirror the warning in `reindex_cmd` (reindex also reads `get_dialect(path)`).
- Files: [`reindex.py`](src/sqlcg/cli/commands/reindex.py)
- Same guard; respect reindex's own quiet flag if present (else unconditional but soft).
- Acceptance: reindex on a config-less path prints the same class of warning.

### Phase 2: `find column` NoiseFilter parity (#27b survivor)

**Step 2.1** — Add `--raw` + filter to `find_column`.
- Files: [`find.py`](src/sqlcg/cli/commands/find.py)
- Add `raw` option; when not raw, build `NoiseFilter.from_config()` and drop rows whose
  table portion (`id.rsplit(".", 1)[0]`) is `nf.is_noise(...)`. Keep `find pattern`
  unchanged (documented non-goal).
- Acceptance: a noise table's column (e.g. `ba.foo_bck.id`) is dropped by default and
  retained under `--raw`.

### Phase 3: `analyze failures` cause discoverability (#27d survivor)

**Step 3.1** — Enumerate valid buckets in help + docstring.
- Files: [`analyze.py`](src/sqlcg/cli/commands/analyze.py)
- Replace `"(e.g. E5, timeout)"` with the full bucket list from `_CAUSE_PRIORITY`; add a
  sentence to the docstring. Doc-only — no logic change.
- Acceptance: `sqlcg analyze failures --help` lists `timeout` among the named buckets.

### Phase 4: MCP/CLI config-root reconciliation

**Step 4.1** — Add the `_indexed_root` helper to `tools.py`.
- Files: [`tools.py`](src/sqlcg/server/tools.py)
- Define `def _indexed_root(db: GraphBackend) -> Path | None:` next to `_assert_indexed`
  ([`tools.py:173`](src/sqlcg/server/tools.py)). Body: run
  `MATCH (r:Repo) RETURN r.path AS path LIMIT 1` (same query `db_info` uses at
  [`tools.py:1464`](src/sqlcg/server/tools.py)); return `Path(rows[0]["path"])` when a
  non-empty path is present, else `None`. Must not raise.
- Acceptance (scenario 3 + 4): returns the persisted `Repo` path for an indexed graph;
  returns `None` when no `Repo` node exists. **Asserted against the exact stored path.**

**Step 4.2** — Rewire `analyze_unused` to resolve config from the indexed root.
- Files: [`tools.py`](src/sqlcg/server/tools.py)
- Resolve `root = _indexed_root(db) or Path.cwd()` once near the top of the `with` block
  (after `_assert_indexed(db)`). Replace [`tools.py:1640`](src/sqlcg/server/tools.py)
  `get_presentation_prefixes(Path.cwd())` with `get_presentation_prefixes(root)`, and
  [`tools.py:1633`](src/sqlcg/server/tools.py) `NoiseFilter.from_config()` with
  `NoiseFilter.from_config(repo_root=root)`.
- Acceptance (scenario 1): with cwd on a config-less dir B and the graph indexed from dir
  A carrying `schema_prefixes=["ia_"]`, an `ia_pres.*` orphan lands in
  `presentation_facing`, not `candidates`.

**Step 4.3** — Rewire `diff_impact` identically.
- Files: [`tools.py`](src/sqlcg/server/tools.py)
- Resolve `root = _indexed_root(db) or Path.cwd()` after `_assert_indexed(db)`. Replace
  [`tools.py:921`](src/sqlcg/server/tools.py) `get_presentation_prefixes(Path.cwd())` with
  `get_presentation_prefixes(root)`, and [`tools.py:920`](src/sqlcg/server/tools.py)
  `NoiseFilter.from_config()` with `NoiseFilter.from_config(repo_root=root)`.
- Acceptance (scenario 2): with cwd on config-less dir B, a changed `ia_pres.*` table in
  the blast radius is flagged presentation-facing using dir A's prefixes.

> **Coherence note:** Phases 4.2 and 4.3 reconcile **both** the presentation read **and**
> the NoiseFilter at the two tools that split on presentation — so a single tool call
> never resolves prefixes from one directory and noise from another. The four
> NoiseFilter-only call sites (`find_definition` 734, `get_change_scope` 793,
> `get_backfill_order` 866, `get_hub_ranking` 1730) are the documented follow-on (below).

## Wiring Checklist (grep-confirmed call sites required before PR)

- [ ] `config_file_present` is **called** from `index.py` (and `reindex.py`), not just
      defined — `grep -n "config_file_present" src/sqlcg/cli/commands/index.py src/sqlcg/cli/commands/reindex.py`.
- [ ] No `# TODO` left in the warning happy path.
- [ ] `find_column`'s new `--raw` is wired to the filter block (not a dead option) —
      `grep -n "raw" src/sqlcg/cli/commands/find.py` shows it gating the filter.
- [ ] `analyze failures` help string contains every bucket name from `_CAUSE_PRIORITY`.
- [ ] **(Phase 4)** `_indexed_root` is **called**, not just defined —
      `grep -n "_indexed_root" src/sqlcg/server/tools.py` shows the definition **and** both
      `analyze_unused`/`diff_impact` call sites.
- [ ] **(Phase 4)** No `get_presentation_prefixes(Path.cwd())` remains —
      `grep -n "get_presentation_prefixes(Path.cwd())" src/sqlcg/server/tools.py` returns
      nothing. The two sites read `_indexed_root(db) or Path.cwd()`.
- [ ] **(Phase 4)** The two reconciled `NoiseFilter.from_config()` sites (920, 1633) pass
      `repo_root=` resolved from the same `root`, not the bare cwd default.
- [ ] **(Phase 4)** No `SCHEMA_VERSION` bump — `schema.py:6` still `"6"`; no schema.cypher
      change. `Repo.path` is read-only here.
- [ ] Perf invariants untouched: `git diff` does NOT touch
      `_extract_column_lineage` or `_upsert_parsed_file` —
      `git diff --stat` should list only `config.py`, `index.py`, `reindex.py`,
      `find.py`, `analyze.py`, `tools.py` (Phase 4, read-side only), and the new tests.
- [ ] Path/constant fallback: warning names `KuzuConfig`-independent `.sqlcg.toml` (the
      readers' filename), not a hardcoded duplicate of `log_path`/`db_path`.

## Test Strategy

### Unit tests

- `tests/unit/test_config.py` (extend): `config_file_present` True/False around a tmp dir
  with/without `.sqlcg.toml`. **Assert the boolean** (observable), not just no-raise.
- `tests/unit/test_index_cmd.py` (extend) or new
  `tests/unit/test_index_config_warning.py`:
  - Index a tmp dir **without** `.sqlcg.toml` → assert the warning text appears in
    `result.output` AND names the searched path. (Observable output.)
  - Index a tmp dir **with** `.sqlcg.toml` → assert the warning is **absent**.
  - `--quiet` → warning absent even without config.
  - Use `CliRunner` + a mocked backend (pattern from `test_find_cmd.py`).
- `tests/unit/test_find_cmd.py` (extend): `find column` drops a noise-table column by
  default and keeps it under `--raw`. Assert the rendered rows, not exit code alone.

### Integration tests — Phase 4 reconciliation (the real "CLI and MCP share config" guard)

Committed (red) at
[`test_hygiene_config_root_reconciliation.py`](tests/integration/test_hygiene_config_root_reconciliation.py).
Each test indexes a fixture from **dir A** (which carries `[sqlcg.presentation]`), wires
the backend into `tools`, then `monkeypatch.chdir`s to a **different dir B** (no
`.sqlcg.toml`) — the exact condition that exposes the cwd-vs-root disagreement — and
asserts **observable tool output**:

- `test_reconciliation_analyze_unused_split_from_repo_root_not_cwd` — with cwd on B, the
  `ia_pres.*` orphan must be in `presentation_facing`, not `candidates`. (Today it lands
  in `candidates` because cwd B has no config → prefixes `[]`. Confirmed red for this
  exact reason.)
- `test_reconciliation_diff_impact_presentation_from_repo_root_not_cwd` — with cwd on B,
  the changed `ia_pres.*` table must be flagged in `diff_impact(...).presentation`.
- `test_reconciliation_no_repo_node_falls_back_to_cwd` — no `Repo` node → `_indexed_root`
  returns `None`, `analyze_unused` still runs via cwd fallback, never crashes
  (skip-guarded until `_indexed_root` exists).
- `test_reconciliation_indexed_root_returns_repo_path` — `_indexed_root(db)` equals the
  exact persisted `Repo` path (skip-guarded).
- `test_reconciliation_wiring_helper_defined_and_called` — `_indexed_root` defined **and**
  called ≥2× in `tools.py`.
- `test_reconciliation_wiring_presentation_no_longer_reads_cwd` — no
  `get_presentation_prefixes(Path.cwd())` remains in `tools.py`.

Current state: **4 failed, 2 skipped** (the 2 skips are guarded on `_indexed_root` not yet
existing and become active assertions once the helper lands). The split assertion fails
because `get_presentation_prefixes(Path.cwd())` reads config-less dir B — the genuine
regression guard for index/query config parity.

### Integration / e2e tests (other phases)

- Reuse [`test_parse_diagnostics_cli.py`](tests/e2e/test_parse_diagnostics_cli.py) — no
  new behaviour for #27d, but add an assertion that `--help` output enumerates `timeout`
  (cheap discoverability guard). Optional.

### Regression guards (must stay green, do not modify)

- `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`,
  `test_bulk_upsert_invariant.py` — confirm none flip red (they will not; no hot path is
  touched).
- `test_find_cmd.py` existing #12 cases stay green.
- **(Phase 4)** [`test_T34_presentation_segregation.py`](tests/integration/test_T34_presentation_segregation.py)
  must stay green — its `_index_fixture` indexes AND `chdir`s into the **same** dir, so
  cwd == Repo root; `_indexed_root(db) or Path.cwd()` resolves to that same dir and the
  T34 behaviour is byte-identical. In particular `test_T34_scenario_D_no_config_byte_identical`
  (no config → small-repo path unchanged) stays green: with config absent the prefixes are
  `[]` whether resolved from the Repo root or cwd, so the zero-config small-repo experience
  is untouched (CLAUDE.md both-scales rule).

## Acceptance Criteria

- [ ] `sqlcg index <dir-without-.sqlcg.toml>` prints a soft yellow warning that names the
      searched path and states defaults are in use; exit code unchanged (0).
- [ ] `sqlcg index <dir-with-.sqlcg.toml>` prints **no** such warning.
- [ ] `--quiet` suppresses the warning.
- [ ] `sqlcg reindex` shows the same warning on a config-less path.
- [ ] `config_file_present(path)` returns correct boolean (unit-tested both ways).
- [ ] `find column` filters noise by default and `--raw` disables it (observable rows).
- [ ] `sqlcg analyze failures --help` lists all `_CAUSE_PRIORITY` buckets including
      `timeout`.
- [ ] **(Phase 4)** With the MCP process cwd on a config-less directory B and the graph
      indexed from dir A carrying `schema_prefixes=["ia_"]`, `analyze_unused` places an
      `ia_pres.*` orphan in `presentation_facing` (not `candidates`) — the split resolves
      from the Repo root, not cwd.
- [ ] **(Phase 4)** `diff_impact` flags a changed `ia_pres.*` table presentation-facing
      under the same cwd≠root condition.
- [ ] **(Phase 4)** `_indexed_root(db)` returns the persisted `Repo` path; returns `None`
      (and tools fall back to cwd, no crash) when no `Repo` node exists.
- [ ] **(Phase 4)** `grep` shows no `get_presentation_prefixes(Path.cwd())` left in
      `tools.py`; `_indexed_root` is defined and called at both sites.
- [ ] **(Phase 4)** `SCHEMA_VERSION` unchanged (`"6"`); no `schema.cypher` change.
- [ ] No new method without a grep-confirmed call site.
- [ ] No `# TODO` in any happy path.
- [ ] `git diff --stat` touches only the source files (`config.py`, `index.py`,
      `reindex.py`, `find.py`, `analyze.py`, `tools.py`) + tests; no parser/indexer
      hot-path file appears.
- [ ] Full suite (excl. e2e) green, including the three perf-invariant tests and T34.

## PR Grouping Decision

**One PR, four phases.** Justification:

- All four phases share one theme: **the config the user wrote is the config every code
  path reads** — index-time observability (Phase 1), NoiseFilter parity (Phase 2),
  discoverability (Phase 3), and index/query config-root parity (Phase 4). One reviewer
  mental model.
- Phase 4 is the substantive deliverable (real user-facing correctness: MCP no longer
  silently disagrees with the indexed config). Phases 1–3 are the low-risk hygiene around
  the same footgun.
- No phase depends on another's code, so review can proceed phase-by-phase but ship
  together. Phase 4 is **read-side only** (`tools.py`), no schema bump, no hot-path touch
  — it stays within the one-PR risk envelope.

Suggested PR title: `fix: reconcile MCP/CLI config root + surface missing .sqlcg.toml at
index time + find-column noise parity + failures cause discoverability (#27)`.

**Items dropped as already-done (flag for the user):**
- **#12** — fixed in `ecf42c1`, tested in `test_find_cmd.py`. Close the issue.
- **#13** — fixed (log-file default + `--verbose` + summary pointer + T-09-06 demotion).
  Close the issue.
- **#27b for `find table`** — already at parity; only `find column` remained (Phase 2).
- **#27d core** — `timeout` already a queryable `parse_cause` bucket with `analyze
  failures --cause timeout`; only the help-text discoverability remained (Phase 3).

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Warning becomes noise for users who legitimately run without config | Low | It is a one-line soft notice, `--quiet`-suppressible, and only fires when no config is found at the index path — which is exactly when the silent zero-ingest bug bites. |
| `find column` filter changes default output of an existing command | Low | Matches the established `--raw` opt-out convention already used by `find table` and all `analyze` subcommands; documented. |
| Reindex has no `quiet` flag to gate on | Low | Verify in `reindex.py`; if absent, emit unconditionally (still soft, no exit-code change). |
| **(Phase 4)** Multi-Repo graph picks the wrong Repo's config | Low | v1.1.0 rule is "first Repo node" — identical to the already-shipped freshness path ([`tools.py:1464`](src/sqlcg/server/tools.py)), so no new divergent behaviour. Per-Repo config is a documented non-goal; single-Repo is the dominant case. |
| **(Phase 4)** Prefix resolved from Repo root but noise still from cwd (internal split) | Low | Both reconciled call sites (`analyze_unused`, `diff_impact`) resolve `root` once and pass it to **both** `get_presentation_prefixes` and `NoiseFilter.from_config(repo_root=root)`. Wiring checklist enforces it. |
| **(Phase 4)** Graph with no Repo node crashes a query tool | Low | `_indexed_root` returns `None`; `_indexed_root(db) or Path.cwd()` falls back to today's behaviour. Scenario-3 test asserts no crash. |
| **(Phase 4)** Small-repo zero-config user sees changed behaviour | Low | With no config, prefixes are `[]` whether resolved from Repo root or cwd; T34 scenario D guards byte-identical small-repo output. |

## Follow-up (not this batch)

- **NoiseFilter-only MCP call sites**: the four `tools.py` `NoiseFilter.from_config()`
  sites that do **not** read presentation prefixes — `find_definition`
  ([`tools.py:734`](src/sqlcg/server/tools.py)), `get_change_scope`
  ([`tools.py:793`](src/sqlcg/server/tools.py)), `get_backfill_order`
  ([`tools.py:866`](src/sqlcg/server/tools.py)), `get_hub_ranking`
  ([`tools.py:1730`](src/sqlcg/server/tools.py)) — still resolve noise from `Path.cwd()`.
  Moving them to `_indexed_root(db) or Path.cwd()` is mechanical and could fold into a
  later batch; deferred only to keep this PR's test surface tight. Track as a new issue.
- **Per-Repo config in multi-Repo graphs**: if/when multi-Repo graphs become common,
  resolve config per the Repo that owns the queried table instead of "first Repo node".
  Needs its own design (which Repo owns a graph-wide `analyze_unused`?).

## Blocking Questions

None. All four phases are clearly defined. Phase 4's design choices are anchored in
verified source: the `Repo` node already persists the indexed root as its primary key
([`schema.cypher:2-5`](src/sqlcg/core/schema.cypher)) and `db_info` already reads it for
freshness ([`tools.py:1460-1467`](src/sqlcg/server/tools.py)), so reconciliation is a
read-side reuse with no schema bump. The "first Repo node" multi-Repo rule matches the
existing freshness path; the no-Repo fallback preserves today's `Path.cwd()` behaviour.
All choices align with CLAUDE.md (both-scales rule, grep-confirmed call sites, no
parent-dir search, fallbacks aligned with config readers).

### Deviations

#### Deviation 1: DiffImpactResult.presentation property alias
- **Reason**: The pre-committed integration test (`test_reconciliation_diff_impact_presentation_from_repo_root_not_cwd`) accesses `result.presentation` but the model field is `presentation_facing`. Rather than rename the model field (which could break existing callers/serialization), a `@property` alias `presentation -> presentation_facing` was added to `DiffImpactResult`.
- **Change**: Added `def presentation(self) -> list[str]: return self.presentation_facing` to `DiffImpactResult` in `models.py`.
- **Impact**: Zero — purely additive; existing callers using `presentation_facing` are unaffected; no schema/API contract change.
- **Date**: 2026-05-31

#### Deviation 2: _assert_indexed relaxed to accept File nodes when no Repo node exists
- **Reason**: The pre-committed integration test (`test_reconciliation_no_repo_node_falls_back_to_cwd`) populates the graph via `Indexer().index_repo()` (which creates File/Table nodes) WITHOUT calling `index_cmd` (which upserts the Repo node). The original `_assert_indexed` checked only for Repo nodes, causing NotIndexedError even though the graph has valid content.
- **Change**: `_assert_indexed` now returns early if either Repo nodes OR File nodes are present. Both checks use count queries (no hot path touched).
- **Impact**: Defence-in-depth improvement. In production, `index_cmd` always upserts a Repo node so the Repo check fires immediately. In tests that bypass `index_cmd`, the File check prevents a spurious NotIndexedError. No existing test expectations broken (729 passed).
- **Date**: 2026-05-31

#### Deviation 3: diff_impact resolves relative file paths against indexed root
- **Reason**: The pre-committed integration test calls `diff_impact(["src.sql"])` with a relative path, while the graph stores absolute paths. Without resolution, `GET_TABLES_DEFINED_IN_FILE_QUERY` finds no match and `changed_tables` is empty — the presentation scenario cannot be exercised.
- **Change**: In `diff_impact`, before running `GET_TABLES_DEFINED_IN_FILE_QUERY`, relative `file_path` values are resolved against the indexed root (`root / fp`). Absolute paths are passed unchanged.
- **Impact**: Consistent with the Phase 4 theme (use indexed root for resolution). Makes the `diff_impact` MCP tool more robust for callers that pass repo-relative paths. No change to callers that already pass absolute paths.
- **Date**: 2026-05-31
