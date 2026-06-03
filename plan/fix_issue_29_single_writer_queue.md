# Feature Plan: #29 — Single-Writer Queue + RO→RW Escalation

## Summary

Make the MCP server **serve the KuzuDB read-only** (after a brief read-write
startup window that **creates the schema on a fresh DB and is a no-op on an
already-initialized DB — it does NOT migrate** — OD-2) and escalate to read-write
**only for the duration of a drain**, so an idle server never blocks CLI readers. Route **every** writer (`index`, `reindex`, git-hook reindexes)
through the server's control socket when a server is live, feeding a single
serialized writer with coalescing. With no server live, the CLI degrades to
taking the lock itself — same code path, "queue of one". Add bounded
retry/backoff on the RW reopen and a clear error + `SQLCG_DB_PATH` workaround
when escalation cannot get the lock.

This is a **1.3 release gate** item (`plan/findings_v1.1.2_dwh_eval.md` §"Verdict
— release gate for 1.3"). It supersedes the deferred ARCHITECTURE_REVIEW.md
line 3029 entry "#28 server read-only-by-default + reindex escalation".

Primary source of truth: [`findings_v1.1.2_dwh_eval.md`](findings_v1.1.2_dwh_eval.md)
— especially the §"Risk analysis — the RO→RW escalation (measured on Kuzu
0.11.3)". The three design constraints from that section are **mandatory** and
are tracked as C1/C2/C3 below.

---

## Scope

### In Scope

