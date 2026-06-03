# Findings — v1.1.2 evaluation against the DWH corpus (2026-06-01)

External evaluation of `sql-code-graph` **1.1.2** (installed from source via
`uv tool install --force /home/ignwrad/Projects/sql-code-graph`) run against a
real Snowflake + Liquibase + Pentaho data-warehouse repo (1340 SQL files). The
focus was re-verifying the open GitHub tickets after the
`fix(#38,#39,#40) CTE recall` and `fix(#28) read-only connection` commits.

## Setup / corpus

```bash
uv tool install --force /home/ignwrad/Projects/sql-code-graph   # → sqlcg 1.1.2
sqlcg db reset && sqlcg db init                                  # schema v6
sqlcg index <repo> --dialect snowflake                          # ~118s
```

Indexed graph state (`sqlcg db info` + direct kuzu read-only queries):

| Metric | Value |
|---|---|
| Files | 1340 |
| SqlTable | 2843 (kind: `table` 2522 / `cte` 321) |
| SqlColumn | 53380 |
| SqlQuery | 5630 (all have `start_line`) |
| COLUMN_LINEAGE edges | 50436 |
| STAR_SOURCE edges | 462 |
| STAR_EXPANSION lineage edges | 12975 |
| Confidence dist. | 1.0 = 36802 · 0.8 = 12975 · 0.0 = 659 |
| Freshness | `indexed at 321bfb6a (up to date, working tree dirty)` |

Index summary line: `1340 files — 2009 tables, 44584 edges · 950 with column
lineage · 324 table-only · 65 DDL-only · 4 timed out · 1 failed`. (The 4
timeouts at the 10s/file limit: `wtdh_artikel.sql`, `wtfa_loonkosten.sql`,
`MSSPR_IA_OUTBOUND_DOORBELASTING.sql`, `wtfa_kpi_datum_klant_us49133.sql`.)

---

## ✅ Confirmed fixed / implemented

| # | Feature | Evidence |
|---|---|---|
| **#30** | Freshness signal | `db info` → `indexed at <sha> (up to date, working tree dirty)`; the dirty sentinel correctly reflects 3 uncommitted files. |
| **#31** | Source `file:line` | `start_line` present on **all** 5630 `SqlQuery` nodes; `file:line` column rendered in CLI `analyze` output. |
| **#32** | Meaningful confidence | `COLUMN_LINEAGE` edges carry `confidence` (+`transform`,`query_id`): 1.0 for plainly-parsed facts (36802), 0.8 for star-expansion-derived (12975), 0.0 for inferred (659). |
| **#33** | Node-kind tagging + CLI/MCP parity | `SqlTable.kind` ∈ {`table`(2522), `cte`(321)}; `analyze upstream` supports `--raw` (disable noise filter) and `--include-intermediate` (show CTE/derived). |
| **#24** | relative-path crash | (already fixed 1.1.0/1.1.1) walker root resolved to absolute. |

Sanity — simple INSERT…SELECT traces to DA raw in one hop:

```
$ sqlcg analyze upstream ba.wtfe_bm_omzet.ma_omzet_prognose --depth 8
→ da.rtwfm_turnover.prognosis     (etl/sql/fact/wtfe_bm_omzet.sql)
```

(The bare-name fallback hint still fires here because that file's INSERT target
is written without a schema prefix — separate, known, low-impact.)

---

## ⚠️ #38 — CTE recall: general fix landed, one residual island

**Fixed:** explicit-column CTEs now trace through to source. Verified the
upstream half of the voorraad chain is healthy:

```
cte_voorraad_bouwmarkt.ma_vrije_vrd  →  ba.wtfs_voorraad_dagstand.ma_vrije_vrd   ✓
```

…and the downstream half is healthy:

```
ba.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd  ←  cte_insert.ma_vrije_vrd   ✓
```

**Residual bug:** the link *between* those two halves is missing. The
`cte_insert` CTE is a `SELECT * … UNION ALL` over sibling CTEs, and the star is
**not** expanded across the union — so `cte_insert.ma_vrije_vrd` has **0**
upstream `COLUMN_LINEAGE` edges and **0** `STAR_SOURCE` edges, even though 462
STAR_SOURCE / 12975 STAR_EXPANSION edges exist elsewhere (so star handling
works generally — this specific shape is the gap).

