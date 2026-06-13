# Getting started with sqlcg

## 1. What is this?

`sqlcg` indexes a SQL repository into a local graph and lets you ask lineage
questions against it: where a column comes from, what breaks downstream if a
table changes, whether a PR deletes a table's sole producer, and which
pipelines go dark if a table was not loaded last night.

**Three audiences this guide is written for:**

- **Impact-analysis engineer** — you are on-call or doing a pre-deploy review
  and need to know the blast radius of a change before it ships.
- **LLM-assisted developer** — you have Claude Code open and want lineage facts
  available to your agent via MCP tools without running queries by hand.
- **Onboarding engineer** — you are new to the warehouse and need a fast way to
  understand which tables are load-bearing and how data flows between schemas.

All three personas share the same setup. The four questions in section 3 are the
daily interface; workflows by persona follow in section 4.

---

## 2. Setup (zero-friction)

### Install

The PyPI package is currently at 1.5.1 (publish pipeline fix is in flight).
Install from source until that lands:

```bash
# Option A — install as a global tool from GitHub (recommended)
uv tool install git+https://github.com/Warhorze/sql-code-graph@master

# Option B — clone and sync
git clone https://github.com/Warhorze/sql-code-graph
cd sql-code-graph
uv sync
```

Verify:

```bash
sqlcg version
# sqlcg version 1.25.x
```

### Initialise the graph database

The graph lives in `~/.sqlcg/graph.db` by default — a single global file, not
a per-repo directory. Running `sqlcg db init` inside your repo does not change
where the graph file lives.

```bash
cd /path/to/your/sql-repo
sqlcg db init
# Database initialised at /home/<you>/.sqlcg/graph.db (schema v8)
```

### Index your SQL files

```bash
sqlcg index . --dialect snowflake
```

Use `--dialect ansi` for standard SQL. Omit `--dialect` if you have a
`.sqlcg.toml` in the repo root that declares the dialect (the `auto` default
reads from there). Indexing 1,335 files on a laptop takes roughly 2–3 minutes.

### Check graph health

```bash
sqlcg gain
```

Real output from the live DWH acceptance run (1,335 files, Snowflake dialect):

```
G. Coverage
  Tables with catalog: 5785 / 6571 (88%)
  Edge health (strict, column-level): 43807 / 52514 (83%)
  Edge health (table-level, legacy): 44344 / 52514 (84%)
  Edge health (scoped, excl. CTE/derived/temp): 35092 / 35688 (98%)
  Phantom edges: 12603 / 52514 (24%)
    confirmed: 12002  contradicted: 413 (0.8% of all edges)  unverified: 188
  Blindspot tables: 637
    15 table(s) cover 80% of bad-edge volume
  Corpus: 1335 files, db_path=/home/ignwrad/.sqlcg/graph.db
  Write queries with zero outgoing lineage: 1090 / 2349
  Rescuable unqualified edges: 86
```

The **scoped edge health** (98% here) is the most meaningful number: it
measures edges that exclude CTEs, derived tables, and temp tables — the ones
whose source can actually be verified against the catalog.

---

## 3. The four questions, increasing in power

### Question 1 — Where is this column defined?

```bash
sqlcg find table ba.wtfe_verkoopinfo
```

```
ba.wtfe_verkoopinfo                  table
ba.wtfe_verkoopinfo_backup_us26608   table
```

Use `find column` to confirm a specific column exists and is indexed:

```bash
sqlcg find column ba.wtfe_verkoopinfo.da_transactie_id
```

```
ba.wtfe_verkoopinfo.da_transactie_id
```

If the column does not exist in the graph `find column` returns "No results"
(not an error — the column genuinely has no indexed definition).

> **MCP equivalent** — in Claude Code, `trace_column_lineage` (with argument
> `table_col`) returns the same lineage together with file:line provenance and
> confidence scores. Use `sqlcg mcp best-practices` first to understand which
> outputs are facts vs. heuristics.

---

### Question 2 — What feeds this table?

```bash
sqlcg analyze upstream ba.wtfe_verkoopinfo.da_transactie_id
```

```
da.ttint_verkooptransactie.dekasnr
da.ttint_verkooptransactie.detranr
da.ttint_verkooptransactie.da_filiaalnr
```

Each row is a source column (schema.table.column) that contributes to the
queried column. The default `--depth 5` traverses up to five hops upstream;
increase it with `--depth N` for deep pipelines.

**How to read the answer:** the source columns tell you which upstream tables
you need to touch if you want to rename, retype, or remove `da_transactie_id`.
They are the tables whose owners to notify before a change.

---

### Question 3 — What's the impact if I change it?

Two commands work together here. `downstream` traces column-level consumers;
`impact` lists every query that reads the table.

```bash
sqlcg analyze downstream ba.wtfe_verkoopinfo.da_transactie_id
```

```
ia_tableau.transactie obt.transactie id
ba.wtfe_kpi_supply_chain_artikel_voorraadlocatie...
ba.wtfe_bijverkoop_matrix.da_transactie_id
ba.wtfe_kpi_voorraadhoudende_artikelen...
```

```bash
sqlcg analyze impact ba.wtfe_verkoopinfo
```

Returns the full list of CREATE_TABLE, CREATE_VIEW, MERGE, and OTHER queries
that read from `ba.wtfe_verkoopinfo`, each with file:line provenance.

**How to read the answer:** `downstream` tells you which downstream columns
carry the value and therefore which columns in which tables need schema
adjustments. `impact` gives you the full set of SQL files to review or retest.

---

### Question 4 — What breaks if this wasn't filled last night?

This is the on-call question. Run it before an incident bridge, not during.

