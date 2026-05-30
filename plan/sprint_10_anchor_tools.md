# Sprint Plan: Anchor Tools — LLM-Oriented DWH Navigation

Plan date: 2026-05-29
Author: sprint-planner
Source authority: User findings (F-CLOSURE, F-HYGIENE, A-DEFINE, A-SCOPE, A-RISK, A-BACKFILL, A-DIFF, A-SCOPE-TOOL, V-GOLDEN); [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md); [`plan/sprint_09_lineage_coverage.md`](sprint_09_lineage_coverage.md); CLAUDE.md invariant table.
Policy: no backward compatibility; re-index is the migration path; no TODO in happy path; every new method has a grep-confirmed call site before PR opens; tests must assert observable output, not "no exception raised"; path/constant fallbacks must match `KuzuConfig`.

---

## Open Design Decision — Column-Precise vs Table-Level Blast Radius

**Decision: column-precise, rolled up to table level for the summary.**

When the LLM changes one column, the blast radius is computed column-by-column (only
downstream tables that actually consume that column via `COLUMN_LINEAGE` edges). The
summary response then rolls the affected column set up to a deduplicated table list so
the LLM sees both answers: `affected_columns` (precise, for understanding what breaks)
and `affected_tables` (coarse, for ordering backfill). This decision affects the query
design in A-SCOPE (PR-04), A-DIFF (PR-05), and A-SCOPE-TOOL (PR-06).

Rationale: a table over-approximation for a 176-column table with 11 joins would
return every downstream table that touches any column of that table — the blast radius
would be 10× too large. Column-precise avoids that noise and maps directly onto what
`COLUMN_LINEAGE` edges already represent.

---

## Design Principle — Open-Source Generality (sprint-wide)

`sqlcg` is an open-source package. No DWH-specific rule may live in shipped code. Three
layers, kept strictly separate:

| Layer | Lives in | Example | Rule |
|-------|----------|---------|------|
| **Mechanism** | shipped code | graph traversal, BFS closure, Kahn sort, set rollup | Generic. Knows nothing about table semantics. |
| **Policy** | `.sqlcg.toml` config (sensible, overridable defaults) | noise patterns, presentation-facing prefixes, risk thresholds | Opinionated defaults OK only if fully overridable; a user who sets nothing still gets correct generic behavior. |
| **Ground truth** | gitignored local fixtures (`golden_lineage.yaml`) | "ma_rotatie_totaal upstream = {…}" | Never ships. Calibrates one deployment's recall. |

**Consequences for this sprint:**
- The lineage engine reports **all real edges** (every `SELECTS_FROM` is upstream). It must
  never guess that a table is "just bookkeeping" — that semantic distinction is the user's
  to express via a noise pattern. So control/delta tables (e.g. load-bookkeeping) appear in
  blast radius by default; users who don't want them add an `ignore_table_patterns` entry.
- `presentation_facing` (PR-05) must NOT hardcode any schema prefix. It reads configured
  prefixes; empty config → feature simply off. See PR-05 amendment below.
- The seed golden set reflects what is genuinely in the SQL (all read inputs), not a
  semantic subset — honesty over convenience, since it is the acceptance target.

---

## Summary

Sprint 09 shipped qualify-once, DDL-skip, subprocess isolation, and log-verbosity fixes.
The graph is now correctly built and fast enough. The **product gap** is that neither the
LLM nor a developer can ask the graph a navigation question without writing raw Cypher:
"Where is `ba.wtfe_verkoopinfo` authoritatively defined?", "What does my change break?",
"In what order do I rebuild the affected tables?". This sprint closes that gap with a set
of MCP anchor tools and a shared noise-filter utility, ordered so that the foundational
fixes (depth truncation, noise) land before the anchor tools that depend on them.

Eight PRs, two foundational (must merge first), five anchor (parallelisable after
foundation), one synthesis (must merge last).

---

## Scope

### In Scope

- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — fix `max_depth` default; add `find_definition`, `scope_change`, `diff_impact` tools
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — new response models for all new tools
- [`src/sqlcg/core/config.py`](../src/sqlcg/core/config.py) — add `get_noise_filter_patterns` reading from `.sqlcg.toml`
- [`src/sqlcg/server/noise_filter.py`](../src/sqlcg/server/noise_filter.py) — NEW: shared noise-filter utility (backup pattern matching + schema-alias unification)
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — new named Cypher blocks for anchor tools
- [`src/sqlcg/core/queries.py`](../src/sqlcg/core/queries.py) — export new named Cypher constants
- [`tests/unit/test_noise_filter.py`](../tests/unit/test_noise_filter.py) — NEW: unit tests for noise_filter module
- [`tests/unit/test_closure_depth.py`](../tests/unit/test_closure_depth.py) — NEW: depth-truncation regression guard
- [`tests/integration/test_anchor_tools.py`](../tests/integration/test_anchor_tools.py) — NEW: integration tests for all anchor tools
- [`tests/e2e/test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py) — extend to validate anchor tool outputs at table granularity (V-GOLDEN)

### Non-Goals

- Changes to [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) or [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) hot paths (CLAUDE.md performance invariants must stay intact; these files are not touched)
- Neo4j backend changes (KuzuDB is the sole production backend)
- New node labels or schema DDL changes (anchor tools query the existing schema; no schema migration)
- CLI `analyze` changes (those are separate from the MCP tools)
- dbt adapter or watch-path changes

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|----------------|
| F-CLOSURE: `get_downstream_dependencies`/`get_upstream_dependencies` default `max_depth=5` silently truncates at depth 6+ | Confirmed. [`tools.py`](../src/sqlcg/server/tools.py) lines 459, 535: `max_depth: int = 5`. BFS loop at lines 464–518, 540–597: `if current_id in visited or depth > max_depth: continue` — the `>` means depth-6 nodes are silently dropped when default is 5. No existing override or "full closure" path. |
| F-HYGIENE: no shared backup-pattern filter or schema-alias unification in tools | Confirmed absent. `grep -n "_bck\|backup\|noise\|filter"` on [`tools.py`](../src/sqlcg/server/tools.py) returns zero results. [`config.py`](../src/sqlcg/core/config.py) has `get_schema_aliases()` (toml-driven) but no `get_noise_filter_patterns()`. No `noise_filter.py` module exists anywhere in `src/`. |
| A-DEFINE: `duplicate_ddl:<table>:already_in:<file>` recorded in `parsed.errors` | Confirmed at [`indexer.py`](../src/sqlcg/indexer/indexer.py) line 510. The indexer logs a DEBUG and appends the string, but no MCP tool surfaces it. `DEFINED_IN` edges exist in schema (schema.cypher line 54); no `find_definition` tool queries them. |
| A-SCOPE: no "minimal reading set" tool (defining file + upstream + downstream) | Confirmed absent. `grep -n "scope\|reading_set\|bounded"` on [`tools.py`](../src/sqlcg/server/tools.py) returns zero results. The tool must query `DEFINED_IN`, `SELECTS_FROM`, and `COLUMN_LINEAGE` edges. |
| A-RISK: `analyze unused` CLI exists but no MCP risk-label tool | Confirmed. [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) line 71: `unused` command queries `NOT (t)<-[:SELECTS_FROM]-()`. No MCP equivalent, no risk weight emitted. |
| A-BACKFILL: no topological sort on affected subgraph | Confirmed absent. Kuzu supports Cypher path queries but does not expose topo-sort natively. The tool must implement BFS-based Kahn's algorithm in Python over the subgraph returned by downstream closure. |
| A-DIFF: no "input changed files → blast radius" tool | Confirmed absent. No file-path-to-column-bridge tool exists in [`tools.py`](../src/sqlcg/server/tools.py). |
| A-SCOPE-TOOL: no synthesis tool `scope_change` | Confirmed absent. No `scope_change` function in [`tools.py`](../src/sqlcg/server/tools.py). |
| V-GOLDEN: harness validates column-level lineage only; no table-level blast-radius scoring | Confirmed. [`test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py) scores `expected_sources` (upstream physical leaves). No `expected_downstream_tables`, `expected_blast_radius`, or anchor-tool output validation. `mode` field exists but only `reachable_leaves`/`direct_sources` are implemented. |
| `get_schema_aliases()` confirmed callable from config | Confirmed at [`config.py`](../src/sqlcg/core/config.py) line 90. Pattern: reads `[sqlcg.schema_aliases]` from `.sqlcg.toml`. The new `get_noise_filter_patterns()` must follow the same signature convention. |

---

## Ticket Table

