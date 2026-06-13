# Feature Acceptance — DWH live exercise (sqlcg 1.25.0)

Date: 2026-06-13. Tester: skeptical-data-engineer acceptance pass.
Install: `/home/ignwrad/.local/bin/sqlcg` = `sqlcg version 1.25.0` (confirmed).
Graph reused (NOT re-indexed): `/home/ignwrad/.sqlcg/graph.db`, schema v8,
indexed_sha `fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6`.
All CLI run from `/home/ignwrad/Projects/dwh`.

Corpus as actually reported by `db info` (differs from the prompt's stated
2122 tables / 40340 edges — recording reality): 1335 files, 6571 SqlTable,
147392 SqlColumn, 5186 SqlQuery, 52514 COLUMN_LINEAGE edges.

## Per-section PASS/FAIL

| Section | Feature | Verdict |
|---|---|---|
| A1 | `find table` / `find column` | PASS |
| A2 | `analyze upstream` (column) | PASS |
| A3 | `analyze downstream` / `analyze impact` | PASS |
| A4 | `analyze empty-impact` | PASS |
| A* | `ba.wtdh_artikel` column trace | PASS (column-level works; upstream shallow/degraded but NOT table-only) |
| B | `analyze unused` | PASS |
| B | `catalog load` coverage (via `gain`) | PASS (88% tables catalogued) |
| B | temp-table identity namespacing | PASS (file-scoped `file::name`, no collision) |
| C1 | `pr-impact` TRUE producer loss | PASS |
| C2 | `pr-impact` RENAME (cry-wolf suppression) | PASS |
| C3 | `pr-impact` BENIGN (no false positive) | PASS |
| C* | `pr-impact` resync ergonomics | FOOTGUN (see below) — functionally correct, operationally fragile |
| D | `mcp setup/start/status/stop/best-practices` | PASS |
| D | `mcp restart` | **FAIL as named** — stop-only, does not restart (see verdict) |
| E | `db info` / `db list-repos` | PASS |
| E | `reindex` incremental (`--from/--to`) | PASS |
| E | `git install-hooks --help` | PASS |
| F | PyPI publish pipeline | BROKEN — root cause identified (see section F) |
| — | **DWH left pristine** | YES (proven; one self-inflicted untracked-file loss occurred and was fully recovered — see incident) |

---

## A. The four guide questions

### A1 — "Where is this column defined?"
`find table` works; `find column` requires a REAL column name (a wrong name
returns "No results", which is correct, not a bug).

```
$ sqlcg find table ba.wtfe_verkoopinfo
qualified                            kind
ba.wtfe_verkoopinfo                  table
ba.wtfe_verkoopinfo_backup_us26608   table

$ sqlcg find column ba.wtfe_verkoopinfo.da_transactie_id
id
ba.wtfe_verkoopinfo.da_transactie_id
```
Verified: Y. Discrepancy: none. (Note: `find column ba.wtfe_verkoopinfo.artikel`
returned "No results" — correct, that column does not exist; the table uses
names like `da_transactie_id`, `ta_rowid`.)

### A2 — "What feeds this table?" `analyze upstream <column>`
```
$ sqlcg analyze upstream ba.wtfe_verkoopinfo.da_transactie_id
id                                  file:line
da.ttint_verkooptransactie.dekasnr  .../ddl/chan...
da.ttint_verkooptransactie.detranr  ...
da.ttint_verkooptransactie.da_filiaa...
```
Verified: Y. Real source columns in `da.ttint_verkooptransactie` with provenance.

### A3 — "What's the impact if I change it?" `downstream` + `impact`
```
$ sqlcg analyze downstream ba.wtfe_verkoopinfo.da_transactie_id
ia_tableau.transactie obt.transactie id
ba.wtfe_kpi_supply_chain_artikel_vo...
ba.wtfe_verkoopinfo_backup_us26608...
ba.wtfe_bijverkoop_matrix.da_transa...
ba.wtfe_kpi_voorraadhoudende_artike...
```
`analyze impact ba.wtfe_verkoopinfo` returns a long list of CREATE_TABLE /
CREATE_VIEW / MERGE / OTHER consumers with file:line. Verified: Y.