Offending pattern (`etl/sql/fact/wtfe_kpi_voorraad_artikel_voorraadlocatie.sql`, ~L224-269):

```sql
WITH cte_voorraad_bouwmarkt AS (SELECT dn_datum, ..., SUM(vrd.ma_vrije_vrd) AS ma_vrije_vrd ... ),
     cte_voorraad_webshop   AS ( ... ),
     cte_voorraad_igdc      AS ( ... ),
     cte_insert AS (
         SELECT * FROM cte_voorraad_bouwmarkt
         UNION ALL
         SELECT * FROM cte_voorraad_webshop
         UNION ALL
         SELECT * FROM cte_voorraad_igdc
     )
SELECT dn_datum, ..., ma_vrije_vrd, ...
FROM cte_insert;
```

The star-expansion resolver needs to expand `SELECT *` against the **CTE schema**
of each `UNION ALL` branch (the branch CTEs have explicit, identical column
lists), then union the per-branch column lineage onto the `cte_insert` columns.

**Corpus-wide impact (kuzu queries over the indexed graph):**

| Measure | Count |
|---|---|
| `cte_insert.*` columns total | 134 |
| …of which are islands (0 upstream) | **80** |
| BA fact `ma_*` columns (excl. `_bck`) | 1395 |
| …reach ≥1 upstream node | 824 (59%) |
| …reach `da.`/`ean.` raw source | 138 (9%) |
| …whose chain routes through a `cte_insert` node | 222 (15%) |

The 9%-to-raw figure is partly legitimate (many BA facts terminate at a BA
snapshot, which is the intended source boundary), but the 80 `cte_insert`
islands sever ~15% of fact-measure chains at the union-of-CTEs boundary. This is
the same `ma_vrije_vrd` chain that reached ~30 DA sources on 1.0.2 — a partial
regression that survives in 1.1.2.

**Reproduction:**

```bash
sqlcg analyze upstream ba.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd \
    --raw --include-intermediate --depth 12
# → only row: cte_insert.ma_vrije_vrd  (dead-ends, no further upstream)
```

```python
# kuzu read-only:
# cte_insert.ma_vrije_vrd has no COLUMN_LINEAGE and no STAR_SOURCE upstream
MATCH (c:SqlColumn {id:'cte_insert.ma_vrije_vrd'})<-[:COLUMN_LINEAGE]-(s) RETURN s.id   # → 0 rows
MATCH (c:SqlColumn {id:'cte_insert.ma_vrije_vrd'})-[:STAR_SOURCE]->(s) RETURN s.id      # → 0 rows
# but the branch CTE traces fine:
MATCH (c:SqlColumn {id:'cte_voorraad_bouwmarkt.ma_vrije_vrd'})<-[:COLUMN_LINEAGE*1..6]-(s) RETURN DISTINCT s.id
# → ba.wtfs_voorraad_dagstand.ma_vrije_vrd
```

---

## ⚠️ #28 — DB lock: hook/reindex path fixed; server still takes a write lock

**Fixed (hook + CLI-read side):**

1. All five `analyze` read commands now open `get_backend(read_only=True)`
   (`src/sqlcg/cli/commands/analyze.py:67,126,194,230,247`). Read-only opens get
   reader/reader concurrency.
2. Git hooks route `reindex --notify` through the server's Unix control socket
   when a server is live (`src/sqlcg/cli/commands/reindex.py:84-152`,
   `src/sqlcg/server/control.py`), so a running server reindexes itself with no
   cross-process lock fight. On failure the hook now prints
   `sqlcg: graph not updated (server busy/locked) -- run 'sqlcg mcp status'` to
   stderr (`src/sqlcg/cli/commands/git.py:46-56`) instead of the old silent
   `|| true` that left a stale graph.
3. The lock semantics are now accurately documented in the `KuzuBackend`
   docstring: *"Does NOT allow reads while a read-write writer holds the lock —
   KùzuDB's exclusive lock is process-level."*

**Still present (server side):** `init_backend()` in
`src/sqlcg/server/tools.py:104-122` constructs `KuzuBackend(path)` with the
default `read_only=False` and calls `init_schema()`, so the MCP server holds
kuzu's **process-level exclusive write lock for its entire lifetime**.
Consequence: while the editor's MCP server is attached, **direct CLI read
commands still fail** with `RuntimeError: Database is locked — another sqlcg
process is running (PID …)`.