```bash
sqlcg analyze empty-impact ba.wtfe_verkoopinfo
```

```
View 2 — Value derivation (PRIMARY)
  ia_businessobjects.ba_wtfe_verkoopinfo   84   full
  ia_analytics.ba_wtfe_verkoopinfo         84   full
  ia_tableau.ba_wtfe_verkoopinfo           84   full
  ... (+ partial ia_semantic / ia_tableau derivations)
```

The command returns two views:

- **View 2 (PRIMARY)** — downstream columns that would contain NULL or zero
  because their value derives (fully or partially) from the named table. The
  number (84 above) is the column count affected per downstream table.
  `full` means all columns in that downstream table derive from the source.
- **View 1 (SUPPLEMENT)** — tables that go row-empty via a direct gating join;
  they receive no rows at all when the source is empty.

Use `--max-depth N` to limit traversal when the graph is very large:

```bash
sqlcg analyze empty-impact ba.wtfe_verkoopinfo --max-depth 3
```

---

### Power-user: branch-vs-master blast radius (`pr-impact`)

Before merging a PR that touches ETL files, check whether any table loses its
sole producer:

```bash
# Ensure the graph is indexed at master (the base ref)
sqlcg reindex .
sqlcg analyze pr-impact --base master
```

The command resyncs the graph from `master` to HEAD, then reports:

- **Lost producers** — tables whose only producing SQL file was removed or
  gutted. These are genuine data-loss risks. The output includes the downstream
  blast radius (same two-view format as `empty-impact`).
- **Renamed producers** — tables where the producer was renamed AND all
  consumers were updated; these are suppressed (no false alarm).
- **Exit code 0** if no genuine losses; **exit code 1** if losses are found.

Real output from the C1 (TRUE loss) acceptance run:

```
Lost producers (genuine data-loss risk):
  ba.wtda_inkoop_herkomst   <- etl/sql/dim/wtda_inkoop_herkomst.sql

View 2 — Value derivation (PRIMARY)  — 20 value-empty columns across 20 tables:
  ba.wtfe_inkoop_pakbon, ba.wtfe_inkoop_factuur, ba.wtfe_inkoop_ontvangst ...
```

Real output from the C2 (RENAME, cry-wolf suppressed) acceptance run:

```
Base: fdf1b551 → Head: 03bf6bf8
No genuine lost producers detected.
```

**Operational note:** `pr-impact` requires the graph to be indexed at `base`
when it starts. Each run leaves the graph at HEAD. If you switch branches
between runs, resync first:

```bash
sqlcg reindex .
sqlcg analyze pr-impact --base <sha>
```

The installed git post-checkout hook fires `sqlcg reindex` automatically on
branch switches, but it is asynchronous — chain the commands explicitly in
scripts to avoid races.

---

## 4. Three workflows by persona

### Impact-analysis engineer

You are on-call. A pipeline alert fires at 03:00.

1. **Which table is empty?** — `sqlcg find table <name>` confirms it is indexed.
2. **What depends on it?** — `sqlcg analyze empty-impact <table>` shows every
   downstream table that will have NULL values (View 2) or zero rows (View 1).
3. **Is this a producer loss?** — if the on-call cause is a code deploy, run
   `sqlcg analyze pr-impact --base <pre-deploy-sha>` to see if a producer SQL
   file was removed in that deploy.

All three commands run in seconds against the local graph. No database or
warehouse connection is needed.

### LLM-assisted developer

You have Claude Code open. Register the MCP server so lineage facts are
available to your agent:

```bash
sqlcg mcp setup
# Prints the JSON config block — paste into Claude Code MCP settings
```

Read `sqlcg mcp best-practices` once to understand which tool outputs are
facts (column lineage, file:line provenance) vs. heuristics (unused tables,
risk scoring) — the boundary matters when your agent reasons about the output.

The key MCP tool is `trace_column_lineage` (argument name is `table_col`, not
`column`). When Claude Code needs the graph to reflect an in-progress change,
stop the server and reconnect:

```bash
sqlcg mcp stop
# In Claude Code: /mcp → reconnect sql-code-graph (or restart the session)
# Then reindex if you switched branches:
sqlcg reindex .
```

`sqlcg mcp restart` stops the server only — it does not bring it back. The MCP
client (Claude Code) owns the process lifecycle and respawns on reconnect.

### Onboarding engineer

You are new to a large warehouse. Three commands orientate you quickly:

1. `sqlcg db info` — how many tables, columns, queries, and edges are indexed;
   schema version; blindspot ranking.
2. `sqlcg gain` — overall edge health and which tables account for most phantom
   (unverifiable) edges. Low scoped-health on a specific table means its DDL
   is not indexed yet.
3. `sqlcg analyze unused` — tables with no detected read. A long list here
   usually means either parse coverage gaps or genuinely stale tables.
   The command prints a KNOWN GAP warning: tables used only as a gating join
   filter (no value selected from them) are not detected and appear as unused.

Then run Question 1 (`find table`) on the hub tables you know — validate that
the graph reflects reality on your known ground truth before relying on it for
unknown territory.

---

## 5. Where to go next

| Resource | Purpose |
|---|---|
| [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) | System design, known limitations, performance postmortems |
| `sqlcg mcp best-practices` | Fact vs. heuristic boundary for MCP tool consumers |
| [`docs/cli.md`](cli.md) | Full CLI reference (every command and flag) |
| `sqlcg analyze failures` | Lists files that failed to parse with their dominant error-code bucket — start here when coverage is lower than expected |
| `sqlcg git install-hooks` | Install post-checkout + post-merge hooks that auto-reindex on branch switch |
