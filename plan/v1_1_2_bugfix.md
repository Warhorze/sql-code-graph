# Feature Plan: v1.1.2 Bug-Fix Release

## Summary

A patch release bundling four open bugs into three PRs: a coordinated CTE-lineage
recall fix ([#39](https://github.com/Warhorze/sql-code-graph/issues/39) root cause +
[#38](https://github.com/Warhorze/sql-code-graph/issues/38) filter regression +
[#40](https://github.com/Warhorze/sql-code-graph/issues/40) test-guard gap, one PR
sharing files), and the KùzuDB single-writer-lock read-path fix
([#28](https://github.com/Warhorze/sql-code-graph/issues/28), split into a read-path
PR and a hook-visibility-verification PR). [#29](https://github.com/Warhorze/sql-code-graph/issues/29)
is **dependency context only** — its machinery already shipped in 1.1.0 and is reused, not rebuilt.

Version bump: `1.1.0` → `1.1.2` in [`pyproject.toml`](../pyproject.toml) and
[`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) (the `version_files` target per
`pyproject.toml:88`). No `1.1.1` tag is created in this plan — `1.1.1` is the
already-merged batch-upsert perf work (see `plan/v1.1.1_batch_upsert_perf.md`); the
internal `[`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py)`
guard references "v1.1.1". The next public release is `1.1.2`.

---

## Scope

### In Scope

- **#39** — emit a `SqlTable` node for source tables referenced **only** inside CTE bodies.
- **#38** — invert the CLI `analyze upstream`/`downstream` default kind-filter from an
  inner-join `MATCH ... WHERE kind IN [...]` to an `OPTIONAL MATCH ... WHERE kind IS NULL
  OR kind IN [...]` (keep-unless-positively-CTE/derived).
- **#40** — three new regression guards that exercise the **filter layer** (not raw edges),
  the **graph-completeness invariant**, and **schema_alias** join correctness.
- **#28 read path** — open the CLI read commands (`analyze`, `db info`, `find`) with a
  read-only KùzuDB connection so they succeed while the MCP server holds the write lock.
- **#28 hook visibility** — **verify and lock in** the already-shipped non-silent hook
  warning + `--notify` write-path routing with a regression test (the prose claims it
  reproduces; the code shows it is already fixed — this PR pins it so it cannot silently
  regress).
- Version bump to `1.1.2`.

### Non-Goals

- **No `SCHEMA_VERSION` bump.** Confirmed below (no new node/edge **types**). Re-index is
  the migration path for #39's additional `SqlTable` rows.
- **No new #29 feature work.** `mcp status/stop/restart`, the control socket, `--notify`
  routing, and the post-merge ORIG_HEAD hook all already shipped in 1.1.0 and are verified,
  not extended. See [§ #28 Scoping Decision](#-28-scoping-decision).
- **No reader/writer lock leasing or server-side lock release.** The read-only-connection
  approach (KùzuDB supports concurrent readers alongside one writer) solves the read half
  without touching the server lifecycle.
- **No change to the MCP `trace_column_lineage` / `get_upstream_dependencies` traversal
  semantics.** Investigation found the MCP path does **not** apply the inner-join kind-filter
  (see [§ Design — MCP parity](#mcp-parity-the-prose-assumption-corrected)); the #38 bug is
  **CLI-only**. The `GET_UPSTREAM_DEPENDENCIES_FILTERED` query is dead code and is removed.
- **No retry/backoff on the write path.** The `--notify` socket route already avoids the
  second-writer lock entirely; backoff would be redundant.
- **No `analyze impact` / `analyze unused` read-only conversion beyond the shared helper.**
  They go through the same `get_backend()` path and inherit the fix for free; covered by AC.

---

## Design

### Cluster 1 — CTE lineage recall (#39 + #38 + #40)

#### Root-cause confirmation (verified against current master)

- **#39 (data gap):** [`indexer.py`](../src/sqlcg/indexer/indexer.py) `_build_file_rows`,
  the `for edge in stmt.column_lineage:` loop (lines 1113–1160), emits a `SqlColumn` row
  for `edge.src` (lines 1119–1128) and a `SqlTable` row for `edge.dst.table` **only when
  `edge.dst.table.role == "cte"`** (lines 1140–1150). It never emits a `SqlTable` for
  `edge.src.table`. Source tables otherwise come only from the `for src_table in
  stmt.sources:` loop (lines 1093–1106), which excludes CTE-body tables. **Confirmed:**
  a table referenced only inside a CTE body gets a `SqlColumn` but no `SqlTable`.
- **#38 (filter gap):** [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) `upstream`
  (lines 38–43) and `downstream` (lines 99–104) build `kind_filter` as an **inner**
  `MATCH (t:SqlTable {qualified: src.table_qualified}) WHERE t.kind IN ['table','external']`.
  When the table node is missing (#39), the inner MATCH yields zero rows → the src column
  is dropped → `"No results"`. **Confirmed.**

#### Half A — emit source-table node (#39)

In [`indexer.py`](../src/sqlcg/indexer/indexer.py) `_build_file_rows`, inside the existing
`for edge in stmt.column_lineage:` loop (after the `edge.src` `column_rows.append`, before
or alongside the existing `edge.dst.table.role == "cte"` block), append a `SqlTable` row
for `edge.src.table`:

```python
rows.table_rows.append(
    {
        "qualified": edge.src.table.full_id,
        "name": edge.src.table.name,
        "catalog": edge.src.table.catalog or "",
        "db": edge.src.table.db or "",
        "kind": edge.src.table.role,   # "table" | "cte" | "derived" — never re-derived from SQL
        "defined_in_file": "",
    }
)
```

**schema_alias correctness (the critical constraint):** `edge.src.table` is a frozen
`TableRef` (`base.py:43`). Schema aliasing is applied during parse via `_apply_table_alias`
([`base.py:435`](../src/sqlcg/parsers/base.py), called at `base.py:398` and `base.py:533`)
**before** the edge is constructed, and the existing `column_rows` entry for `edge.src`
already derives `table_qualified` from `edge.src.table.full_id` (indexer.py:1123). Emitting
the `SqlTable` from the **same** `edge.src.table` object therefore guarantees
`SqlTable.qualified == SqlColumn.table_qualified` by construction — no re-derivation from raw
SQL, no desync. `TableRef.full_id` (`base.py:74`) and the `__post_init__` lowercasing
(`base.py:60`) are shared by both rows, so casing matches too.

**`<output>` sink guard:** the existing loop already `continue`s when
`edge.dst.table.full_id == "<output>"` (indexer.py:1117) **before** the column rows are
emitted, so the new src-table append sits after that guard and never emits an `<output>`
node. (Note: the synthetic sink is on the **dst** side; `edge.src.table` is a real source
or CTE alias, never `<output>` — but the append must stay below the existing `continue`.)

**Dedup / no extra `execute()` calls:** `upsert_nodes_bulk` (kuzu_backend.py:210) uses
`UNWIND $rows AS row MERGE (n:SqlTable {qualified: row.qualified}) SET ...`. `MERGE` on the
primary key dedups re-emitted nodes to a no-op at the DB level. Half A only **appends to the
already-batched `rows.table_rows` list** — it adds zero new `execute()` calls and zero new
round-trips. The per-batch bulk-upsert invariant (CLAUDE.md → `_flush_row_batch` /
[`test_upsert_batch_invariant.py`](../tests/unit/test_upsert_batch_invariant.py)) is
preserved: still one `upsert_nodes_bulk(SqlTable, ...)` call per batch.

> **Row-count note for the developer:** `table_rows` may now contain more SqlTable rows per
> batch (one per distinct CTE-body source). `upsert_nodes_bulk` requires **homogeneous keys**
> across rows of a label (kuzu_backend.py:225–230). The new row uses the identical key set
> (`qualified, name, catalog, db, kind, defined_in_file`) as every other `table_rows` entry
> in `_build_file_rows`, so homogeneity holds. Do not add or omit any field.

#### Half B — invert the CLI kind-filter (#38)

In [`analyze.py`](../src/sqlcg/cli/commands/analyze.py), change `kind_filter` in **both**
`upstream` and `downstream` from the inner MATCH to an OPTIONAL MATCH that defaults a missing
node to KEEP:

`upstream` (filter on `src.table_qualified`):

```python
kind_filter = (
    ""
    if include_intermediate
    else "OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified}) "
    "WITH c, src, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
)
```

`downstream` (filter on `dst.table_qualified`):

```python
kind_filter = (
    ""
    if include_intermediate
    else "OPTIONAL MATCH (t:SqlTable {qualified: dst.table_qualified}) "
    "WITH c, dst, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
)
```

> **WITH-clause placement (developer-critical):** the current `kind_filter` is interpolated
> **before** the `OPTIONAL MATCH (src)-[direct...]` / `OPTIONAL MATCH (q...)` lines (analyze.py:50–51 /
> 111–112). A `WITH` introduces a scope boundary; every variable used downstream must be
> carried through it, and **every variable referenced in the `WITH`/`WHERE` must already be
> bound at that point**. With Half B placed at the current `kind_filter` position, the
> variables bound **before** it are only `c`, `src` (upstream) / `c`, `dst` (downstream); the
> `t` from the immediately-preceding `OPTIONAL MATCH (t:SqlTable ...)`; `direct` and `q` are
> bound **after**. Therefore the `WITH` must carry exactly the three variables in scope at the
> interpolation point — the anchor (`c`), the source/destination (`src`/`dst`), and the optional
> table node (`t`) the `WHERE` filters on. The corrected interpolation order is:
>
> 1. The `MATCH (c)<-[*]-(src)` line (binds `c`, `src`).
> 2. `OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified}) WITH c, src, t WHERE t.kind IS NULL OR t.kind IN ['table','external']`
> 3. The existing `OPTIONAL MATCH (src)-[direct...]->(c)` + `OPTIONAL MATCH (q...)` + `RETURN`.
>
> i.e. the `WITH` projects exactly **`c, src, t`** (upstream) / **`c, dst, t`** (downstream).
> Omitting `t` makes the `WHERE t.kind ...` fail with "Variable t not in scope"; listing
> `q`/`direct` (bound only later) is also invalid. The code blocks above use the verified
> three-variable form. The developer must verify the **fully assembled query string** parses (a
> Cypher syntax error here is the most likely implementation slip). Apply the same change to the
> **bare-ref fallback** queries (analyze.py:57–64 upstream, 118–125 downstream) which reuse the
> same `kind_filter` string — no extra work, the f-string already reuses it.

This exact OPTIONAL-MATCH pattern was verified in the issue write-ups to return the real
sources on the already-broken graph **with no re-index**, while still excluding CTE
intermediates (whose `SqlTable.kind == 'cte'` makes the `WHERE` drop them).

#### MCP parity (the prose assumption, corrected)

The task brief asked to "fix CLI AND MCP consistently." Investigation of the MCP path shows
**the #38 inner-join bug does not exist on the MCP side**, so there is no symmetric filter to
invert:

- `trace_column_lineage` (tools.py:609) uses `TRACE_COLUMN_LINEAGE` (queries.cypher:19–26),
  which already does `OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified})` and
  **returns `table_kind` without filtering** — it traverses through everything and lets the
  client interpret `table_kind` (`'table'`/`'cte'`/`'derived'`). It never drops node-less
  sources.
- `get_upstream_dependencies` (tools.py:1315) uses `GET_UPSTREAM_DEPENDENCIES`
  (queries.cypher:37–39) and `get_downstream_dependencies` (tools.py:1184) uses
  `GET_DOWNSTREAM_DEPENDENCIES` (queries.cypher:33–35) — **both unfiltered**; filtering is not
  applied on the MCP path at all.
- `GET_UPSTREAM_DEPENDENCIES_FILTERED` (queries.cypher:41–45) is the only MCP-side query with
  the inner-join filter, **but it is dead code**: it is loaded
  (`queries.py:31` → `GET_UPSTREAM_DEPENDENCIES_FILTERED_QUERY`) and asserted loadable by
  [`test_queries_loader.py:29`](../tests/unit/test_queries_loader.py), but a repo-wide grep
  finds **no production call site** (only the loader test references the name).

**Decision:** the MCP `trace_column_lineage` / `get_*_dependencies` paths benefit from Half A
automatically (the now-present source `SqlTable` makes `trace_column_lineage` return
`table_kind='table'` instead of `null` for CTE-body sources, improving the node-kind labels
the skill teaches in [`skill.py`](../src/sqlcg/server/skill.py)). **No MCP query is broken by
#38, so no MCP filter inversion is needed.** This PR additionally **removes the dead
`GET_UPSTREAM_DEPENDENCIES_FILTERED` block** from `queries.cypher`, its `queries.py:31`
binding, and the `test_queries_loader.py:29` assertion — leaving dead inner-join filter code
in the tree is exactly the kind of latent #38-shaped trap #40 warns about, and there is no
call site to keep it for.

> **O1 — DECIDED (plan-reviewer confirmed):** `GET_UPSTREAM_DEPENDENCIES_FILTERED` is
> **removed**, not converted to OPTIONAL-MATCH. The removal is atomic: the `queries.cypher`
> block, the `queries.py:31` binding, and the `test_queries_loader.py:29` loadability assertion
> all go in the same commit. There is no production caller, so converting it would only preserve
> an unreachable inner-join-filter trap of exactly the #38 shape. This is the final disposition,
> not an open question.

#### Tests for Cluster 1 (#40 — fix the ineffective guards, do not merely add to them)

The existing guards traverse raw `COLUMN_LINEAGE` edges and are blind to the filter layer
(see [`test_golden_lineage.py`](../tests/e2e/test_golden_lineage.py),
[`test_live_anchors.py`](../tests/integration/test_live_anchors.py)). Add three new guards
(new file [`tests/integration/test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py),
integration tier — real in-memory KùzuDB):

1. **Surface-recall anchor (filter-layer + MCP).** Index a fixture with (a) a **≥2-CTE-hop
   chain** and (b) a **UNION-ALL branch CTE** (single-hop is misleadingly green — see #39).
   Then:
   - Run the **exact filtered CLI query string** that `analyze upstream` builds (kind-filter
     ON, `--include-intermediate` OFF). Assert the real physical sources (`staging.src_a`,
     `staging.src_b`, …) are in the result and CTE aliases (`a`, `b`, `j`) are NOT.
   - Drive the same fixture through the MCP `get_upstream_dependencies` / `trace_column_lineage`
     and assert the physical sources are returned (the MCP path is unfiltered, so this asserts
     Half A made the source `SqlTable` exist and `table_kind == 'table'` is reported by
     `trace_column_lineage`).
   - **Acceptance pinning:** assert on **observable output** (the returned source id set), not
     "no exception". Reintroducing the inner-join filter (revert Half B) OR dropping the
     src-table emission (revert Half A) must turn this red.

2. **Graph-completeness invariant (unit-style, query-independent).** New file
   [`tests/unit/test_cte_source_node_invariant.py`](../tests/unit/test_cte_source_node_invariant.py)
   — actually integration tier since it needs a graph; place under `tests/integration/` and
   name it `test_cte_source_node_invariant.py` to follow the existing tier convention. After
   indexing the same fixture, run:
   `MATCH (s:SqlColumn)-[:COLUMN_LINEAGE]->() ` collect distinct `s.table_qualified`; for each
   that is **not** a known CTE/derived alias (i.e. not present as a `SqlTable {kind IN
   ['cte','derived']}`), assert a `SqlTable {qualified: <that>}` node exists. Catches #39
   directly and is independent of the query layer.

   > Developer note: derive "known CTE/derived alias" from the graph itself
   > (`MATCH (t:SqlTable) WHERE t.kind IN ['cte','derived'] RETURN t.qualified`), not a
   > hardcoded list, so the invariant is fixture-agnostic.

3. **schema_alias join fixture.** A `.sqlcg.toml` with `[sqlcg.schema_aliases]`
   (e.g. `staging_tmp = "staging"`) and a CTE body whose source schema is the aliased one
   (`FROM staging_tmp.src`). Assert the emitted `SqlTable.qualified` equals the source column's
   `table_qualified` **post-alias** (`staging.src`, not `staging_tmp.src`), so the Half-B filter
   join matches. This pins the §Half A schema_alias constraint.

> **Existing-guard remediation (the #40 "fix not add" requirement):** the new
> `test_cte_recall_guard.py::test_surface_recall_*` is the corrected sibling of the raw-edge
> anchors. The developer must **not** add a raw `MATCH (s)-[:COLUMN_LINEAGE]->(d)` assertion as
> the recall guard — that is precisely the blind pattern #40 rejects. The acceptance gate is:
> reverting either half of the fix makes **at least one new guard** red while the old raw-edge
> anchors stay green (demonstrating the old guards' blindness, now covered).
>
> **Guard asymmetry (which guard catches which revert):** the two new guards are NOT redundant —
> they are sentinels for opposite halves. **Guard 2 (graph-completeness invariant) is the primary
> Half-A sentinel:** it is query-independent, so reverting Half A (dropping the src-table
> emission) reds it directly. **Guard 1 (surface-recall) is the Half-B sentinel:** it runs the
> exact filtered CLI query, so reverting Half B (restoring the inner-join filter) reds it. The
> acceptance phrase "revert either half → a guard reds" is therefore true **via different
> guards**, not the same one — keep both.

#### SCHEMA_VERSION

**No change.** `SCHEMA_VERSION = "6"` ([`schema.py:6`](../src/sqlcg/core/schema.py)) stays.
Half A emits more **rows** of existing node type `SqlTable` (kuzu DDL unchanged); no new node
or relationship type is introduced. The reindex-gate in `reindex.py:174` and `watch.py`
compares stored vs build version — leaving it at `6` means **existing graphs are not forced to
re-init**, but they will still show the #39 gap until re-indexed. Re-index is the documented
migration path (CLAUDE.md: "No backward compatibility. Re-index is the migration path.").
Half B (the CLI filter inversion) works on an **already-broken graph with no re-index** —
verified — so users get the #38 recall fix immediately on upgrade, and the #39 completeness
fix after their next index/reindex.

---

### Cluster 2 — KùzuDB single-writer lock (#28)

#### Investigation result (corrects the "still reproducing, broader" framing)

The #29 machinery **already shipped in 1.1.0** and the write-path + hook-visibility halves of
#28 are **already fixed**:

- **Write path routes through the running server.** [`reindex.py`](../src/sqlcg/cli/commands/reindex.py)
  `--notify` (lines 84–153) connects to the control socket (`sock_path()` from
  [`control.py`](../src/sqlcg/server/control.py)), sends an `{"op":"reindex", ...}` payload,
  and the server applies the delta behind `reindex_lock` (server.py:173–200). It does **not**
  spawn a second writer when a server is live. On `TimeoutError` it exits **0** (server is
  working) so the hook never blocks; on no-server (`FileNotFoundError`/`ConnectionRefusedError`/
  `OSError`) it falls through to direct write (reindex.py:143–148).
- **Hooks already use `--notify` and already warn, non-silently.**
  [`git.py`](../src/sqlcg/cli/commands/git.py) post-checkout (lines 34–37) and post-merge
  (lines 49–57) both call `reindex ... --notify` and replace the old silent `|| true` with
  `|| echo "sqlcg: graph not updated (server busy/locked) -- run 'sqlcg mcp status'" >&2`.
  The checkout still never blocks (the `echo` exits 0), but the failure is now **visible on
  stderr**. The post-merge hook passes `ORIG_HEAD` as `--from`.
- **`db info` already reports freshness** (indexed SHA vs HEAD) via `compute_freshness`
  (db.py:82–95).

**What still reproduces (the genuine remaining gap): the READ path.** Every CLI read command
opens the backend via `get_backend()` ([`config.py:349`](../src/sqlcg/core/config.py)), which
constructs `KuzuBackend(...)` **without `read_only=True`** (config.py:363–367). KùzuDB takes
the single-writer lock on a read-write open, so while the server holds it, `analyze`
(analyze.py:45), `db info` (db.py:78), `find` (find.py:21/45/64), `analyze impact/unused`, and
`db list-repos` all fail with `"Database is locked"`. The backend **already supports**
`read_only=True` (kuzu_backend.py:55, 61, 68) — KùzuDB permits concurrent readers alongside the
one writer in read-only mode — so the fix is to route read commands through a read-only
connection. This is **independent of #29** and is the highest-pain half (the whole CLI is
unusable against a live-server DB today).

#### PR 2 — Read path (read-only connection for CLI reads)

Add a `read_only` parameter to `get_backend()` in [`config.py`](../src/sqlcg/core/config.py)
and pass it to `KuzuBackend`:

```python
def get_backend(read_only: bool = False) -> "GraphBackend":
    ...
    if backend_type == "kuzu":
        kuzu_cfg = KuzuConfig.from_env()
        return KuzuBackend(
            str(kuzu_cfg.db_path),
            buffer_pool_size_mb=kuzu_cfg.buffer_pool_size_mb,
            read_only=read_only,
        )
    elif backend_type == "neo4j":
        # Neo4j has no single-writer lock; read_only is a no-op there.
        ...
```

Then pass `read_only=True` at every **read-only** CLI call site:

- [`analyze.py`](../src/sqlcg/cli/commands/analyze.py): `upstream` (45), `downstream` (106),
  `impact` (174), `failures` (210), `unused` (227) — all are pure reads.
- [`find.py`](../src/sqlcg/cli/commands/find.py): `table` (21), `column` (45), `pattern` (64).
- [`db.py`](../src/sqlcg/cli/commands/db.py): `info` (78), `list-repos` (170). **Do NOT** change
  `init` (36) or `reset` (49) — those write.
- [`gain.py`](../src/sqlcg/cli/commands/gain.py): Section F parse-quality read (126) — the
  `with get_backend() as backend` / `run_read` block, already inside a `try/except` that skips
  on no-graph. Pure read.

> **Constant/fallback alignment:** `read_only` defaults to `False` so all existing writer call
> sites (`index`, `reindex` direct path, `watch`, `db init/reset`, server `init_backend`) are
> unchanged. The KùzuDB read-only kwarg is already wired (kuzu_backend.py:68); the DB path comes
> from `KuzuConfig.from_env().db_path` (config.py:38) — never hardcoded.

> **Edge case — DB does not exist yet (developer-critical):** opening a **non-existent** KùzuDB
> path in read-only mode may error (no schema to read). The current read commands call
> `get_backend()` then immediately query; if the DB was never created, today they get an
> empty/uninitialized DB error path. The read-only open must degrade to the **same** "not
> indexed / empty database" message the user sees today, not a raw KùzuDB read-only stacktrace.
> Verify behaviour: if read-only open of a missing/empty DB raises, catch it at the call site
> (or in `get_backend(read_only=True)`) and surface the existing empty-DB guidance
> (db.py:112–115 has the canonical message). Add a test for "read-only open of never-indexed
> DB shows the empty-DB hint, not a crash."

**Tests (PR 2):**

- **Lock-contention regression test** (new
  [`tests/integration/test_readonly_under_lock.py`](../tests/integration/test_readonly_under_lock.py)):
  open a writer `KuzuBackend(path)` (no read_only) to hold the lock, then open a second
  `KuzuBackend(path, read_only=True)` and run a read query — assert it **succeeds** and returns
  the expected rows (observable output, not "no exception"). This is the direct #28 read-path
  regression: reverting the `read_only=True` open makes it fail with "Database is locked".
- **Call-site wiring test / assertion:** assert each read command passes `read_only=True`
  (e.g. patch `get_backend` and assert called with `read_only=True` for `analyze upstream`,
  `db info`, `find table`). Prevents a future edit from dropping the flag.
- **Missing-DB read-only test** (the edge case above).

#### PR 3 — Hook visibility + write-path verification (lock-in, mostly tests)

The behaviour is already implemented; this PR **pins it with regression tests** so it cannot
silently regress, and fixes any verification gap found:

- **Test: hook script content** — assert the generated post-checkout and post-merge scripts
  (`git.py` `_HOOKS` templates) contain `--notify` and the non-silent `|| echo "sqlcg: graph
  not updated ..." >&2` fragment, and do **NOT** contain a bare `|| true` swallow. (Pins
  git.py:34–37, 49–57.)
- **Test: `--notify` falls through to direct write when no server** — with no socket present,
  `reindex --from <a> --to <b> <root> --notify` performs a direct write (assert the graph
  updated). Pins reindex.py:143–148.
- **Test: `--notify` exits 0 (non-fatal) on socket timeout** — pins reindex.py:127–142 (server
  busy must not produce a false "Database is locked" and must keep the hook non-fatal).
- If any of these reveal an actual gap (e.g. a hook still missing `--notify`), fix it in this
  PR. Per current code, the templates are already correct — so this PR is expected to be
  test-only plus the version bump landing here if not already done in PR 1.

> **Optional "dirty sentinel" — DEFERRED.** The issue suggested writing a "dirty" marker the
> next read honors when the hook cannot route. With PR 2 (reads work read-only) **and** the
> existing `--notify` routing + visible stderr warning + `db info` freshness line, the
> staleness is already surfaced (freshness line shows indexed-SHA behind HEAD). A dirty
> sentinel adds a new on-disk file convention and a read-path check for marginal benefit.
> **Defer out of 1.1.2**; revisit only if users report the freshness line is insufficient.

#### #28 Scoping Decision

| Half | Status | In 1.1.2? | Needs #29 extension? |
|------|--------|-----------|----------------------|
| Read path (concurrent read-only CLI) | **Broken today** — genuine remaining gap | **YES (PR 2)** | No — independent of #29; uses existing `read_only` kwarg |
| Write path (route reindex via server) | **Already shipped in 1.1.0** (`--notify` + socket) | Verified + pinned (PR 3) | No — reuses shipped #29 machinery |
| Hook visibility (non-silent warning) | **Already shipped in 1.1.0** (`\|\| echo ... >&2`) | Verified + pinned (PR 3) | No |
| Dirty sentinel | Optional | **DEFERRED** | No |
| `mcp status/stop/restart`, control socket | Already shipped (#29) | Not touched | n/a |

**Net:** 1.1.2 adds exactly one new behaviour for #28 — the read-only read path (PR 2). The
write-path/hook halves are verified and pinned (PR 3). **No genuine #29 extension is required.**

---

## Implementation Steps

### PR 1 — Cluster 1: CTE lineage recall (#39 + #38 + #40) + version bump

**Step 1.1 — Half A: emit source-table node (#39).**
- Files: [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) (`_build_file_rows`,
  ~line 1119–1128, the `column_lineage` loop).
- Acceptance: a `SqlTable` row for `edge.src.table` is appended using `edge.src.table`'s own
  `full_id/name/db/catalog/role`; row key set identical to other `table_rows`; placed after the
  `<output>` `continue`; no new `execute()` calls.

**Step 1.2 — Half B: invert CLI kind-filter (#38).**
- Files: [`src/sqlcg/cli/commands/analyze.py`](../src/sqlcg/cli/commands/analyze.py)
  (`upstream` 38–43, `downstream` 99–104).
- Acceptance: `kind_filter` uses `OPTIONAL MATCH ... WITH c, src, t WHERE t.kind IS NULL OR
  t.kind IN ['table','external']` (and `c, dst, t` for downstream); the assembled query string
  parses;
  bare-ref fallback inherits the same string; `--include-intermediate` still yields the empty
  filter (unchanged behaviour).

**Step 1.3 — Remove dead `GET_UPSTREAM_DEPENDENCIES_FILTERED` (DECIDED: remove, atomic).**
- Files: [`queries.cypher`](../src/sqlcg/core/queries.cypher) (remove block 41–45),
  [`queries.py`](../src/sqlcg/core/queries.py) (remove line 31 binding),
  [`test_queries_loader.py`](../tests/unit/test_queries_loader.py) (remove line 29 assertion).
- Acceptance: grep confirms zero remaining references to `GET_UPSTREAM_DEPENDENCIES_FILTERED`
  in `src/` and `tests/`; suite still loads. All three deletions land in the same commit (no
  intermediate state where the binding loads a missing query or the test asserts a removed
  name). Removal is the confirmed disposition — do **not** convert to OPTIONAL-MATCH.

**Step 1.4 — Guard 1: surface-recall anchor (CLI filter + MCP).**
- Files: new
  [`tests/integration/test_cte_recall_guard.py`](../tests/integration/test_cte_recall_guard.py).
- Acceptance: ≥2-CTE-hop + UNION-ALL fixtures; asserts physical sources returned via the exact
  CLI filtered query and via MCP `get_upstream_dependencies`/`trace_column_lineage`; reverting
  Half A or Half B turns it red; old raw-edge anchors unaffected.

**Step 1.5 — Guard 2: graph-completeness invariant.**
- Files: new
  [`tests/integration/test_cte_source_node_invariant.py`](../tests/integration/test_cte_source_node_invariant.py).
- Acceptance: every non-CTE/derived `src` column `table_qualified` has a `SqlTable`; query-
  independent; reverting Half A turns it red.

**Step 1.6 — Guard 3: schema_alias join fixture.**
- Files: same new test module(s) (a `[sqlcg.schema_aliases]` fixture).
- Acceptance: emitted `SqlTable.qualified` == source column `table_qualified` post-alias.

**Step 1.7 — Version bump.**
- Files: [`pyproject.toml`](../pyproject.toml) (`version = "1.1.2"`),
  [`src/sqlcg/__init__.py`](../src/sqlcg/__init__.py) (`__version__`).
- Acceptance: `uv run sqlcg --version` (or the version surface) shows `1.1.2`.

### PR 2 — Cluster 2: read-path read-only connection (#28)

**Step 2.1 — `get_backend(read_only=...)`.**
- Files: [`config.py`](../src/sqlcg/core/config.py) (`get_backend`).
- Acceptance: defaults `False`; passes through to `KuzuBackend(..., read_only=...)`; Neo4j
  path unaffected.

**Step 2.2 — pass `read_only=True` at read call sites.**
- Files: [`analyze.py`](../src/sqlcg/cli/commands/analyze.py) (upstream/downstream/impact/
  failures/unused), [`find.py`](../src/sqlcg/cli/commands/find.py) (table/column/pattern),
  [`db.py`](../src/sqlcg/cli/commands/db.py) (info, list-repos),
  [`gain.py`](../src/sqlcg/cli/commands/gain.py) (Section F parse-quality read at gain.py:126,
  currently `with get_backend() as backend` → `run_read`). NOT `db init`/`reset`.
- Acceptance: grep confirms each listed read command opens `get_backend(read_only=True)`
  (including `gain.py:126`, whose current `with get_backend(read_only=True) as backend` form
  must be asserted); writers unchanged.

**Step 2.3 — missing/empty-DB read-only degradation.**
- Files: call sites and/or `config.py`.
- Acceptance: read-only open of a never-indexed DB shows the existing empty-DB hint, not a
  KùzuDB stacktrace; test added.

**Step 2.4 — lock-contention + wiring tests.**
- Files: new
  [`tests/integration/test_readonly_under_lock.py`](../tests/integration/test_readonly_under_lock.py).
- Acceptance: with a writer holding the lock, a read-only open + read query succeeds and returns
  expected rows; reverting `read_only=True` makes it fail "Database is locked"; call-site flag
  asserted.

### PR 3 — Cluster 2: hook visibility + write-path verification (#28) [test-led]

**Step 3.1 — hook content regression test.**
- Files: new/extended
  [`tests/unit/test_git_hooks_notify.py`](../tests/unit/test_git_hooks_notify.py).
- Acceptance: generated post-checkout + post-merge scripts contain `--notify` and the visible
  `>&2` warning; contain no bare `|| true` swallow.

**Step 3.2 — `--notify` fallthrough + non-fatal-timeout tests.**
- Files: extend the above or
  [`tests/integration/test_reindex_notify.py`](../tests/integration/test_reindex_notify.py).
- Acceptance: no-server → direct write updates the graph; socket timeout → exit 0 and no false
  lock error.

> If PR 1 already bumped the version, PR 3 carries no version change.

---

## Test Strategy

- **Unit:** dead-query removal (loader), hook-content assertions, call-site `read_only=True`
  wiring assertions.
- **Integration (real in-memory KùzuDB):** CTE surface-recall (CLI filter + MCP),
  graph-completeness invariant, schema_alias join, read-only-under-lock, missing-DB read-only
  degradation, `--notify` fallthrough/timeout.
- **Existing invariants must stay green:** perf scaling guard, bulk/batch upsert invariants
  (Half A must not add `execute()` calls), `test_pr3_kind_tagging.py` (table_kind still
  populated). Run the full suite incl. e2e before each "finished".
- **Observable-output rule:** every new guard asserts on returned ids / row sets / file
  content, never merely "no exception raised."

---

## Acceptance Criteria

- [ ] **#39:** after indexing a ≥2-CTE-hop fixture, `MATCH (t:SqlTable {qualified:'staging.src_a'})`
      returns a node with `kind='table'`.
- [ ] **#39:** `find table staging.src_a` finds the CTE-body-only source.
- [ ] **#38:** `analyze upstream "<cte-built target col>"` (default flags) returns the real
      physical source columns, not "No results", on a freshly indexed fixture.
- [ ] **#38:** `analyze upstream "<...>" --include-intermediate` still includes CTE nodes
      (unchanged).
- [ ] **#38 (no-reindex):** the inverted filter returns sources on an already-broken graph with
      no re-index (Half B independence).
- [ ] **#40:** reverting Half A OR Half B turns at least one new guard red; old raw-edge anchors
      remain green (demonstrating they were blind).
- [ ] **#40:** the new recall guard asserts on CLI filtered-query output and MCP output, not raw
      `COLUMN_LINEAGE` edges.
- [ ] **#40:** the graph-completeness invariant is query-independent and goes red on missing
      src-table nodes.
- [ ] **schema_alias:** emitted `SqlTable.qualified` == source column `table_qualified`
      post-alias.
- [ ] **No `SCHEMA_VERSION` change** (`schema.py` still `"6"`); reindex gate unchanged.
- [ ] **Perf:** Half A adds no new `execute()` calls; `test_upsert_batch_invariant.py` and
      `test_perf_scaling_guard.py` stay green.
- [ ] **#28 read:** with a writer holding the lock, `analyze upstream`, `db info`, and
      `find table` succeed (read-only) and return real rows.
- [ ] **#28 read:** read-only open of a never-indexed DB shows the empty-DB hint, no crash.
- [ ] **#28 hook:** generated hooks contain `--notify` + visible stderr warning, no bare
      `|| true` swallow.
- [ ] **#28 write:** `--notify` routes through a live server (no second writer) and falls
      through to direct write when no server is present.
- [ ] **Version:** `1.1.2` in `pyproject.toml` and `src/sqlcg/__init__.py`.
- [ ] **Dead code:** zero references to `GET_UPSTREAM_DEPENDENCIES_FILTERED` remain (query,
      binding, and loader assertion all removed atomically — DECIDED, not converted).

---

## PR Breakdown & Rationale

| PR | Issues | Files (primary) | Rationale |
|----|--------|-----------------|-----------|
| **PR 1** | #39, #38, #40 | `indexer.py`, `analyze.py`, `queries.cypher`, `queries.py`, new `tests/integration/test_cte_recall_guard.py` + `test_cte_source_node_invariant.py`, `test_queries_loader.py`, version files | One coordinated change: #39 (data) and #38 (filter) **must ship together** — Half B alone leaves the graph incomplete for `find`/`unused`; Half A alone still returns "No results" under the old inner-join filter. #40's guards must land in the same PR to gate both halves (the acceptance criterion is "reverting either half turns a guard red"). Shared files (`indexer.py` + `analyze.py` + queries + tests) reinforce one PR. |
| **PR 2** | #28 (read path) | `config.py`, `analyze.py`, `find.py`, `db.py`, new `tests/integration/test_readonly_under_lock.py` | The genuine remaining #28 gap. Self-contained: a `read_only` param + call-site wiring. Independent of #29 and of Cluster 1. Highest user pain (CLI unusable during a session). |
| **PR 3** | #28 (hook/write verification) | new `tests/unit/test_git_hooks_notify.py`, `tests/integration/test_reindex_notify.py` (+ fixes only if a gap is found) | Pins already-shipped behaviour with regression tests. Separated from PR 2 because it is test-led (no production change expected) and touches the hook/socket surface, not the read surface — keeps each PR reviewable and reverts independent. |

Ordering: **PR 1 → PR 2 → PR 3** (no inter-PR code dependency; this order front-loads the
highest-severity recall fix). PRs 2 and 3 can proceed in parallel after PR 1 if desired.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Half B Cypher syntax slip (WITH-clause scope) | Plan pins the exact 2-variable `WITH c, src` / `WITH c, dst` form and warns the developer to verify the assembled string parses; the surface-recall guard fails on a malformed query. |
| Half A breaks `upsert_nodes_bulk` homogeneity (mismatched keys) | Plan pins the identical key set; homogeneity check (kuzu_backend.py:225) would raise loudly in tests, not silently. |
| Half A inflates `execute()` count (perf invariant) | Append-only to batched list; `test_upsert_batch_invariant.py` / `test_perf_scaling_guard.py` gate it. |
| Read-only open of missing DB crashes | Step 2.3 + dedicated test; degrade to existing empty-DB hint. |
| Removing dead FILTERED query breaks a hidden caller | Grep confirms no production call site (only the loader test referenced it); removal is the confirmed disposition (O1 decided); all three deletions land atomically in one commit so the loader never references a missing query. |
| Single-hop CTE fixture falsely green (#39 trap) | Fixtures mandated to be ≥2-CTE-hop and UNION-ALL branch CTEs. |
| Neo4j backend has no `read_only` semantics | `read_only` is a no-op for Neo4j (no single-writer lock); documented in `get_backend`. |

---

## Rollout / Rollback

- **Rollout:** ship `1.1.2`. Half B (#38) takes effect immediately on upgrade (works on the
  existing graph). Half A (#39) completeness and the read-only read path take effect after the
  user's next `sqlcg index`/`reindex` (Half A) / immediately (PR 2 reads). No `db reset` required
  (no `SCHEMA_VERSION` bump).
- **Rollback:** each PR reverts independently. Reverting PR 1 restores the #38/#39 behaviour;
  reverting PR 2 restores the read-lock failure; PR 3 is test-only.

---

## Blocking Questions

None blocking — the feature (a four-issue patch release) is well-defined by the issues and the
task brief, and aligns with the `ARCHITECTURE_REVIEW.md` "read-only enforcement" v1 decision
(Open Questions §3).

### O1 — RESOLVED

The disposition of the dead `GET_UPSTREAM_DEPENDENCIES_FILTERED` query is **remove it**
(plan-reviewer confirmed). The query block, its `queries.py:31` binding, and the
`test_queries_loader.py:29` loadability assertion are deleted atomically in Step 1.3. The
convert-to-OPTIONAL-MATCH alternative is rejected — there is no production caller, so converting
would only preserve an unreachable inner-join-filter trap of exactly the #38 shape. No open
questions remain; the plan is ready for the developer.