**Empirical confirmation** (holder process + concurrent `sqlcg analyze upstream`):

| Holder opened the DB as | Concurrent CLI read result |
|---|---|
| read-**write** (`read_only=False`, == what the server does) | ❌ `Database is locked` |
| read-**only** (`read_only=True`) | ✅ returns results |

So the original #28 symptom (CLI/queries blocked while the MCP server runs)
persists by design. The two practical mitigations are unchanged:
- run CLI against a side DB: `SQLCG_DB_PATH=/tmp/sqlcg-cli/graph.db sqlcg …`, or
- use the MCP tools (which go through the server) instead of the CLI while the
  server is up.

**Possible improvement:** have the server open read-only by default and only
escalate to a read-write connection for the duration of a reindex (reopen
around the `--notify` socket request), so idle serving doesn't lock out CLI
readers. If escalation is infeasible (kuzu can't upgrade an open connection),
at minimum the "Database is locked" error from a CLI *read* could suggest the
`SQLCG_DB_PATH` side-DB workaround in its message.

---

## Suggested ticket dispositions

- **#30, #31, #32, #33** — verified implemented; OK to close.
- **#38** — keep open; narrow the scope to *"`SELECT *` not expanded across a
  `UNION ALL` of sibling CTEs"* (the general CTE-recall regression is fixed).
- **#28** — keep open for the server-side write-lock; the hook/reindex/CLI-read
  half is genuinely resolved and could be split out and closed separately.

## Repro environment note

Stopping the daemon (`sqlcg mcp stop`) to release the lock disconnects the
editor's live MCP tools until the client respawns the server. A 1.1.0 daemon
left running held a v4-schema DB while the new build wanted v6, which surfaced
as a `db reset` prompt — worth a clearer "server still running on old schema,
run `sqlcg mcp restart`" hint on schema-version mismatch.

---

# Follow-up — v1.2.2 re-evaluation against the DWH corpus (2026-06-03)

Re-ran the open-ticket validation on a freshly built **1.2.2** wheel
(`uv build` + `uv tool install --force dist/sql_code_graph-1.2.2-py3-none-any.whl`),
re-indexed the same DWH repo: 1335 files — 1919 tables, 40,926 edges · 838 with
column lineage · 1 timed out (`wtdh_artikel.sql`, >10s) · 128 failed.

## Status per ticket

| # | Status on 1.2.2 | Evidence |
|---|---|---|
| **#39** | ✅ Fixed | `da.ttint_inventdim_formule` (referenced only inside CTE bodies) now exists as `kind=table`; its bare alias is tagged `kind=derived`. |
| **#45** | 🟡 Mostly fixed | `cte_insert` no longer leaks into filtered output; `file:line` populated on 8/9 upstream nodes — but `da.ttint_inventdim_formule.formule_specifiek` still renders `?`. Likely shares a root with #44 (node hanging off the CTE-body source path). |
| **#27a/e** | ✅ Fixed | `find table "*bck*"` → no results (backup filter works); apparent duplicate rows were actually distinct columns (`sa_formule_gb`/`_kh`/`_gn`) at truncated width. |
| **#44** | ❌ Still reproduces exactly | `find table wtfe_kpi_voorraad_artikel_voorraadlocatie` → 3 disconnected identities (`ba.`, `ia_analytics.ba_`, schema-less). `analyze upstream "ba.wtfe_kpi_voorraad_artikel_voorraadlocatie.ma_vrije_vrd"` → **No results**; only the schema-less spelling returns lineage. |
| **#28/#29** | ❌ Lock contention unchanged | `sqlcg index .` with an attached MCP server → `RuntimeError: Database is locked — another sqlcg process is running (PID …)`. The PID hint is there, but the only way through was killing the server, which disconnected the editor session's sqlcg tools for the rest of the session. The git-hook silent-staleness scenario from #28 therefore still applies on 1.2.2. |

## Verdict — release gate for 1.3

**These are all bugs, not enhancements, and should be addressed before 1.3:**

1. **#44** — the canonical/DDL name silently returning "No results" is the
   single worst trust-breaker: the obvious query spelling fails while a
   non-obvious one succeeds, and the split identity pollutes `find`,
   hub-ranking, and `analyze_unused`. It also blunts the shipped #38/#39 fixes.
2. **#45 residual** — the remaining `?` `file:line` on CTE-body source columns;
   probably falls out of the #44 fix (same node identity problem).
3. **#28/#29 server write-lock** — `index` still cannot run past an attached
   MCP server; the auto-reindex value prop stays broken in exactly the
   situation it's meant for (active editor session). At minimum the
   reindex-via-running-server path (#29) should land before 1.3.

#39, #45 (main), #27a/e are confirmed good on 1.2.2 and can be closed.

## Note on #28/#29 — this is lock discipline, not a kuzu limitation

Kuzu's constraint is narrower than "no concurrent operations": a read-**write**
open takes an exclusive process-level lock (blocks everything, even readers),
but multiple concurrent read-**only** processes are fine. The symptom we keep
hitting is therefore ~entirely self-inflicted: the MCP server
(`init_backend()`, `server/tools.py`) opens `read_only=False` and holds the
exclusive write lock for its **entire lifetime**, even though it serves reads
99% of the time.

Fix shape (no storage-engine change needed):

1. **Server opens read-only by default**, reopening read-write only for the
   duration of an actual reindex. The 1.1.2 evaluation above already verified
   empirically that a read-only holder + concurrent CLI read works ✅.
2. **All writes funnel through the server's control socket** (#29's
   `reindex --notify` design — partially exists for git hooks). The remaining
   gap: a *manual* `sqlcg index .` doesn't use that path; it grabs the lock
   itself and dies. It should detect a live server and route through it.
3. CLI reads already open `read_only=True` since 1.1.2 — that half is done.

Open question (unchanged from 1.1.2): whether kuzu can upgrade an open
connection in place; worst case the server briefly closes/reopens the DB
around a reindex.

## Proposed #29 design — single-writer queue with coalescing + observability

All kuzu write operations (`index`, `reindex`, git-hook reindexes, `db reset`)
become **requests to a single serialized writer** — never direct lock grabs.
When a server is live, the server *is* the writer (via the existing control
socket); with no server, the CLI degenerates to taking the lock itself — same
code path, queue of one.

### Queue semantics (coalescing rules)

The operations are idempotent toward one goal — "graph matches HEAD/worktree" —
so the queue coalesces:

1. **Full `index` supersedes everything** — queued reindexes (hook or manual)
   are dropped; the full rebuild subsumes them.
2. **Reindexes coalesce** — N queued reindex requests collapse into one,
   executed against the HEAD *at drain time* (incremental reindex means
   "catch up to now"; intermediate targets are meaningless).
3. A reindex arriving behind a queued full index is a no-op enqueue.
4. **Write lock held only while draining** — the server sits read-only between
   drains, so CLI readers are never blocked by an idle server.

### Wait semantics — never block a branch switch

This is the sharpest current pain: the post-checkout hook path can sit in the
way of `git checkout`, which is the worst possible place to put indexing
latency when iterating on a feature branch.

- **Hook reindex: fire-and-forget.** Enqueue + return immediately, always.
  A `git checkout`/`git pull` must never wait on the graph.
- **Manual `index`/`reindex`: attach-and-wait** by default (with `--detach`
  opt-out) — the user asked for it, so show them progress until it's done.

### Observability — make the queue visible

Coalescing must not look like silently dropped work:

1. **`sqlcg mcp status`** extends with queue state:
   ```json
   "writer_queue": {
     "active":  {"op": "reindex", "files_total": 118, "files_done": 23, "started_at": "..."},
     "pending": [{"op": "index", "requested_by": "cli", "queued_at": "..."}],
     "coalesced_since_start": 6
   }
   ```
2. **CLI attach experience** — a queued `sqlcg index .` prints
   `queued behind: reindex (23/118 files) — position 1`, then attaches to the
   live progress stream over the control socket once its turn comes. Same
   progress-bar UX whether direct writer or queued (extends the existing
   per-file progress bar, which already covers the active drain).
3. **Server log lines** for lifecycle events: enqueued / coalesced (with
   reason, e.g. "superseded by full index") / started / drained. This is the
   audit trail for the fire-and-forget hook case, where no terminal is
   attached — replacing the single stderr warning line as the only signal.

---

## Risk analysis — the RO→RW escalation (measured on Kuzu 0.11.3, 2026-06-03)

The 1.1.2 eval claimed escalation was "already verified empirically (RO holder +
concurrent CLI read works ✅)." **That verification was incomplete and the
conclusion is wrong as stated.** It tested the static coexistence of two
read-only opens, not the escalation *transition* the design performs. Probing
all four open combinations in two processes against `kuzu==0.11.3`:

