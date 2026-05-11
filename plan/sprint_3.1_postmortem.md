# Sprint Plan: v0.3.1 — Postmortem Fixes

Plan date: 2026-05-06
Author: architect-planner
Source authority: ARCHITECTURE_REVIEW.md section 11 (v0.3.0 Live Session Findings)
Policy: No backward compatibility. Breaking changes to graph schema and CLI output format
are acceptable without deprecation cycles. Re-index is the migration path.

---

## Summary

This sprint addresses the five findings from the v0.3.0 live session postmortem
(2026-05-06). All five findings are regressions or wiring gaps discovered during a
real production run on a ~1,200-file DWH corpus. The previous sprint (sprint_next.md)
is not repeated here; only net-new work is included.

The single most impactful fix is column lineage wiring (11.2-a): a one-line change that
unblocks every column lineage tool in the product. The single most disruptive failure is
OOM (11.1): a structural change to index_repo that makes memory usage flat and
predictable. These two must ship together in the same release because 11.1's per-file
commit pattern changes the write boundary that 11.2's column lineage edges are written
within.

---

## Scope

### In Scope

- Finding 11.1: Per-file commit boundary in `index_repo`; `--buffer-pool-size` CLI flag
- Finding 11.2a: Wire `_extract_column_lineage` call in `_parse_statement` (XS)
- Finding 11.2b: Implement the sqlglot `LineageNode` → `LineageEdge` tree-walking conversion (M)
- Finding 11.3: Priority inversion fix in `install.py` — prefer local `sqlcg` over `uvx`
- Finding 11.4a: Wire `progress_callback` from `cli/commands/index.py` to `indexer.index_repo`
- Finding 11.4b: Catch KuzuDB lock error in `get_backend()` and re-raise with PID hint
- Finding 11.5: One-line fix in `uninstall.py` — use `KuzuConfig.from_env().db_path` instead of hardcoded `~/.sqlcg/kuzu.db`

### Non-Goals

- Full sg_lineage tree traversal for complex CTEs or multi-hop lineage chains (separate future ticket)
- BigQuery procedure body parsing (documented limit)
- Cross-session temp table chains (documented limit)
- Any ticket from sprint_next.md that was correctly shipped: T-01 (QUICK START), T-07 (hint field), T-13 (FN label), T-14 (docs), T-04 (transaction wrap), T-05 (aggregator test)
- T-08 progress output — partially superseded by this sprint (11.4a covers the wiring; rich progress bar rendering can be extended but the callback wiring is the blocker)
- T-09 parse_quality breakdown — already shipped per code inspection; not repeated
- T-10 SELECTS_FROM / INSERT-SELECT investigation — not blocked by postmortem findings; carry forward as-is
- T-11 scripting-block DML rewrite — not blocked by postmortem findings; carry forward as-is

---

## Code-vs-Plan Verification (2026-05-06)

Cross-checked against actual source tree before writing any ticket.

| Finding | Verified state |
|---------|---------------|
| 11.1 — no per-file commit | Confirmed: `index_repo` in `indexer.py` accumulates all results in `pass2_results` then iterates without any `db.transaction()` or commit boundary between files. `reindex_file` (watcher path) uses `with db.transaction()` per file. Gap is real. |
| 11.2a — `_extract_column_lineage` never called | Confirmed: `ansi_parser.py:140` hardcodes `column_lineage = []`. The `_extract_column_lineage` method exists in `base.py` but is unreachable from `_parse_statement`. |
| 11.2b — TODO in happy path | Confirmed: `base.py:397` — `# TODO: convert root to LineageEdge(s)`. No conversion code follows. |
| 11.3 — priority inversion | Confirmed: `install.py:21` checks `shutil.which("uvx")` first. If both `uvx` and `sqlcg` are on PATH, `uvx` wins and the stale entry is never updated. |
| 11.4a — progress_callback not wired | Confirmed: `cli/commands/index.py:69` calls `indexer.index_repo(path, dialect, backend, dbt_manifest, timeout_per_file)` — `progress_callback` parameter is omitted (defaults to None). The parameter exists in `indexer.py:33`. The 100-file invocation logic at `indexer.py:79-80` is implemented but dead. |
| 11.4b — lock error not caught | Confirmed: `kuzu_backend.py:36` calls `kuzu.database.Database(db_path)` bare. `get_backend()` in `config.py:82-104` does not catch any KuzuDB exceptions. |
| 11.5 — hardcoded kuzu.db path | Confirmed: `uninstall.py:206` hardcodes `Path.home() / ".sqlcg" / "kuzu.db"`. `config.py:17` defines the real default as `Path.home() / ".sqlcg" / "graph.db"`. The two strings differ: `kuzu.db` vs `graph.db`. |

---

## Ticket Table

| Ticket | Title | Files Touched | Effort | Priority | Dependency | Blocks |
|--------|-------|---------------|--------|----------|------------|--------|
| P-01 | Per-file commit in `index_repo` + `--buffer-pool-size` flag | `indexer/indexer.py`, `core/kuzu_backend.py`, `core/config.py`, `cli/commands/index.py`, `cli/commands/db.py` | M | CRITICAL | INDEPENDENT | P-02 |
| P-02 | Wire `_extract_column_lineage` call in `_parse_statement` | `parsers/ansi_parser.py` | XS | CRITICAL | DEPENDS ON P-01 (must ship same release) | NONE |
| P-03 | Implement `LineageNode` → `LineageEdge` tree-walking conversion | `parsers/base.py` | M | CRITICAL | DEPENDS ON P-02 | NONE |
| P-04 | Fix install priority: prefer local `sqlcg` over `uvx` | `cli/commands/install.py` | XS | HIGH | INDEPENDENT | NONE |
| P-05 | Wire progress callback + lock error message (combined) | `cli/commands/index.py`, `core/config.py`, `core/kuzu_backend.py` | S | HIGH | INDEPENDENT | NONE |
| P-06 | Fix uninstall DB path fallback | `cli/commands/uninstall.py` | XS | MEDIUM | INDEPENDENT | NONE |

