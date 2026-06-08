# Feature Plan: v1.2.0 — Server-Routed Read Proxy (closes remaining half of #28)

## Summary

Eliminate "Database is locked" failures on CLI **read** commands while the live MCP
server holds the KuzuDB process-level write lock. CLI read commands gain a thin client
that, when a server is live on the target DB, routes the read query over the existing
Unix control socket (`<db>.sock`) and runs it on the server's single Kuzu connection.
When no server is live, behaviour is byte-for-byte unchanged: the fallback opens the DB
read-only. **Note**: `get_backend()` does NOT yet accept a `read_only` flag — adding it is an
explicit implementation step (Step 0.1) so the fallback opens read-WRITE no longer (see BLOCKER 1).

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
- Wiring the helper into the read commands that currently open `get_backend()` (no `read_only`
  arg today — see BLOCKER 1 / Step 0.1):
  - [`find.py`](../src/sqlcg/cli/commands/find.py) — 3 `with get_backend()` blocks (find.py:21, 45,
    64), each with one `run_read` (find.py:22, 46, 65)
  - [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) — 5 `with get_backend()` blocks
    (analyze.py:~45, ~106, ~174, ~210, ~227), but **6 `run_read` call sites** across them:
    `upstream` has 2, `downstream` has 3 (one in a loop at ~156), the rest 1 each. ALL `run_read`
    calls route — see Step 3.2.
  - [`db.py`](../src/sqlcg/cli/commands/db.py) — **only the two READ commands**: `db info`
    (db.py:78, multiple `run_read` at 85/100/109/117/126/139/148/152/160) **and** `list-repos`
    (db.py:170, run_read at 171). **Do NOT touch** `db init` (db.py:36, `init_schema` — write) or
    `db reset --repo` (db.py:49, `run_write` — write); those are write paths and stay on direct
    `get_backend()`.
  - [`gain.py`](../src/sqlcg/cli/commands/gain.py) — 1 site (gain.py:126)
- Add a `read_only: bool = False` parameter to `get_backend()` (config.py:349) and pass it
  through to `KuzuBackend(...)` (the `KuzuBackend.__init__` `read_only` param already exists at
  kuzu_backend.py:55 but is never passed by `get_backend`). This is required by the
  `run_read_routed` fallback — without it the fallback opens read-WRITE and reproduces the lock
  contention this feature fixes (see BLOCKER 1, Step 0.1).
- Update the `get_backend` docstring (config.py:350-357) to document the new `read_only` flag and
  note that read commands route through the live server when present. **Note**: there is no
  "future work: route reads through the live MCP server" line at config.py:362-363 — that was a
  mis-cited location; the docstring is config.py:350-357 and contains no such sentence. The
  developer edits the actual docstring text, not a phantom line.

### Non-Goals

- **Windows**: no control channel exists on Windows today (the control-socket task is only
  started on `sys.platform != "win32"`, server.py:304). Reads on Windows continue to fall
  back to direct `get_backend(read_only=True)` (the new flag, Step 0.1) and **may still lock**
  while a writer holds it (read-only open still contends for the process lock under an active writer).
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
- **Neo4j backend**: has no single-writer lock (config.py:368-372); the proxy is a Kuzu-only
  concern. With `SQLCG_BACKEND=neo4j`, read commands take the direct path unchanged. The new
  `read_only` param on `get_backend()` is ignored for Neo4j (no read-only mode passed to
  `Neo4jBackend`); document this — the fallback `get_backend(read_only=True)` is a no-op flag for
  Neo4j and simply opens the normal Neo4j connection.

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

- **Server** (BLOCKER 2 — mechanism specified): `_handle_connection` currently does
  `raw = await stream.receive(4096)` (server.py:122). anyio `stream.receive(n)` returns
  **AT MOST** n bytes with **no complete-message guarantee** — both the length line and the body
  can arrive fragmented. The handler MUST wrap the stream:
  `buf = anyio.streams.buffered.BufferedByteReceiveStream(stream)`, then:
  - read the length line: `line = await buf.receive_until(b"\n", max_bytes=64)` →
    `n = int(line)` (the `max_bytes=64` cap bounds a malformed/huge length line);
  - read exactly the body: `body = await buf.receive_exactly(n)` → `json.loads(body)`.
  Responses are written with `await stream.send(...)` unchanged (send already takes the full
  buffer; the loop hazard is on receive, not send).
