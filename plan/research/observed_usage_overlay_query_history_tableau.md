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

## Tier 3 — `ACCESS_HISTORY` as the column-lineage oracle (Enterprise only)

For the goal **"where is our graph wrong,"** parsing query text is **circular** — it produces our
parser's answer again on different input, which validates nothing. The independent oracle is
`ACCESS_HISTORY`, where **Snowflake's own engine** already resolved the query through views, dynamic
SQL, `SELECT *`, temp tables, and session context, exposing actual base objects and columns
(`objects_modified[].columns[].directSources/baseSources`). This finally gives a real *denominator*
for column-lineage recall.

- Requires **Enterprise Edition** + `IMPORTED PRIVILEGES` on the `SNOWFLAKE` db (ACCOUNTADMIN-granted).
  So "all Snowflake users" is false for this tier — it's "Enterprise + audit grant."
- Also a higher-fidelity liveness source (column-level, pre-resolved).
- Latency ~45min–3h; 365-day retention.

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

1. **"All Snowflake users could use it"** — true for `QUERY_HISTORY` (no Enterprise gate); false for
   `ACCESS_HISTORY` (Enterprise + audit grant). Privilege is never truly zero.
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
3. **Tier 3** — `ACCESS_HISTORY` as the column oracle where Enterprise allows.
4. **Tier 4** — Tableau Metadata API for dashboard granularity.

One overlay, four tiers, shipped in reach order. Each is independently useful; none requires the next.

## Open decisions before any plan

- **Primary goal first:** dead-table cleanup (→ Tier 1 liveness) vs graph-correctness (→ Tier 3 oracle)?
  They point at different first PRs; the oracle is arguably the stronger bet given the in-flight recall metric.
- **File export vs live connector** for v1 of the overlay (recommend file export).
- **Window length** and refresh story.
- Does the target Tableau estate use **live connections or extracts**, and is **query tagging**
  configured? This decides how far Tier 2 reaches before Tier 4 is needed.
