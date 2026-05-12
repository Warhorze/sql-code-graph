# Sprint Plan: Bulk Upsert Performance + Pass-2 Skip + Timeout Fix + Live Re-measurement

Plan date: 2026-05-12
Author: architect-planner
Source authority: [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) § 11.6 (bulk DB writes), CLAUDE.md "Perf budget — 1,600 files under 5 minutes", [`plan/sprint_07_perf_and_live_test.md`](sprint_07_perf_and_live_test.md) (batched-commits work — necessary but not sufficient), live profiling session (2026-05-12) showing 23 min for 300 files.
Policy: no backward compatibility; re-index is the migration path; no TODO in happy path; serve both small (20-file) and large (1,600-file) repos.

---

## Summary

Sprint 07 reduced **commit** overhead by batching N files per transaction. Live profiling on 300 files post-sprint-07 still measures 23 minutes — roughly **10× over budget**. Root cause is **not** commit overhead; it is **per-call KuzuDB round-trips**: every column / edge in [`_upsert_parsed_file`](../src/sqlcg/indexer/indexer.py) issues its own `MERGE` via [`KuzuBackend.upsert_node`](../src/sqlcg/core/kuzu_backend.py) / [`upsert_edge`](../src/sqlcg/core/kuzu_backend.py), yielding ~100 round-trips per file (~30,000 statements across 300 files, all inside the active transaction).

This sprint has four code tickets and one measurement task:

1. **PERF-UNWIND** (T-01) — Add a bulk-upsert API on `GraphBackend` (`upsert_nodes_bulk`, `upsert_edges_bulk`) using `UNWIND $rows AS row MERGE …`. Rewire [`Indexer._upsert_parsed_file`](../src/sqlcg/indexer/indexer.py) to collect rows per (label / rel-type) group and issue one bulk call per group per file. Target: 100 round-trips/file → 2–4 round-trips/file.

2. **PERF-PASS2-SKIP** (T-02) — In [`CrossFileAggregator.resolve_pass2`](../src/sqlcg/lineage/aggregator.py), skip the file re-parse for files that have no cross-file source dependencies. Pass-1 result is returned unchanged.

3. **FIX-TIMEOUT** (T-03) — Make `--timeout-per-file` actually interrupt. Replace the `with ThreadPoolExecutor(max_workers=1) as ex:` block in [`Indexer._index_single_file`](../src/sqlcg/indexer/indexer.py) with an explicit `executor.shutdown(wait=False, cancel_futures=True)` on the timeout path so the `with` exit does not block on the runaway worker.

4. **MEAS-FULLINDEX** (T-04) — Full-corpus index run post-fix; record measurement to [`plan/measurements/sprint_08_changelogs_fullindex.json`](measurements/) and re-run anchor queries (`omloopsnelheid`, `ma_aantal_op_order`) against the live graph. Compare against [`plan/measurements/sprint_07_changelogs_noschema.json`](measurements/sprint_07_changelogs_noschema.json) (parse-only baseline) and confirm the 5-minute budget at 1,600 files is reachable.

5. **FIX-E1-LITERALS** (T-05) — Suppress the 1,148 `col_lineage:NULL:Cannot find column 'NULL' in query` false fires in [`parsers/base.py`](../src/sqlcg/parsers/base.py) at line ~614. Root cause fully explored: SELECT expressions with zero `exp.Column` descendants (pure NULL / literals / zero-argument functions like `CURRENT_TIMESTAMP()`) reach `sg_lineage('NULL', ...)`, which raises "Cannot find column 'NULL' in query". Fix is a 2-line guard after the star-skip block: `if not list(col_expr.find_all(exp.Column)): continue`. Zero risk of suppressing real E1s — any expression with a real column reference passes through.

---

## Scope

### In Scope

