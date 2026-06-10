# Postmortem: Graph Health Sprint (2026-06-10)

Plan: [`graph_health_catalog_and_metrics.md`](../sprints/graph_health_catalog_and_metrics.md)
(Opus plan-review: APPROVE-WITH-CORRECTIONS, 2 blockers corrected pre-implementation).
Executed in one day as 5 plan PRs + 1 salvage PR, all merged to master:

| PR | # | Shipped | Version |
|---|---|---|---|
| PR 1 | #67 | Honest metrics: strict column-level health, scoped split, phantom 3-way verdict, weighted blindspots, fingerprint, recall lines | 1.9.0 |
| PR 5 | #68 | `parse_failed` reclassification — 96.1% of flagged rows were a measurement artifact (`_can_have_column_lineage` gate); edges byte-identical | 1.9.1 |
| PR 3 | #69 | Usage-derived catalog (`HAS_COLUMN source='usage'`) + precedence-aware upsert (`ddl > information_schema > usage`) | 1.10.0 |
| PR 2 | #71 | `sqlcg catalog load` (INFORMATION_SCHEMA CSV), `get_catalog_path()` persistence, source-aware reindex delete; CLAUDE.md prohibition condition resolved | 1.11.0 |
| salvage | #70 | P1a CTAS kind guard + P5 USE SCHEMA resolution rescued from closed #66 (its central P1b lever stays dead/stashed) | 1.12.0 |
| PR 4 | #72 | Transform-kind classification: PASS_THROUGH / RENAME / AGGREGATION / EXPRESSION (AST-only, zero `.sql()` calls, guard-pinned) | 1.13.0 |

## Final measured state (fresh DWH index + catalog load, 2026-06-10)

| Metric | Sprint start (stale live DB, table-level ruler) | Final |
|---|---|---|
| Strict edge health | not measurable (legacy 72% partly fictional) | **74%** (= structural unscoped ceiling; ~12.3k by-design CTE chain edges cap it) |
| Scoped health (KPI) | not measurable | **84%** on an honest denominator |
| Tables catalogued | 54% | **88%** (6,378 / 7,217) |
| Phantom verdicts | 18.3k undifferentiated (26%) | 8,286 confirmed / **688 contradicted** / 4,349 unverified |
| Blindspot tables | 979 | **602** |
| Index wall time | ~73–75 s | 69–75 s (unchanged; budget 180 s) |

Apparent regressions that are corrections: scoped 91%→84% after #70 (P1a un-hid ~7,600
edges mislabeled `kind='derived'` — physical tables silently excluded from the KPI);
contradicted rising after PR 3 (usage evidence falsifying positional guesses). Both are
the ruler getting *more honest*, not the graph getting worse.

## Key lessons

1. **Measure the ruler before the data.** Three of six PRs fixed measurement, not lineage:
   table-level health hid 1,504 catalog-contradicted edges inside "good"; `parse_failed`
   was 96% artifact; `kind` mislabeling hid 7,600 edges from the scoped KPI. Every
   pre-sprint number was wrong in at least one direction.
2. **The 2026-05 INFORMATION_SCHEMA removal measured the wrong axis.** "Zero edge delta"
   was true — schema never creates edges; it makes edge *targets resolvable*. The metric
   that shows the win (edge health) didn't exist at decision time. CLAUDE.md now records
   the corrected rule: catalog-level enrichment allowed, parse-time schema feeding still
   forbidden.
3. **Stale-export risk is anti-correlated with value** (user insight): XML-DDL-cohort
   tables are old/stable (active tables get migrated to .sql changelogs), and `ddl >
   information_schema` precedence makes drift harmless exactly where drift is likely.
   Live confirmation: only **12** contradictions on info-schema-sourced tables.
4. **Dead levers stay dead**: P1b (node layer) and Fix B (edge layer) both measured ~zero
   because they folded bare names onto *uncatalogued* canonicals. Catalog-first dissolved
   the problem they were attacking.
5. **Live DB staleness**: the production `~/.sqlcg` graph carried ~22k stale edges
   (69,076 vs 46,873 fresh) — all pre-sprint percentages were quoted against a stale
   graph. The fingerprint line in Section G now makes this visible.

## Residual gaps (named, sized, filed)

- **787 ddl-contradicted edges** — real parser positional-guess bugs, now precisely
  countable via the contradicted bucket. Future sprint.
- **`_DST_TABLE` dotted-column strip** — column names containing literal dots
  ("Omzet excl.") break the table-strip in [`coverage.py`](../../src/sqlcg/cli/coverage.py);
  `ba.wtfa/wtfe_kpi_datum_klant` (~608 edges) appear as blindspots despite being
  catalogued. Needs longest-prefix lookup or stored table_id. Filed in PR 5 taxonomy.
- **UPDATE/DELETE/MERGE/CREATE_PROC lineage** (349 statements, no extraction path) —
  structural, T-07-XX.
- **`catalog load` wall time ~81 s** for 144k rows — acceptable one-shot, but the
  end-of-index auto-reapply pays it on every full index (~156 s combined, still under
  budget). Optimization candidate (single-unnest insert is the dominant cost).
- **`<unknown>` / stored procedures** (470 edges) — structural, documented.

## Operator runbook (to see these numbers on the live graph)

```
sqlcg mcp stop          # release the write lock
sqlcg index <dwh-root>
sqlcg catalog load columns.csv
sqlcg gain              # Section G
```

Tags pending at close: v1.9.0 (34d0af8), v1.9.1, v1.10.0, v1.11.0, v1.12.0, v1.13.0 —
user tags after verification per house rule.
