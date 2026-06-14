# Observed-Usage Overlay — Snowflake Query History & Tableau

**Date:** 2026-06-13
**Status:** Research / design exploration. Nothing implemented. Supersedes the framing in
the [[project-v2-snowflake-access-history]] memory note by splitting it into shippable tiers.
**Author context:** user proposal — use Snowflake query history as a runtime signal for
table liveness (alive vs dead), broaden beyond the one DWH to all Snowflake users, and map
the flow from Tableau into the warehouse.

## TL;DR

There are **two distinct goals** wearing one idea, and they want opposite data sources:

| Goal | Best source | Why |
|------|-------------|-----|
| **Liveness** — which tables actually run; dead-code signal | Parse `QUERY_HISTORY` SQL text with our own parser | Table-level refs survive ugly SQL; reuses the engine; no Enterprise gate → bigger user base |
| **Parser-recall oracle** — where is our lineage graph *wrong* | `ACCESS_HISTORY` (Snowflake-resolved) | Self-parsing query text to grade our own parser is **circular**; needs an independent oracle |
| **Tableau / BI impact** — what is *user-facing* alive | Parse Tableau's SQL out of `QUERY_HISTORY` (+ `QUERY_TAG`) | Tableau's custom SQL is invisible in the repo but visible in history; extends lineage past the warehouse |

All three are the **same architectural object**: an *observed-usage overlay* loaded **after**
indexing, exactly like the existing [`catalog load`](../../src/sqlcg/cli/commands) precedent —
source-tagged, never fed into the parser hot loop. Ship in reach order; each tier is independently useful.

## Core principle (inherited, do not violate)

Per [`CLAUDE.md`](../../CLAUDE.md) and [[project-v2-snowflake-access-history]]: **enrich, do not
replace.** The committed-code graph is the unique asset — pre-merge, offline, free, carries
`file:line` provenance, covers code that *hasn't run yet*. Query history is post-hoc,
observational, windowed, Snowflake-only, and needs warehouse access. It overlays the code graph;
it must never become the primary source, and it must never touch `qualify()`/`exp.expand()`
(same prohibition class as parse-time schema feeding — see the perf invariants in `CLAUDE.md`).

## The insight that makes liveness worth it: the 2×2

Static reachability and runtime observation are orthogonal. Crossing them is the product:

|  | runtime-hot | runtime-cold |
|---|---|---|
| **static-reachable** | normal — alive & wired | **deprecation candidate** (cold ≠ dead — see asymmetry below) |
| **static-island** | **parser gap** — table IS used, we failed to capture how → feeds recall/island work | likely genuinely dead |

The "find dead tables" story is the *weaker* half. The strong half is **static-island + runtime-hot =
a free oracle pointing at our parser's blind spots**, which aligns directly with the in-flight
column-lineage recall metric.

### Liveness inference is asymmetric — model it honestly

- **Positive is strong:** observed in query history → table is *definitely* alive.
- **Negative is weak:** absence in window N ≠ dead. Could be quarterly/annual reporting, DR/archive,
  regulatory retention, manually triggered, or accessed via a path we didn't capture.

Therefore model usage as a **windowed node attribute** (`last_observed`, `observed_query_count`
over window N days), never a hard `dead` boolean. "Observed-cold in last N days" is a *candidate for
review*, not a delete instruction.

## Tier 1 — Liveness via parsed `QUERY_HISTORY` text (broadest reach, ship first)

**Why parsing the text is the right call here:** we already own a SQL parser. Feeding it executed
queries instead of repo files reuses the entire engine, and `QUERY_HISTORY` is available without
Enterprise Edition — widening the base, which was the user's goal.

- Liveness only needs **table-level references** ("was this touched, when"), which is the *easy,
  robust* subset of parsing — it survives BI-generated and ad-hoc SQL that would defeat full
  column-lineage extraction.
- `QUERY_HISTORY` carries `database_name`/`schema_name` (session context), so unqualified
  `SELECT * FROM orders` is mostly recoverable using the qualification machinery the indexer
  already has.

**Engineering reality:**
- **Volume:** millions of queries on a busy account, but most are one template with different
  literals. **Normalize + hash query text; parse uniques only.** This is the real work.