- [`src/sqlcg/core/graph_db.py`](../src/sqlcg/core/graph_db.py) — add `upsert_nodes_bulk` / `upsert_edges_bulk` abstract methods (T-01)
- [`src/sqlcg/core/kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py) — implement bulk variants with `UNWIND` (T-01)
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — `_upsert_parsed_file` rewrite to row-group form (T-01); `_index_single_file` timeout fix (T-03)
- [`src/sqlcg/lineage/aggregator.py`](../src/sqlcg/lineage/aggregator.py) — pass-2 skip predicate (T-02)
- `tests/integration/test_bulk_upsert.py` — equivalence + perf-shape tests for T-01
- `tests/unit/test_aggregator_skip.py` — pass-2 skip predicate tests for T-02
- `tests/unit/test_timeout_cancel.py` — timeout-cancellation test for T-03
- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — literal-expression skip guard (T-05)
- `tests/unit/test_literal_column_skip.py` — literal-skip tests for T-05
- [`plan/measurements/sprint_08_changelogs_fullindex.json`](measurements/) — NEW measurement artifact (T-04)
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — new subsection recording sprint-08 measurement

### Non-Goals

- `ProcessPoolExecutor` parallel parsing — still deferred per § 11.6
- Per-statement column-lineage algorithm changes (E5/E36 work is sprint 07 territory)
- Changes to `_expand_star_sources` (untouched; still runs once outside the batched loop)
- Changes to `reindex_file` (`sqlcg watch` path) — its per-file transaction stays; bulk-upsert API is opt-in via call site
- New backend implementations (Neo4j adapter etc.) — only Kuzu is updated
- Broader E1 work beyond the literal-as-column suppression (other E1 causes such as actual unresolved columns are out of scope)

---

## Code-vs-Plan Verification

| Finding | Verified state |
|---------|---------------|
| Each `upsert_node` / `upsert_edge` is a single `self._conn.execute()` call | Confirmed — [`kuzu_backend.py` lines 137–207](../src/sqlcg/core/kuzu_backend.py). One `MERGE` per node, two `MATCH` + one `MERGE` per edge. No bulk path exists today. |
| `_upsert_parsed_file` issues per-row calls inside the batched transaction | Confirmed — [`indexer.py` lines 281–520](../src/sqlcg/indexer/indexer.py): `db.upsert_node(...)` / `db.upsert_edge(...)` are called inside `for table in parsed.defined_tables`, `for col_name in qnode.defined_columns`, `for stmt in parsed.statements`, `for edge in stmt.column_lineage`, etc. For a typical file (1 table, 20 columns, 15 lineage edges, ~5 source tables) this is ~100 calls. |
| `_pk_field` is on the abstract base class | Confirmed — [`graph_db.py` lines 127–143](../src/sqlcg/core/graph_db.py). Bulk variants reuse the same primary-key field convention; no schema change. |
| `_validate_props` is on the abstract base class | Confirmed — [`graph_db.py` lines 145–159](../src/sqlcg/core/graph_db.py). Bulk variants must apply the same per-row validation to prevent Cypher key injection. |
| `resolve_pass2` always re-reads file from disk and re-parses | Confirmed — [`aggregator.py` lines 45–73](../src/sqlcg/lineage/aggregator.py): `sql = parsed.path.read_text(...)` followed by `return parser.parse_file(parsed.path, sql)`. No cross-file-dependency predicate today. |
| Cross-file source registry is the union of `aggregator.sources` (defined tables) and `aggregator.cross_file_sources` (CTAS bodies) | Confirmed — [`aggregator.py` lines 21–43](../src/sqlcg/lineage/aggregator.py). Skip predicate must reference both. |
| `parsed.sources` (statement-level) holds the list of source `TableRef`s for each statement | Confirmed — [`parsers/base.py` line 182](../src/sqlcg/parsers/base.py) — `sources: list[TableRef] = field(default_factory=list)` on the statement node. A file's "cross-file dependency set" is the union of `s.sources` for `s in parsed.statements` whose `full_id` is not a same-file `defined_tables` entry. |
| `_index_single_file` shutdown blocks on timeout | Confirmed — [`indexer.py` lines 246–254](../src/sqlcg/indexer/indexer.py): `with ThreadPoolExecutor(max_workers=1) as ex:` exits via `__exit__ → shutdown(wait=True)`, which awaits the still-running worker. `future.result(timeout=N)` raising `FuturesTimeout` does not cancel the work. `executor.shutdown(wait=False, cancel_futures=True)` available since Python 3.9 — confirmed Python 3.12 minimum in `pyproject.toml`. |
| Sprint-07 batching commits the `with db.transaction()` once per `batch_size` files | Confirmed — [`indexer.py` lines 139–223](../src/sqlcg/indexer/indexer.py). T-01 must keep batched-commits semantics: bulk upsert still happens inside `_flush_batch`'s single `db.transaction()` for the batch. The bulk call is a per-file flush within the per-batch transaction. |
| Anchor fixtures `fixture_*.sql` exist under `tests/snowflake/anchors/` | Confirmed (sprint 07 ticket T-03). T-04 reuses them. |
| `KuzuBackend.transaction()` uses `_in_transaction` flag and forbids nesting | Confirmed — [`kuzu_backend.py` lines 290–317](../src/sqlcg/core/kuzu_backend.py). Bulk-upsert calls do NOT open their own transaction; they execute inside the active batch transaction. |
| `walk_sql_files` and `use_git=False` fallback path | Confirmed by sprint 07 verification; reused by T-04 measurement script. |
| Live profiling baseline: 23 min for 300 files | User-reported (live profiling session 2026-05-12); to be re-measured in T-04 against the same corpus. Baseline JSON is `plan/measurements/sprint_07_changelogs_noschema.json` (parse-only — 796 files, 119.6 s). Sprint-08 measurement adds the upsert pass timing on the same corpus. |
| 1,148 `col_lineage:NULL:Cannot find column 'NULL' in query` errors are E1 (not E5) | Confirmed via baseline JSON `error_summary.E1.count == 1148` and `error_summary.E5.count == 1148` — both buckets contain the same `NULL` / `CURRENT_TIMESTAMP()` / literal-as-column messages. T-05 records the classification but does not fix. |

---

## Ticket Table

| ID | Title | Primary Files | Effort | Priority | Depends on | Blocks |
|----|-------|--------------|--------|----------|-----------|--------|
| T-01 | Bulk upsert API + indexer rewrite (`UNWIND $rows`) | [`graph_db.py`](../src/sqlcg/core/graph_db.py), [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py), [`indexer.py`](../src/sqlcg/indexer/indexer.py) | L | 1 — highest | INDEPENDENT | T-04 |
| T-02 | Skip pass-2 re-parse when file has no cross-file sources | [`aggregator.py`](../src/sqlcg/lineage/aggregator.py) | M | 2 | INDEPENDENT | T-04 |
| T-03 | Fix `--timeout-per-file` to actually cancel | [`indexer.py`](../src/sqlcg/indexer/indexer.py) | XS | 3 | INDEPENDENT | NONE |
| T-04 | MEAS-FULLINDEX + live anchor re-query | new measurement file + ARCHITECTURE_REVIEW.md note | M | 4 | T-01, T-02 | NONE |
| T-05 | Suppress E1 false fires for pure-literal SELECT expressions | [`parsers/base.py`](../src/sqlcg/parsers/base.py) | XS | 5 | INDEPENDENT | T-04 (measurement should reflect the drop) |

---

## Recommended Implementation Order

### Single-Developer Sequence

1. **T-03** first — XS, isolated, unblocks subsequent measurements that need a usable timeout. Land before T-01 so any slow parser is killed cleanly during T-04 runs.
2. **T-01** — highest-impact perf change. Land + measure on the synthetic corpus before moving on.
3. **T-02** — pass-2 skip. Lower risk; independent of T-01 but easier to verify after T-01's bulk-upsert tests are green.
4. **T-05** — 2-line literal-skip fix in `base.py`. Land before T-04 so the measurement reflects the E1 drop.
5. **T-04** — measurement task; only meaningful after T-01, T-02, and T-05 are merged.

### Two-Developer Sequence

- Track A: T-03 → T-01 → T-04 (joint)
- Track B: T-02 → T-05 → T-04 (joint)

---

## Ticket Specifications

---

### T-01 — Bulk upsert API + indexer rewrite (`UNWIND $rows`)

**Source**: User live-profiling finding ("PRIMARY — bulk upsert"). Reinforced by [`ARCHITECTURE_REVIEW.md` § 11.6](../ARCHITECTURE_REVIEW.md) and CLAUDE.md perf budget (1,600 files under 5 minutes).

**Effort**: L
**Depends on**: INDEPENDENT (sprint-07 batched commits already merged)
**Blocks**: T-04

#### Root cause

Inside the sprint-07 per-batch transaction, [`_upsert_parsed_file`](../src/sqlcg/indexer/indexer.py) makes one `self._conn.execute(...)` call per node and per edge. For a typical file:

- 1 File node + 1 SqlTable node + (DEFINED_IN edge) = 3 calls
- 20 columns: 20 `SqlColumn` node calls + 20 `HAS_COLUMN` edge calls = 40 calls
- 3 statements × (1 Query node + 1 QUERY_DEFINED_IN edge + 2 source tables + 2 SELECTS_FROM edges + 5 lineage edges × 2 endpoint nodes + 5 COLUMN_LINEAGE edges) ≈ 60 calls

Total ≈ 100 round-trips per file. For 300 files → ~30,000 statements. Each is a parsed Cypher call with parameter binding overhead inside KuzuDB; the per-call fixed cost dominates the 23-minute runtime.

`UNWIND $rows AS row MERGE (...)` lets KuzuDB process N rows in one parser/binder pass with one execution plan. For the same file, the upsert collapses to:

- 1 bulk `File` node call (1-row)
- 1 bulk `SqlTable` node call (1-row, or N-rows for src tables)
- 1 bulk `SqlColumn` node call (20-rows)
- 1 bulk `HAS_COLUMN` edge call (20-rows)
- 1 bulk `SqlQuery` node call (3-rows)
- 1 bulk `COLUMN_LINEAGE` edge call (15-rows)
- … etc.

Target: ~100 calls/file → 8–12 calls/file. Combined with sprint-07's batching, the expected speedup on the 23-minute corpus is 5–10×.

#### What to do

**Step 1.1 — Extend the abstract base [`graph_db.py`](../src/sqlcg/core/graph_db.py)**

Add two abstract methods after `upsert_edge`:

```python
@abstractmethod
def upsert_nodes_bulk(
    self,
    label: str,
    rows: list[dict[str, Any]],
) -> None:
    """Bulk-upsert nodes of one label in a single backend round-trip.

    Each row dict must contain the primary-key field for `label` (see _pk_field)
    plus any other properties to SET. All rows must share the same property-key
    set; backends MAY raise if rows are heterogeneous (KuzuBackend does).

    Idempotent MERGE semantics, identical to upsert_node per row.

    Args:
        label: Node label (e.g., NodeLabel.COLUMN)
        rows: List of property dicts. Empty list is a no-op.
    """

@abstractmethod
def upsert_edges_bulk(
    self,
    src_label: str,
    dst_label: str,
    rel_type: str,
    rows: list[dict[str, Any]],
) -> None:
    """Bulk-upsert edges of one (src_label, rel_type, dst_label) triple.

    Each row dict must contain:
      - "src_key": source primary-key value (matches src_label _pk_field)
      - "dst_key": destination primary-key value (matches dst_label _pk_field)
      - Any additional keys are set as edge properties.

    Idempotent MERGE semantics, identical to upsert_edge per row. Rows whose
    src or dst node does not exist are silently skipped by KuzuDB's MERGE
    semantics — callers must ensure node upserts happen first within the same
    transaction (see indexer ordering rules in _upsert_parsed_file).

    Args:
        src_label: Source node label
        dst_label: Destination node label
        rel_type: Relationship type
        rows: List of edge property dicts. Empty list is a no-op.
    """
```

**Step 1.2 — Implement bulk variants in [`kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)**

