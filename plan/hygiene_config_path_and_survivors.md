# Feature Plan: Hygiene Batch — config-path footgun + #12/#13/#27 survivor triage

## Summary

A low-risk cleanup batch on `feat/cluster-b-provenance`. Code-vs-plan verification
against the real source (file:line evidence below) shows **three of the four nominated
items are already fixed on this branch**. Only **one genuinely new gap** remains worth
shipping — the silent config-path footgun (item 4) — plus **two small, optional polish
items** (#27d timeout discoverability, #27b `find column` noise parity). The plan ships
the footgun fix as the spine and folds the two polish items into the same PR because they
share the same theme (index-time observability / NoiseFilter parity) and are each <15 LOC.

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

**Chosen fix (minimal, correct): option (a) — emit a visible warning at index time when
no `.sqlcg.toml` is found at the index path.** Rationale:

- Cheapest and dovetails with the #13 observability theme.
- Does NOT silently search parent dirs (per the hard constraint — that hides the problem).
- Zero new friction for the 20-ETL user who keeps config beside their SQL or has no
  config at all: the warning is informational, fires only when a config genuinely is not
  found at the index path, and is suppressed by `--quiet`. The "no config" case is
  legitimate (defaults are sane), so the warning must be **soft** — it states the
  searched path and that defaults are in use, it does not fail the index.

**Explicitly NOT doing option (b)** (reconcile index-path-vs-cwd by threading the index
path into query-time readers): the query side has no index-path context at call time and
would require persisting the indexed root in the graph and plumbing it through tools — a
larger, riskier change than this hygiene batch warrants. Documented as a follow-up below.

## Scope

### In Scope

- A soft, visible warning at `sqlcg index` (and `sqlcg reindex`) when no `.sqlcg.toml` is
  found at the index path — naming the exact path searched, suppressed by `--quiet`.
- A reusable `config_file_present(path) -> bool` helper in `config.py` so the check uses
  the same `.sqlcg.toml` location as the readers (no hardcoded filename drift).
- NoiseFilter parity for `find column` (add `--raw` + filter, matching `find table`).
- Enumerate valid `--cause` buckets in `analyze failures` help text/docstring (doc-only).

### Non-Goals

- Reconciling index-path vs `Path.cwd()` at query time (option b) — follow-up.
- Searching parent directories for `.sqlcg.toml` (forbidden — hides the footgun).
- Any SCHEMA_VERSION bump (none of these items change stored form).
- `find pattern` noise filtering (no qualified-name key to filter on).
- Re-opening #12 / #13 — both verified done; tests already guard them.
- Any change to `_extract_column_lineage` column loop or `_upsert_parsed_file` edge-row
  loop. **Confirmed:** the warning fires in the CLI command before/around `index_repo`,
  and the `find column` filter is post-query in the CLI — no hot-path code is touched.
  All performance invariants in CLAUDE.md remain intact.

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

### Data Models

None. No node/edge/schema changes. SCHEMA_VERSION stays `"6"`.

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

## Wiring Checklist (grep-confirmed call sites required before PR)

- [ ] `config_file_present` is **called** from `index.py` (and `reindex.py`), not just
      defined — `grep -n "config_file_present" src/sqlcg/cli/commands/index.py src/sqlcg/cli/commands/reindex.py`.
- [ ] No `# TODO` left in the warning happy path.
- [ ] `find_column`'s new `--raw` is wired to the filter block (not a dead option) —
      `grep -n "raw" src/sqlcg/cli/commands/find.py` shows it gating the filter.
- [ ] `analyze failures` help string contains every bucket name from `_CAUSE_PRIORITY`.
- [ ] Perf invariants untouched: `git diff` does NOT touch
      `_extract_column_lineage` or `_upsert_parsed_file` —
      `git diff --stat` should list only `config.py`, `index.py`, `reindex.py`,
      `find.py`, `analyze.py`, and the new tests.
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

### Integration / e2e tests

- Reuse [`test_parse_diagnostics_cli.py`](tests/e2e/test_parse_diagnostics_cli.py) — no
  new behaviour for #27d, but add an assertion that `--help` output enumerates `timeout`
  (cheap discoverability guard). Optional.

### Regression guards (must stay green, do not modify)

- `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`,
  `test_bulk_upsert_invariant.py` — confirm none flip red (they will not; no hot path is
  touched).
- `test_find_cmd.py` existing #12 cases stay green.

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
- [ ] No new method without a grep-confirmed call site.
- [ ] No `# TODO` in any happy path.
- [ ] `git diff --stat` touches only the five source files + tests; no parser/indexer
      hot-path file appears.
- [ ] Full suite (excl. e2e) green, including the three perf-invariant tests.

## PR Grouping Decision

**One PR.** Justification:

- All three kept items are low-risk, independent, and small (<15 LOC each of behaviour).
- They share one theme: **index-time observability + NoiseFilter parity** — the same
  reviewer mental model.
- The footgun warning (Phase 1) is the only item with real user impact; Phases 2–3 are
  trivial polish that would be wasteful to gate behind separate PRs.
- No item depends on another, so review can proceed phase-by-phase but ship together.

Suggested PR title: `fix: surface missing .sqlcg.toml at index time + find-column noise
parity + failures cause discoverability (#27)`.

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
| Someone reads option (b) as in-scope and threads index path through query tools | Low | Explicit non-goal; recorded as follow-up below. |
| Reindex has no `quiet` flag to gate on | Low | Verify in `reindex.py`; if absent, emit unconditionally (still soft, no exit-code change). |

## Follow-up (not this batch)

- **Option (b) reconciliation**: persist the indexed root in the graph (e.g. on the Repo
  node, already written at [`index.py:194-201`](src/sqlcg/cli/commands/index.py)) and have
  query-time `get_presentation_prefixes` / `NoiseFilter.from_config` read config from that
  stored root instead of `Path.cwd()`. Larger change; needs its own plan and a
  decision on multi-repo graphs. Track as a new issue.

## Blocking Questions

None. All four items are clearly defined, all design choices (query-time fold for #12,
option (a) for the footgun) are already aligned with documented decisions
(ARCHITECTURE_REVIEW §2.6 C2 normalization; CLAUDE.md "no silent parent-dir search").