| ID | Title | Files | Effort | Priority | Depends on | Blocks |
|----|-------|-------|--------|----------|------------|--------|
| PR-01 | F-CLOSURE: Full-depth transitive closure | `tools.py`, `models.py`, `tests/unit/test_closure_depth.py` | S | P0 | INDEPENDENT | PR-04, PR-05, PR-06 |
| PR-02 | F-HYGIENE: Shared noise filter + config key | `config.py`, `server/noise_filter.py`, `tests/unit/test_noise_filter.py` | S | P0 | INDEPENDENT | PR-03, PR-04, PR-05, PR-06 |
| PR-03 | A-DEFINE: `find_definition` MCP tool | `tools.py`, `models.py`, `queries.cypher`, `queries.py`, `tests/integration/test_anchor_tools.py` | S | P1 | PR-02 | PR-06 |
| PR-04 | A-SCOPE + A-RISK: `get_change_scope` MCP tool | `tools.py`, `models.py`, `queries.cypher`, `queries.py`, `tests/integration/test_anchor_tools.py` | M | P1 | PR-01, PR-02 | PR-06 |
| PR-05 | A-BACKFILL + A-DIFF: `get_backfill_order` + `diff_impact` MCP tools | `tools.py`, `models.py`, `queries.cypher`, `queries.py`, `tests/integration/test_anchor_tools.py` | M | P1 | PR-01, PR-02 | PR-06 |
| PR-06 | A-SCOPE-TOOL: `scope_change` synthesis MCP tool | `tools.py`, `models.py`, `tests/integration/test_anchor_tools.py` | M | P1 | PR-01, PR-02, PR-03, PR-04, PR-05 | NONE |
| PR-07 | V-GOLDEN: Extend golden harness to validate anchor tool outputs | `tests/e2e/test_golden_lineage.py` | S | P1 | PR-01, PR-02 | NONE |

---

## Recommended Implementation Order

### Why This Order

**PR-01 (F-CLOSURE)** is INDEPENDENT and must land before any anchor tool that traverses
the graph, because every downstream/upstream query silently truncates at depth 5 today.
Shipping any anchor tool before PR-01 would mean the blast-radius answers are wrong by
default. PR-01 is also XS risk — it only changes the default value of `max_depth` and
the query strategy in the BFS loop.

**PR-02 (F-HYGIENE)** is INDEPENDENT and must land before any anchor tool that returns
node sets, because without the noise filter every tool answer includes `_bck` snapshot
tables and double-counts schema-alias mirrors. PR-02 introduces `noise_filter.py` and
`get_noise_filter_patterns()` — both purely additive; no existing code is modified. Low
risk, and it unblocks PR-03, PR-04, PR-05, PR-06 simultaneously.

**PR-01 and PR-02 can merge in parallel** — their file sets are disjoint (`tools.py`
+`models.py` vs `config.py`+`noise_filter.py`).

**PR-03 (A-DEFINE)** depends on PR-02 (noise filter must exist before it calls it),
but not on PR-01 (definition lookup does not traverse depth). It can start in a worktree
the moment PR-02 merges.

**PR-04 (A-SCOPE + A-RISK)** depends on both PR-01 and PR-02. It starts after both
foundation PRs merge.

**PR-05 (A-BACKFILL + A-DIFF)** depends on both PR-01 and PR-02. It can land in
parallel with PR-04 — they touch the same files (`tools.py`, `models.py`,
`queries.cypher`, `queries.py`) but are in separate worktrees during development; final
merge must be sequential (PR-04 first, then PR-05 rebases on it to avoid conflicts on
the shared files).

**PR-07 (V-GOLDEN)** depends on PR-01 and PR-02 (correct closure + noise filter) but
not on the anchor tools themselves — the harness validates the graph directly. It can
merge in parallel with PR-03/PR-04/PR-05 once the foundation is in.

**PR-06 (A-SCOPE-TOOL)** is explicitly last: it calls `find_definition`, `get_change_scope`,
`get_backfill_order`, and `diff_impact` internally, so all four must already be merged and
callable before PR-06 opens. It is a synthesis tool, not a query tool.

### Single-Developer Sequence

1. PR-01 — fix closure depth
2. PR-02 — add noise filter
3. PR-03 — `find_definition` (depends on PR-02)
4. PR-04 — `get_change_scope` (depends on PR-01, PR-02)
5. PR-07 — extend golden harness (depends on PR-01, PR-02; independent of PR-03/04/05)
6. PR-05 — `get_backfill_order` + `diff_impact` (rebase on PR-04 before opening)
7. PR-06 — `scope_change` synthesis (all prior merged)

### Two-Developer Sequence

**Week 1 (both start immediately):**
- Agent A: PR-01
- Agent B: PR-02

**After PR-01 and PR-02 merge (start of week 2):**
- Agent A Track: PR-04 → PR-05 (sequential within track; share files)
- Agent B Track: PR-03 → PR-07 → (wait for A to finish PR-05) → PR-06

**PR-06 starts only after both tracks have merged all their PRs.**

Parallelism table:

| Phase | Agent A | Agent B | Gate |
|-------|---------|---------|------|
| 1 | PR-01 | PR-02 | Both merge |
| 2 | PR-04 | PR-03 | PR-01+PR-02 merged |
| 3 | PR-05 (rebase on PR-04) | PR-07 | PR-04 merged |
| 4 | — | PR-06 | All PRs merged |

---

## Ticket Specifications

---

### PR-01 — F-CLOSURE: Full-Depth Transitive Closure

**Source**: User finding F-CLOSURE (measured DWH chains reach depth 6–7; default max_depth=5 silently truncates)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: PR-04, PR-05, PR-06

**Root cause**: [`tools.py`](../src/sqlcg/server/tools.py) lines 459, 535: `max_depth: int = 5` is the function signature default for `get_downstream_dependencies` and `get_upstream_dependencies`. The BFS loop condition `depth > max_depth` means that at the default of 5, depth-6 nodes are silently skipped — no warning is emitted, no truncation flag is set in the response. The DWH corpus has measured chains of depth 6 (`ba.wtfe_verkoopinfo` → 128 downstream tables) and depth 7 (`ba.wtfv_voorraad_dagstand` → 77 tables); both are silently truncated. `trace_column_lineage` has the same default of 5 at line 326 and must also be corrected.

**What to do**:

1. Change the `max_depth` default to `None` (full closure) in all three signatures. When `max_depth` is `None`, the BFS loop runs until exhaustion with no depth cap. When `max_depth` is provided as an integer, the existing `depth > max_depth` guard is preserved.

```python
# Before — tools.py line 459
def get_downstream_dependencies(table_col: str, max_depth: int = 5) -> DependencyResult:

# After
def get_downstream_dependencies(table_col: str, max_depth: int | None = None) -> DependencyResult:
```

```python
# Before — BFS loop condition
if current_id in visited or depth > max_depth:
    continue

# After
if current_id in visited or (max_depth is not None and depth > max_depth):
    continue
```

Apply the same change to `get_upstream_dependencies` (line 535) and `trace_column_lineage` (line 326).

2. Add `truncated: bool` and `depth_reached: int` fields to `DependencyResult` in [`models.py`](../src/sqlcg/server/models.py). Set `truncated=True` and record `depth_reached` when the caller passed an explicit `max_depth` and any node was skipped at that cap. Set `truncated=False` for full-closure runs.

3. Add a safety guard: if the BFS visits more than 50,000 nodes, stop and set `truncated=True` with a warning in the hint. This prevents pathological graphs from hanging the MCP server.

4. Update docstrings: state that `max_depth=None` computes full transitive closure; document the 50,000-node safety cap.

**Wiring verification**:

Before opening the PR:
- `grep -n "max_depth.*= 5" src/sqlcg/server/tools.py` must return zero results
- `grep -n "max_depth.*= None" src/sqlcg/server/tools.py` must show all three function signatures
- `grep -n "max_depth is not None" src/sqlcg/server/tools.py` must show the BFS guard in all three loops
- `grep -n "truncated" src/sqlcg/server/models.py` must show the new field on `DependencyResult`
- Confirm no TODO remains in the BFS loop body of all three tools

**Files affected**:
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — signature + BFS guard in three functions
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `DependencyResult`: add `truncated`, `depth_reached`
- `tests/unit/test_closure_depth.py` — NEW

**Tests to add**:

- **Scenario A — full closure reaches beyond depth 5**: build an in-memory graph with a 7-hop chain (`A→B→C→D→E→F→G`); call `get_downstream_dependencies("A", max_depth=None)`; assert `len(result.nodes) == 6` (B through G). Assertion: `nodes` contains all 6 downstream column IDs, `result.truncated == False`.

- **Scenario B — explicit cap still truncates**: same 7-hop chain; call `get_downstream_dependencies("A", max_depth=3)`; assert `len(result.nodes) == 3` (B, C, D only); `result.truncated == True`; `result.depth_reached == 3`.

- **Scenario C — safety cap at 50k nodes**: build a star graph with 50,001 leaf nodes all connected to one root; call `get_downstream_dependencies(root, max_depth=None)`; assert `result.truncated == True` and `len(result.nodes) <= 50000` and `result.hint` contains the word "cap".

- **Scenario D — DWH depth regression guard (unit fixture)**: build a 6-hop chain; call with `max_depth=None`; assert all 5 intermediate and 1 terminal node appear — `len(result.nodes) == 5`. Failure message: `"Depth-truncation regression: full closure must reach depth 6 — guards against max_depth=5 default"`.

**Acceptance criteria**:
- `[ ]` `get_downstream_dependencies` called with no `max_depth` argument returns nodes at depth > 5 (verified by Scenario A and D)
- `[ ]` `grep -n "max_depth.*= 5" src/sqlcg/server/tools.py` returns zero results
- `[ ]` `DependencyResult.truncated` is `False` on full-closure runs and `True` on capped runs
- `[ ]` No TODO in the BFS happy path of all three tools
- `[ ]` Safety cap of 50,000 nodes is enforced (Scenario C passes)

---

### PR-02 — F-HYGIENE: Shared Noise Filter + Config Key