- Server startup performs a **brief read-write open that creates the schema on a
  fresh DB and is a no-op on an already-initialized DB** (a bounded write window —
  effectively the first drain), then closes and reopens
  [`KuzuBackend`](../src/sqlcg/core/kuzu_backend.py) with `read_only=True` for
  serving ([`server/tools.py`](../src/sqlcg/server/tools.py) `init_backend`). This
  step **does NOT migrate** an existing schema: `init_schema()` only runs DDL when
  the `Repo` table is absent; on an already-initialized DB its `MATCH (n:Repo)`
  probe returns immediately and it neither compares nor upgrades the stored
  `SCHEMA_VERSION` (verified against [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
  L90-140). Re-index is the migration path (CLAUDE.md "No backward compatibility").
  The RW-ensure→close→reopen-RO sequence reuses the same escalation primitive as a
  drain (OD-2 — measured: a RO open of an empty DB fails, and DDL on a RO
  connection fails, so the schema cannot be created on the RO open).
- A **startup schema-version gate** in the RW-ensure window: after the
  create-if-absent step, read the stored `SCHEMA_VERSION` and **refuse to start**
  if it is present and not equal to the current `SCHEMA_VERSION` (because
  `init_schema()` is a silent no-op on a stale-schema DB, an old-schema graph would
  otherwise be served read-only with no warning — a trust bug). No auto-migration
  (OD-2; Step 1.4).
- An **escalation mechanism**: close the RO `Database` + reopen read-write for
  the duration of a drain, then de-escalate back to RO. (Kuzu has no in-place
  connection upgrade — Finding 1.)
- A **single-writer queue** in the server with the four coalescing rules
  (full-index supersedes; reindexes collapse; reindex-behind-index is a no-op;
  lock held only while draining).
- A new **`index` socket op** + routing change so manual `sqlcg index .`
  detects a live server and routes through the socket instead of opening RW
  directly (C1 — closes the invariant-breaking gap).
- **`reindex` op** reused/extended to enqueue rather than grab the backend lock
  for the whole resync; coalescing applies.
- **Bounded retry/backoff** on the RW reopen (C2).
- **Clear error + `SQLCG_DB_PATH` side-DB workaround** surfaced after the retry
  budget is exhausted (C3) — never the raw `Could not set lock on file`.
- **Observability**: `writer_queue` block in `mcp status`; CLI attach-and-wait
  progress over the socket for manual `index`/`reindex`; server lifecycle log
  lines (enqueued / coalesced-with-reason / started / drained).
- **Wait semantics**: hook reindex = fire-and-forget; manual `index`/`reindex` =
  attach-and-wait with `--detach` opt-out.
- **Doc-correctness fix**: correct the `KuzuBackend.__init__` docstring (RO takes
  a *shared* lock that still blocks RW, not *no* lock).

### Non-Goals

- No change to lineage extraction, parsing, or any
  [`base.py`](../src/sqlcg/parsers/base.py) / [`indexer.py`](../src/sqlcg/indexer/indexer.py)
  hot path. The drain reuses `Indexer.index_repo` / `resync_changed` **unchanged**
  (perf invariants inherited; see CLAUDE.md "Performance invariants").
- No Windows control-socket support (existing `sys.platform != "win32"` guard
  stays; Windows keeps the direct-write path).
- No multi-server / multi-DB writer coordination beyond the existing
  per-`db_path` socket isolation.
- No `db reset` routing through the socket in this PR (it is destructive and
  rarely run mid-session). It keeps taking the lock directly; if a server is
  live it must **refuse with a clear message** directing the user to stop the
  server first (or use the control socket) rather than silently fighting the lock
  (OD-3, resolved — see Step 3.4 + acceptance criteria).
- No persistence of the queue across server restarts — the queue is in-memory;
  a restart re-derives state from HEAD vs. stored SHA (re-index is the migration
  path, CLAUDE.md).
- No removal of the existing `--notify` flag semantics for git hooks (reused).

---

## Design

### The invariant this whole design rests on

> "Nothing opens the DB file directly while the server is live."

Escalation (close-RO → reopen-RW) fails **in ~4 ms with no retry** if any other
process holds a shared lock at that instant (Finding 2). Today the invariant is
broken by manual `sqlcg index .` / `reindex` (no `--notify`) which call
`get_backend()` with no `read_only` arg ([`index.py`](../src/sqlcg/cli/commands/index.py) L186,
[`reindex.py`](../src/sqlcg/cli/commands/reindex.py) L169). Closing that gap (C1)
is the *core* of #29: it eliminates the in-process competitors, which is what
makes escalation safe. Retry/backoff (C2) covers the residual out-of-band racer
and the close→reopen interleave; the clear error (C3) is the last resort.

CLI **reads** already route through the socket via `run_read_routed`
([`read_client.py`](../src/sqlcg/server/read_client.py)) and never fall back to a
direct open while a server is live — that half is done (since 1.1.2).

**Reads block for the duration of a drain (OD-1, resolved for 1.3).** The drain
holds the single `backend_lock` across escalate→run→de-escalate, so any `query`
op that arrives mid-drain **queues/blocks on that lock until the drain finishes**
and is then served on the same single connection. There is no second connection
and no concurrent read-during-write path in 1.3. The findings doc's aspiration
("reads stay available *during* a drain — a RW connection serves reads too") is
explicitly **scoped OUT for 1.3** and recorded as a possible future optimization
(a dedicated read connection on the same `Database`). Rationale: incremental
reindex is fast (seconds), so the read pause is imperceptible for the common case;
only a full large-repo reindex makes readers wait (~minutes), and the
single-connection model avoids Kuzu's per-connection thread-safety hazard
(ARCHITECTURE_REVIEW 17.2 / finding 3.3) — a second connection serving reads while
the indexer writes on another connection of the same `Database` is the unsafe path
we are deliberately not taking.

### Escalation mechanism

Add a small **escalation manager** owned by the server, living in a new module
[`server/writer.py`](../src/sqlcg/server/writer.py). It wraps the module-level
backend singleton (`sqlcg.server.tools._backend`) and exposes:

- `escalate_to_rw(db_path, *, current=None, opener=KuzuBackend, retry_budget_s=_ESCALATION_RETRY_BUDGET_S) -> KuzuBackend`
  — closes the **passed-in current backend** (`current`), then attempts
  `opener(path, read_only=False)` under a bounded retry/backoff loop (C2). On
  success, swaps the new RW backend into `tools._backend` (so `query` ops keep
  serving reads through it). On exhaustion, **reopens the RO backend** (so the
  server keeps serving reads) and raises `EscalationLockError` (C3).
  - **W1 — one signature, both callers, backend injected not read from the
    module.** `current` is the live backend handle the caller passes in, and
    `escalate_to_rw` does **not** read `tools._backend` itself — it is pure w.r.t.
    the module singleton on the way *in* (it only *writes* the swap on the way out).
    This makes both callers consistent and keeps the function unit-testable without
    the module global:
    - **Drain caller (Phase 2)**: passes `current = tools._backend` (the live RO
      singleton) so it is closed before the RW open.
    - **Startup caller (Step 1.3 / `init_backend`)**: `init_backend` itself creates
      the first RW backend (today via `KuzuBackend(path)` — `tools.py` L115), so for
      the startup ensure it passes `current=None` (nothing to close — the RW handle
      is opened fresh) and uses the same `de_escalate_to_ro` to drop to RO. The
      `current=None` path means "no existing handle to close, just open RW".
  - `opener` and `retry_budget_s` are **injectable parameters** (default to the
    real `KuzuBackend` and the module constant). Tests pass a fake `opener` and a
    short `retry_budget_s` via these parameters — **never** a global module patch
    (parallel-runner safe; W4 / Test Strategy discipline).
- `de_escalate_to_ro(db_path)` — closes the RW backend, reopens RO, swaps back
  into `tools._backend`. Always runs in a `finally` after a drain. **Skips the RO
  reopen and the `tools._backend` swap when a shutdown is in progress** (B2 — see
  "Shutdown vs. drain ordering" below).

### Shutdown vs. drain ordering (B2)

`_stop_watcher` ([`server.py`](../src/sqlcg/server/server.py) L297-334) calls
`shutdown_backend()` ([`tools.py`](../src/sqlcg/server/tools.py) L133-146), which
**unconditionally** sets `tools._backend = None`, closes the connection, and
closes `_metrics`. If a SIGTERM / `stop` op arrives **mid-drain**, two writers
race on `tools._backend`: `shutdown_backend()` nulls it, and the drain's
`de_escalate_to_ro` `finally` then **reopens RO and re-assigns** `tools._backend`
*after* shutdown nulled it — leaving a dangling live backend after the process
believed it had shut down (and a closed `_metrics` the drain may still poke).

**Resolution — (a) the stop path waits for the active drain to finish, AND (b) a
shutdown-requested flag makes `de_escalate_to_ro` skip the RO reopen. Both, for
defence in depth.** Rationale for choosing (a) as the primary guarantee: the drain
holds an **escalated RW connection with an in-flight write**; nulling/closing it
underneath the indexer mid-write risks a torn write / partial transaction on the
graph (worse than a brief shutdown delay). Waiting for the active drain lets the
RW write commit and `de_escalate_to_ro` run to completion *before* the connection
is closed. (b) covers the residual: even after the drain is allowed to finish,
de-escalation must not reopen RO when we are tearing down anyway.

Concretely:

- Add a module-level **`_shutdown_requested` `anyio.Event`** (owned by the server,
  in `server.py`, shared with the drain task and the writer module — passed in, not
  a global in `writer.py`, to stay parallel-test-safe; W4 discipline). It is set by
  the **same `stop_event` path** that triggers `_stop_watcher`.
- `_stop_watcher` (or the drain-aware stop sequence) **acquires `backend_lock`
  before calling `shutdown_backend()`** — since the drain holds `backend_lock`
  across escalate→run→de-escalate, acquiring it blocks until the active drain has
  fully de-escalated (or, if no drain is active, returns immediately). This is the
  (a) guarantee. (Note: the current `_stop_watcher` uses `os._exit(0)` and does
  **not** acquire `backend_lock`; this step changes it to acquire `backend_lock`
  first — see Step 2.2.)
- `de_escalate_to_ro` checks `_shutdown_requested.is_set()`: if set, it **closes
  the RW backend but does NOT reopen RO and does NOT re-assign `tools._backend`**
  (it leaves teardown to `shutdown_backend()`). This is the (b) guarantee, and it
  prevents the reopen-after-shutdown even if ordering ever changes.
- The drain loop checks `_shutdown_requested` before popping the next request, so
  no **new** drain starts once shutdown is requested.

Escalate/de-escalate happen **inside the single `backend_lock`** held by the
drain so no `query` op observes a half-closed backend. The swap of
`tools._backend` is done while holding `backend_lock`.

**Invariant — no op acts on a backend resolved outside `backend_lock` once
escalation exists (OD-7, B1).** Today the `reindex` op in `_control_socket_task`
([`server.py`](../src/sqlcg/server/server.py) L225-253) resolves `db =
backend_ref()` **before** entering `async with backend_lock` and then passes that
`db` into `_do_reindex()`. Once escalation can swap `tools._backend` under
`backend_lock`, that pattern is unsafe: a `db` captured before the lock can be the
**stale/closed RO handle** mid-drain. Therefore, **as part of Phase 2** (the same
PR slice that introduces escalation — not later), the existing inline `reindex`
op that runs `resync_changed` on a pre-lock `backend_ref()` is **retired**: it is
replaced by an enqueue onto `WriterQueue`, and **only the drain task** ever
resolves a backend, and it does so **after** acquiring `backend_lock` and through
the escalation primitive. The escalation primitive and the retirement of the old
inline reindex path **land together** so there is never a window where both
coexist. The same rule binds the new `index` op (Phase 3): it enqueues, it never
opens a backend in the connection handler. Restated as a one-line invariant:

> Once escalation exists, **no socket op may operate on a `backend_ref()`
> resolved outside `backend_lock`**; the only backend consumer is the drain task,
> which resolves it under the lock via `escalate_to_rw` / serves reads via the
> post-swap `tools._backend`.

`KuzuBackend` already accepts `read_only` ([`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
`__init__`); no constructor change needed. `init_schema()` is called **only on an
RW open** — never on the RO serving connection. This is mandatory, not optional:
measured against kuzu 0.11.3, `init_schema()`'s fall-through path issues DDL
(`CREATE NODE TABLE …`) which raises `Cannot execute write operations in a
read-only database!` on a RO connection. Its leading `MATCH (n:Repo) …` probe is
read-safe **only once the schema already exists** — it must never be the thing
that creates the schema. The startup schema-ensure (RW open) creates the schema on
a fresh DB and is a **no-op on an already-initialized DB — it does NOT migrate**
(`init_schema()` only runs DDL when the `Repo` table is absent; re-index is the
migration path, CLAUDE.md "No backward compatibility"). It and every subsequent
drain share one close→reopen escalation primitive (see Step 1.3).

### Retry/backoff (C2)

`escalate_to_rw` retries the RW open on the lock error. Parameters:

- **Total budget**: `_ESCALATION_RETRY_BUDGET_S = 5.0` seconds.
- **Backoff**: exponential, start `0.02 s`, factor `2`, cap `0.5 s`, with small
  jitter. ~9–10 attempts inside the budget.
- Retry **only** on the recognizable lock error (`"Could not set lock" in
  str(exc)` or `"lock" in str(exc).lower()`, mirroring the existing detection in
  `KuzuBackend.__init__`). Any other `RuntimeError` is non-retryable and
  re-raised immediately.

These are server-side transport/escalation constants, **not** `KuzuConfig`
values — same convention as `_NOTIFY_SOCKET_TIMEOUT_S` /
`_QUERY_SOCKET_TIMEOUT_S`. Define them at module top of `server/writer.py` with a
comment stating the rationale and that they are not config-owned.

**Escalation-failure logging (operator signal).** When (and only when) the retry
loop exhausts `_ESCALATION_RETRY_BUDGET_S` and is about to raise the C3
`EscalationLockError`, `escalate_to_rw` emits a **single named ERROR log line**
on that same path — `getLogger(__name__).error(...)`. This is the most
diagnostically important event in the feature (escalation genuinely could not
proceed and the graph was not updated), so it is logged at ERROR, distinct from
the routine queue-lifecycle lines (Step 4.4) which are INFO/DEBUG. The line names:
the attempt count and elapsed budget, the lock-holder PID hint from
`_find_lock_holder(db_path)` (the same hint embedded in the C3 message — computed
once, reused), and the `db_path`. Stable, greppable prefix
`escalation failed:` so operators and tests can match it. It is emitted **before**
the RO reopen + raise, so the audit line always precedes the user-facing error.
See Step 4.4 for the matching acceptance criterion.

### Clear error + workaround (C3)

`EscalationLockError` carries a message of the form:

```
Could not acquire the write lock to reindex after 5.0s — another process is
holding the database (PID <hint via _find_lock_holder>). The graph was not
updated. To run a one-off index without the server, point at a side DB:
    SQLCG_DB_PATH=/tmp/sqlcg-cli/graph.db sqlcg index <path>
or stop the server first ('sqlcg mcp stop').
```

Reuse the lock-holder helper already in
[`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) for the PID hint
(promoted to public `find_lock_holder(db_path: str) -> str` per W6, returns e.g.
`"PID 1234"` or `"PID unknown"`). Compute it **once** on the exhaustion path and reuse it for both
the ERROR log line (see retry/backoff section + Step 4.4) and this message. The
`SQLCG_DB_PATH` fallback path must match `KuzuConfig` (it is the env var
`KuzuConfig.from_env()` reads — confirm the exact var name `SQLCG_DB_PATH`
against [`core/config.py`](../src/sqlcg/core/config.py) before writing the
string; CLAUDE.md "Path/constant fallbacks must match KuzuConfig"). This message
is what the socket returns as `{"error": ...}` and what the CLI prints — never
the raw Kuzu string.

### The single-writer queue + coalescing

A `WriterQueue` (in `server/writer.py`) is created once in
`_run_with_control` and passed into `_control_socket_task` alongside
`backend_lock`. Model:

- **Request kinds**: `index` (full) and `reindex` (incremental). Each carries
  `{op, root, dialect, requested_by, queued_at}`; reindex also carries
  `from`/`to` (but see rule 2 — resolved at drain time).
- A single **drain task** (`anyio` task started in the task group) consumes the
  queue; only it ever holds the escalated RW backend.

**Coalescing rules (from findings §"Queue semantics"), applied at enqueue time
under an `anyio.Lock` guarding the queue structure:**

1. **Full `index` supersedes everything** — on enqueue of an `index`, drop all
   pending `reindex` and `index` requests; the full rebuild subsumes them. Log
   `coalesced: <n> pending superseded by full index`.
2. **Reindexes coalesce** — if a `reindex` is enqueued and a `reindex` is already
   pending (not yet active), do not add a second; record the coalesce (bump the
   total **and** the per-reason breakdown + last-coalesce timestamp — see
   "Coalesce accounting" below) and log. The pending reindex executes against
   **HEAD at drain time**
   (incremental = "catch up to now"); the queued request's `to` is recomputed to
   current HEAD when the drain starts, not when it was enqueued.
3. **Reindex arriving behind a queued full index is a no-op enqueue** (the index
   subsumes it) — log `coalesced: reindex dropped (full index pending)`.
4. **Write lock held only while draining** — the drain calls `escalate_to_rw`
   immediately before running the op and `de_escalate_to_ro` in a `finally`
   immediately after. Between drains the server is RO.

The **active** request is never coalesced (it is already running); coalescing
only mutates the pending set.

**Coalesce accounting (enriches the bare `coalesced_since_start` counter).** A
single opaque integer tells an operator *that* coalescing happened but not *what*
or *when*. Replace the bare counter with a small accounting structure on
`WriterQueue`, keyed by the three coalesce reasons (the same reason strings the
log lines and metrics use — single source of truth):

- `superseded_by_index` — pending requests dropped by an incoming full index
  (rule 1).
- `collapsed_into_pending_reindex` — a reindex folded into an already-pending
  reindex (rule 2).
- `reindex_dropped_index_pending` — a reindex dropped because a full index is
  already pending (rule 3).

The structure is `_coalesced: dict[str, int]` (per-reason counts) plus
`_last_coalesce_at: float | None` (monotonic-ok wall-clock epoch of the most
recent coalesce) and `_last_coalesce_reason: str | None`. `coalesced_since_start`
remains exposed as the **sum** of the per-reason counts (so the existing top-line
number is preserved, not removed), with the breakdown and last-coalesce context
added alongside it. `coalesce_view()` returns all of this for `status` (Step 4.1).
Reason strings are defined as module-level constants in `server/writer.py` so the
log line (Step 4.4), the metrics record (Step 4.5), and the status shape cannot
drift apart.

> Note: the *active* request is exempt from rule 1 — a full index enqueued while
> a reindex is mid-drain does not interrupt the running reindex; it waits, then
> runs and supersedes any *other* pending reindexes. Document this explicitly so
> "supersedes everything" is not misread as "cancels the running drain".

### Writer routing change for manual `index` (C1)

[`index.py`](../src/sqlcg/cli/commands/index.py) gains the same socket-probe
front-door that `reindex --notify` has, but **on by default** for manual index
(the user is at a terminal):

1. Probe `sock_path()`. If a live server answers, send `{"op": "index", "root",
   "dialect", ...}` framed, then **attach-and-wait** (stream progress; see
   Observability) unless `--detach`.
2. If no server (socket absent / refused / stale), fall through to the existing
   direct-write `_run_index` path **unchanged** — the zero-config small-repo
   "queue of one" (CLAUDE.md small-vs-large rule). This is the *only* path that
   opens RW directly, and it only runs when there is provably no server, so the
   invariant holds.

Add a new **`index` socket op** in `_control_socket_task`
([`server/server.py`](../src/sqlcg/server/server.py)) symmetric to the
(now-enqueueing) `reindex` op, but it **enqueues** onto `WriterQueue` (rule 1:
supersedes) and, for attach-and-wait, streams progress frames until drained.

The existing inline `reindex` op is **retired in Step 2.3** (B1): instead of
running `resync_changed` directly inside the connection handler against a
pre-lock `backend_ref()`, it **enqueues**; the drain (the only backend consumer)
runs the resync under `backend_lock` via the escalation primitive. This retirement
lands in the **same slice** as the escalation primitive so there is no window where
the old inline path and escalation coexist. The git-hook path stays
**fire-and-forget** (enqueue + immediate `{"ok": true, "queued": true}` response;
rule: hooks never wait — findings §"Wait semantics").

### API Changes (socket protocol)

New/changed ops (newline-delimited or framed JSON, per existing protocol in
[`server.py`](../src/sqlcg/server/server.py) L100-109):

- **`index`** (new): `{"op": "index", "root", "dialect", "wait": true|false}`.
  - `wait=false` (hook/`--detach`/git): reply immediately
    `{"ok": true, "queued": true, "position": N}`.
  - `wait=true` (manual default): stream **progress frames** then a single
    **terminal frame**. Framing: reuse the length-prefixed framing already defined
    for `query` (multiple frames on one connection; each frame is one
    progress/terminal JSON object). Document the multi-frame extension in the
    `_control_socket_task` docstring.
  - **Terminal-frame sentinel (W5).** A progress frame and the terminal frame can
    both carry `ok:true`, so the client must not key off content guesswork or EOF.
    **Decision: the terminal frame is the unique frame carrying `"done": true`**;
    progress frames carry `"done": false` (or omit it) plus
    `{files_done, files_total}`; the terminal frame carries `{"ok": true, "done":
    true, "summary": {...}}` (or `{"ok": false, "done": true, "error": ...}` on
    failure — W7). The client recv loop **reads frames until it sees `done == true`
    and then stops** — it does not rely on connection EOF as the terminator. Every
    frame is length-prefixed (recv-exactly), so a frame is never split.
- **`reindex`** (changed): `{root, from, to, dialect}` plus optional `"wait"`,
  where **`from` may be `null`/omitted** to mean "resync from the stored indexed
  SHA" (W3 — server resolves it on the RO connection at drain start; see Step
  3.1). Hook path sends `wait=false` with `from` already resolved (fire-and-forget).
  Manual `reindex` (no `--notify`, now defaulting to socket) sends `wait=true` and
  may send `from=null` (server resolves). Uses the same `done:true` terminal
  sentinel for `wait=true`.
- **`status`** (extended): adds a `writer_queue` object (see Observability).

Backward-compat note (CLAUDE.md "no backward-compat shims"): the wire protocol is
internal between matching `sqlcg` builds; re-install is the migration path. A
mismatched old client hitting the new `index` op simply does not exist (old
clients never send `index`). No shim.

### Data Models

- `WriterRequest` (dataclass): `op: Literal["index","reindex"]`, `root: str`,
  `dialect: str | None`, `from_sha: str | None`, `to_sha: str | None`,
  `requested_by: str` (`"cli"` / `"hook"`), `queued_at: float`.
- `WriterQueue`: holds `_pending: list[WriterRequest]`, `_active: WriterRequest |
  None`, `_active_progress: {files_total, files_done, started_at}`,
  `_coalesced: dict[str, int]` (per-reason counts, keyed by the reason constants),
  `_last_coalesce_at: float | None`, `_last_coalesce_reason: str | None`, an
  `anyio.Lock`, and an `anyio.Event` to wake the drain task. Methods:
  `enqueue(req) -> int (position)`, `coalesce_view() -> dict` (for `status`;
  returns `coalesced_since_start` (sum), `coalesced_by_reason` (the per-reason
  dict), `last_coalesce_at`, `last_coalesce_reason`), `pop_next() ->
  WriterRequest | None`, and a private `_record_coalesce(reason: str)` that bumps
  the per-reason count, sets the timestamp/reason, emits the Step 4.4 log line,
  and records to `MetricsStore` (Step 4.5) — the single choke point so all three
  observability surfaces stay consistent.
- Reason constants in `server/writer.py`: `COALESCE_SUPERSEDED_BY_INDEX`,
  `COALESCE_COLLAPSED_INTO_PENDING_REINDEX`, `COALESCE_REINDEX_DROPPED_INDEX_PENDING`
  (values are the reason strings listed under "Coalesce accounting").
- `EscalationLockError(RuntimeError)`: carries the C3 message.

### Dependencies

No new third-party deps. Uses existing `anyio`, `kuzu`, `typer`, `rich`.

---

## Implementation Steps

### Phase 1: Escalation primitive + doc fix (foundation, independently testable)

**Step 1.1**: Fix the `KuzuBackend.__init__` docstring.
- Files: [`src/sqlcg/core/kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
  (the `read_only` arg doc).
- Change "by not taking the exclusive write lock … Does NOT allow reads while a
  read-write writer holds the lock" to state: RO takes a **shared** lock that
  permits concurrent RO opens but still **blocks any RW open** (and RW blocks
  everything). Cite the Kuzu 0.11.3 lock matrix.
- Acceptance: docstring names "shared lock" and states RO blocks RW; no behaviour
  change.

**Step 1.2**: Create `server/writer.py` with `escalate_to_rw` /
`de_escalate_to_ro` + retry/backoff + `EscalationLockError`.
- Files: new [`src/sqlcg/server/writer.py`](../src/sqlcg/server/writer.py).
- Reuse `_find_lock_holder` for the PID hint (import from `kuzu_backend`).
  - **W6 — cross-layer import of a private helper.** `_find_lock_holder` is
    currently private to [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
    (L26, `_`-prefixed). Importing it into `server/writer.py` couples the server
    layer to a `core` private. **Decision: promote it to a public helper**
    (`find_lock_holder`, drop the underscore) in `kuzu_backend.py` and update its
    existing in-module call site, so the cross-layer use is a deliberate public
    API rather than reaching into a private. (Signature unchanged:
    `find_lock_holder(db_path: str) -> str`, returns `"PID 1234"` / `"PID
    unknown"`.) If promotion is declined in review, the fallback is to explicitly
    acknowledge the private-import coupling in a code comment at the import site —
    but promotion is preferred and is the planned approach.
- Constants `_ESCALATION_RETRY_BUDGET_S`, backoff params at module top with the
  "not KuzuConfig-owned" comment.
- Verify the `SQLCG_DB_PATH` env var name against
  [`core/config.py`](../src/sqlcg/core/config.py) before hardcoding it in the C3
  message. (Confirmed: `KuzuConfig.from_env` reads `SQLCG_DB_PATH` at
  [`core/config.py`](../src/sqlcg/core/config.py) L34 — use that exact name.)
- The same `escalate_to_rw` / `de_escalate_to_ro` pair is reused by Step 1.3 for
  the startup schema-ensure window, not just by drains — design the API so a
  caller can run a one-shot RW op (ensure schema) and return to RO through it.
- Acceptance: given the injectable `opener` parameter (a fake that raises the lock
  error K times then succeeds) and a short `retry_budget_s` parameter,
  `escalate_to_rw` succeeds within budget for K small and raises
  `EscalationLockError` (with the side-DB workaround text) when it never succeeds.
  The retry-budget override is passed **via the `retry_budget_s` parameter, not a
  module-global patch** (parallel-runner safe). Test asserts the **message
  content** (observable), not just the exception type.

**Step 1.3**: Wire `init_backend` to ensure the schema via a bounded RW window,
then serve RO. (Reshaped by OD-2 — see below.)
- Files: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) `init_backend`,
  [`src/sqlcg/server/writer.py`](../src/sqlcg/server/writer.py) (the shared
  escalation primitive from Step 1.2).
- **Sequence** (OD-2-mandated — a RO open of a non-existent/empty DB raises
  `Cannot create an empty database under READ ONLY mode.`, and DDL on a RO
  connection raises `Cannot execute write operations in a read-only database!`,
  both measured on kuzu 0.11.3):
  1. **RW open** `KuzuBackend(path, read_only=False)` → call `init_schema()`,
     which **creates the schema on a fresh DB and is a no-op on an
     already-initialized DB — it does NOT migrate** (verified against
     [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) L90-140: `init_schema()`
     runs DDL only when the `Repo` table is absent; on an existing DB its
     `MATCH (n:Repo)` probe returns early and the stored `SCHEMA_VERSION` is neither
     compared nor upgraded). The bounded startup write window is effectively the
     first drain. Then run the **schema-version gate** (Step 1.4) → **close** the RW
     backend. (Re-index is the migration path, CLAUDE.md "No backward
     compatibility".)
  2. **Reopen RO** `KuzuBackend(path, read_only=True)` and store it as the
     `tools._backend` serving singleton. `init_schema()` is **never** called on
     this RO connection (its DDL fall-through would fail; its `MATCH (n:Repo)`
     probe is only safe now that step 1 created the schema).
  3. Implement step 1↔step 2 using the **same close→reopen escalation primitive**
     (`escalate_to_rw` / `de_escalate_to_ro` from Step 1.2) that drains reuse, so
     there is one code path for "ensure schema" and "drain" — not a bespoke
     startup branch. The startup ensure is the degenerate drain whose op is
     "create schema if absent".
- The `db_path` resolves via `KuzuConfig` / `SQLCG_DB_PATH` (CLAUDE.md
  path-fallback rule) — never a hardcoded path.
- Acceptance:
  - **Fresh-DB init**: a server started against a **non-existent** `db_path`
    initializes the schema correctly (no `Cannot create an empty database under
    READ ONLY mode.` error) and then serves; an integration test points at a temp
    path that does not yet exist, starts the server, and asserts a routed read
    returns rows (e.g. an empty `Repo` count of 0) rather than an error.
  - **RO serving rejects no reads**: with the server up and no drain in flight, a
    routed read / concurrent `KuzuBackend(path, read_only=True)` open in a second
    process succeeds and returns rows (the 1.1.2-verified RO+RO case). The
    integration test asserts the second open returns rows, and asserts the serving
    connection is RO by confirming a concurrent in-process RW open is blocked while
    the server holds its shared RO lock.

**Step 1.4**: Startup schema-version gate — refuse to start on a stale-schema DB.
- Files: [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) `init_backend`
  (inside the RW-ensure window from Step 1.3, after the `init_schema()`
  create-if-absent call and **before** closing the RW backend / reopening RO).
- **Why**: `init_schema()` is a silent no-op on a DB that already has a `Repo`
  table — it does **not** compare the stored `SCHEMA_VERSION` to the current one
  (verified against [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) L90-140).
  Without an explicit gate, a v5 graph under a v6 build would be served read-only
  with **no warning** (a trust bug — the user gets stale/incompatible lineage and
  is told nothing). There is currently **no** startup schema-version gate anywhere
  in [`src/sqlcg/server/`](../src/sqlcg/server/) (confirmed: the only
  version-warning machinery is the `xfail` `db info` test — see note below).
- **Logic** (mirrors the existing CLI gates in
  [`reindex.py`](../src/sqlcg/cli/commands/reindex.py) L173-181 and
  [`index.py`](../src/sqlcg/cli/commands/index.py) L192-198 — same
  `get_schema_version()` API):
  1. On the RW backend, call `stored = backend.get_schema_version()`
     (`KuzuBackend.get_schema_version() -> str | None`, confirmed at
     [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) L356).
  2. Import the current version: `from sqlcg.core.schema import SCHEMA_VERSION`
     (currently `"6"` at [`core/schema.py`](../src/sqlcg/core/schema.py) L6 — read
     the constant, never hardcode the digit).
  3. If `stored is not None and stored != SCHEMA_VERSION`: emit the actionable
     message and **refuse to start** (raise so server startup exits non-zero — do
     **not** swallow it). Message form (names **both** versions and the remedy):

     ```
     Database schema is v<stored>, but this build expects v<SCHEMA_VERSION> —
     run 'sqlcg db reset && sqlcg index <path>' to re-index.
     ```

  4. If `stored is None` (fresh DB just created in Step 1.3 step 1) or
     `stored == SCHEMA_VERSION`: proceed normally to close RW → reopen RO.
- **No auto-migration** — this gate only refuses; it never rewrites the schema
  (CLAUDE.md "No backward compatibility. Re-index is the migration path").
- **No TODO in the happy path**: the gate is fully implemented; the pass-through
  (current version) and the refusal (stale version) are both complete branches.
- **Grep-confirmed call site** (CLAUDE.md): the gate runs inline inside
  `init_backend`'s RW-ensure block — it is not a separate helper unless extracted;
  if extracted to a helper (e.g. `_assert_schema_current(backend)`), grep-confirm
  it is invoked from `init_backend` before the PR opens. The `db_path` /
  `SCHEMA_VERSION` resolve via `KuzuConfig` / `sqlcg.core.schema` respectively —
  never hardcoded.
- **Relationship to the `xfail` `db info` warning**
  ([`test_star_schema_unit.py`](../tests/unit/test_star_schema_unit.py) L172,
  `test_db_info_warns_on_outdated_schema_version`): that test targets a **different
  surface** — `db info` must *warn* and still **exit 0**. This startup gate instead
  *refuses to start* (non-zero). They are independent; this gate does **not**
  implement the `db info` warning, so the `xfail` stays **out of scope** for this
  PR. (Both would share the same `get_schema_version()` API, so the `db info`
  warning remains a small, separate follow-up — not blocked by this gate.)
- Acceptance (all observable output, not "no exception"):
  - **Stale schema refuses**: a server started against a DB seeded with a
    `SCHEMA_VERSION` ≠ current exits **non-zero** with a message naming **both**
    `v<stored>` and `v<current>` and the `sqlcg db reset && sqlcg index <path>`
    remedy. Assert the captured message substrings and the non-zero exit.
  - **Current version starts**: a server against a DB whose stored version equals
    `SCHEMA_VERSION` starts normally and serves a routed read.
  - **Fresh DB starts**: a server against a non-existent `db_path` initializes
    (Step 1.3) and starts (stored version is now current; gate passes) — no
    refusal.

### Phase 2: Single-writer queue + drain task

**Step 2.1**: Implement `WriterQueue` + `WriterRequest` + coalescing rules.
- Files: [`src/sqlcg/server/writer.py`](../src/sqlcg/server/writer.py).
- Implement rules 1–3 in `enqueue`; rule 4 is enforced by the drain task.
- **W4 — test harness for the queue.** `enqueue` acquires the `WriterQueue`
  `anyio.Lock`, which is only usable inside an async runner — so the queue unit
  tests **run under `@pytest.mark.anyio`** (the project already uses anyio; the
  tests `await queue.enqueue(...)` / `await queue.pop_next()`). `coalesce_view()`
  is a pure synchronous read (no lock — it reads a consistent snapshot) and can be
  asserted directly. Decision: **option (b)** — async-marked queue tests — rather
  than splitting the mutation into a separate sync core, because the coalescing
  logic and the lock guard belong together and the anyio marker is zero extra
  machinery here. State this in the test file.
- Acceptance: `@pytest.mark.anyio` unit tests on `enqueue`/`pop_next`/
  `coalesce_view` (no DB, no sockets) assert: index supersedes pending (rule 1);
  two reindexes collapse to one and bump `coalesced_since_start` (rule 2);
  reindex-behind-index is dropped (rule 3); active request is never mutated. All
  assert observable queue state.

**Step 2.2**: Add the drain task to the server task group + the shutdown-ordering
guard (B2).
- Files: [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py)
  `_run_with_control` (start the drain task; create and share the
  `_shutdown_requested` event; change `_stop_watcher` to acquire `backend_lock`
  before `shutdown_backend()`), and the drain implementation (in `writer.py`,
  called from the task).
- Drain loop: check `_shutdown_requested` (skip if set — B2); wait on the wake
  `Event`; `pop_next`; recompute reindex `to` to current HEAD and, when
  `from is None`, resolve `from = db.get_indexed_sha()` at drain start (rule 2 +
  W3); `async with backend_lock:` → `escalate_to_rw` →
  `to_thread.run_sync(<index_repo|resync_changed>)` → `de_escalate_to_ro` in
  `finally`; update `_active_progress` via the indexer `progress_callback`.
- **B2 shutdown ordering**: `_stop_watcher` acquires `backend_lock` before
  `shutdown_backend()` (waits for the active drain to de-escalate);
  `_shutdown_requested` is set on the `stop`/SIGTERM path before that, so the drain
  starts no new work and `de_escalate_to_ro` skips the RO reopen. Net: a stop
  mid-drain lets the in-flight write commit, then tears down once — never reopens
  RO after `shutdown_backend()` nulled `tools._backend`.
- **W7 — drain exception path (non-escalation failure).** If the indexer body
  (`index_repo` / `resync_changed`) **raises** (a parse/IO/Kuzu error that is *not*
  an `EscalationLockError`), the drain's `finally` runs `de_escalate_to_ro` as
  usual, **and** the drain handler must also: (a) clear `WriterQueue._active` (set
  it back to `None`), and (b) set `_active_progress` to a **terminal/failed** state
  (e.g. `{"state": "failed", "error": <str>, "finished_at": <ts>}`) — so `status`
  does **not** show a phantom forever-running drain. For a `wait=true` client, the
  handler emits a **terminal frame** with `{"ok": false, "done": true, "error":
  <relayed message>}` (W5 sentinel) so the attached CLI exits non-zero with the
  error rather than hanging on the missing terminal frame. The exception is logged
  (the routine drain lifecycle line at ERROR for the failed drain), then swallowed
  by the drain loop so the loop survives and processes the next request — one bad
  request must not kill the drain task. `EscalationLockError` keeps its own
  dedicated path (C3 message + the Step 4.4 ERROR line); W7 covers everything else.
- Acceptance: integration test enqueues a reindex against an in-memory/temp DB
  and asserts the drain escalates, runs `resync_changed` **exactly once**, and
  de-escalates back to RO (assert the post-drain backend is RO by opening a
  concurrent RW in-process succeeds only when expected). Pin "resync_changed
  called once" like the existing `test_reindex_via_server.py` Scenario A.
- Acceptance (B2 — stop mid-drain): with a drain in flight (deterministic slow
  drain via the injected `anyio.Event` the drain awaits before releasing
  `backend_lock` — see Test Strategy), fire the stop/SIGTERM path; assert (a) the
  in-flight drain completes (the write is not torn — the post-restart graph
  reflects the committed reindex), and (b) after shutdown `tools._backend is None`
  — `de_escalate_to_ro` did **not** reopen RO after `shutdown_backend()` ran.
  Assert the observable `tools._backend is None` and the committed graph state, not
  just "no exception".
- Acceptance (W7 — drain body raises): inject an indexer whose
  `resync_changed`/`index_repo` raises a non-escalation error; assert (a)
  `WriterQueue._active` is `None` afterward and `coalesce_view()`/`status` show no
  forever-running active drain (the progress is a terminal `failed` state, not a
  stuck running one); (b) the drain de-escalated back to RO (`tools._backend` is the
  RO backend, lock released); (c) a `wait=true` client receives a terminal frame
  with `ok:false, done:true, error:<msg>`; (d) the drain loop then processes a
  subsequent enqueued request (the task survived). Assert the observable
  status/frame/queue state.

**Step 2.3** (B1 — MUST land in the same slice as Step 2.2): **Retire the inline
`reindex` op** so no op holds a pre-lock `backend_ref()` once escalation exists.
- Files: [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py)
  `_control_socket_task` (the `op == "reindex"` block, L225-253).
- The current block resolves `db = backend_ref()` **before** `async with
  backend_lock` and passes it into `_do_reindex()` → `resync_changed`. Replace the
  body with an **enqueue** onto the `WriterQueue` (recompute-`to`-at-drain-time
  semantics, rule 2): build a `WriterRequest(op="reindex", ...)`, call
  `queue.enqueue(req)`, and reply per the `wait` flag (hook/`wait=false` →
  immediate `{"ok": true, "queued": true, "position": N}`; manual/`wait=true` →
  attach-and-wait, Step 3.x progress frames). The connection handler **no longer
  calls `backend_ref()` and no longer runs `resync_changed`** — that work moves
  entirely to the drain task (Step 2.2), which resolves the backend under
  `backend_lock` via the escalation primitive.
- This step **lands in the same commit/PR slice as Step 2.2's escalation
  primitive** so there is never a build where escalation can swap `tools._backend`
  while the old inline `reindex` op still captures a pre-lock `db`. Do not split
  them across commits.
- Grep-confirmed call site (CLAUDE.md): after this step, grep `backend_ref()` in
  `_control_socket_task` must show it is **not** used in the `reindex` op body
  (the `status`/`query` ops keep their own under-lock usage — see B1 note: those
  resolve and use the backend *inside* their `async with backend_lock` /
  read-only path; the violation B1 targets is specifically the pre-lock capture in
  `reindex`).
- Acceptance:
  - A `reindex` socket request **enqueues** and the drain (Step 2.2) performs the
    `resync_changed` under `backend_lock` via escalation; the connection handler
    itself never opens or touches a backend. Assert (a) `resync_changed` is called
    exactly once from the drain path, and (b) the `reindex` handler returns its
    `queued`/summary response **without** having resolved `backend_ref()` (assert
    via a spy/patch on `backend_ref` that the `reindex` branch does not call it).
  - No window of co-existence: a single test confirms that in the shipped code the
    `reindex` op body contains no `backend_ref()`-before-lock pattern (structural
    assertion mirrors the perf-invariant guard style — a code-level guard, not a
    timing test).

### Phase 3: Writer routing (C1) — manual index through the socket; standalone-SHA protocol

**Step 3.1**: Add the `index` socket op in `_control_socket_task` + define the
standalone-reindex (`from=None`) server-side SHA resolution (W3).
- Files: [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py).
- Symmetric to the now-enqueueing `reindex` op (Step 2.3); enqueues onto
  `WriterQueue` (rule 1: index supersedes). Supports `wait=true` (stream progress
  frames + final summary) and `wait=false` (immediate `{ok, queued, position}`).
  Like `reindex`, it **does not** resolve `backend_ref()` in the connection
  handler (B1 invariant) — it only enqueues.
- **Standalone-reindex base-SHA resolution (W3 — required).** The current CLI
  `reindex --notify` path explicitly **refuses** the no-`--from` case because it
  cannot read the stored base SHA without a direct DB open that would fight the
  live server's lock ([`reindex.py`](../src/sqlcg/cli/commands/reindex.py)
  L93-100). Under #29 the manual `reindex` always routes through the socket, so
  the **server** must resolve the base SHA, not the client. Resolution:
  - The `reindex` op accepts `"from"` **omitted or `null`** to mean "resync from
    the stored indexed SHA" (the standalone case). The wire shape becomes
    `{"op": "reindex", "root", "from": <sha|null>, "to": <sha|null>, "dialect",
    "wait"}`.
  - When `from` is null/absent, the **server resolves the base SHA itself** by
    calling `db.get_indexed_sha()` (a metadata read — `MATCH (v:SchemaVersion)
    RETURN v.indexed_sha`, confirmed read-safe and feasible on the **RO** serving
    connection, [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) L388). This
    read is the **only** backend access in the connection handler and it is done
    **under `backend_lock`** on the RO serving connection at enqueue-decode time
    *or* deferred to the drain. **Decision: defer it to the drain** — the drain
    already holds `backend_lock` and, at drain start, resolves `from = stored
    indexed SHA` and `to = current HEAD` (rule 2 recompute) atomically. This keeps
    the B1 invariant clean (the connection handler never touches the backend) and
    matches rule 2 ("catch up to now at drain time"). The `WriterRequest.from_sha`
    is stored as `None` and the drain substitutes the stored SHA.
  - When `from` is present (git-hook path already resolves it,
    [`reindex.py`](../src/sqlcg/cli/commands/reindex.py) L103), it is used as-is;
    no server-side resolution.
  - If the drain resolves `from is None` and `get_indexed_sha()` also returns
    `None` (DB never indexed), the drain responds with an actionable
    `{"error": "no prior index — run 'sqlcg index <path>' first"}` (not a crash,
    no raw lock/Kuzu string) and the request is dropped from the queue.
- Acceptance:
  - a framed `index` request with `wait=false` returns `{ok, queued, position}`;
    with `wait=true` returns ≥1 progress frame then a terminal frame (see W5 for
    the terminal sentinel). Integration test asserts frame sequence;
  - a `reindex` request with **`from` omitted** against a server whose DB has a
    stored indexed SHA drains successfully and `resync_changed` is called with the
    **stored SHA as `from`** and **current HEAD as `to`** (assert the args passed
    to `resync_changed`, observable); a `reindex` with `from` omitted against a
    **never-indexed** DB returns the actionable "no prior index" error (assert the
    message, not just non-zero).

**Step 3.2**: Route manual `sqlcg index .` through the socket by default.
- Files: [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py).
- Add `--detach` option. Probe socket (mirror `reindex.py` L85-153 pattern);
  if live → send `index` op (`wait = not detach`), attach-and-wait progress;
  if no server → existing `_run_index` direct path unchanged.
- On `EscalationLockError` relayed as `{"error": ...}`: print the C3 message and
  exit non-zero. **Never** print the raw Kuzu lock string.
- Grep-confirmed call site (CLAUDE.md): the socket-probe helper must be invoked
  from `index_cmd`; verify before PR.
- Acceptance: with a live server, `sqlcg index .` does **not** open the DB
  directly (assert no second RW open occurs / no "Database is locked"); with no
  server it indexes directly (zero-config small-repo path still works).

**Step 3.3**: CLI-side — switch manual `reindex` to socket-route + attach-and-wait;
keep hook fire-and-forget; **drop the standalone-`--from` refusal**.
- Files: [`src/sqlcg/cli/commands/reindex.py`](../src/sqlcg/cli/commands/reindex.py),
  [`src/sqlcg/cli/commands/git.py`](../src/sqlcg/cli/commands/git.py).
- The server-side retirement of the inline `resync_changed` already happened in
  **Step 2.3** (the op enqueues). This step is the **CLI half**: manual `reindex`
  (no `--notify`) now probes the socket by default and, when a server is live,
  sends the `reindex` op with `wait=true` and attaches to progress frames
  (terminating on the `done:true` sentinel, W5). Git hook (`git.py`) sends
  `wait=false` and returns immediately — a `git checkout`/`pull` must never wait
  (findings §"Wait semantics").
- **W3 — remove the standalone refusal.** The existing `--notify` branch raises
  `OSError("--notify without --from requires direct DB access; falling through")`
  at [`reindex.py`](../src/sqlcg/cli/commands/reindex.py) L93-100 precisely because
  the client could not read the base SHA. Under #29 the server resolves it (Step
  3.1), so the client **sends `from=null`** instead of refusing: when `from_sha is
  None` and a server is live, send `{"op":"reindex","root","from":null,"to":null,
  "dialect","wait":true}` and let the drain substitute stored-SHA→HEAD. The
  no-server fallback keeps the existing direct-write path (which already resolves
  the stored SHA via a direct DB open, safe because there is provably no server).