- **Client helper** (BLOCKER 2 — recv-exactly required): the existing `s.recv(65536)` single-read
  pattern at reindex.py:113 is **insufficient for framed reads** and MUST NOT be copied. The
  client must read-exactly: either wrap the socket with `sock.makefile("rb")` and use
  `f.readline()` for the `<int>\n` length line + `f.read(n)` for the body, OR run an explicit
  recv loop that accumulates until `n` body bytes are received. `json.loads` the assembled body.
  Document that a partial `recv` mid-frame is the failure mode the loop exists to prevent.
- **Back-compat with existing ops**: `status`/`stop`/`reindex` keep their current
  unframed single-`recv` protocol. The server distinguishes by **trying to parse a leading
  `<int>\n` length prefix**; if the first line is not a bare integer it falls back to
  treating the buffer as a single unframed JSON request (existing behaviour). This keeps
  the v1.1.0 ops working unchanged and avoids a protocol-version flag.

  *Sniff scope (WARNING 2)*: the framed/unframed sniff is **server-side, on the REQUEST path
  only**. The server always sends a framed response to a framed `query` request and an unframed
  response to an unframed request — there is no response-path sniff. A client that uses the old
  unframed `s.recv(65536)` + `json.loads` pattern but accidentally receives a framed response (a
  `<int>\n` prefix it does not expect) gets an explicit `json.JSONDecodeError` — a loud, visible
  failure, NOT silent truncation. This is acceptable: the only callers that receive framed
  responses are the new framed clients (`read_client`), which read-exactly. Document this in the
  handler comment.

  *Sniff unambiguity*: the "leading integer line" sniff cannot collide with an unframed JSON
  request — unframed requests always start with `{`, never a digit, so the sniff is unambiguous.
  State this in the handler comment too.

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

  *Server-busy exception type (WARNING 3 — resolved)*: `run_read_routed` MUST raise `typer.Exit`
  on server-busy (or another plain `Exception` subclass) — **NOT** `SystemExit` and **NOT** any
  `BaseException` that bypasses `except Exception`. Rationale: `typer.Exit`'s MRO is
  `typer.Exit → RuntimeError → Exception`, so gain.py:134's `except Exception: pass` catches it
  and degrades the parse-quality section gracefully (the desired behaviour — gain must not crash
  when a server is busy). A `SystemExit`/`BaseException` would escape that handler and crash
  `gain`. Other read commands (find/analyze/db) let the `typer.Exit` propagate to a clean
  non-zero CLI exit with the "server busy" message. This resolves the prior open question.

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
    Raises typer.Exit (an Exception subclass, NOT SystemExit/BaseException) on
    server-busy timeout or server error (caller must NOT fall back — the lock is
    held). typer.Exit is catchable by gain.py's `except Exception` (WARNING 3).
    """
```

The read commands adopt this pattern (replacing each `with get_backend(read_only=True)`):

```python
rows = query_via_server(cypher, params)
if rows is None:
    # get_backend now accepts read_only (Step 0.1) — the fallback opens read-only,
    # NOT read-write. Without Step 0.1 this line would TypeError or silently open RW.
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

### Phase 0: `get_backend(read_only=...)` pass-through (BLOCKER 1)

**Step 0.1**: Add `read_only: bool = False` to `get_backend()` (config.py:349) and pass it through
to `KuzuBackend(...)` (config.py:364-367).
- Files: [`config.py`](../src/sqlcg/core/config.py) (config.py:349, 364-367 — the `KuzuBackend(...)`
  call); docstring config.py:350-357.
- Detail: `KuzuBackend.__init__` already accepts `read_only` (kuzu_backend.py:55) but
  `get_backend()` never passes it. Add the param and forward it:
  `return KuzuBackend(str(kuzu_cfg.db_path), buffer_pool_size_mb=..., read_only=read_only)`.
  For the Neo4j branch the flag is ignored (no read-only mode); document that in the docstring.
- **Why this is a blocker, not a nicety**: the `run_read_routed` fallback calls
  `get_backend(read_only=True)`. Without this step that call is a `TypeError` (unexpected kwarg)
  OR — worse, if someone "fixes" it by dropping the kwarg — it opens read-WRITE and reproduces the
  exact lock contention this feature exists to remove. Scenario B (no server) would pass in tests
  while production silently fails when a writer is active.
- Acceptance: `get_backend(read_only=True)` opens a read-only Kuzu connection (assert via a unit
  test that a second read-only open succeeds while a writer holds the DB, OR assert the
  `read_only=True` kwarg reaches `KuzuBackend.__init__` via a patch/spy). Existing `get_backend()`
  callers (no arg) are unaffected — default `False` preserves current behaviour.

### Phase 1: Server-side `query` op + framing

**Step 1.1**: Add framed-request read + framed-response write in `_handle_connection`
(currently `raw = await stream.receive(4096)` at server.py:122).
- Files: [`server.py`](../src/sqlcg/server/server.py) (server.py:119-215)
- Detail (BLOCKER 2 — mechanism, not just intent): wrap the stream once with
  `buf = anyio.streams.buffered.BufferedByteReceiveStream(stream)`. To sniff framing: read the
  first line with `buf.receive_until(b"\n", max_bytes=64)`. If it parses as a bare decimal `n`,
  this is a framed request → read the body with `buf.receive_exactly(n)` and respond framed
  (`<len>\n` + json bytes via `stream.send(...)`). If the first byte is `{` (not a digit), it is an
  unframed `status`/`stop`/`reindex` request — `json.loads` the line as today and respond unframed.
  `stream.send()` for responses is unchanged in both cases (the receive loop is the hazard, not send).
  Do NOT replace `receive(4096)` with another bare `receive(n)` — that re-introduces the
  fragmentation bug for the framed body.
- Acceptance: existing `status`/`stop`/`reindex` integration tests still pass unchanged
  (the unframed path is byte-for-byte preserved); a framed request whose body is delivered across
  multiple TCP/socket chunks is reassembled correctly (test fragments the send).

**Step 1.2**: Implement the `query` op branch.
- Files: server.py (new `elif op == "query":` branch)
- Detail: read-only keyword guard (allow-list leading keyword); acquire `reindex_lock`
  (server.py:198 is the existing acquire; renamed `backend_lock` if Step 1.3 is taken); call
  `backend_ref().run_read(cypher, params)` via
  `anyio.to_thread.run_sync` (consistent with reindex running off the event loop — a large
  read should not block the loop); return `{"ok": true, "rows": rows}` framed.
- Acceptance: a `query` op over the socket returns the same rows `run_read` returns in-process;
  a `CREATE ...` query is rejected with `{"error": "query op is read-only"}`.

**Step 1.3 (optional)**: Rename `reindex_lock` → `backend_lock`.
- Files: **rename ALL occurrences** in server.py — do not enumerate-and-miss. Grep-confirmed
  occurrences today are at server.py:80 (param), 93 (docstring), 95 (docstring), 198 (acquire),
  292 (docstring), 301 (docstring), 311 (call-site arg) — 7 total, including the four docstring
  mentions (the prior plan's list 80/198/301/312 missed 93/95/292 and mis-cited 312 for 311).
  Use `grep -n reindex_lock src/sqlcg/server/server.py` and replace every hit, prose included.
- Acceptance: `grep reindex_lock src/sqlcg/server/server.py` returns zero matches after rename;
  all server tests pass; a comment at the lock definition notes it now guards reads too.

### Phase 2: Client helper

**Step 2.1**: Create `src/sqlcg/server/read_client.py` with `server_is_live`,
`query_via_server`, `run_read_routed`.
- Files: new `read_client.py`
- Detail: mirror the socket-connect/timeout/fallback structure from reindex.py:85-153 and
  mcp.py:96-118 but for the framed `query` op. **Do NOT copy reindex.py:113's single
  `s.recv(65536)`** — that truncates framed reads (BLOCKER 2). Use a recv-exactly mechanism:
  `sock.makefile("rb")` then `f.readline()` for the `<int>\n` length line + `f.read(n)` for the
  body, OR an explicit recv loop accumulating to `n` bytes. `None` return on no-server; raise
  `typer.Exit` (Exception subclass — WARNING 3) on server-busy timeout (do NOT fall back); raise
  on `{"error": ...}` response. Use a dedicated `_QUERY_SOCKET_TIMEOUT_S` (CLI transport constant,
  NOT a KuzuConfig value — same convention as `_NOTIFY_SOCKET_TIMEOUT_S`, reindex.py:21-25).
- Acceptance: unit test of `server_is_live` (no socket → False); `query_via_server` returns
  `None` when socket absent.

### Phase 3: Wire read commands

**Step 3.1**: Replace direct read in `find.py` (3 sites: find.py:21, 45, 64).
- Acceptance: `sqlcg find table X` returns identical output with and without a live server;
  NoiseFilter post-processing unchanged.

**Step 3.2**: Replace direct read in `analyze.py`. There are **5 `with get_backend()` blocks** at
analyze.py ~45 (`upstream`), ~106 (`downstream`), ~174 (`impact`), ~210 (`failures`), ~227
(`unused`). (Prior plan listed 67/126/194/230/247 — those line numbers were stale by ~20; the
count of 5 blocks is correct.) Critically, several blocks contain **more than one**
`backend.run_read` call — **every** `run_read` must become `run_read_routed`, not just the first
in each block:
- `upstream` (~45): TWO `run_read` — primary (~46) + bare-name fallback (~57).
- `downstream` (~106): THREE `run_read` — primary (~107), bare-name fallback (~118), and one
  **INSIDE the `for tbl in sorted(terminal_tables)` loop** (~156, `GET_TABLE_EXTERNAL_CONSUMERS_QUERY`).
  The in-loop call is easy to miss; it MUST also route. Each loop iteration routes independently
  (each is a fast scalar consumer query); under a live server they queue on the backend lock.
- `impact` (~174): one `run_read` (~175).
- `failures` (~210): one `run_read` (~217).
- `unused` (~227): one `run_read` (~228).
- Acceptance: grep `backend.run_read` in analyze.py returns zero matches after the change (all
  replaced by `run_read_routed`); `upstream`/`downstream` (incl. the bare-name fallbacks and the
  terminal-table consumer loop) return identical output with and without a live server.

**Step 3.3**: Replace direct read in `db.py` — **READ commands only**: `db info` (db.py:78) and
`list-repos` (db.py:170).
- `db info` issues *many* `run_read` calls in one `with` block (db.py:85, 100, 109, 117, 126,
  139, 148, 152, 160) — EACH becomes a `run_read_routed` call. Acceptable: each routes
  independently; under a live server they queue on the backend lock but each is a fast count query.
- `list-repos` (db.py:171): single `run_read` → `run_read_routed`.
- **Out of scope (write paths — leave on direct `get_backend()`)**: `db init` (db.py:36,
  `init_schema`) and `db reset --repo` (db.py:49, `run_write`). Routing a write through the
  read-only `query` op would be rejected by the read-only guard — these must stay direct.

**Step 3.4**: Replace direct read in `gain.py` (gain.py:126; the `run_read` is at gain.py:127).
gain.py's graph read is already wrapped in `try: ... except Exception: pass` (gain.py:134,
`pass  # graph not available — skip quality section`) — **preserve that block**; the
`run_read_routed` call goes inside it.
- *Resolved (WARNING 3)*: `run_read_routed` raises `typer.Exit` on server-busy. `typer.Exit`'s
  MRO is `typer.Exit → RuntimeError → Exception`, so gain.py:134's `except Exception` catches it
  and `gain` degrades gracefully (skips the parse-quality section) instead of crashing — the
  intended behaviour. This is why the server-busy type must be `typer.Exit`/Exception and NOT
  `SystemExit`/`BaseException` (those would escape the handler and crash `gain`). No code change
  needed in gain's except block; just confirm the raised type is Exception-derived.

**Step 3.5**: Update the `get_backend` docstring (config.py:350-357). **There is NO
"future work: route reads through the live MCP server" sentence to replace** — that location
(config.py:362-363) is the `KuzuConfig.from_env()` / `KuzuBackend(...)` call body, not docstring
prose. The docstring (config.py:350-357) currently documents only the return value and the
`ValueError`. ADD: (a) the new `read_only: bool = False` arg (from Step 0.1), and (b) a one-line
note that CLI read commands route through a live MCP server via `read_client.run_read_routed`
(v1.2.0), with the direct `read_only=True` open as the no-server fallback (still contends for the
process lock under an active writer — Windows / fallback path only). The developer must read the
actual docstring text before editing — do not edit a phantom line.

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
- Framing fragmentation (BLOCKER 2): a framed body delivered across multiple chunks is
  reassembled correctly server-side (feed the `BufferedByteReceiveStream` fragmented input) and
  client-side (recv-exactly loop). Assert the decoded JSON equals the input regardless of chunk
  boundaries.
- `get_backend(read_only=True)` (BLOCKER 1 / Step 0.1): the `read_only=True` kwarg reaches
  `KuzuBackend.__init__` (patch/spy the constructor or assert a read-only open succeeds while a
  writer holds the DB). `get_backend()` with no arg still opens read-write (default unchanged).
- Server-busy exception type (WARNING 3): `run_read_routed` raises `typer.Exit` (assert
  `isinstance(exc, Exception)` and NOT `SystemExit`) on a simulated busy server, and a bare
  `except Exception` catches it (mirrors gain.py:134).

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
- [ ] All read `run_read` sites route through `run_read_routed`: find.py (3 — lines 22/46/65),
      analyze.py (6 — upstream ~46/57, downstream ~107/118/156, impact ~175, failures ~217,
      unused ~228), db.py READ commands (`db info` ~85/100/109/117/126/139/148/152/160 +
      `list-repos` ~171), gain.py (1 — ~127). Verified by: `grep -rn "backend.run_read"
      src/sqlcg/cli/commands/{find,analyze,gain}.py` returns zero matches, and db.py's only
      remaining `run_read`/`run_write` are in the WRITE commands `db init`/`db reset`. No read
      command opens `get_backend` except inside `run_read_routed`'s fallback branch.
- [ ] `server_is_live`, `query_via_server`, `run_read_routed` each have a production call site.
- [ ] Existing `status`/`stop`/`reindex` socket ops and their tests pass unchanged.
- [ ] No edit to `parsers/base.py` or `indexer/indexer.py` (performance invariants untouched).
- [ ] `get_backend(read_only=True)` opens a read-only Kuzu connection (BLOCKER 1 / Step 0.1);
      the `read_only` kwarg reaches `KuzuBackend.__init__`. Existing no-arg `get_backend()`
      callers unaffected.
- [ ] Framed request/response uses `BufferedByteReceiveStream` + `receive_until`/`receive_exactly`
      server-side and a recv-exactly client; a fragmented frame is reassembled (BLOCKER 2).
- [ ] Server-busy raises `typer.Exit` (Exception-derived, not SystemExit/BaseException);
      gain.py degrades gracefully via its `except Exception` (WARNING 3).
- [ ] `get_backend` docstring (config.py:350-357) updated to document `read_only` + the
      server-routed read note; version bumped to 1.2.0.

---

## Verified Current State

| Component | Status | Evidence |
|-----------|--------|----------|
| Control socket (`status`/`stop`/`reindex`) | Shipped (v1.1.0) — reuse | server.py:76-215, `reindex_lock` server.py:301 |
| `.sock`/`.pid` helpers | Shipped — reuse | control.py: `sock_path`:32, `pid_path`:42, `read_pid`:80, `is_pid_alive`:123 |
| `reindex --notify` socket client + fallback | Shipped — factor out | reindex.py:85-153 |
| `mcp status` socket client | Shipped — factor out | mcp.py:96-118 |
| Server receives request via `stream.receive(4096)` then `json.loads` | Confirmed — no read-exactly today | server.py:122; anyio `receive(n)` returns AT MOST n bytes (BLOCKER 2 motivates BufferedByteReceiveStream) |
| Single-`recv(65536)` framing (truncation risk for reads) | Confirmed limitation | reindex.py:113 (`data = s.recv(65536)`), mcp.py:101 |
| `sock_path` uses `with_suffix(".sock")` → `graph.db` becomes `graph.sock` (NOT `graph.db.sock`) | Pre-existing cosmetic docstring inconsistency — NOT introduced here | control.py:38-39; docstring at control.py:36 misleadingly says `graph.db.sock`. Out of scope; flagged so the developer is not surprised by the actual socket filename. |
| `reindex_lock` occurrences in server.py | 7 total (incl. 4 docstring mentions) | grep: server.py:80,93,95,198,292,301,311 |
| Read commands open `get_backend()` (NO `read_only` arg today) | Confirmed — 5 analyze blocks (6 run_read), find.py 3, db.py 2, gain.py 1 | find.py:21,45,64; analyze.py:~45,~106,~174,~210,~227 (+ run_read ~46/57, ~107/118/156, ~175, ~217, ~228); db.py:78,170; gain.py:126 |
| `get_backend()` has **no** `read_only` parameter | Confirmed — BLOCKER 1 | config.py:349 signature `def get_backend() -> "GraphBackend"`; flag exists only on `KuzuBackend.__init__` (kuzu_backend.py:55) and is never passed through (config.py:364-367) |
| `get_backend` docstring has NO "route reads through live server" note | Confirmed — BLOCKER 1 mis-cite | config.py:350-357 docstring is return-value + ValueError only; config.py:362-363 is the `KuzuBackend(...)` call body, not prose |
| `read_only=True` (once added) still contends for lock under active writer | Confirmed gap (#28 read half) | Kuzu process-level lock; read-only open is not exempt |
| KuzuDB lock-lease / RW-split rejected | Confirmed still true | ARCHITECTURE_REVIEW.md:2872 "no lock lease — verified no safe KuzuDB API" |
| MCP tools use in-process backend (no second connection) | Confirmed — no change needed | `_tools._backend` via `backend_ref()` server.py:309 |
| Windows has no control socket | Confirmed — non-goal | server.py:304 `sys.platform != "win32"` |
| `run_read`/`run_write` share `_conn.execute` | Confirmed — motivates read-only guard | kuzu_backend.py:303, 318 |

### Discrepancies found vs. the brief (flagged)

1. **db.py has TWO READ sites, not one.** The brief lists "db.py `db info`"; there is also
   `list-repos` at db.py:170. Both are in scope (Step 3.3). db.py actually has FOUR
   `with get_backend()` blocks total (36, 49, 78, 170) — the other two (`db init`:36,
   `db reset --repo`:49) are WRITE paths and stay on direct `get_backend()` (out of scope).
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
| Framing change breaks existing unframed ops | Sniff leading-integer prefix (server-side, REQUEST path only — WARNING 2); unframed JSON always starts with `{` (unambiguous). Old unframed clients that receive a framed response get a loud `JSONDecodeError`, not silent truncation. Existing tests gate it (Step 1.1 acceptance). |
| `receive(4096)`/`recv(65536)` fragmentation on framed body (BLOCKER 2) | Server: `BufferedByteReceiveStream` + `receive_until`/`receive_exactly`. Client: recv-exactly via `makefile`/loop, never a single `recv`. Fragmentation test gates it. |
| Fallback opens read-WRITE, reproducing the lock (BLOCKER 1) | Step 0.1 adds `read_only` to `get_backend()` and threads it to `KuzuBackend`; fallback calls `get_backend(read_only=True)`. Test asserts the kwarg reaches the constructor. |
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
