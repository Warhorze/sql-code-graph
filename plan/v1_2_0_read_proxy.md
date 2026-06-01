# Feature Plan: v1.2.0 — Server-Routed Read Proxy (closes remaining half of #28)

## Summary

Eliminate "Database is locked" failures on CLI **read** commands while the live MCP
server holds the KuzuDB process-level write lock. CLI read commands gain a thin client
that, when a server is live on the target DB, routes the read query over the existing
Unix control socket (`<db>.sock`) and runs it on the server's single Kuzu connection.
When no server is live, behaviour is byte-for-byte unchanged (`get_backend(read_only=True)`).

This completes GitHub issue **#28** (single-writer lock). The **write** half shipped in
v1.1.0 (control socket + `reindex --notify`); this is the **read** half.

---

## Scope

### In Scope

- A new control-socket op `{"op": "query", "cypher": ..., "params": ...}` handled in
  [`_control_socket_task`](../src/sqlcg/server/server.py) (server.py:119 `_handle_connection`),
  serialised on the **same** `reindex_lock` and the **same** single Kuzu connection
  (`backend_ref()`), returning rows as JSON.
- A **length-prefixed framing** protocol for the `query` op only (request and response),
  because read result sets routinely exceed the existing `recv(65536)` single-buffer read
  used by `status`/`reindex`. The existing ops keep their current single-`recv` framing
  (their responses are tiny and bounded); only the new `query` op uses framing.
- A reusable client helper module — factor the inline socket-connect / timeout / fallback
  logic currently duplicated in [`reindex.py`](../src/sqlcg/cli/commands/reindex.py)
  (reindex.py:85-153) and [`mcp.py`](../src/sqlcg/cli/commands/mcp.py) (mcp.py:96-118) into
  a single helper. CLI read commands call it.
- Wiring the helper into the read commands that currently open `get_backend(read_only=True)`:
  - [`find.py`](../src/sqlcg/cli/commands/find.py) — 3 sites (find.py:21, 45, 64)
  - [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) — 5 sites (analyze.py:67, 126, 194, 230, 247)
  - [`db.py`](../src/sqlcg/cli/commands/db.py) `db info` (db.py:78) **and** `list-repos` (db.py:170)
  - [`gain.py`](../src/sqlcg/cli/commands/gain.py) — 1 site (gain.py:126)
- Update the `get_backend` docstring "future work" note (config.py:362-363) to reference the
  shipped read-proxy.

### Non-Goals

- **Windows**: no control channel exists on Windows today (the control-socket task is only
  started on `sys.platform != "win32"`, server.py:304). Reads on Windows continue to fall
  back to direct `get_backend(read_only=True)` and **may still lock** while a writer holds it.
  This is an explicit, documented non-goal — unchanged from the v1.1.0 write half.
- **KuzuDB lock lease / reader-writer split**: rejected. Confirmed still true —
  `ARCHITECTURE_REVIEW.md:2872` records "no lock lease — verified no safe KuzuDB API".
  Do not reintroduce.
- **MCP server's own tool reads**: no change. The MCP tools already use the in-process
  backend singleton (`_tools._backend`); they never open a second connection and never
  contend for the lock. (Grep-confirmed: tools route through `init_backend`/`_backend`,
  same object the `query` op uses via `backend_ref()`.) Out of scope.
- **`reindex --notify` without `--from` standalone write gap**: the post-merge/post-checkout
  hook fallback path where `--notify` is given without `--from` still falls through to a
  direct write (reindex.py:96-100) and can hit lock contention. This is a **write**-path
  gap, not a read-path gap. **Deferred** — out of scope here. It does not block the read
  half and is tracked separately. (Rationale: the read proxy uses a read op on the same
  connection; the residual write gap needs a server-side "resolve stored SHA then resync"
  op, which is a distinct write-side change.)
- **No new Cypher capability**: the proxy executes the *same* Cypher the read commands
  already build; it does not add a query DSL or new graph queries.
- **Neo4j backend**: has no single-writer lock (config.py:399); the proxy is a Kuzu-only
  concern. With `SQLCG_BACKEND=neo4j`, read commands take the direct path unchanged.

---

## Design

### Decision: generic `query` op (raw Cypher over the socket)

