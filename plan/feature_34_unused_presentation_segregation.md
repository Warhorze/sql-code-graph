# Feature Plan: #34 — `analyze_unused` Presentation/Terminal Segregation

**Plan date**: 2026-05-31
**Author**: architect-planner
**Issue**: [#34](https://github.com/Warhorze/sql-code-graph/issues/34) — segregate terminal/presentation
schemas from `dead_code` via the existing `[sqlcg.presentation] schema_prefixes` config.
**Branch**: `feat/cluster-b-provenance` (this branch carries the trust layer #31/#32/#33 and the
living-codebase F1 work via ancestry; #34 lands on top of it).
**Policy**: No TODO in any happy path. Every new method needs a grep-confirmed call site before the
PR opens. Tests assert observable output. **No `SCHEMA_VERSION` bump** — this is a read-side
refinement only (see "Schema Impact"). Small-repo experience must not regress: with no
`[sqlcg.presentation]` configured, behaviour is byte-identical to today.

---

## Summary

`analyze_unused` currently flags every table with zero in-corpus `SELECTS_FROM` consumers as a
`dead_code` candidate. Presentation/terminal tables (BI views, outbound feeds, data products) are
*by design* consumed outside the corpus, so they always land in this bucket and drown genuine
orphans. This feature reads the existing `[sqlcg.presentation] schema_prefixes` and **segregates**
presentation-prefixed tables into a separate `presentation_facing` bucket — visible, reasoned, but
no longer mislabelled as dead code. Genuinely-unreferenced non-terminal tables still surface as
`dead_code`.

---

## Re-Triage of #27 (delivered as part of this planning task)

**The "CTE-alias leak" sub-item (#27c) is confirmed FIXED by PR-3 (#33).** Evidence against the
current source tree on `feat/cluster-b-provenance`:

| #27c claim | Current-code evidence | Verdict |
|------------|----------------------|---------|
| CTE/derived nodes leak as first-class tables | [`base.py:963`](../src/sqlcg/parsers/base.py) constructs `TableRef(name=cte_alias, role="cte")`; `TableRef.role` field defaults to `"table"` ([`base.py:58`](../src/sqlcg/parsers/base.py)) | FIXED |
| CTE pseudo-tables persisted as `kind="TABLE"` | [`indexer.py:1003`](../src/sqlcg/indexer/indexer.py) writes `src_table.role` into the `kind` column; CTE emission site writes `"cte"` ([`indexer.py:1048`](../src/sqlcg/indexer/indexer.py)) | FIXED |
| `<output>` synthetic sink leaks | indexer excludes `<output>` before emit (`grep -n "<output>" indexer.py`) | FIXED |
| CTE pollutes `analyze upstream` | [`analyze.py:41,102`](../src/sqlcg/cli/commands/analyze.py) filter `WHERE t.kind IN ['table', 'external']` by default; `--include-intermediate` restores | FIXED |

**Strike #27c from the issue.**

**#27a (`*_bck` globs) is also FIXED**: [`config.py:147`](../src/sqlcg/core/config.py) added
`"*_bck_*"` to `default_patterns`. `fnmatch("foo_bck_us39553", "*_bck_*")` is `True` and
`fnmatch("bar_bck_archive", "*_bck_*")` is `True`, so both the `_bck_us<digits>` form and the
mid-name `_bck` form the issue called out are now matched by the shipped defaults (the dwh
`.sqlcg.toml` `ignore_table_regexes = ["_bck"]` workaround is no longer required). **Strike #27a.**

**#27e (duplicate rows in `analyze upstream`) is FIXED**: [`analyze.py:51,112`](../src/sqlcg/cli/commands/analyze.py)
both `RETURN ... LIMIT 100` with the kind-filter join and the queries return one row per `src.id`;
`impact`/`unused` use `RETURN DISTINCT`. **Strike #27e.**

### Surviving #27 sub-items

- **#27b (CLI NoiseFilter parity) — PARTIAL, one gap remains.** PR-3 brought `analyze upstream`,
  `downstream`, `impact`, and `unused` to `NoiseFilter.from_config()` parity
  ([`analyze.py:77,138,159,203`](../src/sqlcg/cli/commands/analyze.py)). **But the `find` command
  is still unfiltered** — `grep -rn "NoiseFilter" src/sqlcg/cli/commands/find.py` returns zero hits;
  `find.py:24,35` read and print `t.kind`/table names with no filter call. #27b's wording ("CLI
  `analyze`/`find` do not apply the NoiseFilter") is therefore only partly addressed. **Survives,
  scoped down to `find` only.**
- **#27d (timeout observability) — NOT done.** Timed-out files are logged at index time but not
  recorded as a queryable failure bucket; `analyze failures` ([`analyze.py:164`](../src/sqlcg/cli/commands/analyze.py))
  reports only parse E-codes via `File.parse_cause`, with no `timeout` bucket. **Survives.**

### Recommendation for the surviving #27 items

These are independent of #34/#35 and are LOW priority. They should be planned as a **separate small
hygiene PR after #34** (not folded into #34 — different files, different concern, and bundling would
violate one-feature-at-a-time). Noted here so they are not lost; out of scope for this plan.

---

## Decision: #34 and #35 are TWO features — plan #34 first

**Recommendation: TWO separate features, serialized on this branch. #34 first; #35 the explicit
follow-on.**

### Justification

| Dimension | #34 | #35 |
|-----------|-----|-----|
| Surface | One MCP tool (`analyze_unused`) + its result model + skill doc | New node table, new `CONSUMED_BY` rel table, manifest loader, `index`/CLI flag, schema migration |
| Schema | **No change** — reads `[sqlcg.presentation]` at query time, buckets in Python | **`SCHEMA_VERSION "5" → "6"`** — new `ExternalConsumer` node + `CONSUMED_BY` edge are persisted |
| Risk | LOW — pure read-side Python branching on a string-prefix test | MED-HIGH — ingestion path, new persisted graph entities, forced re-index, new edge traversal in `get_downstream_dependencies`/`diff_impact` |
| Independence | Ships and delivers value alone (segregation is useful immediately) | Depends conceptually on #34's bucket existing to report "true orphan vs expected presentation_facing" |
| Hot-path risk | None — `analyze_unused` is a single aggregation query + a Python loop already; no indexer change | Indexer gains a manifest-ingest pass; must be kept out of `_extract_column_lineage` and the `_upsert_parsed_file` edge-row loop |

They **share** the `[sqlcg.presentation]` config and the notion of a "terminal" table, which is why
they look like one cluster. But the sharing is a clean **dependency**, not a coupling: #34 defines and
populates the `presentation_facing` bucket purely from config prefixes; #35 later makes that bucket
*precise* by injecting external-consumer edges (a presentation table with zero injected consumers
becomes a true orphan). #34 is fully valuable without #35 — it immediately stops drowning real
orphans. Bundling them would (a) violate the strict one-feature-at-a-time rule, (b) force a
`SCHEMA_VERSION` bump and re-index for a change (#34) that needs neither, and (c) produce a large PR
mixing a trivial read-side branch with a new ingestion subsystem.

**#34 first** because it is the smaller, zero-schema, zero-risk refinement of `analyze_unused` — which
#33 (already on this branch) just touched — and it establishes the `presentation_facing` result shape
that #35 will populate with real edges. #35 is the explicit follow-on, owning the `SCHEMA_VERSION`
bump (`"5" → "6"`), the new node/edge tables, and the manifest loader. **#35 is NOT planned in this
file** — it gets its own plan after #34 merges, per the one-feature rule.

---

## Scope

### In Scope

- `analyze_unused` reads `get_presentation_prefixes(Path.cwd())` (already imported in `tools.py:10`).
- A table whose qualified name starts with any configured presentation prefix is routed to a new
  `presentation_facing: list[PresentationCandidate]` bucket on `UnusedTablesResult` **instead of**
  the `candidates` (`dead_code`) bucket.
- Each presentation-facing entry carries a `reason`: `"leaf in a declared egress layer; expected
  to have no in-corpus consumer"`.
- Non-presentation tables with zero `SELECTS_FROM` consumers continue to surface in `candidates`
  with the existing `dead_code` heuristic Judgement (`confidence=0.5`) — unchanged.
- NoiseFilter still runs first: a `_bck` table that also matches a presentation prefix is dropped as
  noise, not surfaced in either bucket (noise wins).
- Skill doc (`skill.py:57`) updated so the LLM knows `dead_code` excludes the declared egress layer.

### Non-Goals

- External-consumer edge injection / consumers manifest (that is **#35**, a separate plan).
- Any `SCHEMA_VERSION` bump or persisted graph change.
- Any change to the indexer, parser, or hot paths.
- Surfacing presentation segregation in the CLI `sqlcg analyze unused` command — the CLI `unused`
  command ([`analyze.py:188`](../src/sqlcg/cli/commands/analyze.py)) is a separate, simpler query
  path that does not return a typed result with buckets. (Optional stretch noted in "Open Question 1";
  default is MCP-tool-only for v1.1.0 to keep the diff small.)
- The surviving #27 items (`find` NoiseFilter, timeout observability) — separate hygiene PR.

---

## Schema Impact

**None.** `analyze_unused` reads `[sqlcg.presentation] schema_prefixes` at call time and buckets the
already-queried rows in Python. No node/edge/property is added or changed. `SCHEMA_VERSION` stays
`"5"`. This is the central reason #34 is separable from #35 and ships without a re-index.

---

## Code-vs-Plan Verification (against the real source)

| Finding | Verified state |
|---------|---------------|
| `analyze_unused` already runs `NoiseFilter.from_config()` and loops `unused_rows` building `UnusedCandidate`s | **Confirmed.** [`tools.py:1574-1602`](../src/sqlcg/server/tools.py): `noise_filter = NoiseFilter.from_config()` at 1576; the `for row in unused_rows` loop at 1584 skips `noise_filter.is_noise(tq)` then appends `UnusedCandidate(...)`. The new prefix branch slots into this exact loop. |
| `get_presentation_prefixes` is already imported in `tools.py` | **Confirmed.** [`tools.py:10`](../src/sqlcg/server/tools.py): `from sqlcg.core.config import get_db_path, get_presentation_prefixes`. No new import needed. |
| `diff_impact` is the only current consumer of `get_presentation_prefixes`, using `startswith` | **Confirmed.** [`tools.py:919`](../src/sqlcg/server/tools.py) `prefixes = get_presentation_prefixes(Path.cwd())`; [`tools.py:948`](../src/sqlcg/server/tools.py) `[t for t in affected_tables if any(t.startswith(p) for p in prefixes)]`. #34 reuses this exact predicate (prefixes are lowercased by the config getter; tables compared lowercased — see Open Question 2). |
| `get_presentation_prefixes` returns **lowercased** prefixes, empty list when unset | **Confirmed.** [`config.py:262-266`](../src/sqlcg/core/config.py): `return [p.lower() for p in raw ...]` and `return []` default. With no config, the prefix list is empty → no table matches → result is byte-identical to today (small-repo safety). |
| `UnusedTablesResult` has fields `candidates`, `total_tables_scanned`, `hint` only | **Confirmed.** [`models.py:397-411`](../src/sqlcg/server/models.py). Adding `presentation_facing: list[...] = Field(default_factory=list, ...)` is additive and back-compatible (default empty). |
| `ANALYZE_UNUSED_TABLES_QUERY` returns only `table_qualified` | **Confirmed.** [`queries.cypher:102-106`](../src/sqlcg/core/queries.cypher): `MATCH (t:SqlTable) WHERE NOT (t)<-[:SELECTS_FROM]-() RETURN t.qualified AS table_qualified`. **No query change needed** — the prefix test runs in Python on the returned `table_qualified`. |
| `analyze_unused` is a single aggregation + Python loop — no hot path | **Confirmed.** [`tools.py:1579`](../src/sqlcg/server/tools.py) is one `run_read`; the loop is over the unused-table result set (bounded, not per-column / per-edge). Adding one `startswith`-any test per row is O(rows × prefixes), with prefixes typically 1-3. Not a parser/indexer hot path. No performance invariant is touched. |
| `analyze_unused` has a `hint` set only when `not candidates` | **Confirmed.** [`tools.py:1604-1609`](../src/sqlcg/server/tools.py). The hint condition must be updated: "no dead-code candidates" is no longer the same as "nothing found" once presentation tables are bucketed separately (see Step 4). |
| Existing tests `test_analyze_unused_fact_heuristic_separation` / `_backup_excluded` assert on `result.candidates` | **Confirmed.** [`test_anchor_tools.py:405,437`](../tests/integration/test_anchor_tools.py). Neither configures `[sqlcg.presentation]`, so both must stay green unchanged — they assert `ba.orphan` (no presentation prefix) stays in `candidates`. This is the regression guard that the default-off behaviour is preserved. |

### `dead_code` happy-path TODO check

No `# TODO` exists in `analyze_unused` (`grep -n "TODO" src/sqlcg/server/tools.py` — none in the
1557-1615 range). The new branch is a complete `if/else` route, not a stub. The feature is not a
"one line at a TODO" reduction — it adds a real second bucket and a model field with a populated
call site.

---

## Design

### Data Models (`server/models.py`)

Add a `PresentationCandidate` model and a `presentation_facing` field on `UnusedTablesResult`:

```python
class PresentationCandidate(BaseModel):
    """A terminal/egress table that has no in-corpus consumer BY DESIGN.

    Surfaced separately from dead_code: its lack of within-corpus consumers is the
    defining property of a declared presentation/egress layer, not evidence it is dead.
    """

    table_qualified: str = Field(..., description="Qualified table name (schema.table).")
    within_corpus_references: int = Field(
        0,
        description="FACT: count of SELECTS_FROM consumers in the indexed corpus (always 0 here).",
    )
    matched_prefix: str = Field(
        ...,
        description="FACT: the configured [sqlcg.presentation] schema_prefix this table matched.",
    )
    reason: str = Field(
        "leaf in a declared egress layer; expected to have no in-corpus consumer",
        description="Why this table is segregated from dead_code rather than flagged.",
    )
```

Extend `UnusedTablesResult`:

```python
class UnusedTablesResult(BaseModel):
    candidates: list[UnusedCandidate] = Field(default_factory=list, ...)         # unchanged
    presentation_facing: list[PresentationCandidate] = Field(                    # NEW
        default_factory=list,
        description="Terminal/egress leaves matched by [sqlcg.presentation] prefixes; "
        "expected to have no in-corpus consumer (NOT dead code). Empty when no "
        "presentation prefix is configured.",
    )
    total_tables_scanned: int = Field(0, ...)                                    # unchanged
    hint: str | None = Field(None, ...)                                          # unchanged
```

`presentation_facing` defaults to an empty list, so callers that ignore it (and the with-no-config
path) are unaffected.

### API Shape (`analyze_unused` routing)

In the existing `for row in unused_rows` loop, after the noise check, branch on prefix match:

```python
prefixes = get_presentation_prefixes(Path.cwd())   # lowercased; [] when unset

candidates: list[UnusedCandidate] = []
presentation_facing: list[PresentationCandidate] = []
for row in unused_rows:
    tq = row["table_qualified"]
    if noise_filter.is_noise(tq):                  # noise wins — drop entirely (unchanged)
        continue
    matched = _matched_presentation_prefix(tq, prefixes)
    if matched is not None:
        presentation_facing.append(
            PresentationCandidate(table_qualified=tq, matched_prefix=matched)
        )
        continue
    candidates.append(UnusedCandidate(... existing dead_code Judgement ...))   # unchanged
```

Helper (module-level in `tools.py`, alongside the other private helpers):

```python
def _matched_presentation_prefix(table_qualified: str, prefixes: list[str]) -> str | None:
    """Return the first configured presentation prefix the table matches, else None.

    Prefixes are pre-lowercased by get_presentation_prefixes; compare lowercased so an
    `IA_ANALYTICS.foo` table matches an `ia_` prefix (Open Question 2 resolution).
    """
    tql = table_qualified.lower()
    for p in prefixes:
        if tql.startswith(p):
            return p
    return None
```

This mirrors `diff_impact`'s `any(t.startswith(p) for p in prefixes)` predicate but returns the
matched prefix so it can be surfaced in `PresentationCandidate.matched_prefix` (observable output for
tests).

---

## Implementation Steps

### Phase 1: Model

**Step 1.1** — Add `PresentationCandidate` model and `presentation_facing` field.
- Files affected: [`src/sqlcg/server/models.py`](../src/sqlcg/server/models.py) (after `UnusedCandidate`,
  before/within `UnusedTablesResult`).
- Acceptance: `grep -n "class PresentationCandidate\|presentation_facing" src/sqlcg/server/models.py`
  returns both the new class and the new field; `UnusedTablesResult.presentation_facing` defaults to
  an empty list.

### Phase 2: Tool routing

**Step 2.1** — Add the `_matched_presentation_prefix` helper to `tools.py`.
- Files affected: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py).
- Acceptance: helper defined AND called (grep both, see Wiring Checklist).

**Step 2.2** — Wire the prefix branch into the `analyze_unused` loop; import
`PresentationCandidate` from `models`.
- Files affected: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) (loop at ~1584; the
  `PresentationCandidate` import added to the existing `from sqlcg.server.models import ...` block).
- Acceptance: a presentation-prefixed orphan lands in `result.presentation_facing`, NOT in
  `result.candidates`; a non-prefixed orphan stays in `result.candidates`.

**Step 2.3** — Return `presentation_facing` in the `UnusedTablesResult(...)` constructor.
- Files affected: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) (~1611).
- Acceptance: `grep -n "presentation_facing=" src/sqlcg/server/tools.py` shows the constructor arg.

### Phase 3: Hint correctness

**Step 3.1** — Update the `hint` logic so it accounts for the new bucket. The old hint ("all tables
have a consumer, or graph is empty") fires when `not candidates`; that is now misleading if all
orphans were presentation-facing. New logic:

```python
hint = None
if not candidates and not presentation_facing:
    hint = ("No unused-table candidates found. All indexed tables have at least one "
            "within-corpus consumer, or the graph is empty.")
elif not candidates and presentation_facing:
    hint = (f"No dead-code candidates outside the declared egress layer. "
            f"{len(presentation_facing)} terminal/presentation leaf(s) are reported "
            f"separately (expected to have no in-corpus consumer).")
```

- Files affected: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) (~1604).
- Acceptance: with only presentation orphans present, `hint` mentions the egress layer and
  `candidates` is empty (Scenario C).

### Phase 4: Skill doc

**Step 4.1** — Update the `analyze_unused` line in `skill.py` so the LLM does not treat a
presentation-facing table as dead.
- Files affected: [`src/sqlcg/server/skill.py`](../src/sqlcg/server/skill.py) (line 57).
- Change:
  ```
  # Before
  - **Dead code**: `analyze_unused()` → each `dead_code` is heuristic; table may be consumed
    externally (BI/API/COPY INTO). Show `reason` before suggesting deletion.
  # After
  - **Dead code**: `analyze_unused()` → `candidates` (`dead_code`, heuristic, confidence 0.5) are
    orphans OUTSIDE the declared egress layer; `presentation_facing` are terminal/egress leaves
    (config `[sqlcg.presentation]`) expected to have no in-corpus consumer — do NOT suggest deleting
    those. Show `reason` before suggesting deletion of any candidate.
  ```
- Acceptance: The **`_WORKFLOWS` string** (the specific string that contains the
  `analyze_unused()` workflow bullet at line 57) must contain `presentation_facing` on the
  `analyze_unused` line after this change. The `_BOUNDARY` string at lines 39-50 describes
  the heuristic contract (`dead_code, confidence 0.5`) and does **not** need updating — it
  remains accurate (the `dead_code` field is still a heuristic; `presentation_facing` entries
  have no `Judgement` field and are facts, not heuristics). Acceptance is confirmed by:
  `grep -A1 analyze_unused src/sqlcg/server/skill.py | grep -c presentation_facing`
  returning ≥1 (the `_WORKFLOWS` line contains `presentation_facing`). The failing test
  `test_T34_skill_workflows_mentions_presentation_facing` encodes this check precisely.

---

## Wiring Checklist

| Question | Answer |
|----------|--------|
| What calls the new function? | `analyze_unused` (the MCP tool) calls `_matched_presentation_prefix(tq, prefixes)` inside its existing `for row in unused_rows` loop. Grep-confirm: `grep -n "_matched_presentation_prefix" src/sqlcg/server/tools.py` must return **≥2 hits** (definition + call). |
| What constructs `PresentationCandidate`? | `analyze_unused` constructs it in the prefix branch. Grep-confirm: `grep -n "PresentationCandidate(" src/sqlcg/server/tools.py` must return ≥1 hit, AND `grep -n "PresentationCandidate" src/sqlcg/server/models.py` shows the class. |
| Where does `presentation_facing` flow? | `analyze_unused` → `UnusedTablesResult(presentation_facing=presentation_facing, ...)`. Grep-confirm: `grep -n "presentation_facing=" src/sqlcg/server/tools.py`. |
| What config/constant does this align with? | `get_presentation_prefixes(Path.cwd())` — the SAME getter `diff_impact` uses; prefixes lowercased by the getter, `[]` default when `[sqlcg.presentation]` absent. No new config key, no `KuzuConfig` path involved. No hardcoded prefix (the no-config path produces zero matches). |
| Does any TODO remain in the happy path? | No. The branch is a complete `if matched ... else ...`; the helper is a complete loop; the `reason` is a real default string, not a placeholder. |
| Is any hot path or performance invariant touched? | No. No change to `base.py`, `indexer.py`, `_extract_column_lineage`, or `_upsert_parsed_file`. The only added op is one `startswith`-loop per unused-table row (bounded result set), outside any parser/indexer loop. |
| Is the small-repo path preserved? | Yes. With no `[sqlcg.presentation]` config (the small-repo default), `prefixes == []`, `_matched_presentation_prefix` always returns `None`, every orphan stays in `candidates`, `presentation_facing == []` — byte-identical to current behaviour. Guarded by the unchanged `test_analyze_unused_fact_heuristic_separation` and a new explicit Scenario D. |

---

## Test Strategy

All tests are integration tests in [`tests/integration/test_anchor_tools.py`](../tests/integration/test_anchor_tools.py),
following the existing `_index_fixture(tmp_path, {...}, monkeypatch)` + `tools.analyze_unused()`
pattern (the `.sqlcg.toml` is written into the fixture dir; `monkeypatch` sets cwd so
`Path.cwd()`-based config resolution picks it up — same mechanism as
`test_diff_impact_presentation_configured` at line 282).

- **Scenario A — presentation table segregated (configured):** fixture with
  `.sqlcg.toml = '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n'`, an orphan
  `CREATE TABLE ia_pres.dashboard_feed (id INT);` and an orphan
  `CREATE TABLE ba.real_orphan (id INT);`. Assert `ia_pres.dashboard_feed` is in
  `result.presentation_facing` (NOT in `candidates`); `ba.real_orphan` is in `candidates` (NOT in
  `presentation_facing`). Assert the presentation entry's `matched_prefix == "ia_"` and `reason`
  contains "egress layer".
- **Scenario B — genuine orphan still flagged:** same config; assert a non-prefixed orphan keeps
  its `dead_code` Judgement (`assertion_type == "heuristic"`, `confidence == 0.5`).
- **Scenario C — all-presentation graph sets the new hint:** config `["ia_"]`, only
  `ia_pres.*` orphans. Assert `result.candidates == []`, `result.presentation_facing` non-empty, and
  `result.hint` mentions the egress layer (not the "graph is empty" message).
- **Scenario D — off by default (small-repo regression guard):** NO `[sqlcg.presentation]` config;
  orphan `ia_pres.dashboard_feed`. Assert it appears in `candidates` (treated as dead_code) and
  `result.presentation_facing == []`. This proves no hardcoded prefix and that the small-repo
  experience is unchanged. (Mirrors `test_diff_impact_presentation_off_by_default` at line 301.)
- **Scenario E — noise wins over presentation:** config `["ia_"]`, fixture with an orphan
  `ia_pres.feed_bck` (matches BOTH a presentation prefix AND the `*_bck_*` noise glob). Assert it
  appears in NEITHER bucket (noise filter drops it first, before the prefix branch).
- **Existing tests `test_analyze_unused_fact_heuristic_separation` and
  `test_analyze_unused_backup_excluded` must pass UNCHANGED** — they configure no presentation
  prefix, so the default-off path keeps `ba.orphan` in `candidates`. Do NOT edit them.

---

## Acceptance Criteria

- `[ ]` With `[sqlcg.presentation] schema_prefixes = ["ia_"]` configured, an orphaned `ia_*` table
  appears in `analyze_unused().presentation_facing` and **not** in `.candidates` (Scenario A).
- `[ ]` A genuinely-unreferenced non-`ia_` table still appears in `.candidates` with the
  `dead_code` heuristic Judgement (`assertion_type == "heuristic"`, `confidence == 0.5`) (Scenario B).
- `[ ]` Each `presentation_facing` entry carries `matched_prefix` and a non-empty `reason` containing
  "egress layer" (Scenario A).
- `[ ]` When all orphans are presentation-facing, `.candidates == []` and `.hint` describes the
  egress segregation rather than "graph is empty" (Scenario C).
- `[ ]` With NO presentation config, behaviour is byte-identical to today: orphaned `ia_*` tables
  land in `.candidates`, `.presentation_facing == []` (Scenario D) — small-repo safety preserved.
- `[ ]` A table matching both a presentation prefix and a noise glob appears in neither bucket
  (Scenario E).
- `[ ]` `SCHEMA_VERSION` is unchanged at `"5"` (`grep -n 'SCHEMA_VERSION = ' src/sqlcg/core/schema.py`).
- `[ ]` `grep -n "_matched_presentation_prefix" src/sqlcg/server/tools.py` returns ≥2 hits
  (definition + call); `grep -n "PresentationCandidate(" src/sqlcg/server/tools.py` returns ≥1 hit.
- `[ ]` No `# TODO` in `analyze_unused`'s body (`grep -n "TODO" src/sqlcg/server/tools.py` shows none
  in 1557-1620).