**Source**: User finding F-HYGIENE (backup snapshot tables and schema-alias mirrors pollute every tool answer)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: PR-03, PR-04, PR-05, PR-06

**Root cause**: No shared noise-filtering utility exists anywhere in `src/`. Each tool returns raw graph results including `_bck` / `_bck_us` / `_bck_<n>` backup snapshot tables and schema-alias mirrors (e.g. `ia_analytics.ba_x` alongside `ba.x`). The `schema_aliases` mechanism in [`config.py`](../src/sqlcg/core/config.py) normalises identities at parse time; at query time, the inverse is needed: given a result set, identify which nodes are alias duplicates and exclude/unify them. No config key for backup ignore patterns exists — the only toml-driven keys are `dialect` and `schema_aliases`.

**What to do**:

1. Add `get_noise_filter_patterns(path: Path) -> list[str]` to [`config.py`](../src/sqlcg/core/config.py), following the exact same signature pattern as `get_schema_aliases`. It reads `[sqlcg.noise_filter]` → `ignore_table_patterns` (a list of glob strings) from `.sqlcg.toml`. Returns the list lowercased. When the key is absent, returns a built-in default list: `["*_bck", "*_bck_us", "*_bck_[0-9]*", "*_backup", "*_backup_[0-9]*"]`.

```toml
# .sqlcg.toml example
[sqlcg.noise_filter]
ignore_table_patterns = ["*_bck", "*_bck_us", "*_bck_[0-9]*"]
```

2. Create [`src/sqlcg/server/noise_filter.py`](../src/sqlcg/server/noise_filter.py) (NEW file). It must expose:

```python
class NoiseFilter:
    """Config-driven filter for backup tables and schema-alias mirrors.

    Loaded once per tool call from KuzuConfig.from_env().db_path parent dir
    (falls back to cwd). All methods are pure — no graph calls.
    """

    def __init__(self, patterns: list[str], schema_aliases: dict[str, str]) -> None:
        ...

    def is_noise(self, table_qualified: str) -> bool:
        """Return True if table_qualified matches any backup pattern (fnmatch)."""
        ...

    def canonical(self, table_qualified: str) -> str:
        """Return the canonical form of a table_qualified, resolving alias mirrors."""
        ...

    def filter_nodes(
        self,
        nodes: list[str],           # list of table_qualified or col_id strings
        report_excluded: bool = True,
    ) -> tuple[list[str], list[str]]:
        """Return (kept, excluded). Excluded contains noise nodes."""
        ...
```

3. Add `NoiseFilter.from_config(repo_root: Path | None = None) -> NoiseFilter` classmethod that calls `get_noise_filter_patterns()` and `get_schema_aliases()` from [`config.py`](../src/sqlcg/core/config.py) and constructs a `NoiseFilter`.

4. The `repo_root` falls back to `Path.cwd()` when `None`. This is the same fallback convention used by `get_dialect(path)`.

**Wiring verification**:

Before opening the PR:
- `grep -n "get_noise_filter_patterns" src/sqlcg/core/config.py` must show the function definition
- `grep -n "NoiseFilter" src/sqlcg/server/noise_filter.py` must show the class definition
- `grep -n "from_config" src/sqlcg/server/noise_filter.py` must show the classmethod
- `grep -n "get_noise_filter_patterns\|get_schema_aliases" src/sqlcg/server/noise_filter.py` must confirm both are imported and called in `from_config`
- Confirm no TODO in `is_noise`, `canonical`, `filter_nodes`, `from_config`

**Files affected**:
- [`src/sqlcg/core/config.py`](../src/sqlcg/core/config.py) — add `get_noise_filter_patterns`
- [`src/sqlcg/server/noise_filter.py`](../src/sqlcg/server/noise_filter.py) — NEW
- `tests/unit/test_noise_filter.py` — NEW

**Tests to add**:

- **Scenario A — backup pattern match**: `NoiseFilter(patterns=["*_bck", "*_bck_[0-9]*"], schema_aliases={})`. Call `is_noise("ba.wtfe_verkoopinfo_bck")` → `True`. Call `is_noise("ba.wtfe_verkoopinfo_bck_20240101")` → `True`. Call `is_noise("ba.wtfe_verkoopinfo")` → `False`. Assertion: booleans, not "no exception".

- **Scenario B — schema-alias canonical form**: `NoiseFilter(patterns=[], schema_aliases={"ia_analytics": "ba"})`. `canonical("ia_analytics.ba_wtfe_verkoopinfo")` → `"ia_analytics.ba_wtfe_verkoopinfo"` (alias applies to schema, not table prefix). `canonical("ia_analytics.x")` is returned unchanged (no structural rewrite, alias unification is schema-level). Assertion: canonical returns a string equal to the expected value.

- **Scenario C — filter_nodes excludes noise and reports it**: `filter_nodes(["ba.dim_table", "ba.dim_table_bck", "ia_analytics.ba_dim_table"])` with `patterns=["*_bck"]` and `schema_aliases={"ia_analytics": "ba"}`. Assert `kept` contains exactly `["ba.dim_table"]` (or also the alias, depending on unification rule — document choice), `excluded` contains `["ba.dim_table_bck"]`, list lengths are observable integers.

- **Scenario D — default patterns applied when config absent**: `get_noise_filter_patterns(Path("/nonexistent"))` returns a list with length >= 3 and all entries are strings matching `"*_bck*"` or similar. Assertion: `len(result) >= 3` and `"*_bck" in result`.

**Acceptance criteria**:
- `[ ]` `grep -n "get_noise_filter_patterns" src/sqlcg/core/config.py` shows the function
- `[ ]` `grep -n "class NoiseFilter" src/sqlcg/server/noise_filter.py` shows the class
- `[ ]` Default patterns cover `*_bck`, `*_bck_us`, `*_bck_[0-9]*` without any `.sqlcg.toml`
- `[ ]` `filter_nodes` returns a tuple of two non-negative-length lists (not a single filtered list)
- `[ ]` No TODO in any of the four methods

---

### PR-03 — A-DEFINE: `find_definition` MCP Tool

**Source**: User finding A-DEFINE ("where is X authoritatively defined?")
**Effort**: S
**Depends on**: PR-02 (noise filter for flagging `_bck` definition files)
**Blocks**: PR-06

**Root cause**: The graph already has `DEFINED_IN` edges from `SqlTable` to `File`
(schema.cypher line 54; indexer.py line 683 `upsert_edges_bulk`). The indexer records
`duplicate_ddl:<table>:already_in:<file>` in `parsed.errors` (indexer.py line 510) when
a table is defined in multiple files, but no MCP tool surfaces this. An LLM navigating
the DWH has no way to ask "which file is the authoritative DDL for `ba.wtfe_verkoopinfo`?"
without raw Cypher.

**What to do**:

1. Add a named Cypher block `FIND_DEFINITION` to [`queries.cypher`](../src/sqlcg/core/queries.cypher):

```cypher
-- FIND_DEFINITION
MATCH (t:SqlTable {qualified: $table_qualified})-[:DEFINED_IN]->(f:File)
RETURN t.qualified AS table_qualified, t.kind AS kind, t.defined_in_file AS defined_in_file, f.path AS file_path
```

2. Export `FIND_DEFINITION_QUERY` from [`queries.py`](../src/sqlcg/core/queries.py).

3. Add `DefinitionResult` and `DefinitionFile` models to [`models.py`](../src/sqlcg/server/models.py):

```python
class DefinitionFile(BaseModel):
    file_path: str
    kind: str | None  # TABLE, VIEW, etc.
    is_authoritative: bool  # True if not a _bck file and not a duplicate
    is_backup: bool          # True if file_path or table_qualified matches backup pattern

class DefinitionResult(BaseModel):
    table_qualified: str
    definitions: list[DefinitionFile]
    duplicate_ddl: bool  # True when len(definitions) > 1
    noise_excluded: list[str]  # backup definition files excluded from definitions
    hint: str | None
```

4. Add `find_definition(table_qualified: str) -> DefinitionResult` to [`tools.py`](../src/sqlcg/server/tools.py), decorated with `@mcp.tool()` and `@_timed_tool("find_definition")`. The tool:
   - Queries `FIND_DEFINITION_QUERY` with `{"table_qualified": table_qualified.lower()}`.
   - Constructs a `NoiseFilter` via `NoiseFilter.from_config()`.
   - Flags each result row: `is_backup = noise_filter.is_noise(row["table_qualified"])`.
   - Returns all definitions (including backups) but marks backups in `is_backup`; separately populates `noise_excluded` with backup file paths that were detected.
   - Sets `duplicate_ddl = len(definitions) > 1`.
   - Populates `hint` when `definitions` is empty.

**Wiring verification**:

Before opening the PR:
- `grep -n "FIND_DEFINITION" src/sqlcg/core/queries.cypher` must show the Cypher block
- `grep -n "FIND_DEFINITION_QUERY" src/sqlcg/core/queries.py` must show the export
- `grep -n "find_definition" src/sqlcg/server/tools.py` must show the `@mcp.tool()` definition
- `grep -n "NoiseFilter" src/sqlcg/server/tools.py` must show the import and call site in `find_definition`
- `grep -n "FIND_DEFINITION_QUERY" src/sqlcg/server/tools.py` must show usage in `find_definition`
- Confirm no TODO in `find_definition` body

