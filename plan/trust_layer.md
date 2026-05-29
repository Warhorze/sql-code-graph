# Feature Plan: Trust Layer for MCP Analysis Payloads

Plan date: 2026-05-29
Author: architect-planner
Builds on: [`plan/sprint_10_anchor_tools.md`](sprint_10_anchor_tools.md) (PR-01 … PR-07, all committed on `feat/sprint-10-anchor-tools`)
Source authority: User feature request (trust layer, two ideas A + B); [`CLAUDE.md`](../CLAUDE.md) invariants; [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §2.3 (honest accuracy / `schema_required` surfacing), §3.4 (silent failure undermines failure inventory).

---

## Summary

Make the boundary between **cold graph facts** and **heuristic interpretations** explicit
and consistent across the analysis MCP tools, and extend the golden harness to freeze
**answers** to higher-level analysis questions (exact counts, dead-code ground truth,
top-hub ranking) as fixture truth — validating the tool *payload*, never LLM prose.

This is a single foundation slice, not the whole analysis engine. It adds the labelling
shape, retrofits it onto exactly the tools whose output already mixes fact and
interpretation, introduces one new heuristic tool (dead-code) and one new fact tool
(hub ranking) so the boundary has a worked example on each side, and wires three
answer-anchors into the existing golden harness.

---

## Scope

### In Scope

1. **Fact/heuristic discriminator** — a small reusable Pydantic shape in
   [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) and the discriminator field
   added to the analysis-tool payloads that currently emit interpretation as if it were fact.
2. **Retrofit existing tools**: `get_change_scope` and `scope_change` — their `risk_label`
   is a heuristic emitted today as a bare string with no confidence and no reason.
3. **One new heuristic tool**: `analyze_unused` MCP tool (dead-code *candidate* — heuristic,
   confidence + reason), surfacing the existing `analyze unused` CLI logic over MCP with the
   trust shape. Cold facts inside it ("zero within-corpus SELECTS_FROM references") are
   labelled as facts; "may be dead code" is labelled heuristic.
4. **One new fact tool**: `get_hub_ranking` (centrality / reuse by downstream-dependent
   count) — a pure deterministic graph aggregation, presented as fact, no heuristic label.
5. **Answer-anchors**: extend the existing golden harness
   [`tests/e2e/test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py) +
   `golden_lineage.yaml` schema with three new optional, separately-gated answer fields
   (exact downstream count, dead-code ground truth, top-k hub membership) — reusing the
   `_score()` / `table_status: curated` enforcement pattern, NOT a parallel fixture.

### Non-Goals

- **KPI / derived-metric discovery is OUT.** It is the least deterministic idea in the
  brief and there is no graph signal that distinguishes a KPI from any other computed
  column without semantic input. No tool, no field, no anchor for it in this PR. If it is
  ever built it must be gated behind the heuristic label defined here — it must never emit
  fact-shaped output. Recorded as an explicit non-goal so a future contributor does not
  smuggle it into a fact payload.
- No changes to [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) or
  [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) hot paths (CLAUDE.md
  performance invariants stay intact; neither file is touched).
- No new node labels or schema DDL changes — no re-index required. All new queries read the
  existing schema (`SELECTS_FROM`, `COLUMN_LINEAGE`, `DEFINED_IN`, `HAS_COLUMN`).
- No retrofit of the pure-fact tools (`find_definition`, `get_backfill_order`, `diff_impact`,
  `trace_column_lineage`, `get_downstream/upstream_dependencies`). Their outputs are already
  deterministic graph reads; adding a redundant `assertion_type: fact` to every field is
  noise. The discriminator is applied only where fact and interpretation are *mixed* (the
  risk label) or where the whole tool is heuristic (dead-code).
- No removal or restyle of existing `LineageNode.confidence` (edge-level confidence stays as
  is — it is already correct and is a fact about how the edge was derived, not a heuristic
  interpretation of intent).

---

## Design

### The discriminator shape

The codebase already uses `kind` for node/statement type (table/column/SELECT/VIEW in
[`models.py`](../src/sqlcg/server/models.py) lines 10, 50, 69, 102, 161). **Do not overload
`kind`.** Use a distinct field name.

New reusable model in [`models.py`](../src/sqlcg/server/models.py). Note: the module
currently imports only `from pydantic import BaseModel, Field` (line 3) — the developer must
add `model_validator` to that import and `from typing import Literal`.

```python
from typing import Literal

class Judgement(BaseModel):
    """A labelled analysis result distinguishing a deterministic graph fact from a
    heuristic interpretation. Heuristics carry confidence and a human-readable reason;
    facts do not (a fact does not need to justify itself)."""

    assertion_type: Literal["fact", "heuristic"] = Field(
        ...,
        description="'fact' = deterministic graph read (counts, references, edges). "
        "'heuristic' = an interpretation that may be wrong; see confidence and reason.",
    )
    label: str = Field(
        ..., description="The interpretation or fact statement (e.g. 'high', 'dead_code_candidate')."
    )
    confidence: float | None = Field(
        None,
        description="0.0-1.0 UNCALIBRATED heuristic estimate — a coarse self-assessment, "
        "NOT a measured probability or frequency. Do not treat as calibrated until the "
        "golden anchor set is large enough to measure false-positive rates. REQUIRED when "
        "assertion_type='heuristic'; MUST be None when assertion_type='fact'.",
    )
    reason: str | None = Field(
        None,
        description="Short human-readable basis for a heuristic. MUST cite the concrete "
        "fact(s) it is derived from (e.g. '34 downstream dependents >= threshold 20', "
        "'zero within-corpus SELECTS_FROM references') so the caller can reason from the "
        "grounded fact even if it ignores the confidence number. REQUIRED when "
        "assertion_type='heuristic'.",
    )

    @model_validator(mode="after")
    def _check_invariants(self) -> "Judgement":
        if self.assertion_type == "heuristic":
            if self.confidence is None or self.reason is None:
                raise ValueError("heuristic Judgement requires confidence and reason")
        else:  # fact
            if self.confidence is not None or self.reason is not None:
                raise ValueError("fact Judgement must not carry confidence or reason")
        return self
```

Rationale for the validator: the brief's hard rule is "heuristics must never be emitted as
if they were facts." A Pydantic `model_validator` makes that structurally impossible — a
heuristic with no reason, or a fact with a confidence, fails construction. This is the
trust guarantee, enforced in code rather than by convention.

### API changes

**`ChangeScopeResult` / `ScopeChangeResult`** — add one field:

```python
risk: Judgement = Field(..., description="Heuristic risk interpretation (assertion_type='heuristic').")
```

Keep `risk_label: str` and `risk_weight: int` for backward field-compatibility *within this
PR's own surface* — NO, see decision (a): the project has a no-backward-compat rule
(re-index is the migration path; same spirit applies to the MCP surface which is consumed
by an LLM per-call, not persisted). **Replace** `risk_label`/`risk_weight` with `risk:
Judgement`. `risk.label` carries the old `risk_label` value; `risk.reason` **must cite the
grounding fact** — the concrete downstream count and the threshold it crosses (e.g. `"57
downstream dependents >= threshold 20"`), not a vague phrase — so the caller can reason from
the count even if it discards the confidence number; `risk.confidence` is a fixed,
*uncalibrated* heuristic confidence (see below). The downstream count, a fact, moves to a
sibling fact field `downstream_count: int` (it is a cold count, not part of the heuristic) —
so the same number appears once as a bare fact and once, named, inside the heuristic's reason.

Net field change on `ChangeScopeResult`:
- remove `risk_label: str`, `risk_weight: int`
- add `downstream_count: int` (fact — already equals `len(affected_tables)`)
- add `risk: Judgement` (heuristic)

Same change mirrored on `ScopeChangeResult`.

**New tool `analyze_unused`** → `UnusedTablesResult`:

```python
class UnusedCandidate(BaseModel):
    table_qualified: str
    within_corpus_references: int          # FACT: count of SELECTS_FROM consumers (0 here)
    dead_code: Judgement                   # HEURISTIC: assertion_type='heuristic'

class UnusedTablesResult(BaseModel):
    candidates: list[UnusedCandidate]
    total_tables_scanned: int              # FACT
    hint: str | None
```

The `dead_code` Judgement always has `assertion_type="heuristic"`,
`label="dead_code_candidate"`, `reason="zero within-corpus SELECTS_FROM references; may be
consumed externally (BI, API, COPY INTO)"`, `confidence=0.5` (deliberately mid — the brief
names external consumption as the dominant false-positive source; ARCHITECTURE_REVIEW §6
documents COPY-INTO / external-consumer blind spots).

**New tool `get_hub_ranking`** → `HubRankingResult`:

```python
class HubEntry(BaseModel):
    table_qualified: str
    downstream_dependents: int             # FACT: distinct consuming tables
    rank: int                              # FACT: 1-based position

class HubRankingResult(BaseModel):
    top: list[HubEntry]                    # all FACTS — no Judgement field
    k: int
    hint: str | None
```

`get_hub_ranking` carries **no `Judgement`** — every field is a deterministic count or
ordinal. This is the worked example that a fact tool is NOT forced to wear the heuristic
shape. The brief says facts should be "present[ed] as facts"; the cleanest expression of
that is: a fact tool returns plain typed fields, the discriminator appears only where
interpretation enters.

### Data models / queries

Two new named Cypher blocks in [`queries.cypher`](../src/sqlcg/core/queries.cypher),
exported from [`queries.py`](../src/sqlcg/core/queries.py). Both are **single aggregations**
— no Python per-row traversal, no O(N²) (CLAUDE.md performance invariant respected):

```cypher
-- ANALYZE_UNUSED_TABLES
MATCH (t:SqlTable)
WHERE NOT (t)<-[:SELECTS_FROM]-()
RETURN t.qualified AS table_qualified
ORDER BY t.qualified

-- HUB_RANKING
-- Table-level dependent count = number of DISTINCT consuming tables (target_table of a
-- query that SELECTS_FROM this table). One aggregation, indexed on SqlTable PK.
MATCH (t:SqlTable)<-[:SELECTS_FROM]-(q:SqlQuery)
WHERE q.target_table <> '' AND q.target_table <> t.qualified
RETURN t.qualified AS table_qualified,
       count(DISTINCT q.target_table) AS downstream_dependents
ORDER BY downstream_dependents DESC, t.qualified
LIMIT $k
```

Both queries filter only on indexed primary-key properties / existing edges. `analyze
unused` reuses the exact CLI predicate (`NOT (t)<-[:SELECTS_FROM]-()`, verified in
[`analyze.py`](../src/sqlcg/cli/commands/analyze.py) line 102). `total_tables_scanned` comes
from the existing `db_info` style `MATCH (t:SqlTable) RETURN count(t)` — reuse, do not invent
a new node label.

Noise filtering: both new tools call `NoiseFilter.from_config()` (PR-02) so `_bck` snapshot
tables do not appear as dead-code candidates or hubs. This keeps them consistent with the
rest of the anchor surface.

### Dependencies

- Pydantic `model_validator` (already a dependency; `pydantic` v2).
- `NoiseFilter` (PR-02, merged).
- `_risk_label` helper (tools.py line 279, merged) — reused to compute `risk.label`.
- Golden harness `_score()` / `evaluate()` (PR-07, merged).

No new third-party dependency.

---

## Implementation Steps

### Phase 1 — Discriminator shape (foundation)

**Step 1.1** — Add `Judgement` model with the `model_validator` invariant.
- Files: [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py)
- Acceptance: `Judgement(assertion_type="fact", label="x", confidence=0.5)` raises
  `ValidationError`; `Judgement(assertion_type="heuristic", label="x")` (no reason) raises;
  `Judgement(assertion_type="heuristic", label="high", confidence=0.7, reason="...")`
  constructs. Unit test asserts all three.

### Phase 2 — Retrofit risk onto existing tools

**Step 2.1** — Change `ChangeScopeResult`: remove `risk_label`/`risk_weight`, add
`downstream_count: int` and `risk: Judgement`.
- Files: [`models.py`](../src/sqlcg/server/models.py)

**Step 2.2** — Update `get_change_scope` in [`tools.py`](../src/sqlcg/server/tools.py) to
build the `Judgement`:
```python
n = len(affected_tables)
risk = Judgement(
    assertion_type="heuristic",
    label=_risk_label(n),
    confidence=0.6,
    reason=f"{n} downstream dependent table(s); thresholds safe=0/low<=5/medium<=20/high>20",
)
```
`downstream_count = n` (fact). `confidence=0.6` is a fixed, *uncalibrated* heuristic
confidence: the count is exact but the *risk interpretation* of that count is a coarse
heuristic (a 25-dependent control table may be low-actual-risk). The `reason` embeds the
exact count `n` and the thresholds so the caller can reason from the fact, not the number.
- Acceptance: `get_change_scope` returns `result.risk.assertion_type == "heuristic"`,
  `result.risk.label in {"safe","low","medium","high"}`, `str(n)` appears in
  `result.risk.reason` (reason is fact-grounded, not vague), and
  `result.downstream_count == len(result.affected_tables)`.

**Step 2.3** — Mirror onto `ScopeChangeResult` + `scope_change`: replace `risk_label`/
`risk_weight` with `risk: Judgement` + `downstream_count`, threading the value through from
the `get_change_scope` delegate (scope_change already calls it — tools.py line 912).
- Acceptance: `scope_change("...").risk.assertion_type == "heuristic"`.

**Step 2.4** — Update existing integration tests in
[`tests/integration/test_anchor_tools.py`](../tests/integration/test_anchor_tools.py)
(`test_risk_label_thresholds`, `test_change_scope_*`, `test_scope_change_*`) to assert on
`result.risk.label` / `result.downstream_count` instead of `result.risk_label` /
`result.risk_weight`. These are the call sites that break under the no-backward-compat
field rename; updating them is part of this PR, not a follow-up.

### Phase 3 — New heuristic tool: analyze_unused

**Step 3.1** — Add `ANALYZE_UNUSED_TABLES` Cypher + export.
- Files: [`queries.cypher`](../src/sqlcg/core/queries.cypher), [`queries.py`](../src/sqlcg/core/queries.py)

**Step 3.2** — Add `UnusedCandidate` + `UnusedTablesResult` models.

**Step 3.3** — Add `analyze_unused()` MCP tool in [`tools.py`](../src/sqlcg/server/tools.py),
decorated `@mcp.tool()` + `@_timed_tool("analyze_unused")`. Query unused tables, apply
`NoiseFilter`, build one `UnusedCandidate` per kept table with a heuristic `dead_code`
Judgement, count `total_tables_scanned` via `MATCH (t:SqlTable) RETURN count(t)`.
- Acceptance: see test scenarios.

### Phase 4 — New fact tool: get_hub_ranking

**Step 4.1** — Add `HUB_RANKING` Cypher + export.

**Step 4.2** — Add `HubEntry` + `HubRankingResult` models (no `Judgement` field).

**Step 4.3** — Add `get_hub_ranking(k: int = 10)` MCP tool, `@mcp.tool()` +
`@_timed_tool("get_hub_ranking")`. Run the single aggregation with `{"k": k}`, apply
`NoiseFilter` to drop backup tables (re-rank after filtering so `rank` is contiguous), assign
1-based `rank`.
- Acceptance: see test scenarios.

### Phase 5 — Answer-anchors in the golden harness

**Step 5.1** — Extend the golden YAML schema (documented in the harness docstring) with three
new optional, independently-gated per-entry fields:
- `expected_downstream_count: int` — exact dependent-count answer (fact anchor for centrality
  / change-scope count).
- `expected_dead_code: bool` — dead-code ground truth (no-false-positive anchor).
- `expected_top_hub_rank: int` — the entry's `target_table` must appear at or above this
  rank in `get_hub_ranking` output (top-k membership anchor).

**Step 5.2** — Add harness helpers reusing existing BFS/aggregation patterns:
- `_downstream_count(db, table_qualified)` → `len(_table_blast_radius(db, table_qualified))`
  (reuse the PR-07 helper; do not re-traverse).
- `_is_dead_code(db, table_qualified)` → no consuming `SELECTS_FROM` (one Cypher read).
- `_hub_rank(db, table_qualified, k)` → run the same `HUB_RANKING` Cypher, return 1-based
  index or `None`. The harness helper must run the query with a large `k` (e.g. `k=1000`),
  NOT the user-facing default of 10 — otherwise a table with genuine dependents that ranks
  below top-10 returns `None` and produces a false-negative anchor failure.

**Step 5.3** — In `evaluate()`, for each entry carrying one of the new keys AND
`table_status: curated`, produce a scored result row with `scope` set to `"downstream_count"`
/ `"dead_code"` / `"hub_rank"`. For `expected_downstream_count`, do **NOT** call `_score()`
(it operates on `set[str]` and computes set intersection — an integer count cannot be passed
to it; Pyright would flag the type and Kuzu sets hold strings, not ints). Emit a result row
directly with `recall = 1.0 if actual_count == expected_count else 0.0`, `precision = recall`,
`f1 = recall`, `missing = [] if recall == 1.0 else [str(expected_count)]`, `spurious = []`,
`status = table_status`. The existing `RECALL_FLOOR` enforcement in
`test_golden_lineage_quality` covers this row because `status == "curated"`.
For dead-code and hub-rank, likewise a boolean pass/fail mapped to recall 1.0/0.0 (`status =
table_status`) so the existing `RECALL_FLOOR` enforcement covers them with no new gate.
Gate strictly on key presence: when none of the three keys is on an entry, `evaluate()` must
add **no** non-`column` scope row for it (preserves `test_evaluate_handles_missing_downstream_key`).

**Step 5.4** — PR unit scenarios (no golden file / no SQLCG_DB_PATH needed), mirroring the
existing PR-07 unit tests at the bottom of
[`test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py):
- `_downstream_count` returns the exact integer on a built 3-table chain.
- `_is_dead_code` returns True for a table with no consumers, False for one with consumers.
- `_hub_rank` ranks the most-referenced table first on a fan-in fixture.

---

## Test Strategy

### Unit tests
- [`tests/unit/test_judgement.py`](../tests/unit/test_judgement.py) NEW — the three validator
  scenarios in Step 1.1 (fact-with-confidence rejected, heuristic-without-reason rejected,
  valid heuristic accepted). Asserts on raised `ValidationError` and on accepted field values.

### Integration tests (real in-memory KuzuDB, reuse `_index_fixture` helper)
In [`tests/integration/test_anchor_tools.py`](../tests/integration/test_anchor_tools.py):

- **analyze_unused — fact + heuristic separation**: index `producer.sql` (defines
  `ba.used` consumed by a view) and `orphan.sql` (defines `ba.orphan`, no consumers). Assert
  `ba.orphan` is in candidates with `within_corpus_references == 0` (fact) and
  `dead_code.assertion_type == "heuristic"`, `dead_code.confidence == 0.5`,
  `dead_code.reason` non-empty; assert `ba.used` is NOT a candidate.
- **analyze_unused — backup excluded**: add `ba.orphan_bck`; assert it is not a candidate.
- **get_hub_ranking — exact ranking is fact**: index a fan-in (`ba.hub` consumed by
  `ba.c1`, `ba.c2`, `ba.c3`; `ba.lonely` consumed by `ba.c1` only). Assert
  `top[0].table_qualified == "ba.hub"`, `top[0].downstream_dependents == 3`,
  `top[0].rank == 1`, and no `Judgement` field exists on `HubEntry` (the model has no such
  attribute — assert via `not hasattr`).
- **get_hub_ranking — k caps results and backup excluded**: assert `len(top) <= k` and a
  `_bck` table never appears.
- **risk retrofit** (Step 2.4 updates): `get_change_scope` / `scope_change` assert
  `risk.assertion_type == "heuristic"`, `downstream_count` equals the affected count, and
  `str(downstream_count)` appears in `risk.reason` (the heuristic's basis is fact-grounded,
  not a vague label).

### E2E / golden harness
The Step 5.4 unit scenarios run with no external data. The full answer-anchor enforcement
runs only when a curated `golden_lineage.yaml` + `SQLCG_DB_PATH` are present (skips cleanly
otherwise — existing harness invariant, must remain).

---

## Acceptance Criteria

- [ ] `Judgement` rejects a fact carrying confidence/reason and a heuristic missing
      confidence/reason (unit test, both raise `ValidationError`).
- [ ] `grep -n "risk_label\|risk_weight" src/sqlcg/server/models.py` returns zero results
      (renamed away under no-backward-compat).
- [ ] `get_change_scope(...).risk.assertion_type == "heuristic"` and `.risk.reason` is
      non-empty; `.downstream_count` equals `len(.affected_tables)`.
- [ ] `scope_change(...).risk.assertion_type == "heuristic"`.
- [ ] `analyze_unused` is an `@mcp.tool()`; each candidate's `within_corpus_references` is a
      fact integer and `dead_code.assertion_type == "heuristic"` with confidence and reason.
- [ ] `analyze_unused` and `get_hub_ranking` both call `NoiseFilter.from_config()`
      (grep-confirmed) and exclude `_bck` tables (test-confirmed).
- [ ] `get_hub_ranking` is an `@mcp.tool()` returning ranked facts; `HubEntry` has NO
      `Judgement`/`confidence`/`reason` attribute (fact tool stays a fact tool).
- [ ] `get_hub_ranking` ranks the known hub at `rank == 1` with the exact dependent count
      (test-confirmed) — this is the centrality answer-anchor.
- [ ] `ANALYZE_UNUSED_TABLES` and `HUB_RANKING` are single Cypher aggregations (no Python
      per-row graph calls in their tool bodies beyond the one query + the noise filter);
      no edit to `base.py` or `indexer.py`.
- [ ] Golden harness accepts `expected_downstream_count` / `expected_dead_code` /
      `expected_top_hub_rank`, scores them under `table_status: curated`, and the new code
      path does not error when those keys are absent (unit scenario).
- [ ] `grep -n "model_validator\|from typing import Literal" src/sqlcg/server/models.py`
      returns results for both (validator + `Literal` imports added).
- [ ] `test_evaluate_handles_missing_downstream_key` still passes after Phase 5 — `evaluate()`
      adds no non-`column` scope rows when the three new anchor keys are absent from an entry.
- [ ] No `# TODO` in the happy path of any new tool or harness helper.
- [ ] Every new tool/method has a grep-confirmed call site (`@mcp.tool()` registration for
      tools; harness helpers called from `evaluate()`).

---

## Scope Decisions (explicit, per the brief)

**(a) The discriminator and which tools adopt it.**
Discriminator = a `Judgement` sub-model with `assertion_type: Literal["fact","heuristic"]`,
plus `confidence`/`reason` REQUIRED for heuristics and FORBIDDEN for facts (enforced by a
`model_validator`). Field name is `assertion_type`, NOT `kind` (the codebase already uses
`kind` for node/statement type — verified at models.py lines 10/50/69/102/161). Adopted by:
`get_change_scope` and `scope_change` (their `risk` is heuristic — `risk_label`/`risk_weight`
are REPLACED, per no-backward-compat, by `risk: Judgement` + a `downstream_count` fact), and
the new `analyze_unused` (dead-code is heuristic). NOT adopted by the pure-fact tools
(`find_definition`, `get_backfill_order`, `diff_impact`, dependency/lineage tools,
`get_hub_ranking`) — they return plain typed facts; forcing a redundant `assertion_type:
fact` on every field would be noise and would dilute the signal. The discriminator marks the
*boundary*, not every field.

**(b) Which 2-3 analysis questions get answer-anchors now.**
Three, all reusing the existing golden harness:
1. *Exact downstream-dependent count* (`expected_downstream_count`) — the count fact behind
   change-scope risk and hub ranking.
2. *Dead-code ground truth, no false positives* (`expected_dead_code`) — validates
   `analyze_unused` does not flag an externally-consumed table.
3. *Top-k hub membership* (`expected_top_hub_rank`) — validates `get_hub_ranking` ranks the
   known hub correctly.
Deferred: column-level exact-N-dependents per individual column (the table-level count covers
the foundation; per-column count anchors can follow once these stick), and any KPI anchor
(KPI discovery is out entirely — see Non-Goals).

**(c) Is hub/centrality in scope now? YES.**
It is genuinely new but cheap: a single `count(DISTINCT q.target_table)` aggregation over
existing `SELECTS_FROM` edges, filtered on indexed PKs — no O(N²), no `base.py`/`indexer.py`
change, no re-index. It is the clean *fact* exemplar opposite the dead-code *heuristic*
exemplar, so the trust boundary has one worked tool on each side in this same PR. It gets the
top-k answer-anchor (decision b.3). It carries no `Judgement` — it is pure fact.

**(d) Extend `golden_lineage.yaml` or new fixture? EXTEND.**
Extend the existing harness and YAML schema. The harness already drives scoring via
`evaluate()` → `_score()` with the `table_status: curated` enforcement gate and already
freezes table-level upstream/downstream truth (PR-07). Answer-anchors are the same kind of
frozen-truth assertion at a higher level; a parallel fixture would duplicate the load/skip/
score/enforce machinery and the gitignore handling, and split the curated ground truth across
two files. Reuse keeps one source of truth and one enforcement path. New per-entry keys are
optional and independently gated, exactly like `expected_downstream_tables` was added in
PR-07 without disturbing column-level entries.

---

## Deviations (recorded during implementation)

**D1 — `HUB_RANKING` Cypher self-reference filter rewritten via `WITH` (platform constraint, semantically identical).**
The plan specified the self-exclusion inline in the initial `WHERE`:

```cypher
MATCH (t:SqlTable)<-[:SELECTS_FROM]-(q:SqlQuery)
WHERE q.target_table <> '' AND q.target_table <> t.qualified
RETURN t.qualified AS table_qualified,
       count(DISTINCT q.target_table) AS downstream_dependents
ORDER BY downstream_dependents DESC, t.qualified
LIMIT $k
```

KuzuDB raised `Variable t is not in scope` for the `t.qualified` reference once the
clause entered the aggregation context. The implemented query
([`queries.cypher`](../src/sqlcg/core/queries.cypher) lines 102–109) projects both
qualifiers into aliases with a `WITH` and applies the self-exclusion afterward:

```cypher
MATCH (t:SqlTable)<-[:SELECTS_FROM]-(q:SqlQuery)
WHERE q.target_table <> ''
WITH t.qualified AS table_qualified, q.target_table AS consumer_table
WHERE consumer_table <> table_qualified
RETURN table_qualified, count(DISTINCT consumer_table) AS downstream_dependents
ORDER BY downstream_dependents DESC, table_qualified
LIMIT $k
```

Assessment: **semantically identical to the plan's intent.** It is still a single
aggregation (no Python per-row traversal, no O(N²)); the metric is still
`count(DISTINCT consumer_table)` (distinct consuming tables — no query-level
double-count, per Open Decision 2); the `<> ''` empty-target guard and the
self-reference exclusion are both preserved (now in two clauses instead of one). The
`WITH` is a pure projection/rename, not a re-aggregation or a re-scope, so it cannot
silently reorder the ranking. Verified by `test_hub_ranking_exact_fact`
([`tests/integration/test_anchor_tools.py`](../tests/integration/test_anchor_tools.py)
line 462), which asserts the known fan-in hub ranks at `rank == 1` with the exact
`downstream_dependents == 3` (3 distinct consumers, self excluded). Legitimate platform
constraint; ranking semantics unchanged.

---

## Open Decisions (genuinely unresolved — flagged for the user / reviewer)

These do not block implementation (sensible defaults are in the plan) but the user owns the
final call:

1. **Heuristic confidence values are placeholders — only the numeric calibration is deferred.**
   `dead_code` confidence = 0.5, `risk` confidence = 0.6 are reasoned defaults, not measured.
   There is no calibration data yet (the full gold standard is still being built — see
   sprint-10 §"will we create the gold standard before or after"). Once dead-code
   false-positive rate and risk-label accuracy are measured against the curated anchor set,
   these constants should be revisited.
   **Mitigations applied this PR** (so the uncalibrated number cannot mislead): (i) the
   `confidence` field description explicitly states it is uncalibrated and not a measured
   probability; (ii) every heuristic `reason` MUST cite the concrete grounding fact (the
   downstream count + threshold, the zero-reference observation) so a caller can reason from
   the fact even if it ignores the number — enforced by acceptance checks asserting the count
   appears in `risk.reason`; (iii) **no answer-anchor is ever curated on a confidence value** —
   anchors freeze facts and boolean/ranking outcomes only. The integration test pinning
   `dead_code.confidence == 0.5` exists to force any future change to be deliberate, not to
   validate the value as truth.

2. **Hub dependent-count definition: table-level distinct consumers vs raw query count.**
   The plan uses `count(DISTINCT q.target_table)` (distinct *consuming tables*), which matches
   the "57 downstream dependents" framing in the brief and the column-precise-rolled-to-table
   decision in sprint-10. An alternative is raw `count(q)` (number of consuming *queries*),
   which double-counts a table consumed by three queries. I chose distinct-tables as the
   honest centrality measure; confirm this is the intended semantics for the hub anchor before
   curating `expected_top_hub_rank`, because the two definitions can reorder the ranking.

---

## Plan Compliance — 2026-05-29

**Verdict: PASS (COMPLIANT-WITH-NOTES).** All implementation steps were addressed as
planned; the single deviation (D1) is sound and is recorded above. Reviewed against the
two trust-layer commits `943a1da` (feat) + `95670ab` (test). Trust-layer test suites:
35 passed, 1 skipped (golden-file-absent, expected).

Itemized:

- **Step 1.1 (Judgement)** — PASS. `model_validator(mode="after")` requires
  confidence+reason for `heuristic` and forbids both on `fact`; `Literal` and
  `model_validator` imported (`models.py` lines 3, 5, 38). Field descriptions mark
  `confidence` UNCALIBRATED and require `reason` to cite the grounding fact (honesty
  pass `e2245c6` folded in). Three validator scenarios pass in `test_judgement.py`.
- **Step 2.1–2.4 (risk retrofit)** — PASS. `risk_label`/`risk_weight` removed from
  both result models; `grep` for `.risk_label`/`.risk_weight` across `src/` + `tests/`
  returns zero. `risk: Judgement` + `downstream_count: int` added to both
  `ChangeScopeResult` and `ScopeChangeResult`. `get_change_scope` builds a heuristic
  Judgement whose `reason` embeds `str(n)` (`tools.py` lines 720–728);
  `downstream_count == len(affected_tables)`. `scope_change` threads `risk` through the
  delegate (`tools.py` line 937). The breaking tests (`test_risk_label_thresholds`,
  `test_change_scope_*`, `test_scope_change_*`) were updated in place, not deleted-around.
- **Step 3 (analyze_unused)** — PASS. `@mcp.tool()` + `@_timed_tool` (lines 1440–1442);
  `within_corpus_references` is a plain fact int (0); `dead_code` is a heuristic
  Judgement, confidence 0.5, reason cites zero within-corpus references; calls
  `NoiseFilter.from_config()` (line 1461). `total_tables_scanned` reuses
  `MATCH (t:SqlTable) RETURN count(t)`.
- **Step 4 (get_hub_ranking)** — PASS with deviation D1. `@mcp.tool()` + `@_timed_tool`
  (lines 1503–1505); all-fact `HubEntry` with NO Judgement attribute (`not hasattr`
  asserted in test); calls `NoiseFilter.from_config()`; single aggregation over existing
  `SELECTS_FROM` edges. The `WITH`-clause rewrite (KuzuDB scope constraint) is
  semantically identical — recorded in Deviations §D1.
- **Step 5 (answer-anchors)** — PASS. All three keys consumed in `evaluate()` (lines
  339–403), each gated on key presence + `target_table` (so
  `test_evaluate_handles_missing_downstream_key` still passes);
  `expected_downstream_count` uses DIRECT equality, not `_score()` (W2);
  `_hub_rank` defaults to `k=1000` (Note-3). No anchor is curated on a confidence value
  (all anchors freeze counts / booleans / ranks only).
- **Invariants** — PASS. The two trust-layer commits touch only the seven planned files
  (`queries.cypher`, `queries.py`, `models.py`, `tools.py`, `test_golden_lineage.py`,
  `test_anchor_tools.py`, `test_judgement.py`); `base.py`/`indexer.py` are NOT in the
  trust-layer diff (their master…HEAD changes belong to earlier sprint-10 commits). No
  schema DDL / re-index path added; no O(N²); no `# TODO` in any new tool/harness happy
  path; every new tool has a grep-confirmed `@mcp.tool()` registration and every harness
  helper is called from `evaluate()`.

Feature is ready to merge.
