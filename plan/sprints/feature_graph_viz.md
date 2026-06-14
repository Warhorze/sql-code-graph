# Feature Plan: `sqlcg viz` — committed graph-visualization generator

**Status:** DRAFT (plan-reviewer gates before any implementation)
**Branch:** `plan/graph-viz` (off `master` @ v1.34.3)
**Version target:** MINOR bump (new CLI surface, nothing breaks) — `1.34.3 → 1.35.0`
**Owner (compliance):** architect-planner owns this file.

## Summary

Add a committed `sqlcg viz` CLI command that queries the live DuckDB graph, bakes the
full node/edge dataset (plus BYO tags and a precomputed adjacency map) into ONE
self-contained `.html` file with the `force-graph` library inlined, openable on Windows
via `file://`. The HTML extends the existing single-schema filter in
[`table_graph.html`](../../table_graph.html) into a multi-facet **schema ∩ kind ∩ tag**
client-side filter with multi-select toggles plus a solo/focus mode.

## Why this reverses the "no committed generator" rule

The historical rule was: keep [`table_graph.html`](../../table_graph.html) hand-edited,
regenerate the `DATA` block by hand, no committed generator (see
[`project_table_graph_html` memory] and [`viz_color_by_schema.md`](viz_color_by_schema.md)).
That rule held while the only variability was the `DATA` block and a curated schema list a
human could maintain. It no longer holds: a **tag CSV (many-to-many + glob)**, **node-kind
toggles tied to the graph `kind` field**, a **config-driven multi-schema multi-select**, and
a **precomputed adjacency bake** cannot be hand-maintained without drift between the graph
and the artifact. The generator becomes the single source of truth; the committed
`table_graph.html` becomes a generator *output*, not a hand-edited file.

**Non-negotiable preserved:** the emitted file stays **self-contained** — force-graph lib
inlined, all DATA/tags/config baked, zero external `src=`/`href=`/`fetch`/CDN. The user
opens it on Windows by double-click (`file://`). This is an acceptance test, not a goal.

---

## Scope

### In Scope

1. New CLI command `sqlcg viz` registered in [`main.py`](../../src/sqlcg/cli/main.py) as a
   single command (like `gain`/`report`), backed by
   `src/sqlcg/cli/commands/viz.py`.
2. A graph-read layer (`src/sqlcg/viz/data.py`) that builds the baked dataset
   (`nodes`, `links`, `adj`) from the live graph via the **server-aware**
   `run_read_routed` path (never a direct `get_backend` open — matches `gain`/`coverage`).
3. A tags layer (`src/sqlcg/viz/tags.py`) that parses a BYO tags CSV (many-to-many + glob)
   and resolves each node's `tags[]` plus an optional per-tag color map.
4. A config key for the **real, declared schema list** read from `.sqlcg.toml`
   (`get_viz_schemas(path)` in [`config.py`](../../src/sqlcg/core/config.py)), following the
   existing `get_*` / fallback pattern.
5. An HTML renderer (`src/sqlcg/viz/render.py`) that bakes `DATA`, `TAGS`, `SCHEMA_CONFIG`
   into a self-contained template with the force-graph lib inlined from a vendored asset.
6. The client-side multi-facet filter (`schema ∩ kind ∩ tag`, edges shown only when both
   endpoints visible), multi-select toggles, tag color + filter legend, solo/focus mode.
7. Baked adjacency in `DATA` for the future neighborhood feature (design-for, do not build
   the neighborhood UI now).
8. Tests: e2e (self-contained assertion, baked DATA/tags/config), unit (tags glob/m2m,
   config reader, data-query shaping), and a JS `node --check` gate on the emitted file.

### Non-Goals

- **No neighborhood-expansion UI.** We bake adjacency so it is cheap later, but ship only
  the tag overlay + facet filter now. Neighborhood UI is an explicit follow-up (§Follow-ups).