```python
def upsert_nodes_bulk(self, label: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    # Validate all property keys across all rows (same guard as upsert_node)
    for row in rows:
        self._validate_props(row)

    pk_field = self._pk_field(label)
    # Determine the property key set from the first row; require homogeneity.
    keys = list(rows[0].keys())
    if pk_field not in keys:
        raise ValueError(
            f"upsert_nodes_bulk({label}): every row must include primary key '{pk_field}'"
        )
    for i, row in enumerate(rows[1:], 1):
        if set(row.keys()) != set(keys):
            raise ValueError(
                f"upsert_nodes_bulk({label}): row {i} has property keys "
                f"{sorted(row.keys())}, expected {sorted(keys)}"
            )

    set_keys = [k for k in keys if k != pk_field]
    # UNWIND $rows AS row MERGE (n:Label {pk: row.pk}) SET n.k = row.k, ...
    query = f"UNWIND $rows AS row MERGE (n:{label} {{{pk_field}: row.{pk_field}}})"
    if set_keys:
        set_parts = [f"n.{k} = row.{k}" for k in set_keys]
        query += f" SET {', '.join(set_parts)}"

    try:
        self._conn.execute(query, {"rows": rows})
    except Exception as e:
        logger.error(f"upsert_nodes_bulk failed: {label} ({len(rows)} rows): {e}")
        raise


def upsert_edges_bulk(
    self,
    src_label: str,
    dst_label: str,
    rel_type: str,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    for row in rows:
        # src_key/dst_key are not graph properties; validate the remainder.
        props = {k: v for k, v in row.items() if k not in ("src_key", "dst_key")}
        self._validate_props(props)

    src_pk = self._pk_field(src_label)
    dst_pk = self._pk_field(dst_label)

    keys = list(rows[0].keys())
    for required in ("src_key", "dst_key"):
        if required not in keys:
            raise ValueError(
                f"upsert_edges_bulk({src_label}->{rel_type}->{dst_label}): "
                f"every row must include '{required}'"
            )
    for i, row in enumerate(rows[1:], 1):
        if set(row.keys()) != set(keys):
            raise ValueError(
                f"upsert_edges_bulk: row {i} has property keys {sorted(row.keys())}, "
                f"expected {sorted(keys)}"
            )

    prop_keys = [k for k in keys if k not in ("src_key", "dst_key")]
    query = (
        f"UNWIND $rows AS row "
        f"MATCH (src:{src_label} {{{src_pk}: row.src_key}}) "
        f"MATCH (dst:{dst_label} {{{dst_pk}: row.dst_key}}) "
        f"MERGE (src)-[r:{rel_type}]->(dst)"
    )
    if prop_keys:
        set_parts = [f"r.{k} = row.{k}" for k in prop_keys]
        query += f" SET {', '.join(set_parts)}"

    try:
        self._conn.execute(query, {"rows": rows})
    except Exception as e:
        logger.error(
            f"upsert_edges_bulk failed: {src_label}->{rel_type}->{dst_label} "
            f"({len(rows)} rows): {e}"
        )
        raise
```

Notes:
- Empty-list short-circuit means the indexer can call these unconditionally.
- Homogeneity check is intentional: heterogeneous property sets would require multiple Cypher plans and defeat the purpose. The indexer must group rows by property-key set before calling. In practice all nodes of a given label written by `_upsert_parsed_file` share the same property keys (verified in step 1.4 below).
- `MERGE` semantics in KuzuDB silently skip rows whose `MATCH` side fails (no node found). This matches single-edge behaviour: caller must upsert nodes first.

**Step 1.3 — Indexer rewrite ([`indexer.py`](../src/sqlcg/indexer/indexer.py) `_upsert_parsed_file`)**

The current method does an inline call sequence. Replace with a row-collection phase followed by a flush phase. Sketch:

```python
def _upsert_parsed_file(self, parsed, db, gold_tables=frozenset()) -> dict:
    # NOTE: the pre-T-01 counts dict (indexer.py line 278) omits "star_sources";
    #       the rewrite initialises it here so _flush_batch can use counts["star_sources"]
    #       without the .get() workaround that currently exists at line 160.
    counts = {"tables": 0, "edges": 0, "columns_defined": 0, "star_sources": 0}

    file_rows: list[dict] = []
    table_rows: list[dict] = []                # NodeLabel.TABLE
    column_rows: list[dict] = []               # NodeLabel.COLUMN
    query_rows: list[dict] = []                # NodeLabel.QUERY

    defined_in_edges: list[dict] = []          # TABLE -> FILE
    has_column_edges: list[dict] = []          # TABLE -> COLUMN
    query_defined_in_edges: list[dict] = []    # QUERY -> FILE
    selects_from_edges: list[dict] = []        # QUERY -> TABLE
    column_lineage_edges: list[dict] = []      # COLUMN -> COLUMN
    star_source_edges: list[dict] = []         # QUERY -> TABLE

    # --- Phase A: collect rows from parsed model ---
    file_rows.append({"path": parsed.path_str, "dialect": parsed.dialect or ""})
    # ... per-table, per-column, per-statement, per-lineage-edge population
    # (mechanical translation of the current inline calls — see lines 281–520)
    # Duplicate-DDL run_read check stays inline (one read per defined table is fine).

    # --- Phase B: flush in dependency order (nodes before their edges) ---
    db.upsert_nodes_bulk(NodeLabel.FILE, file_rows)
    db.upsert_nodes_bulk(NodeLabel.TABLE, table_rows)
    db.upsert_nodes_bulk(NodeLabel.COLUMN, column_rows)
    db.upsert_nodes_bulk(NodeLabel.QUERY, query_rows)

    db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.FILE, RelType.DEFINED_IN, defined_in_edges)
    db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.COLUMN, RelType.HAS_COLUMN, has_column_edges)
    db.upsert_edges_bulk(NodeLabel.QUERY, NodeLabel.FILE, RelType.QUERY_DEFINED_IN, query_defined_in_edges)
    db.upsert_edges_bulk(NodeLabel.QUERY, NodeLabel.TABLE, RelType.SELECTS_FROM, selects_from_edges)
    db.upsert_edges_bulk(NodeLabel.COLUMN, NodeLabel.COLUMN, RelType.COLUMN_LINEAGE, column_lineage_edges)
    db.upsert_edges_bulk(NodeLabel.QUERY, NodeLabel.TABLE, RelType.STAR_SOURCE, star_source_edges)

    return counts
```

**Homogeneity constraints (must verify per row group)**:

| Group | Keys |
|-------|------|
| `file_rows` | `path`, `dialect` |
| `table_rows` | `qualified`, `name`, `catalog`, `db`, `kind`, `defined_in_file` |
| `column_rows` | `id`, `col_name`, `table_qualified`, `catalog`, `db`, `table_name` |
| `query_rows` | `id`, `file_path`, `statement_index`, `sql`, `kind`, `target_table`, `parse_failed`, `confidence`, `parsing_mode` |
| `defined_in_edges` | `src_key`, `dst_key` |
| `has_column_edges` | `src_key`, `dst_key`, `source` |
| `query_defined_in_edges` | `src_key`, `dst_key` |
| `selects_from_edges` | `src_key`, `dst_key` |
| `column_lineage_edges` | `src_key`, `dst_key`, `transform`, `confidence`, `query_id` |
| `star_source_edges` | `src_key`, `dst_key`, `qualifier`, `target_table`, `confidence` |

These are the **exact** property keys currently written by the per-row paths at [`indexer.py` lines 281–520](../src/sqlcg/indexer/indexer.py). The bulk rewrite is a 1:1 collapse — no schema change, no new properties, no removed properties.

**Step 1.4 — Inline duplicate-DDL detection retained**

The duplicate-DDL `db.run_read(...)` check at lines 293–308 stays inline because:
- It is one read per `defined_tables` entry (rare — typically 1 per file).
- Its result feeds a `logger.warning` plus an `errors.append`, not a node/edge mutation.
- Moving it to a bulk read offers no measurable saving.

**Step 1.5 — Star-source target table upsert (lines 502–520)**

The existing code unconditionally upserts the statement's target table when it is not in `defined_tables`. This becomes a contribution to `table_rows` (same keys as other table rows; `defined_in_file=""`). Verify there is no dual-write to the same table key within one file's flush — Cypher `MERGE` is idempotent under deduplication but we still want to deduplicate in Python (cheap, smaller payload):

```python
table_rows = list({r["qualified"]: r for r in table_rows}.values())
column_rows = list({r["id"]: r for r in column_rows}.values())
query_rows = list({r["id"]: r for r in query_rows}.values())
```

Add the dedupe between Phase A and Phase B. Edges do not need dedupe because the (src,dst,rel) tuple already merges.

**Step 1.6 — Keep `_flush_batch` semantics**

`_upsert_parsed_file` is still called inside `_flush_batch`'s active transaction. The bulk calls do NOT open their own transaction (they use `self._conn.execute`, which honours the active `BEGIN TRANSACTION`). No change to [`indexer.py` lines 139–223](../src/sqlcg/indexer/indexer.py) is required.

#### Wiring verification

Before opening the PR, run (all must succeed):