**P-01 and P-02 are combined in the same PR** because the per-file commit boundary is the
write context within which column lineage edges are now emitted. Shipping P-02 without P-01
produces edges that are never committed to disk. Shipping P-01 without P-02 is safe but
produces no additional column lineage. The combined PR ensures the first successful user
session after v0.3.1 sees both OOM prevention and non-zero column lineage edges.

---

## Recommended Implementation Order

### Why this order

**Start with P-04 and P-06** — they are each one or two lines, fully independent, and have
no test complexity. Merging them first reduces the diff surface in subsequent PRs and closes
the two highest-visibility user-facing bugs (install silently using stale entry, uninstall
leaving 50 MB on disk). These can be done in a single PR.

**Then P-05** — wiring the progress callback is XS in isolation (one line in `index.py`) but
combined here with the lock error message (a few lines in `config.py` / `kuzu_backend.py`).
Both touch the index/init startup path. Grouping them avoids two separate PRs touching the
same CLI startup code.

**Then P-01 + P-02 (combined PR)** — this is the largest structural change and the highest-
risk. P-01 restructures the write loop in `index_repo`; P-02 removes the hardcoded
`column_lineage = []`. Together they make column lineage functional for the first time.
Run against the full test suite after this PR — it is the most likely to surface regressions
in integration tests.

**Finally P-03** — the lineage tree-walking conversion. P-02 must be merged first because
P-03 builds on the call being made. P-03 does not need to ship in the same release as P-01
+ P-02; the system is better with wired-but-empty-conversion than unwired, because at least
the infrastructure is now tested.

**Single developer sequence**: P-04 + P-06 (same PR) → P-05 → P-01 + P-02 (same PR) → P-03

**Two developer sequence**:
- Developer A: P-04 + P-06 → P-01 + P-02
- Developer B: P-05 → P-03 (after P-02 merges)

---

## Ticket Specifications

---

### P-04 + P-06 — Install priority fix + Uninstall DB path fix (single PR)

These two fixes are grouped because each is one line of production code, both are
independently testable, and a combined PR keeps the review overhead low.

---

#### P-04 — Fix `sqlcg install` priority: prefer local `sqlcg` over `uvx`

**Source**: ARCHITECTURE_REVIEW.md 11.3 (HIGH)

**Root cause**: `install.py:21` checks `shutil.which("uvx")` first. After `uv tool install
sql-code-graph`, both `uvx` and `sqlcg` are on PATH. The computed ideal entry is still
`{"command": "uvx", …}`, which matches the existing stale entry exactly, so "Already
configured" is printed and the entry is never updated.

**What to do**:

1. In `install.py`, invert the detection order. Current:
   ```python
   if shutil.which("uvx"):
       entry = {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
   else:
       entry = {"command": "sqlcg", "args": ["mcp", "start"]}
   ```
   Replace with:
   ```python
   if shutil.which("sqlcg"):
       entry = {"command": "sqlcg", "args": ["mcp", "start"]}
   elif shutil.which("uvx"):
       entry = {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}
   else:
       console.print("[red]Error:[/red] Neither 'sqlcg' nor 'uvx' found on PATH.")
       raise typer.Exit(1)
   ```

2. When the existing entry uses `uvx` but the computed ideal uses `sqlcg` (i.e., the user
   has since run `uv tool install`), print an upgrade notice before writing:
   ```
   Updating MCP entry from `uvx` to local `sqlcg` binary (faster startup). Writing…
   ```
   This notice is printed only when the command changes from uvx to sqlcg — not on every
   install call.

3. The existing `--dry-run` flag must still work correctly with the new priority order.

**Wiring verification**:
- `install_cmd()` is registered in `cli/main.py`. Confirm the existing registration is
  unchanged — no new wiring needed.
- The `shutil.which("sqlcg")` check is the only call site to verify. After the change,
  `grep -n "shutil.which" install.py` must show `sqlcg` before `uvx`.

**Files affected**:
- `src/sqlcg/cli/commands/install.py` — detection order + upgrade notice

**Tests to add**:

Unit tests (`tests/unit/test_install.py` — extend or create):

- Scenario A — only `sqlcg` on PATH: patch `shutil.which` to return `/usr/local/bin/sqlcg`
  for `"sqlcg"` and `None` for `"uvx"`; invoke `install_cmd` via `CliRunner` with a
  `tmp_path / "settings.json"` target; assert the written JSON has
  `config["mcpServers"]["sql-code-graph"]["command"] == "sqlcg"`; assert output does NOT
  contain "cold cache".

- Scenario B — only `uvx` on PATH: patch `shutil.which` to return `None` for `"sqlcg"`
  and `/usr/bin/uvx` for `"uvx"`; assert written JSON command equals `"uvx"`; assert
  output contains "cold cache" (the existing uvx note).

- Scenario C — both on PATH, existing entry is `uvx`: write `settings.json` with
  `{"mcpServers": {"sql-code-graph": {"command": "uvx", "args": ["sql-code-graph", "mcp", "start"]}}}`;
  patch `shutil.which("sqlcg")` to return a path; invoke `install_cmd`; assert the written
  JSON now has `command == "sqlcg"`; assert output contains "Updating" or "faster startup".

- Scenario D — neither on PATH: patch both `shutil.which` calls to return `None`; assert
  `CliRunner` result exit code is non-zero; assert output contains "not found on PATH".

- Scenario E — `--dry-run` with `sqlcg` on PATH: assert no file is written; assert output
  contains `"sqlcg"` in the JSON preview.

**Acceptance criteria**:
- `[ ]` When `sqlcg` is on PATH, the MCP entry always uses `command: sqlcg`
- `[ ]` When only `uvx` is on PATH, the MCP entry uses `command: uvx`
- `[ ]` When an existing uvx entry is superseded by a local sqlcg install, the entry is
  updated and an upgrade notice is printed
- `[ ]` When neither is on PATH, the command exits non-zero with a clear error
- `[ ]` All five unit scenarios pass

---

#### P-06 — Fix `sqlcg uninstall` DB path fallback

**Source**: ARCHITECTURE_REVIEW.md 11.5 (MEDIUM)

