# Sprint Plan: sprint_13 — v1.1.0 Cluster B: Lineage Provenance & Output Trust

**Plan date**: 2026-05-31
**Author**: sprint-planner
**Source authority**: [`plan/v1.1.0_cluster_b_provenance_trust.md`](v1.1.0_cluster_b_provenance_trust.md) (architect-planner, 2026-05-31)
**Issues**: [#31](https://github.com/Warhorze/sql-code-graph/issues/31) (source location on edges),
[#32](https://github.com/Warhorze/sql-code-graph/issues/32) (meaningful confidence),
[#33](https://github.com/Warhorze/sql-code-graph/issues/33) (CLI/MCP parity + node-kind tagging)
**Branch**: serialise all three PRs on one branch (`feat/cluster-b-provenance`). `server/models.py`
and `server/tools.py` are touched by all three PRs; do NOT parallelise.
**Policy**: No TODO in any happy path. Every new method needs a grep-confirmed call site before PR
opens. Tests assert observable output. `SCHEMA_VERSION` bump is owned by PR-2; PR-3 rides on it.

---

## Summary

Make `trace_column_lineage` and CLI `analyze` outputs trustworthy end-to-end:

- **PR-1 (#32)**: Replace the blanket `0.7` confidence with `1.0` for plainly-parsed fact edges.
  Attach `reason` in the MCP layer for all inferred (`< 1.0`) edges. Fix `skill.py` self-contradiction.
- **PR-2 (#31)**: Persist per-statement start line (`start_line INT64` on `SqlQuery`). Bump
  `SCHEMA_VERSION "4" → "5"`. Expose `file`/`line`/`expression` in MCP trace output and CLI.
- **PR-3 (#33)**: Tag every `SqlTable` node with a structural role (`table`/`cte`/`derived`/`external`).
  Exclude `cte`/`derived` from `upstream`/`impact` by default. Bring `impact`/`unused` to
  `NoiseFilter` + de-dup + `--raw` parity with `upstream`/`downstream`. Drop the dead `STALE_VIEWS`
  query (see Code-vs-Plan Verification for the resolution of the architect-planner's Blocking Question).

---

## Scope

### In Scope

- `base.py:500` `confidence=0.7 → 1.0`
- `LineageNode` model gains `line`, `expression`, `reason` fields
- `SqlQuery` DDL gains `start_line INT64`; `SCHEMA_VERSION` bumps `"4" → "5"`
- `TRACE_COLUMN_LINEAGE` query extended with `OPTIONAL MATCH (q:SqlQuery)` join
- `tools.py` trace loops populate `file`/`line`/`expression`/`reason`; MCP `kind` filter on `upstream`
- `skill.py` `_BOUNDARY` documentation corrected
- `analyze impact` and `unused` gain `NoiseFilter.from_config()` + de-dup + `--raw`
- `SqlTable.kind` repurposed to `{table, cte, derived, external}` enum (no new column)
- `STALE_VIEWS` query dropped; `reindex_file` stale-view cascade replaced with correct signal
- `queries.cypher` kind-filter variants for `upstream`/`impact` + `--include-intermediate` flag

### Non-Goals

- Per-projection (sub-statement) line numbers (v1.2)
- `external` kind from a catalog allowlist (v1.2)
- Calibrated confidence with measured false-positive rates (v1.2)
- Clickable terminal hyperlinks in MCP transport (v1.1.0 returns fields as data only)
- Windows IPC (v1.2 from sprint_12)

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| `confidence=0.7` dominant path is `base.py:500` inside `_lineage_node_to_edges._walk`, NOT `_extract_column_lineage` | **Confirmed.** `grep -n "confidence=0.7" src/sqlcg/parsers/base.py` → line 500 only. The `_extract_column_lineage` column loop contains no confidence assignment at that line. Other values `0.8` (indexer.py:1058 star expansion), `0.5` (base.py:882 schema miss), `0.3` (snowflake_parser.py:275 scripting), `0.0` (base.py:939 failure) are correct and untouched. |
| `tools.py` hardcodes `file=None` at lines 563 and 598 | **Confirmed.** Both primary loop (line 563) and bare-fallback loop (line 598) hardcode `file=None`. `LineageNode.file` field exists but is never populated. |
| `LineageNode` has no `line`, `expression`, or `reason` fields | **Confirmed.** [`models.py:49-58`](../src/sqlcg/server/models.py): only `name/kind/table/file/confidence`. |
| `SqlQuery` DDL has no `start_line` | **Confirmed.** [`schema.cypher:38-48`](../src/sqlcg/core/schema.cypher): nine fields, no `start_line`. `SCHEMA_VERSION = "4"` at [`schema.py:6`](../src/sqlcg/core/schema.py). |
| `query_id` stored on `COLUMN_LINEAGE` edges; `SqlQuery` stores `file_path` + `sql` | **Confirmed.** `schema.cypher:96` shows `query_id STRING` on `COLUMN_LINEAGE`. `indexer.py:975` assigns `query_id = f"{parsed.path_str}:{i}"`; `indexer.py:980` stores `file_path`; `indexer.py:982` stores `sql[:500]`. (Plan previously cited 979-981 — corrected: 975/980/982.) |
| `upstream`/`downstream` already apply `NoiseFilter.from_config()` | **Confirmed.** `analyze.py:57-62, 101-106`. |
| `impact` (analyze.py:109-121) and `unused` (analyze.py:148-159) have no `NoiseFilter`, no de-dup, no `--raw` | **Confirmed.** Both are single `run_read` + `_print_table`; no filter call, no `raw` parameter. |
| `SqlTable.kind == "TABLE"` hardcoded at all four emission sites | **Confirmed.** `indexer.py:933` (defined-table row), `999` (source-table row), `1048` (star-source row), `1072` (target table row). `indexer.py:982` writes `stmt.kind` for `SqlQuery` rows (correct — that is `QueryNode.kind`). |
| `STALE_VIEWS` query matches `{kind: 'VIEW'}` which is never written | **Confirmed.** `queries.cypher:16-19` matches `v:SqlTable {kind: 'VIEW'}`. None of the four `SqlTable` emission sites in `indexer.py` write `kind: "VIEW"` — they all write `"TABLE"`. Result: `STALE_VIEWS` always returns zero rows. `_reindex_view_definition` (indexer.py:1137) is dead in practice. |
| No Python consumer branches on `SqlTable.kind` string values (beyond read-and-display) | **Confirmed.** `grep -rn "\.kind" src/sqlcg/cli/commands/find.py` → lines 24, 35 read and print `t.kind`; `analyze.py:121` reads and prints `q.kind` (that is `SqlQuery.kind`, not `SqlTable.kind`); `tools.py:661, 710` populate `kind=row.get("kind")` into `TableUsage`/`DefinitionFile` models — display only, no branch. The only branch on `SqlTable.kind` is `STALE_VIEWS` in Cypher (dead). Repurposing `kind` to the structural-role enum is safe. |
| `_extract_column_lineage` hot path untouched by any PR-1 change | **Confirmed.** `base.py:500` is inside `_lineage_node_to_edges._walk`, a helper called by `_extract_column_lineage` but structurally outside the per-column qualify/scope loop. The single `confidence=0.7 → 1.0` change adds zero ops. |
| Snowflake parser statement-loop structure (PR-2 open question resolved) | **Resolved, with a caveat — NOT fully "free".** `SnowflakeParser.parse_file` (line 85) delegates to `AnsiParser.parse_file` for non-scripting files, so it runs the `_compute_start_lines` loop — BUT it delegates with the **preprocessed** SQL (`_preprocess_snowflake_sql`, line 77), which strips line-spanning `UNPIVOT`/`WITH TAG` clauses and **deletes entire `ALTER … SET TAG;` statements** (Gap 4b, line 146). Tokenizing the preprocessed text shifts line numbers and desyncs the `stmt_index→start_lines[]` alignment → wrong `start_line` for Snowflake. PR-2 must compute the line map from the **original** SQL (see PR-2 step 5). `_parse_scripting_file` (lines 224–311) has its own regex DML extraction loop; it loses original file line positions and correctly emits `start_line=0` (the default sentinel). PR-2 must add `row.get("line") or None` in `tools.py` to convert the `0` sentinel to `None`. |
| `TableRef` is `frozen=True`; `role` field addition compatibility | **Confirmed safe.** `TableRef` is `@dataclass(frozen=True)`. All current fields have defaults (`catalog: str | None = None`, `db: str | None = None`, `name: str = ""`, `alias: str | None = None`). Adding `role: str = "table"` as a new defaulted field is valid; frozen only prevents mutation of instances, not class-level field additions. Existing `TableRef(name=...)` call sites continue to work unchanged. |

---

## Blocking Question Resolution: `SqlTable.kind` — repurpose vs add column

**Grep evidence** (run against `src/`):

```
grep -rn "SqlTable.*kind\|kind.*SqlTable" src/ → no hits in Python
grep -rn "\"TABLE\"\|'TABLE'" src/sqlcg/ --include="*.py" | grep kind → indexer.py:933,999,1048,1072 (write sites only)
grep -n "kind" src/sqlcg/core/queries.cypher → line 18: STALE_VIEWS {kind:'VIEW'} — dead
                                                  lines 76,80: FIND_DEFINITION, GET_TABLE_DEFINING_FILES — read-only display
```

**Decision**: **Repurpose `SqlTable.kind` to the structural-role enum `{table, cte, derived, external}`.**
No new DDL column is needed. The `STALE_VIEWS` query is dead (zero rows always returned because `kind:'VIEW'`
is never written). **Drop `STALE_VIEWS`** from `queries.cypher` and `queries.py`. Replace the stale-view
cascade in `reindex_file` with a correct signal: query `SqlQuery.kind IN ('CREATE_VIEW', 'CREATE_VIEW_AS')`
joined to the tables that file's views `SELECTS_FROM`, or — simpler and safe given the no-backward-compat
policy — just remove the cascade entirely and rely on the full re-index path (`db reset && sqlcg index`).
The cascade was already a no-op. This is a PR-3 change (kind tagging PR owns the drop).

---

## Ticket Table

| ID | Title | Files | Effort | Priority | Depends on | Blocks |
|----|-------|-------|--------|----------|------------|--------|
| PR-1 | #32 — Meaningful confidence + `reason` | `base.py`, `models.py`, `tools.py`, `skill.py` | S | HIGH | INDEPENDENT | PR-2, PR-3 (touch same files) |
| PR-2 | #31 — Source location (file/line/expression) | `schema.py`, `schema.cypher`, `base.py`, `ansi_parser.py`, `snowflake_parser.py`, `indexer.py`, `queries.cypher`, `models.py`, `tools.py`, `analyze.py` | M | HIGH | PR-1 merged | PR-3 |
| PR-3 | #33 — CLI/MCP parity + node-kind tagging | `base.py`, `indexer.py`, `queries.cypher`, `queries.py`, `analyze.py`, `tools.py` | M | MED-HIGH | PR-2 merged | NONE |

---

## Recommended Implementation Order

### Why this order

**PR-1 first**: it is fully independent (no schema change, no new persisted field), LOW risk (one
value change + two model fields + doc), and its `reason` attachment in `tools.py` is a prerequisite
for the trace loops that PR-2 also edits. Shipping PR-1 first shrinks the diff surface for PR-2's
trace-loop changes — the reviewer sees a clean baseline. Concretely: PR-2 adds `file`/`line`/
`expression` population to the same `LineageNode(...)` constructor calls that PR-1 touches; sequential
merges avoid a rebase conflict at those exact lines.

**PR-2 second**: it owns the one schema bump (`"4" → "5"`) for this entire cluster. PR-3 rides on the
v5 schema (uses `start_line` indirectly and avoids a second forced re-index). PR-2 must be merged and
the v5 graph available before PR-3's kind-filter Cypher queries can be tested against a real graph.
PR-2 is also the highest single-PR risk (new persisted field + query join) — it should land on a
PR-1 baseline, not alongside PR-3's indexer changes.

**PR-3 last**: it depends on PR-2's schema being live (no separate bump needed), and it touches the
indexer emission sites that PR-2 also touches (`indexer.py` query rows, `base.py` `TableRef`
construction). Shipping it after PR-2 keeps each diff reviewable. PR-3 also drops `STALE_VIEWS`,
which is dead code untouched by PR-1 and PR-2 — no risk to ship last.

**No parallelism**: `server/models.py` and `server/tools.py` (trace loops) are edited by all three
PRs. `base.py` and `indexer.py` are edited by PR-1, PR-2, and PR-3. Parallel branches would produce
merge conflicts in the trace loop constructors. Serialise on one branch.

### Single-developer sequence

1. PR-1 — `confidence=0.7→1.0`, `reason` in MCP layer, `skill.py` doc fix
2. PR-2 — `start_line` schema bump v5, persist, trace query join, `file/line/expression` in output
3. PR-3 — kind tagging, STALE_VIEWS drop, kind-filter queries, `impact`/`unused` parity

---

## Ticket Specifications

---

### PR-1 — #32: Meaningful Confidence + `reason`

**Source**: [`plan/v1.1.0_cluster_b_provenance_trust.md`](v1.1.0_cluster_b_provenance_trust.md) Phase 1 (LOW risk)
**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: PR-2 (same `models.py`/`tools.py` lines; merge first to reduce diff surface)

**Root cause**: `_lineage_node_to_edges._walk` at [`base.py:500`](../src/sqlcg/parsers/base.py)
emits `confidence=0.7` for every plainly-parsed `SELECT col AS x` edge. The value `0.7` is not a
measured probability; it actively misleads callers who rely on it as a quality signal. The four
genuinely inferred values (`0.8` star-expansion, `0.5` schema-miss, `0.3` scripting, `0.0` failure)
are already correct. `LineageNode` carries no `reason` field, so inferred edges are indistinguishable
from fact edges in MCP output. `skill.py:46` claims "every other tool returns facts" while trace
returns 0.70 on everything — a direct self-contradiction.

**What to do**:

1. **`base.py:500`** — change the single line:
   ```python
   # Before
   confidence=0.7,
   # After
   confidence=1.0,
   ```
   This is inside `_lineage_node_to_edges._walk`, NOT inside the `_extract_column_lineage` column
   loop. Zero new ops added.

2. **`models.py`** — add two fields to `LineageNode` (after `confidence`):
   ```python
   line: int | None = Field(None, description="1-based start line of the producing statement")
   expression: str | None = Field(None, description="SQL text of the producing statement (truncated)")
   reason: str | None = Field(None, description="Set only when confidence < 1.0; why the edge is inferred")
   ```
   `line` and `expression` are added here so PR-2 only needs to wire them from the query result —
   no second model change. `reason` is PR-1's deliverable.

3. **`tools.py`** — add a `_reason_for` helper and call it in both trace loops (primary loop ~line 558
   and bare-fallback loop ~line 593):
   ```python
   _REASON_MAP: dict[tuple[str | None, float | None], str] = {
       ("STAR_EXPANSION", 0.8): "star-expansion: columns inferred from source table schema",
       ("UNKNOWN", 0.5): "column not found in resolved schema",
       (None, 0.3): "scripting-block fallback: column lineage approximate",
       ("UNKNOWN", 0.0): "lineage extraction failed at index time",
   }

   def _reason_for(transform: str | None, confidence: float | None) -> str | None:
       if confidence is None or confidence >= 1.0:
           return None
       return _REASON_MAP.get((transform, confidence)) or f"inferred edge (confidence={confidence})"
   ```
   In both `LineageNode(...)` constructors, add:
   ```python
   reason=_reason_for(row.get("transform"), row.get("confidence")),
   ```
   Leave `line=None` and `expression=None` for now — PR-2 fills them in.

4. **`skill.py`** — replace the self-contradictory sentence in `_BOUNDARY`:
   ```python
   # Before (line 46)
   "Only `get_change_scope`/`scope_change` (`risk`) and `analyze_unused` \n"
   "(`dead_code`, confidence 0.5) are heuristics; every other tool returns facts."
   # After
   "Only `get_change_scope`/`scope_change` (`risk`) and `analyze_unused` \n"
   "(`dead_code`, confidence 0.5) are heuristics. `trace_column_lineage` returns \n"
   "deterministic fact edges (`confidence=1.0`, `reason=None`) for plainly-parsed \n"
   "SELECT statements; `confidence<1.0` with a non-null `reason` flags an inferred \n"
   "edge — surface `reason` and treat with caution."
   ```

**Wiring verification**:

Before opening the PR, run:
- `grep -n "confidence=0.7" src/sqlcg/parsers/base.py` — must return zero results after the fix
- `grep -n "confidence=1.0" src/sqlcg/parsers/base.py` — must show the updated line 500
- `grep -n "reason" src/sqlcg/server/models.py` — must show the new `reason` field on `LineageNode`
- `grep -n "_reason_for\|reason=" src/sqlcg/server/tools.py` — must show the helper definition AND
  two call sites (one in each trace loop)
- `grep -n "confidence < 1.0\|inferred edge\|confidence=1.0" src/sqlcg/server/skill.py` — must
  show the updated `_BOUNDARY` text
- Confirm `_reason_for` is called (not just defined): `grep -n "_reason_for(" src/sqlcg/server/tools.py`
  must return at least two hits (primary loop + bare-fallback loop)
- Confirm no TODO in `_lineage_node_to_edges._walk`: `grep -n "TODO" src/sqlcg/parsers/base.py | grep -A2 -B2 "_walk"`

**Files affected**:
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — line 500 value change
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `LineageNode` gains `line`, `expression`, `reason`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — `_reason_for` helper + two call sites
- [`src/sqlcg/server/skill.py`](../src/sqlcg/server/skill.py) — `_BOUNDARY` doc correction

**Tests to add**:

- **Scenario A — fact edge confidence**: index a fixture with `SELECT a AS x FROM t`; call
  `trace_column_lineage`; assert the returned `LineageNode` has `confidence == 1.0` AND `reason is None`.
- **Scenario B — star-expansion edge**: index a fixture with `SELECT * FROM t` (+ a DDL for `t`);
  call `trace_column_lineage`; assert a returned node has `confidence == 0.8` AND `reason ==
  "star-expansion: columns inferred from source table schema"`.
- **Scenario C — schema-miss edge**: index a fixture where the source table has no DDL columns;
  assert a returned node has `confidence == 0.5` AND `reason == "column not found in resolved schema"`.
- **Scenario D — `_BOUNDARY` self-consistency**: read `skill._BOUNDARY`; assert it contains the
  substring `"confidence=1.0"` and does NOT contain `"every other tool returns facts"`.

**Acceptance criteria**:
- `[ ]` `grep -n "confidence=0.7" src/sqlcg/parsers/base.py` returns zero results
- `[ ]` A plainly-parsed `SELECT col AS x` edge has `confidence == 1.0` and `reason is None` in
  the MCP `trace_column_lineage` output (integration test Scenario A)
- `[ ]` A STAR_EXPANSION edge has `confidence == 0.8` and a non-empty `reason` (Scenario B)
- `[ ]` `grep -n "_reason_for(" src/sqlcg/server/tools.py` returns at least two hits
- `[ ]` `skill._BOUNDARY` contains `"confidence=1.0"` and does not claim all non-heuristic tools
  return flat-confidence facts (Scenario D)
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`
  pass unchanged (no op added to hot paths)

---

### PR-2 — #31: Source Location (file / line / expression)

**Source**: [`plan/v1.1.0_cluster_b_provenance_trust.md`](v1.1.0_cluster_b_provenance_trust.md) Phase 2 (MED risk)
**Effort**: M
**Depends on**: PR-1 merged (same `models.py` `LineageNode` + same `tools.py` trace loop constructors)
**Blocks**: PR-3 (PR-3 rides on the v5 schema bump; a second bump would force a second re-index)

**Root cause**: Every `COLUMN_LINEAGE` edge already stores `query_id = "{file_path}:{stmt_index}"`
([`indexer.py:1035`](../src/sqlcg/indexer/indexer.py)) and the `SqlQuery` node stores `file_path`
and `sql` ([`indexer.py:979-981`](../src/sqlcg/indexer/indexer.py)), so `file` and `expression` are
a join away. Only `start_line` (the 1-based start line of the producing statement) is never persisted.
The `TRACE_COLUMN_LINEAGE` query does not join `SqlQuery`, so `tools.py` hardcodes `file=None` at
lines 563 and 598. The `SqlQuery` DDL has no `start_line` column. `SCHEMA_VERSION` is `"4"`.

sqlglot expression nodes have empty `.meta`; line positions live only on tokenizer tokens. The correct
unit is the statement start line, computed once per file by grouping tokens on `SEMICOLON` and taking
the first token's `.line` per group. This is O(tokens) per file, done **once before** the statement
loop — never inside `_extract_column_lineage`.

**What to do**:

1. **`schema.py:6`** — bump `SCHEMA_VERSION`:
   ```python
   # Before
   SCHEMA_VERSION = "4"
   # After
   SCHEMA_VERSION = "5"
   ```

2. **`schema.cypher`** — add `start_line INT64` to `SqlQuery`:
   ```
   CREATE NODE TABLE SqlQuery (
       id STRING PRIMARY KEY,
       file_path STRING,
       statement_index INT64,
       sql STRING,
       kind STRING,
       target_table STRING,
       parse_failed BOOLEAN,
       confidence FLOAT,
       parsing_mode STRING,
       start_line INT64
   );
   ```

3. **`base.py`** — add `start_line: int = 0` to `QueryNode`:
   ```python
   @dataclass
   class QueryNode:
       ...
       defined_body: Any | None = None
       start_line: int = 0    # NEW: 1-based start line of statement in file; 0 = unknown
   ```

4. **`ansi_parser.py`** — compute start lines **once per file** before the statement loop in
   `parse_file`. Add a static helper `_compute_start_lines(sql: str) -> list[int]` that tokenizes
   the SQL and groups on `SEMICOLON` token type, returning one 1-based line number per statement:
   ```python
   @staticmethod
   def _compute_start_lines(sql: str) -> list[int]:
       """Compute 1-based start line for each semicolon-delimited statement.

       Uses the sqlglot tokenizer. Groups tokens by SEMICOLON boundaries and
       returns the .line of the first token in each group. O(tokens), called
       once per file before the statement loop.
       """
       from sqlglot.tokens import Tokenizer, TokenType
       tokens = Tokenizer().tokenize(sql)
       lines: list[int] = []
       group_start: int | None = None
       for tok in tokens:
           if group_start is None and tok.token_type != TokenType.SEMICOLON:
               group_start = tok.line
           elif tok.token_type == TokenType.SEMICOLON:
               if group_start is not None:
                   lines.append(group_start)
               group_start = None
       if group_start is not None:  # last statement with no trailing semicolon
           lines.append(group_start)
       return lines
   ```
   In `parse_file`, call this **once** before the `for stmt_index, stmt in enumerate(statements)`
   loop, then pass `start_lines[stmt_index]` to `QueryNode` via `_parse_single_statement` (or
   set it on the returned `QueryNode` directly after the call):
   ```python
   start_lines = self._compute_start_lines(sql)
   for stmt_index, stmt in enumerate(statements):
       ...
       query_node = self._parse_single_statement(...)
       query_node.start_line = start_lines[stmt_index] if stmt_index < len(start_lines) else 0
       out.statements.append(query_node)
   ```

5. **Snowflake override** — [`snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py) has
   TWO code paths; both have been verified against the actual source:

   - **Non-scripting path** (`snowflake_parser.py:85`): `SnowflakeParser.parse_file` calls
     `AnsiParser.parse_file(self, path, sql, dependency_filter=dependency_filter)` directly after
     preprocessing. This path runs through `AnsiParser.parse_file`'s statement loop and therefore
     gets `_compute_start_lines` **for free** — no change required in `snowflake_parser.py` for
     this path.

     **⚠️ Caveat — preprocessing mutates the SQL before delegation (NOT free as written).**
     `SnowflakeParser.parse_file` calls `_preprocess_snowflake_sql(sql)` (`snowflake_parser.py:77`)
     **before** delegating, and the `sql` AnsiParser receives is the *mutated* text. The
     preprocessing (verified against `snowflake_parser.py:88-160`):
     - strips `UNPIVOT (...)` clauses (Gap 3) — can span lines, shifting every subsequent line number;
     - strips `WITH TAG (...)` clauses (Gap 4) — can span lines;
     - **removes entire `ALTER … MODIFY COLUMN … SET TAG …;` statements** (Gap 4b,
       `snowflake_parser.py:146`) — this drops whole statements, so the post-preprocess statement
       count differs from the original file.

     Consequences if `_compute_start_lines` tokenizes the preprocessed `sql` and maps positionally by
     `stmt_index`: (a) reported lines are **shifted** relative to the real file, and (b) when a
     statement is removed by Gap 4b, the `stmt_index → start_lines[]` alignment **desyncs**, so a
     persisted `start_line` can point at the *wrong* statement. For Snowflake DWH files (the large-repo
     target), `SET TAG` DDL is common — this is a realistic wrong-line bug, not a corner case.

     **Required fix for PR-2** (do NOT ship the "for free" wiring for Snowflake): compute the line map
     from the **original, pre-preprocessing SQL**. Either compute `start_lines = self._compute_start_lines(sql)`
     in `SnowflakeParser.parse_file` *before* `_preprocess_snowflake_sql`, then apply it to the
     returned `QueryNode`s by aligning on a stable key (e.g. statement target/kind) rather than naive
     post-preprocess index — or have preprocessing preserve line count (replace stripped statements
     with blank lines instead of deleting them) so positional alignment stays valid. The developer must
     add an integration test on a Snowflake fixture containing a stripped `ALTER … SET TAG;` statement
     and assert the surviving statements' `start_line` still match their original file lines.

   - **Scripting path** (`snowflake_parser.py:82,224-311`): when `_has_scripting_block` returns
     `True`, `_parse_scripting_file` is called. This method uses regex (`_EMBEDDED_DML`) to extract
     DML snippets and has its own `stmt_index` counter. The extracted snippets lose their original
     file line positions — the regex `match.start()` position is never used, so there is no reliable
     way to map `stmt_index` back to the original file line. For this path, **`start_line` must be
     left at the default sentinel `0` (unknown)**. Do NOT call `_compute_start_lines` in
     `_parse_scripting_file` — the tokenizer would parse the full file SQL and return statement
     boundaries for the non-scripting split, which does not correspond to the regex-extracted DML
     snippet indices.

   **Implementation rule for PR-2**: `QueryNode.start_line` defaults to `0`. `_compute_start_lines`
   is only wired in `AnsiParser.parse_file`. Scripting-path `QueryNode`s will have `start_line=0`,
   which correctly surfaces as `line=None` in `trace_column_lineage` output (the `OPTIONAL MATCH`
   on `SqlQuery` will return `start_line=0`; the `tools.py` caller should treat `0` as `None`
   when populating `LineageNode.line`). Add this guard in `tools.py`:
   ```python
   line=row.get("line") or None,   # 0 sentinel → None
   ```
   No change to `snowflake_parser.py` is required.

6. **`indexer.py`** — in the `query_rows` builder (line 976), add `start_line` to the dict:
   ```python
   query_rows.append(
       {
           "id": query_id,
           "file_path": parsed.path_str,
           "statement_index": i,
           "sql": stmt.sql[:500],
           "kind": stmt.kind,
           "target_table": stmt.target.full_id if stmt.target else "",
           "parse_failed": stmt.parse_failed,
           "confidence": stmt.confidence,
           "parsing_mode": stmt.parsing_mode,
           "start_line": stmt.start_line,   # NEW
       }
   )
   ```
   This is one dict-key addition on the existing `query_rows` list — no new bulk call, no per-edge cost.

7. **`queries.cypher`** — extend `TRACE_COLUMN_LINEAGE`:
   ```
   -- TRACE_COLUMN_LINEAGE
   MATCH (dst:SqlColumn {id: $id})<-[r:COLUMN_LINEAGE]-(src:SqlColumn)
   OPTIONAL MATCH (q:SqlQuery {id: r.query_id})
   RETURN src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified,
          r.transform AS transform, r.confidence AS confidence,
          q.file_path AS file, q.start_line AS line, q.sql AS expression
   ```
   `OPTIONAL MATCH` degrades to nulls on STAR_EXPANSION edges where `query_id` points to a valid
   query (they will resolve) or on old/orphaned edges (they will null-degrade gracefully).

8. **`tools.py`** — in both trace loop `LineageNode(...)` constructors, replace `file=None` with:
   ```python
   LineageNode(
       name=row.get("col_name", ""),
       kind="column",
       table=row.get("table_qualified"),
       file=row.get("file"),                    # was: file=None
       line=row.get("line") or None,            # new — `or None` converts 0 sentinel (scripting-path) → None
       expression=row.get("expression"),        # new
       confidence=row.get("confidence"),
       reason=_reason_for(row.get("transform"), row.get("confidence")),
   )
   ```
   The `or None` on `line` is required: scripting-path `SqlQuery` nodes have `start_line=0`
   (the default sentinel in `QueryNode`). `0 or None` evaluates to `None`, correctly
   surfacing unknown line as `None` rather than the misleading value `0`.

9. **`analyze.py`** — `upstream`/`downstream` queries currently return only `src.id`/`dst.id`.
   Extend both queries to also join `SqlQuery` via the column's referencing query and return
   `file`/`line`. Add a `file:line` column to `_print_table` output for `upstream`/`downstream`:
   ```python
   # Upstream query with file:line
   f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
   f"<-[r:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src) "
   "OPTIONAL MATCH (q:SqlQuery {id: r[-1].query_id}) "
   "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100"
   ```
   Render as `"file:line"` column: `f"{row.get('file','?')}:{row.get('line','?')}"`.

**Wiring verification**:

- `grep -n "SCHEMA_VERSION" src/sqlcg/core/schema.py` — must show `"5"`
- `grep -n "start_line" src/sqlcg/core/schema.cypher` — must return a hit
- `grep -n "start_line" src/sqlcg/parsers/base.py` — must show the `QueryNode` field
- `grep -n "_compute_start_lines\|start_lines\[" src/sqlcg/parsers/ansi_parser.py` — must show
  the helper definition AND at least one call site before the statement loop
- `grep -n "start_line" src/sqlcg/indexer/indexer.py` — must show it in the `query_rows` dict
- `grep -n "OPTIONAL MATCH" src/sqlcg/core/queries.cypher` — must show the new join in
  `TRACE_COLUMN_LINEAGE`
- `grep -n "file=None" src/sqlcg/server/tools.py` — must return zero results after the fix
- `grep -n "row.get(\"file\")\|row.get('file')" src/sqlcg/server/tools.py` — must return two hits
  (primary + bare-fallback loop)
- `grep -n "row.get(\"line\") or None\|\"line\") or None" src/sqlcg/server/tools.py` — must show
  the `0→None` sentinel guard in both trace-loop constructors (scripting-path `SqlQuery` rows have
  `start_line=0`; `0 or None` converts that to `None` so `LineageNode.line` is `None` not `0`)
- Confirm `_compute_start_lines` is called (not just defined): `grep -n "_compute_start_lines(" src/sqlcg/parsers/ansi_parser.py` — must return at least two hits
- Confirm no TODO in `parse_file` between `_compute_start_lines` call and the statement loop:
  `grep -n "TODO" src/sqlcg/parsers/ansi_parser.py | head -5`
- Confirm version gate fires: `grep -n "SCHEMA_VERSION\|Exit(1)\|db reset" src/sqlcg/cli/commands/index.py src/sqlcg/cli/commands/reindex.py src/sqlcg/cli/commands/watch.py` — must show existing version-mismatch exit paths; no change needed here, they already hard-fail on mismatch.

**Files affected**:
- [`src/sqlcg/core/schema.py`](../src/sqlcg/core/schema.py) — `SCHEMA_VERSION = "5"`
- [`src/sqlcg/core/schema.cypher`](../src/sqlcg/core/schema.cypher) — `start_line INT64` on `SqlQuery`
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — `QueryNode.start_line: int = 0`
- [`src/sqlcg/parsers/ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) — `_compute_start_lines` + wiring in `parse_file`
- [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py) — **no change required**. Non-scripting path delegates to `AnsiParser.parse_file` (gets `_compute_start_lines` free). Scripting path (`_parse_scripting_file`) correctly leaves `start_line=0` (unknown sentinel).
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — `start_line` in `query_rows`
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — `TRACE_COLUMN_LINEAGE` extended with `OPTIONAL MATCH`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — populate `file`/`line`/`expression` in both trace loops
- [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) — `upstream`/`downstream` render `file:line`

**Tests to add**:

- **Scenario A — start-line tokenizer**: parse a SQL string with three statements separated by
  semicolons and a blank line; assert `_compute_start_lines` returns `[1, 3, 5]` (or the correct
  1-based lines for the fixture). Unit test, no graph.
- **Scenario B — start-line persisted**: index a two-statement fixture file; query the graph for
  `SqlQuery` nodes; assert both have non-zero `start_line` and the second is greater than the first.
  Integration test (real KuzuDB in-memory).
- **Scenario C — trace returns file/line/expression**: index a fixture; call `trace_column_lineage`;
  assert the returned node has `file` matching the fixture path, `line >= 1`, and `expression`
  containing a substring of the SQL. Integration test.
- **Scenario D — v4 graph triggers re-index gate**: `db init` on a v4 schema (write `SchemaVersion`
  node with `version="4"`); call the index command; assert it exits with code 1 and prints a
  message containing "db reset". e2e test (or integration test patching the stored version).
- **Scenario E — bulk-upsert invariant unchanged**: `test_bulk_upsert_invariant.py` passes without
  modification (the new `start_line` key in `query_rows` does not add a new bulk call).
- **Scenario F — Snowflake preprocessing does not desync start lines**: index a Snowflake fixture
  whose statements include a line-spanning `WITH TAG (...)` clause and an `ALTER … MODIFY COLUMN …
  SET TAG …;` statement that `_preprocess_snowflake_sql` deletes (Gap 4b). Query `SqlQuery` nodes for
  the surviving statements and assert each `start_line` equals the statement's line in the
  **original** (un-preprocessed) file. Guards the preprocessing line-shift / index-desync bug
  (PR-2 step 5). Integration test.
- **Scenario G — Snowflake scripting fallback emits `start_line=0`**: index a fixture that triggers
  `_has_scripting_block`; assert the resulting `SqlQuery` nodes have `start_line == 0` and that
  `trace_column_lineage` surfaces `line=None` (not `0`) for their edges. Integration test.

**Acceptance criteria**:
- `[ ]` `SCHEMA_VERSION == "5"` in `schema.py`
- `[ ]` `db init` on a fresh DB creates a `SqlQuery` table with a `start_line` column
  (`grep -n "start_line" src/sqlcg/core/schema.cypher` returns a hit)
- `[ ]` A v4 graph triggers `Exit(1)` with a re-index message (Scenario D)
- `[ ]` `trace_column_lineage(col)` returns non-null `file`, `line >= 1`, and non-empty `expression`
  for a real non-scripting edge (Scenario C)
- `[ ]` Snowflake surviving-statement `start_line` matches the original file line after preprocessing
  strips an `ALTER … SET TAG;` statement (Scenario F); scripting-fallback nodes emit `start_line=0`
  and surface as `line=None` (Scenario G)
- `[ ]` `sqlcg analyze upstream <col>` output includes a `file:line` column
- `[ ]` `grep -n "file=None" src/sqlcg/server/tools.py` returns zero results
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`
  pass unchanged
- `[ ]` `_compute_start_lines` is called (not just defined): `grep -n "_compute_start_lines("
  src/sqlcg/parsers/ansi_parser.py` returns at least two hits

---

### PR-3 — #33: CLI/MCP Parity + Node-Kind Tagging

**Source**: [`plan/v1.1.0_cluster_b_provenance_trust.md`](v1.1.0_cluster_b_provenance_trust.md) Phase 3 (MED-HIGH risk)
**Effort**: M
**Depends on**: PR-2 merged (v5 schema in place; avoids a second SCHEMA_VERSION bump and forced
re-index; `base.py` and `indexer.py` changes from PR-2 landed before touching them again here)
**Blocks**: NONE

**Root cause**: Three distinct problems shipped to this PR:

1. **Dead `STALE_VIEWS` query** — `STALE_VIEWS` in `queries.cypher` matches `{kind: 'VIEW'}` which
   is never written by the indexer. `STALE_VIEWS_QUERY` is imported in `indexer.py:10` and called
   at `indexer.py:777`, but always returns zero rows. The cascade through `_reindex_view_definition`
   is dead in practice. This is a latent bug; the correct structural VIEW signal is
   `SqlQuery.kind IN ('CREATE_VIEW', 'CREATE_VIEW_AS')`.

2. **CTE/derived nodes pollute `upstream`/`impact`** — every `TableRef` minted as a CTE alias at
   `base.py:960` is persisted as `kind="TABLE"` (indexer.py:999), so CTE pseudo-tables appear as
   first-class tables in lineage queries. `<output>` synthetic sinks (base.py:494) also leak.

3. **`analyze impact`/`unused` lack NoiseFilter + de-dup + `--raw`** — unlike `upstream`/
   `downstream`, these two commands have no filtering, no deduplication, and no `--raw` opt-out.

**What to do**:

**Step 3.1 — Drop `STALE_VIEWS`**:

- Remove the `-- STALE_VIEWS` block from [`queries.cypher`](../src/sqlcg/core/queries.cypher) (lines 16-19).
- Remove `STALE_VIEWS_QUERY = _Q["STALE_VIEWS"]` from [`queries.py`](../src/sqlcg/core/queries.py).
- Remove the `from sqlcg.core.queries import STALE_VIEWS_QUERY` import from
  [`indexer.py:10`](../src/sqlcg/indexer/indexer.py).
- In `reindex_file` (indexer.py ~line 777): remove the `stale_views = db.run_read(STALE_VIEWS_QUERY, ...)` call and the `for row in stale_views` loop (lines 777-789). The correct view re-index behaviour — re-indexing views that `SELECT FROM` a changed table — is left as a v1.2 improvement. The simpler and safe behavior for v1.1.0 is: when `reindex_file` is called, only the changed file itself is re-indexed. Full re-index (`db reset && sqlcg index`) is the migration path.

**Step 3.2 — Tag `SqlTable.kind` at emission sites**:

Four emission sites in [`indexer.py`](../src/sqlcg/indexer/indexer.py) currently write `"TABLE"`.
The parser must carry role information to the indexer. The mechanism: add a `role: str` field to
`TableRef` in [`base.py`](../src/sqlcg/parsers/base.py), defaulting to `"table"`. Set it at the
two synthetic construction sites:

- `base.py:960` CTE alias construction: `TableRef(name=cte_alias, role="cte")`
- `base.py:494` `<output>` sink: skip persisting this entirely (filter in the indexer on
  `table.full_id == "<output>"`) — do NOT tag it; just exclude it.

Derived tables (unnamed subqueries as sources) are already resolved by `_lineage_node_to_table_ref`
returning `None` for subqueries — so `role="derived"` is for future use; v1.1.0 focuses on `cte`
tagging and `<output>` exclusion.

In `indexer.py`, at the source-table emission site (line 993-1000), apply the role:
```python
table_rows.append(
    {
        "qualified": src_table.full_id,
        "name": src_table.name,
        "catalog": src_table.catalog or "",
        "db": src_table.db or "",
        "kind": src_table.role if hasattr(src_table, "role") else "table",
        "defined_in_file": "",
    }
)
```
Do the same for the target table upsert site (line 1063-1074) and the defined-table row builder
(line 927-936) — but defined DDL tables always get `kind="table"`. Star-source rows (line 1042-1050)
are real source tables; keep `kind="table"`.

Also add `<output>` exclusion: before appending to `table_rows`, skip any `TableRef` where
`src_table.full_id == "<output>"`.

**Step 3.3 — Kind-filter Cypher queries**:

In [`queries.cypher`](../src/sqlcg/core/queries.cypher), add a kind-filtered variant for upstream:
```
-- GET_UPSTREAM_DEPENDENCIES_FILTERED
MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
MATCH (t:SqlTable {qualified: src.table_qualified})
WHERE t.kind IN ['table', 'external']
RETURN src.id AS id, src.col_name AS col_name, src.table_qualified AS table_qualified
```
The existing `GET_UPSTREAM_DEPENDENCIES` stays as-is (used by `--include-intermediate`).

In [`analyze.py`](../src/sqlcg/cli/commands/analyze.py), add `--include-intermediate` option to
`upstream`/`downstream`. By default, use the filtered query variant. With `--include-intermediate`,
use the existing unfiltered query.

In [`tools.py`](../src/sqlcg/server/tools.py), `trace_column_lineage` shows the full lineage chain
(including CTE hops) but labels each `LineageNode` with the source table's `kind` so the LLM can
distinguish. Add `table_kind` to `LineageNode` (or repurpose the `kind` field — it is currently
hardcoded to `"column"` for all nodes in the trace loop). The cleaner approach is a new
`table_kind: str | None = None` field on `LineageNode`. Populate it from a join on `SqlTable.kind`
via the node's `table_qualified` — or add `src.table_kind` to the `TRACE_COLUMN_LINEAGE` result
set by joining `SqlTable`:
```
OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified})
```
Add `t.kind AS table_kind` to the RETURN clause.

**Step 3.4 — `impact`/`unused` parity with `upstream`/`downstream`**:

In [`analyze.py`](../src/sqlcg/cli/commands/analyze.py):

`impact` command — add `raw: bool` parameter, apply `NoiseFilter` (filtering on the `target_table`
of each query row), de-dup by `q.id`:
```python
@app.command("impact")
def impact(
    table: str = typer.Argument(...),
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),
) -> None:
    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE} {{qualified: $t}})"
            f"<-[:{RelType.SELECTS_FROM}]-(q:{NodeLabel.QUERY}) "
            "RETURN DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target LIMIT 100",
            {"t": table},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter
            nf = NoiseFilter.from_config()
            results = [r for r in results if not nf.is_noise(r.get("target", ""))]
        _print_table(results, ["id", "kind"])
```

`unused` command — add `raw: bool`, apply `NoiseFilter` on `t.qualified`, de-dup:
```python
@app.command("unused")
def unused(
    threshold: int = typer.Option(0, "--threshold", ...),
    raw: bool = typer.Option(False, "--raw", ...),
) -> None:
    with get_backend() as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE}) WHERE NOT (t)<-[:{RelType.SELECTS_FROM}]-() "
            "RETURN DISTINCT t.qualified AS qualified LIMIT 100",
            {},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter
            nf = NoiseFilter.from_config()
            results = [r for r in results if not nf.is_noise(r["qualified"])]
        _print_table(results, ["qualified"])
```

**Wiring verification**:

- `grep -n "STALE_VIEWS" src/sqlcg/core/queries.cypher` — must return zero results
- `grep -n "STALE_VIEWS\|stale_views" src/sqlcg/indexer/indexer.py` — must return zero results
- `grep -n "STALE_VIEWS_QUERY" src/sqlcg/core/queries.py` — must return zero results
- `grep -n "role" src/sqlcg/parsers/base.py | grep "TableRef\|dataclass\|field"` — must show
  `role: str = "table"` on `TableRef`
- `grep -n "role=\"cte\"" src/sqlcg/parsers/base.py` — must show line 960 CTE construction
- `grep -n "<output>" src/sqlcg/indexer/indexer.py` — must show the exclusion check (skip emit)
- `grep -n "t\.kind\|table_kind" src/sqlcg/core/queries.cypher` — must show the filtered query
  variant `GET_UPSTREAM_DEPENDENCIES_FILTERED` and the `TRACE_COLUMN_LINEAGE` `t.kind` join
- `grep -n "include.intermediate\|include_intermediate" src/sqlcg/cli/commands/analyze.py` — must
  show the new flag
- `grep -n "raw.*bool\|--raw" src/sqlcg/cli/commands/analyze.py` — must show `--raw` on `impact`
  and `unused` commands
- `grep -n "NoiseFilter" src/sqlcg/cli/commands/analyze.py` — must show four call sites (upstream,
  downstream, impact, unused)
- Confirm no TODO in `reindex_file` after removing the stale-views cascade:
  `grep -n "TODO" src/sqlcg/indexer/indexer.py | grep -i "view\|stale"`

**Files affected**:
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — `TableRef` gains `role: str = "table"` field; CTE construction site sets `role="cte"`
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — `STALE_VIEWS_QUERY` import removed; stale-view cascade removed; `<output>` exclusion; `kind` from `src_table.role`
- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — `STALE_VIEWS` block removed; `GET_UPSTREAM_DEPENDENCIES_FILTERED` added; `TRACE_COLUMN_LINEAGE` gains `t.kind` join
- [`src/sqlcg/core/queries.py`](../src/sqlcg/core/queries.py) — `STALE_VIEWS_QUERY` removed
- [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) — `impact` + `unused` gain `--raw`, `NoiseFilter`, de-dup; `upstream`/`downstream` gain `--include-intermediate`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — `trace_column_lineage` joins `SqlTable.kind`; labels CTE-origin nodes; `LineageNode` populated with `table_kind`
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — `LineageNode` gains `table_kind: str | None = None`

**Tests to add**:

- **Scenario A — CTE kind tagging**: index a fixture with a CTE (`WITH cte AS (SELECT ...)`);
  query `SqlTable` nodes; assert the CTE alias node has `kind == "cte"` and the real source table
  has `kind == "table"`. Integration test.
- **Scenario B — `<output>` excluded**: index a fixture; query `SqlTable` nodes; assert no node
  has `qualified == "<output>"`. Integration test.
- **Scenario C — upstream excludes CTE by default**: index a fixture where a column lineage chain
  passes through a CTE intermediate; call `analyze upstream`; assert the CTE node does NOT appear
  in results. Assert it DOES appear with `--include-intermediate`. Integration or e2e test.
- **Scenario D — `impact` NoiseFilter + de-dup**: configure a noise pattern matching a test table;
  call `analyze impact <real_table>`; assert the noise-pattern table does not appear in results;
  `--raw` includes it. Integration test.
- **Scenario E — `unused` NoiseFilter + de-dup**: same pattern as Scenario D but for `unused`.
- **Scenario F — `trace_column_lineage` labels CTE nodes**: call `trace_column_lineage` on a
  column whose lineage passes through a CTE; assert at least one `LineageNode` has
  `table_kind == "cte"`. Integration test.

**Acceptance criteria**:
- `[ ]` `grep -n "STALE_VIEWS" src/sqlcg/core/queries.cypher` returns zero results
- `[ ]` `grep -n "STALE_VIEWS\|stale_views" src/sqlcg/indexer/indexer.py` returns zero results
- `[ ]` A CTE alias node has `SqlTable.kind == "cte"` after indexing (Scenario A)
- `[ ]` No `SqlTable` node with `qualified == "<output>"` exists after indexing (Scenario B)
- `[ ]` `analyze upstream` excludes CTE nodes by default; `--include-intermediate` shows them (Scenario C)
- `[ ]` `analyze impact` and `unused` apply `NoiseFilter.from_config()` + de-dup; `--raw` restores
  unfiltered list (Scenarios D and E)
- `[ ]` `grep -n "raw.*bool\|--raw" src/sqlcg/cli/commands/analyze.py` returns hits for both
  `impact` and `unused`
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`
  pass unchanged (kind tagging is a value change at existing emission sites; no new AST traversal)

---

## Test Strategy

### The single most important regression guard

```python
def test_trace_returns_source_location_regression_guard():
    """Column lineage trace must return non-null file and line for real edges.

    Guards against the v1.0.x regression where file=None was hardcoded in
    tools.py trace loops, making the tool useless for provenance audits.
    """
    import pytest
    from pathlib import Path
    import tempfile

    from sqlcg.parsers.ansi_parser import AnsiParser
    from sqlcg.lineage.schema_resolver import SchemaResolver
    from sqlcg.indexer.indexer import Indexer
    from sqlcg.core.kuzu_backend import KuzuBackend
    from sqlcg.server.tools import trace_column_lineage  # or call via DB directly

    sql = "CREATE TABLE src (a INT);\nINSERT INTO dst SELECT a AS x FROM src;"
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        with KuzuBackend(str(db_path)) as db:
            db.init_schema()
            fixture = Path(tmpdir) / "fixture.sql"
            fixture.write_text(sql)
            indexer = Indexer()
            indexer.index_repo(str(tmpdir), db)

            rows = db.run_read(
                "MATCH (dst:SqlColumn {id: $id})<-[r:COLUMN_LINEAGE]-(src:SqlColumn) "
                "OPTIONAL MATCH (q:SqlQuery {id: r.query_id}) "
                "RETURN q.file_path AS file, q.start_line AS line",
                {"id": "dst.x"},
            )
            assert len(rows) >= 1, (
                "No COLUMN_LINEAGE edges found — regression guard: trace must return edges"
            )
            for row in rows:
                assert row["file"] is not None, (
                    "file must be non-null on lineage edges — "
                    "guards against v1.0.x file=None hardcode regression"
                )
                assert row["line"] is not None and row["line"] >= 1, (
                    "line must be >= 1 on lineage edges — "
                    "guards against start_line never being persisted"
                )
```

This test is NOT marked `xfail`. It becomes green when PR-2 is merged. It must be included in the
PR-2 commit (or added as a failing test ahead of the PR to document the gap).

---

## Wiring Checklist

| Question | PR-1 | PR-2 | PR-3 |
|----------|------|------|------|
| What calls the changed function/constructor? | `_lineage_node_to_edges._walk` calls `LineageEdge(confidence=...)` — value change only; both trace loops call `LineageNode(...)` adding `reason=` | `parse_file` calls `_compute_start_lines` before the stmt loop; indexer `_upsert_parsed_file` reads `stmt.start_line`; both trace loops read `row.get("file"/"line"/"expression")` | `parse_file` CTE block constructs `TableRef(role="cte")`; indexer emission sites read `src_table.role`; `analyze impact/unused` call `NoiseFilter.from_config()` |
| Where is the callback/parameter passed? | `_reason_for(row.get("transform"), row.get("confidence"))` passed into `LineageNode.reason` at both trace-loop constructors in `tools.py` | `start_line` flows: `_compute_start_lines(sql)` → list → `QueryNode.start_line` → `query_rows["start_line"]` → `upsert_nodes_bulk` → graph → `TRACE_COLUMN_LINEAGE` RETURN → `row.get("line") or None` → `LineageNode.line` (the `or None` converts the `0` sentinel for scripting-path rows to `None`) | `TableRef.role` flows: `base.py:960` CTE site → `src_table.role` in `indexer.py` emission → `kind` field in `table_rows` dict → `upsert_nodes_bulk` → graph |
| What constant/path does this align with? | `SCHEMA_VERSION` is not affected (PR-1 makes no schema change); `_REASON_MAP` keys align with `transform` values already in the graph (`STAR_EXPANSION`, `UNKNOWN`) | `SCHEMA_VERSION = "5"` in `schema.py:6`; `start_line INT64` in `schema.cypher` `SqlQuery` table; aligned with `KuzuConfig` (no hardcoded paths) | No new schema column; `role` values align with enum `{table, cte, derived, external}` documented in the plan; `NoiseFilter.from_config()` uses `KuzuConfig` internally |
| Does any TODO remain in the happy path? | No — `_reason_for` is a complete implementation, not a stub | No — `_compute_start_lines` is a complete tokenizer pass; `QueryNode.start_line = 0` as default is a safe sentinel | No — `STALE_VIEWS` is fully removed; kind tagging is complete at all emission sites; `impact`/`unused` parity is complete |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg analyze upstream <col>` on an indexed corpus shows a `file:line` column with
  non-null, non-zero values for all resolved lineage edges **from ANSI / non-scripting statements**
  (Snowflake scripting-fallback rows legitimately show `line=None`; see PR-2 step 5)
- `[ ]` `trace_column_lineage` via MCP returns, per node: non-null `file`, non-empty `expression`,
  `line >= 1` **for non-scripting-fallback edges** (`line=None` is accepted for scripting-fallback
  rows only), and `reason=None` for fact edges / non-empty `reason` for inferred edges
- `[ ]` For a Snowflake fixture containing a stripped `ALTER … SET TAG;` statement, surviving
  statements' persisted `start_line` matches their **original** file line (guards the preprocessing
  desync — PR-2 step 5)
- `[ ]` A plainly-parsed `SELECT col AS x` edge reports `confidence == 1.0`; a STAR_EXPANSION edge
  reports `confidence == 0.8` with a non-empty `reason`
- `[ ]` `SCHEMA_VERSION == "5"` in `schema.py`; `sqlcg index` on a v4 graph exits 1 with a message
  directing the user to run `db reset && db init && index`
- `[ ]` CTE alias nodes have `SqlTable.kind == "cte"`; no `SqlTable` node has `qualified == "<output>"`
- `[ ]` `analyze upstream` and `analyze impact` exclude CTE/derived nodes by default; `--include-intermediate` restores them
- `[ ]` `analyze impact` and `analyze unused` apply `NoiseFilter.from_config()` and de-dup; `--raw` gives the raw list
- `[ ]` `skill.py` `_BOUNDARY` no longer claims `trace_column_lineage` always returns a flat-confidence fact; documents the `confidence=1.0` / `reason`-tagged inferred scale
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py` all pass unchanged; `git diff --stat` shows zero edits to `_extract_column_lineage` column loop or `_upsert_parsed_file` edge-row loop (the only edits in those regions are the single `0.7→1.0` value change and the single `start_line` dict-key addition)
- `[ ]` `grep -n "STALE_VIEWS" src/sqlcg/core/queries.cypher src/sqlcg/indexer/indexer.py src/sqlcg/core/queries.py` returns zero results

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `tools.py` trace loops edited three times across three PRs → rebase conflict | HIGH | Sequential PRs on one branch; each PR is a clean rebase on the previous. The `LineageNode(...)` constructor is the collision point — PR-1 adds `reason=`, PR-2 adds `file=/line=/expression=`, PR-3 adds `table_kind=`. Write them in one constructor per PR; no divergent edits. |
| Snowflake scripting-path `start_line` not propagated | LOW | **Resolved pre-implementation.** `_parse_scripting_file` uses regex DML extraction and loses original line positions; scripting-path nodes correctly emit `start_line=0` (sentinel). `tools.py` applies `row.get("line") or None` to convert `0→None` in `LineageNode.line`. No change to `_parse_scripting_file` required. |
| Snowflake **non-scripting** `start_line` is WRONG due to preprocessing | **MED** | `SnowflakeParser.parse_file` delegates the *preprocessed* SQL to `AnsiParser.parse_file`. `_preprocess_snowflake_sql` strips line-spanning clauses and **deletes whole `ALTER … SET TAG;` statements** (Gap 4b), so computing `_compute_start_lines` on the delegated text shifts lines and desyncs the index→line map. `SET TAG` DDL is common in DWH Snowflake corpora — realistic wrong-line bug. **Mitigation**: PR-2 step 5 requires computing the line map from the **original** SQL (before preprocessing) and aligning by a stable key, OR preserving line count during preprocessing. Gated by a dedicated integration test on a fixture with a stripped `ALTER … SET TAG;` statement. |
| `TableRef.role` field addition breaks serialization / pickling in subprocess isolation path | LOW | `QueryNode` and `ParsedFile` are pickled via `multiprocessing.Process` spawn context (`indexer.py:816`). Adding a defaulted field to a `@dataclass` does not break pickle. Confirm with the `test_bulk_upsert_invariant.py` run. |
| `STALE_VIEWS` drop changes `reindex_file` observable behaviour | LOW | The cascade was already a no-op (zero rows returned). Removing dead code cannot break working behaviour. Confirm with existing `reindex_file` integration tests. |
| `analyze upstream` query extension (file/line join via `r[-1].query_id`) fails on multi-hop paths where intermediate edges use different `query_id` values | MED | The join targets the **last** edge in the path (`r[-1]`). For multi-hop traversal, `r` is a list of edges; KuzuDB variable-length match returns the full path's edge list. Validate in Scenario B integration test. If the multi-hop join is unsupported, degrade gracefully: only expose `file:line` on single-hop upstream (emit empty for multi-hop). |
