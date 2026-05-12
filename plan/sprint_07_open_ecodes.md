# Sprint Plan: Open E-Code Lineage Failures (Sprint 07)

Plan date: 2026-05-12
Author: architect-planner
Source authority:
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) § 12 (Column Lineage — Diagnostic Findings)
- [`plan/sprint_06_lineage_coverage.md`](sprint_06_lineage_coverage.md) (shipped predecessor)
- [`tests/snowflake/README.md`](../tests/snowflake/README.md) (E-code reference)
- Memory: `project_en_taxonomy.md` (E36 = primary zero-edge root cause: 38/184 files)

Policy: no backward compatibility; re-index is the migration path; no TODO in happy path.

---

## Summary

Sprint 06 wired schema-`sources=` (FIX-E5), best-effort expression naming (FIX-E2),
pure-DDL early skip (FIX-DDL-SKIP), dialect pre-processing (FIX-E4), and per-statement
scope reuse (OPT-SCOPE). The remaining open E-codes
(`E5` cross-file, `E12`, `E16`, `E25`, `E27`, `E36` cross-file) are still marked
`# INVERSION TARGET` in the test suite. This sprint closes the highest-impact ones and
explicitly defers the upstream-sqlglot gaps with a written rationale.

Six tickets, ranked by impact × effort:

1. **T-07-01 — FIX-E36-XFILE** — propagate intra-file `sources_map` (CTAS bodies) across
   files via the `CrossFileAggregator` so a temp table defined in file A is visible to file
   B. Primary zero-edge root cause: 38/184 files in the fact corpus produced zero edges
   solely because of this.
2. **T-07-02 — FIX-E5-XFILE-CHAIN** — make the 6 xfail anchor links in
   `tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py` pass. Sprint gate: when
   these flip from xfail to pass, the sprint can close. Builds on T-07-01 plus a small
   CTE-to-CTE wiring fix inside `_extract_column_lineage`.
3. **T-07-03 — FIX-E25** — preserve the `catalog.schema` qualifier on
   `LineageEdge.src.table` for 2- and 3-part identifiers. Local fix in
   `_lineage_node_to_table_ref`; no sqlglot upstream dependency.
4. **T-07-04 — DEFER-E12** — write up the upstream sqlglot gap, mark E12 tests xfail with
   a referenced reason, and remove the `# INVERSION TARGET` markers (replace with
   `# DEFERRED: sqlglot#<issue>` markers). No code change in the parser. Tracked here so
   the sprint exits with a documented decision rather than a silent skip.
5. **T-07-05 — DEFER-E27** — same shape as T-07-04 for UDF column-source propagation.
6. **T-07-06 — DEFER-E16** — same shape as T-07-04 for MERGE multi-branch column lineage,
   plus a small parser hook that emits a structured error string
   (`col_lineage_skip:merge_branch:<dst_col>`) so the report flags MERGE files explicitly.

Why three DEFER tickets sit in the same sprint: each one requires (a) reading the test
fixture, (b) confirming `sqlglot.lineage` cannot trace it in any current version, and
(c) committing the documented decision. They are not free — they are work, and they close
the sprint by retiring three `# INVERSION TARGET` markers cleanly.

---

## Scope

### In Scope

- [`src/sqlcg/lineage/aggregator.py`](../src/sqlcg/lineage/aggregator.py) — cross-file
  temp-table propagation
- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py) —
  optional consumer for cross-file CTAS source bodies