- **No parser/indexer/lineage changes.** This is a pure read + render feature. The four
  frozen perf suites ([`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py)) are untouched.
- **No new graph schema / no re-index.** Reads only existing `SqlTable` + edge tables.
- **No job-mode redesign.** The existing job dropdown / `computeJobClosure` behavior is
  preserved as-is; it composes with the new facets but is not re-specified here.
- **No tag editing UI in the browser.** Tags come from the CSV at generate time only.
- **No server / no live reactivity.** Static one-shot bake. Re-run `sqlcg viz` to refresh.

---

## Design

### CLI signature

```
sqlcg viz [--tags PATH] [--out PATH] [--config-dir PATH]
```

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `--tags` | `Path \| None` | `None` | BYO tags CSV. When omitted, every node gets `tags: []`; the tag legend/facet is hidden. |
| `--out` | `Path` | `Path("table_graph.html")` (cwd) | Output HTML path. Default name preserves the existing artifact name so the user's workflow is unchanged. **Constant lives in `viz/render.py` as `DEFAULT_VIZ_OUT`; the CLI default references it — never hardcode the string twice.** |
| `--config-dir` | `Path` | `Path(".")` | Directory searched for `.sqlcg.toml` (for the schema list + aliases). Mirrors how other commands resolve config root. |

The command:
1. Resolves the schema list via `get_viz_schemas(config_dir)`.
2. Builds `DATA` (`nodes`/`links`/`adj`) via `build_viz_data()` over `run_read_routed`.
3. Parses tags via `load_tag_map(tags_path)` (if `--tags` given) and attaches `tags[]`.
4. Renders the self-contained HTML via `render_html(data, tags_meta, schema_config)`.
5. Writes to `--out`, prints a summary line (node count, edge count, tag count, schema count,
   out path) — observable output, not just "wrote file".

Registration in [`main.py`](../../src/sqlcg/cli/main.py):
```python
app.command("viz")(viz.viz_cmd)
```
(grep-confirmed call site: this line is the only caller of `viz_cmd`.)

### Config key — real schema list

The maintainer decision: schemas are **individual, real, flat, configurable** — no tiers,
no hardcoded names. Proposed shape in `.sqlcg.toml`:

```toml
[sqlcg.viz]
schemas = ["ba", "da", "ia_analytics", "ia_tableau", "ia_businessobjects", "ia_semantic", "ia_dataproducts"]
```

Chosen over a top-level `[sqlcg] schemas` because the list is **viz-presentation** config
(which schemas get a distinct hue / appear as multi-select facets), not a global indexing
concept. Keeping it under `[sqlcg.viz]` avoids implying it filters indexing or lineage.

New reader in [`config.py`](../../src/sqlcg/core/config.py), mirroring `get_schema_aliases`
/ `get_noise_filter_patterns` exactly (same `.sqlcg.toml` open, same `except Exception: pass`,
same fallback shape):

```python
def get_viz_schemas(path: Path) -> list[str]:
    """Read [sqlcg.viz] schemas from .sqlcg.toml; [] when absent."""
    # reads config["sqlcg"]["viz"]["schemas"]; returns [] if missing/not a list.
```

**Fallback behavior (DECISION, documented so the dev cannot guess):** when the key is
absent or `[]`, the generator falls back to **data-derived schemas** — every distinct
`schema` present in `DATA.nodes` gets a facet, and coloring cycles the palette (the
*current* `table_graph.html` pre-`viz_color_by_schema` behavior). This preserves the
zero-config small-repo invariant: a 20-ETL user runs `sqlcg viz` with no `[sqlcg.viz]`
block and still gets a usable multi-schema view. The config key is an *override that
curates*, never a *requirement*. The fallback default `[]` matches the `get_*` convention
(no path/constant hardcoded outside the reader).

> **Note for plan-reviewer:** the `.sqlcg.toml` filename (not `sqlcg.toml`) is the
> established constant via `config_file_present`. The user's prompt said "`sqlcg.toml`";
> the codebase uses `.sqlcg.toml`. We use `.sqlcg.toml` to match `config_file_present`
> and all existing `get_*` readers. Flagged so it is a conscious choice, not a typo.

### DATA query + node/edge/adjacency schema

**Source tables (existing graph, DuckDB SQL via `run_read_routed`):**
- Nodes: `"SqlTable"` (cols `qualified, catalog, db, name, kind, defined_in_file` per
  [`schema.cypher`](../../src/sqlcg/core/schema.cypher)).
- "Catalogued" flag: `EXISTS` in `"HAS_COLUMN"` keyed on `src_key = qualified`
  (same join `coverage.py` uses).
- Edges: union of `"SELECTS_FROM"`, `"COLUMN_LINEAGE"` (collapsed to table level), and
  `"STAR_SOURCE"` — the three table-level lineage relations the prompt named.
  - `COLUMN_LINEAGE` keys are `<table>.<col>`; collapse to table by stripping the trailing
    `.<col>` using the established `_DST_TABLE` expression pattern in
    [`coverage.py`](../../src/sqlcg/cli/coverage.py)
    (`left(k, len(k) - instr(reverse(k), '.'))`). Verify against `base.py` `full_id` join,
    as coverage.py already documents.
  - Self-loops dropped; duplicate (src,dst) collapsed with a count `n` (matches existing
    `link.n` seen in the current artifact's link objects).
- Degree `deg`: count of distinct neighbors per node over the collapsed edge set
  (computed in Python from the edge rows; do not add a graph query for it).
- `jobs[]`: preserve the existing field. **DECISION/fork for reviewer:** the current
  `table_graph.html` bakes a `jobs` array per node, but it is unclear which graph relation
  produces it (no obvious `jobs` column on `SqlTable`). See §Blocking/Decision A.

**Baked `DATA` shape (extends the current artifact, additive — never drops a field):**

```jsonc
{
  "nodes": [
    {
      "id": "ba.wtda_webshop_order",   // = SqlTable.qualified (the graph key)
      "schema": "ba",                   // SqlTable.db (the schema part) — same as today
      "kind": "table",                  // SqlTable.kind: table|view|temp|external|cte|derived
      "deg": 12,                        // neighbor count in collapsed edge set
      "cat": true,                      // catalogued (HAS_COLUMN exists)
      "jobs": [],                       // preserved as-is (see Decision A)
      "tags": ["backup"]                // NEW — resolved from tags CSV (m2m); [] when none
    }
  ],
  "links": [
    {"source": "ba.x", "target": "ia_tableau.y", "n": 3}  // n = collapsed edge count
  ],
  "adj": {                              // NEW — precomputed undirected adjacency for the
    "ba.x": ["ia_tableau.y", "da.z"],   //        future neighborhood feature. Baked so the
    "ia_tableau.y": ["ba.x"]            //        client does not rebuild it and a later
  }                                     //        "expand N hops around tagged nodes" is O(lookup).
}
```

`adj` is the **undirected** neighbor map (mirrors the JS `adj` built today at runtime).
Baking it server-side means the future neighborhood UI reads it directly. The existing JS
`for (const l of DATA.links)` adjacency build can either consume the baked `adj` or keep
rebuilding — the plan keeps the runtime rebuild for ego-mode (no behavior change) AND bakes
`adj` for the future feature; the two are derived from the same edge set so they agree.

**`kind` values:** tie node-kind toggles to the existing `SqlTable.kind`. The toggle set is
`table / view / temp / external` (ON by default) and `cte / derived` (OFF by default, but
present in DATA and switchable). The renderer must not invent kinds — it reads the distinct
`kind` values present and maps the known six to toggles; any unexpected `kind` value falls
into a catch-all "table" toggle bucket (documented inline) so the view never silently drops
nodes. **Decision/fork B:** confirm the exact `kind` string vocabulary the indexer emits
(`temp` vs `temporary`, `external` presence) — see §Blocking/Decision B.

### Tags CSV format (many-to-many + glob)

**File:** a 2- or 3-column CSV. Header required.

```csv
pattern,tag,color
ba.*_bck,backup,#6e7681
ba.*_bck,deprecated,#d29922
da.fact_*,facts,
ia_tableau.ba_wtda_webshop_order,reporting,#58a6ff
```

Rules:
- **Column 1 `pattern`** — matched against `table_qualified` (the node `id`, the graph key).
  Supports glob via `fnmatch` (the established matcher — same lib `noise_match.py` uses for
  `ignore_table_patterns`). A literal qualified name (no glob metachars) is an exact match.
  Matching is case-insensitive (lowercase both sides), matching `noise_match` convention.
- **Column 2 `tag`** — the tag label. **Many-to-many both directions:** one pattern may
  appear on multiple rows with different tags (a table gets several tags); one tag may appear
  on many rows/patterns (a tag covers many tables). Resolution: for each node, collect the
  set of tags whose pattern matches → `tags[]` (sorted, deduped).
- **Column 3 `color`** (optional) — a hex color for the tag. If multiple rows give the same
  tag different colors, **first-wins** and the generator prints a warning (deterministic).
  Tags with no color get an auto-assigned palette color (stable by tag name sort order).

**Parser:** `load_tag_map(path) -> TagMap` in `src/sqlcg/viz/tags.py`. `TagMap` holds the
ordered `(pattern, tag)` rules + the `tag -> color` map. `resolve_tags(node_id, tag_map) ->
list[str]` applies `fnmatch` per rule. Performance: ~6k nodes × ~tens of rules = trivial; no
optimization needed, but compile the pattern list once (not per node).

**Glob syntax DECISION:** `fnmatch` semantics (`*`, `?`, `[seq]`) — NOT regex, NOT SQL
`LIKE`. Chosen for exact parity with the existing `ignore_table_patterns` so users learn one
glob dialect. The example `ba.*_bck → backup` works under `fnmatch` (the `.` is literal, `*`
matches the middle). Documented in the CLI `--help` and in a header comment of the emitted
HTML so the user can read it offline.

### Filter composition + solo/focus UI

All filtering is **client-side on the full baked graph** (force-graph handles ~6k nodes).
Extend the existing `currentData()` in [`table_graph.html`](../../table_graph.html) (the
function that today drops nodes via `sc && n.schema !== sc` at ~lines 211/217).

**Facets (three independent multi-selects, union within a facet, intersection across):**
- **Schema** — checkboxes for each configured/derived schema. A node passes iff its
  `schema` is in the checked set (or the set is empty = "all").
- **Kind** — checkboxes `table/view/temp/external/cte/derived`; `cte`+`derived` start
  unchecked. A node passes iff its `kind` is in the checked set.
- **Tag** — a swatch per tag in the legend; checking tags isolates nodes carrying any
  checked tag. Empty tag selection = "do not filter by tag" (all pass).

**Composition:** `visible(n) = schemaPass(n) AND kindPass(n) AND tagPass(n)`.
**Edges:** a link is shown iff **both** endpoints are visible (keep the existing
`ids.has(source) && ids.has(target)` rule — it already implements "both endpoints visible";
the facet just changes which ids are in the set).

**Coloring:** primary fill = schema color (per `viz_color_by_schema` curated approach) OR
tag color when a "color by tag" mode is active. **Decision/fork C (multi-tag coloring):**
when a node carries 2+ tags and tag-coloring is active, which color wins? See §Decision C.
Default in this plan: **color-by-schema is the default fill**; tag colors render as a small
**second ring / swatch indicator**, and "color by tag" is an explicit toggle. With tag-color
mode on and a multi-tag node, use the first checked tag's color (deterministic by legend
order) and show the node has multiple via a thin secondary ring. Reviewer to confirm.

**Solo / focus mode:** alongside the multi-select union, each facet swatch/checkbox supports
a **solo** action (e.g. click the swatch label, or an alt/right-click) that isolates exactly
that one schema/kind/tag (deselects the rest of that facet) — a fast "show only this layer"
without un-checking everything by hand. Re-clicking solo restores the previous multi-select
state. This is the "click to focus one layer/tag" interaction. Node-level focus (the existing
`focusNode` + ego mode) is unchanged.

**Legend:** one row of schema swatches (configured schemas + an "other" swatch, per
`viz_color_by_schema`) and one row of tag swatches (each clickable to toggle/solo that tag,
with its color). Swatches double as the filter control and the color key — no separate
control panel needed.

### Self-contained bake mechanics

- The force-graph lib is **vendored** as a committed asset
  (`src/sqlcg/viz/assets/force-graph.min.js`, the same v1.49.5 UMD bundle already inlined in
  the current artifact). The renderer reads it via `importlib.resources` (same pattern as
  `SCHEMA_DDL` reading `schema.cypher` in [`schema.py`](../../src/sqlcg/core/schema.py)) and
  inlines it into a `<script>` block. No CDN, no network.
- `DATA`, `TAGS` (tag→color map + tag list), and `SCHEMA_CONFIG` (the schema list + palette)
  are JSON-serialized and inlined as `const DATA = {...}` etc.
- The HTML/CSS/UI JS lives in a template string in `render.py` (or a vendored
  `template.html` asset with `{{DATA}}`/`{{LIB}}` placeholders — implementer's choice, but
  the JS must pass `node --check`).

---

## Implementation Steps

### Phase 1: Config + data layer (no HTML yet)

**Step 1.1** — Add `get_viz_schemas(path)` to [`config.py`](../../src/sqlcg/core/config.py).
- Files: `src/sqlcg/core/config.py`.
- Mirror `get_schema_aliases` structure exactly (open `.sqlcg.toml`, `except Exception: pass`,
  fallback `[]`).
- Acceptance: reads `[sqlcg.viz] schemas`; returns `[]` when block/key absent or wrong type.

**Step 1.2** — Add `build_viz_data(schemas, config_dir) -> dict` in `src/sqlcg/viz/data.py`.
- Files: `src/sqlcg/viz/__init__.py` (new pkg), `src/sqlcg/viz/data.py`.
- Queries `SqlTable`, the catalogued `EXISTS HAS_COLUMN` flag, and the three edge relations
  via `run_read_routed`. Collapses `COLUMN_LINEAGE` to table level with the `_DST_TABLE`
  pattern. Builds `nodes` (with `deg`, `cat`, `jobs`, `tags:[]` placeholder), `links`
  (with `n`), and the undirected `adj` map.
- Acceptance: against a small in-memory DuckDB graph fixture, returns the exact node/link/adj
  shape in §DATA; self-loops dropped; duplicate edges collapsed with correct `n`; `deg`
  equals distinct-neighbor count.

### Phase 2: Tags layer

**Step 2.1** — Add `load_tag_map(path)` + `resolve_tags(node_id, tag_map)` in
`src/sqlcg/viz/tags.py`.
- Files: `src/sqlcg/viz/tags.py`.
- CSV parse (header required), `fnmatch` case-insensitive matching, m2m both directions,
  optional `color` column with first-wins + warning on conflict, auto-palette for uncolored
  tags.
- Acceptance: a CSV with a glob row maps the right set of tables; a table matched by two
  patterns gets both tags (sorted/deduped); a tag on many patterns covers all matches;
  color conflict warns and first-wins.

**Step 2.2** — Wire `resolve_tags` into `build_viz_data` (or in `viz_cmd` after data build).
- Acceptance: with `--tags`, nodes carry resolved `tags[]`; without, all `tags == []`.

### Phase 3: Renderer + self-contained HTML

**Step 3.1** — Vendor the force-graph bundle as a committed asset + add `render_html()` in
`src/sqlcg/viz/render.py`.
- Files: `src/sqlcg/viz/assets/force-graph.min.js` (extracted verbatim from the current
  [`table_graph.html`](../../table_graph.html) lib block), `src/sqlcg/viz/render.py`,
  `src/sqlcg/viz/assets/template.html` (or inline template).
- `DEFAULT_VIZ_OUT = "table_graph.html"` constant lives here.
- Reads the lib via `importlib.resources`; inlines lib + `DATA`/`TAGS`/`SCHEMA_CONFIG`.
- Acceptance: emitted HTML has no external `src=`/`href=`/`fetch(`/`http`-scheme reference;
  lib + DATA inlined; `node --check` passes on each `<script>` block.

**Step 3.2** — Extend `currentData()` into the multi-facet filter + add the tag/kind/solo UI.
- Files: the template (HTML/JS).
- Multi-select schema/kind/tag, union within facet, intersection across; edges shown only
  when both endpoints visible; solo/focus per facet; tag swatches in legend.
- Acceptance (browser-level, scripted via the JS unit harness / DOM assertions where
  feasible, otherwise manual + `node --check`): facet intersection produces the right node
  set on a baked fixture; toggling `cte/derived` off by default; solo isolates one layer.

### Phase 4: CLI command + registration

**Step 4.1** — Add `viz_cmd` in `src/sqlcg/cli/commands/viz.py`; register in
[`main.py`](../../src/sqlcg/cli/main.py).
- Files: `src/sqlcg/cli/commands/viz.py`, `src/sqlcg/cli/main.py` (import + `app.command("viz")`).
- Wires config → data → tags → render → write; prints summary line.
- Acceptance: `sqlcg viz --tags t.csv --out /tmp/g.html` exits 0, writes the file, prints
  node/edge/tag/schema counts + path.

### Phase 5: Version + docs

**Step 5.1** — Bump version to `1.35.0` in [`pyproject.toml`](../../pyproject.toml),
[`__init__.py`](../../src/sqlcg/__init__.py), `uv lock`. Add `viz` to the `main.py` help text.
- Acceptance: `sqlcg --version` shows `1.35.0`; `sqlcg viz --help` documents options + glob.

---

## Test Strategy

> Tests named by behavior (`test_<unit>_<scenario>_<expected>`), linking this plan in the
> docstring. No opaque case codes.

**Unit (`tests/unit/`, no graph backend):**
- `get_viz_schemas` returns the configured list; returns `[]` when block absent; ignores a
  non-list value without raising.
- Tags CSV: a glob pattern maps exactly the matching qualified names; a table matched by two
  patterns receives both tags deduped+sorted; a tag spanning many patterns covers all; a
  color conflict on one tag warns and keeps the first color; uncolored tags get a stable
  auto color.
- Renderer self-containment: `render_html(...)` output contains no `http://`/`https://`/
  `src="//`/`fetch(` token and contains the inlined lib sentinel + `const DATA`.

**Integration (`tests/integration/`, real in-memory DuckDB):**
- `build_viz_data` on a seeded graph (a handful of `SqlTable` rows across ≥2 schemas, mixed
  `kind`, some catalogued, edges across `SELECTS_FROM`/`COLUMN_LINEAGE`/`STAR_SOURCE`)
  returns nodes with correct `schema`/`kind`/`cat`/`deg`, links with collapsed `n`, and an
  `adj` map that equals the undirected neighbor sets. COLUMN_LINEAGE collapse strips the
  column suffix to the table level.

**E2E (`tests/e2e/`, full CLI):**
- `sqlcg viz --tags <fixture.csv> --out <tmp>` on a seeded DB:
  - exits 0; file exists; **self-contained** — assert no external resource reference and that
    the force-graph lib + `DATA`/`TAGS`/`SCHEMA_CONFIG` are baked in.
  - `node --check` passes on the emitted script blocks (the user's stated gate).
  - DATA node count == seeded table count; a glob tag row tags the expected nodes (parse the
    baked `DATA` JSON back out and assert `tags`).
  - `cte`/`derived` toggles are emitted as **unchecked** defaults in the HTML; `table/view/
    temp/external` checked.
  - schema config baked matches `[sqlcg.viz] schemas` from a fixture `.sqlcg.toml`; with no
    config, schemas are data-derived.

**Frozen perf suites:** untouched; run them to prove the feature did not perturb the hot path
(they should pass unchanged because no parser/indexer file is modified).

---

## Acceptance Criteria

- [ ] `sqlcg viz` is a registered command; `app.command("viz")` is its only call site.
- [ ] Emitted HTML is **self-contained**: no external `src`/`href`/`fetch`/CDN; force-graph
      lib + DATA + TAGS + SCHEMA_CONFIG all inlined; opens via `file://`.
- [ ] `node --check` passes on every `<script>` block of the emitted file.
- [ ] Baked `DATA.nodes` carry `id, schema, kind, deg, cat, jobs, tags` and `DATA.adj` is a
      correct undirected adjacency map; `DATA.links` carry `source,target,n`.
- [ ] A tags CSV with a glob row (`ba.*_bck`) tags exactly the matching nodes; a table can
      carry multiple tags; a tag can cover many tables (m2m verified both directions).
- [ ] Node-kind toggles: `table/view/temp/external` default ON; `cte/derived` default OFF
      but present and switchable.
- [ ] Client-side filter composes `schema ∩ kind ∩ tag`; an edge renders only when both
      endpoints are visible.
- [ ] Schema multi-select is driven by `[sqlcg.viz] schemas`; with the key absent the view
      falls back to data-derived schemas (zero-config small-repo path works).
- [ ] Solo/focus isolates one schema/kind/tag layer and restores prior multi-select on undo.
- [ ] Tag swatches appear in the legend as both color key and filter control.
- [ ] Version bumped to `1.35.0` (minor); the four frozen perf suites pass unchanged.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Vendored lib drifts from the artifact / hard to re-extract | Extract verbatim from the current `table_graph.html` lib block into a committed asset with the version comment (`1.49.5`) preserved; an e2e sentinel asserts it inlined. |
| Self-containment regresses silently (someone adds a CDN tag) | E2E asserts zero external-resource tokens — a hard gate, not a manual check. |
| `jobs[]` provenance unknown (Decision A) | Block on reviewer; default to baking `jobs: []` if the relation cannot be identified, so the field stays present without fabricating data. |
| `kind` vocabulary mismatch (Decision B) | Block on reviewer; renderer routes unknown kinds to a "table" catch-all so no node is silently dropped. |
| Multi-tag coloring ambiguity (Decision C) | Default color-by-schema; tag color as secondary ring; tag-color mode uses first-checked-tag — reviewer confirms. |
| 6k nodes perf in browser | Force-graph handles it (the current artifact already renders 6,020 nodes); no change. |
| Reversing the "no committed generator" rule conflicts with project memory | Documented rationale above; this plan IS the decision record. Plan-reviewer + maintainer gate it. |

---

## Decisions needed from the maintainer (forks the reviewer must resolve)

These do not block writing the plan but should be resolved before/at plan review:

**Decision A — `jobs[]` provenance.** The current `table_graph.html` bakes a per-node
`jobs` array (used by the job dropdown), but `SqlTable` has no obvious `jobs` column and no
generator is committed. *Where did `jobs` come from?* Options: (a) it was hand-injected /
external (the `viz_color_by_schema` note says `ej_*` job names come from "an external job
source outside this repo"), so the generator bakes `jobs: []` and the job dropdown becomes
inert unless a future source is wired; (b) there is a graph relation we should query.
**Recommended:** (a) — bake `jobs: []`, keep the dropdown code dormant, file a follow-up to
wire a real job source. Confirm.

**Decision B — `kind` string vocabulary.** Confirm the exact strings the indexer writes to
`SqlTable.kind` (`table`, `view`, and whether `temp`/`temporary` and `external` are emitted,
plus `cte`/`derived`). The toggle labels and defaults depend on the exact values.

**Decision C — multi-tag node coloring.** When a node has 2+ tags and "color by tag" is
active, which tag's color fills the node? Recommended: color-by-schema is the default fill;
tag colors show as a secondary ring; in tag-color mode the **first checked tag (legend
order)** wins and a thin extra ring signals "has more tags". Confirm or pick another rule
(e.g. blended color, striped, or "untagged-if-ambiguous").

**Decision D — config key location.** `[sqlcg.viz] schemas` (this plan's choice) vs a
top-level `[sqlcg] schemas`. Recommended `[sqlcg.viz]` (presentation-scoped). Confirm.

---

## Follow-ups (explicitly out of this sprint)

- **Neighborhood expansion UI** — "expand N hops around my tagged/selected nodes." The baked
  `DATA.adj` makes this an O(lookup) client feature; ship as a separate sprint.
- **Real job source** — if Decision A is (a), wire a committed job→table relation so the job
  dropdown is data-backed rather than dormant.
- **`docs/`** — a short "visualizing your graph" doc once the command ships.