- `grep -n "upsert_nodes_bulk\|upsert_edges_bulk" src/sqlcg/core/graph_db.py` — abstract declarations
- `grep -n "def upsert_nodes_bulk\|def upsert_edges_bulk" src/sqlcg/core/kuzu_backend.py` — implementations
- `grep -n "upsert_nodes_bulk\|upsert_edges_bulk" src/sqlcg/indexer/indexer.py` — at least 10 call sites in `_upsert_parsed_file` (4 node groups + 6 edge groups). **Critical**: a function defined but never called is a zero-value delivery.
- `grep -cn "db.upsert_node(\|db.upsert_edge(" src/sqlcg/indexer/indexer.py` — must return 0 after rewrite. **Verified**: all 15 pre-T-01 call sites are inside `_upsert_parsed_file` only; `_reindex_view_definition` and `_expand_star_sources` use `run_read` only and contain zero `upsert_node`/`upsert_edge` calls. After the rewrite the count must be exactly 0.
- `grep -n "TODO" src/sqlcg/indexer/indexer.py src/sqlcg/core/kuzu_backend.py` — zero matches in the happy path.

#### Files affected

- [`src/sqlcg/core/graph_db.py`](../src/sqlcg/core/graph_db.py)
- [`src/sqlcg/core/kuzu_backend.py`](../src/sqlcg/core/kuzu_backend.py)
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py)

#### Tests to add

Create `tests/integration/test_bulk_upsert.py`:

- **Scenario A — bulk and per-row produce identical graph state** (single most important regression guard):
  Setup: same 12-file synthetic corpus used by sprint-07 `test_indexer_batching.py::test_batch_size_equivalence_1_vs_50`.
  Action: index once with the new bulk path; in a second backend, index using a monkeypatched `Indexer` that calls the legacy single-row `upsert_node`/`upsert_edge` (achieved via a debug-only branch is NOT allowed — instead, freeze the post-T-01 single-row code as a test-local helper that mimics the pre-rewrite logic, or use `tests/integration/test_indexer_batching.py`'s existing post-sprint-07 baseline as the comparison run).
  Assertion: identical `(tables_set, columns_set, edges_count, query_count)`.

- **Scenario B — `upsert_nodes_bulk` with `[]` is a no-op**:
  Action: `backend.upsert_nodes_bulk(NodeLabel.COLUMN, [])`.
  Assertion: no exception; node count unchanged.

- **Scenario C — `upsert_nodes_bulk` rejects heterogeneous rows**:
  Action: call with `[{"id": "x"}, {"id": "y", "col_name": "c"}]`.
  Assertion: `pytest.raises(ValueError, match="property keys")`.

- **Scenario D — `upsert_edges_bulk` requires `src_key`/`dst_key`**:
  Action: call with `[{"src_key": "a"}]` (no `dst_key`).
  Assertion: `pytest.raises(ValueError, match="dst_key")`.

- **Scenario E — anchor `omloopsnelheid` survives the rewrite**:
  Index the existing `tests/snowflake/anchors/fixture_omloopsnelheid.sql` file (re-use the sprint-07 `tests/integration/test_live_anchors.py` helper) and assert ≥1 `COLUMN_LINEAGE` edge into a temp table where `LOWER(d.col_name) = 'omloopsnelheid'`. This is the sprint-07 live anchor test re-run; it must keep passing.

- **Scenario F — call-count shape**:
  Patch `KuzuBackend._conn.execute` to record the number of `MERGE` / `UNWIND` statements per file flush.
  Index a single file with 1 table + 20 columns + 5 lineage edges.
  Assertion: `<= 12` distinct execute() calls in the upsert path (10 bulk groups + duplicate-DDL run_read + commit). This protects against accidental regression to per-row calls in a future patch.

- **Scenario G — bulk respects active transaction**:
  Open `db.transaction()`, call a bulk upsert, raise an exception inside the `with`, and assert nothing landed in the graph (rollback covers the bulk write too).

#### Acceptance criteria

- [ ] `upsert_nodes_bulk` and `upsert_edges_bulk` abstract methods exist in `GraphBackend` (grep confirmed).
- [ ] `KuzuBackend` implements both with `UNWIND $rows AS row MERGE …` (grep confirmed).
- [ ] `_upsert_parsed_file` calls `db.upsert_node`/`db.upsert_edge` **zero** times (grep confirmed). All node/edge writes flow through the bulk variants.
- [ ] Scenario A passes — bulk path produces identical graph state to the sprint-07 baseline on the 12-file synthetic corpus.
- [ ] Scenario E passes — `omloopsnelheid` anchor still produces ≥1 lineage edge.
- [ ] Scenario F passes — ≤ 12 `execute()` calls per file in the upsert path.
- [ ] Scenario G passes — bulk writes participate in the active transaction.
- [ ] No new TODO in the happy path of any modified file.
- [ ] Pyright + ruff clean.

---

### T-02 — Skip pass-2 re-parse when file has no cross-file sources

**Source**: User live-profiling finding ("SECONDARY — Pass 2 re-parses all files unconditionally").

**Effort**: M
**Depends on**: INDEPENDENT
**Blocks**: T-04

#### Root cause

[`CrossFileAggregator.resolve_pass2`](../src/sqlcg/lineage/aggregator.py) calls `parser.parse_file(parsed.path, sql)` for every file regardless of whether the file references any table defined elsewhere. For the 300-file profiling corpus this doubles parse time. A file whose statements only reference tables defined within the same file (or tables not in the cross-file registry at all) gains nothing from re-parsing — schema context is identical to pass 1.

The skip predicate is: **a file needs pass-2 re-parse iff any statement source / CTE-source name appears in the cross-file registry (`self.sources` ∪ `self.cross_file_sources`) AND is not defined in the same file.**

#### What to do

**Step 2.1 — Add a `_needs_pass2(parsed)` predicate to `CrossFileAggregator`**

```python
def _needs_pass2(self, parsed: ParsedFile) -> bool:
    """Return True iff pass-2 re-parse would see new schema context for this file.

    A file benefits from pass 2 iff at least one of its statement sources or
    cross-file dependency references a table registered by another file's
    pass-1 result (self.sources) or a CTAS body harvested from another file
    (self.cross_file_sources).

    Files that reference only same-file tables, or only tables absent from
    both registries, gain nothing — pass 1 already had all the schema context
    available. Skipping the re-parse for these files is observably equivalent
    to keeping it (verified by the equivalence test in tests/unit/test_aggregator_skip.py).
    """
    same_file_table_ids = {t.full_id for t in parsed.defined_tables}
    same_file_bare_names = {t.name.lower() for t in parsed.defined_tables}

    for stmt in parsed.statements:
        for src in stmt.sources:
            # Cross-file by full_id?
            if src.full_id in self.sources and src.full_id not in same_file_table_ids:
                return True
            # Cross-file by bare name (CTAS body lookup)?
            bare = (src.name or "").lower()
            if bare and bare in self.cross_file_sources and bare not in same_file_bare_names:
                return True
    return False
```

**Important scope limitation — CTE names are not in `stmt.sources`**:

The `stmt.sources` list contains `TableRef` objects for tables referenced in `FROM`/`JOIN` clauses. CTE-based references (where a CTE name shadows a real table) are stored in `stmt.ctes`, not `stmt.sources`. If a file defines a CTE that references a cross-file table, `stmt.sources` will contain the real table the CTE body selects from (populated by the parser), so the predicate will correctly detect it. However, a file that uses a CTE whose *definition* is held in `self.cross_file_sources` (a CTAS CTE body from another file) will only be detected via the bare-name check on `stmt.sources`. If the cross-file CTAS body is referenced by CTE name and the CTE name does not appear in `stmt.sources`, the predicate may return False (false negative). The equivalence test in Scenario D must include a cross-file CTE body reference to verify this path.

**Step 2.2 — Use the predicate in `resolve_pass2`**

```python
def resolve_pass2(self, parser, parsed: ParsedFile) -> ParsedFile:
    parser._schema.add_view_sources(self.sources)

    if not self._needs_pass2(parsed):
        # File has no cross-file dependencies — pass-1 result is already final.
        return parsed

    try:
        sql = parsed.path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
            parsed.path,
            exc,
        )
        return parsed

    return parser.parse_file(parsed.path, sql)
```

**Step 2.3 — Track skip count in indexer summary (optional, recommended)**

In [`indexer.py`](../src/sqlcg/indexer/indexer.py) Pass-2 loop (lines 112–119), capture how many files were skipped to surface the optimisation impact:

```python
pass2_skipped = 0
for parsed in pass1_results:
    try:
        resolved = aggregator.resolve_pass2(parser, parsed)
        if resolved is parsed:           # identity = no re-parse happened
            pass2_skipped += 1
        pass2_results.append(resolved)
    except Exception as exc:
        logger.warning("resolve_pass2 failed for %s: %s", parsed.path, exc)
        pass2_results.append(parsed)
```

Add `"pass2_skipped": pass2_skipped` to the summary dict returned by `index_repo`. Surface as a measurement signal in T-04.

**Critical correctness rule**: identity comparison (`resolved is parsed`) is **only** safe if `resolve_pass2` returns the SAME `ParsedFile` object on skip. Step 2.2 does exactly that (`return parsed`). Do not introduce a `.copy()` on the skip path — that would break the identity check.

#### Wiring verification

- `grep -n "_needs_pass2" src/sqlcg/lineage/aggregator.py` — definition + one call site in `resolve_pass2`.
- `grep -n "pass2_skipped" src/sqlcg/indexer/indexer.py` — initialiser + identity check + return-dict entry.
- `grep -n "TODO" src/sqlcg/lineage/aggregator.py` — zero matches in happy path.

#### Files affected

- [`src/sqlcg/lineage/aggregator.py`](../src/sqlcg/lineage/aggregator.py)
- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py)