**Chosen**: a single generic op that carries the Cypher string and params, returns rows.

**Why over a fixed set of named read ops:**

1. **The read surface is raw `run_read(query, params)` everywhere.** Every one of the 11
   call sites builds a Cypher string locally and calls `backend.run_read(query, params)`,
   then post-processes rows in Python (NoiseFilter in find.py:28-34, freshness/counts in
   db.py, dict comprehension in gain.py:133). A named-op design would require porting each
   of these queries server-side and re-deriving the post-processing split — large surface,
   high regression risk, and it duplicates query strings across client and server.
2. **A generic op keeps the client/server contract tiny**: client sends Cypher+params,
   server returns rows; all post-processing (NoiseFilter, table rendering) stays exactly
   where it is today, operating on the returned rows. The only thing that changes is *where
   the rows come from*.
3. **Injection surface is acceptable**: the socket is `0o600` owner-only and local-only
   (server.py:117 `sp.chmod(0o600)`). The threat model is the same as the already-shipped
   `reindex` op, which already accepts an arbitrary `root` path and runs an indexer. A
   local user who can write the socket can already run `sqlcg` directly against the DB.
   No new trust boundary is crossed. **However**, the server MUST execute the op on the
   read path only — see the read-only guard below.

**Rejected — fixed named read ops** (`find_table`, `trace_lineage`, …): rejected for the
duplication and regression-surface reasons above. Documented here so it is not re-litigated.

#### Read-only guard (server side)

`run_read` (kuzu_backend.py:300) calls `self._conn.execute(query, params)` — the same
primitive `run_write` uses. A generic `query` op that called `run_read` with an arbitrary
string could therefore execute a mutation (`CREATE`/`DELETE`/`MERGE`/`SET`) on the live
connection. To keep the op honestly read-only, the server handler MUST reject statements
whose first significant keyword is a write keyword before executing. Recommendation:
a small allow-list check — the leading keyword (after stripping whitespace/comments) must
be `MATCH`, `RETURN`, `WITH`, `CALL`, `UNWIND`, or `OPTIONAL`. Reject otherwise with
`{"error": "query op is read-only"}`. This is a guard, not a security boundary (the socket
is already trusted), but it prevents an accidental mutation from a mis-built client query
and documents intent. Test asserts a `CREATE` op is rejected.

### Decision: length-prefixed framing for the `query` op

The existing ops read a single `recv(65536)` (reindex.py:113, mcp.py:101). Read result sets
can exceed 64 KiB (e.g. `find pattern` returns up to 50 rows of full SQL `q.sql`; a wide
lineage trace returns 100 rows). A single `recv` would **silently truncate** and produce
invalid JSON or a short result — a correctness bug, not just a perf issue.

**Chosen framing**: newline-delimited length prefix — `b"<decimal-byte-length>\n" + <json-bytes>`
on both request and response for the `query` op. Reader loops until it has read exactly
`length` bytes. Simple, no external deps, symmetric for request and response.

- **Server**: `_handle_connection` must read the full framed request (loop on `receive`
  until the declared length is consumed) and write a framed response.
- **Client helper**: send framed request, read length line, then loop `recv` until the
  full body is read; `json.loads` the body.
- **Back-compat with existing ops**: `status`/`stop`/`reindex` keep their current
  unframed single-`recv` protocol. The server distinguishes by **trying to parse a leading
  `<int>\n` length prefix**; if the first line is not a bare integer it falls back to
  treating the buffer as a single unframed JSON request (existing behaviour). This keeps
  the v1.1.0 ops working unchanged and avoids a protocol-version flag.

  *Plan-reviewer note*: confirm the "leading integer line" sniff cannot collide with an
  unframed JSON request — unframed requests always start with `{`, never a digit, so the
  sniff is unambiguous. State this in the handler comment.

### Decision: serialise reads behind `reindex_lock` on the single connection

Kuzu's connection is not concurrency-safe; the server holds exactly one (`backend_ref()`).
The `reindex` op already serialises on `reindex_lock` (server.py:198). The `query` op MUST
acquire the same lock. **Rename is optional but recommended**: the lock now guards all
backend access, not just reindex — recommend renaming `reindex_lock` → `backend_lock`
(server.py:301 + the param in `_control_socket_task` server.py:80 + `_run_with_control`
server.py:312). If the rename adds churn the reviewer deems unnecessary, keep the name and
add a comment that it now also serialises reads.