- `[ ]` `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py`, `test_bulk_upsert_invariant.py`
  pass unchanged (no hot path touched).
- `[ ]` `test_analyze_unused_fact_heuristic_separation` and `test_analyze_unused_backup_excluded`
  pass unchanged (not edited).
- `[ ]` `skill.py` no longer implies every consumer-less table is `dead_code`; mentions
  `presentation_facing`.

---

## Open Questions (non-blocking; resolved with defaults)

1. **CLI `sqlcg analyze unused` parity** — the CLI `unused` command
   ([`analyze.py:188`](../src/sqlcg/cli/commands/analyze.py)) is a separate, simpler query path that
   prints a flat table and does not return a typed bucketed result. **Default decision:** leave the
   CLI command as-is for #34 (MCP-tool-only segregation), to keep the diff minimal and the concern
   single. If the user wants CLI parity, it is a 3-line follow-up (apply the same prefix branch and
   print a second table) — call it out but do not build it speculatively.
2. **Prefix case-sensitivity** — `get_presentation_prefixes` lowercases prefixes; qualified table
   names in the graph may be mixed-case (Snowflake). **Resolved:** `_matched_presentation_prefix`
   lowercases the table name before `startswith`, matching the spirit of `diff_impact`. (Note:
   `diff_impact` at `tools.py:948` compares against un-lowercased `affected_tables` — a latent
   case-sensitivity inconsistency there. #34 does the correct lowercased compare; flag the
   `diff_impact` discrepancy to the architect-reviewer as a separate minor finding, do not fix it
   here.)

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Adding `presentation_facing` breaks existing `UnusedTablesResult` callers | LOW | Field is additive with `default_factory=list`; existing tests assert only `candidates`/`total_tables_scanned`/`hint` and stay green. |
| Presentation prefix accidentally swallows a real orphan that happens to share the prefix | LOW (by design) | #34 deliberately *segregates, not hides*: presentation-facing tables remain visible in their own bucket and are individually reviewable. #35 later makes the bucket precise via injected consumer edges. |
| Case mismatch between lowercased prefixes and mixed-case Snowflake table names | MED | `_matched_presentation_prefix` lowercases the table name (Open Question 2). Scenario A uses a Snowflake-style `ia_pres.*` fixture to exercise it. |
| `hint` regression (old message fires when only presentation orphans exist) | LOW | Step 3.1 rewrites the hint branch; Scenario C asserts the new message. |