### A4 — "What breaks if this wasn't filled last night?" `empty-impact`
```
$ sqlcg analyze empty-impact ba.wtfe_verkoopinfo --max-depth 3
View 2 — Value derivation (PRIMARY)
  ia_businessobjects.ba_wtfe_ver...   84   full
  ba.wtfe_verkoopinfo_backup_us2...   84   full
  ia_analytics.ba_wtfe_verkoopin...   84   full
  ia_tableau.ba_wtfe_verkoopinfo      84   full
  ... + many partial (1-col) ia_semantic/ia_tableau derivations
```
Two-view output (value-derivation primary + row-reachability supplement).
Verified: Y.

### A* — `ba.wtdh_artikel` column trace (the timed-out-during-index probe)
`find table ba.wtdh_artikel` → present (plus etl/cm variants). Table has 326
columns in the graph.
```
$ sqlcg analyze upstream ba.wtdh_artikel.s1_artikel
ba.wtdh_artikel_etl.s1_artikel   .../ddl/changelogs/...   (single hop)

$ sqlcg analyze downstream ba.wtdh_artikel.s1_artikel
ba.wtfe_inkoop_order.sa_artikel_s1
ba.wtda_artikel.s1_artikel
ba.wtfv_presentatievariant.sh_artik...   ?     (provenance gap on some rows)
ba.wtda_verkoopprijs_historie.sn_ar...
ba.wtfe_verkoopinfo.sn_artikel_s1
... (rich downstream fan-out)
```
**VERDICT: column-level tracing on artikel WORKS — it is NOT table-only.**
Downstream is a rich, sane blast radius. The UPSTREAM side is shallow (one hop
to `ba.wtdh_artikel_etl`), consistent with the source file's index timeout
degrading the deep producer chain. A few downstream rows show `?` for
file:line (provenance gaps). Net: usable column lineage, mildly degraded on the
upstream/provenance side. Verified: Y (with the noted degradation).

---

## B. Recently-shipped features

### `analyze unused` — PASS
Returns tables with no detected read; output includes a documented KNOWN GAP
(pure-gating CTE reads not detected). Sample rows: `ia_tableau.ba_wtda_marketing_automation_journey`,
`ia_analytics.ba_wtfe_configurator_orders`, etc. Verified: Y.

### `catalog load` coverage via `gain` — PASS
`gain` G-section: `Tables with catalog: 5785 / 6571 (88%)`. Catalog enrichment
is present and applied. (SqlColumn total 147392; the 122,815 figure from the
prompt is the information_schema column subset — coverage confirmed present.)
Verified: Y.

### temp-table identity namespacing — PASS
```
ba.tmp_btw_yde                                   (real persisted temp, schema-qualified)
ba.tmp_prognose_ontdubbeld
ddl/.../WTDH_ARTIKEL.sql::ba.tmp_art             (file-scoped)
ddl/.../WTDH_ARTIKEL_ETL.sql::ba.tmp_art         (file-scoped — DISTINCT from above)
```
`WTDH_ARTIKEL.sql::ba.tmp_art` and `WTDH_ARTIKEL_ETL.sql::ba.tmp_art` are
separate nodes despite the identical local name → namespacing prevents
collision. 1332 file-scoped/CTE entities total. Verified: Y, no collision.

---

## C. `pr-impact` — three designed scenarios (throwaway branch)

Restore point captured BEFORE: branch `chore/wssi_spagethi_DAT-6851`, HEAD
`fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6`, 29 untracked entries, 50 stashes.
Throwaway branch `tmp-shepherd-primpact` created off HEAD.

Target selection note: `pr-impact` flags a table only when it loses ALL
graph-attributed producers. Two false starts (`ba.wtfe_inkoop_order`,
`ba.wtda_artikel`) did NOT flag because each retains another producer in the
graph (other INSERT files / a CREATE_VIEW DDL). The clean single-producer
target was `ba.wtda_inkoop_herkomst` (sole producer
`etl/sql/dim/wtda_inkoop_herkomst.sql`).