- Grep-confirmed call site (CLAUDE.md): the socket-route helper is invoked from
  `reindex_cmd`; verify before PR.
- Acceptance:
  - hook path returns before the drain completes (assert immediate `queued`
    response); manual path blocks until the `done:true` terminal frame;
  - manual `sqlcg reindex` with **no `--from`** against a live server no longer
    errors with "requires direct DB access" — it sends `from=null` and the server
    resolves the base SHA (asserted in Step 3.1 acceptance); with no server it
    falls back to the direct path unchanged.

**Step 3.4**: `db reset` (both full and `--repo`) refuses cleanly when a server is
live (OD-3, resolved; W2).
- Files: [`src/sqlcg/cli/commands/db.py`](../src/sqlcg/cli/commands/db.py)
  `db_reset` (the `reset` subcommand, L43-73).
- **W2 — guard BOTH branches.** `db_reset` has two write paths that each open the
  RW backend / lock directly:
  1. the **`--repo` partial reset** (`db.py` L48-55) — `get_backend()` (RW) +
     `run_write("MATCH (r:Repo {path}) DETACH DELETE r")`; and
  2. the **full reset** (`db.py` L56-73) — deletes the DB file directly.
  Both fight a live server's lock. The server-probe guard must run **before
  either** branch (at the top of `db_reset`), not only before the full reset.