**Cost analysis** (open question from brief, resolved):

- Reads queue behind an in-flight reindex on one connection. A measured DWH `resync_changed`
  ran ~89 s (reindex.py:23 comment); the notify client timeout is 300 s (`_NOTIFY_SOCKET_TIMEOUT_S`,
  reindex.py:25). A CLI read issued *during* a long resync would block up to the resync
  duration.
- **Acceptable**: the alternative today is a hard "Database is locked" failure — a blocked
  read that eventually returns rows is strictly better than an immediate error. Reindex is
  an occasional event (branch switch / pull), not steady-state. Reads outside a resync
  window acquire the lock immediately (reindex is the only other holder).
- **Bound it**: the read client MUST set a socket timeout (recommend reusing a named
  constant, e.g. `_QUERY_SOCKET_TIMEOUT_S`, **distinct** from the notify constant; suggest
  300 s to match the worst-case resync). On timeout, the client MUST **not** fall through to
  a direct open (that would hit the held write lock and reproduce the lock error — exactly
  the F1 timeout bug fixed in reindex.py:127-142). Instead surface a clear
  "server busy (reindex in progress); try again" message and exit non-zero. Test asserts this.

  *Rationale tie-in*: this mirrors the v1.1.0 F1 fix — a timeout means the server is alive
  and holding the lock, so falling back is wrong.

### Client helper API

New module: `src/sqlcg/server/read_client.py` (server package, alongside `control.py`).

```python
def server_is_live(db_path: Path | None = None) -> bool:
    """True if a server owns this DB's socket (connect succeeds)."""

def query_via_server(
    cypher: str,
    params: dict,
    db_path: Path | None = None,
    timeout_s: float = _QUERY_SOCKET_TIMEOUT_S,
) -> list[dict] | None:
    """Send a read query over the control socket.

    Returns rows (list[dict]) on success.
    Returns None when NO server is live (caller falls back to direct open).
    Raises a typer.Exit / dedicated error on server-busy timeout or server error
    (caller must NOT fall back — the lock is held).
    """
```

The read commands adopt this pattern (replacing each `with get_backend(read_only=True)`):

```python
rows = query_via_server(cypher, params)
if rows is None:
    with get_backend(read_only=True) as backend:
        rows = backend.run_read(cypher, params)
# ... existing post-processing on `rows` unchanged ...
```

To avoid 11 hand-rolled try/fallback blocks, provide a convenience wrapper:

```python
def run_read_routed(cypher: str, params: dict, db_path: Path | None = None) -> list[dict]:
    """Route through a live server if present, else direct read_only open.
    Single seam every CLI read command calls instead of building its own backend."""
```

`run_read_routed` is the **one** seam each command calls. This keeps the per-command diff to
a few lines and centralises the fallback semantics (None→direct, timeout→raise) in one place.

*Wiring note (CLAUDE.md "grep-confirmed call site")*: `run_read_routed`, `query_via_server`,
and `server_is_live` must each have a production call site before PR. `server_is_live` is
used by `run_read_routed`; `query_via_server` by `run_read_routed`; `run_read_routed` by all
11 read sites. No helper may ship uncalled.

### API Changes

| Surface | Change |
|---------|--------|
| Control socket | New `query` op; new length-prefix framing for that op only |
| `src/sqlcg/server/read_client.py` | New module: `server_is_live`, `query_via_server`, `run_read_routed` |
| CLI read commands | Replace direct `get_backend(read_only=True)` + `run_read` with `run_read_routed` (11 sites) |
| `get_backend` docstring | Update "future work" note (config.py:362-363) to "shipped in v1.2.0" |
| No graph schema change | Re-index not required for this feature |

### Data Models

JSON over the socket. Request: `{"op": "query", "cypher": str, "params": dict}`.
Response: `{"ok": true, "rows": list[dict]}` or `{"error": str}`. Framed (length-prefixed).