### C1 — TRUE producer loss → PASS
Commented out the entire sole producer file, committed, reverse-indexed graph
to base, ran `pr-impact --base fdf1b551`:
```
Lost producers (genuine data-loss risk):
  ba.wtda_inkoop_herkomst   <- etl/sql/dim/wtda_inkoop_herkomst.sql

View 2 — Value derivation (PRIMARY)  — 20 value-empty columns across 20 tables:
  ba.wtfe_inkoop_pakbon, ba.wtfe_inkoop_factuur, ba.wtfe_inkoop_ontvangst,
  ba.wtfe_inkoop_order_afkeur, ia_semantic.inkoop herkomst, ia_tableau.* views ...
View 1 — Row reachability (SUPPLEMENT): same set of gated tables.
EXIT 1 (non-zero = losses found)
```
Listed the lost table WITH its dropping file AND a full downstream blast
radius. Verified: Y.

### C2 — RENAME (cry-wolf suppression) → PASS
Renamed `wtda_inkoop_herkomst` → `_v2` in the producer AND all 6 consumers,
committed, reverse-indexed to base, ran pr-impact:
```
Base: fdf1b551 → Head: 03bf6bf8
No genuine lost producers detected.
```
No blast warning — the rename with consumers updated is correctly suppressed.
Verified: Y. **Minor discrepancy:** the help text promises a dedicated "verify
consumers updated" section; the actual output simply reports "no genuine lost
producers" with no rename-callout section. Suppression works; the labelled
section does not render. (Also observed: when the graph is at the WRONG base,
the same scenario spuriously flags two file-scoped temp entities with empty
blast radius — a confirming symptom of the resync footgun below, not a C2
failure.)

### C3 — BENIGN (comment-only) → PASS
Appended one comment line to the producer file only, committed,
reverse-indexed to base, ran pr-impact:
```
Base: fdf1b551 → Head: af01b648
No genuine lost producers detected.
```
No false positive. Verified: Y.

### C* — pr-impact resync FOOTGUN (operational finding)
`pr-impact --base X` requires the global graph to be indexed AT base X when it
starts. Each run leaves the graph at HEAD; a second run then refuses with:
`Hint: Graph is indexed at '<HEAD>' but base_ref resolves to '<base>'. Run
'sqlcg index' or 'sqlcg reindex' ... first`. It is not self-healing. Worse, the
installed git post-checkout/post-merge hooks fire a background `sqlcg reindex`
on every branch switch, which silently moves the graph off base and races the
next pr-impact. Working protocol that succeeded: avoid branch checkouts during
the run (commit/amend on the throwaway branch only), and chain
`reindex --from <currentGraphSha> --to <base>` immediately before each
`pr-impact` in one shell command. This matches the deferred "pr-impact
resync-footgun" backlog item — it is real and reproducible.

### Git-pristine PROOF (mandatory)
```
$ git branch -D tmp-shepherd-primpact      → deleted
$ git rev-parse --abbrev-ref HEAD           → chore/wssi_spagethi_DAT-6851
$ git rev-parse HEAD                         → fdf1b551a34601a6cf3ce1c8b9f76e27ce2753e6   (== restore point)
$ git log --oneline -3
  fdf1b551 chore: restore wtfs_artikelstatus and wtfs_openstaande_orders jobs
  e5eebd5a chore: add delete jobs
  cd1b43cc fix: improve effficientcy wtfs_voorraad_week
$ git status: staged=0  tracked-mod=0  untracked=29  tmp-shepherd-branches=0  stashes=50
```
DWH is pristine. No push, no commit left on any real branch, throwaway deleted.