- Before any destructive action, probe `sock_path()` (same helper the
  read/index paths use). If a live server answers, **refuse**: print a clear
  message — "A server is running on this database; stop it first
  (`sqlcg mcp stop`) or reset via the control socket" — and exit non-zero. Do
  **not** attempt the destructive open (RW partial-reset open *or* file delete),
  and do **not** print a raw `Could not set lock on file` error. With no server
  live, `db reset` (full and `--repo`) keeps its existing direct behaviour
  unchanged.
- Grep-confirmed call site (CLAUDE.md): the socket-probe guard must be invoked
  from the `reset` command body; verify before PR.
- Acceptance: with a live server, **both** `sqlcg db reset` and `sqlcg db reset
  --repo <path>` exit non-zero, print the "stop the server first" message naming
  `sqlcg mcp stop`, and the DB is **not** reset (assert the graph still returns
  rows afterward — observable, not just exit code); with no server, both reset as
  before.

### Phase 4: Observability

**Step 4.1**: Extend the `status` socket op with `writer_queue`.
- Files: [`src/sqlcg/server/server.py`](../src/sqlcg/server/server.py) `status` op.
- Emit the `writer_queue` object built from `WriterQueue.coalesce_view()`:
  `{active, pending, coalesced_since_start, coalesced_by_reason,
  last_coalesce_at, last_coalesce_reason}`. The bare `coalesced_since_start`
  (findings §"Observability" (1)) is retained as the sum, but enriched with the
  per-reason breakdown (`{superseded_by_index, collapsed_into_pending_reindex,
  reindex_dropped_index_pending}`) and the last-coalesce context so an operator
  can tell *what* was coalesced and *when*, not just a count.