**Files affected**:
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — add `FIND_DEFINITION` block
- [`src/sqlcg/core/queries.py`](../src/sqlcg/core/queries.py) — export `FIND_DEFINITION_QUERY`
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — add `DefinitionFile`, `DefinitionResult`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — add `find_definition`
- `tests/integration/test_anchor_tools.py` — add `find_definition` scenarios

**Tests to add**:

- **Scenario A — single authoritative definition**: index a fixture with one `CREATE TABLE ba.dim_date (id INT)` in `dim_date.sql`. Call `find_definition("ba.dim_date")`. Assert `len(result.definitions) == 1`; `result.definitions[0].is_authoritative == True`; `result.duplicate_ddl == False`.

- **Scenario B — backup definition flagged**: index two fixtures: `dim_date.sql` (authoritative) and `dim_date_bck.sql` (`CREATE TABLE ba.dim_date_bck (id INT)`). Call `find_definition("ba.dim_date_bck")`. Assert `result.definitions[0].is_backup == True` (the backup pattern matches the table name). Assertion: `is_backup` is `True`, not just "no exception".

- **Scenario C — duplicate DDL detected**: index two fixtures both defining `ba.dim_date`. Call `find_definition("ba.dim_date")`. Assert `result.duplicate_ddl == True` and `len(result.definitions) == 2`.

- **Scenario D — table not indexed**: call `find_definition("ba.nonexistent_table")`. Assert `len(result.definitions) == 0` and `result.hint` is a non-empty string.

**Acceptance criteria**:
- `[ ]` `find_definition` is callable as an MCP tool (decorated with `@mcp.tool()`)
- `[ ]` `grep -n "FIND_DEFINITION_QUERY" src/sqlcg/server/tools.py` shows the call site
- `[ ]` Backup files are flagged `is_backup=True`, not silently excluded (LLM sees them and their flag)
- `[ ]` `duplicate_ddl=True` when multiple files define the same table
- `[ ]` No TODO in `find_definition` body

---

### PR-04 — A-SCOPE + A-RISK: `get_change_scope` MCP Tool

**Source**: User findings A-SCOPE ("bounded change scope") and A-RISK ("dead/risk labeling")
**Effort**: M
**Depends on**: PR-01 (full closure), PR-02 (noise filter)
**Blocks**: PR-06

**Root cause**: No MCP tool returns the minimal reading set for a change (defining file + direct upstream inputs + direct downstream consumers + risk label). The LLM currently has to call `get_downstream_dependencies`, `get_upstream_dependencies`, `find_table_usages`, and `find_definition` separately, then de-duplicate and label manually. The risk labeling (`analyze unused` in the CLI) is not exposed via MCP at all. The combined query requires traversing `DEFINED_IN`, `SELECTS_FROM`, and `COLUMN_LINEAGE` edges in a single response.

**Design note (column-precise)**: The scope query resolves at column granularity first. Downstream affected columns are returned as `affected_columns`; these are then rolled up by `table_qualified` to produce `affected_tables`. This gives both the precise set (for understanding what breaks) and the table list (for backfill ordering).

**What to do**:

1. Add two Cypher blocks to [`queries.cypher`](../src/sqlcg/core/queries.cypher):

```cypher
-- GET_TABLE_DEFINING_FILES
MATCH (t:SqlTable {qualified: $table_qualified})-[:DEFINED_IN]->(f:File)
RETURN f.path AS file_path, t.kind AS kind

-- GET_TABLE_DIRECT_UPSTREAMS
MATCH (t:SqlTable {qualified: $table_qualified})<-[:SELECTS_FROM]-(q:SqlQuery)
-[:QUERY_DEFINED_IN]->(f:File)
OPTIONAL MATCH (q)-[:SELECTS_FROM]->(src:SqlTable)
WHERE src.qualified <> $table_qualified
RETURN DISTINCT src.qualified AS upstream_table, f.path AS in_file
```

Export `GET_TABLE_DEFINING_FILES_QUERY` and `GET_TABLE_DIRECT_UPSTREAMS_QUERY` from [`queries.py`](../src/sqlcg/core/queries.py).

2. Add `ChangeScopeResult` model to [`models.py`](../src/sqlcg/server/models.py):

```python
class ChangeScopeResult(BaseModel):
    target: str  # table_qualified input
    defining_files: list[str]
    upstream_tables: list[str]          # direct upstream inputs (no closure)
    affected_columns: list[str]         # col_ids downstream (full closure via PR-01)
    affected_tables: list[str]          # table_qualified rollup of affected_columns
    risk_label: str                     # "safe" | "low" | "medium" | "high"
    risk_weight: int                    # count of downstream tables
    noise_excluded: list[str]           # noise table_qualifieds excluded from affected
    truncated: bool                     # True if safety cap was hit
    hint: str | None
```

3. Add `get_change_scope(table_qualified: str) -> ChangeScopeResult` to [`tools.py`](../src/sqlcg/server/tools.py):
   - Query defining files with `GET_TABLE_DEFINING_FILES_QUERY`.
   - Query direct upstreams with `GET_TABLE_DIRECT_UPSTREAMS_QUERY`.
   - For downstream columns: BFS over `COLUMN_LINEAGE` edges starting from all `SqlColumn` nodes where `table_qualified = $table_qualified`. Use `max_depth=None` (full closure from PR-01). Apply noise filter.
   - Roll up `affected_columns` → `affected_tables` by extracting `table_qualified` from each col_id.
   - Compute `risk_label`:
     - 0 affected tables → `"safe"`
     - 1–5 → `"low"`
     - 6–20 → `"medium"`
     - > 20 → `"high"`
   - Set `risk_weight = len(affected_tables)`.

**Wiring verification**:

Before opening the PR:
- `grep -n "GET_TABLE_DEFINING_FILES_QUERY\|GET_TABLE_DIRECT_UPSTREAMS_QUERY" src/sqlcg/core/queries.py` must show both exports
- `grep -n "get_change_scope" src/sqlcg/server/tools.py` must show the `@mcp.tool()` definition
- `grep -n "NoiseFilter" src/sqlcg/server/tools.py` must show usage in `get_change_scope`
- `grep -n "max_depth=None" src/sqlcg/server/tools.py` must show the full-closure call in `get_change_scope`
- Confirm no TODO in `get_change_scope` body

**Files affected**:
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — two new blocks
- [`src/sqlcg/core/queries.py`](../src/sqlcg/core/queries.py) — two new exports
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `ChangeScopeResult`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — `get_change_scope`
- `tests/integration/test_anchor_tools.py` — scenarios below

**Tests to add**:

- **Scenario A — terminal table is safe**: index a fixture with `ba.terminal_view` defined as `CREATE VIEW AS SELECT id FROM ba.source_table`. Call `get_change_scope("ba.terminal_view")`. Assert `result.risk_label == "safe"` (no downstream consumers); `len(result.defining_files) == 1`; `len(result.upstream_tables) >= 1`. Assertion: specific string and integer values.

- **Scenario B — source table with downstream**: index a 3-table chain `source → etl → mart`. Call `get_change_scope("source")`. Assert `result.risk_label in ("low", "medium", "high")`; `len(result.affected_tables) >= 1`; `"etl" in result.affected_tables or any("etl" in t for t in result.affected_tables)`. Assertion: list length >= 1 and specific name substring.

- **Scenario C — backup tables excluded from affected**: index the chain `source → source_bck → mart`. Noise filter patterns include `*_bck`. Call `get_change_scope("source")`. Assert `all("_bck" not in t for t in result.affected_tables)` and `"source_bck" in result.noise_excluded`. Assertion: specific table name in noise_excluded list.

- **Scenario D — risk label thresholds**: unit test (no graph). Call the internal risk-label function with counts 0, 3, 15, 25. Assert returns `"safe"`, `"low"`, `"medium"`, `"high"` respectively.

**Acceptance criteria**:
- `[ ]` `grep -n "get_change_scope" src/sqlcg/server/tools.py` shows `@mcp.tool()` decoration
- `[ ]` Full closure (max_depth=None) confirmed by grep in `get_change_scope` body
- `[ ]` Noise filter call confirmed by grep (`NoiseFilter`) in `get_change_scope`
- `[ ]` `risk_label` is one of `"safe"`, `"low"`, `"medium"`, `"high"` (verified by Scenario D)
- `[ ]` No TODO in `get_change_scope` body

---

### PR-05 — A-BACKFILL + A-DIFF: `get_backfill_order` + `diff_impact` MCP Tools

**Source**: User findings A-BACKFILL ("topological rebuild order") and A-DIFF ("input changed file paths → blast radius")
**Effort**: M
**Depends on**: PR-01 (full closure), PR-02 (noise filter)
**Blocks**: PR-06

**Root cause**: No MCP tool returns the topological rebuild order for the downstream affected subgraph. The LLM receives an unordered set of affected tables from `get_downstream_dependencies` but cannot determine which to rebuild first (a table that is both upstream and downstream of another must rebuild first). KuzuDB's Cypher does not expose a native topological sort, so this must be implemented as Kahn's algorithm in Python over the BFS result.

No MCP tool accepts file paths and returns the union blast radius across all changed files (the CI/agent integration point). The `DEFINED_IN` and `QUERY_DEFINED_IN` edges are needed to bridge file paths to table/column identities.

