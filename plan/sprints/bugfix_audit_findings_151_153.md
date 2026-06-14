# Bugfix sprint — live-DWH audit findings (#151, #152, #153)

**Status: REVIEWED-PENDING** (sprint-planner hardened; plan-reviewer gate runs next).
**Current version:** `1.32.1` (master). **Origin:** skeptical data-engineer live-DWH audit
(2026-06-14), filed as [#151](https://github.com/Warhorze/sql-code-graph/issues/151) /
[#152](https://github.com/Warhorze/sql-code-graph/issues/152) /
[#153](https://github.com/Warhorze/sql-code-graph/issues/153).
**Sequence by risk (low→high): PR-A (#151) → PR-B (#152) → PR-C (#153).** Each is independent;
PR-C is design-heavy — see the explicit **MAINTAINER DECISION** (Option A vs B) below.

All three are *trust/usability* findings, not lineage-correctness regressions: core ETL lineage
was validated as trustworthy in the same audit. No backward-compat shims — reindex is the
migration path ([CLAUDE.md](../../CLAUDE.md)).

> **In-flight validation note:** a live-DWH validation workflow is *actively running* against
> `/home/ignwrad/Projects/dwh` — **do not touch that repo**. That workflow may (a) refine PR-B's
> expected `lineage_incomplete_dynamic` count and (b) surface additional items to fold in before
> dispatch. PR-B's numeric acceptance threshold is therefore stated as a *shape* assertion with a
> placeholder count to be pinned from the validation output.

---

## Verification log (sprint-planner, 2026-06-14) — drift corrected vs DRAFT

The DRAFT's anchors were re-checked against the source tree. Corrections folded into the tickets:

| DRAFT claim | Reality (verified) | Action |
|---|---|---|
| `freshness.py:68` is the SHA compare; rendered by `describe` | Compare is at [`freshness.py:88`](../../src/sqlcg/core/freshness.py); the render fn is **`render_freshness_line`** (`:108-134`), **not** `describe` | Corrected in PR-A |
| `SchemaVersion` DDL `:98-100`; upsert `:368`; read col-set `:260` | **All correct.** Note: `version` is the **PRIMARY KEY** and equals `SCHEMA_VERSION` ("9") — a single-row table keyed by schema version, **not** by tool version | PR-A design adjusted (see below) |
| Tool version persisted via `set_indexed_sha`/`:368` | `indexed_sha` is written by **`set_indexed_sha` (`duckdb_backend.py:811`)**, a *separate* method from the schema-init upsert at `:368`. The `:368` path only seeds the row. | PR-A threads version through **both** `:368` (column add) and `set_indexed_sha` (value write) |
| `base.py:515-516` is `full_id` | **WRONG.** `:515-516` is a `db.name` key helper (`f"{db.lower()}.{name}"`). The real `full_id` is **`TableRef.full_id` `:82-99`** and **`ColumnRef.full_id` `:134-140`**; column quote-strip+lower is **`:119-131`** | PR-C anchors corrected |
| `coverage.py:412+` for CoverageStats field add | `CoverageStats` dataclass starts at [`coverage.py:408`](../../src/sqlcg/cli/coverage.py); recall fields incl. `zero_edge_write_queries` at `:453` | Corrected |
| `zero_edge_write_queries` shape | Computed graph-side by `_Q_ZERO_EDGE_WRITES` (`coverage.py:226-234`), a `NOT EXISTS (COLUMN_LINEAGE)` over write-kind `SqlQuery`; assembled in `collect_coverage()` (`:554-680`), read at `:668` | PR-B reuses this exact pattern |
| `_unwrap_execute_immediate` skips non-static | Confirmed: [`snowflake_parser.py:598-653`](../../src/sqlcg/parsers/snowflake_parser.py) returns **empty list** for concat/bind-var; `parsing_mode='dynamic_sql'` set **only** on recovered static SQL at `:768`; CREATE-shape gate `_CREATE_DDL_PREFIX_RE` `:88-91`, applied `:580` | PR-B root cause confirmed |
| `parsing_mode` values | Three: `"sqlglot"` (default), `"dynamic_sql"` (recovered EXECUTE IMMEDIATE), `"scripting"` (scripting-block DML). Persisted via indexer `:1581`; column `duckdb_backend.py:93,:257` | PR-B uses this |
| CLI consumes bare `qualified`/`table_qualified`; no quote-aware parsing | Confirmed. `analyze upstream` does `ref = ref.lower()` (`analyze.py:220`) then `len(ref.split("."))>=3` fallback (`:225`); id splitting via `.split(".")`/`.rsplit(".",1)` at `:414,:422,:536,:710`. MCP mirror in `server/tools.py:301` `_parse_column_ref`, `:79` `_label`. | PR-C blast radius expanded below |

---

## PR-A (#151) — Stamp the sqlcg version in the graph; warn when the indexing tool is older than the running tool

**Rank 1 (lowest risk). Version bump: MINOR → `1.33.0`** (new freshness surface + **schema bump v9→v10**).
Not lineage-metric-moving, but it changes `db info` / `gain` / `index` output.

### Problem
Freshness is keyed only to the indexed **corpus** SHA. A tool/parser upgrade that would now extract
more lineage does NOT invalidate the graph, and `db info` reports "up to date" anyway — so PR-6 /
PR-2b improvements stay invisible until a manual reindex, with no signal.

### Root cause (file:line — verified)
- Freshness compares only `indexed_sha` vs git HEAD: [`core/freshness.py:88`](../../src/sqlcg/core/freshness.py)
  (`compute_freshness`), rendered by **`render_freshness_line`** (`:108-134`). There is no tool-version field.
- `SchemaVersion` stores only `(version PRIMARY KEY, indexed_sha)`: DDL
  [`core/duckdb_backend.py:98-100`](../../src/sqlcg/core/duckdb_backend.py); seed-upsert `:368`; read col-set `:260`;
  value write in `set_indexed_sha` `:811`. **`version` is the schema version ("9"), not the tool version** —
  the tool version (`sqlcg.__version__`) is never persisted.

### Proposed fix
1. **Schema bump v9 → v10** ([`core/schema.py:6`](../../src/sqlcg/core/schema.py) `SCHEMA_VERSION = "10"`).
   Add a `sqlcg_version VARCHAR` column to `SchemaVersion` DDL (`duckdb_backend.py:98-100`). Add
   `"sqlcg_version"` to the read col-set `_NODE_COLUMNS[SCHEMA_VERSION]` (`:260`). **Do not** repurpose
   the `version` PK — it stays the schema version.
2. **Write the tool version at index time.** `set_indexed_sha` (`:811`) currently writes only
   `(SCHEMA_VERSION, sha)`. Extend it to also write `sqlcg.__version__` into `sqlcg_version`
   (import `from sqlcg import __version__`, or thread it from the caller — prefer importing in the
   backend to avoid a wide signature change; confirm no import cycle). Update the seed-upsert at `:368`
   to include the new column (NULL-preserving COALESCE like the existing `indexed_sha` clause).
3. **Read it back.** Add `get_indexed_version() -> str | None` next to `get_indexed_sha` (`:821`),
   reading `sqlcg_version` from the single `SchemaVersion` row. Grep-confirm the call site (added in step 4).
4. **Freshness extension** ([`core/freshness.py`](../../src/sqlcg/core/freshness.py)):
   - Add `indexed_version: str | None` and `tool_version_stale: bool` to the `Freshness` dataclass
     (`:15-38`).
   - `compute_freshness(root, indexed_sha, indexed_version, running_version)` — `tool_version_stale =
     (indexed_version is not None and indexed_version != running_version)`. **Null indexed_version
     (legacy graph that lacks it, or never-indexed) → `tool_version_stale = False`** (no false positive;
     we cannot claim staleness we cannot prove).
   - `render_freshness_line` appends, when stale: `; indexed with sqlcg <old>, running <new> — reindex
     to pick up parser improvements`. Non-fatal.
5. **Surface in every freshness render site.** Grep `compute_freshness`/`render_freshness_line` callers
   and pass the new args: at minimum [`cli/commands/db.py`](../../src/sqlcg/cli/commands/db.py),
   [`cli/commands/index.py`](../../src/sqlcg/cli/commands/index.py), and
   [`cli/coverage.py`](../../src/sqlcg/cli/coverage.py) (the `gain` path). The dev must grep all callers
   — the new args must reach each.

### Acceptance criteria (observable)
- [ ] Index with the running version; `compute_freshness(...)` returns `tool_version_stale == False`
      and `render_freshness_line` contains **no** reindex-hint substring.
- [ ] Construct/patch a `Freshness` with `indexed_version="1.32.1"`, `running_version="1.33.0"`:
      `tool_version_stale is True` and `render_freshness_line(...)` **contains** the substring
      `"reindex to pick up parser improvements"` — even when `stale_by_commits == 0` ("up to date").
- [ ] `indexed_version is None` → `tool_version_stale is False` (degrade gracefully; no crash).
- [ ] A fresh index writes a non-null `sqlcg_version` equal to `sqlcg.__version__`
      (integration assertion: read the `SchemaVersion` row).
- [ ] `SCHEMA_VERSION == "10"`; opening a graph still initializes cleanly.
- [ ] The four perf-invariant suites pass **UNMODIFIED**
      ([`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
      [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
      [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
      [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py)). `pyright`/`ruff` clean.

### Version bump / perf / metric
- **Bump:** `1.32.1 → 1.33.0` (minor; schema v9→v10). Update `pyproject.toml`, `src/sqlcg/__init__.py`,
  `uv lock`.
- **Perf impact:** none — one extra column + one extra read on a single-row table, off the hot path.
- **Metric snapshot:** not required (not lineage-metric-moving).
- **Index slot:** NOT needed — unit/integration tests cover it; no live reindex required.

### Risk
- Schema migration: v9 graphs must reindex (stated migration path). The null-version degrade path
  (criterion 3) ensures a stale-schema open does not crash.

---

## PR-B (#152) — Surface dynamic-SQL "lineage-incomplete" tables so `gain` stops over-reporting

**Rank 2. Version bump: MINOR → `1.34.0`** (new metric). **Metric-moving → `gain --json` snapshot required.**

### Problem
Non-static (`||`-concatenated / bind-variable) `EXECUTE IMMEDIATE` dynamic tables produce ZERO
lineage. With no edges they are neither "bad" nor "phantom" — invisible to `gain`'s strict/scoped/
catalogued numbers, so health over-reports for that table class.

### Root cause (file:line — verified)
- `_unwrap_execute_immediate` ([`snowflake_parser.py:598-653`](../../src/sqlcg/parsers/snowflake_parser.py))
  recovers only **static-literal** CREATE strings; for concat/bind-var it **returns an empty list** →
  no inner query is emitted → **no node is created at all**. There is currently *nothing in the graph to
  count*. `parsing_mode='dynamic_sql'` (`:768`) is set **only** on the recovered static case.
- CREATE-shape gate `_CREATE_DDL_PREFIX_RE` `:88-91`, applied at `:580` (rejects SHOW/CALL/etc.).
- Coverage already has the right pattern: `zero_edge_write_queries` via `_Q_ZERO_EDGE_WRITES`
  (`coverage.py:226-234`, assembled `:554-680`, read `:668`); `CoverageStats` recall block at `:449-454`.

### Design decision — emit a node for skipped CREATE-shaped dynamic SQL
Make the skipped site **visible in the graph** so the metric is derived graph-side (mirroring
`zero_edge_write_queries`), rather than carrying a parser-only counter that `gain` reads from disk:

1. **Parser emits a marker node for the skipped, CREATE-shaped site.** When `_unwrap_execute_immediate`
   rejects a non-static string **but** `_CREATE_DDL_PREFIX_RE` matched (it *was* a CREATE we could not
   statically recover), emit a `SqlQuery` node with the existing create kind, `parse_failed=False`, and a
   **new `parsing_mode` value `'dynamic_sql_unresolved'`**. Emit **no** lineage edges (we cannot know the
   body — no guessing). If the target table name is statically knowable from the `CREATE TABLE <name> AS`
   prefix, stamp `target_table`; otherwise leave it null. **Scope strictly to CREATE-shaped sites** (reuse
   `_CREATE_DDL_PREFIX_RE`) so SHOW/CALL/DML EXECUTE IMMEDIATE never counts.
   - *Why a node, not just a `ParsedFile.errors` string:* it lets `gain` and `db info` derive the count
     from one SQL query (consistent with every other coverage number) and makes the incomplete table
     addressable later. `ParsedFile.errors` (`base.py:296`, list[str]) remains a secondary diagnostic.
2. **Coverage surfaces it.** Add `lineage_incomplete_dynamic: int = 0` to `CoverageStats`
   (recall block, `coverage.py:449-454`). Compute it graph-side, mirroring `_Q_ZERO_EDGE_WRITES`:
   ```sql
   SELECT COUNT(*) AS lineage_incomplete_dynamic
   FROM "SqlQuery" q
   WHERE q.parsing_mode = 'dynamic_sql_unresolved'
     AND NOT EXISTS (SELECT 1 FROM "COLUMN_LINEAGE" cl WHERE cl.query_id = q.id)
   ```
   Wire it into `collect_coverage()` (`:554-680`) alongside the other recall reads.
3. **Render.** Add a line in `render_coverage_lines` (`coverage.py:740+`, near the `:830` zero-edge line):
   `N table(s) defined via non-static dynamic SQL — lineage-incomplete (not statically traceable)`.
   Add the same line to `db info` ([`cli/commands/db.py`](../../src/sqlcg/cli/commands/db.py)).
4. **JSON.** Add `"lineage_incomplete_dynamic"` to `coverage_to_json()` (`coverage.py:874-930`, near `:915`).

### Acceptance criteria (observable)
- [ ] Integration fixture: a concatenated `EXECUTE IMMEDIATE 'CREATE TABLE foo AS SELECT ' || :v`
      yields **exactly one** `SqlQuery` row with `parsing_mode='dynamic_sql_unresolved'` and
      **zero** COLUMN_LINEAGE edges for it; `collect_coverage(...).lineage_incomplete_dynamic == 1`.
- [ ] Static-literal dynamic SQL (PR-6's recovered case, `parsing_mode='dynamic_sql'`) does **NOT**
      increment `lineage_incomplete_dynamic` (it is fully traced).
- [ ] A non-CREATE `EXECUTE IMMEDIATE` (e.g. `'SHOW TABLES'` / a CALL) does **NOT** emit a
      `dynamic_sql_unresolved` node and does **NOT** increment the count.
- [ ] `gain --json` output contains key `lineage_incomplete_dynamic` with the integer value.
- [ ] Live-DWH: `lineage_incomplete_dynamic` is **non-zero** and equals the corpus count of non-static
      CREATE-shaped `EXECUTE IMMEDIATE` sites. **Exact expected number TBD from the in-flight validation
      run** — pin it from that output before marking this criterion (placeholder cross-check: ~20 EXECUTE
      IMMEDIATE files minus PR-6's statically-recovered set).
- [ ] The four perf-invariant suites pass **UNMODIFIED**; `pyright`/`ruff` clean.

### Version bump / perf / metric
- **Bump:** `1.33.0 → 1.34.0` (minor; new metric). **No schema bump** — uses the existing `parsing_mode`
  column; it's a new *value*, not a new column.
- **Perf impact:** the marker-node emission runs **once per skipped EXECUTE IMMEDIATE site**, NOT per
  column — it must NOT enter the per-column hot loop. The new coverage query is one aggregate read.
  Add no new per-statement op to [`base.py`](../../src/sqlcg/parsers/base.py)'s column loop.
- **Metric snapshot:** REQUIRED. Commit before/after `gain --json` to [`plan/metrics/`](../../plan/metrics/)
  showing the new field and that no other field changed except by the expected delta.
- **Index slot:** REQUIRED for the live-DWH acceptance criterion (parser change → reindex).
  Serialize on the single DWH index slot.

### Risk
- Double-count / misfire: the marker must fire **only** for CREATE-shaped, non-recoverable sites. Reuse
  `_CREATE_DDL_PREFIX_RE` (`:88-91,:580`) as the gate; a node must not be emitted for recoverable static
  cases (those already emit a real `dynamic_sql` query) nor for non-CREATE statements.
- The new `parsing_mode` value `'dynamic_sql_unresolved'` is additive — verify nothing switches on the
  closed set `{sqlglot, dynamic_sql, scripting}` in a way that would mis-bucket it (grep `parsing_mode ==`).

---

## PR-C (#153) — Quoted/spaced identifiers: make them round-trip and CLI-addressable

**Rank 3 (highest risk — touches the identity model everything keys on). Version bump: MINOR → `1.35.0`.**
**A pre-implementation architect-review + spike IS warranted** — see MAINTAINER DECISION. The migration
is a **reindex** under either option.

### Problem
Quoted identifiers with spaces/special chars (IA_SEMANTIC / IA_TABLEAU — ~12k columns / ~249 tables)
are stored as bare lowercased tokens with quotes stripped → `ia_semantic.<t>.omzet excl` — ambiguous,
garbled in output, and not re-typeable into the CLI. The IA consumer-view layer (the warehouse boundary
BI reads) is effectively unqueryable from the CLI.

### Root cause (file:line — verified)
- Column normalization strips surrounding quotes then lowercases:
  [`parsers/base.py:119-131`](../../src/sqlcg/parsers/base.py) (`ColumnRef.__post_init__`).
- Table-ref normalization lowercases catalog/db/name: `base.py:74-79` (`TableRef.__post_init__`).
- Column id is `table.full_id.name`: `ColumnRef.full_id` `:134-140`; table id `TableRef.full_id`
  `:82-99`. **(NOT `:515-516`, which is an unrelated `db.name` key helper.)**
- DDL-side quote strip mirrors the same logic:
  [`parsers/ansi_parser.py:724-725`](../../src/sqlcg/parsers/ansi_parser.py) (defined cols),
  `:767-768` (select-output cols) — these must stay consistent with `ColumnRef`.
- CLI consumes the bare `qualified`/`table_qualified` string with no quote-aware parsing:
  [`cli/commands/analyze.py:220`](../../src/sqlcg/cli/commands/analyze.py) (`ref.lower()`), `:225`
  (`len(ref.split("."))>=3` fallback gate), `:414` `_bare_ref`, `:422` `_col_id_to_table` (`rsplit(".",1)`),
  `:536,:710`. MCP mirror: [`server/tools.py:301`](../../src/sqlcg/server/tools.py) `_parse_column_ref`,
  `:79` `_label`.

### Blast radius — EVERYTHING that keys on `full_id` / `qualified` / the lowercased id
A change here can **silently split or merge graph nodes** — the primary hazard. Treat every entry below
as a surface the reviewer/dev must check:

| Consumer | File:line | What it does | A (keep quotes in id) | B (display_name field) |
|---|---|---|---|---|
| Column edge keys | [`indexer.py:1611-1612`](../../src/sqlcg/indexer/indexer.py), `:1674-1683` | `src_key`/`dst_key` = `ColumnRef.full_id` → COLUMN_LINEAGE PK | **CRITICAL** — id shape change re-keys all edges | unaffected (full_id unchanged) |
| Table edge keys | `indexer.py:1603` | SELECTS_FROM `dst_key = table.full_id` | affected | unaffected |
| Catalog load | [`cli/commands/catalog.py:119-135,:149,:159,:191-196`](../../src/sqlcg/cli/commands/catalog.py) | CSV→HAS_COLUMN id `f"{tq}.{col}"`, lowercased, dot-joined, **no quote awareness** | **CRITICAL** — must mirror new id rule or catalog/lineage keys diverge | unaffected |
| DDL col quote-strip | `ansi_parser.py:724-725,:767-768` | strips quotes like ColumnRef | must change in lockstep with ColumnRef | unaffected |
| Noise-match / matcher | [`core/noise_match.py:35-50,:95,:98-101`](../../src/sqlcg/core/noise_match.py) | `table_short_name` rsplit on `.`/`::`; ignore-set exact + glob match | **embedded-dot/space breaks split + glob** | unaffected |
| Lineage aggregator | [`lineage/aggregator.py:69-111,:189`](../../src/sqlcg/lineage/aggregator.py) | stores by full_id; bare-name index lower(); `_table_ref_for_full_id` `split(".")` | **embedded dot mis-reconstructs TableRef** | unaffected |
| CLI analyze input | `analyze.py:220,:225,:414,:422,:536,:710` | `ref.lower()`; `split(".")>=3` fallback; `rsplit(".",1)` table extract | **must add quote-aware split; embedded dot breaks count** | needs quote-aware *input* parse to map display→id, but id stays stable |
| MCP tools input/label | `server/tools.py:79,:301,:1023,:1091,:1184,:1983` | `_parse_column_ref` lower(); `_label` rsplit | same as CLI | same as CLI (input mapping only) |
| Viz / Mermaid render | `server/tools.py:72-98` `_build_mermaid` | splits col_id on `.` for box labels | garbled labels for spaced ids | **the win** — render display_name |
| Golden / anchor tests | [`test_data_models.py:56,62`](../../tests/unit/test_data_models.py), [`test_normalize_keys.py:103,107`](../../tests/unit/test_normalize_keys.py), [`test_analyze_case_fold.py`](../../tests/unit/test_analyze_case_fold.py) (87,118,132,188,222,236), [`tests/snowflake/E25/test_e25_full_id.py:16-27`](../../tests/snowflake/E25/test_e25_full_id.py) | hardcode dotted lowercased ids; assert no-quote full_id | **must be revised** (id contract changes) | unchanged (contract preserved) — **only add new display tests** |

### Design options — MAINTAINER DECISION REQUIRED

> **The sprint-planner's recommendation has CHANGED from the DRAFT.** The DRAFT recommended Option A.
> The blast-radius pass surfaced a decisive problem with A: a quoted identifier may legally contain a
> **`.`** (e.g. `"a.b"`). The id model and ~9 consumer sites assume `.` is the field separator and split
> on it (`analyze.py:225/414/422/536/710`, `noise_match.py:47-50`, `aggregator.py:189`). Embedding a `.`
> inside a kept-quoted token **breaks every one of those splits** and re-keys the whole graph, forcing all
> golden/anchor tests to be rewritten. **The planner now recommends Option B.**

**Option A — Conditional quote-preservation (DRAFT's pick; NOT recommended).**
Strip+lowercase only when the unquoted name is a *safe bare identifier* (`^[a-z_][a-z0-9_]*$` after
lowercasing); otherwise keep it quoted in the id (`"omzet excl"`).
- *Pros:* single source of truth (the id *is* the display form); no new field.
- *Cons:* **(1)** changes the `full_id` contract → re-keys edges → reindex mandatory **and** every
  golden/anchor test (table above) must be rewritten; **(2)** the embedded-`.` case (`"a.b"`) breaks all
  dot-split parsing across CLI/noise-match/aggregator unless they are *all* converted to a quote-aware
  splitter — a large, error-prone surface; **(3)** catalog load + DDL quote-strip must change in lockstep
  or keys silently diverge. The split hazard makes A high-risk for the exact failure mode (#153) we are
  fixing.

**Option B — Separate `display_name` field (RECOMMENDED).**
Keep `full_id` exactly as today (normalized, lowercase, unquoted, dot-joined — the stable graph key).
Add a `display_name` (and a table-level display name) carrying the **original, quote-preserved source
spelling**; flow it through `ParsedFile` → `SqlColumn`/`SqlTable` rows (new VARCHAR column → **schema
bump** — coordinate with PR-A, see sequencing). Render `display_name` in CLI/MCP/viz output. For **input
addressability**, add a quote-aware reverse-resolver: when the user types `schema.table."omzet excl"`,
parse it quote-aware and look the node up by matching against `display_name` (or a normalized fold of it),
returning the canonical `full_id` for the existing graph queries.
- *Pros:* the graph-key contract is **untouched** → no edge re-keying, no golden-test rewrite (only *new*
  display/round-trip tests); the split hazard is contained to one new input parser, not spread across 9
  sites; catalog/DDL paths unchanged. Strictly additive.
- *Cons:* two fields to keep in sync; the CLI must learn one quote-aware input parser + a display→id
  lookup; a schema bump for the new column; render sites must switch to `display_name`.

**RECOMMENDATION: Option B.** It directly delivers #153's two asks (un-garbled output via `display_name`;
re-typeable input via the quote-aware resolver) **without** inverting the C2 normalization invariant the
entire graph, catalog, and golden suite are built on. **Spike (recommended before commit):** a half-day
spike to confirm the display→id reverse lookup is unambiguous when two quoted names fold to the same
normalized id (collision policy), and to size the render-site sweep. **Architect-review:** warranted on
the final A/B pick and the collision policy — route through `architect-reviewer` if the maintainer leans A
or if the spike surfaces ambiguity.

> **MAINTAINER DECISION (blocks PR-C implementation): choose Option A or Option B, and confirm the
> schema-bump coordination with PR-A.** The developer agent must not start PR-C until this is recorded here.

### Acceptance criteria (observable) — written against Option B; revise if A is chosen
- [ ] A spaced quoted column `IA_SEMANTIC."omzet excl"` (ref `ddl/changelogs/IA-SEMANTIC/ARTIKEL.sql:18-19`)
      stores `full_id` unchanged (normalized) **and** a `display_name` carrying `"omzet excl"`.
- [ ] `analyze upstream/downstream` accepts the user-typed quoted form `ia_semantic.<t>."omzet excl"`
      (quote-aware parse) and returns the correct lineage rows (non-empty assertion on a known edge).
- [ ] **Round-trip:** parse → store → render `display_name` → feed the rendered quoted form back to the
      CLI → resolves to the **same** node (assert same `full_id`).
- [ ] **Safe-identifier regression:** `"ma_aantal"` and `ma_aantal` still collapse to one node
      (`full_id` identical; one COLUMN node). Edge/node counts for safe ids are byte-identical to pre-PR.
- [ ] An embedded-dot quoted name `"a.b"` round-trips and is addressable (the specific case Option A fails).
- [ ] The four perf-invariant suites pass **UNMODIFIED**; existing golden/anchor tests
      (`test_data_models`, `test_normalize_keys`, `test_analyze_case_fold`, E25) pass **unchanged** under
      B (proof the key contract is preserved). `pyright`/`ruff` clean.
- [ ] Live-DWH spot-check: IA_SEMANTIC / IA_TABLEAU columns are addressable from the CLI.

### Version bump / perf / metric
- **Bump:** `1.34.0 → 1.35.0` (minor). **Option B adds a schema column → schema bump** (next available
  after PR-A's v10 → v11; coordinate per the MAINTAINER DECISION). Option A also requires reindex (re-keying).
- **Perf impact:** B adds one VARCHAR per node row — off the hot loop; the reverse-resolver runs once per
  CLI invocation, not per column. **No new op in `base.py`'s per-column loop under either option.**
- **Metric snapshot:** not lineage-metric-moving for `gain`'s percentages, but node/edge counts must be
  **identical** under B except the additive column (assert in the regression test). Under A, expect counts
  to shift only for the affected quoted set — capture before/after counts if A is chosen.
- **Index slot:** REQUIRED for the live-DWH spot-check (reindex). Serialize on the single DWH slot.

### Risk
- **HIGH (Option A):** the id model is keyed on by edges, catalog, noise-match, aggregator, golden tests,
  CLI arg parsing, viz (table above) — a change risks splitting/merging nodes wrongly, and the embedded-dot
  case breaks dot-splitting everywhere. **MEDIUM (Option B):** contained to a new field + one input parser +
  render-site sweep; the key contract is preserved, so the golden suite is the safety net.
- Reindex required under both; existing graphs migrate by reindex.

---

## Suggested overall sequence & gating
| Order | PR | Bump | Schema | Index slot? | Notes |
|-------|----|------|--------|-------------|-------|
| 1 | PR-A #151 | `1.33.0` minor | **v9→v10** | no | tool-version stamp; cheap; unit/integration only |
| 2 | PR-B #152 | `1.34.0` minor | none (new `parsing_mode` value) | **yes** (`gain --json` + live) | parser marker node + coverage metric; serialize on DWH slot |
| 3 | PR-C #153 | `1.35.0` minor | **+1 col → v11 (B)** / re-key (A) | **yes** (live spot-check) | id/display model; **MAINTAINER DECISION + spike + architect-review** first; LAST |

- PR-A is independent of the live validation (no reindex). PR-B's live count and PR-C's spot-check both
  need the **one DWH index slot** → serialize PR-B then PR-C; do not run them against the DWH concurrently.
- The in-flight live-DWH validation may refine PR-B's expected `lineage_incomplete_dynamic` count and may
  add items to fold in before dispatch. Re-pin PR-B's count before that ticket is marked ready.
- **Frozen perf suites (all four, UNMODIFIED, every PR):**
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
  [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py).

## Sprint-level regression guard
Add one guard test asserting all three fixes simultaneously on a small synthetic fixture corpus:
1. **#151:** after index, `Freshness.tool_version_stale` flips True when running version ≠ stamped version.
2. **#152:** a non-static `EXECUTE IMMEDIATE 'CREATE TABLE ...' || :v` yields one
   `parsing_mode='dynamic_sql_unresolved'` query, zero edges, and `lineage_incomplete_dynamic == 1`.
3. **#153:** a spaced quoted column is addressable via the CLI round-trip **and** a safe identifier still
   collapses to one node.
Not marked `xfail`. This is the sprint-planner's compliance anchor.

## Wiring checklist (developer pre-PR; answer with grep evidence)
- [ ] PR-A: `get_indexed_version` has a production call site (freshness render path); `sqlcg_version`
      written by `set_indexed_sha`; all `compute_freshness`/`render_freshness_line` callers updated.
- [ ] PR-B: the `dynamic_sql_unresolved` emit site exists in `snowflake_parser.py`, gated by
      `_CREATE_DDL_PREFIX_RE`; `lineage_incomplete_dynamic` read in `collect_coverage`, rendered in
      `render_coverage_lines` + `db info`, present in `coverage_to_json`.
- [ ] PR-C: `display_name` flows parser→row→render; quote-aware input parser has a call site in
      `analyze`/`server.tools`; no `# TODO` in any happy path.
- [ ] No new per-column op added to [`base.py`](../../src/sqlcg/parsers/base.py)'s lineage loop.
- [ ] Versions bumped + `uv lock` per PR; tag after each master merge ([CLAUDE.md](../../CLAUDE.md) releasing).

---

### Blocking Questions
1. **PR-C Option A vs B — MAINTAINER DECISION (blocks PR-C only).** Planner recommends **Option B**
   (display_name + quote-aware resolver) over the DRAFT's Option A, because Option A breaks dot-split
   parsing for embedded-dot quoted names (`"a.b"`) across ~9 sites and forces a rewrite of the entire
   golden/anchor suite. Confirm B, or pick A with eyes open. (PR-A and PR-B are NOT blocked by this.)
2. **PR-C collision policy (needed only if B and the spike confirm ambiguity):** when two distinct quoted
   display names fold to the same normalized `full_id`, how should the reverse display→id lookup behave —
   first-wins, error, or disambiguate? Resolve in the spike/architect-review before PR-C implementation.
3. **Schema-bump coordination:** PR-A takes v10; PR-C-Option-B's new column would take v11. Confirm this
   ordering, or decide whether B folds its column into PR-A's v10 if they land together. Decide alongside Q1.