**Root cause**: `uninstall.py:206` hardcodes `Path.home() / ".sqlcg" / "kuzu.db"` as the
fallback when `SQLCG_DB_PATH` is unset. The real default in `config.py:17` is
`Path.home() / ".sqlcg" / "graph.db"`. The two strings differ — `kuzu.db` vs `graph.db` —
so `_get_db_path()` returns `None` when no env var is set and `~/.sqlcg/graph.db` exists
(the path it checks for does not match the path that exists). The uninstall command then
prints "No database configured" and exits without deleting the 50 MB database.

**What to do**:

Replace the `_get_db_path()` implementation in `uninstall.py`:

```python
# Current (wrong):
def _get_db_path() -> str | None:
    db_path = os.getenv("SQLCG_DB_PATH")
    if db_path:
        return db_path
    default_path = str(Path.home() / ".sqlcg" / "kuzu.db")
    return default_path if Path(default_path).exists() else None

# Correct:
def _get_db_path() -> str | None:
    from sqlcg.core.config import KuzuConfig
    db_path = str(KuzuConfig.from_env().db_path)
    return db_path if Path(db_path).exists() else None
```

This is a one-line semantic change: the hardcoded fallback is replaced by the authoritative
source of truth (`KuzuConfig.from_env().db_path`). The `os.getenv("SQLCG_DB_PATH")` check
is now handled inside `KuzuConfig.from_env()` — the duplicate `os.getenv` call is removed.

**Wiring verification**:
- `_get_db_path()` is called from `_step2_delete_database()` and from the `--keep-db` branch
  of `uninstall_cmd()`. Both call sites are within the same file — no external wiring needed.
- After the change, `grep -n "kuzu.db" uninstall.py` must return zero results.
- Confirm `KuzuConfig.from_env().db_path` returns `Path.home() / ".sqlcg" / "graph.db"` when
  `SQLCG_DB_PATH` is unset by cross-checking `config.py:17`.

**Files affected**:
- `src/sqlcg/cli/commands/uninstall.py` — `_get_db_path()` only

**Tests to add**:

Unit tests (`tests/unit/test_uninstall.py` — extend existing):

- Scenario — no env var, `graph.db` exists: create `tmp_path / ".sqlcg" / "graph.db"` as a
  directory (KuzuDB uses a directory, not a file); patch `Path.home()` to return `tmp_path`;
  invoke `uninstall_cmd --force` via `CliRunner`; assert the directory no longer exists;
  assert output contains "Deleted graph database".

- Scenario — no env var, neither path exists: patch `Path.home()` to return a `tmp_path`
  with no `.sqlcg` subdirectory; invoke `uninstall_cmd --keep-db`; assert output contains
  "No database" or "not found" (not "No database configured" when the real path doesn't
  exist — acceptable either way, but the path checked must be `graph.db` not `kuzu.db`).

- Scenario — `SQLCG_DB_PATH` set: set `os.environ["SQLCG_DB_PATH"]` to a `tmp_path`
  subdirectory that exists; invoke `uninstall_cmd --force`; assert that subdirectory is
  deleted; assert output contains "Deleted".

**Acceptance criteria**:
- `[ ]` `sqlcg uninstall` prompts to delete `~/.sqlcg/graph.db` when no env var is set
- `[ ]` The string `kuzu.db` no longer appears in `uninstall.py`
- `[ ]` `_get_db_path()` derives its fallback from `KuzuConfig.from_env().db_path`
- `[ ]` All three unit scenarios pass

---

### P-05 — Wire progress callback + KuzuDB lock error message

**Source**: ARCHITECTURE_REVIEW.md 11.4 (HIGH — two independent gaps combined)

These two gaps are combined because both touch the index startup path (lock error in
`get_backend()`) and the index loop (progress callback in `index.py`). A developer picking
up this ticket can address both in a single PR with no merge conflicts.

#### P-05a — Wire `progress_callback` from `cli/commands/index.py`

**Root cause**: `indexer.py:33` defines `progress_callback: Callable[[int, int], None] | None = None`.
`indexer.py:79-80` invokes it every 100 files. `cli/commands/index.py:69` calls
`indexer.index_repo(path, dialect, backend, dbt_manifest, timeout_per_file)` without
passing a `progress_callback` argument. The parameter exists. The call site never passes
it. The progress bar is dead code.

**What to do**:

1. In `cli/commands/index.py`, define a progress callback before calling `index_repo`:

   ```python
   def _make_progress_callback(total: int) -> Callable[[int, int], None]:
       def callback(n: int, total: int) -> None:
           console.print(f"\r  Indexed {n}/{total} files...", end="", highlight=False)
       return callback
   ```

   Inject it into the `index_repo` call:
   ```python
   summary = indexer.index_repo(
       path,
       dialect,
       backend,
       dbt_manifest,
       timeout_per_file,
       progress_callback=_make_progress_callback(total_files),
   )
   console.print()  # newline after the carriage-return progress line
   ```

2. The callback must handle the case where `total` is 0 (no SQL files found) without
   division by zero. The existing `index_repo` code already guards this by only calling the
   callback when `i % 100 == 0` — so with fewer than 100 files the callback is never
   invoked. This is acceptable. Document it explicitly in the callback's docstring.

3. After `index_repo` returns, if `summary['lineage_edges_created'] == 0`, print the
   existing yellow warning (this was already in `index.py:93-97` — verify it is present
   and not regressed by the callback change).

**Wiring verification**:
- After the change, `grep -n "progress_callback" cli/commands/index.py` must show at least
  one assignment and one usage.
- `grep -n "progress_callback" indexer/indexer.py` must show the existing parameter and
  invocation at line 79-80 — these must not be changed.
- The callback flows: `index_cmd` defines it → passes to `index_repo` → `index_repo`
  invokes it every 100 files. Verify each link.

**Files affected**:
- `src/sqlcg/cli/commands/index.py` — define and pass `progress_callback`

#### P-05b — KuzuDB lock error: catch and re-raise with PID hint