Row values must be JSON-serialisable. Kuzu `run_read` already returns `dict(zip(cols, row))`
(kuzu_backend.py:308-309). **Plan-reviewer / developer check**: confirm row values are JSON-safe
(strings, ints, None). If any column can return a non-serialisable type (e.g. a Kuzu internal
node/rel object), the server handler must coerce or the op must `RETURN` scalar projections
only — all 11 current queries `RETURN` scalar columns (`AS path`, `AS count`, `AS id`), so
this holds today; add a test that round-trips a representative result.

### Dependencies

None new. `anyio` (server), `socket`/`json` (client) already in use.

---

## Implementation Steps

### Phase 1: Server-side `query` op + framing

**Step 1.1**: Add framed-request read + framed-response write helpers in `_handle_connection`.
- Files: [`server.py`](../src/sqlcg/server/server.py) (server.py:119-215)
- Detail: sniff leading `<int>\n` length prefix; if present, read exactly that many bytes
  as the framed JSON request and respond framed. If absent (buffer starts with `{`), keep
  the existing unframed single-`recv` path for `status`/`stop`/`reindex`.
- Acceptance: existing `status`/`stop`/`reindex` integration tests still pass unchanged.

**Step 1.2**: Implement the `query` op branch.
- Files: server.py (new `elif op == "query":` branch)
- Detail: read-only keyword guard (allow-list leading keyword); acquire `reindex_lock`
  (or renamed `backend_lock`); call `backend_ref().run_read(cypher, params)` via
  `anyio.to_thread.run_sync` (consistent with reindex running off the event loop — a large
  read should not block the loop); return `{"ok": true, "rows": rows}` framed.
- Acceptance: a `query` op over the socket returns the same rows `run_read` returns in-process;
  a `CREATE ...` query is rejected with `{"error": "query op is read-only"}`.

**Step 1.3 (optional)**: Rename `reindex_lock` → `backend_lock`.
- Files: server.py:80, 198, 301, 312 (+ docstrings R2)
- Acceptance: all server tests pass; comment notes the lock now guards reads too.

### Phase 2: Client helper

**Step 2.1**: Create `src/sqlcg/server/read_client.py` with `server_is_live`,
`query_via_server`, `run_read_routed`.
- Files: new `read_client.py`
- Detail: mirror the socket-connect/timeout/fallback structure from reindex.py:85-153 and
  mcp.py:96-118 but for the framed `query` op. `None` return on no-server;
  raise on server-busy timeout (do NOT fall back); raise on `{"error": ...}` response.
  Use a dedicated `_QUERY_SOCKET_TIMEOUT_S` (CLI transport constant, NOT a KuzuConfig value —
  same convention as `_NOTIFY_SOCKET_TIMEOUT_S`, reindex.py:21-25).
- Acceptance: unit test of `server_is_live` (no socket → False); `query_via_server` returns
  `None` when socket absent.

### Phase 3: Wire read commands

**Step 3.1**: Replace direct read in `find.py` (3 sites: find.py:21, 45, 64).
- Acceptance: `sqlcg find table X` returns identical output with and without a live server;
  NoiseFilter post-processing unchanged.

**Step 3.2**: Replace direct read in `analyze.py` (5 sites: 67, 126, 194, 230, 247).

**Step 3.3**: Replace direct read in `db.py` `db info` (db.py:78) and `list-repos` (db.py:170).
- Note: `db info` issues *many* `run_read` calls in one `with` block. Each becomes a
  `run_read_routed` call. Acceptable: each routes independently; under a live server they
  queue on the lock but each is a fast count query.