| Holder | Second opener | Result |
|---|---|---|
| RW | RO | ❌ `Could not set lock on file` |
| RW | RW | ❌ `Could not set lock on file` |
| **RO** | **RW** | ❌ **`Could not set lock on file`** |
| RO | RO | ✅ both open |

A read-only open takes a **shared** lock (multiple coexist — last row); a
read-write open needs an **exclusive** lock (blocks everything, including
shared readers — third row). The eval only ever observed rows 1 and 4.

### Finding 1 — "upgrade an open connection in place" is impossible, and is not the real risk
`read_only` is a **`Database`-level** flag fixed at construction
([`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) — `kuzu.database.Database(db_path, read_only=...)`);
there is no connection-level toggle. Escalation **must** be close-the-`Database`
+ reopen-read-write. That part is cheap (catalog reload; Kuzu warms the buffer
pool lazily), so reopen latency is a non-issue. The lock behaviour during the
reopen is the issue.

### Finding 2 — the escalation transition fails fast and hard when any reader is attached
Running the exact #29 sequence (server opens RO → a reader attaches → server
closes RO, reopens RW):

```
('server', 'OPENED-RO')
('reader', 'OPENED-RO')
('server', 'closed-RO, attempting RW reopen')
('server', 'RW-REOPEN-FAILED after 0.004s', 'Could not set lock on file ...')
```

The RW reopen **fails in ~4 ms with no block and no retry** — Kuzu does not
queue on the lock, it errors immediately. So escalation succeeds only when *no
other process holds the file at that instant*.

### Finding 3 — the architecture is what makes the design viable
`run_read_routed` ([`read_client.py`](../src/sqlcg/server/read_client.py)) means
that **while a server is live, CLI reads go over the control socket and share
the server's connection — they take no separate lock.** A CLI reader opens its
own RO `Database` only in the *no-server fallback* path, and on a socket timeout
it explicitly refuses to fall back to a direct open. So in normal operation
there are no competing direct openers and the close→reopen escalation has the
file to itself and succeeds. Reads also stay available *during* a drain: a RW
connection serves reads too, so socketed reads keep working through the reindex
— only the sub-millisecond close→reopen flip is a gap, not a read outage.

### The risk, restated for the planner
The risk is **not** "can Kuzu upgrade" (no — use close/reopen, it's cheap). It
is that **escalation is correct only under the invariant "nothing opens the DB
file directly while the server is live," and that invariant fails hard and fast
(4 ms, no retry) the instant it is violated.** Today the invariant is broken by
exactly the path #29 targets: manual `sqlcg index .` / `reindex` open the DB
read-write directly ([`index.py`](../src/sqlcg/cli/commands/index.py) — `get_backend()`
with no `read_only` arg) instead of routing through the socket.

**Design constraints this imposes (all three are required):**

1. **Route every writer through the socket** when a server is live — close the
   manual-`index`/`reindex` direct-open gap. This is the core of #29 and is what
   makes escalation safe by eliminating in-process competitors.
2. **Bounded retry/backoff on the RW reopen.** A transient out-of-band reader (a
   stale process, a third-party tool, an escape-hatch `SQLCG_DB_PATH` pointing at
   the *same* db) fails the reopen instantly with no wait. Without retry, one
   stray reader aborts an entire reindex. The retry window also covers the race
   where a reader's shared-lock acquisition interleaves the server's close→reopen.
3. **Clear error + `SQLCG_DB_PATH` side-DB workaround surfaced** when escalation
   genuinely cannot get the lock after the retry budget — not the raw
   `Could not set lock on file`.

### Doc-correctness fix to fold in
The [`KuzuBackend`](../src/sqlcg/core/kuzu_backend.py) docstring says RO opens
work "by not taking the exclusive write lock," which reads as *no* lock. Row 3
of the matrix proves RO takes a **shared** lock that still blocks RW. Correct
the docstring so the next person planning escalation does not repeat this eval's
gap.