**Root cause**: `get_backend()` in `config.py:82-104` constructs `KuzuBackend(str(kuzu_cfg.db_path))`
without catching the `RuntimeError: IO exception: Could not set lock on file: ...` that
KuzuDB raises when the database is already open. The user sees a raw Python traceback with
no guidance.

**What to do**:

In `kuzu_backend.py`, wrap the `kuzu.database.Database(db_path)` call in `__init__`:

```python
def __init__(self, db_path: str) -> None:
    self._db_path = db_path
    try:
        self._db = kuzu.database.Database(db_path)
    except RuntimeError as exc:
        if "Could not set lock" in str(exc) or "lock" in str(exc).lower():
            # Attempt to find the holding PID via lsof
            pid_hint = _find_lock_holder(db_path)
            msg = (
                f"Database is locked — another sqlcg process is running "
                f"({pid_hint}). "
                f"Wait for it to finish or kill it with: kill {pid_hint.split()[-1] if pid_hint else '<PID>'}"
            )
            raise RuntimeError(msg) from exc
        raise
    self._conn = kuzu.Connection(self._db)
    self._in_transaction = False
```

Add a module-level helper (not a method — it has no `self`):

```python
def _find_lock_holder(db_path: str) -> str:
    """Return a human-readable PID string for the process holding the DB lock.

    Uses lsof on Linux/macOS. Returns a descriptive fallback if lsof is
    unavailable or returns no results.
    """
    import shutil
    import subprocess

    if not shutil.which("lsof"):
        return "PID unknown (lsof not available)"
    try:
        result = subprocess.run(
            ["lsof", "-t", db_path],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pids = result.stdout.strip().split()
        if pids:
            return f"PID {', '.join(pids)}"
    except Exception:
        pass
    return "PID unknown"
```

**Important**: the lock detection is in `KuzuBackend.__init__`, not in `get_backend()`.
This means the helpful error fires regardless of how `KuzuBackend` is constructed —
through `get_backend()`, tests, or any direct instantiation.

**Wiring verification**:
- `grep -n "Could not set lock\|lock" kuzu_backend.py` must show the catch clause.
- `grep -n "get_backend\|KuzuBackend" config.py` — `get_backend()` does not need to change;
  the error is caught at the `KuzuBackend.__init__` level.
- `_find_lock_holder` is a module-level function, not a method — it is called from
  `__init__` with `db_path` as argument. Verify the call site uses `_find_lock_holder(db_path)`,
  not `self._find_lock_holder(db_path)`.

**Files affected**:
- `src/sqlcg/core/kuzu_backend.py` — `__init__` + `_find_lock_holder` module-level helper

**Tests to add**:

Unit tests for P-05a (`tests/unit/test_index_progress.py`):

- Scenario A — callback invoked at 100-file boundary: create 105 minimal `.sql` files in
  `tmp_path` (each `SELECT 1;`); index them into an in-memory KuzuBackend; record all
  `(n, total)` tuples passed to the callback; assert `any(n == 100 for n, t in calls)`;
  assert `total == 105` in all calls.

- Scenario B — fewer than 100 files: index 5 files; assert the callback is never called
  (0 invocations). This is the documented behaviour — callback fires only at 100-file
  multiples.

- Scenario C — CLI wiring: invoke `index_cmd` via `CliRunner` on a `tmp_path` containing
  100+ `.sql` files; assert captured output contains `"Indexed "` as a substring
  (the progress line). This is the end-to-end wiring test — if the callback is not passed,
  the line never appears.

Unit tests for P-05b (`tests/unit/test_kuzu_lock.py`):

- Scenario A — lock error re-raised with message: patch `kuzu.database.Database.__init__`
  to raise `RuntimeError("IO exception: Could not set lock on file: /tmp/test.db")`; also
  patch `_find_lock_holder` to return `"PID 12345"`; construct `KuzuBackend("/tmp/test.db")`
  inside a `pytest.raises(RuntimeError)` block; assert the raised message contains
  "Database is locked" and "12345".

- Scenario B — non-lock RuntimeError propagates unchanged: patch `kuzu.database.Database.__init__`
  to raise `RuntimeError("Some other error")`; assert `KuzuBackend(...)` raises
  `RuntimeError` with the original message unchanged.

- Scenario C — `_find_lock_holder` when lsof not available: patch `shutil.which("lsof")`
  to return `None`; call `_find_lock_holder("/tmp/test.db")`; assert return value contains
  "unknown".

**Acceptance criteria**:
- `[ ]` `sqlcg index` on 100+ files prints a `\r`-overwritten progress line per 100-file boundary
- `[ ]` `progress_callback` is passed from `index_cmd` to `indexer.index_repo` — verified by grep
- `[ ]` Attempting to open a locked KuzuDB raises `RuntimeError` with "Database is locked" and a PID hint
- `[ ]` Non-lock errors from KuzuDB propagate unchanged
- `[ ]` All unit scenarios pass

---

### P-01 + P-02 — Per-file commit boundary in `index_repo` + Wire column lineage call (single PR)

**Source**: ARCHITECTURE_REVIEW.md 11.1 (CRITICAL) + 11.2a (CRITICAL)

These are shipped as a combined PR for the reason stated in the Ticket Table section:
per-file commits are the write context within which column lineage edges must be flushed.

---

#### P-01 — Per-file commit boundary in `index_repo` + `--buffer-pool-size` flag

**Root cause**: `index_repo` in `indexer.py` accumulates all `pass1_results` and
`pass2_results` in memory, then calls `_upsert_parsed_file` in a tight loop with no commit
boundary. KuzuDB's buffer pool fills and cannot be flushed because no COMMIT has been
issued. The `reindex_file` method (used by the watcher) correctly wraps each file in
`with db.transaction()`. The two code paths implement the same operation with opposite
memory models.

**What to do**:

**Part A — per-file commit in `index_repo`**:

Restructure the upsert loop at the end of `index_repo` to wrap each file in a transaction:

```python
# Current (accumulates all writes before any commit):
for parsed in pass2_results:
    counts = self._upsert_parsed_file(parsed, db)
    ...

# Correct (per-file commit, flat memory usage):
for parsed in pass2_results:
    with db.transaction():
        counts = self._upsert_parsed_file(parsed, db)
    tables_found += counts["tables"]
    lineage_edges += counts["edges"]
    quality_key = parsed.parse_quality.value.lower()
    quality_counts[quality_key] += 1
```

The `with db.transaction()` context manager already issues `BEGIN TRANSACTION` / `COMMIT` /
`ROLLBACK` via `KuzuBackend.transaction()`. No new infrastructure is needed.

If a single file's upsert fails (exception inside the `with` block), the `ROLLBACK` fires
automatically and the loop continues to the next file. Log the failure at WARNING level.
Do not re-raise — a single bad file must not abort the entire corpus.

Add error handling around the transaction:

```python
for parsed in pass2_results:
    try:
        with db.transaction():
            counts = self._upsert_parsed_file(parsed, db)
        tables_found += counts["tables"]
        lineage_edges += counts["edges"]
        quality_key = parsed.parse_quality.value.lower()
        quality_counts[quality_key] += 1
    except Exception as exc:
        logger.warning("Failed to upsert %s: %s — skipping", parsed.path, exc)
        quality_counts["failed"] += 1
```

**Part B — `--buffer-pool-size` CLI flag**:

Add `buffer_pool_size_mb` to `KuzuConfig`:

```python
class KuzuConfig(BaseModel):
    db_path: Path = Field(default_factory=lambda: Path.home() / ".sqlcg" / "graph.db")
    buffer_pool_size_mb: int = Field(default=0, description="KuzuDB buffer pool size in MB (0 = use KuzuDB default)")
```

Update `KuzuConfig.from_env()` to read `SQLCG_BUFFER_POOL_MB`:

```python
@classmethod
def from_env(cls) -> "KuzuConfig":
    env_path = os.getenv("SQLCG_DB_PATH")
    env_buf = os.getenv("SQLCG_BUFFER_POOL_MB")
    return cls(
        db_path=Path(env_path) if env_path else Path.home() / ".sqlcg" / "graph.db",
        buffer_pool_size_mb=int(env_buf) if env_buf else 0,
    )
```

Update `KuzuBackend.__init__` to pass the buffer pool size when non-zero:

```python
def __init__(self, db_path: str, buffer_pool_size_mb: int = 0) -> None:
    self._db_path = db_path
    kwargs = {}
    if buffer_pool_size_mb > 0:
        kwargs["buffer_pool_size"] = buffer_pool_size_mb * 1024 * 1024
    try:
        self._db = kuzu.database.Database(db_path, **kwargs)
    except RuntimeError as exc:
        ...  # lock error handling from P-05b
```

Add `--buffer-pool-size` to `index_cmd` in `cli/commands/index.py`:

```python
buffer_pool_size: int = typer.Option(
    0,
    "--buffer-pool-size",
    help="KuzuDB buffer pool size in MB (0 = default, ~80% RAM). "
         "Set to 256–512 on memory-constrained machines.",
)
```

Persist it via env var before calling `get_backend()`:

```python
if buffer_pool_size > 0:
    os.environ["SQLCG_BUFFER_POOL_MB"] = str(buffer_pool_size)
```

Also expose `--buffer-pool-size` on `db init` (`cli/commands/db.py`) via the same env
var mechanism.

**Wiring verification**:
- `grep -n "transaction" indexer/indexer.py` must show `with db.transaction()` inside
  the upsert loop, not just in `reindex_file`.
- `grep -n "buffer_pool_size\|SQLCG_BUFFER_POOL_MB" config.py` must show the field and
  env var read.
- `grep -n "buffer_pool_size\|SQLCG_BUFFER_POOL_MB" cli/commands/index.py` must show the
  CLI option and the env var assignment.
- `grep -n "buffer_pool_size" kuzu_backend.py` must show it is passed to `kuzu.database.Database`.
- **No TODO may remain** in the `index_repo` upsert loop or in `KuzuBackend.__init__`.

**Files affected**:
- `src/sqlcg/indexer/indexer.py` — restructure upsert loop with `with db.transaction()`
- `src/sqlcg/core/kuzu_backend.py` — `__init__` accepts and passes `buffer_pool_size_mb`
- `src/sqlcg/core/config.py` — `KuzuConfig.buffer_pool_size_mb` field + `from_env()` update
- `src/sqlcg/cli/commands/index.py` — `--buffer-pool-size` option
- `src/sqlcg/cli/commands/db.py` — `--buffer-pool-size` option on `db init`

---

#### P-02 — Wire `_extract_column_lineage` call in `_parse_statement`

**Source**: ARCHITECTURE_REVIEW.md 11.2a (CRITICAL)

**Root cause**: `ansi_parser.py:140` hardcodes `column_lineage = []` and never calls
`self._extract_column_lineage(stmt, path, out, schema)`. The method exists in `base.py`
and is fully implemented (up to the TODO in the happy path — addressed by P-03). Removing
the hardcode and making the call is a one-line change.

**What to do**:

In `ansi_parser.py`, `_parse_statement`, replace:

```python
# Extract column lineage (currently minimal implementation)
column_lineage = []
```

with:

```python
# Extract column lineage
schema = self._schema_resolver.as_dict() if self._schema_resolver else {}
column_lineage = self._extract_column_lineage(stmt, path, out, schema)
```

**Verify `_schema_resolver` attribute name**: `SqlParser` base class must expose
`self._schema_resolver` (or equivalent). Check `parsers/base.py` `__init__` before
coding — use whatever attribute name is present. Do not add a new attribute.

**Also add**: in `parse_file` in `ansi_parser.py`, after the per-statement loop, upgrade
`parse_quality` to `FULL` if any statement produced column lineage edges:

```python
if any(stmt.column_lineage for stmt in out.statements):
    out.parse_quality = ParseQuality.FULL
```

This already exists at `ansi_parser.py:79-80` — verify it is present and that the
condition checks `stmt.column_lineage`, not just `out.statements`.

**Wiring verification (CRITICAL)**:
- `grep -n "_extract_column_lineage" ansi_parser.py` must show at least one call, not
  just an import. If zero calls appear, the bug has not been fixed.