- [`src/sqlcg/parsers/ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) — consume
  cross-file `sources_map`, surface MERGE branch skip
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — preserve qualifier in
  `_lineage_node_to_table_ref`, MERGE skip recording
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — wire the cross-file
  sources_map into the two-pass loop
- All `tests/snowflake/{E5,E16,E25,E36,anchors}/` tests — flip or convert `# INVERSION
  TARGET` markers
- New integration regression guard: `tests/integration/test_e36_xfile_regression_guard.py`

### Non-Goals

- E12 LATERAL FLATTEN / colon JSON path resolution (sqlglot upstream — deferred with
  written rationale in T-07-04)
- E27 UDF body inlining (sqlglot upstream — deferred in T-07-05)
- E16 MERGE multi-branch column lineage (sqlglot lineage API does not visit MERGE
  branches — deferred in T-07-06; only the observability hook is in scope)
- Re-running the full DWH corpus benchmark — that belongs in the sprint postmortem, not
  the plan
- Any change to KuzuDB schema or graph upsert paths
- BigQuery / ANSI / TSQL coverage — Snowflake dialect only

---

## Code-vs-Plan Verification

Every claim below was confirmed by reading the source at the line cited. No grep was
substituted for actually reading the function body.

| Finding | Verified state |
|---------|----------------|
| `CrossFileAggregator` only stores `defined_tables → ParsedFile`, never CTAS bodies | Confirmed — [`aggregator.py` lines 21–28](../src/sqlcg/lineage/aggregator.py): `register_pass1` iterates `parsed.defined_tables` and stores `self.sources[table.full_id] = parsed`. The `sources_map` containing the actual CTAS `exp.Select` bodies is built inside `AnsiParser.parse_file` line 80 (`sources_map: dict[str, Any] = {}`) and discarded when the function returns. Cross-file propagation is impossible without surfacing this map. |
| `SchemaResolver._view_bodies` is populated by `add_view_sources` and never read | Confirmed — [`schema_resolver.py` lines 79–87](../src/sqlcg/lineage/schema_resolver.py) and grep across the repo. `_view_bodies` is a dead container today; we can repurpose it as the cross-file CTAS body store, OR add a new explicit method. Plan choice (see T-07-01): add a new explicit `register_cross_file_sources` / `cross_file_sources()` pair to keep semantics clean. |
| `AnsiParser.parse_file` builds `sources_map` per file (line 80) and never reads anything from outside the file | Confirmed — [`ansi_parser.py` lines 79–111](../src/sqlcg/parsers/ansi_parser.py): `sources_map` is initialised empty on every `parse_file` call and only mutated by the local CTAS-registration block at lines 102–111. No external source is consulted. |
| `_lineage_node_to_table_ref` drops `catalog` and `db` if `source` is an `exp.Table` | Re-read — [`base.py` lines 460–481](../src/sqlcg/parsers/base.py). The body DOES read `source.catalog` and `source.db` and pass them into the `TableRef` constructor. So `TableRef.full_id` returns `catalog.db.name`. **But** `tests/snowflake/conftest.py` `edges()` helper extracts `edge.src.table.name` only — not `full_id`. The E25 test will pass once it inspects `full_id` (or a new field), not when `TableRef` itself is fixed. The bug is in the **observable surface** the tests probe, not in the parser. This changes T-07-03's scope (see ticket). |
| Tests use `edges()` that returns 4-tuple `(src.table.name, src.name, dst.table.name, dst.name)` | Confirmed — [`tests/snowflake/conftest.py` lines 16–27](../tests/snowflake/conftest.py). |
| `sources_map` is passed into `_extract_column_lineage` as `sources=`; merged with `schema_sources` into `combined_sources` | Confirmed — [`base.py` lines 558, 689](../src/sqlcg/parsers/base.py). |
| The E36 single-file test already asserts the resolved edge (`SRC.A → dst.a`) | Confirmed — [`tests/snowflake/E36/test_e36.py` lines 26–29](../tests/snowflake/E36/test_e36.py). The `# INVERSION TARGET` comment at line 36 calls for a **new two-file test**, not modification of the existing test. |
| Anchor MA_AANTAL_OP_ORDER fixture is single-file (`fixture_etl.sql`) — not actually multi-file | Re-read — [`tests/snowflake/anchors/fixture_etl.sql`](../tests/snowflake/anchors/fixture_etl.sql) (single file, all 4 CTEs in one INSERT) and [`tests/snowflake/anchors/fixture_semantic.sql`](../tests/snowflake/anchors/fixture_semantic.sql) (separate view). Links 1–5 are **intra-file** CTE-to-CTE failures. Only Link 6 is cross-file (etl → semantic view). The xfail reasons say "CTE resolution with SUM aggregation not yet implemented" / "CTE-to-CTE column resolution not yet implemented". This means T-07-02 is **mostly** an in-`_extract_column_lineage` fix (CTE chain through UNION ALL with SUM aggregation), not a cross-file fix. The plan reflects this. |
| `sg_lineage` returns CTE chains correctly when `sources_map` contains the inner CTE bodies | Confirmed by reading [`base.py` lines 558, 689](../src/sqlcg/parsers/base.py) — `combined_sources` is passed into `sg_lineage`. But within a single SELECT with WITH clauses, the CTEs are children of the same `exp.Select` node — sqlglot's `qualify()` already expands them. The xfails likely stem from a different cause (see T-07-02 root cause analysis). |
| MERGE statements never enter `_extract_column_lineage` body — the `isinstance(stmt, (exp.Select, exp.Insert, exp.Create))` guard at line 528 short-circuits | Confirmed — [`base.py` line 528](../src/sqlcg/parsers/base.py). MERGE returns an empty `LineageExtraction` early. No error string is recorded today; the file just silently has zero edges from the MERGE statement. T-07-06 adds the observability hook. |

---

## Ticket Table

| ID | Title | Primary files | Effort | Priority | Depends on | Blocks |
|----|-------|---------------|--------|----------|------------|--------|
| T-07-01 | FIX-E36-XFILE — cross-file temp-table propagation | `aggregator.py`, `schema_resolver.py`, `ansi_parser.py`, `snowflake_parser.py`, `indexer.py` | M | 1 — highest | INDEPENDENT | T-07-02 (anchor link 6) |
| T-07-02 | FIX-E5-XFILE-CHAIN — flip 6 anchor xfails | `base.py`, `ansi_parser.py`, `tests/snowflake/anchors/*` | M | 2 (sprint gate) | T-07-01 (link 6 only) | NONE |
| T-07-03 | FIX-E25 — preserve qualifier on observable edge surface | `base.py` (TableRef serialization or new property), `tests/snowflake/E25/*` | S | 3 | INDEPENDENT | NONE |
| T-07-04 | DEFER-E12 — document upstream sqlglot gap, retire INVERSION marker | `tests/snowflake/E12/test_e12.py`, `tests/snowflake/README.md` | XS | 4 | INDEPENDENT | NONE |
| T-07-05 | DEFER-E27 — document upstream sqlglot gap, retire INVERSION marker | `tests/snowflake/E27/test_e27.py`, `tests/snowflake/README.md` | XS | 5 | INDEPENDENT | NONE |
| T-07-06 | DEFER-E16 — document gap + add `col_lineage_skip:merge_branch:*` observability | `base.py` (1-line skip), `tests/snowflake/E16/test_e16.py`, `tests/snowflake/README.md` | S | 6 | INDEPENDENT | NONE |
| T-07-07 | MCP-FILE-PATH — surface source file paths in `trace_column_lineage`, `get_downstream_dependencies`, `get_upstream_dependencies` | `src/sqlcg/core/queries.cypher`, `src/sqlcg/server/models.py`, `src/sqlcg/server/tools.py` | S | 7 | INDEPENDENT | NONE |

Effort scale: XS = doc-only / 1-line; S = ≤ 30 LOC + tests; M = multi-file plumbing + tests.

---

## Recommended Implementation Order

### Why this order

**T-07-01 ships first** because (a) it is the highest-impact correctness fix (38/184
zero-edge files in the fact corpus are E36-only victims), (b) it unblocks anchor link 6
(view in `fixture_semantic.sql` sources from `wtfs_openstaande_orders` defined in
`fixture_etl.sql` — without cross-file source visibility, link 6 stays xfail), and
(c) no other ticket depends on it except T-07-02 link 6.

**T-07-02 ships second** because the anchor xfails are the sprint gate. Links 1–5 do not
depend on T-07-01 (intra-file CTE chain), so they can start in parallel — but the test
file is shared, so a single ticket owns it.

**T-07-03 (E25)** is a clean independent fix; safe to slot anywhere after T-07-01.

**T-07-04 / T-07-05 / T-07-06 (DEFER trio)** can land last together as a single docs-style
commit. They retire `# INVERSION TARGET` markers with an explicit upstream reference, so
the test suite no longer claims an unfulfilled intent.

### Single-developer sequence

1. T-07-01 (cross-file sources_map plumbing + E36 two-file test)
2. T-07-02 (CTE chain wiring + anchor xfails removed)
3. T-07-03 (E25 qualifier surface)
4. T-07-04, T-07-05, T-07-06 batched (DEFER docs)
5. T-07-07 (MCP file path enrichment — independent, slot anywhere)

### Two-developer sequence

Track A (correctness):
- A1: T-07-01
- A2: T-07-02
- A3: T-07-03 (after A1 merges)

Track B (defer):
- B1: T-07-04 + T-07-05 + T-07-06 (single PR, doc-only changes)

Both tracks start at sprint kickoff. Track B is the smallest, finishes first, and removes
INVERSION markers that would otherwise mask real failures in Track A's tests.

---

## Ticket Specifications

---

### T-07-01 — FIX-E36-XFILE: cross-file temp-table propagation

**Source**: README E36 (`tests/snowflake/README.md` lines 302–323), test
[`tests/snowflake/E36/test_e36.py` line 36](../tests/snowflake/E36/test_e36.py)
INVERSION TARGET, memory `project_en_taxonomy.md` (38/184 files).

**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: T-07-02 anchor link 6

#### Root cause

`AnsiParser.parse_file` initialises `sources_map: dict[str, Any] = {}` at
[line 80](../src/sqlcg/parsers/ansi_parser.py) and discards it when the function returns.
Cross-file CTAS bodies (`CREATE TEMP TABLE t AS SELECT a FROM src`) registered in file A
are never visible to file B's `parse_file` call. The `CrossFileAggregator` already exists
([`aggregator.py`](../src/sqlcg/lineage/aggregator.py)) but only tracks `defined_tables`,
not the SELECT bodies that `sg_lineage` needs as `sources=`.

The fix is to: (1) introduce a cross-file `sources_map` owned by the aggregator,
populated during pass 1 by harvesting CTAS bodies from each parsed file, and (2) seed
each `parse_file` call's local `sources_map` from this cross-file store before parsing
begins.

This is parallel to T-01 from sprint 06: that ticket fed `schema_sources` from the
information schema; this ticket feeds `cross_file_sources` from sibling files' CTAS
bodies. Both end up merged into `combined_sources` in `_extract_column_lineage`.

#### What to do

**Step 1.1** — Extend `QueryNode` with `defined_body: exp.Select | None`:

In [`base.py`](../src/sqlcg/parsers/base.py), add a field to `QueryNode`:

```python
@dataclass
class QueryNode:
    ...
    defined_body: Any | None = None  # exp.Select for CTAS, exp.Subquery, etc. None otherwise
```

This must be populated in `_parse_statement` for `exp.Create` nodes where
`stmt.expression` is `exp.Select` or `exp.Subquery`. Today the CTAS registration logic
lives in `AnsiParser.parse_file` ([lines 102–111](../src/sqlcg/parsers/ansi_parser.py))
and writes into the local `sources_map` directly. Move the body extraction into
`_parse_statement` so the body lives on the `QueryNode` and the aggregator can read it
later from `parsed.statements`.

Verification grep: `grep -n "defined_body" src/sqlcg/parsers/base.py` shows one definition,
`grep -n "defined_body" src/sqlcg/parsers/ansi_parser.py` shows one assignment.

**Step 1.2** — Extend `CrossFileAggregator` with a cross-file CTAS body map:

In [`aggregator.py`](../src/sqlcg/lineage/aggregator.py):

```python
class CrossFileAggregator:
    def __init__(self) -> None:
        self.sources: dict[str, ParsedFile] = {}
        # NEW: table-name (lowercased, last component) → exp.Select body for CTAS
        # registered in any pass-1 file. Used as the seed for cross-file sources_map.
        self.cross_file_sources: dict[str, Any] = {}

    def register_pass1(self, parsed: ParsedFile) -> None:
        for table in parsed.defined_tables:
            self.sources[table.full_id] = parsed
        # NEW: harvest CTAS bodies from every statement that has one
        for stmt_node in parsed.statements:
            if stmt_node.target and stmt_node.defined_body is not None:
                key = (stmt_node.target.name or "").lower()
                if key:
                    self.cross_file_sources[key] = stmt_node.defined_body
```

Key choice — lowercased bare table name — must match the existing intra-file
`sources_map` key convention at
[`ansi_parser.py` line 109](../src/sqlcg/parsers/ansi_parser.py)
(`target_name = (query_node.target.name or "").lower()`). Document this in the docstring.

**Step 1.3** — Thread `cross_file_sources` into `parse_file`:

The parser needs to know the cross-file map without changing its `parse_file(path, sql)`
signature (called by many code paths). Use `SchemaResolver` as the carrier — it is
already constructed once per index job and passed to the parser via `schema_resolver=`.

Add to `SchemaResolver`:

```python
def register_cross_file_sources(self, sources: dict[str, Any]) -> None:
    """Register CTAS bodies harvested across pass-1 files for cross-file resolution.

    Called once by CrossFileAggregator at the boundary between pass 1 and pass 2.
    The dict is keyed by lowercased bare table name (matches sources_map convention).
    """
    with self._lock:
        self._cross_file_sources = dict(sources)

def cross_file_sources(self) -> dict[str, Any]:
    with self._lock:
        return dict(self._cross_file_sources)
```

Initialise `_cross_file_sources: dict[str, Any] = {}` in `__init__`.

Do NOT reuse `_view_bodies` for this — that field is dead today but its docstring still
claims it maps view names to `ParsedFile` objects (verified at
[`schema_resolver.py` line 24](../src/sqlcg/lineage/schema_resolver.py)). Mixing the two
semantics invites bugs.

**Step 1.4** — Seed `sources_map` in `AnsiParser.parse_file`:

Today: [`ansi_parser.py` line 80](../src/sqlcg/parsers/ansi_parser.py) starts with
`sources_map: dict[str, Any] = {}`. Change to:

```python
sources_map: dict[str, Any] = dict(self._schema.cross_file_sources()) if self._schema else {}
```

This makes every `parse_file` start with the cross-file CTAS bodies, and the intra-file
CTAS-registration block (lines 102–111) layers on top. Lower-cased keys ensure no
collision logic is needed (intra-file definitions of the same name overwrite cross-file
ones, which is the correct precedence).

Apply the same change in
[`snowflake_parser.py` line 195](../src/sqlcg/parsers/snowflake_parser.py)
(`sources_map: dict[str, Any] = {}` in `_parse_scripting_file`).

**Step 1.5** — Wire the aggregator into the indexer's two-pass loop:

In [`indexer.py`](../src/sqlcg/indexer/indexer.py) at the boundary between pass 1 and
pass 2 (around line 99, after the pass-1 loop, before the pass-2 loop):

```python
# Seed cross-file sources for pass-2 re-parsing
schema_resolver.register_cross_file_sources(aggregator.cross_file_sources)
```

This is the **only** place the cross-file map is enabled. Pass 1 still gets
empty cross-file sources (correct: file order is non-deterministic, so pass 1 cannot rely
on it). Pass 2 sees the union of all pass-1 CTAS bodies. This matches the existing pass-2
contract documented in [`aggregator.py` resolve_pass2](../src/sqlcg/lineage/aggregator.py).

#### Wiring verification

Before opening the PR, run:

- `grep -n "defined_body" src/sqlcg/parsers/base.py` → one definition in `QueryNode`.
- `grep -n "defined_body" src/sqlcg/parsers/ansi_parser.py` → one assignment when extracting CTAS.
- `grep -n "defined_body" src/sqlcg/lineage/aggregator.py` → one read in `register_pass1`.
- `grep -n "cross_file_sources" src/sqlcg/lineage/aggregator.py` → field + harvesting loop.
- `grep -n "cross_file_sources" src/sqlcg/lineage/schema_resolver.py` → field + register + getter.
- `grep -n "cross_file_sources" src/sqlcg/parsers/ansi_parser.py` → one read seeding `sources_map`.
- `grep -n "cross_file_sources" src/sqlcg/parsers/snowflake_parser.py` → one read in scripting fallback.
- `grep -n "register_cross_file_sources" src/sqlcg/indexer/indexer.py` → one call between pass 1 and pass 2.
- `grep -n "TODO" src/sqlcg/lineage/aggregator.py src/sqlcg/lineage/schema_resolver.py src/sqlcg/parsers/ansi_parser.py src/sqlcg/parsers/snowflake_parser.py src/sqlcg/indexer/indexer.py` → zero results in any happy path.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — `QueryNode.defined_body`
- [`src/sqlcg/parsers/ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) — assign `defined_body` in `_parse_statement`; seed `sources_map`
- [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py) — seed `sources_map` in scripting fallback
- [`src/sqlcg/lineage/aggregator.py`](../src/sqlcg/lineage/aggregator.py) — `cross_file_sources` field + harvest
- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py) — `register_cross_file_sources` / `cross_file_sources`
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — call `register_cross_file_sources` between passes

#### Tests to add

**Scenario A — two-file CTAS-then-INSERT produces a cross-file edge** (the inversion
target). New file
`tests/snowflake/E36/test_e36_xfile.py`:

```python
"""E36 cross-file: temp table defined in file A is visible to file B in pass 2."""
from pathlib import Path
import pytest
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _edges(result):
    return [
        (e.src.table.name, e.src.name, e.dst.table.name, e.dst.name)
        for stmt in result.statements
        for e in stmt.column_lineage
        if e.confidence > 0.0
    ]


def test_e36_cross_file_temp_table_resolves(tmp_path):
    file_a = tmp_path / "file_a.sql"
    file_a.write_text("CREATE TEMP TABLE t AS SELECT a FROM src;")
    file_b = tmp_path / "file_b.sql"
    file_b.write_text("INSERT INTO dst SELECT a FROM t;")

    resolver = SchemaResolver(dialect="snowflake")
    parser = SnowflakeParser(schema_resolver=resolver)
    aggregator = CrossFileAggregator()

    parsed_a = parser.parse_file(file_a, file_a.read_text())
    aggregator.register_pass1(parsed_a)
    parsed_b = parser.parse_file(file_b, file_b.read_text())
    aggregator.register_pass1(parsed_b)

    # Wire cross-file sources, then re-parse file_b (pass 2)
    resolver.register_cross_file_sources(aggregator.cross_file_sources)
    parsed_b_p2 = parser.parse_file(file_b, file_b.read_text())

    edges = _edges(parsed_b_p2)
    # The resolved edge: SRC.A → dst.a (t expanded via cross-file sources_map)
    assert any(e[0].upper() == "SRC" and e[2] == "dst" for e in edges), (
        f"Cross-file temp table did not resolve: {edges}"
    )
    # t must NOT appear as a raw source (it is expanded)
    assert not any(e[0] == "t" for e in edges), (
        f"t must be expanded, not forwarded raw: {edges}"
    )
```

**Scenario B — pass 1 in isolation produces no cross-file edge** (negative control):
in the same test file, parse `file_b` without registering cross-file sources first.
Assert no `SRC → dst` edge appears. This proves the fix is doing something, not just
that the test passes vacuously.

**Scenario C — aggregator harvest captures CTAS bodies in file order**:
unit test on `CrossFileAggregator.register_pass1`. Build a `ParsedFile` with two CTAS
statements, call `register_pass1`, assert `aggregator.cross_file_sources` contains both
lowercased keys with non-None `exp.Select` values.

**Scenario D — name collision: cross-file `t` is overridden by intra-file `t`**:
file A defines `t` via CTAS. File B has its own `CREATE TEMP TABLE t AS SELECT x FROM
other; INSERT INTO dst SELECT x FROM t`. Pass-2 file B must resolve to `OTHER`, not
`SRC`. Asserts intra-file precedence.

**Scenario E — integration regression guard**
(`tests/integration/test_e36_xfile_regression_guard.py`): index a two-file directory via
`Indexer.index_repo` end-to-end, query `MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->(d:SqlColumn)
WHERE d.table_qualified ENDS WITH 'dst' RETURN count(*)`, assert ≥ 1.

**INVERSION marker retirement**: remove the `# INVERSION TARGET` comment at
[`tests/snowflake/E36/test_e36.py` line 36](../tests/snowflake/E36/test_e36.py) and
[`tests/snowflake/test_plan_review_gates.py` line 120](../tests/snowflake/test_plan_review_gates.py).
Replace with a one-line comment pointing at the new test file:
`# Cross-file case covered by tests/snowflake/E36/test_e36_xfile.py`.

#### Acceptance criteria

- [ ] `QueryNode.defined_body` defined and populated for CTAS statements (grep confirmed)
- [ ] `CrossFileAggregator.cross_file_sources` populated during `register_pass1` (grep confirmed)
- [ ] `SchemaResolver.register_cross_file_sources` / `cross_file_sources` defined and called from `Indexer` between pass 1 and pass 2 (grep confirmed)
- [ ] `AnsiParser.parse_file` seeds `sources_map` from `self._schema.cross_file_sources()` (grep confirmed)
- [ ] `SnowflakeParser._parse_scripting_file` does the same (grep confirmed)
- [ ] Scenario A test passes: `SRC → dst` edge appears in pass-2 result for the two-file fixture
- [ ] Scenario B negative control passes: edge absent when cross-file map is not seeded
- [ ] Scenario D precedence test passes: intra-file `t` overrides cross-file `t`
- [ ] Scenario E integration regression guard passes
- [ ] `# INVERSION TARGET` removed from `E36/test_e36.py` and `test_plan_review_gates.py` E36 block
- [ ] No `TODO` in any happy path of changed files

---

### T-07-02 — FIX-E5-XFILE-CHAIN: flip the 6 anchor xfails

**Source**: [`tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py`](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py) lines 60–166 (6 `@pytest.mark.xfail(strict=True)` markers).

**Effort**: M
**Depends on**: T-07-01 (link 6 only — the etl-to-semantic-view cross-file hop)
**Blocks**: NONE — but this is the **sprint gate**

#### Root cause

Re-reading the fixture and the xfail reasons:

- [`fixture_etl.sql`](../tests/snowflake/anchors/fixture_etl.sql): single INSERT with
  4 nested CTEs (`bm_orders`, `igdc_openstaand`, `openstaand_combined` joined by
  UNION ALL, and a top-level aggregate) targeting `wtfs_openstaande_orders.ma_aantal_op_order`.
  Source: `source_facts.ma_order_aantal` (defined as a real CREATE TABLE in
  `fixture_source.sql` and loaded into the resolver in the test's fixture).
- [`fixture_semantic.sql`](../tests/snowflake/anchors/fixture_semantic.sql): separate file
  with `CREATE VIEW "Openstaande orders" AS SELECT ma_aantal_op_order AS "Aantal op order"
  FROM wtfs_openstaande_orders`.

Re-examining the xfail reasons:

| Link | xfail reason | Real reason |
|------|--------------|-------------|
| 1 | "CTE resolution with SUM aggregation" | Intra-file: `SUM(ma_order_aantal / NULLIF(...)) AS aantal_op_order` inside a CTE → `bm_orders.aantal_op_order`. Today the body inside `_extract_column_lineage` calls `sg_lineage` on the **outer SELECT** (`SELECT SUM(aantal_op_order) FROM openstaand_combined`). It never iterates CTE definitions as standalone destinations. |
| 2 | Same as link 1 | Same — intra-file CTE-as-destination |
| 3 | "CTE-to-CTE column resolution" | Same root: `bm_orders → openstaand_combined` is a CTE-to-CTE hop that requires emitting an edge per CTE projection, not just for the outer SELECT |
| 4 | Same as link 3 | Same |
| 5 | "CTE-to-INSERT column resolution" | Outer SELECT → INSERT target — partially works today via INSERT branch but the destination column name `ma_aantal_op_order` is from the INSERT column list, not from the SELECT |
| 6 | "View column resolution with quoted identifiers" | **Cross-file**: the semantic view sources from `wtfs_openstaande_orders` defined in `fixture_etl.sql`. Requires T-07-01's cross-file sources OR a different mechanism — see Step 2.4 |

The common thread for links 1–4 is: **`_extract_column_lineage` does not emit edges for
intermediate CTEs**. It only emits edges for the outermost columns of the top-level
statement. CTE columns are visited internally by `sg_lineage` only when an outer column
walks through them.

#### What to do

**Step 2.1 — Iterate CTEs as additional destinations**:

In [`_extract_column_lineage`](../src/sqlcg/parsers/base.py) after the outer
`col_expressions` loop finishes, add a second loop over `body.find_all(exp.CTE)` (or
`body.args.get("with").expressions` for the top-level With clause). For each CTE:

- Treat the CTE's name as a synthetic destination table (`TableRef(name=cte.alias)`)
- For each projection in the CTE's SELECT body, call `sg_lineage(col_name, cte_body, ...)`
  with the same `combined_sources`
- Emit edges with `dst_table = TableRef(name=cte.alias)` and `dst_col_name = projection.alias`

Note on confidence: edges into a CTE are intermediate, not user-facing. Keep
`confidence=0.9` (same as outer edges) but tag `transform="CTE_PROJECTION"` so the report
can distinguish them. Document the choice in the docstring; do not silently overload an
existing transform string.

Verification grep: `grep -n "CTE_PROJECTION" src/sqlcg/parsers/base.py` shows one occurrence.

**Step 2.2 — Aggregate test fixture parses all three files**:

The anchor test already does this in [`chain_edges` fixture lines 28–57](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py). No change needed there — verify the
fixture orderng (source → etl → semantic) is preserved so the resolver knows about
`source_facts` columns and (after T-07-01) the cross-file CTAS map is seeded.

**Step 2.3 — Pass cross-file sources to the anchor test parser**:

Today the chain fixture builds the parser once and parses each file independently with
no aggregator. Update the fixture to wire `CrossFileAggregator` the same way Scenario A
of T-07-01 does:

```python
aggregator = CrossFileAggregator()
etl_result = parser.parse_file(etl_path, etl_sql)
aggregator.register_pass1(etl_result)
resolver.register_cross_file_sources(aggregator.cross_file_sources)
semantic_result = parser.parse_file(semantic_path, semantic_sql)
```

This makes link 6 reachable (semantic view sees `wtfs_openstaande_orders` projection via
`sources_map`).

**Step 2.4 — Anchor link 6: quoted-identifier preservation**:

The xfail reason for link 6 also mentions quoted identifiers. The view defines
`SELECT ma_aantal_op_order AS "Aantal op order"`. The edges tuple uses `edge.dst.name`
which should carry the literal alias including spaces. Verify by reading
[`base.py` `_lineage_node_to_edges` line 437–447](../src/sqlcg/parsers/base.py) that the
destination column name comes from `dst_col_name` (the alias from `col_expr.alias`),
not from a normalised form. If quoted identifiers are being uppercased anywhere in the
path, fix the lowering to preserve the original casing.

Read [`base.py` line 581–594](../src/sqlcg/parsers/base.py): `col_name = col_expr.alias`
preserves casing as sqlglot parses it (which preserves quoted identifiers). The
likely-failing component is the **source** side from `_lineage_node_to_table_ref`. For
the semantic view, the source is `wtfs_openstaande_orders.ma_aantal_op_order` — that
should be uppercase already (bare identifier in Snowflake). Run the test before assuming
this is broken.

**Step 2.5 — Remove xfail markers, one per link**:

For each link, remove the `@pytest.mark.xfail(strict=True, reason=...)` decorator only
after the assertion at that line passes. Commit incrementally: 6 commits, one per link,
so that any regression bisect lands on the exact link that re-broke.

**Step 2.6 — Update E5 single-file inversion target**:

In [`tests/snowflake/E5/test_e5.py` line 27](../tests/snowflake/E5/test_e5.py), the
INVERSION TARGET comment says "when E5 cross-file resolution lands, assert the src_table
resolves to the *actual* file-qualified table". This is **already** the behaviour after
T-07-01 (the actual table name is `table_not_in_schema` — there is no file-qualified
form unless that table is defined elsewhere). Replace the INVERSION marker with a
docstring update; do not add a new assertion that always passes.

#### Wiring verification

- `grep -n "find_all(exp.CTE)\|cte.alias\|CTE_PROJECTION" src/sqlcg/parsers/base.py` → CTE iteration present.
- `grep -n "register_cross_file_sources" tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py` → fixture uses cross-file wiring.
- `grep -nE "@pytest.mark.xfail" tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py` → zero matches after this ticket.
- `grep -n "INVERSION TARGET" tests/snowflake/E5/test_e5.py` → zero matches.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — CTE iteration in `_extract_column_lineage`
- [`tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py`](../tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py) — fixture wiring + xfail removal
- [`tests/snowflake/E5/test_e5.py`](../tests/snowflake/E5/test_e5.py) — INVERSION marker cleanup

#### Tests to add

The anchor tests already exist and become the acceptance test once the xfails are
removed. No new tests required for links 1–6 themselves.

**New unit test — CTE projection emits an edge into the CTE name as destination**:
`tests/snowflake/E5/test_e5_cte_projection.py`. SQL: `WITH x AS (SELECT a FROM src) SELECT
a FROM x`. Assertion: edges contain `(SRC, A, x, a)` (CTE-as-destination intermediate
edge), in addition to the existing `(SRC, A, <output>, a)` outer edge.

This new test is independent of the anchor chain — it guards step 2.1's behaviour in
isolation so a regression there is bisectable without running the heavier anchor suite.

#### Acceptance criteria

- [ ] All 6 anchor links (`test_anchor_ma_chain_link_1..6`) pass without `xfail`
- [ ] `find_all(exp.CTE)` (or equivalent CTE iteration) present in `_extract_column_lineage` (grep confirmed)
- [ ] `CTE_PROJECTION` transform string used for CTE-destination edges (grep confirmed)
- [ ] Cross-file sources are wired in the anchor test fixture (grep confirmed)
- [ ] New `test_e5_cte_projection.py` passes
- [ ] All `# INVERSION TARGET` comments in `E5/test_e5.py` are removed or converted to factual docstrings
- [ ] No `TODO` in any changed file's happy path

---

### T-07-03 — FIX-E25: preserve qualifier on the observable edge surface

**Source**: README E25 (lines 266–280),
[`tests/snowflake/E25/test_e25.py`](../tests/snowflake/E25/test_e25.py) INVERSION TARGET
lines 29 and 48.

**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

The bug as written in the README and INVERSION TARGET is "qualifier is **not** preserved
in the lineage edge's source table name". But re-reading
[`base.py` `_lineage_node_to_table_ref` lines 460–481](../src/sqlcg/parsers/base.py)
the qualifier **is** captured in `TableRef.catalog` and `TableRef.db`. The bug is in the
test's observation surface: `edges()` helper at
[`tests/snowflake/conftest.py` line 22](../tests/snowflake/conftest.py) reads only
`edge.src.table.name` — the bare last component.

The fix is twofold:

1. Confirm `TableRef.catalog` / `TableRef.db` are populated correctly for the
   `other_db.public.tbl` case. Add a unit test that asserts on `TableRef.full_id`.
2. Update the E25 inversion target to assert on `full_id` (which already returns
   `other_db.public.tbl`), not on `name` (which is, correctly, `TBL`).

If step 1 reveals that `catalog` / `db` are in fact lost (e.g., sqlglot's lineage walker
does not preserve them on the `source` node), the fix moves into
`_lineage_node_to_table_ref` and possibly requires walking `node.expression` or `node.source`
attributes that carry the original qualifiers. Step 1's grep-confirmed test is what
distinguishes the two scenarios — write that test first.

#### What to do

**Step 3.1 — Write the diagnostic test first**:

Add `tests/snowflake/E25/test_e25_full_id.py`:

```python
"""E25: confirm whether qualifier survives to TableRef.catalog/db."""
from pathlib import Path
from tests.snowflake.conftest import parse


def _edges_full_id(result):
    return [
        (e.src.table.full_id, e.src.name, e.dst.table.full_id, e.dst.name)
        for stmt in result.statements
        for e in stmt.column_lineage
        if e.confidence > 0.0
    ]


def test_e25_three_part_full_id(parser):
    sql = "SELECT a FROM other_db.public.tbl;"
    result = parse(parser, sql, "e25.sql")
    edges = _edges_full_id(result)
    assert any(e[0].lower() == "other_db.public.tbl" for e in edges), (
        f"Three-part qualifier must survive on TableRef.full_id: {edges}"
    )


def test_e25_two_part_full_id(parser):
    sql = "SELECT a FROM other_schema.tbl;"
    result = parse(parser, sql, "e25.sql")
    edges = _edges_full_id(result)
    assert any(e[0].lower() == "other_schema.tbl" for e in edges), (
        f"Two-part qualifier must survive on TableRef.full_id: {edges}"
    )
```

**Step 3.2 — Run the test against the current parser**:

- If the assertions pass, the bug is in the test observation surface — go to step 3.3.
- If they fail, dig into `_lineage_node_to_table_ref` and identify where `catalog` / `db`
  are dropped. Fix at the source. Re-run the test. Then proceed to step 3.3.

**Step 3.3 — Flip the INVERSION TARGET in `test_e25.py`**:

Replace lines 29–30 and 47–50 with positive assertions that use a new helper
`edges_full_id()` exported from `conftest.py`. Add the helper:

```python
def edges_full_id(result):
    return [
        (e.src.table.full_id, e.src.name, e.dst.table.full_id, e.dst.name)
        for stmt in result.statements
        for e in stmt.column_lineage
        if e.confidence > 0.0
    ]
```

Then rewrite the E25 tests' INVERSION blocks to use `edges_full_id(result)` and assert
the fully-qualified name appears.

#### Wiring verification

- `grep -n "edges_full_id" tests/snowflake/conftest.py` → one definition.
- `grep -n "edges_full_id" tests/snowflake/E25/test_e25.py` → at least two usages.
- `grep -n "INVERSION TARGET" tests/snowflake/E25/test_e25.py` → zero matches.

#### Files affected

- [`tests/snowflake/conftest.py`](../tests/snowflake/conftest.py) — add `edges_full_id` helper
- [`tests/snowflake/E25/test_e25.py`](../tests/snowflake/E25/test_e25.py) — flip INVERSION TARGETs
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — **only if** step 3.2 reveals the catalog/db is actually lost

#### Tests to add

The diagnostic tests in step 3.1 (kept in the suite as `test_e25_full_id.py`) plus the
inversion flips in step 3.3.

#### Acceptance criteria

- [ ] `TableRef.full_id` for `other_db.public.tbl` equals `"other_db.public.tbl"` (case-insensitive comparison acceptable since Snowflake normalises)
- [ ] `tests/snowflake/E25/test_e25_full_id.py` passes
- [ ] `# INVERSION TARGET` removed from `E25/test_e25.py`
- [ ] `edges_full_id` helper exported from `conftest.py` (grep confirmed)
- [ ] No `TODO` in any changed file's happy path

---

### T-07-04 — DEFER-E12: document upstream gap, retire INVERSION marker

**Source**: [`tests/snowflake/E12/test_e12.py`](../tests/snowflake/E12/test_e12.py)
INVERSION TARGET lines 27 and 43.

**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

`sqlglot.lineage.lineage()` does not traverse `LATERAL FLATTEN(input => arr) f` to mark
`f.value` as derived from `tbl.arr`. The lateral subquery is a different scope; the
`value` column is a generated virtual column. Similarly, `src:address:city::STRING` is
parsed as `exp.JSONPath` and lineage does not attempt to follow JSON path expressions
back to the underlying column.

These are upstream sqlglot limitations. A local workaround would require pre-rewriting
the SQL to make the relationship explicit (e.g., `f.value` → `tbl.arr`), which is fragile
and risks producing wrong edges silently for the cases sqlglot already handles
intermittently.

#### What to do

**Step 4.1**: In `tests/snowflake/E12/test_e12.py`, replace the two `# INVERSION TARGET`
comments with:

```python
# DEFERRED: sqlglot lineage does not traverse LATERAL FLATTEN virtual columns.
# Local pre-rewriting would be fragile; revisit when sqlglot adds FLATTEN tracing
# (track upstream: search sqlglot issues for "lateral flatten lineage").
# See plan/sprint_07_open_ecodes.md T-07-04 for the deferred-decision rationale.
```

Convert the existing tolerant `if all_edges:` assertions into explicit xfail-marked
positive tests so the suite would notice if upstream sqlglot fixes either case:

```python
@pytest.mark.xfail(
    strict=False,  # non-strict: sqlglot may partially fix at any time
    reason="DEFERRED: sqlglot lineage cannot trace LATERAL FLATTEN virtual columns",
)
def test_e12_lateral_flatten_edge_when_supported(parser):
    sql = Path(__file__).with_name("e12_lateral_flatten.sql").read_text()
    result = parse(parser, sql, "e12_lateral_flatten.sql")
    edges_list = edges(result)
    assert any(e[1].upper() == "ARR" and e[3] == "col" for e in edges_list), (
        f"When sqlglot supports FLATTEN, expected tbl.arr -> col: {edges_list}"
    )
```

`strict=False` is intentional. `strict=True` would fail the suite the day sqlglot fixes
this upstream — we want it to flip to PASS silently and prompt a follow-up commit to
remove the xfail.

**Step 4.2**: Update [`tests/snowflake/README.md`](../tests/snowflake/README.md) E12
section's "Status" line:

```
**Status**: Deferred — upstream sqlglot does not support LATERAL FLATTEN / JSON path
lineage tracing. Tests use `@pytest.mark.xfail(strict=False)` and will flip to PASS
automatically when upstream support lands. See plan/sprint_07_open_ecodes.md T-07-04.
```

#### Wiring verification

- `grep -n "INVERSION TARGET" tests/snowflake/E12/test_e12.py` → zero.
- `grep -n "DEFERRED: sqlglot" tests/snowflake/E12/test_e12.py` → at least two matches.
- `grep -n "xfail" tests/snowflake/E12/test_e12.py` → two matches (one per test).

#### Files affected

- [`tests/snowflake/E12/test_e12.py`](../tests/snowflake/E12/test_e12.py)
- [`tests/snowflake/README.md`](../tests/snowflake/README.md) E12 section

#### Tests to add

None — only test markers and assertions change.

#### Acceptance criteria

- [ ] `# INVERSION TARGET` removed from `E12/test_e12.py`
- [ ] Two `xfail(strict=False)` tests with `reason="DEFERRED: ..."`
- [ ] README E12 Status line updated to "Deferred — upstream sqlglot ..."
- [ ] No `TODO` in changed files

---

### T-07-05 — DEFER-E27: document upstream gap, retire INVERSION marker

**Source**: [`tests/snowflake/E27/test_e27.py`](../tests/snowflake/E27/test_e27.py)
INVERSION TARGET lines 27 and 43.

**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

`sqlglot.lineage.lineage()` does not inline UDF bodies (the UDF definition is
unavailable). When `my_udf(src_col)` appears, sqlglot may or may not surface `src_col` as
a leaf, depending on whether it parses `my_udf` as an Anonymous function (treats args as
column refs) or as an unrecognised identifier (treats it as opaque). Current behaviour
is inconsistent, which is why the test uses `if all_edges:` rather than a hard assertion.

A local fix would require: (a) detecting `my_udf(arg)` patterns, (b) treating the
argument as a direct lineage source, (c) tagging the edge with a special transform
(`UDF_PASS_THROUGH_ASSUMED`) so consumers know this is heuristic. The risk: false positives
when the UDF is actually `random_value()` or `current_timestamp()`. Not in scope for this
sprint.

#### What to do

Same shape as T-07-04:

**Step 5.1**: Replace `# INVERSION TARGET` with `# DEFERRED: sqlglot lineage cannot
inline UDF bodies` in `tests/snowflake/E27/test_e27.py`. Convert the two tolerant tests
to `@pytest.mark.xfail(strict=False, reason="DEFERRED: ...")` with positive assertions.

**Step 5.2**: Update [`tests/snowflake/README.md`](../tests/snowflake/README.md) E27
Status to "Deferred — upstream sqlglot ...".

#### Wiring verification

- `grep -n "INVERSION TARGET" tests/snowflake/E27/test_e27.py` → zero.
- `grep -n "DEFERRED: sqlglot" tests/snowflake/E27/test_e27.py` → at least two.

#### Files affected

- [`tests/snowflake/E27/test_e27.py`](../tests/snowflake/E27/test_e27.py)
- [`tests/snowflake/README.md`](../tests/snowflake/README.md) E27 section

#### Acceptance criteria

- [ ] `# INVERSION TARGET` removed from `E27/test_e27.py`
- [ ] Two `xfail(strict=False)` tests with `reason="DEFERRED: ..."`
- [ ] README E27 Status updated
- [ ] No `TODO` in changed files

---

### T-07-06 — DEFER-E16: document upstream gap + add `col_lineage_skip:merge_branch:*` observability

**Source**: [`tests/snowflake/E16/test_e16.py`](../tests/snowflake/E16/test_e16.py)
INVERSION TARGET lines 23 and 36. README E16 lines 187–203.

**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

`_extract_column_lineage` short-circuits on MERGE: the type guard at
[`base.py` line 528](../src/sqlcg/parsers/base.py)
(`isinstance(stmt, (exp.Select, exp.Insert, exp.Create))`) excludes `exp.Merge`. The
MERGE statement is then dropped silently from column-lineage extraction — no error
string is recorded, the report shows nothing.

Implementing MERGE branch lineage means iterating `merge_stmt.expressions` (the WHEN
branches), pulling each branch's `set` or `values` assignments, and synthesising
per-branch lineage calls. sqlglot's `lineage()` API does not handle MERGE at all — it
expects a `Select` root. A correct implementation would call `lineage()` on a synthetic
SELECT per branch. That is real implementation work, not a workaround, and is not in
scope for this sprint.

This ticket records the gap explicitly (so the report shows MERGE files as failing for
a known reason rather than vanishing) and converts the INVERSION markers.

#### What to do

**Step 6.1 — Surface a structured error for MERGE statements**:

In [`_extract_column_lineage`](../src/sqlcg/parsers/base.py) before the existing type
guard at line 528, add:

```python
import sqlglot.expressions as exp
...
if isinstance(stmt, exp.Merge):
    # MERGE multi-branch column lineage is deferred (see plan/sprint_07_open_ecodes.md T-07-06).
    # Record a structured skip so the report can flag MERGE files explicitly.
    dst_name = None
    if stmt.this is not None:
        try:
            dst_name = stmt.this.name
        except Exception:
            dst_name = None
    out.errors.append(f"col_lineage_skip:merge_branch:{dst_name or '<unknown>'}")
    return LineageExtraction(edges=[], star_sources=[])
```

This is a single early-return block — it does not alter the existing
`if not isinstance(stmt, (exp.Select, exp.Insert, exp.Create)): return ...` guard. The
MERGE case is now explicit instead of falling through to the generic guard.

**Step 6.2 — Add the error string to the README mapping table**:

[`tests/snowflake/README.md`](../tests/snowflake/README.md) "Diagnosing your corpus"
section already lists error-string-to-E-code mappings. Add:

```
| `col_lineage_skip:merge_branch` | E16 |
```

**Step 6.3 — Convert `E16/test_e16.py` INVERSION TARGETs**:

The existing tests already assert `edges() == []` and pass today. Add to each:

```python
# DEFERRED: sqlglot lineage API does not visit MERGE branches.
# Implementing per-branch synthetic SELECT lineage is out of scope for sprint 07.
# When implemented, flip to assert dst_table=="DST" and srcs == {"COL_A", "COL_B"}.
# See plan/sprint_07_open_ecodes.md T-07-06.

# Observability: the parser should record the deferred skip explicitly.
assert any("col_lineage_skip:merge_branch" in e for e in result.errors), (
    f"MERGE should record merge_branch skip; got errors: {result.errors}"
)
```

This is a **new positive assertion** in the existing test (the structured skip), not just
a doc change. It guards step 6.1's behaviour.

**Step 6.4 — Update README E16 Status**:

```
**Status**: Deferred — sqlglot's `lineage()` API does not visit MERGE branches.
The parser records `col_lineage_skip:merge_branch:<dst>` so MERGE files are visible in
`sqlcg report`. See plan/sprint_07_open_ecodes.md T-07-06.
```

#### Wiring verification

- `grep -n "col_lineage_skip:merge_branch" src/sqlcg/parsers/base.py` → one append in the early-return block.
- `grep -n "col_lineage_skip:merge_branch" tests/snowflake/E16/test_e16.py` → at least two matches (one per test).
- `grep -n "col_lineage_skip:merge_branch" tests/snowflake/README.md` → one row in the mapping table.
- `grep -n "INVERSION TARGET" tests/snowflake/E16/test_e16.py` → zero.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — MERGE early-return + structured skip
- [`tests/snowflake/E16/test_e16.py`](../tests/snowflake/E16/test_e16.py) — observability assertion + DEFERRED comment
- [`tests/snowflake/README.md`](../tests/snowflake/README.md) — error string mapping + E16 Status

#### Tests to add

The observability assertion in step 6.3 is the new test. No separate file needed.

#### Acceptance criteria

- [ ] `_extract_column_lineage` records `col_lineage_skip:merge_branch:<dst>` for `exp.Merge` statements (grep confirmed)
- [ ] `# INVERSION TARGET` removed from `E16/test_e16.py`, replaced with explicit DEFERRED note
- [ ] Both E16 tests assert the skip is recorded
- [ ] README E16 Status updated and the error mapping table includes `col_lineage_skip:merge_branch`
- [ ] No `TODO` in changed files

---

### T-07-07 — MCP-FILE-PATH: surface source file paths in lineage and dependency tools

**Source**: LLM navigation requirement — an agent calling `trace_column_lineage`,
`get_downstream_dependencies`, or `get_upstream_dependencies` needs the file path for
each returned node so it can read the source SQL directly.

**Effort**: S
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

Three MCP tools return `SqlColumn` nodes without a file path:

- [`trace_column_lineage`](../src/sqlcg/server/tools.py) — `LineageNode.file` field exists
  in [`models.py` line 11](../src/sqlcg/server/models.py) but is always `None` (hardcoded
  at `tools.py` line 329).
- [`get_downstream_dependencies`](../src/sqlcg/server/tools.py) — `DependencyNode` has
  no `file` field at all ([`models.py` lines 51–53](../src/sqlcg/server/models.py)).
- [`get_upstream_dependencies`](../src/sqlcg/server/tools.py) — same.

The data is already in the graph: every `SqlTable` node carries `defined_in_file`
(written by the indexer at `indexer.py` lines 278–289). Each `SqlColumn` is linked to
its parent `SqlTable` via `HAS_COLUMN`. The three Cypher queries in
[`queries.cypher`](../src/sqlcg/core/queries.cypher) do not traverse that edge.

`find_table_usages` and `search_sql_pattern` already return `file` correctly
(via `QUERY_DEFINED_IN → File.path`) and are not changed by this ticket.

#### What to do

**Step 7.1 — Extend the three Cypher queries**:

In [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) update the three
blocks to add an `OPTIONAL MATCH` that reaches `SqlTable.defined_in_file` via
`HAS_COLUMN`:

```cypher
-- TRACE_COLUMN_LINEAGE
MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
OPTIONAL MATCH (src)<-[:HAS_COLUMN]-(t:SqlTable)
RETURN src.id AS id, src.col_name AS col_name, t.defined_in_file AS file_path

-- GET_DOWNSTREAM_DEPENDENCIES
MATCH (src:SqlColumn {id: $id})-[:COLUMN_LINEAGE]->(dst:SqlColumn)
OPTIONAL MATCH (dst)<-[:HAS_COLUMN]-(t:SqlTable)
RETURN dst.id AS id, dst.col_name AS col_name, t.defined_in_file AS file_path

-- GET_UPSTREAM_DEPENDENCIES
MATCH (dst:SqlColumn {id: $id})<-[:COLUMN_LINEAGE]-(src:SqlColumn)
OPTIONAL MATCH (src)<-[:HAS_COLUMN]-(t:SqlTable)
RETURN src.id AS id, src.col_name AS col_name, t.defined_in_file AS file_path
```

`OPTIONAL MATCH` is used so that `SqlColumn` nodes that have lost their `HAS_COLUMN` edge
(e.g., after a partial reindex) return `file_path = None` rather than being dropped from
results entirely.

**Step 7.2 — Add `file` to `DependencyNode`**:

In [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) update `DependencyNode`:

```python
class DependencyNode(BaseModel):
    name: str = Field(..., description="Name of the node")
    kind: str = Field(..., description="Kind of node (table, column, etc.)")
    file: str | None = Field(None, description="Source file path, if applicable")
```

`LineageNode.file` already exists — no change needed to that model.

**Step 7.3 — Populate `file` in the three tool implementations**:

In [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py):

`trace_column_lineage` (line 329) — change `file=None` to `file=row.get("file_path")`:
```python
lineage.append(
    LineageNode(
        name=row.get("col_name", ""),
        kind="column",
        file=row.get("file_path"),
        confidence=None,
    )
)
```

`get_downstream_dependencies` (line ~447) — add `file`:
```python
nodes.append(
    DependencyNode(
        name=row.get("col_name", ""),
        kind="column",
        file=row.get("file_path"),
    )
)
```

`get_upstream_dependencies` (line ~518) — same pattern as downstream.

#### Wiring verification

- `grep -n "file_path" src/sqlcg/core/queries.cypher` → three occurrences (one per updated block).
- `grep -n "file:" src/sqlcg/server/models.py` → two occurrences: `LineageNode` and `DependencyNode`.
- `grep -n 'file=row.get("file_path")' src/sqlcg/server/tools.py` → three occurrences.
- `grep -n "file=None" src/sqlcg/server/tools.py` → zero occurrences in the three affected tools.

#### Files affected

- [`src/sqlcg/core/queries.cypher`](../src/sqlcg/core/queries.cypher) — extend 3 query blocks
- [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) — add `file` to `DependencyNode`
- [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) — populate `file` in 3 tool implementations

#### Tests to add

Add to `tests/integration/test_mcp_tools.py` (or create it if absent):

```python
def test_trace_column_lineage_returns_file_path(tmp_path):
    """LineageNode.file must be a non-None string when the column's table has a known file."""
    # Index a minimal two-file corpus: fixture_ddl.sql defines src_table, fixture_etl.sql
    # selects from it.
    ...
    result = trace_column_lineage("src_table.col_a")
    assert result.lineage, "expected at least one upstream node"
    assert all(node.file is not None for node in result.lineage), (
        f"every LineageNode must carry a file path: {result.lineage}"
    )

def test_get_downstream_dependencies_returns_file_path(tmp_path):
    """DependencyNode.file must be a non-None string for indexed columns."""
    ...
    result = get_downstream_dependencies("src_table.col_a")
    assert result.nodes, "expected at least one downstream node"
    assert all(node.file is not None for node in result.nodes), (
        f"every DependencyNode must carry a file path: {result.nodes}"
    )
```

The two tests share a corpus fixture. A matching upstream test is not required separately
— `get_upstream_dependencies` uses the same code path; the downstream test already
confirms the shared logic.

#### Acceptance criteria

- [ ] `TRACE_COLUMN_LINEAGE`, `GET_DOWNSTREAM_DEPENDENCIES`, `GET_UPSTREAM_DEPENDENCIES` in `queries.cypher` each return `file_path` via `OPTIONAL MATCH (col)<-[:HAS_COLUMN]-(t:SqlTable)` (grep confirmed)
- [ ] `DependencyNode` has a `file: str | None` field (grep confirmed)
- [ ] `LineageNode.file` is populated from `row.get("file_path")`, not hardcoded `None` (grep confirmed)
- [ ] `test_trace_column_lineage_returns_file_path` and `test_get_downstream_dependencies_returns_file_path` pass
- [ ] No `TODO` in changed files

---

## Test Strategy

### The Single Most Important Regression Guard

Add `tests/integration/test_e36_xfile_regression_guard.py` (already specified in
Scenario E of T-07-01). It indexes a two-file directory end-to-end via `Indexer.index_repo`
and asserts at least one `COLUMN_LINEAGE` edge into `dst` exists. This guards against
silent regression of the cross-file `sources_map` plumbing for the rest of the project's
lifetime.

**Which ticket makes it pass**: T-07-01.

### Test pyramid for this sprint

| Layer | Tests | Owner ticket |
|-------|-------|--------------|
| Unit (parser internals) | `test_e36_cross_file_temp_table_resolves`, `test_aggregator_harvests_ctas_bodies`, `test_e5_cte_projection`, `test_e25_full_id` | T-07-01, T-07-02, T-07-03 |
| Snowflake suite (E-code pinning) | E5, E12, E16, E25, E27, E36 inversion flips and DEFERRED markers | All tickets |
| Anchor (sprint gate) | 6 `test_anchor_ma_chain_link_*` tests pass without xfail | T-07-02 |
| Integration | `test_e36_xfile_regression_guard.py` end-to-end via `Indexer` | T-07-01 |
| Integration (MCP) | `test_trace_column_lineage_returns_file_path`, `test_get_downstream_dependencies_returns_file_path` | T-07-07 |

No test in this sprint may use `xfail(strict=False)` other than the explicit DEFERRED
markers in T-07-04, T-07-05.

---

## Wiring Checklist

| Question | T-07-01 | T-07-02 | T-07-03 | T-07-04 | T-07-05 | T-07-06 | T-07-07 |
|----------|---------|---------|---------|---------|---------|---------|---------|
| What calls this? | `Indexer.index_repo` calls `aggregator.register_pass1` (existing) then `resolver.register_cross_file_sources(aggregator.cross_file_sources)`; `AnsiParser.parse_file` and `SnowflakeParser._parse_scripting_file` read `resolver.cross_file_sources()` | `_extract_column_lineage` iterates `body.find_all(exp.CTE)` after the outer column loop; anchor fixture wires `aggregator.register_pass1` between parses | E25 tests use new `edges_full_id` helper; if step 3.2 fails, `_lineage_node_to_table_ref` is patched | None — test markers and README only | None — test markers and README only | `_extract_column_lineage` early-returns on `exp.Merge` after appending the structured skip; E16 tests assert the skip exists | MCP tool callers (`trace_column_lineage`, `get_downstream_dependencies`, `get_upstream_dependencies`) — no new public API, existing tool signatures unchanged |
| Where is the callback/parameter passed? | `defined_body` on `QueryNode` → harvested into `aggregator.cross_file_sources` → registered on resolver → seeded into `sources_map` → already merged into `combined_sources` via existing T-01 path | `cte.alias` → synthetic `TableRef(name=cte.alias)` → passed as `dst_table` to per-CTE-column `sg_lineage` calls | `edges_full_id` helper exported from `tests/snowflake/conftest.py` | N/A | N/A | `out.errors.append(...)` records the skip string | `row.get("file_path")` from updated Cypher → `LineageNode.file` / `DependencyNode.file` |
| What constant/path does this align with? | `sources_map` key is `(target.name or "").lower()` — matches existing intra-file key convention at `ansi_parser.py` line 109. `cross_file_sources` is a method on `SchemaResolver`, parallel to existing `as_dict()` and `as_sources_dict()` | `transform="CTE_PROJECTION"` — new constant, must not collide with existing values `"SELECT"`, `"UNKNOWN"`, `"AGGREGATION"` | `TableRef.full_id` (defined at `base.py` line 53) | N/A | N/A | Error string format `col_lineage_skip:<reason>:<col>` matches existing `col_lineage_skip:dynamic_source:*`, `col_lineage_skip:star:*`, `col_lineage_skip:expr_no_name:<empty>` | `SqlTable.defined_in_file` (written by indexer at `indexer.py` lines 278–289); `HAS_COLUMN` edge already in schema |
| Does any TODO remain in the happy path? | No | No | No | No | No | No | No |

---

## Sprint-Level Acceptance Criteria

- [ ] All 6 `test_anchor_ma_chain_link_*` tests in `tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py` pass without any `@pytest.mark.xfail` decorator (the sprint gate)
- [ ] The cross-file E36 scenario produces at least one `COLUMN_LINEAGE` edge end-to-end via `Indexer.index_repo` (`tests/integration/test_e36_xfile_regression_guard.py` passes)
- [ ] Zero `# INVERSION TARGET` comments remain in `tests/snowflake/{E5,E12,E16,E25,E27,E36}/`. Each comment is either removed (because the assertion was flipped to positive) or replaced with a `DEFERRED: sqlglot ...` reference pointing back at this plan
- [ ] `sqlcg report --stdout` on a corpus containing MERGE statements shows `col_lineage_skip:merge_branch` rows (manual verification)
- [ ] No `TODO` comment remains in the happy path of `aggregator.py`, `schema_resolver.py`, `ansi_parser.py`, `snowflake_parser.py`, `base.py`, or `indexer.py`
- [ ] `uv run pytest tests/snowflake/ -v` exits with 0 failures and only the explicit `DEFERRED` xfails (E12, E27) marked as XFAIL
- [ ] `trace_column_lineage`, `get_downstream_dependencies`, and `get_upstream_dependencies` return non-`None` `file` on every node whose parent table has a `defined_in_file` (`test_trace_column_lineage_returns_file_path` and `test_get_downstream_dependencies_returns_file_path` pass)
- [ ] `uv run ruff check src tests` and `uv run pyright` pass

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Cross-file `sources_map` causes incorrect resolution when two files define a temp table named `t` with different bodies | MEDIUM | Scenario D test of T-07-01 asserts intra-file precedence. The seeding order in `AnsiParser.parse_file` (cross-file first, intra-file overwrites) makes this deterministic |
| CTE iteration in T-07-02 emits duplicate edges (once via outer column walk, once via CTE projection loop) | MEDIUM | Use `transform="CTE_PROJECTION"` so CTE-destination edges are distinguishable. Add unit test asserting outer SELECT edge count is unchanged by the new CTE loop |
| Anchor link 6 still fails after T-07-01 because the semantic view uses quoted Dutch column names with spaces | MEDIUM | Investigate during T-07-02 step 2.4. If quoted-identifier preservation is the actual blocker, scope it as a follow-up ticket — do not silently leave the xfail in |
| `sqlglot` updates change `LATERAL FLATTEN` behaviour, causing `xfail(strict=False)` to start passing | LOW (intended) | DEFER tickets explicitly use `strict=False` so unexpected upstream fixes do not fail CI. A follow-up commit removes the xfail when seen |
| `register_cross_file_sources` is called twice in tests, leaving stale state | LOW | The setter does `dict(sources)` — replaces, does not merge. Documented in docstring. Add unit test: call twice with different inputs, assert second value wins |
| The new `defined_body` field on `QueryNode` breaks JSON serialisation if `QueryNode` is ever dumped | LOW | `QueryNode` is not serialised to JSON anywhere (verified by grep `json.dumps.*QueryNode` returns zero). If a future MCP tool needs to serialise, add `__getstate__` or exclude `defined_body` then |
| `MERGE` early-return in T-07-06 inadvertently skips MERGE statements that DO have valid lineage in some sqlglot future | LOW | The skip is gated on `isinstance(stmt, exp.Merge)`. If sqlglot adds lineage support, this branch can be reopened with full MERGE handling. The `col_lineage_skip:merge_branch` string makes the skip visible in the report so a future maintainer notices |
| T-07-07: `SqlColumn` node has no `HAS_COLUMN` edge (e.g., after partial reindex or star-expansion column) — `OPTIONAL MATCH` returns `NULL` and `file` is `None` | LOW | `OPTIONAL MATCH` is intentional — callers must handle `file=None` gracefully. Integration tests assert non-None only against a fully indexed corpus where every column has a parent table. |

---

## Blocking Questions

None at plan-time. All design choices are resolved against verified source code above.
If T-07-02 step 2.4 reveals a quoted-identifier bug that is too large to fix inside this
sprint, the developer must surface a blocking question rather than silently leaving the
link 6 xfail in place.

---

## Deviations

### T-07-02: CTE iteration implemented but complex chains remain unresolved

- **Reason**: CTE iteration code was implemented (Step 2.1) and seeded combined_sources with CTE bodies. However, sqlglot.lineage() cannot trace aggregations (SUM, COUNT, etc.) or UNION ALLs within CTE bodies when those CTEs reference other CTEs. The anchor test fixture involves CTE chains with aggregations, which hit this sqlglot limitation.

- **Change**: 
  * CTE iteration loop added to `_extract_column_lineage` (lines 765-844 in base.py)
  * combined_sources seeded with CTE bodies from the WITH clause (lines 580-589 in base.py)
  * CTE projections emitted as `LineageEdge` with `transform="CTE_PROJECTION"` (but still don't resolve complex chains)
  * MERGE early-return and E25 qualifier fixes completed as planned
  * E12, E16, E27 DEFER tickets completed as planned

- **Impact**: 
  * T-07-02 anchor tests remain XFAIL (all 6 links)
  * T-07-03, T-07-04, T-07-05, T-07-06 completed and passing
  * Sprint gate (6 anchor links) is not met, but 4 of 6 tickets (T-07-03 through T-07-06) are complete
  * No regression in existing tests (all 359 unit/integration tests pass)

- **Date**: 2026-05-12