#### Tests to add

Create `tests/unit/test_aggregator_skip.py`:

- **Scenario A — file with no cross-file sources skips pass 2**:
  Build a `CrossFileAggregator` with two `ParsedFile` objects: `f_a.sql` defines `db.s.a` and references only `db.s.a` (same-file), `f_b.sql` defines `db.s.b` and references only `db.s.b`. Call `aggregator.register_pass1` on both.
  Action: `result = aggregator.resolve_pass2(mock_parser, f_a)`.
  Assertion: `result is f_a` (no re-parse), and `mock_parser.parse_file.call_count == 0`.

- **Scenario B — file with cross-file source triggers pass 2**:
  Same setup, but `f_a.sql` now references `db.s.b` (cross-file).
  Action: `result = aggregator.resolve_pass2(mock_parser, f_a)`.
  Assertion: `mock_parser.parse_file.call_count == 1`.

- **Scenario C — file with CTAS-body bare-name reference triggers pass 2**:
  Setup: `f_a.sql` has a CTAS that defines temp table `tmp_x` (registered in `cross_file_sources['tmp_x']`). `f_b.sql` references `tmp_x` as a bare name.
  Action: `aggregator.resolve_pass2(mock_parser, f_b)`.
  Assertion: re-parse triggered.

- **Scenario D — graph equivalence end-to-end**:
  Integration-style: index a 4-file corpus where 2 files are cross-file-dependent and 2 are not. Run twice — once with the skip predicate enabled (post-T-02), once with a monkeypatched `_needs_pass2` that always returns `True` (pre-T-02 behaviour). Compare graph states.
  Assertion: identical edge counts, identical table sets, identical column-lineage edge sets. **This is the single most important regression guard for T-02.**

- **Scenario E — pass2_skipped is observable**:
  Action: index the 4-file corpus from Scenario D.
  Assertion: `summary["pass2_skipped"] == 2`.

#### Acceptance criteria

- [ ] `CrossFileAggregator._needs_pass2(parsed)` exists and is called from `resolve_pass2` (grep confirmed).
- [ ] Scenario D passes — graph state identical with/without the skip.
- [ ] Scenario E passes — `pass2_skipped` reported in summary.
- [ ] Identity-return contract documented in `resolve_pass2` docstring (so future maintainers do not break the `is` check by adding a `.copy()`).
- [ ] No new TODO in happy path.

---

### T-03 — Fix `--timeout-per-file` to actually cancel

**Source**: User live-profiling finding ("TERTIARY — `--timeout-per-file` is a no-op").

**Effort**: XS
**Depends on**: INDEPENDENT
**Blocks**: NONE

#### Root cause

[`Indexer._index_single_file`](../src/sqlcg/indexer/indexer.py) lines 246–254:

```python
with ThreadPoolExecutor(max_workers=1) as ex:
    future = ex.submit(parser.parse_file, path, sql)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout:
        logger.warning("Timeout parsing %s (>%ds) — skipping", path, timeout)
        out = ParsedFile(path=path, dialect=parser.DIALECT)
        out.errors.append(f"timeout:{timeout}s")
        return out
```

When `future.result(timeout=N)` raises `FuturesTimeout`, control flow falls through the `except`, then exits the `with` block. The `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)` — which blocks until the parser worker finishes. The user-observed effect is "timeout is silently ignored on slow files".

#### What to do

Refactor to manage the executor lifecycle explicitly so the timeout path bypasses the join:

```python
def _index_single_file(self, parser, path: Path, sql: str, timeout: int) -> ParsedFile:
    """Parse one file, with optional timeout.

    On timeout (>0 seconds), the worker thread is abandoned via
    executor.shutdown(wait=False, cancel_futures=True) so the indexer does not
    block waiting for a runaway parser to return. The worker may continue
    running in the background until it naturally completes; its result is
    discarded. This matches the user expectation that --timeout-per-file
    is a hard cap on indexing latency per file.
    """
    if timeout <= 0:
        return parser.parse_file(path, sql)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(parser.parse_file, path, sql)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            logger.warning("Timeout parsing %s (>%ds) — skipping", path, timeout)
            out = ParsedFile(path=path, dialect=parser.DIALECT)
            out.errors.append(f"timeout:{timeout}s")
            return out
    finally:
        # On the success path: a normal shutdown(wait=True) is fine and fast.
        # On the timeout path: shutdown(wait=False, cancel_futures=True) lets the
        # method return immediately. cancel_futures requires Python 3.9+ (we are 3.12).
        # The orphaned thread will exit when sqlglot finishes; this is acceptable
        # because the process-wide thread cap is bounded by indexer concurrency=1.
        executor.shutdown(wait=False, cancel_futures=True)
```

**Note on the `finally` block — success path vs. timeout path**:

`executor.shutdown(wait=False, cancel_futures=True)` is called unconditionally in the `finally` block for both the success path and the timeout path. On the success path, `future` is already resolved when `future.result(timeout=timeout)` returns, so `cancel_futures=True` is a no-op and `wait=False` means the executor's internal cleanup happens asynchronously. In practice, since the one worker thread has already finished, this is instantaneous. The plan's inline comment ("On the success path: a normal shutdown(wait=True) is fine and fast") is accurate — using `wait=False` here is safe but slightly diverges from the comment's intent. If strict cleanup on the success path is desired, branch on a `timed_out` flag; however, the current unconditional design is simpler and passes Scenario B.

**Caveat for the developer**: Python threads cannot be force-killed. Orphaning a slow-parser thread is the best we can do at this layer. The thread leaks until the sqlglot call returns naturally — typically seconds to minutes after the timeout. For the worst-case file (sprint-06 baseline: 22.74 s for `ODS_WORKAROUND_ARTIKELDIMENSIE_CONCEPT.sql`) at a 30 s timeout, the leak is bounded; at a 5 s timeout it may linger up to ~18 s. Acceptable per the architecture review.

#### Wiring verification

- `grep -n "shutdown(wait=False, cancel_futures=True)" src/sqlcg/indexer/indexer.py` — must appear exactly once.
- `grep -n "with ThreadPoolExecutor" src/sqlcg/indexer/indexer.py` — must NOT appear in `_index_single_file` after the fix (replaced by explicit `try/finally`).
- `grep -n "TODO" src/sqlcg/indexer/indexer.py` — zero matches in happy path.

#### Files affected

- [`src/sqlcg/indexer/indexer.py`](../src/sqlcg/indexer/indexer.py) — `_index_single_file` only

#### Tests to add

Create `tests/unit/test_timeout_cancel.py`:

- **Scenario A — slow parser is cut off and indexer returns the timeout marker**:
  Use a fake parser whose `parse_file` calls `time.sleep(10)` and returns a `ParsedFile`. Call `_index_single_file(fake_parser, Path("x.sql"), "select 1", timeout=1)` and **measure wall-clock time** via `time.perf_counter()`.
  Assertion: returns within `< 2 seconds`, returned `ParsedFile.errors == ["timeout:1s"]`. The `< 2 s` bound proves the executor did not wait on the worker.

- **Scenario B — fast parser returns normally**:
  Fake parser returns immediately.
  Action: `_index_single_file(fake_parser, Path("x.sql"), "select 1", timeout=5)`.
  Assertion: wall-clock < 1 s, returned `ParsedFile.errors == []`, and `parse_file` was called exactly once.

- **Scenario C — `timeout=0` bypasses the executor entirely**:
  Action: `_index_single_file(fake_parser, Path("x.sql"), "select 1", timeout=0)`.
  Assertion: the result comes directly from `parser.parse_file` (the executor branch is not entered — verify by patching `ThreadPoolExecutor` and asserting it is NOT instantiated).