- `grep -n "column_lineage = \[\]" ansi_parser.py` must return zero results after the fix.
- Confirm `_extract_column_lineage` is defined in `parsers/base.py` (it is — confirmed
  at `base.py:326`). The call in `ansi_parser.py` must match the signature:
  `self._extract_column_lineage(stmt, path, out, schema)`.
- **No TODO may remain in the happy path** — the method is called; it returns `[]` on the
  happy path because of the TODO in `base.py:397`. That TODO is P-03's responsibility.
  P-02's job is only to make the call.

**Files affected**:
- `src/sqlcg/parsers/ansi_parser.py` — remove `column_lineage = []`, add the call

**Tests to add** (combined with P-01 tests in the same PR):

Tests for P-01 (`tests/integration/test_indexer_commits.py`):

- Scenario A — per-file commit prevents OOM simulation: index a directory containing
  200 `.sql` files (each `SELECT 1;`) into an in-memory KuzuBackend; mock
  `KuzuBackend.transaction` to count how many times it is entered; assert the count equals
  200 (one transaction per file, not one transaction for all 200).

  Implementation note: use `unittest.mock.patch.object` on `KuzuBackend.transaction` to
  wrap the real implementation and count calls. The real transaction must still execute —
  use `wraps=KuzuBackend.transaction`.

- Scenario B — single file failure does not abort the corpus: index a directory with 5
  valid `.sql` files and 1 file that causes `_upsert_parsed_file` to raise; mock
  `Indexer._upsert_parsed_file` to raise `RuntimeError` on the 3rd call (by counting
  invocations); assert `summary["files_parsed"] == 5` and `summary["quality"]["failed"] >= 1`
  (the failed file is counted in quality, not silently lost).

- Scenario C — `--buffer-pool-size` is passed to KuzuBackend: set env var
  `SQLCG_BUFFER_POOL_MB=256`; construct `KuzuConfig.from_env()`; assert
  `config.buffer_pool_size_mb == 256`; construct a `KuzuBackend` via `get_backend()` and
  verify the `kuzu.database.Database` constructor was called with `buffer_pool_size=256*1024*1024`
  (use `unittest.mock.patch`).

Tests for P-02 (`tests/unit/test_column_lineage_wiring.py`):

- Scenario — `_extract_column_lineage` is called for SELECT statements: parse
  `CREATE VIEW v AS SELECT amount FROM orders` with `AnsiParser`; assert
  `out.statements[0].column_lineage` is not equal to `[]` OR assert
  `_extract_column_lineage` was called (use `patch.object` with `wraps` to count calls);
  **at minimum**: assert `out.statements[0].column_lineage` is a list (type check) — this
  fails if the line is still `column_lineage = []` because a hardcoded list would never
  change type. A stronger assertion is that `_extract_column_lineage` was invoked — use
  `patch.object(AnsiParser, "_extract_column_lineage", wraps=original)` and assert
  `mock.called`.

  This test is the regression guard: if the hardcode is re-introduced, this test catches it.

- Scenario — `_extract_column_lineage` is NOT called for DDL-only CREATE TABLE: parse
  `CREATE TABLE orders (id INT, amount DECIMAL)` with `AnsiParser`; assert
  `out.statements[0].column_lineage == []` (DDL has no column lineage by definition).

**Acceptance criteria**:
- `[ ]` `index_repo` wraps each file's upsert in `with db.transaction()` — transaction
  count equals file count
- `[ ]` A single file failure during upsert logs a WARNING and continues — not an abort
- `[ ]` `--buffer-pool-size N` on `sqlcg index` passes `buffer_pool_size=N*1024*1024` to
  KuzuDB
- `[ ]` `SQLCG_BUFFER_POOL_MB` env var is read by `KuzuConfig.from_env()`
- `[ ]` `column_lineage = []` hardcode is removed from `_parse_statement`
- `[ ]` `_extract_column_lineage` is called for SELECT/INSERT/CREATE statements
- `[ ]` DDL-only CREATE TABLE still produces `column_lineage == []`
- `[ ]` All tests pass

---

### P-03 — Implement `LineageNode` → `LineageEdge` tree-walking conversion

**Source**: ARCHITECTURE_REVIEW.md 11.2b (CRITICAL)

**Depends on**: P-02 merged. Without P-02, `_extract_column_lineage` is never called and
this code is unreachable.

**Root cause**: `base.py:393-403` — when `sg_lineage()` returns a root `LineageNode`,
the code logs a debug message and does nothing. The `# TODO: convert root to LineageEdge(s)`
comment documents the gap. The sqlglot `lineage.lineage()` function returns a `LineageNode`
tree where each node represents a column source. Walking the tree produces `src → dst`
column pairs.

**What to do**:

**Step 1 — Understand the sqlglot `LineageNode` API**:

Before implementing, the developer must inspect the installed `sqlglot.lineage.Node` class
(the type returned by `lineage()`). The key attributes are:

- `node.name` — the column name at this node
- `node.source` — the upstream source expression (usually a `Table` node)
- `node.downstream` — list of child `Node` objects (columns this node feeds)
- `node.upstream` — list of parent `Node` objects (columns that feed this node)

Run in a Python shell against the installed version:

```python
from sqlglot import lineage
import inspect
print(inspect.getsource(lineage.Node))
```

Document the exact attribute names in a code comment before implementing the walker.

**Step 2 — Implement `_lineage_node_to_edges`**:

Add a new private method to `SqlParser` in `base.py`:

```python
def _lineage_node_to_edges(
    self,
    root: Any,  # sqlglot.lineage.Node
    dst_col_name: str,
    dst_table: "TableRef",
    path: "Path",
    out: "ParsedFile",
) -> "list[LineageEdge]":
    """Walk the sqlglot LineageNode tree and emit LineageEdge objects.

    Each leaf in the tree represents a source column. The walk stops at
    nodes whose source is a Table (a real table reference, not a CTE alias).

    Args:
        root: The LineageNode returned by sg_lineage()
        dst_col_name: The output column name (destination)
        dst_table: The TableRef for the destination table
        path: Source file path (for error recording)
        out: ParsedFile for error recording

    Returns:
        List of LineageEdge objects (may be empty if tree is malformed)
    """
    edges: list[LineageEdge] = []
    visited: set[int] = set()  # guard against cycles

    def _walk(node: Any) -> None:
        if id(node) in visited:
            return
        visited.add(id(node))

        # If this node has upstream sources, recurse
        if node.upstream:
            for parent in node.upstream:
                _walk(parent)
        else:
            # Leaf node — extract the source table and column
            try:
                src_table_ref = self._lineage_node_to_table_ref(node)
                if src_table_ref is None:
                    return
                src_col_name = node.name.split(".")[-1] if node.name else dst_col_name
                edges.append(
                    LineageEdge(
                        src=ColumnRef(src_table_ref, src_col_name),
                        dst=ColumnRef(dst_table, dst_col_name),
                        transform="SELECT",
                        confidence=0.9,
                    )
                )
            except Exception as exc:
                out.errors.append(f"col_lineage:tree_walk:{dst_col_name}:{exc}")
                self._log.warning(
                    "LineageNode walk failed: file=%s col=%s error=%s",
                    path,
                    dst_col_name,
                    exc,
                )

    _walk(root)
    return edges
```

Add a helper to extract a `TableRef` from a `LineageNode`:

```python
def _lineage_node_to_table_ref(self, node: Any) -> "TableRef | None":
    """Extract a TableRef from a sqlglot LineageNode's source attribute."""
    import sqlglot.expressions as exp

    source = getattr(node, "source", None)
    if source is None:
        return None
    if isinstance(source, exp.Table):
        return TableRef(
            catalog=source.catalog or None,
            db=source.db or None,
            name=source.name,
        )
    # Subquery or CTE — return None (cannot resolve to a concrete table)
    return None
```

**Step 3 — Replace the TODO**:

In `_extract_column_lineage`, replace:

```python
try:
    root = sg_lineage(col_name, body, schema=schema, dialect=self.DIALECT)
    if root:
        # Successfully extracted lineage
        # TODO: convert root to LineageEdge(s)
        self._log.debug(
            "sg_lineage root obtained but conversion not yet "
            "implemented: file=%s col=%s",
            path,
            col_name,
        )
```

with:

```python
try:
    root = sg_lineage(col_name, body, schema=schema, dialect=self.DIALECT)
    if root:
        new_edges = self._lineage_node_to_edges(
            root,
            dst_col_name=col_name,
            dst_table=TableRef(name="<output>"),  # caller must refine if target is known
            path=path,
            out=out,
        )
        edges.extend(new_edges)
        if not new_edges:
            self._log.debug(
                "sg_lineage returned root but no edges emitted: file=%s col=%s",
                path,
                col_name,
            )
```

**Note on `dst_table`**: the destination table is not always known inside
`_extract_column_lineage` — it depends on whether the statement is a `CREATE VIEW AS SELECT`
(destination is the view name), `INSERT INTO t SELECT` (destination is `t`), or bare
`SELECT` (no destination). The caller (`_parse_statement`) has the `target` table in
scope. Pass it through to `_extract_column_lineage` as an additional parameter:

Update the signature:
```python
def _extract_column_lineage(
    self,
    stmt: Any,
    path: Path,
    out: ParsedFile,
    schema: dict,
    dst_table: "TableRef | None" = None,  # NEW
) -> list[LineageEdge]:
```

And update the call in `ansi_parser.py` (from P-02):
```python
column_lineage = self._extract_column_lineage(stmt, path, out, schema, dst_table=target)
```

**No TODO may remain** in the success branch after this ticket. The `# TODO: convert root`
comment must be removed. If the conversion is partial (e.g., subquery sources return None),
that is logged at DEBUG and is not a TODO.

**Wiring verification**:
- `grep -n "TODO" parsers/base.py` must return zero results in `_extract_column_lineage`.
- `grep -n "_lineage_node_to_edges" parsers/base.py` must show both the definition and the
  call site.
- `grep -n "_extract_column_lineage" parsers/ansi_parser.py` must show the updated call
  with `dst_table=target`.

**Files affected**:
- `src/sqlcg/parsers/base.py` — replace TODO with `_lineage_node_to_edges` call; add
  `_lineage_node_to_edges` method; add `_lineage_node_to_table_ref` helper; update
  `_extract_column_lineage` signature
- `src/sqlcg/parsers/ansi_parser.py` — update call to pass `dst_table=target`

**Tests to add**:

Unit tests (`tests/unit/test_lineage_conversion.py`):

- Scenario A — `CREATE VIEW AS SELECT` produces column lineage edges: parse
  `CREATE VIEW revenue AS SELECT amount, customer_id FROM orders` with `AnsiParser`;
  assert `len(out.statements[0].column_lineage) >= 1`; assert at least one edge has
  `confidence >= 0.5`; assert at least one edge's `dst.col_name` is `"amount"` or
  `"customer_id"`.

  This is the primary regression guard: if `column_lineage` is still always `[]`,
  this test fails immediately. The test must assert on the list contents, not just the type.

- Scenario B — edge `dst` table is the view name: parse the same `CREATE VIEW revenue AS
  SELECT amount FROM orders`; assert the edge's `dst.table.name == "revenue"` (the
  destination is the view being created, not an intermediate name).

- Scenario C — INSERT INTO target SELECT sources: parse
  `INSERT INTO dwh.target SELECT id, amount FROM raw.source`; assert at least one edge
  has `dst.table.name == "target"` and `src.table.name == "source"` (or contains "source").

- Scenario D — bare SELECT with no target: parse `SELECT amount FROM orders`; assert
  `column_lineage` is a list (may be empty or contain edges — the key is no exception
  is raised and no TODO is hit).