- **Blind spots to flag, not hide:** dynamic SQL / `EXECUTE IMMEDIATE` / stored-proc bodies — the
  row holds the *call*, not what ran, so proc-driven usage is under-counted. Views mask base tables
  (`FROM my_view` → edge to view; chase base via the static graph). `log()`/surface these gaps;
  don't let the overlay imply completeness.
- **Privilege:** `ACCOUNT_USAGE.QUERY_HISTORY` (365d) still needs the SNOWFLAKE-db grant.
  `INFORMATION_SCHEMA.QUERY_HISTORY()` is lower-privilege but ~7–14d and scoped. "Bigger base," yes;
  "frictionless for everyone," no.

## Tier 2 — Tableau / BI impact (the genuinely strong part)

**Reframe the user's "Tableau hides their queries":** Tableau does *not* hide SQL from Snowflake — it
hides it from **our repo**. Custom SQL lives inside the workbook (`.twb`/`.twbx`), never in the
indexed files, so statically we are blind to it. But when the workbook runs, the query executes and
lands in history, wrapped as an inner subquery:

```sql
SELECT SUM("t0"."amount"), "t0"."region"
FROM ( <the analyst's custom SQL> ) "t0"
GROUP BY ...
```

The custom SQL is right there in the inner block → our parser can extract table/column refs from it.
Query history is exactly how we **un-hide** what the repo can't see.

**Why this is the strongest tier:** liveness says "alive"; Tableau parsing says **"user-facing
alive — changing this breaks a dashboard."** It extends lineage *past the warehouse boundary into
consumption*, which the repo-only graph can never reach. This is the direct answer to the
dependency-impact / outage-planning north-star ([[project-dependency-impact-goal]]): "if I touch this
column, what BI breaks."

**Isolating Tableau queries — two levers:**
- **`SESSIONS.client_environment`** — join `QUERY_HISTORY` to `SESSIONS` on `session_id`; the client
  app identifies the Tableau driver. Works out of the box.
- **`QUERY_TAG`** — the prize. Mature Tableau+Snowflake setups configure connection query-banding to
  tag each query with the **workbook/view name** (often for cost attribution). If on, dashboard
  identity is *in the query text* → parse it straight out.

**Honest boundary (where Snowflake-only stops):**
- Without query tagging: you get "Tableau touched table/column X," not "dashboard *Sales Overview* →
  X." For the **impact** question that is often enough — you don't need the dashboard name to know a
  column is user-facing-critical; you need it to know *who to notify*.
- **Extracts (`.hyper`) decouple it:** an extract runs its query once at refresh; dashboards then
  serve from the extract. You see refresh-time table access, not which dashboards use the extract or
  whether anyone views them. A heavily-extract-based estate is partly invisible to Snowflake.
- **Full workbook→datasource→field flow** needs **Tableau's Metadata API** (GraphQL content catalog).
  Snowflake-side parsing gets the table/column boundary; the Metadata API gets the rest. → Tier 4.

## Tier 3 — `ACCESS_HISTORY` as the parser-recall oracle (access confirmed)

For the goal **"where is our graph wrong,"** parsing query text is **circular** — it produces our
parser's answer again on different input, which validates nothing. The independent oracle is
`ACCESS_HISTORY`, where **Snowflake's own engine** already resolved the query through views, dynamic
SQL, `SELECT *`, temp tables, and session context, exposing actual base objects and columns
(`objects_modified[].columns[].directSources/baseSources`). This finally gives a real *external*
denominator for lineage recall — the thing every current metric lacks (see the recall gap below).

- Also a higher-fidelity liveness source (column-level, pre-resolved).
- Latency ~45min–3h; 365-day retention.