- `last_coalesce_at` is an epoch float (or `null` if nothing coalesced yet); the
  CLI renders it human-readably (Step 4.2). `coalesced_by_reason` always carries
  all three reason keys (zero-filled) so the shape is stable.
- **Frame the `status` response** with the same `<decimal-byte-length>\n<json-body>`
  framing the `query` op uses, so the (now larger, queue-bearing) `status` payload
  is read in full by the recv-exactly client (OD-4 — Step 4.2). Document this in
  the `_control_socket_task` docstring (status moves from unframed to framed).
- **B3 — atomic with Step 4.2 (single commit).** The server-side framing of
  `status` (this step) and the client-side recv-exactly parse (Step 4.2) **MUST
  land in one commit**. A split where the server frames `status` before the client
  can parse the frame breaks `mcp status` for any live user in the interval
  (the unframed `recv(4096)` would read `<len>\n{...}` and `json.loads` on the
  digit-prefixed bytes → `JSONDecodeError`). Treat Step 4.1's status-framing change
  and Step 4.2 as a single indivisible change. **The `stop` op stays UNFRAMED**:
  [`mcp.py`](../src/sqlcg/cli/commands/mcp.py) `mcp_stop` does `s.recv(128)`
  (L143) and the server replies to `stop` unframed (`server.py` L216-218) — do
  **not** touch the `stop` request/response framing. Only the `status` op changes.
- Acceptance: integration test enqueues work that triggers each coalesce reason
  and asserts `status.writer_queue` returns the active op, a non-empty `pending`,
  `coalesced_since_start` equal to the sum of `coalesced_by_reason`, a non-null
  `last_coalesce_reason` matching the last reason triggered, and a `last_coalesce_at`
  greater than the test start time. Assert the observable dict, not just presence
  of the key.

**Step 4.2** (B3 — same commit as Step 4.1): CLI `mcp status` renders
`writer_queue` and switches to framed recv-exactly.
- **Lands in the same commit as Step 4.1** (B3): the server-frames-status change
  and this client-parse change are one atomic change so `mcp status` is never
  broken in between. Only `status` is affected; `mcp_stop`'s `s.recv(128)` (L143)
  is left untouched (the `stop` op stays unframed).
- Files: [`src/sqlcg/cli/commands/mcp.py`](../src/sqlcg/cli/commands/mcp.py)
  (the status handler L91-118; **not** `mcp_stop` L121-153).
