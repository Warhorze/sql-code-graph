# Issue #38 — Island-Count Lever: Live-DWH Verification (the gap prior reports left open)

**Date:** 2026-06-14
**Reviewer:** adversarial live-DWH acceptance ("try to break it")
**Under test:** origin/master HEAD `52a7f48` (PR-F #138 merged; worktree pyproject v1.28.3) —
the `_real_tables()` CTE/subscope-descent lever (PR-2 v1.26.1 + PR-3 v1.28.0).
**Corpus:** live DWH `/home/ignwrad/Projects/dwh` @ sha `fdf1b551` (1335 files), read-only.
**Method doc:** `plan/research/table_level_island_lever.md` §2 (recovered from git `4ff91cc`),
SELECTS_FROM-WCC reproduction, reused **exactly**.

---

## Verdict

**The island count dropped — and the drop is GENUINE.** Table-level `SELECTS_FROM` small
islands fell **147 → 43 (−104, a 71% reduction)** — well beyond the research-doc projection of
147→99 (−48). PR-3's subscope descent recovered substantially more sources than PR-2's CTE-body
descent alone projected (consistent with the PR-3 metric drop 110→1 exceeding the projected ~48
floor). Every adversarial probe came back clean: 0 phantom edges, 0 self-loops, 0 dropped
edges, every sampled new edge present in its on-disk source file, and **no island was merged by a
false edge** — 283 formerly-islanded tables all folded into the *real* giant component, and 0
giant-component tables were pushed out into islands. This is the measurement prior acceptance
reports (which verified edge correctness but never the WCC count itself) left open. **Lever
confirmed effective.**

Task 2 (forward-resync crash #128): **NOT reproduced** on this HEAD with memory available.

---

## TASK 1 — Island-count drop (MAIN)

### Baseline (PRE-lever) — provenance

Source: the user's existing graph `/home/ignwrad/.sqlcg/graph.db` (READ-ONLY), which is a
**genuine pre-lever DWH index**: `SchemaVersion` row v8 → sha `fdf1b551` (the research-doc
baseline sha), `SELECTS_FROM = 4871` (matches the PR-2 acceptance report's pre-PR-2 "master 4871"
exactly), `SqlQuery = 5183`. This is a real pre-lever index, not a re-derivation.

The §2 WCC repro run against it reproduces the published baseline **byte-for-byte**:

```
nodes=2030, edges=2930
WCC histogram = {2:86, 3:34, 4:18, 5:2, 6:3, 7:2, 8:1, 9:1, 1625:1}
small islands = 147, giant = 1625, island-member nodes = 405
```

All three independent figures from research §2 (nodes/edges, the full histogram, and the
405-member set) match exactly — this validates both the baseline *and* my WCC method before any
delta is claimed. **PRE baseline = 147 islands.**

### Current (POST-lever, origin/master HEAD)

Indexed the DWH ONCE from scratch into `/tmp/island_verify.duckdb` (worktree at `52a7f48`),
`sqlcg index` then `sqlcg catalog load columns.csv` (per project convention). Wall-time **~175 s**
(well under the 3-min budget); catalog load +64 s. Same §2 repro:

```
nodes=2137, edges=3909
WCC histogram = {2:22, 3:15, 4:3, 6:1, 7:1, 8:1, 2015:1}
small islands = 43, giant = 2015
```

### Delta

| Metric (SELECTS_FROM-WCC, noise-filtered both endpoints) | Baseline | Current | Δ |
|---|---|---|---|
| **small table-level islands** | **147** | **43** | **−104** |
| giant component | 1625 | 2015 | +390 |
| table-level edges (post-noise) | 2930 | 3909 | +979 |
| nodes | 2030 | 2137 | +107 |
| raw `SELECTS_FROM` edges (graph total) | 4871 | 6094 | +1223 |

### Exact SQL / method (identical for both DBs)

Noise filter from `/home/ignwrad/Projects/dwh/.sqlcg.toml`: drop tables matching regex `_bck`,
drop exact `ma.rtetl_delta`; applied to **both** endpoints. Table-level edge contract (§2):

```sql
SELECT DISTINCT sf.dst_key AS src, q.target_table AS tgt
FROM "SELECTS_FROM" sf JOIN "SqlQuery" q ON q.id = sf.src_key
WHERE q.target_table <> '' AND q.target_table <> sf.dst_key;
```

Rows → NetworkX `DiGraph` → `weakly_connected_components`; "small island" = any component that is
not the single giant component. Script: `/tmp/wcc_island.py`.

### Adversarial confirmation — the drop is REAL, not false-edge merging

| Probe | Result | Evidence |
|---|---|---|
| **Spot-check 20 new edges vs ON-DISK SQL** (truth, not 500-char-truncated `SqlQuery.sql`) | **20/20 genuine** | Every dst table-name present in its producing query's on-disk `.sql` file. 0 absent, 0 self-loops, 0 unqualified, in the sample. |
| **All 1223 new edges: phantom dst** | **0** | Every new dst is a real `SqlTable` node (PK `qualified`). |
| **All 1223 new edges: self-loop (dst==target)** | **0** | None. |
| **All 1223 new edges: dropped real edges** | **0** | `baseline − current = 0`: purely additive, no real edge removed. |
| **Did unqualified/alias edges falsely merge islands?** | **NO** | 9 new edges have an unqualified dst (genuine CTE aliases, e.g. `periode_selectie` — confirmed a `WITH periode_selectie AS (…)` CTE on disk). Removing ALL unqualified-source edges leaves the island count at **43→43** (giant only −11). The 147→43 drop stands entirely on properly-qualified real-table edges. |
| **Where did the islands go?** | **into the real giant** | 283 tables left small-island status; **all 283** folded into the giant component (none into new false mini-merges). |
| **Regression: any giant→island demotion?** | **0** | No table that was in the giant at baseline is islanded now. The lever is strictly additive. |
| **Plan proof-case** | confirmed | `ba.wtfe_bijverkoop_matrix`: baseline sources `[]` → current sources `ba.wtdh_artikel, ba.wtfe_verkoopinfo, ba.wtda_datum, ba.wtda_korting, ba.wtdh_artikel_cm_groep/segment/subgroep` — exactly the giant-component hubs §5 predicted its CTE body reads. |

### Supporting health (current graph)

| Metric | Value | Note |
|---|---|---|
| `cte_source_gap_writes` (raw plan SQL) | **1** | matches PR-3 report 110→1; the lone residual is a pure self-reload, correctly no edge |
| `cte_source_gap_writes` (`gain`, real-edges-only / inference-filtered) | **0** | PR-A guard |
| total `SELECTS_FROM` edges | 6094 | baseline 4871 → +1223 (≈ PR-2 +473 then PR-3 +755) |
| total tables (`SqlTable`) | 6577 | |
| total queries (`SqlQuery`) | 5186 | |
| edge health — strict (column) | 84% (46156/54736) | no regression vs prior reports (84%) |
| edge health — scoped (excl CTE/derived/temp) | 99% (36593/37147) | matches/improves prior (98%) |
| catalogued | 88% (5794/6577) | matches prior (88%) |
| degraded-parse queries (stored `parse_failed`) | 20 / 5186 | matches prior reports' 20 — no parse regression |

---

## TASK 2 — Forward-resync crash repro (#128 / pr_impact_followups OPEN 1)

**Result: NOT reproduced.**

Reused the already-indexed graph (copied to `/tmp/resync_repro.duckdb` — no second full index).
The DWH delta `cd1b43cc → fdf1b551` (= base→HEAD) is the exact repro scenario: it **adds** three
pure delete-only SQL files (`A etl/sql/fact/wtfa_omzet_week_delete.sql`,
`wtfs_voorraad_week_delete.sql`, `wtfa_inkoop_ontvangsten_week_delete.sql`; each is a bare
`DELETE FROM … WHERE …`). `cd1b43cc` is the parent of `e5eebd5a "chore: add delete jobs"`.

```
sqlcg analyze pr-impact --base cd1b43cc16541644d5cb7b6f113003f434e9e73a   (graph at HEAD fdf1b551)
```

This drove the full two-phase path: step-2 self-heal **backward** to `cd1b43cc`
(`_reindex_to_sha`), then **step-4b forward resync** `resync_changed(cd1b43cc → fdf1b551)`
(the `indexer.py:1108` site in the crash trace) over the delete-only delta. It **completed cleanly
(exit 0, ~3 s)**, advanced `indexed_sha` back to HEAD `fdf1b551` (proving the forward resync ran to
completion), and printed "No genuine lost producers". **No DuckDB internal assertion** in
`upsert_nodes_bulk(NodeLabel.QUERY)` — no crash anywhere.

The three delete-only files correctly produce **0 QUERY nodes** (a bare `DELETE` has no
producer/target) — i.e. the empty/no-QUERY-row case that the crash report implicates was exercised
and handled cleanly.

**Memory pressure plausibly implicated in the original report.** Throughout Task 2, memory was
comfortable (1.0–1.2 GB free, swap only ~388 MB — no thrash). The followups doc itself flags the
crash "surfaced on the 3.8 GB swap-thrashing dev box … *may* be memory/environment-induced rather
than a logic bug," and PR-F's `get_indexed_sha` SCHEMA_VERSION fix (which this HEAD includes)
repaired the self-heal SHA check that the original crashing run predates. On this HEAD with memory
available, the delete-only forward resync does not assert. This is a repro/diagnosis pass only — no
fix attempted. A definitive reproduction likely needs the original swap-thrashing condition (a full
DWH resync under memory pressure), which was not recreated here.

---

## Notes on environment / discipline

- DWH (`/home/ignwrad/Projects/dwh`) left **read-only** — only git refs read; no checkout, no write.
- The user's `/home/ignwrad/.sqlcg/graph.db` was opened **read-only** for the baseline measurement
  only; not modified.
- All new graphs built at `/tmp` (`island_verify.duckdb`, `resync_repro.duckdb`). One full DWH
  index, reused for every measurement.
- Work done in a throwaway worktree `/tmp/sqlcg-master` @ `52a7f48`; the user's branch
  `feat/pr-e-read-concurrency-memory-ceiling` and all uncommitted working-tree files were left
  untouched. Nothing committed.

## Minor observation (not a defect, not blocking)

The subscope descent emits **CTE alias names** as `SELECTS_FROM` dst for a handful of producers
(9 edges; e.g. `periode_selectie`, `klanten_history`, `collection_costs_parcels`). These are
unqualified (no schema), are genuine CTE bodies on disk, and provably contribute **0** to the
island reduction (they live inside the giant component). Cosmetically a CTE alias should not appear
as a "table" dst; functionally harmless. Worth a one-line follow-up to filter CTE-alias names from
the dst set, but it neither inflates the island drop nor creates a false edge to a real table.