- Scenario E — `_lineage_node_to_edges` cycle guard: construct a mock `LineageNode` with
  a cycle (node A's upstream contains node A); call `_lineage_node_to_edges` directly;
  assert it terminates (no infinite loop) and returns a list (possibly empty).

Integration tests (`tests/integration/test_column_lineage_e2e.py`):

- Scenario — end-to-end: index a fixture containing `CREATE VIEW v AS SELECT amount, id FROM orders`
  into an in-memory KuzuBackend (after P-01's per-file commit is in place); query
  `MATCH (src:SqlColumn)-[e:COLUMN_LINEAGE]->(dst:SqlColumn) RETURN src.col_name, dst.col_name, e.confidence`;
  assert `len(rows) >= 1`; assert at least one row has `confidence >= 0.5`.

  This is the single most important test in the sprint — it is the test that would have
  caught the zero-column-lineage regression in v0.3.0. Mark it with `@pytest.mark.e2e` or
  place it in `tests/integration/` so it runs in CI.

**Acceptance criteria**:
- `[ ]` `_lineage_node_to_edges` method exists in `SqlParser` and is called from
  `_extract_column_lineage`
- `[ ]` `# TODO: convert root to LineageEdge(s)` is removed from `base.py`
- `[ ]` Parsing `CREATE VIEW AS SELECT col FROM table` produces at least one `LineageEdge`
  with `confidence >= 0.5`
- `[ ]` The `dst_table` parameter is threaded from `_parse_statement` through
  `_extract_column_lineage` to `_lineage_node_to_edges`
- `[ ]` Integration test confirms non-zero `COLUMN_LINEAGE` edges in the graph
- `[ ]` All tests pass

---

## Test Strategy

### The single most important regression guard

Add to `tests/integration/test_column_lineage_e2e.py`:

```python
def test_column_lineage_edges_nonzero_after_index(tmp_path):
    """Guard against the v0.3.0 regression: column lineage was always 0."""
    sql = "CREATE VIEW revenue AS SELECT amount, customer_id FROM orders;"
    (tmp_path / "views.sql").write_text(sql)
    backend = KuzuBackend(":memory:")
    backend.init_schema()
    indexer = Indexer()
    summary = indexer.index_repo(tmp_path, dialect=None, db=backend)
    rows = backend.run_read(
        "MATCH (s:SqlColumn)-[e:COLUMN_LINEAGE]->(d:SqlColumn) RETURN COUNT(e) AS cnt",
        {},
    )
    assert rows[0]["cnt"] > 0, (
        "Column lineage edges must be non-zero after indexing a CREATE VIEW AS SELECT. "
        f"Index summary: {summary}"
    )
```

This test must be committed at the same time as P-02 and P-03. It must NOT be marked
`xfail`. If P-03 is not complete when P-02 merges, this test is expected to fail — that
failure is a signal to complete P-03, not to remove the test.

### Wiring checklist (developer must verify before opening each PR)

For every ticket in this sprint, the developer must answer these questions before opening
a PR. The answers must be documented in the PR description.

| Question | P-04 | P-06 | P-05 | P-01+P-02 | P-03 |
|----------|------|------|------|-----------|------|
| What calls this? | `install_cmd` via `cli/main.py` | `uninstall_cmd` → `_step2_delete_database` | `index_cmd` → `index_repo`; `get_backend` → `KuzuBackend.__init__` | `index_repo` upsert loop; `_parse_statement` | `_extract_column_lineage` |
| Where is the callback/parameter passed? | N/A | N/A | `progress_callback=<fn>` in `index_cmd:69` | `with db.transaction()` wraps each file; `column_lineage=self._extract_column_lineage(...)` | `self._lineage_node_to_edges(root, ...)` |
| What constant/path does this align with? | `shutil.which("sqlcg")` before `"uvx"` | `KuzuConfig.from_env().db_path` == `graph.db` | `progress_callback` parameter in `indexer.py:33` | `db.transaction()` in `reindex_file` is the reference implementation | `sg_lineage()` root in `base.py:394` |
| Does any TODO remain in the happy path? | No | No | No | No | No — TODO must be removed |

---

## Acceptance Criteria (sprint-level)

- `[ ]` `sqlcg index` on a ~1,200-file corpus does not OOM on a 3.8 GiB RAM machine with
  default KuzuDB settings
- `[ ]` `sqlcg index --buffer-pool-size 256` passes 256 MB to KuzuDB's buffer pool
- `[ ]` After indexing a file containing `CREATE VIEW v AS SELECT col FROM table`,
  `db info` shows `COLUMN_LINEAGE edges: >= 1`
- `[ ]` `sqlcg install` after `uv tool install sql-code-graph` updates the MCP entry
  from `uvx` to `sqlcg` (not "Already configured")
- `[ ]` `sqlcg uninstall` (with no env vars set) prompts to delete `~/.sqlcg/graph.db`,
  not `~/.sqlcg/kuzu.db`
- `[ ]` `sqlcg index` on 100+ files prints a `\rIndexed N/total files...` progress line
- `[ ]` Attempting `sqlcg db info` while `sqlcg index` is running prints
  "Database is locked — another sqlcg process is running (PID NNN)"
- `[ ]` The regression test `test_column_lineage_edges_nonzero_after_index` passes

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `sqlglot.lineage.Node` attribute names differ from documented | MEDIUM | Developer must inspect with `inspect.getsource(lineage.Node)` before implementing P-03; attribute names must be documented in a code comment |
| Per-file transactions slow indexing significantly | LOW | Benchmark before and after on a 200-file corpus; if > 2x slower, switch to configurable batch commits (every N files, default N=50). Document the benchmark result in the PR. |
| `lsof` not available on Windows or in some containers | MEDIUM | `_find_lock_holder` gracefully returns "PID unknown (lsof not available)" when `shutil.which("lsof")` returns None — this path is explicitly tested in P-05b Scenario C |
| P-03 tree-walker emits duplicate edges (same src/dst) | LOW | Deduplicate on `(src.full_id, dst.full_id)` before extending `edges` list — same pattern as `_deduplicate_table_refs` |
| `SQLCG_BUFFER_POOL_MB` env var not cleared between test runs | LOW | Tests that set env vars must use `monkeypatch.setenv` / `os.environ` with teardown, not direct `os.environ["KEY"] = value` assignment |