#### INCIDENT (self-inflicted, fully recovered) — `git add -A` swallowed untracked files
During the first (abandoned) C1 attempt I ran `git add -A && git commit` on the
throwaway branch. The DWH had 39 untracked, NON-gitignored files (`.sqlcg.toml`,
`.sqlcgignore`, `anomalies_with_jobs.csv`, `plan.md`, the changelog XMLs, k8s
job templates, `sqlcg-issues/*`, two `etl/sql/validation/*.sql`, etc.). `git add
-A` staged ALL of them into commit `b00934dd`. The subsequent `git reset --hard
fdf1b551` (whose tree lacks them) then DELETED them from the working tree.
Recovery: `git checkout b00934dd -- <each of the 39 files>` then `git reset HEAD`
to return them to untracked. Final `git status` matches the original 29-entry
untracked listing byte-for-byte. **Lesson for the section-C protocol: NEVER use
`git add -A` in the DWH — stage only the specific test files by path.** No data
was permanently lost.

---

## D. MCP — clean-restart test

The MCP server is a stdio JSON-RPC server (`sqlcg mcp start`, no daemon flag).
It only stays up while it has an open stdin; backgrounded with no stdin it sees
EOF and exits immediately (expected). I kept it alive via a fifo for testing.

| Step | Result |
|---|---|
| `mcp setup` | PASS — prints valid `uvx sql-code-graph mcp start` config JSON |
| `mcp best-practices` | PASS — renders the full fact/heuristic boundary + tool table |
| `mcp start` (with stdin) | PASS — server up, `status` reports pid/version/indexed_sha |
| `mcp status` (before/after) | PASS — `{"running": false}` before; rich JSON when up |
| lineage query via JSON-RPC | PASS — `initialize` handshake OK; `trace_column_lineage` returned full lineage with file/line/confidence/expression provenance (arg name is `table_col`, not `column`) |
| `mcp restart` | see verdict below |
| `mcp stop` | PASS — `Server stopped.`, status returns to false, process gone |

### MCP-RESTART VERDICT — **FAIL as named (stop-only)**
`sqlcg mcp restart` does NOT restart the server. Observed:
```
$ sqlcg mcp status   → running: true, pid: 1458872
$ sqlcg mcp restart
  Server stopped.
  Server stopped. In Claude Code run /mcp → reconnect sql-code-graph (or restart
  the session) to pick up the new build.
$ sqlcg mcp status   → {"running": false}   (old pid 1458872 dead, no new server)
```
The command STOPS the server and instructs the human/client to reconnect. Its
own `--help` says "Stop the server. Use only when the process is wedged." This
is defensible for a stdio MCP server (the MCP CLIENT owns the process lifecycle
and respawns on reconnect), but the command NAME "restart" and the
live-verify expectation ("comes back and still answers") are NOT met by the CLI
alone. I separately proved the stopped server IS cleanly recoverable: manually
re-running `mcp start` brought it back (new pid 1459707) and it answered
`trace_column_lineage` (13 lineage rows). So: recovery works; auto-restart does
not. Recommend either renaming the command to `stop`/`reload`, or having it
actually re-exec the server.

---

## F. PyPI publish pipeline — ROOT CAUSE

