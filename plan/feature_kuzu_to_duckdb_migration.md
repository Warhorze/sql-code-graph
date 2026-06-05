# Feature Plan: Migrate graph store KuzuDB → DuckDB

> **Status:** **Ready for developer** (v4 — FINALIZED, 2026-06-05; plan-reviewer fixes applied). Branch
> `feat/duckdb-migration` off `master` (v1.3.0). Phase 0 feasibility gate (C1–C8)
> **PASSED** on the real DWH corpus — treat as a cleared gate, do not redo.
> The rebuild-and-swap approach (symlink `.A`/`.B`, `recover_on_open`) is replaced:
> DuckDB transactions provide the atomic visibility the swap was hand-rolling, so that
> machinery is deleted rather than finished. (There is no standalone
> `plan/feature_rebuild_swap_reindex.md` on disk — the swap work lived inline in
> `writer.py`/`tools.py`; nothing to supersede as a file.)
>
> **v3 changes (architect-planner, 2026-06-05):** re-validated every named call site
> against live code at the cited (or adjusted) lines; corrected the Cypher surface count
> to live grep; reclassified `tools.py:1660` / `db.py:125` as **exception handlers** tied
> to the Neo4j `get_indexed_sha` stub (not deletable "branches" — they go dead-but-harmless
> when Neo4j is removed); promoted `skill.py:85,112` and `uninstall.py:99,231` into the
> rename/removal inventory; pinned the **full** node + edge label set from `schema.py`;
> pinned `_pk_field` to `graph_db.py:193` (ABC static method — inherited free); added a
> **newly-found, previously-missing Phase 5 item**: `uninstall.py` deletes the DB with
> `shutil.rmtree` because Kuzu is a *directory* — DuckDB is a *single file*, so this must
> become `Path.unlink` (+ `.wal`). See **§ Drift found** at the end.
>
> **v4 changes (architect-planner, 2026-06-05, plan-reviewer round):** corrected the **wrong**
> D7 "there is no `watcher.py`" claim — `indexer/watcher.py` **does** exist (245 LOC:
> `SqlFileEventHandler`, `BranchMonitor`), imported by `watch.py:13`; the real path is
> `watch.py → watcher.py (SqlFileEventHandler) → jobs.py (WatchJobManager._run_job) →
> indexer.reindex_file`. Added the **double-reindex no-crash + lineage-parity** test (user's
> top acceptance criterion) and a real-corpus perf check to the Phase 5 exit gate. Added the
> two `--buffer-pool-size` CLI flags (`db.py`, `index.py`) to the removal inventory. Added a
> **Kuzu reference-fixture extraction** step at the START of Phase 3 (no parity fixture exists
> yet) and made Phase 5 Kuzu deletion blocked on it.

## Summary

Replace the KuzuDB backend with a DuckDB backend behind the existing
[`GraphBackend`](../src/sqlcg/core/graph_db.py) ABC. Lineage queries become recursive
SQL CTEs over relational node/edge tables (no graph engine, no DuckPGQ). Atomic
visibility comes from `COMMIT`; concurrent reads come from DuckDB's same-process MVCC.
The migration is **net code-negative**: the swap/symlink/recovery subsystem and the
exclusive-lock workarounds go away.

## Motivation (measured, not assumed)

Spike on the real DWH corpus (1,340 files, 47,404 `SqlColumn` nodes, 45,415
`COLUMN_LINEAGE` edges), extracted from the live Kuzu DB into DuckDB:

| Metric | Kuzu 0.11.3 | DuckDB (plain SQL) |
|---|---|---|
| Lineage traversal, 200 seeds, depth≤10 | 797 ms (p95 5.76) | **663 ms (p95 6.46)** |
| Reached-row parity | 2,178 | **2,178 — exact** |
| Full-graph bulk load + index | (indexer-built) | **10.6 s** (`unnest` insert) |

Engine drivers: Kuzu upstream archived Oct 2025; the Vela fork segfaults at DWH scale.
Both Kuzu paths are dead ends at our scale. The recursive-CTE result proves our core
query (bounded reachability) needs neither a graph engine nor an extension.

## Design

### Concurrency model (the linchpin)

DuckDB is single-process / multi-threaded / MVCC:
- **Many concurrent readers** (separate connections via `con.cursor()`) — always allowed.
- **Readers + one writer, same process** — MVCC: reads see a consistent snapshot and are
  never blocked by an in-flight write; on `COMMIT` new readers see the new graph.
- **Cross-process** — one R/W process takes an **exclusive file lock**; other processes
  cannot open the file (even read-only) while it is held. Multiple read-only processes are
  fine.

**Architectural rule:** the process that writes must also serve reads. Consolidate all
writes into the read-serving (server) process and route CLI/watcher reindex requests
through the existing writer-queue ([`server/writer.py`](../src/sqlcg/server/writer.py),
[`core/jobs.py`](../src/sqlcg/core/jobs.py)). This queue already exists for Kuzu's
exclusive lock — its body changes, not its shape.

### `COMMIT` replaces the swap

Rebuild-and-swap existed to give atomic visibility (symlink always resolves to a complete
DB). DuckDB gives this natively: readers on MVCC snapshots see the old graph until the
write transaction commits, then atomically the new one. **No symlink, no `.A`/`.B`, no
`recover_on_open`, no ownership marker, no concurrent-build corruption window.** Crash
mid-write → transaction rolls back via DuckDB's WAL on next open (not hand-rolled).

### Schema (relational)

One table per node label, one table per edge type — mirror
[`core/schema.py`](../src/sqlcg/core/schema.py) exactly (grep-pinned 2026-06-05):

- **Node labels** (`NodeLabel`, schema.py:9): `Repo`, `File`, `SqlTable`, `SqlColumn`,
  `SqlQuery`, `SchemaVersion`, `ExternalConsumer` — **7 node tables.**