- The handler currently does a single `s.recv(4096)` — a populated `writer_queue`
  may exceed 4096 bytes and truncate. Switch the status recv to the **framed
  recv-exactly read already implemented in
  [`read_client.py`](../src/sqlcg/server/read_client.py)** (OD-4, resolved): the
  `makefile("rb")` → `readline()` length line → `read(body_len)` pattern from
  `query_via_server` (L105-120). The server `status` op must emit its response
  with the same `<decimal-byte-length>\n<json-body>` framing the `query` op uses
  so the client can read it exactly. Do **not** invent a new recv loop and do
  **not** bound/cap the rendered queue payload — read it in full.
- Render the enriched coalesce context, not the bare integer: print
  `coalesced: <total> (superseded_by_index=<n>, collapsed_into_pending_reindex=<n>,
  reindex_dropped_index_pending=<n>)` and, when `last_coalesce_at` is non-null, a
  `last coalesce: <reason> at <human time>` line (format the epoch float via the
  existing timestamp-rendering helper in `mcp.py` if one exists; otherwise
  `datetime.fromtimestamp`). When nothing has coalesced, print `coalesced: 0` and
  omit the last-coalesce line.
- Acceptance: `mcp status` prints the `writer_queue` block including the
  per-reason breakdown and (when present) the last-coalesce reason + time; the
  printed total equals the sum of the per-reason numbers; output is not truncated
  for a queue of ≥5 pending entries.

**Step 4.3**: CLI attach progress for queued `index`/`reindex`.
- Files: [`src/sqlcg/cli/commands/index.py`](../src/sqlcg/cli/commands/index.py),
  [`src/sqlcg/cli/commands/reindex.py`](../src/sqlcg/cli/commands/reindex.py).
- When queued behind other work, print `queued behind: <op> (done/total files) —
  position N`, then attach to the live progress frames once it is the active
  drain. Reuse the existing `rich.Progress` bar from `_run_index`.
- **W5 — recv loop keys off the `done:true` sentinel.** The client reads
  length-prefixed frames in a loop and treats the frame with `"done": true` as the
  terminal frame (it then stops); progress frames carry `"done": false` +
  `{files_done, files_total}`. The loop does **not** terminate on EOF and does not
  guess from `ok`. On a terminal frame with `ok:false` (drain failed — W7), print
  the relayed error and exit non-zero (never the raw Kuzu string).
- Acceptance: with a live drain in progress, a second `sqlcg index .` prints the
  "queued behind … position N" line, then a progress bar; final summary matches
  the direct-index summary fields.

**Step 4.4**: Server lifecycle + escalation-failure log lines.
- Files: [`src/sqlcg/server/writer.py`](../src/sqlcg/server/writer.py) (enqueue /
  coalesce / drain / escalation) using the existing `getLogger`.
- Emit the routine lifecycle trail at INFO/DEBUG — `enqueued`,
  `coalesced (reason=<reason-constant>)`, `started`, `drained` — the audit trail
  for the fire-and-forget hook case (findings §"Observability" (3)). The
  `reason=` value is one of the three reason constants from the Data Models
  section (single source of truth shared with `status` and metrics).
- Emit the **escalation-failure** line at **ERROR** on the C2-exhaustion path in
  `escalate_to_rw` (defined in the Retry/backoff design section), just before the
  RO reopen + `EscalationLockError` raise. Stable greppable prefix
  `escalation failed:`; it names the attempt count, the elapsed budget
  (`_ESCALATION_RETRY_BUDGET_S`), the `_find_lock_holder` PID hint, and the
  `db_path`. This is the operator-facing signal that escalation genuinely could
  not proceed and the graph was not updated — distinct in level (ERROR) from the
  routine coalesce/drain lines so it is filterable.
- Acceptance:
  - a coalesced reindex emits an INFO log line naming the reason constant
    (e.g. `coalesced (reason=collapsed_into_pending_reindex)`);
  - when `escalate_to_rw` exhausts its retry budget (driven deterministically by
    the injected always-failing opener from Step 1.2 / Test Strategy), exactly one
    ERROR log line is emitted with the `escalation failed:` prefix, the attempt
    count, and the PID hint, and it is emitted **before** the `EscalationLockError`
    is raised. Assert the captured log record (level + message substrings) via
    `caplog`, not just that an exception was raised.

**Step 4.5**: Record queue/drain metrics to `MetricsStore`.
- Files: [`src/sqlcg/metrics/store.py`](../src/sqlcg/metrics/store.py) (new table +
  `record_*` method), [`src/sqlcg/server/writer.py`](../src/sqlcg/server/writer.py)
  (call sites), [`src/sqlcg/server/tools.py`](../src/sqlcg/server/tools.py) (pass
  the `_metrics` singleton into the `WriterQueue`/drain).
- **Why**: today the queue emits only ephemeral stdlib `getLogger` lines (Step
  4.4) — nothing queryable/historical. `MetricsStore` (SQLite, already
  initialized in `tools.init_backend` as the module-level `_metrics` singleton at
  `Path.home()/".sqlcg"/"metrics.db"`) is where `tool_calls` and `index_runs`
  already live; queue behaviour belongs there too.
- **Concrete schema addition** — a new append-only table mirroring the existing
  `record_index_run` / `record_tool_call` style (best-effort, `try/except` +
  WARNING, never raises; honours `SQLCG_METRICS=0` via the existing `_enabled`
  guard):
  - `CREATE TABLE IF NOT EXISTS writer_queue_events (id INTEGER PRIMARY KEY
    AUTOINCREMENT, timestamp TEXT NOT NULL, event TEXT NOT NULL, op TEXT,
    reason TEXT, queue_depth INTEGER, duration_ms REAL)` — added to the existing
    idempotent `init_schema()` alongside the current three `CREATE TABLE IF NOT
    EXISTS` statements.
  - `event` ∈ `{"enqueued", "coalesced", "drained"}`. For `enqueued`:
    `op` + `queue_depth` (depth at enqueue time, after this request is added) set,
    `reason`/`duration_ms` null. For `coalesced`: `reason` (one of the three
    reason constants) set, `queue_depth` = pending depth at coalesce time. For
    `drained`: `op` + `duration_ms` (wall-clock drain duration, monotonic delta)
    set.
  - New method `record_queue_event(self, event: str, *, op: str | None = None,
    reason: str | None = None, queue_depth: int | None = None,
    duration_ms: float | None = None) -> None` following `record_index_run`
    exactly (timestamp via `datetime.now(UTC).isoformat()`, single INSERT +
    commit, `try/except sqlite3.Error` → WARNING).