> **Access status (2026-06-14): confirmed.** The target account has `ACCESS_HISTORY` /
> `ACCOUNT_USAGE` access. The earlier "Enterprise-only / Enterprise + audit grant" caveat is **not a
> blocker for this account** and is retired for this tier (see
> [corrections](#what-the-original-framing-got-wrong-corrections-logged) #1). The grant remains a
> *portability* note for other accounts, not a gate for this work.

### Why this tier matters: every existing metric uses an INTERNAL denominator

The graph-health metrics in [`coverage.py`](../../src/sqlcg/cli/coverage.py) all grade our graph
against **our own parse output** — they cannot measure recall:

| Metric | Definition (verified) | Denominator | What it can't tell you |
|--------|-----------------------|-------------|------------------------|
| `edge_health_strict_pct` | `good_edges_strict / total_edges` ([`coverage.py:62-68`](../../src/sqlcg/cli/coverage.py), [`:496-500`](../../src/sqlcg/cli/coverage.py)) | `COUNT(*)` of `COLUMN_LINEAGE` edges **we produced** | Quality of the edges we have — not the edges we **missed** |
| `edge_health_scoped_pct` | same, minus CTE/derived/temp dst ([`:74-86`](../../src/sqlcg/cli/coverage.py), [`:503-507`](../../src/sqlcg/cli/coverage.py)) | our scoped edge count | same — self-relative |
| `catalog_pct` | `catalogued_tables / total_tables` ([`:38-42`](../../src/sqlcg/cli/coverage.py), [`:482-486`](../../src/sqlcg/cli/coverage.py)) | `COUNT(*)` of `SqlTable` rows **we created** | tables we never parsed are not in the denominator |
| `cte_source_gap_writes` | CTE-wrapped writes with real edges but no `SELECTS_FROM` ([`:327-338`](../../src/sqlcg/cli/coverage.py)) | our write-query population | a count of one failure shape, not coverage of truth |
| `resolvable_write_col_edges` | `COLUMN_LINEAGE` rows on resolvable-source writes ([`:358-365`](../../src/sqlcg/cli/coverage.py)) | — (a monotone-up **count**, not a ratio) | "more" ≠ "more complete"; no ceiling |

Grading our graph against our own parse is circular: a parser blind spot is invisible because the
edge it should have produced is in *neither* the numerator *nor* the denominator. `ACCESS_HISTORY`
breaks the circle by supplying edges **we never saw**.

### 3.1 The oracle edge set (`E_oracle`)

`ACCOUNT_USAGE.ACCESS_HISTORY` has one row per executed query. Relevant columns (Snowflake shapes):

- `QUERY_ID` — string.
- `QUERY_START_TIME` — `TIMESTAMP_LTZ` (the window key).
- `DIRECT_OBJECTS_ACCESSED` — `ARRAY` of objects; the tables/views **named in the SQL text** (pre-view-resolution).
- `BASE_OBJECTS_ACCESSED` — `ARRAY` of objects; the **Snowflake-resolved base** tables/views, **chased through views** — this is the oracle source set. Each element:
  ```json
  { "objectId": 12345,
    "objectName": "MY_DB.MY_SCHEMA.MY_TABLE",
    "objectDomain": "Table",            // or "View" / "Materialized view" / "External table" / "Stream" / "Stage"
    "columns": [ { "columnId": 9, "columnName": "AMOUNT" }, ... ] }
  ```
- `OBJECTS_MODIFIED` — `ARRAY` of write targets, **same element shape** as the accessed arrays, with the columns that were written and their resolved upstreams:
  ```json
  { "objectName": "MY_DB.MY_SCHEMA.FACT_SALES",
    "objectDomain": "Table",
    "columns": [
      { "columnName": "AMOUNT",
        "directSources": [ { "objectName": "MY_DB.MY_SCHEMA.STG_SALES", "columnName": "AMT" } ],
        "baseSources":   [ { "objectName": "MY_DB.MY_SCHEMA.RAW_SALES", "columnName": "AMT" } ] } ] }
  ```

**Deriving table→table oracle edges.** Per query row, the oracle table→table edge set is the cross
product of resolved sources to write targets:

```
E_oracle_raw(row) = { (norm(s.objectName) -> norm(m.objectName))
                      | s in BASE_OBJECTS_ACCESSED,
                        m in OBJECTS_MODIFIED,
                        s.objectDomain in {Table, View, Materialized view, External table},
                        m.objectDomain in {Table, View, Materialized view, External table},
                        norm(s) is not None and norm(m) is not None and norm(s) != norm(m) }
```

This deliberately mirrors what our `SELECTS_FROM` edge means: a write query reads from a source
table (`SELECTS_FROM.dst_key = SqlTable.qualified`, `src_key = SqlQuery.id`) and writes to
`SqlQuery.target_table` (verified [`coverage.py:327-338`](../../src/sqlcg/cli/coverage.py),
[`indexer.py:991-992`](../../src/sqlcg/indexer/indexer.py)). Using `BASE_OBJECTS_ACCESSED` (not
`DIRECT_OBJECTS_ACCESSED`) is the point: it has already chased views and `SELECT *` to base tables,
which is exactly the resolution our parser is being graded on. (A view-layer-only variant using
`DIRECT_OBJECTS_ACCESSED` can be computed too, to separate "missed the view edge" from "missed the
base edge" — optional, see §3.4 residuals.)

Aggregate over a **window** (default proposed: **90 days** — see
[open decisions](#open-decisions-before-any-plan)) by union, collapsing duplicates and recording
support:

```
E_oracle = { e : (e, query_count, last_seen) | e in union over all rows in window }
```

`query_count`/`last_seen` per edge are kept for the liveness overlay and for the residual triage
in §3.4 (a once-in-90-days edge is treated differently from a daily one).

**Cheaper exact cross-check — `OBJECT_DEPENDENCIES`.** `ACCOUNT_USAGE.OBJECT_DEPENDENCIES` lists
declared object→object dependencies (`REFERENCING_*` → `REFERENCED_*`) for **views, materialized
views, and other DDL-declared objects**. It is *exact* (Snowflake's parser, not windowed
observation) and *cheap*, and is a strong cross-check for the **view layer** of our graph. It does
**not** cover ad-hoc `INSERT … SELECT` / CTAS-into-existing-table data flows (no DDL dependency is
recorded for a one-off DML), so it is a complement, not a replacement, for the `ACCESS_HISTORY`
oracle. Recommended use: validate our view→base edges against `OBJECT_DEPENDENCIES` (should be near
100% — a miss there is a pure parser bug, not a windowing artefact), and use `ACCESS_HISTORY` for
the DML lineage that `OBJECT_DEPENDENCIES` cannot see.

### 3.2 Name normalization — `norm(objectName)` → our canonical key

Our table key (`SqlTable.qualified` == `TableRef.full_id`, verified
[`base.py:81-99`](../../src/sqlcg/parsers/base.py)) is:

- **lowercase**, dot-joined, of the present identity parts: physical tables are `db.name`
  (schema.table) — or `catalog.db.name` when a database part was parsed — never quoted
  ([`base.py:65-99`](../../src/sqlcg/parsers/base.py): `catalog`/`db`/`name` are lowercased in
  `__post_init__`; `full_id` joins the non-empty parts with `.`).
- CTE/derived/temp nodes are namespaced `namespace::bare` and are **never** physical tables — they
  must not appear in `E_oracle` and are excluded by §3.3 scope.
- `db` here is the **schema** slot. `schema_aliases` (lowercased `staging → canonical`, from
  `[sqlcg.schema_aliases]`, verified [`config.py:84-114`](../../src/sqlcg/core/config.py)) is applied
  to that slot ([`base.py:765-772`](../../src/sqlcg/parsers/base.py) rewrites `ref.db`).

Snowflake `objectName` is **fully-qualified three-part** `DB.SCHEMA.TABLE` (uppercase by default,
quoted if the identifier was created quoted). The normalization rule that makes `E_oracle` and
`E_ours` comparable:

```
norm(objectName):
  1. Split on '.' into at most 3 parts, honouring double-quoted segments
     (a quoted segment may itself contain a '.'); strip surrounding double quotes
     from each part.  Snowflake always emits 3 parts in ACCESS_HISTORY.
  2. Lowercase every part (matches TableRef.__post_init__ case-folding;
     a quoted mixed-case Snowflake name will NOT round-trip — flag as a
     known residual, see note).
  3. Map to (database, schema, table). Apply schema_aliases to the SCHEMA part:
       schema := schema_aliases.get(schema, schema)        # lowercased map
  4. Emit the key in OUR shape. OUR physical keys are predominantly the
     2-part `schema.table` form (db slot = schema), with the database elided.
     Therefore the PRIMARY normalized key is:  f"{schema}.{table}"
     and a SECONDARY 3-part key  f"{database}.{schema}.{table}"  is kept as a
     fallback for the minority of our keys that carried a catalog/database part.
     Match E_oracle against E_ours on the 2-part key first; fall back to the
     3-part key only when our side actually stored 3 parts. (Rationale: our
     db slot is the schema, not the Snowflake database — see base.py:95-99.)
  5. EXCLUDE from E_oracle (return None) any object that is:
       - objectDomain not in {Table, View, Materialized view, External table}
         (drops Stage, Stream, Sequence, Function, etc.);
       - a transient/temp/scratch object: name matches the project noise_filter
         ignore_table_patterns (config.py get_noise_filter_patterns) OR the
         object is a session-temp (Snowflake temp tables surface with a
         session-scoped objectName); treat objectDomain-flagged temporaries and
         names matching the temp patterns as out-of-scope, mirroring our
         kind IN ('cte','derived','temp') exclusion in _Q_EDGE_HEALTH_SCOPED.
```

**Known normalization residuals (document, do not hide):** (a) a Snowflake identifier created
**quoted with mixed case** lower-cases on our side and will not match — rare in this estate, counted
separately so it never silently inflates the gap; (b) a table referenced in Snowflake under its full
`DB.SCHEMA.TABLE` whose DDL we indexed only as `SCHEMA.TABLE` is reconciled by the 2-part primary
key in step 4 — this is the expected common case, not a residual.

### 3.3 Scope discipline — measure recall only on the INTERSECTION (critical)

Recall **must** be measured on oracle edges **both of whose endpoints are tables our corpus
actually indexed**. Otherwise "missing" conflates *not-indexed* (a table whose SQL is simply not in
the repo we pointed `sqlcg` at — outside our mandate) with *not-extracted* (a table we indexed but
whose lineage our parser failed to produce — the actual recall defect). Only the second is a parser
bug; mixing them makes the metric un-actionable.

Define the **indexed table universe** `T_indexed = { t.qualified | t in SqlTable, t.kind not in
('cte','derived','temp') }` (physical tables only; same exclusion as
[`_Q_EDGE_HEALTH_SCOPED`](../../src/sqlcg/cli/coverage.py)). Then two oracle edge sets and therefore
**two denominators**:

- **(i) `E_oracle_scoped`** = `{ (s -> d) in E_oracle | s in T_indexed AND d in T_indexed }` — the
  **true recall denominator**. Both endpoints are tables we claim to cover, so every edge here is
  one we *could* have extracted.
- **(ii) `E_oracle`** (unscoped) — the **coverage ceiling**: how much of real observed lineage even
  touches our corpus. `|E_oracle_scoped| / |E_oracle|` is a separate *corpus-coverage* number ("what
  fraction of live lineage is even in scope for us"), reported alongside recall but never conflated
  with it.

### 3.4 The recall metric

> **Definitions (verbatim).**
>
> Let `E_ours` = the set of distinct table→table edges our graph asserts, derived as
> `(SELECTS_FROM.dst_key  ->  SqlQuery.target_table)` over write-kind queries — i.e. the same
> `(source-table -> written-table)` pairs the oracle produces — restricted to both endpoints in
> `T_indexed`, normalized to the same lowercase `schema.table` key.
>
> Let `E_oracle_scoped` be as defined in §3.3 (oracle edges with **both** endpoints in `T_indexed`).
>
> **Recall denominator (i) — the headline number:**
> ```
> oracle_recall_pct = | E_ours ∩ E_oracle_scoped |  /  | E_oracle_scoped |   × 100
> ```
> The denominator is **external** (Snowflake-resolved), so this is the first metric that measures
> what fraction of *real, observed* in-corpus lineage our parser actually captured.
>
> **Coverage ceiling (denominator ii) — reported, never conflated:**
> ```
> oracle_corpus_coverage_pct = | E_oracle_scoped | / | E_oracle |   × 100
> ```
> How much of all observed lineage even falls inside the corpus we indexed.

**Residual buckets** (the actionable output, not just a percentage):

- **oracle-has / we-miss** = `E_oracle_scoped \ E_ours` — **TRUE recall gaps.** Both tables are
  indexed, Snowflake observed the flow, we produced no edge. This is the **ranked worklist** that
  drives parser fixes (group by dst table → the analogue of the existing blindspot ranking, but
  externally grounded). Every item here is a falsifiable "we should have this edge."
- **we-have / oracle-lacks** = `E_ours \ E_oracle_scoped` — **ambiguous, cannot be fully
  disambiguated.** An edge here is *either* a real edge that simply **did not run in the window**
  (committed code that is correct but cold — recall the liveness asymmetry: absence ≠ wrong) *or* a
  genuine false positive (a phantom edge our parser invented). `ACCESS_HISTORY` cannot tell these
  apart on its own. The **window length is the trade-off knob**: a longer window (365d) shrinks the
  "real-but-cold" share of this bucket (fewer edges are unobserved), tightening it toward "probable
  false positive," at the cost of more staleness and more ad-hoc/one-off flows entering `E_oracle`.
  We therefore report this bucket as a *signal*, cross-referenced against
  `phantom_contradicted` ([`coverage.py:104-121`](../../src/sqlcg/cli/coverage.py)): an edge that is
  both oracle-lacking **and** catalog-contradicted is a high-confidence false positive; one that is
  oracle-lacking but catalog-confirmed is most likely real-but-cold.
- **both have** = `E_ours ∩ E_oracle_scoped` — agreement, the numerator.

**As a `gain`-style metric.** Surface in `sqlcg gain` Section G (next to the existing coverage
lines) as an **optional, overlay-gated** block — present only when an `access_history` overlay is
loaded (mirroring the `catalog load` precedent; never computed from the parser path):
```
Oracle recall (90d window, overlay loaded 2026-06-14):
  recall:           E_ours ∩ E_oracle_scoped / E_oracle_scoped   (N / D, P%)
  corpus coverage:  E_oracle_scoped / E_oracle                   (N / D, P%)
  TRUE recall gaps (oracle-has/we-miss): K   [top dst tables by gap count]
  oracle-lacking (ambiguous):            M   (of which catalog-contradicted: X = likely false positive)
```
Recall is a **ratio with an external denominator** (unlike the monotone-up
`resolvable_write_col_edges` count), so it has a real ceiling (100%) and regressions are visible.

### 3.5 Reframing the E8 / `cte_source_gap` gates in recall terms

The in-flight gates are phrased self-relatively and should be **restated against the oracle** once
the overlay exists:

- **E8 revival gate** is currently "`resolvable_write_col_edges` rises by ≥ +1,000 on the same
  corpus" ([`coverage.py:340-365`](../../src/sqlcg/cli/coverage.py)). That proves *more edges*, not
  *more correct coverage*. Recall reframing:
  > **E8 closed _X%_ of the measured recall gap** = `(gaps_before − gaps_after) / gaps_before` over
  > the **same `E_oracle_scoped`** (same window, same corpus). A +1,000-edge change that does not
  > move `oracle-has/we-miss` is adding edges the oracle never asked for; a change that closes the
  > gap is real recall.
- **`cte_source_gap_writes`** ([`coverage.py:327-338`](../../src/sqlcg/cli/coverage.py)) becomes
  "_Y_ of the CTE-source-gap writes correspond to a TRUE recall gap in `E_oracle_scoped`" — i.e. the
  internal failure-shape counter is *prioritized* by which of its rows the oracle confirms are real
  missing edges, turning a count of a symptom into a ranked, externally-validated worklist.

This keeps the existing internal counters (cheap, no warehouse access, good for fast CI signal) but
**anchors their improvement claims to recall** whenever the overlay is present.

### 3.6 Concrete `ACCOUNT_USAGE` access — grants and queries

Grants needed (run once by `ACCOUNTADMIN`; access **confirmed present** for this account):
```sql
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE <analyst_role>;
-- ACCESS_HISTORY and OBJECT_DEPENDENCIES both live in SNOWFLAKE.ACCOUNT_USAGE.
```

Oracle export (one query → file, exactly the `catalog load` ingestion shape — zero secrets, no live
connector for v1):
```sql
-- E_oracle source rows: resolved source -> write-target pairs over the window.
SELECT
    ah.query_id,
    ah.query_start_time,
    src.value:objectName::string   AS src_object,
    src.value:objectDomain::string AS src_domain,
    tgt.value:objectName::string   AS dst_object,
    tgt.value:objectDomain::string AS dst_domain
FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
     LATERAL FLATTEN(input => ah.base_objects_accessed) src,
     LATERAL FLATTEN(input => ah.objects_modified)      tgt
WHERE ah.query_start_time >= DATEADD('day', -90, CURRENT_TIMESTAMP())
  AND ARRAY_SIZE(ah.objects_modified) > 0;     -- write queries only

-- View-layer exact cross-check (cheap, non-windowed):
SELECT referenced_database, referenced_schema, referenced_object_name,
       referencing_database, referencing_schema, referencing_object_name,
       dependency_type
FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES;
```
Column-level recall (a future extension of the same oracle) reads
`tgt.value:columns[].directSources/baseSources` from the same `OBJECTS_MODIFIED` array; the
table-level metric above is the first deliverable.

## Tier 4 — Tableau Metadata API (dashboard granularity)

The only complete source for the BI-internal graph (workbook → datasource → field → lineage). A
separate connector, separate auth, post-Tier-2. Closes the dashboard-identity gap that query
tagging only partially fills.

## Architecture: one overlay, four fidelity tiers

- **Ingestion shape:** mirror [`catalog load <csv>`](../../src/sqlcg/cli/commands) — a post-index
  command (`sqlcg observe load <export>`), source-tagged (`source='query_history'` vs
  `source='access_history'`), with a defined precedence between sources. Start with a **file export**
  (user runs one Snowflake query, exports rows): zero secrets, no live-connector code, proves value
  before committing to a warehouse-connection subsystem. Build the live connector only if file-based
  ingestion earns it.
- **Never a parser input.** Overlay applies after indexing; runtime data never enters
  `qualify()`/`expand()`. Same rule as the removed parse-time schema-CSV path.
- **Lifecycle:** unlike static code, the overlay goes stale the moment it's loaded. Windowing
  (30/90/365d) and periodic refresh are first-class concerns — beware resync footguns
  (cf. pr-impact resync deferral in [[project-data-loss-impact-sprint]]).
- **Surface it in `gain` and blast-radius:** a runtime-cold table in a blast radius is lower-risk; a
  runtime-hot static-island is a parser gap to chase; a Tableau-consumed column is user-facing.

## Identity matching — the real cost (all tiers)

Joining runtime object names to static graph nodes is the same class of pain already hit with
temp-table namespacing, qualification, and dialect defaults — but on runtime names that include
ephemera (transients, CTAS-created temps, view resolution, case/quoting variants). `QUERY_HISTORY`'s
session-context columns help seed qualification (a point in favor of the parsed-text path), but the
long tail of mismatches is where the effort actually goes. Do not underestimate it.

## What the original framing got wrong (corrections logged)

1. **"All Snowflake users could use it"** — true for `QUERY_HISTORY` (no Enterprise gate). For
   `ACCESS_HISTORY` the gate is `IMPORTED PRIVILEGES` on the `SNOWFLAKE` db (historically also an
   Enterprise-Edition feature). **Update (2026-06-14):** the target account **has** this access, so
   for the recall-oracle work in [Tier 3](#tier-3--access_history-as-the-parser-recall-oracle-access-confirmed)
   the gate is **not a blocker** — it stays a portability note for other accounts, not a gate here.
   Privilege is never truly zero, but for this estate it is granted.
2. **"Alive vs dead"** — positive (alive) is strong; negative (dead) is weak. Model as windowed
   observed-usage, not a boolean.
3. **"Fully map the flow from Tableau"** — Snowflake gets the table/column boundary; full
   dashboard-level flow needs the Tableau Metadata API. Extracts further decouple refresh from view.
4. **"Tableau hides their queries"** — backwards: hidden from the *repo*, visible in *history*. That
   visibility is the whole opportunity.

## Recommended sequencing

1. **Tier 1** — file-based `QUERY_HISTORY` liveness overlay → broad reach, reuses the parser, the 2×2
   classification (lead with "find parser blind spots," not "delete dead tables").
2. **Tier 2** — Tableau-tagged subset of that same overlay → the user-facing impact edge.
3. **Tier 3** — `ACCESS_HISTORY` as the parser-recall oracle (access confirmed for this account; see §3).
4. **Tier 4** — Tableau Metadata API for dashboard granularity.

One overlay, four tiers, shipped in reach order. Each is independently useful; none requires the next.

## Open decisions before any plan

- **Primary goal first:** dead-table cleanup (→ Tier 1 liveness) vs graph-correctness (→ Tier 3 oracle)?
  They point at different first PRs; the oracle is arguably the stronger bet given the in-flight recall metric.
- **File export vs live connector** for v1 of the overlay (recommend file export).
- **Window length** and refresh story.
- Does the target Tableau estate use **live connections or extracts**, and is **query tagging**
  configured? This decides how far Tier 2 reaches before Tier 4 is needed.