#### Acceptance criteria

- [ ] `executor.shutdown(wait=False, cancel_futures=True)` is called in the `finally` block (grep confirmed).
- [ ] Scenario A wall-clock assertion passes (< 2 s for a 1 s timeout on a 10 s sleep).
- [ ] Scenario B happy path unchanged.
- [ ] Scenario C `timeout=0` short-circuit preserved.
- [ ] No new TODO in `_index_single_file`.

---

### T-04 — MEAS-FULLINDEX + live anchor re-query

**Source**: User request — confirm 5-minute budget reachable post-fix; record real-corpus anchor edge counts.

**Effort**: M (measurement task; one full-corpus index run plus query scripting)
**Depends on**: T-01, T-02 (must be merged; T-03 not strictly required but should land first)
**Blocks**: NONE

#### Root cause

Sprint 07 measurement ([`sprint_07_changelogs_noschema.json`](measurements/sprint_07_changelogs_noschema.json)) captured **parse-only** timing on 796 files in 119.6 s. The current sprint's regression is in the upsert pass, which sprint-07 did not measure end-to-end. Without a full-index timing post-T-01/T-02, sprint 08 has no quantitative basis to declare the perf budget met.

Two anchor columns (`omloopsnelheid`, `ma_aantal_op_order`) must be queried against the real corpus graph (not the synthetic fixtures of sprint-07's `test_live_anchors.py`). The synthetic tests pass 3/3 today; this ticket validates that the real corpus also produces edges for the two anchors.

#### What to do

**Step 4.1 — Full-corpus index run**

Index the same corpus used in the sprint-07 baseline:

```bash
uv run sqlcg index /home/ignwrad/Projects/dwh/ddl/changelogs \
    --dialect snowflake \
    --batch-size 50 \
    --db-path /tmp/sqlcg_sprint08.kuzu \
    --timeout-per-file 30
```

Record wall-clock time (`time` prefix or `--summary-json` if available).

**Step 4.2 — Measurement JSON**

Write a JSON file at `plan/measurements/sprint_08_changelogs_fullindex.json` containing at least:

```json
{
  "run_date": "<ISO8601>",
  "corpus_dir": "/home/ignwrad/Projects/dwh/ddl/changelogs",
  "dialect": "snowflake",
  "total_files": 796,
  "runtime_seconds_total": <float>,
  "runtime_seconds_parse": <float>,
  "runtime_seconds_upsert": <float>,
  "pass2_skipped": <int>,
  "lineage_edges_created": <int>,
  "tables_found": <int>,
  "columns_defined": <int>,
  "batch_size": 50,
  "anchor_omloopsnelheid": {
    "lineage_edge_count": <int>,
    "sample_edges": [
      {"src": "...", "dst": "...", "transform": "..."}
    ]
  },
  "anchor_ma_aantal_op_order": {
    "target_column_exists": <bool>,
    "lineage_edge_count_to_target": <int>,
    "lineage_edge_count_from_source": <int>,
    "sample_edges": [...]
  },
  "comparison_with_sprint_07": {
    "parse_only_baseline_seconds": 119.6,
    "parse_seconds_now": <float>,
    "parse_delta_pct": <float>,
    "e1_count_baseline": 1148,
    "e1_count_now": <int>,
    "e1_null_subcount_now": <int>
  },
  "budget_check_1600_files": {
    "extrapolated_seconds": <float>,
    "budget_seconds": 300,
    "passes_budget": <bool>
  }
}
```

The developer scripts the anchor queries inline (no new test file is required — T-04 is a measurement task, not a test ticket). Suggested query for `omloopsnelheid`:

```cypher
MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn)
WHERE LOWER(d.col_name) = 'omloopsnelheid'
RETURN s.table_name AS s_t, s.col_name AS s_c,
       d.table_name AS d_t, d.col_name AS d_c, e.transform AS t
LIMIT 50
```

For `ma_aantal_op_order`:

```cypher
MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn)
WHERE (LOWER(d.col_name) = 'ma_aantal_op_order')
   OR (LOWER(s.col_name) = 'ma_aantal_op_order')
RETURN s.table_qualified AS s_q, s.col_name AS s_c,
       d.table_qualified AS d_q, d.col_name AS d_c, e.transform AS t
LIMIT 50
```

**Step 4.3 — Budget extrapolation**

The corpus is 796 files; the budget is 1,600 files in < 5 minutes. Linear extrapolation:

```
extrapolated_seconds = runtime_seconds_total * (1600 / 796)
```

If `extrapolated_seconds < 300`, mark `passes_budget: true`. Otherwise the postmortem must record the gap and the next-sprint hypothesis (parallel parsing per § 11.6, or further bulk-call tuning).

**Step 4.4 — ARCHITECTURE_REVIEW.md update**

Append `### 11.X Sprint-08 Bulk-Upsert Re-measurement (2026-05-12)` with a table:

| Metric | Sprint-07 baseline | Sprint-08 result | Delta | Sign |
|--------|-------------------:|----------------:|------:|:-----|
| Parse-only seconds (796 files) | 119.6 | __ | __ | __ |
| Full-index seconds (796 files) | (not measured) | __ | __ | n/a |
| Extrapolated to 1,600 files | (~23 min reported live) | __ | __ | __ |
| Passes 5-minute budget | ❌ | __ | __ | __ |
| `omloopsnelheid` edge count | 0 (real corpus, unmeasured) | __ | __ | __ |
| `ma_aantal_op_order` edges | 0 (xfail) | __ | __ | __ |
| `pass2_skipped` count | 0 (not implemented) | __ | __ | __ |
| E1 count (`col_lineage:NULL` total) | 1148 | __ | __ | __ |
| E1 `NULL` literal subset | 1148 (all literals) | __ | __ | __ |

"Sign": ✅ improved, ➖ unchanged within ±5%, ❌ regression.

**Step 4.5 — Decision rule**

- If budget passes AND `omloopsnelheid` edge count ≥ 1 in the real corpus: sprint 08 closes; sprint 09 can target E36 / cross-file scope.
- If budget passes but `omloopsnelheid` real-corpus count is 0: open a sprint-09 finding on "synthetic-pass / real-fail" parser gap and re-rank.
- If budget does NOT pass: do NOT close the sprint. Re-open T-01 with a follow-up sub-ticket and consult the architect-reviewer on whether parallel parsing must be brought forward.

#### Wiring verification

- `ls plan/measurements/sprint_08_changelogs_fullindex.json` — file exists, ≥ 1 KB.
- `grep -c "anchor_omloopsnelheid\|anchor_ma_aantal_op_order\|budget_check_1600_files" plan/measurements/sprint_08_changelogs_fullindex.json` — at least 3 matches.
- `grep -n "### 11.X Sprint-08 Bulk-Upsert Re-measurement" ARCHITECTURE_REVIEW.md` — section exists and table populated (no `__` placeholders).

#### Files affected

- `plan/measurements/sprint_08_changelogs_fullindex.json` — NEW
- [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — new subsection

#### Tests to add

None — measurement task.

#### Acceptance criteria

- [ ] `plan/measurements/sprint_08_changelogs_fullindex.json` exists with the fields listed in Step 4.2.
- [ ] `ARCHITECTURE_REVIEW.md` has the populated comparison table (no `__` placeholders).
- [ ] Budget check (`passes_budget` cell) is set explicitly to `true` or `false` with the extrapolated number visible.
- [ ] Decision rule outcome (close / re-open / escalate) is recorded in the new subsection.

---

### T-05 — Suppress E1 false fires for pure-literal SELECT expressions

**Source**: User profiling finding — 1,148 occurrences of `col_lineage:NULL:Cannot find column 'NULL' in query` in [`sprint_07_changelogs_noschema.json`](measurements/sprint_07_changelogs_noschema.json) (`E1 = 1148`). Root cause fully explored.

**Effort**: XS (2 lines + tests)
**Depends on**: INDEPENDENT
**Blocks**: T-04 (measurement must reflect the E1 drop)

#### Root cause

`INSERT INTO t (a, b) SELECT NULL, CURRENT_TIMESTAMP()` passes the star-skip guard in [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py). At [`base.py` line ~614](../src/sqlcg/parsers/base.py), `sg_lineage('NULL', ...)` is invoked for each SELECT expression. sqlglot raises `Cannot find column 'NULL' in query` because NULL and literals are not real columns. The same path emits errors for:

- `NULL` (sqlglot `exp.Null`)
- Numeric and string literals (`exp.Literal`)
- Zero-argument functions like `CURRENT_TIMESTAMP()`, `CURRENT_DATE()`, `SYSDATE()`
- Constant boolean expressions (`exp.Boolean`)
- Arithmetic on literals only (e.g. `1 + 2`)

**Common property**: zero `exp.Column` descendants. Any expression containing at least one real column reference passes through to the existing `sg_lineage` call site unchanged.

#### What to do

**Step 5.1 — Insert the literal-skip guard at [`base.py` line 614](../src/sqlcg/parsers/base.py)**

Add the guard immediately after the star-skip block (the existing `continue` on line 613) and **before** the `if col_expr.alias:` branch (currently line 615):

```python
# Skip pure-literal expressions — NULL, numeric/string literals, zero-argument
# functions like CURRENT_TIMESTAMP(), and arithmetic on literals only. None of
# these have a source column, so the downstream sg_lineage(...) call would raise
# "Cannot find column '<literal>' in query" and emit a noise error (E1).
# An expression containing at least one real exp.Column descendant is NOT skipped
# and reaches the regular code path unchanged.
if not list(col_expr.find_all(exp.Column)):
    continue
```

**Why this is safe**:

- `sqlglot.exp.Column` descendants are the canonical way sqlglot represents an identifier reference. An expression with **zero** of them cannot resolve to a source column — the resolver call is guaranteed to fail with E1.
- The guard sits **after** the star-skip block, so star expansion semantics are untouched (star is `exp.Star`, not `exp.Column`, and is handled by the prior block via its own `continue`).
- The guard sits **before** the alias-naming branch, so it does not suppress aliased real columns (an aliased column still has a `Column` descendant).
- No new error is emitted on skip. The expression silently contributes no edge — which is the correct semantics for a literal in a SELECT list. Downstream `lineage_edges_created` counts are not affected for any real column.

**Step 5.2 — No new error code, no schema change**

The skip is a silent no-op. The plan rule is "no TODO in happy path" and this guard *is* the happy path for literals. Do not append a new `errors` entry on the skip — that would re-introduce the noise this ticket eliminates.

#### Wiring verification

- `grep -n "find_all(exp.Column)" src/sqlcg/parsers/base.py` — must appear exactly once and inside the SELECT-expression loop body of the lineage extractor (around line 614).
- `grep -n "col_lineage:NULL" src/sqlcg/parsers/base.py` — must remain present elsewhere only if the corresponding error path is still reachable for real columns; not introduced by the new guard.
- `grep -n "TODO" src/sqlcg/parsers/base.py` — zero new matches.

#### Files affected

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — the 2-line guard plus comment

#### Tests to add

Create `tests/unit/test_literal_column_skip.py`:

- **Scenario A — `SELECT NULL` emits zero E1 errors and zero lineage edges**:
  SQL: `INSERT INTO db.s.t (a) SELECT NULL`.
  Action: parse with `SnowflakeParser`.
  Assertion: no `errors` entry matching `col_lineage:NULL:Cannot find column`; statement's `column_lineage` is `[]` (the target column has no source — correct).

- **Scenario B — `SELECT CURRENT_TIMESTAMP()` is silently skipped**:
  SQL: `INSERT INTO db.s.t (a) SELECT CURRENT_TIMESTAMP()`.
  Assertion: no `errors` entry starting with `col_lineage:CURRENT_TIMESTAMP`; no lineage edge produced.

- **Scenario C — `SELECT 1 + 2` is silently skipped**:
  SQL: `INSERT INTO db.s.t (a) SELECT 1 + 2`.
  Assertion: no `errors` entry matching `col_lineage:.*Cannot find column`.

- **Scenario D — Real columns still resolve (negative regression test)**:
  SQL: `INSERT INTO db.s.t (a) SELECT src.col FROM db.s.src`.
  Assertion: statement has ≥ 1 `column_lineage` edge with `src.col -> db.s.t.a`. The guard MUST NOT skip this expression.

- **Scenario E — Mixed literal + real column in same SELECT list**:
  SQL: `INSERT INTO db.s.t (a, b) SELECT NULL, src.col FROM db.s.src`.
  Assertion: exactly one `column_lineage` edge (for `b`), zero errors mentioning `NULL`.

- **Scenario F — Aliased literal is still skipped**:
  SQL: `INSERT INTO db.s.t (a) SELECT NULL AS the_alias`.
  Assertion: no E1 error; no lineage edge. (The alias does not create a `Column` descendant.)

- **Scenario G — Function with column argument is NOT skipped**:
  SQL: `INSERT INTO db.s.t (a) SELECT COALESCE(src.col, 0) FROM db.s.src`.
  Assertion: ≥ 1 lineage edge produced with source `src.col` (the function call has a real `Column` descendant, so the guard lets it through to `sg_lineage`).

- **Scenario H — Corpus-level signal** *(manual verification only — not a CI test)*:
  Index the sprint-07 changelogs corpus subset (or a representative 10-file fixture containing at least one `INSERT … SELECT NULL`) and assert that the parsed-file errors contain zero `col_lineage:NULL` entries (was 1,148 in baseline). This is verified at the parser level — does not require a graph backend.
  **This scenario requires the live DWH corpus and MUST NOT be added as an automated test file.** It is verified only during the T-04 measurement run. Do not add a CI test for Scenario H.

#### Acceptance criteria

- [ ] `if not list(col_expr.find_all(exp.Column)): continue` appears exactly once in `base.py`, between the star-skip block and the alias branch (grep confirmed).
- [ ] Scenarios A, B, C, F pass — pure literals emit zero `col_lineage:.*Cannot find column` errors.
- [ ] Scenarios D, E, G pass — real-column expressions still produce lineage edges identical to pre-T-05 behaviour.
- [ ] Scenario H passes — corpus-level E1 (`col_lineage:NULL`) count drops from 1,148 to 0 on the changelogs corpus (verified during T-04 measurement run).
- [ ] No new error string introduced by the skip path.
- [ ] No new TODO in `base.py`.

---

## Test Strategy

### The Single Most Important Regression Guard

**File**: `tests/integration/test_bulk_upsert.py::test_bulk_path_equivalence_vs_baseline`
(Scenario A of T-01).

**Why**: T-01 rewrites the entire upsert pipeline. Equivalence with the sprint-07 single-row path on the same 12-file synthetic corpus is the property that proves the rewrite is **observably non-behavioural at the graph level**. The test must compare:

- The set of all `:SqlTable.qualified` values
- The set of all `:SqlColumn.id` values
- The total count of `:COLUMN_LINEAGE` edges
- The total count of `:HAS_COLUMN` edges
- The total count of `:SELECTS_FROM` edges
- The total count of `:STAR_SOURCE` edges

If any of these diverge, the bulk rewrite has dropped or duplicated graph elements — a silent correctness regression.

```python
def test_bulk_path_equivalence_vs_baseline(tmp_path):
    """Bulk upsert (T-01) must produce identical graph state to the
    single-row path on a 12-file synthetic corpus."""
    # ... see sprint_07 test_batch_size_equivalence_1_vs_50 for the
    # corpus-staging helper. Compare current head's graph against a
    # snapshot stored in the test as a constant dict captured pre-T-01.
```

### Secondary regression guards

| Ticket | Test file | Layer |
|--------|-----------|-------|
| T-01 | `tests/integration/test_bulk_upsert.py` | integration (in-memory KuzuBackend) |
| T-02 | `tests/unit/test_aggregator_skip.py` | unit (mocked parser) + integration equivalence test |
| T-03 | `tests/unit/test_timeout_cancel.py` | unit (fake parser with `time.sleep`) |
| T-04 | n/a — measurement task | n/a |
| T-05 | `tests/unit/test_literal_column_skip.py` | unit (parser-only, no graph backend) |

The sprint-07 `tests/integration/test_live_anchors.py::test_live_anchor_omloopsnelheid_has_column_lineage_edges` is **already** the cross-sprint anchor regression guard; it must keep passing through this sprint without modification.

---

## Wiring Checklist

| Question | T-01 | T-02 | T-03 | T-04 | T-05 |
|----------|------|------|------|------|------|
| What calls this? | `_upsert_parsed_file` calls `upsert_nodes_bulk`/`upsert_edges_bulk`; `_flush_batch` calls `_upsert_parsed_file`; `_flush_batch` is called inside `index_repo`. Existing tests reach all three. | `index_repo`'s pass-2 loop calls `aggregator.resolve_pass2`. The new predicate `_needs_pass2` is called only from `resolve_pass2`. | `index_repo`'s pass-1 loop calls `_index_single_file`. | The CLI `sqlcg index` (existing). | The SELECT-expression loop body inside the lineage extractor in `base.py` calls `sg_lineage`; the new guard short-circuits that call for literal-only expressions. The guard sits inline in the existing loop — no new function. |
| Where is the parameter passed? | Row groups are local to `_upsert_parsed_file`; the bulk methods accept `list[dict]`. No new public CLI flag. | No new parameter. `pass2_skipped` flows through the local accumulator into the summary dict. | No new parameter. Behavioural fix only. | No new parameter — same `--batch-size 50` default from sprint 07. | No parameter. The guard uses the existing `col_expr` loop variable. |
| What constant/path does this align with? | `_pk_field` from `GraphBackend` — the bulk variants MUST reuse it (no second source of truth for primary-key field names). Row-property keys MUST match the per-row writes at `indexer.py` lines 281–520 — listed exhaustively in T-01 Step 1.3. | Cross-file registry membership lookups (`self.sources`, `self.cross_file_sources`) — same keys used by `register_pass1` and `schema_resolver.register_cross_file_sources`. | `--timeout-per-file` default is 30 (per `cli/commands/index.py`); no change. | Corpus path matches sprint-07 baseline: `/home/ignwrad/Projects/dwh/ddl/changelogs`. Output filename follows sprint-07 convention: `sprint_08_changelogs_fullindex.json`. | `exp.Column` is the sqlglot AST node already used by the existing star-skip block on line 597 (`isinstance(col_expr, exp.Column)`) — same import, same canonical class. No new constant. |
| Does any TODO remain in the happy path? | No. | No. | No. | No. | No. |

---

## Acceptance Criteria (sprint-level)

- [ ] `sqlcg index` on the 796-file changelogs corpus completes with the bulk-upsert path enabled and produces ≥ 17,629 `COLUMN_LINEAGE` edges (the sprint-07 parse-only success-edge count) — i.e., bulk path drops no edges that the parser produces.
- [ ] Linear extrapolation of the 796-file run to 1,600 files comes in **under 300 seconds** (5-minute budget per CLAUDE.md). Recorded in `sprint_08_changelogs_fullindex.json`.
- [ ] Full-corpus run logs ≥ 1 lineage edge with `LOWER(d.col_name) = 'omloopsnelheid'` (real-corpus anchor verification).
- [ ] `--timeout-per-file 5` on a corpus containing the known-slow `ODS_WORKAROUND_ARTIKELDIMENSIE_CONCEPT.sql` (22.74 s in baseline) returns from `_index_single_file` in < 10 s wall-clock (T-03 behavioural validation, observable in indexer logs).
- [ ] `pass2_skipped > 0` in the summary for the 796-file corpus (most files have no cross-file refs — confirmation that T-02 skips them).
- [ ] On the small-repo 20-file ETL fixture (sprint-07 small-repo regression test), graph state is byte-identical to the pre-T-01 baseline (no small-repo regression — same as sprint-07 acceptance rule).
- [ ] `tests/integration/test_bulk_upsert.py` (T-01) and `tests/unit/test_aggregator_skip.py` (T-02) and `tests/unit/test_timeout_cancel.py` (T-03) all pass.
- [ ] `col_lineage:NULL` error count drops from 1,148 (sprint-07 baseline) to 0 on the same changelogs corpus (verified in T-04 measurement and in T-05 Scenario H).
- [ ] No new TODO in any file changed by this sprint.
- [ ] `ARCHITECTURE_REVIEW.md` has the new sprint-08 measurement subsection.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Bulk `UNWIND … MERGE` writes silently drop rows when a referenced node does not yet exist in the same transaction | MEDIUM — KuzuDB MERGE-with-MATCH semantics fail the MATCH and skip the row | T-01 Step 1.3 explicitly orders flushes: all node groups before all edge groups. Scenario A graph-equivalence test catches any dropped edges. |
| Heterogeneous row property sets within a group cause `upsert_nodes_bulk` to raise | LOW — the indexer writes uniform property keys per label by design | The homogeneity check in `upsert_nodes_bulk` raises `ValueError` with a helpful message. Tests Scenario C/D pin the contract. |
| Bulk Cypher payloads exceed KuzuDB's parameter-binding limits on huge files (e.g., 10,000-column DDL) | LOW — typical file has < 100 rows per group; the worst observed file (sprint-06 baseline) is < 500 columns | If a row group is unexpectedly large, the per-group `MERGE` still works; if it ever fails, fall back to chunking in 1,000-row sub-batches inside `upsert_nodes_bulk`. **Document** as a TODO-in-test (not in production code) and revisit only if a real failure surfaces. |
| `_needs_pass2` returns `False` for a file that actually benefits from pass 2 (false negative) | MEDIUM — predicate is heuristic on statement sources | Scenario D's graph-equivalence test catches any edge that would have been added by a re-parsed pass 2. If it fails, the predicate is too aggressive — relax to "any statement source not in `defined_tables`". |
| Identity check `resolved is parsed` breaks if a future contributor adds `parsed.copy()` to `resolve_pass2` skip path | LOW | Docstring in `resolve_pass2` and inline comment at the identity check both pin the contract. |
| Orphaned parser thread (T-03) leaks memory or holds external resources | LOW — sqlglot parsers are pure in-memory | The thread holds at most one file's SQL string plus the parser's intermediate AST. Bounded; GC reclaims when the thread exits. |
| Full-corpus measurement (T-04) reveals the budget is still NOT met | MEDIUM | Sprint stays open; architect-reviewer is consulted; next sprint targets parallel parsing (§ 11.6). The measurement itself is the win — it locks in a quantitative success criterion for sprint 09. |
| T-05 literal-skip guard accidentally suppresses real column resolution for an expression where sqlglot's `find_all(exp.Column)` returns empty despite a real reference | LOW — `exp.Column` is the canonical AST node for any identifier reference in sqlglot; literals never expand to it | Scenarios D, E, G of T-05 are the regression guards. If a real-corpus regression appears, the guard can be tightened to `if isinstance(col_expr, (exp.Null, exp.Literal, exp.Boolean)) or (isinstance(col_expr, exp.Func) and not list(col_expr.find_all(exp.Column))): continue` — narrower but equivalent for the observed cases. |

---

## Blocking Questions

None. All architecture decisions are documented:
- Bulk-upsert API shape — derived from the existing `upsert_node` / `upsert_edge` signatures with `UNWIND $rows`.
- Pass-2 skip predicate — sourced from `aggregator.sources` and `aggregator.cross_file_sources` which are already authoritative.
- Timeout fix — Python stdlib (`cancel_futures=True` since 3.9; we are 3.12).
- Measurement corpus and filename — match sprint-07 conventions.
- E1 literal-skip fix — root cause and exact patch site supplied by the user; no design choice remains.

---

## Postmortem

To be filled in at sprint close by the developer / architect-reviewer. Required sections:

### Measurement Targets vs. Actuals

| Target | Goal | Actual | Pass |
|--------|------|--------|------|
| Full-index 796 files | < 150 s (≈ extrapolates to < 300 s for 1,600) | __ | __ |
| Extrapolated 1,600 files | < 300 s (CLAUDE.md budget) | __ | __ |
| `col_lineage:NULL` errors on changelogs corpus | 0 (from 1,148 baseline) | __ | __ |
| `omloopsnelheid` real-corpus edge count | ≥ 1 | __ | __ |
| `ma_aantal_op_order` target column node exists | yes | __ | __ |
| `--timeout-per-file 5` returns from a 22 s slow-parser file in | < 10 s wall-clock | __ | __ |
| `pass2_skipped` on 796-file corpus | > 0 | __ | __ |
| Small-repo 20-file regression test | byte-identical graph state | __ | __ |

### What Worked

- _(developer fills in: which ticket landed cleanly, which test caught a regression, etc.)_

### What Did Not Work

- _(developer fills in: any ticket that hit unexpected friction, any test that needed re-design)_

### Findings to Carry Forward

- _(architect-reviewer fills in: anything to add to `ARCHITECTURE_REVIEW.md` § 11 or § 12 — e.g., new perf hypothesis if budget still not met, new E-code class discovered, etc.)_

### Decision Outcome

- _Per T-04 Step 4.5: close sprint OR re-open T-01 OR escalate to parallel-parsing sprint?_