- **Migration / SCHEMA_VERSION rule**: `MetricsStore` has **no** versioning
  mechanism today (no `PRAGMA user_version`, no migration code — confirmed in
  [`store.py`](../src/sqlcg/metrics/store.py)); it relies entirely on idempotent
  `CREATE TABLE IF NOT EXISTS`. The new table follows the same pattern: an old
  metrics.db simply gains the table on next `init_schema()`. No backward-compat
  shim, consistent with CLAUDE.md ("re-index is the migration path; no
  backward-compatibility"). Do **not** introduce a version column or migration
  framework for this one table — that would be a new convention this codebase does
  not have (OD-6, resolved: keep `MetricsStore` version-less).
- **Call sites** (grep-confirmed before PR, CLAUDE.md): `record_queue_event` is
  invoked from `WriterQueue._record_coalesce` (coalesced), `enqueue` (enqueued),
  and the drain task after `de_escalate_to_ro` (drained, with the measured
  duration). The `WriterQueue`/drain receives the metrics store by dependency
  injection from `tools` (pass `_metrics`), defaulting to `None` so pure
  `WriterQueue` unit tests need no SQLite — when `None`, the calls are skipped
  (the store is best-effort anyway).
- Acceptance: an integration test with metrics enabled enqueues work that drains
  and coalesces, then `MetricsStore.execute_query("SELECT event, op, reason,
  queue_depth, duration_ms FROM writer_queue_events ...")` returns rows asserting:
  an `enqueued` row with the correct `queue_depth`, a `coalesced` row with the
  matching `reason` constant, and a `drained` row with `duration_ms > 0`. Assert
  the persisted rows (observable output), not merely that the method was called.
  A second test with `SQLCG_METRICS=0` asserts the queue still drains and no rows
  are written (best-effort opt-out preserved).

### Phase 5: Version bump + docs

**Step 5.1**: Bump version to 1.3.0 (additive capability → minor, CLAUDE.md SemVer).
- Files: [`pyproject.toml`](../pyproject.toml),
  [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py), then `uv lock`.
- Acceptance: `sqlcg --version` reports 1.3.0; lockfile updated.

**Step 5.2**: Regenerate `docs/cli.md` (new `--detach` flag on `index`; changed
`reindex`).
- Files: `docs/cli.md`.
- Acceptance: stale-docs CI check passes.

**Step 5.3**: Update ARCHITECTURE_REVIEW.md §17 (escalation now implemented;
move the line-3029 deferred item to done) — via the architect-reviewer, not in
this plan's PR if it is large.

---

## Test Strategy

The hard part is testing the **lock-escalation behaviour deterministically**
(Finding 2 says the real reopen fails in ~4 ms, no retry — wall-clock and
real-process races are flaky). Strategy:

- **Escalation retry (unit, deterministic)**: inject the `opener` into
  `escalate_to_rw` (a callable, default = real `KuzuBackend`) **and** the
  `retry_budget_s` parameter. The test passes a fake opener that raises the lock
  `RuntimeError` a fixed K times then returns a stub, and a short `retry_budget_s`.
  Assert: succeeds for K within budget; raises `EscalationLockError` with the
  side-DB workaround **text** when K is unbounded. **The budget override is passed
  via the `retry_budget_s` parameter, NOT a global module patch** (parallel-runner
  safe — two tests running concurrently must not stomp a shared module constant).
  No wall-clock dependence — count attempts, not seconds (mirror the
  deterministic-op-count discipline of `test_perf_scaling_guard.py`).
- **Coalescing rules (unit, `@pytest.mark.anyio`)**: `WriterQueue` tests, no DB,
  no sockets — assert observable queue state after each enqueue sequence
  (rules 1–4 + active exemption). Marked anyio because `enqueue`/`pop_next` take
  the queue's `anyio.Lock` (W4); `coalesce_view()` is read synchronously.
- **Fresh-DB schema-ensure (integration)**: start a server against a temp path
  that does **not** yet exist; assert it does not raise `Cannot create an empty
  database under READ ONLY mode.` and that a routed read then returns rows (empty
  `Repo` count is fine). Guards the OD-2 RW-ensure→reopen-RO sequence in Step 1.3.
- **RO-by-default coexistence (integration)**: start a server on a temp DB; from
  the same test process open a second `KuzuBackend(path, read_only=True)` and
  assert it returns rows (RO+RO, the 1.1.2-verified case); also assert a
  concurrent in-process RW open is blocked while the server holds its shared RO
  lock (the serving connection really is RO). This guards Step 1.3.
- **Startup schema-version gate (integration, deterministic)**: seed a temp DB
  whose stored `SCHEMA_VERSION` is an old value (e.g. open RW, `init_schema()`,
  then overwrite the `SchemaVersion` node to `"5"` while the current build is
  `"6"`), then start the server against it and assert it **exits non-zero** with a
  message containing both `v5` and `v6` and the `sqlcg db reset && sqlcg index`
  remedy substring. A paired positive case seeds a current-version DB and asserts
  the server starts and a routed read returns rows. Fully deterministic — the
  seeded version drives the branch, no wall-clock/race. Guards Step 1.4.
- **Reads block during a drain (integration, deterministic ordering)**: the drain
  must provably be in-flight when the read arrives — **do not guess with a
  `sleep`**. Inject an `anyio.Event` the drain `await`s *while holding
  `backend_lock`* (after escalate, before releasing the lock). The test: start the
  drain (it blocks on the event mid-drain, lock held), issue a routed `query`
  (which must block on `backend_lock`), assert the query has **not** returned, then
  set the event to let the drain finish, and assert the query returns **after** the
  drain completes (served on the single connection) — pinning the OD-1 blocking
  semantics by ordering, not timing. The same injected `anyio.Event` is the
  mechanism the B2 stop-mid-drain test (Step 2.2) uses to hold the drain open
  deterministically.
- **Escalation transition (integration, real Kuzu, single process)**: open RO,
  then call `escalate_to_rw` with **no competing opener** → succeeds; open a
  second RO holder in-process, then call `escalate_to_rw` → with retry budget
  exhausted, asserts `EscalationLockError` (this exercises the real
  `Could not set lock` path deterministically because the in-process RO holder
  is held for the whole budget). Mark with a short budget override so the test is
  fast.
- **Drain runs the indexer once (integration)**: enqueue a reindex; assert
  `resync_changed` called exactly once and the backend is RO again afterward.
  Extend the existing [`test_reindex_via_server.py`](../tests/integration/test_reindex_via_server.py)
  Scenario A pattern.
- **Inline-reindex retired / no pre-lock backend (unit, structural — B1)**: assert
  the shipped `reindex` op body does not capture `backend_ref()` before
  `backend_lock` — a spy on `backend_ref` confirms the `reindex` branch never calls
  it (the drain is the only consumer, under the lock). Mirrors the perf-invariant
  guard discipline (a code-level guard, not a timing test).
- **Stop mid-drain (integration — B2)**: with the injected drain-hold
  `anyio.Event`, fire the stop/SIGTERM path while a drain holds `backend_lock`;
  assert the in-flight write commits (post-restart graph reflects it) and
  `tools._backend is None` afterward (no reopen-after-shutdown). Deterministic via
  the event, not a sleep.
- **Standalone reindex `from=null` (integration — W3)**: send a `reindex` op with
  `from` omitted against a server whose DB has a stored indexed SHA; assert the
  drain calls `resync_changed` with `from = stored SHA` and `to = current HEAD`
  (assert the args). A paired never-indexed DB returns the actionable "no prior
  index" error (assert the message).
- **Terminal-frame sentinel (integration — W5)**: a `wait=true` `index`/`reindex`
  streams ≥1 frame with `done:false` then exactly one `done:true` terminal frame;
  the client stops on `done:true`, not EOF. On a forced drain failure the terminal
  frame carries `ok:false, done:true, error:<msg>` (W7) and the CLI exits non-zero.
- **Manual index routes through socket (integration)**: with a live server,
  `index_cmd` must not trigger a direct RW open. Assert via no "Database is
  locked" and the server's `writer_queue` showing the enqueued index.
- **No-server fallback (e2e)**: `sqlcg index .` with no server still indexes
  (zero-config small-repo invariant) — assert observable summary output.
- **`db reset` refusal (integration)**: with a live server, `sqlcg db reset` exits
  non-zero, prints the "stop the server first (`sqlcg mcp stop`)" message, and the
  graph still returns rows afterward (not reset); with no server it resets
  normally (OD-3, Step 3.4).
- **Framed `status` recv (integration)**: enqueue ≥5 pending entries, then assert
  `mcp status` reads the full framed response (no truncation) — exercises the
  OD-4 recv-exactly path on a payload that exceeds the old 4096-byte single recv.
- **status `writer_queue` (integration)**: assert the shape/contents, including
  the per-reason `coalesced_by_reason` breakdown, `last_coalesce_reason`, and
  `last_coalesce_at` (Step 4.1) — `coalesced_since_start` must equal the sum of
  the per-reason counts.
- **Escalation-failure log line (unit, `caplog`)**: drive `escalate_to_rw` with
  the always-failing injected opener (same fixture as the retry test); assert
  exactly one ERROR record with the `escalation failed:` prefix carrying the
  attempt count + PID hint, emitted before `EscalationLockError` (Step 4.4).
- **Queue metrics persisted (integration)**: with metrics enabled, drain +
  coalesce, then query `writer_queue_events` and assert the `enqueued` /
  `coalesced` / `drained` rows with expected `queue_depth`, `reason`, and
  `duration_ms > 0` (Step 4.5). A paired `SQLCG_METRICS=0` test asserts the drain
  still completes and no rows are written.
- **Perf guards untouched**: `test_perf_scaling_guard.py`,
  `test_upsert_batch_invariant.py`, `test_bulk_upsert_invariant.py`,
  `test_T09_01_qualify_once.py` must stay green (no `base.py`/`indexer.py` edits).

---

## Acceptance Criteria

- [ ] Server started against a **non-existent** `db_path` initializes the schema
      via a bounded RW open then serves RO (no `Cannot create an empty database
      under READ ONLY mode.`); a routed read returns rows afterward (OD-2).
- [ ] Server startup **refuses to start** (exits non-zero) when the stored
      `SCHEMA_VERSION` is present and differs from the current build's, with a
      message naming **both** versions and the `sqlcg db reset && sqlcg index <path>`
      remedy; a current-version DB starts normally; a fresh DB initializes and
      starts (Step 1.4 — no auto-migration; re-index is the migration path).
- [ ] The startup RW-ensure window **creates the schema on a fresh DB and is a
      no-op on an already-initialized DB — it does NOT migrate** (OD-2).
- [ ] While serving RO (no drain in flight), the server rejects **no** reads: a
      concurrent RO open / routed CLI read succeeds and returns rows; a concurrent
      RW open is blocked (confirming the serving connection is RO).
- [ ] During a drain, a `query` op **blocks until the drain finishes** and is then
      served on the single connection (OD-1 — reads queue, no second connection,
      no read-during-write path in 1.3); `mcp status` wording reflects this
      blocking behavior.
- [ ] `sqlcg index .` against an attached server **does not** raise
      `Database is locked`; it enqueues through the socket and attaches to
      progress (the exact 1.2.2 reproduction in findings line 221 is resolved).
- [ ] Manual `index`/`reindex` attach-and-wait by default; `--detach` returns
      immediately; the git hook is always fire-and-forget (never blocks a
      `git checkout`).
- [ ] Coalescing: a full `index` supersedes pending reindexes; N reindexes
      collapse to one executed against HEAD-at-drain-time; reindex-behind-index
      is dropped — all visible in `mcp status` `writer_queue` and server logs
      (not silently lost).
- [ ] Drain escalates RO→RW only for its duration and de-escalates in a
      `finally`; the write lock is never held between drains.
- [ ] **No op operates on a `backend_ref()` resolved outside `backend_lock` once
      escalation exists** (B1): the inline `reindex` op is retired (enqueues; the
      drain is the only backend consumer, under the lock), and this lands in the
      **same slice** as the escalation primitive (no co-existence window).
- [ ] **A stop/SIGTERM mid-drain** lets the in-flight write commit, then tears down
      once: `de_escalate_to_ro` does **not** reopen RO after `shutdown_backend()`
      nulled `tools._backend` (B2 — `tools._backend is None` after shutdown).
- [ ] **Standalone manual `reindex` (no `--from`) works through the socket** (W3):
      the server resolves the base SHA from its RO connection
      (`get_indexed_sha()`) at drain start (`from`=stored SHA, `to`=HEAD); the old
      "--notify without --from requires direct DB access" refusal is gone; a
      never-indexed DB returns an actionable error, not a crash.
- [ ] **A drain whose indexer body raises** (non-escalation error) clears
      `WriterQueue._active`, sets a terminal/failed progress (no phantom
      forever-running drain in `status`), de-escalates to RO, sends a
      `ok:false,done:true` terminal frame to a `wait=true` client, and the drain
      loop survives to process the next request (W7).
- [ ] **`mcp status` framing change is atomic** (B3): the server framing of the
      `status` response and the client recv-exactly parse ship in **one commit**;
      the `stop` op stays unframed (`mcp_stop`'s `s.recv(128)` untouched).
- [ ] RW reopen retries within a bounded budget; one transient out-of-band reader
      does not abort a reindex (C2).
- [ ] On budget exhaustion, the user sees the clear message with the
      `SQLCG_DB_PATH=/tmp/sqlcg-cli/graph.db` workaround and the PID hint — never
      the raw `Could not set lock on file` (C3); **and** the server emits exactly
      one ERROR log line (`escalation failed:` prefix, attempt count, PID hint)
      on that same path before the error is raised (Step 4.4).
- [ ] `KuzuBackend.__init__` docstring states RO takes a **shared** lock that
      blocks RW.
- [ ] `mcp status` renders `writer_queue` with the enriched coalesce context
      (`coalesced_since_start` + per-reason `coalesced_by_reason` +
      `last_coalesce_reason`/`last_coalesce_at`), not a bare integer, without
      truncation for ≥5 pending entries.
- [ ] Queue/drain behaviour is recorded to `MetricsStore.writer_queue_events`
      (queue depth at enqueue, coalesce events with reason, drain duration);
      `SQLCG_METRICS=0` suppresses the rows without affecting the drain (Step 4.5).
      The table is added via plain `CREATE TABLE IF NOT EXISTS` with no version
      column or migration framework (OD-6 — `MetricsStore` stays version-less).
- [ ] `sqlcg db reset` against a **live** server refuses with a clear "stop the
      server first (`sqlcg mcp stop`)" message and exits non-zero without resetting
      the graph (OD-3); with no server it resets as before.
- [ ] `mcp status` reads its response via the framed recv-exactly path
      (`read_client` `makefile`+`readline`+`read(n)`) and is not truncated for a
      large `writer_queue` (OD-4).
- [ ] No edits to `parsers/base.py` or `indexer.py` hot paths; all perf-invariant
      guard tests stay green.
- [ ] Every new method has a grep-confirmed call site before the PR opens.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Close→reopen race: a reader grabs the shared lock in the sub-ms window between RO close and RW open | C2 bounded retry/backoff (Step 1.2) covers exactly this interleave (findings Finding 2 + constraint 2). |
| An out-of-band `SQLCG_DB_PATH` pointing at the *same* DB defeats escalation | C3 message names the side-DB workaround as a *different* path; retry budget tolerates a transient holder; persistent holder → clear error, graph not updated (not a crash). |
| Half-closed backend observed by a `query` op mid-escalation | Escalate/de-escalate + `tools._backend` swap happen inside `backend_lock`; `query` ops already serialize on that lock. |
| Multi-frame `wait=true` protocol mis-parsed by the framed reader | Reuse the recv-exactly framing from `read_client`; document the multi-frame extension; test the frame sequence (Step 3.1). |
| Drain task crash leaves backend RW (lock held) | `de_escalate_to_ro` in `finally`; on unexpected drain-task exception (W7), the handler clears `WriterQueue._active`, sets a terminal/failed progress (no phantom running drain in `status`), force-reopens RO, sends a `ok:false,done:true` terminal frame, logs at ERROR, and the loop survives for the next request; server `finally`/`_stop_watcher` still calls `shutdown_backend`. |
| Stop/SIGTERM mid-drain reopens RO after `shutdown_backend()` nulled `tools._backend` (dangling backend) | B2: `_stop_watcher` acquires `backend_lock` before `shutdown_backend()` (waits for the active drain's in-flight write to commit + de-escalate), and `de_escalate_to_ro` skips the RO reopen when `_shutdown_requested` is set. Net: tear down once, never reopen after shutdown. |
| Manual `reindex` with no `--from` cannot resolve the base SHA while a server holds the lock | W3: the **server** resolves it from its RO connection (`get_indexed_sha()` is a metadata read, feasible on RO) at drain start; the client sends `from=null`. The old direct-DB-access refusal is removed for the live-server path. |
| `init_schema()` on an RO backend issues DDL and fails (measured on kuzu 0.11.3) | Step 1.3 ensures the schema via a bounded RW open at startup (the shared escalation primitive), then reopens RO; `init_schema()` is never called on the RO connection. The RW open creates the schema on a fresh DB and is a no-op on an existing one — it does NOT migrate. |
| `init_schema()` silently no-ops on a stale-schema DB, so an old graph (e.g. v5 under v6) is served RO with no warning (trust bug) | Step 1.4 startup schema-version gate reads `get_schema_version()` after create-if-absent and **refuses to start** (non-zero, names both versions + `sqlcg db reset && sqlcg index` remedy) when it differs from the current `SCHEMA_VERSION`. No auto-migration. |
| `mcp status` 4096-byte single recv truncates a large queue | Step 4.2 + 4.1 use the framed recv-exactly read/response already in `read_client.py` (`makefile`+`readline`+`read(n)`); the status response is framed so it is read in full. |
| Startup RW schema-ensure window collides with an out-of-band holder | The startup ensure reuses `escalate_to_rw`'s C2 bounded retry/backoff; on exhaustion it surfaces the same clear C3 message (server fails to start with the side-DB workaround), never a raw lock error. |
| `MetricsStore` SQLite write inside the drain task stalls/raises and blocks the writer | `record_queue_event` is best-effort (try/except → WARNING, never raises) like every existing `MetricsStore` write; it runs off the hot path and `None` injection skips it entirely in unit tests. The escalation-failure ERROR log (Step 4.4) is the *required* operator signal and uses stdlib logging, not SQLite — metrics are the historical/queryable supplement, not the critical path. |
| Coalesce reason strings drift between `status`, logs, and metrics | All three read the same module-level reason constants in `server/writer.py`; `_record_coalesce` is the single choke point that updates the counter, logs, and records the metric. |

### Rollout / rollback

- Ships in **1.3.0**. No data migration (queue is in-memory; graph schema
  unchanged). Rollback = reinstall the prior wheel; re-index is the migration
  path (CLAUDE.md "no backward compatibility").
- The socket wire protocol is internal between matching builds; an old client
  never sends the new `index` op, so no version-skew shim is needed.

---

## Resolved Decisions

All previously-open decisions are resolved and baked into the body above; the
**open-decisions list is empty** (the plan-review findings B1/B2/B3/W1–W7 were all
resolvable from the source — no new undecided design choice was surfaced).
Recorded here for traceability.

- **OD-7 (B1 — inline `reindex` op + escalation co-existence) — RESOLVED: retire
  the inline op in the same slice as escalation.** Once escalation can swap
  `tools._backend` under `backend_lock`, the existing `reindex` op (which resolves
  `db = backend_ref()` *before* the lock, `server.py` L235) is unsafe. Resolution:
  Step 2.3 retires that op (it enqueues; the drain is the only backend consumer,
  resolving it under the lock) and **lands together with** the Phase 2 escalation
  primitive (Step 2.2) — no commit window where both coexist. Invariant stated in
  "Escalation mechanism". Re-sequencing: a **new Step 2.3** was added to Phase 2,
  and Phase 3's Step 3.3 was narrowed to the CLI half (the server-side retirement
  moved up to Phase 2). Baked into: Escalation-mechanism invariant box, Step 2.2,
  Step 2.3 (new), Step 3.1/3.3, acceptance criteria, B1 structural test.

- **B2 (stop mid-drain race) — RESOLVED: wait-for-drain + shutdown-flag, both.**
  `_stop_watcher` acquires `backend_lock` before `shutdown_backend()` (lets the
  in-flight RW write commit and de-escalate), and `de_escalate_to_ro` skips the RO
  reopen when `_shutdown_requested` is set. Primary guarantee (a) chosen because
  closing the RW connection mid-write risks a torn graph write. Baked into:
  "Shutdown vs. drain ordering", `de_escalate_to_ro` contract, Step 2.2, acceptance
  criteria, stop-mid-drain test.

- **B3 (status framing atomicity) — RESOLVED: Steps 4.1+4.2 one commit; `stop`
  stays unframed.** The server-side `status` framing and the client recv-exactly
  parse ship together so `mcp status` is never broken mid-rollout; `mcp_stop`'s
  `s.recv(128)` and the unframed `stop` reply are untouched. Baked into: Step 4.1,
  Step 4.2, acceptance criteria.

- **W3 (standalone `reindex` base-SHA over the socket) — RESOLVED: server resolves
  the base SHA on its RO connection at drain start.** `get_indexed_sha()` is a
  metadata read feasible on the RO serving connection; the client sends
  `from=null` and the drain substitutes stored-SHA→HEAD (rule 2). Removes the old
  "--notify without --from requires direct DB access" refusal for the live-server
  path; a never-indexed DB returns an actionable error. Baked into: `index`/
  `reindex` op definitions, Step 3.1, Step 3.3, acceptance criteria, W3 test.

- **W1/W4/W5/W6/W7 (should-fix)** — resolved in the body: `escalate_to_rw` one
  signature with injectable `current`/`opener`/`retry_budget_s` (W1); anyio-marked
  queue unit tests (W4); `done:true` terminal-frame sentinel (W5); promote
  `find_lock_holder` to public (W6); drain-exception handler clears `_active` +
  terminal/failed progress + `ok:false,done:true` terminal frame (W7). W2 extends
  the Step 3.4 server-probe guard to the `db reset --repo` path.

- **OD-1 (reads during a drain) — RESOLVED: block reads during the drain.**
  The drain holds the single `backend_lock` across escalate→run→de-escalate;
  `query` ops arriving mid-drain **queue/block on that lock until the drain
  finishes** and are then served on the single connection. No second connection
  and no concurrent read-during-write path in 1.3. The findings doc's "reads stay
  available during a drain" aspiration is **scoped OUT for 1.3** and noted as a
  possible future optimization (dedicated read connection on the same `Database`).
  Rationale: incremental reindex is fast (seconds) so the pause is imperceptible
  in the common case; only a full large-repo reindex makes readers wait
  (~minutes), and the single-connection model avoids Kuzu's per-connection
  thread-safety hazard (ARCHITECTURE_REVIEW 17.2 / finding 3.3). Baked into:
  "The invariant this whole design rests on", Step 2.2, acceptance criteria,
  "reads block during a drain" test.

- **OD-2 (schema init on RO open) — RESOLVED empirically (kuzu 0.11.3), reshapes
  Phase 1.** Measured: a RO open of a non-existent/empty DB fails with
  `Cannot create an empty database under READ ONLY mode.`, and any DDL on a RO
  connection fails with `Cannot execute write operations in a read-only
  database!`. The server therefore **cannot** open RO and call `init_schema()`
  (which issues DDL on its fall-through). Resolution: `init_backend` does a brief
  **RW open → create schema if absent → schema-version gate → close → reopen RO**
  for serving, reusing the same close→reopen escalation primitive as a drain;
  `init_schema()` runs only on the RW open. **This window does NOT migrate**:
  verified against [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) L90-140,
  `init_schema()` probes `MATCH (n:Repo)` and returns immediately if the `Repo`
  table exists — it runs DDL **only** on a fresh DB and never compares or upgrades
  the stored `SCHEMA_VERSION`. The existing `MATCH (n:Repo)` probe is safe on the
  RO connection only once the schema exists — it must never create it. Because that
  no-op silently serves a stale-schema DB, a **startup schema-version gate**
  (Step 1.4) reads `get_schema_version()` after the create-if-absent step and
  **refuses to start** when the stored version differs from the current
  `SCHEMA_VERSION` — no auto-migration, consistent with CLAUDE.md "No backward
  compatibility. Re-index is the migration path." Baked into: Summary, Scope,
  "Escalation mechanism", Step 1.2, Step 1.3 (rewritten), Step 1.4 (new gate),
  fresh-DB + stale-schema acceptance + tests, risks table.

- **OD-3 (`db reset` with a live server) — RESOLVED: clear refusal.** `db reset`
  against a live server refuses with a message directing the user to stop the
  server (`sqlcg mcp stop`) or use the control socket, exits non-zero, and does
  not touch the graph — never silently fights the lock or prints a raw lock error.
  Baked into: Non-Goals, Step 3.4 (new), acceptance criteria, `db reset` refusal
  test.

- **OD-4 (`mcp status` recv size) — RESOLVED: reuse the framed recv-exactly
  helper.** `status` reuses the length-prefixed `makefile`+`readline`+`read(n)`
  recv-exactly read already implemented in
  [`read_client.py`](../src/sqlcg/server/read_client.py); the server frames the
  `status` response the same way the `query` op does. No new recv loop, no
  bounding of the rendered queue. Baked into: Step 4.1 (frame the response),
  Step 4.2, acceptance criteria, framed-status test, risks table.

- **OD-6 (metrics schema versioning) — RESOLVED: keep `MetricsStore`
  version-less.** `MetricsStore` has no versioning today (only idempotent
  `CREATE TABLE IF NOT EXISTS`). The new `writer_queue_events` table follows that
  pattern with plain `CREATE TABLE IF NOT EXISTS`; no `PRAGMA user_version` /
  migration framework is introduced. Baked into: Step 4.5 (already the
  recommendation), acceptance criteria.