**What to do**:

1. Add `GET_COLUMNS_FOR_TABLE` and `GET_TABLES_DEFINED_IN_FILE` Cypher blocks to [`queries.cypher`](../src/sqlcg/core/queries.cypher):

```cypher
-- GET_COLUMNS_FOR_TABLE
MATCH (t:SqlTable {qualified: $table_qualified})-[:HAS_COLUMN]->(c:SqlColumn)
RETURN c.id AS col_id, c.col_name AS col_name

-- GET_TABLES_DEFINED_IN_FILE
MATCH (f:File {path: $file_path})<-[:DEFINED_IN]-(t:SqlTable)
RETURN t.qualified AS table_qualified
```

Export `GET_COLUMNS_FOR_TABLE_QUERY` and `GET_TABLES_DEFINED_IN_FILE_QUERY` from [`queries.py`](../src/sqlcg/core/queries.py).

2. Add `BackfillOrderResult` and `DiffImpactResult` models to [`models.py`](../src/sqlcg/server/models.py):

```python
class BackfillOrderResult(BaseModel):
    target: str
    backfill_order: list[str]   # table_qualified in topological rebuild order (leaf first)
    affected_columns: list[str]  # col_ids for column-precise understanding
    noise_excluded: list[str]
    truncated: bool
    hint: str | None

class DiffImpactResult(BaseModel):
    changed_files: list[str]
    changed_tables: list[str]          # tables defined in those files
    affected_tables: list[str]         # union blast radius, deduplicated
    presentation_facing: list[str]     # subset of affected_tables whose schema matches a configured presentation prefix (empty when unconfigured)
    backfill_order: list[str]          # topological order across union blast radius
    noise_excluded: list[str]
    truncated: bool
    hint: str | None
```

3. Implement `_kahn_topological_sort(affected_tables: list[str], db: GraphBackend) -> list[str]`:
   - Internal helper (not an MCP tool, not exported). Takes the affected table set, queries `SELECTS_FROM` edges among members to build an adjacency list, runs Kahn's algorithm.
   - Returns tables in processing order (producers before consumers, so a table with no internal incoming edges from the affected set comes first).
   - If a cycle is detected (SELECTS_FROM forms a cycle among affected tables — possible with CTEs/views), returns the tables in BFS order with a logged warning. No exception raised.

4. Add `get_backfill_order(table_qualified: str) -> BackfillOrderResult` and `diff_impact(changed_files: list[str]) -> DiffImpactResult` to [`tools.py`](../src/sqlcg/server/tools.py).

   `get_backfill_order`:
   - Queries all columns for `table_qualified` via `GET_COLUMNS_FOR_TABLE_QUERY`.
   - BFS downstream over `COLUMN_LINEAGE` edges (full closure, PR-01) to collect `affected_columns`.
   - Rolls up to `affected_tables`.
   - Applies noise filter.
   - Calls `_kahn_topological_sort(affected_tables, db)`.

   `diff_impact`:
   - For each file path: query `GET_TABLES_DEFINED_IN_FILE_QUERY` → `changed_tables`.
   - For each changed table: expand to its columns via `GET_COLUMNS_FOR_TABLE_QUERY`; BFS downstream (full closure).
   - Union all affected column sets; roll up to `affected_tables`.
   - Filter `presentation_facing` by configured schema prefixes (see config note below).
   - Apply noise filter.
   - Call `_kahn_topological_sort(affected_tables, db)`.

**Config-driven presentation prefixes (open-source generality, per the sprint Design Principle)**:
`ia_` is the DWH's analytics schema — it must not be hardcoded in shipped code. Add
`get_presentation_prefixes(path: Path) -> list[str]` to [`config.py`](../src/sqlcg/core/config.py),
following the `get_schema_aliases`/`get_noise_filter_patterns` convention: read
`[sqlcg.presentation]` → `schema_prefixes` (list of strings, lowercased). **Default: `[]`** —
when unset, `presentation_facing` is always empty and the feature is simply off (correct
generic behavior for any user). `diff_impact` computes
`presentation_facing = [t for t in affected_tables if any(t.startswith(p) for p in prefixes)]`.

```toml
# .sqlcg.toml — this repo sets:
[sqlcg.presentation]
schema_prefixes = ["ia_"]
```

**Wiring verification**:

Before opening the PR:
- `grep -n "GET_COLUMNS_FOR_TABLE_QUERY\|GET_TABLES_DEFINED_IN_FILE_QUERY" src/sqlcg/core/queries.py` must show both exports
- `grep -n "get_backfill_order\|diff_impact" src/sqlcg/server/tools.py` must show both `@mcp.tool()` definitions
- `grep -n "_kahn_topological_sort" src/sqlcg/server/tools.py` must show both the definition and two call sites (in `get_backfill_order` and `diff_impact`)
- `grep -n "NoiseFilter" src/sqlcg/server/tools.py` must show usage in both new tools
- `grep -n "max_depth=None" src/sqlcg/server/tools.py` must show full-closure calls in both new tools
- `grep -n "get_presentation_prefixes" src/sqlcg/core/config.py` must show the function; `grep -n "ia_" src/sqlcg/server/tools.py` must return zero hardcoded results (prefix comes from config only)
- Confirm no TODO in `_kahn_topological_sort`, `get_backfill_order`, `diff_impact`

**Files affected**:
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — two new blocks
- [`src/sqlcg/core/queries.py`](../src/sqlcg/core/queries.py) — two new exports
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `BackfillOrderResult`, `DiffImpactResult`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — `_kahn_topological_sort`, `get_backfill_order`, `diff_impact`
- `tests/integration/test_anchor_tools.py` — scenarios below

**Tests to add**:

- **Scenario A — backfill order is topologically correct**: index a 3-table chain `raw → staged → mart`. Call `get_backfill_order("raw")`. Assert `result.backfill_order.index("staged") < result.backfill_order.index("mart")` — `staged` must appear before `mart`. Assertion: specific relative ordering, not just "list is non-empty".

- **Scenario B — Kahn cycle degrades gracefully**: build an intentional cycle `A → B → A` in the fixture (via mutually-dependent views). Call `get_backfill_order("A")`. Assert `result.backfill_order` is non-empty (no exception) and `result.hint` contains the word "cycle". Assertion: list is non-empty and hint string contains "cycle".

- **Scenario C — diff_impact from file path to blast radius**: index a fixture where `source.sql` defines `ba.source_table` and `etl.sql` defines `ba.etl_table` which selects from `ba.source_table`. Call `diff_impact(["<path>/source.sql"])`. Assert `"ba.source_table" in result.changed_tables`; `"ba.etl_table" in result.affected_tables`. Assertion: specific table names.

- **Scenario D — presentation_facing is config-driven**: index a fixture with `ba.source_table → ia_analytics.ba_view`. With `[sqlcg.presentation] schema_prefixes = ["ia_"]` configured (write a temp `.sqlcg.toml` or pass the prefix list), call `diff_impact(["<path>/source.sql"])` and assert `"ia_analytics.ba_view" in result.presentation_facing`. **Then with no presentation config**, assert `result.presentation_facing == []` (feature off by default — no hardcoded `ia_`). Assertion: specific table name present when configured, empty list when not.

- **Scenario E — noise excluded from backfill order**: index `ba.source → ba.source_bck → ba.mart`. Call `get_backfill_order("ba.source")`. Assert `"ba.source_bck" not in result.backfill_order` and `"ba.source_bck" in result.noise_excluded`.

**Acceptance criteria**:
- `[ ]` `grep -n "get_backfill_order\|diff_impact" src/sqlcg/server/tools.py` shows both `@mcp.tool()` decorations
- `[ ]` `grep -n "_kahn_topological_sort" src/sqlcg/server/tools.py` returns >= 3 lines (definition + 2 call sites)
- `[ ]` Topological order is correct (Scenario A verified)
- `[ ]` `presentation_facing` is config-driven via `[sqlcg.presentation] schema_prefixes`; empty (feature off) when unconfigured, no hardcoded `ia_` in shipped code (Scenario D verified)
- `[ ]` `get_presentation_prefixes` added to `config.py` with default `[]` (grep-confirmed)
- `[ ]` No TODO in any of the three new functions

---

### PR-06 — A-SCOPE-TOOL: `scope_change` Synthesis MCP Tool

**Source**: User finding A-SCOPE-TOOL ("one call returns everything the LLM needs")
**Effort**: M
**Depends on**: PR-01, PR-02, PR-03, PR-04, PR-05 (all must be merged)
**Blocks**: NONE

**Root cause**: The LLM must currently call 4–5 separate MCP tools to assemble the context it needs before starting a change. Each additional round-trip costs latency and risks the LLM's context being incomplete if one call fails silently. A synthesis tool that delegates to the already-correct individual tools and returns a single unified response eliminates this problem.

**What to do**:

1. Add `ScopeChangeResult` model to [`models.py`](../src/sqlcg/server/models.py):

```python
class ScopeChangeResult(BaseModel):
    target: str
    authoritative_files: list[str]       # from find_definition
    upstream_inputs: list[str]           # direct upstream tables
    downstream_blast_radius: list[str]   # full-depth affected tables (noise-filtered)
    affected_columns: list[str]          # column-precise blast radius
    backfill_order: list[str]            # topological rebuild order
    risk_label: str
    risk_weight: int
    noise_excluded: list[str]
    truncated: bool
    hint: str | None
```