- **Edge types** (`EdgeType`, schema.py:19): `BELONGS_TO`, `DEFINED_IN`,
  `QUERY_DEFINED_IN`, `HAS_COLUMN`, `SELECTS_FROM`, `INSERTS_INTO`, `DELETES_FROM`,
  `UPDATES`, `COLUMN_LINEAGE`, `DECLARES`, `STAR_SOURCE`, `CONSUMED_BY` — **12 edge
  tables.** (`STAR_SOURCE` and the `kind IS NULL` sink filters in `analyze.py` are the
  #38/#40/19.2-sensitive ones — port their guard semantics exactly, see Phase 3.)

Each node table keys on the same PK field the ABC's **`_pk_field` static method
([`graph_db.py:193`](../src/sqlcg/core/graph_db.py)) already returns** — `DuckDBBackend`
inherits it unchanged, so PK naming cannot drift between backends. Edge tables are
`(src_key, dst_key, <props>)`. Index every edge table on `src_key` (and `dst_key` for
upstream traversal). `SchemaVersion.indexed_sha` preserved (it backs `db info` freshness).

### Query translation — live surface (grep-verified 2026-06-05)

The Cypher surface is **~7 production files**. Counts below are a live grep
(2026-06-05) of Cypher/keyword + routing lines per file — they are an *order-of-magnitude
porting guide*, not exact statement counts (some lines are routing calls, comments, or
metric strings; see per-row notes). Port in this order (heaviest / most product-critical
first):

| File | Keyword/route lines | Read path | Notes |
|---|---|---|---|
| [`server/tools.py`](../src/sqlcg/server/tools.py) | ~37 | `_get_backend()` | MCP tools; `execute_cypher` def at **1677–1678** → rename to `execute_sql`; the `except NotImplementedError` at **~1660** is the Neo4j `get_indexed_sha`-stub **handler** — it becomes dead-but-harmless when Neo4j is removed (leave or drop; not a "branch") |
| [`cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py) | ~36 (9 `run_read_routed` sites) | `run_read_routed` | heaviest *parity risk* — OPTIONAL MATCH + `WITH … WHERE` lineage walks, the `kind IS NULL` star/sink filters from #38/#40/19.2. **Port these guard semantics exactly.** |
| [`indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) | ~24 | bulk sink + `run_write` | writes only — covered by the bulk-sink port (Phase 2), not the read port |
| [`cli/commands/db.py`](../src/sqlcg/cli/commands/db.py) | ~20 | `run_read_routed` / `get_backend()` | counts, `db init`, `db reset --repo` (`run_write` at db.py:74); the `except NotImplementedError` at **124–125** is the same Neo4j-stub handler (not a deletable branch) |
| [`cli/commands/gain.py`](../src/sqlcg/cli/commands/gain.py) | ~17 | metrics DB (SQLite) | the `execute_cypher` refs (lines 33,115–201) are **metrics tool-name strings + a `tool_name='execute_cypher'` SQLite filter, NOT graph Cypher.** **Decision (this plan): rename to `execute_sql` consistently** — the SQLite filter string, the `execute_cypher_ratio`/`execute_cypher_count` vars, and the user-facing label — so the metric keeps tracking the renamed tool. See Tier 2. |
| [`cli/commands/find.py`](../src/sqlcg/cli/commands/find.py) | ~7 (7 `run_read_routed` sites) | `run_read_routed` | node lookups |
| [`server/server.py`](../src/sqlcg/server/server.py) | ~4 | startup count | liveness/schema-version probe |

Also in the rename set (not Cypher, but tool-name strings):
[`server/skill.py:85,112`](../src/sqlcg/server/skill.py) — the skill manifest's
`"execute_cypher"` key + description must become `"execute_sql"`.

`cli/commands/index.py` has 1 incidental keyword (a write probe). The ABC contract to
port is **both** `run_read` ([`graph_db.py:117`](../src/sqlcg/core/graph_db.py)) **and
`run_write`** ([`graph_db.py:129`](../src/sqlcg/core/graph_db.py)) — the draft's earlier
"`run_read` only" was incomplete. (Note: the `tools.py`/`db.py` `except NotImplementedError`
handlers exist because the Neo4j backend stubbed `get_indexed_sha`; once Neo4j is deleted
the path can no longer raise it, so the handler is dead — keep it as a cheap guard or
delete it, developer's call. It is **not** a Cypher site and needs no SQL port.)

| Cypher pattern | DuckDB SQL |
|---|---|
| `MATCH (n:Label) RETURN COUNT(*)` | `SELECT count(*) FROM label` |
| `MATCH (a)-[:E*1..d]->(b) RETURN DISTINCT b` | `WITH RECURSIVE` over edge table, depth guard `WHERE depth < d` + cycle guard |
| OPTIONAL MATCH + `WITH … WHERE t.kind IS NULL` (analyze.py star/sink filters) | `LEFT JOIN` + `WHERE t.kind IS NULL OR …` — **port these guard semantics exactly; #38/#40/19.2 live here** |
| degree / hub ranking | `GROUP BY ... COUNT(DISTINCT ...) ORDER BY ... LIMIT k` |
| property lookup | `SELECT ... WHERE pk = ?` |
| `execute_cypher` (raw passthrough) | rename → `execute_sql`, raw read-only SQL (keep write guard) |

The recursive CTE must carry a **cycle guard** (track visited / cap depth) and bounded
depth — verified against Kuzu output for row parity (the spike proved this for
`COLUMN_LINEAGE` and, in C7/C8, for hub ranking and cyclic graphs).

### The reindex path

**Option 1 — full rebuild in a transaction (ship first).** Keep today's "rebuild
everything" semantics but swap-free:
```
with write_conn:                 # BEGIN ... COMMIT
    clear node/edge tables       # DELETE or DROP/CREATE (DuckDB DDL is transactional)
    for batch in passes:         # existing batch_size loop, Appender/COPY sink
        bulk_insert(batch)
    # COMMIT → readers atomically flip
```
Deletes the whole swap subsystem immediately; reads never block. The two passes
(parse + harvest CTAS bodies; build rows) are backend-agnostic and unchanged — only the
bulk-upsert sink changes (`upsert_*_bulk` → DuckDB `Appender`/`COPY`).

**Option 2 — true incremental (later epic).** `DELETE FROM <t> WHERE owner_file = ?` +
re-insert that file's rows, per change. Brings back the granularity the swap removed.
**Genuine subtlety (data-modeling, not concurrency):** `COLUMN_LINEAGE` edges cross files
(CTAS in file A feeds file B), so a single-file reindex must recompute cross-file edges
without deleting edges anchored on other files — the same problem the old
`reindex_file` + `CrossFileAggregator` had. Defer; Option 1's safe full-rebuild is the
fallback while this is hardened.

## Scope

### In scope
- `core/duckdb_backend.py` implementing `GraphBackend` (schema, bulk upsert, `run_read`
  via SQL, transaction, in-place delete).
- Port query layer (`server/tools.py`, `cli/commands/{analyze,find,db,gain}.py`,
  `indexer/indexer.py`) from Cypher to SQL.
- Consolidate writes into the server process; rewire the `writer.py` drain body and the
  watch-reindex path to transaction-based reindex. The watch path is
  `watch.py → indexer/watcher.py (`SqlFileEventHandler`) → core/jobs.py
  (`WatchJobManager._run_job`) → indexer.reindex_file`. `watcher.py` is backend-agnostic
  (it dispatches file events and calls `db.delete_nodes_for_file` / `indexer.resync_changed`
  through the `GraphBackend` ABC) — **no DuckDB-specific change is needed inside it**; only
  the downstream reindex sink (Phase 2) and the drain body (Phase 4) change.
- Config: `KuzuConfig` → backend-neutral config; default DB path stays a single file.
- Delete `kuzu_backend.py` and all Kuzu deps from `pyproject.toml`; `uv lock`.
- Delete `neo4j_backend.py` + `Neo4jConfig` + `SQLCG_BACKEND` dispatch → single backend
  (`get_backend` returns `DuckDBBackend` directly).
- Rename `execute_cypher` MCP tool → `execute_sql` (read-only SQL passthrough, write guard kept).

### Non-goals (this feature)
- Incremental per-file reindex (Option 2) — separate follow-up.
- Graph-analytics features (centrality/community via igraph) — separate epic.
- DuckPGQ — not used; revisit only if iterative graph-math features are added.

## Phase 0 — Feasibility gate (do this FIRST; throwaway spike, no production code)

Before building any backend, prove every **critical, load-bearing capability** holds water
on the real extracted DWH graph (the spike harness already has it in memory). This is a
single throwaway script with a pass/fail checklist — we do **not** stand up the full
backend, wire the server, or port the full query surface. If any ✗ fails, the migration stops here
and we reassess. If all pass, the unknowns are gone and Phases 1–5 are mechanical.

| # | Critical capability | Why it's load-bearing | Status / test |
|---|---|---|---|
| C1 | Recursive-CTE lineage = Kuzu parity | core product query | ✅ **PROVEN** — 663 ms, exact 2,178-row parity |
| C2 | Bulk load is fast enough | indexing throughput | ✅ **PROVEN** — 10.6 s whole graph (`unnest`); confirm with Appender |
| C3 | **MVCC: concurrent read during a write txn + atomic commit flip** | **this is what kills the swap** — reader sees old snapshot mid-write, new on COMMIT | ⬜ TEST: open writer conn + reader conn; mutate uncommitted; assert reader sees OLD; COMMIT; assert reader sees NEW |
| C4 | **Cross-process exclusive lock** behaves as documented | defines the "writes funnel into one process" rule | ⬜ TEST: open file R/W in proc A, try to open in proc B — assert it's blocked; assert 2 RO procs OK |
| C5 | In-place single-file `DELETE … WHERE owner_file=? ` + re-INSERT, transactional | the incremental-reindex win; the granularity the swap removed | ⬜ TEST: delete+reinsert one file's rows in a txn; assert counts/edges correct, rollback restores |
| C6 | Crash mid-write → rollback leaves old graph intact (WAL) | replaces hand-rolled `recover_on_open` | ⬜ TEST: raise inside a txn; reopen; assert old graph intact, no half-write |
| C7 | Analysis surface ports beyond COLUMN_LINEAGE | proves it's not just one lucky query | ⬜ TEST parity vs Kuzu for: hub ranking (`GROUP BY` degree), backfill reachability closure, a table-level edge query, star-source |
| C8 | Recursive CTE is correct on **cyclic** lineage | cycle guard must not infinite-loop or miss rows | ⬜ TEST: inject a cycle; assert traversal terminates with correct reached-set vs Kuzu |

Deliverable: extend the spike harness into one `/tmp/feasibility.py` that prints the C3–C8
checklist with PASS/FAIL and the parity numbers. **Gate:** all ✅/PASS → proceed to Phase 1.

### Phase 0 RESULT — 2026-06-05: ALL PASS ✅ (gate cleared, proceed to Phase 1)

| Check | Observed |
|---|---|
| C3 MVCC | reader saw OLD (45,415) while writer's uncommitted DELETE was in flight; flipped to NEW on `COMMIT`. Atomic visibility confirmed — **this is the swap, for free.** |
| C4 cross-process lock | 2nd process R/W → `BLOCKED: IOException`. **Sharpening: RO from a 2nd process is ALSO blocked** while one process holds R/W. So when the server is live, **all** access (reads too, not just writes) must funnel through the holding process — which the existing `read_client.py` socket routing already does. |
| C5 in-place delete+reinsert | partition delete + re-INSERT in one txn; counts correct, rest of graph preserved. |
| C6 rollback | raise mid-txn → `ROLLBACK` → graph fully intact (45,415). Replaces `recover_on_open`. |
| C7 hub ranking | `GROUP BY dst COUNT(*)` top-10 **exactly matched** Kuzu's degree ranking. |
| C8 cyclic CTE | cycle `a→b→c→a` (+`c→d`) terminated with depth guard, correct reached-set `{a,b,c,d}`. |

### Backend-open-site audit RESULT — 2026-06-05 (the cross-process constraint is mostly pre-solved)

The routing the C4 constraint demands **already exists on master**, built for Kuzu's lock
in v1.2.0. Classified the 27 sites:

- **CLI reads** (`analyze`, `db`, `find`, `gain`) → **all use `run_read_routed`** →
  socket when a server is live, direct **read-only** open only when none is. ✅ safe.
- **CLI writes** (`index`, `reindex`) → **already route through the server control socket**
  (`_try_route_index_via_server` / `_try_route_reindex_via_server`); they open the DB
  directly only when **no server is live**. ✅ safe.
- **Server process** holds the one backend (`tools._backend`, `init_backend`) and owns the
  **single-writer queue** (`writer.py` — `enqueue` + coalescing + drain). All in one process.
- **"Do NOT fall back to a direct open when the server is alive"** invariant already exists
  (`read_client.py`). Under DuckDB this flips from *preferred* to *mandatory* (a direct open
  while the server holds the file would be blocked, per C4).

**What the migration changes (all in-process, simplifying):**
1. Server holds **one R/W handle for its lifetime** instead of RO+escalate. The whole
   escalate/de-escalate dance (`escalate_to_rw`, `de_escalate_to_ro`, `_get_or_escalate_rw`)
   is **deleted** — DuckDB's single handle reads (MVCC) and writes (txn) without mode-switching.
2. Drain body: `escalate + rebuild-and-swap` → `BEGIN … bulk insert … COMMIT`.

**Residual item to verify (1 spot):** `db init/reset` and `uninstall` open R/W directly
(`get_backend(read_only=False)`). They must route through the server (or require it stopped)
when one is live — the abandoned swap branch already tackled this (`db-reset + uninstall`
routing), so the pattern exists to copy.

**Bottom line:** the single scariest architectural risk (cross-process access) is ~90%
pre-solved by existing routing. The migration mostly *deletes* the escalation machinery.

## Phases 1–5 — full migration (Phase 0 gate is cleared)

Each phase lists its **deliverables**, the **exact files touched**, and an **exit gate**
that must be green before the next phase starts. Every new method must have a
grep-confirmed call site before the PR opens (CLAUDE.md). No `# TODO` in any happy path.

### Phase 1 — Backend skeleton + schema (no production rewire yet)

**Deliverables:**
- `core/duckdb_backend.py`: `DuckDBBackend(GraphBackend)` implementing the ABC
  (`init_schema`, `upsert_node`, `upsert_edge`, `upsert_nodes_bulk`, `upsert_edges_bulk`,
  `run_read`, `run_write`, `transaction`, `get_schema_version`, `get_indexed_sha`,
  `delete_nodes_for_file`, `close`). Inherits `_pk_field` from the ABC (graph_db.py:193).
- `init_schema`: `CREATE TABLE` for the 7 node + 12 edge tables (full list in § Schema),
  `CREATE INDEX` on each edge's `src_key`/`dst_key`. Idempotent (`CREATE … IF NOT EXISTS`),
  wrapped in one transaction (closes ARCHITECTURE_REVIEW §3.1 atomicity for init).
- `transaction()` is a **real** context manager (`BEGIN … COMMIT`/`ROLLBACK`) — overriding
  the ABC no-op, which ARCHITECTURE_REVIEW §3.1 flags as HIGH for any backend.

**Files:** `core/duckdb_backend.py` (new); `tests/unit/test_duckdb_backend.py` (new).
**Exit gate:** in-memory DuckDB unit tests assert observable output — schema introspection
shows all 19 tables + indexes; round-trip a node/edge and read it back; a `transaction()`
that raises leaves the DB unchanged (mirrors C6). Not just "no exception raised".

### Phase 2 — Bulk-upsert sink (re-point the indexer)

**Deliverables:**
- `upsert_nodes_bulk`/`upsert_edges_bulk` via DuckDB `Appender` or
  `INSERT … SELECT … FROM (unnest(?))` (the spike's 10.6 s path). **One `execute()`-class
  call per label/edge-type per batch** — this is the CLAUDE.md `_flush_row_batch`
  per-batch (NOT per-file) invariant; it is backend-agnostic and survives.
- Re-point `indexer.py`'s `_flush_row_batch` sink from the Kuzu bulk calls to the DuckDB
  backend. The two-pass parse + `dependency_filter` + `body_scope` perf invariants in
  `base.py`/`indexer.py` are **untouched** (they are parser-side, backend-agnostic).

**Files:** `core/duckdb_backend.py`; `indexer/indexer.py` (sink wiring only).
**Exit gate:** the existing perf-invariant guards stay green —
[`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py),
[`test_bulk_upsert_invariant.py`](../tests/unit/test_bulk_upsert_invariant.py),
[`test_perf_scaling_guard.py`](../tests/unit/test_perf_scaling_guard.py). Bulk-load a
DWH-scale row set and assert node/edge counts match the Kuzu reference.

### Phase 3 — Query port + parity gate (the parity-risk phase)

**STEP 0 (DO THIS FIRST — blocks everything else in Phase 3 and gates Phase 5):**
**Extract and commit the Kuzu reference outputs as a test fixture BEFORE writing any SQL
ports.** No parity fixture exists in `tests/` today — the spike's numbers lived in a throwaway
`/tmp/feasibility.py`. From the **live Kuzu DB** on the DWH corpus, capture and commit (e.g.
`tests/fixtures/duckdb_parity/kuzu_reference.json`): node counts per label, edge counts per
type, `COLUMN_LINEAGE` traversal reached-sets (upstream + downstream for a fixed seed set),
hub ranking top-k (C7), backfill reachability closure, a table-level edge query result,
star-source result, and the cyclic-lineage reached-set (C8). The Phase 3 parity gate diffs the
DuckDB ports against this committed fixture.
**ORDERING CONSTRAINT:** Kuzu (`core/kuzu_backend.py` + dep) is deleted in **Phase 5**, which
runs *after* Phase 3 — so the reference **must be captured and committed while Kuzu still
exists.** **Phase 5's Kuzu deletion is BLOCKED until this fixture is committed.** If the fixture
is missing, do not start Phase 5.

**Deliverables:** translate every Cypher read site (per the surface table) to recursive-CTE
SQL. Port order: `tools.py` → `analyze.py` → `db.py` → `find.py` → `server.py`.
- Recursive lineage CTEs carry a **cycle guard** (visited-set / depth cap) and bounded
  depth — proven in C1/C8.
- Port the `analyze.py` `OPTIONAL MATCH` + `WITH … WHERE … kind IS NULL` star/sink filters
  as `LEFT JOIN` + `WHERE` with **identical** semantics (#38/#40/19.2 regressions live here).
- Rename `execute_cypher` → `execute_sql` (read-only passthrough, keep the write-rejection
  guard) in `tools.py`, `skill.py`, and the `gain.py` metric (Tier 2).

**Files:** `server/tools.py`, `server/skill.py`, `cli/commands/{analyze,db,find,gain}.py`,
`server/server.py`; `tests/fixtures/duckdb_parity/kuzu_reference.json` (new, committed in
STEP 0 from the live Kuzu DB); `tests/.../test_duckdb_parity.py` (new, diffs ports vs the
committed reference).
**Exit gate — PARITY GATE (hard):** for the DWH fixture, every ported query returns
**identical rows** vs the **committed Kuzu reference fixture (STEP 0)**: node/edge counts,
traversal reached-sets (`COLUMN_LINEAGE` upstream + downstream), hub ranking (C7), backfill
closure, table-level edges, star-source, and a cyclic-lineage case (C8). A row diff fails the
gate. **The committed fixture must exist before this gate can run.**

### Phase 4 — Reindex path = full-rebuild-in-transaction (delete the swap)

**Deliverables:**
- Drain body (`writer.py`): `escalate_to_rw + rebuild-and-swap` → `BEGIN … clear tables …
  bulk insert (Phase 2 sink) … COMMIT`. **Delete** the escalation primitives
  (`escalate_to_rw` writer.py:312, `de_escalate_to_ro` writer.py:410) and the tool-side
  `_get_or_escalate_rw`/`_de_escalate_to_ro_from_tool`/`_escalation_db_path`
  (tools.py:217/236/249) — DuckDB's single RW handle reads (MVCC) + writes (txn) with no
  mode-switch.
- Server (`server.py`/`tools.py`): hold **one R/W handle for the process lifetime** instead
  of RO-open-then-escalate. Simplify the B2 shutdown plumbing (server.py:381–399) — the
  `de_escalate_to_ro`-skip reason disappears; the close path stays.
- Watch path: `watch.py → indexer/watcher.py (`SqlFileEventHandler`) → core/jobs.py
  (`WatchJobManager._run_job`) → indexer.reindex_file`. `watcher.py` itself dispatches
  file-system events and calls through the `GraphBackend` ABC
  (`db.delete_nodes_for_file`, `indexer.resync_changed`) — **it needs no DuckDB-specific
  change**; only the downstream `reindex_file` sink (Phase 2) and the drain body route through
  the transactional rebuild.
- **Watch ⊥ live server — DELIBERATE DECISION (no change, accept current behavior):**
  `watch.py:29` opens the DB **directly** via `get_backend()` with **no server-live check**
  and holds it for the watch session's lifetime. Under DuckDB's cross-process exclusive lock
  (C4), this makes `sqlcg watch` **mutually exclusive** with a live MCP server: whichever
  starts second fails to open the file. **We KEEP this as-is** — `watch` is an interactive,
  foreground, long-running session a user starts deliberately on a dev machine; it is not a
  routed command and there is no use case for running it concurrently with a server on the
  same DB. We do **not** add a `db reset`-style live-server guard to `watch.py` in this
  feature (it would only convert a clear OS-level lock error into a different error). If the
  raw lock IOException proves too opaque in practice, a friendlier pre-check is a trivial
  follow-up — explicitly out of scope here. **Files-touched note:** `watch.py` and
  `indexer/watcher.py` are therefore in the Phase 4 files list for *audit only* (confirm no
  Cypher, confirm the ABC calls are backend-neutral); the only edit either may need is if
  `get_backend()`'s signature changes when RO/escalation is removed.
- Verify concurrent reads during a rebuild see the OLD snapshot then flip on COMMIT (C3).

**Files:** `server/writer.py`, `server/tools.py`, `server/server.py`, `core/jobs.py`,
`indexer/watcher.py` (audit — backend-neutral, no change expected),
`cli/commands/watch.py` (audit — direct `get_backend()` open, watch⊥server decision above).
**Exit gate:** a long write transaction + concurrent reads observe a consistent snapshot
and flip atomically on COMMIT (C3 as an integration test); a forced mid-rebuild raise
leaves the prior graph intact (C6). Single-writer queue + coalescing + drain shape
unchanged (Tier 3 KEEP).

### Phase 5 — Cutover + cleanup + perf measurement

**ORDERING PRECONDITION (blocks this phase):** Kuzu may only be deleted **after** the Phase 3
STEP 0 reference fixture (`tests/fixtures/duckdb_parity/kuzu_reference.json`) is committed —
once Kuzu is gone there is no way to regenerate it. If that fixture is not in git, **stop and
go back to Phase 3 STEP 0** before deleting anything below.

**Deliverables:**
- Delete `core/kuzu_backend.py` (449 LOC) and `core/neo4j_backend.py` (233 LOC).
- Delete `Neo4jConfig` (config.py:44), the `SQLCG_BACKEND` dispatch + `elif
  backend_type=="neo4j"` (config.py:376–404); `get_backend` returns `DuckDBBackend`
  directly. Rename `KuzuConfig` → neutral `DbConfig`, drop `buffer_pool_size_mb`
  (config.py:18,39); keep `get_db_path()` and the `~/.sqlcg/graph.db` default
  (config.py:17,38) — **path/constant fallbacks must match the renamed config** (CLAUDE.md).
- **`buffer_pool_size_mb` removal is NOT just the config field — two CLI flags expose it and
  would silently become dead env-sets after the field is deleted (plan-reviewer catch):**
  - `cli/commands/db.py` (`db_init`, lines ~24–33): remove the `--buffer-pool-size`
    `typer.Option`, the `os.environ["SQLCG_BUFFER_POOL_MB"] = ...` set (line ~32–33), and the
    `"KuzuDB buffer pool size in MB"` help text.
  - `cli/commands/index.py` (`index_cmd`, lines ~40–45 + ~158–159): remove the same
    `--buffer-pool-size` `typer.Option`, the `SQLCG_BUFFER_POOL_MB` env set (line ~158–159),
    and the same help text. (Note: `index.py:51` `--batch-size` help text also says
    "Files per **KuzuDB** transaction" — reword to "per DuckDB transaction" while here.)
- `uninstall.py`: **(newly-found item)** replace `shutil.rmtree(db_path)`
  (uninstall.py:131, "Delete the database directory") with single-file deletion —
  `Path(db_path).unlink()` plus the DuckDB `.wal` sibling — because **DuckDB is one file,
  Kuzu was a directory**. Delete/neutralize `_is_kuzu_backend` (uninstall.py:99,231) and the
  `SQLCG_BACKEND` read inside it — it is vacuous under a single backend; the guard at line 99
  must not block deletion of a valid DuckDB file.
- Remove the Neo4j mock guard in `tests/integration/test_freshness_mcp.py`.
- Remove Kuzu dep from `pyproject.toml` + `uv lock`.
- **Version bump → minor** (additive capability, no compat shim): `pyproject.toml`,
  `src/sqlcg/__init__.py` `1.3.0` → `1.4.0`, `uv lock` (per CLAUDE.md release steps).
- **PERF MEASUREMENT (required, gates the release):** run a **real** parse-and-index of the
  full ~1,600-file corpus with `DuckDBBackend` end-to-end and record wall-clock. The Phase 0
  spike only timed loading an *already-extracted* graph (10.6 s), **not** a parse — so the
  `< 5 min` budget (CLAUDE.md) is unproven for the real path and must be measured here.
  Record the figure (and batch size) in the postmortem; a regression vs the Kuzu baseline
  (~210–256 s, see MEMORY indexer-perf-baseline) is a release blocker.

**Files:** `core/kuzu_backend.py` (del), `core/neo4j_backend.py` (del), `core/config.py`,
`cli/commands/uninstall.py`, `cli/commands/db.py` (remove `--buffer-pool-size` flag + env set
+ help), `cli/commands/index.py` (remove `--buffer-pool-size` flag + env set + help; reword
`--batch-size` help "KuzuDB" → "DuckDB"), `tests/integration/test_freshness_mcp.py`,
`pyproject.toml`, `uv.lock`, `src/sqlcg/__init__.py`.
**Exit gate (ALL must be green):**
1. **Symbol-deletion gate:** `grep -rn <symbol> src tests` returns zero surviving callers for
   each deleted symbol; full test suite green.
2. **DOUBLE-REINDEX NO-CRASH + LINEAGE PARITY (user's top acceptance criterion):** run
   `sqlcg index <DWH corpus>`, then `sqlcg reindex` **TWICE consecutively**. Assert **no
   crash** on either reindex — Kuzu raised `KU_UNREACHABLE` on the 2nd consecutive reindex
   (see MEMORY: kuzu-upgrade-dead-end / issue29-dwh-test-findings); **DuckDB must fix this.**
   Then assert `sqlcg analyze` **column-trace lineage is identical after the 2nd reindex as
   after the initial index** (same reached-sets, same edge counts — re-indexing the same
   corpus is idempotent). This is the headline reason the migration exists; it gates the
   release.
3. **FULL-INDEX PERF CHECK:** a real parse-and-index of the full **~1,600-file** corpus with
   `DuckDBBackend` completes in **< 5 min** on a laptop (CLAUDE.md budget). Baseline to beat /
   not regress: Kuzu ~210–256 s (MEMORY: indexer-perf-baseline). Record the wall-clock figure
   **and** the batch size in the postmortem; a regression vs the Kuzu baseline is a release
   blocker. (This is the D10 real-corpus measurement — the Phase 0 spike's 10.6 s timed only a
   load of an already-extracted graph, not a parse.)
4. **COLUMN-TRACE SANITY / PARITY:** a fresh `index` → `analyze` column-trace on the DWH
   corpus returns the **same lineage reached-set as the committed Kuzu reference fixture**
   (the fixture extracted at the start of Phase 3). Not just "no exception" — assert the
   observable reached-set rows.
5. **Clean-machine round-trip:** `db info` and a fresh `index` → `analyze` round-trip work on
   a clean machine (re-index is the migration path).

## Removal inventory (grep-verified — deletion is where migrations rot)

Three tiers: **DELETE** (dead after cutover), **DECISION** (depends on scope), and the
dangerous **KEEP** traps (look dead, are load-bearing). Per CLAUDE.md, every DELETE was
checked for call sites confined to the to-be-removed set.

### Tier 1 — DELETE (Kuzu-only, no surviving caller after cutover)

| Target | Evidence it's safe | ~LOC |
|---|---|---|
| `core/kuzu_backend.py` (whole file) | only file importing `kuzu`; replaced by `duckdb_backend.py` | 449 |
| Kuzu dep in `pyproject.toml` + `uv.lock` | follows file deletion; `uv lock` | — |
| **Escalation machinery**: `escalate_to_rw`, `de_escalate_to_ro` (`writer.py:312,410`); `_get_or_escalate_rw`, `_de_escalate_to_ro_from_tool`, `_escalation_db_path` (`tools.py:217,236,249`) | exists only because Kuzu can't read+write on one handle; DuckDB's single RW handle + MVCC removes the entire RO→RW→RO dance. Call sites are all inside the drain/write path being rewritten | ~150 |
| B2 "skip RO reopen on shutdown" plumbing (`server.py:382,398`) tied to `de_escalate_to_ro` | the *reason* (RO reopen after escalation) disappears; shutdown-close logic stays but simplifies | ~20 |
| `buffer_pool_size_mb` field + env (`config.py:18,39`) **AND its two CLI flags** — `--buffer-pool-size` in `db.py` (`db_init`, ~24–33: flag + `SQLCG_BUFFER_POOL_MB` env set + "KuzuDB buffer pool size in MB" help) and `index.py` (`index_cmd`, ~40–45 flag/help + ~158–159 env set) | Kuzu-specific knob; DuckDB sizes differently. **The flags must die with the field or they become dead env-sets** (plan-reviewer catch). | ~5 + flags |

### Tier 2 — RESOLVED 2026-06-05 → both now DELETE/rename (DuckDB-only)

| Target | Decision |
|---|---|
| `core/neo4j_backend.py` (233 LOC) + `Neo4jConfig` (`config.py:44`) + the `elif backend_type=="neo4j"` branch (`config.py:397`) + `__init__` export + the mock guard in `test_freshness_mcp.py` | **DELETE all of it.** DuckDB-only; Neo4j was never actually used (only a `NotImplementedError` mock guard). Also drop the `SQLCG_BACKEND` env switch and `get_backend`'s backend dispatch — there is now one backend. |
| `uninstall.py` `_is_kuzu_backend` (def **231**, called at **99**) + its `SQLCG_BACKEND` read | **DELETE / neutralize.** Vacuous under one backend (always True). The line-99 guard must not block deleting a valid DuckDB file. **AND** change the deletion itself (line ~131) from `shutil.rmtree` (Kuzu = directory) to `Path.unlink` of the single DuckDB file + its `.wal` sibling. See Phase 5 — this is the **newly-found drift item**. |
| `execute_cypher` MCP tool (`tools.py:1677–1678`) + `skill.py:85,112` manifest entry/desc + `gain.py` metric (`tool_name='execute_cypher'` filter @116, `execute_cypher_ratio`/`_count` vars, user label) | **Rename → `execute_sql`** everywhere (raw read-only SQL passthrough; keep the write-rejection guard). Renaming the `gain.py` SQLite filter string keeps the usage metric tracking the renamed tool. |

### Tier 3 — KEEP (looks removable, is load-bearing — the traps)

| Tempting to delete | Why it MUST stay |
|---|---|
| `read_client.py` / `run_read_routed` / socket routing | **C4 proved the opposite** — while the server holds the DuckDB file, other processes can't open it *even read-only*. Socket routing is *more* essential, not less. |
| `writer.py` single-writer **queue + coalescing + drain_loop** | DuckDB still needs writes serialized in one process; only the *escalation primitive inside* the drain is removed, not the queue. |
| `KuzuConfig` itself (`config.py:14`) | It's the de-facto app config — `get_db_path()`, `log_path`, used by `control.py`, `noise_filter.py`, `index.py`, `uninstall.py`. **Rename → neutral `DbConfig`, drop only `buffer_pool_size_mb`.** Deleting it wholesale breaks path resolution everywhere. |
| `indexer.py` two-pass / batch / perf invariants | backend-agnostic — the CLAUDE.md perf invariants live here and survive untouched. |
| `control.py` (pid/sock) | process-liveness + socket path; unrelated to the engine. |

**Net:** ~600 (Tier 1) + up to ~250 (Tier 2 neo4j) LOC removed vs. ~one new
`duckdb_backend.py` (≈ kuzu_backend's size minus the lock/escalation cruft). Confirms
"deletes more than it adds." **Verification gate before each deletion:** `grep -rn <symbol>
src tests` returns zero surviving callers outside the removed set (CLAUDE.md rule).

## Risks & open decisions

- **Cross-process lock (C4-confirmed, sharper than first thought)** — while one process
  holds the file R/W, **no other process can open it at all, even read-only.** So with a
  live server, *all* access funnels through it. Audit the 27 backend-open sites: confirm
  CLI read commands route via `read_client.py` (socket) when a server is up, and only open
  the file directly in the no-server case. This is the one architectural must-get-right.
- ~~`execute_cypher` tool~~ — **RESOLVED:** rename to `execute_sql` (read-only passthrough).
- ~~Neo4j / multi-backend~~ — **RESOLVED:** DuckDB-only; delete Neo4j backend, config, and
  the `SQLCG_BACKEND` dispatch.
- **Recursive CTE on dense/cyclic lineage** — cycle guard correctness; parity-gate every
  traversal query, not just `COLUMN_LINEAGE`.
- **Perf budget unchanged** — full re-index of 1,600 files < 5 min on a laptop; measure a
  real `DuckDBBackend` index (spike only timed load of an extracted graph, not a parse).
- **No backward compat** — re-index is the migration (project rule).

## Test strategy
- Reuse the spike's extract→load→parity harness as a backend conformance test.
- Parity gate: for the DWH fixture, every ported query returns identical rows vs the Kuzu
  reference (node/edge counts, traversal reached-sets, hub ranking, backfill order).
- Concurrency test: long write transaction + concurrent reads see a consistent snapshot
  and flip atomically on commit.
- **Double-reindex idempotence (top acceptance criterion):** `index` → `reindex` → `reindex`
  on the DWH corpus must not crash (Kuzu's 2nd-reindex `KU_UNREACHABLE` must be gone) and the
  `analyze` column-trace lineage after the 2nd reindex must equal the post-initial-index
  lineage. Gated in Phase 5.
- Keep all existing parser-side perf invariants (CLAUDE.md) — they are backend-agnostic
  (`test_upsert_batch_invariant.py`, `test_bulk_upsert_invariant.py`,
  `test_perf_scaling_guard.py`, `test_T09_01_qualify_once.py` stay green untouched).

## § Drift found (live code vs draft, validated 2026-06-05)

All named call sites in the draft **exist** in live code. Corrections applied:

| # | Draft claim | Live reality | Action taken |
|---|---|---|---|
| D1 | "Supersedes `plan/feature_rebuild_swap_reindex.md`" | **No such file on disk.** Swap work lives inline in `writer.py`/`tools.py`. | Reworded header — nothing to supersede as a file. |
| D2 | `tools.py:1660` / `db.py:125` = "Neo4j branch to delete" | Both are `except NotImplementedError` **handlers** for the Neo4j `get_indexed_sha` stub, not deletable branches. Go dead-but-harmless when Neo4j is removed. | Reclassified in the surface table; not a Cypher port. |
| D3 | `run_write` at `graph_db.py:128` | Live def is at **129** (`run_read` 117). | Pinned to 129. |
| D4 | Cypher surface "~80 lines, analyze.py heaviest" | Live grep: `tools.py` ~37 lines is highest *volume*; `analyze.py` ~36 (9 routed sites) is highest *parity risk*. Counts are mixed (routing/comments/strings). | Replaced table with live counts + "order-of-magnitude guide" caveat; port order now `tools.py` first. |
| D5 | `skill.py:85,112` listed only in narrative | They are the **skill-manifest tool name + description** — must be in the rename set. | Promoted into the rename inventory + Phase 3. |
| D6 | gain.py `execute_cypher` — "leave OR rename" (undecided) | It's a SQLite metric (`tool_name='execute_cypher'` filter + ratio vars + label). Leaving it un-renamed silently breaks the usage metric after the tool rename. | **Decided: rename** for metric continuity. |
| D7 | ~~v3 claimed "no `watcher.py` exists"~~ — **v3 WAS WRONG (caught by plan-reviewer).** | `indexer/watcher.py` **does** exist (245 LOC: `SqlFileEventHandler`, `BranchMonitor`), imported by `watch.py:13`. Real path: `watch.py → watcher.py (`SqlFileEventHandler`) → jobs.py (`WatchJobManager._run_job`) → indexer.reindex_file`. | **v4 fix:** corrected Scope + Phase 4 to name `watcher.py`; added it to Phase 4 files (audit only — backend-neutral via the ABC, no DuckDB change needed); recorded the deliberate watch⊥live-server decision (keep current direct-open behavior under the C4 lock, no new guard). |
| D8 | **(MISSED entirely by the draft)** uninstall deletion | `uninstall.py:131` uses `shutil.rmtree` because **Kuzu is a directory**; DuckDB is a **single file** → `rmtree` is wrong. `_is_kuzu_backend` (99/231) becomes vacuous. | Added as a Phase 5 deliverable + Tier 2 row. **This is the most consequential drift** — a missed cutover step that would leave a broken `uninstall`. |
| D9 | Schema "etc." edge list | Full set is 7 node + 12 edge labels (pinned from `schema.py`). `_pk_field` is an **ABC static method (graph_db.py:193)** — inherited, cannot drift. | Pinned the full list + PK source. |
| D10 | Perf budget | Spike timed extracted-graph load (10.6 s), **not** a real parse-and-index. | Phase 5 now mandates a real end-to-end perf measurement as a release gate. |
| D11 | **(v4)** `buffer_pool_size_mb` removal scoped only to the config field | Two CLI flags expose it: `db.py` `db_init` (~24–33) and `index.py` `index_cmd` (~40–45, ~158–159), each setting `SQLCG_BUFFER_POOL_MB`. Deleting the field alone leaves dead env-sets + stale "KuzuDB buffer pool" help. | Added both files + flags to the Phase 5 deliverables, files list, and Tier 1 inventory; also reword `index.py` `--batch-size` "KuzuDB" → "DuckDB". |
| D12 | **(v4)** Phase 3 parity gate referenced a Kuzu reference dump that **doesn't exist** | No parity fixture in `tests/`; spike numbers were in throwaway `/tmp/feasibility.py`. Kuzu is deleted in Phase 5, after Phase 3. | Added Phase 3 STEP 0: extract + commit the Kuzu reference fixture **before** any SQL port; made Phase 5 Kuzu deletion **blocked** on that fixture existing. |
| D13 | **(v4, non-blocking confirmation)** `db.py` full-reset deletion under DuckDB | `db.py:84–92` already dispatches on `target.is_dir()` → `rmtree` else `unlink`, and already drops the `.wal` sidecar — **so it is already DuckDB-correct.** Only `uninstall.py` (D8) hardcodes `shutil.rmtree` and still needs the fix. | No change to `db.py` reset needed; noted here so the developer does not "fix" something already correct. |

**Verdict: ready to hand to the developer (v4, plan-reviewer fixes applied).** Phase 0 gate
is cleared; Phases 1–5 have concrete deliverables, exact files, and hard exit gates; the
removal inventory is grep-accurate (incl. the two `--buffer-pool-size` flags, D11); the
cutover deletion step (D8 uninstall) is captured, with `db.py` reset confirmed already-correct
(D13); the watch path is correctly described (`watcher.py` exists, D7-corrected) with a
deliberate watch⊥live-server decision; the parity fixture must be committed before any port
and gates Kuzu deletion (D12). The user's top acceptance criterion — **double-reindex no-crash
+ lineage parity** — plus the real-corpus perf check (D10) are now explicit Phase 5 exit
gates.