Premise correction: in this repo the highest tag (local AND on origin) is
**v1.14.2**, not v1.25.0. (v1.15.0–v1.25.0 are not tagged yet — tagging is the
user's manual post-merge step and is deferred per project workflow.) The
unpublished window is therefore **v1.6.0 → v1.14.2**; PyPI's latest is **v1.5.1**.

`.github/workflows/release.yml`: `on: push: tags: ["v*"]` (+ `workflow_dispatch`).
Jobs: `test` → `publish-pypi` (`needs: test`, `environment: pypi`, trusted
publishing via `pypa/gh-action-pypi-publish`). The `pypi` environment has NO
blocking approval gate — when reached it publishes (v1.5.1's run shows
`publish-pypi` ran straight through with no wait).

Run history (oldest→newest) — note the gap and the event types:
```
... v1.4.3 push success | v1.5.0 push success | v1.5.1 push success (2026-06-08)
[ NO push runs for v1.6.0 .. v1.14.1 — none exist ]
v1.14.2 workflow_dispatch failure (2026-06-11)
```
v1.5.1 was the last tag whose `push` triggered a run, and it published — hence
PyPI = 1.5.1.

### Two compounding root causes
1. **Batch-tag-push dropped the trigger (primary).** Tags v1.6.0–v1.14.2 are on
   origin but produced ZERO `push`-event workflow runs. This is GitHub Actions'
   documented behavior: when more than 3 tags are pushed in a single push
   (`git push --tags` / batch), Actions creates runs for at most ~3 of them and
   silently skips the rest. The MEMORY note "tags backlog cleared 2026-06-11
   (v1.6.0–v1.14.2 pushed)" confirms these ~10 tags were pushed as one batch —
   so no release runs fired for them, and nothing was built/published.
2. **The one manually-dispatched run failed its gate.** v1.14.2 was kicked off
   via `workflow_dispatch`, but the `test` job FAILED (`uv run pytest
   tests/unit tests/integration`), and `publish-pypi` (`needs: test`) was
   therefore `skipped`. So even the one tag that did run never reached publish.

### Recommended fix (seeds a separate thread — not done here)
- Re-publish by triggering a release run per missing tag via `workflow_dispatch`
  with an input ref, OR push the missing tags ONE AT A TIME (≤3 per push) so the
  `push` trigger fires for each.
- First fix the v1.14.2 `test`-job failure (publish is correctly gated behind
  green tests — that gate is working as designed; the tests are red).
- Consider decoupling build/publish from the full test gate for tag pushes, or
  add a `workflow_dispatch` input to re-publish an arbitrary tag.

---

## E. Lifecycle edges

- `db info` — PASS (schema v8, full coverage block, blindspot ranking).
- `db list-repos` — PASS (single repo `dwh` at the DWH path; sane).
- `reindex --from/--to` — PASS. Used repeatedly to move the graph between base
  and HEAD by SHA-diff; correctly landed `indexed_sha` at the requested ref each
  time (e.g. `indexed at fdf1b551 (1 commit(s) behind HEAD)`). Incremental
  resync is correct.
- `git install-hooks --help` — PASS (documents post-checkout + post-merge,
  idempotent, embeds absolute binary path). NOTE: the installed hooks point at
  `/home/ignwrad/Projects/sql-code-graph/.venv/bin/sqlcg`, not the
  user-installed `~/.local/bin/sqlcg` — both are 1.25.0 here, but that is a
  version-skew surface worth watching.

---

## Captured outputs for the guide (clean, paste-ready)

**Q1 — Where is this column defined?**
```
$ sqlcg find table ba.wtfe_verkoopinfo
ba.wtfe_verkoopinfo                  table
ba.wtfe_verkoopinfo_backup_us26608   table

$ sqlcg find column ba.wtfe_verkoopinfo.da_transactie_id
ba.wtfe_verkoopinfo.da_transactie_id
```

**Q2 — What feeds this table?**
```
$ sqlcg analyze upstream ba.wtfe_verkoopinfo.da_transactie_id
da.ttint_verkooptransactie.dekasnr
da.ttint_verkooptransactie.detranr
da.ttint_verkooptransactie.da_filiaalnr
```

**Q3 — What's the impact if I change it?**
```
$ sqlcg analyze downstream ba.wtfe_verkoopinfo.da_transactie_id
ia_tableau.transactie obt.transactie id
ba.wtfe_kpi_supply_chain_artikel_voorraadlocatie...
ba.wtfe_bijverkoop_matrix.da_transactie_id
ba.wtfe_kpi_voorraadhoudende_artikelen...

$ sqlcg analyze impact ba.wtfe_verkoopinfo
# long list of CREATE_TABLE / CREATE_VIEW / MERGE consumers with file:line
```

**Q4 — What breaks if this wasn't filled last night?**
```
$ sqlcg analyze empty-impact ba.wtfe_verkoopinfo
View 2 — Value derivation (PRIMARY)
  ia_businessobjects.ba_wtfe_verkoopinfo   84   full
  ia_analytics.ba_wtfe_verkoopinfo         84   full
  ia_tableau.ba_wtfe_verkoopinfo           84   full
  ... (+ partial ia_semantic / ia_tableau derivations)
```