**Step 3.4**: Replace direct read in `gain.py` (gain.py:126). Note gain.py's graph read is
already wrapped in `try/except Exception: pass` (gain.py:134) — preserve that; a server-busy
raise must be caught there so `gain` degrades gracefully (it already treats "graph not
available" as a skipped section).
- *Plan-reviewer note*: confirm `run_read_routed`'s server-busy exception type is catchable
  by gain.py's existing `except Exception` without also swallowing it elsewhere.

**Step 3.5**: Update `get_backend` docstring (config.py:362-363) — replace
"future work: route reads through the live MCP server" with a note that this shipped in
v1.2.0 via `read_client.run_read_routed`, and that the direct `read_only=True` open remains
the no-server fallback (still locks under an active writer — Windows / fallback path only).

### Phase 4: Version + docs

**Step 4.1**: Bump version 1.1.x → 1.2.0 in `pyproject.toml` and `src/sqlcg/__init__.py`.
**Step 4.2**: README: document that read commands auto-route through a live server (no flag —
zero-config, consistent with CLAUDE.md "serve both ends of the spectrum").

---

## Test Strategy

### Unit tests

- `read_client.server_is_live` → False when no socket file; True when a listening socket exists.
- `query_via_server` → returns `None` when socket absent (fallback signal).
- `query_via_server` → raises (does NOT return None) on a simulated server-busy timeout.
- Framing round-trip: a response body > 64 KiB is read in full, not truncated (assert the
  exact row count / a marker row near the tail survives).

### Integration tests (`tests/integration/test_read_via_server.py`)

Mirror `test_reindex_via_server.py` conventions (Unix-socket skip on Windows, `git_repo`
+ in-memory/temp DB fixtures, real subprocess server).

- **Scenario A — read succeeds while writer holds lock** (the core #28 proof): start a real
  MCP server on a temp DB so it holds the write lock; from a **second process / second
  connection attempt**, issue a `query` op over the socket and assert **real rows** are
  returned (non-empty, expected `path`/`id` values) — not "no exception". This is the test
  that proves a direct `get_backend(read_only=True)` would have failed with "Database is locked".
- **Scenario B — fallback when no server**: with no server live, `run_read_routed` falls
  through to direct `get_backend(read_only=True)` and returns the same rows. Assert byte-for-byte
  identical output to the pre-feature direct path (zero-config invariant).
- **Scenario C — stale socket**: socket file present but no live server (ConnectionRefused) →
  `run_read_routed` falls through to direct open (R3 parity with the write path).
- **Scenario D — read-only guard**: a `query` op carrying `CREATE (:X)` returns
  `{"error": "query op is read-only"}` and does NOT mutate the graph (assert node count
  unchanged via a follow-up read).
- **Scenario E — server-busy timeout does NOT fall through**: simulate the lock held by a
  long operation (or stub the server-side read to block past a short client timeout); assert
  the client raises / exits non-zero with a "server busy" message and does **not** attempt a
  direct open (no "Database is locked"). Direct mirror of the v1.1.0 F1 timeout fix.
- **Scenario F — large result not truncated**: a `find pattern` returning > 64 KiB of rows
  over the socket returns the full set (framing guard at the integration level).

### Existing-test regression

- `test_reindex_via_server.py`, `test_mcp_control.py`, `test_server.py` must pass unchanged
  (framing sniff must not break `status`/`stop`/`reindex`).

### Performance invariants

This is read-path/IPC work. It does **not** touch `parsers/base.py` or `indexer/indexer.py`.
Confirmed: no change to any CLAUDE.md performance invariant. `test_perf_scaling_guard.py` and
the bulk/batch upsert guards are unaffected. The developer must confirm via grep that no edit
lands in `base.py`/`indexer.py`.

---

## Acceptance Criteria

- [ ] A CLI read command returns correct rows while a live MCP server holds the KuzuDB write
      lock (Scenario A) — the documented #28 failure is gone.
- [ ] With no server live, every read command produces byte-for-byte identical output to the
      pre-feature direct-open path (Scenario B) — zero-config small-repo experience intact.
- [ ] Stale socket falls through to direct open (Scenario C).
- [ ] `query` op rejects write statements (Scenario D); graph unchanged after a rejected op.
- [ ] Server-busy timeout surfaces a clear message and does NOT fall back to a direct open
      (no "Database is locked") (Scenario E).
- [ ] Large result sets (> 64 KiB) are returned in full, not truncated (Scenario F + unit).
- [ ] All 11 read sites route through `run_read_routed`; no read site opens `get_backend`
      directly except inside the helper's fallback branch (grep-confirmed).
- [ ] `server_is_live`, `query_via_server`, `run_read_routed` each have a production call site.
- [ ] Existing `status`/`stop`/`reindex` socket ops and their tests pass unchanged.
- [ ] No edit to `parsers/base.py` or `indexer/indexer.py` (performance invariants untouched).
- [ ] `get_backend` docstring (config.py) updated; version bumped to 1.2.0.

---

## Verified Current State

| Component | Status | Evidence |
|-----------|--------|----------|
| Control socket (`status`/`stop`/`reindex`) | Shipped (v1.1.0) — reuse | server.py:76-215, `reindex_lock` server.py:301 |
| `.sock`/`.pid` helpers | Shipped — reuse | control.py: `sock_path`:32, `pid_path`:42, `read_pid`:80, `is_pid_alive`:123 |
| `reindex --notify` socket client + fallback | Shipped — factor out | reindex.py:85-153 |
| `mcp status` socket client | Shipped — factor out | mcp.py:96-118 |
| Single-`recv(65536)` framing (truncation risk for reads) | Confirmed limitation | reindex.py:113, mcp.py:101 |
| Read commands open `get_backend(read_only=True)` | Confirmed — 11 sites | find.py:21,45,64; analyze.py:67,126,194,230,247; db.py:78,170; gain.py:126 |
| `read_only=True` still locks under active writer | Confirmed gap (#28 read half) | config.py:359-363 docstring |
| KuzuDB lock-lease / RW-split rejected | Confirmed still true | ARCHITECTURE_REVIEW.md:2872 "no lock lease — verified no safe KuzuDB API" |
| MCP tools use in-process backend (no second connection) | Confirmed — no change needed | `_tools._backend` via `backend_ref()` server.py:309 |
| Windows has no control socket | Confirmed — non-goal | server.py:304 `sys.platform != "win32"` |
| `run_read`/`run_write` share `_conn.execute` | Confirmed — motivates read-only guard | kuzu_backend.py:303, 318 |

### Discrepancies found vs. the brief (flagged)

1. **db.py has TWO read-only sites, not one.** The brief lists "db.py `db info`"; there is also
   `list-repos` at db.py:170. Both are in scope (Step 3.3).
2. **mcp.py is a second source of the inline socket-client logic** to factor out (mcp.py:96-118),
   in addition to reindex.py. The brief named only reindex.py. The helper should be reused by
   the read path; refactoring `mcp status`/`mcp stop` to use it is optional (they use the small
   unframed ops) and can be deferred to avoid scope creep — recommend leaving them as-is for
   this PR and noting the duplication.
3. **The `query` op needs a read-only guard** because `run_read` and `run_write` share the same
   `_conn.execute` primitive (kuzu_backend.py). Without a guard, a generic `query` op could
   mutate. Added as Step 1.2 + Scenario D. (Not in the brief; surfaced during verification.)

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Framing change breaks existing unframed ops | Sniff leading-integer prefix; unframed JSON always starts with `{` (unambiguous). Existing tests gate it (Step 1.1 acceptance). |
| Generic `query` op enables accidental mutation | Read-only keyword allow-list guard server-side; Scenario D test. |
| Read blocks behind a long reindex | Bounded client timeout (`_QUERY_SOCKET_TIMEOUT_S`); timeout surfaces "server busy", does NOT fall back (avoids reproducing the lock error). Reindex is occasional, not steady-state. |
| Non-JSON-serialisable Kuzu row values | All current queries RETURN scalar projections; add a round-trip test. If a future query returns node/rel objects, server coerces or query must project scalars. |
| Scope creep (refactoring mcp.py clients too) | Leave `mcp status`/`stop` on their unframed path; only the read path uses the helper. |
| Per-command churn across 11 sites | Single `run_read_routed` seam keeps each diff to ~3 lines; fallback semantics centralised. |

---

## Rollout / Rollback

- **Rollout**: no schema change, no re-index required. Ships as v1.2.0. Behaviour with no
  server running is unchanged, so existing users see no difference until they attach a server.
- **Rollback**: revert the PR. The control socket reverts to `status`/`stop`/`reindex` only;
  read commands revert to direct `get_backend(read_only=True)`. No persisted state to migrate.
  The framing sniff is additive — removing it leaves the unframed ops working.
- **No backward-compat shim** (per CLAUDE.md): the `query` op and framing are new; old clients
  never sent them. No version negotiation needed.

---

### Blocking Questions

None blocking. Two design points were resolved with recommendations above and are flagged for
the plan-reviewer to confirm rather than block on:

1. Rename `reindex_lock` → `backend_lock` (Step 1.3) — recommended but optional.
2. Whether to also refactor `mcp status`/`mcp stop` onto the new helper — recommend deferring.
