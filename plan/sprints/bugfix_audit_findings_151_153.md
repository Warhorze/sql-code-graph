# Bugfix sprint — live-DWH audit findings (#151, #152, #153)

**Status: DRAFT — awaiting plan-reviewer gate.**
**Origin:** skeptical data-engineer live-DWH audit (2026-06-14), filed as #151/#152/#153.
**Sequence by risk (low→high): PR-A (#151) → PR-B (#152) → PR-C (#153).** Each is independent;
PR-C is design-heavy and may warrant an architect-review pass before implementation.

All three are *trust/usability* findings, not lineage-correctness regressions: core ETL lineage
was validated as trustworthy in the same audit. No backward-compat shims — reindex is the
migration path (CLAUDE.md).

---

## PR-A (#151) — Stamp the sqlcg version in the graph; warn when the indexing tool is older than the running tool

**Rank 1 (lowest risk). Version bump: MINOR** (new freshness surface + schema bump). Not metric-moving for lineage, but it changes `db info`/`gain` output.

### Problem
Graph freshness is keyed only to the indexed **corpus** SHA. A tool/parser upgrade that would now
extract more lineage does NOT invalidate the graph, and `db info` reports "up to date" anyway —
so PR-6/PR-2b improvements stay invisible until a manual reindex, with no signal.

### Root cause (file:line — verified)
- Freshness compares only `indexed_sha` vs git HEAD: [`core/freshness.py:68`](../../src/sqlcg/core/freshness.py) (`compute_freshness`), rendered by `describe` (`freshness.py:119-133`).
- The `SchemaVersion` table stores only `(version, indexed_sha)` — `version` is the SCHEMA_VERSION ("9"), NOT the tool version: [`core/duckdb_backend.py:98-100`](../../src/sqlcg/core/duckdb_backend.py) (DDL) and `:368` (the `INSERT OR REPLACE`). Tool version (`__version__`) is never persisted.

### Proposed fix
1. **Schema bump v9 → v10** ([`core/schema.py:6`](../../src/sqlcg/core/schema.py)): add a `sqlcg_version VARCHAR` column to `SchemaVersion` (`duckdb_backend.py:98-100`), and write `sqlcg.__version__` alongside `indexed_sha` at index time (`set_indexed_sha`/the `:368` upsert; thread `__version__` in). Add `sqlcg_version` to the read column-set (`duckdb_backend.py:260`).
2. **Freshness extension:** add `indexed_version: str | None` to the `Freshness` dataclass and a `tool_version_stale: bool` (true when `indexed_version` differs from the running `__version__`). `compute_freshness` takes the running version; `describe` adds a line: `indexed with sqlcg <old> — running <new>; reindex to pick up parser improvements`.
3. **Surface in `db info` and `gain`** — wherever `compute_freshness`/`describe` is rendered (`cli/commands/db.py`, `cli/commands/index.py`, `cli/coverage.py`). A mismatch should be visible but NON-fatal.

### Acceptance criteria
- After indexing with vX then "running" vY (Y≠X), `sqlcg db info` shows the tool-version-stale warning even when the corpus SHA is unchanged ("up to date").
- Same tool version → NO warning (no false positive).
- Schema bump v10 applied; a fresh index populates `sqlcg_version`; the four perf-invariant suites pass UNMODIFIED; `pyright`/`ruff` clean.
- Unit test: `compute_freshness` sets `tool_version_stale` correctly for match/mismatch/null.

### Risk
- Schema migration: existing v9 graphs must reindex (acceptable — stated migration path). Guard: reading a v9 graph where `sqlcg_version` is absent must degrade gracefully (treat as unknown → no crash, optional "reindex recommended").

---

## PR-B (#152) — Surface dynamic-SQL "lineage-incomplete" tables so `gain` stops over-reporting

**Rank 2. Version bump: MINOR** (new metric). **Metric-moving → `gain --json` snapshot required.**

### Problem
Non-static (`||`-concatenated / bind-variable) `EXECUTE IMMEDIATE` dynamic tables produce ZERO
lineage. Because they have no edges, they are neither "bad" nor "phantom" — invisible to `gain`'s
strict/scoped/catalogued numbers, so health over-reports for that class.

### Root cause (file:line — verified)
- PR-6 recovers only STATIC-literal `EXECUTE IMMEDIATE` (sets `parsing_mode='dynamic_sql'` on the recovered query — `parsers/snowflake_parser.py`). The non-recoverable sites (`_unwrap_execute_immediate` returns nothing for concat/bind-var) emit **no node at all** → there is nothing in the graph to count.
- `coverage.py` already has the shape to extend: `zero_edge_write_queries` (`coverage.py:311`, `:668`) and `SqlQuery.parsing_mode` exists (`duckdb_backend.py:93,:257`).

### Proposed fix
1. **Parser records SKIPPED dynamic-SQL sites.** Where `_unwrap_execute_immediate` / the scripting path encounters an `EXECUTE IMMEDIATE` it cannot statically recover, emit a lightweight diagnostic (e.g. a per-file `dynamic_sql_skipped` count in `ParsedFile.errors`/a counter, mirroring existing `dynamic_sql_parse_error` handling) AND, where a target table name is statically knowable but its body isn't, tag that table/file as `lineage-incomplete`. Do not guess lineage — only record that a recoverable-shaped site was skipped.
2. **Coverage surfaces it.** Add a `lineage_incomplete_dynamic` count to `CoverageStats` (`coverage.py:412+`) and render it in `gain` §G + `db info`: "N tables defined via dynamic SQL we could not statically trace (lineage-incomplete)".
3. `gain --json` includes the new field.

### Acceptance criteria
- On the live DWH, `gain` reports a non-zero `lineage_incomplete_dynamic` count that matches the corpus's known non-static `EXECUTE IMMEDIATE` table count (cross-check vs the ~20 EXECUTE IMMEDIATE files, minus the static ones PR-6 recovers).
- A fixture with a concatenated `EXECUTE IMMEDIATE 'CREATE TABLE … AS ' || :v` produces a `lineage_incomplete_dynamic` increment and 0 spurious edges.
- Static-literal dynamic SQL (PR-6's case) does NOT count as incomplete (it's fully traced).
- Four perf-invariant suites UNMODIFIED; `gain --json` snapshot committed to `plan/metrics/`; `pyright`/`ruff` clean.

### Risk
- The "skipped" detection must not double-count or misfire on non-CREATE EXECUTE IMMEDIATE (SHOW/CALL) — scope the count to CREATE-shaped sites only, reusing PR-6's `_CREATE_DDL_PREFIX_RE` classification.
- Live-DWH acceptance needed (parser change) — index-heavy, serialize on the one slot.

---

## PR-C (#153) — Quoted/spaced identifiers: make them round-trip and CLI-addressable

**Rank 3 (highest risk — core id model). Version bump: MINOR (reindex migration).**
**Plan-reviewer/architect note:** this touches the identity model everything keys on; recommend an
architect-review pass + a spike before committing to the exact id form.

### Problem
Quoted identifiers with spaces/special chars (IA_SEMANTIC/IA_TABLEAU, ~12k columns / ~249 tables)
are stored as bare lowercased tokens with quotes stripped → `ia_semantic.<t>.omzet excl` — ambiguous,
garbled in output, and not re-typeable into the CLI. The IA consumer-view layer is effectively
unqueryable from the CLI.

### Root cause (file:line — verified)
- Column normalization strips surrounding quotes + lowercases: [`parsers/base.py:119-131`](../../src/sqlcg/parsers/base.py).
- Table ref normalization lowercases catalog/db/name: [`parsers/base.py:75-79`](../../src/sqlcg/parsers/base.py); `full_id` builds `f"{db.lower()}.{name}"` (`base.py:515-516`).
- CLI consumes the bare `qualified`/`table_qualified` string (e.g. [`cli/commands/analyze.py:64,:102`](../../src/sqlcg/cli/commands/analyze.py)); there is no quote-aware arg parsing.

### Proposed fix (design options — reviewer to pick)
- **Option A (recommended, minimal):** CONDITIONAL quote preservation — strip+lowercase only when the
  unquoted name is a *safe bare identifier* (`^[a-z_][a-z0-9_]*$` after lowercasing); otherwise KEEP
  the identifier quoted in the id (`"omzet excl"`). This preserves the common-case collapse
  (`"ma_aantal"`→`ma_aantal`) while making spaced/special ids unambiguous and re-typeable. The CLI
  must accept the quoted form as input (quote-aware split of `schema.table."col with space"`).
- **Option B:** store a separate `display_name`/quoted form alongside the normalized id (two fields) —
  more invasive, touches the node schema and every consumer.
- **Out of scope:** changing the case-folding policy for safe identifiers (keep `.lower()` there).

### Acceptance criteria
- A spaced quoted column (e.g. `IA_SEMANTIC."omzet excl"`, ref `ddl/changelogs/IA-SEMANTIC/ARTIKEL.sql:18-19`) is stored unambiguously and `analyze upstream/downstream` accepts the user-typed quoted form and returns the right lineage.
- Safe identifiers are UNCHANGED (`"ma_aantal"` and `ma_aantal` still collapse to one node — regression test).
- Round-trip test: parse → store → render → re-feed the rendered id to the CLI → same node.
- Four perf-invariant suites UNMODIFIED; live-DWH spot-check that IA_SEMANTIC/IA_TABLEAU columns are now addressable; `pyright`/`ruff` clean.

### Risk
- HIGH: the id model is keyed on by edges, catalog, noise-match, golden tests — any change risks
  splitting/merging nodes wrongly. Mitigate: conditional (only non-safe names change), a broad
  regression suite, and a live A/B (node/edge counts should only shift for the affected quoted set).
- Reindex required; existing graphs migrate by reindex.

---

## Suggested overall sequence & gating
| Order | PR | Bump | Index-heavy? | Notes |
|-------|----|------|--------------|-------|
| 1 | PR-A #151 | minor | no | schema bump v9→v10; cheap, do first |
| 2 | PR-B #152 | minor | yes (`gain --json`) | parser + coverage; serialize on index slot |
| 3 | PR-C #153 | minor | yes (live spot-check) | core id model; architect-review first; LAST |

PR-A and PR-C are independent of the running live-DWH validation; PR-B's acceptance and PR-C's
spot-check need the one index slot (serialize). The in-flight validation workflow may refine PR-B's
expected count and could surface additional items to fold in before dispatch.