2. Add `scope_change(target: str) -> ScopeChangeResult` to [`tools.py`](../src/sqlcg/server/tools.py). The tool must call the individual tool functions **directly** (not via MCP round-trips), delegating to the already-implemented business logic:
   - `find_definition(target)` → `authoritative_files`
   - `get_change_scope(target)` → `upstream_inputs`, `downstream_blast_radius`, `affected_columns`, `risk_label`, `risk_weight`, `noise_excluded`, `truncated`
   - `get_backfill_order(target)` → `backfill_order`

   The synthesis tool does not reimplement any query logic — it only assembles and deduplicates results from the three callee tools. Set `hint` to a combined hint if any callee returned a non-empty hint.

3. The tool is decorated with `@mcp.tool()` and `@_timed_tool("scope_change")`.

**Wiring verification**:

Before opening the PR:
- `grep -n "scope_change" src/sqlcg/server/tools.py` must show the `@mcp.tool()` definition
- `grep -n "find_definition\|get_change_scope\|get_backfill_order" src/sqlcg/server/tools.py` must show call sites inside `scope_change` body (not just the function definitions)
- `grep -n "ScopeChangeResult" src/sqlcg/server/models.py` must show the model definition
- `grep -n "ScopeChangeResult" src/sqlcg/server/tools.py` must show the return type annotation
- Confirm no TODO in `scope_change` body

**Files affected**:
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `ScopeChangeResult`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — `scope_change`
- `tests/integration/test_anchor_tools.py` — scenarios below

**Tests to add**:

- **Scenario A — full synthesis round-trip**: index a 3-table fixture `raw → staged → mart` where `raw` is defined in `raw.sql`, `staged` in `staged.sql`, `mart` in `mart.sql`. Call `scope_change("raw")`. Assert all of:
  - `len(result.authoritative_files) >= 1` and `result.authoritative_files[0].endswith("raw.sql")`
  - `"staged" in result.upstream_inputs or any("staged" in t for t in result.downstream_blast_radius)`
  - `result.risk_label in ("low", "medium", "high")` (not "safe" — there are consumers)
  - `len(result.backfill_order) >= 1`
  - `result.truncated == False`
  Assertion: each field is checked independently with a descriptive failure message.

- **Scenario B — noise excluded from synthesis**: same fixture plus `staged_bck.sql`. Call `scope_change("raw")`. Assert `all("_bck" not in t for t in result.downstream_blast_radius)` and `len(result.noise_excluded) >= 1`. Assertion: specific list-membership check.

- **Scenario C — undefined table**: call `scope_change("ba.does_not_exist")`. Assert `result.authoritative_files == []`; `result.downstream_blast_radius == []`; `result.risk_label == "safe"`; `result.hint` is non-empty. Assertion: empty list and non-empty string.

**Acceptance criteria**:
- `[ ]` `scope_change` calls `find_definition`, `get_change_scope`, `get_backfill_order` internally (grep confirms)
- `[ ]` No new Cypher queries are introduced in `scope_change` (all queries delegate to callees)
- `[ ]` `ScopeChangeResult` has all seven required fields (grep on model definition)
- `[ ]` No TODO in `scope_change` body

---

### PR-07 — V-GOLDEN: Extend Golden Harness to Validate Anchor Tool Outputs

**Source**: User finding V-GOLDEN ("golden blast-radius validation set, recall-first")
**Effort**: S
**Depends on**: PR-01 (full closure), PR-02 (noise filter)
**Blocks**: NONE

**Root cause**: [`tests/e2e/test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py) scores column-level upstream lineage (physical leaves from BFS upstream). It has no mode for validating downstream blast radius at table granularity, which is the primary output of the new anchor tools. The bootstrap workflow (`scratch_bootstrap_golden.py`) populates `expected_sources` but no `expected_downstream_tables`. The harness skips cleanly when the golden file is absent (correct), but the golden YAML schema needs new optional fields.

**What to do**:

1. Extend the golden YAML schema (documented in the harness) to accept two new optional per-column fields:
   - `expected_downstream_tables: list[str]` — the expected set of tables in the blast radius (noise-filtered)
   - `expected_upstream_tables: list[str]` — the expected set of upstream tables (table rollup, not column leaves)

2. Add `_table_blast_radius(db, table_qualified: str) -> set[str]` to the harness: BFS downstream over `COLUMN_LINEAGE` from all columns of `table_qualified`, roll up to `table_qualified`, apply default backup patterns as noise filter (matching the same defaults from PR-02). Returns a `set[str]` of affected table_qualifieds.

3. Add `_upstream_tables(db, table_qualified: str) -> set[str]`: BFS upstream, roll up, filter noise.

4. In `evaluate()`, for each entry that has `expected_downstream_tables`, compute `actual_downstream` via `_table_blast_radius(db, entry["target_table"])` and score with the existing `_score()` function. For entries with `expected_upstream_tables`, score similarly.

5. In `test_golden_lineage_quality`, add enforcement for table-level blast-radius recall. **Gate table-level enforcement on a separate `table_status: curated` key, NOT the existing `status` key.** Rationale: `status` gates column-level `expected_sources` enforcement; a seed anchor can have hand-verified *table-level* upstream truth (`table_status: curated`) while its column-level sources are still `status: draft` (foundation PRs move column identities). Entries with `table_status: curated` and `expected_downstream_tables` (or `expected_upstream_tables`) must meet `RECALL_FLOOR` on the corresponding table-level score; entries without `table_status: curated` are reported but not enforced. The `ma_rotatie_totaal` seed anchor in `golden_lineage.yaml` already carries `expected_upstream_tables` and the `table_status` marker comment — flip it to `table_status: curated` when this harness change lands.

6. Update the `_format_report` output to show a separate "BLAST RADIUS" section.

7. The harness **must not re-index** — it validates a pre-built graph only. This invariant (already present) must remain.

**Wiring verification**:

Before opening the PR:
- `grep -n "_table_blast_radius\|_upstream_tables" tests/e2e/test_golden_lineage.py` must show both functions
- `grep -n "expected_downstream_tables\|expected_upstream_tables" tests/e2e/test_golden_lineage.py` must show usage in `evaluate()`
- `grep -n "index\|Indexer\|index_repo" tests/e2e/test_golden_lineage.py` must return zero results (harness must not re-index)
- Confirm no TODO in `_table_blast_radius`, `_upstream_tables`, `evaluate` additions

**Files affected**:
- [`tests/e2e/test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py) — extend with table-level scoring

**Tests to add**:

The harness itself is the test. For the PR unit test (no golden file required):

- **Scenario A — _table_blast_radius returns non-empty on fixture graph**: build a 3-table in-memory fixture `source → etl → mart`. Call `_table_blast_radius(db, "source")`. Assert `len(result) >= 1` and `"etl" in result or "mart" in result`. Assertion: specific table name.

- **Scenario B — noise filter applied inside _table_blast_radius**: `source → source_bck → mart`. Call `_table_blast_radius(db, "source")`. Assert `"source_bck" not in result`. Assertion: specific name absent from set.

- **Scenario C — harness skips cleanly without golden file**: the existing skip logic is unchanged; this scenario confirms the new code path (when `expected_downstream_tables` key is absent from an entry) does not error. Assert `evaluate()` returns a list (length >= 0) without raising when no curated downstream entries exist.

**Acceptance criteria**:
- `[ ]` `grep -n "expected_downstream_tables" tests/e2e/test_golden_lineage.py` shows usage in `evaluate()`
- `[ ]` `grep -n "index_repo\|Indexer" tests/e2e/test_golden_lineage.py` returns zero results
- `[ ]` `_table_blast_radius` applies noise filter (Scenario B passes)
- `[ ]` No TODO in any added function

---

## Test Strategy

### The Single Most Important Regression Guard

The most critical finding is F-CLOSURE: the blast radius for `ba.wtfe_verkoopinfo` is
128 tables but the default `max_depth=5` returns only a fraction of them. An LLM told
"12 affected tables" when the real count is 128 will miss 116 backfills — data corruption.

```python
# tests/unit/test_closure_depth.py

import pytest
from sqlcg.core.kuzu_backend import KuzuBackend

def _build_depth7_chain(backend: KuzuBackend) -> None:
    """Build a 7-hop column lineage chain: col_0 -> col_1 -> ... -> col_7."""
    backend.init_schema()
    # Upsert 8 column nodes and 7 COLUMN_LINEAGE edges
    for i in range(8):
        backend.upsert_node("SqlColumn", f"tbl_{i}.col", {
            "id": f"tbl_{i}.col",
            "col_name": "col",
            "table_qualified": f"tbl_{i}",
            "catalog": "", "db": "", "table_name": f"tbl_{i}",
        })
    for i in range(7):
        backend.upsert_edge(
            "SqlColumn", f"tbl_{i}.col",
            "SqlColumn", f"tbl_{i+1}.col",
            "COLUMN_LINEAGE",
            {"transform": "SELECT", "confidence": 1.0, "query_id": f"q{i}"},
        )

def test_full_closure_reaches_depth_7():
    """Full closure must traverse all 7 hops; max_depth=5 default would miss the last two.

    This is the regression guard against the v0.10.0 truncation bug where
    ba.wtfe_verkoopinfo (128 downstream tables at depth 6-7) was silently cut to
    depth 5, producing an LLM blast-radius answer that missed ~116 tables.
    """
    backend = KuzuBackend(":memory:")
    _build_depth7_chain(backend)

    # Import tools and initialize with the in-memory backend
    import sqlcg.server.tools as tools
    tools._backend = backend

    result = tools.get_downstream_dependencies("tbl_0.col")  # no max_depth — full closure

    assert len(result.nodes) == 7, (
        f"Full closure must reach depth 7 (found {len(result.nodes)} nodes). "
        "Depth-truncation regression: full closure must traverse beyond depth 5 — "
        "guards against ba.wtfe_verkoopinfo blast-radius truncation (128 vs 12 tables)"
    )
    assert result.truncated is False, (
        "truncated must be False for full-closure run — guards against false-truncation flag"
    )
```

