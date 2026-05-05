# Feature Plan: Sprint Next — v0.3.0 Implementation

Review date: 2026-05-05
Author: architect-planner
Source authority: ARCHITECTURE_REVIEW.md sections 10.1–10.2.10, 10.B, 10.C, 8.2 comment 7, 4 (ranked improvements)

Policy: No backward compatibility. Breaking changes to MCP models, graph schema, MetricsStore
SQLite schema, and `.sqlcg.toml` format are acceptable without deprecation cycles. Re-index is
the migration path for graph schema changes.

---

## Summary

This plan covers all open work from the ARCHITECTURE_REVIEW.md as of 2026-05-05. It groups 13
ranked items from section 10.4 plus the three architectural improvements from section 4 (findings
3.1, 3.2, 3.4) and the parser refactor from section 8.2 comment 7 into 12 discrete tickets.
Dependencies are mapped explicitly. Each ticket is sized to fit in a single PR.

---

## Scope

### In Scope

All open items from the architecture review:
- ARCHITECTURE_REVIEW.md section 10.4 ranks 1–13 (issues #5 and #6 remediation)
- Section 4 findings 3.1, 3.2, 3.4 (atomicity, file re-open safety, exception recording)
- Section 8.2 comment 7 (`_classify` match/case + `exp.DML` refactor)
- Section 10.2.7 (tokenizer-based DML extraction, M effort)
- Section 10.2.4 (medallion-aware `analyze unused`)
- Section 10.B (hint field in result models)
- Section 10.C (parse_quality breakdown)

### Non-Goals

- `add_information_schema` (deferred to v2, finding 3.8 — raise `NotImplementedError` is already in code)
- `wizard.py` (no spec, confirmed scope creep)
- Cross-session temp table chains (fundamental static-analysis limit, section 6.4)
- BigQuery procedure body full parsing (fundamental limit, regex fallback is the defined approach)
- T-SQL procedure body parsing (not yet verified, out of scope)
- KùzuDB cluster mode
- DataHub pipeline integration

---

## Ticket Table

| Ticket | Title | Files Touched | Effort | Impact | Dependency | Blocks |
|--------|-------|---------------|--------|--------|------------|--------|
| T-01 | Add QUICK START block to CLI help | `cli/main.py`, `server/exceptions.py`, `server/tools.py` | XS | CRITICAL | INDEPENDENT | NONE |
| T-02 | Add db info health-check warnings | `cli/commands/db.py`, `server/models.py`, `server/tools.py` | XS | HIGH | INDEPENDENT | T-09 |
| T-03 | Implement sqlcg uninstall command | `cli/commands/uninstall.py` (new), `cli/main.py` | S | HIGH | INDEPENDENT | NONE |
| T-04 | Fix atomicity: implement transaction() in both backends | `core/kuzu_backend.py`, `core/neo4j_backend.py`, `core/graph_db.py` | S | HIGH | INDEPENDENT | NONE |
| T-05 | Fix resolve_pass2 file re-open safety | `lineage/aggregator.py` | XS | HIGH | INDEPENDENT | NONE |
| T-06 | Fix _extract_column_lineage exception recording | `parsers/base.py` | XS | HIGH | INDEPENDENT | T-10 |
| T-07 | Add hint field to empty result models | `server/models.py`, `server/tools.py` | S | HIGH | INDEPENDENT | NONE |
| T-08 | Add index progress output and edges warning | `indexer/indexer.py`, `cli/commands/index.py` | S | HIGH | INDEPENDENT | T-09 |
| T-09 | Introduce parse_quality breakdown | `parsers/base.py`, `indexer/indexer.py`, `cli/commands/db.py`, `cli/commands/index.py` | M | MEDIUM | DEPENDS ON T-06 | NONE |
| T-10 | Verify and fix SELECTS_FROM edges for INSERT-SELECT | `indexer/indexer.py`, `parsers/base.py`, `parsers/ansi_parser.py` | M | HIGH | DEPENDS ON T-06 | NONE |
| T-11 | Rewrite scripting-block DML extraction | `parsers/snowflake_parser.py` | M | HIGH | DEPENDS ON T-06 | NONE |
| T-12 | Medallion-aware analyze unused + .sqlcg.toml write | `cli/commands/analyze.py`, `cli/commands/index.py`, `core/config.py` | S | MEDIUM | INDEPENDENT | NONE |
| T-13 | Add FN label and execute_cypher ratio to gain | `server/tools.py`, `metrics/store.py`, `cli/commands/gain.py` | S | MEDIUM | INDEPENDENT | NONE |
| T-14 | Binary/package name note and uvx cold-start docs | `server/tools.py`, `cli/commands/mcp.py`, `README.md` | XS | LOW | INDEPENDENT | NONE |

---

## Ticket Specifications

---

### T-01 — Add QUICK START block to CLI help

**Source**: ARCHITECTURE_REVIEW.md 10.2.2 (rank 1, CRITICAL), 10.2.1 (rank 12, LOW — combined)

**What to do**:

1. In `cli/main.py`, change the `Typer(help=...)` string to include a `QUICK START` section
   at the top, listing the three required steps in numbered order:
   ```
   QUICK START:
     1. sqlcg db init
     2. sqlcg index <path> --dialect snowflake
     3. sqlcg git install-hooks
   Note: Binary is `sqlcg`; PyPI package is `sql-code-graph`.
   ```

2. In `server/exceptions.py`, update the `NotIndexedError` message to:
   `"No repos indexed. Run 'sqlcg db init' then 'sqlcg index <path>' first."`
   Confirm the message includes the binary name `sqlcg`, not the package name.

3. In `server/tools.py`, add a one-line note to the `index_repo` and `list_dialects_and_repos`
   docstrings: "Binary is `sqlcg`; PyPI package is `sql-code-graph`."

4. In `cli/commands/mcp.py`, add the same one-line binary/package note to the `setup`
   command output (print it after writing the JSON).

**Files affected**:
- `src/sqlcg/cli/main.py` — Typer help string
- `src/sqlcg/server/exceptions.py` — NotIndexedError message
- `src/sqlcg/server/tools.py` — `index_repo`, `list_dialects_and_repos` docstrings
- `src/sqlcg/cli/commands/mcp.py` — setup output

**Tests to add**:
- `tests/unit/test_cli_help.py`: assert `sqlcg --help` output contains "QUICK START", "sqlcg db init", "sqlcg index", "sqlcg git install-hooks"
- `tests/unit/test_exceptions.py`: assert `NotIndexedError` message contains "sqlcg db init" and "sqlcg index"

**Definition of done**:
- `sqlcg --help` displays the QUICK START block with all three steps in order
- `sqlcg --help` contains "Binary is `sqlcg`; PyPI package is `sql-code-graph`"
- `NotIndexedError` message names both commands with their correct syntax
- `index_repo` and `list_dialects_and_repos` tool docstrings name the binary

---

### T-02 — Add db info health-check warnings

**Source**: ARCHITECTURE_REVIEW.md 10.2.3 (rank 2, HIGH), 10.B (partially addressed here)

**What to do**:

1. In `cli/commands/db.py` `db_info()`, after printing node counts, add a structured health
   check section:
   - If `Repo == 0`: print red error "Database is empty. Run 'sqlcg db init' and 'sqlcg index <path>' first."
   - If `Repo > 0` and `SqlQuery == 0`: print yellow warning "No queries indexed. Run 'sqlcg index <path>' to populate the graph."
   - If `SqlQuery > 0` and `SqlColumn == 0`: print yellow warning "Column lineage not available. Tools trace_column_lineage, get_downstream_dependencies, and get_upstream_dependencies will return empty results."
   - After the warning block, print a `edges:` count field explicitly (query `MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count`).

2. In `server/models.py`, add `warnings: list[str]` field to `DialectRepoResult`:
   ```python
   warnings: list[str] = Field(default_factory=list, description="Health warnings about the indexed graph state")
   ```

3. In `server/tools.py` `list_dialects_and_repos()`, after building `repos`, append
   health-check warnings to `DialectRepoResult.warnings` using the same logic as the CLI:
   count `SqlColumn` nodes and append a diagnostic string if zero.

**Files affected**:
- `src/sqlcg/cli/commands/db.py` — `db_info()` health check section
- `src/sqlcg/server/models.py` — `DialectRepoResult.warnings`
- `src/sqlcg/server/tools.py` — `list_dialects_and_repos()` warnings population

**Tests to add**:
- `tests/unit/test_db_info.py`: with a mock backend returning zero counts for each label,
  assert the correct warning string is printed
- `tests/unit/test_tools.py`: assert `list_dialects_and_repos()` populates `warnings`
  when `SqlColumn == 0`

**Definition of done**:
- `sqlcg db info` on an empty database prints a red "Database is empty" message
- `sqlcg db info` on an indexed-but-no-column-lineage graph prints the yellow column warning
- `sqlcg db info` always prints an `edges:` count line
- `list_dialects_and_repos` MCP tool populates `warnings` when column lineage is absent

---

### T-03 — Implement sqlcg uninstall command

**Source**: ARCHITECTURE_REVIEW.md 10.1, 10.A, 10.6 Q1 (rank 3, HIGH)

**What to do**:

Create `src/sqlcg/cli/commands/uninstall.py` implementing `uninstall_cmd` with this exact
three-step interaction contract (resolved in section 10.6 Q1):

Step 1 — always remove `mcpServers["sql-code-graph"]` from `~/.claude/settings.json`:
- Read the file atomically (same JSON load as `install.py`)
- Pop the `sql-code-graph` key from `mcpServers`
- Write back via `.tmp` + `os.replace` (same pattern as `install.py`)
- If key was not present, print "MCP entry not found — already removed"
- Print "Removed MCP registration from ~/.claude/settings.json"

Step 2 — offer to delete the local KùzuDB:
- Only if `SQLCG_BACKEND` is `kuzu` (or unset, defaulting to kuzu) AND the backend is NOT Neo4j
- If `--keep-db` flag: skip this step entirely and print "Keeping database at <path>"
- If `--force` flag: delete without prompting
- Otherwise: print prompt "This will delete the graph database at <path>. Continue? [y/N]"
  and wait for stdin input (use `typer.confirm` or read one line)
- Deletion: `shutil.rmtree(db_path, ignore_errors=True)` for the graph DB directory
- If `--force`: also delete `~/.sqlcg/metrics.db` (or the metrics file alongside the graph DB)
- Print "Deleted graph database at <path>"

Step 3 — remove the git hook sentinel block:
- Target: `.git/hooks/post-checkout` in `Path.cwd()` (or `--repo <path>` if provided)
- Read the file if it exists; strip the `# sqlcg post-checkout hook` sentinel block
  (from that sentinel comment through the next blank line or end-of-block marker)
- If the hook file becomes empty after stripping, delete it
- If the hook file does not exist, print "No git hook found in <path>"
- Print "Removed git hook from <path>/.git/hooks/post-checkout"

Options:
- `--keep-db`: skip DB deletion (step 2)
- `--force`: skip DB deletion prompt; also delete metrics store
- `--repo <path>`: target directory for git hook removal (default: cwd)

Register in `cli/main.py`:
```python
app.command("uninstall")(uninstall.uninstall_cmd)
```

**Files affected**:
- `src/sqlcg/cli/commands/uninstall.py` (new file)
- `src/sqlcg/cli/main.py` — add import and register

**Tests to add**:
- `tests/unit/test_uninstall.py`:
  - MCP entry removal: write a fake settings.json with the key, call `uninstall_cmd`, assert key gone
  - MCP entry missing: call `uninstall_cmd` on settings.json without the key, assert no error
  - `--keep-db`: assert DB path is not deleted
  - `--force`: assert DB path and metrics store are deleted without prompt
  - Git hook removal: write a fake `.git/hooks/post-checkout` with the sentinel block,
    assert the block is stripped; if no other content remains, assert file is deleted
  - Git hook file absent: assert no error
  - All tests use `tmp_path` — no test may write to `~/.claude/` or `~/.sqlcg/`

**Definition of done**:
- `sqlcg uninstall` removes the MCP registration, prompts for DB deletion, removes git hook
- `sqlcg uninstall --keep-db` skips DB deletion
- `sqlcg uninstall --force` deletes DB and metrics store without prompting
- Each step prints a confirmation line
- All 10+ unit tests pass with `tmp_path` fixtures

---

### T-04 — Fix atomicity: implement transaction() in both backends

**Source**: ARCHITECTURE_REVIEW.md finding 3.1 (rank 1 in section 4, HIGH), 8.2 comment 4

**What to do**:

The current `GraphBackend.transaction()` base method in `core/graph_db.py` yields `self` and
does nothing. Both concrete backends must implement real transactions.

1. In `core/graph_db.py`, update the `transaction()` abstract method docstring to require
   subclass override. Consider marking it `@abstractmethod` with a `contextmanager` that
   subclasses must wrap.

2. In `core/kuzu_backend.py`, implement `transaction()` using KùzuDB's connection-level API:
   ```python
   @contextmanager
   def transaction(self):
       self._conn.begin()
       try:
           yield self
           self._conn.commit()
       except Exception:
           self._conn.rollback()
           raise
   ```
   Verify the exact API method names against the installed `kuzu` package version.

3. In `core/neo4j_backend.py`, implement `transaction()` using Neo4j's explicit transaction:
   ```python
   @contextmanager
   def transaction(self):
       with self._driver.session() as session:
           with session.begin_transaction() as tx:
               try:
                   yield self
                   tx.commit()
               except Exception:
                   tx.rollback()
                   raise
   ```

4. In `core/kuzu_backend.py` `init_schema()`, wrap the entire DDL execution loop in a
   transaction (finding 8.2 comment 4 — schema init should also be atomic).

**Files affected**:
- `src/sqlcg/core/graph_db.py` — `transaction()` docstring/contract
- `src/sqlcg/core/kuzu_backend.py` — `transaction()` implementation, `init_schema()` wrap
- `src/sqlcg/core/neo4j_backend.py` — `transaction()` implementation

**Tests to add**:
- `tests/unit/test_kuzu_backend.py`: inject a failure mid-index (raise inside `_upsert_parsed_file`),
  assert the graph is in the pre-delete state (no orphaned nodes)
- `tests/unit/test_transaction.py`: test that a failed `transaction()` rolls back by verifying
  node count before and after a failed upsert sequence

**Definition of done**:
- `KuzuBackend.transaction()` and `Neo4jBackend.transaction()` are real transaction wrappers
- A failed re-index (simulated exception) leaves the graph with the pre-existing state
- `init_schema()` on KuzuBackend is wrapped in a transaction

---

### T-05 — Fix resolve_pass2 file re-open safety

**Source**: ARCHITECTURE_REVIEW.md finding 3.2 (rank 3 in section 4, HIGH)

**What to do**:

In `lineage/aggregator.py`, the `resolve_pass2` method re-reads the file from disk. If the
file has been deleted, moved, or is unreadable between pass 1 and pass 2, it raises an
unhandled `FileNotFoundError`.

Wrap the `open()` call in `try/except (FileNotFoundError, OSError)` and return the pass-1
`ParsedFile` unchanged on failure:

```python
def resolve_pass2(self, parser, parsed: ParsedFile) -> ParsedFile:
    parser.schema_resolver.add_view_sources(self.sources)
    try:
        sql = Path(parsed.path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
            parsed.path,
            exc,
        )
        return parsed
    return parser.parse_file(parsed.path, sql)
```

**Files affected**:
- `src/sqlcg/lineage/aggregator.py` — `resolve_pass2()`

**Tests to add**:
- `tests/unit/test_aggregator.py`: create a temp file, run pass 1, delete the file, call
  `resolve_pass2`, assert it returns the pass-1 result and logs a warning (use `caplog`)

**Definition of done**:
- A deleted or unreadable file during pass 2 logs a warning and returns the pass-1 result
- No `FileNotFoundError` propagates to the caller
- Unit test confirms the warning is emitted

---

### T-06 — Fix _extract_column_lineage exception recording

**Source**: ARCHITECTURE_REVIEW.md finding 3.4 (rank 2 in section 4, HIGH), finding 10.C (amplification)

**What to do**:

The current `_extract_column_lineage` in `parsers/base.py` already logs warnings and appends
to `ParsedFile.errors` (the architecture review found this to already be partially implemented
in the code). However, the fix is incomplete: inspect the current implementation carefully and
verify that:

1. The `except Exception as exc` block around `sg_lineage()` call:
   - Logs at WARNING with file path, column name, and exception
   - Appends to `out.errors` as `f"col_lineage:{col_name}:{exc}"`
   - Emits a zero-confidence `LineageEdge` placeholder

2. The outer `except Exception` block (entire statement failure):
   - Logs at WARNING
   - Appends to `out.errors` as `f"col_lineage:statement:{exc}"`

3. The `# TODO: convert root to LineageEdge(s)` placeholder in the `SELECT` body path
   is a known limitation (lineage conversion not yet implemented); add a `logger.debug`
   when `root` is returned from `sg_lineage()` but is not yet converted.

These are the only changes for this ticket. The full lineage-to-edge conversion is NOT in
scope for this ticket (it is a separate feature requiring deeper design work).

**Also**: the `parse_quality` breakdown (T-09) depends on T-06 being complete first because
T-09 uses the error list populated by T-06 to determine quality categories.

**Files affected**:
- `src/sqlcg/parsers/base.py` — `_extract_column_lineage()` — verify all three points above

**Tests to add**:
- `tests/unit/test_base_parser.py`: call `_extract_column_lineage` with a statement that
  causes `sg_lineage` to raise; assert `out.errors` is non-empty, the log warning is emitted,
  and a zero-confidence edge is in the returned list

**Definition of done**:
- Column lineage failures append to `ParsedFile.errors` with a structured key
- A zero-confidence edge is returned (not silent skip)
- The WARNING log includes file path, column name, and exception text
- T-09 is unblocked

---

### T-07 — Add hint field to empty result models

**Source**: ARCHITECTURE_REVIEW.md finding 10.B (rank 5, HIGH)

**What to do**:

1. In `server/models.py`, add `hint: str | None = None` field to:
   - `LineageResult`
   - `DependencyResult`
   - `TableUsageResult`
   - `SqlPatternResult`

   Field description: `"Diagnostic hint when result list is empty. Explains the likely cause and suggests a next step."`

2. In `server/tools.py`, populate `hint` when the result list (`lineage`, `nodes`,
   `usages`, `matches`) is empty. Use these exact diagnostic strings:

   For `trace_column_lineage` and `get_upstream_dependencies` / `get_downstream_dependencies`
   when `lineage` / `nodes` is empty:
   ```
   "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0. If SqlColumn is 0, column lineage was not extracted — check parse errors. Submit feedback with submit_feedback tool if this was a false negative."
   ```

   For `find_table_usages` when `usages` is empty:
   ```
   "No usages found for this table. The table may not be referenced by any indexed SQL file, or it may be consumed externally (BI tools, APIs). Run 'analyze impact <table>' from the CLI to cross-check."
   ```

   For `search_sql_pattern` when `matches` is empty:
   ```
   "No matches found. Try a shorter or partial pattern. Pattern matching is case-sensitive substring search."
   ```

**Files affected**:
- `src/sqlcg/server/models.py` — add `hint` field to four models
- `src/sqlcg/server/tools.py` — populate `hint` in four tools on empty result

**Tests to add**:
- `tests/unit/test_tools_hints.py`: mock backend to return empty rows for each tool;
  assert `result.hint is not None` and contains the expected diagnostic phrase

**Definition of done**:
- `LineageResult`, `DependencyResult`, `TableUsageResult`, `SqlPatternResult` all have `hint: str | None`
- When the result list is empty, `hint` is populated with a diagnostic string
- When the result list is non-empty, `hint` is `None`
- Unit tests verify hint content for each tool

---

### T-08 — Add index progress output and edges warning

**Source**: ARCHITECTURE_REVIEW.md 10.2.9 (rank 4, HIGH)

**What to do**:

1. In `indexer/indexer.py` `index_repo()`, add periodic progress output. After every 100
   files parsed in the pass-1 loop, call a progress callback or emit a structured log event.
   The implementation should accept an optional `progress_callback: Callable[[int, int], None] | None`
   parameter (current_count, total_count) so tests can inject a mock without touching
   stdout.

2. In `cli/commands/index.py` `index_cmd()`, inject a progress callback that calls
   `console.print(f"\r  Indexed {n}/{total}...", end="")` using `rich`. Use carriage return
   to overwrite the same line. After the loop completes, print a newline to clear the
   carriage return.

3. After `indexer.index_repo()` returns, in `index_cmd()`, if `summary['lineage_edges_created'] == 0`:
   print a yellow warning to stdout:
   "Warning: 0 lineage edges created. Column-level lineage tracing will not be available. Check parse errors above."

4. In `cli/commands/db.py` `db_info()`, add an `edges` count line:
   - Query: `MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count`
   - Print: `  COLUMN_LINEAGE edges: {count}`
   - This is additive to the health warnings from T-02.

**Files affected**:
- `src/sqlcg/indexer/indexer.py` — `index_repo()` progress callback parameter
- `src/sqlcg/cli/commands/index.py` — progress output, edges=0 warning
- `src/sqlcg/cli/commands/db.py` — `COLUMN_LINEAGE edges` count line

**Tests to add**:
- `tests/unit/test_indexer_progress.py`: call `index_repo` with a mock progress callback,
  assert the callback is called with `(100, total)` after 100 files
- `tests/unit/test_index_cmd.py`: assert `edges=0` warning is printed on stdout when
  `lineage_edges_created == 0`

**Definition of done**:
- `sqlcg index` on 1457 files prints progress every 100 files
- A zero-edges result prints a yellow warning to stdout (not just the log)
- `sqlcg db info` shows the `COLUMN_LINEAGE edges` count

---

### T-09 — Introduce parse_quality breakdown

**Source**: ARCHITECTURE_REVIEW.md finding 10.C (rank 11, MEDIUM)

**Depends on**: T-06 (must be complete so `ParsedFile.errors` is reliably populated)

**What to do**:

1. In `parsers/base.py`, add a `ParseQuality` `StrEnum`:
   ```python
   class ParseQuality(StrEnum):
       FULL = "full"           # sqlglot parsed fully, column lineage extracted
       TABLE_ONLY = "table_only"  # tables extracted, column lineage not available
       SCRIPTING_FALLBACK = "scripting_fallback"  # regex extraction, confidence 0.3
       FAILED = "failed"       # unhandled exception, no lineage
   ```

2. In `parsers/base.py`, add `parse_quality: ParseQuality = ParseQuality.TABLE_ONLY` field
   to `ParsedFile`. The default is `TABLE_ONLY` (most common case — tables extracted but
   no column lineage yet because the sg_lineage→edge conversion is incomplete).

3. In `parsers/base.py` `SqlParser`, where a file parses successfully with column lineage
   edges, set `parse_quality = ParseQuality.FULL`. In `snowflake_parser.py`
   `_parse_scripting_file`, set `parse_quality = ParseQuality.SCRIPTING_FALLBACK`. In the
   timeout/exception handler in `indexer.py`, set `parse_quality = ParseQuality.FAILED`.

4. In `indexer/indexer.py` `index_repo()`, collect quality counts. Return them in the
   summary dict:
   ```python
   {
       "files_parsed": ...,
       "parse_errors": ...,
       "tables_found": ...,
       "lineage_edges_created": ...,
       "quality": {
           "full": N,
           "table_only": N,
           "scripting_fallback": N,
           "failed": N,
       }
   }
   ```

5. In `cli/commands/index.py` `index_cmd()`, print the quality breakdown after the summary
   line (unless `--quiet`):
   ```
   Parse quality: full=N table_only=N scripting_fallback=N failed=N
   ```

6. In `cli/commands/db.py` `db_info()`, add a query to count `SqlQuery` nodes grouped by
   `parsing_mode` property. Display the breakdown.

**Files affected**:
- `src/sqlcg/parsers/base.py` — `ParseQuality` enum, `ParsedFile.parse_quality` field
- `src/sqlcg/parsers/snowflake_parser.py` — set `SCRIPTING_FALLBACK`
- `src/sqlcg/indexer/indexer.py` — collect quality counts, include in return dict
- `src/sqlcg/cli/commands/index.py` — print quality breakdown
- `src/sqlcg/cli/commands/db.py` — query and display parsing_mode distribution

**Tests to add**:
- `tests/unit/test_parse_quality.py`: parse a scripting file, assert `parse_quality == SCRIPTING_FALLBACK`
- `tests/unit/test_indexer_quality.py`: index a fixture directory with mixed files, assert
  quality counts match expected values in the returned summary dict

**Definition of done**:
- `ParseQuality` enum exists in `parsers/base.py`
- `ParsedFile` carries `parse_quality`
- `sqlcg index` prints the four-category quality breakdown
- `sqlcg db info` shows the `parsing_mode` distribution from the graph
- The index summary dict includes the `quality` key

---

### T-10 — Verify and fix SELECTS_FROM edges for INSERT-SELECT

**Source**: ARCHITECTURE_REVIEW.md 10.2.5, 10.6 Q4 (rank 10, elevated to HIGH — confirmed parser bug)

**Depends on**: T-06 (for clean error visibility during investigation)

**What to do**:

This ticket requires investigation before implementation. The symptom: `analyze impact`
returns only DDL files for a table while `find pattern` finds ETL INSERT statements for the
same table. The bug is suspected to be in `_upsert_parsed_file` not creating `SELECTS_FROM`
edges for INSERT statements, or in the parser not extracting `sources` for INSERT statements.

**Investigation steps** (developer must complete before coding):

1. Index a test fixture containing:
   ```sql
   -- tests/fixtures/synthetic/insert_select_impact.sql
   INSERT INTO dwh.target_table
   SELECT t.col_a, c.col_b
   FROM source_schema.source_table t
   JOIN ref_schema.dim_customers c ON t.id = c.id;
   ```

2. Run `execute_cypher("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) RETURN q.id, t.qualified LIMIT 20")`.
   If `source_schema.source_table` appears as a target of `SELECTS_FROM`, the edges are being
   created and the bug is elsewhere (likely query routing in `analyze impact`). If it does not
   appear, the bug is in the parser or `_upsert_parsed_file`.

3. Check `parsers/ansi_parser.py` `_parse_statement()` for INSERT handling: verify that
   `stmt.sources` is populated for `exp.Insert` nodes. sqlglot represents `INSERT INTO t SELECT ...`
   as `exp.Insert` with the SELECT as the body; the sources in the SELECT body must be extracted
   by the scope walker.

**Implementation** (after investigation identifies root cause):

- If the bug is in `_parse_statement`: fix the INSERT source extraction in `ansi_parser.py`.
- If the bug is in `_upsert_parsed_file`: verify the `for src_table in stmt.sources` loop
  also runs for INSERT nodes (it should — the code is dialect-agnostic at that layer).
- If the bug is in the Cypher query in `analyze.py` `impact()`: verify that `SELECTS_FROM`
  is the correct relationship for INSERT-SELECT (it is — `SELECTS_FROM` means "this query
  reads from this table", not "this query selects from").

**Mandatory deliverable**: the test fixture `tests/fixtures/synthetic/insert_select_impact.sql`
and a test that calls `analyze impact` on the indexed fixture and asserts the INSERT file
appears in results.

**Files affected** (expected, pending investigation):
- `src/sqlcg/parsers/ansi_parser.py` — INSERT source extraction
- `tests/fixtures/synthetic/insert_select_impact.sql` (new)
- `tests/unit/test_insert_select_impact.py` (new)

**Tests to add**:
- Index `insert_select_impact.sql`, run the graph query for `SELECTS_FROM` edges,
  assert `source_schema.source_table` is a target of a `SELECTS_FROM` edge
- Also add an intra-file multi-step ETL fixture as described in section 6.5:
  `tests/fixtures/synthetic/etl_chain.sql` with a CTAS followed by INSERT-SELECT,
  assert that both table references appear as `SELECTS_FROM` targets

**Definition of done**:
- `analyze impact <table>` returns the INSERT file when the table is a source in an INSERT-SELECT
- `find pattern <table>` and `analyze impact <table>` return consistent (overlapping) results for the same table
- `tests/fixtures/synthetic/insert_select_impact.sql` exists with the fixture SQL
- Tests pass

---

### T-11 — Rewrite scripting-block DML extraction

**Source**: ARCHITECTURE_REVIEW.md 10.2.7 (rank 9, HIGH, M effort), 3.12 (SUPERSEDED — fixed by this ticket)

**Depends on**: T-06 (for clean error recording during the rewrite)

**What to do**:

Replace the `_EMBEDDED_DML` regex in `SnowflakeParser._parse_scripting_file` with a two-step
sqlglot-native approach. The `_has_scripting_block()` method remains unchanged.

**Step 1 — Tokenizer-based statement splitting**:

```python
def _split_statements(self, sql: str) -> list[str]:
    """Split SQL into statement strings using the sqlglot tokenizer.
    
    Handles semicolons inside string literals correctly — no regex needed.
    """
    from sqlglot.tokens import Tokenizer, TokenType
    toks = Tokenizer.from_dialect(self.DIALECT).tokenize(sql)
    chunks = []
    current = []
    for tok in toks:
        if tok.token_type == TokenType.SEMICOLON:
            chunk = sql[current[0].start:tok.start].strip() if current else ""
            if chunk:
                chunks.append(chunk)
            current = []
        else:
            current.append(tok)
    # Handle final statement without trailing semicolon
    if current:
        chunk = sql[current[0].start:current[-1].end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks
```

Note: use `tok.start` and `tok.end` — check the exact attribute names on the installed
`sqlglot.tokens.Token` class (may be `start`/`end` or `col`/`line`). An alternative is to
collect tokens by position and reassemble from the raw SQL text. Verify against installed version.

**Step 2 — DML classification filter**:

After splitting, parse each chunk and keep only DML/SELECT:

```python
import sqlglot.expressions as exp

def _is_indexable(stmt) -> bool:
    if isinstance(stmt, (exp.Select,)):
        return True
    if isinstance(stmt, exp.DML) and not isinstance(stmt, exp.Copy):
        return True
    if isinstance(stmt, exp.Create):
        # Pass CTAS through — _parse_statement handles it
        return True
    return False
```

Drop `exp.Command`, `exp.DDL` (ALTER, SET, LET, etc.) at DEBUG level, not WARNING.

**Step 3 — Snowflake/Databricks procedure body extraction**:

After the tokenizer split, scan for `exp.Create` nodes that contain an `exp.RawString` body
(the `$$...$$` delimiter). Extract the body string, strip the outer `BEGIN` / `END` wrapper,
then recursively apply the tokenizer split + DML filter to the inner content.

```python
def _extract_procedure_body(self, stmt: exp.Create) -> str | None:
    """Extract the SQL body from a Snowflake/Databricks procedure."""
    body = stmt.find(exp.RawString)
    if body is None:
        return None
    text = body.this  # The raw string content
    # Strip leading/trailing BEGIN...END wrapper
    text = re.sub(r"^\s*BEGIN\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*END\s*$", "", text, flags=re.IGNORECASE)
    return text.strip()
```

**Step 4 — BigQuery fallback**:

For BigQuery (detected when `self.DIALECT == "bigquery"`), `exp.Create` bodies land as
`exp.Command`. Keep the existing `_EMBEDDED_DML` regex as the fallback path, called only for
`exp.Command` nodes with a BigQuery dialect. Do NOT remove the `_EMBEDDED_DML` regex from the
module entirely — it must remain for the BigQuery case.

**Step 5 — Remove the top-level DML regex path**:

The primary path in `_parse_scripting_file` changes from:
```python
dml_matches = _EMBEDDED_DML.finditer(sql)
for match in dml_matches: ...
```
to the tokenizer split + parse + DML filter approach.

Rename `_parse_scripting_file` internals; the public signature stays the same.

**Files affected**:
- `src/sqlcg/parsers/snowflake_parser.py` — rewrite `_parse_scripting_file`, add `_split_statements`, `_extract_procedure_body`, `_is_indexable`; keep `_EMBEDDED_DML` for BigQuery fallback only

**Test fixtures to add**:
- `tests/fixtures/synthetic/snowflake_scripting_noise.sql`:
  A scripting block with `ALTER WAREHOUSE IDENTIFIER($var) SET WAREHOUSE_SIZE = 'X-Large'`,
  `CALL MA.MSSPR_UPDATE()`, `MERGE INTO target USING source ON ...`, `INSERT INTO t SELECT ...`.
  Expected: only MERGE and INSERT are indexed; ALTER and CALL are dropped at DEBUG.
- `tests/fixtures/synthetic/snowflake_procedure.sql`:
  `CREATE OR REPLACE PROCEDURE my_proc() AS $$ BEGIN INSERT INTO t SELECT ... END $$`.
  Expected: INSERT inside the body is indexed.
- `tests/fixtures/synthetic/bigquery_procedure.sql`:
  `CREATE PROCEDURE my_proc() BEGIN INSERT INTO t SELECT ... END`.
  Expected: existing regex fallback still extracts the INSERT.

**Tests to add**:
- `tests/unit/test_snowflake_scripting.py`: parse `snowflake_scripting_noise.sql`, assert
  only MERGE and INSERT appear in `parsed.statements`, ALTER and CALL are absent
- `tests/unit/test_snowflake_procedure.py`: parse `snowflake_procedure.sql`, assert
  INSERT is extracted from the procedure body
- `tests/unit/test_bigquery_fallback.py`: parse `bigquery_procedure.sql` with BigQuery
  dialect, assert INSERT is extracted via regex fallback

**Definition of done**:
- `ALTER WAREHOUSE`, `CALL`, `SET`, `LET`, `RETURN`, and other non-DML statements are
  dropped at DEBUG level, not WARNING, and do not appear in `parsed.statements`
- `MERGE INTO` and `INSERT INTO ... SELECT` inside scripting blocks are indexed
- Snowflake procedure bodies (via `$$...$$`) have their DML extracted and indexed
- BigQuery `exp.Command` bodies continue to use the regex fallback (no regression)
- All three test fixtures produce the expected `parsed.statements` contents

---

### T-12 — Medallion-aware analyze unused and .sqlcg.toml write

**Source**: ARCHITECTURE_REVIEW.md 10.2.4 (rank 7, MEDIUM), 10.2.8 (rank 6, MEDIUM), 10.6 Q2

**What to do**:

This ticket combines two items that both touch `cli/commands/analyze.py` and
`cli/commands/index.py` / `core/config.py` without overlapping change sets. Combining avoids
a merge conflict on `index.py`.

**Part A — Medallion-aware analyze unused** (10.2.4):

1. In `cli/commands/analyze.py` `unused()`:
   - Add `--exclude-schema` option: `exclude_schema: list[str] = typer.Option([], "--exclude-schema", help="Exclude tables in these schemas (e.g. IA_, RPT_). Use for schemas consumed externally by BI tools or APIs.")`
   - After fetching results, classify each `qualified` name by tier using a configurable
     default tier map:

     ```python
     _GOLD_PREFIXES = ("DIM_", "FACT_", "MART_", "IA_", "RPT_", "ODS_")
     _SILVER_PREFIXES = ("INT_", "PREP_", "DWH_", "BA_")
     _BRONZE_PREFIXES = ("RAW_", "STG_", "STAGE_", "LAND_")

     def _infer_tier(qualified: str) -> str:
         name = qualified.split(".")[-1].upper()
         if any(name.startswith(p) for p in _GOLD_PREFIXES):
             return "gold"
         if any(name.startswith(p) for p in _SILVER_PREFIXES):
             return "silver"
         if any(name.startswith(p) for p in _BRONZE_PREFIXES):
             return "bronze"
         return "unknown"
     ```

   - Filter out rows where the schema component (the part before the final `.`) matches any
     `--exclude-schema` value (case-insensitive prefix match).
   - Add a `tier` column to the Rich table output.
   - Always append this caveat line after the table:
     "Note: 'unused' means no SQL file in the indexed corpus selects from this table.
      External consumers (Tableau, BI tools, APIs) are not visible to this tool."

2. Update the `analyze unused --help` text to explain the tier model and when to use
   `--exclude-schema`.

**Part B — Write .sqlcg.toml on successful index** (10.2.8 / 10.6 Q2):

Resolved spec from 10.6 Q2: `path` is optional in `.sqlcg.toml`. If `path == cwd`, omit it.
The file is safe to commit.

1. In `cli/commands/index.py` `index_cmd()`, after a successful index, write `.sqlcg.toml`
   to the root of the indexed path if it does not already exist with the same dialect:

   ```python
   config_path = path / ".sqlcg.toml"
   new_content = f'[sqlcg]\ndialect = "{dialect}"\n'
   if not config_path.exists() or config_path.read_text() != new_content:
       config_path.write_text(new_content)
   ```

   Do not write a `path` key (per Q2 resolution — defaults to cwd).

2. In `core/config.py` `get_dialect()`, the existing implementation already reads
   `.sqlcg.toml`. No change needed for the read path.

3. In `cli/commands/index.py`, allow `path` argument to be optional: if not provided and
   `.sqlcg.toml` exists in `cwd`, use `cwd` as the path and read the dialect from the toml.
   The `path` argument should default to `None` and resolve to `Path.cwd()` when absent and
   `.sqlcg.toml` is present. If both are absent, print an error and exit 1.

**Files affected**:
- `src/sqlcg/cli/commands/analyze.py` — `unused()` tier classification, `--exclude-schema`, caveat
- `src/sqlcg/cli/commands/index.py` — `.sqlcg.toml` write, optional path argument

**Tests to add**:
- `tests/unit/test_analyze_unused.py`:
  - Mock backend returning a mix of `DIM_PRODUCT`, `BA_ORDERS`, `RAW_EVENTS` as unused tables
  - Assert the Rich table output contains `gold`, `silver`, `bronze` tier labels
  - Assert `--exclude-schema IA` filters out `IA_` tables
  - Assert the caveat line appears in output
- `tests/unit/test_sqlcg_toml.py`:
  - Run `index_cmd` with a tmp_path target, assert `.sqlcg.toml` is written with the correct dialect
  - Run `index_cmd` again, assert the file is not overwritten if content matches
  - Run `index_cmd` with no path argument in a dir containing `.sqlcg.toml`, assert it
    uses the dialect from the file

**Definition of done**:
- `analyze unused` output includes a `tier` column and the closed-world caveat
- `--exclude-schema` filters results by schema prefix
- `sqlcg index` writes `.sqlcg.toml` after a successful index (idempotent)
- `sqlcg index` with no path argument uses `.sqlcg.toml` from cwd

---

### T-13 — Add FN label and execute_cypher ratio to gain

**Source**: ARCHITECTURE_REVIEW.md 10.2.6 (rank 8, MEDIUM), 10.6 Q3

**What to do**:

**Part A — FN label** (10.6 Q3):

1. In `server/tools.py` `submit_feedback()`:
   - Change the valid labels check from `if label not in ("TP", "FP")` to
     `if label not in ("TP", "FP", "FN")`
   - Update the `ValueError` message to list all three labels
   - Update the docstring: add `FN` to the label description:
     `"FN" (false negative — expected a result but got empty)`

2. In `server/tools.py` docstring for `submit_feedback`, add:
   "Use label='FN' when the tool returned empty results but you expected results."

3. No MetricsStore schema change is needed — the `label` column is a free-text field;
   `FN` values are valid as-is.

**Part B — execute_cypher ratio in gain** (10.2.6):

1. In `cli/commands/gain.py` `gain_cmd()`, add a Section E after section D:
   - Query: count total tool calls and count `execute_cypher` calls
   - Compute ratio: `execute_cypher_count / total_calls`
   - If ratio > 0.3, print a yellow warning:
     "High raw-Cypher ratio: {ratio:.0%} of calls are execute_cypher. The high-level tools may not be meeting your needs. Check hint fields on empty results."
   - If ratio <= 0.3, print the ratio without a warning

2. Add this to the `--json` output as `"execute_cypher_ratio"`.

**Files affected**:
- `src/sqlcg/server/tools.py` — `submit_feedback()` label validation and docstring
- `src/sqlcg/cli/commands/gain.py` — Section E, execute_cypher ratio

**Tests to add**:
- `tests/unit/test_submit_feedback.py`: assert `submit_feedback("trace_column_lineage", "t.col", "FN")` does not raise; assert `"TP"`, `"FP"`, `"FN"` are all valid; assert `"XX"` raises `ValueError`
- `tests/unit/test_gain_ratio.py`: mock MetricsStore with 15 `execute_cypher` calls and 5 `trace_column_lineage` calls (ratio = 0.75), assert the warning string is printed; mock with 2 `execute_cypher` calls and 18 others (ratio = 0.1), assert no warning

**Definition of done**:
- `submit_feedback` accepts `"FN"` as a valid label
- `submit_feedback` with `"XX"` raises `ValueError` naming all three valid labels
- `sqlcg gain` prints an `execute_cypher / total_calls` ratio section
- A ratio above 0.3 prints a yellow warning

---

### T-14 — Binary/package name note and uvx cold-start docs

**Source**: ARCHITECTURE_REVIEW.md 10.2.1 (combined into T-01 for code changes), 10.2.10 (rank 13, LOW)

Note: The code changes for T-01 already cover the binary/package name note in tool docstrings
and MCP setup output. T-14 covers the README documentation-only changes that are distinct.

**What to do**:

1. In `README.md`, add or update the `QUICK START` section:
   - Recommend `uv tool install sql-code-graph` for persistent local installs (fastest MCP startup)
   - Reserve `uvx sql-code-graph` for one-shot tries with a note: "First run may take several minutes on a cold cache."
   - Include the ordered three-step workflow (`db init`, `index`, `git install-hooks`)

2. In `cli/commands/install.py` `install_cmd()`, update the confirmation message to include:
   - If `uvx` was chosen: append "Note: Using uvx — first MCP startup may take several minutes on a cold cache. Run 'uv tool install sql-code-graph' for faster startup."

**Files affected**:
- `README.md` — QUICK START, `uv tool install` recommendation, cold-start warning
- `src/sqlcg/cli/commands/install.py` — uvx confirmation message

**Tests to add**:
- `tests/unit/test_install_message.py`: assert the install_cmd output contains the uvx warning
  when `shutil.which("uvx")` returns a path (mock `shutil.which`)

**Definition of done**:
- README QUICK START section recommends `uv tool install` over `uvx` for regular use
- README notes that first `uvx` run may be slow
- `sqlcg install` confirmation message notes cold-start latency when using uvx

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| T-11 tokenizer API mismatch (Token.start/end attribute names) | MEDIUM | Developer must inspect installed `sqlglot.tokens.Token` class before coding; add a compatibility check at the top of the method |
| T-04 KùzuDB transaction API not matching documented pattern | MEDIUM | Check `kuzu` package version; read `conn.begin()` / `conn.commit()` / `conn.rollback()` method names from the live package; add a smoke test before implementing |
| T-10 investigation reveals a deeper architectural issue | LOW | If the root cause cannot be addressed in one PR (e.g., requires a new scope-traversal approach), T-10 should produce a regression test documenting the failure and a follow-up ticket |
| T-09 parse_quality field conflicts with existing `parsing_mode` field on QueryNode | LOW | These are different fields: `parsing_mode` is on `QueryNode` (already persisted to graph); `parse_quality` is on `ParsedFile` (aggregated metadata). No conflict expected. |

---

## Recommended Implementation Order

The following sequence minimizes merge conflicts and unblocks downstream tickets:

```
Week 1 (INDEPENDENT, XS/S):
  T-01 (XS) → T-02 (XS) → T-05 (XS) → T-14 (XS) → T-07 (S) → T-04 (S)

Week 2 (INDEPENDENT, S + foundation for blocked):
  T-03 (S) → T-08 (S) → T-13 (S) → T-06 (XS — clears T-09 and T-11 blocks)

Week 3 (DEPENDS ON T-06, M effort):
  T-12 (S) → T-10 (M) → T-11 (M) → T-09 (M)
```

T-06 is XS in code change but is a blocker for three M-effort tickets. Prioritize it early
in Week 2 so the three M-effort tickets can proceed in parallel if multiple developers are
working.