This test:
- Uses a real in-memory KuzuBackend (real backend, real graph)
- Asserts on observable output (integer count == 7, boolean == False)
- Is NOT marked xfail
- Passes only after PR-01 is merged (fails on the current `max_depth=5` default)
- Failure message names the original regression and the DWH table

---

## Wiring Checklist

| Question | PR-01 | PR-02 | PR-03 | PR-04 | PR-05 | PR-06 | PR-07 |
|----------|-------|-------|-------|-------|-------|-------|-------|
| What calls this? | `get_downstream_dependencies`, `get_upstream_dependencies`, `trace_column_lineage` called by LLM/MCP | `NoiseFilter.from_config()` called by PR-03, PR-04, PR-05, PR-06 tools | MCP client + `scope_change` (PR-06) | MCP client + `scope_change` (PR-06) | MCP client + `scope_change` (PR-06) | MCP client directly | pytest harness only |
| Where is the callback/parameter passed? | `max_depth=None` default in function signature; BFS guard `if max_depth is not None and depth > max_depth` | `NoiseFilter.from_config(repo_root=None)` called at top of each tool function body | `FIND_DEFINITION_QUERY` passed to `db.run_read()`; `NoiseFilter.from_config()` called | `GET_TABLE_DEFINING_FILES_QUERY` and `GET_TABLE_DIRECT_UPSTREAMS_QUERY` in `db.run_read()`; `NoiseFilter.from_config()` called | `GET_COLUMNS_FOR_TABLE_QUERY` and `GET_TABLES_DEFINED_IN_FILE_QUERY` in `db.run_read()`; `NoiseFilter.from_config()` called | `find_definition(target)`, `get_change_scope(target)`, `get_backfill_order(target)` called directly (not via MCP) | `_table_blast_radius` and `_upstream_tables` called from `evaluate()` |
| What constant/path does this align with? | `KuzuConfig.from_env().db_path` unchanged; no new path introduced | `get_noise_filter_patterns(Path.cwd())` fallback matches `get_dialect(path)` and `get_schema_aliases(path)` convention in `config.py` | `FIND_DEFINITION_QUERY` constant in `queries.py` | `GET_TABLE_DEFINING_FILES_QUERY`, `GET_TABLE_DIRECT_UPSTREAMS_QUERY` in `queries.py` | `GET_COLUMNS_FOR_TABLE_QUERY`, `GET_TABLES_DEFINED_IN_FILE_QUERY` in `queries.py` | No new constants — delegates to callees | `RECALL_FLOOR` and `PRECISION_FLOOR` env vars (existing) |
| Does any TODO remain in the happy path? | No | No | No | No | No | No | No |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg` MCP tool `get_downstream_dependencies("ba.wtfe_verkoopinfo")` returns >= 100 affected nodes on a graph where `ba.wtfe_verkoopinfo` has depth-7 chains (verified manually post-index)
- `[ ]` `get_downstream_dependencies` called with no `max_depth` argument does not truncate at depth 5: the depth-7 regression guard test passes (`test_full_closure_reaches_depth_7`)
- `[ ]` `find_definition("ba.some_table")` on a correctly-indexed graph returns `definitions` with at least one entry whose `file_path` ends with the expected SQL file name
- `[ ]` `find_definition` flags `_bck` tables with `is_backup=True` rather than silently excluding them
- `[ ]` `get_change_scope("ba.wtfe_verkoopinfo")` returns `risk_label == "high"` on the production DWH graph (128 downstream tables > 20 threshold)
- `[ ]` `diff_impact(["path/to/source.sql"])` returns `changed_tables` non-empty and `backfill_order` non-empty when `source.sql` defines a table with downstream consumers
- `[ ]` `scope_change("ba.wtfe_verkoopinfo")` returns in a single call: non-empty `authoritative_files`, non-empty `downstream_blast_radius`, non-empty `backfill_order`, `risk_label == "high"`, and `noise_excluded` contains any `_bck` tables that were present in the graph
- `[ ]` `.sqlcg.toml` with `[sqlcg.noise_filter] ignore_table_patterns = ["*_staging"]` causes `get_change_scope` to exclude `*_staging` tables from `affected_tables` and report them in `noise_excluded`
- `[ ]` Golden harness `test_golden_lineage_quality` passes (skips cleanly when `SQLCG_DB_PATH` is unset; enforces recall floors when golden file is curated and graph is present)

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Kahn's algorithm on a large affected subgraph (1,000+ tables) is slow | Low | BFS for closure is already bounded by the 50k-node safety cap; Kahn's runs over the rolled-up table set (hundreds, not thousands). If measured slow, add an optional `max_order_tables=500` cap and set `truncated=True`. |
| `scope_change` cascades failures from callees | Medium | Each callee is independently tested. `scope_change` should catch individual callee exceptions, log them, and return partial results with the failure noted in `hint`. Do not let one failed sub-call blank the entire response. |
| KuzuDB `MATCH … STARTS WITH` is O(N) on large graphs without index | Low | All anchor queries filter on `PRIMARY KEY` properties (`qualified`, `id`, `path`). KuzuDB indexes primary keys. No unindexed table scans introduced. |
| `noise_filter.py` `from_config()` called once per tool call is redundant | Low | Cost is one `Path.cwd()` + one `tomllib.load()` per call — microseconds. If profiling shows it matters, cache with `functools.lru_cache` keyed on `str(repo_root)`. Do not pre-optimize. |
| golden harness new modes fail silently when golden YAML has no `expected_downstream_tables` key | Low | Guard: `entry.get("expected_downstream_tables")` returns `None`; skip scoring for that entry. This is the same pattern as `entry.get("expected_sources")`. Scenario C in PR-07 confirms this. |


❯ and will we create the gold standard dataset before or after?

● Both — but split deliberately, because there's a trap in doing the full gold standard before the
  foundation lands.

  The trap: the bootstrap draft is currently circular (all 1.00) — it's "what the engine says," which
  includes the very bugs we're fixing (depth-5 truncation, _bck noise, ba.x vs ia_analytics.ba_x split
  identity). And F-HYGIENE will rewrite node identities (alias unification) and F-CLOSURE will change
  what's reachable. So if you hand-curate 15–20 anchors now, you're curating against identities and a
  closure depth that the foundation PRs will move out from under you → rework.

  But doing it entirely after is the "trust me it works" failure you've been burned by — the foundation
  PRs (does closure really recover the depth-6/7 tables? does hygiene really drop the backups?) would
  have no objective acceptance target.

  So:

  Before the features — a small hand-verified SEED (3–5 anchors). Curated from the SQL / your domain
  knowledge, not from current output. This is the acceptance target the foundation PRs must hit, e.g.:
  - "ba.wtfe_verkoopinfo blast radius must include the depth-6/7 tables" → proves F-CLOSURE.
  - "blast radius must not include any _bck table" → proves F-HYGIENE.
  - "ma_rotatie_totaal upstream = {ma_aantal, sn_artikel_s1}" → the anchor we already reasoned out.

  We can knock the seed out fast: I draft the proposed truth by reading each anchor's SQL, you
  confirm/correct.

  After the foundation stabilizes (post F-CLOSURE + F-HYGIENE) — finalize the full 15–20 anchor set.
  Now node identity and closure semantics are fixed, so curation sticks, and the A-* anchor PRs get
  validated against it with recall floors.

  Re: the merge — this is independent and doesn't block it. The harness is committable and already in
  the set going to master; the gold data (golden_lineage.yaml) is gitignored and lives locally
  regardless of timing. So: merge first (once the planner lands), build the seed alongside the
  foundation PRs, full curation after.

  Net sequencing: merge → seed (with foundation as its first customer) → foundation PRs → full gold
  standard → anchor PRs. Want me to fold "build the seed" in as an explicit early step when I spawn the
  developers?

✻ Waiting for 1 background agent to finish

❯ y

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      echo "=== latest commits (did plan land?) ==="; git log --oneline -4…)
  ⎿  === latest commits (did plan land?) ===
     4e703e0 chore(checkpoint): perf scaling guard + branch WIP, fix lint/tests
     d8bbf35 fix: restore schema_aliases and _apply_table_alias accidentally removed by developer
     … +11 lines (ctrl+o to expand)

● Done — the seed is now an explicit Step 0 in the spawn sequence: right after the merge, before any
  foundation work, and it doubles as F-CLOSURE's and F-HYGIENE's acceptance target. So the order is:

  Step 0 — Seed gold standard (3–5 hand-verified anchors; I draft proposed truth from the SQL, you
  confirm) →
  Foundation PRs (F-CLOSURE, F-HYGIENE — must satisfy the seed) →
  Full gold standard (15–20 anchors, once identity/closure are stable) →
  Anchor PRs (A-*, validated against it with recall floors).

  The sprint-planner is still running (HEAD is still 4e703e0, no plan commit yet), so I'm holding the
  merge to avoid the index.lock race and to make sure the plan lands on master too.

  When it finishes, I'll: commit the golden-set files → fast-forward master (clean, local-only unless
  you tell me to push) → then spawn the developers per the plan's schedule, leading with the Step 0
  seed.

  I'll get pinged automatically when the planner completes — no need to poll. Want me to get a head
  start on the seed draft now (read the ma_rotatie_totaal / wtfe_verkoopinfo defining SQL and propose
  their expected blast-radius + upstream truth for your confirmation) while the planner finishes? It's
  non-conflicting work.

✻ Waiting for 1 background agent to finish

❯ y

● On it. The seed must be ground truth from the SQL, not from the (buggy) graph — so let me use the
  graph to locate the defining files, then read the actual SQL to verify what each column really
  derives from.

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python -c "…)
  ⎿      result = self._conn.execute(query, params)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
         query_result_internal = self._connection.execute(prepared_statement._prepared_statement,
     … +5 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Node labels differ — let me get the actual schema:

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python -c "…)
  ⎿  No matches found
  ⎿  Allowed by auto mode classifier

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python -c "…)
  ⎿  Traceback (most recent call last):
       File "<string>", line 4, in <module>
     TypeError: KuzuBackend.run_read() missing 1 required positional argument: 'params'
  ⎿  Allowed by auto mode classifier

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python -c "…)
  ⎿  REL | SELECTS_FROM
     REL | STAR_SOURCE
     REL | QUERY_DEFINED_IN
     … +14 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Got the schema (File, SqlTable, SqlQuery, DEFINED_IN, QUERY_DEFINED_IN, INSERTS_INTO). Let me locate
  the defining files for the seed anchors:

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      SQLCG_DB_PATH=/tmp/sqlcg_timing/graph.db uv run python -c "…)
  ⎿  ### ba.wtfa_kpi_rotatie_bouwmarkt
        DEFINED_IN     : ['/home/ignwrad/Projects/dwh/ddl/changelogs/BA-TABLES/WTFA_KPI_ROTATIE_BO
     UWMARKT.sql']
     … +22 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● The graph points me to the real compute: etl/sql/fact/wtfa_kpi_rotatie_bouwmarkt.sql. Let me read the
  actual SQL to establish ground truth for ma_rotatie_totaal (and note wtfv_voorraad_dagstand has no
  DEFINED_IN — a gap worth flagging):

● Bash(cd /home/ignwrad/Projects/dwh
      f=etl/sql/fact/wtfa_kpi_rotatie_bouwmarkt.sql…)
  ⎿  === file size / line count ===
     348 etl/sql/fact/wtfa_kpi_rotatie_bouwmarkt.sql
     === how ma_rotatie_totaal is computed (context) ===
     … +48 lines (ctrl+o to expand)
  ⎿  Shell cwd was reset to /home/ignwrad/Projects/sql-code-graph

● I read the actual ETL and now have SQL-verified ground truth for the first anchor. Here's the compute
  (etl/sql/fact/wtfa_kpi_rotatie_bouwmarkt.sql):

  ma_rotatie_totaal = NVL(rotatie_totaal, 0)
  rotatie_totaal    = afzet_totaal_meetperiode / NULLIFZERO(dagen_null_facing_meetperiode / 7)
                      └─ afzet  CTE  ← FROM ba.wtfe_verkoopinfo      (sales → numerator)
                      └─ voorraad CTE ← JOIN ba.wtfv_voorraad_dagstand (stock → null-facing days,
  denominator)

  So the value of ma_rotatie_totaal is sales ÷ stock-days — exactly the business intuition. The graph's
  direct-mode answer (wtfe_verkoopinfo + wtfv_voorraad_dagstand) is correct at table level. ✓

  Proposed seed (draft — your confirmation needed on the judgment calls)

  ┌─────┬─────────────────────────────────────────────────┬─────────────────────────┬──────────────┐
  │  #  │                     Anchor                      │        Assertion        │  Source /    │
  │     │                                                 │                         │     type     │
  ├─────┼─────────────────────────────────────────────────┼─────────────────────────┼──────────────┤
  │     │                                                 │ upstream value-sources  │ SQL-verified │
  │ 1   │ ba.wtfa_kpi_rotatie_bouwmarkt.ma_rotatie_totaal │ = {wtfe_verkoopinfo,    │  (truth)     │
  │     │                                                 │ wtfv_voorraad_dagstand} │              │
  ├─────┼─────────────────────────────────────────────────┼─────────────────────────┼──────────────┤
  │     │                                                 │ downstream blast radius │ proves       │
  │ 2   │ ba.wtfe_verkoopinfo                             │  must include tables at │ F-CLOSURE    │
  │     │                                                 │  depth ≥6 (the 128-set) │              │
  ├─────┼─────────────────────────────────────────────────┼─────────────────────────┼──────────────┤
  │     │                                                 │ blast radius must       │ proves       │
  │ 3   │ (any anchor)                                    │ contain zero *_bck*     │ F-HYGIENE    │
  │     │                                                 │ tables                  │              │
  ├─────┼─────────────────────────────────────────────────┼─────────────────────────┼──────────────┤
  │     │                                                 │ must resolve a defining │              │
  │ 4   │ ba.wtfv_voorraad_dagstand                       │  file (currently        │ proves       │
  │     │                                                 │ DEFINED_IN is empty —   │ A-DEFINE     │
  │     │                                                 │ graph gap)              │              │
  └─────┴─────────────────────────────────────────────────┴─────────────────────────┴──────────────┘

  Two judgment calls I need you on (the domain part bootstrap can't decide):

  1. For #1, the value of ma_rotatie_totaal depends on wtfe_verkoopinfo + wtfv_voorraad_dagstand. But
  the ETL also joins ba.wtfs_artikel_wissel (article-replacement remapping) and ba.wtda_datum (date
  grain). For backfill purposes, do those count as upstream triggers (a change to wtfs_artikel_wissel →
  recompute rotatie)? Or only the two value-sources? This is exactly the column-precise-vs-table
  question, made concrete.
  2. A real finding worth your eye: ba.wtfv_voorraad_dagstand has no DEFINED_IN in the graph — its DDL
  is via a clone template (maak_clone_voorraad_tabellen.sql), so A-DEFINE would currently fail to
  locate a core source table. Is that table created by a template/clone in your world (i.e. expected),
  or should it have a real DDL?

  Planner's still running — I'll fold whatever you decide into the seed. Which way on the
  backfill-trigger question: value-sources only, or all join inputs?

✻ Waiting for 1 background agent to finish

● Agent "Plan blast-radius anchor sprint" completed · 6m 24s

● Planner's done — plan/sprint_10_anchor_tools.md is committed. Clean breakdown:

  ┌────────────────┬──────────────────────────────────────────────────┬────────────────────────────┐
  │     Phase      │                Agents (parallel)                 │            PRs             │
  ├────────────────┼──────────────────────────────────────────────────┼────────────────────────────┤
  │ 1 — foundation │ A: PR-01 F-CLOSURE · B: PR-02 F-HYGIENE          │ disjoint files ✓           │
  ├────────────────┼──────────────────────────────────────────────────┼────────────────────────────┤
  │ 2 — track A    │ PR-04 (scope+risk) → PR-05 (backfill+diff)       │ share tools.py, sequential │
  ├────────────────┼──────────────────────────────────────────────────┼────────────────────────────┤
  │ 2 — track B    │ PR-03 (find_definition) → PR-07 (golden harness) │                            │
  ├────────────────┼──────────────────────────────────────────────────┼────────────────────────────┤
  │ 3 — synthesis  │ PR-06 scope_change (after all)                   │ last                       │
  └────────────────┴──────────────────────────────────────────────────┴────────────────────────────┘
  └────────────────┴──────────────────────────────────────────────────┴────────────────────────────┘

  It also locked the design decision we discussed: column-precise traversal, rolled up to table — every
  tool returns both affected_columns and affected_tables.

  The planner's commit landed, so the race is over and I can merge. Let me lint the golden files,
  commit them, and fast-forward master (local-only — you haven't said push):

● Bash(cd /home/ignwrad/Projects/sql-code-graph
      echo "=== planner commit present? ==="; git log --oneline -2…)
  ⎿  === planner commit present? ===
     f312cb3 chore(plan): sprint-10 anchor tools for LLM DWH navigation
     4e703e0 chore(checkpoint): perf scaling guard + branch WIP, fix lint/tests
     … +12 lines (ctrl+o to expand)
  ⎿  You've hit your session limit · resets 5:40pm (Europe/Paris)

✻ Worked for 9m 8s

───────────────────────────────────────────────────────────────────────────────────────────────────────
❯ 1. true all three of those affect the value for ma_rotatie_totaal, so if those change it also
  affects all the upstream tables (tables that use ma_rotatie_totaal), 2. preferably the "real" ddl
  but i don't want to create a hacky way around our sloppy dwh repo. and continue the work :)
